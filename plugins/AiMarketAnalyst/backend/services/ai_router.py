"""Unified, DB-backed, multi-provider AI router with automatic failover.

This is the single entry point used across the app (agent decisions, Telegram
sniper entry analysis, signal generation, insights). It loads enabled provider
accounts from the DB ordered by priority and calls them as OpenAI-compatible
chat endpoints, failing over to the next provider on error.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import (
    AILLMProvider,
    AIRouterSettings,
    AIUsageRecord,
)

try:
    # Headroom context-compression (core util). Cuts tokens 60-95%.
    from app.utils.headroom_compress import compress_messages as _headroom_compress
except Exception:  # pragma: no cover - core util always present, but stay safe
    _headroom_compress = None  # type: ignore

# ── Headroom Dashboard Sync ───────────────────────────────────────────────────
# After each successful provider call we:
#  1. Write to ~/.headroom/proxy_savings.json  → updates "Per-Project Savings"
#  2. Fire a fire-and-forget notification to the headroom proxy so the
#     in-memory "Per-Model Token Savings" and "Recent Requests" tables stay live.
_HEADROOM_PROXY = os.getenv("HEADROOM_PROXY_URL", "http://127.0.0.1:8787")
_HEADROOM_PROJECT = "tradebot"
_SAVINGS_PATH = Path.home() / ".headroom" / "proxy_savings.json"


def _update_proxy_savings(model: str, provider_label: str, prompt_tokens: int,
                          completion_tokens: int, tokens_saved: int, cost_usd: float) -> None:
    """Atomically update ~/.headroom/proxy_savings.json with this call's data.

    This makes the "Per-Project Savings" section of the headroom dashboard
    reflect ALL AI provider calls, not just the ones routed through the proxy.
    """
    try:
        data: dict[str, Any] = {}
        if _SAVINGS_PATH.exists():
            try:
                data = json.loads(_SAVINGS_PATH.read_text())
            except Exception:
                data = {}

        if data.get("schema_version") != 3:
            data = {"schema_version": 3, "lifetime": {}, "display_session": {}, "history_points": [], "projects": {}, "projects_limit": 50}

        projects: dict = data.setdefault("projects", {})
        entry: dict = projects.setdefault(_HEADROOM_PROJECT, {
            "requests": 0,
            "tokens_saved": 0,
            "compression_savings_usd": 0.0,
            "total_input_tokens": 0,
            "total_input_cost_usd": 0.0,
            "last_activity_at": "",
            # extended: per-model breakdown (non-standard, read by the tradebot UI)
            "models": {},
        })

        entry["requests"] = entry.get("requests", 0) + 1
        entry["tokens_saved"] = entry.get("tokens_saved", 0) + tokens_saved
        entry["total_input_tokens"] = entry.get("total_input_tokens", 0) + prompt_tokens
        entry["total_input_cost_usd"] = entry.get("total_input_cost_usd", 0.0) + cost_usd
        entry["last_activity_at"] = datetime.utcnow().isoformat() + "Z"

        # per-model row (displayed in the tradebot provider dashboard)
        model_key = f"{model} ({provider_label})"
        mdl: dict = entry.setdefault("models", {}).setdefault(model_key, {
            "requests": 0, "total_input_tokens": 0, "tokens_saved": 0,
        })
        mdl["requests"] = mdl.get("requests", 0) + 1
        mdl["total_input_tokens"] = mdl.get("total_input_tokens", 0) + prompt_tokens
        mdl["tokens_saved"] = mdl.get("tokens_saved", 0) + tokens_saved

        _SAVINGS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:  # pragma: no cover - best-effort, never raise
        logger.debug(f"[headroom sync] savings update skipped: {exc}")


async def _notify_headroom_proxy(model: str, provider_label: str,
                                 prompt_tokens: int, tokens_saved: int) -> None:
    """Fire-and-forget: call the headroom proxy with a no-cost tracking request
    so the in-memory Per-Model stats and Recent Requests tables stay current.
    We use a GET /health request with custom headers to register the model name
    in the proxy's tracking without making an actual LLM call."""
    try:
        # The savings file is our source of truth; the proxy HTTP request is
        # bonus coverage for the in-memory stats panel. Both are best-effort.
        headers = {
            "X-Headroom-Model": model,
            "X-Headroom-Provider": provider_label,
            "X-Headroom-Tokens": str(prompt_tokens),
        }
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.get(f"{_HEADROOM_PROXY}/health", headers=headers)
    except Exception:  # proxy may not be running — silent
        pass


_TIMEOUT = 40.0
# Short circuit breaker so a failing provider is skipped briefly
_circuits: dict[int, float] = {}
_CB_COOLDOWN = 120.0


def _cb_open(pid: int) -> bool:
    return time.time() < _circuits.get(pid, 0)


def _cb_trip(pid: int) -> None:
    _circuits[pid] = time.time() + _CB_COOLDOWN


def _chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


async def get_router_settings(db: AsyncSession) -> AIRouterSettings:
    """Fetch (or lazily create) the singleton router-settings row."""
    settings = await db.get(AIRouterSettings, 1)
    if settings is None:
        settings = AIRouterSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def _reset_usage_windows(p: AILLMProvider, now: datetime) -> None:
    """Roll daily/monthly counters when their window has elapsed."""
    if p.daily_reset_at is None or now >= p.daily_reset_at:
        p.daily_calls = 0
        p.daily_reset_at = now + timedelta(days=1)
    if p.monthly_reset_at is None or now >= p.monthly_reset_at:
        p.monthly_calls = 0
        p.monthly_reset_at = now + timedelta(days=30)


def _is_capped(p: AILLMProvider, reserve_pct: float = 0.0) -> bool:
    """True if the provider has hit its daily or monthly limit (free-tier guard).

    A reserve buffer (e.g. 0.10) stops the provider a bit early so the free
    monthly tier is never fully exhausted.
    """
    reserve = max(0.0, min(reserve_pct, 0.9))
    if p.daily_limit is not None:
        effective = p.daily_limit * (1 - reserve)
        if (p.daily_calls or 0) >= effective:
            return True
    if p.monthly_limit is not None:
        effective = p.monthly_limit * (1 - reserve)
        if (p.monthly_calls or 0) >= effective:
            return True
    return False


def _order_providers(
    providers: list[AILLMProvider],
    strategy: str,
    cursor: int,
) -> list[AILLMProvider]:
    """Return providers ordered by the load-balancing strategy.

    - priority    : lowest priority number first (deterministic failover)
    - round_robin : rotate the start point so calls spread across providers
    - least_used  : provider with the most monthly headroom first
    """
    usable = [p for p in providers if p.api_key and p.base_url]
    if not usable:
        return []
    if strategy == "least_used":
        def headroom(p: AILLMProvider) -> float:
            if p.monthly_limit:
                return (p.monthly_limit - (p.monthly_calls or 0)) / max(1, p.monthly_limit)
            return 1.0  # unlimited tiers always have headroom
        return sorted(usable, key=lambda p: (-headroom(p), p.priority, p.id))
    if strategy == "round_robin" and len(usable) > 1:
        ordered = sorted(usable, key=lambda p: (p.priority, p.id))
        start = cursor % len(ordered)
        return ordered[start:] + ordered[:start]
    # default: priority
    return sorted(usable, key=lambda p: (p.priority, p.id))


async def _call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, dict[str, int], str | None]:
    """Call an OpenAI-compatible chat endpoint.

    For OpenAI (base_url with "openai.com"), routes through the headroom proxy
    for compression. For other providers (Groq, Mistral, Cerebras, OpenRouter),
    calls them directly to avoid 401 errors.

    Returns (content, usage, routed_via).
    """
    # ── Determine routing: OpenAI through proxy, others direct ──────────────
    is_openai = "openai.com" in base_url
    if is_openai:
        # Route OpenAI through headroom proxy for compression
        headroom_proxy = os.getenv("HEADROOM_PROXY_URL", "http://127.0.0.1:8787")
        url = f"{headroom_proxy.rstrip('/')}/p/tradebot/v1/chat/completions"
    else:
        # Direct endpoint for other providers
        url = f"{base_url.rstrip('/')}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            # Retry once without json_mode (some free models reject it)
            if json_mode:
                payload.pop("response_format", None)
                resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # FreeLLMAPI (and similar proxies) report the upstream they routed to.
        routed_via = resp.headers.get("x-routed-via") or resp.headers.get("X-Routed-Via")
        attempts = resp.headers.get("x-fallback-attempts")
        if routed_via and attempts:
            routed_via = f"{routed_via} (×{attempts})"
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("No choices in response")
    content = (choices[0].get("message") or {}).get("content") or ""
    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
        "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
        "total_tokens": int(raw_usage.get("total_tokens") or 0),
    }
    return content, usage, routed_via


async def get_enabled_providers(db: AsyncSession) -> list[AILLMProvider]:
    res = await db.execute(
        select(AILLMProvider)
        .where(AILLMProvider.enabled.is_(True))
        .order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    return list(res.scalars().all())


async def db_chat(
    db: AsyncSession,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 800,
    json_mode: bool = False,
    model_override: str | None = None,
    agent_name: str | None = None,
    agent_role: str | None = None,
    source: str = "chat",
) -> dict[str, Any]:
    """Call the next provider chosen by the load-balancing strategy; failover on error.

    Applies Headroom compression, enforces the free-tier reserve, records token
    usage + compression savings per call, and returns
    {ok, content, provider, model, usage} or {ok: False, error}.
    """
    settings = await get_router_settings(db)
    providers = await get_enabled_providers(db)
    if not providers:
        return {"ok": False, "error": "No AI providers configured", "content": None}

    # ── Headroom compression (token minimisation) ──
    orig_chars = _chars(messages)
    send_messages = messages
    if settings.headroom_enabled and _headroom_compress is not None:
        try:
            send_messages = _headroom_compress(messages, caller=agent_name or source)
        except Exception:
            send_messages = messages
    comp_chars = _chars(send_messages)

    # Clamp tokens to the per-agent ceiling when this is an agent call
    if source == "agent" and settings.per_agent_max_tokens:
        max_tokens = min(max_tokens, settings.per_agent_max_tokens)

    ordered = _order_providers(providers, settings.strategy, settings.round_robin_cursor)
    if not ordered:
        return {"ok": False, "error": "No usable (keyed) providers", "content": None}

    # advance the round-robin cursor for the next call
    if settings.strategy == "round_robin":
        settings.round_robin_cursor = (settings.round_robin_cursor + 1) % max(1, len(ordered))

    errors: list[str] = []
    now = datetime.utcnow()
    for p in ordered:
        if _cb_open(p.id):
            errors.append(f"{p.label}: circuit open")
            continue
        # Roll usage windows, then skip if the free-tier cap (minus reserve) is reached
        _reset_usage_windows(p, now)
        if _is_capped(p, settings.reserve_pct):
            errors.append(f"{p.label}: usage cap reached (protecting free tier)")
            await db.commit()
            continue
        model = model_override or p.default_model or "fable-5-high"
        try:
            content, usage, routed_via = await _call_openai_compatible(
                base_url=p.base_url,
                api_key=p.api_key,
                model=model,
                messages=send_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
            p.total_calls = (p.total_calls or 0) + 1
            p.daily_calls = (p.daily_calls or 0) + 1
            p.monthly_calls = (p.monthly_calls or 0) + 1
            p.status = "ok"
            p.last_error = None
            # For proxies (FreeLLMAPI) record the upstream actually served so
            # the provider tab shows e.g. "google/gemini-2.5-flash" not "auto".
            p.last_model_used = routed_via or model
            p.last_tested_at = datetime.utcnow()
            db.add(AIUsageRecord(
                provider_id=p.id,
                provider_label=p.label,
                agent_name=agent_name,
                agent_role=agent_role,
                model=routed_via or model,
                source=source,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                orig_chars=orig_chars,
                comp_chars=comp_chars,
                success=True,
            ))
            await db.commit()

            # ── Headroom dashboard sync ────────────────────────────────────
            tokens_saved_this_call = max(0, orig_chars - comp_chars) // 4  # rough token estimate from char savings
            cost_usd = usage["prompt_tokens"] * 0.000001  # rough $1/1M tokens
            _update_proxy_savings(
                model=routed_via or model,
                provider_label=p.label,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                tokens_saved=tokens_saved_this_call,
                cost_usd=cost_usd,
            )
            asyncio.ensure_future(_notify_headroom_proxy(
                model=routed_via or model,
                provider_label=p.label,
                prompt_tokens=usage["prompt_tokens"],
                tokens_saved=tokens_saved_this_call,
            ))

            return {
                "ok": True,
                "content": content,
                "provider": p.label,
                "model": model,
                "routed_via": routed_via,
                "usage": usage,
            }
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:300]
            errors.append(f"{p.label}: {msg}")
            p.total_errors = (p.total_errors or 0) + 1
            p.status = "error"
            p.last_error = msg
            db.add(AIUsageRecord(
                provider_id=p.id,
                provider_label=p.label,
                agent_name=agent_name,
                agent_role=agent_role,
                model=model,
                source=source,
                orig_chars=orig_chars,
                comp_chars=comp_chars,
                success=False,
            ))
            await db.commit()
            _cb_trip(p.id)
            logger.warning("AI provider {} failed: {}", p.label, msg)
            continue

    return {"ok": False, "error": " | ".join(errors) or "All providers failed", "content": None}


async def test_provider(provider: AILLMProvider) -> dict[str, Any]:
    """Ping a single provider with a tiny prompt to verify the API key."""
    if not provider.api_key:
        return {"ok": False, "error": "No API key set"}
    if not provider.base_url:
        return {"ok": False, "error": "No base URL"}
    model = provider.default_model or "fable-5-high"
    try:
        content, _usage, routed_via = await _call_openai_compatible(
            base_url=provider.base_url,
            api_key=provider.api_key,
            model=model,
            messages=[
                {"role": "system", "content": "You are a connectivity test. Reply with the single word OK."},
                {"role": "user", "content": "ping"},
            ],
            temperature=0.0,
            max_tokens=5,
            json_mode=False,
        )
        return {
            "ok": True,
            "model": routed_via or model,
            "reply": (content or "").strip()[:40],
        }
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {exc.response.status_code}: {body}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


def parse_json_content(content: str | None) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response."""
    if not content:
        return None
    text = content.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        text = text.replace("json", "", 1).strip() if text.lower().startswith("json") else text
    # Find the outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except Exception:
        return None


async def has_enabled_providers(db: AsyncSession) -> bool:
    """True if at least one usable (enabled, keyed) provider exists."""
    providers = await get_enabled_providers(db)
    return any(p.api_key and p.base_url for p in providers)


async def agent_chat(
    db: AsyncSession,
    *,
    system_prompt: str,
    user_prompt: str,
    reference_context: str | None = None,
    max_tokens: int = 800,
    model_override: str | None = None,
    agent_name: str | None = None,
    agent_role: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """LLM call for AI trading agents, routed through the connected DB providers.

    ``reference_context`` (stored knowledge + Graphify map + past decisions) is
    sent as an earlier user turn so Headroom can compress that repetitive
    boilerplate, while the final ``user_prompt`` (live market data + the actual
    instruction) stays protected. Returns {ok, content(dict|None), provider,
    model, usage, error}. Respects the load-balancing strategy and usage caps.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if reference_context:
        messages.append({"role": "user", "content": reference_context})
    messages.append({"role": "user", "content": user_prompt})
    res = await db_chat(
        db,
        messages,
        temperature=0.2,
        max_tokens=max_tokens,
        json_mode=True,
        model_override=model_override,
        agent_name=agent_name,
        agent_role=agent_role,
        source=source,
    )
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "content": None}
    parsed = parse_json_content(res.get("content"))
    return {
        "ok": parsed is not None,
        "content": parsed,
        "provider": res.get("provider"),
        "model": res.get("model"),
        "usage": res.get("usage"),
        "error": None if parsed is not None else "Could not parse model JSON output",
    }

