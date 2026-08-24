"""Tests for the universal market-data resolver.

The symbol tests here guard the two defects that made FX, metals and indices
unreachable: a ``USDT`` suffix appended to instruments that already had a quote
leg, and a classifier that only recognised crypto.
"""

from __future__ import annotations

import asyncio

import pytest

from app.exchanges.yahoo_provider import YAHOO_TICKERS
from app.services import market_data as md


# ── Symbol normalisation ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("GBPUSD", "GBPUSD"),
        ("gbpusd", "GBPUSD"),
        ("GBP/USD", "GBPUSD"),
        ("XAUUSD", "XAUUSD"),
        ("XAUUSD.m", "XAUUSD"),
        ("US30.cash", "US30"),
        # Three letters, and no USD tail to demangle.
        ("dxy", "DXY"),
        ("VIX", "VIX"),
    ],
)
def test_normalize_leaves_complete_instruments_alone(raw, expected):
    assert md.normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    "mangled,repaired",
    [
        ("GBPUSDUSDT", "GBPUSD"),
        ("XAUUSDUSDT", "XAUUSD"),
        ("EURUSDUSDT", "EURUSD"),
        ("US30USDT", "US30"),
        ("NAS100USDT", "NAS100"),
    ],
)
def test_demangles_legacy_double_quote(mangled, repaired):
    """The old dispatcher appended USDT unconditionally; those strings persist."""
    assert md.normalize_symbol(mangled) == repaired


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
def test_crypto_keeps_its_quote_leg(symbol):
    """BTC alone is not a pair, so BTCUSDT must not be "repaired" to BTC."""
    assert md.normalize_symbol(symbol) == symbol


@pytest.mark.parametrize("token", ["GBPUSD", "XAUUSD", "EURUSD", "US30", "USOIL"])
def test_gbpusd_never_gains_a_usdt_suffix(token):
    """The exact regression that produced 'no Bitget-tradeable pair for GBPUSD'."""
    symbol, asset_class = md.canonicalize_for_analysis(token)
    assert not symbol.endswith("USDT"), f"{token} was mangled into {symbol}"
    assert symbol == token
    assert asset_class != md.CRYPTO


def test_canonicalize_crypto_returns_bare_base():
    """pair_catalog expands a bare base itself; gluing a quote on only hurts."""
    assert md.canonicalize_for_analysis("BTCUSDT") == ("BTC", md.CRYPTO)
    assert md.canonicalize_for_analysis("BTC") == ("BTC", md.CRYPTO)


def test_canonicalize_resolves_plain_english_aliases():
    assert md.canonicalize_for_analysis("gold")[0] == "XAUUSD"
    assert md.canonicalize_for_analysis("silver")[0] == "XAGUSD"
    assert md.canonicalize_for_analysis("nasdaq")[0] == "NAS100"


# ── Classification ───────────────────────────────────────────────────────────

def test_classify_covers_every_yahoo_ticker_key():
    """Every instrument Yahoo knows must classify — catches map drift."""
    unclassified = [s for s in YAHOO_TICKERS if md.classify(s) == md.UNKNOWN]
    assert not unclassified, f"unclassified Yahoo symbols: {unclassified}"


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("XAUUSD", md.METAL),
        ("XAGUSD", md.METAL),
        ("GBPUSD", md.FX),
        ("EURGBP", md.FX),
        ("US30", md.INDEX),
        ("GER40", md.INDEX),
        # Macro gauges — classified off their Yahoo ticker like any other index.
        ("DXY", md.INDEX),
        ("VIX", md.INDEX),
        ("USOIL", md.ENERGY),
        ("COCOA", md.SOFT),
        ("BTCUSDT", md.CRYPTO),
    ],
)
def test_classify_by_asset_class(symbol, expected):
    assert md.classify(symbol) == expected


def test_macro_gauges_resolve_to_their_cash_tickers():
    """Yahoo's chart endpoint 404s on DX=F and VX=F — only the cash series serve."""
    from app.exchanges.yahoo_provider import resolve_ticker

    assert resolve_ticker("DXY") == "DX-Y.NYB"
    assert resolve_ticker("VIX") == "^VIX"
    # The raw broker/Yahoo spelling folds onto the same key.
    assert resolve_ticker("DX-Y.NYB") == "DX-Y.NYB"


@pytest.mark.parametrize(
    "symbol", ["XAUUSD", "GBPUSD", "EURGBP", "US30", "USOIL", "DXY", "VIX"]
)
def test_is_universal_symbol_accepts_non_crypto(symbol):
    assert md.is_universal_symbol(symbol) is True


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
def test_is_universal_symbol_rejects_crypto(symbol):
    """Crypto belongs on the exchange connectors — deeper history, real volume."""
    assert md.is_universal_symbol(symbol) is False


# ── Extraction ───────────────────────────────────────────────────────────────

def test_extract_symbols_finds_mixed_asset_classes():
    found = md.extract_symbols("compare XAUUSD against GBPUSD and BTCUSDT please")
    assert found == ["XAUUSD", "GBPUSD", "BTCUSDT"]


def test_extract_symbols_rejects_stopwords():
    """A false positive costs a network fetch and a misleading prompt line."""
    assert md.extract_symbols("what is the price of the market right now") == []
    assert md.extract_symbols("can you close my long trade please") == []


def test_extract_symbols_resolves_aliases():
    assert md.extract_symbols("hows gold looking today") == ["XAUUSD"]


def test_extract_symbols_dedupes_and_preserves_order():
    assert md.extract_symbols("XAUUSD then GBPUSD then XAUUSD") == ["XAUUSD", "GBPUSD"]


def test_extract_symbols_honours_limit():
    text = "XAUUSD GBPUSD EURUSD US30 USOIL NAS100 GER40 UK100 JPN225 XAGUSD"
    assert len(md.extract_symbols(text, limit=3)) == 3


# ── Quote priority ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_priority_prefers_broker_then_yahoo(monkeypatch):
    """A live MT5 account wins: its bid/ask is where the user's orders fill.

    Uses an FX pair — metals have their own chain (spot ahead of Yahoo's
    futures), covered in test_metals_spot.py.
    """
    md._cache.clear()
    broker = md.Quote(
        symbol="GBPUSD", price=1.2700, source="mt5:Broker-Live",
        ts=0, asset_class=md.FX, bid=1.2699, ask=1.2701,
    )
    yahoo = md.Quote(
        symbol="GBPUSD", price=1.2710, source="yahoo:GBPUSD=X", ts=0, asset_class=md.FX
    )

    async def _mt5(symbol, db):
        return broker

    async def _yahoo(symbol):
        return yahoo

    monkeypatch.setattr(md, "_mt5_quote", _mt5)
    monkeypatch.setattr(md, "_yahoo_quote", _yahoo)

    got = await md.get_quote("GBPUSD", db=object())
    assert got is broker

    # With no broker, Yahoo serves it.
    md._cache.clear()
    monkeypatch.setattr(md, "_mt5_quote", lambda symbol, db: _none())
    got = await md.get_quote("GBPUSD", db=object())
    assert got is yahoo


async def _none():
    return None


@pytest.mark.asyncio
async def test_get_quotes_respects_wall_clock_budget(monkeypatch):
    """A hung provider must not stall the chat turn — partial beats nothing."""
    md._cache.clear()
    monkeypatch.setattr(md, "_BATCH_BUDGET_S", 0.3)

    async def _slow_or_fast(symbol, *, db=None):
        if symbol == "GBPUSD":
            return md.Quote(
                symbol="GBPUSD", price=1.27, source="yahoo:GBPUSD=X",
                ts=0, asset_class=md.FX,
            )
        await asyncio.sleep(30)
        return None

    monkeypatch.setattr(md, "get_quote", _slow_or_fast)

    started = asyncio.get_event_loop().time()
    out = await md.get_quotes(["GBPUSD", "XAUUSD"])
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < 3.0, "batch ignored its wall-clock budget"
    assert "GBPUSD" in out and "XAUUSD" not in out


# ── Prompt block ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_block_includes_non_crypto(monkeypatch):
    """The whole point: gold reaches the prompt, so the model can price it."""
    async def _quotes(symbols, *, db=None):
        return {
            "XAUUSD": md.Quote(
                symbol="XAUUSD", price=2400.0, source="yahoo:GC=F",
                ts=0, asset_class=md.METAL,
            )
        }

    monkeypatch.setattr(md, "get_quotes", _quotes)
    block = await md.price_block(["XAUUSD"])

    assert "XAUUSD" in block
    assert "2,400.00" in block
    assert "yahoo:GC=F" in block


@pytest.mark.asyncio
async def test_price_block_never_claims_the_feed_does_not_exist(monkeypatch):
    """An empty fetch is a transient failure, not a missing capability."""
    async def _empty(symbols, *, db=None):
        return {}

    monkeypatch.setattr(md, "get_quotes", _empty)
    block = await md.price_block(["XAUUSD"])

    lowered = block.lower()
    assert "does not provide" not in lowered
    assert "toolset does not include" not in lowered
    assert "retry" in lowered


@pytest.mark.asyncio
async def test_price_block_respects_max_lines(monkeypatch):
    async def _quotes(symbols, *, db=None):
        return {
            s: md.Quote(symbol=s, price=1.0, source="test", ts=0, asset_class=md.FX)
            for s in symbols
        }

    async def _top(limit):
        return [f"  FILLER{i}: 1.0" for i in range(200)]

    monkeypatch.setattr(md, "get_quotes", _quotes)
    monkeypatch.setattr(md, "_top_crypto_lines", _top)

    block = await md.price_block(["GBPUSD"], include_top_crypto=True, max_lines=10)
    price_lines = [ln for ln in block.splitlines() if ln.startswith("  ")]
    assert len(price_lines) <= 10


@pytest.mark.asyncio
async def test_price_block_keeps_mentioned_symbol_ahead_of_filler(monkeypatch):
    """Truncation must never drop the instrument the user actually asked about."""
    async def _quotes(symbols, *, db=None):
        return {
            "XAUUSD": md.Quote(
                symbol="XAUUSD", price=2400.0, source="yahoo:GC=F",
                ts=0, asset_class=md.METAL,
            )
        }

    async def _top(limit):
        return [f"  FILLER{i}: 1.0" for i in range(200)]

    monkeypatch.setattr(md, "get_quotes", _quotes)
    monkeypatch.setattr(md, "_top_crypto_lines", _top)

    block = await md.price_block(["XAUUSD"], include_top_crypto=True, max_lines=5)
    assert "XAUUSD" in block


# ── One token → one instrument ───────────────────────────────────────────────

@pytest.mark.parametrize(("token", "expected"), [
    ("CADJPY", "CADJPY"), ("cadjpy", "CADJPY"), ("GBPJPY", "GBPJPY"),
    ("XAU/USD", "XAUUSD"), ("US30", "US30"), ("nas100", "NAS100"),
    ("gold", "XAUUSD"), ("oil", "USOIL"), ("BTC", "BTCUSDT"),
    ("ETH/USDT", "ETHUSDT"),
    ("why", None), ("the", None), ("analyse", None), ("about", None),
    ("USD", None), ("1h", None), ("", None),
])
def test_symbol_from_token_reads_one_token(token, expected):
    assert md.symbol_from_token(token) == expected


@pytest.mark.parametrize(("token", "expected"), [
    # Unmistakable spellings survive inside a sentence.
    ("CADJPY", "CADJPY"), ("XAUUSD", "XAUUSD"), ("US30", "US30"),
    ("BTCUSDT", "BTCUSDT"),
    # Plain-English names are words first when they are not typed alone.
    ("gold", None), ("oil", None), ("silver", None), ("dow", None),
])
def test_strict_mode_only_accepts_an_unmistakable_spelling(token, expected):
    assert md.symbol_from_token(token, strict=True) == expected
