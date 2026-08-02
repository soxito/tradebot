"""Settlement rules for JARVIS's own trade proposals.

The judgement calls encoded here matter more than the plumbing: an entry price
never reaches is not a loss, a bar spanning both stop and target is, and a
proposal still legitimately running is not settled early. Each of those is a way
a learning loop can quietly flatter itself into uselessness.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.timezone import now_sast

from app.models.database import JarvisAnalysisJournal
from app.services import analysis_journal as journal


def _row(**kw) -> JarvisAnalysisJournal:
    base = dict(
        source="test", symbol="XAUUSD", asset_class="metal", timeframe="4h",
        side="long", entry=100.0, stop_loss=90.0, take_profit=120.0,
        confidence=0.75, created_at=now_sast() - timedelta(hours=6),
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


# ── Outcome resolution ───────────────────────────────────────────────────────

def test_settles_win_when_target_hit_first():
    row = _row()
    verdict = journal.evaluate(row, _bars([(101, 99), (105, 98), (125, 118)]))
    assert verdict["outcome"] == "win"
    assert verdict["exit_reason"] == "tp"
    assert verdict["outcome_r"] > 0


def test_settles_loss_when_stop_hit_first():
    row = _row()
    verdict = journal.evaluate(row, _bars([(101, 99), (100, 88)]))
    assert verdict["outcome"] == "loss"
    assert verdict["exit_reason"] == "sl"
    assert verdict["outcome_r"] < 0


def test_bar_spanning_stop_and_target_resolves_as_loss():
    """Assuming the favourable fill is how backtests flatter themselves."""
    row = _row()
    verdict = journal.evaluate(row, _bars([(101, 99), (130, 85)]))
    assert verdict["outcome"] == "loss", "ambiguous bar was read optimistically"
    assert verdict["exit_reason"] == "sl"


def test_unfilled_entry_is_no_fill_not_loss():
    """Price never came back to the entry — that is a distinct failure mode."""
    row = _row(created_at=now_sast() - timedelta(hours=100))
    verdict = journal.evaluate(row, _bars([(140, 130), (150, 135)]), expiry_hours=72)
    assert verdict["outcome"] == "no_fill"
    assert verdict["exit_reason"] == "no_fill"


def test_still_running_inside_its_window_stays_unsettled():
    """Settling early would record a verdict the market has not delivered."""
    row = _row(created_at=now_sast() - timedelta(hours=2))
    assert journal.evaluate(row, _bars([(101, 99), (105, 98)]), expiry_hours=72) is None


def test_unfilled_but_not_yet_expired_stays_unsettled():
    row = _row(created_at=now_sast() - timedelta(hours=2))
    assert journal.evaluate(row, _bars([(140, 130)]), expiry_hours=72) is None


def test_expired_without_resolution_is_marked_to_market():
    row = _row(created_at=now_sast() - timedelta(hours=100))
    verdict = journal.evaluate(row, _bars([(101, 99), (110, 105)]), expiry_hours=72)
    assert verdict["outcome"] in ("win", "loss", "break_even")
    assert verdict["exit_reason"] == "expiry"


def test_no_candles_before_expiry_stays_unsettled():
    """A feed gap must not be recorded as a real result."""
    row = _row(created_at=now_sast() - timedelta(hours=2))
    assert journal.evaluate(row, [], expiry_hours=72) is None


def test_short_side_uses_the_opposite_fill_and_target():
    row = _row(side="short", entry=100.0, stop_loss=110.0, take_profit=80.0)
    verdict = journal.evaluate(row, _bars([(101, 99), (95, 78)]))
    assert verdict["outcome"] == "win"
    assert verdict["exit_reason"] == "tp"


# ── Persistence ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_and_settle_roundtrip(async_session):
    rid = await journal.record_proposal(
        async_session,
        source="jarvis_command", symbol="XAUUSD", asset_class="metal",
        side="long", entry=100.0, stop_loss=90.0, take_profit=120.0,
        confidence=0.8, price_source="yahoo:GC=F",
        features={"trend": "uptrend", "rsi": 61},
    )
    assert rid is not None

    pending = await journal.unsettled(async_session, older_than_s=0)
    assert len(pending) == 1

    await journal.settle(async_session, pending[0], {
        "outcome": "win", "outcome_r": 2.0, "mfe": 20.0, "mae": 2.0,
        "exit_price": 120.0, "exit_reason": "tp", "bars_to_outcome": 3,
    })
    assert not await journal.unsettled(async_session, older_than_s=0)


@pytest.mark.asyncio
async def test_settle_is_idempotent(async_session):
    await journal.record_proposal(
        async_session, source="t", symbol="XAUUSD", side="long",
        entry=100.0, stop_loss=90.0, take_profit=120.0,
    )
    row = (await journal.unsettled(async_session, older_than_s=0))[0]
    await journal.settle(async_session, row, {"outcome": "win", "outcome_r": 2.0})
    await journal.settle(async_session, row, {"outcome": "loss", "outcome_r": -1.0})
    assert row.outcome == "win", "a settled row was overwritten"


@pytest.mark.asyncio
async def test_recording_never_raises_on_bad_input(async_session):
    """Journalling must never cost the user the analysis they asked for."""
    assert await journal.record_proposal(
        async_session, source="t", symbol="X", side="long",
        entry="not-a-number", stop_loss=90.0, take_profit=120.0,
    ) is None


@pytest.mark.asyncio
async def test_unsettled_skips_very_recent_rows(async_session):
    """A proposal made a minute ago has no bars to settle against."""
    await journal.record_proposal(
        async_session, source="t", symbol="XAUUSD", side="long",
        entry=100.0, stop_loss=90.0, take_profit=120.0,
    )
    assert await journal.unsettled(async_session, older_than_s=1800) == []


# ── Learned statistics ───────────────────────────────────────────────────────

async def _seed(session, outcomes, *, confidence=0.85, symbol="XAUUSD"):
    for outcome, r in outcomes:
        await journal.record_proposal(
            session, source="t", symbol=symbol, asset_class="metal", side="long",
            entry=100.0, stop_loss=90.0, take_profit=120.0, confidence=confidence,
        )
    rows = await journal.unsettled(session, older_than_s=0)
    for row, (outcome, r) in zip(rows, outcomes):
        await journal.settle(session, row, {"outcome": outcome, "outcome_r": r})


@pytest.mark.asyncio
async def test_no_fill_is_excluded_from_win_rate_but_still_reported(async_session):
    """Otherwise a run of unreachable entries would look like a clean record."""
    await _seed(async_session, [("win", 2.0), ("loss", -1.0)] + [("no_fill", None)] * 5)
    stats = await journal.learned_stats(async_session)
    assert stats["settled"] == 2
    assert stats["win_rate"] == 0.5, "no_fill leaked into the win rate"
    assert stats["no_fill"] == 5, "no_fill was hidden rather than reported"


@pytest.mark.asyncio
async def test_stats_bucket_by_stated_confidence(async_session):
    await _seed(async_session, [("win", 2.0)] * 2 + [("loss", -1.0)] * 2, confidence=0.9)
    stats = await journal.learned_stats(async_session)
    assert stats["by_confidence"]["80%+"]["n"] == 4
    assert stats["by_confidence"]["80%+"]["win_rate"] == 0.5


@pytest.mark.asyncio
async def test_memory_block_is_silent_below_min_samples(async_session):
    """A win rate over three trades is noise presented as evidence."""
    await _seed(async_session, [("win", 2.0), ("loss", -1.0)])
    stats = await journal.learned_stats(async_session)
    assert stats["settled"] < journal.MIN_SAMPLES
    assert journal.memory_block(stats) is None


@pytest.mark.asyncio
async def test_memory_block_reports_realised_calibration(async_session):
    await _seed(async_session, [("win", 2.0)] * 5 + [("loss", -1.0)] * 5, confidence=0.9)
    block = journal.memory_block(await journal.learned_stats(async_session))
    assert block is not None
    assert "50% win rate" in block
    assert "80%+" in block, "the confidence bucket should be named"
    assert "calibrate" in block.lower()


@pytest.mark.asyncio
async def test_memory_block_respects_its_char_cap(async_session):
    await _seed(async_session, [("win", 2.0)] * 20 + [("loss", -1.0)] * 20)
    block = journal.memory_block(await journal.learned_stats(async_session))
    assert block is not None and len(block) <= journal._MEMORY_BLOCK_CHARS


@pytest.mark.asyncio
async def test_memory_block_for_never_raises(async_session):
    assert await journal.memory_block_for(async_session, ["XAUUSD"]) is None


# ── Learning from the macro read ─────────────────────────────────────────────
# The macro weight starts as somebody's guess (0.05). These buckets are how the
# record earns it back: if calls the dollar backed do not out-perform the ones
# it opposed, that is evidence against the factor, and the prompt says so.

import json  # noqa: E402


def _settled(outcome, *, macro_aligned=None, applied=True, r=1.0):
    features = {"trend": "up"}
    if macro_aligned is not None:
        features.update({"macro_applied": applied, "macro_aligned": macro_aligned})
    elif applied is False:
        features.update({"macro_applied": False})
    return _row(outcome=outcome, outcome_r=r, features=json.dumps(features))


def test_macro_buckets_split_supported_from_opposed():
    rows = [
        _settled("win", macro_aligned=0.6),
        _settled("win", macro_aligned=0.4),
        _settled("loss", macro_aligned=-0.5, r=-1.0),
        _settled("loss", macro_aligned=-0.7, r=-1.0),
    ]
    buckets = journal._macro_buckets(rows)

    assert buckets["supported"]["n"] == 2
    assert buckets["supported"]["win_rate"] == 1.0
    assert buckets["opposed"]["n"] == 2
    assert buckets["opposed"]["win_rate"] == 0.0


def test_a_shrug_is_not_counted_as_a_stance():
    """A |reading| under the floor is a coin-flip, not evidence either way."""
    buckets = journal._macro_buckets([_settled("win", macro_aligned=0.02)])
    assert "supported" not in buckets and "opposed" not in buckets
    assert buckets["neutral"]["n"] == 1


def test_rows_where_macro_did_not_apply_are_kept_separate():
    """EURGBP calls must not dilute the evidence for or against the factor."""
    buckets = journal._macro_buckets([_settled("win", applied=False)])
    assert buckets["not_applied"]["n"] == 1
    assert "supported" not in buckets


def test_rows_with_no_features_do_not_break_the_split():
    rows = [_row(outcome="win", outcome_r=1.0, features=None),
            _row(outcome="loss", outcome_r=-1.0, features="not json")]
    assert journal._macro_buckets(rows) == {} or "supported" not in journal._macro_buckets(rows)


def test_the_prompt_states_the_macro_track_record_once_there_is_enough():
    stats = {
        "settled": 12, "wins": 7, "losses": 5, "win_rate": 0.58, "avg_r": 0.3,
        "by_confidence": {},
        "by_macro": {
            "supported": {"n": 6, "win_rate": 0.83, "avg_r": 0.9},
            "opposed": {"n": 6, "win_rate": 0.33, "avg_r": -0.4},
        },
    }
    block = journal.memory_block(stats)
    assert "backing the call" in block
    assert "83%" in block and "33%" in block


def test_the_prompt_stays_quiet_until_both_sides_have_samples():
    """A win rate over two trades is noise dressed as evidence."""
    stats = {
        "settled": 12, "wins": 7, "losses": 5, "win_rate": 0.58, "avg_r": 0.3,
        "by_confidence": {},
        "by_macro": {"supported": {"n": 2, "win_rate": 1.0, "avg_r": 1.2}},
    }
    assert "backing the call" not in journal.memory_block(stats)
