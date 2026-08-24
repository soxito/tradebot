"""A published signal must be one a trader can actually fill.

The card is what gets copied into a terminal, so the arithmetic is checked here
rather than trusted: a target behind the entry has already "hit" the moment the
trade opens, and a stop on the wrong side of the band is not a stop at all.
Models produce both often enough that the discard path matters more than the
happy one — a malformed plan is dropped, never tidied into something that looks
tradeable but never was.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.TelegramSignalNewsPlugin.backend.services import signal_card  # noqa: E402

SELL = {
    "action": "sell",
    "confidence": 0.78,
    "entry_zone": [4403, 4405],
    "take_profits": [4400, 4397, 4394, 4391, 4388, 4385],
    "stop_loss": 4411,
    "reaction_zone": {
        "side": "buy",
        "low": 4371,
        "high": 4374,
        "stop_loss": 4365,
        "take_profits": [4379, 4389, 4404],
        "note": "The 4371 - 4374 zone is a key reaction area.",
    },
}


def _card(overrides: dict | None = None, **kwargs):
    decision = {**SELL, **(overrides or {})}
    return signal_card.build(
        decision,
        symbol=kwargs.pop("symbol", "XAUUSD"),
        price=kwargs.pop("price", 4404.0),
        analysis_number=kwargs.pop("analysis_number", 9),
        at=kwargs.pop("at", datetime(2026, 8, 13)),
    )


# ── The published shape ──────────────────────────────────────────────────────

def test_it_publishes_the_format_the_channel_uses():
    body = signal_card.render(_card())
    assert body.splitlines()[0] == "#XAUUSD <b>Sell 4403/4405</b>"
    assert "TP 4400" in body and "TP 4385" in body
    assert "SL 4411" in body
    assert "<b>Analysis 9 💫 AUGUST 13 - XAUUSD</b>" in body
    assert "🟢 BUY ZONE 4371 - 4374" in body
    assert "🔸 Stoploss: 4365" in body
    assert "🔹 Takeprofit 1: 4379" in body
    assert "🔹 Takeprofit 3: 4404" in body


def test_the_ladder_is_published_in_the_order_it_would_be_hit():
    card = _card({"take_profits": [4385, 4394, 4400, 4391, 4397, 4388]})
    assert card.take_profits == [4400, 4397, 4394, 4391, 4388, 4385]


def test_a_long_ladder_is_capped():
    card = _card({"take_profits": [4400 - i for i in range(12)]})
    assert len(card.take_profits) == signal_card.MAX_TAKE_PROFITS


def test_a_buy_reads_the_other_way_round():
    card = _card({
        "action": "buy", "entry_zone": [4371, 4374], "stop_loss": 4365,
        "take_profits": [4379, 4389, 4404], "reaction_zone": None,
    })
    assert card.take_profits == [4379, 4389, 4404]
    assert signal_card.render(card).splitlines()[0] == "#XAUUSD <b>Buy 4371/4374</b>"


def test_prices_are_published_without_trailing_noise():
    card = _card({"entry_zone": [4403.0, 4405.0]})
    assert "4403/4405" in signal_card.render(card)


# ── Plans that must not be published ─────────────────────────────────────────

def test_a_hold_publishes_nothing():
    assert _card({"action": "hold"}) is None


def test_a_target_behind_the_entry_is_dropped():
    """4410 is above a sell entered at 4403 — it 'hits' by losing money."""
    card = _card({"take_profits": [4410, 4400, 4397]})
    assert card.take_profits == [4400, 4397]


def test_a_plan_whose_targets_are_all_behind_the_entry_is_refused():
    assert _card({"take_profits": [4410, 4420]}) is None


def test_a_stop_on_the_wrong_side_is_refused():
    """A sell stopped at 4390 is stopped out below its own first target."""
    assert _card({"stop_loss": 4390}) is None


def test_a_missing_stop_is_refused():
    assert _card({"stop_loss": None}) is None


def test_an_entry_band_wider_than_one_order_is_narrowed():
    """A 50-point "zone" is a different trade at each end, on one shared stop."""
    card = _card({"entry_zone": [4380, 4430], "stop_loss": 4440})
    width = card.entry_high - card.entry_low
    assert width == pytest.approx(4405 * signal_card.MAX_ZONE_WIDTH, rel=1e-6)
    assert card.entry_low < 4405 < card.entry_high


def test_narrowing_does_not_rescue_a_stop_inside_the_band():
    """Refusing beats publishing a stop the entry itself would trigger."""
    assert _card({"entry_zone": [4380, 4430], "stop_loss": 4411}) is None


def test_a_single_entry_price_still_publishes_a_band():
    card = _card({"entry_zone": None, "entry_price": 4404})
    assert card.entry_low < 4404 < card.entry_high


def test_nothing_to_enter_on_publishes_nothing():
    assert _card({"entry_zone": None, "entry_price": None}, price=None) is None


# ── The reaction zone is optional and separately validated ───────────────────

def test_a_card_without_a_reaction_zone_publishes_just_the_signal():
    body = signal_card.render(_card({"reaction_zone": None}))
    assert "Analysis" not in body
    assert body.strip().endswith("SL 4411")


def test_an_incoherent_reaction_zone_is_dropped_but_the_signal_survives():
    """A buy zone stopped above itself is nonsense; the sell above it is not."""
    card = _card({"reaction_zone": {**SELL["reaction_zone"], "stop_loss": 4380}})
    assert card is not None
    assert card.zone is None
    assert "BUY ZONE" not in signal_card.render(card)


def test_a_zone_note_is_escaped_not_rendered():
    card = _card({"reaction_zone": {**SELL["reaction_zone"], "note": "<b>hi</b>"}})
    assert "&lt;b&gt;hi&lt;/b&gt;" in signal_card.render(card)


def test_the_heading_survives_a_missing_analysis_number():
    body = signal_card.render(_card(analysis_number=None))
    assert "Analysis 💫 AUGUST 13 - XAUUSD" in body


# ── The room only publishes a card when there is one ─────────────────────────

@pytest.mark.asyncio
async def test_the_room_publishes_the_signal_generators_call():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    result = {
        "final_action": "sell", "price": 4404.0,
        "decisions": [{"agent_role": "signal_generator", **SELL}],
    }
    body = await room_bridge.signal_card_for(result, "XAUUSD")
    assert body is not None and body.startswith("#XAUUSD <b>Sell")


@pytest.mark.asyncio
async def test_the_room_stays_quiet_when_the_seat_held():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    result = {
        "final_action": "hold", "price": 4404.0,
        "decisions": [{"agent_role": "signal_generator", "action": "hold"}],
    }
    assert await room_bridge.signal_card_for(result, "XAUUSD") is None


@pytest.mark.asyncio
async def test_a_card_that_contradicts_the_room_is_withheld():
    """Two opposite instructions in consecutive messages is worse than one."""
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    result = {
        "final_action": "buy", "price": 4404.0,
        "decisions": [{"agent_role": "signal_generator", **SELL}],
    }
    assert await room_bridge.signal_card_for(result, "XAUUSD") is None


@pytest.mark.asyncio
async def test_a_seat_call_under_a_hold_verdict_still_publishes():
    """HOLD is the room declining to add, not a veto on the seat's own read."""
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    result = {
        "final_action": "hold", "price": 4404.0,
        "decisions": [{"agent_role": "signal_generator", **SELL}],
    }
    assert (await room_bridge.signal_card_for(result, "XAUUSD")) is not None


# ── The card the room publishes when the agents had no levels ────────────────
#
# This is the common case, not the edge one: when every connected provider is
# rate-limited the seats fall back to a local read, which carries a direction
# and no prices. The chart is already drawing a plan for exactly that, so the
# message publishes those same levels rather than saying nothing.

def _overlay(**kw):
    from plugins.AiMarketAnalyst.backend.services.chart_render import PlanOverlay

    base = dict(
        direction="long", entry=4391.16, stop_loss=4364.13,
        take_profits=[4425.16, 4450.0, 4475.0],
        fib_golden_zone={"low": 4356.81, "high": 4369.86},
        resistance_zones=[{"low": 4425.1, "high": 4434.38}],
        support_zones=[{"low": 4358.6, "high": 4364.13}],
    )
    base.update(kw)
    return PlanOverlay(**base)


def _local_read(action="buy"):
    return {
        "final_action": "hold", "price": 4391.16,
        "decisions": [{"agent_role": "signal_generator", "action": action,
                       "confidence": 0.3, "reasoning": "local technical read"}],
    }


@pytest.mark.asyncio
async def test_a_local_read_still_publishes_the_drawn_plan():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    body = await room_bridge.signal_card_for(_local_read(), "XAUUSD", _overlay())
    assert body is not None
    assert body.startswith("#XAUUSD <b>Buy ")
    assert "TP 4425.16" in body
    assert "SL 4364.13" in body


@pytest.mark.asyncio
async def test_the_published_entry_is_a_band_not_a_single_tick():
    """One price is not an order anybody catches."""
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    body = await room_bridge.signal_card_for(_local_read(), "XAUUSD", _overlay())
    head = body.splitlines()[0]
    low, high = head.split()[-1].replace("</b>", "").split("/")
    assert float(low) < 4391.16 < float(high)


@pytest.mark.asyncio
async def test_the_reaction_zone_comes_from_the_drawn_structure():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    body = await room_bridge.signal_card_for(_local_read(), "XAUUSD", _overlay())
    assert "🟢 BUY ZONE 4356.81 - 4369.86" in body      # the fib golden zone
    assert "🔹 Takeprofit 1: 4434.38" in body            # the band above it


@pytest.mark.asyncio
async def test_no_structure_means_no_zone_but_still_a_signal():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    body = await room_bridge.signal_card_for(
        _local_read(), "XAUUSD", _overlay(fib_golden_zone=None),
    )
    assert body is not None and "BUY ZONE" not in body


@pytest.mark.asyncio
async def test_a_held_seat_publishes_nothing_even_with_a_drawn_plan():
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    assert await room_bridge.signal_card_for(
        _local_read("hold"), "XAUUSD", _overlay(),
    ) is None
