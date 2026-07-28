"""`/forecast` and `/order` on non-crypto pairs.

Kronos now forecasts FX, metals and indices (Yahoo prices them, CME contract
volume satisfies the hard volume gate), so `/forecast GBPUSD 1h` produces real
sniper entries where it used to return NO_TRADE. Those entries are *analysis*:
Bitget lists none of these instruments, so the execute buttons the sniper
keyboard normally renders would offer an order the exchange cannot fill.

These tests pin both halves — the symbol spellings Telegram sends reach the
forecast service intact, and nothing offers to trade an unlistable pair.
"""
from __future__ import annotations

import asyncio

import pytest

from plugins.KronosForecastPlugin.backend.services import forecast_service as fs
from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


# ── the symbol Telegram actually sends ───────────────────────────────────────

@pytest.mark.parametrize("typed,sent", [
    ("GBPUSD", "GBP/USD"),      # the reported command
    ("gbpusd", "GBP/USD"),
    ("EURUSD", "EUR/USD"),
    ("USDJPY", "USDJPY"),       # no trailing USD to split on — passed through
    ("EURGBP", "EURGBP"),
    ("XAUUSD", "XAU/USD"),
    ("US30", "US30"),
    ("BTCUSDT", "BTC/USDT"),
])
def test_normalised_symbol_is_what_the_forecast_service_receives(typed, sent):
    assert cs._norm_sym(typed) == sent


@pytest.mark.parametrize("typed", [
    "GBPUSD", "gbpusd", "EURUSD", "USDJPY", "EURGBP", "GBPJPY",
    "XAUUSD", "XAGUSD", "USOIL", "US30", "NAS100",
])
def test_every_spelling_telegram_sends_routes_to_the_fx_volume_path(typed):
    """The Telegram-normalised form must still be recognised as non-crypto —
    a spelling that slipped through would fall back to the crypto connectors
    and fail the volume gate exactly as before."""
    assert fs._is_forex(cs._norm_sym(typed)) is True


@pytest.mark.parametrize("typed", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCUSD"])
def test_crypto_spellings_stay_on_the_exchange_connectors(typed):
    assert fs._is_forex(cs._norm_sym(typed)) is False


# ── argument parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("args,exchange,timeframe", [
    # the reported command — used to read "1h" as the exchange and then forecast
    # the default 1h by coincidence; "/forecast GBPUSD 4h" was silently wrong
    (["1h"], "bitget", "1h"),
    (["4h"], "bitget", "4h"),
    (["15m"], "bitget", "15m"),
    ([], "bitget", "1h"),
    (["bitget"], "bitget", "1h"),
    (["bitget", "4h"], "bitget", "4h"),
    (["4h", "binance"], "binance", "4h"),      # either order
    (["BINANCE", "1D"], "binance", "1d"),      # case-insensitive
])
def test_exchange_and_timeframe_are_order_independent(args, exchange, timeframe):
    assert cs._parse_exchange_timeframe(args) == (exchange, timeframe)


# ── nothing offers to trade a pair the exchange does not list ────────────────

@pytest.mark.parametrize("symbol", [
    "GBP/USD", "EUR/USD", "USDJPY", "EURGBP", "XAU/USD", "USOIL", "US30",
])
def test_non_crypto_entries_are_analysis_only(symbol):
    assert fs.is_order_placeable(symbol) is False


@pytest.mark.parametrize("symbol", ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
def test_crypto_entries_stay_executable(symbol):
    assert fs.is_order_placeable(symbol) is True


def _order(args):
    class _DB:  # the guard returns before the session is touched
        pass
    return asyncio.run(cs._handle_order(args, _DB()))


@pytest.mark.parametrize("args", [
    "long GBPUSD 100",
    "live long GBPUSD 5",
    "live limit short XAUUSD 50",
    "long US30 25",
])
def test_order_refuses_an_unlistable_pair_before_sizing(args):
    """Both the typed command and every inline button land in `_handle_order`,
    so the guard sits there — a stale button from an older message is refused
    too, rather than sizing a position and firing it at Bitget."""
    text, _mode = _order(args)
    assert "cannot be ordered here" in text
    assert "forecast" in text.lower()


def test_order_still_works_for_crypto(monkeypatch):
    """The guard must not swallow the crypto path it sits in front of."""
    async def sniper(exchange, symbol, timeframe):
        raise RuntimeError("reached the sniper lookup")

    monkeypatch.setattr(fs, "generate_sniper_signals", sniper)
    text, _mode = _order("long BTCUSDT 100")
    assert "cannot be ordered here" not in text
