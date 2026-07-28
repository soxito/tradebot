"""Volume gate on the Telegram sniper.

Every sniper entry must resolve a VolumeContext first: missing / stale volume is
NO_TRADE, and a regime or divergence that argues against the signal's own
direction blocks auto-execution.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from plugins.KronosForecastPlugin.backend.schemas import VolumeContext
from plugins.TelegramSignalNewsPlugin.backend.services import sniper_service as ss


HOUR = 3600
NOW = 1_800_000_000


def ctx(status="OK", regime="NORMAL", divergence="NEUTRAL", rv=1.0, detail="") -> VolumeContext:
    return VolumeContext(
        status=status, symbol="BTC/USDT", source="ohlcv:15m",
        volume_24h=2_400_000.0, volume_1h=100_000.0 * rv, hourly_mean_24h=100_000.0,
        relative_volume=rv, regime=regime, divergence=divergence,
        divergence_bars=6, price_change_pct=0.6, volume_slope_norm=0.12,
        hours_covered=24, detail=detail or f"{regime} volume.",
    )


# ── volume_supports: the direction rules ─────────────────────────────────────

@pytest.mark.parametrize("status", ["UNAVAILABLE", "STALE", "INSUFFICIENT"])
def test_unresolved_volume_never_supports_a_side(status):
    for direction in ("long", "short"):
        ok, why = ss.volume_supports(direction, ctx(status=status))
        assert ok is False
        assert status.lower() in why


def test_missing_context_never_supports_a_side():
    ok, why = ss.volume_supports("long", None)
    assert ok is False and "unresolved" in why


def test_rising_volume_confirmation_supports_a_long():
    ok, why = ss.volume_supports(
        "long", ctx(regime="ELEVATED", divergence="CONFIRMED_UP", rv=1.8)
    )
    assert ok is True
    assert "participation is behind the move" in why


def test_exhaustion_in_the_signals_own_direction_blocks_a_long():
    """rising price + falling relative volume → weaken / flag exhaustion."""
    ok, why = ss.volume_supports("long", ctx(divergence="EXHAUSTION_UP"))
    assert ok is False
    assert "running dry" in why


def test_exhaustion_in_the_signals_own_direction_blocks_a_short():
    ok, why = ss.volume_supports("short", ctx(divergence="EXHAUSTION_DOWN"))
    assert ok is False
    assert "EXHAUSTION_DOWN" in why


def test_exhaustion_against_the_signal_does_not_block():
    """Selling drying up is not a reason to refuse a long."""
    ok, _ = ss.volume_supports("long", ctx(divergence="EXHAUSTION_DOWN"))
    assert ok is True


def test_climactic_volume_against_the_move_blocks_with_reversal_reason():
    ok, why = ss.volume_supports(
        "long", ctx(regime="CLIMACTIC", divergence="CONFIRMED_DOWN", rv=3.2)
    )
    assert ok is False
    assert "reversal risk" in why
    # the mirror-image short is supported by the same tape
    ok_short, _ = ss.volume_supports(
        "short", ctx(regime="CLIMACTIC", divergence="CONFIRMED_DOWN", rv=3.2)
    )
    assert ok_short is True


def test_dead_regime_blocks_both_sides():
    for direction in ("long", "short"):
        ok, why = ss.volume_supports(direction, ctx(regime="DEAD", rv=0.2))
        assert ok is False
        assert "DEAD volume regime" in why


# ── the note that lands in the DB ────────────────────────────────────────────

def test_gate_note_carries_the_numbers():
    note = ss.volume_gate_note(ctx(regime="ELEVATED", divergence="CONFIRMED_UP", rv=1.9))
    assert "24h" in note and "Relative volume" in note and "ELEVATED" in note
    assert len(note) <= 400  # fits the DB text/varchar columns


def test_gate_note_explains_a_no_trade():
    note = ss.volume_gate_note(ctx(status="STALE", detail="last hour closed 6h ago"))
    assert note.startswith("NO_TRADE") and "last hour closed 6h ago" in note


def test_gate_note_handles_a_missing_context():
    assert "unresolved" in ss.volume_gate_note(None)


# ── resolve_volume against the real aggregation ──────────────────────────────

def _rows_15m(hourly_volumes, *, gap_s=0, now=None):
    """Build 15m rows: four bars per hour, ``hourly_volumes[-1]`` = latest hour.

    ``resolve_volume`` reads the wall clock, so these rows are anchored to it.
    """
    now = int(time.time()) if now is None else int(now)
    n = len(hourly_volumes)
    last_hour = (now // HOUR) * HOUR - HOUR - gap_s
    rows = []
    for i, hv in enumerate(hourly_volumes):
        start = last_hour - (n - 1 - i) * HOUR
        for b in range(4):
            rows.append([(start + b * 900) * 1000, 100, 100, 100, 100, hv / 4.0])
    return rows


def test_resolve_volume_ok_from_exchange_rows(monkeypatch):
    async def fake_fetch(symbol, timeframe, limit):
        assert (timeframe, limit) == (ss._VOL_TF, ss._VOL_LIMIT)
        return _rows_15m([100.0] * 24 + [300.0])

    monkeypatch.setattr(ss, "_fetch_ta_ohlcv", fake_fetch)
    resolved = asyncio.run(ss.resolve_volume("BTC/USDT"))
    assert resolved.status == "OK"
    assert resolved.regime == "CLIMACTIC"     # ×~2.8 of the 24h hourly mean
    assert resolved.volume_1h == pytest.approx(300.0)


def test_resolve_volume_reports_unavailable_when_the_feed_has_none(monkeypatch):
    async def fake_fetch(*_a, **_k):
        return _rows_15m([0.0] * 30)

    monkeypatch.setattr(ss, "_fetch_ta_ohlcv", fake_fetch)
    resolved = asyncio.run(ss.resolve_volume("EURUSD"))
    assert resolved.status == "UNAVAILABLE"
    ok, _ = ss.volume_supports("long", resolved)
    assert ok is False


def test_resolve_volume_reports_stale(monkeypatch):
    async def fake_fetch(*_a, **_k):
        return _rows_15m([100.0] * 30, gap_s=8 * HOUR)

    monkeypatch.setattr(ss, "_fetch_ta_ohlcv", fake_fetch)
    resolved = asyncio.run(ss.resolve_volume("BTC/USDT"))
    assert resolved.status == "STALE"


def test_resolve_volume_survives_a_failing_fetch(monkeypatch):
    async def fake_fetch(*_a, **_k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(ss, "_fetch_ta_ohlcv", fake_fetch)
    resolved = asyncio.run(ss.resolve_volume("BTC/USDT"))
    assert resolved.status == "UNAVAILABLE"
    ok, _ = ss.volume_supports("long", resolved)
    assert ok is False


def test_resolve_volume_insufficient_history(monkeypatch):
    async def fake_fetch(*_a, **_k):
        return _rows_15m([100.0] * 8)

    monkeypatch.setattr(ss, "_fetch_ta_ohlcv", fake_fetch)
    resolved = asyncio.run(ss.resolve_volume("NEWCOIN/USDT"))
    assert resolved.status == "INSUFFICIENT"
