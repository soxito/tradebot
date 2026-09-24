"""Kronos + OBLIVION: the engine leads, the forecast confirms, the news
window vetoes, and the multi-timeframe consensus is timeframe-weighted."""
from __future__ import annotations

import asyncio

import pytest

from plugins.KronosForecastPlugin.backend.schemas import (
    ForecastCandle, ForecastResponse, ForecastSignal, OblivionTfSignal, VolumeContext,
)
from plugins.KronosForecastPlugin.backend.services import forecast_service as fs
from plugins.KronosForecastPlugin.backend.services import oblivion_signals as osig


def _kronos(direction="up", conf=0.7):
    return ForecastSignal(direction=direction, pct_change=1.2 if direction == "up" else -1.2, confidence=conf,
                          target_price=101.0, anchor_price=100.0, summary="t", decision="OK")


def _resp(oblivion=None, kronos_direction="up", kconf=0.7, decision="OK", vol_status="OK"):
    sig = _kronos(kronos_direction, kconf)
    sig.decision = decision
    return ForecastResponse(
        exchange="bitget", symbol="BTC/USDT", timeframe="1h", engine="heuristic", model_name="t",
        lookback=100, pred_len=12, samples=1, anchor_time=1_700_000_000, anchor_price=100.0,
        forecast=[ForecastCandle(time=1_700_003_600, open=100, high=102, low=99, close=101)],
        signal=sig, oblivion=oblivion,
        volume=VolumeContext(status=vol_status, symbol="BTC/USDT", source="test", volume_24h=1e6, volume_1h=5e4,
                             hourly_mean_24h=4e4, relative_volume=1.2, regime="NORMAL"),
        decision=decision,
    )


def _obl(action="buy", confirmed=True, news=False, bull=8, bear=1):
    entry, sl = 100.0, 97.0
    up = action == "buy"
    plan = dict(entry=entry, stop_loss=sl if up else 103.0,
                tp1=103.0 if up else 97.0, tp2=106.0 if up else 94.0, tp3=109.0 if up else 91.0)
    return {
        "score": {"bull": bull, "bear": bear, "required": 6},
        "bias_label": "BULLISH" if up else "BEARISH", "market_bias": 1 if up else -1,
        "structure": {"now": "BOS ↑"}, "session": {"name": "LONDON"},
        "signal": {"action": action, "new": True, "confirmed": confirmed, "score": bull if up else bear, **plan},
        "news": {"blocked": news, "window": "1330-1430", "tz": "America/New_York"},
        "summary": "OBLIVION 1h: test",
    }


# ── fusion rules ─────────────────────────────────────────────────────────────

def test_aligned_kronos_lifts_confidence_and_marks_alignment():
    conf, aligned, veto = fs.fuse_oblivion_with_kronos(_obl()["signal"], {"required": 6}, _kronos("up", 0.8))
    assert aligned is True and veto is None
    assert conf == pytest.approx(min(1.0, 8 / 12 + 0.25 * 0.8), abs=1e-3)


def test_decisively_opposing_kronos_vetoes():
    _c, aligned, veto = fs.fuse_oblivion_with_kronos(_obl()["signal"], {"required": 6}, _kronos("down", 0.6))
    assert aligned is False and veto == "kronos"


def test_weakly_opposing_kronos_only_trims():
    conf, aligned, veto = fs.fuse_oblivion_with_kronos(_obl()["signal"], {"required": 6}, _kronos("down", 0.3))
    assert veto is None and aligned is False
    assert conf < 8 / 12


def test_flat_or_missing_kronos_leaves_the_engine_call_alone():
    conf, aligned, veto = fs.fuse_oblivion_with_kronos(_obl()["signal"], {"required": 6}, None)
    assert (conf, aligned, veto) == (pytest.approx(8 / 12, abs=1e-3), None, None)
    conf2, *_ = fs.fuse_oblivion_with_kronos(_obl()["signal"], {"required": 6}, _kronos("flat", 0.9))
    assert conf2 == conf


# ── sniper entries ───────────────────────────────────────────────────────────

def test_oblivion_entry_is_the_first_sniper_signal_and_carries_its_ladder():
    signals = fs.build_sniper_signals(_resp(oblivion=_obl()))
    assert signals and signals[0].id.endswith("-oblivion")
    first = signals[0]
    assert first.side == "long" and first.order_kind == "market"
    assert (first.entry, first.stop_loss, first.take_profit_1, first.take_profit_2) == (100.0, 97.0, 103.0, 106.0)
    assert first.risk_reward == 1.0
    assert any("Kronos confirms" in r for r in first.reasons)


def test_forming_oblivion_signal_rests_as_a_limit():
    signals = fs.build_sniper_signals(_resp(oblivion=_obl(confirmed=False)))
    assert signals[0].order_kind == "limit"


def test_oblivion_entry_survives_a_low_confidence_kronos_path():
    signals = fs.build_sniper_signals(_resp(oblivion=_obl(), decision="LOW_CONFIDENCE"))
    assert len(signals) == 1 and signals[0].id.endswith("-oblivion")


def test_kronos_veto_drops_the_oblivion_entry_but_keeps_kronos_entries():
    signals = fs.build_sniper_signals(_resp(oblivion=_obl(action="buy"), kronos_direction="down", kconf=0.7))
    assert not any(s.id.endswith("-oblivion") for s in signals)
    assert all(s.side == "short" for s in signals)


def test_news_window_emits_nothing_at_all():
    assert fs.build_sniper_signals(_resp(oblivion=_obl(news=True))) == []


def test_volume_gate_still_precedes_everything():
    assert fs.build_sniper_signals(_resp(oblivion=_obl(), vol_status="STALE")) == []


# ── multi-timeframe ──────────────────────────────────────────────────────────

def test_consensus_is_timeframe_weighted():
    per = [
        OblivionTfSignal(timeframe="1m", action="sell", confidence=0.9),
        OblivionTfSignal(timeframe="5m", action="sell", confidence=0.9),
        OblivionTfSignal(timeframe="4h", action="buy", confidence=0.7),
        OblivionTfSignal(timeframe="1d", action="buy", confidence=0.7),
        OblivionTfSignal(timeframe="1w", action="buy", confidence=0.7),
        OblivionTfSignal(timeframe="1h", action="wait"),
        OblivionTfSignal(timeframe="15m", action="buy", vetoed_by="kronos", confidence=0.6),
    ]
    c = osig.consensus_of(per)
    assert c.action == "buy"
    assert set(c.agreeing_tfs) == {"4h", "1d", "1w"}
    assert set(c.opposing_tfs) == {"1m", "5m"}
    assert c.vetoed_tfs == ["15m"]


def test_multi_tf_runs_every_timeframe_on_the_forecast_feed(monkeypatch):
    import random
    rng = random.Random(1)

    async def fake_rows(exchange, symbol, tf, limit):
        rows, p = [], 100.0
        for i in range(limit):
            o = p
            p += 0.5 + rng.uniform(-1, 1)
            rows.append([1_700_000_000_000 + i * 3_600_000, o, max(o, p) + 0.3, min(o, p) - 0.3, p, 1000.0])
        return rows

    async def fake_forecast(exchange, symbol, tf, **kw):
        return _resp(kronos_direction="up", kconf=0.6)

    monkeypatch.setattr(fs, "_fetch_ohlcv", fake_rows)
    monkeypatch.setattr(fs, "run_forecast_cached", fake_forecast)

    out = asyncio.run(osig.oblivion_multi_tf("bitget", "BTC/USDT"))
    assert [t.timeframe for t in out.per_tf] == osig.ALL_TFS
    assert all(t.error is None for t in out.per_tf), [t.error for t in out.per_tf]
    assert all(t.kronos_direction == "up" for t in out.per_tf)
    assert out.consensus.action in ("buy", "sell", "wait")
    assert out.per_tf[0].summary.startswith("OBLIVION")


def test_multi_tf_reports_a_dead_feed_per_timeframe(monkeypatch):
    async def no_rows(*a, **k):
        return []

    monkeypatch.setattr(fs, "_fetch_ohlcv", no_rows)
    out = asyncio.run(osig.oblivion_multi_tf("bitget", "BTC/USDT", timeframes=["1h", "4h"], with_kronos=False))
    assert [t.timeframe for t in out.per_tf] == ["1h", "4h"]
    assert all(t.error for t in out.per_tf)
    assert out.consensus.action == "wait"
