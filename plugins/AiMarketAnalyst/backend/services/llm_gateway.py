"""AI Market Analyst - LLM Gateway"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

from plugins.AiMarketAnalyst.backend.config import ai_analyst_config
from plugins.AiMarketAnalyst.backend.services.llm_registry import (
    LLMProvider,
    get_enabled_providers,
)
from plugins.AiMarketAnalyst.backend.services.llm_usage import (
    WINDOWS,
    get_usage,
    increment_usage,
)


_COOLDOWN = 300
_circuits: Dict[str, Dict[str, Any]] = {}
_rr_index = 0


def _trip(provider_id: str, reason: str) -> None:
    _circuits[provider_id] = {
        "open_until": time.time() + _COOLDOWN,
        "reason": reason,
    }
    logger.warning(f"[AI-Analyst CB] {provider_id} OPEN for {_COOLDOWN}s — {reason}")


def circuit_is_open(provider_id: str) -> bool:
    entry = _circuits.get(provider_id)
    if not entry:
        return False
    return time.time() < entry["open_until"]


def get_circuit_status(provider_id: str) -> dict:
    entry = _circuits.get(provider_id)
    if not entry:
        return {"open": False}
    remaining = round(max(0, entry["open_until"] - time.time()))
    return {"open": remaining > 0, "reason": entry["reason"], "remaining_s": remaining}


def get_gateway_status(providers: List[LLMProvider]) -> List[Dict[str, Any]]:
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "type": provider.type,
            "enabled": provider.enabled,
            "circuit": get_circuit_status(provider.id),
        }
        for provider in providers
    ]


def _is_fatal(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("429", "rate limit", "quota", "billing", "invalid_api_key"))


def _parse_model_hint(model: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not model:
        return None, None
    if ":" in model:
        provider_id, model_name = model.split(":", 1)
        return provider_id.strip(), model_name.strip()
    return None, model


def _ordered_candidates(providers: List[LLMProvider]) -> List[LLMProvider]:
    global _rr_index
    if not providers:
        return []

    strategy = ai_analyst_config.routing_strategy

    if strategy == "weighted_random":
        weights = [max(p.weight, 0.01) for p in providers]
        first = random.choices(providers, weights=weights, k=1)[0]
        rest = [p for p in providers if p.id != first.id]
        return [first] + rest

    start = _rr_index % len(providers)
    _rr_index += 1
    return providers[start:] + providers[:start]


async def _quota_available(provider: LLMProvider) -> bool:
    usage = await get_usage(provider.id)
    for window in WINDOWS:
        limit = provider.rate_limits.get(window)
        if limit and usage.get(window, 0) >= limit:
            return False
    return True


def _model_for_provider(provider: LLMProvider, model_hint: Optional[str]) -> str:
    if model_hint and provider.supports_model(model_hint):
        return model_hint
    if provider.models and "*" not in provider.models:
        return provider.models[0]
    return ai_analyst_config.default_model


def _get_api_key(provider: LLMProvider) -> str:
    key = os.getenv(provider.api_key_env, "")
    if not key:
        raise RuntimeError(f"{provider.api_key_env} not set")
    return key


async def _call_openai_compatible(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    model: str,
    reasoning_effort: Optional[str],
    max_tokens: Optional[int],
    tools: Optional[List[Dict]],
) -> Dict[str, Any]:
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed")

    api_key = _get_api_key(provider)
    client = AsyncOpenAI(api_key=api_key, base_url=provider.base_url)

    is_reasoning = model.startswith(("o1", "o3", "o4"))
    max_tokens = max_tokens or ai_analyst_config.default_max_tokens

    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    if is_reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning"] = {"effort": reasoning_effort or "medium"}
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0.2

    if tools:
        kwargs["tools"] = tools

    resp = await client.chat.completions.create(**kwargs)
    raw = resp.choices[0].message.content or "{}"
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        content = {"raw": raw}

    return {
        "content": content,
        "response_id": resp.id,
        "model": resp.model,
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
        },
    }


async def _call_anthropic(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: Optional[int],
) -> Dict[str, Any]:
    api_key = _get_api_key(provider)
    base_url = provider.base_url or "https://api.anthropic.com"
    url = f"{base_url.rstrip('/')}/v1/messages"

    payload = {
        "model": model,
        "max_tokens": max_tokens or ai_analyst_config.default_max_tokens,
        "temperature": 0.2,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    timeout = ai_analyst_config.provider_timeout_s
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    parts = data.get("content", [])
    text = "".join([p.get("text", "") for p in parts if p.get("type") == "text"]) or "{}"
    try:
        content = json.loads(text)
    except json.JSONDecodeError:
        content = {"raw": text}

    usage = data.get("usage", {})
    return {
        "content": content,
        "response_id": data.get("id"),
        "model": data.get("model", model),
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
        },
    }


async def _call_google(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: Optional[int],
) -> Dict[str, Any]:
    api_key = _get_api_key(provider)
    base_url = provider.base_url or "https://generativelanguage.googleapis.com"
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens or ai_analyst_config.default_max_tokens,
        },
    }

    timeout = ai_analyst_config.provider_timeout_s
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, params={"key": api_key})
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates", [])
    text = "{}"
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            text = parts[0].get("text", "{}")

    try:
        content = json.loads(text)
    except json.JSONDecodeError:
        content = {"raw": text}

    usage = data.get("usageMetadata", {})
    return {
        "content": content,
        "response_id": data.get("responseId"),
        "model": model,
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
        },
    }


async def _call_provider(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    model: str,
    reasoning_effort: Optional[str],
    max_tokens: Optional[int],
    tools: Optional[List[Dict]],
) -> Dict[str, Any]:
    if provider.type in ("openai", "openai_compatible"):
        return await _call_openai_compatible(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            tools=tools,
        )

    if provider.type == "anthropic":
        return await _call_anthropic(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
        )

    if provider.type == "google":
        return await _call_google(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
        )

    raise RuntimeError(f"Unsupported provider type: {provider.type}")


async def call_model(
    *,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
    previous_response_id: Optional[str] = None,
    tools: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    providers = get_enabled_providers()
    if not providers:
        return {"error": "No enabled providers", "content": {}}

    provider_hint, model_hint = _parse_model_hint(model)

    if provider_hint:
        providers = [p for p in providers if p.id == provider_hint]

    if model_hint:
        supported = [p for p in providers if p.supports_model(model_hint)]
        providers = supported or providers

    providers = _ordered_candidates(providers)

    if tools:
        providers = [p for p in providers if p.type in ("openai", "openai_compatible")]

    if not providers:
        return {"error": "No compatible providers", "content": {}}

    errors: List[str] = []

    for provider in providers:
        if circuit_is_open(provider.id):
            errors.append(f"{provider.id}: circuit open")
            continue

        if not await _quota_available(provider):
            errors.append(f"{provider.id}: quota exceeded")
            continue

        model_name = _model_for_provider(provider, model_hint)

        try:
            await increment_usage(provider.id)
            result = await _call_provider(
                provider=provider,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model_name,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                tools=tools,
            )

            result.update({"provider": provider.id, "model": result.get("model", model_name)})
            return result

        except Exception as exc:
            if _is_fatal(exc):
                _trip(provider.id, str(exc)[:120])
            logger.error(f"[AI-Analyst] Provider error ({provider.id}): {exc}")
            errors.append(f"{provider.id}: {exc}")

        if not ai_analyst_config.routing_fallback:
            break

    return {"error": " | ".join(errors) or "Provider failure", "content": {}}
