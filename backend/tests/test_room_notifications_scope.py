"""What the desk is allowed to interrupt someone about.

The room analyses continuously and publishes a full call — four messages and a
chart — for every actionable verdict. Left ungated that reaches the channel for
whatever the rotation landed on, including instruments nobody holds and strings
that are not instruments at all, and it reaches it again every time the board
re-reads the same pair. Each of those trains the reader to ignore the ones that
matter, which is the real cost.
"""

from __future__ import annotations

import pytest

from app.agents import scope
from app.services import room_publisher


@pytest.fixture(autouse=True)
def _fresh():
    room_publisher.forget_published()
    scope.invalidate()
    yield
    room_publisher.forget_published()
    scope.invalidate()


def _result(**over):
    base = {"symbol": "XAUUSD", "final_action": "buy", "trigger": "room"}
    base.update(over)
    return base


@pytest.fixture
def _active(monkeypatch):
    def _set(*symbols):
        wanted = {scope.normalise(s) for s in symbols}

        async def _is_active(db, symbol):
            return scope.normalise(symbol) in wanted

        monkeypatch.setattr(scope, "is_active", _is_active)
    return _set


@pytest.mark.asyncio
async def test_a_pair_the_desk_is_working_on_is_published(_active):
    _active("XAUUSD")
    assert await room_publisher._worth_sending(_result(), "XAUUSD", "buy") is True


@pytest.mark.asyncio
async def test_a_pair_nobody_holds_or_pinned_stays_in_the_room(_active):
    """The rotation may land anywhere; the channel is not the rotation."""
    _active("XAUUSD")
    assert await room_publisher._worth_sending(_result(symbol="NOK"), "NOK", "buy") is False


@pytest.mark.asyncio
async def test_a_pair_someone_asked_about_is_answered_by_whoever_they_asked(_active):
    """/room XAUUSD used to send the caller's four messages and the channel's four."""
    _active("XAUUSD")
    asked = _result(trigger="telegram")
    assert await room_publisher._worth_sending(asked, "XAUUSD", "buy") is False


@pytest.mark.asyncio
async def test_the_same_call_is_not_repeated_every_time_the_board_re_reads_it(_active):
    _active("XAUUSD")
    assert await room_publisher._worth_sending(_result(), "XAUUSD", "buy") is True
    assert await room_publisher._worth_sending(_result(), "XAUUSD", "buy") is False


@pytest.mark.asyncio
async def test_a_turn_in_the_other_direction_is_always_news(_active):
    _active("XAUUSD")
    assert await room_publisher._worth_sending(_result(), "XAUUSD", "buy") is True
    assert await room_publisher._worth_sending(
        _result(final_action="sell"), "XAUUSD", "sell"
    ) is True


@pytest.mark.asyncio
async def test_a_scope_failure_publishes_rather_than_swallowing_the_call(monkeypatch):
    """Losing a real signal to an infrastructure error is the worse failure."""
    async def _boom(db, symbol):
        raise RuntimeError("db down")

    monkeypatch.setattr(scope, "is_active", _boom)
    assert await room_publisher._worth_sending(_result(), "XAUUSD", "buy") is True


def test_a_symbol_is_matched_however_it_is_spelled():
    assert scope.normalise("xau/usd") == scope.normalise("XAUUSD")


def test_the_room_never_convenes_on_something_that_is_not_an_instrument():
    from app.workers.room_worker import _tradeable

    assert _tradeable("XAUUSD") and _tradeable("CADJPY") and _tradeable("BTCUSDT")
    assert not _tradeable("NOK") and not _tradeable("PLEASE")


# ── Building the set ─────────────────────────────────────────────────────────

def test_parse_noise_never_counts_as_something_the_desk_is_working_on():
    """Every table feeding the scope carries strings that came out of a parser."""
    from app.agents.scope import _is_instrument

    assert _is_instrument("XAUUSD") and _is_instrument("BTCUSDT")
    for junk in ("NOK", "07", "TAKE", "ON", "AKE"):
        assert not _is_instrument(junk), f"{junk} is not an instrument"


def test_a_doubled_quote_leg_still_matches_the_position_it_names():
    """LTCUSDTUSDT is in the database; a call on LTCUSDT must match it."""
    from app.agents.scope import normalise

    assert normalise("LTCUSDTUSDT") == normalise("LTCUSDT") == "LTCUSDT"
    assert normalise("SEIUSDTUSDT") == "SEIUSDT"
    # A single quote leg is left exactly as it is.
    assert normalise("BTCUSDT") == "BTCUSDT"
    assert normalise("XAU/USD") == "XAUUSD"


@pytest.mark.asyncio
async def test_the_scope_is_what_is_pinned_held_or_being_managed(async_session):
    """A live read of the real sources — it must not raise or return junk."""
    scope.invalidate()
    symbols = await scope.active_symbols(async_session)
    assert isinstance(symbols, set)
    assert all(scope._is_instrument(s) for s in symbols)
