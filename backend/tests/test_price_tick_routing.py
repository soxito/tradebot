"""The price-tick loop asks the source that actually carries the instrument.

Asking Bitget for XAU/USD is not just a wasted round trip: the connector logs
it at ERROR, so every gold, FX and index symbol on the watchlist produced an
error line on every tick — several a second, drowning the log.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (str(REPO_ROOT), str(REPO_ROOT / "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.core import scheduler  # noqa: E402
from app.services import market_data  # noqa: E402


class _Quote:
    def __init__(self, price: float, source: str):
        self.price = price
        self.source = source


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["XAU/USD", "XAUUSD", "EURUSD", "US30", "USOIL"])
async def test_non_crypto_never_reaches_the_crypto_exchange(monkeypatch, symbol):
    asked_bitget = []

    def _boom(*_a, **_k):
        asked_bitget.append(symbol)
        raise AssertionError(f"{symbol} was sent to the crypto exchange")

    monkeypatch.setattr(
        "app.exchanges.manager.exchange_manager.get_exchange", _boom, raising=False
    )

    async def _quote(sym, *, db=None, **_k):
        return _Quote(2650.5, "mt5:broker")

    monkeypatch.setattr(market_data, "get_quote", _quote)

    got_symbol, price = await scheduler._fetch_one_price(symbol, db=object())
    assert got_symbol == symbol
    assert price == 2650.5
    assert asked_bitget == []


@pytest.mark.asyncio
async def test_crypto_still_uses_the_exchange_fast_path(monkeypatch):
    """Crypto is what the exchange is for — that route must not regress."""
    called = {}

    class _Connector:
        async def get_ticker(self, sym):
            called["symbol"] = sym
            return {"last": 64000.0}

    monkeypatch.setattr(
        "app.exchanges.manager.exchange_manager.get_exchange",
        lambda *_a, **_k: _Connector(),
        raising=False,
    )

    async def _never(*_a, **_k):
        raise AssertionError("crypto must not go through the broker resolver")

    monkeypatch.setattr(market_data, "get_quote", _never)

    symbol, price = await scheduler._fetch_one_price("BTC/USDT")
    assert price == 64000.0
    assert called["symbol"] == "BTC/USDT"


@pytest.mark.asyncio
async def test_a_broker_lookup_without_a_session_is_not_silently_wrong(monkeypatch):
    """`_mt5_quote` returns None when `db` is None, dropping to a reference feed.

    The tick loop therefore has to pass a session — this asserts the session it
    is given actually reaches the quote layer, so "use the MT5 Live source"
    cannot quietly degrade to Yahoo.
    """
    seen = {}

    async def _quote(sym, *, db=None, **_k):
        seen["db"] = db
        return _Quote(1.085, "mt5:broker")

    monkeypatch.setattr(market_data, "get_quote", _quote)
    sentinel = object()

    await scheduler._fetch_one_price("EURUSD", db=sentinel)
    assert seen["db"] is sentinel, "the tick's session never reached the quote layer"


@pytest.mark.asyncio
async def test_an_unpriceable_symbol_yields_none_rather_than_raising(monkeypatch):
    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(market_data, "get_quote", _none)
    assert await scheduler._fetch_one_price("XAUUSD", db=object()) == ("XAUUSD", None)


def test_classification_sends_each_instrument_to_the_right_family():
    classify = lambda s: market_data.classify(market_data.normalize_symbol(s))  # noqa: E731
    assert classify("XAU/USD") == market_data.METAL
    assert classify("EURUSD") == "fx"
    assert classify("BTC/USDT") == market_data.CRYPTO
    assert classify("ETHUSDT") == market_data.CRYPTO
