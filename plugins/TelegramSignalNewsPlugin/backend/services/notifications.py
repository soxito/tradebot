"""Best-effort Telegram-bot notifications for the trade lifecycle.

Sends execution / TP / SL / trailing-SL updates through the linked Telegram bot
so the user is told, in real time, WHY a trade was placed and how it resolves.

Every function is fully graceful — a missing token, no target chat, or a
Telegram outage can never break the sniper, reconcile loop or scalp bot.
"""
from __future__ import annotations

import os
from typing import Iterable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _under_test() -> bool:
    """Is this a test run rather than the real desk?

    Nothing in this module may reach a real chat from a test. A guard fixture
    tests could forget once produced a run of trade updates on the user's phone
    for a position that did not exist — ticket #99, from a fixture — which is
    indistinguishable from the desk malfunctioning, and worse, entirely
    believable. The environment variable is set by pytest for every test.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


async def _resolve(db: AsyncSession) -> tuple[str, list[str]]:
    """Return (bot_token, [chat_id, ...]) using the same resolution as the bot."""
    from plugins.TelegramSignalNewsPlugin.backend.models import (
        TelegramBotConfig,
        TelegramPluginSettings,
    )
    from plugins.TelegramSignalNewsPlugin.backend.config import telegram_plugin_config

    token = ""
    chat_ids: list[str] = []

    cfg = (await db.execute(select(TelegramBotConfig).limit(1))).scalars().first()
    if cfg and cfg.bot_token_override:
        token = cfg.bot_token_override
    if not token:
        ps = (await db.execute(select(TelegramPluginSettings).limit(1))).scalars().first()
        if ps and getattr(ps, "bot_token", None):
            token = ps.bot_token
    if not token:
        token = telegram_plugin_config.bot_token or ""

    if cfg and cfg.allowed_chat_ids_json:
        chat_ids = [str(c) for c in cfg.allowed_chat_ids_json if c]

    return token, chat_ids


async def notify(text: str, db: AsyncSession | None = None) -> bool:
    """Send *text* to every configured notification chat. Returns True if sent."""
    if _under_test():
        logger.debug("notify suppressed under test: {}", text[:80])
        return False
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.bot_service import send_message
    except Exception:  # noqa: BLE001
        return False

    async def _send(_db: AsyncSession) -> bool:
        token, chat_ids = await _resolve(_db)
        if not token or not chat_ids:
            return False
        sent = False
        for cid in chat_ids:
            try:
                res = await send_message(token, cid, text, parse_mode="HTML")
                sent = sent or bool(res.get("ok"))
            except Exception:  # noqa: BLE001
                pass
        return sent

    try:
        if db is not None:
            return await _send(db)
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            return await _send(_db)
    except Exception as exc:  # noqa: BLE001
        logger.debug("notify failed: {}", exc)
        return False


async def notify_photo(
    photo: bytes, caption: str = "", db: AsyncSession | None = None
) -> bool:
    """Send an image to every configured notification chat.

    The room publishes a drawn plan alongside its verdict; without this the
    picture stayed on the web page and the Telegram reader got the words only.
    """
    if _under_test():
        logger.debug("notify_photo suppressed under test")
        return False
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.bot_service import send_photo
    except Exception:  # noqa: BLE001
        return False

    async def _send(_db: AsyncSession) -> bool:
        token, chat_ids = await _resolve(_db)
        if not token or not chat_ids:
            return False
        sent = False
        for cid in chat_ids:
            try:
                res = await send_photo(token, cid, photo, caption=caption, parse_mode="HTML")
                sent = sent or bool(res.get("ok"))
            except Exception:  # noqa: BLE001
                pass
        return sent

    try:
        if db is not None:
            return await _send(db)
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            return await _send(_db)
    except Exception as exc:  # noqa: BLE001
        logger.debug("notify_photo failed: {}", exc)
        return False


def _fmt_levels(tps: Iterable[float] | None, sl: float | None) -> str:
    parts: list[str] = []
    tp_list = [t for t in (tps or []) if t]
    if tp_list:
        parts.append("🎯 " + " · ".join(f"{t:g}" for t in tp_list[:8]))
    if sl:
        parts.append(f"🛑 {sl:g}")
    return "\n".join(parts)


def format_execution(
    *,
    source: str,
    symbol: str,
    direction: str,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    take_profits: Iterable[float] | None = None,
    venue: str,
    reason: str,
    channel: str | None = None,
) -> str:
    """Build the 'order executed' message.

    source: 'telegram' | 'scalp'. venue: 'MT5 (live)', 'Bitget sandbox', …
    """
    arrow = "🟢 BUY" if direction.lower() in {"long", "buy"} else "🔴 SELL"
    head = f"✅ <b>ORDER EXECUTED</b> — {arrow} <b>{symbol}</b>"
    lines = [head, f"📍 Venue: <b>{venue}</b>"]
    if source == "telegram":
        lines.append(f"📡 Signal: Telegram{f' · <b>{channel}</b>' if channel else ''}")
    else:
        lines.append("🤖 Source: <b>App Scalp Bot</b>")
    if entry:
        lines.append(f"➡️ Entry: <code>{entry:g}</code>")
    lv = _fmt_levels(take_profits or ([take_profit] if take_profit else None), stop_loss)
    if lv:
        lines.append(lv)
    if reason:
        lines.append(f"💡 {reason[:280]}")
    return "\n".join(lines)


def format_tp_hit(
    *, symbol: str, direction: str, tp_index: int, tp_total: int,
    tp_price: float, trailing_sl: float | None, channel: str | None = None,
) -> str:
    all_done = tp_index >= tp_total and tp_total > 0
    head = (
        f"🏁 <b>ALL TAKE-PROFITS HIT</b> — <b>{symbol}</b>"
        if all_done
        else f"🎯 <b>TP {tp_index}/{tp_total} HIT</b> — <b>{symbol}</b>"
    )
    lines = [head, f"Direction: {direction.upper()} · TP @ <code>{tp_price:g}</code>"]
    if trailing_sl:
        lines.append(f"🔒 Trailing SL moved to <code>{trailing_sl:g}</code> (profit locked)")
    if channel:
        lines.append(f"📡 {channel}")
    return "\n".join(lines)


def format_close(
    *, symbol: str, direction: str, kind: str, price: float,
    channel: str | None = None,
) -> str:
    """kind: 'tp' (closed in profit at trailing SL) | 'sl' (stopped out)."""
    if kind == "tp":
        head = f"✅ <b>CLOSED IN PROFIT</b> — <b>{symbol}</b>"
        note = "Price returned to the trailing SL after locking take-profits."
    else:
        head = f"🛑 <b>STOP-LOSS HIT</b> — <b>{symbol}</b>"
        note = "Closed at stop-loss."
    lines = [head, f"Direction: {direction.upper()} · @ <code>{price:g}</code>", note]
    if channel:
        lines.append(f"📡 {channel}")
    return "\n".join(lines)
