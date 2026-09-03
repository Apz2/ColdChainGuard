"""phase 2 tests for cloud.thermal."""

from __future__ import annotations

import pytest

from cloud.thermal import degree_minutes, mean_kinetic_temperature


def test_mkt_constant_series_returns_mean() -> None:
    """flat 5 °C series — MKT should come back ~5.0."""
    result = mean_kinetic_temperature([5.0] * 100)
    assert result == pytest.approx(5.0, abs=0.01)


def test_mkt_alternating_series_above_arithmetic_mean() -> None:
    """alternating 2/8 °C — MKT should sit above the arithmetic mean (5.0)."""
    temps_c: list[float] = []
    for _ in range(50):
        temps_c.append(2.0)
        temps_c.append(8.0)
    result = mean_kinetic_temperature(temps_c)
    assert result > 5.0


def test_mkt_empty_raises_value_error() -> None:
    """empty list should blow up."""
    with pytest.raises(ValueError):
        mean_kinetic_temperature([])


def test_degree_minutes_over_high_limit() -> None:
    """10 °C for 30 s over 8 °C limit — expecting 1.0 degree-minute."""
    assert degree_minutes(10.0, 2.0, 8.0, 30) == 1.0


def test_degree_minutes_within_limits() -> None:
    """in-range reading — should contribute 0 degree-minutes."""
    assert degree_minutes(5.0, 2.0, 8.0, 30) == 0.0
