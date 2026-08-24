"""Market-analysis maths: ATR, directional bias, and setup construction.

These cover the accuracy rules the analysis output depends on — that a trend is
not flipped by a single indicator, that stop distance scales with volatility,
and that a setup is never published with a target on the wrong side of entry.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# jarvis.py pulls in the whole app (exchanges, DB, plugins) at import time, which
# is far more than these pure functions need. Load just the module's maths by
# stubbing the package imports it performs at module scope.
_JARVIS = Path(__file__).resolve().parents[1] / "app" / "api" / "jarvis.py"


def _load_helpers():
    """Extract the pure helper functions from jarvis.py without importing the app."""
    import ast

    tree = ast.parse(_JARVIS.read_text())
    wanted = {"_ema", "_rsi", "_atr", "_directional_bias", "_build_setup", "_price_dp"}
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    missing = wanted - {n.name for n in picked}
    assert not missing, f"helpers missing from jarvis.py: {missing}"

    mod = types.ModuleType("jarvis_helpers")
    mod.__dict__["math"] = __import__("math")
    from typing import Dict, List, Optional
    mod.__dict__.update(Dict=Dict, List=List, Optional=Optional)
    exec(compile(ast.Module(body=picked, type_ignores=[]), "<jarvis-helpers>", "exec"), mod.__dict__)
    return mod


H = _load_helpers()


# ── ATR ──────────────────────────────────────────────────────────────────────

def test_atr_scales_with_volatility():
    calm_h = [100 + i * 0.1 for i in range(40)]
    calm_l = [h - 0.5 for h in calm_h]
    wild_h = [100 + i * 0.1 for i in range(40)]
    wild_l = [h - 5.0 for h in wild_h]
    calm = H._atr(calm_h, calm_l, calm_h, 14)
    wild = H._atr(wild_h, wild_l, wild_h, 14)
    assert wild > calm * 5


def test_atr_returns_zero_when_history_too_short():
    assert H._atr([1, 2, 3], [1, 2, 3], [1, 2, 3], 14) == 0.0


# ── Directional bias ─────────────────────────────────────────────────────────

def test_overbought_rsi_does_not_flip_a_confirmed_uptrend():
    """The core regression: RSI sits overbought for long stretches in a strong
    trend, so treating it as a reversal signal shorted every rally."""
    bias, conf, _ = H._directional_bias(
        current=110, ema50=105, ema200=100, rsi=78, trend="uptrend",
    )
    assert bias == "long"
    assert conf > 0


def test_oversold_rsi_does_not_flip_a_confirmed_downtrend():
    bias, _, _ = H._directional_bias(
        current=90, ema50=95, ema200=100, rsi=22, trend="downtrend",
    )
    assert bias == "short"


def test_overbought_rsi_does_flip_a_range():
    """Mean reversion is the right read when there is no trend to ride."""
    bias, _, _ = H._directional_bias(
        current=100.5, ema50=100, ema200=100, rsi=80, trend="ranging",
    )
    assert bias == "short"


def test_order_flow_does_not_override_a_strong_trend():
    """Retail buy/sell split used to override the entire technical picture."""
    bias, _, _ = H._directional_bias(
        current=90, ema50=95, ema200=100, rsi=45, trend="downtrend",
        buy_pct=65, sell_pct=35,
    )
    assert bias == "short"


def test_order_flow_can_decide_a_balanced_market():
    long_bias, _, _ = H._directional_bias(
        current=100, ema50=100, ema200=100, rsi=50, trend="ranging",
        buy_pct=85, sell_pct=15,
    )
    short_bias, _, _ = H._directional_bias(
        current=100, ema50=100, ema200=100, rsi=50, trend="ranging",
        buy_pct=15, sell_pct=85,
    )
    assert long_bias == "long"
    assert short_bias == "short"


def test_confidence_is_higher_when_signals_agree():
    agree, conf_agree, _ = H._directional_bias(
        current=110, ema50=106, ema200=100, rsi=60, trend="uptrend", buy_pct=75, sell_pct=25,
    )
    _, conf_mixed, _ = H._directional_bias(
        current=110, ema50=106, ema200=100, rsi=60, trend="uptrend", buy_pct=25, sell_pct=75,
    )
    assert agree == "long"
    assert conf_agree > conf_mixed


def test_confidence_stays_in_range():
    for rsi in (5, 30, 50, 70, 95):
        for trend in ("uptrend", "downtrend", "ranging"):
            _, conf, _ = H._directional_bias(100, 100, 100, rsi, trend, 50, 50)
            assert 0.0 <= conf <= 1.0


# ── Setup construction ───────────────────────────────────────────────────────

def test_long_setup_is_correctly_ordered():
    s = H._build_setup("long", current=100, swing_high=110, swing_low=90, atr=3)
    assert s is not None
    assert s["sl"] < s["entry"] < s["tp1"] < s["tp2"]
    assert s["rr1"] > 0 and s["rr2"] > s["rr1"]


def test_short_setup_is_correctly_ordered():
    s = H._build_setup("short", current=100, swing_high=110, swing_low=90, atr=3)
    assert s is not None
    assert s["sl"] > s["entry"] > s["tp1"] > s["tp2"]
    assert s["rr1"] > 0 and s["rr2"] > s["rr1"]


def test_long_entry_never_sits_above_current_price():
    """A long limit above market fills instantly at a worse price."""
    s = H._build_setup("long", current=91, swing_high=110, swing_low=90, atr=8)
    assert s is None or s["entry"] <= 91


def test_short_entry_never_sits_below_current_price():
    s = H._build_setup("short", current=109, swing_high=110, swing_low=90, atr=8)
    assert s is None or s["entry"] >= 109


def test_targets_never_sit_behind_current_price():
    """The real XAUUSD case: price 4376, pullback entry 4321, TP1 4369.

    Ordering against the *entry* was satisfied, so this passed every check and
    shipped — but TP1 sat below where price was already trading. "Buy the dip at
    4321, take profit at 4369" is an instruction to sell into a level the market
    has gone past, and it read as if the plan contradicted the chart beside it.
    """
    s = H._build_setup("long", current=4376.0, swing_high=4447.0, swing_low=4305.0, atr=32.25)
    assert s is not None
    assert s["entry"] < 4376.0, "a pullback entry should still sit below market"
    assert s["tp1"] > 4376.0, f"TP1 {s['tp1']} is behind current price"
    assert s["tp2"] > s["tp1"]

    t = H._build_setup("short", current=4376.0, swing_high=4447.0, swing_low=4305.0, atr=32.25)
    assert t is not None
    assert t["entry"] > 4376.0
    assert t["tp1"] < 4376.0, f"TP1 {t['tp1']} is behind current price"
    assert t["tp2"] < t["tp1"]


@pytest.mark.parametrize("bias", ["long", "short"])
def test_reward_to_risk_is_measured_from_the_published_levels(bias):
    """R:R must describe the levels actually quoted, not the ones first computed."""
    s = H._build_setup(bias, current=4376.0, swing_high=4447.0, swing_low=4305.0, atr=32.25)
    assert s is not None
    risk = abs(s["entry"] - s["sl"])
    reward1 = abs(s["tp1"] - s["entry"])
    assert s["rr1"] == pytest.approx(reward1 / risk, abs=0.05)


def test_degenerate_range_yields_no_setup_rather_than_a_fake_one():
    """Previously this produced targets on the wrong side of entry, which
    ``abs()`` then reported as a healthy positive reward-to-risk."""
    assert H._build_setup("long", current=100, swing_high=100, swing_low=100, atr=0) is None
    assert H._build_setup("short", current=100, swing_high=100, swing_low=100, atr=0) is None


def test_stop_distance_tracks_atr():
    tight = H._build_setup("long", current=100, swing_high=110, swing_low=90, atr=1)
    wide  = H._build_setup("long", current=100, swing_high=110, swing_low=90, atr=6)
    assert tight and wide
    assert (tight["entry"] - tight["sl"]) < (wide["entry"] - wide["sl"])


def test_falls_back_to_range_when_atr_unavailable():
    s = H._build_setup("long", current=100, swing_high=110, swing_low=90, atr=0)
    assert s is not None
    assert s["sl"] < s["entry"] < s["tp1"] < s["tp2"]


@pytest.mark.parametrize("bias", ["long", "short"])
def test_reported_rr_matches_the_actual_levels(bias):
    """R:R must be derived from the published prices, not computed separately."""
    s = H._build_setup(bias, current=100, swing_high=112, swing_low=88, atr=2.5)
    assert s is not None
    if bias == "long":
        risk, reward1, reward2 = s["entry"] - s["sl"], s["tp1"] - s["entry"], s["tp2"] - s["entry"]
    else:
        risk, reward1, reward2 = s["sl"] - s["entry"], s["entry"] - s["tp1"], s["entry"] - s["tp2"]
    assert s["rr1"] == pytest.approx(reward1 / risk, abs=0.15)
    assert s["rr2"] == pytest.approx(reward2 / risk, abs=0.15)
