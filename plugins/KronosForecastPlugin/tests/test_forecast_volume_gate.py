"""The volume gate as enforced by forecast_service.

Proves that no forecast, no sniper entry and no direction is emitted when the
volume context cannot be resolved, and that the direction rules
(continuation / exhaustion / reversal) actually move the confidence.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from plugins.KronosForecastPlugin.backend.schemas import (
    BandPoint, ForecastCandle, ForecastResponse, VolumeContext,
)
from plugins.KronosForecastPlugin.backend.services import forecast_service as fs
from plugins.KronosForecastPlugin.backend.services import volume_context as vc


def ctx(status="OK", regime="NORMAL", divergence="NEUTRAL", detail="") -> VolumeContext:
    return VolumeContext(
        status=status, symbol="BTC/USDT", source="ohlcv:1h",
        volume_24h=2_400_000.0, volume_1h=100_000.0, hourly_mean_24h=100_000.0,
        relative_volume=1.0, regime=regime, divergence=divergence,
        divergence_bars=6, price_change_pct=0.5, volume_slope_norm=0.1,
        hours_covered=24, detail=detail or f"{regime} volume.",
    )


def paths(final_prices, anchor=100.0, horizon=4):
    """One sample path per requested final price, linear from the anchor."""
    out = []
    for fp in final_prices:
        closes = np.linspace(anchor, fp, horizon + 1)[1:]
        out.append(pd.DataFrame({
            "open": closes, "high": closes * 1.001, "low": closes * 0.999,
            "close": closes, "volume": np.full(horizon, 1000.0),
        }))
    return out


# ── signal construction ──────────────────────────────────────────────────────

def test_no_volume_yields_no_trade_signal():
    sig = fs._volume_gated_signal(
        paths([105.0] * 10), 100.0, 4, ctx(status="UNAVAILABLE", detail="feed carries no volume"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert sig.direction == "no_trade"
    assert sig.decision == "NO_TRADE"
    assert sig.confidence == 0.0
    # the model *did* see +5%, but it is deliberately not reported as a direction
    assert sig.pct_change == 0.0
    assert "NO_TRADE" in sig.summary
    assert any("never inferred from price alone" in r for r in sig.rationale)


def test_stale_volume_yields_no_trade_signal():
    sig = fs._volume_gated_signal(
        paths([95.0] * 10), 100.0, 4, ctx(status="STALE"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert sig.direction == "no_trade"
    assert sig.decision == "NO_TRADE"


def test_ok_volume_produces_a_direction_with_evidence():
    sig = fs._volume_gated_signal(
        paths([104.0, 105.0, 106.0, 105.0], anchor=100.0), 100.0, 4,
        ctx(regime="ELEVATED", divergence="CONFIRMED_UP"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert sig.direction == "up"
    assert sig.decision == "OK"
    assert sig.confidence > 0
    joined = " ".join(sig.rationale)
    assert "24h" in joined and "ELEVATED" in joined and "Final confidence" in joined
    assert "ELEVATED volume" in sig.summary


def test_rising_price_falling_volume_scores_below_confirmation():
    """rising price + falling relative volume → weaken / flag exhaustion."""
    ups = [104.0, 105.0, 106.0, 105.0]
    confirmed = fs._volume_gated_signal(
        paths(ups), 100.0, 4, ctx(divergence="CONFIRMED_UP"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    exhausted = fs._volume_gated_signal(
        paths(ups), 100.0, 4, ctx(divergence="EXHAUSTION_UP"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert exhausted.confidence < confirmed.confidence


def test_climactic_volume_against_the_move_flags_reversal():
    sig = fs._volume_gated_signal(
        paths([104.0, 105.0, 106.0, 105.0]), 100.0, 4,
        ctx(regime="CLIMACTIC", divergence="CONFIRMED_DOWN"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert sig.direction == "up"
    assert any("REVERSAL RISK" in r for r in sig.rationale)


def test_dead_volume_can_drop_a_weak_call_to_low_confidence():
    # barely-directional paths on a dead tape with exhaustion against them
    sig = fs._volume_gated_signal(
        paths([100.5, 99.0, 101.5, 98.0]), 100.0, 4,
        ctx(regime="DEAD", divergence="EXHAUSTION_UP"),
        symbol="BTC/USDT", horizon_label="4×1h",
    )
    assert sig.decision in ("LOW_CONFIDENCE", "OK")
    if sig.decision == "LOW_CONFIDENCE":
        assert sig.confidence < vc.MIN_TRADEABLE_CONFIDENCE
        assert "no entry" in sig.summary.lower()


# ── the NO_TRADE response ────────────────────────────────────────────────────

def test_no_trade_response_carries_evidence_and_no_forecast():
    resp = fs._no_trade_response(
        exchange="bitget", symbol="EURUSD", timeframe="1h", engine="unavailable",
        model_name="NeoQuasar/Kronos-base", lookback=400, pred_len=24, samples=10,
        anchor_time=1_800_000_000, anchor_price=1.08,
        ctx=ctx(status="UNAVAILABLE", detail="feed carries no volume"),
    )
    assert resp.decision == "NO_TRADE"
    assert resp.forecast == [] and resp.overlays == [] and resp.markers == []
    assert resp.signal.direction == "no_trade"
    assert resp.volume.status == "UNAVAILABLE"
    assert "hard precondition" in resp.note


# ── sniper entries ───────────────────────────────────────────────────────────

def _resp(volume, decision="OK", direction="up", confidence=0.8):
    from plugins.KronosForecastPlugin.backend.schemas import ForecastSignal
    fc = [ForecastCandle(time=1_800_000_000 + i * 3600, open=100, high=102,
                         low=99, close=101 + i, volume=10.0) for i in range(4)]
    return ForecastResponse(
        exchange="bitget", symbol="BTC/USDT", timeframe="1h", engine="kronos",
        model_name="m", lookback=400, pred_len=4, samples=10,
        anchor_time=1_800_000_000, anchor_price=100.0,
        forecast=fc,
        upper_band=[BandPoint(time=c.time, value=110.0) for c in fc],
        lower_band=[BandPoint(time=c.time, value=95.0) for c in fc],
        signal=ForecastSignal(
            direction=direction, pct_change=4.0, confidence=confidence,
            target_price=104.0, anchor_price=100.0, summary="s",
            decision=decision, rationale=["because volume said so"],
        ),
        volume=volume, decision=decision,
    )


def test_no_sniper_entry_without_volume():
    for status in ("UNAVAILABLE", "STALE", "INSUFFICIENT"):
        resp = _resp(ctx(status=status), decision="NO_TRADE")
        assert fs.build_sniper_signals(resp) == [], status


def test_no_sniper_entry_when_volume_context_is_missing_entirely():
    resp = _resp(None, decision="OK")
    assert fs.build_sniper_signals(resp) == []


def test_no_sniper_entry_on_low_confidence():
    resp = _resp(ctx(), decision="LOW_CONFIDENCE", confidence=0.2)
    assert fs.build_sniper_signals(resp) == []


def test_every_sniper_entry_carries_volume_evidence():
    resp = _resp(ctx(regime="ELEVATED", divergence="CONFIRMED_UP"))
    signals = fs.build_sniper_signals(resp, max_leverage=20)
    assert signals, "expected entries on an OK volume context"
    for s in signals:
        assert s.volume_24h == pytest.approx(2_400_000.0)
        assert s.volume_1h == pytest.approx(100_000.0)
        assert s.relative_volume == pytest.approx(1.0)
        assert s.volume_regime == "ELEVATED"
        assert s.volume_divergence == "CONFIRMED_UP"
        joined = " ".join(s.reasons)
        assert "24h" in joined and "Relative volume" in joined
        assert "because volume said so" in joined


# ── generate_sniper_signals notes ────────────────────────────────────────────

def test_generate_sniper_signals_reports_no_trade(monkeypatch):
    resp = fs._no_trade_response(
        exchange="bitget", symbol="BTC/USDT", timeframe="1h", engine="unavailable",
        model_name="m", lookback=400, pred_len=24, samples=10,
        anchor_time=1_800_000_000, anchor_price=100.0,
        ctx=ctx(status="STALE", detail="last hour closed 6h ago"),
    )

    async def fake_forecast(*_a, **_k):
        return resp

    async def fake_lev(*_a, **_k):
        return None

    monkeypatch.setattr(fs, "run_forecast_cached", fake_forecast)
    monkeypatch.setattr(fs, "_max_leverage", fake_lev)

    out = asyncio.run(fs.generate_sniper_signals("bitget", "BTC/USDT", "1h"))
    assert out.decision == "NO_TRADE"
    assert out.signals == []
    assert out.direction == "no_trade"
    assert out.volume.status == "STALE"
    assert "NO_TRADE" in out.note and "last hour closed 6h ago" in out.note
