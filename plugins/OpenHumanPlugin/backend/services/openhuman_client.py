"""
OpenHumanPlugin — HTTP Client

Talks to:
  1. agentmemory API (http://127.0.0.1:8900) — shared memory store
  2. OpenHuman local JSON-RPC (http://127.0.0.1:19500) — desktop app API

Both are optional. All calls fail-open: network errors return {"error": "..."}.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from loguru import logger

try:
    import httpx
    _HTTPX = True
except ImportError:
    _HTTPX = False

from plugins.OpenHumanPlugin.backend.config import openhuman_config


async def _get(base: str, path: str, params: Optional[Dict] = None) -> Any:
    if not _HTTPX:
        return {"error": "httpx not installed"}
    url = base.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=openhuman_config.request_timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.debug(f"[OpenHumanClient] GET {path} failed: {exc}")
        return {"error": str(exc)}


async def _post(base: str, path: str, json: Any = None) -> Any:
    if not _HTTPX:
        return {"error": "httpx not installed"}
    url = base.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=openhuman_config.request_timeout) as client:
            r = await client.post(url, json=json)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.debug(f"[OpenHumanClient] POST {path} failed: {exc}")
        return {"error": str(exc)}


# ── agentmemory helpers ──────────────────────────────────────────────────────

AM = lambda: openhuman_config.agentmemory_url  # noqa: E731


async def agentmemory_health() -> bool:
    """
    agentmemory is OpenHuman's built-in memory_tree_db (no separate process needed).
    Check its health via the OpenHuman /health endpoint — the memory_tree_db
    component status confirms the memory backend is active.
    """
    result = await _get(AM(), "/health")
    if isinstance(result, dict):
        components = result.get("components", {})
        mem_db = components.get("memory_tree_db", {})
        return mem_db.get("status") == "ok"
    return False


async def add_memory(content: str, tags: Optional[List[str]] = None) -> Any:
    """Ingest a text chunk into OpenHuman's memory tree."""
    return await _post(AM(), "/rpc", json={
        "jsonrpc": "2.0",
        "method": "openhuman.memory_tree_ingest",
        "params": {"text": content, "source": "tradebot", "tags": tags or []},
        "id": 1,
    })


async def search_memory(query: str, limit: int = 5) -> Any:
    """Search OpenHuman's memory tree."""
    return await _post(AM(), "/rpc", json={
        "jsonrpc": "2.0",
        "method": "openhuman.memory_tree_search",
        "params": {"query": query, "limit": limit},
        "id": 1,
    })


async def list_memory(limit: int = 50) -> Any:
    """List recent memory chunks from OpenHuman's memory tree."""
    result = await _post(AM(), "/rpc", json={
        "jsonrpc": "2.0",
        "method": "openhuman.memory_tree_list_chunks",
        "params": {"limit": limit},
        "id": 1,
    })
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


# ── OpenHuman desktop app helpers ────────────────────────────────────────────

OH = lambda: openhuman_config.api_url  # noqa: E731


async def openhuman_health() -> bool:
    """Check OpenHuman desktop health at /health (no auth required)."""
    result = await _get(OH(), "/health")
    # OpenHuman health returns {"healthy": true, ...} when all good
    if isinstance(result, dict):
        return result.get("healthy", False) or ("error" not in result and "pid" in result)
    return False


async def openhuman_research(prompt: str) -> Any:
    """Call OpenHuman via RPC to recall memory context for a prompt."""
    # Use memory_recall_context which works unauthenticated on localhost
    result = await _post(OH(), "/rpc", json={
        "jsonrpc": "2.0",
        "method": "openhuman.memory_tree_recall",
        "params": {"query": prompt, "limit": 10},
        "id": 1,
    })
    if isinstance(result, dict) and "result" in result:
        return {"answer": result["result"], "source": "openhuman_memory_tree"}
    # Fallback: return the raw result
    return result
