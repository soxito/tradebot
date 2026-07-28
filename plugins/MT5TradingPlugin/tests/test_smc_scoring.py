"""
Deterministic SMC detection + scoring tests on designed fixture candles.

Fixtures are hand-built (no randomness) so the BOS, FVG and liquidity-sweep
cases are known in advance and the assertions state exactly which structure is
expected at which price. Every scoring assertion is arithmetic on those same
candles — nothing here depends on a model, a network call, or a DB.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.services import smc_scoring  # noqa: E402
from plugins.MT5TradingPlugin.backend.services.smc_strategy import (  # noqa: E402
    Candle,
    SMCStrategyEngine,
    _atr,
    _detect_liquidity_sweeps,
    _fair_value_gaps,
    _structure_bias,
    _swings,
)

T0 = 1_700_000_000
STEP = 3600


def _build(rows) -> list[Candle]:
    return [
        Candle(time=T0 + i * STEP, open=o, high=h, low=l, close=c, volume=v)
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


def _strip_volume(candles: list[Candle]) -> list[Candle]:
    """Same price action, no volume data at all (some brokers report none)."""
    return [
        Candle(time=c.time, open=c.open, high=c.high, low=c.low,
               close=c.close, volume=0.0)
        for c in candles
    ]


# ── Fixture: a designed bullish market ───────────────────────────────────────
#
#   bars   0- 59  oscillating range ~1978-2022, baseline volume 1000
#   bars  60- 69  high-volume (3000) bullish displacement, gapping up each bar
#                 -> creates a run of bullish FVGs and a bullish CHoCH at bar 60
#   bars  70-129  oscillating drift higher on baseline volume
#                 -> forms the upper dealing range; price ends far above the FVGs
#
# The offsets use fixed arithmetic jitter (never random) so that no two adjacent
# bars tie on high/low — fractal pivot detection needs strict inequalities.

def designed_bullish_market() -> list[Candle]:
    rows = []
    for i in range(60):
        mid = 2000.0 + 22.0 * math.sin(i * 0.55) + ((i * 7) % 5) * 0.13
        o, c = mid - 2.0, mid + 2.0
        rows.append((o, max(o, c) + 3.0 + ((i * 3) % 4) * 0.11,
                     min(o, c) - 3.0 - ((i * 5) % 4) * 0.11, c, 1000.0))
    p = 2020.0
    for _ in range(10):
        o, c = p, p + 14.0
        rows.append((o, c + 1.0, o - 1.0, c, 3000.0))
        p = c + 7.0  # gap up: next.low > prev.high -> bullish FVG
    for i in range(60):
        mid = p + 18.0 * math.sin(i * 0.5) + i * 1.6 + ((i * 11) % 5) * 0.17
        o, c = mid - 2.0, mid + 2.0
        rows.append((o, max(o, c) + 3.0 + ((i * 3) % 4) * 0.13,
                     min(o, c) - 3.0 - ((i * 7) % 4) * 0.13, c, 1000.0))
    return _build(rows)


# ── Fixture: a market ending in a liquidity sweep of the lows ────────────────
#
#   bars  0-49   oscillation that plants a clear swing low near 1970
#   bars 50-64   recovery away from that low
#   bar     65   the sweep: wicks well below the swing low, closes back above it

def swept_lows_market() -> tuple[list[Candle], float]:
    rows = []
    for i in range(50):
        mid = 2000.0 + 25.0 * math.sin(i * 0.6) + ((i * 7) % 5) * 0.11
        o, c = mid - 2.0, mid + 2.0
        rows.append((o, max(o, c) + 3.0 + ((i * 3) % 4) * 0.09,
                     min(o, c) - 3.0 - ((i * 5) % 4) * 0.09, c, 1000.0))
    candles = _build(rows)
    swing_low = min(s.price for s in _swings(candles, 2, 2) if s.kind == "low")

    # Drift back toward the low without printing a new swing high, so the sweep
    # bar can only register as a downside hunt.
    p = swing_low + 15.0
    for i in range(15):
        o, c = p, p + 0.5 + (i % 3) * 0.1
        rows.append((o, c + 1.0, o - 1.0, c, 1000.0))
        p = c
    # The sweep bar: deep wick under the swing low, close back above it.
    o = p
    c = swing_low + 25.0
    rows.append((o, max(o, c) + 1.0, swing_low - 18.0, c, 900.0))
    return _build(rows), swing_low


# ── Structure detection ──────────────────────────────────────────────────────

def test_fair_value_gap_is_detected_at_the_expected_levels():
    candles = designed_bullish_market()
    fvgs = _fair_value_gaps(candles, _atr(candles))

    assert fvgs, "the high-volume displacement leg must produce FVGs"
    assert all(z.kind == "bullish_fvg" for z in fvgs)

    first = min(fvgs, key=lambda z: z.index)
    assert first.index == 60, "the first FVG forms on the first displacement bar"
    # Bullish FVG spans prev.high -> next.low around the displacement bar.
    assert first.bottom == pytest.approx(candles[59].high, abs=1e-6)
    assert first.top == pytest.approx(candles[61].low, abs=1e-6)
    assert first.top > first.bottom


def test_fair_value_gap_rejected_when_displacement_volume_is_average():
    """Same geometry, flat volume -> the engine's institutional gate rejects it."""
    candles = designed_bullish_market()
    flat = [
        Candle(time=c.time, open=c.open, high=c.high, low=c.low,
               close=c.close, volume=1000.0)
        for c in candles
    ]
    assert _fair_value_gaps(flat, _atr(flat)) == []


def test_break_of_structure_and_choch_are_detected():
    candles = designed_bullish_market()
    swings = _swings(candles, 2, 2)
    bias, events = _structure_bias(candles, swings)

    assert bias == "bullish"
    types = [e["type"] for e in events]
    assert "CHoCH" in types, "the displacement leg flips character to bullish"
    assert "BOS" in types, "subsequent higher highs are continuation breaks"

    # The character change is the first bullish event, on the displacement bar.
    choch = next(e for e in events if e["type"] == "CHoCH")
    assert choch["direction"] == "bullish"
    assert choch["index"] == 60

    # Every BOS after it continues in the same direction at a higher level.
    bos = [e for e in events if e["type"] == "BOS"]
    assert bos, "expected at least one continuation break"
    assert all(e["direction"] == "bullish" for e in bos)
    levels = [e["level"] for e in bos]
    assert levels == sorted(levels), "continuation breaks must be at rising levels"


def test_liquidity_sweep_of_lows_is_detected():
    candles, swing_low = swept_lows_market()
    swings = _swings(candles, 2, 2)
    sweeps = _detect_liquidity_sweeps(candles, swings, _atr(candles))

    assert sweeps["swept_lows"], "the final bar hunts stops below the swing low"
    assert sweeps["is_sweep_bar"] is True
    assert sweeps["sweep_direction"] == "down"
    assert round(swing_low, 6) in sweeps["swept_lows"]
    # Deep lower wick with a close back above -> high false-break probability.
    assert sweeps["rejection_wick"] > 0.55
    assert sweeps["false_break_score"] >= 60


def test_no_sweep_reported_on_a_clean_market():
    candles = designed_bullish_market()
    sweeps = _detect_liquidity_sweeps(candles, _swings(candles, 2, 2), _atr(candles))
    assert sweeps["swept_lows"] == []
    assert sweeps["swept_highs"] == []
    assert sweeps["false_break_score"] == 0.0


# ── Volume primitives are numeric comparisons, not labels ────────────────────

def test_relative_volume_is_an_exact_ratio_against_the_rolling_mean():
    rows = [(100, 101, 99, 100, 200.0) for _ in range(20)]
    rows.append((100, 101, 99, 100, 500.0))
    candles = _build(rows)
    # Mean of the 20 preceding bars is exactly 200 -> 500/200 = 2.5
    assert smc_scoring.rolling_mean_volume(candles, 20) == pytest.approx(200.0)
    assert smc_scoring.relative_volume(candles, 20) == pytest.approx(2.5)


def test_relative_volume_is_zero_when_no_volume_data_exists():
    candles = _build([(100, 101, 99, 100, 0.0) for _ in range(25)])
    assert smc_scoring.rolling_mean_volume(candles, 24) == 0.0
    assert smc_scoring.relative_volume(candles, 24) == 0.0


def test_volume_at_price_measures_concentration_in_the_band():
    # Ten bars trading 100-102, then ten trading 200-202.
    rows = [(100, 102, 100, 101, 100.0) for _ in range(10)]
    rows += [(200, 202, 200, 201, 100.0) for _ in range(10)]
    candles = _build(rows)
    low_band = smc_scoring.volume_at_price(candles, 100, 102, len(candles) - 1)
    high_band = smc_scoring.volume_at_price(candles, 200, 202, len(candles) - 1)
    assert low_band == pytest.approx(0.5, abs=0.01)
    assert high_band == pytest.approx(0.5, abs=0.01)
    # A band nothing traded in gets zero.
    assert smc_scoring.volume_at_price(candles, 150, 160, len(candles) - 1) == 0.0


def test_delta_imbalance_is_signed_by_close_position():
    closes_at_high = _build([(100, 110, 100, 110, 100.0) for _ in range(10)])
    closes_at_low = _build([(110, 110, 100, 100, 100.0) for _ in range(10)])
    assert smc_scoring.delta_imbalance(closes_at_high, 9) == pytest.approx(1.0)
    assert smc_scoring.delta_imbalance(closes_at_low, 9) == pytest.approx(-1.0)


def test_bos_volume_confirmation_uses_a_numeric_threshold():
    rows = [(100, 101, 99, 100, 1000.0) for _ in range(20)]
    rows.append((100, 101, 99, 100, 1000.0 * smc_scoring.BOS_VOLUME_CONFIRM_RATIO))
    rows.append((100, 101, 99, 100, 900.0))
    candles = _build(rows)

    ok, ratio = smc_scoring.bos_volume_confirmed(candles, 20)
    assert ok is True
    assert ratio == pytest.approx(smc_scoring.BOS_VOLUME_CONFIRM_RATIO)

    ok, ratio = smc_scoring.bos_volume_confirmed(candles, 21)
    assert ok is False
    assert ratio < smc_scoring.BOS_VOLUME_CONFIRM_RATIO


def test_relevant_structure_event_anchors_to_the_zone():
    events = [
        {"index": 10, "direction": "bullish", "type": "CHoCH"},
        {"index": 60, "direction": "bullish", "type": "BOS"},
        {"index": 90, "direction": "bullish", "type": "BOS"},
        {"index": 45, "direction": "bearish", "type": "BOS"},
    ]
    # Prefer the earliest break at/after the zone — the one that validated it.
    assert smc_scoring.relevant_structure_event(events, 55, "bullish")["index"] == 60
    # Nothing after the zone -> fall back to the most recent prior break.
    assert smc_scoring.relevant_structure_event(events, 200, "bullish")["index"] == 90
    assert smc_scoring.relevant_structure_event(events, 0, "bearish")["index"] == 45
    assert smc_scoring.relevant_structure_event(events, 0, "neutral") is None


# ── Movement primitives ──────────────────────────────────────────────────────

def test_displacement_magnitude_is_in_atr_multiples():
    candles = _build([(100, 106, 99, 105, 100.0) for _ in range(5)])
    assert smc_scoring.displacement_magnitude(candles, 4, atr=2.5) == pytest.approx(2.0)


def test_wick_rejection_ratio_is_side_aware():
    # Long lower wick: range 100-110, body 108-109.
    hammer = Candle(time=0, open=108, high=110, low=100, close=109, volume=1.0)
    assert smc_scoring.wick_rejection_ratio(hammer, "buy") == pytest.approx(0.8)
    assert smc_scoring.wick_rejection_ratio(hammer, "sell") == pytest.approx(0.1)


def test_atr_momentum_is_signed_and_atr_normalised():
    rows = [(100 + i, 101 + i, 99 + i, 100 + i, 100.0) for i in range(20)]
    candles = _build(rows)
    # Close rose by exactly 10 over the 10-bar window; ATR 2 -> 5.0
    assert smc_scoring.atr_momentum(candles, atr=2.0, period=10) == pytest.approx(5.0)


# ── The score breakdown is auditable ─────────────────────────────────────────

def _score_the_fixture_signal():
    candles = designed_bullish_market()
    res = SMCStrategyEngine(min_rr=1.5).analyze(candles)
    assert res["signals"], "the designed market must produce a setup"
    return res["signals"][0]


def test_every_signal_carries_a_numeric_factor_breakdown():
    sig = _score_the_fixture_signal()
    bd = sig["score_breakdown"]

    assert bd["factors"], "breakdown must name the contributing factors"
    for f in bd["factors"]:
        assert set(f) == {"name", "family", "raw_value", "normalized", "weight",
                          "contribution"}
        assert f["family"] in ("volume", "structure", "movement", "risk")
        assert -1.0 <= f["normalized"] <= 1.0
        assert f["contribution"] == pytest.approx(f["normalized"] * f["weight"], abs=1e-3)

    # Every scored factor is present exactly once.
    names = [f["name"] for f in bd["factors"]]
    assert sorted(names) == sorted(smc_scoring.DEFAULT_WEIGHTS)


def test_contributions_sum_to_the_reported_total():
    sig = _score_the_fixture_signal()
    bd = sig["score_breakdown"]
    summed = sum(f["contribution"] for f in bd["factors"])
    assert summed == pytest.approx(bd["raw_total"], abs=1e-3)
    assert bd["total"] == pytest.approx(min(bd["raw_total"], smc_scoring.MAX_CONFIDENCE),
                                        abs=1e-3)
    assert sig["confidence"] == pytest.approx(round(bd["total"], 2), abs=1e-6)
    # Family subtotals must also reconcile.
    assert sum(bd["by_family"].values()) == pytest.approx(bd["raw_total"], abs=1e-3)


def test_reason_string_names_the_numeric_contributions():
    sig = _score_the_fixture_signal()
    assert "score " in sig["reason"]
    top = max(sig["score_breakdown"]["factors"], key=lambda f: f["contribution"])
    assert top["name"].replace("_", " ") in sig["reason"]


def test_default_weights_sum_to_one():
    assert sum(smc_scoring.DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_learned_weights_override_the_defaults():
    candles = designed_bullish_market()
    base = SMCStrategyEngine(min_rr=1.5).analyze(candles)["signals"][0]
    # Zero out the volume family entirely; the score must fall.
    zeroed = {k: 0.0 for k, v in smc_scoring.FACTOR_FAMILY.items() if v == "volume"}
    tuned = SMCStrategyEngine(min_rr=1.5, factor_weights=zeroed).analyze(candles)
    assert tuned["signals"], "gating is unchanged — only the weighting moved"
    assert tuned["signals"][0]["confidence"] < base["confidence"]
    assert tuned["signals"][0]["score_breakdown"]["by_family"]["volume"] == 0.0


# ── The hard gates ───────────────────────────────────────────────────────────

def test_instrument_without_any_volume_yields_structure_only_setups():
    """Spot FX publishes no volume; the gate is not applicable, not failed.

    The gate exists to reject breaks on *thin* volume, which needs a series to
    be thin relative to. On an instrument that publishes none, applying it as a
    rejection bans the instrument instead of filtering its weak setups — so the
    setups fire, flagged structure_only so callers can say so.
    """
    candles = designed_bullish_market()
    with_volume = SMCStrategyEngine(min_rr=1.5).analyze(candles)
    without_volume = SMCStrategyEngine(min_rr=1.5).analyze(_strip_volume(candles))

    assert with_volume["signals"], "sanity: the fixture does produce a setup"
    assert without_volume["signals"], (
        "an instrument with no volume feed must still produce setups"
    )
    assert all(
        s["score_breakdown"]["structure_only"] for s in without_volume["signals"]
    ), "setups scored without volume must be flagged structure_only"
    assert not any(
        s["score_breakdown"]["structure_only"] for s in with_volume["signals"]
    ), "a volume-bearing instrument is never structure_only"


def test_structure_only_scoring_is_not_penalised_for_the_missing_family():
    """Dropping the volume family must not cost a third of the confidence.

    The volume weights are redistributed over the measurable factors, so a
    setup on a volume-less instrument scores comparably to the same structure
    on one that reports volume — the feed shouldn't decide the grade.
    """
    candles = designed_bullish_market()
    scored = SMCStrategyEngine(min_rr=1.5).analyze(_strip_volume(candles))["signals"][0]
    bd = scored["score_breakdown"]

    assert bd["by_family"].get("volume", 0.0) == 0.0
    # Reported weights are rounded to 4dp per factor, so 11 of them can drift a
    # few ten-thousandths off 1.0 — the redistribution itself is exact.
    assert sum(f["weight"] for f in bd["factors"]) == pytest.approx(1.0, abs=1e-3), (
        "redistributed weights must still sum to 1.0"
    )
    assert scored["confidence"] > 0.0


def test_thin_volume_is_still_a_hard_gate_when_volume_exists():
    """The relaxed-fallback passes must not route around a *failed* gate."""
    rows = [(100, 101, 99, 100, 1000.0) for _ in range(30)]
    rows.append((100, 104, 99, 103, 400.0))   # zone candle, below the mean
    rows += [(103, 105, 102, 104, 1000.0) for _ in range(5)]

    bd = smc_scoring.score_signal(
        side="buy", zone_index=30, zone_kind="bullish_fvg",
        zone_top=104.0, zone_bottom=99.0, entry=101.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=_build(rows), atr=2.0, bias="bullish",
        events=[], sweeps={}, equilibrium=102.0, range_low=99.0, range_high=105.0,
    )
    assert bd.structure_only is False, "volume exists here — the gate applies"
    assert bd.volume_confirmed is False


def test_volume_confirmation_requires_an_above_mean_zone_candle():
    """Scored directly: a zone candle at below-average volume is not confirmed."""
    rows = [(100, 101, 99, 100, 1000.0) for _ in range(30)]
    rows.append((100, 104, 99, 103, 400.0))   # zone candle, well below the mean
    rows += [(103, 105, 102, 104, 1000.0) for _ in range(5)]
    candles = _build(rows)

    bd = smc_scoring.score_signal(
        side="buy", zone_index=30, zone_kind="bullish_fvg",
        zone_top=104.0, zone_bottom=99.0, entry=101.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=2.0, bias="bullish",
        events=[], sweeps={}, equilibrium=102.0, range_low=99.0, range_high=105.0,
    )
    assert bd.volume_data_available is True
    assert bd.volume_confirmed is False
    assert bd.factors[0].name == "relative_volume"
    assert bd.factors[0].raw_value == pytest.approx(0.4)


def test_break_of_structure_on_thin_volume_blocks_confirmation():
    rows = [(100, 101, 99, 100, 1000.0) for _ in range(30)]
    rows.append((100, 104, 99, 103, 2000.0))          # strong zone candle
    rows.append((103, 108, 103, 107, 500.0))          # the break — thin volume
    rows += [(107, 109, 106, 108, 1000.0) for _ in range(3)]
    candles = _build(rows)
    events = [{"index": 31, "direction": "bullish", "type": "BOS", "level": 107.0}]

    bd = smc_scoring.score_signal(
        side="buy", zone_index=30, zone_kind="bullish_fvg",
        zone_top=104.0, zone_bottom=99.0, entry=101.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=2.0, bias="bullish",
        events=events, sweeps={}, equilibrium=102.0,
        range_low=99.0, range_high=109.0,
    )
    assert bd.volume_confirmed is False, "an unconfirmed break must block the setup"

    # Same setup, but the break prints on heavy volume -> confirmed.
    rows[31] = (103, 108, 103, 107, 3000.0)
    bd_ok = smc_scoring.score_signal(
        side="buy", zone_index=30, zone_kind="bullish_fvg",
        zone_top=104.0, zone_bottom=99.0, entry=101.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=_build(rows), atr=2.0, bias="bullish",
        events=events, sweeps={}, equilibrium=102.0,
        range_low=99.0, range_high=109.0,
    )
    assert bd_ok.volume_confirmed is True


def test_htf_bias_gates_opposing_entries():
    candles = designed_bullish_market()

    aligned = SMCStrategyEngine(min_rr=1.5).analyze(candles, htf_candles=candles)
    assert aligned["htf_bias"] == "bullish"
    assert aligned["signals"], "an aligned HTF must not block the LTF entry"

    # A bearish higher timeframe must veto the bullish LTF setups outright.
    inverted = _build([
        (4000 - c.open, 4000 - c.low, 4000 - c.high, 4000 - c.close, c.volume)
        for c in candles
    ])
    opposed = SMCStrategyEngine(min_rr=1.5).analyze(candles, htf_candles=inverted)
    assert opposed["htf_bias"] == "bearish"
    assert opposed["signals"] == []


def test_htf_bias_is_none_when_no_higher_timeframe_supplied():
    candles = designed_bullish_market()
    res = SMCStrategyEngine(min_rr=1.5).analyze(candles)
    assert res["htf_bias"] is None
    assert res["signals"], "missing HTF data must not block entries"


def test_too_little_htf_history_is_ignored_rather_than_guessed():
    """A short HTF series must yield None, not a bias inferred from noise."""
    candles = designed_bullish_market()
    engine = SMCStrategyEngine(min_rr=1.5)
    assert engine._htf_bias(candles[:20]) is None
    assert engine._htf_bias([]) is None
    assert engine._htf_bias(None) is None
    assert engine._htf_bias(candles) == "bullish"


def test_analyze_data_request_accepts_htf_candles():
    """The source-agnostic path must be able to gate on HTF too.

    Without this the exchange-fallback route that /mt5-live uses when MT5
    history is thin would silently skip the HTF gate.
    """
    from plugins.MT5TradingPlugin.backend.schemas import MT5SmcAnalyzeDataRequest

    fields = MT5SmcAnalyzeDataRequest.model_fields
    assert "htf_candles" in fields
    # Optional: omitting it must not be a validation error.
    req = MT5SmcAnalyzeDataRequest(symbol="XAUUSD", candles=[])
    assert req.htf_candles == []
