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
    is_pending_order,
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
from plugins.MT5TradingPlugin.backend.services.candle_feed import (
    resolve_candles,
    quote_buffer,
)


# ── Config ──────────────────────────────────────────────────────────────────────

CYCLE_SECONDS = 10
MIN_CANDLES = 40
COMMENT_TAG = "ScalpBot"

# An unfilled pending entry is cancelled and re-analysed after this many seconds
# so the bot never sits forever on a stale limit level in a fast market.
PENDING_TTL_SECONDS = 180

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
                           timeframe: str, count: int = 200,
                           mid_hint: Optional[float] = None) -> List[Candle]:
    """Fetch one timeframe of candles via the resilient candle-feed resolver.

    The mtapi bridge history is frozen for some brokers/accounts, so the
    resolver falls through fresh mtapi → exchange → forex_provider → a live
    quote-built series so the engine always has usable recent data.
    """
    dicts = await resolve_candles(
        mt5_client, login, server, password, symbol, timeframe, count,
        account_key=str(login), mid_hint=mid_hint,
    )
    return candles_from_payload([
        {"time": b["time"], "open": b["open"], "high": b["high"],
         "low": b["low"], "close": b["close"], "volume": b.get("volume")}
        for b in dicts
    ])


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


def _order_ticket(o: Dict[str, Any]) -> int:
    """Ticket id from an order/position payload (handles key casing variants)."""
    for k in ("ticket", "order", "Ticket", "Order"):
        v = o.get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


def _order_open_price(o: Dict[str, Any]) -> float:
    for k in ("openPrice", "open_price", "priceOpen", "price"):
        v = o.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _match_symbol(a: str, b: str) -> bool:
    return (a or "").upper().replace("/", "") == (b or "").upper().replace("/", "")


# ── Manager ─────────────────────────────────────────────────────────────────────

class ScalpBotManager:
    """Owns one asyncio task per active scalp session (keyed by session id)."""

    def __init__(self) -> None:
        self._tasks: Dict[int, asyncio.Task] = {}
        self._resumed: bool = False

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

    async def ensure_resumed(self) -> None:
        """Idempotently resume ACTIVE sessions once after process startup.

        The app uses a ``lifespan`` handler, so router ``on_event("startup")``
        hooks never fire; instead this is triggered from the frequently-polled
        scalp status endpoint so loops recover within seconds of the UI loading.
        """
        if self._resumed:
            return
        self._resumed = True
        try:
            n = await self.resume_active_sessions()
            if n:
                logger.info(f"[ScalpBot] resumed {n} active session(s) after startup")
        except Exception as e:  # noqa: BLE001
            self._resumed = False  # allow a later retry if the first attempt failed
            logger.warning(f"[ScalpBot] resume_active_sessions failed: {e}")

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

        # ── Live quote first (real-time), then market data ───────────────────
        # The quote mid feeds the live candle buffer, so it must be fetched
        # before candles when the bridge history is stale.
        try:
            quote = await mt5_client.get_symbol_price(login, server, password, symbol)
        except Exception:  # noqa: BLE001
            quote = {"bid": 0.0, "ask": 0.0}
        bid = float(quote.get("bid", 0.0) or 0.0)
        ask = float(quote.get("ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if (bid and ask) else 0.0
        if mid > 0:
            quote_buffer.record(str(login), symbol, mid)

        candles_by_tf: Dict[str, List[Candle]] = {}
        for tf in ALL_SCALP_TFS:
            candles_by_tf[tf] = await _load_tf_candles(
                login, server, password, symbol, tf, mid_hint=mid,
            )
        m5 = candles_by_tf.get(PRIMARY_SCALP_TF) or []
        if mid <= 0:
            mid = m5[-1].close if m5 else 0.0
        if len(m5) < MIN_CANDLES:
            self._diag(session_id, symbol,
                       f"warming-up M5={len(m5)}/{MIN_CANDLES} (live buffer building)")
            await self._update_phase(
                session_id, "analyzing",
                note=f"Building live candles ({len(m5)}/{MIN_CANDLES} M5 bars)")
            return False

        engine = ScalpStrategyEngine(
            symbol=symbol,
            lot_size=cfg["lot_size"],
            auto_lot=cfg["auto_lot"],
            risk_per_trade_pct=cfg["risk_per_trade_pct"],
        )

        # ── Live orders belonging to this session (positions + pending) ──────
        # CRITICAL: scalp entries are *pending* limit/stop orders, so we must
        # read BOTH filled positions and resting pending orders. Treating a
        # resting pending ticket as "closed" (the previous bug) made the bot
        # thrash — re-placing a new pending order every cycle and never
        # managing a real trade.
        try:
            all_orders = await mt5_client.get_orders(login, server, password)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ScalpBot] get_orders {symbol}: {e}")
            all_orders = []
        pos_by_ticket: Dict[int, Dict[str, Any]] = {}
        pend_by_ticket: Dict[int, Dict[str, Any]] = {}
        for o in all_orders:
            tk = _order_ticket(o)
            if not tk:
                continue
            if is_pending_order(o):
                pend_by_ticket[tk] = o
            else:
                pos_by_ticket[tk] = o

        def _leg(ticket_val) -> tuple[str, Optional[Dict[str, Any]]]:
            """Classify a tracked ticket: filled position, resting pending, or gone."""
            if not ticket_val:
                return "none", None
            tk = int(ticket_val)
            if tk in pos_by_ticket:
                return "filled", pos_by_ticket[tk]
            if tk in pend_by_ticket:
                return "pending", pend_by_ticket[tk]
            return "gone", None

        t1_state, t1 = _leg(cfg["trade1_ticket"])
        t2_state, t2 = _leg(cfg["trade2_ticket"])

        # A tracked pending order that just filled → flip its trade row to open.
        if t1_state == "filled":
            await self._mark_trade_filled(session_id, int(cfg["trade1_ticket"]))
        if t2_state == "filled":
            await self._mark_trade_filled(session_id, int(cfg["trade2_ticket"]))

        # Reconcile only tickets that are genuinely GONE (filled+closed or
        # cancelled) — never a resting pending order.
        gone: List[int] = []
        if cfg["trade1_ticket"] and t1_state == "gone":
            gone.append(int(cfg["trade1_ticket"]))
        if cfg["trade2_ticket"] and t2_state == "gone":
            gone.append(int(cfg["trade2_ticket"]))
        if gone:
            await self._reconcile_gone(session_id, gone)
            async with AsyncSessionLocal() as db:
                session = await db.get(MT5ScalpSession, session_id)
                cfg["trade1_ticket"] = session.trade1_ticket
                cfg["trade2_ticket"] = session.trade2_ticket
            t1_state, t1 = _leg(cfg["trade1_ticket"])
            t2_state, t2 = _leg(cfg["trade2_ticket"])

        # Combined PnL only from *filled* legs (pending legs are unrealised 0).
        combined_pnl = 0.0
        for p in (t1, t2):
            if p and not is_pending_order(p):
                combined_pnl += float(p.get("profit", 0.0) or 0.0)

        self._diag(
            session_id, symbol,
            f"cycle bal={balance:.2f} eq={equity:.2f} mid={mid:.5f} "
            f"t1={t1_state} t2={t2_state} combPnL={combined_pnl:+.2f} "
            f"pos={len(pos_by_ticket)} pend={len(pend_by_ticket)}"
        )

        # ── State machine ────────────────────────────────────────────────────
        if not cfg["trade1_ticket"]:
            # Flat → look for a fresh entry.
            entry, bias = engine.analyse(candles_by_tf, mid, balance)
            note = bias.reason
            await self._store_bias(session_id, bias, "analyzing" if not entry else "waiting")
            if not entry:
                self._diag(session_id, symbol,
                           f"no-entry bias={bias.direction} conf={bias.confidence:.2f} :: {bias.reason}")
                return False

            # Optional ML directional veto.
            if cfg["use_kronos"]:
                kd = await _kronos_direction(m5, symbol, PRIMARY_SCALP_TF)
                entry.kronos_score = round(kd, 3)
                if (entry.side == "buy" and kd < -0.4) or (entry.side == "sell" and kd > 0.4):
                    self._diag(session_id, symbol, f"kronos-veto {entry.side} kd={kd:+.2f}")
                    await self._update_phase(session_id, "waiting",
                                             note=f"Kronos opposes ({kd:+.2f}) — waiting")
                    return False

            # Optional AI + economic-calendar gate.
            if cfg["use_ai"]:
                gate = await _ai_gate(symbol, entry.side, bias.reason, entry.confidence)
                note = f"{bias.reason} | AI: {gate.get('note', '')}"
                if gate.get("decision") == "skip":
                    self._diag(session_id, symbol, f"ai-skip {gate.get('note','')}")
                    await self._update_phase(session_id, "waiting", note=f"AI skip: {gate.get('note','')}")
                    return False

            self._diag(
                session_id, symbol,
                f"ENTRY {entry.order_type} {entry.lot}@{entry.entry} "
                f"SL{entry.stop_loss} TP{entry.take_profit} conf={entry.confidence:.2f}"
            )
            await self._open_trade(session_id, login, server, password, entry, note)
            return False

        # ── There is an active primary leg (pending or filled) ───────────────
        if t1_state == "pending" and not cfg["trade2_ticket"]:
            # Resting entry — wait for the fill; cancel & re-analyse if stale.
            if await self._pending_is_stale(session_id, int(cfg["trade1_ticket"])):
                self._diag(session_id, symbol, "pending stale — cancelling & re-analysing")
                try:
                    await mt5_client.cancel_order(login, server, password, int(cfg["trade1_ticket"]))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[ScalpBot] cancel stale {cfg['trade1_ticket']}: {e}")
                await self._reconcile_gone(session_id, [int(cfg["trade1_ticket"])])
                await self._update_phase(session_id, "analyzing", note="Stale entry cancelled — re-analysing")
            else:
                await self._update_phase(session_id, "entry_pending",
                                         note="Pending entry resting — awaiting fill")
            return False

        target_amt = self._target_amount(balance, cfg["target_profit_pct"])

        # Recovery leg exists — manage the combined position.
        if cfg["trade2_ticket"]:
            await self._store_bias_phase(session_id, "recovery", combined_pnl)
            if t2_state == "pending":
                await self._update_phase(session_id, "recovery",
                                         note="Recovery entry resting — awaiting fill")
                return False
            if combined_pnl > 0:
                await self._close_all_session_trades(session_id, login, server, password, symbol,
                                                     record=True)
                await self._update_phase(session_id, "analyzing",
                                         note=f"Closed combined +{combined_pnl:.2f}")
            return False

        # Single FILLED trade — manage take-profit and arm recovery.
        if t1 is None:
            return False
        p1_profit = float(t1.get("profit", 0.0) or 0.0)
        p1_side = _position_side(t1)
        p1_open = _order_open_price(t1)
        p1_lot = float(t1.get("lots", t1.get("volume", cfg["lot_size"])) or cfg["lot_size"])

        await self._store_bias_phase(session_id, "in_trade", p1_profit)
        if target_amt > 0 and p1_profit >= target_amt:
            await self._close_all_session_trades(session_id, login, server, password, symbol,
                                                 record=True)
            await self._update_phase(session_id, "analyzing",
                                     note=f"Target hit +{p1_profit:.2f}")
            return False

        # Arm the recovery leg when the filled trade is meaningfully offside.
        if cfg["recovery_enabled"] and mid > 0 and p1_open > 0:
            atr_m5 = _atr(m5)
            offside = (p1_open - mid) if p1_side == "buy" else (mid - p1_open)
            if atr_m5 > 0 and offside >= RECOVERY_DRAWDOWN_ATR * atr_m5 and p1_profit < 0:
                recovery = engine.build_recovery(
                    p1_side, p1_lot, mid, candles_by_tf, balance
                )
                if recovery:
                    self._diag(session_id, symbol,
                               f"RECOVERY {recovery.order_type} {recovery.lot}@{recovery.entry}")
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
            detail = str(e)[:400]
            logger.warning(f"[ScalpBot] session {session_id} order REJECTED: {detail}")
            self._diag(session_id, symbol, f"order-rejected {order_type} {entry.lot}@{limit_price}: {detail}")
            await self._update_phase(session_id, "waiting", note=f"Order rejected: {detail}")
            return

        ticket = _order_ticket(result) if isinstance(result, dict) else 0
        # Some mtapi-io builds acknowledge the order without echoing a ticket —
        # recover it from the resting pending orders by our unique comment.
        if not ticket:
            ticket = await self._recover_ticket_by_comment(login, server, password, comment) or 0

        # No ticket at all → treat as a rejection (capture broker detail) and do
        # NOT leave a phantom leg that would trigger duplicate placement.
        if not ticket:
            detail = ""
            if isinstance(result, dict):
                detail = str(result.get("comment") or result.get("description")
                             or result.get("retcode") or result)[:400]
            logger.warning(f"[ScalpBot] session {session_id} order returned no ticket: {detail}")
            self._diag(session_id, symbol, f"order-no-ticket {order_type} {entry.lot}@{limit_price}: {detail}")
            await self._update_phase(session_id, "waiting",
                                     note=f"Order not accepted: {detail or 'no ticket returned'}")
            return

        ticket = int(ticket)
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
                # Resting pending order — flipped to "open" once it fills.
                status="pending",
            )
            db.add(trade)
            session.total_trades = (session.total_trades or 0) + 1
            if recovery:
                session.trade2_ticket = ticket
                session.phase = "recovery"
            else:
                session.trade1_ticket = ticket
                session.phase = "entry_pending"
            session.ai_note = (note or "")[:1000]
            session.last_cycle_at = datetime.utcnow()
            await db.commit()
        logger.info(
            f"[ScalpBot] session {session_id} placed {order_type} {entry.lot} "
            f"@{entry.entry} SL {entry.stop_loss} TP {entry.take_profit} "
            f"ticket={ticket} recovery={recovery}"
        )

    async def _recover_ticket_by_comment(self, login: str, server: str, password: str,
                                         comment: str) -> Optional[int]:
        """Find a just-placed pending order's ticket by its unique comment."""
        try:
            pend = await mt5_client.get_pending_orders(login, server, password)
        except Exception:  # noqa: BLE001
            return None
        matches = [
            _order_ticket(o) for o in pend
            if str(o.get("comment", "")).strip() == comment
        ]
        matches = [m for m in matches if m]
        return max(matches) if matches else None

    async def _session_symbol(self, session_id: int) -> str:
        async with AsyncSessionLocal() as db:
            s = await db.get(MT5ScalpSession, session_id)
            return s.symbol if s else ""

    async def _close_all_session_trades(self, session_id: int, login: str, server: str,
                                        password: str, symbol: str, record: bool = False) -> None:
        """Close filled legs, cancel resting pending legs, and roll up realised PnL."""
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            tickets = [int(t) for t in (session.trade1_ticket, session.trade2_ticket) if t]

        # Classify each tracked ticket as a filled position or a resting pending
        # order so we call the right MT5 op (close vs cancel).
        try:
            all_orders = await mt5_client.get_orders(login, server, password)
        except Exception:  # noqa: BLE001
            all_orders = []
        pnl_by_ticket: Dict[int, float] = {}
        price_by_ticket: Dict[int, float] = {}
        pending_tickets: set = set()
        for o in all_orders:
            tk = _order_ticket(o)
            if not tk:
                continue
            if is_pending_order(o):
                pending_tickets.add(tk)
            else:
                pnl_by_ticket[tk] = float(o.get("profit", 0.0) or 0.0)
                price_by_ticket[tk] = _order_open_price(o) or float(o.get("currentPrice", 0.0) or 0.0)

        realised = 0.0
        for tk in tickets:
            try:
                if tk in pending_tickets:
                    await mt5_client.cancel_order(login, server, password, tk)
                else:
                    await mt5_client.close_position(login, server, password, tk)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[ScalpBot] close/cancel {tk} failed: {e}")
            realised += pnl_by_ticket.get(tk, 0.0)

        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            for tk in tickets:
                rows = await db.execute(
                    select(MT5ScalpTrade).where(
                        MT5ScalpTrade.session_id == session_id,
                        MT5ScalpTrade.ticket == int(tk),
                        MT5ScalpTrade.status.in_(["open", "pending"]),
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

    async def _reconcile_gone(self, session_id: int, tickets: List[int]) -> None:
        """Clear tickets that are genuinely gone (SL/TP-closed or cancelled).

        Only called for tickets that were NOT found in either live positions or
        resting pending orders — so a pending entry is never mistaken for closed.
        """
        if not tickets:
            return
        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            for tk in tickets:
                rows = await db.execute(
                    select(MT5ScalpTrade).where(
                        MT5ScalpTrade.session_id == session_id,
                        MT5ScalpTrade.ticket == int(tk),
                        MT5ScalpTrade.status.in_(["open", "pending"]),
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
            session.last_cycle_at = datetime.utcnow()
            await db.commit()

    async def _mark_trade_filled(self, session_id: int, ticket: int) -> None:
        """Flip a resting pending trade row to 'open' once the broker fills it."""
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(MT5ScalpTrade).where(
                    MT5ScalpTrade.session_id == session_id,
                    MT5ScalpTrade.ticket == int(ticket),
                    MT5ScalpTrade.status == "pending",
                )
            )
            changed = False
            for tr in rows.scalars().all():
                tr.status = "open"
                changed = True
            if changed:
                await db.commit()

    async def _pending_is_stale(self, session_id: int, ticket: int) -> bool:
        """True when a resting pending entry has outlived ``PENDING_TTL_SECONDS``."""
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(MT5ScalpTrade).where(
                    MT5ScalpTrade.session_id == session_id,
                    MT5ScalpTrade.ticket == int(ticket),
                ).order_by(MT5ScalpTrade.id.desc()).limit(1)
            )
            tr = rows.scalar_one_or_none()
        if not tr or not tr.opened_at:
            return False
        age = (datetime.utcnow() - tr.opened_at).total_seconds()
        return age >= PENDING_TTL_SECONDS

    def _diag(self, session_id: int, symbol: str, msg: str) -> None:
        """Structured per-cycle telemetry so a live session is fully traceable."""
        logger.info(f"[ScalpBot#{session_id}] {symbol} :: {msg}")

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
