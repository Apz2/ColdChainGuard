"""thermal exposure maths — MKT, degree-minutes, disposition."""

from __future__ import annotations

import math

EA_J_PER_MOL: float = 83144.0  # activation energy J/mol (using 83.144 kJ/mol convention)
R_GAS: float = 8.314  # gas constant J/(mol·K)

# tuning these so S2 (short door opens) stays RELEASE but S3 (fridge fail) hits QUARANTINE
INVESTIGATE_BUDGET_FRACTION: float = 0.30
QUARANTINE_PEAK_DEVIATION_C: float = 5.0


def mean_kinetic_temperature(temps_c: list[float]) -> float:
    """computing Arrhenius-weighted MKT from a temp series.

    MKT = (Ea/R) / -ln( (1/n) * sum( exp(-Ea / (R * T_i)) ) )
    T_i needs to be in kelvin. returning celsius.
    """
    if not temps_c:
        raise ValueError("temps_c must not be empty")

    temps_k: list[float] = []
    for temp_c in temps_c:
        temp_k = temp_c + 273.15
        assert temp_k > 200, "forgot to convert to kelvin before MKT"
        temps_k.append(temp_k)

    n = len(temps_k)
    sum_exp = 0.0
    for temp_k in temps_k:
        sum_exp += math.exp(-EA_J_PER_MOL / (R_GAS * temp_k))

    mean_exp = sum_exp / n
    mkt_k = (EA_J_PER_MOL / R_GAS) / (-math.log(mean_exp))
    return mkt_k - 273.15


def degree_minutes(
    temp_c: float,
    low_c: float,
    high_c: float,
    interval_s: float,
) -> float:
    """working out degree-minutes for one reading — 0 if in range."""
    if low_c <= temp_c <= high_c:
        return 0.0

    if temp_c < low_c:
        deviation_c = low_c - temp_c
    else:
        deviation_c = temp_c - high_c

    return abs(deviation_c) * (interval_s / 60.0)


def disposition(
    consumed_dm: float,
    budget_dm: float,
    active_excursion: bool,
    peak_deviation_c: float,
) -> str:
    """picking RELEASE / INVESTIGATE / QUARANTINE based on exposure."""
    if consumed_dm >= budget_dm or peak_deviation_c >= QUARANTINE_PEAK_DEVIATION_C:
        return "QUARANTINE"
    if consumed_dm >= INVESTIGATE_BUDGET_FRACTION * budget_dm or active_excursion:
        return "INVESTIGATE"
    return "RELEASE"
