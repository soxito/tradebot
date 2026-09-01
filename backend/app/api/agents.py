"""
AI Agent API Routes — CRUD for agents + orchestration triggers.
Optional: Guarded by ENABLE_AI_AGENTS setting. CRUD always works,
analysis endpoints require the toggle to be ON.
"""
import json
import os
from typing import Literal, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from loguru import logger

from app.core.database import get_db
from app.core.config import settings
from app.models.database import Agent, AgentDecision, RoomAgentProfile
from app.agents.specialists import DEFAULT_AGENTS
from app.agents.orchestrator import AgentOrchestrator
from app.agents import room
from app.agents.memory import get_learning_stats
from app.core.timezone import now_sast

router = APIRouter(prefix="/agents", tags=["agents"])


# ─── Pydantic Schemas ───────────────────────────────────────


class AgentCreate(BaseModel):
    name: str
    role: str
    description: Optional[str] = None
    system_prompt: str
    model: str = "fable-5-high"
    temperature: float = 0.3
    max_tokens: int = 2000
    is_active: bool = True
    pairs: Optional[str] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    is_active: Optional[bool] = None
    pairs: Optional[str] = None


class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"


class AnalyzeMultipleRequest(BaseModel):
    symbols: List[str]
    timeframe: str = "1h"


# ─── Agent CRUD ──────────────────────────────────────────────


@router.get("")
async def list_agents(db: AsyncSession = Depends(get_db)):
    """List all agents."""
    result = await db.execute(select(Agent).order_by(Agent.id))
    agents = result.scalars().all()
    return {
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "description": a.description,
                "system_prompt": a.system_prompt,
                "model": a.model,
                "temperature": a.temperature,
                "max_tokens": a.max_tokens,
                "is_active": a.is_active,
                "pairs": a.pairs,
                "created_at": str(a.created_at) if a.created_at else None,
                "updated_at": str(a.updated_at) if a.updated_at else None,
            }
            for a in agents
        ]
    }


@router.post("")
async def create_agent(data: AgentCreate, db: AsyncSession = Depends(get_db)):
    """Create a new agent."""
    agent = Agent(
        name=data.name,
        role=data.role,
        description=data.description,
        system_prompt=data.system_prompt,
        model=data.model,
        temperature=data.temperature,
        max_tokens=data.max_tokens,
        is_active=data.is_active,
        pairs=data.pairs,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return {"agent": {"id": agent.id, "name": agent.name, "role": agent.role}}


@router.put("/{agent_id}")
async def update_agent(agent_id: int, data: AgentUpdate, db: AsyncSession = Depends(get_db)):
    """Update an existing agent."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)

    await db.commit()
    return {"agent": {"id": agent.id, "name": agent.name, "role": agent.role, "is_active": agent.is_active}}


@router.delete("/{agent_id}")
async def delete_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an agent."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    await db.delete(agent)
    await db.commit()
    return {"deleted": agent_id}


@router.post("/{agent_id}/toggle")
async def toggle_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    """Toggle agent active/inactive."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.is_active = not agent.is_active
    await db.commit()
    return {"id": agent.id, "is_active": agent.is_active}


@router.post("/seed-defaults")
async def seed_default_agents(db: AsyncSession = Depends(get_db)):
    """Create default agents, adding any missing roles."""
    result = await db.execute(select(Agent))
    existing = result.scalars().all()
    existing_roles = {a.role for a in existing}

    created = []
    for spec in DEFAULT_AGENTS:
        if spec["role"] not in existing_roles:
            agent = Agent(**spec)
            db.add(agent)
            created.append(spec["name"])

    if created:
        await db.commit()

    return {
        "message": f"Created {len(created)} agent(s)" if created else f"All {len(existing)} agents already exist",
        "agents": created,
        "existing": len(existing),
    }


# ─── Trading Room ────────────────────────────────────────────


@router.get("/room/state")
async def room_state(db: AsyncSession = Depends(get_db)):
    """Live snapshot of the trading room — seats, personas and recent sessions.

    Lets a client that opens the room mid-flight (or after background work)
    render the current picture without waiting for the next SSE event.
    """
    result = await db.execute(select(Agent).where(Agent.is_active == True))
    rows = result.scalars().all()

    snap = room.snapshot()
    live = {a["role"]: a for a in snap["agents"]}
    seats = []
    for r in rows:
        persona = room.persona_for(r.role)
        seats.append({
            **persona,
            "agent_id": r.id,
            "agent_name": r.name,
            "description": r.description,
            "pairs": r.pairs,
            "state": room.IDLE,
            **{k: v for k, v in live.get(r.role, {}).items() if k not in persona},
        })
    seats.sort(key=lambda s: s["seat"])

    return {
        "focus_symbol": snap["focus_symbol"],
        "focus_symbols": snap.get("focus_symbols", []),
        "ceo": snap["ceo"],
        "seats": seats,
        "sessions": snap["sessions"],
        "server_time": snap["server_time"],
    }


class FocusRequest(BaseModel):
    # Back-compat: a single `symbol`, or a list of `symbols`. Null/empty clears.
    symbol: Optional[str] = None
    symbols: Optional[List[str]] = None


@router.post("/room/focus")
async def set_room_focus(data: FocusRequest, db: AsyncSession = Depends(get_db)):
    """Pin every agent to one or more pairs. Pass nothing to resume free roaming."""
    if data.symbols is not None:
        chosen: Optional[object] = data.symbols
    elif data.symbol is not None:
        chosen = data.symbol
    else:
        chosen = None

    await room.set_focus(chosen)
    symbols = room.get_focus_symbols()

    # Persist as a comma-joined string so the pin(s) survive a restart.
    try:
        from app.agents.execution import get_settings
        s = await get_settings(db)
        s.focus_symbol = ",".join(symbols) if symbols else None
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - the live pin still took effect
        logger.warning(f"[room] focus set to {symbols} but could not be persisted: {exc}")

    return {"focus_symbol": room.get_focus_symbol(), "focus_symbols": symbols}


class ProfileUpdate(BaseModel):
    """Who an agent is in the room, plus the config the orchestrator runs on."""
    agent_id: int
    human_name: Optional[str] = None
    title: Optional[str] = None
    color: Optional[str] = None
    seat: Optional[int] = None
    gender: Optional[Literal["male", "female"]] = None
    tasks: Optional[str] = None
    # Passed straight through to the Agent row
    name: Optional[str] = None
    role: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    is_active: Optional[bool] = None
    pairs: Optional[str] = None


async def refresh_persona_overrides(db: AsyncSession) -> None:
    """Push saved profiles into the in-memory persona table the room emits with."""
    rows = (await db.execute(
        select(RoomAgentProfile, Agent).join(Agent, Agent.id == RoomAgentProfile.agent_id)
    )).all()
    room.set_persona_overrides({
        agent.role: {
            "human_name": profile.human_name,
            "title": profile.title,
            "color": profile.color,
            "seat": profile.seat,
            "gender": profile.gender,
            "tasks": profile.tasks,
        }
        for profile, agent in rows
    })


@router.get("/room/profiles")
async def list_room_profiles(db: AsyncSession = Depends(get_db)):
    """Every agent with its room identity — the settings page's source of truth."""
    agents = (await db.execute(select(Agent).order_by(Agent.id))).scalars().all()
    profiles = {
        p.agent_id: p
        for p in (await db.execute(select(RoomAgentProfile))).scalars().all()
    }

    out = []
    for a in agents:
        persona = room.persona_for(a.role)
        p = profiles.get(a.id)
        out.append({
            "agent_id": a.id,
            "name": a.name,
            "role": a.role,
            "description": a.description,
            "system_prompt": a.system_prompt,
            "model": a.model,
            "is_active": a.is_active,
            "pairs": a.pairs,
            "human_name": p.human_name if p else persona["human_name"],
            "title": p.title if p else persona["title"],
            "color": p.color if p else persona["color"],
            "seat": p.seat if p else persona["seat"],
            "gender": p.gender if p else persona["gender"],
            "tasks": p.tasks if p else None,
            "customised": p is not None,
        })
    out.sort(key=lambda r: r["seat"])
    return {"profiles": out}


@router.put("/room/profiles")
async def save_room_profiles(
    updates: List[ProfileUpdate], db: AsyncSession = Depends(get_db)
):
    """Rename, reseat, recolour and re-task the agents in one save."""
    saved = 0
    for u in updates:
        agent = await db.get(Agent, u.agent_id)
        if not agent:
            continue

        for field in ("name", "role", "system_prompt", "model", "is_active", "pairs"):
            value = getattr(u, field)
            if value is not None:
                setattr(agent, field, value)

        profile = (await db.execute(
            select(RoomAgentProfile).where(RoomAgentProfile.agent_id == u.agent_id)
        )).scalar_one_or_none()
        if profile is None:
            persona = room.persona_for(agent.role)
            profile = RoomAgentProfile(
                agent_id=u.agent_id,
                human_name=persona["human_name"],
                title=persona["title"],
                color=persona["color"],
                seat=persona["seat"],
                gender=persona["gender"],
            )
            db.add(profile)

        for field in ("human_name", "title", "color", "seat", "gender", "tasks"):
            value = getattr(u, field)
            if value is not None:
                setattr(profile, field, value)
        saved += 1

    await db.commit()
    await refresh_persona_overrides(db)
    return {"saved": saved}


@router.post("/room/profiles/seed-instructions")
async def seed_room_instructions(
    overwrite: bool = False, db: AsyncSession = Depends(get_db)
):
    """Fill each agent's standing instructions with the worked example for its role.

    Only writes into an empty brief unless ``overwrite`` is set, so a desk that
    has already written its own instructions (or had them improved) is never
    silently reset.
    """
    from app.agents.room import DEFAULT_TASKS

    agents = (await db.execute(select(Agent))).scalars().all()
    profiles = {
        p.agent_id: p
        for p in (await db.execute(select(RoomAgentProfile))).scalars().all()
    }

    seeded = []
    for a in agents:
        sample = DEFAULT_TASKS.get(a.role)
        if not sample:
            continue

        profile = profiles.get(a.id)
        if profile is None:
            persona = room.persona_for(a.role)
            profile = RoomAgentProfile(
                agent_id=a.id,
                human_name=persona["human_name"],
                title=persona["title"],
                color=persona["color"],
                seat=persona["seat"],
                gender=persona["gender"],
            )
            db.add(profile)

        if profile.tasks and not overwrite:
            continue
        profile.tasks = sample
        seeded.append(a.role)

    await db.commit()
    await refresh_persona_overrides(db)
    return {"seeded": seeded, "count": len(seeded)}


@router.get("/room/self-improve/history")
async def self_improve_history(
    role: Optional[str] = None, limit: int = 25, db: AsyncSession = Depends(get_db)
):
    """Past instruction rewrites, newest first — the audit trail."""
    from app.models.database import AgentInstructionRevision

    stmt = select(AgentInstructionRevision).order_by(AgentInstructionRevision.id.desc()).limit(limit)
    if role:
        stmt = stmt.where(AgentInstructionRevision.role == role)

    return {
        "revisions": [
            {
                "id": r.id,
                "role": r.role,
                "agent_id": r.agent_id,
                "previous_instructions": r.previous_instructions,
                "new_instructions": r.new_instructions,
                "rationale": r.rationale,
                "decisions_reviewed": r.decisions_reviewed,
                "win_rate": r.win_rate,
                "avg_pnl": r.avg_pnl,
                "applied": r.applied,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in (await db.execute(stmt)).scalars().all()
        ]
    }


@router.get("/room/self-improve/scores")
async def self_improve_scores(db: AsyncSession = Depends(get_db)):
    """How each agent is actually performing — the input to the next rewrite."""
    from app.agents.self_improve import MIN_DECISIONS, score_agent

    agents = (await db.execute(select(Agent).where(Agent.is_active == True))).scalars().all()  # noqa: E712
    out = []
    for a in agents:
        s = await score_agent(db, a)
        out.append({
            "role": s.role,
            "agent_name": s.agent_name,
            "total": s.total,
            "resolved": s.resolved,
            "wins": s.wins,
            "losses": s.losses,
            "break_even": s.break_even,
            "win_rate": s.win_rate,
            "avg_pnl": s.avg_pnl,
            "avg_conf_win": round(s.avg_conf_win, 3),
            "avg_conf_loss": round(s.avg_conf_loss, 3),
            "enough_history": s.enough_history,
        })
    return {"scores": out, "min_decisions": MIN_DECISIONS}


@router.post("/room/self-improve/run")
async def self_improve_run(
    role: Optional[str] = None,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Score the agents and rewrite the instructions of those with the history for it.

    ``force`` runs the rewrite even below the minimum sample size — useful for
    trying it out, but a rewrite from a handful of trades is noise-fitting.
    """
    from app.agents.self_improve import improve_agent, improve_all

    if role:
        agent = (
            await db.execute(select(Agent).where(Agent.role == role))
        ).scalars().first()
        if agent is None:
            raise HTTPException(status_code=404, detail=f"No agent with role {role}")
        result = await improve_agent(db, agent, force=force)
        await refresh_persona_overrides(db)
        return {"results": [result]}

    return {"results": await improve_all(db, force=force)}


@router.post("/room/self-improve/revert/{revision_id}")
async def self_improve_revert(revision_id: int, db: AsyncSession = Depends(get_db)):
    """Put back the instructions a given revision replaced."""
    from app.agents.self_improve import revert_revision

    result = await revert_revision(db, revision_id)
    if not result.get("reverted"):
        raise HTTPException(status_code=404, detail=result.get("reason", "revert failed"))
    return result


class RoomSettingsUpdate(BaseModel):
    execution_enabled: Optional[bool] = None
    dry_run: Optional[bool] = None
    allow_sim: Optional[bool] = None
    allow_crypto: Optional[bool] = None
    allow_mt5: Optional[bool] = None
    mt5_account_id: Optional[int] = None
    mt5_live_mode: Optional[bool] = None
    mt5_demo_account_id: Optional[int] = None
    risk_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    min_consensus: Optional[float] = None
    min_confidence: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_leverage: Optional[int] = None
    focus_interval_s: Optional[int] = None
    focus_timeframe: Optional[str] = None
    worker_enabled: Optional[bool] = None
    manage_copy_profiles: Optional[bool] = None
    copy_max_drawdown_pct: Optional[float] = None
    # Bitcoin 1064-day cycle — anchors are cycle-bottom ISO dates.
    cycle_anchors: Optional[List[str]] = None
    cycle_bull_days: Optional[int] = None
    cycle_bear_days: Optional[int] = None
    cycle_auto_risk: Optional[bool] = None
    cycle_risk_multiplier: Optional[float] = None
    cycle_history_years: Optional[int] = None


def _settings_payload(s, db_extra: dict | None = None) -> dict:
    return {
        "execution_enabled": s.execution_enabled,
        "dry_run": s.dry_run,
        "allow_sim": s.allow_sim,
        "allow_crypto": s.allow_crypto,
        "allow_mt5": s.allow_mt5,
        "mt5_account_id": s.mt5_account_id,
        "mt5_live_mode": getattr(s, "mt5_live_mode", False),
        "mt5_demo_account_id": getattr(s, "mt5_demo_account_id", None),
        "risk_pct": s.risk_pct,
        "max_open_positions": s.max_open_positions,
        "min_consensus": s.min_consensus,
        "min_confidence": s.min_confidence,
        "max_trades_per_day": s.max_trades_per_day,
        "max_leverage": s.max_leverage,
        "focus_interval_s": getattr(s, "focus_interval_s", 300),
        "focus_timeframe": getattr(s, "focus_timeframe", "1h") or "1h",
        "worker_enabled": getattr(s, "worker_enabled", True),
        "manage_copy_profiles": getattr(s, "manage_copy_profiles", False),
        "copy_max_drawdown_pct": getattr(s, "copy_max_drawdown_pct", 20.0),
        "focus_symbol": getattr(s, "focus_symbol", None),
        # Cycle settings — anchors come back as a list regardless of storage.
        "cycle_anchors": _cycle_anchors_out(getattr(s, "cycle_anchors", None)),
        "cycle_bull_days": getattr(s, "cycle_bull_days", 1064) or 1064,
        "cycle_bear_days": getattr(s, "cycle_bear_days", 365) or 365,
        "cycle_auto_risk": getattr(s, "cycle_auto_risk", False),
        "cycle_risk_multiplier": getattr(s, "cycle_risk_multiplier", 0.5),
        "cycle_history_years": getattr(s, "cycle_history_years", 15) or 15,
        **(db_extra or {}),
    }


def _cycle_anchors_out(raw) -> list[str]:
    """Persisted anchor JSON → list of ISO dates for the client."""
    import json

    from app.services.market_cycle import DEFAULT_ANCHORS

    if not raw:
        return list(DEFAULT_ANCHORS)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(a) for a in parsed]
    except (ValueError, TypeError):
        pass
    return list(DEFAULT_ANCHORS)


@router.get("/room/copy-overview")
async def get_room_copy_overview(db: AsyncSession = Depends(get_db)):
    """Copy-trading supervision state: gate, latest supervisor decisions, profiles."""
    from app.models.database import AgentDecision
    from app.agents.copy_supervisor import SUPERVISOR_AGENT_ROLE

    gate_on = False
    try:
        from app.agents.execution import get_settings

        s = await get_settings(db)
        gate_on = bool(getattr(s, "manage_copy_profiles", False))
    except Exception:  # noqa: BLE001 - plugin/room optional
        pass

    decisions = []
    try:
        result = await db.execute(
            select(AgentDecision)
            .where(AgentDecision.agent_role == SUPERVISOR_AGENT_ROLE)
            .order_by(AgentDecision.id.desc())
            .limit(20)
        )
        for d in result.scalars().all():
            decisions.append({
                "id": d.id, "symbol": d.symbol, "action": d.action,
                "reasoning": d.reasoning, "confidence": d.confidence,
                "created_at": str(d.created_at) if hasattr(d, "created_at") else None,
            })
    except Exception:  # noqa: BLE001
        pass

    return {"manage_copy_profiles": gate_on, "decisions": decisions}


@router.get("/room/settings")
async def get_room_settings(db: AsyncSession = Depends(get_db)):
    """Execution policy, plus the context the settings page needs to render it."""
    from app.agents.execution import get_settings, trades_today

    s = await get_settings(db)

    mt5_accounts = []
    try:
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        mt5_accounts = [
            {"id": a.id, "name": a.name, "login": a.login,
             "balance": a.balance, "equity": a.equity, "currency": a.currency}
            for a in (await db.execute(select(MT5Account))).scalars().all()
        ]
    except Exception:  # noqa: BLE001 - plugin-optional
        pass

    return _settings_payload(s, {
        "trades_today": trades_today(),
        "mt5_accounts": mt5_accounts,
        # The .env master switch still outranks everything on this page.
        "global_auto_trading_enabled": settings.ENABLE_AUTO_TRADING,
    })


@router.put("/room/settings")
async def update_room_settings(data: RoomSettingsUpdate, db: AsyncSession = Depends(get_db)):
    """Update the execution policy.

    Going live is deliberately awkward: turning ``dry_run`` off while the global
    ENABLE_AUTO_TRADING flag is unset is rejected rather than silently ignored,
    so nobody believes they armed something they did not.
    """
    from app.agents.execution import get_settings

    s = await get_settings(db)

    if data.dry_run is False and not settings.ENABLE_AUTO_TRADING:
        raise HTTPException(
            status_code=403,
            detail=(
                "Live execution needs ENABLE_AUTO_TRADING=true in your .env. "
                "Dry run stays on until then."
            ),
        )
    if data.risk_pct is not None and not (0 < data.risk_pct <= 10):
        raise HTTPException(status_code=400, detail="risk_pct must be between 0 and 10")
    if data.max_open_positions is not None and not (0 < data.max_open_positions <= 50):
        raise HTTPException(status_code=400, detail="max_open_positions must be between 1 and 50")

    from app.workers.room_worker import FOCUS_INTERVAL_CHOICES, FOCUS_TIMEFRAME_CHOICES

    if data.focus_interval_s is not None and data.focus_interval_s not in FOCUS_INTERVAL_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"focus_interval_s must be one of {list(FOCUS_INTERVAL_CHOICES)}",
        )
    if data.focus_timeframe is not None:
        data.focus_timeframe = data.focus_timeframe.strip().lower()
        if data.focus_timeframe not in FOCUS_TIMEFRAME_CHOICES:
            raise HTTPException(
                status_code=400,
                detail=f"focus_timeframe must be one of {list(FOCUS_TIMEFRAME_CHOICES)}",
            )

    # ── Cycle settings validation ──
    # Anchors must be real ISO dates in the past; the calendar cannot anchor a
    # cycle to a day that has not happened.
    if data.cycle_anchors is not None:
        from datetime import date as _date

        cleaned: list[str] = []
        for raw in data.cycle_anchors:
            try:
                parsed = _date.fromisoformat(str(raw).strip()[:10])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"cycle anchor '{raw}' is not an ISO date (YYYY-MM-DD)",
                )
            if parsed >= _date.today():
                raise HTTPException(
                    status_code=400,
                    detail=f"cycle anchor '{raw}' must be in the past",
                )
            cleaned.append(parsed.isoformat())
        if not cleaned:
            raise HTTPException(status_code=400, detail="at least one cycle anchor is required")
        data.cycle_anchors = sorted(cleaned)
    if data.cycle_bull_days is not None and not (200 <= data.cycle_bull_days <= 2000):
        raise HTTPException(status_code=400, detail="cycle_bull_days must be 200–2000")
    if data.cycle_bear_days is not None and not (60 <= data.cycle_bear_days <= 1200):
        raise HTTPException(status_code=400, detail="cycle_bear_days must be 60–1200")
    if data.cycle_history_years is not None and not (1 <= data.cycle_history_years <= 20):
        raise HTTPException(status_code=400, detail="cycle_history_years must be 1–20")
    if data.cycle_risk_multiplier is not None and not (0 < data.cycle_risk_multiplier <= 1):
        raise HTTPException(
            status_code=400,
            detail="cycle_risk_multiplier must be between 0 and 1"
        )
    if data.copy_max_drawdown_pct is not None and not (
        0 < data.copy_max_drawdown_pct <= 100
    ):
        raise HTTPException(
            status_code=400,
            detail="copy_max_drawdown_pct must be between 0 and 100",
        )

    # Boolean fields must be settable to False, not just truthy values.
    _bool_fields = {
        "execution_enabled", "dry_run", "allow_sim", "allow_crypto",
        "allow_mt5", "worker_enabled", "mt5_live_mode", "cycle_auto_risk",
        "manage_copy_profiles",
    }
    updates = data.model_dump(exclude_unset=True)
    # The anchors column is TEXT; the API contract is a list.
    if "cycle_anchors" in updates and updates["cycle_anchors"] is not None:
        updates["cycle_anchors"] = json.dumps(updates["cycle_anchors"])
    for field, value in updates.items():
        if value is not None or field in _bool_fields:
            setattr(s, field, value)

    await db.commit()
    await db.refresh(s)

    # The cached snapshot was built from the previous anchors — drop it so the
    # next read (and every agent seat) sees the new calendar immediately.
    if any(k.startswith("cycle_") for k in updates):
        try:
            from app.services import market_cycle

            market_cycle.reset_cache()
        except Exception:  # noqa: BLE001 - cache invalidation is best-effort
            pass

    # Push the cadence into the live worker so a change takes effect on the very
    # next cycle rather than at the next restart.
    if data.focus_interval_s is not None:
        from app.workers.room_worker import set_focus_interval
        set_focus_interval(data.focus_interval_s)

    # Same for the timeframe: the next meeting analyses on it, and the room's
    # chart draws it, without waiting for a restart.
    if data.focus_timeframe is not None:
        from app.workers.room_worker import set_focus_timeframe
        set_focus_timeframe(data.focus_timeframe)

    # Arming/disarming the 24/7 board from the same switch that persists it.
    if data.worker_enabled is not None:
        from app.workers.room_worker import start_room_worker, stop_room_worker
        if data.worker_enabled:
            start_room_worker(
                settings.ROOM_WORKER_INTERVAL_SECONDS,
                settings.ROOM_WORKER_COOLDOWN_SECONDS,
                focus_interval=s.focus_interval_s,
            )
        else:
            stop_room_worker()
    logger.warning(
        f"[room] execution policy updated — enabled={s.execution_enabled} "
        f"dry_run={s.dry_run} risk={s.risk_pct}% venues="
        f"{[v for v, on in (('sim', s.allow_sim), ('crypto', s.allow_crypto), ('mt5', s.allow_mt5)) if on]}"
    )
    return _settings_payload(s)


class RoomBriefRequest(BaseModel):
    symbol: str
    timeframe: Optional[str] = None
    #: Re-run the seats rather than describing the last meeting. The room is
    #: expensive, so the default is to report what it already concluded.
    analyse: bool = True


@router.post("/room/brief")
async def room_brief(data: RoomBriefRequest, db: AsyncSession = Depends(get_db)):
    """Everything the ``/room`` command sends on Telegram, as JSON.

    The web room showed seats, states and a verdict; the Telegram command sent
    the verdict *plus* the structural read, the plan's levels, the copyable
    signal card, the forecast and a drawn chart. Two surfaces of the same desk
    should not deliver different amounts of the same analysis, so this is the
    one builder both of them call.
    """
    from app.agents.orchestrator import AgentOrchestrator
    from app.workers.room_worker import get_focus_timeframe

    symbol = (data.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    # The room's own timeframe unless the caller asked for another, so a brief
    # opened from the room describes the same bars the board just argued over.
    timeframe = (data.timeframe or get_focus_timeframe()).strip().lower()

    result: dict = {}
    if data.analyse:
        result = await AgentOrchestrator.analyze_symbol(
            db, symbol, timeframe=timeframe, trigger="manual"
        )
    else:
        # Describe the most recent meeting on this pair instead of convening.
        for session in reversed(room.snapshot().get("sessions") or []):
            if str(session.get("symbol") or "").upper() == symbol:
                result = {
                    "symbol": symbol,
                    "timeframe": session.get("timeframe") or timeframe,
                    "final_action": session.get("final_action"),
                    "final_confidence": session.get("final_confidence"),
                    "final_reasoning": session.get("final_reasoning"),
                    "decisions": session.get("decisions") or [],
                }
                break
        if not result:
            raise HTTPException(
                status_code=404, detail=f"The room has not met on {symbol} yet"
            )

    payload: dict = {
        "symbol": symbol,
        "timeframe": timeframe,
        "result": result,
        "consensus": room.consensus_from(result.get("decisions") or []),
        "forecast": result.get("kronos_forecast"),
        "momentum": result.get("momentum"),
        # The season and the whale flow the verdict was made under — the same
        # snapshot the seats received, so the web brief matches the /room card.
        "btc_cycle": result.get("btc_cycle"),
        "btc_whales": result.get("btc_whales"),
    }

    # The narrative half — best-effort, and each piece independently: a missing
    # chart must not cost the levels, and missing levels must not cost the read.
    try:
        import base64

        from app.services import candles as candle_source
        from plugins.TelegramSignalNewsPlugin.backend.services import room_bridge

        candles = await candle_source.fetch(symbol, timeframe)
        payload["candles_available"] = len(candles)
        price = float(result.get("price") or (candles[-1][4] if candles else 0) or 0)
        overlay, chart = await room_bridge.room_plan(symbol, timeframe, result, price)
        payload["plan"] = {
            "direction": getattr(overlay, "direction", None),
            "entry": getattr(overlay, "entry", None),
            "stop_loss": getattr(overlay, "stop_loss", None),
            "take_profits": list(getattr(overlay, "take_profits", []) or []),
        } if overlay else None
        payload["plan_levels_text"] = room_bridge.plan_levels_text(overlay)
        payload["market_read"] = (
            await room_bridge.market_read_text(symbol, timeframe, candles)
            if candles else ""
        )
        payload["signal_card"] = await room_bridge.signal_card_for(
            result, symbol, overlay, candles=candles,
        )
        payload["chart_png_base64"] = (
            base64.b64encode(chart).decode("ascii") if chart else None
        )
    except Exception as exc:  # noqa: BLE001 — the verdict is the deliverable
        logger.warning(f"[Agents] room brief extras unavailable for {symbol}: {exc}")
        payload.setdefault("chart_png_base64", None)

    # How earlier plans on this pair are tracking — the follow-up the Telegram
    # command leads with, and the web room never had at all.
    try:
        from app.services.scenario_tracker import scenario_narrative, track_symbol

        payload["scenario_follow_up"] = scenario_narrative(await track_symbol(db, symbol))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Agents] scenario follow-up skipped for {symbol}: {exc}")
        payload["scenario_follow_up"] = ""

    return payload


@router.get("/room/worker")
async def room_worker_state():
    """Whether the room keeps meeting while nobody is watching."""
    from app.workers.room_worker import room_worker_status
    return room_worker_status()


@router.post("/room/worker/start")
async def room_worker_start(db: AsyncSession = Depends(get_db)):
    from app.agents.execution import get_settings
    from app.workers.room_worker import room_worker_status, start_room_worker

    s = await get_settings(db)
    started = start_room_worker(
        settings.ROOM_WORKER_INTERVAL_SECONDS,
        settings.ROOM_WORKER_COOLDOWN_SECONDS,
        focus_interval=getattr(s, "focus_interval_s", 300),
    )
    return {"started": started, **room_worker_status()}


@router.post("/room/worker/stop")
async def room_worker_stop():
    from app.workers.room_worker import room_worker_status, stop_room_worker
    stopped = stop_room_worker()
    return {"stopped": stopped, **room_worker_status()}


# ─── Status & Toggle ─────────────────────────────────────────


@router.get("/status")
async def agent_status(db: AsyncSession = Depends(get_db)):
    """Check agent system status including AI toggle, circuit breaker, and learning stats."""
    from app.agents.base import get_ai_status
    from app.agents.custom_agents import get_custom_agent_status
    api_key = os.getenv("OPENAI_API_KEY", "")
    result = await db.execute(select(Agent))
    agents = result.scalars().all()
    active = [a for a in agents if a.is_active]

    learning = await get_learning_stats(db)
    ai_status = get_ai_status()

    return {
        "ai_enabled": settings.ENABLE_AI_AGENTS,
        "openai_configured": bool(api_key),
        "ai_available": ai_status["available"],
        "circuit_breaker": {
            "open": ai_status["circuit_breaker_open"],
            "reason": ai_status.get("circuit_breaker_reason", ""),
            "remaining_s": ai_status.get("circuit_breaker_remaining_s", 0),
        },
        "custom_agents": get_custom_agent_status(),
        "model": os.getenv("OPENAI_MODEL", "fable-5-high"),
        "total_agents": len(agents),
        "active_agents": len(active),
        "roles": list(set(a.role for a in active)),
        "memory_lookback": settings.AI_MEMORY_LOOKBACK,
        "min_memory_for_local": settings.AI_MIN_MEMORY_FOR_LOCAL,
        "local_confidence_threshold": settings.AI_LOCAL_CONFIDENCE_THRESHOLD,
        "learning": learning,
    }


@router.post("/toggle")
async def toggle_ai_agents():
    """Toggle ENABLE_AI_AGENTS on/off at runtime (does not persist to .env)."""
    settings.ENABLE_AI_AGENTS = not settings.ENABLE_AI_AGENTS
    logger.info(f"[Agents] AI agents {'ENABLED' if settings.ENABLE_AI_AGENTS else 'DISABLED'}")
    return {"ai_enabled": settings.ENABLE_AI_AGENTS}


@router.post("/custom/toggle")
async def toggle_custom_agents():
    """Toggle custom rule-based agents on/off. When on, they replace AI when unavailable."""
    from app.agents.custom_agents import are_custom_agents_enabled, set_custom_agents_enabled
    new_state = not are_custom_agents_enabled()
    set_custom_agents_enabled(new_state)
    logger.info(f"[Agents] Custom agents {'ENABLED' if new_state else 'DISABLED'}")
    return {"custom_agents_enabled": new_state}


@router.get("/custom/status")
async def custom_agent_status(db: AsyncSession = Depends(get_db)):
    """Get detailed status of custom rule-based agents including learning stats."""
    from app.agents.custom_agents import get_custom_agent_status
    status = get_custom_agent_status()

    # Get learning stats per role
    roles = ["market_analyst", "sentiment_analyst", "signal_generator", "risk_manager", "trade_executor", "position_reviewer"]
    role_stats = {}
    for role in roles:
        role_stats[role] = await get_learning_stats(db, role=role)

    # Count custom agent decisions
    custom_count = (await db.execute(
        select(AgentDecision).where(
            AgentDecision.ai_called == False,
            AgentDecision.agent_name.like("Custom%"),
        ).with_only_columns(func.count(AgentDecision.id))
    )).scalar() or 0

    return {
        **status,
        "total_custom_decisions": custom_count,
        "role_learning": role_stats,
        "agents": [
            {"name": "Custom Market Analyst", "role": "market_analyst", "type": "Technical Analysis (RSI, MACD, EMA, BB, ADX)"},
            {"name": "Custom Sentiment Analyst", "role": "sentiment_analyst", "type": "DB Sentiment + CMC Community"},
            {"name": "Custom Signal Generator", "role": "signal_generator", "type": "Combined TA + Sentiment + Learning"},
            {"name": "Custom Risk Manager", "role": "risk_manager", "type": "Position Limits + Exposure + Streak Detection"},
            {"name": "Custom Trade Executor", "role": "trade_executor", "type": "Order Sizing + Spread Detection"},
            {"name": "Custom Position Reviewer", "role": "position_reviewer", "type": "Reversal Detection (RSI, MACD, EMA)"},
        ],
    }


@router.post("/custom/test")
async def test_custom_agents(
    symbol: str = Query("BTC/USDT"),
    db: AsyncSession = Depends(get_db),
):
    """Run the custom agent pipeline on a symbol for testing (no trade executed)."""
    from app.agents.custom_agents import custom_validate_trade
    context = await AgentOrchestrator._gather_context(symbol)
    signal_data = {"action": "buy", "price": context.get("current_price", 0), "confidence": 0.80}
    position_context = {"open_positions": 0, "max_positions": 3, "available_balance": 100, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
    result = await custom_validate_trade(db, symbol, signal_data, position_context, context)
    return result


# ─── Orchestration Endpoints ────────────────────────────────


@router.post("/analyze")
async def analyze_symbol_endpoint(data: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    """Run the full agent pipeline for a single symbol.
    Requires ENABLE_AI_AGENTS=true. Returns graceful error if disabled."""
    if not settings.ENABLE_AI_AGENTS:
        return {
            "error": "AI agents are disabled. Toggle them on in the Agents page.",
            "ai_enabled": False,
            "symbol": data.symbol,
        }

    try:
        result = await AgentOrchestrator.analyze_symbol(
            db=db,
            symbol=data.symbol,
            timeframe=data.timeframe,
            trigger="manual",
        )
        return result
    except Exception as e:
        logger.error(f"[Agents] Analysis failed for {data.symbol}: {e}")
        return {
            "error": f"Analysis failed: {str(e)}",
            "symbol": data.symbol,
            "ai_enabled": True,
        }


@router.post("/analyze-multiple")
async def analyze_multiple(data: AnalyzeMultipleRequest, db: AsyncSession = Depends(get_db)):
    """Run the agent pipeline for multiple symbols."""
    if not settings.ENABLE_AI_AGENTS:
        return {
            "error": "AI agents are disabled.",
            "ai_enabled": False,
        }

    try:
        results = await AgentOrchestrator.analyze_multiple(
            db=db,
            symbols=data.symbols,
            timeframe=data.timeframe,
        )
        return {"results": results}
    except Exception as e:
        logger.error(f"[Agents] Multi-analysis failed: {e}")
        return {"error": str(e)}


# ─── Decision History & Outcomes ─────────────────────────────


@router.get("/decisions")
async def list_decisions(
    limit: int = Query(50, le=200),
    symbol: Optional[str] = None,
    session_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List recent agent decisions with outcome data."""
    query = select(AgentDecision).order_by(desc(AgentDecision.created_at)).limit(limit)
    if symbol:
        query = query.where(AgentDecision.symbol == symbol)
    if session_id:
        query = query.where(AgentDecision.session_id == session_id)

    result = await db.execute(query)
    rows = result.scalars().all()

    return {
        "decisions": [
            {
                "id": d.id,
                "agent_id": d.agent_id,
                "agent_name": d.agent_name,
                "agent_role": d.agent_role,
                "symbol": d.symbol,
                "action": d.action,
                "confidence": d.confidence,
                "reasoning": d.reasoning,
                "signal_id": d.signal_id,
                "session_id": d.session_id,
                "outcome": d.outcome,
                "outcome_pnl": d.outcome_pnl,
                "ai_called": d.ai_called,
                "memory_context_used": d.memory_context_used,
                "created_at": str(d.created_at) if d.created_at else None,
            }
            for d in rows
        ]
    }


@router.get("/decisions/stats")
async def get_decision_stats(db: AsyncSession = Depends(get_db)):
    """
    Aggregate decision statistics broken down by agent_role and action.
    Returned as a summary for the Insights → AI Decisions tab.

    Uses grouped SQL aggregates instead of loading the whole AgentDecision
    table into Python.
    """
    from sqlalchemy import func, case

    total = (await db.execute(select(func.count(AgentDecision.id)))).scalar() or 0

    _act = func.lower(func.coalesce(AgentDecision.action, "hold"))

    role_rows = (await db.execute(
        select(
            AgentDecision.agent_role,
            func.count(AgentDecision.id),
            func.sum(case((AgentDecision.ai_called == True, 1), else_=0)),  # noqa: E712
            func.sum(case((AgentDecision.outcome == "win", 1), else_=0)),
        ).group_by(AgentDecision.agent_role)
    )).all()
    by_role: dict = {}
    for role, count, ai_calls, wins in role_rows:
        r = role or "unknown"
        cnt = int(count or 0)
        ai = int(ai_calls or 0)
        by_role[r] = {"count": cnt, "ai_calls": ai, "local": cnt - ai, "wins": int(wins or 0)}

    action_rows = (await db.execute(
        select(_act, func.count(AgentDecision.id)).group_by(_act)
    )).all()
    by_action: dict = {a: int(c or 0) for a, c in action_rows}

    sym_rows = (await db.execute(
        select(
            func.coalesce(AgentDecision.symbol, "unknown"),
            func.count(AgentDecision.id),
            func.sum(case((_act.in_(("buy", "long")), 1), else_=0)),
            func.sum(case((_act.in_(("sell", "short")), 1), else_=0)),
        ).group_by(func.coalesce(AgentDecision.symbol, "unknown"))
    )).all()
    by_symbol: dict = {}
    for symbol, count, buy, sell in sym_rows:
        cnt = int(count or 0)
        b = int(buy or 0)
        s = int(sell or 0)
        by_symbol[symbol] = {"count": cnt, "buy": b, "sell": s, "hold": cnt - b - s}

    # Top 10 symbols by decision count
    top_symbols = sorted(by_symbol.items(), key=lambda x: -x[1]["count"])[:10]

    return {
        "total": int(total),
        "by_role": by_role,
        "by_action": by_action,
        "top_symbols": [{"symbol": s, **v} for s, v in top_symbols],
    }


class RecordOutcomeRequest(BaseModel):
    outcome: str  # win, loss, break_even
    pnl: Optional[float] = None


@router.patch("/decisions/{decision_id}/outcome")
async def record_decision_outcome(
    decision_id: int,
    data: RecordOutcomeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record the outcome of an agent decision for learning."""
    if data.outcome not in ("win", "loss", "break_even"):
        raise HTTPException(status_code=400, detail="outcome must be: win, loss, break_even")

    result = await db.execute(select(AgentDecision).where(AgentDecision.id == decision_id))
    decision = result.scalars().first()
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")

    decision.outcome = data.outcome
    decision.outcome_pnl = data.pnl
    decision.outcome_recorded_at = now_sast()
    await db.commit()

    # ── Live vault capture: record outcome ────────────────────────────────────
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
        pnl_str = f" PnL={data.pnl:+.4f}" if data.pnl else ""
        vault_capture(
            action_type="decision-outcome",
            symbol=getattr(decision, "symbol", "") or "",
            summary=f"Outcome: {data.outcome.upper()}{pnl_str} | {getattr(decision,'agent_role','?')} → {getattr(decision,'action','?')}",
            detail=f"Decision ID {decision_id} recorded as {data.outcome}",
            tags=["outcome", data.outcome, getattr(decision, "symbol", "")],
            agent_role=getattr(decision, "agent_role", ""),
        )
    except Exception:
        pass

    return {
        "id": decision.id,
        "outcome": decision.outcome,
        "outcome_pnl": decision.outcome_pnl,
    }


@router.patch("/decisions/session/{session_id}/outcome")
async def record_session_outcome(
    session_id: str,
    data: RecordOutcomeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record outcome for ALL decisions in a session at once."""
    if data.outcome not in ("win", "loss", "break_even"):
        raise HTTPException(status_code=400, detail="outcome must be: win, loss, break_even")

    result = await db.execute(
        select(AgentDecision).where(AgentDecision.session_id == session_id)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    for d in rows:
        d.outcome = data.outcome
        d.outcome_pnl = data.pnl
        d.outcome_recorded_at = now_sast()

    await db.commit()
    return {"session_id": session_id, "updated": len(rows), "outcome": data.outcome}


@router.get("/learning-stats")
async def learning_stats(
    symbol: Optional[str] = None,
    role: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get learning statistics: accuracy, win rate, local vs API decisions."""
    stats = await get_learning_stats(db, symbol=symbol, role=role)
    return stats


@router.get("/decisions/{session_id}")
async def get_session_decisions(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get all decisions from a specific orchestration session."""
    result = await db.execute(
        select(AgentDecision)
        .where(AgentDecision.session_id == session_id)
        .order_by(AgentDecision.id)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "symbol": rows[0].symbol,
        "decisions": [
            {
                "id": d.id,
                "agent_name": d.agent_name,
                "agent_role": d.agent_role,
                "action": d.action,
                "confidence": d.confidence,
                "reasoning": d.reasoning,
                "outcome": d.outcome,
                "outcome_pnl": d.outcome_pnl,
                "ai_called": d.ai_called,
                "memory_context_used": d.memory_context_used,
                "created_at": str(d.created_at) if d.created_at else None,
            }
            for d in rows
        ],
    }


# ─── Position Monitor Endpoints ─────────────────────────────

@router.post("/position-monitor/start")
async def start_position_monitor_endpoint(interval: int = 900):
    """Start the position monitor loop. Default interval: 900s (15 minutes)."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    from app.core.scheduler import start_position_monitor
    started = start_position_monitor(interval)
    if not started:
        return {"status": "already_running"}
    return {"status": "started", "interval_seconds": interval}


@router.post("/position-monitor/stop")
async def stop_position_monitor_endpoint():
    """Stop the position monitor loop."""
    from app.core.scheduler import stop_position_monitor
    stopped = stop_position_monitor()
    if not stopped:
        return {"status": "not_running"}
    return {"status": "stopped"}


@router.get("/position-monitor/status")
async def position_monitor_status():
    """Get position monitor loop status."""
    from app.core.scheduler import get_position_monitor_status
    return get_position_monitor_status()


@router.post("/position-monitor/run")
async def run_position_monitor(
    min_hold_hours: float = 2.0,
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a position review cycle."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    result = await AgentOrchestrator.analyze_positions(db, min_hold_hours=min_hold_hours)
    return result


# ─── AI Strategy Generation & Chart Analysis ────────────────

STRATEGY_AI_PROMPT = """You are an expert crypto trading strategy designer.
You create optimal trading strategies based on market conditions, technical indicators, and risk tolerance.

You MUST respond with valid JSON in this exact format:
{
  "name": "Strategy Name",
  "description": "Brief strategy description",
  "timeframe": "1h",
  "indicators": [
    {"name": "rsi", "enabled": true, "params": {"period": 14, "overbought": 70, "oversold": 30}, "weight": 1.0},
    {"name": "macd", "enabled": true, "params": {"fast": 12, "slow": 26, "signal": 9}, "weight": 1.2}
  ],
  "buy_threshold": 0.25,
  "sell_threshold": -0.25,
  "stop_loss_pct": 2.0,
  "take_profit_pct": 4.0,
  "trade_type": "futures",
  "leverage": 5,
  "reasoning": "Explanation of why these settings work"
}

Available indicators (use these names exactly):
- rsi: RSI (params: period, overbought, oversold)
- macd: MACD (params: fast, slow, signal)
- bollinger: Bollinger Bands (params: period, mult)
- ema_cross: EMA Crossover (params: fast, slow)
- stoch_rsi: Stochastic RSI (params: period, overbought, oversold)
- adx: ADX (params: period, threshold)
- volume: Volume Surge (params: period, mult)

Strategy design principles:
1. Use 3-5 complementary indicators (avoid overlapping signals)
2. Weight indicators that suit the timeframe higher
3. Aggressive thresholds for scalping (short TFs), conservative for swing trading
4. Adjust SL/TP based on asset volatility
5. Higher leverage only for high-confidence setups with tight SL
6. Trending markets: prefer EMA cross, MACD, ADX
7. Ranging markets: prefer RSI, Bollinger Bands, Stochastic RSI
8. Risk:reward ratio must be at least 1:1.5
9. Include ALL 7 indicators in the response — set enabled=false for those not used"""

CHART_ANALYSIS_PROMPT = """You are an expert crypto chart analyst and trading advisor.
You analyze real-time chart data (OHLCV, technical indicators) and provide actionable trading insights.

You MUST respond with valid JSON:
{
  "market_structure": "trending_up" | "trending_down" | "ranging" | "breakout" | "breakdown",
  "confidence": 0.0-1.0,
  "key_levels": {
    "support": [price1, price2],
    "resistance": [price1, price2]
  },
  "recommended_action": "buy" | "sell" | "wait",
  "entry_zone": {"low": price, "high": price},
  "stop_loss": price,
  "take_profit_targets": [price1, price2, price3],
  "indicator_signals": {
    "rsi": "oversold|overbought|neutral",
    "macd": "bullish_cross|bearish_cross|neutral",
    "bollinger": "squeeze|expansion|upper_touch|lower_touch",
    "ema": "bullish|bearish|neutral",
    "volume": "increasing|decreasing|spike"
  },
  "strategy_suggestions": [
    "Specific strategy recommendation 1",
    "Specific strategy recommendation 2"
  ],
  "risk_assessment": "low|medium|high",
  "reasoning": "Detailed analysis of chart patterns and indicator confluence"
}

Focus on price action patterns, indicator confluence, volume confirmation, key S/R levels, and risk:reward quality."""


class GenerateStrategyRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    trade_type: str = "futures"
    risk_level: str = "medium"


class ImproveStrategyRequest(BaseModel):
    strategy: dict
    goals: Optional[str] = None


class AnalyzeChartRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"


async def _call_openai_json(system_prompt: str, user_message: str) -> dict:
    """Call OpenAI with JSON response format."""
    from app.agents.base import _openai_available, _get_client, _circuit_is_open, _is_quota_or_auth_error, _trip_circuit

    if not _openai_available():
        raise HTTPException(status_code=503, detail="OpenAI not configured — AI features unavailable")

    if _circuit_is_open():
        raise HTTPException(status_code=503, detail="OpenAI quota exhausted — AI temporarily unavailable. Retrying automatically.")

    client = _get_client()
    model = os.getenv("OPENAI_MODEL", "o3")
    is_reasoning = model.startswith(("o1", "o3", "o4"))
    extra: dict = {}
    if is_reasoning:
        extra["max_completion_tokens"] = 4000
    else:
        extra["max_tokens"] = 4000
        extra["temperature"] = 0.3

    try:
        resp = await client.chat.completions.create(
            model=model,
            **extra,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except HTTPException:
        raise
    except Exception as e:
        if _is_quota_or_auth_error(e):
            _trip_circuit(str(e)[:200])
            raise HTTPException(status_code=503, detail="OpenAI quota exhausted — AI temporarily unavailable")
        raise


@router.post("/generate-strategy")
async def generate_strategy_endpoint(data: GenerateStrategyRequest):
    """Use AI to generate a new trading strategy."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        user_msg = json.dumps({
            "task": "generate_new_strategy",
            "target_symbol": data.symbol,
            "timeframe": data.timeframe,
            "trade_type": data.trade_type,
            "risk_level": data.risk_level,
        })
        result = await _call_openai_json(STRATEGY_AI_PROMPT, user_msg)
        return {"strategy": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Strategy] Generation failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI generation failed: {str(e)[:200]}")


@router.post("/improve-strategy")
async def improve_strategy_endpoint(data: ImproveStrategyRequest):
    """Use AI to improve an existing trading strategy."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        user_msg = json.dumps({
            "task": "improve_existing_strategy",
            "current_strategy": data.strategy,
            "improvement_goals": data.goals or "Optimize for better risk-adjusted returns",
        }, default=str)
        prompt = STRATEGY_AI_PROMPT + (
            "\n\nYou are improving an existing strategy. Analyze the current config, "
            "identify weaknesses, and return an improved version. Keep what works well "
            "and fix what doesn't. Explain your changes in the reasoning field."
        )
        result = await _call_openai_json(prompt, user_msg)
        return {"strategy": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Strategy] Improvement failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI improvement failed: {str(e)[:200]}")


@router.post("/analyze-chart")
async def analyze_chart_endpoint(data: AnalyzeChartRequest):
    """Use AI to analyze chart data and provide trading insights."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        context = await AgentOrchestrator._gather_context(data.symbol, data.timeframe)
        user_msg = json.dumps({
            "symbol": data.symbol,
            "timeframe": data.timeframe,
            "market_data": context,
        }, default=str)
        result = await _call_openai_json(CHART_ANALYSIS_PROMPT, user_msg)
        result["symbol"] = data.symbol
        result["timeframe"] = data.timeframe
        return {"analysis": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Chart] Analysis failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI analysis failed: {str(e)[:200]}")
