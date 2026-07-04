"""
Agent Paul Plugin — API Router

All routes prefixed at /plugins/agent-paul by the plugin loader.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

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


# ── MT5 account helper (used by agent-paul.tsx decide console) ─────────────


@router.get("/mt5-accounts")
async def list_mt5_accounts():
    """List configured MT5 accounts for the decision console."""
    try:
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client  # type: ignore
        if mt5_client is None:
            return []
        return await mt5_client.get_accounts()
    except ImportError:
        return []
    except Exception:
        return []


# ── JARVIS Chat (SSE streaming) ────────────────────────────────────────────


class JarvisChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    pathname: Optional[str] = "/"
    session_key: Optional[str] = None


@router.post("/chat")
async def jarvis_chat(payload: JarvisChatRequest, db: AsyncSession = Depends(get_db)):
    """Stream a JARVIS response as Server-Sent Events.

    Each event is: ``data: {"delta": "..."}\n\n``
    Terminated by: ``data: {"done": true}\n\n``
    """
    from plugins.AgentPaulPlugin.backend.services.paul_chat import stream_jarvis_chat  # noqa

    session_key = payload.session_key or "default"

    async def event_generator():
        async for chunk in stream_jarvis_chat(
            db, payload.messages, payload.pathname or "/", session_key
        ):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── MT5 Position Monitor ───────────────────────────────────────────────────


@router.get("/mt5-monitor/status")
async def mt5_monitor_status():
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import get_monitor_status  # noqa
    return get_monitor_status()


@router.post("/mt5-monitor/start")
async def mt5_monitor_start():
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import start_monitor  # noqa
    started = start_monitor()
    return {"started": started}


@router.post("/mt5-monitor/stop")
async def mt5_monitor_stop():
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import stop_monitor  # noqa
    stopped = stop_monitor()
    return {"stopped": stopped}


# ── JARVIS Alerts ──────────────────────────────────────────────────────────


@router.get("/alerts")
async def get_alerts(unread_only: bool = Query(False)):
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import get_alerts as _get  # noqa
    return {"alerts": _get(unread_only=unread_only)}


@router.patch("/alerts/{alert_id}/read")
async def read_alert(alert_id: str):
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import mark_alert_read  # noqa
    found = mark_alert_read(alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}


@router.delete("/alerts")
async def clear_alerts():
    from plugins.AgentPaulPlugin.backend.services.mt5_monitor import clear_all_alerts  # noqa
    n = clear_all_alerts()
    return {"cleared": n}


# ── JARVIS memory, news, research & prediction ─────────────────────────────


@router.get("/chat/history")
async def chat_history(
    session_key: str = Query("default"),
    db: AsyncSession = Depends(get_db),
):
    """Return the persisted message history for the active conversation.

    Returns the most recent messages (kept intact until the user starts a new
    chat). The cap is generous so reloading never appears to drop the
    conversation.
    """
    from plugins.AgentPaulPlugin.backend.services import knowledge_base  # noqa
    history = await knowledge_base.get_history(db, session_key, limit=500)
    return {"session_key": session_key, "messages": history}


@router.post("/chat/new")
async def chat_new(
    session_key: str = Query("default"),
    db: AsyncSession = Depends(get_db),
):
    """Archive the active conversation and start a fresh one."""
    from plugins.AgentPaulPlugin.backend.services import knowledge_base  # noqa
    conv = await knowledge_base.start_new_conversation(db, session_key)
    return {"ok": True, "conversation_id": conv.id if conv else None}


@router.get("/news")
async def get_news(
    topic: Optional[str] = Query(None, description="crypto|stocks|macro"),
    symbol: Optional[str] = Query(None),
):
    """Aggregated live RSS news (cached 5 min)."""
    from plugins.AgentPaulPlugin.backend.services import news_research  # noqa
    if symbol:
        items = await news_research.news_for_symbol(symbol, limit=20)
    else:
        items = await news_research.fetch_news(topic=topic)
    return {"count": len(items), "items": items[:30]}


@router.post("/news/ingest")
async def ingest_news(db: AsyncSession = Depends(get_db)):
    """Pull fresh RSS news into JARVIS long-term knowledge."""
    from plugins.AgentPaulPlugin.backend.services.market_predictor import ingest_news_to_knowledge  # noqa
    stored = await ingest_news_to_knowledge(db)
    return {"ok": True, "stored": stored}


@router.get("/predict")
async def predict(
    pair: str = Query(..., description="e.g. BTCUSDT, XAUUSD, EURUSD, AAPL"),
    db: AsyncSession = Depends(get_db),
):
    """Forecast direction for any stock or crypto pair."""
    from plugins.AgentPaulPlugin.backend.services.market_predictor import predict_pair  # noqa
    return await predict_pair(db, pair)


@router.get("/knowledge/search")
async def knowledge_search(
    q: str = Query(""),
    symbol: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Search JARVIS long-term learned knowledge."""
    from plugins.AgentPaulPlugin.backend.services import knowledge_base  # noqa
    items = await knowledge_base.search_knowledge(db, q, limit=20, symbol=symbol)
    return {"count": len(items), "items": items}


@router.get("/knowledge/stats")
async def knowledge_stats(db: AsyncSession = Depends(get_db)):
    """Knowledge counts for the brain-map intelligence panel."""
    from plugins.AgentPaulPlugin.backend.services import knowledge_base  # noqa
    return await knowledge_base.knowledge_stats(db)


@router.post("/research")
async def research(
    url: str = Query(..., description="Public http(s) URL to research"),
):
    """Fetch a public URL and return cleaned text for online research."""
    from plugins.AgentPaulPlugin.backend.services import news_research  # noqa
    return await news_research.research_url(url)


# ── JARVIS Skills ───────────────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import list_skills as _list, seed_defaults  # noqa
    await seed_defaults(db)
    return {"skills": await _list(db)}


@router.post("/skills")
async def create_skill(payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import create_skill as _create  # noqa
    return await _create(db, payload)


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: int, payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import update_skill as _update  # noqa
    try:
        return await _update(db, skill_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import delete_skill as _del  # noqa
    await _del(db, skill_id)
    return {"ok": True}


# ── JARVIS Hooks ────────────────────────────────────────────────────────────


@router.get("/hooks")
async def list_hooks(db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import list_hooks as _list, seed_defaults  # noqa
    await seed_defaults(db)
    return {"hooks": await _list(db)}


@router.post("/hooks")
async def create_hook(payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import create_hook as _create  # noqa
    return await _create(db, payload)


@router.put("/hooks/{hook_id}")
async def update_hook(hook_id: int, payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import update_hook as _update  # noqa
    try:
        return await _update(db, hook_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/hooks/{hook_id}")
async def delete_hook(hook_id: int, db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services.skills_hooks import delete_hook as _del  # noqa
    await _del(db, hook_id)
    return {"ok": True}


# ── JARVIS Voice Trade Execution ─────────────────────────────────────────────


class VoiceTradeRequest(BaseModel):
    command: str


@router.post("/voice-trade")
async def voice_trade(payload: VoiceTradeRequest, db: AsyncSession = Depends(get_db)):
    """Parse a natural-language trade command and execute the best matching signal."""
    from plugins.AgentPaulPlugin.backend.services.voice_trade import execute_voice_trade_command  # noqa
    result = await execute_voice_trade_command(db, payload.command)
    return {"ok": True, "response": result}


# ── JARVIS Natural-Language Intent Parser (hands-free AI fallback) ────────────


class IntentRequest(BaseModel):
    text: str
    pathname: Optional[str] = "/"


@router.post("/intent")
async def parse_intent_route(payload: IntentRequest, db: AsyncSession = Depends(get_db)):
    """Map a natural-language command to a structured hands-free action."""
    from plugins.AgentPaulPlugin.backend.services.intent_parser import parse_intent  # noqa
    try:
        return await parse_intent(db, payload.text, payload.pathname or "/")
    except Exception:
        return {"type": "none"}


# ── JARVIS Self-Improvement ────────────────────────────────────────────────────

class JarvisImproveRequest(BaseModel):
    """Request body for Jarvis to store a self-improvement."""
    type: str = "knowledge"       # knowledge | agent_update | rule | pattern
    content: str                  # the learning / insight / rule to store
    symbol: Optional[str] = None  # trading symbol if relevant
    agent_role: Optional[str] = None  # which agent to update (for agent_update type)
    importance: float = 0.7       # 0-1 importance weight
    topic: Optional[str] = "general"


@router.post("/jarvis-improve", summary="Jarvis stores a self-improvement")
async def jarvis_improve(payload: JarvisImproveRequest, db: AsyncSession = Depends(get_db)):
    """
    Called by JARVIS to permanently store a learning, pattern, or rule.

    Writes to:
    1. The Paul knowledge base (immediately queryable by future conversations)
    2. The Obsidian vault (persistent across restarts, visible in brain map)

    This is the core self-improvement loop — every time Jarvis learns something
    valuable, it calls this endpoint so it never forgets.
    """
    from plugins.AgentPaulPlugin.backend.services import knowledge_base as _kb

    stored_kb = False
    stored_vault = False

    # 1. Store in Paul knowledge base
    try:
        content = payload.content.strip()
        if payload.symbol:
            content = f"[{payload.symbol}] {content}"
        if payload.agent_role:
            content = f"[agent:{payload.agent_role}] {content}"

        await _kb.record_knowledge(
            db,
            kind="insight" if payload.type == "knowledge" else payload.type,
            content=content,
            source="jarvis_self_improvement",
            topic=payload.topic or "general",
            importance=min(1.0, max(0.0, payload.importance)),
        )
        stored_kb = True
    except Exception as _kbe:
        pass

    # 2. Store in Obsidian vault (persistent memory)
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
        vault_capture(
            action_type="jarvis-improvement",
            symbol=payload.symbol or "",
            summary=f"[{payload.type}] {payload.content[:150]}",
            detail=payload.content,
            tags=["self-improvement", payload.type, payload.topic or "general"],
            agent_role=payload.agent_role or "",
        )
        stored_vault = True
    except Exception:
        pass

    # 3. If agent_update, also update the agent's configuration in the DB
    if payload.type == "agent_update" and payload.agent_role:
        try:
            from app.models.database import Agent
            from sqlalchemy import select as _sel
            _agent = (await db.execute(
                _sel(Agent).where(Agent.role == payload.agent_role)
            )).scalars().first()
            if _agent:
                # Append the improvement to the agent's notes/memory field
                existing = _agent.notes or ""
                _agent.notes = (existing + f"\n[JARVIS IMPROVEMENT] {payload.content}").strip()[-2000:]
                await db.commit()
        except Exception:
            pass

    return {
        "stored_to_knowledge_base": stored_kb,
        "stored_to_vault": stored_vault,
        "type": payload.type,
        "message": "Improvement stored. I will use this in future conversations.",
    }


# ── Memory Tree + Goals/Todos + Idle (OpenHuman-inspired) ──────────────────

def _ensure_background() -> None:
    """Lazily start background loops (app uses lifespan → no startup event)."""
    try:
        from plugins.AgentPaulPlugin.backend.services.auto_fetch import auto_fetch
        auto_fetch.ensure_started(AsyncSessionLocal)
    except Exception:
        pass
    try:
        from plugins.AgentPaulPlugin.backend.services.subconscious import subconscious
        subconscious.ensure_started(AsyncSessionLocal)
    except Exception:
        pass


@router.get("/jarvis/memory/stats")
async def memory_stats(db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import memory_tree
    _ensure_background()
    return await memory_tree.stats(db)


@router.get("/jarvis/memory/new")
async def memory_new(
    since_minutes: int = Query(20, ge=1, le=1440),
    min_importance: float = Query(0.65, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
):
    """New high-importance memories — polled by the extension to surface alerts."""
    from plugins.AgentPaulPlugin.backend.services import memory_tree
    _ensure_background()
    items = await memory_tree.recent_high_importance(
        db, since_minutes=since_minutes, min_importance=min_importance
    )
    return {"items": items, "count": len(items)}


@router.post("/jarvis/memory/rollup")
async def memory_rollup(db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import memory_tree
    node = await memory_tree.rollup(db)
    return {"ok": node is not None, "day": node.day if node else None}


@router.get("/jarvis/auto-fetch/status")
async def auto_fetch_status():
    from plugins.AgentPaulPlugin.backend.services.auto_fetch import auto_fetch
    auto_fetch.ensure_started(AsyncSessionLocal)
    return auto_fetch.status()


@router.post("/jarvis/auto-fetch/run")
async def auto_fetch_run():
    from plugins.AgentPaulPlugin.backend.services.auto_fetch import auto_fetch
    return await auto_fetch.run_once()


# ── Goals ──────────────────────────────────────────────────

@router.get("/jarvis/goals")
async def list_goals(
    session_key: Optional[str] = Query(None),
    scope: Optional[str] = Query(None, description="account | thread"),
    include_done: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    _ensure_background()
    return {"goals": await goals_todos.list_goals(
        db, session_key=session_key, scope=scope, include_done=include_done)}


@router.post("/jarvis/goals/reflect", summary="Reflect agent: review long-term goals vs memory")
async def reflect_goals(db: AsyncSession = Depends(get_db)):
    """OpenHuman goals_agent — reviews account-level goals against recent memory,
    makes minimal justified changes, and bootstraps a starter set if empty."""
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    return await goals_todos.reflect(db)


@router.post("/jarvis/goals")
async def create_goal(payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    return await goals_todos.create_goal(db, payload)


@router.patch("/jarvis/goals/{goal_id}")
async def update_goal(goal_id: int, payload: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    try:
        return await goals_todos.update_goal(db, goal_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/jarvis/goals/{goal_id}")
async def delete_goal(goal_id: int, db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    await goals_todos.delete_goal(db, goal_id)
    return {"ok": True}


# ── Todos ──────────────────────────────────────────────────

@router.get("/jarvis/todos")
async def list_todos(
    session_key: Optional[str] = Query(None),
    goal_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    return {"todos": await goals_todos.list_todos(db, session_key, goal_id)}


@router.post("/jarvis/todos")
async def create_todo(payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    return await goals_todos.create_todo(db, payload)


@router.patch("/jarvis/todos/{todo_id}")
async def update_todo(todo_id: int, payload: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    try:
        return await goals_todos.update_todo(db, todo_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/jarvis/todos/{todo_id}")
async def delete_todo(todo_id: int, db: AsyncSession = Depends(get_db)):
    from plugins.AgentPaulPlugin.backend.services import goals_todos
    await goals_todos.delete_todo(db, todo_id)
    return {"ok": True}


# ── Idle continuation (read-only) ──────────────────────────

@router.get("/jarvis/idle-status")
async def idle_status():
    from plugins.AgentPaulPlugin.backend.services import idle_worker
    return idle_worker.current_status()


@router.post("/jarvis/idle-run")
async def idle_run(payload: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_db)):
    """Trigger ONE read-only idle research step for the session's active goal."""
    from plugins.AgentPaulPlugin.backend.services import idle_worker
    session_key = (payload or {}).get("session_key") or "default"
    return await idle_worker.run_idle_step(db, session_key)


# ── Subconscious heartbeat (OpenHuman-style) ───────────────

@router.get("/jarvis/subconscious/status")
async def subconscious_status():
    from plugins.AgentPaulPlugin.backend.services.subconscious import subconscious
    subconscious.ensure_started(AsyncSessionLocal)
    return subconscious.status()


@router.post("/jarvis/subconscious/tick", summary="Run one heartbeat tick now")
async def subconscious_tick():
    from plugins.AgentPaulPlugin.backend.services.subconscious import subconscious
    return await subconscious.run_tick()


@router.get("/jarvis/activity", summary="Subconscious activity feed")
async def subconscious_activity(
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    from plugins.AgentPaulPlugin.backend.services import subconscious as sc
    _ensure_background()
    items = await sc.list_activity(db, limit=limit)
    return {"items": items, "count": len(items)}

