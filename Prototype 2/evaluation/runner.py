"""running eval scenarios and dumping metrics, dwell sweep, and charts."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cloud.integrity import IntegrityVerifier
from cloud.store import Store
from common.canonical import canonical_bytes
from evaluation import metrics
from evaluation.scenarios import (
    CONSIGNMENT_ID,
    DEVICE_IDS,
    SAMPLE_INTERVAL_S,
    SCENARIO_IDS,
    InjectionKind,
    ScenarioId,
    ScenarioSpec,
    build_scenario,
    door_is_open,
    network_is_up,
    primary_fault_injection_ts,
    target_temperature_c,
)
from gateway.queue import StoreAndForwardQueue
from gateway.rules import ExcursionStateMachine
from node.simulator import SensorNode

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DWELL_VALUES_S = (60.0, 120.0, 180.0, 240.0)


class StepClock:
    """stepping sim time forward in fixed intervals."""

    def __init__(self, start_epoch: float) -> None:
        self._ts = start_epoch

    def now(self) -> float:
        return self._ts

    def advance(self, sim_seconds: float) -> None:
        self._ts += sim_seconds


@dataclass
class RunResult:
    """holding everything i measured from one scenario run."""

    scenario_id: ScenarioId
    repetition: int
    seed: int
    dwell_s: float
    detection_latency_s: float | None
    alert_precision: float
    alert_recall: float
    disposition_accuracy: float
    data_completeness: float
    sequence_gaps: int
    rejection_rate: float
    false_positive_alerts: int
    final_disposition: str
    injection_records: list[dict[str, object]] = field(default_factory=list)


def _load_device_keys(devices_path: Path) -> dict[str, str]:
    with devices_path.open(encoding="utf-8") as handle:
        devices = yaml.safe_load(handle)
    return {device_id: details["key"] for device_id, details in devices.items()}


def _count_sequence_gaps(conn: sqlite3.Connection, consignment_id: str) -> int:
    rows = conn.execute(
        """
        SELECT device_id, seq
        FROM readings
        WHERE consignment_id = ? AND verified = 1
        ORDER BY device_id ASC, seq ASC
        """,
        (consignment_id,),
    ).fetchall()

    gap_count = 0
    last_by_device: dict[str, int] = {}
    for device_id, seq in rows:
        previous = last_by_device.get(device_id)
        if previous is not None and seq != previous + 1:
            gap_count += 1
        last_by_device[device_id] = int(seq)
    return gap_count


def _sign_payload(payload: dict, key_hex: str) -> str:
    return hmac.new(
        bytes.fromhex(key_hex),
        canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _alert_intervals(
    events: list[tuple[float, str]],
    duration_s: float = SAMPLE_INTERVAL_S,
) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for event_ts, event_type in events:
        if event_type in {"WARNING", "EXCURSION"}:
            intervals.append((event_ts, event_ts + duration_s))
    return intervals


def _insert_verified_reading(store: Store, envelope: dict) -> bool:
    """storing a verified reading without recomputing disposition every time — eval is slow enough already."""
    payload = envelope["payload"]
    try:
        store._conn.execute(
            """
            INSERT INTO readings (
                device_id, consignment_id, seq, ts, temp_c, humidity_pct,
                door_open, lat, lon, mac, verified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["device_id"],
                payload["consignment_id"],
                payload["seq"],
                payload["ts"],
                payload["temp_c"],
                payload["humidity_pct"],
                1 if payload["door_open"] else 0,
                payload["lat"],
                payload["lon"],
                envelope["mac"],
                1,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _inject_adversarial_frames(
    spec: ScenarioSpec,
    keys: dict[str, str],
    verifier: IntegrityVerifier,
    store: Store,
    accepted_samples: list[dict],
    security_log: list[str],
) -> tuple[int, int]:
    """throwing 50 forged + 50 replayed frames at the adversarial timestamp for S5."""
    injected = 0
    rejected = 0
    node_key = keys["NODE-01"]
    adversarial_ts = spec.injections[0].injection_ts

    if not accepted_samples:
        accepted_samples.append(
            {
                "device_id": "NODE-01",
                "consignment_id": CONSIGNMENT_ID,
                "seq": 1,
                "ts": adversarial_ts - SAMPLE_INTERVAL_S,
                "temp_c": 5.23,
                "humidity_pct": 44.1,
                "door_open": False,
                "lat": 6.5854,
                "lon": 79.9607,
            }
        )

    for index in range(50):
        base_payload = dict(accepted_samples[index % len(accepted_samples)])
        forged_payload = dict(base_payload)
        forged_payload["seq"] = 9000 + index
        forged_payload["ts"] = adversarial_ts + index * 0.01
        forged_envelope = {
            "payload": forged_payload,
            "mac": _sign_payload(forged_payload, "ffffffffffffffff"),
        }
        injected += 1
        accepted, reason = verifier.verify(forged_envelope, forged_payload["ts"])
        if accepted:
            _insert_verified_reading(store, forged_envelope)
        else:
            rejected += 1
            if reason is not None:
                security_log.append(reason)

    for index in range(50):
        replay_source = accepted_samples[index % len(accepted_samples)]
        replay_payload = dict(replay_source)
        replay_envelope = {
            "payload": replay_payload,
            "mac": _sign_payload(replay_payload, node_key),
        }
        injected += 1
        accepted, reason = verifier.verify(replay_envelope, replay_payload["ts"])
        if accepted:
            stored = _insert_verified_reading(store, replay_envelope)
            if not stored:
                rejected += 1
                security_log.append("REPLAY")
        else:
            rejected += 1
            if reason is not None:
                security_log.append(reason)

    store._conn.commit()
    return injected, rejected


def simulate_scenario(spec: ScenarioSpec, devices_path: Path, consignments_path: Path) -> RunResult:
    """simulating one scenario in-process and collecting the metrics."""
    keys = _load_device_keys(devices_path)

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "eval.db")
        queue_path = str(Path(temp_dir) / "queue.db")

        store = Store(db_path, str(consignments_path))
        verifier = IntegrityVerifier(keys, accept_window_s=300.0)
        queue = StoreAndForwardQueue(queue_path)
        state_machine = ExcursionStateMachine(
            low_c=2.0,
            high_c=8.0,
            dwell_s=spec.dwell_s,
            recovery_s=300.0,
        )

        clock = StepClock(spec.start_epoch)
        rng = np.random.default_rng(spec.seed)
        nodes: dict[str, SensorNode] = {}
        for device_id in DEVICE_IDS:
            nodes[device_id] = SensorNode(
                device_id=device_id,
                consignment_id=CONSIGNMENT_ID,
                key=keys[device_id],
                clock=clock,
                rng=np.random.default_rng(spec.seed + hash(device_id) % 10_000),
            )

        gateway_events: list[tuple[float, str]] = []
        accepted_samples: list[dict] = []
        security_log: list[str] = []
        generated_readings = 0
        stored_readings = 0
        adversarial_injected = 0
        adversarial_rejected = 0
        adversarial_done = False

        def ingest_envelope(envelope: dict) -> bool:
            nonlocal stored_readings
            payload = envelope["payload"]
            sim_ts = float(payload["ts"])
            accepted, reason = verifier.verify(envelope, sim_ts)
            if not accepted:
                if reason is not None:
                    security_log.append(reason)
                return False

            if not _insert_verified_reading(store, envelope):
                security_log.append("REPLAY")
                return False

            stored_readings += 1
            if payload["device_id"] == "NODE-01":
                accepted_samples.append(dict(payload))
                if len(accepted_samples) > 200:
                    accepted_samples.pop(0)
            return True

        def process_reading(envelope: dict) -> None:
            payload = envelope["payload"]
            temp_c = float(payload["temp_c"])
            sim_ts = float(payload["ts"])

            state_change = state_machine.update(temp_c, sim_ts)
            if state_change is not None:
                gateway_events.append((sim_ts, state_change))
                store._conn.execute(
                    """
                    INSERT INTO events (consignment_id, ts, type, severity, detail)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        CONSIGNMENT_ID,
                        sim_ts,
                        state_change,
                        {"WARNING": "WARN", "EXCURSION": "CRITICAL", "RECOVERED": "INFO"}.get(
                            state_change, "INFO"
                        ),
                        None,
                    ),
                )

            if network_is_up(spec, sim_ts):
                if queue.pending_count() > 0:
                    def publish_fn(item: dict) -> bool:
                        return ingest_envelope(item)

                    queue.flush(publish_fn)
                    for device_id in DEVICE_IDS:
                        row = store._conn.execute(
                            """
                            SELECT MAX(seq) FROM readings
                            WHERE device_id = ? AND verified = 1
                            """,
                            (device_id,),
                        ).fetchone()
                        if row is not None and row[0] is not None:
                            queue.acknowledge(device_id, int(row[0]))
                ingest_envelope(envelope)
            else:
                queue.enqueue(envelope)

        for _step in range(spec.num_steps):
            sim_ts = clock.now()
            target_c = target_temperature_c(spec, sim_ts)
            is_open = door_is_open(spec, sim_ts)

            for device_id in DEVICE_IDS:
                node = nodes[device_id]
                node.set_target(target_c)
                node.set_door(is_open)
                envelope = node.read()
                generated_readings += 1
                process_reading(envelope)

            if (
                spec.scenario_id == "S5"
                and not adversarial_done
                and sim_ts >= spec.injections[0].injection_ts
            ):
                injected, rejected = _inject_adversarial_frames(
                    spec,
                    keys,
                    verifier,
                    store,
                    accepted_samples,
                    security_log,
                )
                adversarial_injected += injected
                adversarial_rejected += rejected
                adversarial_done = True

            clock.advance(SAMPLE_INTERVAL_S)

        store._conn.commit()

        if queue.pending_count() > 0 and network_is_up(spec, clock.now()):
            def final_publish(item: dict) -> bool:
                return ingest_envelope(item)

            queue.flush(final_publish)
            for device_id in DEVICE_IDS:
                row = store._conn.execute(
                    """
                    SELECT MAX(seq) FROM readings
                    WHERE device_id = ? AND verified = 1
                    """,
                    (device_id,),
                ).fetchone()
                if row is not None and row[0] is not None:
                    queue.acknowledge(device_id, int(row[0]))

        store.recompute_disposition(CONSIGNMENT_ID)
        final_row = store._conn.execute(
            "SELECT disposition FROM consignments WHERE id = ?",
            (CONSIGNMENT_ID,),
        ).fetchone()
        final_disposition = str(final_row["disposition"]) if final_row is not None else "RELEASE"

        gap_count = _count_sequence_gaps(store._conn, CONSIGNMENT_ID)
        completeness = metrics.data_completeness(generated_readings, stored_readings, gap_count)

        detected_intervals = _alert_intervals(gateway_events)
        precision, recall = metrics.precision_recall(detected_intervals, spec.truth_windows)

        disposition_acc = metrics.disposition_accuracy(
            [final_disposition],
            [spec.expected_disposition],
        )

        excursion_events = [event_ts for event_ts, event_type in gateway_events if event_type == "EXCURSION"]
        fault_ts = primary_fault_injection_ts(spec)
        latency_s: float | None = None
        if fault_ts is not None and excursion_events:
            latency_s = metrics.detection_latency(fault_ts, min(excursion_events))

        false_positive_alerts = sum(
            1 for _, event_type in gateway_events if event_type in {"WARNING", "EXCURSION"}
        )

        if spec.scenario_id == "S5":
            reject_rate = metrics.rejection_rate(adversarial_injected, adversarial_rejected)
        else:
            reject_rate = 1.0

        injection_records = [
            {
                "kind": record.kind.value,
                "injection_ts": record.injection_ts,
                "duration_s": record.duration_s,
                "detail": record.detail,
            }
            for record in spec.injections
        ]

        store.close()
        queue.close()

    return RunResult(
        scenario_id=spec.scenario_id,
        repetition=0,
        seed=spec.seed,
        dwell_s=spec.dwell_s,
        detection_latency_s=latency_s,
        alert_precision=precision,
        alert_recall=recall,
        disposition_accuracy=disposition_acc,
        data_completeness=completeness,
        sequence_gaps=gap_count,
        rejection_rate=reject_rate,
        false_positive_alerts=false_positive_alerts,
        final_disposition=final_disposition,
        injection_records=injection_records,
    )


def _seed_for(scenario_id: ScenarioId, repetition: int, base_seed: int) -> int:
    scenario_index = SCENARIO_IDS.index(scenario_id)
    return base_seed + scenario_index * 10_000 + repetition * 101


def run_all_scenarios(
    repetitions: int,
    dwell_s: float,
    base_seed: int,
    devices_path: Path,
    consignments_path: Path,
) -> list[RunResult]:
    results: list[RunResult] = []
    for scenario_id in SCENARIO_IDS:
        for repetition in range(repetitions):
            seed = _seed_for(scenario_id, repetition, base_seed)
            logger.info(
                "Running %s repetition %s/%s seed=%s dwell_s=%.0f",
                scenario_id,
                repetition + 1,
                repetitions,
                seed,
                dwell_s,
            )
            spec = build_scenario(scenario_id, seed=seed, dwell_s=dwell_s)
            result = simulate_scenario(spec, devices_path, consignments_path)
            result.repetition = repetition
            results.append(result)
    return results


def run_dwell_sweep(
    repetitions: int,
    base_seed: int,
    devices_path: Path,
    consignments_path: Path,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for dwell_s in DWELL_VALUES_S:
        false_positive_flags: list[float] = []
        latencies_s: list[float] = []

        for repetition in range(repetitions):
            seed = _seed_for("S1", repetition, base_seed)
            s1_spec = build_scenario("S1", seed=seed, dwell_s=dwell_s)
            s1_result = simulate_scenario(s1_spec, devices_path, consignments_path)
            had_false_positive = 1.0 if s1_result.false_positive_alerts > 0 else 0.0
            false_positive_flags.append(had_false_positive)
            logger.info(
                "Dwell sweep S1 dwell_s=%.0f rep=%s seed=%s false_positive=%s",
                dwell_s,
                repetition + 1,
                seed,
                had_false_positive,
            )

            s3_seed = _seed_for("S3", repetition, base_seed)
            s3_spec = build_scenario("S3", seed=s3_seed, dwell_s=dwell_s)
            s3_result = simulate_scenario(s3_spec, devices_path, consignments_path)
            if s3_result.detection_latency_s is not None:
                latencies_s.append(s3_result.detection_latency_s)
            logger.info(
                "Dwell sweep S3 dwell_s=%.0f rep=%s seed=%s latency_s=%s",
                dwell_s,
                repetition + 1,
                s3_seed,
                s3_result.detection_latency_s,
            )

        false_positive_rate = float(np.mean(false_positive_flags)) if false_positive_flags else 0.0
        mean_latency_s = float(np.mean(latencies_s)) if latencies_s else float("nan")
        std_latency_s = float(np.std(latencies_s)) if latencies_s else float("nan")

        rows.append(
            {
                "dwell_s": dwell_s,
                "false_positive_rate": false_positive_rate,
                "mean_detection_latency_s": mean_latency_s,
                "std_detection_latency_s": std_latency_s,
                "repetitions": repetitions,
            }
        )

    return pd.DataFrame(rows)


def _results_to_dataframe(results: list[RunResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for result in results:
        rows.append(
            {
                "scenario": result.scenario_id,
                "repetition": result.repetition,
                "seed": result.seed,
                "dwell_s": result.dwell_s,
                "detection_latency_s": result.detection_latency_s,
                "alert_precision": result.alert_precision,
                "alert_recall": result.alert_recall,
                "disposition_accuracy": result.disposition_accuracy,
                "data_completeness": result.data_completeness,
                "sequence_gaps": result.sequence_gaps,
                "rejection_rate": result.rejection_rate,
                "false_positive_alerts": result.false_positive_alerts,
                "final_disposition": result.final_disposition,
                "injection_records_json": json.dumps(result.injection_records),
            }
        )
    return pd.DataFrame(rows)


def _plot_results(metrics_df: pd.DataFrame, dwell_df: pd.DataFrame, output_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    scenario_order = list(SCENARIO_IDS)
    latency_means: list[float] = []
    latency_stds: list[float] = []
    for scenario_id in scenario_order:
        subset = metrics_df[metrics_df["scenario"] == scenario_id]
        values = subset["detection_latency_s"].dropna()
        if len(values) == 0:
            latency_means.append(0.0)
            latency_stds.append(0.0)
        else:
            latency_means.append(float(values.mean()))
            latency_stds.append(float(values.std()))

    axes[0].bar(
        scenario_order,
        latency_means,
        yerr=latency_stds,
        capsize=4,
        color="#2c6e9d",
        edgecolor="black",
    )
    axes[0].set_xlabel("Scenario")
    axes[0].set_ylabel("Detection latency (simulated seconds)")
    axes[0].set_title("Detection latency by scenario")
    axes[0].grid(axis="y", linestyle="--", alpha=0.4)

    axes[1].plot(
        dwell_df["mean_detection_latency_s"],
        dwell_df["false_positive_rate"],
        marker="o",
        linewidth=2,
        color="#b33f3f",
    )
    for _, row in dwell_df.iterrows():
        axes[1].annotate(
            f"{int(row['dwell_s'])} s",
            (row["mean_detection_latency_s"], row["false_positive_rate"]),
            textcoords="offset points",
            xytext=(6, 4),
            fontsize=9,
        )
    axes[1].set_xlabel("Mean detection latency (simulated seconds)")
    axes[1].set_ylabel("False-positive rate")
    axes[1].set_title("Dwell trade-off: false positives vs latency")
    axes[1].grid(True, linestyle="--", alpha=0.4)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ColdChainGuard evaluation runner")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--dwell-s", type=float, default=120.0)
    parser.add_argument("--base-seed", type=int, default=2026)
    parser.add_argument("--devices", type=Path, default=_REPO_ROOT / "config" / "devices.yaml")
    parser.add_argument(
        "--consignments",
        type=Path,
        default=_REPO_ROOT / "config" / "consignments.yaml",
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    args.results_dir.mkdir(parents=True, exist_ok=True)

    results = run_all_scenarios(
        repetitions=args.repetitions,
        dwell_s=args.dwell_s,
        base_seed=args.base_seed,
        devices_path=args.devices,
        consignments_path=args.consignments,
    )
    metrics_df = _results_to_dataframe(results)
    metrics_path = args.results_dir / "metrics.csv"
    metrics_df.to_csv(metrics_path, index=False)
    logger.info("Wrote %s (%s rows)", metrics_path, len(metrics_df))

    dwell_df = run_dwell_sweep(
        repetitions=args.repetitions,
        base_seed=args.base_seed,
        devices_path=args.devices,
        consignments_path=args.consignments,
    )
    dwell_path = args.results_dir / "dwell_sweep.csv"
    dwell_df.to_csv(dwell_path, index=False)
    logger.info("Wrote %s", dwell_path)

    chart_path = args.results_dir / "05_results.png"
    _plot_results(metrics_df, dwell_df, chart_path)
    logger.info("Wrote %s", chart_path)


if __name__ == "__main__":
    main(sys.argv[1:])
