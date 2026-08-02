"""Macro shapes a Kronos forecast's confidence — within bounds, and out loud.

Macro is context, not a gate. These pin the two properties that keep it that
way: the multiplier can never be large enough to decide a trade on its own, and
an unavailable read leaves confidence exactly where volume put it.
"""
from __future__ import annotations

import pytest

from plugins.KronosForecastPlugin.backend.services import volume_context as vc
from plugins.KronosForecastPlugin.backend.schemas import VolumeContext


class _Bias:
    def __init__(self, applicable, normalized=0.0, reason="", lines=None):
        self.applicable = applicable
        self.normalized = normalized
        self.reason = reason
        self.lines = lines or []


def _ok_ctx() -> VolumeContext:
    return VolumeContext(
        status="OK", symbol="BTCUSDT", source="bitget", volume_unit="base",
        volume_24h=1000.0, volume_1h=50.0, hourly_mean_24h=41.7,
        relative_volume=1.2, z_score=0.4, regime="NORMAL",
        divergence="CONFIRMED_UP", divergence_bars=6, price_change_pct=0.4,
        volume_slope_norm=0.02, hours_covered=24, last_bar_time=0, age_seconds=60,
        detail="",
    )


@pytest.mark.parametrize("normalized", [-1.0, -0.5, 0.0, 0.5, 1.0, 5.0, -5.0])
def test_the_multiplier_never_leaves_its_bounds(normalized):
    """Even a nonsense bias cannot become the veto we chose not to build."""
    mult = vc.macro_multiplier("up", _Bias(True, normalized))
    assert vc.MACRO_MULT_MIN <= mult <= vc.MACRO_MULT_MAX


@pytest.mark.parametrize("macro", [None, _Bias(False), _Bias(False, -0.9)])
def test_an_unavailable_macro_read_changes_nothing(macro):
    assert vc.macro_multiplier("up", macro) == 1.0
    ctx = _ok_ctx()
    with_macro = vc.score_confidence("up", 0.9, 0.004, ctx, macro)
    without = vc.score_confidence("up", 0.9, 0.004, ctx)
    assert with_macro == pytest.approx(without)


def test_a_short_reads_the_bias_with_the_sign_flipped():
    """The bias is signed for the long side."""
    tailwind_for_longs = _Bias(True, 0.8)
    assert vc.macro_multiplier("up", tailwind_for_longs) > 1.0
    assert vc.macro_multiplier("down", tailwind_for_longs) < 1.0


def test_confidence_moves_with_the_macro_read():
    # Deliberately unsaturated inputs: at high agreement the base already hits
    # MAX_CONFIDENCE and no multiplier can show through the clamp.
    ctx = _ok_ctx()
    base = vc.score_confidence("up", 0.6, 0.02, ctx)
    better = vc.score_confidence("up", 0.6, 0.02, ctx, _Bias(True, 0.9))
    worse = vc.score_confidence("up", 0.6, 0.02, ctx, _Bias(True, -0.9))
    assert better > base > worse
    # And it stays a nudge: no more than the documented ±10%.
    assert better <= base * vc.MACRO_MULT_MAX + 1e-9
    assert worse >= base * vc.MACRO_MULT_MIN - 1e-9


def test_the_rationale_states_the_macro_position_either_way():
    ctx = _ok_ctx()
    applied = vc.direction_rationale(
        "up", ctx, agreement=0.9, dispersion=0.004, confidence=0.5,
        decision="OK", macro=_Bias(True, 0.5, lines=["DXY 99.8 (-0.20%) is offered."]),
    )
    assert any("DXY" in line for line in applied)
    assert any("Macro adjustment" in line for line in applied)
    # The arithmetic must remain reproducible from the text.
    assert any("(macro)" in line and "(volume)" in line for line in applied)

    skipped = vc.direction_rationale(
        "up", ctx, agreement=0.9, dispersion=0.004, confidence=0.5,
        decision="OK", macro=_Bias(False, reason="no USD leg"),
    )
    assert any("did not apply" in line and "no USD leg" in line for line in skipped)
