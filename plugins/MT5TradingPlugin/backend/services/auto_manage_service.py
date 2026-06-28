"""
MT5 Auto-Manage Service

Runs a periodic background loop that:
  1. Fetches all active MT5 accounts.
  2. Resolves watchlist + open symbols per account.
  3. Runs SMC + AI analysis per symbol (H1 candles, 300 bars).
  4. Applies strict guards: confidence, price geometry validity.
  5. Updates position TP/SL when signal side matches position side.
  6. Cancels opposing-side pending orders.
  7. Adjusts same-side pending orders to new entry/SL/TP.

Policy (locked from user preferences):
  - use_ai=True
  - All connected MT5 accounts (active + reachable)
  - Watchlist symbols per account (global fallback: XAUUSD, EURUSD, GBPUSD, USDJPY)
  - Position update: only when signal side == position side
  - Pending: opposing side → cancel; same side → modify entry/SL/TP
  - Run interval: 60s (configurable via MT5PluginSetting)
  - Per-ticket cooldown: 180s (prevents repeated modifications)
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from plugins.MT5TradingPlugin.backend.models import (
    MT5Account,
    MT5AccountStatus,
    MT5Order,
    MT5Position,
    MT5PluginSetting,
)
from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
    SMCStrategyEngine,
    candles_from_payload,
    contract_size_for_symbol,
)


# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_WATCHLIST: List[str] = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
DEFAULT_MIN_CONFIDENCE: float = 0.60
DEFAULT_INTERVAL: int = 60
DEFAULT_COOLDOWN: int = 180

# Setting key names stored in mt5_plugin_settings
_SK_INTERVAL = "auto_manage_interval"
_SK_COOLDOWN = "auto_manage_cooldown"
_SK_MIN_CONF = "auto_manage_min_confidence"
_SK_USE_AI = "auto_manage_use_ai"
_SK_WATCHLIST = "auto_manage_watchlist"


# ── Runtime state ─────────────────────────────────────────────────────────────

class _LoopState:
    """Holds all mutable runtime state for the background loop."""

    def __init__(self) -> None:
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self.started_at: Optional[datetime] = None
        self.last_run_at: Optional[datetime] = None
        self.last_summary: Optional[Dict[str, Any]] = None
        self.error_count: int = 0
        self.interval_seconds: int = DEFAULT_INTERVAL
        self.cooldown_seconds: int = DEFAULT_COOLDOWN
        # {ticket: last_action_unix_ts}
        self._ticket_cooldowns: Dict[int, float] = {}

    def is_in_cooldown(self, ticket: int) -> bool:
        last = self._ticket_cooldowns.get(ticket)
        if last is None:
            return False
        return (time.time() - last) < self.cooldown_seconds

    def record_action(self, ticket: int) -> None:
        self._ticket_cooldowns[ticket] = time.time()

    def prune_cooldowns(self) -> None:
        cutoff = time.time() - self.cooldown_seconds * 2
        self._ticket_cooldowns = {k: v for k, v in self._ticket_cooldowns.items() if v > cutoff}


_loop_state = _LoopState()


# ── Settings helpers ─────────────────────────────────────────────────────────

async def _get_setting(key: str, default: Any) -> Any:
    """Read a single MT5PluginSetting row by key, returning `default` if absent."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(MT5PluginSetting).where(MT5PluginSetting.key == key))
        row = row.scalar_one_or_none()
    if row is None or row.value is None:
        return default
    v = str(row.value)
    if isinstance(default, bool):
        return v.lower() in ("1", "true", "yes")
    if isinstance(default, int):
        try:
            return int(v)
        except (ValueError, TypeError):
            return default
    if isinstance(default, float):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default
    return v


async def _load_settings() -> Dict[str, Any]:
    """Load all auto-manage configurable settings at cycle start."""
    import json  # local import — only needed when settings are loaded

    interval = await _get_setting(_SK_INTERVAL, DEFAULT_INTERVAL)
    cooldown = await _get_setting(_SK_COOLDOWN, DEFAULT_COOLDOWN)
    min_confidence = await _get_setting(_SK_MIN_CONF, DEFAULT_MIN_CONFIDENCE)
    use_ai = await _get_setting(_SK_USE_AI, True)
    watchlist_str = await _get_setting(_SK_WATCHLIST, "")

    watchlist: List[str] = DEFAULT_WATCHLIST
    if watchlist_str:
        try:
            parsed = json.loads(watchlist_str)
            if isinstance(parsed, list) and parsed:
                watchlist = [str(s) for s in parsed]
        except Exception:
            pass

    return {
        "interval": interval,
        "cooldown": cooldown,
        "min_confidence": float(min_confidence),
        "use_ai": bool(use_ai),
        "watchlist": watchlist,
    }


# ── Core cycle ────────────────────────────────────────────────────────────────

async def run_cycle() -> Dict[str, Any]:
    """Execute one full auto-manage cycle across all active accounts."""
    settings = await _load_settings()
    _loop_state.cooldown_seconds = settings["cooldown"]

    summary: Dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "accounts_processed": 0,
        "symbols_analyzed": 0,
        "signals_accepted": 0,
        "signals_rejected": 0,
        "position_updates": 0,
        "orders_cancelled": 0,
        "orders_modified": 0,
        "cooldown_skips": 0,
        "errors": [],
    }

    # Fetch all active, reachable accounts
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5Account).where(
                MT5Account.api_reachable == True,  # noqa: E712
                MT5Account.status == MT5AccountStatus.ACTIVE,
            )
        )
        accounts: List[MT5Account] = result.scalars().all()

    if not accounts:
        logger.info("[MT5 AutoManage] No active accounts found — cycle skipped")
        return summary

    for account in accounts:
        try:
            await _process_account(account, settings, summary)
            summary["accounts_processed"] += 1
        except Exception as exc:
            msg = f"account_{account.id}: {str(exc)[:200]}"
            logger.error(f"[MT5 AutoManage] {msg}")
            summary["errors"].append(msg)

    _loop_state.prune_cooldowns()
    logger.info(
        "[MT5 AutoManage] Cycle — accounts=%d symbols=%d accepted=%d "
        "pos_updates=%d cancelled=%d modified=%d cooldown_skips=%d",
        summary["accounts_processed"],
        summary["symbols_analyzed"],
        summary["signals_accepted"],
        summary["position_updates"],
        summary["orders_cancelled"],
        summary["orders_modified"],
        summary["cooldown_skips"],
    )
    return summary


async def _process_account(
    account: MT5Account,
    settings: Dict[str, Any],
    summary: Dict[str, Any],
) -> None:
    """Process all relevant symbols for a single account."""
    async with AsyncSessionLocal() as db:
        pos_rows = await db.execute(select(MT5Position).where(MT5Position.account_id == account.id))
        positions: List[MT5Position] = pos_rows.scalars().all()

        ord_rows = await db.execute(select(MT5Order).where(MT5Order.account_id == account.id))
        orders: List[MT5Order] = ord_rows.scalars().all()

    # Symbols = watchlist + any symbols from existing positions/orders
    all_symbols = (
        set(settings["watchlist"])
        | {p.symbol for p in positions}
        | {o.symbol for o in orders}
    )

    for symbol in sorted(all_symbols):
        try:
            await _process_symbol(account, symbol, positions, orders, settings, summary)
            summary["symbols_analyzed"] += 1
        except Exception as exc:
            msg = f"symbol_{symbol}_acct_{account.id}: {str(exc)[:200]}"
            logger.warning(f"[MT5 AutoManage] {msg}")
            summary["errors"].append(msg)


async def _process_symbol(
    account: MT5Account,
    symbol: str,
    all_positions: List[MT5Position],
    all_orders: List[MT5Order],
    settings: Dict[str, Any],
    summary: Dict[str, Any],
) -> None:
    """Analyse one symbol and apply actions to matching positions/orders."""
    min_confidence: float = settings["min_confidence"]
    use_ai: bool = settings["use_ai"]

    # Fetch H1 candles
    candles_raw = await mt5_client.get_candles(
        account.login, account.server, account.password_encrypted,
        symbol=symbol, timeframe="H1", count=300,
    )
    if not candles_raw or len(candles_raw) < 50:
        return

    candles = candles_from_payload(candles_raw)
    contract_size = contract_size_for_symbol(symbol)

    engine = SMCStrategyEngine(
        min_rr=1.5,
        min_confidence=min_confidence,
        contract_size=contract_size,
        symbol=symbol,
    )
    analysis = engine.analyze(candles)

    if analysis.get("error") or not analysis.get("signals"):
        summary["signals_rejected"] += 1
        return

    # Optional AI review
    if use_ai:
        try:
            from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_review
            async with AsyncSessionLocal() as db:
                ai_result = await ai_review(
                    db=db,
                    symbol=symbol,
                    timeframe="H1",
                    analysis=analysis,
                )
            # Merge AI confidence adjustments into signals when AI is available
            if ai_result.get("available") and ai_result.get("rated_signals"):
                ai_ratings = {float(r["entry"]): r for r in ai_result["rated_signals"] if "entry" in r}
                updated_signals = []
                for sig in analysis["signals"]:
                    entry = float(sig["entry"])
                    rating = next(
                        (v for k, v in ai_ratings.items() if abs(k - entry) < entry * 0.001),
                        None,
                    )
                    if rating and rating.get("verdict") == "skip":
                        continue  # AI says skip
                    if rating and rating.get("confidence") is not None:
                        sig = dict(sig)
                        # Blend engine confidence with AI confidence (equal weight)
                        sig["confidence"] = (float(sig["confidence"]) + float(rating["confidence"])) / 2.0
                    updated_signals.append(sig)
                if updated_signals:
                    analysis["signals"] = updated_signals
        except Exception as exc:
            logger.warning(f"[MT5 AutoManage] AI review failed for {symbol}: {exc}")

    if not analysis.get("signals"):
        summary["signals_rejected"] += 1
        return

    # Pick best signal (already sorted by confidence descending from engine)
    best = analysis["signals"][0]
    confidence = float(best.get("confidence", 0))

    if confidence < min_confidence:
        summary["signals_rejected"] += 1
        return

    entry = float(best.get("entry", 0))
    sl = float(best.get("stop_loss", 0))
    tp = float(best.get("take_profit", 0))
    side = str(best.get("side", ""))

    if not entry or not sl or not tp or not side:
        summary["signals_rejected"] += 1
        return

    # Geometry guard: SL and TP must be on correct sides of entry
    if side == "buy":
        if sl >= entry or tp <= entry:
            summary["signals_rejected"] += 1
            return
    elif side == "sell":
        if sl <= entry or tp >= entry:
            summary["signals_rejected"] += 1
            return
    else:
        summary["signals_rejected"] += 1
        return

    summary["signals_accepted"] += 1

    # ── Update open positions ─────────────────────────────────────────────────
    for pos in [p for p in all_positions if p.symbol == symbol]:
        pos_side = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
        if pos_side != side:
            continue  # Side mismatch — do NOT modify

        ticket = pos.mt5_ticket
        if _loop_state.is_in_cooldown(ticket):
            summary["cooldown_skips"] += 1
            continue

        sl_diff = abs((pos.sl or 0) - sl)
        tp_diff = abs((pos.tp or 0) - tp)
        # Only update when there is a meaningful price difference (> 1 point)
        if sl_diff < 1e-5 and tp_diff < 1e-5:
            continue

        try:
            await mt5_client.modify_order(
                account.login, account.server, account.password_encrypted,
                ticket=ticket,
                sl=sl if sl_diff >= 1e-5 else None,
                tp=tp if tp_diff >= 1e-5 else None,
            )
            _loop_state.record_action(ticket)
            summary["position_updates"] += 1
            logger.info(
                "[MT5 AutoManage] Updated position %d (%s %s) → SL=%.5f TP=%.5f",
                ticket, symbol, pos_side, sl, tp,
            )
        except Exception as exc:
            logger.error("[MT5 AutoManage] Failed to modify position %d: %s", ticket, exc)

    # ── Update pending orders ─────────────────────────────────────────────────
    for order in [o for o in all_orders if o.symbol == symbol]:
        otype = order.order_type.value if hasattr(order.order_type, "value") else str(order.order_type)
        order_is_buy = otype in ("buy_limit", "buy_stop", "buy_stop_limit")
        signal_is_buy = side == "buy"

        ticket = order.mt5_ticket
        if _loop_state.is_in_cooldown(ticket):
            summary["cooldown_skips"] += 1
            continue

        if order_is_buy != signal_is_buy:
            # Opposing side → cancel
            try:
                await mt5_client.cancel_order(
                    account.login, account.server, account.password_encrypted,
                    ticket=ticket,
                )
                _loop_state.record_action(ticket)
                summary["orders_cancelled"] += 1
                logger.info("[MT5 AutoManage] Cancelled opposing order %d (%s %s)", ticket, symbol, otype)
            except Exception as exc:
                logger.error("[MT5 AutoManage] Failed to cancel order %d: %s", ticket, exc)
        else:
            # Same side → adjust to new signal values
            price_diff = abs((order.price or 0) - entry)
            sl_diff = abs((order.sl or 0) - sl)
            tp_diff = abs((order.tp or 0) - tp)
            if price_diff < 1e-5 and sl_diff < 1e-5 and tp_diff < 1e-5:
                continue

            try:
                await mt5_client.modify_order(
                    account.login, account.server, account.password_encrypted,
                    ticket=ticket,
                    price=entry if price_diff >= 1e-5 else None,
                    sl=sl if sl_diff >= 1e-5 else None,
                    tp=tp if tp_diff >= 1e-5 else None,
                )
                _loop_state.record_action(ticket)
                summary["orders_modified"] += 1
                logger.info(
                    "[MT5 AutoManage] Modified same-side order %d (%s %s) → entry=%.5f SL=%.5f TP=%.5f",
                    ticket, symbol, otype, entry, sl, tp,
                )
            except Exception as exc:
                logger.error("[MT5 AutoManage] Failed to modify order %d: %s", ticket, exc)


# ── Background thread ─────────────────────────────────────────────────────────

def _thread_target(interval: int) -> None:
    """Thread function: drives the async cycle loop until stop requested."""
    _loop_state.started_at = datetime.utcnow()
    _loop_state.error_count = 0
    logger.info("[MT5 AutoManage] Loop started (interval=%ds, cooldown=%ds)", interval, _loop_state.cooldown_seconds)

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    try:
        while not _loop_state._stop_event.is_set():
            try:
                summary = event_loop.run_until_complete(run_cycle())
                _loop_state.last_run_at = datetime.utcnow()
                _loop_state.last_summary = summary
            except Exception as exc:
                _loop_state.error_count += 1
                logger.error("[MT5 AutoManage] Cycle error: %s", exc)

            # Sleep in 1-second increments so stop event is responsive
            deadline = time.time() + interval
            while time.time() < deadline and not _loop_state._stop_event.is_set():
                time.sleep(1)
    finally:
        event_loop.close()
        _loop_state.running = False
        logger.info("[MT5 AutoManage] Loop stopped")


# ── Public control API ────────────────────────────────────────────────────────

def start_loop(interval: int = DEFAULT_INTERVAL) -> bool:
    """Start the auto-manage background loop. Returns False if already running."""
    if _loop_state.running:
        return False

    _loop_state._stop_event.clear()
    _loop_state.running = True
    _loop_state.interval_seconds = interval

    thread = threading.Thread(
        target=_thread_target,
        args=(interval,),
        daemon=True,
        name="mt5_auto_manage",
    )
    _loop_state.thread = thread
    thread.start()
    return True


def stop_loop() -> bool:
    """Stop the auto-manage background loop. Returns False if not running."""
    if not _loop_state.running:
        return False

    _loop_state._stop_event.set()
    if _loop_state.thread:
        _loop_state.thread.join(timeout=15)
    _loop_state.running = False
    return True


def get_loop_status() -> Dict[str, Any]:
    """Return the current runtime status of the loop."""
    return {
        "running": _loop_state.running,
        "interval_seconds": _loop_state.interval_seconds,
        "cooldown_seconds": _loop_state.cooldown_seconds,
        "started_at": _loop_state.started_at.isoformat() if _loop_state.started_at else None,
        "last_run_at": _loop_state.last_run_at.isoformat() if _loop_state.last_run_at else None,
        "last_summary": _loop_state.last_summary,
        "error_count": _loop_state.error_count,
    }


# ── Manual analyse-all-positions helper ──────────────────────────────────────

async def analyze_positions_for_account(account_id: int) -> List[Dict[str, Any]]:
    """
    Run SMC+AI analysis for every open position on the given account.
    Returns a suggestion per position — never applies any changes.

    Each suggestion:
        ticket          int   — MT5 ticket
        account_id      int
        symbol          str
        side            str   — 'buy' | 'sell'
        volume          float
        price_open      float
        current_sl      float|None
        current_tp      float|None
        has_suggestion  bool
        suggested_sl    float|None
        suggested_tp    float|None
        confidence      float|None
        rr              float|None
        reason          str   — human-readable explanation
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            return []
        pos_rows = await db.execute(
            select(MT5Position).where(MT5Position.account_id == account_id)
        )
        positions: List[MT5Position] = pos_rows.scalars().all()

    if not positions:
        return []

    # Cache analysis per symbol so two positions on the same symbol reuse it
    symbol_analysis_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    min_conf = DEFAULT_MIN_CONFIDENCE

    async def _get_signal(symbol: str) -> Optional[Dict[str, Any]]:
        if symbol in symbol_analysis_cache:
            return symbol_analysis_cache[symbol]
        try:
            candles_raw = await mt5_client.get_candles(
                account.login, account.server, account.password_encrypted,
                symbol=symbol, timeframe="H1", count=300,
            )
            if not candles_raw or len(candles_raw) < 50:
                symbol_analysis_cache[symbol] = None
                return None

            candles = candles_from_payload(candles_raw)
            engine = SMCStrategyEngine(
                min_rr=1.5,
                min_confidence=min_conf,
                contract_size=contract_size_for_symbol(symbol),
                symbol=symbol,
            )
            analysis = engine.analyze(candles)

            if analysis.get("error") or not analysis.get("signals"):
                symbol_analysis_cache[symbol] = None
                return None

            # Optional AI review
            try:
                from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_review
                async with AsyncSessionLocal() as db2:
                    ai_result = await ai_review(
                        db=db2, symbol=symbol, timeframe="H1", analysis=analysis
                    )
                if ai_result.get("available") and ai_result.get("rated_signals"):
                    ai_ratings = {
                        float(r["entry"]): r
                        for r in ai_result["rated_signals"]
                        if "entry" in r
                    }
                    updated = []
                    for sig in analysis["signals"]:
                        entry = float(sig["entry"])
                        rating = next(
                            (v for k, v in ai_ratings.items() if abs(k - entry) < entry * 0.001),
                            None,
                        )
                        if rating and rating.get("verdict") == "skip":
                            continue
                        if rating and rating.get("confidence") is not None:
                            sig = dict(sig)
                            sig["confidence"] = (
                                float(sig["confidence"]) + float(rating["confidence"])
                            ) / 2.0
                        updated.append(sig)
                    if updated:
                        analysis["signals"] = updated
            except Exception:
                pass  # Proceed without AI

            # Best signal = highest confidence after filtering
            best = max(
                (s for s in analysis.get("signals", []) if float(s.get("confidence", 0)) >= min_conf),
                key=lambda s: float(s.get("confidence", 0)),
                default=None,
            )
            symbol_analysis_cache[symbol] = best
            return best
        except Exception as exc:
            logger.warning("[MT5 AutoManage] analyze_positions: %s %s", symbol, exc)
            symbol_analysis_cache[symbol] = None
            return None

    suggestions: List[Dict[str, Any]] = []
    for pos in positions:
        pos_side = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
        base = {
            "ticket": pos.mt5_ticket,
            "account_id": account_id,
            "symbol": pos.symbol,
            "side": pos_side,
            "volume": pos.volume,
            "price_open": pos.price_open,
            "current_sl": pos.sl,
            "current_tp": pos.tp,
            "has_suggestion": False,
            "suggested_sl": None,
            "suggested_tp": None,
            "confidence": None,
            "rr": None,
            "reason": "",
        }

        signal = await _get_signal(pos.symbol)
        if signal is None:
            base["reason"] = "No valid signal (insufficient candles or low confidence)"
            suggestions.append(base)
            continue

        signal_side = str(signal.get("side", ""))
        entry = float(signal.get("entry", 0))
        sl = float(signal.get("stop_loss", 0))
        tp = float(signal.get("take_profit", 0))
        confidence = float(signal.get("confidence", 0))
        rr = float(signal.get("rr", 0))

        if signal_side != pos_side:
            base["reason"] = f"Signal is {signal_side.upper()} — does not match position side ({pos_side.upper()})"
            suggestions.append(base)
            continue

        # Geometry guard
        valid = (
            (signal_side == "buy" and sl < entry and tp > entry)
            or (signal_side == "sell" and sl > entry and tp < entry)
        )
        if not valid:
            base["reason"] = "Signal geometry invalid (SL/TP on wrong side of entry)"
            suggestions.append(base)
            continue

        # Only suggest if the values meaningfully differ from what's already set
        sl_diff = abs((pos.sl or 0.0) - sl)
        tp_diff = abs((pos.tp or 0.0) - tp)
        if sl_diff < 1e-5 and tp_diff < 1e-5:
            base["reason"] = "SL/TP already match current signal — no change needed"
            suggestions.append(base)
            continue

        base.update({
            "has_suggestion": True,
            "suggested_sl": sl,
            "suggested_tp": tp,
            "confidence": round(confidence, 3),
            "rr": round(rr, 2),
            "reason": f"SMC signal confidence {confidence:.0%} | R:R {rr:.1f}",
        })
        suggestions.append(base)

    return suggestions


async def apply_position_suggestions(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Apply a list of SL/TP updates to positions.

    Each item: {ticket, account_id, sl, tp}
    Returns per-item result: {ticket, success, error}
    """
    # Group by account to avoid repeated DB lookups
    from collections import defaultdict

    by_account: Dict[int, List[Dict]] = defaultdict(list)
    for item in items:
        by_account[int(item["account_id"])].append(item)

    results: List[Dict[str, Any]] = []

    for account_id, account_items in by_account.items():
        async with AsyncSessionLocal() as db:
            account = await db.get(MT5Account, account_id)
        if not account:
            for item in account_items:
                results.append({"ticket": item["ticket"], "success": False, "error": "Account not found"})
            continue

        for item in account_items:
            ticket = int(item["ticket"])
            sl = item.get("sl")
            tp = item.get("tp")
            try:
                await mt5_client.modify_order(
                    account.login, account.server, account.password_encrypted,
                    ticket=ticket,
                    sl=float(sl) if sl is not None else None,
                    tp=float(tp) if tp is not None else None,
                )
                _loop_state.record_action(ticket)
                logger.info("[MT5 AutoManage] Applied suggestion: ticket=%d SL=%.5f TP=%.5f", ticket, sl or 0, tp or 0)
                results.append({"ticket": ticket, "success": True})
            except Exception as exc:
                logger.error("[MT5 AutoManage] Apply suggestion failed ticket=%d: %s", ticket, exc)
                results.append({"ticket": ticket, "success": False, "error": str(exc)[:200]})

    return results
