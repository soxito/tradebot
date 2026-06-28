"""
Agent Paul Plugin — API Router

All routes prefixed at /plugins/agent-paul by the plugin loader.
"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.AgentPaulPlugin.backend.models import PaulDecision
from plugins.AgentPaulPlugin.backend.schemas import (
    PaulSettingsUpdate,
    PaulDecideRequest,
    PaulUnifyRequest,
)
from plugins.AgentPaulPlugin.backend.services.paul_loop import (
    PaulLoop,
    PaulError,
    PaulDisabledError,
    PaulNotFoundError,
    PaulStateError,
)

router = APIRouter(prefix="/plugins/agent-paul", tags=["Agent Paul"])


# ── DB dependency ──────────────────────────────────────────


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Serialization ──────────────────────────────────────────


def _settings_to_dict(s) -> Dict[str, Any]:
    return {
        "enabled": s.enabled,
        "mode": s.mode.value,
        "require_approval": s.require_approval,
        "kill_switch": s.kill_switch,
        "default_timeframe": s.default_timeframe,
        "min_confidence": s.min_confidence,
        "allowed_symbols": s.allowed_symbols,
        "risk_max_position_usdt": s.risk_max_position_usdt,
        "risk_max_open_positions": s.risk_max_open_positions,
        "max_queue_size": s.max_queue_size,
        "cooldown_minutes": s.cooldown_minutes,
        "mt5_default_account_id": s.mt5_default_account_id,
        "mt5_default_volume": s.mt5_default_volume,
        "mt5_timeframe": s.mt5_timeframe,
        "mt5_min_rr": s.mt5_min_rr,
    }


def _decision_to_dict(d: PaulDecision) -> Dict[str, Any]:
    return {
        "id": d.id,
        "session_id": d.session_id,
        "symbol": d.symbol,
        "timeframe": d.timeframe,
        "trigger": d.trigger,
        "mode": d.mode.value,
        "provenance": d.provenance.value,
        "market": d.market,
        "account_id": d.account_id,
        "volume": d.volume,
        "action": d.action,
        "confidence": d.confidence,
        "entry": d.entry,
        "stop_loss": d.stop_loss,
        "take_profit": d.take_profit,
        "risk_reward": d.risk_reward,
        "reasoning": d.reasoning,
        "acceptance_criteria": d.acceptance_criteria,
        "qualify_status": d.qualify_status.value,
        "qualify_notes": d.qualify_notes,
        "status": d.status.value,
        "signal_id": d.signal_id,
        "execution_result": d.execution_result,
        "error": d.error,
        "outcome": d.outcome,
        "outcome_pnl": d.outcome_pnl,
        "unify_notes": d.unify_notes,
        "unified_at": str(d.unified_at) if d.unified_at else None,
        "created_at": str(d.created_at) if d.created_at else None,
        "updated_at": str(d.updated_at) if d.updated_at else None,
    }


# ── Status & Settings ──────────────────────────────────────


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Connectivity, mode, risk limits, and live counters."""
    return await PaulLoop.status(db)


@router.get("/loop-info")
async def get_loop_info():
    """Static PAUL framework reference for the workflow tab (dev-workflow layer)."""
    return PaulLoop.loop_info()


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    s = await PaulLoop.get_settings(db)
    return _settings_to_dict(s)


@router.put("/settings")
async def update_settings(payload: PaulSettingsUpdate, db: AsyncSession = Depends(get_db)):
    s = await PaulLoop.update_settings(db, payload.model_dump(exclude_unset=True))
    return _settings_to_dict(s)


# ── Decision console ───────────────────────────────────────


@router.post("/decide")
async def decide(payload: PaulDecideRequest, db: AsyncSession = Depends(get_db)):
    """Run one PAUL loop (Plan → Qualify → Apply/Queue) for a symbol."""
    try:
        decision = await PaulLoop.decide(
            db, payload.symbol, payload.timeframe, payload.trigger,
            market=payload.market, account_id=payload.account_id,
        )
    except PaulDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaulError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _decision_to_dict(decision)


# ── Queue & history ────────────────────────────────────────


@router.get("/queue")
async def get_queue(db: AsyncSession = Depends(get_db)):
    """Decisions awaiting human approval."""
    rows = await PaulLoop.list_decisions(db, limit=100, queued_only=True)
    return {"queue": [_decision_to_dict(d) for d in rows]}


@router.get("/decisions")
async def get_decisions(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Recent decision history (audit trail)."""
    rows = await PaulLoop.list_decisions(db, limit=limit)
    return {"decisions": [_decision_to_dict(d) for d in rows]}


@router.post("/decisions/{decision_id}/approve")
async def approve_decision(decision_id: int, db: AsyncSession = Depends(get_db)):
    try:
        decision = await PaulLoop.approve(db, decision_id)
    except PaulNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PaulStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _decision_to_dict(decision)


@router.post("/decisions/{decision_id}/reject")
async def reject_decision(decision_id: int, db: AsyncSession = Depends(get_db)):
    try:
        decision = await PaulLoop.reject(db, decision_id)
    except PaulNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _decision_to_dict(decision)


@router.post("/decisions/{decision_id}/execute")
async def execute_decision(decision_id: int, db: AsyncSession = Depends(get_db)):
    try:
        decision = await PaulLoop.execute(db, decision_id)
    except PaulNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PaulStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _decision_to_dict(decision)


@router.post("/decisions/{decision_id}/unify")
async def unify_decision(
    decision_id: int, payload: PaulUnifyRequest, db: AsyncSession = Depends(get_db)
):
    """Close the loop — reconcile outcome and PnL."""
    try:
        decision = await PaulLoop.unify(
            db, decision_id, payload.outcome, payload.pnl, payload.notes
        )
    except PaulNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _decision_to_dict(decision)
