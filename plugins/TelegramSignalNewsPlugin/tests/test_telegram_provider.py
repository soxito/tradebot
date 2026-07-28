from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from plugins.TelegramSignalNewsPlugin.backend.config import TelegramPluginConfig
from plugins.TelegramSignalNewsPlugin.backend.services.telegram_provider import (
    TelegramChannelInfo,
    TelegramMcpProvider,
    TelethonProvider,
    TelegramProviderRegistry,
    _parse_core_methods_html,
)


def test_registry_auto_candidates_include_mcp_when_only_mcp_is_configured():
    cfg = TelegramPluginConfig(
        api_id=0,
        api_hash="",
        bot_token="",
        mcp_chat_id="123456",
    )

    registry = TelegramProviderRegistry(cfg)
    names = [provider.name for provider in registry._iter_candidates("auto")]

    assert names == ["telegram_mcp"]


@pytest.mark.asyncio
async def test_mcp_provider_fetch_recent_messages_filters_and_orders(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(
        mcp_chat_id="123456",
        mcp_server_url="https://example.com",
    )
    provider = TelegramMcpProvider(cfg)

    payload = {
        "messages": [
            {
                "id": "2",
                "text": "BUY BTCUSDT",
                "date": 1700000002,
                "chat": {"username": "alpha"},
            },
            {
                "id": "1",
                "text": "OLD",
                "date": 1700000001,
                "chat": {"username": "alpha"},
            },
            {
                "id": "3",
                "text": "",
                "date": 1700000003,
                "chat": {"username": "alpha"},
            },
            {
                "id": "4",
                "text": "OTHER CHANNEL",
                "date": 1700000004,
                "chat": {"username": "beta"},
            },
        ]
    }
    monkeypatch.setattr(provider, "_request_messages", AsyncMock(return_value=payload))

    rows = await provider.fetch_recent_messages("@alpha", limit=10, min_message_id="1")

    assert [row.message_id for row in rows] == ["2"]
    assert rows[0].text == "BUY BTCUSDT"


@pytest.mark.asyncio
async def test_mcp_provider_resolve_channel_probes_server(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(mcp_chat_id="123456")
    provider = TelegramMcpProvider(cfg)

    mock_request = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(provider, "_request_messages", mock_request)

    info = await provider.resolve_channel("@alpha")

    mock_request.assert_awaited_once_with(limit=1)
    assert info.handle == "@alpha"
    assert info.channel_id == "123456"


@pytest.mark.asyncio
async def test_mcp_provider_list_subscribed_channels_extracts_unique_chat_entries(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = TelegramPluginConfig(mcp_chat_id="123456")
    provider = TelegramMcpProvider(cfg)

    payload = {
        "messages": [
            {
                "id": "1",
                "text": "BUY BTCUSDT",
                "chat": {"username": "alpha", "title": "Alpha", "id": 100},
            },
            {
                "id": "2",
                "text": "SELL ETHUSDT",
                "chat": {"username": "alpha", "title": "Alpha", "id": 100},
            },
            {
                "id": "3",
                "text": "Macro update",
                "chat": {"title": "Beta Desk", "id": -200},
            },
        ]
    }
    monkeypatch.setattr(provider, "_request_messages", AsyncMock(return_value=payload))

    rows = await provider.list_subscribed_channels(limit=10)

    assert [(row.title, row.handle, row.channel_id) for row in rows] == [
        ("Alpha", "@alpha", "100"),
        ("Beta Desk", "-200", "-200"),
    ]


@pytest.mark.asyncio
async def test_registry_list_subscribed_channels_uses_available_provider(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(
        api_id=0,
        api_hash="",
        bot_token="",
        mcp_chat_id="123456",
    )
    registry = TelegramProviderRegistry(cfg)
    mcp_provider = next(provider for provider in registry._providers if provider.name == "telegram_mcp")
    monkeypatch.setattr(
        mcp_provider,
        "list_subscribed_channels",
        AsyncMock(return_value=[TelegramChannelInfo(title="Alpha", handle="@alpha", channel_id="100")]),
    )

    rows, provider_name = await registry.list_subscribed_channels(limit=5, provider_hint="auto")

    assert provider_name == "telegram_mcp"
    assert [(row.title, row.handle, row.channel_id) for row in rows] == [
        ("Alpha", "@alpha", "100")
    ]


@pytest.mark.asyncio
async def test_registry_list_subscribed_channels_prefers_non_empty_provider_in_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = TelegramPluginConfig(
        api_id=0,
        api_hash="",
        bot_token="token",
        mcp_chat_id="123456",
    )
    registry = TelegramProviderRegistry(cfg)
    bot_provider = next(provider for provider in registry._providers if provider.name == "bot_api")
    mcp_provider = next(provider for provider in registry._providers if provider.name == "telegram_mcp")

    monkeypatch.setattr(bot_provider, "list_subscribed_channels", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        mcp_provider,
        "list_subscribed_channels",
        AsyncMock(return_value=[TelegramChannelInfo(title="BullFrogCrypto", handle="@BullFrogCrypto", channel_id="999")]),
    )

    rows, provider_name = await registry.list_subscribed_channels(limit=5, provider_hint="auto")

    assert provider_name == "telegram_mcp"
    assert [(row.title, row.handle, row.channel_id) for row in rows] == [
        ("BullFrogCrypto", "@BullFrogCrypto", "999")
    ]


def test_telethon_provider_is_unavailable_when_telethon_dependency_missing(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(api_id=1, api_hash="hash")
    provider = TelethonProvider(cfg)

    def raise_missing_telethon():
        raise RuntimeError("Telethon is not installed. Install it with 'pip install telethon'.")

    monkeypatch.setattr(provider, "_get_client_cls", raise_missing_telethon)

    assert provider.is_available() is False


@pytest.mark.asyncio
async def test_telethon_list_subscribed_channels_scans_all_dialogs(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(api_id=1, api_hash="hash")
    provider = TelethonProvider(cfg)

    class FakeEntity:
        def __init__(self, username: str, entity_id: int, title: str):
            self.username = username
            self.id = entity_id
            self.title = title

    class FakeDialog:
        def __init__(self, entity: FakeEntity):
            self.entity = entity
            self.name = entity.title

    class FakeClient:
        last_limit: int | None = -1
        connected = False
        disconnected = False

        def __init__(self, *_args, **_kwargs):
            pass

        # _connected_client() drives the client explicitly so the session lock
        # can serialise Telethon's SQLite access — not via `async with client`.
        async def connect(self):
            FakeClient.connected = True

        async def disconnect(self):
            FakeClient.disconnected = True

        def iter_dialogs(self, limit=None):
            FakeClient.last_limit = limit

            async def generator():
                for i in range(1, 221):
                    label = f"zzz-{i:03d}"
                    yield FakeDialog(FakeEntity(username=label, entity_id=i, title=label))

                yield FakeDialog(
                    FakeEntity(username="BullFrogCrypto", entity_id=999999, title="BullFrogCrypto")
                )

            return generator()

    monkeypatch.setattr(provider, "_get_client_cls", lambda: FakeClient)

    rows = await provider.list_subscribed_channels(limit=200)

    assert FakeClient.last_limit is None
    assert len(rows) == 200
    assert any(row.handle.lower() == "@bullfrogcrypto" for row in rows)
    # The session must be opened and always released back to the lock.
    assert FakeClient.connected and FakeClient.disconnected


def test_parse_core_methods_html_extracts_unique_names():
    html = """
    <a href=\"/method/messages.getHistory\">messages.getHistory</a>
    <a href=\"https://core.telegram.org/method/help.getConfig\">help.getConfig</a>
    <a href=\"/method/messages.getHistory\">duplicate</a>
    <a href=\"/constructor/user\">constructor</a>
    """

    names = _parse_core_methods_html(html)

    assert names == ["help.getConfig", "messages.getHistory"]


@pytest.mark.asyncio
async def test_registry_test_core_methods_reports_unsupported_when_no_telethon(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(
        api_id=0,
        api_hash="",
        bot_token="",
        mcp_chat_id="123456",
    )
    registry = TelegramProviderRegistry(cfg)
    monkeypatch.setattr(
        registry,
        "get_core_methods",
        AsyncMock(return_value=["help.getConfig", "messages.getHistory"]),
    )

    result = await registry.test_core_methods(provider_hint="auto", refresh=False)

    assert result["provider"] == "telegram_mcp"
    assert result["summary"]["total_methods"] == 2
    assert result["summary"]["tested_methods"] == 2
    assert result["summary"]["passed"] == 0
    assert result["summary"]["unsupported"] == 2


@pytest.mark.asyncio
async def test_registry_build_core_methods_catalog_marks_supported(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(
        api_id=1,
        api_hash="hash",
    )
    registry = TelegramProviderRegistry(cfg)
    monkeypatch.setattr(
        registry,
        "get_core_methods",
        AsyncMock(return_value=["help.getConfig"]),
    )

    telethon_provider = registry._get_telethon_provider()
    assert telethon_provider is not None
    monkeypatch.setattr(
        telethon_provider,
        "inspect_method_binding",
        lambda method_name: (True, "telethon.tl.functions.help.GetConfigRequest", "Resolved binding"),
    )

    catalog = await registry.build_core_methods_catalog(refresh=False)

    assert catalog["total_methods"] == 1
    assert catalog["methods"][0]["name"] == "help.getConfig"
    assert catalog["methods"][0]["provider_supported"] == ["telethon"]


@pytest.mark.asyncio
async def test_registry_test_core_methods_invoke_readonly_applies_allowlist_guardrails(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = TelegramPluginConfig(
        api_id=1,
        api_hash="hash",
    )
    registry = TelegramProviderRegistry(cfg)
    monkeypatch.setattr(
        registry,
        "get_core_methods",
        AsyncMock(return_value=["help.getConfig", "messages.getHistory", "help.getNearestDc"]),
    )

    telethon_provider = registry._get_telethon_provider()
    assert telethon_provider is not None

    def fake_details(method_name: str):
        if method_name == "help.getConfig":
            return True, "telethon.tl.functions.help.GetConfigRequest", "Resolved binding", 0
        if method_name == "help.getNearestDc":
            return True, "telethon.tl.functions.help.GetNearestDcRequest", "Resolved binding", 1
        return True, "telethon.tl.functions.messages.GetHistoryRequest", "Resolved binding", 0

    invoke_mock = AsyncMock(return_value=(True, "Invoked safely via telethon.tl.functions.help.GetConfigRequest"))
    monkeypatch.setattr(telethon_provider, "inspect_method_binding_details", fake_details)
    monkeypatch.setattr(telethon_provider, "invoke_readonly_method", invoke_mock)

    result = await registry.test_core_methods(provider_hint="telethon", refresh=False, mode="invoke_readonly")

    assert result["provider"] == "telethon"
    assert result["mode"] == "invoke_readonly"
    assert "help.getConfig" in result["readonly_allowlist"]
    assert result["summary"]["total_methods"] == 3
    assert result["summary"]["tested_methods"] == 3
    assert result["summary"]["passed"] == 1
    assert result["summary"]["unsupported"] == 2
    assert result["summary"]["failed"] == 0

    by_method = {row["method"]: row for row in result["results"]}
    assert by_method["help.getConfig"]["status"] == "supported"
    assert by_method["messages.getHistory"]["status"] == "unsupported"
    assert by_method["help.getNearestDc"]["status"] == "unsupported"
    invoke_mock.assert_awaited_once_with("help.getConfig")


@pytest.mark.asyncio
async def test_registry_test_core_methods_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch):
    cfg = TelegramPluginConfig(mcp_chat_id="123456")
    registry = TelegramProviderRegistry(cfg)
    monkeypatch.setattr(registry, "get_core_methods", AsyncMock(return_value=[]))

    with pytest.raises(ValueError, match="Unsupported test mode"):
        await registry.test_core_methods(mode="unknown")  # type: ignore[arg-type]
