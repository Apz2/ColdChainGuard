"""quick console run of the thermal engine — grabbing screenshot output for phase 2.

printing MKT, degree-minutes, disposition at three checkpoints:
RELEASE -> INVESTIGATE -> QUARANTINE
"""

from __future__ import annotations

from cloud.thermal import degree_minutes, disposition, mean_kinetic_temperature

CONSIGNMENT_ID = "CN-0417"
LOW_C = 2.0
HIGH_C = 8.0
BUDGET_DM = 300.0
INTERVAL_S = 30.0


def _peak_deviation(temp_c: float) -> float:
    if temp_c < LOW_C:
        return LOW_C - temp_c
    if temp_c > HIGH_C:
        return temp_c - HIGH_C
    return 0.0


def _snapshot(
    label: str,
    temps_c: list[float],
    consumed_dm: float,
    peak_deviation_c: float,
    active_excursion: bool,
) -> None:
    mkt_c = mean_kinetic_temperature(temps_c)
    disp = disposition(consumed_dm, BUDGET_DM, active_excursion, peak_deviation_c)
    print(f"\n--- {label} ---")
    print(f"  Consignment : {CONSIGNMENT_ID}")
    print(f"  Readings    : {len(temps_c)}")
    print(f"  MKT         : {mkt_c:.2f} °C")
    print(f"  Consumed    : {consumed_dm:.1f} / {BUDGET_DM:.1f} degree-minutes")
    print(f"  Peak dev.   : {peak_deviation_c:.1f} °C")
    print(f"  Excursion   : {'active' if active_excursion else 'none'}")
    print(f"  Disposition : {disp}")


def main() -> None:
    print("ColdChainGuard — Phase 2 thermal exposure engine")
    print(f"Limits {LOW_C:.1f}–{HIGH_C:.1f} °C | Budget {BUDGET_DM:.0f} degree-minutes")

    temps_c: list[float] = []
    consumed_dm = 0.0
    peak_deviation_c = 0.0
    active_excursion = False

    for _ in range(20):
        temp_c = 5.0
        temps_c.append(temp_c)
        consumed_dm += degree_minutes(temp_c, LOW_C, HIGH_C, INTERVAL_S)
        peak_deviation_c = max(peak_deviation_c, _peak_deviation(temp_c))

    _snapshot("Stable transit (in-range readings)", temps_c, consumed_dm, peak_deviation_c, active_excursion)

    for _ in range(90):
        temp_c = 10.0
        temps_c.append(temp_c)
        consumed_dm += degree_minutes(temp_c, LOW_C, HIGH_C, INTERVAL_S)
        peak_deviation_c = max(peak_deviation_c, _peak_deviation(temp_c))

    _snapshot(
        "Sustained warm excursion (budget threshold crossed)",
        temps_c,
        consumed_dm,
        peak_deviation_c,
        active_excursion,
    )

    active_excursion = True
    temp_c = 13.0
    temps_c.append(temp_c)
    consumed_dm += degree_minutes(temp_c, LOW_C, HIGH_C, INTERVAL_S)
    peak_deviation_c = max(peak_deviation_c, _peak_deviation(temp_c))

    _snapshot(
        "Refrigeration failure (peak deviation >= 5 °C)",
        temps_c,
        consumed_dm,
        peak_deviation_c,
        active_excursion,
    )


if __name__ == "__main__":
    main()
