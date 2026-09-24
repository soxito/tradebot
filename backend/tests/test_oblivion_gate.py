"""OBLIVION is the room's main engine: no confirmed signal, no trade — and the
board can confirm or veto the engine's side, never flip it."""
from __future__ import annotations

import pytest

from app.agents import execution, oblivion_gate


def _read(*, sig=None, last=None, news=False):
    return {
        "score": {"buy": 7, "sell": 2, "scalp_min": 6, "sniper_min": 9},
        "signal": sig or {"action": "wait", "new": False, "confirmed": False},
        "last_signal": last,
        "news": {"blocked": news, "window": "13:30-14:30"},
        "bias_label": "BULLISH",
    }


_BUY = {"action": "buy", "kind": "scalp", "score": 7, "entry": 100.0, "stop_loss": 98.0,
        "tp1": 101.0, "tp2": 102.0, "tp3": 103.0, "time": 1_700_000_000,
        "bars_since": 1, "outcome": {"status": "open"}}


def test_no_read_is_a_hard_block():
    v = oblivion_gate.verdict(None)
    assert v["hard_block"] and not v["tradeable"]
    assert "no OBLIVION read" in v["reason"]


def test_a_forming_signal_is_not_tradeable():
    v = oblivion_gate.verdict(_read(sig={"action": "buy", "new": True, "confirmed": False}))
    assert not v["tradeable"]
    assert "still forming" in v["reason"]


def test_a_signal_confirmed_on_the_last_closed_bar_is_the_mandate():
    v = oblivion_gate.verdict(_read(last=_BUY), price=100.2)
    assert v["tradeable"] and v["action"] == "buy"
    assert (v["entry"], v["stop_loss"], v["tp3"]) == (100.0, 98.0, 103.0)


def test_a_stale_signal_is_not_a_mandate():
    v = oblivion_gate.verdict(_read(last={**_BUY, "bars_since": oblivion_gate.FRESH_BARS + 1}), price=100.2)
    assert not v["tradeable"]


def test_the_news_window_blocks_even_a_confirmed_signal():
    v = oblivion_gate.verdict(_read(last=_BUY, news=True), price=100.2)
    assert not v["tradeable"] and v["confirmed"]
    assert "news window" in v["reason"]


def test_price_through_the_stop_or_past_tp1_is_not_an_entry():
    assert "through its stop" in oblivion_gate.verdict(_read(last=_BUY), price=97.5)["reason"]
    assert "chasing" in oblivion_gate.verdict(_read(last=_BUY), price=101.5)["reason"]


def test_a_resolved_signal_has_nothing_left_to_enter():
    v = oblivion_gate.verdict(_read(last={**_BUY, "outcome": {"status": "tp1"}}), price=100.2)
    assert not v["tradeable"] and "resolved" in v["reason"]


def test_an_opposite_setup_forming_now_blocks_the_older_signal():
    v = oblivion_gate.verdict(
        _read(sig={"action": "sell", "new": True, "confirmed": False}, last=_BUY), price=100.2,
    )
    assert not v["tradeable"] and "opposite" in v["reason"]


def test_the_board_can_confirm_but_never_flip_the_engine():
    gate = oblivion_gate.verdict(_read(last=_BUY), price=100.2)
    assert oblivion_gate.enforce("buy", gate) == ("buy", None, "board")
    action, reason, origin = oblivion_gate.enforce("sell", gate)
    assert action == "hold" and origin == "oblivion_override_blocked" and "cannot flip" in reason
    assert oblivion_gate.enforce("hold", gate)[0] == "hold", "a veto stands"


def test_no_signal_means_the_board_cannot_trade():
    gate = oblivion_gate.verdict(_read())
    action, reason, origin = oblivion_gate.enforce("buy", gate)
    assert action == "hold" and origin == "oblivion_gate"


def test_oblivion_levels_are_the_order_levels_and_the_board_may_only_tighten():
    v = {"stop_loss": 98.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0}
    stop, tp, src = execution.oblivion_levels(v, "buy", 100.0, 95.0, 110.0, "board levels")
    assert (stop, tp) == (98.0, 103.0), "a wider board stop and further target are replaced"
    stop, _, src = execution.oblivion_levels(v, "buy", 100.0, 99.0, 110.0, "board levels")
    assert stop == 99.0 and "tightened" in src
    stop, _, _ = execution.oblivion_levels(v, "buy", 100.0, 99.0, 110.0, "percentage fallback")
    assert stop == 98.0, "a derived stop is not a board decision"


@pytest.mark.asyncio
async def test_execution_blocks_when_the_engine_has_flipped(monkeypatch):
    async def _read_pair(symbol, tf, *, source, session_id=None, **_):
        v = oblivion_gate.verdict(_read(last={**_BUY, "action": "sell", "stop_loss": 102.0,
                                              "tp1": 99.0, "tp3": 97.0}), price=100.2)
        return {"verdict": v, "result": {}, "read_id": 7, "price": 100.2}

    monkeypatch.setattr(oblivion_gate, "read_pair", _read_pair)

    class _S:
        focus_timeframe, focus_interval_s = "1h", 300

    with pytest.raises(execution.Blocked, match="says SELL, not BUY"):
        await execution.oblivion_recheck(_S(), symbol="XAUUSD", action="buy")


@pytest.mark.asyncio
async def test_execution_refuses_a_pair_oblivion_has_not_seen(monkeypatch):
    async def _no_bars(symbol, tf, *, source, session_id=None, **_):
        return {"verdict": oblivion_gate.verdict({"error": "x"}), "result": {"error": "only 3 bars"},
                "read_id": None, "price": None}

    async def _nothing(symbol, tf, max_age_s, db=None):
        return None

    monkeypatch.setattr(oblivion_gate, "read_pair", _no_bars)
    monkeypatch.setattr(oblivion_gate, "latest_read", _nothing)

    class _S:
        focus_timeframe, focus_interval_s = "1h", 300

    with pytest.raises(execution.Blocked, match="has not seen"):
        await execution.oblivion_recheck(_S(), symbol="XAUUSD", action="buy")


def test_a_seat_with_no_usable_answer_has_not_vetoed():
    assert oblivion_gate.abstained(None)
    assert oblivion_gate.abstained({"action": "hold", "reasoning": "", "confidence": 0})
    assert oblivion_gate.abstained({"action": "hold", "reasoning": "looks weak", "confidence": 0})
    assert not oblivion_gate.abstained({"action": "hold", "reasoning": "sweep of the high failed", "confidence": 0.6})


def test_engine_confidence_scales_the_score():
    assert oblivion_gate.engine_confidence({"score": 6}) == 0.5
    assert oblivion_gate.engine_confidence({"score": 30}) == 0.95
