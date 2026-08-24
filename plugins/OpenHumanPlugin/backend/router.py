"""
OpenHumanPlugin — FastAPI Router

Routes under /plugins/openhuman.
Provides:
 - Status (agentmemory + OpenHuman reachability)
 - Memory sync (push recent TradeBot data to agentmemory)
 - Memory query (search agentmemory)
 - Research (forward to OpenHuman desktop if running)
 - MCP schema + live SSE stub (for OpenHuman to subscribe)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.OpenHumanPlugin.backend.config import openhuman_config
from plugins.OpenHumanPlugin.backend.models import OpenHumanMemoryEntry
from plugins.OpenHumanPlugin.backend.schemas import (
    OpenHumanStatus, MemoryEntryRow, MemorySyncResponse,
    ResearchRequest, ResearchResponse,
)
from plugins.OpenHumanPlugin.backend.services.openhuman_client import (
    agentmemory_health, openhuman_health, add_memory,
    search_memory, list_memory, openhuman_research,
)
from plugins.OpenHumanPlugin.backend.services.memory_sync_service import (
    sync_recent_signals, sync_recent_forecasts,
)
from plugins.OpenHumanPlugin.backend.services.mcp_server import build_manifest

router = APIRouter(prefix="/plugins/openhuman", tags=["OpenHuman"])


# ── DB dependency ─────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=OpenHumanStatus)
async def get_status(db: AsyncSession = Depends(get_db)):
    am_ok, oh_ok = await asyncio.gather(
        agentmemory_health(), openhuman_health()
    )
    # Both checks hit the same OpenHuman endpoint (port 7788).
    # am_ok = memory_tree_db component is healthy
    # oh_ok = OpenHuman process is alive and healthy

    count_result = await db.execute(select(func.count()).select_from(OpenHumanMemoryEntry))
    count = int(count_result.scalar() or 0)

    msg = []
    if not oh_ok:
        msg.append(
            "OpenHuman not running on :7788 — make sure the app is open"
        )
    elif not am_ok:
        msg.append("OpenHuman memory_tree_db not ready yet — try again in a moment")

    return OpenHumanStatus(
        agentmemory_reachable=am_ok,
        openhuman_reachable=oh_ok,
        memory_entry_count=count,
        message="; ".join(msg) or "OpenHuman memory brain active",
    )


# ── Memory ────────────────────────────────────────────────────────────────────

@router.post("/memory/sync", response_model=MemorySyncResponse)
async def sync_memory(db: AsyncSession = Depends(get_db)):
    """Push recent signals + active-symbol forecasts to agentmemory."""
    sig_result, fc_result = await asyncio.gather(
        sync_recent_signals(limit=20),
        sync_recent_forecasts(),
    )
    total_synced = sig_result["synced"] + fc_result["synced"]
    total_failed = sig_result["failed"] + fc_result["failed"]
    return MemorySyncResponse(
        synced_count=total_synced,
        failed_count=total_failed,
        details=[sig_result, fc_result],
    )


@router.get("/memory/entries")
async def get_memory_entries(
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(OpenHumanMemoryEntry).order_by(
        desc(OpenHumanMemoryEntry.created_at)
    ).limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_entry_row(r) for r in rows]


@router.get("/memory/query")
async def query_memory(q: str = Query(..., min_length=2)):
    result = await search_memory(q, limit=10)
    return result


# ── Research ──────────────────────────────────────────────────────────────────

@router.post("/research", response_model=ResearchResponse)
async def research(req: ResearchRequest):
    """Forward research prompt to OpenHuman desktop app if reachable."""
    if not await openhuman_health():
        return ResearchResponse(
            status="unavailable",
            result={"message": "OpenHuman desktop app not running."},
        )
    result = await openhuman_research(req.prompt)
    ok = isinstance(result, dict) and "error" not in result
    return ResearchResponse(status="ok" if ok else "error", result=result)


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@router.get("/mcp/schema")
async def mcp_schema(request: Request):
    """JSON MCP manifest — paste into OpenHuman Settings → MCP Servers."""
    base = str(request.base_url).rstrip("/")
    return build_manifest(base)


@router.get("/mcp/sse")
async def mcp_sse_endpoint(request: Request):
    """
    Live MCP SSE endpoint for OpenHuman to subscribe to TradeBot tools.
    Implements a minimal MCP server-sent events handshake.
    """
    if not openhuman_config.mcp_sse_enabled:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "MCP SSE disabled"}, status_code=403)

    async def event_stream():
        # MCP initialise event
        yield f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'initialize', 'id': 1, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'tradebot', 'version': '1.0.0'}}})}\n\n"
        # Keep alive
        while True:
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'ping'})}\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _entry_row(r: OpenHumanMemoryEntry) -> dict:
    return {
        "id": r.id,
        "source": r.source,
        "symbol": r.symbol,
        "content": r.content,
        "tags": r.tags,
        "remote_id": r.remote_id,
        "synced": r.synced,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
