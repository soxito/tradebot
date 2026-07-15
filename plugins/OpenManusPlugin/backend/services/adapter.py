"""OpenManusPlugin — Adapter layer.

Drop-in replacement for `db_chat` that:
  1. Tries OpenManus MCP first (when openmanus.enabled=True)
  2. Falls back to the existing AiMarketAnalyst router on failure
     (when openmanus.fallback_enabled=True)
  3. Logs every call to `openmanus_call_log` for audit and telemetry

Usage (from Jarvis, Kronos, SMC, etc.):

    from plugins.OpenManusPlugin.backend.services.adapter import openmanus_chat

    result = await openmanus_chat(
        db=db,
        messages=messages,
        flow="jarvis",
        agent_name="jarvis-deep-analysis",
        json_mode=False,
    )
    if result["ok"]:
        content = result["content"]
    else:
        # OpenManus + fallback both failed
        ...

The return dict is compatible with db_chat's return:
    {ok, content, provider, model, usage, route_source, latency_ms}
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.OpenManusPlugin.backend.config import openmanus_config
from plugins.OpenManusPlugin.backend.models import OpenManusCallLog, RouteSource
from plugins.OpenManusPlugin.backend.services.openmanus_client import (
    mcp_health,
    mcp_chat,
)

# ── Lazy import of existing router to avoid circular dependency issues ────────
# We import db_chat at call time from AiMarketAnalyst to maintain loose coupling.
def _get_db_chat():
    from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat  # type: ignore
    return db_chat


def _extract_content_from_mcp(response: dict[str, Any]) -> Optional[str]:
    """Extract text content from an MCP tools/call response.

    OpenManus returns results in MCP format:
    {
      "jsonrpc": "2.0",
      "id": "...",
      "result": {
        "content": [{"type": "text", "text": "..."}],
        "isError": false
      }
    }
    """
    if "error" in response and "result" not in response:
        return None

    result = response.get("result") or {}

    # MCP standard: result.content is a list of content blocks
    content_blocks = result.get("content") or []
    if isinstance(content_blocks, list):
        texts = [
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t)
        if text:
            return text

    # Fallback: some OpenManus tools return result.output directly
    output = result.get("output") or result.get("response") or result.get("answer")
    if isinstance(output, str):
        return output

    # Last resort: stringify the result
    if result and not result.get("isError"):
        return str(result)

    return None


async def _try_openmanus(
    messages: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[dict[str, int]], str]:
    """Try routing through OpenManus MCP.

    Returns (content, usage, error_msg).
    content is None and error_msg is set on failure.
    """
    try:
        response = await mcp_chat(messages=messages)

        if "error" in response and "result" not in response:
            err = response.get("error")
            if isinstance(err, dict):
                err = err.get("message", str(err))
            return None, None, str(err)

        content = _extract_content_from_mcp(response)
        if content is None:
            return None, None, "Empty or unreadable OpenManus response"

        # OpenManus doesn't return token usage; estimate from char count
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        out_chars = len(content)
        usage = {
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, out_chars // 4),
            "total_tokens": max(1, (prompt_chars + out_chars) // 4),
        }
        return content, usage, ""

    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:300]


async def openmanus_chat(
    db: AsyncSession,
    messages: list[dict[str, Any]],
    *,
    flow: str = "unknown",
    agent_name: Optional[str] = None,
    source: str = "chat",
    temperature: float = 0.35,
    max_tokens: int = 1200,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Primary entry point replacing db_chat for OpenManus-enabled flows.

    Returns a dict compatible with db_chat's output plus extra fields:
      route_source: "openmanus" | "fallback" | "error"
      latency_ms: float
    """
    t0 = time.monotonic()
    route_source = RouteSource.error
    content: Optional[str] = None
    usage: Optional[dict] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    error_msg: Optional[str] = None
    schema_ok: Optional[bool] = None

    # ── Phase 1: Try OpenManus ────────────────────────────────────────────────
    if openmanus_config.enabled:
        reachable = await mcp_health()
        if reachable:
            content, usage, error_msg = await _try_openmanus(messages)
            if content is not None:
                route_source = RouteSource.openmanus
                provider = "openmanus"
                model = "openmanus-mcp"
                error_msg = None
                schema_ok = True
                logger.debug("[OpenManus] flow={} served by OpenManus", flow)
            else:
                logger.warning(
                    "[OpenManus] flow={} MCP call failed: {} — falling back",
                    flow, error_msg,
                )
        else:
            error_msg = "OpenManus MCP not reachable"
            logger.debug("[OpenManus] flow={} MCP not reachable — falling back", flow)

    # ── Phase 2: Fallback to existing router ──────────────────────────────────
    if content is None and openmanus_config.fallback_enabled:
        try:
            db_chat = _get_db_chat()
            fallback_result = await db_chat(
                db,
                messages,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
                agent_name=agent_name,
                source=source,
            )
            if fallback_result.get("ok"):
                content = fallback_result.get("content")
                usage = fallback_result.get("usage")
                provider = fallback_result.get("provider")
                model = fallback_result.get("model")
                route_source = RouteSource.fallback
                error_msg = None
                schema_ok = True
                logger.debug(
                    "[OpenManus] flow={} served by fallback provider={}", flow, provider
                )
            else:
                error_msg = fallback_result.get("error", "Fallback router failed")
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)[:300]
            logger.error("[OpenManus] flow={} fallback failed: {}", flow, error_msg)

    latency_ms = (time.monotonic() - t0) * 1000
    ok = content is not None

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        log_entry = OpenManusCallLog(
            flow=flow,
            agent_name=agent_name,
            source=source,
            route_source=route_source,
            provider_label=provider,
            model=model,
            prompt_tokens=usage.get("prompt_tokens") if usage else None,
            completion_tokens=usage.get("completion_tokens") if usage else None,
            total_tokens=usage.get("total_tokens") if usage else None,
            latency_ms=round(latency_ms, 2),
            success=ok,
            error_msg=error_msg if not ok else None,
            schema_ok=schema_ok,
        )
        db.add(log_entry)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OpenManus] Failed to write call log: {}", exc)

    return {
        "ok": ok,
        "content": content,
        "provider": provider,
        "model": model,
        "usage": usage or {},
        "route_source": route_source.value,
        "latency_ms": round(latency_ms, 2),
        "error": error_msg if not ok else None,
    }
