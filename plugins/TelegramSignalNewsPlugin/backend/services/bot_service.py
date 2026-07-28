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
from typing import Any

import httpx
from loguru import logger

BOT_API_BASE = "https://api.telegram.org/bot{token}"
_HTTP_TIMEOUT = 15  # seconds


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
    url = f"{_base(token)}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload or {})
            data = resp.json()
            return data  # {ok: bool, result: ..., description: ...}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotService] {} failed: {}", method, exc)
        return {"ok": False, "description": str(exc)}


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
