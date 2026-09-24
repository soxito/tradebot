"""The MT5 scalp bot must refuse entries in the OBLIVION news window and must
not trade against a fresh OBLIVION signal (unless the preset says so)."""
from __future__ import annotations

import random

import pytest

from plugins.MT5TradingPlugin.backend.services.scalp_strategy import ScalpBias, ScalpStrategyEngine
from plugins.MT5TradingPlugin.backend.services.smc_strategy import Candle


def _candles(n=300, step=300, t0=1_700_000_000, seed=7):
    rng = random.Random(seed)
    out, p = [], 2000.0
    for i in range(n):
        o = p
        p += 0.8 + rng.uniform(-1.5, 1.5)  # gently rising
        c = p
        out.append(Candle(time=t0 + i * step, open=o, high=max(o, c) + rng.uniform(0, 1), low=min(o, c) - rng.uniform(0, 1),
                          close=c, volume=1000 + rng.uniform(-200, 600)))
    return out


def _stack():
    return {"M1": _candles(300, 60), "M5": _candles(300, 300), "H1": _candles(300, 3600),
            "H4": _candles(300, 14400), "D1": _candles(300, 86400)}


def _bias(**over) -> ScalpBias:
    base = dict(direction="buy", confidence=0.8, tf_bias={"M5": "bullish", "H1": "bullish"}, atr_m5=2.0,
                rsi_m5=60.0, volume_z=0.5, buy_pressure_pct=60.0, sell_pressure_pct=40.0,
                volume_imbalance=0.3, reason="test")
    base.update(over)
    return ScalpBias(**base)


def test_compute_bias_reports_the_oblivion_read():
    eng = ScalpStrategyEngine(symbol="XAUUSD", primary_tf="M5")
    bias = eng.compute_bias(_stack())
    assert "oblivion" in bias.reason
    assert -1.0 <= bias.oblivion_score <= 1.0
    assert bias.oblivion_signal in ("buy", "sell", "wait")
    assert isinstance(bias.news_blocked, bool)
    assert bias.oblivion_session in ("ASIA", "LONDON", "NEW YORK", "LONDON + NY", "OFF")


def test_news_window_blocks_the_entry(monkeypatch):
    eng = ScalpStrategyEngine(symbol="XAUUSD", primary_tf="M5", strictness="scalper")
    monkeypatch.setattr(eng, "compute_bias", lambda by: _bias(news_blocked=True))
    stack = _stack()
    entry, bias = eng.analyse(stack, current_price=stack["M5"][-1].close, balance=1000.0)
    assert entry is None
    assert "news window" in bias.reason


def test_fresh_opposite_oblivion_signal_vetoes_on_balanced(monkeypatch):
    eng = ScalpStrategyEngine(symbol="XAUUSD", primary_tf="M5", strictness="balanced")
    monkeypatch.setattr(eng, "compute_bias", lambda by: _bias(oblivion_signal="sell", oblivion_score=-0.8))
    stack = _stack()
    entry, bias = eng.analyse(stack, current_price=stack["M5"][-1].close, balance=1000.0)
    assert entry is None
    assert "Oblivion SELL veto" in bias.reason


def test_scalper_preset_only_weighs_an_opposite_signal(monkeypatch):
    eng = ScalpStrategyEngine(symbol="XAUUSD", primary_tf="M5", strictness="scalper")
    assert eng.oblivion_veto is False
    monkeypatch.setattr(eng, "compute_bias", lambda by: _bias(oblivion_signal="sell", oblivion_score=-0.8))
    stack = _stack()
    _entry, bias = eng.analyse(stack, current_price=stack["M5"][-1].close, balance=1000.0)
    assert "veto" not in bias.reason
    assert "weighed only" in bias.reason


def test_aligned_signal_is_recorded_as_confluence(monkeypatch):
    eng = ScalpStrategyEngine(symbol="XAUUSD", primary_tf="M5", strictness="scalper")
    monkeypatch.setattr(eng, "compute_bias", lambda by: _bias(oblivion_signal="buy", oblivion_score=0.9, oblivion_bull=8,
                                                              oblivion_plan={"action": "buy", "entry": 1.0}))
    stack = _stack()
    entry, bias = eng.analyse(stack, current_price=stack["M5"][-1].close, balance=1000.0)
    if entry is not None:
        assert any(c.startswith("oblivion:buy") for c in entry.confluence)
        assert entry.gate_results["oblivion"]["signal"] == "buy"
        assert entry.gate_results["news"] == "clear"
    else:
        # the trade can still be refused by a later gate (chop, RR); the
        # oblivion gate itself must not have been the reason
        assert "Oblivion" not in bias.reason or "veto" not in bias.reason
