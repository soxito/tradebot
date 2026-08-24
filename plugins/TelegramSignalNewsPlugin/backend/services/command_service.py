"""Telegram Bot command routing service.

Parses an incoming Telegram Update dict and routes each command to the
appropriate backend handler (Jarvis, trading, signals, monitor).

Entry point: ``parse_and_execute(update, token, allowed_chat_ids, db)``
Returns ``(reply_text, parse_mode)`` — the caller is responsible for
sending the reply via ``bot_service.send_message``.
"""
from __future__ import annotations

import asyncio
import math
import re
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.text_format import format_for_telegram

# ── Per-chat conversation history ─────────────────────────────────────────────
# Keyed by Telegram chat_id string; stores last _MAX_HISTORY * 2 messages so
# Jarvis can follow the thread across turns.
_CHAT_HISTORY: dict[str, list[dict[str, str]]] = {}
_MAX_HISTORY = 20  # most-recent user+assistant pairs to keep

# ── Pending command offer ─────────────────────────────────────────────────────
# When a chat reply offers a command ("I can run /forecast BTCUSDT"), the offer
# is parked here so the user's "yes" actually runs it instead of being answered
# with another paragraph. Keyed by chat_id → (command, args).
_PENDING_OFFER: dict[str, tuple[str, str]] = {}

#: Commands a bare "yes" is allowed to trigger. Read-only by design — an
#: affirmative must never be able to place, close or amend a trade, because the
#: text that offered it was written by the model, not by the user.
_AUTORUN_SAFE = frozenset({
    "forecast", "analyze", "signals", "sniper", "status", "positions",
    "portfolio", "mt5",
})

#: Non-symbol words that are legitimately arguments to those commands, so an
#: offered "/mt5 positions" keeps its subcommand while "/forecast BTCUSDT for
#: you" drops the prose.
_OFFER_SUBCOMMANDS = frozenset({
    "status", "positions", "orders", "scalp", "smc", "bitget", "binance",
})


# ── Public entry point ────────────────────────────────────────────────────────

async def parse_and_execute(
    update: dict[str, Any],
    token: str,
    allowed_chat_ids: list[str],
    db: AsyncSession,
) -> tuple[str | None, str, dict | None]:
    """Process a Telegram update dict.

    Returns ``(reply_text, parse_mode, reply_markup)`` where ``reply_text`` is
    None if no reply should be sent.  ``reply_markup`` carries an optional
    Telegram inline-keyboard dict.
    """
    # ── Callback query (inline-keyboard button press) ─────────────────────────
    cq = update.get("callback_query")
    if cq:
        from_chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
        if allowed_chat_ids and from_chat_id not in allowed_chat_ids:
            return None, "HTML", None
        data = (cq.get("data") or "").strip()
        if data:
            return await _dispatch_callback(data, db)
        return None, "HTML", None

    message = update.get("message") or update.get("edited_message")
    if not message:
        return None, "HTML", None

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    image = _extract_image(message)
    if not text and not image:
        return None, "HTML", None

    # ── Security gate ─────────────────────────────────────────────────────────
    # Ahead of every branch below, images included: an unauthorized chat must
    # not be able to spend a vision call any more than it can place a trade.
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        logger.warning("[BotCommand] Rejected update from unauthorized chat_id={}", chat_id)
        return None, "HTML", None

    # ── Photo / image document ────────────────────────────────────────────────
    if image:
        file_id, mime = image
        caption = (message.get("caption") or "").strip()
        return await _handle_photo(file_id, mime, caption, token, chat_id, db)

    # ── Command dispatch ──────────────────────────────────────────────────────
    if text.startswith("/"):
        _PENDING_OFFER.pop(chat_id, None)
        cmd_raw, _, args = text.partition(" ")
        # Strip any @BotName suffix from the command
        cmd = cmd_raw.split("@")[0].lstrip("/").lower()
        # /room is handled here rather than in _dispatch because it needs the
        # token and chat_id to deliver its own result later — the room runs for
        # minutes, long after this reply has been sent.
        if cmd == "room":
            sub = args.strip().split(maxsplit=1)
            head = (sub[0] or "").lower() if sub else ""
            if head in {"agents", "seats"}:
                return await _room_seats_reply()
            if head == "status":
                return await _room_status_reply(chat_id)
            if head == "focus":
                return await _room_focus_reply(
                    sub[1].strip() if len(sub) > 1 else "", chat_id
                )
            return _start_room(args.strip(), token, chat_id)
        return await _dispatch(cmd, args.strip(), db)

    # ── "Yes" to a command the last reply offered ─────────────────────────────
    # Jarvis used to end on "Would you like me to execute that?" and then had
    # nowhere to put the answer: the confirmation went back through free-text
    # chat and produced another paragraph. Run what was offered instead.
    pending = _PENDING_OFFER.get(chat_id)
    if pending and _is_affirmative(text):
        _PENDING_OFFER.pop(chat_id, None)
        cmd, cmd_args = pending
        _record_history(chat_id, "user", text)
        reply, mode, markup = await _dispatch(cmd, cmd_args, db)
        if reply:
            _record_history(chat_id, "assistant", reply)
        return reply, mode, markup

    # Non-command free text → full Jarvis AI chat
    fallback = await _ai_fallback(text, db, chat_id=chat_id)
    # _ai_fallback now returns 3-tuple; keep keyboard as None for chat replies
    return fallback[0], fallback[1], fallback[2]


# ── Command handlers ──────────────────────────────────────────────────────────

async def _dispatch(
    cmd: str,
    args: str,
    db: AsyncSession,
) -> tuple[str, str, dict | None]:
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
        # ── Integrated commands ──────────────────────────────
        "forecast":  _handle_forecast,
        "order":     _handle_order,
        "analyze":   _handle_analyze,
        "mt5":       _handle_mt5,
    }
    handler = handlers.get(cmd, _handle_unknown)
    try:
        result = await handler(args, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BotCommand] /{} failed: {}", cmd, exc)
        return f"❌ Command failed: {exc}", "HTML", None
    # Normalise to 3-tuple — most handlers return 2-tuple, forecast returns 3
    if len(result) == 2:
        return result[0], result[1], None
    return result[0], result[1], result[2]


async def _dispatch_callback(
    data: str,
    db: AsyncSession,
) -> tuple[str, str, dict | None]:
    """Route a Telegram inline-keyboard ``callback_data`` to the right handler.

    Format conventions:
      cq_order:{side}:{kind}:{symbol}:{margin}[:{live}]
      cq_analyze:{symbol}
    """
    if data.startswith("cq_order:"):
        # cq_order:short:market:UNI/USDT:100  OR  cq_order:short:limit:...:100:live
        parts = data.split(":")
        if len(parts) < 5:
            return "❓ Invalid order action — use /order directly.", "HTML", None
        side = parts[1]        # long | short
        kind = parts[2]        # market | limit
        sym = parts[3]         # UNI/USDT  (may contain / but no :)
        margin = parts[4]      # 100
        is_live = len(parts) > 5 and parts[5] == "live"
        live_flag = "live " if is_live else ""
        kind_flag = "limit " if kind == "limit" else ""
        order_args = f"{live_flag}{kind_flag}{side} {sym} {margin}"
        result = await _handle_order(order_args, db)
        # Pad to 3-tuple; no keyboard on order confirmation
        return result[0], result[1], None

    if data.startswith("cq_analyze:"):
        sym = data[len("cq_analyze:"):]
        result = await _handle_analyze(sym, db)
        return result[0], result[1], None

    if data.startswith("cq_custom_order:"):
        # cq_custom_order:{side}:{kind}:{symbol}
        parts = data.split(":")
        if len(parts) >= 4:
            side = parts[1]        # long | short
            kind = parts[2]        # market | limit
            sym  = parts[3]        # e.g. UNI/USDT
            kind_flag = "limit " if kind == "limit" else ""
            cmd_paper = f"/order {kind_flag}{side} {sym} "
            cmd_live  = f"/order live {kind_flag}{side} {sym} "
            msg = (
                f"✏️ <b>Custom amount order — {sym}</b>\n"
                f"Copy and send one of these commands, replacing the amount:\n\n"
                f"📋 <b>Paper:</b>\n"
                f"<code>{cmd_paper}50</code>\n\n"
                f"🔴 <b>LIVE Bitget:</b>\n"
                f"<code>{cmd_live}50</code>\n\n"
                f"<i>Replace <code>50</code> with any amount in USD (min $1).</i>"
            )
            return msg, "HTML", None
        return "❓ Invalid custom order action.", "HTML", None

    return "❓ Unknown button action.", "HTML", None


async def _handle_start(_args: str, _db: AsyncSession) -> tuple[str, str]:
    lines = [
        "🤖 <b>Jarvis TradeBot Online</b>",
        "",
        "I'm connected to your trading app.  Here's what I can do:",
        "",
        "/status — App &amp; exchange health",
        "/positions — Open futures positions",
        "/portfolio — Total PnL &amp; equity",
        "/signals — Latest channel signals",
        "/sniper — Sniper auto-trade status",
        "/sniper XAUUSD 1h — MT5 SMC sniper analysis (1m|5m|15m|30m|1h|4h|1d)",
        "/monitor start|stop — Signal monitor control",
        "",
        "🏛 <b>Trading Room</b> — the full agent desk, results sent when done",
        "/room BTCUSDT 4h — agents review the pair, then a plan chart with",
        "<i>order blocks, entry, SL and TP plus the projected move</i>",
        "/room why is gold selling off? — put a question to the agents",
        "/room agents — who's on the board and what each seat last decided",
        "/room status — is a meeting running, which pairs are pinned",
        "/room focus BTCUSDT,XAUUSD — pin pairs (or <code>off</code> to unpin)",
        "<i>Send a chart image with caption /room and they'll read it, mark it up</i>",
        "<i>and review whatever pair it shows. A plain image gets a quick read.</i>",
        "",
        "🔮 <b>Kronos Forecast &amp; Order Execution</b>",
        "/forecast BTCUSDT [exchange] [1h] — Kronos ML forecast + sniper entries",
        "/order long BTCUSDT 100 — Paper trade from Kronos signal ($100 margin)",
        "/order live long BTCUSDT 100 — LIVE Bitget futures order",
        "/analyze BTCUSDT — Deep AI analysis (Kronos + news + position)",
        "<i>Both work on every pair — crypto and MT5 (FX majors/crosses/exotics,</i>",
        "<i>metals, indices, energy, softs). e.g. /forecast XAUUSD 4h, /analyze GBPJPY.</i>",
        "<i>MT5 pairs are analysis-only — place those with your broker.</i>",
        "",
        "🖥 <b>MT5 Trading</b>",
        "/mt5 status — List MT5 accounts",
        "/mt5 positions 5 — Open MT5 positions (account_id=5)",
        "/mt5 scalp start 5 EURUSD — Start ScalpBot",
        "/mt5 scalp stop 5 — Stop ScalpBot",
        "/mt5 scalp status 5 — ScalpBot live status",
        "/mt5 close 12345 5 — Close MT5 position by ticket",
        "",
        "/close BTCUSDT — Close a Bitget futures position",
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


async def _handle_sniper(args: str, db: AsyncSession) -> tuple[str, str]:
    """Sniper auto-trade status, or MT5 SMC analysis when a symbol is supplied.

      /sniper                — rug-pull sniper auto-trade status (legacy)
      /sniper XAUUSD 1h      — MT5 SMC sniper analysis (same as the /mt5-live page)
    """
    if args.strip():
        return await _sniper_smc(args, db)
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


# ── MT5 SMC sniper analysis (/sniper XAUUSD 1h) ───────────────────────────────

# User-friendly timeframe → MT5 code.  Both "1h" and "h1" spellings accepted.
_SNIPER_TIMEFRAMES = {
    "1m": "M1", "m1": "M1",
    "5m": "M5", "m5": "M5",
    "15m": "M15", "m15": "M15",
    "30m": "M30", "m30": "M30",
    "1h": "H1", "h1": "H1",
    "4h": "H4", "h4": "H4",
    "1d": "D1", "d1": "D1",
}
_SNIPER_TF_HELP = "1m, 5m, 15m, 30m, 1h, 4h, 1d"

# Telegram hard-caps a message at 4096 chars; stay clear of the edge.
_SNIPER_MAX_LEN = 3900


def _sniper_usage(problem: str = "") -> str:
    head = f"❓ {problem}\n" if problem else "❓ "
    return (
        f"{head}Usage: <code>/sniper &lt;SYMBOL&gt; [TIMEFRAME]</code>\n"
        "Example: <code>/sniper XAUUSD 1h</code>\n"
        f"Timeframes: <code>{_SNIPER_TF_HELP}</code>\n"
        "<code>/sniper</code> with no arguments shows auto-trade status."
    )


def _norm_mt5_sym(symbol: str) -> str:
    """MT5 symbols are plain upper-case tickers: xauusd → XAUUSD."""
    return symbol.strip().upper()


def _parse_sniper_args(args: str) -> tuple[str | None, str | None, str | None]:
    """Parse ``<SYMBOL> [TIMEFRAME]`` → ``(symbol, timeframe, error_text)``."""
    tokens = args.split()
    if not tokens:
        return None, None, _sniper_usage()
    symbol = _norm_mt5_sym(tokens[0])
    if not symbol:
        return None, None, _sniper_usage()
    if len(tokens) < 2:
        return symbol, "H1", None
    timeframe = _SNIPER_TIMEFRAMES.get(tokens[1].strip().lower())
    if not timeframe:
        return None, None, _sniper_usage(f"Unknown timeframe <code>{_esc(tokens[1])}</code>.")
    return symbol, timeframe, None


async def _sniper_smc(args: str, db: AsyncSession) -> tuple[str, str]:
    """Run the SMC sniper analysis the /mt5-live page renders, in-process."""
    symbol, timeframe, err = _parse_sniper_args(args)
    if err:
        return err, "HTML"

    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5Account

        account = (await db.execute(_sel(MT5Account).limit(1))).scalars().first()
        if not account:
            return (
                "❌ No MT5 account connected — connect one before running "
                "<code>/sniper &lt;SYMBOL&gt;</code>."
            ), "HTML"

        # Same code path the /mt5-live page hits over HTTP.  Every query param is
        # passed explicitly because the FastAPI Query() defaults are not usable
        # when the endpoint is awaited directly.
        from plugins.MT5TradingPlugin.backend.router import smc_analyze

        resp = await smc_analyze(
            account_id=account.id,
            symbol=symbol,
            timeframe=timeframe,
            count=400,
            min_rr=1.5,
            max_rr=3.0,
            sl_buffer_atr=1.0,
            min_confidence=0.6,
            use_ai=True,
        )
    except Exception as exc:  # noqa: BLE001
        return f"❌ Sniper analysis failed: {exc}", "HTML"

    return _fmt_sniper_analysis(resp), "HTML"


def _esc(text: Any) -> str:
    """Minimal HTML escape for model- or broker-supplied text."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_sniper_analysis(resp: Any) -> str:
    """Render an ``MT5SmcAnalyzeResponse`` as an HTML Telegram message."""
    def g(key: str, default: Any = None) -> Any:
        return getattr(resp, key, default)

    symbol = _esc(g("symbol", "?"))
    timeframe = _esc(g("timeframe", "?"))
    if g("error"):
        return f"❌ <b>{symbol}</b> {timeframe}: {_esc(g('error'))}"

    bias = str(g("bias") or "neutral").lower()
    bias_icon = {"bullish": "🟢", "bearish": "🔴"}.get(bias, "⚪")
    lines = [f"🎯 <b>SMC Sniper — {symbol} {timeframe}</b>", ""]

    last_price = g("last_price")
    lines.append(f"{bias_icon} Bias: <b>{bias.upper()}</b>  Momentum: {_esc(g('momentum') or '—')}")
    if last_price is not None:
        atr, atr_pct = g("atr"), g("atr_pct")
        atr_txt = f"  ATR: <code>{_fmt_p(atr)}</code> ({atr_pct}%)" if atr else ""
        lines.append(f"Price: <code>{_fmt_p(last_price)}</code>{atr_txt}")
    rsi, vol_z = g("rsi"), g("volume_z")
    if rsi is not None or vol_z is not None:
        lines.append(f"RSI: {rsi if rsi is not None else '—'}  Vol-Z: {vol_z if vol_z is not None else '—'}")

    # Premium / discount relative to the dealing-range equilibrium.
    rng, eq = g("range") or {}, g("equilibrium")
    if rng:
        lines.append(f"Range: <code>{_fmt_p(rng.get('low', 0))}</code> – <code>{_fmt_p(rng.get('high', 0))}</code>")
    if eq is not None:
        zone = "—"
        if last_price is not None:
            zone = "PREMIUM 🔺" if last_price > eq else ("DISCOUNT 🔻" if last_price < eq else "EQUILIBRIUM")
        lines.append(f"Equilibrium: <code>{_fmt_p(eq)}</code>  → <b>{zone}</b>")

    events = g("structure_events") or []
    if events:
        arrow = {"bullish": "↑", "bearish": "↓"}
        recent = ", ".join(
            f"{_esc(e.get('type', '?'))}{arrow.get(e.get('direction'), '')}" for e in events[-3:]
        )
        lines.append(f"Structure: {recent}")

    zones = g("zones") or []
    obs = sum(1 for z in zones if "ob" in str(getattr(z, "kind", "")).lower())
    fvgs = sum(1 for z in zones if "fvg" in str(getattr(z, "kind", "")).lower())
    if zones:
        lines.append(f"Zones: <b>{obs}</b> order blocks · <b>{fvgs}</b> FVGs")

    liq = g("liquidity") or {}
    bsl, ssl = (liq.get("buyside") or [])[-3:], (liq.get("sellside") or [])[-3:]
    if bsl or ssl:
        lines.append(
            f"Liquidity: BSL {', '.join(_fmt_p(p) for p in bsl) or '—'}"
            f" | SSL {', '.join(_fmt_p(p) for p in ssl) or '—'}"
        )

    # ── Ranked limit setups (top 3) ───────────────────────────────────────────
    signals = g("signals") or []
    lines.append("")
    if not signals:
        lines.append("📭 <b>No qualifying setups</b> at the current confidence floor.")
    else:
        lines.append(f"📌 <b>Top Setups</b> ({min(3, len(signals))} of {len(signals)})")
        for i, s in enumerate(signals[:3], start=1):
            def sg(key: str, default: Any = None) -> Any:
                return getattr(s, key, default)
            side = str(sg("side", "")).lower()
            side_icon = "🟢" if side == "buy" else "🔴"
            conf = float(sg("confidence", 0.0) or 0.0)
            lines.append(
                f"{i}. {side_icon} <b>{_esc(str(sg('order_type', side)).upper())}</b> "
                f"@ <code>{_fmt_p(sg('entry', 0.0))}</code>"
            )
            lines.append(
                f"   SL <code>{_fmt_p(sg('stop_loss', 0.0))}</code>  "
                f"TP <code>{_fmt_p(sg('take_profit', 0.0))}</code>  "
                f"R:R <b>{sg('rr', 0.0)}</b>  Conf <b>{conf * 100:.0f}%</b>"
            )
            aligned = sg("kronos_aligned")
            tag = "✅ aligned" if aligned else ("⚠️ opposed" if aligned is False else "· n/a")
            fusion = sg("fusion_score")
            lines.append(
                f"   {_esc(sg('zone_kind', '—'))} · fusion "
                f"{fusion if fusion is not None else '—'} {tag}"
            )
            # The factor attribution. This message used to print no reason at
            # all — only the AI block's prose — so every deterministic factor
            # that produced the score was invisible to the user.
            reason = sg("reason")
            if reason:
                lines.append(f"   <i>{_esc(_clip(str(reason), 220))}</i>")

    # ── Macro context ─────────────────────────────────────────────────────────
    # Stated whether or not it applied: "the dollar says nothing about this pair,
    # and here is why" is the honest answer, and silence would read as an
    # omission rather than a decision.
    macro = g("macro")
    if isinstance(macro, dict) and (macro.get("reason") or macro.get("lines")):
        lines.append("")
        if macro.get("applied"):
            regime = str(macro.get("regime") or "UNKNOWN").replace("_", "-").lower()
            lines.append(
                f"🌍 <b>Macro</b> — {_esc(str(macro.get('reason') or regime))}"
            )
            for line in list(macro.get("lines") or [])[:2]:
                lines.append(f"   <i>{_esc(_clip(str(line), 200))}</i>")
        else:
            lines.append(
                f"🌍 <b>Macro</b> — <i>not applied: "
                f"{_esc(_clip(str(macro.get('reason') or 'unavailable'), 160))}</i>"
            )

    # ── Kronos ML fusion ──────────────────────────────────────────────────────
    k = g("kronos")
    lines.append("")
    if isinstance(k, dict) and k.get("direction"):
        kconf = float(k.get("confidence") or 0.0)
        lines.append(
            f"🔮 <b>Kronos</b> — {_esc(str(k.get('direction')).upper())}  "
            f"conf <b>{kconf * 100:.0f}%</b>  Δ {k.get('pct_change')}%"
        )
        if k.get("target_price") is not None:
            lines.append(
                f"   Target <code>{_fmt_p(k['target_price'])}</code> · "
                f"engine <code>{_esc(k.get('engine') or '—')}</code>"
            )
        if k.get("summary"):
            lines.append(f"   <i>{_esc(_clip(k['summary'], 180))}</i>")
    else:
        lines.append("🔮 <b>Kronos</b> — no forecast available")

    # ── AI review ─────────────────────────────────────────────────────────────
    ai = g("ai")
    lines.append("")
    if isinstance(ai, dict) and ai.get("available"):
        lines.append(
            f"🧠 <b>AI Review</b> <code>{_esc(ai.get('provider') or '—')}"
            f"/{_esc(ai.get('model') or '—')}</code>"
        )
        if ai.get("bias_comment"):
            lines.append(f"<i>{_esc(_clip(ai['bias_comment'], 300))}</i>")
        if ai.get("market_read"):
            lines.append(_esc(_clip(ai["market_read"], 500)))
        top_pick = ai.get("top_pick_entry")
        if top_pick is not None:
            lines.append(f"⭐ Top pick: <code>{_fmt_p(top_pick)}</code>")
        for r in (ai.get("rated_signals") or [])[:3]:
            verdict = str(r.get("verdict", "watch")).lower()
            icon = {"take": "✅", "skip": "🚫"}.get(verdict, "👀")
            lines.append(
                f"{icon} <code>{_fmt_p(r.get('entry', 0.0))}</code> {verdict.upper()} — "
                f"{_esc(_clip(r.get('note', ''), 140))}"
            )
        if ai.get("risk_warning"):
            lines.append(f"⚠️ {_esc(_clip(ai['risk_warning'], 240))}")
    else:
        reason = ai.get("reason") if isinstance(ai, dict) else None
        lines.append(f"🧠 <b>AI Review</b> — unavailable{f': {_esc(reason)}' if reason else ''}")

    text = "\n".join(lines)
    if len(text) > _SNIPER_MAX_LEN:
        text = text[: _SNIPER_MAX_LEN - 1].rstrip() + "…"
    return text


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


async def _handle_jarvis(args: str, db: AsyncSession) -> tuple[str, str]:
    """Forward free-form text to the Jarvis command engine."""
    if not args:
        return "❓ Usage: /jarvis &lt;command&gt;\nExample: /jarvis show positions", "HTML"
    return await _jarvis_command(args, db)


async def _handle_unknown(_args: str, _db: AsyncSession) -> tuple[str, str]:
    return "❓ Unknown command.  Type /help for a list of commands.", "HTML"


# ── Jarvis bridge ─────────────────────────────────────────────────────────────

async def _jarvis_command(cmd: str, db: AsyncSession | None = None) -> tuple[str, str]:
    """Call the Jarvis execute_command handler and return (reply_text, parse_mode)."""
    try:
        from app.api.jarvis import execute_command, CommandRequest
        result = await execute_command(CommandRequest(command=cmd))

        # A failed *analysis* is not the end of the conversation. This is where
        # "❌ I couldn't find a Bitget-tradeable pair for GBPUSD, Sir." was
        # rendered — a dead end for a question the AI chat can answer perfectly
        # well. Hand it over instead of showing the user a wall.
        if not result.ok and result.action in ("analyze", "unknown") and db is not None:
            reply, mode, _ = await _ai_fallback(
                cmd, db, hint=result.detail or result.speech or ""
            )
            if reply:
                return reply, mode

        emoji = "✅" if result.ok else "❌"
        # For rich analysis, prefer the full detail (AI narrative + news + levels)
        # over the short spoken summary so Telegram users get the complete research.
        if result.action in ("analyze", "news_position_analysis") and result.detail:
            body = result.detail
        else:
            body = result.speech or result.detail
        # `body` carries the AI-composed narrative, so it can contain the same
        # markdown and LaTeX as a chat reply — and its bare "&" and "<" would
        # make Telegram reject the message under parse_mode=HTML. The old
        # text[:4000] also cut mid-word.
        return f"{emoji} {format_for_telegram(body)}", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Jarvis error: {exc}", "HTML"


async def _news_context(text: str, symbols: list[str]) -> str | None:
    """Headlines for the instruments named, plus a live search on the question.

    Mirrors what the web chat already injects (paul_chat.build_jarvis_system_prompt);
    Telegram had none of it, which is why the same question answered well in the
    browser and got a refusal here. Both sources are cached and keyless.
    """
    try:
        from plugins.AgentPaulPlugin.backend.services import news_research
    except Exception:  # noqa: BLE001
        return None

    blocks: list[str] = []
    try:
        for sym in symbols[:2]:
            hits = await news_research.news_for_symbol(sym, limit=3)
            if hits:
                lines = [f"### Recent news — {sym}"]
                lines += [
                    f"  - [{h['source']}] ({h['sentiment']:+.2f}) {h['title']}"
                    for h in hits[:3]
                ]
                blocks.append("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI] symbol news skipped: {}", exc)

    try:
        web = await news_research.web_news_search(text.strip(), limit=5)
        if web:
            lines = ["### Live web search (most recent first)"]
            for item in web[:5]:
                when = item.get("published_at")
                when_s = when.strftime("%Y-%m-%d %H:%M") if when else ""
                lines.append(f"  - [{item.get('source', '?')}] {when_s} {item.get('title', '')}")
            blocks.append("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI] web search skipped: {}", exc)

    if not blocks:
        return None
    return "## Live News & Web Results\n" + "\n\n".join(blocks)


async def _fib_context(symbols: list[str]) -> str | None:
    """Auto fib retracement golden-zone confluence for the instruments named.

    Mirrors _news_context's additive pattern: computes the same ZigZag-based
    auto fib retracement used on the web charts (app.signals.technical) and
    the /sniper SMC engine, formatted as a small markdown block, or None on
    any failure — /analyze and general chat must degrade to their pre-existing
    behavior, not error, when fib data is unavailable.
    """
    if not symbols:
        return None
    try:
        from app.signals.technical import auto_fib_retracement, ohlcv_to_dataframe
        from app.services import market_data
    except Exception:  # noqa: BLE001
        return None

    blocks: list[str] = []
    for sym in symbols[:2]:
        try:
            ohlcv, _ = await market_data.fetch_ohlcv_universal(sym, timeframe="1h", limit=200)
            if not ohlcv or len(ohlcv) < 20:
                try:
                    from app.exchanges.manager import exchange_manager, SupportedExchange
                    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                    if connector:
                        ohlcv = await connector.get_ohlcv(symbol=sym, timeframe="1h", limit=200)
                except Exception:  # noqa: BLE001
                    ohlcv = []
            if not ohlcv or len(ohlcv) < 20:
                continue
            df = ohlcv_to_dataframe(ohlcv)
            fib = auto_fib_retracement(df)
            swing = fib.get("swing")
            zone = fib.get("golden_zone")
            if not swing or not zone:
                continue
            cp = float(df["close"].iloc[-1])
            in_zone = zone["low"] <= cp <= zone["high"]
            blocks.append(
                f"### Fibonacci confluence — {sym}\n"
                f"  Auto-detected swing: {swing['start_price']:.4f} → {swing['end_price']:.4f} ({swing['direction']})\n"
                f"  Golden zone: {zone['low']:.4f}–{zone['high']:.4f} — price is currently "
                f"{'inside' if in_zone else 'outside'} this zone (last: {cp:.4f})"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[AI] fib context skipped for {}: {}", sym, exc)

    if not blocks:
        return None
    return "## Fibonacci Confluence\n" + "\n\n".join(blocks)


async def _learned_context(db: AsyncSession, symbols: list[str]) -> str | None:
    """What past calls on these instruments actually did — win rate, realised R.

    Returns None until enough proposals have settled; a win rate over three
    trades is noise dressed as evidence and would make the model *more*
    confidently wrong, not less.
    """
    try:
        from app.services import analysis_journal

        return await analysis_journal.memory_block_for(db, symbols)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI] learned context skipped: {}", exc)
        return None


# ── Conversation history helpers ─────────────────────────────────────────────

def _get_history(chat_id: str) -> list[dict[str, str]]:
    """Return the stored conversation history for a chat (last _MAX_HISTORY pairs)."""
    return list(_CHAT_HISTORY.get(chat_id, [])[-(_MAX_HISTORY * 2):])


def _record_history(chat_id: str, role: str, content: str) -> None:
    """Append a message to the per-chat history, capping at 2×_MAX_HISTORY entries."""
    bucket = _CHAT_HISTORY.setdefault(chat_id, [])
    bucket.append({"role": role, "content": content[:800]})
    if len(bucket) > _MAX_HISTORY * 2:
        _CHAT_HISTORY[chat_id] = bucket[-(_MAX_HISTORY * 2):]


def _jarvis_system_prompt(
    live_data: str | None = None,
    *,
    price_block: str | None = None,
    news_block: str | None = None,
    learned: str | None = None,
    fib_block: str | None = None,
) -> str:
    """System prompt giving the LLM the Jarvis persona and its real capabilities.

    Every block is optional and appended only when the caller could fetch it, so
    a failed lookup degrades to a thinner prompt rather than a false statement
    about what the assistant can do.
    """
    base = (
        "You are JARVIS, the AI trading assistant for your principal. Always address "
        "them as 'Sir'. You are connected to a live trading system with the following "
        "capabilities:\n"
        "• Crypto futures (Bitget): /order, /close, /tp, /sl, /positions, /portfolio\n"
        "• Kronos ML forecasting engine: /forecast BTCUSDT — works for crypto AND\n"
        "  every MT5 instrument (e.g. /forecast XAUUSD 4h, /forecast GBPJPY,\n"
        "  /forecast US30, /forecast USOIL). Timeframes 1m–1w.\n"
        "• MT5 forex trading: /mt5 status, /mt5 positions, /mt5 scalp\n"
        "• Live market signals & sniper: /signals, /sniper\n"
        "• Deep AI market analysis: /analyze BTCUSDT — works for crypto AND for FX,\n"
        "  metals, indices, energy and softs (e.g. /analyze GBPUSD, /analyze XAUUSD)\n\n"
        "When a user asks how to forecast or predict an instrument, do NOT stop at\n"
        "describing the method — these two commands ARE the platform's answer, and\n"
        "they run on whatever pair they named. RUN them: call the forecast_symbol\n"
        "or analyze_symbol tool and answer with the real numbers it returns.\n"
        "NEVER end on 'Would you like me to execute that?' or 'shall I run it?' —\n"
        "asking permission to read data the user already asked for wastes their\n"
        "turn. Run it first, then offer the follow-up.\n"
        "When you DO have live engine numbers, they are the answer: lead with the\n"
        "direction, % move, target, confidence and the anchor price they came\n"
        "from, and say it is a live Kronos run — never file them at the bottom as\n"
        "'for reference' or as an 'example' under a general explanation.\n"
        "NEVER close by asking the user to name an instrument. If the thread has\n"
        "named one — this turn or an earlier one — that is the instrument, and you\n"
        "forecast it. The platform appends the live forecast card beneath your\n"
        "reply automatically, so 'tell me a pair and I'll run it instantly' is\n"
        "always wrong: it has already run.\n"
        "Any follow-up you offer must be written as the literal command, e.g.\n"
        "`/forecast BTCUSDT 4h` or `/analyze XAUUSD`, so 'yes' can run it.\n"
        "NEVER offer to place, size or manage a trade for the user and NEVER ask\n"
        "them for a position size or risk parameters — orders are theirs to send.\n"
        "If a trade is the natural next step, print the exact command they would\n"
        "send (`/order long BTCUSDT 100`) and stop there.\n"
        "A cash index (GER40, UK100, FRA40 …) carries no traded volume, so /forecast\n"
        "returns NO_TRADE for it by design — /analyze still gives the full read.\n\n"
        "LIVE DATA — READ THIS CAREFULLY:\n"
        "You HAVE live market data for EVERY asset class: crypto, FX majors and "
        "crosses, metals (gold XAUUSD, silver XAGUSD, platinum, palladium), indices "
        "(US30, NAS100, US500, GER40, UK100, JPN225 …), energy (USOIL, UKOIL, NGAS) "
        "and softs. You also have live internet news search.\n"
        "NEVER say 'the current platform does not provide a live gold-price feed', "
        "'the current toolset does not include a live price feed', 'I cannot pull the "
        "exact spot price from external markets', or any variation. Those are FALSE. "
        "Never ask the user to supply a price you can fetch, and never ask permission "
        "to query market data — you already have it. If a fetch genuinely failed, say "
        "so and offer to retry; do not call it a missing capability.\n\n"
        "YOUR SCOPE IS EVERYTHING — NOT JUST TRADING:\n"
        "Trading is one of your jobs, not the limit of your knowledge. Answer "
        "questions in ANY field — mathematics (show your working), physics, "
        "chemistry, biology, medicine, history, geography, law, philosophy, "
        "programming, languages, cooking, everyday practical help. NEVER refuse "
        "a question for being 'not crypto', 'outside my domain' or 'not what I "
        "was built for' — none of that is true. NEVER reply with a bare "
        "'I can't help with that': if something really is beyond you, say "
        "specifically what and why, then give the closest useful help you can. "
        "When unsure of a fact, search for it rather than guessing.\n\n"
        "HOW TO FORMAT — YOU ARE REPLYING IN TELEGRAM ON A PHONE:\n"
        "- Write maths in plain Unicode, NEVER LaTeX. Do not use \\[ … \\], "
        "\\( … \\), $ … $, \\frac, \\boxed, \\begin{aligned} or \\mathbf — they "
        "arrive as raw backslashes and are unreadable.\n"
        "    Write:  ∂u/∂t + (u·∇)u = -∇p + νΔu + f,   ∇·u = 0\n"
        "    NOT:    \\frac{\\partial \\mathbf{u}}{\\partial t} = -\\nabla p\n"
        "    Use the real symbols: ∂ ∇ Δ ∑ ∏ ∫ √ ∞ ≤ ≥ ≠ ≈ ± × ÷ · ∈ → ⇒ "
        "ℝ ℂ ℚ ℤ π α β θ λ μ σ ω ζ ν, superscripts ⁰¹²³ⁿˣ, subscripts ₀₁₂ₙᵢ.\n"
        "- NEVER use markdown tables or pipe characters for layout — a phone "
        "screen is too narrow. Use short bullet lines instead.\n"
        "- Keep it SHORT: aim for under 250 words. Telegram truncates long "
        "messages, so lead with the answer and cut the preamble. Offer to go "
        "deeper rather than sending an essay.\n"
        "- Use *bold* sparingly for the key term, `backticks` for code or "
        "commands, and '- ' for bullets. No ## headings, no horizontal rules.\n\n"
        "Guidelines:\n"
        "- Answer trading questions with confidence and precision.\n"
        "- When the user wants to execute a trade, show them the exact command.\n"
        "- For everything else, be sharp and genuinely helpful — answer what "
        "  was actually asked instead of steering back to markets.\n"
        "- Keep responses concise — 2-8 sentences unless deep analysis is requested.\n"
        "- Use the live figures below. Never invent a number that isn't there — if a\n"
        "  price you need is missing, fetch it or say the fetch failed.\n"
        "- Format numbers cleanly; use emojis sparingly.\n"
        "- Maintain the conversation thread — reference prior messages when relevant.\n"
        "- IMPORTANT: When live portfolio data is included, ALWAYS analyse it directly.\n"
        "  Give clear hold/close/adjust recommendations for each position with reasoning.\n"
        "  Never tell the user to 'run a command' if data is already shown.\n\n"
        "HOW TO DELIVER A MARKET READ (same voice on every instrument — crypto,\n"
        "FX, metals, indices):\n"
        "1. 👁‍🗨 Structure — the timeframe and whether price is trending (higher\n"
        "   lows / lower highs) or consolidating and waiting for a break.\n"
        "2. ⚖️ Momentum — MACD against the zero line and the RSI value.\n"
        "3. Volatility — position against the Bollinger Bands, whether volume is\n"
        "   drying up or expanding versus average, and the Stochastic.\n"
        "4. 🔔 Key Levels — the level that ignites upside and the one whose loss\n"
        "   opens a correction; entry, stop and targets when a plan exists.\n"
        "5. A one-line bias with the tally, e.g. 🟡 Bias: Neutral (Buy 3/6 | Sell 3/6).\n"
        "Skip any part you have no data for. QUOTE the numbers you were given —\n"
        "never invent an RSI, a level, a volume ratio or a vote tally.\n\n"
        "ECONOMIC DATA (CPI, PPI, NFP, claims): call the `economic_release` tool\n"
        "and report only what it returns — each line as actual vs forecast vs\n"
        "previous, a one-line read of each, then the overall insight: hawkish or\n"
        "dovish, the currency direction, and gold's (gold moves inversely to the\n"
        "dollar). Never state a figure that has not printed. If nothing has been\n"
        "released, say exactly that.\n\n"
        "FOLLOW-UP ON AN EARLIER CALL: use the `scenario_check` tool and report\n"
        "what it returns — whether price reached our entry, how much of the mapped\n"
        "move is complete, and whether the invalidation was hit. NEVER claim a\n"
        "previous call worked, or that a level was called at all, unless the tool\n"
        "returned it; a congratulatory update on a losing or imaginary call is the\n"
        "most damaging thing you can send, because the user will size the next\n"
        "trade on it. When a plan is genuinely running, say so and give the "
        "percentage completed.\n\n"
        "ZONES: for 'which levels should I watch', call `zone_levels`. For the\n"
        "dollar or the macro backdrop, call `dollar_read`.\n\n"
        "CANDLES — STUDY THE MOVE, DON'T GUESS IT: judgements about direction,\n"
        "trend or where a move can run come from the CLOSED candles behind the\n"
        "current one. Call the `candles` tool and choose the depth yourself: 28\n"
        "closed candles is the floor, and you should ask for far more (100, 200,\n"
        "500) for anything structural. There is no cap. It separates closed\n"
        "candles from the bar still forming and returns the measured movement\n"
        "across the window — window high/low, net change, up/down counts, the\n"
        "run in progress and the swing structure. Never treat the forming bar as\n"
        "closed, and never say you cannot judge a move for lack of data: fetch\n"
        "more candles."
    )
    if price_block:
        base += f"\n\n{price_block}"
    if news_block:
        base += f"\n\n{news_block}"
    if learned:
        base += f"\n\n{learned}"
    if fib_block:
        base += f"\n\n{fib_block}"
    if live_data:
        base += f"\n\n## Live Portfolio Snapshot (fetched moments ago)\n{live_data}"
    return base


# ── Position-context keywords ─────────────────────────────────────────────────

_POSITION_KEYWORDS = frozenset({
    "position", "positions", "trade", "trades", "portfolio", "pnl", "p&l",
    "profit", "loss", "losses", "hold", "holding", "close", "close out",
    "open trade", "open position", "my trade", "my position", "my trades",
    "my positions", "margin", "leverage", "exposure", "equity", "unrealised",
    "unrealized", "short", "long", "forex", "mt5", "bitget", "crypto trade",
})


def _wants_position_analysis(text: str) -> bool:
    """Return True when the message seems to be about live positions / portfolio."""
    lower = text.lower()
    return any(kw in lower for kw in _POSITION_KEYWORDS)


#: Asking for real analysis earns the slow, strong model. Everything else — the
#: bulk of bot traffic — gets the fast one, because on Telegram there is no
#: streaming or progress indicator and a long silence reads as a broken bot.
#:
#: The rule lives in the router now (``ai_router.wants_deep_thinking``) so the
#: bot and the web chat cannot drift apart on what counts as a big task. The
#: local keyword set it replaced matched "why", "plan", "strategy" and "explain"
#: as bare substrings, which sent nearly every message down the slow path.
def _wants_deep_reasoning(text: str) -> bool:
    from plugins.AiMarketAnalyst.backend.services.ai_router import wants_deep_thinking

    return wants_deep_thinking(text)


async def _fetch_live_position_context(db: AsyncSession) -> str | None:
    """Fetch live positions from crypto (Bitget) and MT5, return a compact summary."""
    sections: list[str] = []

    # ── Crypto positions ──────────────────────────────────────────────────────
    try:
        from app.api.jarvis import get_all_positions, get_portfolio
        positions = await get_all_positions()
        if positions:
            lines = ["### Crypto / Bitget Futures Positions"]
            for p in positions:
                sign = "+" if p.pnl >= 0 else ""
                lines.append(
                    f"• {p.symbol} ({p.exchange}) {p.side.upper()}"
                    f"  size={p.size}"
                    f"  entry={p.entry_price:.6g}"
                    f"  mark={p.mark_price:.6g}"
                    f"  PnL={sign}{p.pnl:.4f} ({sign}{p.pnl_pct:.2f}%)"
                    f"  lev={getattr(p, 'leverage', '?')}x"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("### Crypto / Bitget Futures Positions\nNone open.")

        try:
            port = await get_portfolio()
            sign = "+" if port.total_pnl >= 0 else ""
            sections.append(
                f"**Crypto Portfolio:** {port.total_positions} positions"
                f"  notional={port.total_notional:,.2f}"
                f"  totalPnL={sign}{port.total_pnl:+,.4f}"
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        sections.append(f"### Crypto Positions\nUnavailable: {exc}")

    # ── MT5 positions ─────────────────────────────────────────────────────────
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        accounts = (await db.execute(_sel(MT5Account))).scalars().all()
        any_mt5 = False
        for acct in accounts[:3]:  # cap at 3 accounts
            try:
                positions = await mt5_client.get_positions(acct)
                if not positions:
                    continue
                any_mt5 = True
                label = getattr(acct, "label", None) or getattr(acct, "login", acct.id)
                lines = [f"### MT5 Positions — Account {acct.id} ({label})"]
                for p in positions[:20]:
                    pnl = float(p.get("profit", 0) or 0)
                    swap = float(p.get("swap", 0) or 0)
                    sign = "+" if pnl >= 0 else ""
                    lines.append(
                        f"• {p.get('symbol','?')} {p.get('type','?').upper()}"
                        f"  vol={p.get('volume','?')}"
                        f"  open={p.get('price_open','?')}"
                        f"  current={p.get('price_current','?')}"
                        f"  PnL={sign}{pnl:.2f}  swap={swap:.2f}"
                        f"  ticket={p.get('ticket','?')}"
                    )
                sections.append("\n".join(lines))
            except Exception:  # noqa: BLE001
                pass
        if not any_mt5:
            sections.append("### MT5 Positions\nNone open (or no accounts configured).")
    except Exception:  # noqa: BLE001
        sections.append("### MT5 Positions\nPlugin unavailable.")

    return "\n\n".join(sections) if sections else None


# ── Prediction intent ─────────────────────────────────────────────────────────
# "based mathematics how can we predict bitcoin" used to return an essay on
# ARIMA and GARCH ending in "Would you like me to execute /forecast BTCUSDT?" —
# a question about the platform's own forecasting answered by describing it.
# A message that asks for a prediction on a named instrument now runs Kronos.

_PREDICT_RE = re.compile(
    r"\b(?:predict\w*|forecast\w*|projection|project\w*\s+(?:price|move)|"
    r"price\s+target|target\s+price|outlook)\b"
    r"|\bwhere\s+(?:is|will|do you think)\b[^?]{0,40}\b(?:go|going|head\w*|end up|land)\b"
    r"|\bwhat(?:'?s|\s+is|\s+are)?\s+next\s+for\b"
    # "will gold rise this week" is a forecast request in every way except that
    # it uses none of the words above.
    r"|\b(?:will|won'?t)\b[^?]{0,40}\b(?:rise|fall|drop|climb|dip|go\s+up|go\s+down|"
    r"rally|crash|pump|dump|hit|reach|break)\b"
    r"|\bhow\s+(?:high|low|far)\b",
    re.IGNORECASE,
)

_OWN_BOOK_RE = re.compile(
    r"\bmy\b[^.?!]{0,40}\b(?:position|positions|portfolio|trade|trades|pnl|p&l|"
    r"account|equity|exposure|book)\b",
    re.IGNORECASE,
)

#: A prediction question that also asks *how* it is done gets a short written
#: answer above the live forecast, rather than one or the other.
_METHOD_RE = re.compile(
    r"\bhow\s+(?:can|do|does|would|should|might)\b"
    r"|\b(?:explain|methodolog\w*|derivation)\b"
    r"|\bmathematic\w*|\bmaths\b|\bmath\b|\bstatistic\w*|\bformula\w*|\bequation\w*"
    r"|\bwhat\s+(?:model|models|method|methods|approach)\b",
    re.IGNORECASE,
)


def _is_affirmative(text: str) -> bool:
    """True for a bare yes — nothing that carries a new question or condition.

    Deliberately strict: "yes but what about gold?" is a new message, not a
    confirmation, and running the parked command for it would answer something
    the user did not ask.
    """
    cleaned = text.strip().strip(".!,").lower()
    if not cleaned or len(cleaned) > 30:
        return False
    cleaned = re.sub(r"\b(please|sir|thanks|thank you|now|jarvis)\b", " ", cleaned)
    cleaned = re.sub(r"[^a-z ]+", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned in {
        "y", "ya", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "k",
        "go", "go ahead", "goahead", "do it", "do that", "run it", "run that",
        "execute", "execute it", "execute that", "proceed", "affirmative",
        "absolutely", "yes do it", "yes run it", "yes execute", "yes go ahead",
        "confirm", "confirmed", "show me", "show it", "lets go", "let s go",
    }


def _remember_offer(chat_id: str, reply: str) -> None:
    """Park the first read-only command a reply offers, for a later "yes".

    Only ``_AUTORUN_SAFE`` commands are parked, so no confirmation can ever be
    turned into an order — a ``/order`` line in a model's reply stays something
    the user has to send themselves.
    """
    if not chat_id:
        return
    _PENDING_OFFER.pop(chat_id, None)
    match = re.search(
        r"/(" + "|".join(sorted(_AUTORUN_SAFE)) + r")\b([^\n]*)", reply or ""
    )
    if not match:
        return

    # Only argument-shaped tokens are kept. The command usually sits mid-
    # sentence ("I can run /forecast BTCUSDT for you"), and taking the rest of
    # the line verbatim would hand "BTCUSDT for you." to the handler.
    args: list[str] = []
    for token in match.group(2).split():
        token = token.strip(".,;:!?)—-").strip("<b>").strip("</")
        if not token or len(args) >= 3:
            break
        if token.lower() in _TIMEFRAMES or token.lower() in _OFFER_SUBCOMMANDS:
            args.append(token.lower())
        elif re.fullmatch(r"[A-Z0-9][A-Z0-9/._-]{1,14}", token):
            args.append(token)
        else:
            break
    _PENDING_OFFER[chat_id] = (match.group(1).lower(), " ".join(args))


def _symbols_in(text: str) -> list[str]:
    """Instruments named in a string, or [] — never raises."""
    try:
        from app.services import market_data

        return market_data.extract_symbols(text or "", limit=2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Forecast] symbol extraction failed: {}", exc)
        return []


def _recent_symbol(chat_id: str, *, turns: int = 6) -> str | None:
    """The instrument the thread is currently about, from recent history.

    A conversation names its subject once. "How do I turn those bullets into a
    working predictor" is still a question about Bitcoin, and re-reading the
    last few turns is what lets the follow-up run the engine too.
    """
    if not chat_id:
        return None
    for message in reversed(_CHAT_HISTORY.get(chat_id, [])[-turns:]):
        symbols = _symbols_in(message.get("content") or "")
        if symbols:
            return symbols[0]
    return None


def _forecast_request(text: str, chat_id: str = "") -> tuple[str, str] | None:
    """``(symbol, timeframe)`` when the message asks for a prediction, else None.

    The instrument has to be resolvable — from the message, or failing that from
    the thread it belongs to. "Can you predict the future?" names nothing in
    either place and stays a conversation.
    """
    if not text or not _PREDICT_RE.search(text):
        return None
    # "what's the outlook on my BTC position" is a question about the book, not
    # a forecast request — that one belongs to the live-position path below.
    if _OWN_BOOK_RE.search(text):
        return None
    symbols = _symbols_in(text)
    if not symbols:
        carried = _recent_symbol(chat_id)
        if not carried:
            return None
        symbols = [carried]

    return symbols[0], _timeframe_in(text)


def _timeframe_in(text: str, default: str = "1h") -> str:
    """The candle size the message names, or the default."""
    for tok in re.findall(r"\b(\d+[mhdw])\b", (text or "").lower()):
        if tok in _TIMEFRAMES:
            return tok
    return default


async def _method_note(text: str, db: AsyncSession) -> str | None:
    """Two or three sentences on the method, to sit above the live forecast."""
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            chat_with_tools, resolve_chat_route,
        )

        # Ninety words above a forecast card is never deep work, whatever the
        # question sounded like — and a thinking model here would spend the
        # whole 10s budget reasoning and return nothing.
        route = await resolve_chat_route(
            db, task="telegram_chat", text=text, force_deep=False, surface="telegram"
        )
        system = (
            "You are JARVIS, addressing the user as 'Sir'. Answer the question "
            "about forecasting method in at most 90 words, in plain Unicode "
            "maths (never LaTeX), as short '- ' bullets. The platform's live "
            "Kronos forecast for the instrument is printed directly BELOW your "
            "answer, so do NOT offer to run it, do NOT ask permission, and do "
            "NOT tell the user to send a command — it has already run."
        )
        resp = await chat_with_tools(
            db,
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            max_iterations=1,
            total_budget_s=10.0,
            temperature=0.4,
            max_tokens=480,
            json_mode=False,
            model_override=route.model,
            max_retries=route.max_retries,
            agent_name="jarvis-telegram-method",
            source="telegram",
            task=route.task,
        )
        if resp.get("ok") and resp.get("content"):
            return str(resp["content"]).strip()
    except Exception as exc:  # noqa: BLE001 — the forecast is the answer; this is trim
        logger.debug("[Forecast] method note skipped: {}", exc)
    return None


#: A reply that is *about* forecasting. Whether the model reached for the
#: forecast tool is its own choice and varies by provider and by turn — this is
#: how the answer gets the platform's own numbers regardless.
_TALKS_FORECAST_RE = re.compile(
    r"\b(?:forecast\w*|predict\w*|project(?:s|ed|ion|ions)|kronos|"
    r"arima\w*|garch|varx|lstm|monte[- ]?carlo|log[- ]?returns?|"
    r"out[- ]of[- ]sample|ensemble|price\s+target|target\s+price|outlook)\b",
    re.IGNORECASE,
)


async def _attach_forecast(
    reply: str,
    text: str,
    db: AsyncSession,
    *,
    chat_id: str = "",
) -> tuple[str, dict | None]:
    """Append the live Kronos card to any answer about forecasting.

    A method answer with no figures is the failure the user reported twice: the
    model explained ARIMA, GARCH and ensembles and ran nothing. The engine is
    right here, so the explanation ends with what it actually predicts.

    The card is attached even when the model called ``forecast_symbol`` itself.
    Quoting "+2.08%, target 64164.5" inside a bullet is not the same as handing
    the user the card with its anchor price, sniper entries and buttons, and the
    two cannot disagree: ``run_forecast_cached`` serves both from one cached
    response, so the second call is a cache hit on the same numbers.

    Returns display-ready text: the narrative is converted to Telegram HTML
    *before* the card is appended, because the card is already HTML and running
    the converter over the pair would escape its tags into visible &lt;b&gt;.
    """
    def _plain() -> tuple[str, dict | None]:
        return format_for_telegram(reply), None

    if not reply or not _TALKS_FORECAST_RE.search(reply):
        return _plain()
    if _OWN_BOOK_RE.search(text):
        return _plain()

    symbols = _symbols_in(text) or _symbols_in(reply)
    symbol = symbols[0] if symbols else _recent_symbol(chat_id)
    if not symbol:
        return _plain()

    try:
        card, _mode, markup = await _handle_forecast(
            f"{symbol} {_timeframe_in(text)}", db
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Forecast] could not attach live numbers: {}", exc)
        return _plain()
    if not card or card[:1] in {"❓", "⚠️", "❌"}:
        return _plain()

    room = 4096 - len(card) - 2
    if room < 200:
        return card, markup         # the card alone beats a truncated pair
    # format_for_telegram trims on a word boundary and closes its own tags —
    # slicing the HTML by hand would leave a half-written tag Telegram rejects.
    return f"{format_for_telegram(reply, limit=room)}\n\n{card}", markup


async def _forecast_reply(
    text: str,
    symbol: str,
    timeframe: str,
    db: AsyncSession,
    *,
    chat_id: str = "",
) -> tuple[str | None, str, dict | None]:
    """Run the real forecast for a free-text prediction question.

    Returns ``(None, …)`` when the engine could not answer, so the caller falls
    through to ordinary chat rather than showing the user a dead end.
    """
    try:
        card, mode, markup = await _handle_forecast(f"{symbol} {timeframe}", db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Forecast] auto-run for '{}' failed: {}", text[:60], exc)
        return None, "HTML", None

    if not card or card[:1] in {"❓", "⚠️", "❌"}:
        return None, "HTML", None

    body = card
    if _METHOD_RE.search(text):
        note = await _method_note(text, db)
        # The card is the answer; the note only gets whatever room is left
        # under Telegram's 4096-character ceiling, and format_for_telegram
        # trims it on a word boundary with its tags closed.
        room = 4096 - len(card) - 2
        if note and room > 200:
            body = f"{format_for_telegram(note, limit=room)}\n\n{card}"

    if chat_id:
        _record_history(chat_id, "user", text)
        _record_history(chat_id, "assistant", body)
        # Nothing left to confirm — the command already ran.
        _PENDING_OFFER.pop(chat_id, None)
    return body[:4096], mode, markup


# ── Images ────────────────────────────────────────────────────────────────────

def _extract_image(message: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(file_id, mime)`` for an image message, else None.

    Handles both ways an image reaches a bot:

    * ``photo`` — Telegram's recompressed sizes, ascending, so the last entry is
      the highest resolution available.
    * ``document`` with an image mime — what you get by sending a chart as a
      *file*, which skips recompression and keeps the candles legible. Worth
      supporting precisely because that is the better way to send a chart.
    """
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]
        if isinstance(largest, dict) and largest.get("file_id"):
            return str(largest["file_id"]), "image/jpeg"

    doc = message.get("document")
    if isinstance(doc, dict):
        mime = str(doc.get("mime_type") or "")
        if mime.startswith("image/") and doc.get("file_id"):
            return str(doc["file_id"]), mime

    return None


def _symbol_from_chart_title(raw: str) -> str | None:
    """Turn whatever is printed on a chart into a symbol we can price.

    Charts are titled for humans — "Gold Spot / U.S. Dollar", "Bitcoin / TetherUS"
    — and none of that resolves to an instrument. Reduce the title to its words
    and take the first one the market-data aliases recognise, so the plan is
    still built for the right market.
    """
    from app.services import market_data

    text = (raw or "").strip()
    if not text:
        return None

    # "XAU/USD" is one instrument, not two tokens — join the slash spellings
    # before extraction so the pair survives as a pair.
    joined = re.sub(r"\b([A-Za-z]{3})\s*/\s*([A-Za-z]{3})\b", r"\1\2", text)

    # extract_symbols is deliberately conservative: a token has to be a known
    # alias or a real instrument, so "a photo of a cat" yields nothing rather
    # than sending "PHOTO" off to the analysis route.
    for token in market_data.extract_symbols(joined, limit=4):
        symbol, cls = market_data.canonicalize_for_analysis(token)
        if symbol and cls != market_data.UNKNOWN:
            return symbol
    return None


async def _live_plan(instrument: str) -> dict | None:
    """Live trade proposal for the instrument a chart shows, or None.

    The picture supplies the *market*; every number comes from live data for it.
    Reading levels off a screenshot would inherit whatever the screenshot was —
    a stale chart, a different broker's scale — and quote them as current.
    """
    symbol = _symbol_from_chart_title(instrument) or instrument
    try:
        from app.api.jarvis import _analyze_symbol

        result = await _analyze_symbol(symbol, f"analyze {symbol}", None)
    except Exception as exc:  # noqa: BLE001 — no plan is fine, a crash is not
        logger.debug("[Vision] live plan failed for {}: {}", instrument, exc)
        return None

    if not (result and result.ok and isinstance(result.order, dict)):
        return None
    order = result.order
    return order if order.get("proposed_entry") is not None else None


def _format_plan(instrument: str, plan: dict, vision) -> str:
    """Render the proposal as a Telegram card that explains itself.

    The old output was a wall of raw indicator readings with the setup buried at
    the bottom, and nothing tying the two together — so a plan could read as if
    it contradicted the chart it came with. This leads with the decision, then
    says in one line why, then shows the levels.
    """
    import html as _html

    def num(key: str):
        try:
            return float(plan.get(key))
        except (TypeError, ValueError):
            return None

    side = str(plan.get("side") or "").lower()
    arrow = "🟢 BUY" if side == "long" else "🔴 SELL" if side == "short" else "⚪ WAIT"
    entry, sl = num("proposed_entry"), num("sl")
    tp1, tp2 = num("tp1"), num("tp2")
    current = num("current_price")
    conf = num("confidence") or 0
    rsi = num("rsi")

    def px(v):
        return f"{v:g}" if v is not None else "—"

    lines = [
        f"{arrow} <b>{_html.escape(instrument)}</b>  ·  confidence {conf:.0%}",
        f"Live {px(current)} · {_html.escape(str(plan.get('trend') or '')).lower()}"
        + (f" · RSI {rsi:.0f}" if rsi is not None else ""),
        "",
        "<b>The setup</b>",
    ]

    # One plain sentence explaining what the plan actually asks the trader to do.
    if entry is not None and current is not None:
        if side == "long":
            move = "a pullback to" if entry < current else "a break above"
        else:
            move = "a rally to" if entry > current else "a break below"
        risk = abs(entry - sl) if sl is not None else None
        lines.append(
            f"Wait for {move} <b>{px(entry)}</b>, then risk "
            f"{px(risk) if risk else '—'} to <b>{px(tp1)}</b>."
        )
    if reasons := plan.get("bias_reasons"):
        text = reasons if isinstance(reasons, str) else "; ".join(str(r) for r in reasons)
        lines.append(f"<i>{_html.escape(text)[:300]}</i>")

    lines += [
        "",
        "<b>Levels</b>",
        f"Entry <code>{px(entry)}</code>",
        f"Stop  <code>{px(sl)}</code>",
        f"TP1   <code>{px(tp1)}</code>  (R:R {plan.get('rr1', '—')}x)",
        f"TP2   <code>{px(tp2)}</code>  (R:R {plan.get('rr2', '—')}x)",
    ]

    if buy_pct := plan.get("buy_volume_pct"):
        lines.append(f"\nFlow: buy {buy_pct:.0f}% / sell {plan.get('sell_volume_pct', 0):.0f}%")
    if plan.get("macro_applied") and (macro := plan.get("macro_reason")):
        lines.append(f"Macro: {_html.escape(str(macro))[:140]}")

    # What the chart itself showed, so the plan and the picture are read together
    # rather than as two unrelated opinions.
    if vision is not None and (bias := str(vision.findings.get("bias") or "").strip()):
        agree = (bias.startswith("bull") and side == "long") or (
            bias.startswith("bear") and side == "short"
        )
        lines.append(
            f"Chart read: {_html.escape(bias)} — "
            + ("agrees with this plan." if agree else "differs from the live read; size down.")
        )

    lines += [
        f"\nSource: {_html.escape(str(plan.get('price_source') or 'live'))} · not executed.",
        f"To place it: <code>{_html.escape(str(plan.get('confirm_command') or ''))}</code>",
    ]
    return "\n".join(lines)[:3900]


async def _room_seats_reply() -> tuple[str, str, dict | None]:
    """/room agents — who sits on the board and what each seat last decided."""
    from app.agents import room as trading_room

    snap = trading_room.snapshot()
    ceo = snap.get("ceo") or {}
    lines = [f"🏛 <b>Trading room seats</b>{' — pinned: ' + ', '.join(snap.get('focus_symbols') or []) if snap.get('focus_symbols') else ''}", ""]

    def _seat(role_info: dict) -> str:
        state = role_info.get("state", "idle")
        icon = {"analyzing": "🔄", "presenting": "🗣", "resting": "✅", "error": "⚠️"}.get(state, "💤")
        name = html.escape(str(role_info.get("human_name") or role_info.get("role")))
        title = html.escape(str(role_info.get("title") or ""))
        line = f"{icon} <b>{name}</b> — {title} ({state})"
        last = role_info.get("last_decision") or {}
        if last:
            action = html.escape(str(last.get("action", "-")).upper())
            conf = last.get("confidence")
            try:
                conf_txt = f" · {float(conf)*100:.0f}%" if conf is not None else ""
            except (TypeError, ValueError):
                conf_txt = ""
            line += f"\n   └ {action}{conf_txt}: {html.escape(str(last.get('reasoning', ''))[:160])}"
        return line

    for agent in snap.get("agents", []):
        lines.append(_seat(agent))
    if ceo:
        lines.append("")
        lines.append(f"🏛 <b>{html.escape(str(ceo.get('human_name', 'CEO')))}</b> chairs.")
    if not snap.get("agents"):
        lines.append("No seats have convened yet — run <code>/room PAIR 1h</code> to wake the board.")
    return "\n".join(lines), "HTML", None


async def _room_status_reply(chat_id: str) -> tuple[str, str, dict | None]:
    """/room status — is a meeting running, what's pinned."""
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge
    from app.agents import room as trading_room

    busy = room_bridge.is_busy(chat_id)
    focus = trading_room.get_focus_symbols()
    lines = [
        "🏛 <b>Trading room status</b>",
        f"Meeting in progress here: {'yes — verdict coming' if busy else 'no'}",
        f"Pinned pairs: {', '.join(focus) if focus else 'none (free roaming)'}",
    ]
    running = [s for s in reversed(trading_room.snapshot().get("sessions", [])) if s.get("status") == "running"]
    if running:
        s0 = running[0]
        lines.append(
            f"Current session: <b>{html.escape(str(s0.get('symbol')))}</b> "
            f"{html.escape(str(s0.get('timeframe')))} — {len(s0.get('decisions') or [])} seat(s) reported"
        )
    return "\n".join(lines), "HTML", None


async def _room_focus_reply(args: str, chat_id: str) -> tuple[str, str, dict | None]:
    """/room focus BTCUSDT,XAUUSD — pin pairs; /room focus off resumes free roam."""
    from app.agents import room as trading_room

    if not args or args.lower() in {"off", "none", "clear"}:
        await trading_room.set_focus(None)
        return "🏛 Room unpinned — back to free roaming.", "HTML", None

    symbols = [s.strip().upper() for s in args.replace(",", " ").split() if s.strip()]
    if not symbols:
        return "Give me at least one pair, e.g. <code>/room focus BTCUSDT,XAUUSD</code>.", "HTML", None
    await trading_room.set_focus(symbols)
    shown = ", ".join(trading_room.get_focus_symbols())
    return f"📌 Pinned: <b>{shown}</b> — the board will keep returning to these.", "HTML", None


def _start_room(
    args: str,
    token: str,
    chat_id: str,
    *,
    image: tuple[bytes, str] | None = None,
) -> tuple[str, str, dict | None]:
    """Kick off a trading-room job and acknowledge immediately.

    The room is a seven-agent pipeline; the answer arrives as its own message
    when it is done. Returning right away is what keeps the bot responsive to
    everyone else in the meantime.
    """
    from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

    if not chat_id:
        return "❌ /room needs a chat to reply to.", "HTML", None
    if room_bridge.is_busy(chat_id):
        return (
            "🏛 The room is still sitting on your last request — "
            "I'll report back as soon as it finishes.",
            "HTML", None,
        )

    symbol, timeframe, question = room_bridge.parse_args(args)

    if image is None and symbol:
        room_bridge.spawn(
            room_bridge.run_pair(token, chat_id, symbol, timeframe), chat_id, token
        )
        return (
            f"🏛 Convening the trading room on <b>{symbol}</b> {timeframe}. "
            "I'll send the verdict and the chart when the agents are done.",
            "HTML", None,
        )

    if image is None and not question:
        return (
            "🏛 <b>/room</b> — put the trading room on something.\n\n"
            "• <code>/room BTCUSDT 4h</code> — full agent review + plan chart\n"
            "• <code>/room why is gold selling off?</code> — ask the agents\n"
            "• send a chart image with caption <code>/room</code> — read it, "
            "then review that pair",
            "HTML", None,
        )

    room_bridge.spawn(
        room_bridge.run_context(token, chat_id, question, image=image), chat_id, token
    )
    return (
        "🏛 Taking that to the trading room — I'll reply when the agents are done.",
        "HTML", None,
    )


async def _handle_photo(
    file_id: str,
    mime: str,
    caption: str,
    token: str,
    chat_id: str,
    db: AsyncSession,
) -> tuple[str | None, str, dict | None]:
    """Read an image the user sent, then answer about it with live context.

    The vision model's read is fed back through :func:`_ai_fallback` rather than
    returned directly, so a chart screenshot picks up the same live prices,
    news, learned history and Kronos forecast a typed question would — the
    instrument the model read off the chart is what drives that lookup.
    """
    from plugins.TelegramSignalNewsPlugin.backend.config import telegram_plugin_config as cfg
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service, vision_service

    if not cfg.vision_enabled:
        return "🖼 Image analysis is turned off.", "HTML", None

    # Downloading, reading and then enriching runs well past the point where
    # silence reads as a dead bot, and the transports only send what we return.
    # "/room" on a photo hands the whole thing to the agents instead: read the
    # chart, work out the pair, then convene the room on it.
    wants_room = caption.lstrip("/").strip().lower().startswith("room")

    if token and chat_id and not wants_room:
        await bot_service.send_message(token, chat_id, "🔍 Reading your image…")

    file_path = await bot_service.get_file(token, file_id)
    if not file_path:
        return "❌ Couldn't fetch that image from Telegram.", "HTML", None

    data = await bot_service.download_file(
        token, file_path, max_bytes=cfg.vision_max_image_bytes
    )
    if not data:
        return (
            "❌ Couldn't download that image — it may be too large. "
            "Try a smaller screenshot.",
            "HTML",
            None,
        )

    if wants_room:
        # Strip the command word; whatever follows is the question for the room.
        room_args = caption.lstrip("/").strip()[len("room"):].strip()
        return _start_room(room_args, token, chat_id, image=(data, mime))

    # Chart-first, caption overrides: a bare photo gets the chart prompt, a
    # caption becomes the question instead. A leading slash is stripped so
    # "/analyze this" reads as a question rather than a failed command.
    question = caption.lstrip("/").strip() or vision_service.DEFAULT_CHART_PROMPT

    vision = await vision_service.read_chart(data, mime, question, db)
    if not vision:
        return (
            "❌ I couldn't read that image — the vision model didn't respond. "
            "Try again in a moment.",
            "HTML",
            None,
        )
    read = vision.narrative

    # A chart is only worth marking up if we know which market it is: the plan
    # levels come from live data for that instrument, not from the picture.
    instrument = str(vision.findings.get("instrument") or "").strip()
    plan = await _live_plan(instrument) if instrument else None

    # Send the marked-up chart straight back, before the slower enrichment runs.
    # The overlay is drawn on the user's own screenshot, so the levels sit on the
    # real chart rather than on a regenerated lookalike.
    if token and chat_id and (vision.findings or plan):
        from plugins.AiMarketAnalyst.backend.services import chart_annotate

        if overlay := chart_annotate.annotate(data, vision.findings, plan=plan):
            bias = str(vision.findings.get("bias") or "").strip()
            overlay_caption = " ".join(
                p for p in (instrument, f"({bias} bias)" if bias else "") if p
            )
            await bot_service.send_photo(token, chat_id, overlay, caption=overlay_caption)

    # A live plan is a better answer than a paragraph — send it formatted, with
    # the reasoning behind it, instead of the raw analysis dump.
    if plan:
        card = _format_plan(instrument, plan, vision)
        if chat_id:
            _record_history(chat_id, "user", f"[image] {caption.strip() or 'sent a chart'}")
            _record_history(chat_id, "assistant", card)
        return card, "HTML", None

    # Hand the read to the normal chain. The prompt names it as a chart the user
    # sent so the model treats the description as observed fact, not as its own
    # speculation, and answers the caption rather than restating the image.
    asked = caption.lstrip("/").strip()
    prompt = (
        "I sent you an image. A vision model read it as follows:\n\n"
        f"{read}\n\n"
        + (
            f"My question about it: {asked}"
            if asked
            else "Analyse it as a trading chart: confirm the levels against live "
                 "price where you can, and give me the bias and any entry worth taking."
        )
    )

    reply, mode, markup = await _ai_fallback(prompt, db, chat_id=chat_id)
    if reply:
        return reply, mode, markup

    # The enrichment failed but the read is still worth having.
    if chat_id:
        _record_history(chat_id, "user", f"[image] {asked or 'sent a chart'}")
        _record_history(chat_id, "assistant", read)
    return format_for_telegram(read, limit=4000), "HTML", None


# ── AI fallback ───────────────────────────────────────────────────────────────

async def _ai_fallback(
    text: str,
    db: AsyncSession,
    *,
    chat_id: str = "",
    hint: str = "",
) -> tuple[str | None, str, dict | None]:
    """Route unrecognised free-text through Jarvis AI.

    Strategy:
    1. Try execute_command first — handles natural-language trading commands
       (e.g. "close my BTC", "what are my positions") without needing a slash.
    2. If Jarvis doesn't recognise it (action == 'unknown') or fails, fall back
       to a full AI chat with the same live context the web chat gets: prices
       for whatever instruments the message names (any asset class), news and
       web search, learned trade history, and the fetch tools. Telegram used to
       send a bare persona with no data at all, which is why it refused
       questions the browser answered fine.

    ``hint`` carries an upstream failure (e.g. a symbol that wouldn't resolve)
    so the model can explain it rather than repeat it.
    """
    # ── Step 0: A prediction question runs the forecaster ─────────────────────
    # Ahead of the generic dispatcher, because "how can we predict bitcoin" is
    # a question this platform answers by *running* Kronos, not by describing
    # which models exist.
    request = _forecast_request(text, chat_id)
    if request is not None:
        reply, mode, markup = await _forecast_reply(
            text, request[0], request[1], db, chat_id=chat_id
        )
        if reply:
            return reply, mode, markup

    # ── Step 1: Try structured Jarvis command dispatch ────────────────────────
    try:
        from app.api.jarvis import execute_command, CommandRequest
        cmd_result = await execute_command(CommandRequest(command=text))
        if cmd_result.action not in ("unknown", None) and cmd_result.ok:
            # Use full detail for analysis-type actions, speech for everything else
            if cmd_result.action in ("analyze", "news_position_analysis") and cmd_result.detail:
                body = cmd_result.detail
            else:
                body = cmd_result.speech or cmd_result.detail or ""
            if body:
                reply = f"✅ {body}"
                if chat_id:
                    _record_history(chat_id, "user", text)
                    _record_history(chat_id, "assistant", reply)
                    _remember_offer(chat_id, reply)
                return reply[:4000], "HTML", None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI] execute_command pre-check failed: {}", exc)

    # ── Step 2: Full AI chat with live prices, news, memory and fetch tools ───
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            chat_turn, resolve_chat_route,
        )

        # Auto-fetch live portfolio data when the message is about positions
        live_data: str | None = None
        if _wants_position_analysis(text):
            try:
                live_data = await _fetch_live_position_context(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AI] Live position context fetch failed: {}", exc)

        # Live prices for whatever the message names — crypto, FX, metals,
        # indices, energy. Gathered concurrently so the reply stays prompt.
        price_blk: str | None = None
        news_blk: str | None = None
        learned: str | None = None
        fib_blk: str | None = None
        try:
            from app.services import market_data

            symbols = market_data.extract_symbols(text)
            price_blk, news_blk, learned, fib_blk = await asyncio.gather(
                market_data.price_block(symbols, db=db, max_lines=25),
                _news_context(text, symbols),
                _learned_context(db, symbols),
                _fib_context(symbols),
                return_exceptions=False,
            )
        except Exception as exc:  # noqa: BLE001 — context is a bonus, not a gate
            logger.warning("[AI] live context fetch failed: {}", exc)

        history = _get_history(chat_id) if chat_id else []
        system = _jarvis_system_prompt(
            live_data, price_block=price_blk, news_block=news_blk, learned=learned,
            fib_block=fib_blk,
        )
        if hint:
            system += (
                f"\n\n## Note\nA structured command for this message failed with: "
                f"{hint}\nAnswer the user's question directly using the live data "
                "above or the fetch tools. Do not repeat that error, and do not "
                "claim the instrument is unsupported."
            )
        messages = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": text},
        ]

        # Match the model to what was asked: a real analysis request is worth
        # waiting longer for, a one-line question is not. The route is resolved
        # against the providers this surface can actually reach — pinning the
        # deep chain blind used to name a model that lives on a profile
        # dedicated to another task, so every provider in the pool 400'd on it
        # in turn before anything answered.
        route = await resolve_chat_route(
            db, task="telegram_chat", text=text, surface="telegram"
        )
        logger.info(
            "[AI] telegram turn routed {} (model={})",
            route.label, route.model or "provider default",
        )

        # Budgets come from the route: Telegram shows no streaming or progress,
        # so its ceiling is tighter than the web chat's — a silent 25s wait
        # reads as a broken bot. The Telegram surface can be given a profile of
        # its own; unset, it falls through to any available provider (or, on a
        # deep turn, to the profile reserved for reasoning), so nothing has to
        # be configured for the bot to keep answering.
        resp = await chat_turn(
            db,
            messages,
            route=route,
            surface_task="telegram_chat",
            json_mode=False,
            agent_name="jarvis-telegram-chat",
            source="telegram",
        )

        if resp.get("ok") and resp.get("content"):
            reply = str(resp["content"]).strip()
            # A forecasting answer must carry the engine's numbers, whether or
            # not the model thought to ask for them.
            display, markup = await _attach_forecast(reply, text, db, chat_id=chat_id)
            if chat_id:
                # History keeps the raw text: the model should see what it
                # actually said, not the HTML the client was shown.
                _record_history(chat_id, "user", text)
                _record_history(chat_id, "assistant", reply)
                # If the reply offered a command, a plain "yes" now runs it.
                _remember_offer(chat_id, reply)
            # Models answer maths in LaTeX and comparisons in markdown tables.
            # Sent raw under parse_mode=HTML those arrive as backslashes, literal
            # ** asterisks and pipe rows, and the old reply[:3800] cut the last
            # answer off mid-word. _attach_forecast has already run the text
            # through format_for_telegram, which fixes all three.
            return display, "HTML", markup

        if resp.get("error"):
            logger.warning("[AI] Jarvis chat returned error: {}", resp["error"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AI] Jarvis chat fallback failed: {}", exc)

    return None, "HTML", None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm_sym(symbol: str) -> str:
    """Convert BTCUSDT → BTC/USDT; pass-through BTC/USDT."""
    s = symbol.upper().strip()
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return s


def _fmt_p(v: float) -> str:
    """Compact price formatter: 65000 → '65,000.00', 0.0012 → '0.001200'."""
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


#: Timeframes Kronos accepts. Used to tell a timeframe argument from an exchange
#: one, so the two can be given in either order.
_TIMEFRAMES = frozenset({
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w",
})


def _parse_exchange_timeframe(
    tokens: list[str], *, default_exchange: str = "bitget", default_tf: str = "1h",
) -> tuple[str, str]:
    """Split trailing `/forecast` arguments into (exchange, timeframe).

    Both are optional and order-independent, because the documented positional
    form ate the common case: `/forecast GBPUSD 4h` read "4h" as the *exchange*
    and silently forecast the default 1h instead. A token that names a timeframe
    is one; anything else is the exchange.
    """
    exchange, timeframe = default_exchange, default_tf
    for tok in tokens:
        t = tok.lower().strip()
        if not t:
            continue
        if t in _TIMEFRAMES:
            timeframe = t
        else:
            exchange = t
    return exchange, timeframe


# ── Kronos Forecast ───────────────────────────────────────────────────────────

async def _handle_forecast(args: str, db: AsyncSession) -> tuple[str, str, dict | None]:
    """Kronos ML forecast + Jarvis AI narrative + clickable sniper entries.

    Usage: /forecast <SYMBOL> [exchange] [timeframe]
    Examples: /forecast BTC  |  /forecast BTCUSDT bitget 4h
    """
    tokens = args.split()
    if not tokens:
        return (
            "❓ Usage: /forecast &lt;SYMBOL&gt; [exchange] [timeframe]\n"
            "Examples: /forecast BTC  |  /forecast BTCUSDT bitget 4h"
        ), "HTML", None

    raw_sym = tokens[0]
    exchange, timeframe = _parse_exchange_timeframe(tokens[1:])
    symbol = _norm_sym(raw_sym)

    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            run_forecast_cached,
            generate_sniper_signals,
        )
        from plugins.KronosForecastPlugin.backend.services.jarvis_analysis import (
            analyze_forecast,
        )
    except ImportError as ie:
        return f"⚠️ KronosForecastPlugin not installed: {ie}", "HTML", None

    # ── Run forecast (cached) ──────────────────────────────────────────────────
    try:
        forecast_resp = await run_forecast_cached(exchange, symbol, timeframe)
    except Exception as exc:  # noqa: BLE001
        return f"❌ Forecast failed: {exc}", "HTML", None

    # ── Build sniper signals via public API (uses same cache hit) ─────────────
    try:
        sniper_resp = await generate_sniper_signals(exchange, symbol, timeframe)
        signals = sniper_resp.signals
    except Exception:  # noqa: BLE001
        signals = []

    sig = forecast_resp.signal
    dir_emoji = {"up": "🟢📈", "down": "🔴📉", "flat": "➡️"}.get(
        sig.direction if sig else "flat", "➡️"
    )
    conf_pct = int((sig.confidence if sig else 0) * 100)
    pct_chg = sig.pct_change if sig else 0.0
    direction = (sig.direction if sig else "flat").upper()

    # For forex/metals: replace the stale candle-close anchor with the live
    # Swissquote BBO mid price so "Now:" always reflects the tradeable market.
    _model_anchor = forecast_resp.anchor_price
    _live_now: float = _model_anchor
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.forex_price_service import (
            get_forex_price as _get_sq_price,
            is_forex_pair as _is_fx_pair,
        )
        if _is_fx_pair(symbol):
            _sq = await _get_sq_price(symbol)
            if _sq and _sq > 0:
                _live_now = float(_sq)
    except Exception:  # noqa: BLE001 — fall back to candle anchor silently
        pass

    # Scale all model price levels by (live / model_anchor) so percentages and
    # R:R ratios stay consistent with where price actually is right now.
    _scale = (_live_now / _model_anchor) if _model_anchor > 0 else 1.0
    _display_target = ((sig.target_price * _scale) if sig and sig.target_price else None)
    target = _fmt_p(_display_target) if _display_target else "—"

    lines: list[str] = [
        f"🔮 <b>Kronos Forecast — {symbol} ({timeframe})</b>",
        f"Engine: <code>{forecast_resp.engine}</code>",
        "",
        f"{dir_emoji} <b>{direction}  |  {pct_chg:+.2f}%  |  {conf_pct}% confidence</b>",
        f"Now: <code>{_fmt_p(_live_now)}</code>  →  "
        f"Target: <code>{target}</code>",
    ]
    if abs(_scale - 1.0) > 0.001:  # note when Swissquote price was used
        lines.append(f"<i>(model anchor: {_fmt_p(_model_anchor)} → Swissquote live: {_fmt_p(_live_now)})</i>")
    if forecast_resp.note:
        lines.append(f"⚠️ <i>{forecast_resp.note}</i>")

    # ── Macro context ─────────────────────────────────────────────────────────
    # The dollar/VIX read that shaped this confidence, pulled out of the signal
    # rationale so the card states it rather than burying it in the narrative.
    macro_lines = [
        r for r in (getattr(sig, "rationale", None) or [])
        if "DXY" in r or "VIX" in r or r.startswith("Macro")
    ]
    if macro_lines:
        lines.append("")
        lines.append("🌍 <b>Macro</b>")
        for line in macro_lines[:3]:
            lines.append(f"   <i>{_esc(_clip(line, 180))}</i>")

    # ── Jarvis AI narrative (best-effort — never blocks the forecast) ──────────
    jarvis = None
    try:
        jarvis = await analyze_forecast(forecast_resp, learn=False)
    except Exception as jexc:  # noqa: BLE001
        logger.debug("[ForecastCmd] Jarvis analysis failed: {}", jexc)

    if jarvis:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Market cap context
        mkt = jarvis.market
        if mkt and mkt.is_crypto:
            mkt_parts: list[str] = []
            if mkt.market_cap_rank:
                mkt_parts.append(f"Rank #{mkt.market_cap_rank}")
            if mkt.market_cap:
                cap_b = mkt.market_cap / 1e9
                mkt_parts.append(f"MCap ${cap_b:.2f}B" if cap_b >= 1 else f"MCap ${mkt.market_cap / 1e6:.1f}M")
            if mkt.volume_24h:
                vol_m = mkt.volume_24h / 1e6
                mkt_parts.append(f"Vol ${vol_m:.1f}M/24h")
            if mkt_parts:
                lines.append("📊 " + "  |  ".join(mkt_parts))

        # Open position context
        pos = jarvis.position
        if pos:
            pnl_sign = "🟢" if pos.pnl >= 0 else "🔴"
            lines.append(
                f"📈 Open {pos.side.upper()}: {pos.size} @ {_fmt_p(pos.entry_price)}"
                f"  {pnl_sign} PnL {pos.pnl:+.2f} ({pos.pnl_pct:+.1f}%)"
            )

        # Core narrative — max 1 100 chars so rest of message fits in Telegram limit
        lines.append("")
        lines.append("🤖 <b>JARVIS Analysis</b>")
        # The narrative is model-written prose: it arrives with **markdown**
        # asterisks and bare "&" and "<", which show up literally and can make
        # Telegram reject the whole message under parse_mode=HTML.
        raw_analysis = (jarvis.analysis or "").strip()
        analysis_text = format_for_telegram(raw_analysis, limit=1100)
        if len(raw_analysis) > 1100:
            analysis_text += " … <i>(tap Analyze for full)</i>"
        lines.append(f"<i>{analysis_text}</i>")

        # Position advice if present
        if jarvis.position_advice and pos:
            adv = format_for_telegram(jarvis.position_advice.strip(), limit=300)
            lines.append("")
            lines.append(f"💡 <b>Position Advice:</b> <i>{adv}</i>")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    else:
        # Jarvis unavailable — show the signal summary if we have one
        if sig and sig.summary:
            lines += ["", f"🤖 <i>{sig.summary}</i>"]

    # ── Sniper entries ─────────────────────────────────────────────────────────
    inline_keyboard: list[list[dict]] = []

    # FX, metals and indices forecast fine (Yahoo prices them and CME volume
    # backs the gate) but Bitget lists none of them, so their entries are
    # analysis only — rendering an execute button would offer an unfillable
    # order. See KronosForecastPlugin.forecast_service.is_order_placeable.
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            is_order_placeable,
        )
        placeable = is_order_placeable(symbol)
    except Exception:  # noqa: BLE001 — never break the forecast over this
        placeable = True

    if signals:
        header = (
            "⚡ <b>Sniper Entries</b>  (tap a button to execute)" if placeable
            else "⚡ <b>Sniper Entries</b>  (analysis only — see note below)"
        )
        lines += ["", header]
        for i, s in enumerate(signals[:4], 1):
            side_emoji = "🟢 LONG" if s.side == "long" else "🔴 SHORT"
            kind_label = "Market" if s.order_kind == "market" else "Limit"
            entry_str = _fmt_p(s.entry * _scale)
            sl_str = _fmt_p(s.stop_loss * _scale)
            tp_str = _fmt_p(s.take_profit_1 * _scale)
            lines.append(
                f"[{i}] {side_emoji} {kind_label} @ <code>{entry_str}</code>"
                f"  SL <code>{sl_str}</code>  TP1 <code>{tp_str}</code>"
                f"  R:R {s.risk_reward}  {s.leverage}x"
            )

            if not placeable:
                continue

            # Inline keyboard — Row 1: quick $5 execute (paper + live)
            cb_base = f"cq_order:{s.side}:{s.order_kind}:{symbol}:5"
            paper_btn = {
                "text": f"[{i}] {side_emoji.split()[1]} {kind_label} — $5 Paper",
                "callback_data": cb_base,
            }
            live_btn = {
                "text": "🔴 LIVE $5",
                "callback_data": f"{cb_base}:live",
            }
            # Row 2: custom amount helper
            kind_flag = "limit " if s.order_kind == "limit" else ""
            custom_btn = {
                "text": f"✏️ Custom amount",
                "callback_data": f"cq_custom_order:{s.side}:{s.order_kind}:{symbol}",
            }
            inline_keyboard.append([paper_btn, live_btn, custom_btn])

        if not placeable:
            lines.append("")
            lines.append(
                f"ℹ️ <i>{symbol} is not listed on {exchange} — these levels are "
                f"analysis only. Place the trade with your FX/CFD broker "
                f"(e.g. MT5).</i>"
            )
    else:
        lines.append("")
        # A pair whose feed carries no volume at all (the cash indices — GER40,
        # UK100, FRA40 … — which Yahoo lists no future for) fails the hard
        # volume gate on *every* timeframe, so "try a different timeframe" sent
        # the user round a loop that cannot terminate. Technical analysis needs
        # no volume, so /analyze still answers in full — point there instead.
        volume_gated = (
            forecast_resp.engine == "unavailable"
            and "volume" in (forecast_resp.note or "").lower()
        )
        if volume_gated:
            lines.append(
                f"ℹ️ No entries: {symbol}'s feed carries no traded volume, which "
                f"Kronos requires. No timeframe changes that.\n"
                f"Technical analysis needs no volume — use "
                f"<code>/analyze {raw_sym}</code> for EMA/RSI/ATR, swing levels "
                f"and entry/stop/target."
            )
        else:
            lines.append(
                f"ℹ️ No sniper entries at {timeframe} — try another timeframe, or "
                f"<code>/analyze {raw_sym}</code> for the technical read."
            )

    # ── Jarvis full-analysis button ────────────────────────────────────────────
    inline_keyboard.append([{
        "text": "🤖 Full Jarvis Analysis",
        "callback_data": f"cq_analyze:{symbol}",
    }])

    reply_markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None
    return "\n".join(lines)[:4096], "HTML", reply_markup


# ── Order execution from Kronos sniper signal ─────────────────────────────────

async def _handle_order(args: str, db: AsyncSession) -> tuple[str, str]:
    """Execute a Kronos sniper signal as a paper or live trade.

    Usage:
      /order long BTCUSDT 100              → paper, market long, $100 margin
      /order live long BTCUSDT 100         → LIVE Bitget futures order
      /order live limit short BTCUSDT 50   → live, limit short, $50
      /order long BTCUSDT 100 bitget 20x   → paper, 20x leverage override
    """
    if not args:
        return (
            "❓ Usage: /order [live] [limit] &lt;long|short&gt; &lt;SYMBOL&gt; &lt;margin_usd&gt;\n"
            "Default: paper mode, market order\n"
            "Example: /order long BTCUSDT 100\n"
            "Live:    /order live long BTCUSDT 100"
        ), "HTML"

    tokens = args.lower().split()

    # parse flags
    live_mode = False
    order_kind = "market"
    idx = 0
    while idx < len(tokens) and tokens[idx] in ("live", "paper", "limit", "market"):
        if tokens[idx] == "live":
            live_mode = True
        elif tokens[idx] == "limit":
            order_kind = "limit"
        idx += 1

    if idx + 2 >= len(tokens):
        return "❓ Need: [live] [limit] &lt;long|short&gt; &lt;SYMBOL&gt; &lt;margin_usd&gt;", "HTML"

    side_raw = tokens[idx]; idx += 1
    if side_raw not in ("long", "short", "buy", "sell"):
        return f"❓ Unknown side '{side_raw}'. Use long or short.", "HTML"
    side = "long" if side_raw in ("long", "buy") else "short"
    bs = "buy" if side == "long" else "sell"

    raw_sym = tokens[idx].upper(); idx += 1
    try:
        margin_usd = float(tokens[idx]) if idx < len(tokens) else 100.0; idx += 1
    except (ValueError, IndexError):
        return "❓ margin_usd must be a number. E.g.: /order long BTCUSDT 100", "HTML"

    exchange = tokens[idx].lower() if idx < len(tokens) and tokens[idx].isalpha() else "bitget"; idx += 1
    leverage_override = None
    if idx < len(tokens):
        m = re.match(r"^(\d+)x?$", tokens[idx])
        if m:
            leverage_override = int(m.group(1))

    symbol = _norm_sym(raw_sym)
    compact = symbol.replace("/", "")

    # ── Tradability guard ─────────────────────────────────────────────────────
    # Kronos forecasts FX, metals and indices, so a sniper entry can legitimately
    # exist for GBP/USD — but no crypto connector lists it. Refuse here rather
    # than sizing a position and firing an order the exchange cannot fill. This
    # is the single choke point: typed /order and every inline button land here.
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            is_order_placeable,
        )
        if not is_order_placeable(symbol):
            return (
                f"🚫 <b>{symbol} cannot be ordered here.</b>\n"
                f"{exchange} lists crypto USDT futures only — FX, metals and "
                f"indices are forecast-only.\n\n"
                f"Use <code>/forecast {raw_sym}</code> for the levels and place "
                f"the trade with your FX/CFD broker."
            ), "HTML"
    except ImportError:
        pass  # plugin missing is reported by the call below

    # ── Get sniper signals to find entry/SL/TP ────────────────────────────────
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            generate_sniper_signals,
        )
        resp = await generate_sniper_signals(exchange, symbol, "1h")
    except ImportError:
        return "⚠️ KronosForecastPlugin not installed.", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Kronos error: {exc}", "HTML"

    # Find the matching signal (matching side + order_kind, else matching side)
    match = next(
        (s for s in resp.signals if s.side == side and s.order_kind == order_kind),
        next((s for s in resp.signals if s.side == side), None),
    )

    if match:
        entry = match.entry
        sl = match.stop_loss
        tp = match.take_profit_1
        leverage = leverage_override or match.leverage
    else:
        # No pre-built sniper signal — check if forecast direction at least aligns
        expected_side = "short" if resp.direction == "down" else ("long" if resp.direction == "up" else None)
        if expected_side and expected_side != side:
            # Direction opposes the requested side — warn the user
            return (
                f"⚠️ Kronos direction is <b>{resp.direction.upper()}</b> — suggests <b>{expected_side.upper()}</b>, not {side.upper()}.\n\n"
                f"Execute the aligned direction:\n"
                f"<code>/order {expected_side} {raw_sym} {margin_usd}</code>"
            ), "HTML"
        # Direction aligns (or flat) but no pre-built signal — synthesize from anchor price
        entry = resp.anchor_price
        sl_pct = 0.015  # default 1.5% stop loss
        tp_pct = 0.030  # default 3.0% take profit
        if side == "short":
            sl = round(entry * (1 + sl_pct), 8)
            tp = round(entry * (1 - tp_pct), 8)
        else:
            sl = round(entry * (1 - sl_pct), 8)
            tp = round(entry * (1 + tp_pct), 8)
        leverage = leverage_override or 10

    # Size: floor to 3dp (conservative, never exceed margin)
    prec = 3
    raw_size = (margin_usd * leverage) / entry
    size = math.floor(raw_size * 10 ** prec) / 10 ** prec
    if size <= 0:
        return "❌ Computed size is 0 — increase margin or reduce precision.", "HTML"

    sl_pct = abs(entry - sl) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100
    mode_label = "🔴 LIVE" if live_mode else "📄 Paper"

    if not live_mode:
        # Paper trade via SimulationEngine
        try:
            from app.trading.simulation import SimulationEngine
            result = await SimulationEngine.place_order(
                db=db,
                symbol=symbol,
                side=bs,
                amount=size,
                price=entry,
                order_type=order_kind,
                stop_loss=sl,
                take_profit=tp,
                trade_type="futures",
                leverage=leverage,
                margin_mode="isolated",
            )
            if not result.get("success", True):
                return f"❌ Paper order failed: {result.get('error', 'unknown')}", "HTML"
        except Exception as exc:  # noqa: BLE001
            return f"❌ Paper order error: {exc}", "HTML"

        return (
            f"✅ {mode_label} order placed!\n\n"
            f"<b>{side.upper()} {symbol}</b> @ {_fmt_p(entry)}\n"
            f"Size: <code>{size}</code>  Leverage: {leverage}x\n"
            f"Margin: ${margin_usd:.2f}  Notional: ${size * entry:,.2f}\n"
            f"SL: {_fmt_p(sl)} ({sl_pct:.1f}%)  TP: {_fmt_p(tp)} ({tp_pct:.1f}%)\n"
            f"R:R ≈ {match.risk_reward}  Engine: {resp.engine}"
        ), "HTML"

    # ── LIVE order via exchange connector ─────────────────────────────────────
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange
        connector = exchange_manager.get_exchange(SupportedExchange(exchange))
        if connector is None:
            return f"❌ Exchange '{exchange}' not configured.", "HTML"

        result = await connector.create_futures_order(
            symbol=compact,
            margin_coin="USDT",
            side=bs,
            order_type=order_kind,
            size=str(size),
            price=str(entry) if order_kind == "limit" else None,
            margin_mode="isolated",
            leverage=leverage,
            trade_side="open",
            product_type="USDT-FUTURES",
            stop_loss=sl,
            take_profit=tp,
        )
        order_id = result.get("orderId") or result.get("order_id") or "—"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Live order failed: {exc}", "HTML"

    return (
        f"✅ {mode_label} order sent!\n\n"
        f"<b>{side.upper()} {symbol}</b> @ {_fmt_p(entry)}\n"
        f"Size: <code>{size}</code>  Leverage: {leverage}x\n"
        f"Margin: ${margin_usd:.2f}  Order ID: <code>{order_id}</code>\n"
        f"SL: {_fmt_p(sl)} ({sl_pct:.1f}%)  TP: {_fmt_p(tp)} ({tp_pct:.1f}%)\n"
        f"R:R ≈ {match.risk_reward}  Engine: {resp.engine}"
    ), "HTML"


# ── Analyze (deep Jarvis) ─────────────────────────────────────────────────────

async def _handle_analyze(args: str, db: AsyncSession) -> tuple[str, str]:
    """Deep Jarvis analysis: Kronos + AI narrative + news + open position.

    Usage: /analyze BTCUSDT
    """
    if not args:
        return "❓ Usage: /analyze &lt;SYMBOL&gt;  e.g. /analyze BTCUSDT", "HTML"
    return await _jarvis_command(f"analyze {args}", db)


# ── MT5 commands ──────────────────────────────────────────────────────────────

def _enum_val(v) -> str:
    """Render an enum/str consistently (MT5AccountType.LIVE -> 'live')."""
    if v is None:
        return ""
    return str(getattr(v, "value", v))


async def _handle_mt5(args: str, db: AsyncSession) -> tuple[str, str]:
    """MT5 account, position, and ScalpBot control.

    Subcommands:
      (none) / status           — list MT5 accounts + live equity
      positions [account_id]    — open MT5 positions
      orders [account_id]       — pending MT5 orders
      scalp status [account_id] — ScalpBot session status
      scalp start <id> <sym>    — start ScalpBot
      scalp stop <id>           — stop ScalpBot
      close <ticket> <id>       — close MT5 position by ticket
    """
    tokens = args.strip().lower().split()
    sub = tokens[0] if tokens else "status"

    if sub in ("", "status"):
        return await _mt5_status(db)
    if sub == "positions":
        aid = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else None
        return await _mt5_positions(aid, db)
    if sub == "orders":
        aid = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else None
        return await _mt5_orders(aid, db)
    if sub == "scalp":
        subsub = tokens[1] if len(tokens) > 1 else "status"
        if subsub == "status":
            aid = int(tokens[2]) if len(tokens) > 2 and tokens[2].isdigit() else None
            return await _mt5_scalp_status(aid, db)
        if subsub == "start":
            if len(tokens) < 4:
                return "❓ Usage: /mt5 scalp start &lt;account_id&gt; &lt;SYMBOL&gt;", "HTML"
            aid, sym = int(tokens[2]), tokens[3].upper()
            return await _mt5_scalp_start(aid, sym, db)
        if subsub == "stop":
            if len(tokens) < 3:
                return "❓ Usage: /mt5 scalp stop &lt;account_id&gt;", "HTML"
            aid = int(tokens[2])
            return await _mt5_scalp_stop(aid, db)
        return "❓ Usage: /mt5 scalp &lt;status|start|stop&gt;", "HTML"
    if sub == "close":
        if len(tokens) < 3:
            return "❓ Usage: /mt5 close &lt;ticket&gt; &lt;account_id&gt;", "HTML"
        ticket_raw, aid = tokens[1], int(tokens[2])
        return await _mt5_close(ticket_raw, aid, db)
    return (
        "❓ Usage: /mt5 &lt;status|positions|orders|scalp|close&gt;\n"
        "Examples:\n"
        "  /mt5 status\n"
        "  /mt5 positions 5\n"
        "  /mt5 scalp start 5 EURUSD\n"
        "  /mt5 scalp stop 5\n"
        "  /mt5 close 12345 5"
    ), "HTML"


async def _mt5_status(db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        from plugins.MT5TradingPlugin.backend.services.mt5_client import (
            mt5_client, normalize_order_type,
        )

        rows = (await db.execute(_sel(MT5Account))).scalars().all()
        if not rows:
            return "📭 No MT5 accounts configured.", "HTML"

        lines = ["🖥 <b>MT5 Accounts</b>", ""]
        for a in rows:
            status_val = _enum_val(getattr(a, "status", "")).lower()
            badge = "🟢" if status_val in ("active", "connected") else "⚪"
            acct_type = _enum_val(getattr(a, "account_type", "")) or "demo"
            reach = "" if getattr(a, "api_reachable", False) else " · unreachable"
            lines.append(
                f"{badge} [{a.id}] <b>{a.name or a.login}</b> — {acct_type}{reach}"
            )
            creds = (a.login, a.server, a.password_encrypted)
            try:
                info = await mt5_client.get_account_info(*creds)
                if info:
                    eq = float(info.get("equity") or info.get("balance") or 0)
                    bal = float(info.get("balance") or 0)
                    lines.append(
                        f"     Equity: {eq:.2f}  Balance: {bal:.2f}  "
                        f"{info.get('currency', '')}".rstrip()
                    )
            except Exception:  # noqa: BLE001
                lines.append("     ⚠️ live equity unavailable")

            # ── Active trades (open positions) ──
            try:
                positions = await mt5_client.get_positions(*creds)
            except Exception:  # noqa: BLE001
                positions = None
            if positions is None:
                lines.append("     📈 positions unavailable")
            elif not positions:
                lines.append("     📈 No open positions")
            else:
                tot = sum(float(p.get("profit", 0) or 0) for p in positions)
                tot_e = "🟢" if tot >= 0 else "🔴"
                lines.append(f"     📈 <b>{len(positions)} open</b>  {tot_e} PnL {tot:+.2f}")
                for p in positions[:5]:
                    pnl = float(p.get("profit", 0) or 0)
                    side = (normalize_order_type(p) or str(p.get("type", ""))).upper()
                    vol = p.get("lots", p.get("volume", "?"))
                    open_px = p.get("openPrice", p.get("price_open", "?"))
                    cur_px = p.get("currentPrice", p.get("price_current", "?"))
                    lines.append(
                        f"        {'🟢' if pnl >= 0 else '🔴'} {p.get('symbol', '?')} "
                        f"{side} {vol} @ {open_px} → {cur_px}  {pnl:+.2f}  #{p.get('ticket', '?')}"
                    )
                if len(positions) > 5:
                    lines.append(f"        …+{len(positions) - 5} more")

            # ── Pending orders ──
            try:
                pend = await mt5_client.get_pending_orders(*creds)
            except Exception:  # noqa: BLE001
                pend = None
            if pend is None:
                lines.append("     📋 pending unavailable")
            elif not pend:
                lines.append("     📋 No pending orders")
            else:
                lines.append(f"     📋 <b>{len(pend)} pending</b>")
                for o in pend[:5]:
                    otype = (normalize_order_type(o) or str(o.get("orderType", "?"))).upper()
                    vol = o.get("lots", o.get("volume", "?"))
                    px = o.get("openPrice", o.get("price", "?"))
                    lines.append(
                        f"        • {o.get('symbol', '?')} {otype} {vol} @ {px}  "
                        f"SL {o.get('stopLoss', 0)}  TP {o.get('takeProfit', 0)}  "
                        f"#{o.get('ticket', '?')}"
                    )
                if len(pend) > 5:
                    lines.append(f"        …+{len(pend) - 5} more")
            lines.append("")
        return "\n".join(lines).rstrip(), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ MT5 status failed: {exc}", "HTML"


async def _mt5_positions(account_id, db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        account = (
            await db.get(MT5Account, account_id) if account_id
            else (await db.execute(_sel(MT5Account).limit(1))).scalars().first()
        )
        if not account:
            return "❌ No MT5 account found.", "HTML"

        from plugins.MT5TradingPlugin.backend.services.mt5_client import normalize_order_type

        positions = await mt5_client.get_positions(
            account.login, account.server, account.password_encrypted
        )
        if not positions:
            return f"📭 No open positions on account {account.id}.", "HTML"

        lines = [f"📈 <b>MT5 Positions — Account {account.id}</b>", ""]
        for p in positions[:10]:
            pnl = float(p.get("profit", 0) or 0)
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            side = (normalize_order_type(p) or str(p.get("type", ""))).upper()
            vol = p.get("lots", p.get("volume", "?"))
            open_px = p.get("openPrice", p.get("price_open", "?"))
            cur_px = p.get("currentPrice", p.get("price_current", "?"))
            lines.append(
                f"{pnl_e} <b>{p.get('symbol', '?')}</b> {side} {vol}\n"
                f"   Open: {open_px}  Cur: {cur_px}\n"
                f"   SL: {p.get('stopLoss', 0)}  TP: {p.get('takeProfit', 0)}\n"
                f"   PnL: {pnl:+.2f}  Ticket: {p.get('ticket', '?')}"
            )
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ MT5 positions failed: {exc}", "HTML"


async def _mt5_orders(account_id, db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        account = (
            await db.get(MT5Account, account_id) if account_id
            else (await db.execute(_sel(MT5Account).limit(1))).scalars().first()
        )
        if not account:
            return "❌ No MT5 account found.", "HTML"

        from plugins.MT5TradingPlugin.backend.services.mt5_client import normalize_order_type

        orders = await mt5_client.get_pending_orders(
            account.login, account.server, account.password_encrypted
        )
        if not orders:
            return f"📋 No pending orders on account {account.id}.", "HTML"

        lines = [f"📋 <b>MT5 Pending Orders — Account {account.id}</b>", ""]
        for o in orders[:10]:
            otype = (normalize_order_type(o) or str(o.get("orderType", "?"))).upper()
            vol = o.get("lots", o.get("volume", "?"))
            px = o.get("openPrice", o.get("price", "?"))
            lines.append(
                f"• <b>{o.get('symbol', '?')}</b> {otype} "
                f"vol {vol} @ {px}\n"
                f"  SL: {o.get('stopLoss', 0)}  TP: {o.get('takeProfit', 0)}  "
                f"Ticket: {o.get('ticket', '?')}"
            )
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ MT5 orders failed: {exc}", "HTML"


async def _mt5_scalp_status(account_id, db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel, desc as _desc
        from plugins.MT5TradingPlugin.backend.models import MT5ScalpSession

        q = _sel(MT5ScalpSession).order_by(_desc(MT5ScalpSession.created_at)).limit(5)
        if account_id:
            q = q.where(MT5ScalpSession.account_id == account_id)
        sessions = (await db.execute(q)).scalars().all()

        if not sessions:
            aid_str = f" for account {account_id}" if account_id else ""
            return f"📭 No ScalpBot sessions{aid_str}.", "HTML"

        lines = ["🤖 <b>ScalpBot Sessions</b>", ""]
        for s in sessions:
            status_val = str(s.status.value if hasattr(s.status, "value") else s.status).lower()
            status_emoji = {
                "active": "🟢", "paused": "🟡", "stopped": "⚫",
                "completed": "✅", "error": "🔴",
            }.get(status_val, "⚪")
            pnl = float(s.session_pnl or 0)
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{status_emoji} [{s.id}] Acct {s.account_id}  {s.symbol}\n"
                f"   Phase: {s.phase or '—'}  {pnl_e} PnL: {pnl:+.4f}\n"
                f"   Wins: {s.wins or 0}  Losses: {s.losses or 0}"
            )
        return "\n".join(lines), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ ScalpBot status failed: {exc}", "HTML"


async def _mt5_scalp_start(account_id: int, symbol: str, db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import (
            MT5ScalpSession, MT5ScalpSessionStatus, MT5Account,
        )
        from plugins.MT5TradingPlugin.backend.services.scalp_bot_service import scalp_bot_manager

        account = await db.get(MT5Account, account_id)
        if not account:
            return f"❌ MT5 account {account_id} not found.", "HTML"

        # Reuse existing active session or create a new one
        existing = (await db.execute(
            _sel(MT5ScalpSession)
            .where(MT5ScalpSession.account_id == account_id)
            .where(MT5ScalpSession.symbol == symbol)
            .where(MT5ScalpSession.status == MT5ScalpSessionStatus.active)
        )).scalars().first()

        if existing:
            session_id = existing.id
        else:
            sess = MT5ScalpSession(
                account_id=account_id,
                symbol=symbol,
                status=MT5ScalpSessionStatus.active,
                phase="analyzing",
            )
            db.add(sess)
            await db.commit()
            await db.refresh(sess)
            session_id = sess.id

        await scalp_bot_manager.start(session_id)
        return (
            f"▶️ ScalpBot started!\n"
            f"Account: {account_id}  Symbol: {symbol}  Session: {session_id}\n"
            f"Use /mt5 scalp status {account_id} to monitor."
        ), "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ ScalpBot start failed: {exc}", "HTML"


async def _mt5_scalp_stop(account_id: int, db: AsyncSession) -> tuple[str, str]:
    try:
        from sqlalchemy import select as _sel
        from plugins.MT5TradingPlugin.backend.models import MT5ScalpSession, MT5ScalpSessionStatus
        from plugins.MT5TradingPlugin.backend.services.scalp_bot_service import scalp_bot_manager

        sessions = (await db.execute(
            _sel(MT5ScalpSession)
            .where(MT5ScalpSession.account_id == account_id)
            .where(MT5ScalpSession.status == MT5ScalpSessionStatus.active)
        )).scalars().all()

        if not sessions:
            return f"⚠️ No active ScalpBot sessions for account {account_id}.", "HTML"

        stopped = []
        for s in sessions:
            await scalp_bot_manager.stop(s.id)
            stopped.append(str(s.id))

        return f"⏹ ScalpBot stopped. Sessions: {', '.join(stopped)}", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ ScalpBot stop failed: {exc}", "HTML"


async def _mt5_close(ticket_raw: str, account_id: int, db: AsyncSession) -> tuple[str, str]:
    try:
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
        from plugins.MT5TradingPlugin.backend.models import MT5Account

        account = await db.get(MT5Account, account_id)
        if not account:
            return f"❌ MT5 account {account_id} not found.", "HTML"

        try:
            ticket_int = int(str(ticket_raw).strip())
        except (TypeError, ValueError):
            return f"❌ Invalid ticket '{ticket_raw}'.", "HTML"

        result = await mt5_client.close_position(
            account.login, account.server, account.password_encrypted, ticket_int
        )
        retcode = str(result.get("retcode")) if isinstance(result, dict) else None
        if retcode in ("10009", "0"):
            return f"✅ Position {ticket_int} closed.", "HTML"
        elif result:
            return f"✅ Close sent for {ticket_int}. Result: {result}", "HTML"
        else:
            return f"⚠️ Close returned no result for ticket {ticket_int}.", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ MT5 close failed: {exc}", "HTML"
