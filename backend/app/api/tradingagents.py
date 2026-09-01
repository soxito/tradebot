"""TradingAgents proxy router.

Bridges the main backend to the TradingAgents sidecar service
(backend/tradingagents_service) so the frontend never talks to the
sidecar directly:

    POST /api/v1/tradingagents/analyze        start an analysis run
    GET  /api/v1/tradingagents/status         sidecar health
    GET  /api/v1/tradingagents/runs           recent runs (from our DB)
    GET  /api/v1/tradingagents/runs/{run_id}  one run with full reports
    GET  /api/v1/tradingagents/runs/{run_id}/stream   SSE live progress

Every started run is persisted to ``tradingagents_runs`` and settled by a
background poller, so history survives restarts whether or not anyone
watched the live stream.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.config import settings

router = APIRouter(prefix="/tradingagents", tags=["tradingagents"])

_SETTLE_POLL_SECONDS = 5
_SETTLE_MAX_WAIT_S = 45 * 60  # a deep multi-round run can legitimately take tens of minutes


def _sidecar_url(path: str) -> str:
    base = str(settings.TRADINGAGENTS_SERVICE_URL).rstrip("/")
    return f"{base}{path}"


async def _get_sidecar_json(client: httpx.AsyncClient, path: str) -> dict:
    resp = await client.get(
        _sidecar_url(path),
        timeout=settings.TRADINGAGENTS_PROXY_TIMEOUT_READ,
    )
    resp.raise_for_status()
    return resp.json()


# ── Models ────────────────────────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., description="Symbol, e.g. AAPL, BTC/USDT, 0700.HK")
    trade_date: str | None = Field(None, description="YYYY-MM-DD; default today")
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    reasoning_effort: str | None = None
    response_language: str | None = None
    max_debate_rounds: int | None = Field(None, ge=1, le=6)
    max_risk_discuss_rounds: int | None = Field(None, ge=1, le=6)
    source: str = Field("manual", description="manual | trade_validation")


# ── Persistence ───────────────────────────────────────────────────────────────


async def _persist_start(run_id: str, req: AnalyzeRequest, mapped_ticker: str, config_used: dict) -> None:
    from app.core.database import AsyncSessionLocal
    from app.models.database import TradingAgentsRun
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(TradingAgentsRun).where(TradingAgentsRun.run_id == run_id))
        row = existing.scalar_one_or_none()
        if row:
            return
        db.add(
            TradingAgentsRun(
                run_id=run_id,
                ticker=req.ticker,
                mapped_ticker=mapped_ticker or None,
                trade_date=req.trade_date or "",
                source=req.source or "manual",
                status="running",
                config_used=config_used or None,
            )
        )
        await db.commit()


async def _persist_finish(run_id: str) -> None:
    """Pull the finished run from the sidecar and settle its DB row."""
    from app.core.database import AsyncSessionLocal
    from app.models.database import TradingAgentsRun
    from sqlalchemy import select

    try:
        async with httpx.AsyncClient() as client:
            snapshot = await _get_sidecar_json(client, f"/api/runs/{run_id}")
    except Exception as exc:  # noqa: BLE001 - sidecar may have restarted; keep row running
        logger.warning(f"[TradingAgents] could not fetch finished run {run_id}: {exc}")
        return

    status = snapshot.get("status") or "error"
    result = snapshot.get("result")
    rec = (result or {}).get("recommendation") or {}
    decision = (
        rec.get("action")
        or rec.get("decision")
        or ((result or {}).get("decision_summary") or "").split(" ")[0].lower()
        or None
    )

    async with AsyncSessionLocal() as db:
        found = await db.execute(select(TradingAgentsRun).where(TradingAgentsRun.run_id == run_id))
        row = found.scalar_one_or_none()
        if not row:
            return
        row.status = "done" if status == "done" else "error"
        row.error = snapshot.get("error")
        if status == "done":
            row.result = result
            row.decision = (decision or "").lower() or None
            conf = rec.get("confidence")
            try:
                row.confidence = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                row.confidence = None
            row.reasoning = rec.get("reasoning") or (result or {}).get("final_trade_decision") or None
        finished_at = snapshot.get("finished_at")
        if finished_at:
            try:
                # Column is TIMESTAMP WITHOUT TIME ZONE — store naive UTC.
                row.finished_at = datetime.fromisoformat(finished_at).replace(tzinfo=None)
            except ValueError:
                row.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    logger.info(f"[TradingAgents] persisted run {run_id} → {row.status}")


async def _settle_run(run_id: str) -> None:
    """Background poller: wait for the sidecar run to finish, then persist."""
    deadline = asyncio.get_event_loop().time() + _SETTLE_MAX_WAIT_S
    try:
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(_SETTLE_POLL_SECONDS)
                try:
                    snapshot = await _get_sidecar_json(client, f"/api/runs/{run_id}")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[TradingAgents] settle poll {run_id} failed: {exc}")
                    continue
                if snapshot.get("status") in ("done", "error"):
                    break
            else:
                logger.warning(f"[TradingAgents] run {run_id} still unfinished after {_SETTLE_MAX_WAIT_S}s")
        await _persist_finish(run_id)
    except Exception as exc:  # noqa: BLE001 - settlement must never crash anything
        logger.warning(f"[TradingAgents] settle failed for {run_id}: {exc}")


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/status")
async def sidecar_status() -> dict:
    """Health of the sidecar service."""
    try:
        async with httpx.AsyncClient() as client:
            data = await _get_sidecar_json(client, "/health")
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "url": settings.TRADINGAGENTS_SERVICE_URL}


@router.get("/providers")
async def provider_availability() -> dict:
    """Which LLM providers have keys configured in the sidecar env."""
    try:
        async with httpx.AsyncClient() as client:
            data = await _get_sidecar_json(client, "/api/providers")
        return data
    except Exception as exc:  # noqa: BLE001
        return {"providers": {}, "error": str(exc)[:200]}


@router.post("/analyze")
async def analyze(req: AnalyzeRequest) -> dict:
    """Start a TradingAgents analysis run on the sidecar."""
    payload = req.model_dump(exclude={"source"}, exclude_none=True)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _sidecar_url("/api/runs"),
                json=payload,
                timeout=settings.TRADINGAGENTS_PROXY_TIMEOUT_START,
            )
            if resp.status_code == 429:
                raise HTTPException(status_code=429, detail=resp.json().get("detail", "sidecar busy"))
            resp.raise_for_status()
            data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"TradingAgents sidecar unreachable: {exc}") from exc

    run_id = data.get("run_id")
    if run_id:
        # The sidecar resolves the default trade date — store what it uses so
        # the durable row matches the actual analysis window.
        req.trade_date = data.get("trade_date") or req.trade_date
        await _persist_start(run_id, req, data.get("mapped_ticker") or "", payload)
        asyncio.create_task(_settle_run(run_id))
    return data


@router.get("/runs")
async def list_runs(limit: int = 50) -> dict:
    """Recent runs from our own database (survives sidecar restarts)."""
    from app.core.database import AsyncSessionLocal
    from app.models.database import TradingAgentsRun
    from sqlalchemy import select

    limit = max(1, min(limit, 200))
    async with AsyncSessionLocal() as db:
        found = await db.execute(
            select(TradingAgentsRun)
            .order_by(TradingAgentsRun.created_at.desc())
            .limit(limit)
        )
        rows = found.scalars().all()
        return {
            "runs": [
                {
                    "run_id": r.run_id,
                    "ticker": r.ticker,
                    "mapped_ticker": r.mapped_ticker,
                    "trade_date": r.trade_date,
                    "source": r.source,
                    "status": r.status,
                    "decision": r.decision,
                    "confidence": r.confidence,
                    "reasoning": (r.reasoning or "")[:500] or None,
                    "duration_s": r.duration_s,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "has_result": bool(r.result),
                }
                for r in rows
            ]
        }


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    """One run: prefer the durable DB row, fall back to the sidecar."""
    from app.core.database import AsyncSessionLocal
    from app.models.database import TradingAgentsRun
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        found = await db.execute(select(TradingAgentsRun).where(TradingAgentsRun.run_id == run_id))
        row = found.scalar_one_or_none()

    if row:
        payload = {
            "run_id": row.run_id,
            "ticker": row.ticker,
            "mapped_ticker": row.mapped_ticker,
            "trade_date": row.trade_date,
            "source": row.source,
            "status": row.status,
            "decision": row.decision,
            "confidence": row.confidence,
            "reasoning": row.reasoning,
            "result": row.result,
            "config_used": row.config_used,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
        # A running row may already have a finished sidecar copy we haven't
        # settled — or the sidecar restarted and lost the run entirely.
        if row.status == "running":
            try:
                async with httpx.AsyncClient() as client:
                    snapshot = await _get_sidecar_json(client, f"/api/runs/{run_id}")
                if snapshot.get("status") in ("done", "error"):
                    asyncio.create_task(_persist_finish(run_id))
                else:
                    return payload
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    return payload
                # Sidecar explicitly says unknown run → it was lost on restart.
                from app.core.database import AsyncSessionLocal
                from app.models.database import TradingAgentsRun
                from sqlalchemy import select

                async with AsyncSessionLocal() as db:
                    found = await db.execute(
                        select(TradingAgentsRun).where(TradingAgentsRun.run_id == run_id)
                    )
                    stale = found.scalar_one_or_none()
                    if stale and stale.status == "running":
                        stale.status = "error"
                        stale.error = "run lost: sidecar restarted before completion"
                        await db.commit()
                payload["status"] = "error"
                payload["error"] = "run lost: sidecar restarted before completion"
                return payload
            except Exception:  # noqa: BLE001 - sidecar down is fine, DB view stands
                pass
        return payload

    # Unknown locally — maybe started before this integration deployed.
    try:
        async with httpx.AsyncClient() as client:
            return await _get_sidecar_json(client, f"/api/runs/{run_id}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """Proxy the sidecar's SSE progress feed to the browser."""
    import httpx as _hx

    upstream = _sidecar_url(f"/api/runs/{run_id}/stream")

    async def relay():
        async with _hx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", upstream) as resp:
                if resp.status_code != 200:
                    yield {"event": "error", "data": json.dumps({"error": f"upstream {resp.status_code}"})}
                    return
                event_name = "message"
                async for line in resp.aiter_lines():
                    if not line.strip():
                        event_name = "message"  # blank line = end of this SSE block
                        continue
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip() or "message"
                        continue
                    if line.startswith("data:"):
                        yield {"event": event_name, "data": line.split(":", 1)[1].strip()}

    return EventSourceResponse(relay(), ping=15, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
