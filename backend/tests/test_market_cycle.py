"""Bitcoin 1064-day cycle — calendar math, validation, expectations, bias.

Fixed dates are the point: the pattern is a date law, so the tests pin real
calendar days (2025-10-20 is the projected top of the cycle anchored at
2022-11-21) rather than relative deltas that would hide an off-by-one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import market_cycle as mc


def _ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _synthetic_bars(bottom: date, cycles: int = 2) -> list[dict]:
    """Daily closes that top exactly at bottom+1064 and bottom at +1429.

    A clean sawtooth: the validation must score it a perfect hit, and the
    expectation maths must read the returns it was built to encode.
    """
    bars: list[dict] = []
    total = mc.BULL_DAYS + mc.BEAR_DAYS
    for c in range(cycles):
        base = bottom + timedelta(days=total * c)
        for i in range(total):
            day = base + timedelta(days=i)
            if i <= mc.BULL_DAYS:
                close = 100.0 + 100.0 * (i / mc.BULL_DAYS)      # 100 → 200
            else:
                close = 200.0 - 100.0 * ((i - mc.BULL_DAYS) / mc.BEAR_DAYS)  # 200 → 100
            bars.append({"time": _ts(day), "close": close, "volume": 1})
    return bars


ANCHORS = ["2015-01-14", "2018-12-15", "2022-11-21"]


# ── Anchor parsing ───────────────────────────────────────────────────────────


def test_parse_anchors_from_every_shape():
    assert mc.parse_anchors(None) == [date.fromisoformat(a) for a in mc.DEFAULT_ANCHORS]
    assert mc.parse_anchors(ANCHORS) == [date.fromisoformat(a) for a in ANCHORS]
    assert mc.parse_anchors('["2015-01-14","2018-12-15"]') == [
        date(2015, 1, 14), date(2018, 12, 15)]
    assert mc.parse_anchors("2015-01-14, 2018-12-15") == [
        date(2015, 1, 14), date(2018, 12, 15)]
    assert mc.parse_anchors(["junk", "2022-11-21", ""]) == [date(2022, 11, 21)]
    assert mc.parse_anchors("") == [date.fromisoformat(a) for a in mc.DEFAULT_ANCHORS]


# ── The snapshot ─────────────────────────────────────────────────────────────


def test_snapshot_late_bull_counts_down_to_the_projected_top():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2025, 8, 23))
    assert snap.ok
    assert snap.phase == "bull"
    assert snap.day_of_cycle == 1006
    assert snap.projected_top == "2025-10-20"
    assert snap.days_to_top == 58
    assert snap.projected_bottom == "2026-10-20"
    assert snap.days_to_bottom == 58 + mc.BEAR_DAYS
    assert snap.late_phase is True          # inside the 90-day caution window
    assert 0 < snap.phase_pct < 1


def test_snapshot_bear_after_the_projected_top():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2025, 11, 15))
    assert snap.phase == "bear"
    assert snap.day_of_cycle == 1090
    assert snap.phase_day == 1090 - mc.BULL_DAYS
    assert snap.days_to_bottom == (date(2026, 10, 20) - date(2025, 11, 15)).days
    assert snap.late_phase is False


def test_snapshot_on_projected_top_day_flips_to_bear():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2025, 10, 20))
    assert snap.phase == "bear"
    assert snap.phase_day == 0
    assert snap.days_to_top == 0


def test_snapshot_uses_last_anchor_before_today():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2019, 6, 1))
    assert snap.anchor == "2018-12-15"
    assert snap.phase == "bull"


def test_snapshot_without_usable_anchors_is_silent_not_wrong():
    snap = mc.build_cycle_snapshot(["2030-01-01"], today=date(2025, 8, 23))
    assert not snap.ok


def test_snapshot_attaches_price_stats_from_bars():
    bars = _synthetic_bars(date(2022, 11, 21))
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2024, 1, 1), bars=bars)
    assert snap.price is not None and snap.price > 100
    assert snap.cycle_high and snap.cycle_high <= 200.01
    assert snap.cycle_low and snap.cycle_low >= 99.99


# ── Validation ───────────────────────────────────────────────────────────────


def test_validation_scores_a_perfect_pattern_as_hits():
    bars = _synthetic_bars(date(2015, 1, 14))
    rows = mc.validate_pattern([date(2015, 1, 14)], bars=bars)
    assert rows["cycles"], "the synthetic cycle must produce a scored row"
    row = rows["cycles"][0]
    assert row["top_hit"] is True
    assert abs(row["top_error_days"]) <= 2   # daily bars → the top bar is the day itself
    assert rows["top_hit_rate"] == 1.0


def test_validation_flags_a_top_that_missed_the_window():
    # Shift the sawtooth so the top lands 60 days late → outside tolerance.
    bars = _synthetic_bars(date(2015, 1, 14) + timedelta(days=60))
    rows = mc.validate_pattern([date(2015, 1, 14)], bars=bars)
    row = rows["cycles"][0]
    assert row["top_hit"] is False
    assert abs(row["top_error_days"]) == 60


def test_validation_without_bars_reports_projection_only():
    rows = mc.validate_pattern([date(2015, 1, 14), date(2018, 12, 15)], bars=None)
    assert rows["top_hit_rate"] is None
    assert rows["cycles"][0]["projected_top"] == "2017-12-13"  # 2015-01-14 + 1064


# ── Day expectations ─────────────────────────────────────────────────────────


def test_day_expectation_reads_forward_returns_off_the_sawtooth():
    bars = _synthetic_bars(date(2015, 1, 14), cycles=2)
    out = mc.day_expectation([date(2015, 1, 14)], bars, offset=100, horizon_days=7)
    assert out["samples"] >= 1
    # 7 days into a linear 100→200 ramp over 1064 days ≈ +0.66%.
    assert 0.3 < out["avg_return_pct"] < 1.0
    assert out["worst_return_pct"] <= out["avg_return_pct"]


def test_day_expectation_with_no_data_is_empty_not_an_error():
    out = mc.day_expectation([date(2015, 1, 14)], [], 100)
    assert out["samples"] == 0


# ── Applicability + bias ─────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    ("BTCUSD", True), ("BTCUSDT", True), ("ETHUSDT", True), ("SOL/USD", True),
    ("XAUUSD", False), ("EURUSD", False), ("US30USD", False), ("", False),
])
def test_cycle_applies_only_to_crypto_that_follows_btc(symbol, expected):
    assert mc.cycle_applies(symbol) is expected


def test_bias_mid_bull_is_a_tailwind():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2024, 6, 1))
    bias = mc.cycle_bias("BTCUSD", snap)
    assert bias.applicable
    assert bias.normalized > 0
    assert "bull phase" in bias.reason
    assert bias.lines and "Projected top" in bias.lines[1]


def test_bias_late_bull_fades_the_tailwind():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2025, 8, 23))
    bias = mc.cycle_bias("BTCUSD", snap)
    assert 0 < bias.normalized < 0.3
    assert "late bull" in bias.reason


def test_bias_bear_is_a_headwind_that_eases_near_the_bottom():
    mid = mc.cycle_bias("BTCUSD", mc.build_cycle_snapshot(ANCHORS, today=date(2026, 2, 1)))
    late = mc.cycle_bias("BTCUSD", mc.build_cycle_snapshot(ANCHORS, today=date(2026, 9, 25)))
    assert mid.normalized < 0
    assert late.normalized > mid.normalized


def test_bias_for_non_crypto_is_silence():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2024, 6, 1))
    bias = mc.cycle_bias("XAUUSD", snap)
    assert not bias.applicable
    assert bias.normalized == 0.0


# ── Windows + calendar grid ──────────────────────────────────────────────────


def test_windows_mark_history_vs_projection():
    windows = mc.build_windows(ANCHORS, today=date(2025, 8, 23))
    bull_live = [w for w in windows if w.phase == "bull" and w.projected]
    assert bull_live, "the live bull box must be flagged projected"
    assert bull_live[-1].end == "2025-10-20"
    history = [w for w in windows if not w.projected]
    assert history, "past cycles must render as history"


def test_calendar_grid_paints_phases_and_marks_today():
    days = mc.build_cycle_calendar(ANCHORS, year=2025, month=10, today=date(2025, 8, 23))
    by_date = {d["date"]: d for d in days}
    assert len(days) == 31
    assert by_date["2025-10-01"]["phase"] == "bull"
    assert by_date["2025-10-21"]["phase"] == "bear"
    assert by_date["2025-10-20"]["is_top"] is True
    assert all(d["projected"] for d in days)   # whole month is in the future


def test_calendar_grid_marks_anchors_and_halvings():
    days = mc.build_cycle_calendar(ANCHORS, year=2024, month=4, today=date(2024, 4, 10))
    by_date = {d["date"]: d for d in days}
    assert by_date["2024-04-20"]["is_halving"] is True
    days22 = mc.build_cycle_calendar(ANCHORS, year=2022, month=11, today=date(2022, 12, 1))
    assert any(d["is_anchor"] for d in days22)


# ── Evidence ─────────────────────────────────────────────────────────────────


def test_evidence_lines_name_the_phase_and_the_dates():
    snap = mc.build_cycle_snapshot(ANCHORS, today=date(2025, 8, 23))
    lines = mc.evidence_lines(snap)
    assert any("BULL" in line for line in lines)
    assert any("2025-10-20" in line for line in lines)


def test_evidence_lines_empty_without_anchor():
    assert mc.evidence_lines(mc.build_cycle_snapshot([], today=date(2025, 8, 23))) == []
