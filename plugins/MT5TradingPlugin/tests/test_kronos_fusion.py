"""Kronos must actually influence sniper decisions.

Both defects these cover made Kronos look wired-in while being incapable of
changing any outcome — the worst kind of failure, because every panel showed a
forecast and nothing downstream was listening.

1. ``scalp_bot_service._kronos_direction`` divided a percentage by 100, leaving
   the score ~100x below the 0.25–0.70 veto band. A veto needed a 21.8% move on
   a 5-minute horizon; the alignment bonus came out near 0.0001.
2. ``_apply_kronos_to_signals`` keyed off the up/down/flat *label*. The forecast
   service calls anything under +/-0.15% "flat", which on a 12-bar M15 gold
   horizon is nearly every cycle, so the forecast was discarded exactly where
   the live sniper operates.
"""

from __future__ import annotations

import pytest

from plugins.MT5TradingPlugin.backend.router import _apply_kronos_to_signals
from plugins.MT5TradingPlugin.backend.services.scalp_bot_service import kronos_score_from

# The veto thresholds the scalp engine measures this score against.
_VETO_PRESETS = (0.25, 0.40, 0.55, 0.70)


# ── Scalp score scaling ──────────────────────────────────────────────────────

def test_observed_forecast_is_no_longer_rounded_to_nothing():
    """The real measured case: -0.07% at 65% confidence used to score -0.0008."""
    score = kronos_score_from(-0.07, 0.649)
    assert score < 0, "a downward forecast must score negative"
    assert abs(score) > 0.05, f"still vanishingly small ({score})"


def test_a_decisive_forecast_can_actually_veto():
    """The whole point: a strongly opposing forecast must reach the veto band."""
    score = kronos_score_from(-0.6, 0.9)
    assert abs(score) >= max(_VETO_PRESETS), (
        f"a decisive -0.6% forecast at 90% confidence scores {score}, which "
        "cannot trip even the loosest veto — Kronos would stay inert"
    )


def test_score_spans_the_engines_scale():
    """Realistic scalp forecasts must land across 0..1, not bunched at zero."""
    scores = [abs(kronos_score_from(p, 0.7)) for p in (0.05, 0.15, 0.3, 0.45, 1.0)]
    assert scores == sorted(scores), "score must rise with the predicted move"
    assert scores[0] < 0.2, "a tiny move should stay weak"
    assert scores[-1] > 0.7, "a large move should be near full conviction"


@pytest.mark.parametrize("pct,expected_sign", [(0.3, 1), (-0.3, -1)])
def test_sign_follows_the_forecast(pct, expected_sign):
    assert kronos_score_from(pct, 0.8) * expected_sign > 0


def test_no_move_is_no_conviction():
    """Confidence alone must never manufacture a direction."""
    assert kronos_score_from(0.0, 0.99) == 0.0


def test_score_stays_within_bounds():
    for pct in (-50.0, -1.0, 0.0, 1.0, 50.0):
        assert -1.0 <= kronos_score_from(pct, 1.0) <= 1.0


def test_confidence_scales_but_never_flips():
    strong = kronos_score_from(0.5, 0.9)
    weak = kronos_score_from(0.5, 0.1)
    assert strong > weak > 0, "confidence should scale conviction, not invert it"


# ── SMC signal fusion ────────────────────────────────────────────────────────

def _signals():
    return [
        {"side": "sell", "entry": 4089.95, "confidence": 0.58},
        {"side": "buy", "entry": 4054.25, "confidence": 0.50},
    ]


def test_flat_but_leaning_forecast_still_ranks_setups():
    """The measured regression: -0.07% "flat" against two sells did nothing."""
    sigs = _signals()
    _apply_kronos_to_signals(
        {"signals": sigs},
        {"direction": "flat", "pct_change": -0.07, "confidence": 0.649},
    )
    by_side = {s["side"]: s for s in sigs}
    assert by_side["sell"]["kronos_aligned"] is True, (
        "a downward lean must be recognised as agreeing with a sell"
    )
    assert by_side["buy"]["kronos_aligned"] is False
    assert by_side["sell"]["fusion_score"] > 0.58, "agreement did not lift the sell"
    assert by_side["buy"]["fusion_score"] < 0.50, "opposition did not sink the buy"
    assert sigs[0]["side"] == "sell", "aligned setup should rank first"


def test_genuine_noise_contributes_nothing():
    """A forecast of essentially zero must not fabricate a ranking signal."""
    sigs = _signals()
    _apply_kronos_to_signals(
        {"signals": sigs},
        {"direction": "flat", "pct_change": 0.001, "confidence": 0.9},
    )
    for s in sigs:
        assert s["kronos_aligned"] is None
        assert s["fusion_score"] == pytest.approx(s["confidence"])


def test_a_decisive_forecast_moves_more_than_a_faint_one():
    faint, decisive = _signals(), _signals()
    _apply_kronos_to_signals(
        {"signals": faint}, {"direction": "flat", "pct_change": -0.05, "confidence": 0.8})
    _apply_kronos_to_signals(
        {"signals": decisive}, {"direction": "down", "pct_change": -0.9, "confidence": 0.8})

    faint_sell = next(s for s in faint if s["side"] == "sell")
    decisive_sell = next(s for s in decisive if s["side"] == "sell")
    assert decisive_sell["fusion_score"] > faint_sell["fusion_score"], (
        "conviction must scale with the size of the predicted move"
    )


def test_labelled_direction_still_works():
    """The original up/down path must keep behaving as before."""
    sigs = _signals()
    _apply_kronos_to_signals(
        {"signals": sigs}, {"direction": "up", "pct_change": 0.8, "confidence": 0.7})
    by_side = {s["side"]: s for s in sigs}
    assert by_side["buy"]["kronos_aligned"] is True
    assert by_side["sell"]["kronos_aligned"] is False
    assert sigs[0]["side"] == "buy"


def test_missing_forecast_leaves_signals_untouched():
    sigs = _signals()
    original = [s["confidence"] for s in sigs]
    _apply_kronos_to_signals({"signals": sigs}, None)
    for s, conf in zip(sigs, original):
        assert s["kronos_aligned"] is None
        assert s["fusion_score"] == pytest.approx(conf)
        assert "no Kronos forecast" in s["kronos_note"]


def test_every_signal_gets_an_auditable_note():
    """A silent reshuffle is not reviewable — say why each setup moved."""
    sigs = _signals()
    _apply_kronos_to_signals(
        {"signals": sigs}, {"direction": "down", "pct_change": -0.5, "confidence": 0.75})
    for s in sigs:
        note = s["kronos_note"]
        assert "Kronos" in note and "-0.50%" in note
        assert ("agrees" in note) or ("opposes" in note)


def test_fusion_score_stays_in_range():
    sigs = [{"side": "buy", "confidence": 0.98}, {"side": "sell", "confidence": 0.02}]
    _apply_kronos_to_signals(
        {"signals": sigs}, {"direction": "up", "pct_change": 5.0, "confidence": 1.0})
    for s in sigs:
        assert 0.0 <= s["fusion_score"] <= 1.0


def test_empty_analysis_is_safe():
    _apply_kronos_to_signals({"signals": []}, {"direction": "up", "pct_change": 1.0})
    _apply_kronos_to_signals({}, {"direction": "up", "pct_change": 1.0})
