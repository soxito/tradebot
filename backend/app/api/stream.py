"""
Server-Sent Events (SSE) endpoint.

A single long-lived stream that pushes realtime events to the browser, replacing
per-component polling. Clients connect with the native ``EventSource`` API:

    const es = new EventSource('/api/v1/stream/events?topics=signal.new,trade.update');
    es.addEventListener('signal.new', e => { ... JSON.parse(e.data) ... });

Events are emitted with a named ``event:`` field (the topic) and a JSON ``data:``
payload, so the client can register one handler per topic.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Query, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.core.events import Topics, event_bus


router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/events")
async def stream_events(
    request: Request,
    topics: Optional[str] = Query(
        default=None,
        description="Comma-separated topics to subscribe to. Omit for all topics.",
    ),
):
    """Open an SSE stream of realtime events, optionally filtered by topic."""
    requested: Optional[set[str]] = None
    if topics:
        requested = {t.strip() for t in topics.split(",") if t.strip()} & Topics.ALL
        if not requested:
            requested = None  # unknown topics → subscribe to everything

    async def event_generator():
        # Announce a successful connection so the client can flip to "live".
        yield {"event": "connected", "data": json.dumps({"ok": True})}
        try:
            async for payload in event_bus.subscribe(requested):
                if await request.is_disconnected():
                    break
                yield {
                    "event": payload.get("topic", "message"),
                    "data": json.dumps(payload.get("data"), default=str),
                }
        except asyncio.CancelledError:  # client disconnected / server shutdown
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSE stream error: {exc}")

    return EventSourceResponse(
        event_generator(),
        ping=15,  # heartbeat comment every 15s to keep proxies from closing idle conns
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/health")
async def stream_health():
    """Report how many SSE subscribers are currently attached to this worker."""
    return {"subscribers": event_bus.subscriber_count(), "topics": sorted(Topics.ALL)}
