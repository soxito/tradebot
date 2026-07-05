"""
MT5 Trading Plugin — Autonomous Scalp Bot Service.

Runs one asyncio background loop per active scalp session (account + symbol).
Each cycle (~10s) it:
  1. Pulls account balance/equity and enforces the daily-loss guard.
  2. Loads M1/M5/H1/H4/D1 candles (MT5 bridge → exchange fallback).
  3. Optionally runs a Kronos ML forecast + Jarvis/AiMarketAnalyst AI gate.
  4. Uses the multi-timeframe SMC ``ScalpStrategyEngine`` to decide a market entry.
  5. Places market orders with SL/TP, closes them on the profit target, and when
     the first trade is offside opens an SMC-guided recovery leg so a modest
     retracement takes the *combined* position back to profit.

All state is persisted to ``mt5_scalp_sessions`` / ``mt5_scalp_trades`` so the
frontend can poll ``/plugins/mt5/scalp/status`` for a live view.

Standalone plugin service — never imports or mutates core trading logic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from plugins.MT5TradingPlugin.backend.models import (
    MT5Account,
    MT5ScalpSession,
    MT5ScalpSessionStatus,
    MT5ScalpTrade,
)
from plugins.MT5TradingPlugin.backend.services.mt5_client import (
    mt5_client,
    normalize_order_type,
)
from plugins.MT5TradingPlugin.backend.services.scalp_strategy import (
    ScalpStrategyEngine,
    ScalpEntry,
    ALL_SCALP_TFS,
    PRIMARY_SCALP_TF,
    RECOVERY_DRAWDOWN_ATR,
)
from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
    Candle,
    candles_from_payload,
    _atr,
)


# ── Config ──────────────────────────────────────────────────────────────────────

CYCLE_SECONDS = 10
MIN_CANDLES = 40
COMMENT_TAG = "ScalpBot"

# MT5 timeframe → ccxt/exchange timeframe string (exchange fallback)
_MT5_TF_TO_EX: Dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}

_MT5_SYMBOL_TO_EX: Dict[str, str] = {
    "XAUUSD": "XAU/USDT", "XAGUSD": "XAG/USDT",
    "BTCUSD": "BTC/USDT", "ETHUSD": "ETH/USDT",
    "EURUSD": "EUR/USDT", "GBPUSD": "GBP/USDT",
}


def _symbol_to_exchange(symbol: str) -> Optional[str]:
    s = (symbol or "").upper().replace("/", "")
    if s in _MT5_SYMBOL_TO_EX:
        return _MT5_SYMBOL_TO_EX[s]
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    if s.endswith("USD") and not s.endswith("USDT") and len(s) > 3:
        return f"{s[:-3]}/USDT"
    return None


async def _exchange_candles(symbol: str, timeframe: str, count: int) -> List[Candle]:
    """Fallback candle feed from Bitget/Binance when MT5 history is sparse."""
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore

        ex_symbol = _symbol_to_exchange(symbol)
        if not ex_symbol:
            return []
        ex_tf = _MT5_TF_TO_EX.get((timeframe or "").upper(), "5m")
        conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if conn is None:
            conn = exchange_manager.get_exchange(SupportedExchange.BINANCE)
        if conn is None:
            return []
        raw = await conn.get_ohlcv(ex_symbol, ex_tf, count)
        if not raw:
            return []
        return candles_from_payload([
            {"time": int(c[0] / 1000), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5] or 0)}
            for c in raw
        ])
    except Exception as exc:  # noqa: BLE001 — fallback must never raise
        logger.debug(f"[ScalpBot] exchange candle fallback {symbol}/{timeframe}: {exc}")
        return []


async def _load_tf_candles(login: str, server: str, password: str, symbol: str,
                           timeframe: str, count: int = 200) -> List[Candle]:
    """Fetch one timeframe of candles (MT5 first, exchange fallback)."""
    bars: List[Dict[str, Any]] = []
    try:
        bars = await mt5_client.get_candles(
            login, server, password, symbol, timeframe, count,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ScalpBot] MT5 candles {symbol}/{timeframe}: {e}")
        bars = []

    candles = candles_from_payload([
        {"time": b["time"], "open": b["open"], "high": b["high"],
         "low": b["low"], "close": b["close"], "volume": b.get("volume")}
        for b in bars
    ])
    if len(candles) < MIN_CANDLES:
        ex = await _exchange_candles(symbol, timeframe, count)
        if len(ex) >= MIN_CANDLES:
            return ex
    return candles


async def _kronos_direction(candles: List[Candle], symbol: str, timeframe: str) -> float:
    """
    Optional Kronos ML directional score (-1..1). 0 when unavailable.

    Positive → forecast up, negative → down. Fully graceful: any failure or a
    missing plugin returns 0 so the scalp engine's SMC decision stands alone.
    """
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service as _kronos  # type: ignore
        rows = []
        for c in list(candles)[-400:]:
            t = int(getattr(c, "time", 0) or 0)
            t_ms = t * 1000 if t < 1_000_000_000_000 else t
            rows.append([t_ms, float(c.open), float(c.high), float(c.low),
                         float(c.close), float(getattr(c, "volume", 0) or 0)])
        if len(rows) < 60:
            return 0.0
        ex_tf = _MT5_TF_TO_EX.get((timeframe or "").upper(), "5m")
        resp = await _kronos.forecast_from_rows(rows, symbol=symbol, timeframe=ex_tf)
        signal = getattr(resp, "signal", None) if resp else None
        if not signal:
            return 0.0
        pct = float(getattr(signal, "pct_change", 0.0) or 0.0)
        conf = float(getattr(signal, "confidence", 0.5) or 0.5)
        # Normalise into a small directional score bounded to [-1, 1].
        return max(-1.0, min(1.0, pct / 100.0 * (0.5 + conf)))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ScalpBot] kronos direction {symbol}: {exc}")
        return 0.0


async def _ai_gate(symbol: str, side: str, bias_reason: str, confidence: float) -> Dict[str, Any]:
    """
    Optional AI confirmation via the shared AiMarketAnalyst router.
    Also fetches economic calendar events (CPI, rates, NFP, etc.) and includes
    them in the prompt so the AI can flag imminent high-impact news risk.

    Returns ``{"decision": "take"|"skip", "note": str}``. Fails open (``take``)
    if no AI provider is configured.
    """
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat  # type: ignore
    except Exception:
        return {"decision": "take", "note": "ai_unavailable"}

    # Fetch economic calendar for this symbol (non-blocking, best-effort)
    eco_events: List[Dict[str, Any]] = []
    try:
        from plugins.MT5TradingPlugin.backend.services.smc_ai import fetch_economic_events  # type: ignore
        eco_events = await fetch_economic_events(symbol)
    except Exception:
        pass

    eco_context = ""
    if eco_events:
        upcoming = [e for e in eco_events if -2 <= e.get("hours_away", 99) <= 24]
        if upcoming:
            eco_context = (
                "\nUpcoming high-impact economic events: "
                + ", ".join(
                    f"{e['title']} ({e['currency']}) in {e['hours_away']:.1f}h"
                    + (f" [prev={e['previous']}, fcst={e['forecast']}]"
                       if e.get("previous") or e.get("forecast") else "")
                    for e in upcoming[:4]
                )
                + "."
            )

    prompt = (
        f"You are a scalping risk filter. Instrument {symbol}. The engine wants a "
        f"{side.upper()} pending limit scalp (confidence {confidence:.2f}). "
        f"Multi-timeframe read: {bias_reason}.{eco_context} "
        "Reply STRICT JSON only: "
        '{"decision":"take"|"skip","note":str}. Skip only if there is a STRONG '
        "reason (e.g. imminent high-impact news within 2h, confirmed opposing "
        "higher-timeframe trend, extreme overbought/oversold). "
        "Otherwise default to take — the engine already filters most bad setups."
    )
    try:
        async with AsyncSessionLocal() as db:
            res = await db_chat(
                db,
                [{"role": "user", "content": prompt}],
                json_mode=True,
            )
        content = res.get("content") if isinstance(res, dict) else res
        import json as _json
        parsed = _json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, dict):
            decision = str(parsed.get("decision", "take")).lower()
            if decision not in ("take", "skip"):
                decision = "take"
            return {"decision": decision, "note": str(parsed.get("note", ""))[:280]}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ScalpBot] AI gate {symbol}: {exc}")
    return {"decision": "take", "note": "ai_error"}


def _position_side(p: Dict[str, Any]) -> str:
    return normalize_order_type(p) or "buy"


def _match_symbol(a: str, b: str) -> bool:
    return (a or "").upper().replace("/", "") == (b or "").upper().replace("/", "")


# ── Manager ─────────────────────────────────────────────────────────────────────

class ScalpBotManager:
    """Owns one asyncio task per active scalp session (keyed by session id)."""

    def __init__(self) -> None:
        self._tasks: Dict[int, asyncio.Task] = {}

    # -- lifecycle -------------------------------------------------------------

    def is_running(self, session_id: int) -> bool:
        t = self._tasks.get(session_id)
        return bool(t) and not t.done()

    def start(self, session_id: int) -> bool:
        """Launch (or relaunch) the loop for a session. Returns True if started."""
        if self.is_running(session_id):
            return False
        task = asyncio.create_task(self._run_loop(session_id))
        self._tasks[session_id] = task
        task.add_done_callback(lambda _t, sid=session_id: self._tasks.pop(sid, None))
        logger.info(f"[ScalpBot] session {session_id} loop started")
        return True

    async def stop(self, session_id: int) -> None:
        """Cancel a session loop and mark it stopped in the DB."""
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._set_status(session_id, MT5ScalpSessionStatus.STOPPED, phase="stopped")
        logger.info(f"[ScalpBot] session {session_id} stopped")

    async def resume_active_sessions(self) -> int:
        """Restart loops for sessions left ACTIVE after a backend restart."""
        started = 0
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(MT5ScalpSession).where(
                    MT5ScalpSession.status == MT5ScalpSessionStatus.ACTIVE
                )
            )
            sessions = rows.scalars().all()
        for s in sessions:
            if self.start(s.id):
                started += 1
        return started

    # -- db helpers ------------------------------------------------------------

    async def _set_status(self, session_id: int, status: MT5ScalpSessionStatus,
                          phase: Optional[str] = None, error: Optional[str] = None) -> None:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            if not s:
                return
            s.status = status
            if phase is not None:
                s.phase = phase
            if error is not None:
                s.error_msg = error
            if status in (MT5ScalpSessionStatus.STOPPED, MT5ScalpSessionStatus.COMPLETED,
                          MT5ScalpSessionStatus.ERROR):
                s.stopped_at = datetime.utcnow()
            await db.commit()

    # -- main loop -------------------------------------------------------------

    async def _run_loop(self, session_id: int) -> None:
        try:
            while True:
                stop = await self._cycle(session_id)
                if stop:
                    break
                await asyncio.sleep(CYCLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[ScalpBot] session {session_id} crashed: {e}")
            await self._set_status(session_id, MT5ScalpSessionStatus.ERROR,
                                   phase="error", error=str(e)[:500])

    async def _cycle(self, session_id: int) -> bool:
        """Run one scalp cycle. Returns True when the loop should terminate."""
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session or session.status != MT5ScalpSessionStatus.ACTIVE:
                return True
            account = await db.get(MT5Account, session.account_id)
            if not account:
                session.status = MT5ScalpSessionStatus.ERROR
                session.error_msg = "Account not found"
                await db.commit()
                return True
            # Snapshot the fields we need outside the session.
            cfg = {
                "symbol": session.symbol,
                "lot_size": session.lot_size,
                "auto_lot": session.auto_lot,
                "risk_per_trade_pct": session.risk_per_trade_pct,
                "max_daily_loss_pct": session.max_daily_loss_pct,
                "target_profit_pct": session.target_profit_pct,
                "recovery_enabled": session.recovery_enabled,
                "use_ai": session.use_ai,
                "use_kronos": session.use_kronos,
                "timeframe": session.timeframe or PRIMARY_SCALP_TF,
                "trade1_ticket": session.trade1_ticket,
                "trade2_ticket": session.trade2_ticket,
                "start_equity": session.start_equity,
            }
            login, server, password = account.login, account.server, account.password_encrypted

        symbol = cfg["symbol"]

        # ── Account state + daily loss guard ─────────────────────────────────
        try:
            info = await mt5_client.get_account_info(login, server, password)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ScalpBot] account_info {symbol}: {e}")
            info = {}
        balance = float(info.get("balance", 0.0) or 0.0)
        equity = float(info.get("equity", balance) or balance)

        # Seed the starting equity on the first successful cycle.
        if cfg["start_equity"] <= 0 and equity > 0:
            async with AsyncSessionLocal() as db:
                s = await db.get(MT5ScalpSession, session_id)
                if s:
                    s.start_equity = equity
                    await db.commit()
            cfg["start_equity"] = equity

        if cfg["start_equity"] > 0 and equity > 0:
            dd_pct = (cfg["start_equity"] - equity) / cfg["start_equity"] * 100.0
            if dd_pct >= cfg["max_daily_loss_pct"]:
                logger.info(
                    f"[ScalpBot] session {session_id} hit daily loss "
                    f"{dd_pct:.2f}% ≥ {cfg['max_daily_loss_pct']}% — stopping"
                )
                await self._close_all_session_trades(session_id, login, server, password, symbol)
                await self._set_status(session_id, MT5ScalpSessionStatus.COMPLETED,
                                       phase="stopped", error=None)
                return True

        # ── Market data ──────────────────────────────────────────────────────
        candles_by_tf: Dict[str, List[Candle]] = {}
        for tf in ALL_SCALP_TFS:
            candles_by_tf[tf] = await _load_tf_candles(login, server, password, symbol, tf)
        m5 = candles_by_tf.get(PRIMARY_SCALP_TF) or []
        if len(m5) < MIN_CANDLES:
            await self._update_phase(session_id, "analyzing",
                                     note="Waiting for candle data")
            return False

        try:
            quote = await mt5_client.get_symbol_price(login, server, password, symbol)
        except Exception:  # noqa: BLE001
            quote = {"bid": 0.0, "ask": 0.0}
        bid = float(quote.get("bid", 0.0) or 0.0)
        ask = float(quote.get("ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if (bid and ask) else (m5[-1].close if m5 else 0.0)

        engine = ScalpStrategyEngine(
            symbol=symbol,
            lot_size=cfg["lot_size"],
            auto_lot=cfg["auto_lot"],
            risk_per_trade_pct=cfg["risk_per_trade_pct"],
        )

        # ── Live positions belonging to this session ─────────────────────────
        try:
            positions = await mt5_client.get_positions(login, server, password)
        except Exception:  # noqa: BLE001
            positions = []
        by_ticket = {int(p.get("ticket", 0)): p for p in positions if p.get("ticket")}
        t1 = by_ticket.get(int(cfg["trade1_ticket"])) if cfg["trade1_ticket"] else None
        t2 = by_ticket.get(int(cfg["trade2_ticket"])) if cfg["trade2_ticket"] else None

        # Reconcile closed tickets (hit SL/TP outside our control).
        await self._reconcile_closed(session_id, cfg, t1, t2)
        # Re-read fresh ticket state after reconcile.
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            cfg["trade1_ticket"] = session.trade1_ticket
            cfg["trade2_ticket"] = session.trade2_ticket
        t1 = by_ticket.get(int(cfg["trade1_ticket"])) if cfg["trade1_ticket"] else None
        t2 = by_ticket.get(int(cfg["trade2_ticket"])) if cfg["trade2_ticket"] else None

        combined_pnl = 0.0
        for p in (t1, t2):
            if p:
                combined_pnl += float(p.get("profit", 0.0) or 0.0)

        # ── State machine ────────────────────────────────────────────────────
        if not t1:
            # Flat → look for a fresh entry.
            entry, bias = engine.analyse(candles_by_tf, mid, balance)
            note = bias.reason
            await self._store_bias(session_id, bias, "analyzing" if not entry else "waiting")
            if not entry:
                return False

            # Optional ML + AI confirmation.
            if cfg["use_kronos"]:
                kd = await _kronos_direction(m5, symbol, PRIMARY_SCALP_TF)
                entry.kronos_score = round(kd, 3)
                if (entry.side == "buy" and kd < -0.4) or (entry.side == "sell" and kd > 0.4):
                    await self._update_phase(session_id, "waiting",
                                             note=f"Kronos opposes ({kd:+.2f}) — waiting")
                    return False
            if cfg["use_ai"]:
                gate = await _ai_gate(symbol, entry.side, bias.reason, entry.confidence)
                note = f"{bias.reason} | AI: {gate.get('note', '')}"
                if gate.get("decision") == "skip":
                    await self._update_phase(session_id, "waiting", note=f"AI skip: {gate.get('note','')}")
                    return False

            await self._open_trade(session_id, login, server, password, entry, note)
            return False

        # Trade 1 open — manage it.
        p1_profit = float(t1.get("profit", 0.0) or 0.0)
        p1_side = _position_side(t1)
        p1_open = float(t1.get("openPrice", 0.0) or 0.0)
        p1_lot = float(t1.get("lots", t1.get("volume", cfg["lot_size"])) or cfg["lot_size"])

        target_amt = self._target_amount(balance, cfg["target_profit_pct"])

        # Close on combined profit when a recovery leg is open.
        if t2:
            await self._store_bias_phase(session_id, "recovery", combined_pnl)
            if combined_pnl > 0:
                await self._close_all_session_trades(session_id, login, server, password, symbol,
                                                     record=True)
                await self._update_phase(session_id, "analyzing",
                                         note=f"Closed combined +{combined_pnl:.2f}")
            return False

        # Single trade: take profit at the target.
        await self._store_bias_phase(session_id, "in_trade", p1_profit)
        if target_amt > 0 and p1_profit >= target_amt:
            await self._close_all_session_trades(session_id, login, server, password, symbol,
                                                 record=True)
            await self._update_phase(session_id, "analyzing",
                                     note=f"Target hit +{p1_profit:.2f}")
            return False

        # Arm the recovery leg when the trade is meaningfully offside.
        if cfg["recovery_enabled"] and mid > 0 and p1_open > 0:
            atr_m5 = _atr(m5)
            offside = (p1_open - mid) if p1_side == "buy" else (mid - p1_open)
            if atr_m5 > 0 and offside >= RECOVERY_DRAWDOWN_ATR * atr_m5 and p1_profit < 0:
                recovery = engine.build_recovery(
                    p1_side, p1_lot, mid, candles_by_tf, balance
                )
                if recovery:
                    await self._open_trade(session_id, login, server, password, recovery,
                                           "Recovery leg armed", recovery=True)
                    await self._update_phase(session_id, "recovery",
                                             note="Recovery leg opened")
        return False

    # -- trade helpers ---------------------------------------------------------

    def _target_amount(self, balance: float, target_pct: float) -> float:
        if balance <= 0 or target_pct <= 0:
            return 0.0
        return balance * (target_pct / 100.0)

    async def _open_trade(self, session_id: int, login: str, server: str, password: str,
                          entry: ScalpEntry, note: str, recovery: bool = False) -> None:
        comment = f"{COMMENT_TAG}{'-R' if recovery else ''}#{session_id}"
        symbol = await self._session_symbol(session_id)
        # Use the SMC-derived order type (buy_limit, sell_limit, buy_stop, sell_stop)
        # and the zone entry price so the order rests at the institutional level.
        order_type = getattr(entry, "order_type", entry.side)
        limit_price = entry.entry  # non-zero → pending order placed at this price
        try:
            result = await mt5_client.place_order(
                login=login, server=server, password=password,
                symbol=symbol, order_type=order_type,
                volume=entry.lot, price=limit_price, sl=entry.stop_loss, tp=entry.take_profit,
                comment=comment,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ScalpBot] session {session_id} order failed: {e}")
            await self._update_phase(session_id, "waiting", note=f"Order failed: {e}")
            return

        ticket = None
        if isinstance(result, dict):
            ticket = result.get("ticket")
        ticket = int(ticket) if ticket else None

        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            trade = MT5ScalpTrade(
                session_id=session_id,
                account_id=session.account_id,
                symbol=session.symbol,
                side=entry.side,
                lot=entry.lot,
                entry_price=entry.entry,
                sl=entry.stop_loss,
                tp=entry.take_profit,
                is_recovery=recovery,
                ticket=ticket,
                confidence=entry.confidence,
                reason=(note or entry.reason)[:1000],
                status="open",
            )
            db.add(trade)
            session.total_trades = (session.total_trades or 0) + 1
            if recovery:
                session.trade2_ticket = ticket
                session.phase = "recovery"
            else:
                session.trade1_ticket = ticket
                session.phase = "in_trade"
            session.ai_note = (note or "")[:1000]
            session.last_cycle_at = datetime.utcnow()
            await db.commit()
        logger.info(
            f"[ScalpBot] session {session_id} opened {entry.side} {entry.lot} "
            f"@~{entry.entry} SL {entry.stop_loss} TP {entry.take_profit} "
            f"ticket={ticket} recovery={recovery}"
        )

    async def _session_symbol(self, session_id: int) -> str:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            return s.symbol if s else ""

    async def _close_all_session_trades(self, session_id: int, login: str, server: str,
                                        password: str, symbol: str, record: bool = False) -> None:
        """Close both legs, mark trades closed, and roll up realised PnL."""
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            tickets = [t for t in (session.trade1_ticket, session.trade2_ticket) if t]

        # Fetch live PnL before closing so we can record it.
        try:
            positions = await mt5_client.get_positions(login, server, password)
        except Exception:  # noqa: BLE001
            positions = []
        pnl_by_ticket = {int(p.get("ticket", 0)): float(p.get("profit", 0.0) or 0.0)
                         for p in positions if p.get("ticket")}
        price_by_ticket = {int(p.get("ticket", 0)): float(p.get("currentPrice", 0.0) or 0.0)
                           for p in positions if p.get("ticket")}

        realised = 0.0
        for tk in tickets:
            try:
                await mt5_client.close_position(login, server, password, int(tk))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[ScalpBot] close {tk} failed: {e}")
            realised += pnl_by_ticket.get(int(tk), 0.0)

        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            for tk in tickets:
                rows = await db.execute(
                    select(MT5ScalpTrade).where(
                        MT5ScalpTrade.session_id == session_id,
                        MT5ScalpTrade.ticket == int(tk),
                        MT5ScalpTrade.status == "open",
                    )
                )
                for tr in rows.scalars().all():
                    tr.status = "closed"
                    tr.close_price = price_by_ticket.get(int(tk)) or tr.entry_price
                    tr.pnl = pnl_by_ticket.get(int(tk), 0.0)
                    tr.closed_at = datetime.utcnow()
            if record:
                session.session_pnl = (session.session_pnl or 0.0) + realised
                if realised >= 0:
                    session.wins = (session.wins or 0) + 1
                else:
                    session.losses = (session.losses or 0) + 1
            session.trade1_ticket = None
            session.trade2_ticket = None
            session.last_cycle_at = datetime.utcnow()
            await db.commit()

    async def _reconcile_closed(self, session_id: int, cfg: Dict[str, Any],
                                t1: Optional[Dict], t2: Optional[Dict]) -> None:
        """Detect tickets that closed (SL/TP) outside our loop and clear them."""
        cleared: List[int] = []
        if cfg["trade1_ticket"] and t1 is None:
            cleared.append(int(cfg["trade1_ticket"]))
        if cfg["trade2_ticket"] and t2 is None:
            cleared.append(int(cfg["trade2_ticket"]))
        if not cleared:
            return
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            for tk in cleared:
                rows = await db.execute(
                    select(MT5ScalpTrade).where(
                        MT5ScalpTrade.session_id == session_id,
                        MT5ScalpTrade.ticket == tk,
                        MT5ScalpTrade.status == "open",
                    )
                )
                for tr in rows.scalars().all():
                    tr.status = "closed"
                    tr.closed_at = datetime.utcnow()
                if session.trade1_ticket == tk:
                    session.trade1_ticket = None
                if session.trade2_ticket == tk:
                    session.trade2_ticket = None
            if not session.trade1_ticket and not session.trade2_ticket:
                session.phase = "analyzing"
            await db.commit()

    # -- phase / bias persistence ----------------------------------------------

    async def _update_phase(self, session_id: int, phase: str,
                            note: Optional[str] = None) -> None:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            if not s:
                return
            s.phase = phase
            if note is not None:
                s.ai_note = note[:1000]
            s.last_cycle_at = datetime.utcnow()
            await db.commit()

    async def _store_bias(self, session_id: int, bias, phase: str) -> None:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            if not s:
                return
            s.bias_direction = bias.direction
            s.bias_confidence = round(bias.confidence, 3)
            s.phase = phase
            s.ai_note = bias.reason[:1000]
            s.last_cycle_at = datetime.utcnow()
            await db.commit()

    async def _store_bias_phase(self, session_id: int, phase: str, live_pnl: float) -> None:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            if not s:
                return
            s.phase = phase
            s.last_cycle_at = datetime.utcnow()
            await db.commit()


# Singleton
scalp_bot_manager = ScalpBotManager()
