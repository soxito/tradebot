"""Candlestick pattern detectors — named reversals, hit and miss."""
from __future__ import annotations

from app.signals.candle_patterns import detect_patterns


def _bar(ms, o, h, l, c):
    return [ms, o, h, l, c, 0.0]


def _down(ms, price, size=10.0):
    """A tall red bar: opens high, closes low."""
    return _bar(ms, price + size, price + size + 2, price - 2, price - size)


def _up(ms, price, size=10.0):
    return _bar(ms, price - size, price + size + 2, price - size - 2, price + size)


def _star(ms, price):
    """A small-bodied bar at `price`."""
    return _bar(ms, price + 1, price + 2, price - 2, price - 1)


def test_morning_star_detected_at_a_bottom():
    """Tall red → small star below → tall green closing into the first body."""
    base = 100.0
    rows = [
        _up(0, base + 30),
        _down(60_000, base + 20),
        _down(120_000, base + 10),
        # The bottom: tall red, star, tall green closing above the midpoint.
        _bar(180_000, 100, 101, 88, 89),
        _bar(240_000, 88.5, 90, 86.5, 87.5),
        _bar(300_000, 89, 103, 88, 96),
        _up(360_000, base + 20),
    ]
    events = detect_patterns(rows, patterns=["morning_star"])
    assert any(e["name"] == "Morning Star" and e["direction"] == "bull" for e in events)


def test_evening_star_detected_at_a_top():
    rows = [
        _down(0, 120),
        _up(60_000, 130),
        _up(120_000, 140),
        _bar(180_000, 150, 163, 149, 161),
        _bar(240_000, 162.5, 164, 160.5, 161.5),
        _bar(300_000, 160, 161, 147, 149),
        _down(360_000, 130),
    ]
    events = detect_patterns(rows, patterns=["evening_star"])
    assert any(e["name"] == "Evening Star" and e["direction"] == "bear" for e in events)


def test_inverted_hammer_detected():
    """Small body at the bottom of its bar, long upper wick."""
    rows = [
        _down(0, 120),
        _bar(60_000, 100, 112, 100, 101),
        _down(120_000, 90),
    ]
    events = detect_patterns(rows, patterns=["inverted_hammer"])
    assert any(e["name"] == "Inverted Hammer" for e in events)


def test_hammer_after_a_down_bar_is_bullish():
    rows = [
        _up(0, 120),
        _down(60_000, 110),
        _bar(120_000, 100, 100.5, 88, 100.4),
    ]
    events = detect_patterns(rows, patterns=["hammer"])
    assert any(e["name"] == "Hammer" and e["direction"] == "bull" for e in events)


def test_bullish_engulfing_detected():
    rows = [
        _down(0, 120),
        _bar(60_000, 100, 101, 92, 93),
        _bar(120_000, 92.5, 103, 92, 101),
    ]
    events = detect_patterns(rows, patterns=["engulfing"])
    assert any(e["name"] == "Bullish Engulfing" for e in events)


def test_quiet_series_finds_nothing():
    """A flat drift produces no named patterns — silence over false positives."""
    rows = [_bar(i * 60_000, 100 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100.2 + i * 0.01)
            for i in range(30)]
    assert detect_patterns(rows) == []


def test_junk_rows_are_dropped_and_series_sorted():
    rows = [
        _bar(240_000, 90, 103, 88, 96),
        "garbage",
        _bar(180_000, 100, 101, 88, 89),
        _bar(300_000, 89, 103, 88, 96),
    ]
    events = detect_patterns(rows, patterns=["morning_star"])
    if events:
        assert events[0]["index"] >= 1  # a star needs two bars before it
