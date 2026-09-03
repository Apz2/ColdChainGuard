"""defining the S1–S5 scenarios and when i'm injecting faults."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import numpy as np

SAMPLE_INTERVAL_S = 30.0
TWELVE_HOURS_S = 12.0 * 3600.0
CONSIGNMENT_ID = "CN-0417"
LOW_LIMIT_C = 2.0
HIGH_LIMIT_C = 8.0
DEVICE_IDS = ("NODE-01", "NODE-02", "NODE-03")

ScenarioId = Literal["S1", "S2", "S3", "S4", "S5"]
SCENARIO_IDS: tuple[ScenarioId, ...] = ("S1", "S2", "S3", "S4", "S5")


class InjectionKind(str, Enum):
    """fault types i'm tracking as ground truth."""

    NOMINAL = "nominal"
    DOOR_OPEN = "door_open"
    REFRIGERATION_FAILURE = "refrigeration_failure"
    NETWORK_OUTAGE = "network_outage"
    ADVERSARIAL = "adversarial"


@dataclass(frozen=True)
class InjectionRecord:
    """one fault injection — when it starts and how long it lasts."""

    kind: InjectionKind
    injection_ts: float
    duration_s: float | None = None
    detail: str = ""

    @property
    def end_ts(self) -> float | None:
        """working out when a timed injection ends (if it has a duration)."""
        if self.duration_s is None:
            return None
        return self.injection_ts + self.duration_s


@dataclass
class ScenarioSpec:
    """everything needed to run one eval scenario."""

    scenario_id: ScenarioId
    seed: int
    duration_s: float
    start_epoch: float
    dwell_s: float
    expected_disposition: str
    expect_excursion: bool
    injections: list[InjectionRecord] = field(default_factory=list)
    truth_windows: list[tuple[float, float]] = field(default_factory=list)
    adversarial_count: int = 0

    @property
    def num_steps(self) -> int:
        return int(self.duration_s / SAMPLE_INTERVAL_S)


def _door_schedule(rng: np.random.Generator, start_epoch: float) -> list[InjectionRecord]:
    """scheduling six door opens (4–8 min each) during the first 2 h loading window."""
    loading_offsets_s = np.array([600.0, 1500.0, 2400.0, 3300.0, 4200.0, 5100.0])
    durations_s = rng.integers(240, 481, size=6)

    injections: list[InjectionRecord] = []
    for offset_s, duration_s in zip(loading_offsets_s, durations_s, strict=True):
        injection_ts = start_epoch + float(offset_s)
        injections.append(
            InjectionRecord(
                kind=InjectionKind.DOOR_OPEN,
                injection_ts=injection_ts,
                duration_s=float(duration_s),
                detail=f"door open {duration_s:.0f} simulated seconds",
            )
        )
    return injections


def _refrigeration_failure(
    start_epoch: float,
    ramp_start_offset_s: float = 6.0 * 3600.0,
    ramp_duration_s: float = 45.0 * 60.0,
) -> InjectionRecord:
    """ramping target temp to 14 °C over 45 min starting at T+6 h."""
    injection_ts = start_epoch + ramp_start_offset_s
    return InjectionRecord(
        kind=InjectionKind.REFRIGERATION_FAILURE,
        injection_ts=injection_ts,
        duration_s=ramp_duration_s,
        detail="target ramp 5 -> 14 C over 45 min",
    )


def _network_outage(
    start_epoch: float,
    outage_start_offset_s: float,
    outage_duration_s: float = 40.0 * 60.0,
) -> InjectionRecord:
    """dropping the broker for 40 sim minutes."""
    injection_ts = start_epoch + outage_start_offset_s
    return InjectionRecord(
        kind=InjectionKind.NETWORK_OUTAGE,
        injection_ts=injection_ts,
        duration_s=outage_duration_s,
        detail="broker unreachable 40 min",
    )


def build_scenario(
    scenario_id: ScenarioId,
    seed: int,
    start_epoch: float = 1_735_689_600.0,
    dwell_s: float = 120.0,
) -> ScenarioSpec:
    """building a scenario with fixed injection times for a given seed."""
    rng = np.random.default_rng(seed)
    injections: list[InjectionRecord] = []
    truth_windows: list[tuple[float, float]] = []

    if scenario_id == "S1":
        return ScenarioSpec(
            scenario_id="S1",
            seed=seed,
            duration_s=TWELVE_HOURS_S,
            start_epoch=start_epoch,
            dwell_s=dwell_s,
            expected_disposition="RELEASE",
            expect_excursion=False,
            injections=[],
            truth_windows=[],
        )

    if scenario_id == "S2":
        injections = _door_schedule(rng, start_epoch)
        for record in injections:
            assert record.end_ts is not None
            truth_windows.append((record.injection_ts, record.end_ts))
        return ScenarioSpec(
            scenario_id="S2",
            seed=seed,
            duration_s=TWELVE_HOURS_S,
            start_epoch=start_epoch,
            dwell_s=dwell_s,
            expected_disposition="RELEASE",
            expect_excursion=False,
            injections=injections,
            truth_windows=truth_windows,
        )

    if scenario_id == "S3":
        failure = _refrigeration_failure(start_epoch)
        injections = [failure]
        truth_windows = [
            (failure.injection_ts, start_epoch + TWELVE_HOURS_S),
        ]
        return ScenarioSpec(
            scenario_id="S3",
            seed=seed,
            duration_s=TWELVE_HOURS_S,
            start_epoch=start_epoch,
            dwell_s=dwell_s,
            expected_disposition="QUARANTINE",
            expect_excursion=True,
            injections=injections,
            truth_windows=truth_windows,
        )

    if scenario_id == "S4":
        failure = _refrigeration_failure(start_epoch)
        outage = _network_outage(
            start_epoch,
            outage_start_offset_s=6.0 * 3600.0 + 30.0 * 60.0,
        )
        injections = [failure, outage]
        truth_windows = [
            (failure.injection_ts, start_epoch + TWELVE_HOURS_S),
        ]
        return ScenarioSpec(
            scenario_id="S4",
            seed=seed,
            duration_s=TWELVE_HOURS_S,
            start_epoch=start_epoch,
            dwell_s=dwell_s,
            expected_disposition="QUARANTINE",
            expect_excursion=True,
            injections=injections,
            truth_windows=truth_windows,
        )

    if scenario_id == "S5":
        adversarial_ts = start_epoch + 1800.0
        injections = [
            InjectionRecord(
                kind=InjectionKind.ADVERSARIAL,
                injection_ts=adversarial_ts,
                duration_s=None,
                detail="50 forged + 50 replayed frames",
            )
        ]
        return ScenarioSpec(
            scenario_id="S5",
            seed=seed,
            duration_s=3600.0,
            start_epoch=start_epoch,
            dwell_s=dwell_s,
            expected_disposition="RELEASE",
            expect_excursion=False,
            injections=injections,
            truth_windows=[],
            adversarial_count=100,
        )

    raise ValueError(f"Unknown scenario: {scenario_id}")


def target_temperature_c(spec: ScenarioSpec, sim_ts: float) -> float:
    """figuring out what the setpoint temp should be at this sim time."""
    base_target_c = 5.0

    for record in spec.injections:
        if record.kind == InjectionKind.DOOR_OPEN:
            end_ts = record.end_ts
            if end_ts is not None and record.injection_ts <= sim_ts < end_ts:
                return 9.0

        if record.kind == InjectionKind.REFRIGERATION_FAILURE:
            ramp_start = record.injection_ts
            ramp_duration = record.duration_s or 0.0
            if sim_ts < ramp_start:
                continue
            elapsed_s = sim_ts - ramp_start
            if elapsed_s >= ramp_duration:
                return 14.0
            fraction = elapsed_s / ramp_duration
            return base_target_c + (14.0 - base_target_c) * fraction

    return base_target_c


def door_is_open(spec: ScenarioSpec, sim_ts: float) -> bool:
    """checking if the door is open at this sim time."""
    for record in spec.injections:
        if record.kind != InjectionKind.DOOR_OPEN:
            continue
        end_ts = record.end_ts
        if end_ts is not None and record.injection_ts <= sim_ts < end_ts:
            return True
    return False


def network_is_up(spec: ScenarioSpec, sim_ts: float) -> bool:
    """returning False while the broker outage is active."""
    for record in spec.injections:
        if record.kind != InjectionKind.NETWORK_OUTAGE:
            continue
        end_ts = record.end_ts
        if end_ts is not None and record.injection_ts <= sim_ts < end_ts:
            return False
    return True


def primary_fault_injection_ts(spec: ScenarioSpec) -> float | None:
    """picking the injection time i'm using to measure detection latency."""
    for record in spec.injections:
        if record.kind in {
            InjectionKind.REFRIGERATION_FAILURE,
            InjectionKind.NETWORK_OUTAGE,
            InjectionKind.ADVERSARIAL,
        }:
            return record.injection_ts
    return None
