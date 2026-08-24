"""The room's clock, its timeframe, and what it publishes when it agrees."""
from __future__ import annotations

import pytest

from app.workers import room_worker


@pytest.fixture(autouse=True)
def _restore_worker_state():
    before = (room_worker._focus_interval_s, room_worker._focus_timeframe,
              room_worker._cooldown_s, room_worker._interval)
    yield
    (room_worker._focus_interval_s, room_worker._focus_timeframe,
     room_worker._cooldown_s, room_worker._interval) = before


# ── Cadence ──────────────────────────────────────────────────────────────────


def test_the_chosen_cadence_is_the_rooms_clock():
    """"Re-analyse the focused pair every X" is the setting that governs.

    The loop used to sleep ``min(rotation, focus)`` only while a pair was
    pinned, so a 4-hour choice silently became the 5-minute rotation default
    the moment nothing was pinned — and the setting the user actually changed
    did nothing.
    """
    room_worker.set_focus_interval(14400)
    assert room_worker._focus_interval_s == 14400
    # The rotation cooldown may not out-wait it either.
    room_worker._cooldown_s = 1800
    room_worker._last_analyzed["XAUUSD"] = 0.0
    assert room_worker._cooled_down("XAUUSD") is True


def test_a_short_cadence_still_throttles_a_pair_just_analysed():
    import time

    room_worker.set_focus_interval(3600)
    room_worker._cooldown_s = 1800
    room_worker._last_analyzed["XAUUSD"] = time.time()
    assert room_worker._cooled_down("XAUUSD") is False
    assert room_worker._focus_due("XAUUSD") is False


def test_the_cadence_has_a_floor():
    assert room_worker.set_focus_interval(1) == 60


# ── Timeframe ────────────────────────────────────────────────────────────────


def test_the_room_analyses_on_the_timeframe_the_settings_chose():
    assert room_worker.set_focus_timeframe("4h") == "4h"
    assert room_worker.get_focus_timeframe() == "4h"


def test_an_unsupported_timeframe_is_ignored_rather_than_applied():
    """A bad value must not leave the board analysing a timeframe no feed serves."""
    room_worker.set_focus_timeframe("1h")
    assert room_worker.set_focus_timeframe("7m") == "1h"
    assert room_worker.get_focus_timeframe() == "1h"


def test_the_worker_status_reports_the_timeframe_it_is_using():
    room_worker.set_focus_timeframe("15m")
    assert room_worker.room_worker_status()["focus_timeframe"] == "15m"


# ── Publishing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agreed_signal_reaches_telegram_fully_explained(monkeypatch):
    """The desk's conclusion must arrive with the argument behind it.

    The room used to publish through the generic alert service: a title, one
    sentence and three key/value pairs. That tells a trader what was decided
    and nothing about where to act on it.
    """
    from app.services import room_publisher

    sent: list[str] = []
    photos: list[bytes] = []

    class _Notifications:
        @staticmethod
        async def notify(text, db=None):
            sent.append(text)
            return True

        @staticmethod
        async def notify_photo(photo, caption="", db=None):
            photos.append(photo)
            return True

    class _RoomBridge:
        @staticmethod
        async def room_plan(symbol, timeframe, result, price=None):
            class _Overlay:
                direction, entry, stop_loss = "long", 4400.0, 4380.0
                take_profits = [4440.0, 4470.0]
            return _Overlay(), b"PNGDATA"

        @staticmethod
        def plan_levels_text(overlay):
            return "🎯 Entry 4400 · SL 4380 · TP1 4440"

        @staticmethod
        async def market_read_text(symbol, timeframe, candles):
            return "Gold is extending above the range."

        @staticmethod
        async def built_card_for(result, symbol, overlay=None, candles=None):
            class _Card:
                side, entry_low, entry_high = "buy", 4398.0, 4402.0
                stop_loss, take_profits = 4380.0, [4440.0, 4470.0]
            return _Card()

        @staticmethod
        def signal_card_module():
            class _Module:
                @staticmethod
                def render(card):
                    return "BUY XAUUSD @ 4400"
            return _Module()

        @staticmethod
        async def trade_published_card(card, symbol, price=None):
            # The publisher must ship the card whether or not an order follows.
            return {"status": "skipped", "reason": "execution is switched off"}

    import sys
    import types

    pkg = types.ModuleType("plugins.TelegramSignalNewsPlugin.backend.services")
    pkg.notifications = _Notifications
    pkg.room_bridge = _RoomBridge
    monkeypatch.setitem(
        sys.modules, "plugins.TelegramSignalNewsPlugin.backend.services", pkg
    )

    from app.services import candles as candle_source

    async def _candles(symbol, timeframe, limit=220, **kw):
        return [[i * 60_000, 1, 2, 0.5, 1.5, 10] for i in range(60)]

    monkeypatch.setattr(candle_source, "fetch", _candles)

    # The desk only interrupts about pairs it is working on; pin this one so
    # the test is about the message, not about the scope gate (which has its
    # own tests below).
    from app.agents import scope
    from app.services import room_publisher as _publisher

    async def _in_scope(db, symbol):
        return True

    monkeypatch.setattr(scope, "is_active", _in_scope)
    _publisher.forget_published()

    result = {
        "symbol": "XAUUSD",
        "timeframe": "1h",
        "final_action": "buy",
        "final_confidence": 0.72,
        "final_reasoning": "Structure, momentum and the forecast all point up.",
        "agents_used": 5,
        "ai_calls": 5,
        "price": 4400.0,
        "kronos_forecast": {
            "engine": "kronos", "direction": "up", "pct_change": 1.4,
            "confidence": 0.66, "horizon": "24×1h",
        },
        "decisions": [
            {"agent_name": "Market Analyst", "agent_role": "market_analyst",
             "action": "bullish", "confidence": 0.8, "reasoning": "Clean stack."},
            {"agent_name": "Risk Manager", "agent_role": "risk_manager",
             "action": "approve", "confidence": 0.6, "reasoning": "1% risk, stop is tight."},
        ],
    }
    consensus = {"tally": {"buy": 2, "sell": 0, "hold": 0}, "agreement": 1.0}

    assert await room_publisher.publish_meeting(result, consensus) is True

    body = "\n".join(sent)
    # The verdict, every seat's reasoning, the forecast, the levels and the card.
    assert "BUY" in body
    assert "Market Analyst" in body and "Clean stack." in body
    assert "Risk Manager" in body and "stop is tight" in body
    assert "Forecast" in body and "+1.40%" in body
    assert "Entry 4400" in body
    assert "BUY XAUUSD @ 4400" in body
    assert "Gold is extending above the range." in body
    assert photos == [b"PNGDATA"]


@pytest.mark.asyncio
async def test_a_hold_verdict_publishes_nothing(monkeypatch):
    """A room that reported every HOLD would train the user to ignore it."""
    from app.services import room_publisher

    result = {"symbol": "XAUUSD", "timeframe": "1h", "final_action": "hold"}
    assert await room_publisher.publish_meeting(result, {"tally": {}}) is False
