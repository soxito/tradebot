"""Telegram Bot command routing service.

Parses an incoming Telegram Update dict and routes each command to the
appropriate backend handler (Jarvis, trading, signals, monitor).

Entry point: ``parse_and_execute(update, token, allowed_chat_ids, db)``
Returns ``(reply_text, parse_mode)`` — the caller is responsible for
sending the reply via ``bot_service.send_message``.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


# ── Public entry point ────────────────────────────────────────────────────────

async def parse_and_execute(
    update: dict[str, Any],
    token: str,
    allowed_chat_ids: list[str],
    db: AsyncSession,
) -> tuple[str | None, str]:
    """Process a Telegram update dict.

    Returns ``(reply_text, parse_mode)`` where ``reply_text`` is None if no
    reply should be sent.  ``parse_mode`` is always "HTML".
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None, "HTML"

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        return None, "HTML"

    # ── Security gate ─────────────────────────────────────────────────────────
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        logger.warning("[BotCommand] Rejected update from unauthorized chat_id={}", chat_id)
        return None, "HTML"

    # ── Command dispatch ──────────────────────────────────────────────────────
    if text.startswith("/"):
        cmd_raw, _, args = text.partition(" ")
        # Strip any @BotName suffix from the command
        cmd = cmd_raw.split("@")[0].lstrip("/").lower()
        return await _dispatch(cmd, args.strip(), db)

    # Non-command free text → AI fallback
    return await _ai_fallback(text, db)


# ── Command handlers ──────────────────────────────────────────────────────────

async def _dispatch(
    cmd: str,
    args: str,
    db: AsyncSession,
) -> tuple[str, str]:
    handlers = {
        "start":     _handle_start,
        "help":      _handle_help,
        "status":    _handle_status,
        "positions": _handle_positions,
        "portfolio": _handle_portfolio,
        "signals":   _handle_signals,
        "sniper":    _handle_sniper,
        "monitor":   _handle_monitor,
        "close":     _handle_close,
        "tp":        _handle_tp,
        "sl":        _handle_sl,
        "jarvis":    _handle_jarvis,
    }
    handler = handlers.get(cmd, _handle_unknown)
    try:
        return await handler(args, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotCommand] /{} failed: {}", cmd, exc)
        return f"❌ Command failed: {exc}", "HTML"


async def _handle_start(_args: str, _db: AsyncSession) -> tuple[str, str]:
    lines = [
        "🤖 <b>Jarvis TradeBot Online</b>",
        "",
        "I'm connected to your trading app.  Here's what I can do:",
        "",
        "/status — App & exchange health",
        "/positions — Open futures positions",
        "/portfolio — Total PnL & equity",
        "/signals — Latest channel signals",
        "/sniper — Sniper auto-trade status",
        "/monitor start|stop — Signal monitor control",
        "",
        "/close BTCUSDT — Close a position",
        "/tp 0.025 BTCUSDT — Set take-profit",
        "/sl 0.020 BTCUSDT — Set stop-loss",
        "",
        "/jarvis &lt;command&gt; — Free-form Jarvis command",
        "/help — Show this message",
    ]
    return "\n".join(lines), "HTML"


async def _handle_help(args: str, db: AsyncSession) -> tuple[str, str]:
    return await _handle_start(args, db)


async def _handle_status(_args: str, _db: AsyncSession) -> tuple[str, str]:
    """Return basic app health information."""
    try:
        from app.exchanges.manager import exchange_manager
        exchanges = exchange_manager.get_all_exchanges()
        ex_count = len(exchanges)

        from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import signal_monitor
        mon_status = signal_monitor.status()
        mon_running = "🟢 Running" if mon_status.get("running") else "🔴 Stopped"
        last_run = mon_status.get("last_run") or "never"

        lines = [
            "📊 <b>System Status</b>",
            f"Exchanges connected: <b>{ex_count}</b>",
            f"Signal monitor: {mon_running}",
            f"Last monitor run: <code>{last_run}</code>",
        ]
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Status unavailable: {exc}", "HTML"


async def _handle_positions(args: str, _db: AsyncSession) -> tuple[str, str]:
    """List all open positions via Jarvis API."""
    try:
        from app.api.jarvis import get_all_positions
        exchange_filter = args.strip() or None
        positions = await get_all_positions(exchange=exchange_filter)
        if not positions:
            return "📭 No open positions.", "HTML"

        lines = ["📈 <b>Open Positions</b>", ""]
        for p in positions:
            pnl_emoji = "🟢" if p.pnl >= 0 else "🔴"
            lines.append(
                f"{pnl_emoji} <b>{p.symbol}</b> ({p.exchange})\n"
                f"  Side: {p.side.upper()}  Size: {p.size}\n"
                f"  Entry: {p.entry_price:.6g}  Mark: {p.mark_price:.6g}\n"
                f"  PnL: {p.pnl:+.4f} ({p.pnl_pct:+.2f}%)"
            )
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Positions fetch failed: {exc}", "HTML"


async def _handle_portfolio(_args: str, _db: AsyncSession) -> tuple[str, str]:
    """Aggregate portfolio summary."""
    try:
        from app.api.jarvis import get_portfolio
        p = await get_portfolio()
        pnl_emoji = "🟢" if p.total_pnl >= 0 else "🔴"
        lines = [
            "💼 <b>Portfolio Summary</b>",
            f"Open positions: <b>{p.total_positions}</b>",
            f"Total notional: <b>{p.total_notional:,.2f}</b>",
            f"{pnl_emoji} Total PnL: <b>{p.total_pnl:+,.4f}</b>",
        ]
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Portfolio fetch failed: {exc}", "HTML"


async def _handle_signals(_args: str, db: AsyncSession) -> tuple[str, str]:
    """Show latest parsed Telegram signals."""
    try:
        from sqlalchemy import select, desc
        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramParsedSignal, SignalStatus

        result = await db.execute(
            select(TelegramParsedSignal)
            .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
            .order_by(desc(TelegramParsedSignal.created_at))
            .limit(5)
        )
        signals = result.scalars().all()

        if not signals:
            return "📭 No active signals.", "HTML"

        lines = [f"📡 <b>Active Signals</b> (latest {len(signals)})", ""]
        for s in signals:
            tps = ", ".join(str(t) for t in (s.take_profits_json or [])[:3])
            lines.append(
                f"<b>{s.symbol}</b> {s.direction.upper()} "
                f"@ {s.entry}\n"
                f"  SL: {s.stop_loss}  TP: {tps or '—'}"
            )
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Signals fetch failed: {exc}", "HTML"


async def _handle_sniper(_args: str, db: AsyncSession) -> tuple[str, str]:
    """Sniper auto-trade status."""
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import (
            get_or_create_settings as get_sniper_settings,
        )
        from sqlalchemy import select, desc
        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramSniperTrade, SniperTradeStatus

        cfg = await get_sniper_settings(db)
        enabled = getattr(cfg, "enabled", False)
        status_icon = "🟢" if enabled else "🔴"

        result = await db.execute(
            select(TelegramSniperTrade)
            .where(TelegramSniperTrade.status == SniperTradeStatus.PLACED)
            .order_by(desc(TelegramSniperTrade.created_at))
            .limit(5)
        )
        trades = result.scalars().all()

        lines = [
            f"🎯 <b>Sniper</b>: {status_icon} {'Active' if enabled else 'Disabled'}",
            f"Live trades: <b>{len(trades)}</b>",
        ]
        for t in trades:
            lines.append(f"  {t.symbol} {t.direction or ''} @ {t.entry_price or '—'}")
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Sniper status failed: {exc}", "HTML"


async def _handle_monitor(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Start or stop the signal monitor."""
    from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import signal_monitor

    sub = args.lower().strip()
    if sub == "start":
        from app.core.database import AsyncSessionLocal
        signal_monitor.ensure_started(AsyncSessionLocal)
        return "▶️ Signal monitor started.", "HTML"
    elif sub == "stop":
        signal_monitor.stop()
        return "⏹ Signal monitor stopped.", "HTML"
    else:
        st = signal_monitor.status()
        running = "🟢 Running" if st.get("running") else "🔴 Stopped"
        return (
            f"📡 Monitor: {running}\n"
            f"Use <code>/monitor start</code> or <code>/monitor stop</code>."
        ), "HTML"


async def _handle_close(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Close a position by symbol."""
    if not args:
        return "❓ Usage: /close BTCUSDT", "HTML"
    symbol = args.upper().split()[0]
    return await _jarvis_command(f"close {symbol}")


async def _handle_tp(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Set take-profit: /tp 0.025 BTCUSDT."""
    parts = args.split()
    if len(parts) < 2:
        return "❓ Usage: /tp &lt;price&gt; &lt;SYMBOL&gt;", "HTML"
    price, symbol = parts[0], parts[1].upper()
    return await _jarvis_command(f"take profit at {price} on {symbol}")


async def _handle_sl(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Set stop-loss: /sl 0.020 BTCUSDT."""
    parts = args.split()
    if len(parts) < 2:
        return "❓ Usage: /sl &lt;price&gt; &lt;SYMBOL&gt;", "HTML"
    price, symbol = parts[0], parts[1].upper()
    return await _jarvis_command(f"set stop loss at {price} on {symbol}")


async def _handle_jarvis(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Forward free-form text to the Jarvis command engine."""
    if not args:
        return "❓ Usage: /jarvis &lt;command&gt;\nExample: /jarvis show positions", "HTML"
    return await _jarvis_command(args)


async def _handle_unknown(_args: str, _db: AsyncSession) -> tuple[str, str]:
    return "❓ Unknown command.  Type /help for a list of commands.", "HTML"


# ── Jarvis bridge ─────────────────────────────────────────────────────────────

async def _jarvis_command(cmd: str) -> tuple[str, str]:
    """Call the Jarvis execute_command handler and return (reply_text, parse_mode)."""
    try:
        from app.api.jarvis import execute_command, CommandRequest
        result = await execute_command(CommandRequest(command=cmd))
        emoji = "✅" if result.ok else "❌"
        return f"{emoji} {result.speech or result.detail}", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Jarvis error: {exc}", "HTML"


# ── AI fallback ───────────────────────────────────────────────────────────────

async def _ai_fallback(text: str, db: AsyncSession) -> tuple[str | None, str]:
    """Route unrecognised text to the AiMarketAnalyst if available."""
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
        messages = [{"role": "user", "content": text}]
        result = await db_chat(db, messages, json_mode=False)
        reply = result.get("content") or result.get("text") or ""
        if reply:
            return reply[:3000], "HTML"
    except Exception:  # noqa: BLE001
        pass
    return None, "HTML"
