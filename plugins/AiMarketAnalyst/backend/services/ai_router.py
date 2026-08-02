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
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

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


#: Cached probe of the headroom compression proxy: (checked_at, reachable).
#:
#: OpenAI and NVIDIA calls are routed through it for compression. When it is not
#: running, that routing sent every one of their requests to a dead local port,
#: which answered 401 — indistinguishable, from the outside, from a bad NVIDIA
#: key. Probed rather than assumed, and cached so it costs one request a minute.
_headroom_state: tuple[float, bool] = (0.0, False)
_HEADROOM_PROBE_TTL = 60.0


async def _headroom_available() -> bool:
    global _headroom_state
    checked_at, reachable = _headroom_state
    now = time.time()
    if now - checked_at < _HEADROOM_PROBE_TTL:
        return reachable
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            resp = await c.get(f"{_HEADROOM_PROXY.rstrip('/')}/health")
        reachable = resp.status_code < 500
    except Exception:  # noqa: BLE001 — not running is the common case
        reachable = False
    if not reachable and _headroom_state[1]:
        logger.info(
            "[ai_router] headroom proxy unreachable — sending OpenAI/NVIDIA "
            "traffic direct (compression off, calls still work)"
        )
    _headroom_state = (now, reachable)
    return reachable


_TIMEOUT = 40.0
# Short circuit breaker so a failing provider is skipped briefly
_circuits: dict[int, float] = {}
_CB_COOLDOWN = 120.0

#: A provider answering 401/403/404/410 is misconfigured or retired, not having
#: a bad minute. Two minutes means every request keeps paying for it — it is
#: re-tried, fails, and consumes a slot in the cascade that a working provider
#: needed. Sit it out for long enough that the rest of the list gets the budget,
#: but not so long that fixing the key goes unnoticed.
_CB_CONFIG_COOLDOWN = 1800.0
#: HTTP statuses that mean "this will fail again until a human changes something".
CONFIG_FAULT_STATUS = {401, 403, 404, 410}


def _cb_open(pid: int) -> bool:
    return time.time() < _circuits.get(pid, 0)


def _cb_trip(pid: int, cooldown: float | None = None) -> None:
    """Skip this provider for a while. Later trips never shorten an open breaker."""
    until = time.time() + (cooldown if cooldown is not None else _CB_COOLDOWN)
    _circuits[pid] = max(_circuits.get(pid, 0.0), until)


def config_fault_status(exc: Exception) -> int | None:
    """The HTTP status when `exc` means the provider needs fixing, else None."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    return status if status in CONFIG_FAULT_STATUS else None


#: Markers a provider uses to say it has been shut down for good, rather than
#: that our credential is wrong. GitHub Models emits the first of these during
#: its scheduled retirement brownouts.
_RETIREMENT_MARKERS = (
    "retirement_brownout",
    "has been retired",
    "no longer available",
    "service has been discontinued",
    "endpoint has been deprecated",
)


def is_retired_upstream(exc: Exception) -> bool:
    """True when the provider is telling us it is gone, not that we are wrong.

    Worth separating: no API key or base URL change recovers a retired service,
    so presenting it as a configuration fault costs people real time.
    """
    text = str(exc).lower()
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text += " " + str(response.text).lower()
        except Exception:  # noqa: BLE001 — body may not be readable
            pass
    return any(marker in text for marker in _RETIREMENT_MARKERS)


# Which providers accept OpenAI-style `tools`. Learned at runtime from the 400
# a non-supporting provider returns, and kept in memory alongside _circuits —
# it is a property of the endpoint, not of the account, so it costs nothing to
# rediscover after a restart.
_tool_support: dict[int, bool] = {}


def _supports_tools(pid: int) -> bool:
    """Assume yes until a provider proves otherwise.

    Optimistic on purpose: guessing wrong costs one extra request (the retry in
    _do_request strips the tools key), whereas a pessimistic default would leave
    capable models unable to fetch anything until someone hand-maintained a list.
    """
    return _tool_support.get(pid, True)


def _chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


def _is_tool_plumbing(message: dict) -> bool:
    """True for messages that must reach the provider byte-for-byte.

    Headroom rewrites message content to save tokens. Doing that to a tool
    result — or to the assistant turn carrying tool_calls — breaks the
    tool_call_id pairing, and the provider answers with a 400 that looks like a
    model bug rather than a compression bug.
    """
    return message.get("role") == "tool" or bool(message.get("tool_calls"))


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


import asyncio
import random
from typing import Callable, TypeVar

# ── Retry configuration ────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 30.0
_JITTER = 0.1  # 10% jitter

# Error codes that should trigger a retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_ERRORS = (
    "ResourceExhausted",
    "rate limit",
    "quota exceeded",
    "too many requests",
    "Worker local total request limit reached",
    "connection reset",
    "timeout",
)

T = TypeVar("T")

async def _retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
    max_delay: float = _MAX_DELAY,
    **kwargs,
) -> T:
    """Execute func with exponential backoff retry for transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            status = getattr(exc, "response", None)
            status_code = getattr(status, "status_code", None) if status else None
            
            is_retryable = (
                status_code in _RETRYABLE_STATUS
                or any(e.lower() in err_str for e in _RETRYABLE_ERRORS)
            )
            
            if not is_retryable or attempt >= max_retries:
                raise
            
            # Exponential backoff with jitter
            delay = min(base_delay * (2 ** attempt), max_delay)
            delay += random.uniform(-delay * _JITTER, delay * _JITTER)
            logger.warning(
                f"Retryable error (attempt {attempt + 1}/{max_retries + 1}): {exc}. "
                f"Waiting {delay:.1f}s before retry..."
            )
            await asyncio.sleep(delay)
    
    raise last_exc


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
    """Call an OpenAI-compatible chat endpoint. Returns (content, usage, routed_via).

    Kept at three return values because callers outside this module — jarvis.py,
    analysis_router.py and four monkeypatch sites in the MT5 plugin's tests —
    unpack exactly three. Tool-aware callers use
    :func:`_call_openai_compatible_msg`, which this thin wrapper delegates to.
    """
    content, usage, routed_via, _msg = await _call_openai_compatible_msg(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    return content, usage, routed_via


async def _call_openai_compatible_msg(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    provider_id: int | None = None,
) -> tuple[str, dict[str, int], str | None, dict]:
    """Call an OpenAI-compatible chat endpoint, returning the full message.

    For OpenAI (base_url with "openai.com") or NVIDIA (base_url with "nvidia.com" or "integrate.api.nvidia.com"),
    routes through the headroom proxy for compression. For other providers (Groq, Mistral, Cerebras, OpenRouter),
    calls them directly to avoid 401 errors.

    Returns (content, usage, routed_via, message) — ``message`` carries any
    ``tool_calls`` the model asked for.
    """
    # ── Determine routing: OpenAI through the proxy, everything else direct ──
    #
    # ONLY OpenAI. The headroom proxy has a single upstream — OpenAI — so it
    # forwards whatever key it is handed there. NVIDIA used to be routed through
    # it too, which meant an `nvapi-…` key was presented to OpenAI and came back
    # as "Incorrect API key provided: nvapi-… find your API key at
    # platform.openai.com". NVIDIA could never authenticate that way, and the
    # error pointed at the wrong provider entirely.
    is_openai = "openai.com" in base_url
    if is_openai and await _headroom_available():
        # Use /v1/chat/completions (local headroom binary) not /p/project/v1/ (Cloudflare Workers)
        url = f"{_HEADROOM_PROXY.rstrip('/')}/v1/chat/completions"
    else:
        # Direct endpoint — every non-OpenAI provider, plus OpenAI itself when
        # the proxy is down. Compression is an optimisation; losing it beats
        # every call failing against a proxy that cannot serve them.
        url = f"{base_url.rstrip('/')}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Reasoning models (o3, o1 family) do not accept temperature or max_tokens;
    # they require max_completion_tokens instead.
    _reasoning_prefixes = ("o3", "o1", "o1-mini", "o1-preview", "o1-pro")
    _is_reasoning = any(
        model == m or model.split("/")[-1].startswith(m)
        for m in _reasoning_prefixes
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if _is_reasoning:
        # Reasoning models: use max_completion_tokens, omit temperature
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    async def _do_request(current_payload: dict) -> tuple[str, dict[str, int], str | None, dict]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=current_payload)
            if resp.status_code >= 400:
                # Retry once without json_mode (some free models reject it)
                if json_mode and "response_format" in current_payload:
                    current_payload.pop("response_format", None)
                    resp = await client.post(url, headers=headers, json=current_payload)
                # Several providers in the catalog are aggregator proxies with no
                # tool support and 400 on the `tools` key. Drop it and retry HERE,
                # before raise_for_status, so this never reaches the retry/circuit
                # layer — otherwise the first tool-bearing call would trip the
                # breaker and take that provider out for the full cooldown.
                if resp.status_code >= 400 and "tools" in current_payload:
                    body = (resp.text or "")[:400].lower()
                    if any(
                        kw in body
                        for kw in ("tool", "function", "unsupported", "not supported")
                    ):
                        current_payload.pop("tools", None)
                        current_payload.pop("tool_choice", None)
                        if provider_id is not None:
                            _tool_support[provider_id] = False
                            logger.info(
                                "[AIRouter] provider {} rejected tools — "
                                "falling back to text directives", provider_id,
                            )
                        resp = await client.post(url, headers=headers, json=current_payload)
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
            message = (choices[0].get("message") or {})
            content = message.get("content") or ""
            raw_usage = data.get("usage") or {}
            usage = {
                "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
                "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
                "total_tokens": int(raw_usage.get("total_tokens") or 0),
            }
            return content, usage, routed_via, message

    return await _retry_with_backoff(_do_request, payload)


#: Endpoints that have been retired upstream, and what replaced them.
#:
#: A provider pointed at a retired host answers 401/404 no matter how good the
#: credential is, which reads as "your key is wrong" and sends people off
#: regenerating a perfectly good token. Repairing the stored row means the fix
#: reaches every existing install without anyone having to know the endpoint
#: moved.
RETIRED_BASE_URLS: dict[str, str] = {
    # GitHub Models left the Azure preview host for models.github.ai.
    "https://models.inference.ai.azure.com": "https://models.github.ai/inference",
}

#: Publisher prefixes models.github.ai requires but the old host did not.
_GITHUB_MODEL_PUBLISHERS: dict[str, str] = {
    "o3": "openai", "o3-mini": "openai", "o1": "openai", "o1-mini": "openai",
    "gpt-4o": "openai", "gpt-4o-mini": "openai", "gpt-4.1": "openai",
    "Llama-4-Scout-17B-16E-Instruct": "meta", "Llama-3.3-70B-Instruct": "meta",
    "DeepSeek-R1": "deepseek", "DeepSeek-V3": "deepseek",
    "Ministral-3B": "mistral-ai", "Mistral-large": "mistral-ai",
}

_endpoints_repaired = False


def normalise_model_list(value: Any) -> list[str]:
    """A provider's model list, whatever shape it got stored in.

    ``models_json`` is a JSON column, so it should always come back as a list.
    A string means something wrote ``json.dumps(...)`` into it and double
    encoded the value — repair that rather than propagating a str to callers
    that will treat it as a sequence of characters, or hand it to a response
    model that expects a list and 500s.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(decoded, list):
            return [str(v) for v in decoded]
    return []


def _github_model_id(model: str) -> str:
    """Add the publisher prefix models.github.ai needs, if it is missing."""
    if not model or "/" in model:
        return model
    publisher = _GITHUB_MODEL_PUBLISHERS.get(model)
    return f"{publisher}/{model}" if publisher else model


async def repair_retired_endpoints(db: AsyncSession) -> int:
    """Point providers at their current endpoints. Idempotent, once per process."""
    global _endpoints_repaired
    if _endpoints_repaired:
        return 0
    _endpoints_repaired = True

    try:
        rows = list((await db.execute(select(AILLMProvider))).scalars().all())
        fixed = 0
        for p in rows:
            replacement = RETIRED_BASE_URLS.get((p.base_url or "").rstrip("/"))
            if not replacement:
                continue
            p.base_url = replacement
            if "models.github.ai" in replacement:
                p.default_model = _github_model_id(p.default_model or "")
                # models_json is a JSON column: SQLAlchemy serialises it for us.
                # Assigning json.dumps(...) here stores a JSON *string* inside
                # the JSON value, and everything downstream that expects a list
                # then breaks on a str.
                models = normalise_model_list(p.models_json)
                if models:
                    p.models_json = [_github_model_id(m) for m in models]
            fixed += 1
            logger.warning(
                f"[ai_router] {p.label}: endpoint had been retired — repointed to "
                f"{replacement}. Its existing API key should work again."
            )
        if fixed:
            await db.commit()
        return fixed
    except Exception as exc:  # noqa: BLE001 — never block provider lookup
        logger.debug(f"[ai_router] endpoint repair skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def get_enabled_providers(db: AsyncSession) -> list[AILLMProvider]:
    await repair_retired_endpoints(db)
    res = await db.execute(
        select(AILLMProvider)
        .where(AILLMProvider.enabled.is_(True))
        .order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    return list(res.scalars().all())


async def get_all_available_brain_providers(db: AsyncSession) -> list[AILLMProvider]:
    """Return ALL enabled, credentialed, non-circuit-open, non-capped providers.

    Used by the JARVIS multi-brain network to spread brain manager calls
    across ALL available AI providers rather than relying on one model.
    Adding a new provider in Connect AI tab → it is automatically included
    in the next brain cycle with no code changes required.
    Providers are returned ordered by priority (lowest number = highest priority).
    """
    settings = await get_router_settings(db)
    providers = await get_enabled_providers(db)
    return [
        p for p in providers
        if p.api_key and p.base_url
        and not _cb_open(p.id)
        and not _is_capped(p, settings.reserve_pct)
    ]


async def call_targeted_provider(
    db: AsyncSession,
    provider_label_fragment: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.35,
    max_tokens: int = 800,
    json_mode: bool = False,
    agent_name: str | None = None,
    source: str = "chat",
) -> dict[str, Any]:
    """Call a specific provider by label fragment and model, bypassing router ordering.

    Finds the first enabled provider whose label contains `provider_label_fragment`
    (case-insensitive) and is not circuit-open. Calls it directly so ONLY that
    provider's circuit is affected on failure — other providers are untouched.

    Returns the same structure as db_chat: {ok, content, provider, model, usage}
    or {ok: False, error} on failure (caller should fall back to db_chat).
    """
    settings = await get_router_settings(db)
    providers = await get_enabled_providers(db)

    target = next(
        (
            p for p in providers
            if provider_label_fragment.lower() in (p.label or "").lower()
            and not _cb_open(p.id)
        ),
        None,
    )
    if not target:
        return {
            "ok": False,
            "error": f"No available provider matching '{provider_label_fragment}'",
            "content": None,
        }

    now = datetime.utcnow()
    _reset_usage_windows(target, now)
    if _is_capped(target, settings.reserve_pct):
        return {
            "ok": False,
            "error": f"Provider '{target.label}' is at its usage cap",
            "content": None,
        }

    orig_chars = _chars(messages)
    try:
        content, usage, routed_via = await _call_openai_compatible(
            base_url=target.base_url,
            api_key=target.api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        target.total_calls = (target.total_calls or 0) + 1
        target.daily_calls = (target.daily_calls or 0) + 1
        target.monthly_calls = (target.monthly_calls or 0) + 1
        target.status = "ok"
        target.last_error = None
        target.last_model_used = routed_via or model
        target.last_tested_at = datetime.utcnow()
        db.add(AIUsageRecord(
            provider_id=target.id,
            provider_label=target.label,
            agent_name=agent_name,
            agent_role="jarvis",
            model=routed_via or model,
            source=source,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            orig_chars=orig_chars,
            comp_chars=orig_chars,
            success=True,
        ))
        await db.commit()
        return {
            "ok": True,
            "content": content,
            "provider": target.label,
            "model": model,
            "routed_via": routed_via,
            "usage": usage,
        }
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        target.total_errors = (target.total_errors or 0) + 1
        target.status = "error"
        target.last_error = msg
        db.add(AIUsageRecord(
            provider_id=target.id,
            provider_label=target.label,
            agent_name=agent_name,
            agent_role="jarvis",
            model=model,
            source=source,
            orig_chars=orig_chars,
            comp_chars=orig_chars,
            success=False,
        ))
        await db.commit()
        _cb_trip(target.id)
        logger.debug("[call_targeted_provider] {} / {} failed: {}", target.label, model, msg)
        return {"ok": False, "error": msg, "content": None}


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
    bypass_circuits: bool = False,
    preferred_providers: list[str] | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    bypass_openmanus: bool = False,
) -> dict[str, Any]:
    """Call the next provider chosen by the load-balancing strategy; failover on error.

    Applies Headroom compression, enforces the free-tier reserve, records token
    usage + compression savings per call, and returns
    {ok, content, provider, model, usage} or {ok: False, error}.

    OpenManus-first routing (Phase 3):
    If the OpenManusPlugin is installed and OPENMANUS_ENABLED=true, attempts
    to route through the OpenManus MCP server first. Falls through to the
    standard provider loop on failure (phased fallback strategy).
    """
    # ── OpenManus MCP primary route (transparent to callers) ─────────────────
    # Skipped for tool-calling turns: OpenManus returns early on success and has
    # no tool support, so leaving it in front would make every tool call quietly
    # do nothing — the worst kind of failure, because nothing errors.
    try:
        import os as _os
        _om_enabled = _os.getenv("OPENMANUS_ENABLED", "true").lower() not in ("0", "false", "no", "off")
        if _om_enabled and not bypass_openmanus and not tools:
            from plugins.OpenManusPlugin.backend.services.openmanus_client import (
                mcp_health as _om_health,
                mcp_chat as _om_chat,
            )
            from plugins.OpenManusPlugin.backend.services.adapter import (
                _extract_content_from_mcp as _om_extract,
            )
            # Fast reachability probe (3s timeout)
            if await _om_health():
                _t0 = time.time()
                _om_resp = await _om_chat(messages=list(messages))
                _latency = (time.time() - _t0) * 1000
                _content = _om_extract(_om_resp)
                if _content:
                    # Log to openmanus_call_log (best-effort)
                    try:
                        from plugins.OpenManusPlugin.backend.models import (
                            OpenManusCallLog as _OmLog,
                            RouteSource as _OmRS,
                        )
                        _chars_prompt = _chars(messages)
                        _usage_est = {
                            "prompt_tokens": max(1, _chars_prompt // 4),
                            "completion_tokens": max(1, len(_content) // 4),
                            "total_tokens": max(1, (_chars_prompt + len(_content)) // 4),
                        }
                        _log = _OmLog(
                            flow=source,
                            agent_name=agent_name,
                            source=source,
                            route_source=_OmRS.openmanus,
                            provider_label="openmanus",
                            model="openmanus-mcp",
                            prompt_tokens=_usage_est["prompt_tokens"],
                            completion_tokens=_usage_est["completion_tokens"],
                            total_tokens=_usage_est["total_tokens"],
                            latency_ms=round(_latency, 2),
                            success=True,
                            schema_ok=True,
                        )
                        db.add(_log)
                        await db.commit()
                    except Exception:
                        pass  # never block on logging
                    return {
                        "ok": True,
                        "content": _content,
                        "provider": "openmanus",
                        "model": "openmanus-mcp",
                        "routed_via": "openmanus-mcp",
                        "usage": _usage_est,
                        "route_source": "openmanus",
                    }
                else:
                    logger.debug("[OpenManus] empty response — falling through to provider loop")
    except Exception as _om_exc:  # noqa: BLE001
        logger.debug("[OpenManus] db_chat hook skipped: {}", _om_exc)
    # ─────────────────────────────────────────────────────────────────────────

    settings = await get_router_settings(db)
    providers = await get_enabled_providers(db)
    if not providers:
        return {"ok": False, "error": "No AI providers configured", "content": None}

    # ── Headroom compression (token minimisation) ──
    orig_chars = _chars(messages)
    send_messages = messages
    if settings.headroom_enabled and _headroom_compress is not None:
        try:
            # Tool plumbing is passed through untouched and spliced back in
            # order; compressing it would break the tool_call_id pairing.
            plumbing = {i for i, m in enumerate(messages) if _is_tool_plumbing(m)}
            if plumbing:
                compressible = [m for i, m in enumerate(messages) if i not in plumbing]
                compressed = list(
                    _headroom_compress(compressible, caller=agent_name or source)
                )
                if len(compressed) == len(compressible):
                    it = iter(compressed)
                    send_messages = [
                        messages[i] if i in plumbing else next(it)
                        for i in range(len(messages))
                    ]
                else:
                    # Compression changed the message count, so positions no
                    # longer line up — send the originals rather than risk
                    # splicing a tool result next to the wrong call.
                    send_messages = messages
            else:
                send_messages = _headroom_compress(messages, caller=agent_name or source)
        except Exception:
            send_messages = messages
    comp_chars = _chars(send_messages)

    # Clamp tokens to the per-agent ceiling when this is an agent call.
    # Never clamp below 1200 — analysis responses need room to finish sentences.
    if source == "agent" and settings.per_agent_max_tokens:
        floor = 1200
        max_tokens = max(floor, min(max_tokens, settings.per_agent_max_tokens))

    ordered = _order_providers(providers, settings.strategy, settings.round_robin_cursor)
    if not ordered:
        return {"ok": False, "error": "No usable (keyed) providers", "content": None}

    # Filter to preferred providers when the caller restricts provider selection
    # (e.g. Kronos JARVIS analysis must use NVIDIA NIM, never fall to Groq).
    if preferred_providers:
        _pref_lower = [p_.lower() for p_ in preferred_providers]
        ordered = [
            p for p in ordered
            if any(tok in (p.label or "").lower() for tok in _pref_lower)
            or any(tok in (p.base_url or "").lower() for tok in _pref_lower)
        ]
        if not ordered:
            return {"ok": False, "error": f"No preferred providers available: {preferred_providers}", "content": None}

    # advance the round-robin cursor for the next call
    if settings.strategy == "round_robin":
        settings.round_robin_cursor = (settings.round_robin_cursor + 1) % max(1, len(ordered))

    errors: list[str] = []
    now = datetime.utcnow()
    for p in ordered:
        if not bypass_circuits and _cb_open(p.id):
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
            # Only offer tools to a provider not already known to reject them,
            # so a repeat call costs no wasted round trip.
            _send_tools = tools if (tools and _supports_tools(p.id)) else None
            content, usage, routed_via, message = await _call_openai_compatible_msg(
                base_url=p.base_url,
                api_key=p.api_key,
                model=model,
                messages=send_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                tools=_send_tools,
                tool_choice=tool_choice if _send_tools else None,
                provider_id=p.id,
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
                # Additive: existing callers ignore these, the tool loop needs them.
                "message": message,
                "tool_calls": message.get("tool_calls") or [],
                "tools_supported": _supports_tools(p.id) if tools else None,
                "provider_id": p.id,
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
            temperature=0.1,
            max_tokens=50,
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


async def chat_with_tools(
    db: AsyncSession,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    max_iterations: int = 3,
    total_budget_s: float = 25.0,
    **db_chat_kwargs: Any,
) -> dict[str, Any]:
    """Chat, letting the model fetch live data mid-answer.

    Two paths, chosen per provider:

    * Providers that accept OpenAI-style ``tools`` run a normal call/execute/
      call loop.
    * Providers that don't (several in the catalog are aggregator proxies) get
      the same capability through a text protocol — the model emits a
      ``<<TOOL: …>>`` directive, we run it, and hand the result back for exactly
      one more turn. One extra round trip, no loop.

    ``total_budget_s`` exists because ``_TIMEOUT`` is *per request*: three
    iterations of a slow provider could otherwise run past two minutes, long
    after the user's client has given up.
    """
    from plugins.AiMarketAnalyst.backend.services import ai_tools

    schemas = tools if tools is not None else ai_tools.TOOL_SCHEMAS
    convo: list[dict[str, Any]] = list(messages)
    deadline = time.monotonic() + total_budget_s
    executed: list[str] = []

    async def _run_calls(calls: list[dict[str, Any]]) -> list[str]:
        """Execute tool calls concurrently; each returns text, never raises."""
        sem = asyncio.Semaphore(4)

        async def _one(name: str, args: Any) -> str:
            async with sem:
                return await ai_tools.execute_tool(name, args)

        return list(await asyncio.gather(*[_one(n, a) for n, a in calls]))

    last: dict[str, Any] = {}
    for iteration in range(max_iterations):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            logger.info("[AIRouter] tool loop out of budget after {} calls", len(executed))
            break

        last = await db_chat(
            db, convo, tools=schemas, bypass_openmanus=True, **db_chat_kwargs
        )
        if not last.get("ok"):
            return last

        # ── Text-directive path (provider has no native tool support) ────────
        if last.get("tools_supported") is False:
            directives = ai_tools.parse_text_directives(last.get("content") or "")
            if not directives:
                return last
            results = await _run_calls(
                [(d["name"], d["arguments"]) for d in directives]
            )
            executed.extend(d["name"] for d in directives)
            convo = convo + [
                {"role": "assistant", "content": last.get("content") or ""},
                {
                    "role": "user",
                    "content": "Results of the data you requested:\n\n"
                    + "\n\n".join(
                        f"{d['name']}:\n{r}" for d, r in zip(directives, results)
                    )
                    + "\n\nNow answer my original question using these.",
                },
            ]
            final = await db_chat(db, convo, bypass_openmanus=True, **db_chat_kwargs)
            if final.get("ok"):
                final["tools_used"] = executed
            return final

        # ── Native tool-calling path ─────────────────────────────────────────
        tool_calls = last.get("tool_calls") or []
        if not tool_calls:
            last["tools_used"] = executed
            return last

        pending = [
            (
                (c.get("function") or {}).get("name") or "",
                (c.get("function") or {}).get("arguments") or "{}",
            )
            for c in tool_calls
        ]
        results = await _run_calls(pending)
        executed.extend(name for name, _ in pending)

        convo = convo + [last.get("message") or {"role": "assistant", "content": ""}]
        for call, result in zip(tool_calls, results):
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result,
                }
            )

    # Iteration cap or budget reached with tools still pending — ask once more
    # with tools switched off so the model has to answer from what it has.
    final = await db_chat(db, convo, bypass_openmanus=True, **db_chat_kwargs)
    if final.get("ok"):
        final["tools_used"] = executed
    return final if final.get("ok") else (last or final)


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
    tools: list[dict] | None = None,
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
        tools=tools,
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

