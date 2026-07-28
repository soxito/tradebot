"""Where FX volume comes from, and that the gate can actually pass on it.

Regression cover for `/forecast GBPUSD 1h` returning NO_TRADE forever. Two
faults sat behind that:

  1. every forex symbol was routed to ``forex_provider``, whose FX leg is
     Frankfurter — *daily* bars with a hard-coded ``0.0`` volume — so a "1h"
     request silently ran on daily candles and then failed the volume gate,
     which is a hard precondition;
  2. spot FX is OTC and prints no volume on any feed, so no spot source could
     ever have satisfied that gate.

The fix routes these symbols through ``yahoo_provider``, which already borrows
the matching CME currency future's contract volume (GBP → ``6B=F``) and matches
it bar-for-bar. These tests are offline — the provider is stubbed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from plugins.KronosForecastPlugin.backend.services import forecast_service as fs
from plugins.KronosForecastPlugin.backend.services import volume_context as vc


# ── symbol routing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", [
    "GBPUSD", "GBP/USD", "EURUSD", "USDJPY",   # forex_provider majors
    "EURGBP", "EURJPY", "AUDNZD",              # crosses only Yahoo knows
    "XAUUSD", "US30", "NAS100", "USOIL",       # metals / indices / energy
])
def test_non_crypto_symbols_route_to_the_forex_path(symbol):
    assert fs._is_forex(symbol) is True


@pytest.mark.parametrize("symbol", ["BTC/USDT", "BTCUSD", "ETH/USDT", "SOLUSD", "ETHBTC"])
def test_crypto_stays_on_the_exchange_connectors(symbol):
    """Yahoo resolves BTCUSD to `BTC-USD`, but crypto must not be diverted:
    the exchanges carry deeper history and real traded volume."""
    assert fs._is_forex(symbol) is False


# ── volume unit labelling ────────────────────────────────────────────────────

def test_fx_volume_is_labelled_as_futures_not_spot():
    assert fs._volume_unit("bitget", "GBP/USD") == "futures"


def test_mt5_volume_is_labelled_as_tick():
    assert fs._volume_unit("mt5", "GBPUSD") == "tick"


def test_crypto_volume_is_labelled_as_base():
    assert fs._volume_unit("bitget", "BTC/USDT") == "base"


def test_futures_unit_is_named_in_user_facing_evidence():
    ctx = vc.VolumeContext(
        status="OK", symbol="GBP/USD", source="ohlcv:1h", volume_unit="futures",
        volume_24h=60_000.0, volume_1h=4_500.0, hourly_mean_24h=2_500.0,
        relative_volume=1.8, regime="ELEVATED", divergence="CONFIRMED_UP",
        divergence_bars=6, price_change_pct=0.3, volume_slope_norm=0.05,
        hours_covered=24,
    )
    joined = " ".join(vc.volume_evidence_lines(ctx))
    # The reader must not mistake CME contract volume for spot GBP/USD volume.
    assert "CME futures volume" in joined


# ── Yahoo adapter ────────────────────────────────────────────────────────────

def _yahoo_bars(n: int, *, start: int, volume=1000.0):
    return [
        {"time": start + i * 3600, "open": 1.33, "high": 1.34,
         "low": 1.32, "close": 1.33, "volume": volume}
        for i in range(n)
    ]


def test_yahoo_bars_become_ccxt_rows(monkeypatch):
    from app.exchanges import yahoo_provider

    seen = {}

    async def fake(symbol, timeframe, limit):
        seen.update(symbol=symbol, timeframe=timeframe, limit=limit)
        return _yahoo_bars(3, start=1_700_000_000)

    monkeypatch.setattr(yahoo_provider, "fetch_candles", fake)
    rows = asyncio.run(fs._fetch_yahoo_ohlcv("GBP/USD", "1h", 400))

    # ccxt timeframes are translated to the MT5-style names Yahoo speaks.
    assert seen["timeframe"] == "H1"
    assert rows[0] == [1_700_000_000_000, 1.33, 1.34, 1.32, 1.33, 1000.0]


def test_yahoo_bars_without_volume_become_zero_not_none(monkeypatch):
    """A None must not reach the gate as a bare null — it is scored as 0.0,
    which the 24h total then reports as UNAVAILABLE rather than 'no activity'."""
    from app.exchanges import yahoo_provider

    async def fake(symbol, timeframe, limit):
        return _yahoo_bars(3, start=1_700_000_000, volume=None)

    monkeypatch.setattr(yahoo_provider, "fetch_candles", fake)
    rows = asyncio.run(fs._fetch_yahoo_ohlcv("GBP/USD", "1h", 400))
    assert [r[5] for r in rows] == [0.0, 0.0, 0.0]


def test_timeframe_yahoo_cannot_serve_falls_back(monkeypatch):
    """Yahoo has no 6h bar; returning [] lets _fetch_ohlcv try the next source
    instead of silently answering a 6h request with something else."""
    from app.exchanges import yahoo_provider

    async def fake(symbol, timeframe, limit):  # pragma: no cover — must not run
        raise AssertionError("Yahoo should not be called for an unmappable timeframe")

    monkeypatch.setattr(yahoo_provider, "fetch_candles", fake)
    assert asyncio.run(fs._fetch_yahoo_ohlcv("GBP/USD", "6h", 400)) == []


# ── the gate's own fetch ─────────────────────────────────────────────────────

def test_volume_gate_never_reads_the_zero_volume_forex_provider(monkeypatch):
    """forex_provider is a *price* fallback only. Frankfurter reports 0.0 and
    CoinGecko's PAXG proxy measures a token rather than the metal — letting
    either back the gate would refuse every forecast or mislabel the evidence."""
    async def boom(*a, **k):  # pragma: no cover — must not run
        raise AssertionError("forex_provider must not back the volume gate")

    called = {}

    async def yahoo(symbol, timeframe, limit):
        called["yahoo"] = True
        return [[1_700_000_000_000, 1.33, 1.34, 1.32, 1.33, 900.0]]

    monkeypatch.setattr(fs, "_fetch_forex_ohlcv", boom)
    monkeypatch.setattr(fs, "_fetch_yahoo_ohlcv", yahoo)

    rows = asyncio.run(fs._fetch_volume_ohlcv("bitget", "GBP/USD", "1h", 48))
    assert rows and called.get("yahoo") is True


def test_price_fetch_prefers_yahoo_and_falls_back_to_forex_provider(monkeypatch):
    order = []

    async def yahoo(symbol, timeframe, limit):
        order.append("yahoo")
        return []          # Yahoo down

    async def forex(symbol, timeframe, limit):
        order.append("forex")
        return [[1_700_000_000_000, 1.33, 1.34, 1.32, 1.33, 0.0]]

    monkeypatch.setattr(fs, "_fetch_yahoo_ohlcv", yahoo)
    monkeypatch.setattr(fs, "_fetch_forex_ohlcv", forex)

    rows = asyncio.run(fs._fetch_ohlcv("bitget", "GBP/USD", "1h", 400))
    assert order == ["yahoo", "forex"] and len(rows) == 1


# ── end to end: the gate now passes on real-shaped FX rows ───────────────────

def _hourly_fx_rows(hours: int, now: float, volume=2_500.0, close=1.33):
    """`hours` complete clock hours ending at the last full hour before `now`."""
    last_start = int(now - now % 3600) - 3600
    return [
        [(last_start - (hours - 1 - i) * 3600) * 1000,
         close, close * 1.0005, close * 0.9995, close, volume]
        for i in range(hours)
    ]


def test_fx_rows_with_cme_volume_resolve_ok():
    now = time.time()
    ctx = vc.build_volume_context(
        _hourly_fx_rows(24, now), symbol="GBP/USD", timeframe="1h",
        source="ohlcv:1h", volume_unit="futures", now=now,
    )
    assert ctx.status == "OK"
    assert ctx.hours_covered == 24
    assert ctx.volume_24h == pytest.approx(24 * 2_500.0)
    assert ctx.regime == "NORMAL"


def test_the_original_frankfurter_shape_is_still_refused():
    """Daily bars at 0.0 volume — exactly what `/forecast GBPUSD 1h` used to get.
    The gate must keep refusing it; the fix is a better source, not a looser gate."""
    now = time.time()
    day = int(now - now % 86400)
    rows = [[(day - (30 - i) * 86400) * 1000, 1.33, 1.34, 1.32, 1.33, 0.0] for i in range(30)]
    ctx = vc.build_volume_context(
        rows, symbol="GBP/USD", timeframe="1d", source="ohlcv:1d", now=now,
    )
    assert ctx.status != "OK"


# ── coverage across the instrument classes ───────────────────────────────────

@pytest.mark.parametrize("symbol,future", [
    # majors
    ("EURUSD", "6E=F"), ("GBPUSD", "6B=F"), ("USDJPY", "6J=F"),
    ("AUDUSD", "6A=F"), ("USDCAD", "6C=F"), ("USDCHF", "6S=F"),
    ("NZDUSD", "6N=F"), ("USDMXN", "6M=F"),
    # thinner contracts — a thin tape is a real read, not a missing one
    ("USDZAR", "6Z=F"), ("USDBRL", "6L=F"), ("USDSEK", "SEK=F"),
    ("USDNOK", "NOK=F"), ("USDCNH", "CNH=F"),
])
def test_every_mapped_currency_has_its_futures_leg(symbol, future):
    from app.exchanges import yahoo_provider
    assert yahoo_provider._fx_volume_tickers(symbol) == [future]


@pytest.mark.parametrize("symbol,legs", [
    ("EURGBP", ["6E=F", "6B=F"]),
    ("GBPJPY", ["6B=F", "6J=F"]),
    ("AUDNZD", ["6A=F", "6N=F"]),
])
def test_a_cross_uses_both_legs(symbol, legs):
    """A cross is only as active as its least active side, so both legs are
    required and `_attach_fx_volume` takes the lower of the two."""
    from app.exchanges import yahoo_provider
    assert yahoo_provider._fx_volume_tickers(symbol) == legs


@pytest.mark.parametrize("symbol", ["USDTRY", "USDSGD", "USDHKD", "EURPLN"])
def test_currencies_without_a_listed_future_get_no_volume(symbol):
    """No volume is better evidence than another instrument's volume: half a
    cross measures one currency's activity, not the pair's. These stay
    NO_TRADE, which is the honest answer rather than a manufactured one."""
    from app.exchanges import yahoo_provider
    assert yahoo_provider._fx_volume_tickers(symbol) == []


@pytest.mark.parametrize("symbol,future", [
    ("US30", "YM=F"), ("DJ30", "YM=F"), ("US500", "ES=F"), ("SP500", "ES=F"),
    ("NAS100", "NQ=F"), ("USTEC", "NQ=F"), ("US2000", "RTY=F"),
    ("JPN225", "NKD=F"),
])
def test_indices_resolve_to_their_front_month_future(symbol, future):
    """A cash index only prints during its own exchange session — ^DJI measured
    16.6h stale mid-Europe-session — and sits ~1.25% under the future an MT5
    index CFD is actually quoted off. Resolving straight to the future keeps the
    chart fresh, matches fills, and puts price and volume on the same bars."""
    from app.exchanges import yahoo_provider
    assert yahoo_provider.resolve_ticker(symbol) == future


@pytest.mark.parametrize("symbol", ["GER40", "UK100", "HK50", "AUS200"])
def test_indices_without_a_listed_future_stay_on_cash(symbol):
    """Yahoo 404s on FDAX=F / FESX=F / HSI=F. These keep pointing at cash and
    carry no volume, which the gate reports as NO_TRADE rather than papering
    over with another index's activity."""
    from app.exchanges import yahoo_provider
    assert (yahoo_provider.resolve_ticker(symbol) or "").startswith("^") or \
        yahoo_provider.resolve_ticker(symbol) == "000001.SS"


def test_index_price_and_volume_come_from_one_series(monkeypatch):
    """Because the symbol resolves to the future, the rows the forecast already
    holds carry that contract's volume — no second fetch, and no chance of the
    gate reading a different instrument than the chart draws."""
    now = time.time()
    rows = _hourly_fx_rows(24, now, volume=9_000.0, close=52_861.0)

    async def must_not_refetch(*a, **k):  # pragma: no cover
        raise AssertionError("index volume must come from the rows already held")

    monkeypatch.setattr(fs, "_fetch_volume_ohlcv", must_not_refetch)
    ctx = asyncio.run(fs._resolve_volume("bitget", "US30", "1h", rows))

    assert ctx.status == "OK"
    assert ctx.volume_24h == pytest.approx(24 * 9_000.0)


def test_cme_maintenance_halt_hour_does_not_break_the_read():
    """CME FX futures halt 21:00-22:00 UTC, so one hour a day genuinely trades
    nothing. That is real data, not a gap — the 24h read must still resolve."""
    now = time.time()
    rows = _hourly_fx_rows(24, now)
    rows[10][5] = 0.0
    ctx = vc.build_volume_context(
        rows, symbol="GBP/USD", timeframe="1h", source="ohlcv:1h",
        volume_unit="futures", now=now,
    )
    assert ctx.status == "OK"
    assert ctx.volume_24h == pytest.approx(23 * 2_500.0)
