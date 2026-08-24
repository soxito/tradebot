"""Unit tests for the volume gate.

Covers: missing volume data, stale volume, every regime, every divergence
branch, the confidence formula, and the NO_TRADE / LOW_CONFIDENCE decisions.
"""
from __future__ import annotations

import asyncio

import pytest

from plugins.KronosForecastPlugin.backend.services import volume_context as vc


HOUR = 3600
NOW = 1_800_000_000  # fixed clock so nothing in these tests depends on wall time


def rows_1h(volumes, *, closes=None, end=NOW, gap_s=0):
    """Build ccxt 1h rows ending on the hour before ``end``.

    ``volumes[-1]`` is the most recent COMPLETED hour. ``gap_s`` pushes the whole
    series further into the past to exercise staleness.
    """
    n = len(volumes)
    closes = closes if closes is not None else [100.0] * n
    last_start = (int(end) // HOUR) * HOUR - HOUR - gap_s
    out = []
    for i, (v, c) in enumerate(zip(volumes, closes)):
        start = last_start - (n - 1 - i) * HOUR
        out.append([start * 1000, c, c, c, c, v])
    return out


def flat_24h(volume=100.0, n=25):
    return [volume] * n


# ── missing / unusable volume data ───────────────────────────────────────────

def test_no_rows_is_unavailable():
    ctx = vc.build_volume_context([], symbol="BTC/USDT", timeframe="1h", source="t", now=NOW)
    assert ctx.status == "UNAVAILABLE"
    assert ctx.relative_volume is None


def test_all_zero_volume_fx_is_not_applicable_not_dead():
    """A weekend-closing feed hard-coding 0.0 volume (e.g. Frankfurter FX) must
    report NOT_APPLICABLE — volume is not required to forecast it — never a DEAD
    regime and never a hard NO_TRADE."""
    ctx = vc.build_volume_context(
        rows_1h([0.0] * 30), symbol="EURUSD", timeframe="1h", source="t", now=NOW
    )
    assert ctx.status == "NOT_APPLICABLE"
    assert ctx.regime == "UNKNOWN"
    assert "does not carry volume" in ctx.detail


def test_all_zero_volume_crypto_is_unavailable():
    """Crypto trades 24/7, so a zero-volume feed there is a broken feed and must
    still refuse — volume stays a hard precondition for coins."""
    ctx = vc.build_volume_context(
        rows_1h([0.0] * 30), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    assert ctx.status == "UNAVAILABLE"
    assert ctx.regime == "UNKNOWN"


def test_short_history_is_insufficient():
    ctx = vc.build_volume_context(
        rows_1h(flat_24h(n=10)), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    assert ctx.status == "INSUFFICIENT"
    assert "10 complete hour" in ctx.detail


def test_bars_longer_than_an_hour_are_insufficient():
    """4h bars cannot be aggregated into whole hours — the caller must refetch."""
    rows = [[(NOW - (30 - i) * 4 * HOUR) * 1000, 1, 1, 1, 1, 10.0] for i in range(30)]
    ctx = vc.build_volume_context(rows, symbol="BTC/USDT", timeframe="4h", source="t", now=NOW)
    assert ctx.status == "INSUFFICIENT"


def test_partial_hours_are_dropped():
    """15m bars only form an hour once all four have printed."""
    rows = []
    start = (NOW // HOUR) * HOUR - 30 * HOUR
    for h in range(30):
        # the final hour gets only 2 of its 4 bars → must not be counted
        bars = 2 if h == 29 else 4
        for b in range(bars):
            ts = start + h * HOUR + b * 900
            rows.append([ts * 1000, 100, 100, 100, 100, 25.0])
    ctx = vc.build_volume_context(rows, symbol="BTC/USDT", timeframe="15m", source="t", now=NOW)
    assert ctx.status == "OK"
    # 29 complete hours available, the incomplete one excluded
    assert ctx.hours_covered == 24
    assert ctx.volume_1h == pytest.approx(100.0)


# ── staleness ────────────────────────────────────────────────────────────────

def test_stale_volume_is_reported_stale():
    ctx = vc.build_volume_context(
        rows_1h(flat_24h(), gap_s=6 * HOUR),
        symbol="BTC/USDT", timeframe="1h", source="t", now=NOW,
    )
    assert ctx.status == "STALE"
    assert ctx.age_seconds > vc.stale_after_seconds(HOUR)
    # the measured numbers are still reported so the UI can explain itself
    assert ctx.volume_24h == pytest.approx(2400.0)


def test_fresh_volume_is_ok():
    ctx = vc.build_volume_context(
        rows_1h(flat_24h()), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    assert ctx.status == "OK"
    assert ctx.age_seconds <= vc.STALE_FLOOR_S


# ── regimes ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "last_hour_volume,expected",
    [
        (20.0, "DEAD"),        # ×0.2  — below DEAD_RV (0.50)
        (100.0, "NORMAL"),     # ×1.0
        (180.0, "ELEVATED"),   # ×1.8  — at/above ELEVATED_RV (1.50)
        (400.0, "CLIMACTIC"),  # ×4.0  — at/above CLIMACTIC_RV (2.50)
    ],
)
def test_each_regime(last_hour_volume, expected):
    vols = [100.0] * 24 + [last_hour_volume]
    ctx = vc.build_volume_context(
        rows_1h(vols), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    assert ctx.status == "OK"
    assert ctx.regime == expected


def test_regime_boundaries_are_the_named_constants():
    assert vc.classify_regime(vc.DEAD_RV - 0.01) == "DEAD"
    assert vc.classify_regime(vc.DEAD_RV) == "NORMAL"
    assert vc.classify_regime(vc.ELEVATED_RV) == "ELEVATED"
    assert vc.classify_regime(vc.CLIMACTIC_RV) == "CLIMACTIC"
    assert vc.classify_regime(None) == "UNKNOWN"


def test_relative_volume_and_z_score():
    vols = [100.0] * 24 + [300.0]
    ctx = vc.build_volume_context(
        rows_1h(vols), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    # trailing 24h = 23×100 + 300 = 2600 → hourly mean 108.33
    assert ctx.volume_24h == pytest.approx(2600.0)
    assert ctx.volume_1h == pytest.approx(300.0)
    assert ctx.relative_volume == pytest.approx(300.0 / (2600.0 / 24), abs=1e-3)
    # the 23 prior hours are identical → zero stdev → no z-score is invented
    assert ctx.z_score is None


def test_z_score_present_when_prior_hours_vary():
    vols = [100.0 + (i % 5) * 10 for i in range(24)] + [400.0]
    ctx = vc.build_volume_context(
        rows_1h(vols), symbol="BTC/USDT", timeframe="1h", source="t", now=NOW
    )
    assert ctx.z_score is not None and ctx.z_score > 2


# ── divergence branches ──────────────────────────────────────────────────────

def _divergence(price_slope, vol_slope_dir):
    """Build 25 hours whose last 6 have the requested price/volume trends."""
    n = 25
    closes = [100.0] * (n - 6)
    vols = [100.0] * (n - 6)
    for i in range(6):
        closes.append(100.0 * (1 + price_slope * (i + 1) / 100.0))
        # ±15% of the baseline per hour — a clear trend that stays positive
        vols.append(100.0 * (1 + vol_slope_dir * 0.15 * (i + 1)))
    ctx = vc.build_volume_context(
        rows_1h(vols, closes=closes), symbol="BTC/USDT", timeframe="1h",
        source="t", now=NOW,
    )
    return ctx


def test_divergence_confirmed_up():
    ctx = _divergence(price_slope=0.5, vol_slope_dir=+1)
    assert ctx.divergence == "CONFIRMED_UP"
    assert ctx.price_change_pct > vc.PRICE_NOISE_PCT
    assert ctx.volume_slope_norm > vc.VOLUME_SLOPE_NOISE


def test_divergence_exhaustion_up():
    """Rising price on falling volume — the move is running dry."""
    ctx = _divergence(price_slope=0.5, vol_slope_dir=-1)
    assert ctx.divergence == "EXHAUSTION_UP"


def test_divergence_confirmed_down():
    ctx = _divergence(price_slope=-0.5, vol_slope_dir=+1)
    assert ctx.divergence == "CONFIRMED_DOWN"


def test_divergence_exhaustion_down():
    ctx = _divergence(price_slope=-0.5, vol_slope_dir=-1)
    assert ctx.divergence == "EXHAUSTION_DOWN"


def test_divergence_neutral_inside_noise():
    ctx = _divergence(price_slope=0.001, vol_slope_dir=0)
    assert ctx.divergence == "NEUTRAL"


def test_classify_divergence_unknown_on_missing_inputs():
    assert vc.classify_divergence(None, 0.5) == "UNKNOWN"
    assert vc.classify_divergence(1.0, None) == "UNKNOWN"


# ── confidence formula ───────────────────────────────────────────────────────

def _ctx(regime="NORMAL", divergence="NEUTRAL", status="OK"):
    from plugins.KronosForecastPlugin.backend.schemas import VolumeContext
    return VolumeContext(
        status=status, symbol="BTC/USDT", source="t",
        volume_24h=2400.0, volume_1h=100.0, hourly_mean_24h=100.0,
        relative_volume=1.0, regime=regime, divergence=divergence,
    )


def test_base_confidence_matches_documented_formula():
    base = vc.base_model_confidence(agreement=1.0, dispersion=0.0)
    assert base == pytest.approx(vc.AGREEMENT_WEIGHT + vc.DISPERSION_WEIGHT)
    # dispersion of 2.5% wipes out the whole dispersion term
    assert vc.base_model_confidence(1.0, 1.0 / vc.DISPERSION_SCALE) == pytest.approx(
        vc.AGREEMENT_WEIGHT
    )


def test_confidence_is_zero_without_volume():
    ctx = _ctx(status="UNAVAILABLE")
    assert vc.score_confidence("up", 1.0, 0.0, ctx) == 0.0
    assert vc.decide(0.0, ctx) == "NO_TRADE"


def test_rising_price_rising_volume_lifts_confidence():
    """rising price + rising relative volume → trend continuation confidence up."""
    plain = vc.score_confidence("up", 0.6, 0.005, _ctx("NORMAL", "NEUTRAL"))
    confirmed = vc.score_confidence("up", 0.6, 0.005, _ctx("ELEVATED", "CONFIRMED_UP"))
    assert confirmed > plain
    assert confirmed == pytest.approx(
        plain * vc.REGIME_WEIGHT["ELEVATED"] * vc.DIVERGENCE_WEIGHT[("up", "CONFIRMED_UP")]
    )


def test_rising_price_falling_volume_weakens_confidence():
    """rising price + falling relative volume → exhaustion, confidence down."""
    neutral = vc.score_confidence("up", 0.9, 0.0, _ctx("NORMAL", "NEUTRAL"))
    exhausted = vc.score_confidence("up", 0.9, 0.0, _ctx("NORMAL", "EXHAUSTION_UP"))
    assert exhausted < neutral
    assert exhausted == pytest.approx(neutral * vc.DIVERGENCE_WEIGHT[("up", "EXHAUSTION_UP")])


def test_dead_regime_discounts_confidence():
    normal = vc.score_confidence("up", 0.9, 0.0, _ctx("NORMAL"))
    dead = vc.score_confidence("up", 0.9, 0.0, _ctx("DEAD"))
    assert dead == pytest.approx(normal * vc.REGIME_WEIGHT["DEAD"])


def test_climactic_volume_against_the_move_flags_reversal():
    ctx = _ctx("CLIMACTIC", "CONFIRMED_DOWN")
    assert vc.is_reversal_risk("up", ctx) is True
    assert vc.volume_multiplier("up", ctx) == vc.CLIMACTIC_AGAINST_WEIGHT
    # the same context is *supportive* for a short
    assert vc.is_reversal_risk("down", ctx) is False
    assert vc.volume_multiplier("down", ctx) > vc.CLIMACTIC_AGAINST_WEIGHT


def test_low_confidence_decision():
    # weak agreement + dead tape lands under the tradeable floor
    ctx = _ctx("DEAD", "EXHAUSTION_UP")
    conf = vc.score_confidence("up", 0.4, 0.01, ctx)
    assert conf < vc.MIN_TRADEABLE_CONFIDENCE
    assert vc.decide(conf, ctx) == "LOW_CONFIDENCE"


def test_ok_decision():
    ctx = _ctx("NORMAL", "CONFIRMED_UP")
    conf = vc.score_confidence("up", 0.95, 0.0, ctx)
    assert vc.decide(conf, ctx) == "OK"


def test_confidence_is_never_reported_as_a_certainty():
    """Unanimous paths on elevated confirming volume must still cap below 1."""
    conf = vc.score_confidence("up", 1.0, 0.0, _ctx("ELEVATED", "CONFIRMED_UP"))
    assert conf == pytest.approx(vc.MAX_CONFIDENCE)
    assert conf < 1.0


def test_rationale_explains_a_no_trade():
    lines = vc.direction_rationale(
        "up", _ctx(status="STALE"), agreement=0.9, dispersion=0.0,
        confidence=0.0, decision="NO_TRADE",
    )
    assert any("NO_TRADE" in ln for ln in lines)
    assert any("never inferred from price alone" in ln for ln in lines)


def test_rationale_carries_the_volume_numbers():
    lines = vc.direction_rationale(
        "up", _ctx("ELEVATED", "CONFIRMED_UP"), agreement=0.9, dispersion=0.0,
        confidence=0.7, decision="OK",
    )
    joined = " ".join(lines)
    assert "24h" in joined and "Relative volume" in joined and "ELEVATED" in joined
    assert "Final confidence" in joined


# ── resolve_volume_context (fetch fallback) ──────────────────────────────────

def test_resolver_refetches_1h_when_rows_are_too_coarse():
    calls = []

    async def fetcher(symbol, timeframe, limit):
        calls.append((symbol, timeframe, limit))
        return rows_1h(flat_24h())

    coarse = [[(NOW - (30 - i) * 4 * HOUR) * 1000, 1, 1, 1, 1, 10.0] for i in range(30)]
    ctx = asyncio.run(vc.resolve_volume_context(
        symbol="BTC/USDT", timeframe="4h", rows=coarse, fetcher=fetcher, now=NOW,
    ))
    assert calls == [("BTC/USDT", "1h", vc.REFETCH_LIMIT)]
    assert ctx.status == "OK"
    assert ctx.source == "ohlcv:1h"


def test_resolver_uses_caller_rows_without_fetching():
    async def fetcher(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("should not refetch when the caller's rows suffice")

    ctx = asyncio.run(vc.resolve_volume_context(
        symbol="BTC/USDT", timeframe="1h", rows=rows_1h(flat_24h()),
        fetcher=fetcher, now=NOW,
    ))
    assert ctx.status == "OK"
    assert ctx.source == "ohlcv:1h"


def test_resolver_without_fetcher_reports_unavailable():
    ctx = asyncio.run(vc.resolve_volume_context(
        symbol="BTC/USDT", timeframe="1d", rows=None, fetcher=None, now=NOW,
    ))
    assert ctx.status == "UNAVAILABLE"


def test_resolver_without_fetcher_is_not_applicable_for_metals():
    ctx = asyncio.run(vc.resolve_volume_context(
        symbol="XAUUSD", timeframe="1d", rows=None, fetcher=None, now=NOW,
    ))
    assert ctx.status == "NOT_APPLICABLE"


def test_resolver_survives_a_failing_fetcher():
    async def fetcher(*_a, **_k):
        raise RuntimeError("exchange down")

    ctx = asyncio.run(vc.resolve_volume_context(
        symbol="BTC/USDT", timeframe="1d", rows=None, fetcher=fetcher, now=NOW,
    ))
    assert ctx.status == "UNAVAILABLE"
    assert "exchange down" in ctx.detail
