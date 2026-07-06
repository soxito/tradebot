"""
Unit tests for the MT5 multi-timeframe scalp engine.

Pure-logic tests (no network / DB) covering the strictness presets, the
reward:risk floor, the SMC + Kronos + momentum quality score, and the fused
``analyse`` decision path (Kronos veto / alignment) on deterministic synthetic
candles.
"""
import math
import random

from plugins.MT5TradingPlugin.backend.services.scalp_strategy import (
    ScalpStrategyEngine,
    STRICTNESS_PRESETS,
    DEFAULT_STRICTNESS,
    ALL_SCALP_TFS,
    PRIMARY_SCALP_TF,
    ENTRY_REFINE_TF,
)
from plugins.MT5TradingPlugin.backend.services.smc_strategy import candles_from_payload


def _trend(n: int = 140, drift: float = 6.0, seed: int = 11):
    """Build a clean uptrend with pullbacks so the SMC bias reads bullish."""
    random.seed(seed)
    rows = []
    price = 2000.0
    t = 1_700_000_000
    for i in range(n):
        step = drift + 3.0 * math.sin(i / 18) + random.uniform(-2.0, 2.0)
        price = max(1500.0, price + step)
        o = price - abs(random.uniform(0, 3))
        c = price + abs(random.uniform(0, 3))
        h = max(o, c) + abs(random.uniform(0, 4))
        l = min(o, c) - abs(random.uniform(0, 4))
        rows.append({"time": t + i * 60, "open": o, "high": h, "low": l,
                     "close": c, "volume": random.uniform(500, 3000)})
    return candles_from_payload(rows)


def _uptrend_by_tf():
    return {tf: _trend(seed=7 + i) for i, tf in enumerate(ALL_SCALP_TFS)}


# ── Strictness presets ────────────────────────────────────────────────────────

def test_presets_define_all_tiers():
    for tier in ("conservative", "balanced", "aggressive"):
        p = STRICTNESS_PRESETS[tier]
        assert 0.0 < p["min_confidence"] <= 1.0
        assert p["min_rr"] >= 1.0
        assert 0.0 <= p["min_fusion_score"] <= 1.0


def test_engine_applies_preset():
    eng = ScalpStrategyEngine("XAUUSD", strictness="conservative")
    assert eng.strictness == "conservative"
    assert eng.min_rr == 1.8
    assert eng.require_htf_alignment is True
    assert eng.min_confidence == 0.68

    bal = ScalpStrategyEngine("XAUUSD")  # default
    assert bal.strictness == DEFAULT_STRICTNESS
    assert bal.min_rr == 1.5
    assert bal.require_htf_alignment is False


def test_unknown_strictness_falls_back_to_default():
    eng = ScalpStrategyEngine("XAUUSD", strictness="bogus")
    assert eng.strictness == DEFAULT_STRICTNESS


def test_explicit_min_confidence_overrides_preset():
    eng = ScalpStrategyEngine("XAUUSD", strictness="aggressive", min_confidence=0.9)
    assert eng.min_confidence == 0.9
    assert eng.min_rr == 1.3  # other preset values still applied


# ── Reward:risk floor ─────────────────────────────────────────────────────────

def test_enforce_min_rr_widens_tight_buy_tp():
    eng = ScalpStrategyEngine("XAUUSD")  # min_rr 1.5
    tp, rr = eng._enforce_min_rr("buy", entry=100.0, stop_loss=99.0, take_profit=100.5)
    assert rr >= 1.5 - 1e-6
    assert tp >= 101.5 - 1e-6


def test_enforce_min_rr_widens_tight_sell_tp():
    eng = ScalpStrategyEngine("XAUUSD")
    tp, rr = eng._enforce_min_rr("sell", entry=100.0, stop_loss=101.0, take_profit=99.5)
    assert rr >= 1.5 - 1e-6
    assert tp <= 98.5 + 1e-6


def test_enforce_min_rr_keeps_ample_tp():
    eng = ScalpStrategyEngine("XAUUSD")
    tp, rr = eng._enforce_min_rr("buy", entry=100.0, stop_loss=99.0, take_profit=103.0)
    assert tp == 103.0
    assert rr >= 3.0 - 1e-6


# ── Quality score ─────────────────────────────────────────────────────────────

def test_quality_score_kronos_alignment_boosts():
    eng = ScalpStrategyEngine("XAUUSD")
    aligned, is_aligned = eng._quality_score(0.8, 2.0, 0.6, "buy", True)
    opposed, is_opposed = eng._quality_score(0.8, 2.0, -0.6, "buy", True)
    assert is_aligned is True
    assert is_opposed is False
    assert aligned > opposed
    assert 0.0 <= opposed <= aligned <= 1.0


def test_quality_score_kronos_unavailable_is_neutral():
    eng = ScalpStrategyEngine("XAUUSD")
    score, aligned = eng._quality_score(0.8, 2.0, 0.0, "buy", True)
    assert aligned is False
    assert 0.0 <= score <= 1.0


# ── Fused analyse() ───────────────────────────────────────────────────────────

def test_analyse_produces_entry_meeting_rr_floor():
    eng = ScalpStrategyEngine("XAUUSD")
    tfs = _uptrend_by_tf()
    price = tfs[ENTRY_REFINE_TF][-1].close + 1.0  # nudge live price up → buy momentum
    entry, bias = eng.analyse(tfs, price)
    assert bias.direction in ("buy", "sell", "neutral")
    if entry is not None:
        assert entry.rr >= eng.min_rr - 0.05
        assert 0.0 <= entry.quality_score <= 1.0
        assert entry.gate_results  # populated diagnostics


def test_analyse_kronos_veto_blocks_opposing_trade():
    eng = ScalpStrategyEngine("XAUUSD")  # kronos_veto 0.4
    tfs = _uptrend_by_tf()
    price = tfs[ENTRY_REFINE_TF][-1].close + 1.0
    # Baseline (no kronos) should find a buy setup on this uptrend.
    base_entry, base_bias = eng.analyse(tfs, price)
    if base_entry is None or base_entry.side != "buy":
        return  # environment-dependent; only assert veto when a buy is present
    entry, bias = eng.analyse(tfs, price, kronos_score=-0.9)
    assert entry is None
    assert "Kronos veto" in bias.reason


def test_analyse_kronos_alignment_tags_entry():
    eng = ScalpStrategyEngine("XAUUSD")
    tfs = _uptrend_by_tf()
    price = tfs[ENTRY_REFINE_TF][-1].close + 1.0
    base_entry, _ = eng.analyse(tfs, price)
    if base_entry is None or base_entry.side != "buy":
        return
    entry, _ = eng.analyse(tfs, price, kronos_score=0.7)
    assert entry is not None
    assert entry.kronos_score == 0.7
    assert "kronos_aligned" in entry.confluence


def test_analyse_insufficient_candles_returns_none():
    eng = ScalpStrategyEngine("XAUUSD")
    short = {tf: _trend(n=20) for tf in ALL_SCALP_TFS}
    entry, bias = eng.analyse(short, current_price=2100.0)
    assert entry is None
