"""Telegram Bot command routing service.

Parses an incoming Telegram Update dict and routes each command to the
appropriate backend handler (Jarvis, trading, signals, monitor).

Entry point: ``parse_and_execute(update, token, allowed_chat_ids, db)``
Returns ``(reply_text, parse_mode)`` — the caller is responsible for
sending the reply via ``bot_service.send_message``.
"""
from __future__ import annotations

import math
import re
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# ── Per-chat conversation history ─────────────────────────────────────────────
# Keyed by Telegram chat_id string; stores last _MAX_HISTORY * 2 messages so
# Jarvis can follow the thread across turns.
_CHAT_HISTORY: dict[str, list[dict[str, str]]] = {}
_MAX_HISTORY = 20  # most-recent user+assistant pairs to keep


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
    if not text:
        return None, "HTML", None

    # ── Security gate ─────────────────────────────────────────────────────────
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        logger.warning("[BotCommand] Rejected update from unauthorized chat_id={}", chat_id)
        return None, "HTML", None

    # ── Command dispatch ──────────────────────────────────────────────────────
    if text.startswith("/"):
        cmd_raw, _, args = text.partition(" ")
        # Strip any @BotName suffix from the command
        cmd = cmd_raw.split("@")[0].lstrip("/").lower()
        return await _dispatch(cmd, args.strip(), db)

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
        "🔮 <b>Kronos Forecast &amp; Order Execution</b>",
        "/forecast BTCUSDT [exchange] [1h] — Kronos ML forecast + sniper entries",
        "/order long BTCUSDT 100 — Paper trade from Kronos signal ($100 margin)",
        "/order live long BTCUSDT 100 — LIVE Bitget futures order",
        "/analyze BTCUSDT — Deep AI analysis (Kronos + news + position)",
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
        # For rich analysis, prefer the full detail (AI narrative + news + levels)
        # over the short spoken summary so Telegram users get the complete research.
        if result.action in ("analyze", "news_position_analysis") and result.detail:
            body = result.detail
        else:
            body = result.speech or result.detail
        text = f"{emoji} {body}"
        return text[:4000], "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ Jarvis error: {exc}", "HTML"


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


def _jarvis_system_prompt(live_data: str | None = None) -> str:
    """System prompt that gives the LLM the full Jarvis persona and capability list.

    If *live_data* is provided it is appended as a data block so the LLM can
    analyse real numbers instead of suggesting the user run commands.
    """
    base = (
        "You are JARVIS, the AI trading assistant for your principal. Always address "
        "them as 'Sir'. You are connected to a live trading system with the following "
        "capabilities:\n"
        "• Crypto futures (Bitget): /order, /close, /tp, /sl, /positions, /portfolio\n"
        "• Kronos ML forecasting engine: /forecast BTCUSDT\n"
        "• MT5 forex trading: /mt5 status, /mt5 positions, /mt5 scalp\n"
        "• Live market signals & sniper: /signals, /sniper\n"
        "• Deep AI market analysis: /analyze BTCUSDT\n\n"
        "Guidelines:\n"
        "- Answer trading questions with confidence and precision.\n"
        "- When the user wants to execute a trade, show them the exact command.\n"
        "- For general chat, be sharp, helpful, and focused on trading outcomes.\n"
        "- Keep responses concise — 2-8 sentences unless deep analysis is requested.\n"
        "- Never invent price data; work only from the figures provided below.\n"
        "- Format numbers cleanly; use emojis sparingly.\n"
        "- Maintain the conversation thread — reference prior messages when relevant.\n"
        "- IMPORTANT: When live portfolio data is included, ALWAYS analyse it directly.\n"
        "  Give clear hold/close/adjust recommendations for each position with reasoning.\n"
        "  Never tell the user to 'run a command' if data is already shown."
    )
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


# ── AI fallback ───────────────────────────────────────────────────────────────

async def _ai_fallback(
    text: str,
    db: AsyncSession,
    *,
    chat_id: str = "",
) -> tuple[str | None, str, dict | None]:
    """Route unrecognised free-text through Jarvis AI.

    Strategy:
    1. Try execute_command first — handles natural-language trading commands
       (e.g. "close my BTC", "what are my positions") without needing a slash.
    2. If Jarvis doesn't recognise it (action == 'unknown') or fails, fall back
       to a full AI chat via db_chat with:
       - The Jarvis persona + conversation history
       - Live portfolio data when the query is about positions/portfolio
    """
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
                return reply[:4000], "HTML", None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI] execute_command pre-check failed: {}", exc)

    # ── Step 2: Full AI chat with Jarvis persona + optional live position data ─
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat

        # Auto-fetch live portfolio data when the message is about positions
        live_data: str | None = None
        if _wants_position_analysis(text):
            try:
                live_data = await _fetch_live_position_context(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AI] Live position context fetch failed: {}", exc)

        history = _get_history(chat_id) if chat_id else []
        messages = [
            {"role": "system", "content": _jarvis_system_prompt(live_data)},
            *history,
            {"role": "user", "content": text},
        ]

        resp = await db_chat(
            db,
            messages,
            temperature=0.5,
            max_tokens=1200,  # more room for position analysis
            json_mode=False,
            agent_name="jarvis-telegram-chat",
            source="telegram",
        )

        if resp.get("ok") and resp.get("content"):
            reply = str(resp["content"]).strip()
            if chat_id:
                _record_history(chat_id, "user", text)
                _record_history(chat_id, "assistant", reply)
            return reply[:3800], "HTML", None

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
    target = _fmt_p(sig.target_price) if sig else "—"

    lines: list[str] = [
        f"🔮 <b>Kronos Forecast — {symbol} ({timeframe})</b>",
        f"Engine: <code>{forecast_resp.engine}</code>",
        "",
        f"{dir_emoji} <b>{direction}  |  {pct_chg:+.2f}%  |  {conf_pct}% confidence</b>",
        f"Now: <code>{_fmt_p(forecast_resp.anchor_price)}</code>  →  "
        f"Target: <code>{target}</code>",
    ]
    if forecast_resp.note:
        lines.append(f"⚠️ <i>{forecast_resp.note}</i>")

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
        analysis_text = (jarvis.analysis or "").strip()
        if len(analysis_text) > 1100:
            analysis_text = analysis_text[:1100].rsplit(" ", 1)[0] + " … <i>(tap Analyze for full)</i>"
        lines.append(f"<i>{analysis_text}</i>")

        # Position advice if present
        if jarvis.position_advice and pos:
            adv = jarvis.position_advice.strip()
            if len(adv) > 300:
                adv = adv[:300].rsplit(" ", 1)[0] + "…"
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
            entry_str = _fmt_p(s.entry)
            sl_str = _fmt_p(s.stop_loss)
            tp_str = _fmt_p(s.take_profit_1)
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
        lines.append("ℹ️ No sniper entries — try a different timeframe.")

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

async def _handle_analyze(args: str, _db: AsyncSession) -> tuple[str, str]:
    """Deep Jarvis analysis: Kronos + AI narrative + news + open position.

    Usage: /analyze BTCUSDT
    """
    if not args:
        return "❓ Usage: /analyze &lt;SYMBOL&gt;  e.g. /analyze BTCUSDT", "HTML"
    return await _jarvis_command(f"analyze {args}")


# ── MT5 commands ──────────────────────────────────────────────────────────────

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
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        rows = (await db.execute(_sel(MT5Account))).scalars().all()
        if not rows:
            return "📭 No MT5 accounts configured.", "HTML"

        lines = ["🖥 <b>MT5 Accounts</b>", ""]
        for a in rows:
            badge = "🟢" if str(getattr(a, "status", "")).lower() == "active" else "⚪"
            lines.append(
                f"{badge} [{a.id}] <b>{a.label or a.login}</b> — {getattr(a, 'account_type', '')}"
            )
            try:
                info = await mt5_client.get_account_info(a)
                if info:
                    eq = info.get("equity") or info.get("balance") or 0
                    bal = info.get("balance") or 0
                    lines.append(f"     Equity: {eq:.2f}  Balance: {bal:.2f}")
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(lines), "HTML"
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

        positions = await mt5_client.get_positions(account)
        if not positions:
            return f"📭 No open positions on account {account.id}.", "HTML"

        lines = [f"📈 <b>MT5 Positions — Account {account.id}</b>", ""]
        for p in positions[:10]:
            pnl = float(p.get("profit", 0) or 0)
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{pnl_e} <b>{p.get('symbol', '?')}</b> "
                f"{p.get('type', '').upper()} {p.get('volume', '?')}\n"
                f"   Open: {p.get('price_open', '?')}  Cur: {p.get('price_current', '?')}\n"
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

        orders = await mt5_client.get_orders(account)
        if not orders:
            return f"📋 No pending orders on account {account.id}.", "HTML"

        lines = [f"📋 <b>MT5 Pending Orders — Account {account.id}</b>", ""]
        for o in orders[:10]:
            lines.append(
                f"• <b>{o.get('symbol', '?')}</b> {o.get('type', '?').upper()} "
                f"vol {o.get('volume_initial', '?')} @ {o.get('price_open', '?')}\n"
                f"  SL: {o.get('sl', 0)}  TP: {o.get('tp', 0)}  Ticket: {o.get('ticket', '?')}"
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

        result = await mt5_client.close_position(account, ticket_raw)
        if result and result.get("retcode") == 10009:
            return f"✅ Position {ticket_raw} closed.", "HTML"
        elif result:
            return f"✅ Close sent. Result: {result}", "HTML"
        else:
            return f"⚠️ Close returned no result for ticket {ticket_raw}.", "HTML"
    except Exception as exc:  # noqa: BLE001
        return f"❌ MT5 close failed: {exc}", "HTML"
