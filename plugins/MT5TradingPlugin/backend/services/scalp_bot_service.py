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
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from plugins.MT5TradingPlugin.backend.models import (
    MT5Account,
    MT5Deal,
    MT5DealType,
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
    DEFAULT_STRICTNESS,
    VOL_SPIKE_STRONG,
    get_tf_stack,
    get_entry_refine_tf,
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

# Maximum concurrent open/pending orders per scalp session (configurable via
# raw_settings["max_open_orders"]).  Default 2 = primary + optional recovery/spike.
MAX_OPEN_ORDERS_DEFAULT: int = 2

_STRICTNESS_ORDER = ["scalper", "aggressive", "balanced", "conservative"]
_RR_RE = re.compile(r"rr=([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_KRONOS_RE = re.compile(r"kronos=([+-]?[0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_VOL_IMB_RE = re.compile(r"vol-imb:([+-]?[0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)

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


#: A predicted move of this size (in percent) is treated as a fully decisive
#: forecast — it maps to a score of ±1.0 before the confidence weighting.
#:
#: Anchored on the forecast service's own ``_FLAT_PCT`` (0.15%), the threshold
#: below which it says the paths are not directional at all. Three times that is
#: a move the model is clearly committing to. Using an absolute percentage keeps
#: this readable, and the scalp timeframes (M1–M15) all live in the same range.
_KRONOS_DECISIVE_PCT = 0.45


async def _kronos_direction(candles: List[Candle], symbol: str, timeframe: str) -> float:
    """
    Optional Kronos ML directional score (-1..1). 0 when unavailable.

    Positive → forecast up, negative → down. Fully graceful: any failure or a
    missing plugin returns 0 so the scalp engine's SMC decision stands alone.

    Scaling note — this used to be ``pct / 100.0 * (0.5 + conf)``, which converted
    the percentage to a fraction and left the score ~100× smaller than the scale
    it is measured against. The engine's veto thresholds are 0.25–0.70, so a veto
    needed a *21.8% move on a 5-minute horizon* — unreachable — and the alignment
    bonus came out around 0.0001. Kronos was wired in everywhere but could never
    change a single decision. The move is now normalised against a decisive move
    for these timeframes, so the score actually spans the range the engine uses.
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
        return kronos_score_from(pct, conf)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ScalpBot] kronos direction {symbol}: {exc}")
        return 0.0


def kronos_score_from(pct_change: float, confidence: float) -> float:
    """Map a Kronos forecast onto the engine's -1..1 conviction scale.

    Magnitude carries the direction and how far the move is toward a decisive
    one; confidence scales it, so a hesitant forecast of a large move and a
    confident forecast of a small one both land mid-scale rather than at an
    extreme. Confidence alone can never create conviction out of a zero move.
    """
    if not pct_change:
        return 0.0
    magnitude = min(1.0, abs(pct_change) / _KRONOS_DECISIVE_PCT)
    # Map confidence 0..1 onto 0.5..1.0 — an uncertain forecast still counts for
    # something, since the model has committed to a side.
    weight = 0.5 + 0.5 * max(0.0, min(1.0, confidence))
    score = magnitude * weight
    return round(max(-1.0, min(1.0, score if pct_change > 0 else -score)), 4)


async def _ai_gate(symbol: str, side: str, bias_reason: str, confidence: float) -> Dict[str, Any]:
    """
    Multi-AI ensemble confirmation gate — queries ALL configured AI providers
    in PARALLEL and uses majority vote for higher accuracy.

    Also integrates:
    - Jarvis intelligence context (telegram signals, knowledge base)
    - Previous scalp learnings for self-improvement
    - Economic calendar events

    Returns ``{"decision": "take"|"skip", "note": str, "votes": dict}``.
    Fails open (``take``) if no AI provider is configured.
    """
    # ── Gather Jarvis + telegram + learning context ───────────────────────────
    jarvis_ctx = await _gather_scalp_intelligence(symbol, side)

    # ── Fetch economic calendar ───────────────────────────────────────────────
    eco_context = ""
    try:
        from plugins.MT5TradingPlugin.backend.services.smc_ai import fetch_economic_events  # type: ignore
        eco_events = await fetch_economic_events(symbol)
        upcoming = [e for e in (eco_events or []) if -2 <= e.get("hours_away", 99) <= 24]
        if upcoming:
            eco_context = (
                "\nUpcoming high-impact events: "
                + ", ".join(
                    f"{e['title']} ({e['currency']}) in {e['hours_away']:.1f}h"
                    + (f" [prev={e['previous']}, fcst={e['forecast']}]"
                       if e.get("previous") or e.get("forecast") else "")
                    for e in upcoming[:4]
                ) + "."
            )
    except Exception:
        pass

    prompt = (
        f"You are an elite scalping risk filter. Instrument: {symbol}. "
        f"The scalp engine wants a {side.upper()} pending limit scalp "
        f"(confidence {confidence:.2f}).\n"
        f"Multi-timeframe bias: {bias_reason}.{eco_context}\n"
        + (f"Market intelligence context:\n{jarvis_ctx}\n" if jarvis_ctx else "")
        + "Reply STRICT JSON only: "
        '{"decision":"take"|"skip","note":str}. '
        "Skip ONLY for: imminent high-impact news (<2h), confirmed opposing HTF trend, "
        "active telegram signals in OPPOSITE direction for this pair. "
        "Otherwise DEFAULT to take — the SMC engine already filters most bad setups."
    )

    # ── Ensemble: call ALL providers in parallel ──────────────────────────────
    return await _ensemble_vote(symbol, side, prompt)


async def _gather_scalp_intelligence(symbol: str, side: str) -> str:
    """
    Gather context from Jarvis brains + telegram signals + scalp learnings.
    Returns a compact string for the AI gate prompt.
    """
    import os as _os
    base = "http://127.0.0.1:{}".format(_os.environ.get("BACKEND_PORT", "1448"))
    parts: List[str] = []

    async with __import__("httpx").AsyncClient(timeout=5.0) as client:
        # ── 1. Telegram signals for this symbol ────────────────────────────────
        try:
            sym_bare = symbol.replace("USD", "").replace("USDT", "").upper()
            r = await client.get(
                f"{base}/api/v1/plugins/telegram/signals",
                params={"limit": 5},
            )
            if r.status_code == 200:
                sigs = r.json() or []
                # Filter signals for our pair (last 6h)
                matching = []
                for s in sigs:
                    s_sym = (s.get("symbol") or "").upper()
                    if sym_bare in s_sym or symbol.upper()[:6] in s_sym:
                        matching.append(s)
                if matching:
                    sig_lines = []
                    for s in matching[:3]:
                        dir_ = s.get("direction", "?").upper()
                        chan = s.get("channel_title", "?")
                        conf = s.get("confidence", 0)
                        sig_lines.append(f"{chan}: {dir_} conf={conf:.0%}")
                    parts.append("Telegram signals: " + " | ".join(sig_lines))
        except Exception:
            pass

        # ── 2. Obsidian knowledge context for this symbol ─────────────────────
        try:
            r = await client.get(f"{base}/api/v1/plugins/obsidian-knowledge/context/{symbol}")
            if r.status_code == 200:
                ctx = r.json() or {}
                summary = ctx.get("summary") or ctx.get("description") or ""
                if summary and len(summary) > 10:
                    parts.append(f"Knowledge context: {summary[:200]}")
        except Exception:
            pass

        # ── 3. Recent scalp learnings from AI knowledge base ──────────────────
        try:
            r = await client.get(
                f"{base}/api/v1/plugins/ai-analyst/ai/knowledge",
            )
            if r.status_code == 200:
                resp_data = r.json() or {}
                # API returns either {"items": [...]} or [...] directly
                knowledge = (
                    resp_data.get("items", []) if isinstance(resp_data, dict)
                    else (resp_data if isinstance(resp_data, list) else [])
                )
                scalp_learnings = [
                    k for k in knowledge
                    if "scalp" in (k.get("kind") or "").lower()
                    or symbol.upper() in (k.get("title") or "").upper()
                ]
                if scalp_learnings:
                    lessons = []
                    for k in scalp_learnings[:3]:
                        content = (k.get("content") or "")[:120]
                        if content:
                            lessons.append(content)
                    if lessons:
                        parts.append("Past scalp learnings: " + " | ".join(lessons))
        except Exception:
            pass

    return "\n".join(parts) if parts else ""


async def _ensemble_vote(symbol: str, side: str, prompt: str) -> Dict[str, Any]:
    """
    Route the ensemble AI gate through the shared AI router (db_chat).

    Previously called all providers in parallel via _call_openai_compatible
    directly — this bypassed the router and OpenManus integration.
    Now routes through db_chat so OpenManus MCP is tried first (if enabled),
    falling back to the priority-ordered provider pool on failure.

    Returns {"decision": "take"|"skip", "note": str, "votes": {provider: decision}}.
    """
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            db_chat,  # type: ignore
        )
    except Exception:
        return {"decision": "take", "note": "ai_router_unavailable", "votes": {}}

    import json as _json

    messages = [{"role": "user", "content": prompt}]

    async with AsyncSessionLocal() as db:
        result = await db_chat(
            db,
            messages,
            temperature=0.1,
            max_tokens=200,
            json_mode=True,
            agent_name="scalp-ensemble-vote",
            source="scalp-ensemble",
        )

    if not result.get("ok") or not result.get("content"):
        logger.debug(f"[ScalpBot] ensemble vote failed for {symbol}/{side}")
        return {"decision": "take", "note": "router_failed", "votes": {}}

    try:
        content = result["content"]
        parsed = _json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, dict):
            dec = str(parsed.get("decision", "take")).lower()
            if dec not in ("take", "skip"):
                dec = "take"
            note = str(parsed.get("note", ""))[:200]
            provider = result.get("provider") or result.get("routed_via", "unknown")
            return {"decision": dec, "note": note, "votes": {provider: dec}}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ScalpBot] ensemble vote parse error {symbol}: {exc}")

    return {"decision": "take", "note": "parse_error", "votes": {}}


async def _store_scalp_outcome_to_brain(
    session_id: int,
    symbol: str,
    side: str,
    entry_price: float,
    close_price: float,
    pnl: float,
    bias_reason: str,
    confidence: float,
    strictness: str = "scalper",
) -> None:
    """
    Store a closed scalp trade outcome to ALL knowledge brains for self-improvement:
    1. Obsidian vault via jarvis-learn endpoint (markdown note in /trades/ folder)
    2. AI knowledge base (ai-analyst/ai/knowledge) for future AI gate learning

    This drives the self-improvement loop:
    - Wins teach WHAT to look for (winning signal patterns)
    - Losses teach WHAT to avoid (false setups, choppy conditions)
    """
    import os as _os
    base = "http://127.0.0.1:{}".format(_os.environ.get("BACKEND_PORT", "1448"))

    outcome = "WIN" if pnl >= 0 else "LOSS"
    pnl_str = f"{pnl:+.2f}"
    move_pips = abs(close_price - entry_price) if entry_price > 0 and close_price > 0 else 0.0

    # Compact lesson derived from the trade outcome
    if pnl >= 0:
        lesson = (
            f"✅ {symbol} {side.upper()} SCALP WON {pnl_str}. "
            f"Signals that worked: {bias_reason[:300]}. "
            f"Key: price moved {move_pips:.2f} in our favour."
        )
    else:
        lesson = (
            f"❌ {symbol} {side.upper()} SCALP LOST {pnl_str}. "
            f"Signals present at entry: {bias_reason[:300]}. "
            f"Review: these conditions may have been choppy or conflicting."
        )

    async with __import__("httpx").AsyncClient(timeout=8.0) as client:
        # ── 1. Jarvis-learn (Obsidian vault) ─────────────────────────────────
        try:
            await client.post(
                f"{base}/api/v1/plugins/obsidian-knowledge/jarvis-learn",
                json={
                    "question": f"What happened on this {symbol} scalp trade?",
                    "answer": lesson,
                    "tags": ["scalp", symbol.lower(), side, outcome.lower(), strictness],
                },
            )
        except Exception as exc:
            logger.debug(f"[ScalpBot] jarvis-learn store {symbol}: {exc}")

        # ── 2. AI knowledge base ──────────────────────────────────────────────
        try:
            await client.post(
                f"{base}/api/v1/plugins/ai-analyst/ai/knowledge",
                json={
                    "title": f"Scalp {outcome}: {symbol} {side.upper()} session#{session_id}",
                    "content": lesson,
                    "kind": "scalp_outcome",
                    "symbol": symbol,
                    "weight": 2.0 if abs(pnl) > 1.0 else 1.0,
                    "source": "scalp_bot",
                    "agent_role": "scalp_learner",
                },
            )
        except Exception as exc:
            logger.debug(f"[ScalpBot] knowledge store {symbol}: {exc}")

    logger.info(
        f"[ScalpBot] 🧠 Trade outcome stored → brain: {symbol} {side.upper()} "
        f"{outcome} {pnl_str} (session#{session_id})"
    )


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


async def _trigger_jarvis_harvest(base: str) -> None:
    """
    Fire a Jarvis intelligence harvest so brains stay fresh with:
    - Latest telegram signals and news
    - Recent AI agent decisions
    - Sentiment data
    Called every 5 minutes from each scalp loop.
    """
    try:
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            await client.post(f"{base}/api/v1/plugins/ai-analyst/ai/harvest")
    except Exception as exc:
        logger.debug(f"[ScalpBot] jarvis harvest trigger: {exc}")


# ── Manager ─────────────────────────────────────────────────────────────────────

class ScalpBotManager:
    """Owns one asyncio task per active scalp session (keyed by session id)."""

    def __init__(self) -> None:
        self._tasks: Dict[int, asyncio.Task] = {}
        self._resumed: bool = False
        # Maps (session_id, ticket) → best SL price we have applied as a trailing
        # stop.  Used by _maybe_trail_sl to guarantee the SL only advances in
        # the profitable direction (never pulled backward).
        self._trailing_sl: Dict[tuple, float] = {}

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

    async def pause(self, session_id: int) -> None:
        """Halt a session loop but leave it resumable, and leave trades alone.

        The difference from ``stop`` is the whole point of this method: STOPPED
        is terminal and the user has to rebuild the bot from the pair picker,
        whereas PAUSED keeps the row — symbol, lot, risk, strictness, direction,
        every setting in ``raw_settings`` — so ``resume`` can put the same bot
        back without the user re-selecting anything.

        Open positions are NOT closed. A paused bot stops *deciding*, it does
        not liquidate; closing is what ``stop`` and close-all are for.
        """
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._set_status(session_id, MT5ScalpSessionStatus.PAUSED, phase="paused")
        logger.info(f"[ScalpBot] session {session_id} paused (trades left open)")

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
        """On backend startup: PAUSE any ACTIVE sessions so the user must
        explicitly click Start to resume.  This prevents silent order placement
        on a live account after a process restart without user confirmation.

        The app uses a ``lifespan`` handler, so router ``on_event("startup")``
        hooks never fire; this is triggered from the frequently-polled scalp
        status endpoint on first load.
        """
        if self._resumed:
            return
        self._resumed = True
        try:
            n = await self._pause_active_on_restart()
            if n:
                logger.warning(
                    f"[ScalpBot] {n} session(s) were ACTIVE before restart — "
                    "PAUSED for safety. User must click Start to resume."
                )
        except Exception as e:  # noqa: BLE001
            self._resumed = False  # allow a later retry if the first attempt failed
            logger.warning(f"[ScalpBot] startup-pause failed: {e}")

    async def _pause_active_on_restart(self) -> int:
        """Transition ACTIVE-but-not-running sessions to PAUSED on backend restart.

        Only sessions whose asyncio loop is NOT currently running are affected
        so that sessions started within the same process lifetime are untouched.
        """
        paused = 0
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(MT5ScalpSession).where(
                    MT5ScalpSession.status == MT5ScalpSessionStatus.ACTIVE
                )
            )
            sessions = rows.scalars().all()
            for s in sessions:
                if not self.is_running(s.id):
                    s.status = MT5ScalpSessionStatus.PAUSED
                    s.phase = "paused"
                    s.ai_note = (
                        "⚠️ Paused after backend restart — "
                        "click Start to resume trading on this session"
                    )[:1000]
                    paused += 1
            if paused:
                await db.commit()
        return paused

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
        """Main loop. Triggers a Jarvis intelligence harvest every 5 minutes
        so the AI gate always has fresh telegram signals, sentiment, and knowledge."""
        import os as _os
        _harvest_base = "http://127.0.0.1:{}".format(_os.environ.get("BACKEND_PORT", "1448"))
        _last_harvest = 0.0
        _HARVEST_INTERVAL = 300.0  # 5 minutes
        try:
            while True:
                # ── Periodic Jarvis harvest (refresh brains) ──────────────────
                import time as _time
                _now = _time.monotonic()
                if _now - _last_harvest > _HARVEST_INTERVAL:
                    _last_harvest = _now
                    asyncio.ensure_future(_trigger_jarvis_harvest(_harvest_base))

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
                # Strictness preset lives in raw_settings (no dedicated column).
                "strictness": (
                    (session.raw_settings or {}).get(
                        "adaptive_strictness",
                        (session.raw_settings or {}).get("strictness", DEFAULT_STRICTNESS),
                    )
                    if isinstance(session.raw_settings, dict) else DEFAULT_STRICTNESS
                ),
                "raw_settings": (session.raw_settings or {}) if isinstance(session.raw_settings, dict) else {},
                # Max concurrent open+pending orders for this session.
                "max_open_orders": int(
                    ((session.raw_settings or {}).get("max_open_orders", MAX_OPEN_ORDERS_DEFAULT))
                    if isinstance(session.raw_settings, dict) else MAX_OPEN_ORDERS_DEFAULT
                ),
                # Direction filter: "buy" = only longs, "sell" = only shorts, "both" = no filter.
                "allowed_direction": str(
                    ((session.raw_settings or {}).get("allowed_direction", "both"))
                    if isinstance(session.raw_settings, dict) else "both"
                ),
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

        # ── Today-loss analysis + prevention loop ───────────────────────────
        prevention = await self._analyse_today_and_apply_prevention(
            session_id=session_id,
            account_id=int(account.id),
            symbol=symbol,
            current_strictness=cfg["strictness"],
        )
        cfg["strictness"] = prevention.get("strictness", cfg["strictness"])
        if prevention.get("cooldown_active"):
            msg = prevention.get("note", "Protective cooldown active")
            self._diag(session_id, symbol, f"prevention cooldown :: {msg}")
            await self._update_phase(session_id, "waiting", note=msg)
            return False

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
        # Fetch only the TFs needed by the selected primary scalp timeframe.
        session_tfs = get_tf_stack(cfg["timeframe"])
        for tf in session_tfs:
            candles_by_tf[tf] = await _load_tf_candles(
                login, server, password, symbol, tf, mid_hint=mid,
            )
        primary_tf = cfg["timeframe"]
        primary_candles = candles_by_tf.get(primary_tf) or []
        if mid <= 0:
            mid = primary_candles[-1].close if primary_candles else 0.0
        if len(primary_candles) < MIN_CANDLES:
            self._diag(session_id, symbol,
                       f"warming-up {primary_tf}={len(primary_candles)}/{MIN_CANDLES}")
            await self._update_phase(
                session_id, "analyzing",
                note=f"Building live candles ({len(primary_candles)}/{MIN_CANDLES} {primary_tf} bars)")
            return False

        engine = ScalpStrategyEngine(
            symbol=symbol,
            lot_size=cfg["lot_size"],
            auto_lot=cfg["auto_lot"],
            risk_per_trade_pct=cfg["risk_per_trade_pct"],
            strictness=cfg["strictness"],
            primary_tf=cfg["timeframe"],
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
            await self._reconcile_gone(session_id, gone, login, server, password)
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
            f"tf={cfg['timeframe']} t1={t1_state} t2={t2_state} combPnL={combined_pnl:+.2f} "
            f"pos={len(pos_by_ticket)} pend={len(pend_by_ticket)}"
        )

        # ── State machine ────────────────────────────────────────────────────
        if not cfg["trade1_ticket"]:
            # Flat → look for a fresh entry.
            # Run the Kronos ML forecast FIRST so it is fused into the entry
            # decision (agreement lifts conviction, strong opposition vetoes),
            # not merely applied as an afterthought.
            kd = 0.0
            if cfg["use_kronos"]:
                kd = await _kronos_direction(primary_candles, symbol, cfg["timeframe"])

            entry, bias = engine.analyse(
                candles_by_tf, mid, balance, bid=bid, ask=ask, kronos_score=kd,
            )
            note = bias.reason
            await self._store_bias(session_id, bias, "analyzing" if not entry else "waiting")
            if not entry:
                # Distinguish a Kronos veto (surfaced in bias.reason) from a
                # plain no-setup so the UI/telemetry is transparent.
                self._diag(
                    session_id, symbol,
                    f"no-entry bias={bias.direction} conf={bias.confidence:.2f} "
                    f"kronos={kd:+.2f} :: {bias.reason}"
                )
                if "Kronos veto" in bias.reason:
                    await self._update_phase(
                        session_id, "waiting",
                        note=f"Kronos opposes ({kd:+.2f}) — waiting")
                return False

            # ── Direction filter ─────────────────────────────────────────────
            # Respect the user's "allowed_direction" setting:
            #   "buy"  → only take BUY entries
            #   "sell" → only take SELL entries
            #   "both" → trade both directions (default)
            allowed_dir = cfg.get("allowed_direction", "both")
            if allowed_dir != "both" and entry.side != allowed_dir:
                self._diag(
                    session_id, symbol,
                    f"direction-filter: entry is {entry.side.upper()} but "
                    f"allowed_direction={allowed_dir.upper()} — skipping"
                )
                await self._update_phase(
                    session_id, "waiting",
                    note=f"Direction filter: only {allowed_dir.upper()} allowed — "
                         f"skipped {entry.side.upper()} setup"
                )
                return False

            # ── Max-open-orders guard (account-wide for this symbol) ──────────
            # Count ALL open + pending orders for this symbol on this account
            # (not just the 2 session tickets) so the cap applies globally.
            sym_upper = symbol.upper()
            symbol_orders_count = sum(
                1 for o in all_orders
                if (o.get("symbol") or o.get("Symbol") or "").upper().replace("/", "") ==
                   sym_upper.replace("/", "")
            )
            # Also count session-tracked tickets even if not returned yet by get_orders
            session_tracked = sum([
                1 if cfg["trade1_ticket"] else 0,
                1 if cfg["trade2_ticket"] else 0,
            ])
            total_open = max(symbol_orders_count, session_tracked)
            if total_open >= cfg["max_open_orders"]:
                self._diag(session_id, symbol,
                           f"max-orders guard: {total_open}/{cfg['max_open_orders']} orders for {symbol} — skipping")
                await self._update_phase(session_id, "waiting",
                                         note=f"Max open orders reached ({total_open}/{cfg['max_open_orders']})")
                return False

            # ── Fusion quality gate ──────────────────────────────────────────
            # The engine already vetoes strong Kronos opposition and enforces a
            # reward:risk floor. Here the bot enforces the strictness preset's
            # composite quality threshold so only high-conviction, well-shaped
            # setups reach the broker.
            if entry.quality_score < engine.min_fusion_score:
                self._diag(
                    session_id, symbol,
                    f"quality-gate skip q={entry.quality_score:.2f} "
                    f"< {engine.min_fusion_score:.2f} ({cfg['strictness']}) "
                    f"rr={entry.rr:.1f} conf={entry.confidence:.2f} kronos={kd:+.2f}"
                )
                await self._update_phase(
                    session_id, "waiting",
                    note=(f"Setup quality {entry.quality_score:.2f} below "
                          f"{cfg['strictness']} floor {engine.min_fusion_score:.2f} "
                          f"(RR {entry.rr:.1f}, conf {entry.confidence:.2f})"))
                return False

            # Optional AI + economic-calendar gate.
            fusion_note = (
                f"q={entry.quality_score:.2f} rr={entry.rr:.1f} "
                f"conf={entry.confidence:.2f} kronos={kd:+.2f}"
            )
            if cfg["use_ai"]:
                gate = await _ai_gate(symbol, entry.side, bias.reason, entry.confidence)
                note = f"{bias.reason} | {fusion_note} | AI: {gate.get('note', '')}"
                if gate.get("decision") == "skip":
                    self._diag(session_id, symbol, f"ai-skip {gate.get('note','')}")
                    await self._update_phase(session_id, "waiting", note=f"AI skip: {gate.get('note','')}")
                    return False
            else:
                note = f"{bias.reason} | {fusion_note}"

            self._diag(
                session_id, symbol,
                f"ENTRY {entry.order_type} {entry.lot}@{entry.entry} "
                f"SL{entry.stop_loss} TP{entry.take_profit} {fusion_note}"
            )
            await self._open_trade(session_id, login, server, password, entry, note)

            # ── Volume spike stacking ────────────────────────────────────────
            # When directional volume imbalance is a strong spike AND there is
            # still an order slot available, place a second continuation order
            # at a wider entry to capitalise on extended momentum.
            spike_side = entry.side
            spike_imb = bias.volume_imbalance
            spike_detected = (
                (spike_side == "sell" and spike_imb <= -VOL_SPIKE_STRONG)
                or (spike_side == "buy"  and spike_imb >= VOL_SPIKE_STRONG)
            )
            if spike_detected and (cfg["max_open_orders"] >= 2) and not cfg["trade2_ticket"] and total_open + 1 < cfg["max_open_orders"]:
                stack = engine.build_spike_stack_entry(
                    side=spike_side, current_price=mid,
                    candles_by_tf=candles_by_tf, balance=balance,
                    bias=bias, bid=bid, ask=ask,
                )
                if stack:
                    self._diag(
                        session_id, symbol,
                        f"SPIKE-STACK {stack.order_type} {stack.lot}@{stack.entry} "
                        f"SL{stack.stop_loss} TP{stack.take_profit} imb={spike_imb:+.2f}"
                    )
                    await self._open_trade(
                        session_id, login, server, password, stack,
                        f"Spike stack — imb={spike_imb:+.2f}", recovery=False,
                    )
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
                await self._reconcile_gone(session_id, [int(cfg["trade1_ticket"])], login, server, password)
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

        # ── Trailing SL ──────────────────────────────────────────────────────
        # Once the trade has gained ≥ 80 % of the session target, lock 50 % of
        # the current profit by moving the SL to the break-even + 50 % price.
        # Every subsequent cycle the SL is recalculated and advanced (never
        # retreated) so the locked fraction grows with the running profit.
        if target_amt > 0 and p1_profit >= 0.8 * target_amt and cfg["trade1_ticket"]:
            await self._maybe_trail_sl(
                session_id, login, server, password, symbol,
                ticket=int(cfg["trade1_ticket"]),
                side=p1_side,
                open_price=p1_open,
                lot=p1_lot,
                current_profit=p1_profit,
            )

        # Arm the recovery leg when the filled trade is meaningfully offside.
        if cfg["recovery_enabled"] and mid > 0 and p1_open > 0:
            _m5_candles = candles_by_tf.get(primary_tf) or []
            atr_m5 = _atr(_m5_candles)
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

    async def _maybe_trail_sl(
        self,
        session_id: int,
        login: str,
        server: str,
        password: str,
        symbol: str,
        ticket: int,
        side: str,
        open_price: float,
        lot: float,
        current_profit: float,
    ) -> None:
        """
        Trail the SL on the primary scalp position to always lock 50 % of the
        running profit once 80 % of the session target has been reached.

        Algorithm
        ---------
        1. Derive the *price distance* whose P&L equals 50 % of current_profit:
              lock_profit = 0.5 × current_profit
              price_to_lock = lock_profit / (lot × contract_size)
        2. Place the SL at open_price ± price_to_lock (+ for BUY, − for SELL).
        3. Only ever advance the SL in the favourable direction — once set, it is
           never moved backward regardless of whether profit dips between cycles.
        4. Snap the computed price to the symbol's broker tick size to avoid
           invalid-price rejections.
        """
        if lot <= 0 or open_price <= 0 or current_profit <= 0:
            return

        from plugins.MT5TradingPlugin.backend.services.smc_strategy import (  # type: ignore
            contract_size_for_symbol,
            point_size_for_symbol,
        )
        contract = contract_size_for_symbol(symbol)
        if contract <= 0:
            return

        # Price distance that, at close, yields 50 % of the current profit.
        lock_profit = 0.5 * current_profit
        price_to_lock = lock_profit / (lot * contract)

        # SL price that captures lock_profit if the trade closes at that level.
        new_sl = (open_price + price_to_lock) if side == "buy" else (open_price - price_to_lock)

        # Snap to broker tick (avoids INVALID_PRICE broker rejects).
        ps = point_size_for_symbol(symbol)
        if ps > 0:
            new_sl = round(new_sl / ps) * ps

        # Trail-forward-only: never move the SL in the loss direction.
        state_key = (session_id, ticket)
        prior = self._trailing_sl.get(state_key)
        if prior is not None:
            if side == "buy" and new_sl <= prior:
                return  # Would retreat the SL on a long
            if side == "sell" and new_sl >= prior:
                return  # Would retreat the SL on a short

        # Push the updated SL to the broker.
        # On first activation also clear the fixed TP (pass tp=0) so the
        # trailing stop becomes the sole exit — the TP won't cut profit short
        # before the SL has had a chance to trail up.
        is_first = prior is None
        tp_to_set: Optional[float] = 0.0 if is_first else None
        try:
            await mt5_client.modify_order(login, server, password, ticket, sl=new_sl, tp=tp_to_set)
            self._trailing_sl[state_key] = new_sl
            prior_txt = f" prev_sl={prior:.5f}" if prior is not None else " (first activation — TP cleared)"
            self._diag(
                session_id, symbol,
                f"TRAIL-SL ticket={ticket} side={side} new_sl={new_sl:.5f} "
                f"locked={lock_profit:.2f} profit={current_profit:.2f}{prior_txt}",
            )
            await self._update_phase(
                session_id,
                "in_trade",
                note=(
                    f"Trailing SL active{' — TP removed' if is_first else ''} "
                    f"— 50% locked ({lock_profit:.2f}) @ {new_sl:.5f}"
                    f" | running P&L {current_profit:.2f}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ScalpBot] trail SL modify ticket={ticket}: {exc}")

    async def _open_trade(self, session_id: int, login: str, server: str, password: str,
                          entry: ScalpEntry, note: str, recovery: bool = False) -> None:
        comment = f"{COMMENT_TAG}{'-R' if recovery else ''}#{session_id}"
        symbol = await self._session_symbol(session_id)

        # ── Hard lot-size sanity guard ─────────────────────────────────────────────
        # The ScalpStrategyEngine already applies an auto-lot cap, but we add a
        # second, independent check here as the last line of defence before any
        # volume reaches the broker.  An entry whose lot exceeds the configured
        # base lot by more than 50× is almost certainly a calculation error
        # (e.g. ATR=0 from stale candles) and is unconditionally rejected.
        try:
            from app.core.database import AsyncSessionLocal as _ASL
            async with _ASL() as _db:
                _sess = await _db.get(
                    __import__('plugins.MT5TradingPlugin.backend.models',
                               fromlist=['MT5ScalpSession']).MT5ScalpSession,
                    session_id,
                )
                _base_lot = float(getattr(_sess, 'lot_size', entry.lot) or entry.lot)
        except Exception:
            _base_lot = entry.lot

        _max_safe_lot = max(_base_lot * 50.0, 1.0)  # never more than 50× configured lot
        if entry.lot > _max_safe_lot:
            logger.critical(
                f"[ScalpBot♯{session_id}] LOT-SIZE GUARD TRIGGERED — "
                f"computed lot={entry.lot} exceeds 50× base ({_base_lot}) for {symbol}. "
                f"Order ABORTED. Check auto_lot / SL distance / candle quality."
            )
            await self._update_phase(
                session_id, "waiting",
                note=f"ABORTED: lot {entry.lot} exceeds safety limit ({_max_safe_lot:.2f}). "
                     "Disable auto-lot or verify candle data.",
            )
            return

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
            brain_records_close: List[Dict[str, Any]] = []
            sym_close = session.symbol or "UNKNOWN"
            close_strictness = (
                (session.raw_settings or {}).get("strictness", "scalper")
                if isinstance(session.raw_settings, dict) else "scalper"
            )
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
                    if int(tk) in pnl_by_ticket:
                        brain_records_close.append({
                            "side": tr.side or "buy",
                            "entry": float(tr.entry_price or 0.0),
                            "close": float(tr.close_price or 0.0),
                            "pnl": tr.pnl,
                            "confidence": float(tr.confidence or 0.0),
                            "bias_reason": tr.reason or "",
                        })
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
        # Purge trailing SL state for all closed tickets.
        for tk in tickets:
            self._trailing_sl.pop((session_id, tk), None)
        # Store outcomes to brains (fire-and-forget)
        if record:
            for rec in brain_records_close:
                asyncio.ensure_future(
                    _store_scalp_outcome_to_brain(
                        session_id=session_id,
                        symbol=sym_close,
                        side=rec["side"],
                        entry_price=rec["entry"],
                        close_price=rec["close"],
                        pnl=rec["pnl"],
                        bias_reason=rec["bias_reason"],
                        confidence=rec["confidence"],
                        strictness=close_strictness,
                    )
                )

    async def _reconcile_gone(
        self,
        session_id: int,
        tickets: List[int],
        login: Optional[str] = None,
        server: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Clear tickets that are genuinely gone (SL/TP-closed or cancelled).

        When broker credentials are supplied, queries the recent deal/order
        history to recover the actual close P&L for each ticket and updates:
          • ``MT5ScalpTrade.pnl``  — the realised profit on that leg
          • ``MT5ScalpTrade.close_price``
          • ``MT5ScalpSession.wins`` / ``.losses`` / ``.session_pnl``

        Without credentials (cancelled pending, stale entry), the trade is still
        marked closed but wins/losses are not updated (no real P&L to count).
        """
        if not tickets:
            return

        # ── Fetch P&L from broker deal history ─────────────────────────────────
        pnl_by_ticket: Dict[int, float] = {}
        close_price_by_ticket: Dict[int, float] = {}
        if login and server and password:
            try:
                deals = await mt5_client.get_deals(
                    login, server, password,
                    date_from=datetime.utcnow() - timedelta(days=2),
                )
                for d in deals:
                    tk = _order_ticket(d)
                    if not tk:
                        continue
                    # Profit field name varies by broker build
                    profit = (
                        d.get("profit") or d.get("Profit") or
                        d.get("closeProfit") or d.get("realizedPnL") or 0.0
                    )
                    pnl_by_ticket[tk] = float(profit)
                    # Close price field name varies
                    cp = (
                        d.get("closePrice") or d.get("priceClose") or
                        d.get("close_price") or d.get("price") or 0.0
                    )
                    close_price_by_ticket[tk] = float(cp)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[ScalpBot] reconcile deal-fetch session={session_id}: {exc}")

        # P&L total across all reconciled tickets (for session stats)
        realised = sum(pnl_by_ticket.get(tk, 0.0) for tk in tickets)
        has_pnl = bool(pnl_by_ticket)   # True when we got actual deal data

        # Snapshot trade data before committing (for brain storage below)
        brain_records: List[Dict[str, Any]] = []

        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return
            sym = session.symbol or "UNKNOWN"
            sess_strictness = (
                (session.raw_settings or {}).get("strictness", "scalper")
                if isinstance(session.raw_settings, dict) else "scalper"
            )
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
                    if tk in pnl_by_ticket:
                        tr.pnl = pnl_by_ticket[tk]
                    if close_price_by_ticket.get(tk, 0.0) > 0:
                        tr.close_price = close_price_by_ticket[tk]
                    # Capture for brain storage (while session is still available)
                    if tk in pnl_by_ticket:
                        brain_records.append({
                            "side": tr.side or "buy",
                            "entry": float(tr.entry_price or 0.0),
                            "close": close_price_by_ticket.get(tk, 0.0),
                            "pnl": pnl_by_ticket[tk],
                            "confidence": float(tr.confidence or 0.0),
                            "bias_reason": tr.reason or "",
                        })
                if session.trade1_ticket == tk:
                    session.trade1_ticket = None
                if session.trade2_ticket == tk:
                    session.trade2_ticket = None

            # Update session W/L/P&L when we have real broker P&L data
            if has_pnl:
                session.session_pnl = (session.session_pnl or 0.0) + realised
                if realised >= 0:
                    session.wins = (session.wins or 0) + 1
                else:
                    session.losses = (session.losses or 0) + 1

            if not session.trade1_ticket and not session.trade2_ticket:
                session.phase = "analyzing"
            session.last_cycle_at = datetime.utcnow()
            await db.commit()

        # ── Store closed trade outcomes to knowledge brains ───────────────────
        # Fire-and-forget: never block the main cycle on brain storage
        for rec in brain_records:
            asyncio.ensure_future(
                _store_scalp_outcome_to_brain(
                    session_id=session_id,
                    symbol=sym,
                    side=rec["side"],
                    entry_price=rec["entry"],
                    close_price=rec["close"],
                    pnl=rec["pnl"],
                    bias_reason=rec["bias_reason"],
                    confidence=rec["confidence"],
                    strictness=sess_strictness,
                )
            )

        # Purge trailing SL state for reconciled tickets so a future trade
        # re-using the same ticket number starts fresh.
        for tk in tickets:
            self._trailing_sl.pop((session_id, tk), None)

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

    # -- adaptive prevention --------------------------------------------------

    def _tighten_strictness(self, strictness: str) -> str:
        cur = (strictness or DEFAULT_STRICTNESS).lower()
        if cur not in _STRICTNESS_ORDER:
            cur = DEFAULT_STRICTNESS
        idx = _STRICTNESS_ORDER.index(cur)
        return _STRICTNESS_ORDER[min(idx + 1, len(_STRICTNESS_ORDER) - 1)]

    def _parse_reason_metrics(self, reason: str) -> Dict[str, float]:
        text = reason or ""
        out: Dict[str, float] = {}
        m_rr = _RR_RE.search(text)
        m_k = _KRONOS_RE.search(text)
        m_v = _VOL_IMB_RE.search(text)
        if m_rr:
            out["rr"] = float(m_rr.group(1))
        if m_k:
            out["kronos"] = float(m_k.group(1))
        if m_v:
            out["vol_imb"] = float(m_v.group(1))
        return out

    async def _analyse_today_and_apply_prevention(
        self,
        session_id: int,
        account_id: int,
        symbol: str,
        current_strictness: str,
    ) -> Dict[str, Any]:
        """Analyse today's filled outcomes and apply strictness/cooldown safeguards.

        The goal is to prevent repeated low-quality entries after a loss streak by:
        1) classifying loss causes from today, 2) tightening strictness one notch,
        3) activating a temporary cooldown when mistake density is high.
        """
        now = datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        async with AsyncSessionLocal() as db:
            session = await db.get(MT5ScalpSession, session_id)
            if not session:
                return {"strictness": current_strictness, "cooldown_active": False}

            raw = (session.raw_settings or {}) if isinstance(session.raw_settings, dict) else {}
            today_str = day_start.date().isoformat()

            # ── Day-start reset ──────────────────────────────────────────────
            # Wipe adaptive overrides at the start of each new trading day so
            # yesterday's losses do not permanently elevate strictness.
            last_reset = raw.get("last_reset_date")
            if last_reset != today_str:
                base_strict = raw.get("strictness", DEFAULT_STRICTNESS) or DEFAULT_STRICTNESS
                raw["adaptive_strictness"] = base_strict
                raw["adaptive_cooldown_until"] = None
                raw["last_triggered_date"] = None
                raw["last_reset_date"] = today_str
                session.raw_settings = raw
                await db.commit()

            cooldown_until = None
            cooldown_txt = raw.get("adaptive_cooldown_until")
            if isinstance(cooldown_txt, str):
                try:
                    cooldown_until = datetime.fromisoformat(cooldown_txt)
                except ValueError:
                    cooldown_until = None

            # Active cooldown → keep it, do not re-arm each cycle.
            if cooldown_until and cooldown_until > now:
                strict = str(raw.get("adaptive_strictness", current_strictness) or current_strictness)
                mins = int((cooldown_until - now).total_seconds() // 60)
                return {
                    "strictness": strict,
                    "cooldown_active": True,
                    "note": f"Protective cooldown ({mins}m left) after repeated mistakes",
                }

            rows = await db.execute(
                select(MT5ScalpTrade).where(
                    MT5ScalpTrade.session_id == session_id,
                    MT5ScalpTrade.status == "closed",
                    MT5ScalpTrade.closed_at >= day_start,
                )
            )
            closed_today = rows.scalars().all()
            losses = [t for t in closed_today if float(t.pnl or 0.0) < 0.0]

            # Also inspect today's filled live-account deals for this symbol.
            deal_rows = await db.execute(
                select(MT5Deal).where(
                    MT5Deal.account_id == account_id,
                    MT5Deal.symbol == symbol,
                    MT5Deal.mt5_time >= day_start,
                    MT5Deal.deal_type.in_([MT5DealType.BUY, MT5DealType.SELL]),
                )
            )
            deal_items = deal_rows.scalars().all()
            deal_losses = [d for d in deal_items if float(d.profit or 0.0) < 0.0]

            mistake_counts = {
                "volume_opposition": 0,
                "weak_confidence": 0,
                "weak_rr": 0,
                "kronos_opposition": 0,
            }

            for tr in losses:
                side = (tr.side or "").lower()
                metrics = self._parse_reason_metrics(tr.reason or "")
                if float(tr.confidence or 0.0) < 0.60:
                    mistake_counts["weak_confidence"] += 1
                rr = metrics.get("rr")
                if rr is not None and rr < 1.5:
                    mistake_counts["weak_rr"] += 1
                kronos = metrics.get("kronos")
                if kronos is not None:
                    if (side == "buy" and kronos < -0.2) or (side == "sell" and kronos > 0.2):
                        mistake_counts["kronos_opposition"] += 1
                imb = metrics.get("vol_imb")
                if imb is not None:
                    if (side == "buy" and imb < 0) or (side == "sell" and imb > 0):
                        mistake_counts["volume_opposition"] += 1

            total_closed = len(closed_today)
            total_losses = len(losses)
            loss_ratio = (total_losses / total_closed) if total_closed else 0.0

            # Only trigger once per day — guard against re-arming every cycle.
            already_triggered_today = (raw.get("last_triggered_date") == today_str)

            trigger_guard = (
                not already_triggered_today
                and total_closed >= 3
                and total_losses >= 2
                and (
                    loss_ratio >= 0.60
                    or mistake_counts["volume_opposition"] >= 1
                    or mistake_counts["weak_confidence"] >= 2
                    or len(deal_losses) >= 3
                )
            )
            severe = (
                loss_ratio >= 0.75
                or total_losses >= 3
                or mistake_counts["volume_opposition"] >= 2
                or len(deal_losses) >= 4
            )

            new_strictness = str(raw.get("adaptive_strictness", current_strictness) or current_strictness)
            if trigger_guard:
                new_strictness = self._tighten_strictness(new_strictness)

            # Persist diagnostics every cycle for traceability.
            raw["today_mistakes"] = {
                "closed_trades": total_closed,
                "losses": total_losses,
                "loss_ratio": round(loss_ratio, 3),
                "deal_losses": len(deal_losses),
                **mistake_counts,
                "scanned_at": now.isoformat(),
            }
            raw["adaptive_strictness"] = new_strictness

            cooldown_active = False
            note = ""
            if trigger_guard:
                minutes = 20 if severe else 10
                cool_until = now + timedelta(minutes=minutes)
                raw["adaptive_cooldown_until"] = cool_until.isoformat()
                raw["last_triggered_date"] = today_str
                raw["last_prevention_reason"] = (
                    f"loss_ratio={loss_ratio:.2f}, vol_opp={mistake_counts['volume_opposition']}, "
                    f"weak_conf={mistake_counts['weak_confidence']}"
                )
                cooldown_active = True
                note = (
                    f"Protection active: tightened to {new_strictness}, cooling down {minutes}m "
                    f"(losses {total_losses}/{total_closed} today)"
                )

            session.raw_settings = raw
            await db.commit()

        return {
            "strictness": new_strictness,
            "cooldown_active": cooldown_active,
            "note": note,
        }


# Singleton
scalp_bot_manager = ScalpBotManager()
