"""Agents must keep reporting real analysis when no LLM provider answers.

Providers are rate-limited and the shared ones are consumed hourly, so the
no-provider path is a normal operating state, not an edge case. In it the
agents must still produce a direction, a calibrated confidence and reasoning
that quotes the numbers behind it — never a blank hold.
"""
from __future__ import annotations

import pytest

from app.agents.local_analysis import (
    DECISION_THRESHOLD,
    MAX_LOCAL_CONFIDENCE,
    analyze_locally,
    memory_vocabulary,
)


def ctx(**indicators):
    """A context shaped the way the orchestrator builds it."""
    return {
        "symbol": "XAUUSD",
        "technical": {"indicators": {"price": 4300.0, **indicators}},
    }


BULLISH = dict(
    ema50=4320.0, ema200=4200.0, macd_histogram=9.0, adx=34.0,
    plus_di=30.0, minus_di=12.0, rsi=28.0, bb_pct_b=0.05,
    stoch_rsi=0.12, volume_ratio=1.9, buy_ratio=0.74,
)
BEARISH = dict(
    ema50=4200.0, ema200=4320.0, macd_histogram=-9.0, adx=34.0,
    plus_di=12.0, minus_di=30.0, rsi=76.0, bb_pct_b=0.95,
    stoch_rsi=0.9, volume_ratio=1.9, buy_ratio=0.26,
)


# ── it actually decides ──────────────────────────────────────────────────────

def test_aligned_bullish_indicators_produce_a_buy():
    d = analyze_locally(role="signal_generator", context=ctx(**BULLISH))
    assert d["action"] == "buy"
    assert d["confidence"] > 0.3
    assert d["composite_score"] > DECISION_THRESHOLD


def test_aligned_bearish_indicators_produce_a_sell():
    d = analyze_locally(role="signal_generator", context=ctx(**BEARISH))
    assert d["action"] == "sell"
    assert d["confidence"] > 0.3
    assert d["composite_score"] < -DECISION_THRESHOLD


def test_conflicting_indicators_hold_with_low_confidence():
    d = analyze_locally(role="risk_manager", context=ctx(
        ema50=4300.0, ema200=4300.0, macd_histogram=0.0, adx=12.0,
        plus_di=20.0, minus_di=20.0, rsi=50.0, bb_pct_b=0.5,
    ))
    assert d["action"] == "hold"
    assert d["confidence"] <= 0.35


# ── it never reports blank ───────────────────────────────────────────────────

def test_reasoning_quotes_the_actual_numbers_it_used():
    d = analyze_locally(role="signal_generator", context=ctx(**BULLISH))
    why = d["reasoning"]
    assert "RSI 28.0" in why
    assert "EMA50 above EMA200" in why
    assert "ADX 34.0" in why
    assert "composite" in why.lower()
    # every evaluator that fired is named
    assert len(d["evaluators"]) == 7


def test_a_hold_still_explains_itself():
    d = analyze_locally(role="risk_manager", context=ctx(rsi=50.0, bb_pct_b=0.5))
    assert d["action"] == "hold"
    assert "RSI 50.0" in d["reasoning"]
    assert d["reasoning"].strip() != ""
    assert not d["degraded"]


def test_provider_reason_is_recorded_without_replacing_the_analysis():
    d = analyze_locally(
        role="signal_generator", context=ctx(**BULLISH),
        reason="AI circuit breaker open: 429 rate limit",
    )
    assert d["action"] == "buy"           # still a real call
    assert "429" in d["reasoning"]        # provenance kept
    assert d["ai_called"] is False
    assert d["source"] == "local_analysis"


def test_missing_indicators_are_reported_honestly_not_invented():
    d = analyze_locally(role="signal_generator", context={"symbol": "XAUUSD"})
    assert d["action"] == "hold"
    assert d["confidence"] == 0.0
    assert d["degraded"] is True
    assert "no indicators" in d["reasoning"].lower()


# ── partial data still yields a read ─────────────────────────────────────────

def test_a_sparse_context_uses_whatever_is_present():
    d = analyze_locally(role="signal_generator", context=ctx(rsi=22.0))
    assert set(d["evaluators"]) == {"rsi"}
    assert d["action"] == "buy"
    assert not d["degraded"]


def test_indicators_are_found_in_a_multi_timeframe_context():
    d = analyze_locally(role="signal_generator", context={
        "symbol": "XAUUSD",
        "multi_timeframe": {"1h": {"indicators": {"price": 4300.0, **BULLISH}}},
    })
    assert d["action"] == "buy"
    assert len(d["evaluators"]) == 7


# ── calibration ──────────────────────────────────────────────────────────────

def test_local_confidence_is_capped_below_a_full_model_read():
    d = analyze_locally(role="signal_generator", context=ctx(**BULLISH))
    assert d["confidence"] <= MAX_LOCAL_CONFIDENCE


def test_disagreement_lowers_confidence_versus_full_agreement():
    aligned = analyze_locally(role="x", context=ctx(**BULLISH))
    mixed = analyze_locally(role="x", context=ctx(
        **{**BULLISH, "rsi": 74.0, "bb_pct_b": 0.93, "stoch_rsi": 0.88},
    ))
    assert mixed["confidence"] < aligned["confidence"]


def test_adx_below_twenty_contributes_no_direction():
    d = analyze_locally(role="x", context=ctx(adx=11.0, plus_di=30.0, minus_di=5.0))
    assert d["evaluators"]["adx"] == 0.0
    assert "no directional trend" in d["reasoning"]


def test_bad_indicator_values_do_not_break_the_read():
    d = analyze_locally(role="x", context=ctx(
        rsi="not-a-number", ema50=None, macd_histogram=float("nan"), bb_pct_b=0.05,
    ))
    assert d["action"] in {"buy", "sell", "hold"}
    assert "bollinger" in d["evaluators"]


# ── it speaks the brain's language ───────────────────────────────────────────

def test_vocabulary_is_taken_from_memory_not_invented():
    memory = (
        "Past read on XAUUSD: bullish bias held through the London session; "
        "price swept liquidity then printed a higher high off the order block."
    )
    terms = memory_vocabulary(memory)
    assert "bullish bias" in terms
    assert "order block" in terms
    assert "higher high" in terms


def test_memory_terms_appear_in_the_report():
    d = analyze_locally(
        role="signal_generator", context=ctx(**BULLISH),
        memory_prompt="Prior sessions describe a bullish bias with accumulation.",
    )
    assert d["memory_terms"]
    assert d["memory_terms"][0] in d["reasoning"]


def test_no_memory_still_produces_a_clean_report():
    d = analyze_locally(role="signal_generator", context=ctx(**BULLISH), memory_prompt="")
    assert d["memory_terms"] == []
    assert d["action"] == "buy"
    assert "  " not in d["reasoning"]  # no gap where the vocabulary would have gone


# ── the agent path uses it ───────────────────────────────────────────────────

def test_base_agent_safe_hold_returns_analysis_when_context_is_present():
    from app.agents.base import BaseAgent

    agent = BaseAgent(agent_id=1, name="Signal Generator", role="signal_generator",
                      system_prompt="")
    d = agent._safe_hold("AI circuit breaker open: quota", ctx(**BULLISH))
    assert d["action"] == "buy"
    assert d["ai_called"] is False
    assert d["agent_name"] == "Signal Generator"
    assert "RSI" in d["reasoning"]


def test_base_agent_safe_hold_without_context_keeps_the_old_shape():
    from app.agents.base import BaseAgent

    agent = BaseAgent(agent_id=1, name="Risk Manager", role="risk_manager",
                      system_prompt="")
    d = agent._safe_hold("no providers")
    assert d["action"] == "hold"
    assert d["error"] is True
