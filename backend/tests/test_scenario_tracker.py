"""Follow-up on a published plan must be earned by what price actually did.

The dangerous output here is a congratulatory update — "our zone held, 50% of
the plan is done" — attached to a call that was never triggered or was already
stopped out. A reader sizes their next trade on that sentence, so every
celebratory phrase in the narrative has to be gated on real excursion.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.timezone import now_sast
from app.models.database import JarvisAnalysisJournal
from app.services.scenario_tracker import progress_of, scenario_narrative


def _row(**kw) -> JarvisAnalysisJournal:
    base = dict(
        source="trading_room", symbol="XAUUSD", asset_class="metal", timeframe="4h",
        side="long", entry=100.0, stop_loss=90.0, take_profit=120.0,
        confidence=0.7, created_at=now_sast() - timedelta(hours=6),
    )
    base.update(kw)
    return JarvisAnalysisJournal(**base)


def _bars(rows, *, start: datetime | None = None, step_s: int = 14400):
    """[(high, low)] → ccxt rows starting just after the proposal."""
    start = start or (now_sast() - timedelta(hours=5))
    ts = int(start.timestamp())
    return [
        [(ts + i * step_s) * 1000, low, high, low, (high + low) / 2]
        for i, (high, low) in enumerate(rows)
    ]


# ── Progress measurement ─────────────────────────────────────────────────────

def test_a_plan_price_never_reached_is_untriggered_not_a_result():
    """Price stayed above the entry the whole time."""
    state = progress_of(_row(), _bars([(108, 104), (110, 103)]))
    assert state["status"] == "waiting"
    assert state["filled"] is False
    assert state["pct_complete"] == 0.0


def test_half_the_mapped_move_reads_as_half():
    """Entry 100, target 120; price traded to 110."""
    state = progress_of(_row(), _bars([(101, 99), (110, 102)]))
    assert state["status"] == "running"
    assert state["pct_complete"] == pytest.approx(50.0)


def test_progress_cannot_exceed_the_plan():
    state = progress_of(_row(), _bars([(101, 99), (140, 105)]))
    assert state["pct_complete"] == 100.0


def test_a_plan_whose_invalidation_was_hit_is_dead():
    state = progress_of(_row(), _bars([(101, 99), (102, 88)]))
    assert state["status"] == "invalidated"


def test_a_settled_loss_is_never_re_read_as_progress():
    """A later rally must not rewrite a recorded loss into a running plan."""
    row = _row(outcome="loss", outcome_r=-1.0)
    state = progress_of(row, _bars([(101, 99), (125, 118)]))
    assert state["status"] == "loss"
    assert state["pct_complete"] == 0.0


def test_a_short_plan_is_measured_downwards():
    row = _row(side="short", entry=100.0, stop_loss=110.0, take_profit=80.0)
    state = progress_of(row, _bars([(101, 99), (99, 90)]))
    assert state["status"] == "running"
    assert state["pct_complete"] == pytest.approx(50.0)


def test_candles_from_before_the_plan_are_ignored():
    """Otherwise yesterday's move would count as progress on today's call."""
    old = _bars([(130, 125)], start=now_sast() - timedelta(hours=40))
    assert progress_of(_row(), old) is None


def test_a_plan_with_no_distance_to_target_is_unmeasurable():
    assert progress_of(_row(take_profit=100.0), _bars([(101, 99)])) is None


# ── The words that go out ────────────────────────────────────────────────────

def test_an_untriggered_plan_is_never_reported_as_a_success():
    text = scenario_narrative([progress_of(_row(), _bars([(108, 104)]))])
    assert "untriggered" in text
    for claim in ("✔️", "completed", "reacted"):
        assert claim not in text


def test_an_invalidated_plan_says_so_plainly():
    text = scenario_narrative([progress_of(_row(), _bars([(101, 99), (102, 88)]))])
    assert "❌" in text and "did not hold" in text
    assert "completed" not in text


def test_a_plan_running_to_schedule_may_be_reported_as_such():
    text = scenario_narrative([progress_of(_row(), _bars([(101, 99), (110, 102)]))])
    assert "reached and reacted" in text
    assert "50% of the plan" in text


def test_a_barely_moved_plan_is_not_dressed_up_as_a_reaction():
    """A touch that goes nowhere is a touch, not a reaction."""
    text = scenario_narrative([progress_of(_row(), _bars([(101, 99), (101.5, 99)]))])
    assert "reacted" not in text
    assert "Too early" in text


def test_no_plans_means_no_follow_up_text():
    assert scenario_narrative([]) == ""
