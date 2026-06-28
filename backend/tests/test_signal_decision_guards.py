"""Focused regression tests for SMC decision guardrails in compute_final_signal."""

from app.signals.pipeline import compute_final_signal


def _base_ta_data(
    *,
    ta_score: float,
    buy_ratio: float | None,
    volume_confirms: bool = True,
    agreement_met: bool = True,
) -> dict:
    indicators = {}
    if buy_ratio is not None:
        indicators["buy_ratio"] = buy_ratio

    return {
        "ta_score": ta_score,
        "ta_confidence": 0.82,
        "reasons": ["Base TA score from structure"],
        "agreement_met": agreement_met,
        "volume_confirms": volume_confirms,
        "indicators": indicators,
    }


def test_btc_headwind_downgrades_marginal_action_to_hold():
    ta_data = _base_ta_data(ta_score=0.60, buy_ratio=0.70)

    decision = compute_final_signal(
        ta_data=ta_data,
        sentiment=None,
        btc_sentiment={"score": -0.50, "confidence": 1.0, "label": "bearish"},
    )

    assert decision["action"] == "hold"
    assert decision["sentiment"]["btc_confirms"] is False
    assert any("Strong BTC macro headwind" in reason for reason in decision["reasons"])


def test_directional_order_flow_conflict_downgrades_to_hold():
    ta_data = _base_ta_data(ta_score=0.60, buy_ratio=0.50)

    decision = compute_final_signal(
        ta_data=ta_data,
        sentiment=None,
        btc_sentiment=None,
    )

    assert decision["action"] == "hold"
    assert decision["order_flow_confirmed"] is False
    assert any("Order-flow split does not confirm direction" in reason for reason in decision["reasons"])


def test_unavailable_order_flow_downgrades_marginal_action_to_hold():
    ta_data = _base_ta_data(ta_score=0.52, buy_ratio=None)

    decision = compute_final_signal(
        ta_data=ta_data,
        sentiment=None,
        btc_sentiment=None,
    )

    assert decision["action"] == "hold"
    assert decision["order_flow_confirmed"] is None
    assert any("Order-flow split unavailable" in reason for reason in decision["reasons"])


def test_aligned_order_flow_and_btc_context_keeps_actionable_signal():
    ta_data = _base_ta_data(ta_score=0.68, buy_ratio=0.62)

    decision = compute_final_signal(
        ta_data=ta_data,
        sentiment=None,
        btc_sentiment={"score": 0.45, "confidence": 1.0, "label": "bullish"},
    )

    assert decision["action"] == "buy"
    assert decision["order_flow_confirmed"] is True
    assert decision["sentiment"]["btc_confirms"] is True
    assert not any("downgraded to hold" in reason for reason in decision["reasons"])
