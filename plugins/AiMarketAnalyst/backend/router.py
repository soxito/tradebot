"""
AI Market Analyst Plugin — API Router

All routes prefixed at /plugins/ai-analyst by the plugin loader.
"""
from typing import Any, List, Optional
import time
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
from plugins.AiMarketAnalyst.backend.services import ai_router, usage_service, knowledge_service, graphify_service
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

def mask_api_key(key: Optional[str]) -> Optional[str]:
    """First five and last four characters, so a key is recognisable but not usable.

    Short keys are masked entirely rather than partially: revealing nine
    characters of a twelve-character secret gives away most of it, and no real
    provider issues keys that short anyway.
    """
    if not key:
        return None
    key = key.strip()
    if len(key) < 16:
        return "•" * 8
    return f"{key[:5]}…{key[-4:]}"


async def find_duplicate_key(
    db: AsyncSession, api_key: str, exclude_id: Optional[int] = None
) -> Optional[AILLMProvider]:
    """The provider already holding this key, if any.

    The same key configured twice does not buy extra capacity — it is one
    upstream quota being drawn down from two rows, so the router's load
    balancing spreads calls across what it believes are two independent keys and
    hits the rate limit twice as fast.
    """
    needle = (api_key or "").strip()
    if not needle:
        return None

    rows = (await db.execute(select(AILLMProvider))).scalars().all()
    for row in rows:
        if exclude_id is not None and row.id == exclude_id:
            continue
        if (row.api_key or "").strip() == needle:
            return row
    return None


def _duplicate_key_error(existing: AILLMProvider) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"That key is already connected as “{existing.label}” "
            f"(id {existing.id}). Using one key twice draws down a single quota "
            f"from two rows and rate-limits twice as fast — add a different key, "
            f"or edit the existing one."
        ),
    )


async def _resync_shared_pool_models(db: AsyncSession) -> None:
    """Give every model exactly one home.

    A model reachable from two profiles is a back door: a call naming it lands
    on whichever profile the router happens to pick, spending a quota the
    dedication was meant to reserve. So models are claimed in priority order and
    never re-offered:

    1. Tasks with a model chain claim their chain — the strongest statement of
       intent, and the models are named explicitly.
    2. Tasks without one (the chat surfaces) claim what is left of their own
       provider's catalogue. They pin no models by design, so they must yield to
       the chains rather than swallow them.
    3. The shared pool gets the remainder.

    A profile stripped to nothing keeps its default model: a row offering no
    models cannot serve anything and reads as broken rather than narrowed.
    """
    rows = (await db.execute(select(AILLMProvider))).scalars().all()
    claimed: set[str] = set()

    def catalogue_of(p: AILLMProvider) -> list[str]:
        return _provider_models(get_preset(p.provider_key), p.default_model) or []

    def settle(p: AILLMProvider, kept: list[str]) -> None:
        p.models_json = kept or ([p.default_model] if p.default_model else [])
        claimed.update(p.models_json or [])
        if p.default_model and p.default_model not in (p.models_json or []):
            p.default_model = (p.models_json or [None])[0]

    dedicated = [p for p in rows if p.assigned_task]
    # Chain-holders first, so a surface task on the same vendor cannot absorb
    # models another task depends on.
    chained = [
        p for p in dedicated
        if ai_router.resolve_model_for_task(p.assigned_task or "")
    ]
    for p in chained:
        settle(p, ai_router.models_for_dedicated_profile(p.assigned_task, catalogue_of(p)))

    for p in dedicated:
        if p in chained:
            continue
        settle(p, [m for m in catalogue_of(p) if m not in claimed])

    for p in rows:
        if p.assigned_task:
            continue
        p.models_json = [m for m in catalogue_of(p) if m not in claimed] or (
            [p.default_model] if p.default_model else []
        )
        if p.default_model and p.default_model not in (p.models_json or []):
            p.default_model = (p.models_json or [None])[0]


async def _validated_task_assignment(
    db: AsyncSession,
    raw: str | None,
    *,
    exclude_id: int | None = None,
) -> str | None:
    """Normalise and check a requested task dedication.

    Refuses a task that another profile already holds. The column is unique, so
    the database would refuse it anyway — but as an IntegrityError raised at
    commit, which surfaces as a 500 and rolls back every other edit in the same
    request. Checking here returns a 409 that names the profile already holding
    it, which is the thing the user needs to know to resolve it.
    """
    task = (raw or "").strip()
    if not task:
        return None

    known = set(ai_router.TASK_MODEL_CHAINS)
    if task not in known:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown task {task!r}. Known tasks: {', '.join(sorted(known))}.",
        )

    stmt = select(AILLMProvider).where(AILLMProvider.assigned_task == task)
    if exclude_id is not None:
        stmt = stmt.where(AILLMProvider.id != exclude_id)
    clash = (await db.execute(stmt)).scalars().first()
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"“{clash.label}” (id {clash.id}) is already dedicated to "
                f"{task}. A task runs on exactly one profile — free that one "
                f"first, or pick a different task."
            ),
        )
    return task


def _provider_to_response(p: AILLMProvider) -> AIProviderResponse:
    # Tolerate a double-encoded model list. `models_json` is a JSON column and
    # should always be a list, but anything that ever wrote json.dumps(...) into
    # it leaves a str behind — and a single bad row used to fail response
    # validation and 500 the entire providers page, hiding every healthy
    # provider along with it.
    models = ai_router.normalise_model_list(p.models_json)

    # `models_json` is a snapshot taken when the row was created, so a model
    # added to a preset later never reached the dropdown for providers people
    # already had. For fixed-endpoint presets the catalog in code is the truth —
    # serve that, and keep the stored list only for custom endpoints the user
    # curates themselves.
    preset = get_preset(p.provider_key)
    if preset and not preset.get("editable_endpoint"):
        catalog = list(preset["models"])
        # Anything the user added by hand stays available.
        models = catalog + [m for m in models if m not in catalog]

    model_info = {m: info for m in models if (info := get_model_info(m))}
    return AIProviderResponse(
        id=p.id,
        provider_key=p.provider_key,
        label=p.label,
        type=p.type,
        api_key_set=bool(p.api_key),
        api_key_preview=mask_api_key(p.api_key),
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


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _provider_models(preset: dict[str, Any] | None, default_model: Optional[str]) -> list[str] | None:
    if preset and not preset.get("editable_endpoint"):
        return list(preset["models"])
    if default_model:
        return [default_model]
    return [] if preset and preset.get("editable_endpoint") else None


@router.get("/ai/providers/presets", response_model=list[LLMProviderPreset])
async def list_provider_presets():
    return [LLMProviderPreset(**p) for p in PROVIDER_PRESETS]


@router.get("/ai/providers", response_model=list[AIProviderResponse])
async def list_providers(db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(AILLMProvider).order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    return [_provider_to_response(p) for p in res.scalars().all()]


class TaskAssignmentRequest(BaseModel):
    #: null releases the task back to the shared provider pool.
    provider_id: Optional[int] = None


@router.get("/ai/task-assignments")
async def list_task_assignments(db: AsyncSession = Depends(get_db)):
    """Which profile serves which task, for the Recommended Setup panels.

    Returns every known task, including the ones nobody is dedicated to, so the
    UI can render "shared pool" as a state of its own.
    """
    tasks = list((await ai_router.task_assignments(db)).values())
    unmet = [t for t in tasks if t["needs_key"]]
    # Count profiles the user could still dedicate without taking one off a job
    # it already has, so the UI can say how many more keys are actually needed.
    free = (await db.execute(
        select(AILLMProvider)
        .where(AILLMProvider.enabled.is_(True))
        .where(AILLMProvider.assigned_task.is_(None))
    )).scalars().all()
    return {
        "tasks": tasks,
        "required_unmet": [t["task"] for t in unmet],
        "free_profiles": len(free),
        # Every required slot needs its own profile; the shared pool must keep at
        # least one, or untasked calls have nowhere to go.
        "keys_needed": max(0, len(unmet) - max(0, len(free) - 1)),
        "signup_urls": ai_router.KEY_SIGNUP_URLS,
    }


@router.post("/ai/task-assignments/{task}/test")
async def test_task_assignment(task: str, db: AsyncSession = Depends(get_db)):
    """Send one real call down whatever this task would actually use.

    Deliberately not a provider ping: it resolves the profile the same way the
    task does at runtime — dedicated if set, borrowed from the shared pool if
    not — so a pass means *this task* works, not merely that some key somewhere
    answers. The reply says which profile served it and whether that profile was
    the task's own or a borrowed one.
    """
    if task not in ai_router.TASK_MODEL_CHAINS:
        raise HTTPException(status_code=404, detail=f"Unknown task {task!r}")

    dedicated = await ai_router.dedicated_profile_for(db, task)
    started = time.perf_counter()
    try:
        res = await ai_router.chat_for_task(
            db,
            [{"role": "user", "content": "Reply with exactly: READY"}],
            task=task,
            max_tokens=2048,   # reasoning models spend budget before answering
            temperature=0,
            bypass_openmanus=True,
            agent_name=f"test-{task}",
            source="settings-test",
        )
    except Exception as exc:  # noqa: BLE001 — a failed test is a result, not a 500
        return {
            "task": task, "ok": False, "error": str(exc)[:300],
            "dedicated": dedicated is not None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    content = (res.get("content") or "").strip()
    return {
        "task": task,
        "ok": bool(res.get("ok") and content),
        "provider": res.get("provider"),
        "model": res.get("model"),
        "reply": content[:120] or None,
        "error": res.get("error"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "dedicated": dedicated is not None,
        # True when the task has no profile of its own and ran on a shared key.
        "borrowed": dedicated is None,
    }


@router.put("/ai/task-assignments/{task}")
async def assign_task(
    task: str,
    payload: TaskAssignmentRequest,
    db: AsyncSession = Depends(get_db),
):
    """Dedicate one profile to ``task``, or release it with provider_id: null."""
    if task not in ai_router.TASK_MODEL_CHAINS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown task {task!r}. Known: {', '.join(sorted(ai_router.TASK_MODEL_CHAINS))}.",
        )

    # Whoever holds it now loses it — assigning is a move, not a copy, which is
    # what keeps one task on exactly one profile without the caller having to
    # clear the old one first.
    current = (
        await db.execute(select(AILLMProvider).where(AILLMProvider.assigned_task == task))
    ).scalars().all()

    if payload.provider_id is None:
        for p in current:
            p.assigned_task = None
            # Hand the full catalogue back — off duty it rejoins the shared pool
            # and should be able to serve anything again.
            p.models_json = _provider_models(get_preset(p.provider_key), p.default_model)
        await _resync_shared_pool_models(db)
        await db.commit()
        return {"task": task, "provider_id": None, "dedicated": False}

    target = await db.get(AILLMProvider, payload.provider_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if target.assigned_task and target.assigned_task != task:
        raise HTTPException(
            status_code=409,
            detail=(
                f"“{target.label}” is already dedicated to {target.assigned_task}. "
                f"One profile serves one task — release it first, or pick "
                f"another profile."
            ),
        )

    for p in current:
        if p.id != target.id:
            p.assigned_task = None
            p.models_json = _provider_models(get_preset(p.provider_key), p.default_model)

    target.assigned_task = task
    # Narrow the profile to its task's chain. The chains are disjoint (asserted
    # at import), so this is what guarantees two dedicated profiles can never
    # offer the same model — rather than leaving every profile holding the full
    # catalogue and trusting the router not to cross over.
    catalogue = _provider_models(get_preset(target.provider_key), target.default_model) or []
    chain = ai_router.models_for_dedicated_profile(task, catalogue)
    if chain:
        target.models_json = chain
        if target.default_model not in chain:
            target.default_model = chain[0]
    await _resync_shared_pool_models(db)
    await db.commit()
    await db.refresh(target)
    return {
        "task": task,
        "provider_id": target.id,
        "provider_label": target.label,
        "models": list(target.models_json or []),
        "default_model": target.default_model,
        "dedicated": True,
    }


@router.post("/ai/providers", response_model=AIProviderResponse)
async def add_provider(payload: AIProviderCreate, db: AsyncSession = Depends(get_db)):
    existing = await find_duplicate_key(db, payload.api_key)
    if existing is not None:
        raise _duplicate_key_error(existing)

    preset = get_preset(payload.provider_key)
    base_url = _normalize_optional_text(payload.base_url) or (preset["base_url"] if preset else None)
    default_model = _normalize_optional_text(payload.default_model) or (preset["default_model"] if preset else None)
    # Determine next priority (append to end)
    max_pri = await db.scalar(select(func.max(AILLMProvider.priority)))
    next_pri = payload.priority if payload.priority is not None else (int(max_pri or 0) + 10)

    provider = AILLMProvider(
        provider_key=payload.provider_key,
        label=payload.label or (preset["label"] if preset else payload.provider_key),
        type=payload.type or (preset["type"] if preset else "openai_compatible"),
        api_key=payload.api_key.strip() or None,
        base_url=base_url,
        default_model=default_model,
        models_json=_provider_models(preset, default_model),
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
    preset = get_preset(provider.provider_key)
    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data and data["api_key"]:
        # Same guard on edit: pasting a key another row already holds is the
        # easy way to create the duplicate the create path refuses.
        clash = await find_duplicate_key(db, data["api_key"], exclude_id=provider.id)
        if clash is not None:
            raise _duplicate_key_error(clash)
        provider.api_key = data["api_key"].strip()
    for attr in ("label", "enabled", "priority", "daily_limit", "monthly_limit"):
        if attr in data and data[attr] is not None:
            setattr(provider, attr, data[attr])
    if "base_url" in data and data["base_url"] is not None:
        provider.base_url = _normalize_optional_text(data["base_url"])
    if "default_model" in data and data["default_model"] is not None:
        provider.default_model = _normalize_optional_text(data["default_model"])
        catalogue = _provider_models(preset, provider.default_model)
        # A dedicated profile must not be re-expanded to the vendor's whole
        # catalogue here. Setting a model is a routine edit — and it was silently
        # undoing the narrowing every time, handing the profile back every model
        # including ones another task owns, which is how everything drifted back
        # onto one NVIDIA profile.
        if provider.assigned_task:
            provider.models_json = ai_router.models_for_dedicated_profile(
                provider.assigned_task, catalogue or []
            ) or catalogue
        else:
            provider.models_json = catalogue
    if "assigned_task" in data:
        provider.assigned_task = await _validated_task_assignment(
            db, data["assigned_task"], exclude_id=provider.id
        )
    # Keep the shared pool clear of dedicated models after any edit that could
    # have widened a catalogue again.
    await _resync_shared_pool_models(db)
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


# ── JARVIS Intelligence Harvester ──────────────────────────────────────────────

@router.post("/ai/harvest")
async def jarvis_harvest_endpoint(db: AsyncSession = Depends(get_db)):
    """Pull live data from sentiment, SMC, Telegram signals, decisions, news
    and store critical findings as knowledge nodes that expand the Brain Map."""
    from plugins.AiMarketAnalyst.backend.services.jarvis_intelligence import harvest_intelligence
    result = await harvest_intelligence(db)
    return result


# ── JARVIS Strategy Synthesis ─────────────────────────────────────────────────

class StrategySynthesisRequest(BaseModel):
    n_strategies: int = 3

@router.post("/ai/strategies/synthesize")
async def jarvis_synthesize_strategies(
    payload: StrategySynthesisRequest,
    db: AsyncSession = Depends(get_db),
):
    """Read accumulated JARVIS knowledge nodes and synthesise executable Python
    strategies that can be used by the market analysis engine."""
    from plugins.AiMarketAnalyst.backend.services.jarvis_intelligence import synthesize_strategies
    strategies = await synthesize_strategies(db, n_strategies=payload.n_strategies)
    return {"strategies": strategies, "count": len(strategies)}

@router.get("/ai/strategies/synthesized")
async def list_synthesized_strategies(db: AsyncSession = Depends(get_db)):
    """List all JARVIS-generated strategies stored in the knowledge base."""
    from plugins.AiMarketAnalyst.backend.services.jarvis_intelligence import list_generated_strategy_artifacts
    strategies = await list_generated_strategy_artifacts(db, limit=20)
    return {"strategies": strategies}


@router.post("/ai/strategies/evaluate")
async def evaluate_jarvis_strategies(payload: dict, db: AsyncSession = Depends(get_db)):
    """Evaluate generated JARVIS Python strategies against supplied candles."""
    from plugins.AiMarketAnalyst.backend.services.jarvis_intelligence import evaluate_generated_strategies
    symbol = str(payload.get("symbol") or "")
    candles = payload.get("candles") or []
    if not symbol or not isinstance(candles, list):
        raise HTTPException(status_code=400, detail="symbol and candles are required")
    scores = await evaluate_generated_strategies(db, candles=candles, symbol=symbol)
    return {"scores": scores, "count": len(scores)}


# ── Graphify (code/knowledge map) ──────────────────────────

@router.get("/ai/graph/overview")
async def graph_overview_endpoint():
    return graphify_service.graph_overview()


@router.get("/ai/graph/query")
async def graph_query_endpoint(term: str = Query(...), limit: int = 8):
    return graphify_service.query_map(term, limit=limit)


@router.get("/ai/graph/full")
async def graph_full_endpoint(db: AsyncSession = Depends(get_db)):
    """Full node+link data for the 2D/3D force-graph visualization.

    Enriches the graph with synthetic DB-entity nodes from recent trades,
    signals, and sniper trades so the brain map shows live DB state.
    """
    from sqlalchemy import select, desc as sqldesc
    from app.models.database import Signal, Trade
    from plugins.AiMarketAnalyst.backend.models import AIAgentKnowledge

    db_entities: list[dict] = []
    try:
        # Recent signals
        sigs = (await db.execute(
            select(Signal.id, Signal.symbol, Signal.action, Signal.status)
            .order_by(sqldesc(Signal.created_at)).limit(20)
        )).all()
        for row in sigs:
            db_entities.append({
                "id": str(row.id),
                "type": "signal",
                "label": f"Signal: {row.symbol} {row.action}",
                "symbol": row.symbol,
            })
        # Recent trades
        trades = (await db.execute(
            select(Trade.id, Trade.symbol, Trade.action, Trade.status)
            .order_by(sqldesc(Trade.created_at)).limit(20)
        )).all()
        for row in trades:
            db_entities.append({
                "id": str(row.id),
                "type": "trade",
                "label": f"Trade: {row.symbol} {row.action}",
                "symbol": row.symbol,
            })
    except Exception:
        pass

    # Sniper trades (optional — telegram plugin)
    try:
        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramSniperTrade
        sniper_rows = (await db.execute(
            select(TelegramSniperTrade.id, TelegramSniperTrade.symbol, TelegramSniperTrade.direction, TelegramSniperTrade.status)
            .order_by(sqldesc(TelegramSniperTrade.created_at)).limit(10)
        )).all()
        for row in sniper_rows:
            db_entities.append({
                "id": f"sniper_{row.id}",
                "type": "sniper_trade",
                "label": f"Sniper: {row.symbol} {row.direction}",
                "symbol": row.symbol,
            })
    except Exception:
        pass

    try:
        knowledge_rows = (await db.execute(
            select(
                AIAgentKnowledge.id,
                AIAgentKnowledge.title,
                AIAgentKnowledge.kind,
                AIAgentKnowledge.symbol,
                AIAgentKnowledge.source,
            )
            .where(AIAgentKnowledge.agent_role.in_(["jarvis_intelligence", "jarvis_strategy_engine"]))
            .order_by(sqldesc(AIAgentKnowledge.updated_at))
            .limit(80)
        )).all()
        for row in knowledge_rows:
            label_prefix = "Strategy" if row.kind == "strategy" else "Knowledge"
            db_entities.append({
                "id": f"knowledge_{row.id}",
                "type": "jarvis_knowledge",
                "label": row.title or f"{label_prefix}: {row.kind}",
                "symbol": row.symbol,
                "source": row.source,
            })
    except Exception:
        pass

    return graphify_service.graph_full(db_entities=db_entities)


@router.get("/ai/graph/active-nodes")
async def graph_active_nodes_endpoint(window: float = 90.0):
    """Node IDs that agents queried within the last *window* seconds."""
    return {"active_nodes": graphify_service.get_active_nodes(window_seconds=window)}


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


@router.post("/llm/reset-circuits")
async def reset_circuit_breakers():
    """Reset all circuit breakers and reload provider registry."""
    from plugins.AiMarketAnalyst.backend.services import llm_gateway, llm_registry
    
    circuit_count = len(llm_gateway._circuits)
    llm_gateway._circuits.clear()
    
    providers = llm_registry.get_providers(force_reload=True)
    enabled = [p for p in providers if p.enabled]
    disabled = [p for p in providers if not p.enabled]
    
    return {
        "success": True,
        "circuits_cleared": circuit_count,
        "providers_reloaded": len(providers),
        "enabled_providers": [{"id": p.id, "label": p.label} for p in enabled],
        "disabled_providers": [{"id": p.id, "label": p.label, "api_key_env": p.api_key_env} for p in disabled],
    }


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
