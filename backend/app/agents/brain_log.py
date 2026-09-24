"""The AI brain's single write path.

Every agent decision, every OBLIVION read and every order outcome is recorded
here. The main database (``agent_decisions`` / ``oblivion_reads``) is the
guaranteed record; the neuro brain (``app.neuro_brain``, the /brain page and
federated recall) gets a mirror of each one. A mirror failure is counted and
logged loudly, never raised — losing the mirror must not lose the trade.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from loguru import logger

from app.models.database import AgentDecision

_health: Dict[str, Any] = {
    "mirrored": 0,
    "mirror_failed": 0,
    "mirror_disabled": 0,
    "recorded": 0,
    "record_failed": 0,
    "last_error": None,
    "last_error_at": None,
}


#: The suite runs against the production database and brain; tests that want
#: these writes must set this and point the session factory elsewhere.
ALLOW_UNDER_TEST = False


def suppressed() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not ALLOW_UNDER_TEST


def health() -> Dict[str, Any]:
    return dict(_health)


def _as_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _fail(where: str, exc: Any) -> None:
    _health["mirror_failed"] += 1
    _health["last_error"] = f"{where}: {exc}"[:300]
    _health["last_error_at"] = time.time()
    logger.warning(f"[brain] {where} mirror failed: {exc}")


def _ingest_sync(kind: str, content: str, symbol: str, session_id: str, source: str, meta: Dict[str, Any]) -> None:
    try:
        from app.neuro_brain import is_enabled
        from app.neuro_brain.state_store import ingest_local

        if not is_enabled():
            _health["mirror_disabled"] += 1
            return
        if ingest_local(kind=kind, content=content, symbol=symbol or "",
                        session_id=session_id or "", source=source, meta=meta):
            _health["mirrored"] += 1
        else:
            _fail(kind, "ingest_local returned False")
    except Exception as exc:  # noqa: BLE001
        _fail(kind, exc)


def _segment_sync(**kwargs: Any) -> None:
    try:
        from app.neuro_brain import is_enabled
        from app.neuro_brain.state_store import record_trade_segment

        if not is_enabled():
            _health["mirror_disabled"] += 1
            return
        if record_trade_segment(**kwargs) is None:
            _fail("trade_segment", "record_trade_segment returned None")
        else:
            _health["mirrored"] += 1
    except Exception as exc:  # noqa: BLE001
        _fail("trade_segment", exc)


def _dispatch(fn, *args: Any, **kwargs: Any) -> None:
    """Run a blocking SQLite mirror write off the event loop, fire-and-forget."""
    if suppressed():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        fn(*args, **kwargs)
        return
    task = loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    task.add_done_callback(
        lambda t: (not t.cancelled() and t.exception() is not None) and _fail("executor", t.exception())
    )


def mirror(kind: str, content: str, *, symbol: str = "", session_id: str = "",
           source: str = "trading-room", meta: Optional[Dict[str, Any]] = None) -> None:
    if not (content or "").strip():
        return
    _dispatch(_ingest_sync, kind, content, symbol, session_id or "", source, meta or {})


def _decision_line(role: str, symbol: str, action: str, confidence: Any, origin: str, reasoning: Any) -> str:
    try:
        conf = f"{float(confidence or 0):.2f}"
    except (TypeError, ValueError):
        conf = str(confidence)
    text = str(reasoning or "").strip().replace("\n", " ")
    return f"[{symbol} {str(action or '').upper()} conf={conf} {role} via {origin}] {text[:900]}"


def log_decision(
    db: Any,
    *,
    agent_id: int = 0,
    agent_name: str,
    agent_role: str,
    symbol: str,
    action: str,
    confidence: Any = 0,
    reasoning: Any = "",
    market_data: Any = None,
    session_id: Optional[str] = None,
    signal_id: Optional[int] = None,
    ai_called: bool = True,
    memory_context_used: int = 0,
    origin: str = "board",
    oblivion_read_id: Optional[int] = None,
) -> AgentDecision:
    """Add one decision to ``db`` (the caller commits) and mirror it to the brain."""
    try:
        conf = float(confidence or 0)
    except (TypeError, ValueError):
        conf = 0.0
    row = AgentDecision(
        agent_id=int(agent_id or 0),
        agent_name=agent_name,
        agent_role=agent_role,
        symbol=symbol or "",
        action=str(action or "hold"),
        confidence=conf,
        reasoning=str(reasoning) if reasoning is not None else None,
        market_data=_as_json(market_data),
        signal_id=signal_id,
        session_id=session_id,
        ai_called=bool(ai_called),
        memory_context_used=int(memory_context_used or 0),
        origin=origin,
        oblivion_read_id=oblivion_read_id,
    )
    db.add(row)
    _health["recorded"] += 1
    mirror(
        "decision",
        _decision_line(agent_role, symbol, action, conf, origin, reasoning),
        symbol=symbol, session_id=session_id or "",
        meta={"agent": agent_name, "role": agent_role, "origin": origin,
              "action": action, "confidence": conf, "oblivion_read_id": oblivion_read_id,
              "signal_id": signal_id},
    )
    return row


async def record_decision(**kwargs: Any) -> Optional[int]:
    """``log_decision`` in its own session, committed — for code with no session."""
    from app.core.database import AsyncSessionLocal

    if suppressed():
        return None
    try:
        async with AsyncSessionLocal() as db:
            row = log_decision(db, **kwargs)
            await db.commit()
            return row.id
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        _health["record_failed"] += 1
        _health["last_error"] = f"record_decision: {exc}"[:300]
        _health["last_error_at"] = time.time()
        logger.warning(f"[brain] decision not recorded ({kwargs.get('symbol')} {kwargs.get('action')}): {exc}")
        return None


def log_oblivion_read(read: Any, *, changed: bool = True) -> None:
    """Mirror one stored OBLIVION read. Repeats of an unchanged read on the same
    bar carry no new information and are only kept in ``oblivion_reads``."""
    if not changed:
        return
    action = (read.signal_action or "wait").upper()
    levels = ""
    if read.entry:
        levels = f" entry {read.entry} SL {read.stop_loss} TP1 {read.tp1} TP3 {read.tp3}"
    content = (
        f"[OBLIVION {read.symbol} {read.timeframe} {action}"
        f"{' ' + read.signal_kind if read.signal_kind else ''}"
        f"{' confirmed' if read.signal_confirmed else ''}] buy {read.score_buy} / sell {read.score_sell}, "
        f"bias {read.market_bias}{levels}"
        f"{' NEWS-BLOCKED' if read.news_blocked else ''} — {read.verdict_reason or ''}"
    )
    mirror("oblivion_read", content, symbol=read.symbol, session_id=read.session_id or "",
           source="oblivion", meta={"read_id": read.id, "source": read.source,
                                    "tradeable": read.tradeable, "timeframe": read.timeframe})
    if read.tradeable and read.entry and read.signal_action in ("buy", "sell"):
        _dispatch(
            _segment_sync,
            symbol=read.symbol, side=read.signal_action, entry=read.entry,
            stop_loss=read.stop_loss, take_profit=read.tp3 or read.tp1,
            confidence=None, price_at_signal=read.price, timeframe=read.timeframe,
            source="oblivion", agent_role="oblivion_engine",
            meta={"read_id": read.id, "kind": read.signal_kind, "tp1": read.tp1, "tp2": read.tp2},
        )


async def log_order(order: Dict[str, Any], *, symbol: str, action: str,
                    session_id: Optional[str] = None,
                    oblivion_read_id: Optional[int] = None) -> None:
    """Record one venue outcome (placed / skipped / error) as a decision + mirror."""
    status = str(order.get("status") or "?")
    venue = order.get("venue") or "?"
    where = order.get("account") or order.get("role") or venue
    reason = order.get("reason") or ""
    text = (
        f"{venue}/{order.get('role', '')} {where}: {status}"
        + (f" ticket {order['ticket']}" if order.get("ticket") else "")
        + (f" vol {order['volume']}" if order.get("volume") else "")
        + (f" entry {order.get('entry')} SL {order.get('stop_loss')} TP {order.get('take_profit')}"
           if order.get("entry") else "")
        + (f" — {reason}" if reason else "")
    )
    await record_decision(
        agent_name="Execution", agent_role="execution", symbol=symbol,
        action=action if status == "placed" else f"{action}:{status}",
        confidence=1.0 if status == "placed" else 0.0, reasoning=text,
        market_data=order, session_id=session_id, ai_called=False,
        origin="execution" if status == "placed" else "execution_blocked",
        oblivion_read_id=oblivion_read_id,
    )
    if status == "placed" and order.get("entry"):
        _dispatch(
            _segment_sync,
            symbol=symbol, side=action, entry=float(order.get("entry") or 0),
            stop_loss=order.get("stop_loss"), take_profit=order.get("take_profit"),
            confidence=None, price_at_signal=order.get("entry"), timeframe="",
            exchange=f"{venue}:{where}", source="room", agent_role="trade_executor",
            meta={k: order.get(k) for k in ("ticket", "volume", "account_id", "role", "levels")},
        )


def _backtest_sync(doc: Dict[str, Any]) -> Dict[str, int]:
    """Every simulated trade as a settled trade segment, plus a summary episode per pair."""
    from app.neuro_brain import is_enabled
    from app.neuro_brain.state_store import ingest_local, mark_outcome, record_trade_segment

    counts = {"segments": 0, "episodes": 0}
    if not is_enabled():
        _health["mirror_disabled"] += 1
        return counts
    run_id, p = doc.get("id"), doc.get("params") or {}
    tf = p.get("timeframe", "")
    for sym, res in (doc.get("results") or {}).items():
        for book, trades in (res.get("trades") or {}).items():
            for t in trades:
                if t.get("status") != "closed":
                    continue
                seg = record_trade_segment(
                    symbol=sym, side=t["action"], entry=float(t.get("fill") or t.get("entry") or 0),
                    stop_loss=t.get("initial_stop"), take_profit=t.get("tp3"), confidence=None,
                    price_at_signal=t.get("entry"), timeframe=tf, source="backtest",
                    agent_role=f"backtest_{book}",
                    meta={"run_id": run_id, "book": book, "kind": t.get("kind"), "entry_time": t.get("entry_time"),
                          "exit_time": t.get("exit_time"), "exit_reason": t.get("exit_reason"),
                          "r": t.get("r"), "reached": t.get("reached"), "tp1": t.get("tp1")},
                )
                if seg is not None:
                    mark_outcome(seg, t.get("outcome") or "breakeven", t.get("pnl"))
                    counts["segments"] += 1
        for book, s in (res.get("summary") or {}).items():
            wr = s.get("win_rate")
            text = (f"[BACKTEST #{run_id} {sym} {tf} {book}] {s['trades']} trades, {s['wins']}W/{s['losses']}L, "
                    f"win rate {'n/a' if wr is None else f'{wr * 100:.1f}%'}, net {s['net_profit']:+.2f}, "
                    f"max DD {s.get('max_drawdown_pct')}%, avg {s.get('avg_r')}R — profile {p.get('profile_label')}")
            if ingest_local(kind="backtest", content=text, symbol=sym, source="backtest",
                            meta={"run_id": run_id, "book": book, "summary": s, "params": p}):
                counts["episodes"] += 1
    _health["mirrored"] += counts["segments"] + counts["episodes"]
    return counts


async def log_backtest(doc: Dict[str, Any]) -> Dict[str, int]:
    """Store a finished backtest in the AI brain: DB rows per pair + every trade mirrored."""
    if suppressed():
        return {"segments": 0, "episodes": 0, "decisions": 0}
    run_id, p = doc.get("id"), doc.get("params") or {}
    decisions = 0
    for sym, res in (doc.get("results") or {}).items():
        for book, s in (res.get("summary") or {}).items():
            trades = [{k: t.get(k) for k in ("action", "kind", "entry_time", "exit_time", "fill", "initial_stop",
                                              "tp1", "tp3", "exit_reason", "outcome", "r", "pnl")}
                      for t in (res.get("trades") or {}).get(book, []) if t.get("status") == "closed"]
            if await record_decision(
                agent_name="Backtester", agent_role="backtest", symbol=sym,
                action=f"backtest_{book}", confidence=float(s.get("win_rate") or 0),
                reasoning=(f"Backtest #{run_id} {p.get('timeframe')} {book}: {s['trades']} trades, "
                           f"{s['wins']}W/{s['losses']}L, net {s['net_profit']:+.2f}"),
                market_data={"run_id": run_id, "params": p, "summary": s, "trades": trades},
                ai_called=False, origin="backtest",
            ):
                decisions += 1
    try:
        counts = await asyncio.to_thread(_backtest_sync, doc)
    except Exception as exc:  # noqa: BLE001
        _fail("backtest", exc)
        counts = {"segments": 0, "episodes": 0}
    return {**counts, "decisions": decisions}
