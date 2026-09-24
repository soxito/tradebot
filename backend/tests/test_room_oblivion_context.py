"""Every seat must see the OBLIVION read, and the no-LLM fallback must use it."""
from __future__ import annotations

import asyncio

import pytest

from app.agents import execution
from app.agents.local_analysis import analyze_locally
from app.agents.orchestrator import AgentOrchestrator


def _series(prices, *, spread=0.002, volume=1000.0):
    rows = []
    for i, close in enumerate(prices):
        prev = prices[i - 1] if i else close
        high = max(prev, close) * (1 + spread)
        low = min(prev, close) * (1 - spread)
        rows.append([1_700_000_000_000 + i * 3_600_000, prev, high, low, close, volume])
    return rows


def _trend(n=120, up=True):
    prices = [100.0]
    for i in range(1, n):
        prices.append(prices[-1] + ((0.9 if i % 5 else -1.2) if up else (-0.9 if i % 5 else 1.2)))
    return prices


def test_add_oblivion_puts_the_engine_read_and_news_flag_on_the_context():
    context = {"symbol": "XAUUSD", "timeframe": "1h"}
    AgentOrchestrator._add_oblivion(context, _series(_trend()), "1h")
    obl = context["oblivion"]
    assert obl["engine"] in ("oblivion_v4", "oblivion_v5")
    assert obl["summary"].startswith("OBLIVION")
    assert obl["score"]["required"] >= 1
    assert str(obl["dashboard"]["Signal"]).startswith(("BUY", "SELL", "WAIT"))
    assert isinstance(context["news_blocked"], bool)
    # chart-only history stays out of the prompt
    assert "signals_history" not in obl
    assert "labels" not in obl["structure"]


def test_add_oblivion_is_silent_on_a_short_window():
    context = {"symbol": "BTC/USDT"}
    AgentOrchestrator._add_oblivion(context, _series(_trend(30)), "1h")
    assert "oblivion" not in context


def test_local_fallback_scores_the_oblivion_read():
    context = {
        "symbol": "XAUUSD",
        "technical": {"indicators": {"price": 100.0, "atr": 1.0}},
        "oblivion": {
            "mode": "HYBRID",
            "score": {"buy": 8, "sell": 1, "scalp_min": 6, "sniper_min": 9},
            "market_bias": 1, "bias_label": "BULLISH",
            "signal": {"action": "buy", "kind": "sniper", "new": True, "confirmed": True, "score": 10,
                       "entry": 100.0, "stop_loss": 98.0, "tp1": 102.0},
            "news": {"blocked": False},
        },
    }
    out = analyze_locally(role="market_analyst", context=context)
    assert "Oblivion sniper BUY confirmed" in out["reasoning"]
    assert "buy 8/sell 1" in out["reasoning"]
    assert out["evaluators"].get("oblivion", 0) > 0.5


def test_local_fallback_signal_seat_holds_inside_the_news_window():
    context = {
        "symbol": "XAUUSD",
        "news_blocked": True,
        "technical": {"indicators": {"price": 100.0, "atr": 1.0, "ema50": 105.0, "ema200": 100.0,
                                     "macd_histogram": 1.0, "adx": 40, "plus_di": 30, "minus_di": 10}},
        "oblivion": {
            "mode": "HYBRID",
            "score": {"buy": 9, "sell": 0, "scalp_min": 6, "sniper_min": 9}, "market_bias": 1, "bias_label": "BULLISH",
            "signal": {"action": "buy", "kind": "scalp", "new": True, "confirmed": True, "score": 9,
                       "entry": 100.0, "stop_loss": 98.0, "tp1": 102.0},
            "news": {"blocked": True},
        },
    }
    out = analyze_locally(role="signal_generator", context=context)
    assert out["action"] == "hold"
    assert "NEWS WINDOW" in out["reasoning"]


def test_execution_fills_missing_levels_from_the_oblivion_plan(monkeypatch):
    async def _no_candles(*a, **k):
        return []

    from app.services import candles as candle_source
    monkeypatch.setattr(candle_source, "fetch", _no_candles)

    oblivion = {"signal": {"action": "buy", "entry": 100.0, "stop_loss": 97.0,
                           "tp1": 103.0, "tp2": 106.0, "tp3": 109.0}}
    stop, tp, source = asyncio.run(execution._levels_from([], 100.0, "buy", symbol="XAUUSD", oblivion=oblivion))
    assert stop == 97.0
    assert tp == 109.0  # furthest rung
    assert "Oblivion" in source

    # a plan for the other side is ignored → percentage fallback
    stop, tp, source = asyncio.run(execution._levels_from([], 100.0, "sell", symbol="XAUUSD", oblivion=oblivion))
    assert source == "percentage fallback"
    assert stop > 100.0 and tp < 100.0
