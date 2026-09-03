"""filling ccg_dashboard.db with mixed-consignment data for dashboard screenshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cloud.integrity import IntegrityVerifier
from cloud.store import Store
from evaluation.runner import StepClock, _load_device_keys
from evaluation.scenarios import (
    SAMPLE_INTERVAL_S,
    ScenarioId,
    build_scenario,
    door_is_open,
    target_temperature_c,
)
from gateway.rules import ExcursionStateMachine
from node.simulator import SensorNode

REPO = Path(__file__).resolve().parent
db_path = REPO / "ccg_dashboard.db"
STOP_AFTER_EXCURSION_STEPS = 40  # stopping a bit after excursion so quarantine + peak temp show up


@dataclass(frozen=True)
class SeedTrack:
    consignment_id: str
    scenario_id: ScenarioId
    seed: int
    device_ids: tuple[str, ...]


TRACKS = (
    SeedTrack("CN-0417", "S3", 2026, ("NODE-01", "NODE-02", "NODE-03")),
    SeedTrack("CN-0418", "S2", 2048, ("NODE-04",)),
    SeedTrack("CN-0419", "S1", 2112, ("NODE-05",)),
)


def _build_nodes(
    track: SeedTrack,
    keys: dict[str, str],
    clock: StepClock,
) -> dict[str, SensorNode]:
    return {
        device_id: SensorNode(
            device_id,
            track.consignment_id,
            keys[device_id],
            clock,
            np.random.default_rng(track.seed + index),
        )
        for index, device_id in enumerate(track.device_ids)
    }


if db_path.exists():
    db_path.unlink()

keys = _load_device_keys(REPO / "config" / "devices.yaml")
store = Store(str(db_path), str(REPO / "config" / "consignments.yaml"))
verifier = IntegrityVerifier(keys)

primary = TRACKS[0]
spec_by_id = {
    track.consignment_id: build_scenario(track.scenario_id, seed=track.seed)
    for track in TRACKS
}
primary_spec = spec_by_id[primary.consignment_id]
clock = StepClock(primary_spec.start_epoch)

nodes_by_track = {track.consignment_id: _build_nodes(track, keys, clock) for track in TRACKS}
state_machines = {
    track.consignment_id: ExcursionStateMachine(2.0, 8.0, dwell_s=spec_by_id[track.consignment_id].dwell_s)
    for track in TRACKS
}

excursion_seen = False
steps_after_excursion = 0
max_steps = max(spec.num_steps for spec in spec_by_id.values())

for _step in range(max_steps):
    sim_ts = clock.now()

    for track in TRACKS:
        spec = spec_by_id[track.consignment_id]
        target_c = target_temperature_c(spec, sim_ts)
        is_open = door_is_open(spec, sim_ts)
        state_machine = state_machines[track.consignment_id]

        for device_id in track.device_ids:
            node = nodes_by_track[track.consignment_id][device_id]
            node.set_target(target_c)
            node.set_door(is_open)
            envelope = node.read()
            payload = envelope["payload"]
            temp_c = float(payload["temp_c"])

            if device_id == track.device_ids[0]:
                state_change = state_machine.update(temp_c, sim_ts)
                if state_change is not None:
                    store.insert_event(track.consignment_id, sim_ts, state_change)

            accepted, _reason = verifier.verify(envelope, sim_ts)
            if accepted:
                store.insert_reading(envelope, verified=1)

    primary_machine = state_machines[primary.consignment_id]
    if primary_machine.state == "EXCURSION":
        if not excursion_seen:
            excursion_seen = True
        else:
            steps_after_excursion += 1
        if excursion_seen and steps_after_excursion >= STOP_AFTER_EXCURSION_STEPS:
            break

    clock.advance(SAMPLE_INTERVAL_S)

for track in TRACKS:
    store.recompute_disposition(track.consignment_id)

print(f"Seeded {db_path}")
for track in TRACKS:
    row = store._conn.execute(
        "SELECT disposition FROM consignments WHERE id = ?",
        (track.consignment_id,),
    ).fetchone()
    reading_count = store._conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE consignment_id = ?",
        (track.consignment_id,),
    ).fetchone()["n"]
    print(
        f"  {track.consignment_id} ({track.scenario_id}): "
        f"disposition={row['disposition'] if row else 'unknown'} "
        f"readings={reading_count}"
    )
store.close()
print()
print("Next (do NOT run run_demo.py — it deletes ccg.db):")
print("  Terminal 1: python run_demo.py --broker")
print("  Terminal 2: python -m cloud.api --db ccg_dashboard.db")
print("  Browser:  http://localhost:8000/dashboard")
