"""
AI Market Analyst Plugin — API Router

All routes prefixed at /plugins/ai-analyst by the plugin loader.
"""
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.AiMarketAnalyst.backend.models import (
    AIAgent, AITradeSettings, AITradeDecision, AIBase, DecisionStatus,
    AILLMProvider,
)
from plugins.AiMarketAnalyst.backend.schemas import (
    AIAgentCreate, AIAgentUpdate, AIAgentResponse,
    AITradeSettingsUpdate, AITradeSettingsResponse,
    AIAnalyzeRequest, AIProposeLimitRequest, AIPlaceLimitRequest,
    AIDecisionResponse, AIOverlayResponse,
    LLMProviderResponse, LLMUsageResponse,
    LLMProviderPreset, AIProviderCreate, AIProviderUpdate, AIProviderResponse,
    AIProviderTestResponse, AIChatRequest,
    AIProviderTestAllResponse, AIProviderTestAllResult,
)
from plugins.AiMarketAnalyst.backend.services.agent_runtime import run_analysis, place_decision
from plugins.AiMarketAnalyst.backend.services.llm_gateway import get_gateway_status
from plugins.AiMarketAnalyst.backend.services.llm_registry import get_enabled_providers
from plugins.AiMarketAnalyst.backend.services.llm_usage import get_usage_snapshot
from plugins.AiMarketAnalyst.backend.services.overlay_service import build_overlay
from plugins.AiMarketAnalyst.backend.services.provider_presets import PROVIDER_PRESETS, get_preset, get_model_info
from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat, test_provider, get_router_settings
from plugins.AiMarketAnalyst.backend.services import usage_service, knowledge_service, graphify_service
from pydantic import BaseModel

router = APIRouter(prefix="/plugins/ai-analyst", tags=["AI Analyst"])


def _normalize_user_id(user_id: str) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return 0


# ── DB dependency ──────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Multi-provider AI accounts ─────────────────────────────

def _provider_to_response(p: AILLMProvider) -> AIProviderResponse:
    models = p.models_json or []
    model_info = {m: info for m in models if (info := get_model_info(m))}
    return AIProviderResponse(
        id=p.id,
        provider_key=p.provider_key,
        label=p.label,
        type=p.type,
        api_key_set=bool(p.api_key),
        base_url=p.base_url,
        default_model=p.default_model,
        models=models,
        model_info=model_info,
        enabled=p.enabled,
        priority=p.priority,
        free_tier=p.free_tier,
        status=p.status,
        last_error=p.last_error,
        last_tested_at=p.last_tested_at,
        last_model_used=p.last_model_used,
        total_calls=p.total_calls,
        total_errors=p.total_errors,
        daily_limit=p.daily_limit,
        monthly_limit=p.monthly_limit,
        daily_calls=p.daily_calls or 0,
        monthly_calls=p.monthly_calls or 0,
        daily_reset_at=p.daily_reset_at,
        monthly_reset_at=p.monthly_reset_at,
    )


@router.get("/ai/providers/presets", response_model=list[LLMProviderPreset])
async def list_provider_presets():
    return [LLMProviderPreset(**p) for p in PROVIDER_PRESETS]


@router.get("/ai/providers", response_model=list[AIProviderResponse])
async def list_providers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(AILLMProvider).order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    return [_provider_to_response(p) for p in res.scalars().all()]


@router.post("/ai/providers", response_model=AIProviderResponse)
async def add_provider(payload: AIProviderCreate, db: AsyncSession = Depends(get_db)):
    preset = get_preset(payload.provider_key)
    # Determine next priority (append to end)
    max_pri = await db.scalar(select(func.max(AILLMProvider.priority)))
    next_pri = payload.priority if payload.priority is not None else (int(max_pri or 0) + 10)

    provider = AILLMProvider(
        provider_key=payload.provider_key,
        label=payload.label or (preset["label"] if preset else payload.provider_key),
        type=payload.type or (preset["type"] if preset else "openai_compatible"),
        api_key=payload.api_key.strip() or None,
        base_url=payload.base_url or (preset["base_url"] if preset else None),
        default_model=payload.default_model or (preset["default_model"] if preset else None),
        models_json=(preset["models"] if preset else None),
        enabled=payload.enabled,
        priority=next_pri,
        free_tier=payload.free_tier if payload.free_tier is not None else (preset["free_tier"] if preset else True),
        daily_limit=payload.daily_limit if payload.daily_limit is not None else (preset.get("daily_limit") if preset else None),
        monthly_limit=payload.monthly_limit if payload.monthly_limit is not None else (preset.get("monthly_limit") if preset else None),
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)

    # Auto-test on creation
    result = await test_provider(provider)
    provider.status = "ok" if result.get("ok") else "error"
    provider.last_error = None if result.get("ok") else result.get("error")
    provider.last_tested_at = datetime.utcnow()
    await db.commit()
    await db.refresh(provider)
    return _provider_to_response(provider)


@router.put("/ai/providers/{provider_id}", response_model=AIProviderResponse)
async def update_provider(provider_id: int, payload: AIProviderUpdate, db: AsyncSession = Depends(get_db)):
    provider = await db.get(AILLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data and data["api_key"]:
        provider.api_key = data["api_key"].strip()
    for attr in ("label", "base_url", "default_model", "enabled", "priority", "daily_limit", "monthly_limit"):
        if attr in data and data[attr] is not None:
            setattr(provider, attr, data[attr])
    await db.commit()
    await db.refresh(provider)
    return _provider_to_response(provider)


@router.delete("/ai/providers/{provider_id}")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    provider = await db.get(AILLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.delete(provider)
    await db.commit()
    return {"deleted": True}


@router.post("/ai/providers/{provider_id}/test", response_model=AIProviderTestResponse)
async def test_provider_endpoint(provider_id: int, db: AsyncSession = Depends(get_db)):
    provider = await db.get(AILLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    result = await test_provider(provider)
    provider.status = "ok" if result.get("ok") else "error"
    provider.last_error = None if result.get("ok") else result.get("error")
    provider.last_tested_at = datetime.utcnow()
    if result.get("model"):
        provider.last_model_used = result["model"]
    await db.commit()
    return AIProviderTestResponse(**result)


@router.post("/ai/providers/test-all", response_model=AIProviderTestAllResponse)
async def test_all_providers(db: AsyncSession = Depends(get_db)):
    """Test every configured provider's API key in one call."""
    res = await db.execute(
        select(AILLMProvider).order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    providers = list(res.scalars().all())
    results: list[AIProviderTestAllResult] = []
    ok_count = 0
    for provider in providers:
        r = await test_provider(provider)
        ok = bool(r.get("ok"))
        if ok:
            ok_count += 1
        provider.status = "ok" if ok else "error"
        provider.last_error = None if ok else r.get("error")
        provider.last_tested_at = datetime.utcnow()
        if r.get("model"):
            provider.last_model_used = r["model"]
        results.append(AIProviderTestAllResult(
            id=provider.id,
            label=provider.label,
            ok=ok,
            model=r.get("model"),
            error=r.get("error"),
        ))
    await db.commit()
    return AIProviderTestAllResponse(tested=len(providers), ok_count=ok_count, results=results)


@router.get("/ai/usage")
async def ai_usage(db: AsyncSession = Depends(get_db)):
    """Per-provider + overall usage with monthly remaining (calls + tokens).

    Powers the provider tab on /telegram-signals and the providers panel on
    /agents.
    """
    return await usage_service.provider_usage(db)


@router.get("/ai/usage/agents")
async def ai_usage_agents(db: AsyncSession = Depends(get_db)):
    """Per-agent token + call usage for the current month (for /agents)."""
    return await usage_service.agent_usage(db)


@router.get("/ai/headroom")
async def ai_headroom(days: int = 30, db: AsyncSession = Depends(get_db)):
    """Headroom compression savings for the Intelligence page."""
    return await usage_service.headroom_stats(db, days=days)


# ── Router settings (load balancing + token controls) ──────

class RouterSettingsUpdate(BaseModel):
    strategy: Optional[str] = None  # priority | round_robin | least_used
    agents_use_providers: Optional[bool] = None
    agent_token_mode: Optional[str] = None  # telegram_only | always
    per_agent_max_tokens: Optional[int] = None
    reserve_pct: Optional[float] = None
    headroom_enabled: Optional[bool] = None
    graphify_enabled: Optional[bool] = None


def _router_settings_to_dict(s) -> dict:
    return {
        "strategy": s.strategy,
        "agents_use_providers": s.agents_use_providers,
        "agent_token_mode": getattr(s, "agent_token_mode", "telegram_only"),
        "per_agent_max_tokens": s.per_agent_max_tokens,
        "reserve_pct": s.reserve_pct,
        "headroom_enabled": s.headroom_enabled,
        "graphify_enabled": s.graphify_enabled,
    }


@router.get("/ai/router-settings")
async def get_router_settings_endpoint(db: AsyncSession = Depends(get_db)):
    s = await get_router_settings(db)
    return _router_settings_to_dict(s)


@router.put("/ai/router-settings")
async def update_router_settings(payload: RouterSettingsUpdate, db: AsyncSession = Depends(get_db)):
    s = await get_router_settings(db)
    data = payload.model_dump(exclude_unset=True)
    if "strategy" in data and data["strategy"] not in {"priority", "round_robin", "least_used"}:
        raise HTTPException(status_code=400, detail="Invalid strategy")
    if "agent_token_mode" in data and data["agent_token_mode"] not in {"telegram_only", "always"}:
        raise HTTPException(status_code=400, detail="Invalid token mode")
    for attr in ("strategy", "agents_use_providers", "agent_token_mode", "per_agent_max_tokens",
                 "reserve_pct", "headroom_enabled", "graphify_enabled"):
        if attr in data and data[attr] is not None:
            setattr(s, attr, data[attr])
    await db.commit()
    await db.refresh(s)
    return _router_settings_to_dict(s)


# ── Agent knowledge store ──────────────────────────────────

class KnowledgeCreate(BaseModel):
    content: str
    agent_role: Optional[str] = None
    symbol: Optional[str] = None
    kind: str = "insight"
    title: Optional[str] = None
    weight: float = 1.0
    source: Optional[str] = None


@router.get("/ai/knowledge")
async def list_knowledge_endpoint(db: AsyncSession = Depends(get_db)):
    return {"items": await knowledge_service.list_knowledge(db)}


@router.post("/ai/knowledge")
async def add_knowledge_endpoint(payload: KnowledgeCreate, db: AsyncSession = Depends(get_db)):
    row = await knowledge_service.store_knowledge(
        db,
        content=payload.content,
        agent_role=payload.agent_role,
        symbol=payload.symbol,
        kind=payload.kind,
        title=payload.title,
        weight=payload.weight,
        source=payload.source or "manual",
    )
    return {"id": row.id, "stored": True}


@router.delete("/ai/knowledge/{knowledge_id}")
async def delete_knowledge_endpoint(knowledge_id: int, db: AsyncSession = Depends(get_db)):
    from plugins.AiMarketAnalyst.backend.models import AIAgentKnowledge
    row = await db.get(AIAgentKnowledge, knowledge_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


# ── Graphify (code/knowledge map) ──────────────────────────

@router.get("/ai/graph/overview")
async def graph_overview_endpoint():
    return graphify_service.graph_overview()


@router.get("/ai/graph/query")
async def graph_query_endpoint(term: str = Query(...), limit: int = 8):
    return graphify_service.query_map(term, limit=limit)


@router.post("/ai/chat")
async def ai_chat(payload: AIChatRequest, db: AsyncSession = Depends(get_db)):
    messages = []
    if payload.system:
        messages.append({"role": "system", "content": payload.system})
    messages.append({"role": "user", "content": payload.prompt})
    result = await db_chat(
        db, messages, json_mode=payload.json_mode, max_tokens=payload.max_tokens
    )
    return result


# ── Status ─────────────────────────────────────────────────

@router.get("/status")
async def plugin_status():
    providers = get_enabled_providers()
    usage = await get_usage_snapshot(providers)
    return {
        "plugin": "ai-analyst",
        "version": "1.0.0",
        "llm": {
            "providers": get_gateway_status(providers),
            "usage": usage["total"],
        },
    }


@router.get("/llm/providers", response_model=List[LLMProviderResponse])
async def list_llm_providers():
    providers = get_enabled_providers()
    status_map = {p["id"]: p for p in get_gateway_status(providers)}
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "type": provider.type,
            "enabled": provider.enabled,
            "models": provider.models,
            "rate_limits": provider.rate_limits,
            "circuit": status_map.get(provider.id, {}).get("circuit", {}),
        }
        for provider in providers
    ]


@router.get("/llm/usage", response_model=LLMUsageResponse)
async def get_llm_usage():
    providers = get_enabled_providers()
    return await get_usage_snapshot(providers)


# ── Agent Admin ────────────────────────────────────────────

@router.get("/agents", response_model=List[AIAgentResponse])
async def list_agents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AIAgent).order_by(AIAgent.name))
    return result.scalars().all()


@router.post("/agents", response_model=AIAgentResponse)
async def create_agent(body: AIAgentCreate, db: AsyncSession = Depends(get_db)):
    agent = AIAgent(**body.model_dump())
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("/agents/{agent_id}", response_model=AIAgentResponse)
async def get_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@router.patch("/agents/{agent_id}", response_model=AIAgentResponse)
async def update_agent(agent_id: int, body: AIAgentUpdate, db: AsyncSession = Depends(get_db)):
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(agent, k, v)
    agent.version += 1
    await db.commit()
    await db.refresh(agent)
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    await db.delete(agent)
    await db.commit()
    return {"deleted": True}


@router.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    agent.is_enabled = not agent.is_enabled
    await db.commit()
    return {"id": agent.id, "is_enabled": agent.is_enabled}


# ── Trade Settings ─────────────────────────────────────────

@router.get("/settings", response_model=AITradeSettingsResponse)
async def get_settings(user_id: str = "default", db: AsyncSession = Depends(get_db)):
    normalized_user_id = _normalize_user_id(user_id)
    result = await db.execute(
        select(AITradeSettings).where(AITradeSettings.user_id == normalized_user_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AITradeSettings(user_id=normalized_user_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.patch("/settings", response_model=AITradeSettingsResponse)
async def update_settings(
    body: AITradeSettingsUpdate,
    user_id: str = "default",
    db: AsyncSession = Depends(get_db),
):
    normalized_user_id = _normalize_user_id(user_id)
    result = await db.execute(
        select(AITradeSettings).where(AITradeSettings.user_id == normalized_user_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AITradeSettings(user_id=normalized_user_id)
        db.add(settings)
        await db.flush()
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(settings, k, v)
    await db.commit()
    await db.refresh(settings)
    return settings


# ── Analysis / Propose / Place ─────────────────────────────

@router.post("/analyze")
async def analyze(body: AIAnalyzeRequest, db: AsyncSession = Depends(get_db)):
    result = await run_analysis(
        db=db,
        symbol=body.symbol,
        timeframe=body.timeframe,
        action="analyze",
    )
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@router.post("/propose-limit")
async def propose_limit(body: AIProposeLimitRequest, db: AsyncSession = Depends(get_db)):
    result = await run_analysis(
        db=db,
        symbol=body.symbol,
        timeframe=body.timeframe,
        action="propose_limit",
    )
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@router.post("/place-limit")
async def place_limit(body: AIPlaceLimitRequest, db: AsyncSession = Depends(get_db)):
    result = await place_decision(db=db, decision_id=body.decision_id)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


# ── Decision History ───────────────────────────────────────

@router.get("/decisions", response_model=List[AIDecisionResponse])
async def list_decisions(
    symbol: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = select(AITradeDecision).order_by(desc(AITradeDecision.created_at))
    if symbol:
        q = q.where(AITradeDecision.symbol == symbol)
    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/decisions/{decision_id}", response_model=AIDecisionResponse)
async def get_decision(decision_id: int, db: AsyncSession = Depends(get_db)):
    d = await db.get(AITradeDecision, decision_id)
    if not d:
        raise HTTPException(404, "Decision not found")
    return d


# ── Overlay ────────────────────────────────────────────────

@router.get("/overlay/{symbol}", response_model=AIOverlayResponse)
async def get_overlay(symbol: str, db: AsyncSession = Depends(get_db)):
    """Get the latest proposed/placed decision's overlay for a symbol."""
    result = await db.execute(
        select(AITradeDecision)
        .where(AITradeDecision.symbol == symbol)
        .where(AITradeDecision.status.in_([
            DecisionStatus.DRAFTED,
            DecisionStatus.SENT_TO_MT5,
            DecisionStatus.MT5_ACCEPTED,
        ]))
        .order_by(desc(AITradeDecision.created_at))
        .limit(1)
    )
    d = result.scalar_one_or_none()
    if not d:
        return AIOverlayResponse()
    return build_overlay(
        direction=d.direction.value if d.direction else "none",
        entry_price=d.entry_price,
        sl_price=d.sl_price,
        tp_price=d.tp_price,
        confidence=d.confidence,
        status=d.status.value if d.status else "drafted",
    )
