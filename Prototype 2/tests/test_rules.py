"""phase 2 tests for gateway.rules."""

from __future__ import annotations

from gateway.rules import ExcursionStateMachine

_LOW_C = 2.0
_HIGH_C = 8.0


def _run_series(
    sm: ExcursionStateMachine,
    readings: list[tuple[float, float]],
) -> list[str]:
    """feeding (temp_c, ts) pairs and collecting state changes."""
    transitions: list[str] = []
    for temp_c, ts in readings:
        change = sm.update(temp_c, ts)
        if change is not None:
            transitions.append(change)
    return transitions


def test_brief_breach_warning_then_nominal_never_excursion() -> None:
    """one OOR reading then back in range — should flap back to NOMINAL, never EXCURSION."""
    sm = ExcursionStateMachine(_LOW_C, _HIGH_C, dwell_s=120.0)
    transitions = _run_series(
        sm,
        [
            (10.0, 0.0),
            (5.0, 30.0),
            (5.0, 60.0),
            (5.0, 90.0),
        ],
    )
    assert "WARNING" in transitions
    assert "NOMINAL" in transitions
    assert "EXCURSION" not in transitions


def test_continuous_breach_produces_exactly_one_excursion() -> None:
    """150 s continuous breach with dwell_s=120 — expecting exactly one EXCURSION."""
    sm = ExcursionStateMachine(_LOW_C, _HIGH_C, dwell_s=120.0)
    transitions: list[str] = []
    ts = 0.0
    while ts <= 150.0:
        change = sm.update(10.0, ts)
        if change is not None:
            transitions.append(change)
        ts += 30.0
    assert transitions.count("EXCURSION") == 1


def test_oscillations_never_produce_excursion() -> None:
    """bouncing in/out of range faster than dwell — should never reach EXCURSION."""
    sm = ExcursionStateMachine(_LOW_C, _HIGH_C, dwell_s=120.0)
    transitions: list[str] = []
    ts = 0.0
    for _ in range(20):
        change = sm.update(10.0, ts)
        if change is not None:
            transitions.append(change)
        ts += 10.0
        change = sm.update(5.0, ts)
        if change is not None:
            transitions.append(change)
        ts += 60.0
    assert "EXCURSION" not in transitions


def test_dwell_s_settable_at_runtime() -> None:
    """changing dwell_s at runtime for the phase 5 sweep."""
    sm = ExcursionStateMachine(_LOW_C, _HIGH_C, dwell_s=120.0)
    sm.dwell_s = 300.0
    transitions: list[str] = []
    ts = 0.0
    while ts <= 150.0:
        change = sm.update(10.0, ts)
        if change is not None:
            transitions.append(change)
        ts += 30.0
    assert "EXCURSION" not in transitions
