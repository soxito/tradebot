"""Broker-agnostic symbol resolution for order placement.

Every broker spells the same instrument differently — Exness Standard/Cent use
BTCUSDm, raw/ECN books use BTCUSD.r / #BTCUSD, some slash it (BTC/USD), and a
MetaQuotes forex demo carries no crypto at all. These tests lock the resolver
that scans the broker's own SymbolList so a bare "BTCUSD" is never sent blind.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.services.mt5_client import MT5Client  # noqa: E402


def _client_with_symbols(monkeypatch, symbols: list[str]) -> MT5Client:
    client = MT5Client()

    async def _fake_list(login, server, password):  # noqa: ANN001
        return symbols

    monkeypatch.setattr(client, "_list_symbols_cached", _fake_list)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table, request_sym, expected",
    [
        # Exness Standard/Cent: 'm' suffix on every symbol.
        (["EURUSDm", "XAUUSDm", "BTCUSDm", "ETHUSDm"], "BTCUSD", "BTCUSDm"),
        # Raw/ECN book: '.r' suffix.
        (["BTCUSD.r", "ETHUSD.r"], "BTCUSD", "BTCUSD.r"),
        # Prefixed listing (some brokers group crypto under '#').
        (["#BTCUSD", "#ETHUSD"], "ETHUSD", "#ETHUSD"),
        # Slashed spelling folds to the same canonical pair.
        (["BTC/USD", "ETH/USD"], "BTCUSD", "BTC/USD"),
        # Exchange-format request (USDT) resolves to the broker's USD CFD.
        (["BTCUSD", "ETHUSD"], "BTCUSDT", "BTCUSD"),
        # Plain names pass through unchanged (Markets.com, IC Markets Raw).
        (["BTCUSD", "EURUSD", "XAUUSD"], "BTCUSD", "BTCUSD"),
        # Prefer the least-decorated variant when several match.
        (["BTCUSD", "BTCUSDm", "BTCUSD.r"], "BTCUSD", "BTCUSD"),
        # ISO 'XBT' code for Bitcoin (Kraken-lineage / some EU brokers).
        (["XBTUSD", "ETHUSD"], "BTCUSD", "XBTUSD"),
        (["XBTUSDm", "ETHUSDm"], "BTCUSDT", "XBTUSDm"),
    ],
)
async def test_resolve_symbol_across_broker_spellings(monkeypatch, table, request_sym, expected):
    client = _client_with_symbols(monkeypatch, table)
    got = await client.resolve_symbol("100", "Broker-Demo", "pw", request_sym)
    assert got == expected


@pytest.mark.asyncio
async def test_metaquotes_demo_without_crypto_resolves_to_none(monkeypatch):
    # A forex-only demo (MetaQuotes-Demo) lists no crypto — BTCUSD must be
    # reported absent so the caller skips the account, never firing a doomed order.
    client = _client_with_symbols(monkeypatch, ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"])
    assert await client.resolve_symbol("100", "MetaQuotes-Demo", "pw", "BTCUSD") is None


@pytest.mark.asyncio
async def test_get_symbol_info_reports_not_found_for_missing_crypto(monkeypatch):
    client = _client_with_symbols(monkeypatch, ["EURUSD", "XAUUSD"])
    info = await client.get_symbol_info("100", "MetaQuotes-Demo", "pw", "BTCUSD")
    assert info["available"] is False
    assert "not found" in info["message"].lower()


@pytest.mark.asyncio
async def test_place_order_skips_cleanly_when_symbol_absent(monkeypatch):
    # place_order must NOT send an order for an unlisted symbol; it returns a
    # ticketless SYMBOL_NOT_FOUND result so callers report a clean skip.
    client = _client_with_symbols(monkeypatch, ["EURUSD", "XAUUSD"])
    sent: list = []

    async def _fake_token(login, server, password):  # noqa: ANN001
        return "tok"

    async def _fake_get(path, params=None, retries=1):  # noqa: ANN001
        sent.append((path, params))
        return {"ticket": "999"}

    monkeypatch.setattr(client, "get_token", _fake_token)
    monkeypatch.setattr(client, "_get", _fake_get)

    result = await client.place_order(
        login="100", server="MetaQuotes-Demo", password="pw",
        symbol="BTCUSD", order_type="buy", volume=0.1, price=0,
    )
    assert result.get("code") == "SYMBOL_NOT_FOUND"
    assert result.get("ticket") is None
    assert sent == []  # no OrderSendSafe call was made


@pytest.mark.asyncio
async def test_execute_mt5_emits_skipped_flag_for_absent_symbol(monkeypatch):
    # _execute_mt5 must propagate skipped=True so the Kronos /execute-mt5 route
    # can distinguish a missing instrument from a real broker error.
    import sys
    from pathlib import Path

    REPO_ROOT = Path(__file__).resolve().parents[3]
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if str(REPO_ROOT / "backend") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "backend"))

    from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import _execute_mt5
    from plugins.MT5TradingPlugin.backend.services.mt5_client import MT5Client

    client = MT5Client()

    async def _fake_place(**kw):  # noqa: ANN001
        return {"code": "SYMBOL_NOT_FOUND", "ticket": None,
                "message": "Symbol not found: BTCUSD is not listed on MetaQuotes-Demo"}

    monkeypatch.setattr(client, "place_order", _fake_place)

    # Patch the singleton used inside _execute_mt5.
    import plugins.MT5TradingPlugin.backend.services.mt5_client as _mt5_mod
    monkeypatch.setattr(_mt5_mod, "mt5_client", client)

    class FakeAcct:
        id = 5
        login = "100"
        server = "MetaQuotes-Demo"
        password_encrypted = "pw"
        name = "Demo"

    async def _fake_get_acct(db, account_id):  # noqa: ANN001
        return FakeAcct()

    import plugins.TelegramSignalNewsPlugin.backend.services.sniper_service as _svc
    monkeypatch.setattr(_svc, "_get_live_mt5_account", _fake_get_acct)

    result = await _execute_mt5(
        None, symbol="BTC/USD", direction="long",
        stop_loss=50000.0, take_profit=70000.0, lot_size=0.01,
        account_id=None,
    )
    assert result["success"] is False
    assert result.get("skipped") is True
    assert "not listed" in result.get("error", "")


@pytest.mark.asyncio
async def test_place_order_sends_resolved_broker_symbol(monkeypatch):
    # When the broker lists BTCUSD as BTCUSDm, the order must be sent with the
    # broker's exact spelling, not the bare canonical name.
    client = _client_with_symbols(monkeypatch, ["EURUSDm", "BTCUSDm"])
    sent: dict = {}

    async def _fake_token(login, server, password):  # noqa: ANN001
        return "tok"

    async def _fake_get(path, params=None, retries=1):  # noqa: ANN001
        sent["path"] = path
        sent["params"] = params
        return {"ticket": "12345"}

    monkeypatch.setattr(client, "get_token", _fake_token)
    monkeypatch.setattr(client, "_get", _fake_get)

    result = await client.place_order(
        login="100", server="Exness-Demo", password="pw",
        symbol="BTCUSD", order_type="buy", volume=0.1, price=0,
    )
    assert result.get("ticket") == "12345"
    assert sent["params"]["symbol"] == "BTCUSDm"
    assert sent["params"]["operation"] == "Buy"
