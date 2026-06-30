"""
AI Market Analyst — OpenAI Client (Responses API)

Wraps the OpenAI SDK for the AI Market Analyst plugin.
Uses the Responses API with structured JSON output, reasoned output,
and multi-turn via previous_response_id.
"""
import os
import json
import time
from typing import Dict, Any, Optional, List
from loguru import logger

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

from plugins.AiMarketAnalyst.backend.config import ai_analyst_config


# ── Circuit Breaker (plugin-local) ─────────────────────────
_COOLDOWN = 300
_open_until: float = 0.0
_open_reason: str = ""


def _trip(reason: str):
    global _open_until, _open_reason
    _open_until = time.time() + _COOLDOWN
    _open_reason = reason
    logger.warning(f"[AI-Analyst CB] OPEN for {_COOLDOWN}s — {reason}")


def circuit_is_open() -> bool:
    return time.time() < _open_until


def get_circuit_status() -> dict:
    is_open = circuit_is_open()
    r: dict = {"open": is_open}
    if is_open:
        r["reason"] = _open_reason
        r["remaining_s"] = round(max(0, _open_until - time.time()))
    return r


def _is_fatal(err) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("429", "rate limit", "quota", "billing", "invalid_api_key"))


def _get_client() -> "AsyncOpenAI":
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed")
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return AsyncOpenAI(api_key=key)


async def call_model(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    previous_response_id: Optional[str] = None,
    tools: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Call OpenAI and return parsed JSON + metadata.

    Returns:
        {
            "content": <parsed dict>,
            "response_id": <str>,
            "model": <str>,
            "usage": {...},
        }
    """
    if circuit_is_open():
        return {"error": f"Circuit breaker open: {_open_reason}", "content": {}}

    model = model or ai_analyst_config.default_model
    reasoning_effort = reasoning_effort or ai_analyst_config.default_reasoning_effort
    max_tokens = max_tokens or ai_analyst_config.default_max_tokens

    client = _get_client()

    is_reasoning = model.startswith(("o1", "o3", "o4"))

    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Compress through headroom before sending
    messages = compress_messages(messages, caller="openai_client")

    try:
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }

        if is_reasoning:
            kwargs["max_completion_tokens"] = max_tokens
            kwargs["reasoning"] = {"effort": reasoning_effort}
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

    except Exception as exc:
        if _is_fatal(exc):
            _trip(str(exc)[:120])
        logger.error(f"[AI-Analyst] OpenAI error: {exc}")
        return {"error": str(exc), "content": {}}
