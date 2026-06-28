"""
Unit tests for the MT5 SMC sniper strategy engine.

Pure-logic tests (no network / DB) covering signal geometry, RR caps, bias
filtering and backtest stat integrity on deterministic synthetic candles.
"""
import math
import random

from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
    SMCStrategyEngine, candles_from_payload,
)


def _synthetic(n: int = 600, trend: float = 0.15, seed: int = 7):
    random.seed(seed)
    rows = []
    price = 2000.0
    t = 1_700_000_000
    for i in range(n):
        drift = 0.6 * math.sin(i / 40) + trend
        price = max(1500.0, price + drift * 4 + random.uniform(-6, 6))
        o = price + random.uniform(-3, 3)
        c = price + random.uniform(-3, 3)
        h = max(o, c) + abs(random.uniform(0, 5))
        l = min(o, c) - abs(random.uniform(0, 5))
        rows.append({"time": t + i * 3600, "open": o, "high": h, "low": l,
                     "close": c, "volume": random.uniform(500, 3000)})
    return candles_from_payload(rows)


def test_analyze_returns_valid_signals():
    cs = _synthetic()
    res = SMCStrategyEngine(min_rr=2.0).analyze(cs)
    assert "error" not in res or not res["error"]
    assert res["bias"] in ("bullish", "bearish", "neutral")
    assert isinstance(res["signals"], list)
    for s in res["signals"]:
        assert s["side"] in ("buy", "sell")
        assert s["rr"] >= 2.0
        # Geometry must be coherent for a resting limit + SL + TP.
        if s["side"] == "buy":
            assert s["stop_loss"] < s["entry"] < s["take_profit"]
        else:
            assert s["take_profit"] < s["entry"] < s["stop_loss"]
        assert 0.0 <= s["confidence"] <= 1.0


def test_min_rr_is_enforced():
    cs = _synthetic()
    res = SMCStrategyEngine(min_rr=3.0).analyze(cs)
    for s in res["signals"]:
        assert s["rr"] >= 3.0


def test_max_rr_caps_targets():
    cs = _synthetic()
    eng = SMCStrategyEngine(min_rr=2.0, max_rr=4.0)
    res = eng.analyze(cs)
    for s in res["signals"]:
        assert s["rr"] <= 4.0 + 1e-6


def test_not_enough_candles():
    cs = _synthetic(n=20)
    res = SMCStrategyEngine().analyze(cs)
    assert res.get("error")
    assert res["signals"] == []


def test_backtest_stats_are_consistent():
    cs = _synthetic()
    bt = SMCStrategyEngine(min_rr=2.0).backtest(cs)
    stats = bt["stats"]
    assert stats["total"] == len(bt["trades"])
    assert stats["wins"] + stats["losses"] == stats["total"]
    if stats["total"]:
        assert 0.0 <= stats["win_rate"] <= 100.0
        # Losses are SL-capped at ~ -1R; wins capped by max_rr.
        assert stats["avg_loss_r"] <= 0.0
        assert stats["max_drawdown_r"] <= 0.0


def test_backtest_guards_small_history():
    cs = _synthetic(n=50)
    bt = SMCStrategyEngine().backtest(cs)
    assert bt.get("error")
    assert bt["trades"] == []
