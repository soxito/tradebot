"""Metals must be quoted at spot, not at the COMEX future.

Yahoo has no spot metal ticker, so ``yahoo_provider`` maps gold to ``GC=F``.
A future carries cost of carry to expiry: measured on one tick, ``GC=F`` read
4105.10 while spot gold was 4047.31 — $58/oz apart. Quoting that as "the gold
price" is the wrong number, and building a trade proposal from futures candles
puts entry, stop and target where no broker would fill them.
"""

from __future__ import annotations

import pytest

from app.exchanges import metals_provider as mp
from app.services import market_data as md


# ── Symbol coverage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "symbol,code",
    [
        ("XAUUSD", "XAU"), ("GOLD", "XAU"), ("xauusd", "XAU"), ("XAU/USD", "XAU"),
        ("XAGUSD", "XAG"), ("SILVER", "XAG"),
        ("XPTUSD", "XPT"), ("XPDUSD", "XPD"),
    ],
)
def test_metal_codes_resolve(symbol, code):
    assert mp.metal_code(symbol) == code
    assert mp.is_spot_metal(symbol) is True


@pytest.mark.parametrize("symbol", ["GBPUSD", "BTCUSDT", "US30", "USOIL", ""])
def test_non_metals_are_not_claimed(symbol):
    assert mp.metal_code(symbol) is None
    assert mp.is_spot_metal(symbol) is False


# ── Sanity band ──────────────────────────────────────────────────────────────

def test_sanity_band_rejects_an_obviously_wrong_quote():
    """A feed that changes shape must not silently publish a nonsense price."""
    assert mp._sane("XAU", 4047.0) is True
    assert mp._sane("XAU", 4.0) is False        # silver-scale value under gold
    assert mp._sane("XAG", 57.9) is True
    assert mp._sane("XAG", 4047.0) is False     # the old silver-returns-gold bug


@pytest.mark.asyncio
async def test_swissquote_parsing_picks_the_tightest_spread(monkeypatch):
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [{
                "topo": {"platform": "SwissquoteLtd"},
                "ts": 1785525396378,
                "spreadProfilePrices": [
                    {"spreadProfile": "standard", "askSpread": 90.0,
                     "bid": 4040.0, "ask": 4055.0},
                    {"spreadProfile": "premium", "askSpread": 25.4,
                     "bid": 4047.186, "ask": 4047.844},
                ],
            }]

    class _Client:
        async def get(self, *a, **k): return _Resp()

    out = await mp._from_swissquote(_Client(), "XAU")
    assert out["bid"] == 4047.186 and out["ask"] == 4047.844
    assert out["price"] == pytest.approx(4047.515, abs=0.01)
    assert out["source"] == "swissquote-spot"
    assert out["ts"] == 1785525396


@pytest.mark.asyncio
async def test_a_dead_source_falls_through_to_the_next(monkeypatch):
    mp._cache.clear()
    calls = []

    async def _dead(client, code):
        calls.append("swissquote")
        raise RuntimeError("connection refused")

    async def _alive(client, code):
        calls.append("gold-api")
        return {"price": 4047.6, "bid": None, "ask": None,
                "source": "gold-api-spot", "ts": 0}

    monkeypatch.setattr(mp, "_from_swissquote", _dead)
    monkeypatch.setattr(mp, "_from_gold_api", _alive)

    out = await mp.fetch_spot("XAUUSD")
    assert out["source"] == "gold-api-spot"
    assert calls == ["swissquote", "gold-api"]
    mp._cache.clear()


@pytest.mark.asyncio
async def test_fetch_spot_returns_none_rather_than_a_futures_price(monkeypatch):
    """This function only ever answers with genuine spot."""
    mp._cache.clear()

    async def _dead(client, code):
        raise RuntimeError("down")

    for name in ("_from_swissquote", "_from_gold_api", "_from_coingecko"):
        monkeypatch.setattr(mp, name, _dead)
    assert await mp.fetch_spot("XAUUSD") is None
    mp._cache.clear()


# ── Quote priority ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spot_beats_yahoo_futures_for_metals(monkeypatch):
    md._cache.clear()
    spot = md.Quote(symbol="XAUUSD", price=4047.31, source="swissquote-spot",
                    ts=0, asset_class=md.METAL)
    futures = md.Quote(symbol="XAUUSD", price=4105.10, source="yahoo:GC=F",
                       ts=0, asset_class=md.METAL)

    async def _spot(symbol): return spot
    async def _yahoo(symbol): return futures
    async def _no_mt5(symbol, db): return None

    monkeypatch.setattr(md, "_metals_spot_quote", _spot)
    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)
    monkeypatch.setattr(md, "_mt5_quote", _no_mt5)

    got = await md.get_quote("XAUUSD")
    assert got is spot, "the COMEX future was quoted as the gold price"
    md._cache.clear()


@pytest.mark.asyncio
async def test_yahoo_still_serves_metals_when_spot_is_down(monkeypatch):
    md._cache.clear()
    futures = md.Quote(symbol="XAUUSD", price=4105.10, source="yahoo:GC=F",
                       ts=0, asset_class=md.METAL)

    async def _none(symbol): return None
    async def _yahoo(symbol): return futures
    async def _no_mt5(symbol, db): return None

    monkeypatch.setattr(md, "_metals_spot_quote", _none)
    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)
    monkeypatch.setattr(md, "_mt5_quote", _no_mt5)

    assert (await md.get_quote("XAUUSD")) is futures
    md._cache.clear()


# ── Candle anchoring ─────────────────────────────────────────────────────────

def _futures_rows(last: float):
    """Five bars ending at *last*, each spanning ±2."""
    return [
        [1000 * (i + 1), last - 10 + i * 2, last - 8 + i * 2,
         last - 12 + i * 2, last - 10 + i * 2, 100.0]
        for i in range(4)
    ] + [[5000, last - 2, last + 2, last - 4, last, 100.0]]


@pytest.mark.asyncio
async def test_metal_candles_are_anchored_onto_spot(monkeypatch):
    """Levels must match the quote — otherwise proposals are unfillable."""
    async def _spot(symbol):
        return md.Quote(symbol="XAUUSD", price=4047.31, source="swissquote-spot",
                        ts=0, asset_class=md.METAL)

    monkeypatch.setattr(md, "_metals_spot_quote", _spot)
    rows = _futures_rows(4105.10)
    out, source = await md.anchor_metal_series_to_spot("XAUUSD", rows, "GC=F")

    assert out[-1][4] == pytest.approx(4047.31, abs=0.01)
    assert "swissquote-spot" in source and "GC=F" in source
    # Shape and volume are the futures market's and must survive untouched.
    assert out[-1][2] - out[-1][3] == pytest.approx(rows[-1][2] - rows[-1][3], abs=1e-6)
    assert [r[5] for r in out] == [r[5] for r in rows]
    assert [r[0] for r in out] == [r[0] for r in rows]


@pytest.mark.asyncio
async def test_absurd_basis_leaves_the_series_alone(monkeypatch):
    """A bad tick must not shift the whole series by a bogus amount."""
    async def _spot(symbol):
        return md.Quote(symbol="XAUUSD", price=40.0, source="broken",
                        ts=0, asset_class=md.METAL)

    monkeypatch.setattr(md, "_metals_spot_quote", _spot)
    rows = _futures_rows(4105.10)
    out, source = await md.anchor_metal_series_to_spot("XAUUSD", rows, "GC=F")

    assert out == rows, "the series was shifted by an implausible basis"
    assert "futures" in source, "an unanchored series must be labelled as futures"


@pytest.mark.asyncio
async def test_no_spot_available_labels_the_series_as_futures(monkeypatch):
    async def _none(symbol): return None

    monkeypatch.setattr(md, "_metals_spot_quote", _none)
    rows = _futures_rows(4105.10)
    out, source = await md.anchor_metal_series_to_spot("XAUUSD", rows, "GC=F")

    assert out == rows
    assert source == "yahoo:GC=F (futures)", (
        "futures must never be presented as spot without saying so"
    )


# ── Spot / futures basis selection ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_basis_futures_returns_the_contract_not_spot(monkeypatch):
    """Asking for futures must not be quietly answered with spot."""
    md._cache.clear()
    spot = md.Quote(symbol="XAUUSD", price=4047.31, source="swissquote-spot",
                    ts=0, asset_class=md.METAL)
    futures = md.Quote(symbol="XAUUSD", price=4105.10, source="yahoo:GC=F",
                       ts=0, asset_class=md.METAL)

    async def _spot(symbol): return spot
    async def _yahoo(symbol): return futures
    async def _no_mt5(symbol, db): return None

    monkeypatch.setattr(md, "_metals_spot_quote", _spot)
    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)
    monkeypatch.setattr(md, "_mt5_quote", _no_mt5)

    assert (await md.get_quote("XAUUSD", basis=md.FUTURES)) is futures
    md._cache.clear()
    assert (await md.get_quote("XAUUSD", basis=md.SPOT)) is spot
    md._cache.clear()


@pytest.mark.asyncio
async def test_the_two_bases_do_not_share_a_cache_entry(monkeypatch):
    """Otherwise whichever was asked for first would answer both."""
    md._cache.clear()
    spot = md.Quote(symbol="XAUUSD", price=4047.31, source="swissquote-spot",
                    ts=0, asset_class=md.METAL)
    futures = md.Quote(symbol="XAUUSD", price=4105.10, source="yahoo:GC=F",
                       ts=0, asset_class=md.METAL)

    async def _spot(symbol): return spot
    async def _yahoo(symbol): return futures
    async def _no_mt5(symbol, db): return None

    monkeypatch.setattr(md, "_metals_spot_quote", _spot)
    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)
    monkeypatch.setattr(md, "_mt5_quote", _no_mt5)

    first = await md.get_quote("XAUUSD", basis=md.SPOT)
    second = await md.get_quote("XAUUSD", basis=md.FUTURES)
    assert first.price != second.price, "the futures request was served from the spot cache"
    md._cache.clear()


@pytest.mark.asyncio
async def test_basis_is_ignored_for_non_metals(monkeypatch):
    """FX has one series — asking for futures must still return the pair."""
    md._cache.clear()
    fx = md.Quote(symbol="GBPUSD", price=1.27, source="yahoo:GBPUSD=X",
                  ts=0, asset_class=md.FX)

    async def _yahoo(symbol): return fx
    async def _no_mt5(symbol, db): return None

    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)
    monkeypatch.setattr(md, "_mt5_quote", _no_mt5)

    assert (await md.get_quote("GBPUSD", basis=md.FUTURES)) is fx
    md._cache.clear()
