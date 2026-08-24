"""Telegram Bot HTTP API service.

Thin async wrapper around the Telegram Bot API.  All network calls go through
``httpx`` (already in requirements).  The token is resolved in priority order:

    1. DB row (TelegramPluginSettings.bot_token)
    2. TelegramPluginConfig.bot_token  (env: TELEGRAM_BOT_TOKEN / TELEGRAM_PLUGIN_BOT_TOKEN)

Only raises on truly unrecoverable errors — every public method returns a
structured dict so callers can inspect ``ok`` without try/except.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

BOT_API_BASE = "https://api.telegram.org/bot{token}"
#: File downloads live on a different path than the method API — /file/bot<token>/
#: — and are plain GETs, so they cannot go through ``_call``.
FILE_API_BASE = "https://api.telegram.org/file/bot{token}"
_HTTP_TIMEOUT = 15  # seconds
_DOWNLOAD_TIMEOUT = 30  # seconds — a photo is bigger than a JSON reply


def _base(token: str) -> str:
    return BOT_API_BASE.format(token=token)


async def _call(
    method: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = _HTTP_TIMEOUT,
) -> dict[str, Any]:
    """POST to the Bot API and return the parsed JSON (always a dict)."""
    if _under_test():
        # Every outbound call funnels through here, so this is the one place a
        # test cannot get past. It exists because it already happened: a guard
        # fixture nobody had written let a suite send a run of trade updates to
        # the user's phone, for a position that only existed in a fixture.
        logger.debug("[BotService] {} suppressed under test", method)
        return {"ok": False, "description": "suppressed: test environment"}

    url = f"{_base(token)}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload or {})
            data = resp.json()
            return data  # {ok: bool, result: ..., description: ...}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotService] {} failed: {}", method, exc)
        return {"ok": False, "description": str(exc)}


def _under_test() -> bool:
    """True when pytest is running this process.

    Telegram is the one side effect in this app that reaches a person directly,
    and a message about a trade is acted on. No test may produce one.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


# ── Public helpers ────────────────────────────────────────────────────────────

async def get_me(token: str) -> dict[str, Any]:
    """Return bot identity information (getMe)."""
    return await _call("getMe", token)


async def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> dict[str, Any]:
    """Send a text message to chat_id."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],  # Telegram limit
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _call("sendMessage", token, payload)


async def send_photo(
    token: str,
    chat_id: str | int,
    photo: bytes,
    caption: str = "",
    parse_mode: str = "HTML",
    filename: str = "chart.png",
) -> dict[str, Any]:
    """Send an image to chat_id (multipart, so no upload round trip is needed)."""
    if _under_test():
        logger.debug("[BotService] sendPhoto suppressed under test")
        return {"ok": False, "description": "suppressed: test environment"}

    url = f"{_base(token)}/sendPhoto"
    data: dict[str, Any] = {"chat_id": str(chat_id), "parse_mode": parse_mode}
    if caption:
        data["caption"] = caption[:1024]  # Telegram caption limit
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                url, data=data, files={"photo": (filename, photo, "image/png")}
            )
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotService] sendPhoto failed: {}", exc)
        return {"ok": False, "description": str(exc)}


async def answer_callback_query(
    token: str,
    callback_query_id: str,
    text: str | None = None,
    show_alert: bool = False,
) -> dict[str, Any]:
    """Acknowledge a button press so Telegram removes the loading spinner."""
    payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text[:200]
    return await _call("answerCallbackQuery", token, payload)


async def get_file(token: str, file_id: str) -> str | None:
    """Resolve a ``file_id`` to the relative path used for downloading.

    Returns None rather than raising — a photo we cannot fetch should degrade to
    a normal "couldn't read that" reply, not a 500 on the webhook.
    """
    resp = await _call("getFile", token, {"file_id": file_id})
    if not resp.get("ok"):
        logger.warning("[BotService] getFile failed: {}", resp.get("description"))
        return None
    return (resp.get("result") or {}).get("file_path")


async def download_file(
    token: str,
    file_path: str,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes | None:
    """Download a file resolved by :func:`get_file`.

    ``max_bytes`` guards the LLM call downstream as much as memory here: base64
    inflates by a third, and a large screenshot can outgrow the model's image
    budget long before it troubles the process.
    """
    url = f"{FILE_API_BASE.format(token=token)}/{file_path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("[BotService] file download HTTP {}", resp.status_code)
                return None
            data = resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotService] file download failed: {}", exc)
        return None
    if len(data) > max_bytes:
        logger.warning(
            "[BotService] file too large: {} bytes > {} limit", len(data), max_bytes
        )
        return None
    return data


async def get_webhook_info(token: str) -> dict[str, Any]:
    """Return current webhook configuration."""
    return await _call("getWebhookInfo", token)


async def set_webhook(
    token: str,
    url: str,
    secret_token: str | None = None,
    allowed_updates: list[str] | None = None,
) -> dict[str, Any]:
    """Register a webhook URL with Telegram."""
    payload: dict[str, Any] = {"url": url, "drop_pending_updates": False}
    if secret_token:
        payload["secret_token"] = secret_token
    if allowed_updates:
        payload["allowed_updates"] = allowed_updates
    else:
        payload["allowed_updates"] = ["message", "callback_query"]
    return await _call("setWebhook", token, payload)


async def delete_webhook(token: str) -> dict[str, Any]:
    """Remove any registered webhook (switches back to long-polling)."""
    return await _call("deleteWebhook", token, {"drop_pending_updates": False})


async def get_updates(token: str, offset: int | None = None, timeout: int = 2) -> dict[str, Any]:
    """Long-poll for new updates (used in polling mode)."""
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
        "limit": 100,
    }
    if offset is not None:
        payload["offset"] = offset
    return await _call("getUpdates", token, payload, timeout=timeout + 5)


async def set_my_commands(
    token: str,
    commands: list[dict[str, str]],
) -> dict[str, Any]:
    """Register the bot's command list with Telegram (shown in the / menu).

    Each command must be ``{"command": "...", "description": "..."}``.
    """
    return await _call("setMyCommands", token, {"commands": commands[:100]})


async def delete_my_commands(token: str) -> dict[str, Any]:
    """Clear all registered commands from the bot menu."""
    return await _call("deleteMyCommands", token, {})


# ── Command list ─────────────────────────────────────────────────────────────

JARVIS_COMMANDS: list[dict[str, str]] = [
    {"command": "start",      "description": "Get started — show help"},
    {"command": "help",       "description": "List all available commands"},
    {"command": "status",     "description": "App status (connections, auto-trading, monitor)"},
    {"command": "positions",  "description": "List all open futures positions"},
    {"command": "portfolio",  "description": "Portfolio summary (total PnL, equity)"},
    {"command": "signals",    "description": "Latest parsed Telegram signals"},
    {"command": "sniper",     "description": "Sniper status, or SMC analysis — /sniper XAUUSD 1h"},
    {"command": "monitor",    "description": "Start or stop the signal monitor"},
    {"command": "close",      "description": "Close a position — usage: /close BTCUSDT"},
    {"command": "tp",         "description": "Set take-profit — usage: /tp 0.025 BTCUSDT"},
    {"command": "sl",         "description": "Set stop-loss — usage: /sl 0.020 BTCUSDT"},
    {"command": "jarvis",     "description": "Free-form Jarvis command — usage: /jarvis <text>"},
    # ── New integrated commands ──────────────────────────────────────────────
    {"command": "forecast",   "description": "Kronos ML forecast + sniper entries — /forecast BTCUSDT [exchange] [1h]"},
    {"command": "order",      "description": "Execute Kronos signal — /order [live] long BTCUSDT 100"},
    {"command": "analyze",    "description": "Deep AI analysis (Kronos+news+position) — /analyze BTCUSDT"},
    {"command": "mt5",        "description": "MT5 accounts/positions/scalp — /mt5 status|positions|scalp|close"},
    {"command": "room",       "description": "Trading room agents — /room BTCUSDT 4h, a question, or a chart image"},
]


async def sync_bot_commands(token: str) -> bool:
    """Push ``JARVIS_COMMANDS`` to Telegram so the / menu matches the code.

    Best-effort: logs and returns False on failure instead of raising, so a
    Telegram outage can never take down the caller (polling loop, webhook
    setup, mode switch).
    """
    if not token:
        return False
    try:
        result = await set_my_commands(token, JARVIS_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotCommands] / menu sync failed: {}", exc)
        return False
    if not result.get("ok"):
        logger.warning("[BotCommands] / menu sync rejected: {}", result.get("description"))
        return False
    logger.info("🤖 Telegram / menu synced ({} commands)", len(JARVIS_COMMANDS))
    return True
