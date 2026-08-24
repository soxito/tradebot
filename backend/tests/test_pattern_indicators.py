"""A named pattern is what a reader trades against, so it must be earned.

`detect_triangle` is deliberately narrow: these lock in that ordinary drifting
and ranging price return nothing, and only a genuinely flat boundary with a
converging opposite side is given a name.
"""

from __future__ import annotations

import pytest

from app.signals.technical import detect_triangle, ichimoku, ohlcv_to_dataframe


def _bars(rows: list[tuple[float, float, float]]):
    """(high, low, close) tuples → the OHLCV frame the indicators expect."""
    return ohlcv_to_dataframe([
        [1_700_000_000_000 + i * 86_400_000, c, h, l, c, 1000.0]
        for i, (h, l, c) in enumerate(rows)
    ])


def _trend(n: int, drift: float, start: float = 100.0):
    rows, price = [], start
    for _ in range(n):
        price += drift
        rows.append((price + 1.0, price - 1.0, price))
    return _bars(rows)


def _ramp(start: float, end: float, steps: int) -> list[float]:
    return [start + (end - start) * (i + 1) / steps for i in range(steps)]


def _ascending_triangle(cycles: int = 5, resistance: float = 100.0, breakout: bool = True):
    """Flat highs at `resistance`, each pullback low higher than the last.

    Peaks and troughs are single bars: `pivot_highs` only confirms a strict
    maximum, so a repeated plateau value would register no pivot at all.
    """
    prices: list[float] = [78.0]
    low = 80.0
    for cycle in range(cycles):
        # A hair of variation keeps each peak a unique maximum while staying
        # well inside the flatness tolerance.
        peak = resistance - (0.3 if cycle % 2 else 0.0)
        prices += _ramp(prices[-1], peak, 6)
        prices += _ramp(peak, low, 6)
        low += 3.0
    prices += _ramp(prices[-1], resistance + 6.0 if breakout else resistance - 4.0, 8)

    rows = [(p + 0.4, p - 0.4, p) for p in prices]
    return _bars(rows)


# ── Triangles ────────────────────────────────────────────────────────────────

def test_a_real_ascending_triangle_is_recognised_and_its_break_reported():
    result = detect_triangle(_ascending_triangle())
    assert result is not None
    assert result["kind"] == "ascending"
    assert result["level"] == pytest.approx(100.0, abs=1.0)
    assert result["broken"] is True


def test_the_same_triangle_unbroken_is_reported_as_unbroken():
    result = detect_triangle(_ascending_triangle(breakout=False))
    assert result is not None and result["broken"] is False


def test_a_steady_trend_is_not_a_triangle():
    assert detect_triangle(_trend(150, 0.6)) is None
    assert detect_triangle(_trend(150, -0.6)) is None


def test_a_flat_range_is_not_a_triangle():
    """Both sides flat means no convergence — naming it would be wrong."""
    rows = [(102.0, 98.0, 100.0), (101.0, 99.0, 100.0)] * 60
    assert detect_triangle(_bars(rows)) is None


def test_too_little_history_yields_no_pattern():
    assert detect_triangle(_trend(12, 0.5)) is None


# ── Ichimoku ─────────────────────────────────────────────────────────────────

def test_the_cloud_sits_below_a_rising_market_and_above_a_falling_one():
    assert ichimoku(_trend(120, 0.6))["position"] == "above"
    assert ichimoku(_trend(120, -0.6))["position"] == "below"


def test_the_cloud_is_unreadable_without_enough_history():
    """52 bars is the shortest span; fewer must not produce a confident read."""
    result = ichimoku(_trend(20, 0.5))
    assert result["position"] is None
    assert result["cloud_top"] is None


def test_the_cloud_bounds_are_ordered():
    result = ichimoku(_trend(120, 0.6))
    assert result["cloud_top"] >= result["cloud_bottom"]
