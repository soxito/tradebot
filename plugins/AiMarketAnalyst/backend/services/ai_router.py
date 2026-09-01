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
import re
import time
from dataclasses import dataclass
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


#: Out of credit is not a bad minute either — nothing recovers it until someone
#: tops the account up, so it earns the same long sit-out as a config fault.
PAYMENT_FAULT_STATUS = {402}

#: Consecutive failures per provider, used to widen the cooldown instead of
#: retrying a struggling provider on the same short cycle forever. Reset by
#: :func:`_cb_reset` the moment it serves a request again.
_cb_failures: dict[int, int] = {}

#: Backoff ladder applied to repeat failures. A free tier that has hit its daily
#: cap answers 429 all day; retrying every 2 minutes burns a slot in every
#: cascade and slows down every call the user makes.
_CB_LADDER = (60.0, 300.0, 900.0, 1800.0, 3600.0)


def _cb_open(pid: int) -> bool:
    return time.time() < _circuits.get(pid, 0)


def _cb_reset(pid: int) -> None:
    """A working call clears both the breaker and the escalation it earned."""
    _cb_failures.pop(pid, None)
    _circuits.pop(pid, None)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Honour a provider's own Retry-After header when it sends one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Seconds form; the HTTP-date form is rare here and not worth parsing.
        return max(0.0, min(float(raw), 3600.0))
    except (TypeError, ValueError):
        return None


def _cooldown_for(pid: int, exc: Exception | None) -> float:
    """How long to sit this provider out, based on *why* it failed.

    Treating every failure the same is what makes a provider list feel broken:
    a hard "you are out of credit" and a transient blip both come back in two
    minutes, so the dead one keeps taking a slot in front of live ones.
    """
    fails = _cb_failures.get(pid, 0)
    status = None
    if exc is not None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None

    if status in PAYMENT_FAULT_STATUS or status in CONFIG_FAULT_STATUS:
        return _CB_CONFIG_COOLDOWN
    if status == 429:
        # The provider knows best when it will serve again.
        if (explicit := _retry_after_seconds(exc)) is not None:
            return max(explicit, _CB_LADDER[0])
        return _CB_LADDER[min(fails, len(_CB_LADDER) - 1)]
    if exc is not None and "timed out" in str(exc).lower():
        # Slow, not broken — come back sooner than a rate limit, but still back
        # off if it keeps happening.
        return min(_CB_COOLDOWN * max(1, fails), _CB_LADDER[-2])
    return _CB_LADDER[min(fails, len(_CB_LADDER) - 1)] if fails else _CB_COOLDOWN


def _cb_trip(pid: int, cooldown: float | None = None, exc: Exception | None = None) -> None:
    """Skip this provider for a while. Later trips never shorten an open breaker."""
    # Cooldown is computed from the failures that came *before* this one, so a
    # provider's first stumble starts at the bottom of the ladder rather than
    # already a rung up it.
    if cooldown is None:
        cooldown = _cooldown_for(pid, exc)
    _cb_failures[pid] = _cb_failures.get(pid, 0) + 1
    until = time.time() + cooldown
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
    # Only string content is counted. A multimodal turn carries its image as a
    # base64 data URI inside a content *list*; measuring that would report a
    # megabyte of payload as prompt text and make the compression savings on
    # every image call look absurd.
    return sum(len(c) for m in messages if isinstance(c := m.get("content"), str))


def _is_tool_plumbing(message: dict) -> bool:
    """True for messages that must reach the provider byte-for-byte.

    Headroom rewrites message content to save tokens. Doing that to a tool
    result — or to the assistant turn carrying tool_calls — breaks the
    tool_call_id pairing, and the provider answers with a 400 that looks like a
    model bug rather than a compression bug.

    Multimodal turns qualify for the same reason: their content is a list of
    blocks holding a base64 data URI, and text compression applied to that
    produces an image the provider cannot decode.
    """
    return (
        message.get("role") == "tool"
        or bool(message.get("tool_calls"))
        or isinstance(message.get("content"), list)
    )


async def get_router_settings(db: AsyncSession) -> AIRouterSettings:
    """Fetch (or lazily create) the singleton router-settings row."""
    settings = await db.get(AIRouterSettings, 1)
    if settings is None:
        settings = AIRouterSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    # One-time legacy fix: rows created before the raise still carry the old
    # 800 ceiling, which clipped every published agent analysis mid-sentence.
    # Only the exact legacy value is rewritten, so a deliberately smaller
    # figure set afterwards is respected.
    if settings.per_agent_max_tokens == 800:
        settings.per_agent_max_tokens = 4000
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
    request_timeout: float | None = None,
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
        request_timeout=timeout_for_model(model, request_timeout),
    )
    return content, usage, routed_via


#: Ceiling for the one widened retry below. Above the seats' own ceiling so the
#: retry has somewhere to go, and bounded so a model that never stops cannot
#: spend a free tier's whole minute on one turn.
_MAX_WIDENED_TOKENS = 32000

#: Room a structured agent answer gets, whatever model ends up serving it.
#: Measured across the NVIDIA catalogue on a real room prompt: every model that
#: answered did so well inside this, and the ones that failed were still
#: deliberating when a smaller ceiling ran out.
_STRUCTURED_FLOOR = 8000

#: Bookkeeping keys kept on the payload dict but never sent upstream.
_LOCAL_PAYLOAD_KEYS = ("_widened",)


def _wire(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as it goes on the wire — local bookkeeping stripped.

    Always a fresh dict: the caller keeps mutating the original between
    attempts (dropping ``tools``, widening ``max_tokens``), and a request must
    carry what it was sent with rather than what the payload became later.
    """
    return {k: v for k, v in payload.items() if k not in _LOCAL_PAYLOAD_KEYS}


def _truncated(data: dict[str, Any]) -> bool:
    """Whether the model stopped because it hit the token budget."""
    choices = data.get("choices") or []
    if not choices:
        return False
    return (choices[0].get("finish_reason") or "") == "length"


def _rejects_json_mode(body: str | None) -> bool:
    """Whether a 400 body is the model refusing ``response_format``.

    Anything else — a rate limit, a bad key, a context-length error — must keep
    JSON mode on: dropping it turns a retryable failure into an unparsable
    answer, which costs the turn instead of a second of backoff.
    """
    text = (body or "")[:600].lower()
    if not text:
        return False
    if "response_format" in text or "json_object" in text or "json mode" in text:
        return True
    # Some gateways name only the concept ("structured output is not supported").
    return "json" in text and any(
        kw in text for kw in ("not supported", "unsupported", "invalid", "does not support")
    )


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
    request_timeout: float = _TIMEOUT,
    max_retries: int | None = None,
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
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            resp = await client.post(url, headers=headers, json=_wire(current_payload))
            if resp.status_code >= 400:
                # Retry once without json_mode — but ONLY when the model is
                # actually rejecting the parameter. This used to fire on any
                # 4xx, so a 429 (routine on a free key) permanently stripped
                # JSON mode from the payload every later retry shares. The
                # model then answered in prose, ran past its budget mid-sentence
                # and the caller recorded "Could not parse model JSON output" —
                # a rate limit surfacing as an agent that cannot make decisions.
                if (
                    json_mode
                    and "response_format" in current_payload
                    and resp.status_code == 400
                    and _rejects_json_mode(resp.text)
                ):
                    logger.info(
                        "[AIRouter] {} rejected response_format — retrying as free text",
                        current_payload.get("model"),
                    )
                    current_payload.pop("response_format", None)
                    resp = await client.post(url, headers=headers, json=_wire(current_payload))
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
                        resp = await client.post(url, headers=headers, json=_wire(current_payload))
            resp.raise_for_status()
            data = resp.json()

            # ── Ran out of room before the JSON started ──────────────────────
            # Several models narrate their analysis before emitting the object,
            # and how much they narrate scales with the prompt. Cut off at the
            # budget, the answer is prose ending mid-sentence: the caller reads
            # it as "could not parse", the provider is blamed, and the agent
            # falls back to a local read having spent the whole budget for
            # nothing. Which models do this is not knowable from a list —
            # `finish_reason` says it outright, so ask once with more room.
            spent = time.monotonic() - started
            # Two widening attempts, not one. A single doubling is enough for a
            # model that narrated a little; a model that narrates a lot needs
            # the second, and without it the caller gets a half-finished
            # sentence and blames the provider.
            _widen_rounds = int(current_payload.get("_widened") or 0)
            # Widening applies to prose too: an analysis cut mid-sentence is
            # exactly the "responses are not full" complaint. The retry costs
            # time even past half the deadline — a truncated answer that fails
            # over to another provider costs at least as long and still ends
            # up short somewhere else, so we always spend the extra round.
            if _truncated(data) and _widen_rounds < 2:
                widened = min(
                    int(current_payload.get("max_tokens") or 0) * (4 if _widen_rounds else 2),
                    _MAX_WIDENED_TOKENS,
                )
                if widened > (current_payload.get("max_tokens") or 0):
                    logger.info(
                        "[AIRouter] {} was cut off after {:.0f}s — retrying with {} tokens",
                        current_payload.get("model"), spent, widened,
                    )
                    current_payload["max_tokens"] = widened
                    current_payload["_widened"] = _widen_rounds + 1
                    retry = await client.post(url, headers=headers, json=_wire(current_payload))
                    if retry.status_code < 400:
                        data = retry.json()
                        resp = retry
                        if _truncated(data) and _widen_rounds + 1 < 2:
                            second = min(widened * 2, _MAX_WIDENED_TOKENS)
                            if second > widened:
                                logger.info(
                                    "[AIRouter] {} cut off again — final retry with {} tokens",
                                    current_payload.get("model"), second,
                                )
                                current_payload["max_tokens"] = second
                                current_payload["_widened"] = _widen_rounds + 2
                                last = await client.post(
                                    url, headers=headers, json=_wire(current_payload)
                                )
                                if last.status_code < 400:
                                    data = last.json()
                                    resp = last

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

    try:
        # An interactive turn passes a smaller ladder: three backoff sleeps
        # against a rate-limited free tier is ~7s of silence before failover
        # even begins, and the person is watching.
        return await _retry_with_backoff(
            _do_request,
            payload,
            max_retries=_MAX_RETRIES if max_retries is None else max_retries,
        )
    except httpx.TimeoutException as exc:
        # httpx timeout errors stringify to "" — surface a useful message so the
        # caller's error/note reads "timed out after Ns" instead of blank.
        raise RuntimeError(f"request timed out after {request_timeout:.0f}s") from exc


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


async def get_enabled_providers(
    db: AsyncSession,
    *,
    include_dedicated: bool = False,
) -> list[AILLMProvider]:
    """Enabled providers, excluding task-dedicated profiles by default.

    Excluding by default is deliberate. Several callers select providers
    themselves — the JARVIS multi-brain network, Paul's provider sweep, the
    model-picker endpoints — and none of them route through
    :func:`_apply_task_dedication`. Left in, they would spend a dedicated key on
    unrelated work, which is the exact thing dedication exists to prevent, and
    the leak would be invisible until the reserved task started rate-limiting.

    :func:`db_chat` opts back in, because it is the one caller that knows which
    task is being served and can pick the right profile for it.
    """
    await repair_retired_endpoints(db)
    stmt = (
        select(AILLMProvider)
        .where(AILLMProvider.enabled.is_(True))
        .order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    if not include_dedicated:
        stmt = stmt.where(AILLMProvider.assigned_task.is_(None))
    return list((await db.execute(stmt)).scalars().all())


def _apply_task_dedication(
    providers: list[AILLMProvider],
    task: str | None,
) -> list[AILLMProvider]:
    """Honour per-profile task dedication when picking providers.

    Two rules, and the second is the one that makes dedication mean anything:

    * A call *for* a task uses the profile dedicated to it, alone. Without this
      the task would still spill onto every other profile of the same provider
      on failover.
    * A call for anything else never touches a dedicated profile. This is the
      half that keeps the dedicated key's rate limit and quota reserved — a
      profile is only genuinely "used by nothing else" if the general pool is
      also barred from it.

    A task with no profile dedicated to it falls back to the shared pool, so
    nothing has to be configured for the router to keep working.
    """
    if task:
        dedicated = [p for p in providers if (p.assigned_task or "") == task]
        if dedicated:
            return dedicated
    return [p for p in providers if not (p.assigned_task or "")]


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
    timeout: float | None = None,
    task: str | None = None,
    max_retries: int | None = None,
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
    # The one caller that knows the task, so the one allowed to see dedicated
    # profiles — _apply_task_dedication then narrows to the right one.
    providers = await get_enabled_providers(db, include_dedicated=True)
    providers = _apply_task_dedication(providers, task)
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
    # Never below the floors — analysis responses need room to finish
    # sentences (prose) or the whole structured object (JSON). The budget the
    # caller asked for is kept: a reasoning model is exempt from the ceiling
    # (see the loop below) and needs the caller's figure, not this.
    requested_max_tokens = max_tokens
    if source == "agent" and settings.per_agent_max_tokens:
        floor = 2048 if json_mode else 3000
        max_tokens = max(floor, min(max_tokens, settings.per_agent_max_tokens))

    ordered = _order_providers(providers, settings.strategy, settings.round_robin_cursor)
    if not ordered:
        return {"ok": False, "error": "No usable (keyed) providers", "content": None}

    # When a per-agent model is requested (trading room seat / Recommended
    # Setup), route to the provider that actually offers it. Providers that do
    # not list the model keep their own default_model on failover, so an
    # override for a model no connected provider has (e.g. a stale ``o3``) is
    # harmlessly ignored instead of 400-ing every provider in turn.
    def _provider_has_model(prov: Any, model_id: str) -> bool:
        if not model_id:
            return False
        if model_id == (prov.default_model or ""):
            return True
        return model_id in (normalise_model_list(prov.models_json) or [])

    if model_override:
        ordered = sorted(
            ordered,
            key=lambda prov: 0 if _provider_has_model(prov, model_override) else 1,
        )

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

    # A breaker is a backoff hint, not a verdict on the key. When every provider
    # is inside its cooldown the hint has become a lockout: the room reports
    # "AI calls: 0", every seat falls back to a local read, and the user sees a
    # bot that has quietly stopped thinking. One pass through the breakers beats
    # that — a provider that is genuinely down just re-trips on the way past.
    if not bypass_circuits and ordered and all(_cb_open(p.id) for p in ordered):
        logger.warning(
            "[AIRouter] all {} providers are backing off — trying them anyway "
            "rather than answering with nothing", len(ordered),
        )
        bypass_circuits = True

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
        # Agent seats: only force the override on a provider that actually
        # offers it, else this provider serves its own default_model on
        # failover. Other callers keep the exact model they asked for.
        if source == "agent":
            model = (
                model_override
                if (model_override and _provider_has_model(p, model_override))
                else (p.default_model or model_override or "fable-5-high")
            )
        else:
            model = model_override or p.default_model or "fable-5-high"
        try:
            # Only offer tools to a provider not already known to reject them,
            # so a repeat call costs no wasted round trip.
            _send_tools = tools if (tools and _supports_tools(p.id)) else None
            # How much room the answer needs is a property of the model that
            # actually serves it — which is often not the one the caller asked
            # for. A seat configured for a model this provider does not carry
            # falls back to the provider's own default just above, and sizing
            # the budget from the requested name then starves the model that
            # replies: measured, a seat set to `open-mistral-nemo` was served by
            # nemotron-3.5-lightning on 2048 tokens and was cut off mid-thought
            # every single time, while the same model given room answered in
            # 1599 tokens and 10s.
            #
            # So a structured agent answer gets the generous ceiling outright,
            # rather than one conditioned on recognising the model. It is a
            # ceiling, not a spend — tokens are billed as produced — and being
            # wrong about it in this direction costs nothing, while being wrong
            # the other way costs the whole turn.
            if json_mode and source == "agent":
                call_max_tokens = max(requested_max_tokens, _STRUCTURED_FLOOR)
            elif is_reasoning_model(model):
                call_max_tokens = max(requested_max_tokens, _MIN_REASONING_TOKENS)
            else:
                call_max_tokens = max_tokens
            content, usage, routed_via, message = await _call_openai_compatible_msg(
                base_url=p.base_url,
                api_key=p.api_key,
                model=model,
                messages=send_messages,
                temperature=temperature,
                max_tokens=call_max_tokens,
                json_mode=json_mode,
                tools=_send_tools,
                tool_choice=tool_choice if _send_tools else None,
                provider_id=p.id,
                request_timeout=timeout_for_model(model, timeout),
                max_retries=max_retries,
            )
            p.total_calls = (p.total_calls or 0) + 1
            p.daily_calls = (p.daily_calls or 0) + 1
            p.monthly_calls = (p.monthly_calls or 0) + 1
            p.status = "ok"
            p.last_error = None
            # Served again: drop the breaker and the escalation it had earned,
            # so one bad patch cannot leave a healthy provider backed off for an
            # hour after it recovers.
            _cb_reset(p.id)
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
            _cb_trip(p.id, exc=exc)
            logger.warning(
                "AI provider {} failed ({} consecutive, out for {:.0f}s): {}",
                p.label, _cb_failures.get(p.id, 1),
                max(0.0, _circuits.get(p.id, 0.0) - time.time()), msg,
            )
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
            # A reasoning model spends its budget thinking before it writes a
            # word, so a 50-token ping came back empty and a perfectly healthy
            # provider tested as broken.
            max_tokens=_MIN_REASONING_TOKENS if is_reasoning_model(model) else 50,
            json_mode=False,
        )
        reply = (content or "").strip()
        if not reply:
            return {"ok": False, "error": f"{model} returned an empty reply"}
        return {
            "ok": True,
            "model": routed_via or model,
            "reply": reply[:40],
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


# ── Task-based model routing ────────────────────────────────────────────────
# One map so "which model handles this kind of work" is a single-line change
# instead of a string scattered across agents, Paul chat and the Telegram bot.
# Every model below is on NVIDIA NIM and was verified serving on 2026-08-15.

#: Ordered candidates per task. Later entries are tried when earlier ones fail —
#: these live on the *same* provider, so ordinary provider failover cannot cover
#: them (a transient 500 from one NIM model is not the provider being down).
TASK_MODEL_CHAINS: dict[str, list[str]] = {
    # Chart/screenshot reads. Dedicated vision models lead, because the big
    # reasoning models measurably cannot serve this prompt:
    #   thinkingmachines/inkling  — 70s and spends the ENTIRE token budget on
    #     reasoning_content, returning empty content. Raising the budget only
    #     buys a slower empty answer, so it is not in this chain at all.
    #   meta/muse-glimmer-30b     — answers well but took 138s, past the 120s
    #     deadline, so in production it only ever timed out. Kept last as a
    #     deliberate long-shot once the fast models have already failed.
    # Measured 2026-08-16 on a 590x1280 phone screenshot with the real prompt:
    # llama-3.2-11b-vision 17.4s, nemotron-nano-12b-v2-vl 63.1s, both returning
    # a complete findings block including axis calibration.
    "vision_analysis": [
        "meta/llama-3.2-11b-vision-instruct",
        "nvidia/nemotron-nano-12b-v2-vl",
        "meta/muse-glimmer-30b",
    ],
    # Snappy tool-calling turns: bot replies, position checks, quick lookups.
    "fast_agentic": ["nvidia/nemotron-3.5-lightning-30b-a3b"],
    # Strategy synthesis, forecast narration, full market analysis.
    "deep_reasoning": ["z-ai/glm-5.2", "nvidia/nemotron-3-super-120b-a12b"],

    # ── Surfaces ────────────────────────────────────────────────────────────
    # These name a *place the user talks to the system* rather than a kind of
    # work, so they deliberately pin no models. Empty means "whatever the
    # chosen provider serves": unset, they use any available provider exactly as
    # before; dedicated, they get a profile to themselves without also being
    # locked to a model list that would have to be maintained per vendor.
    "jarvis_chat": [],
    "paul_chat": [],
    "telegram_chat": [],

    # ── JARVIS brain network ────────────────────────────────────────────────
    # Five roles that run concurrently after every analysis cycle. They pin no
    # models for the same reason the surfaces do not — the role is the job, the
    # provider decides the model. Unlike the surfaces these are *required*: the
    # roles run in parallel and adversarially, so sharing one key across them
    # serialises the cycle behind a single rate limit and, worse, has the critic
    # reviewing the consolidator on the same model that wrote it.
    "brain_consolidator": [],
    "brain_indexer": [],
    "brain_critic": [],
    "brain_researcher": [],
    "brain_news_organiser": [],
}

#: Presentation and policy for each task: whether a dedicated profile is
#: required, and how to describe it. Kept beside the chains so a new task
#: cannot be added without deciding both.
TASK_META: dict[str, dict[str, Any]] = {
    "vision_analysis":  {"group": "work",    "required": False, "label": "Chart / image reads"},
    "fast_agentic":     {"group": "work",    "required": False, "label": "Fast agentic turns"},
    "deep_reasoning":   {"group": "work",    "required": False, "label": "Deep reasoning"},
    "jarvis_chat":      {"group": "surface", "required": False, "label": "JARVIS chat"},
    "paul_chat":        {"group": "surface", "required": False, "label": "Agent Paul chat"},
    "telegram_chat":    {"group": "surface", "required": False, "label": "Telegram bot chat"},
    "brain_consolidator":   {"group": "brain", "required": True, "label": "Brain — consolidator"},
    "brain_indexer":        {"group": "brain", "required": True, "label": "Brain — indexer"},
    "brain_critic":         {"group": "brain", "required": True, "label": "Brain — critic"},
    "brain_researcher":     {"group": "brain", "required": True, "label": "Brain — researcher"},
    "brain_news_organiser": {"group": "brain", "required": True, "label": "Brain — news organiser"},
}

#: Free providers to point someone at when a required slot has no profile.
#: Surfaced by the API so the UI can say "get a key" and link somewhere useful
#: rather than just reporting that something is missing.
KEY_SIGNUP_URLS: list[dict[str, str]] = [
    {"label": "NVIDIA NIM", "url": "https://build.nvidia.com", "note": "Free frontier models, incl. the only vision models here"},
    {"label": "Mistral", "url": "https://console.mistral.ai", "note": "Fast, generous free tier"},
    {"label": "Groq", "url": "https://console.groq.com", "note": "Fastest inference, free tier"},
    {"label": "Cerebras", "url": "https://cloud.cerebras.ai", "note": "Free tier, very fast"},
    {"label": "OpenRouter", "url": "https://openrouter.ai", "note": "Many models behind one key"},
]


def required_tasks() -> list[str]:
    """Tasks that must have a dedicated profile before they are healthy."""
    return [t for t, m in TASK_META.items() if m.get("required")]

#: These models emit `reasoning_content` before `content`; too small a budget
#: spends the whole allowance on reasoning and returns empty content.
_REASONING_MODELS = {
    "thinkingmachines/inkling",
    "meta/muse-glimmer-30b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "z-ai/glm-5.2",
}
_MIN_REASONING_TOKENS = 2048


def resolve_model_for_task(task: str) -> list[str]:
    """Ordered model candidates for a task category; empty means "use defaults"."""
    return list(TASK_MODEL_CHAINS.get(task, []))


def _assert_chains_are_disjoint() -> None:
    """No model may appear in two task chains.

    Dedicated profiles are narrowed to their task's chain, so a model shared
    between two chains would put the same model on two keys — and the whole
    point of dedication is that one job's quota cannot be spent by another.
    Checked at import so a chain edit fails loudly here rather than quietly
    re-coupling two tasks in production.
    """
    seen: dict[str, str] = {}
    for task, models in TASK_MODEL_CHAINS.items():
        for m in models:
            if m in seen:
                raise ValueError(
                    f"Model {m!r} is in both {seen[m]!r} and {task!r} chains. "
                    f"Task chains must be disjoint so dedicated profiles never "
                    f"share a model."
                )
            seen[m] = task


_assert_chains_are_disjoint()


def models_for_dedicated_profile(task: str, catalogue: list[str] | None = None) -> list[str]:
    """The models a profile dedicated to ``task`` should carry.

    Narrowing the profile is what makes "never share models" true by
    construction rather than convention. Which models depends on what the
    provider can actually serve:

    * A provider that offers the task's chain (the NVIDIA profiles) is narrowed
      to exactly that chain, in chain order.
    * A provider that offers none of it — Mistral has no NVIDIA model ids — keeps
      its own catalogue. Forcing the chain there would dedicate a profile to a
      task it could not serve, which is worse than not dedicating it at all, and
      is what blocks spreading tasks across vendors.
    """
    chain = list(TASK_MODEL_CHAINS.get(task, []))
    if catalogue is None:
        return chain
    overlap = [m for m in chain if m in catalogue]
    return overlap or list(catalogue)


async def dedicated_profile_for(db: AsyncSession, task: str) -> AILLMProvider | None:
    """The enabled profile dedicated to ``task``, if any."""
    res = await db.execute(
        select(AILLMProvider)
        .where(AILLMProvider.enabled.is_(True))
        .where(AILLMProvider.assigned_task == task)
        .limit(1)
    )
    return res.scalars().first()


async def has_dedicated_profile(db: AsyncSession, task: str) -> bool:
    """True when some enabled provider profile is dedicated to ``task``."""
    return await dedicated_profile_for(db, task) is not None


async def task_assignments(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Task → the profile serving it, for the settings UI.

    Every task in :data:`TASK_MODEL_CHAINS` appears, whether or not a profile is
    dedicated to it, so the UI can show "shared pool" as a real state instead of
    an empty row the user cannot tell apart from a bug.
    """
    res = await db.execute(
        select(AILLMProvider).where(AILLMProvider.assigned_task.isnot(None))
    )
    by_task = {p.assigned_task: p for p in res.scalars().all()}
    out: dict[str, dict[str, Any]] = {}
    for task, chain in TASK_MODEL_CHAINS.items():
        p = by_task.get(task)
        meta = TASK_META.get(task, {})
        required = bool(meta.get("required"))
        out[task] = {
            "task": task,
            "label": meta.get("label", task),
            "group": meta.get("group", "work"),
            "required": required,
            "models": list(chain),
            "provider_id": p.id if p else None,
            "provider_label": p.label if p else None,
            "provider_enabled": bool(p.enabled) if p else None,
            "provider_status": p.status if p else None,
            "dedicated": p is not None,
            # A required slot with nobody on it is the actionable state: the UI
            # turns this into "get a key", not just a blank dropdown.
            "needs_key": required and p is None,
        }
    return out


def is_reasoning_model(model: str | None) -> bool:
    """True when the model thinks before it answers, so needs a bigger budget."""
    return (model or "") in _REASONING_MODELS


#: Substrings of models that cannot answer inside the default 40s. Giving them
#: the standard budget guarantees a timeout on every call, which then trips the
#: breaker and takes the whole provider down with it — the provider is fine, the
#: deadline was never realistic. Matched as substrings so size variants and
#: re-tagged ids stay covered without another edit here.
_SLOW_MODEL_MARKERS = (
    "ultra-550b", "inkling", "muse-glimmer", "glm-5.2", "405b", "253b", "120b",
)
_SLOW_MODEL_TIMEOUT = 120.0


def timeout_for_model(model: str | None, requested: float | None = None) -> float:
    """Request deadline for ``model``; an explicit ``requested`` always wins."""
    if requested:
        return requested
    lowered = (model or "").lower()
    if any(marker in lowered for marker in _SLOW_MODEL_MARKERS):
        return _SLOW_MODEL_TIMEOUT
    # A model that reasons before answering is slow for the same reason it needs
    # a bigger budget: it writes a thousand tokens of thinking first. Judging it
    # by the default deadline times out a provider that was answering normally,
    # and the breaker then takes it out for everything else too.
    if is_reasoning_model(model):
        return _SLOW_MODEL_TIMEOUT
    return _TIMEOUT


# ── Conversational latency routing ───────────────────────────────────────────
# The chat surfaces (web Paul chat, Telegram bot) are the two places a human is
# sitting there waiting, so they get their own routing rule: answer with the
# fastest model that can do the job, and only reach for a thinking model when
# the work actually calls for one or the user asks for it.
#
# Why this is not just "use the fast_agentic chain": that chain names ONE model
# on one vendor, and a surface's provider pool is whatever is connected — which
# may not include it. Forcing a model no provider in the pool serves makes every
# provider 400 in turn, which is the slowest possible outcome. So the model is
# picked from what the pool can actually serve, in preference order.

#: Models that reply in a couple of seconds: no hidden reasoning pass, no
#: 100B+ weights. Ordered best-first; the first one a connected provider serves
#: wins. Ids are matched exactly against each provider's catalogue, so listing a
#: model no one has connected costs nothing.
#
# Timings below are measured, not guessed — one short prompt per model against
# the connected accounts on 2026-08-16. Two results shaped this order:
# ``meta/llama-3.3-70b-instruct`` took 15.8s for a one-line answer (it is here,
# but last of the NVIDIA entries), and ``deepseek-ai/deepseek-v4-flash`` is
# *absent* from the list despite being in the stored catalogue — it answers 410
# Gone, and a 410 is a config fault that sits the whole provider out for half an
# hour, so heading the list with it would take NVIDIA down on the first chat.
FAST_CHAT_MODELS: tuple[str, ...] = (
    # NVIDIA NIM — nano 1.3s, mini 0.9s, nano-9b 4.0s, llama-3.3-70b 15.8s
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "nvidia/nemotron-mini-4b-instruct",
    "meta/llama-3.3-70b-instruct",
    # Groq / Cerebras — fastest inference of the free tiers
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "gpt-oss-20b",
    # Google
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    # Mistral
    "ministral-8b-latest",
    "mistral-small-latest",
    "open-mistral-nemo",
    # Others
    "Meta-Llama-3.3-70B-Instruct",
    "deepseek-chat",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    "google/gemma-4-31b-it:free",
)

#: The user asking for it, in the words people actually use. Any of these makes
#: the turn deep no matter how short the message is.
_DEEP_REQUEST_RE = re.compile(
    r"\b(think\s+(?:deeply|hard|harder|carefully|it\s+through|step[-\s]?by[-\s]?step)|"
    r"deep[-\s]?(?:think|dive|thinking|analysis|research)|ultra\s?think|"
    r"take\s+your\s+time|be\s+thorough|thoroughly|in[-\s]depth|"
    r"full\s+(?:analysis|breakdown|report|write[-\s]?up)|reason\s+(?:it\s+)?through)\b",
    re.IGNORECASE,
)

#: Work that is big whether or not it was asked for deeply: a real analysis, a
#: trade decision, a comparison. Deliberately narrower than the old keyword set,
#: which matched "why", "plan" and "explain" anywhere in the message and so sent
#: nearly every turn down the slow path.
_DEEP_WORK_RE = re.compile(
    r"\b(analys[ei]s?|analyz[ei]|backtest|thesis|trade\s+plan|trading\s+plan|"
    r"market\s+structure|confluence|risk\s+(?:assessment|analysis|review)|"
    r"should\s+i\s+(?:buy|sell|enter|exit|short|long|close|add|hold)|"
    r"portfolio\s+review|write\s+(?:me\s+)?(?:a\s+)?(?:strategy|report)|"
    r"multi[-\s]?agent|all\s+agents|orchestrat\w+)\b",
    re.IGNORECASE,
)

#: A message this long is a brief, not a question — worth the slower model even
#: with no keyword in it.
_BIG_TASK_CHARS = 320


def wants_deep_thinking(text: str | None) -> bool:
    """True when the turn earns a reasoning model.

    Two ways in, and no others: the user asked for depth, or the message is
    genuinely a big piece of work (a real analysis request, a multi-part
    question, or a long brief). Everything else — status checks, prices, "why is
    gold up", follow-ups — is a fast turn, because the thinking model's extra
    minute buys nothing there and the user is watching a blank screen for it.
    """
    body = (text or "").strip()
    if not body:
        return False
    if _DEEP_REQUEST_RE.search(body):
        return True
    if len(body) >= _BIG_TASK_CHARS:
        return True
    if body.count("?") >= 3:
        return True
    return bool(_DEEP_WORK_RE.search(body))


def is_fast_model(model: str | None) -> bool:
    """True when ``model`` answers directly rather than thinking first."""
    name = (model or "").strip()
    if not name:
        return False
    if name in _REASONING_MODELS:
        return False
    lowered = name.lower()
    return not any(marker in lowered for marker in _SLOW_MODEL_MARKERS)


def _catalogue(p: AILLMProvider) -> list[str]:
    """Every model id a provider can serve, its default first."""
    models = normalise_model_list(p.models_json) or []
    if p.default_model and p.default_model not in models:
        models = [p.default_model] + models
    return models


@dataclass(frozen=True)
class ChatRoute:
    """How one conversational turn should be run."""

    deep: bool
    #: Model to pin, or None to let each provider serve its own default.
    model: str | None
    #: Task to route the call under — usually the surface, but a deep turn is
    #: reasoning work and is handed to the profile dedicated to that when one
    #: exists, which is where the strong models live.
    task: str
    max_tokens: int
    #: Wall-clock ceiling for the whole tool loop.
    budget_s: float
    max_iterations: int
    temperature: float
    #: Retries per provider. Interactive turns cannot afford the full ladder —
    #: three backoff sleeps against a rate-limited free tier is ~7s of silence
    #: before failover even starts.
    max_retries: int

    @property
    def label(self) -> str:
        return "deep" if self.deep else "fast"


async def resolve_chat_route(
    db: AsyncSession,
    *,
    task: str,
    text: str | None,
    force_deep: bool | None = None,
    surface: str = "web",
) -> ChatRoute:
    """Pick the model and the budgets for a conversational turn.

    ``force_deep`` overrides the classifier in both directions, for callers that
    already know (an explicit /deep command, or a path that must stay snappy).
    ``surface`` tightens the wall clock: Telegram shows no streaming and no
    progress, so a silent wait there reads as a broken bot much sooner than it
    does in the web chat, which is already printing tokens.
    """
    deep = wants_deep_thinking(text) if force_deep is None else force_deep

    route_task = task
    served: list[str] = []
    try:
        enabled = await get_enabled_providers(db, include_dedicated=True)
        # A surface with a profile of its own keeps it, deep or not — that is a
        # deliberate assignment. Otherwise a deep turn IS reasoning work, so it
        # goes to the profile reserved for reasoning, which is where the strong
        # models are. Without this the surface can only ever see the shared
        # pool, and "run a full analysis" quietly ran on a small chat model.
        if deep and not any((p.assigned_task or "") == task for p in enabled):
            if any((p.assigned_task or "") == "deep_reasoning" for p in enabled):
                route_task = "deep_reasoning"
        for p in _apply_task_dedication(enabled, route_task):
            if p.api_key and p.base_url:
                served.extend(_catalogue(p))
    except Exception as exc:  # noqa: BLE001
        # Not being able to read the provider list is not a reason to fail the
        # turn: an unpinned model still routes, each provider serving its own
        # default. The budgets below are the part that matters most anyway.
        logger.debug("[AIRouter] chat route could not read providers: {}", exc)

    model: str | None = None
    if deep:
        # Prefer a real reasoning model, but only one the pool can serve —
        # pinning an id nobody has makes every provider 400 in turn.
        model = next(
            (m for m in TASK_MODEL_CHAINS.get("deep_reasoning", []) if m in served),
            None,
        )
    else:
        model = next((m for m in FAST_CHAT_MODELS if m in served), None)
        if model is None:
            # Nothing on the fast list is connected. A provider default that is
            # at least not a known-slow model still beats pinning one.
            model = next((m for m in served if is_fast_model(m)), None)

    if deep:
        return ChatRoute(
            deep=True,
            model=model,
            task=route_task,
            # Reasoning models spend tokens thinking before they answer; too
            # small a budget returns nothing at all.
            max_tokens=max(3000, _MIN_REASONING_TOKENS),
            budget_s=40.0 if surface == "telegram" else 60.0,
            max_iterations=3,
            temperature=0.5,
            max_retries=2,
        )
    return ChatRoute(
        deep=False,
        model=model,
        task=route_task,
        max_tokens=1100,
        budget_s=12.0 if surface == "telegram" else 18.0,
        max_iterations=2,
        temperature=0.5,
        max_retries=1,
    )


async def chat_turn(
    db: AsyncSession,
    messages: list[dict[str, Any]],
    *,
    route: ChatRoute,
    surface_task: str,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one conversational turn under ``route``, with the budgets it chose.

    A deep turn is handed to the profile reserved for reasoning, and that
    profile is *alone* — dedication deliberately bars failover onto anything
    else, so a rate-limited reasoning key would otherwise cost the user their
    answer entirely. When that happens the turn is re-run once on the surface's
    own pool with no model pinned: a shorter answer from a smaller model beats
    "no response from AI provider".
    """
    result = await chat_with_tools(
        db,
        messages,
        max_iterations=route.max_iterations,
        total_budget_s=route.budget_s,
        temperature=route.temperature,
        max_tokens=route.max_tokens if max_tokens is None else max_tokens,
        model_override=route.model,
        max_retries=route.max_retries,
        task=route.task,
        **kwargs,
    )
    if (result.get("ok") and (result.get("content") or "").strip()) or route.task == surface_task:
        return result

    logger.warning(
        "[AIRouter] {} turn on {} failed ({}) — retrying on the shared pool",
        route.label, route.task, str(result.get("error"))[:120],
    )
    return await chat_with_tools(
        db,
        messages,
        max_iterations=route.max_iterations,
        total_budget_s=route.budget_s,
        temperature=route.temperature,
        max_tokens=route.max_tokens if max_tokens is None else max_tokens,
        model_override=None,
        max_retries=route.max_retries,
        task=surface_task,
        **kwargs,
    )


async def chat_for_task(
    db: AsyncSession,
    messages: list[dict[str, Any]],
    *,
    task: str,
    max_tokens: int = 800,
    **kwargs: Any,
) -> dict[str, Any]:
    """``db_chat`` with the model chosen by task category.

    Unknown tasks fall straight through to the normal provider defaults, so a
    caller can adopt task routing without any task needing to be defined first.
    """
    # A dedicated profile decides its own models. Its catalogue was narrowed to
    # this task when it was dedicated, and it may be a vendor that carries none
    # of the default chain at all (Mistral serving fast turns), so the profile —
    # not the hardcoded chain — is the authority on what to try.
    dedicated = await dedicated_profile_for(db, task)
    if dedicated:
        candidates = normalise_model_list(dedicated.models_json) or []
        if dedicated.default_model and dedicated.default_model in candidates:
            candidates = [dedicated.default_model] + [
                m for m in candidates if m != dedicated.default_model
            ]
        candidates = candidates or [dedicated.default_model or ""]
        candidates = [m for m in candidates if m]
    else:
        candidates = resolve_model_for_task(task)

    if not candidates:
        return await db_chat(db, messages, max_tokens=max_tokens, task=task, **kwargs)

    kwargs.pop("model_override", None)
    caller_bypass = kwargs.pop("bypass_circuits", False)

    # A dedicated profile already *is* the provider choice, and db_chat matches
    # it by id. Adding a label filter on top would drop it whenever the profile
    # is named something other than the preset key ("Vision", "Nvidia 2") — which
    # is exactly what a dedicated profile tends to be called, and would also bar
    # a non-NVIDIA vendor from ever holding a task.
    preferred = kwargs.pop("preferred_providers", None)
    if not preferred and not dedicated:
        preferred = ["nvidia"]

    last: dict[str, Any] = {"ok": False, "error": f"No model served task {task!r}"}
    for attempt, model in enumerate(candidates):
        budget = max_tokens
        if model in _REASONING_MODELS:
            budget = max(budget, _MIN_REASONING_TOKENS)
        res = await db_chat(
            db,
            messages,
            max_tokens=budget,
            model_override=model,
            preferred_providers=preferred,
            task=task,
            # A chain is deliberately several models on ONE provider, so the
            # first model timing out trips that provider's breaker and would
            # lock out its own fallback. One slow model is not the provider
            # being down, so later candidates ignore the breaker.
            bypass_circuits=caller_bypass or attempt > 0,
            **kwargs,
        )
        if res.get("ok") and (res.get("content") or "").strip():
            return res
        last = res
        logger.warning(f"task-route {task}: {model} failed ({res.get('error')}), trying next")
    return last


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
            # A tool-less provider may still emit a ChatML <tool_call> block —
            # run those too rather than return the raw markup as the answer.
            directives += ai_tools.parse_inline_tool_calls(last.get("content") or "")
            if not directives:
                last["tools_used"] = executed
                if last.get("content"):
                    last["content"] = ai_tools.strip_inline_tool_calls(last["content"])
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
                if final.get("content"):
                    final["content"] = ai_tools.strip_inline_tool_calls(final["content"])
            return final

        # ── Native tool-calling path ─────────────────────────────────────────
        tool_calls = last.get("tool_calls") or []
        if not tool_calls:
            # Nemotron, Qwen and Hermes-family models often emit the call as
            # *text* in the content even though tools were offered natively —
            # a bare ``<tool_call>analyze_symbol<arg_key>…>`` that would
            # otherwise leak to the user verbatim. Parse those (plus any
            # ``<<TOOL: …>>`` directive) and run one more turn on the results.
            inline = ai_tools.parse_inline_tool_calls(last.get("content") or "")
            inline += ai_tools.parse_text_directives(last.get("content") or "")
            if inline:
                results = await _run_calls(
                    [(d["name"], d["arguments"]) for d in inline]
                )
                executed.extend(d["name"] for d in inline)
                convo = convo + [
                    {"role": "assistant", "content": last.get("content") or ""},
                    {
                        "role": "user",
                        "content": "Results of the data you requested:\n\n"
                        + "\n\n".join(
                            f"{d['name']}:\n{r}" for d, r in zip(inline, results)
                        )
                        + "\n\nNow answer my original question using these. "
                        "Reply in plain prose — do NOT emit another tool call.",
                    },
                ]
                continue
            last["tools_used"] = executed
            # Belt and suspenders: strip any inline markup that did not parse
            # so the user never sees raw <tool_call> text.
            if last.get("content"):
                last["content"] = ai_tools.strip_inline_tool_calls(last["content"])
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
        if final.get("content"):
            final["content"] = ai_tools.strip_inline_tool_calls(final["content"])
    return final if final.get("ok") else (last or final)


def _repair_truncated_json(text: str) -> str | None:
    """Close a JSON object the model ran out of room to finish.

    A budget-truncated answer is not a failed answer: the decision, the
    confidence and most of the reasoning are already there, and throwing all of
    it away is what turned a verbose model into an agent that "could not make a
    decision". The repair is mechanical and conservative — close whatever
    string, array and object nesting is still open, dropping only a trailing
    key that has no value yet — and any string it does close is trimmed back to
    its last complete sentence so nothing is ever published mid-word.
    """
    start = text.find("{")
    if start == -1:
        return None
    body = text[start:]

    stack: list[str] = []
    in_string = False
    escaped = False
    last_safe = None  # index just after the last completed key/value pair
    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
            last_safe = i + 1
        elif ch == ",":
            last_safe = i

    repaired = body
    if in_string:
        # Cut the dangling string back to its last finished sentence so the
        # reader never sees half a word, then close the quote.
        cut = max(repaired.rfind(". "), repaired.rfind(".\n"), repaired.rfind("! "), repaired.rfind("? "))
        opening = repaired.rfind('"')
        if cut > opening:
            repaired = repaired[: cut + 1]
        repaired += '"'
    elif last_safe is not None and repaired.rstrip().endswith(","):
        repaired = repaired.rstrip().rstrip(",")

    # A key with no value at all ("reasoning": ) cannot be salvaged — drop it.
    tail = repaired.rstrip()
    if tail.endswith(":"):
        cut = tail.rfind(",")
        repaired = tail[:cut] if cut != -1 else tail[: tail.rfind("{") + 1]

    return repaired + "".join(reversed(stack))


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
        candidate = text[start : end + 1]
    else:
        candidate = text
    try:
        return json.loads(candidate)
    except Exception:
        pass

    # The answer was cut off before it closed. Salvage what the model did say
    # rather than discarding a decision it had already made.
    repaired = _repair_truncated_json(text)
    if not repaired:
        return None
    try:
        parsed = json.loads(repaired)
    except Exception:
        return None
    if isinstance(parsed, dict):
        parsed["_truncated"] = True
        logger.info(
            "[ai_router] recovered a truncated JSON answer ({} keys)", len(parsed) - 1
        )
        return parsed
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
    bypass_circuits: bool = False,
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
        bypass_circuits=bypass_circuits,
    )
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "content": None}
    parsed = parse_json_content(res.get("content"))
    if parsed is None:
        # "Could not parse model JSON output" on its own is unactionable — it
        # cannot tell a truncated answer (budget too small) from prose the model
        # wrapped around the JSON. The tail says which: a cut-off answer ends
        # mid-token, a chatty one ends in punctuation.
        raw = str(res.get("content") or "")
        logger.warning(
            "[ai_router] {} ({}) returned unparsable output for {}: {} chars, ends …{!r}",
            res.get("model"), res.get("provider"), agent_name or source,
            len(raw), raw[-120:],
        )
    return {
        "ok": parsed is not None,
        "content": parsed,
        "provider": res.get("provider"),
        "model": res.get("model"),
        "usage": res.get("usage"),
        "error": None if parsed is not None else "Could not parse model JSON output",
    }

