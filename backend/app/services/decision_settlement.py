"""Settle agent_decisions outcomes from closed trades, then evolve the seats.

The missing half of the room's learning loop: every buy/sell decision is
persisted with ``outcome`` / ``outcome_pnl`` columns, but nothing ever wrote
them automatically — only a manual API call did. With no resolved decisions the
win-rate stats stay empty and the self-improve pass (which needs >= 12 resolved
samples) never fires. This module closes that loop:

1. :func:`settle_agent_decisions` matches unresolved buy/sell decisions against
   closed positions in both order books (live ``trades`` and sandbox
   ``sim_positions``) by symbol + direction inside a time window, and writes
   the outcome and realised PnL.
2. :func:`auto_improve_agents` runs the existing revision-tracked self-improve
   pass for any seat whose resolved-decision count crosses
   ``self_improve.MIN_DECISIONS``, at most once per seat per day.

A background loop (:func:`run_learning_cycle`) drives both on a timer; see
``app.core.scheduler.start_decision_learning_loop``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Decisions older than this are expired as "no_fill" rather than left NULL
#: forever (a HOLD-the-door-forever row would pollute the pending queue).
_DECISION_EXPIRY_HOURS = 14 * 24

#: A position counts as the decision's outcome when it was opened at/after the
#: decision and closed within this window.
_MATCH_WINDOW_HOURS = 48
_OPEN_GRACE_MINUTES = 5

#: Minimum days between automatic instruction rewrites for one seat.
_IMPROVE_COOLDOWN_HOURS = 24


def _norm_symbol(symbol: str | None) -> str:
    return (symbol or "").upper().replace("/", "").replace("-", "").replace(":", "").strip()


def _outcome_for(pnl: float | None) -> str:
    if pnl is None:
        return "break_even"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "break_even"


async def settle_agent_decisions(
    db: AsyncSession, *, limit: int = 100
) -> dict[str, int]:
    """Match unresolved buy/sell decisions to closed trades in both books."""
    from app.models.database import AgentDecision, SimPosition, Trade

    now = datetime.utcnow()

    rows = await db.execute(
        select(AgentDecision)
        .where(
            AgentDecision.action.in_(["buy", "sell"]),
            AgentDecision.outcome.is_(None),
            AgentDecision.created_at >= now - timedelta(hours=_DECISION_EXPIRY_HOURS),
        )
        .order_by(AgentDecision.created_at)
        .limit(limit)
    )
    decisions = list(rows.scalars().all())
    if not decisions:
        return {"pending": 0, "settled": 0}

    # Live book: the opener Trade row is updated IN PLACE on close
    # (status="closed", pnl=realised) — see live.check_stop_loss_take_profit —
    # so its `side` is still the entry direction (buy=long, sell=short).
    live_rows = await db.execute(
        select(Trade).where(
            Trade.status == "closed",
            Trade.pnl.isnot(None),
            Trade.closed_at.isnot(None),
            Trade.closed_at >= now - timedelta(hours=_MATCH_WINDOW_HOURS + _DECISION_EXPIRY_HOURS),
        )
    )
    closed_live = list(live_rows.scalars().all())

    # Sandbox book: SimPosition carries side long/short + realized_pnl.
    sim_rows = await db.execute(
        select(SimPosition).where(
            SimPosition.status == "closed",
            SimPosition.realized_pnl.isnot(None),
            SimPosition.closed_at.isnot(None),
            SimPosition.closed_at >= now - timedelta(hours=_MATCH_WINDOW_HOURS + _DECISION_EXPIRY_HOURS),
        )
    )
    closed_sim = list(sim_rows.scalars().all())

    settled = 0
    for dec in decisions:
        # Defence in depth: the query already filters these, but a caller
        # handing us pre-loaded rows must never see an already-resolved or
        # non-directional decision overwritten.
        if (dec.action or "").lower() not in ("buy", "sell") or dec.outcome is not None:
            continue
        want_long = dec.action.lower() == "buy"
        opened_after = dec.created_at - timedelta(minutes=_OPEN_GRACE_MINUTES)
        close_before = dec.created_at + timedelta(hours=_MATCH_WINDOW_HOURS)
        sym = _norm_symbol(dec.symbol)

        match_pnl: float | None = None
        match_src = ""
        for tr in closed_live:
            if _norm_symbol(tr.symbol) != sym or tr.side is None:
                continue
            tr_long = tr.side.lower() == "buy"
            if tr_long != want_long:
                continue
            if tr.created_at and tr.created_at < opened_after:
                continue
            if not (opened_after <= tr.closed_at <= close_before):
                continue
            match_pnl = float(tr.pnl)
            match_src = f"trade#{tr.id}"
            break
        if match_pnl is None:
            for pos in closed_sim:
                if _norm_symbol(pos.symbol) != sym:
                    continue
                pos_long = (pos.side or "").lower() == "long"
                if pos_long != want_long:
                    continue
                if pos.created_at and pos.created_at < opened_after:
                    continue
                if not (opened_after <= pos.closed_at <= close_before):
                    continue
                match_pnl = float(pos.realized_pnl)
                match_src = f"sim#{pos.id}"
                break

        if match_pnl is None:
            continue
        dec.outcome = _outcome_for(match_pnl)
        dec.outcome_pnl = match_pnl
        dec.outcome_recorded_at = now
        settled += 1
        logger.info(
            "[Settle] decision#{} {} {} → {} ({:+.2f} USDT via {}) ",
            dec.id, dec.symbol.upper(), dec.action.upper(), dec.outcome,
            match_pnl, match_src,
        )

    if settled:
        await db.commit()
    return {"pending": len(decisions), "settled": settled}


async def auto_improve_agents(db: AsyncSession) -> list[dict[str, Any]]:
    """Self-improve any seat that has enough fresh evidence, at most daily."""

    from app.agents.self_improve import MIN_DECISIONS, improve_agent
    from app.models.database import (
        Agent,
        AgentDecision,
        AgentInstructionRevision,
    )

    agents = (
        await db.execute(select(Agent).where(Agent.is_active == True))  # noqa: E712
    ).scalars().all()
    if not agents:
        return []

    results: list[dict[str, Any]] = []
    cooldown = timedelta(hours=_IMPROVE_COOLDOWN_HOURS)
    for agent in agents:
        try:
            resolved = (
                await db.execute(
                    select(func.count(AgentDecision.id)).where(
                        AgentDecision.agent_id == agent.id,
                        AgentDecision.action.in_(["buy", "sell"]),
                        AgentDecision.outcome.isnot(None),
                    )
                )
            ).scalar() or 0
            if resolved < MIN_DECISIONS:
                continue
            last_revision = (
                await db.execute(
                    select(func.max(AgentInstructionRevision.created_at)).where(
                        AgentInstructionRevision.agent_id == agent.id
                    )
                )
            ).scalar()
            if last_revision and datetime.utcnow() - last_revision < cooldown:
                continue
            result = await improve_agent(db, agent)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 — one bad seat must not stop the rest
            logger.warning("[Settle] auto-improve failed for {}: {}", agent.role, exc)
            await db.rollback()
    return [r for r in results if r.get("changed")]


async def run_learning_cycle(*, limit: int = 100) -> dict[str, Any]:
    """One settle + evolve pass. Called by the scheduler loop."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        settle_stats = await settle_agent_decisions(db, limit=limit)
        improved: list[dict[str, Any]] = []
        if settle_stats.get("settled"):
            improved = await auto_improve_agents(db)
            if improved:
                logger.info(
                    "[Settle] auto-improved {} seat(s): {}",
                    len(improved),
                    ", ".join(str(r.get("role")) for r in improved),
                )
    return {**settle_stats, "improved": improved}
