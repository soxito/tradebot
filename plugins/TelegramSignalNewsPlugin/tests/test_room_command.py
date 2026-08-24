"""/room hands work to the trading room without blocking the bot.

The room takes minutes. The two properties that matter are that the command
returns straight away (so the polling loop keeps serving everyone else) and
that nothing the agents did not actually decide ends up drawn on a chart.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.AiMarketAnalyst.backend.services.chart_render import PlanOverlay, render_plan_chart
from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs
from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge as rb


class _FakeDB:
    pass


# ── Argument parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(("args", "expected"), [
    ("BTCUSDT 4h", ("BTCUSDT", "4h", "")),
    ("ETH/USDT 15m", ("ETH/USDT", "15m", "")),
    ("XAUUSD", ("XAUUSD", "1h", "")),
    ("", (None, "1h", "")),
])
def test_pair_and_timeframe_are_recognised(args, expected):
    assert rb.parse_args(args) == expected


@pytest.mark.parametrize("args", [
    "CADJPY", "CADJPY 1h", "cadjpy 4h", "GBPJPY", "AUDCAD 15m", "USDZAR",
    "NZDCHF 1d", "EURAUD", "US30 15m", "NAS100", "usoil", "XAGUSD 4h",
    "gold 4h", "silver", "dow 1h",
])
def test_every_cross_index_and_commodity_reaches_the_board(args):
    """The old regex knew seven currency bases; CADJPY was answered as chat.

    A token the platform can price is a request for analysis, not a question —
    if the symbol comes back None the command falls through to the free-text
    path and the user gets a description of the pair instead of a plan.
    """
    symbol, _tf, rest = rb.parse_args(args)
    assert symbol, f"{args!r} was read as free text, not as an instrument"
    assert rest == ""


@pytest.mark.parametrize("args", [
    "why is gold selling off?",
    "what do you make of the dollar right now",
    "is oil worth a look this week",
])
def test_a_question_about_a_market_is_still_a_question(args):
    """A plain-English name inside a sentence must not convene the room."""
    symbol, _tf, rest = rb.parse_args(args)
    assert symbol is None
    assert rest == args


def test_a_pair_named_mid_sentence_still_gets_the_full_review():
    """"is CADJPY a buy?" is a request for a plan, and reads as one."""
    assert rb.parse_args("is CADJPY a buy?")[0] == "CADJPY"


# ── Reading the timeframe off a screenshot ───────────────────────────────────

@pytest.mark.parametrize(("label", "expected"), [
    # Exactly as TradingView mobile prints it beside the symbol.
    ("4H", "4h"), ("1H", "1h"), ("30m", "30m"), ("15m", "15m"),
    ("1D", "1d"), ("1W", "1w"), ("5m", "5m"),
    # MT5 wording for the same charts.
    ("H4", "4h"), ("H1", "1h"), ("M30", "30m"), ("M15", "15m"),
    ("D1", "1d"), ("W1", "1w"),
    # TradingView's numeric codes.
    ("240", "4h"), ("60", "1h"), ("30", "30m"),
    # Written out.
    ("Daily", "1d"), ("weekly", "1w"), ("1 hour", "1h"),
    # The whole toolbar string, symbol and all.
    ("XAUUSD 4H", "4h"), ("XAUUSD  1W", "1w"),
])
def test_every_way_a_chart_writes_its_timeframe_is_understood(label, expected):
    assert rb.normalize_timeframe(label) == expected


def test_an_unreadable_timeframe_falls_back_rather_than_breaking_the_feed():
    """A label we cannot parse must not reach the provider as-is."""
    for junk in ("", None, "banana", "??"):
        assert rb.normalize_timeframe(junk) == "1h"
    assert rb.normalize_timeframe("banana", default="4h") == "4h"


def test_a_weekly_chart_is_never_silently_read_as_hourly():
    """The old map knew only five aliases; W1 fell through to the 1h default."""
    for weekly in ("1W", "W1", "weekly", "1week"):
        assert rb.normalize_timeframe(weekly) == "1w"


@pytest.mark.asyncio
@pytest.mark.parametrize(("title", "tf_label", "expected_tf"), [
    ("Gold Spot / U.S. Dollar", "4H", "4h"),
    ("Gold Spot / U.S. Dollar", "1H", "1h"),
    ("Gold Spot / U.S. Dollar", "30m", "30m"),
    ("Gold Spot / U.S. Dollar", "15m", "15m"),
    ("Gold Spot / U.S. Dollar", "1D", "1d"),
    ("Gold Spot / U.S. Dollar", "1W", "1w"),
])
async def test_a_screenshot_convenes_the_room_on_what_it_shows(
    monkeypatch, title, tf_label, expected_tf
):
    """The pair and candle size come off the image, not off a default."""
    seen: dict = {}

    class _Vision:
        narrative = "structure read"
        findings = {"instrument": "XAUUSD", "timeframe": tf_label}

    async def _fake_read(*a, **kw):
        return _Vision()

    async def _fake_run_pair(token, chat_id, symbol, timeframe):
        seen.update(symbol=symbol, timeframe=timeframe)

    sent: list[str] = []

    async def _fake_send(token, chat_id, text, *a, **kw):
        sent.append(text)

    monkeypatch.setattr(
        "plugins.AiMarketAnalyst.backend.services.vision.read_image", _fake_read
    )
    monkeypatch.setattr(
        "plugins.AiMarketAnalyst.backend.services.chart_annotate.annotate",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(rb, "run_pair", _fake_run_pair)
    monkeypatch.setattr(
        "plugins.TelegramSignalNewsPlugin.backend.services.bot_service.send_message",
        _fake_send,
    )

    await rb.run_context("tok", "1", "read this", image=(b"\x89PNG fake", "image/png"))

    assert seen == {"symbol": "XAUUSD", "timeframe": expected_tf}
    assert any(expected_tf in m for m in sent), "the user is told which chart was read"


@pytest.mark.asyncio
async def test_a_title_the_model_did_not_convert_is_still_resolved(monkeypatch):
    """The prompt asks for a symbol, but "XAU/USD" must not reach the feed raw."""
    seen: dict = {}

    class _Vision:
        narrative = "n"
        findings = {"instrument": "XAU/USD", "timeframe": "H4"}

    monkeypatch.setattr(
        "plugins.AiMarketAnalyst.backend.services.vision.read_image",
        lambda *a, **kw: _async(_Vision()),
    )
    monkeypatch.setattr(
        "plugins.AiMarketAnalyst.backend.services.chart_annotate.annotate",
        lambda *a, **kw: None,
    )

    async def _fake_run_pair(token, chat_id, symbol, timeframe):
        seen.update(symbol=symbol, timeframe=timeframe)

    monkeypatch.setattr(rb, "run_pair", _fake_run_pair)
    monkeypatch.setattr(
        "plugins.TelegramSignalNewsPlugin.backend.services.bot_service.send_message",
        lambda *a, **kw: _async(None),
    )

    await rb.run_context("tok", "1", "", image=(b"x", "image/png"))
    assert seen["symbol"] == "XAUUSD"
    assert seen["timeframe"] == "4h"


async def _async(value):
    return value


def test_a_question_is_not_mistaken_for_a_pair():
    """"why is gold selling off" must reach the agents as a question."""
    symbol, _tf, question = rb.parse_args("why is gold selling off?")
    assert symbol is None
    assert question == "why is gold selling off?"


# ── Dispatch is non-blocking ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_command_acknowledges_without_waiting(monkeypatch):
    """The ack must come back while the agents are still working."""
    started = asyncio.Event()
    spawned: dict = {}

    async def slow_job(token, chat_id, symbol, timeframe):
        spawned["symbol"] = symbol
        spawned["timeframe"] = timeframe
        started.set()
        await asyncio.sleep(30)  # far longer than any reply may take

    monkeypatch.setattr(rb, "run_pair", slow_job)

    update = {"message": {"chat": {"id": 42}, "text": "/room BTCUSDT 4h"}}
    reply, mode, _ = await asyncio.wait_for(
        cs.parse_and_execute(update, "tok", [], _FakeDB()), timeout=2
    )

    assert "BTCUSDT" in reply and "4h" in reply
    await asyncio.wait_for(started.wait(), timeout=2)
    assert spawned == {"symbol": "BTCUSDT", "timeframe": "4h"}
    assert rb.is_busy("42")
    rb._BUSY.discard("42")


@pytest.mark.asyncio
async def test_second_request_is_refused_while_the_room_sits(monkeypatch):
    """Two concurrent rooms would double the spend and interleave the answers."""
    monkeypatch.setattr(rb, "run_pair", lambda *a, **kw: asyncio.sleep(30))
    update = {"message": {"chat": {"id": 77}, "text": "/room BTCUSDT"}}

    first, _, _ = await cs.parse_and_execute(update, "tok", [], _FakeDB())
    second, _, _ = await cs.parse_and_execute(update, "tok", [], _FakeDB())

    assert "Convening" in first
    assert "still sitting" in second
    rb._BUSY.discard("77")


@pytest.mark.asyncio
async def test_bare_room_command_explains_itself():
    reply, _, _ = await cs.parse_and_execute(
        {"message": {"chat": {"id": 43}, "text": "/room"}}, "tok", [], _FakeDB()
    )
    assert "/room" in reply and "chart image" in reply
    assert not rb.is_busy("43")


# ── Levels come from the agents, never from nowhere ──────────────────────────

def test_a_hold_verdict_draws_no_entry_plan():
    """Agents that decided nothing must not produce stop/target lines."""
    overlay = rb.overlay_from_result({"final_action": "hold", "decisions": []}, price=100.0)
    assert overlay.stop_loss is None
    assert overlay.take_profits == []
    assert overlay.projection == []


def test_absolute_executor_levels_win_over_percentages():
    result = {
        "final_action": "buy",
        "decisions": [
            {"agent_role": "trade_executor", "entry": 100.0, "stop_loss": 95.0,
             "take_profit": 120.0},
            {"agent_role": "signal_generator", "entry_price": 999.0,
             "stop_loss_pct": 50.0, "take_profit_pct": 50.0},
        ],
    }
    overlay = rb.overlay_from_result(result, price=101.0)
    assert (overlay.entry, overlay.stop_loss, overlay.take_profits) == (100.0, 95.0, [120.0])


def test_percentages_are_used_when_that_is_all_the_agents_gave():
    result = {
        "final_action": "sell",
        "decisions": [{"agent_role": "signal_generator", "entry_price": 200.0,
                       "stop_loss_pct": 2.0, "take_profit_pct": 4.0}],
    }
    overlay = rb.overlay_from_result(result, price=200.0)
    assert overlay.direction == "short"
    assert overlay.stop_loss == pytest.approx(204.0)   # stop sits above a short
    assert overlay.take_profits[0] == pytest.approx(192.0)


# ── Rendering ────────────────────────────────────────────────────────────────

def _candles(n: int = 60, start: float = 100.0) -> list[list[float]]:
    return [[i, start + i, start + i + 2, start + i - 2, start + i + 1, 10.0] for i in range(n)]


def test_chart_renders_a_png_with_a_full_plan():
    out = render_plan_chart(
        _candles(), symbol="BTC/USDT", timeframe="1h",
        overlay=PlanOverlay(direction="long", entry=140.0, stop_loss=130.0,
                            take_profits=[160.0, 175.0],
                            order_blocks=[{"low": 128.0, "high": 136.0, "kind": "bullish"}],
                            projection=[145.0, 155.0, 165.0]),
    )
    assert out and out[:8] == b"\x89PNG\r\n\x1a\n"


def test_market_context_is_drawn_whatever_the_agents_decided():
    """Fib and structure describe the market, not a trade — a HOLD still gets them."""
    out = render_plan_chart(
        _candles(), symbol="BTC/USDT", timeframe="1h",
        overlay=PlanOverlay(
            entry=140.0,
            ema={20: [None] * 5 + [130.0 + i for i in range(55)]},
            fib_levels=[{"ratio": 0.618, "price": 138.0, "label": "61.8%"}],
            fib_golden_zone={"low": 136.0, "high": 139.0},
            support_zones=[{"low": 120.0, "high": 124.0}],
            resistance_zones=[{"low": 158.0, "high": 162.0}],
        ),
    )
    assert out and out[:8] == b"\x89PNG\r\n\x1a\n"


# ── The ATR fallback ─────────────────────────────────────────────────────────

def test_a_direction_with_no_levels_gets_the_standard_atr_frame():
    """A buy call with no risk drawn is the one gap worth filling ourselves."""
    result = {"final_action": "buy",
              "decisions": [{"agent_role": "trade_executor", "entry": 100.0}]}
    overlay = rb.overlay_from_result(result, price=100.0, context={"atr": 2.0})
    assert overlay.stop_loss == pytest.approx(97.0)      # 1.5x ATR below
    assert overlay.take_profits[0] == pytest.approx(106.0)  # 3x ATR above


def test_the_atr_frame_never_overrides_what_the_agents_said():
    result = {
        "final_action": "buy",
        "decisions": [{"agent_role": "trade_executor", "entry": 100.0,
                       "stop_loss": 95.0, "take_profit": 120.0}],
    }
    overlay = rb.overlay_from_result(result, price=100.0, context={"atr": 2.0})
    assert (overlay.stop_loss, overlay.take_profits) == (95.0, [120.0])


def test_a_hold_gets_no_atr_levels_either():
    """The fallback fills in a decided direction; it must not invent one."""
    overlay = rb.overlay_from_result(
        {"final_action": "hold", "decisions": []}, price=100.0, context={"atr": 2.0},
    )
    assert overlay.stop_loss is None
    assert overlay.take_profits == []


def test_context_layers_reach_the_overlay():
    overlay = rb.overlay_from_result(
        {"final_action": "hold", "decisions": []}, price=100.0,
        context={
            "fib_levels": [{"ratio": 0.5, "price": 99.0, "label": "50.0%"}],
            "fib_golden_zone": {"low": 98.0, "high": 100.0},
            "support_zones": [{"low": 90.0, "high": 92.0}],
            "ema": {20: [100.0]},
        },
    )
    assert overlay.fib_levels and overlay.fib_golden_zone
    assert overlay.support_zones and overlay.ema


def test_too_few_candles_render_nothing():
    """Better no chart than a chart drawn from three bars."""
    assert render_plan_chart(_candles(3), symbol="X", timeframe="1h") is None


def test_levels_far_outside_the_candles_still_fit_on_the_canvas():
    """A stop drawn off-frame is worse than no stop — the range must include it."""
    out = render_plan_chart(
        _candles(), symbol="X", timeframe="1h",
        overlay=PlanOverlay(entry=100.0, stop_loss=1.0, take_profits=[9000.0]),
    )
    assert out and out[:8] == b"\x89PNG\r\n\x1a\n"


# ── The 110-pip worth-holding floor (1 gold pip = 0.10, so 110 pips = 11.0) ──

def test_a_tight_gold_ladder_has_its_furthest_target_stretched():
    """A 3-9 point ladder banks nothing worth the hold; the last rung reaches 11."""
    result = {"final_action": "buy", "decisions": [{
        "agent_role": "signal_generator", "action": "buy",
        "entry_zone": [4390, 4392], "stop_loss": 4384,
        "take_profits": [4394, 4397, 4399],
    }]}
    overlay = rb.overlay_from_result(result, 4390.0, {"atr": 4.0}, symbol="XAUUSD")

    entry = overlay.entry
    # Near rungs untouched — they are the partial-take levels.
    assert overlay.take_profits[0] == pytest.approx(4394.0)
    assert overlay.take_profits[1] == pytest.approx(4397.0)
    # Furthest lifted to exactly 110 pips.
    assert overlay.take_profits[-1] - entry == pytest.approx(11.0)


def test_a_ladder_already_past_the_floor_is_left_alone():
    result = {"final_action": "buy", "decisions": [{
        "agent_role": "signal_generator", "action": "buy",
        "entry_zone": [4390, 4390], "stop_loss": 4384,
        "take_profits": [4395, 4405, 4420],   # furthest is 30 pts = 300 pips
    }]}
    overlay = rb.overlay_from_result(result, 4390.0, symbol="XAUUSD")
    assert overlay.take_profits == [4395, 4405, 4420]


def test_the_floor_reaches_the_other_way_for_a_sell():
    result = {"final_action": "sell", "decisions": [{
        "agent_role": "signal_generator", "action": "sell",
        "entry_zone": [4400, 4402], "stop_loss": 4408,
        "take_profits": [4398, 4396, 4394],
    }]}
    overlay = rb.overlay_from_result(result, 4401.0, {"atr": 4.0}, symbol="XAUUSD")
    assert overlay.entry - overlay.take_profits[-1] == pytest.approx(11.0)


def test_the_floor_scales_with_the_instrument_not_a_fixed_price():
    """Silver's pip is 0.01, so 110 pips = 1.1 — a tenth of gold's, not the same 11."""
    result = {"final_action": "buy", "decisions": [{
        "agent_role": "signal_generator", "action": "buy",
        "entry_zone": [31.0, 31.0], "stop_loss": 30.5,
        "take_profits": [31.1, 31.2, 31.3],
    }]}
    overlay = rb.overlay_from_result(result, 31.0, symbol="XAGUSD")
    assert overlay.take_profits[-1] - overlay.entry == pytest.approx(1.1)
