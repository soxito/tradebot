"""
VibeTradingPlugin — FastAPI Router

All routes under /plugins/vibe-trading.
Acts as a bridge between TradeBot's frontend and the vibe-trading REST server.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.VibeTradingPlugin.backend.config import vibe_config
from plugins.VibeTradingPlugin.backend.models import VibeTradingRun, VibeTradingSchedule
from plugins.VibeTradingPlugin.backend.schemas import (
    VibeTradingStatus, RunRow, ResearchRequest, ResearchResponse,
    BacktestRequest, BacktestResponse, SwarmRequest, SwarmResponse,
    AlphaBenchRequest, ShadowAccountResponse, ScheduleCreate, ScheduleRow,
    EnrichSignalRequest, EnrichSignalResponse,
)
from plugins.VibeTradingPlugin.backend.services import vibe_client, sidecar_manager
from plugins.VibeTradingPlugin.backend.services.signal_exporter import (
    build_context_from_signals, build_swarm_variables,
)

router = APIRouter(prefix="/plugins/vibe-trading", tags=["Vibe Trading"])


# ── DB dependency ────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── MCP tool manifest (for OpenHuman / agent.json) ─────────────────────────

_MCP_SCHEMA = {
    "name": "tradebot",
    "description": "TradeBot trading intelligence tools",
    "tools": [
        {
            "name": "tradebot_get_signals",
            "description": "Get recent TradeBot trading signals",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
        {
            "name": "tradebot_get_forecast",
            "description": "Get Kronos ML price forecast for a symbol",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "exchange": {"type": "string", "default": "bitget"},
                },
            },
        },
        {
            "name": "tradebot_get_position",
            "description": "Get open Bitget futures position for a symbol",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {"symbol": {"type": "string"}},
            },
        },
        {
            "name": "tradebot_get_smc_analysis",
            "description": "Get Smart Money Concepts bias for a symbol",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1h"},
                },
            },
        },
        {
            "name": "tradebot_ask_jarvis",
            "description": "Ask JARVIS to analyze a symbol or answer a trading question",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        },
    ],
}


# ── Status ──────────────────────────────────────────────────────────────────

@router.get("/status", response_model=VibeTradingStatus)
async def get_status():
    reachable = await vibe_client.is_reachable()
    if reachable:
        health = await vibe_client.health()
        version = health.get("version") if isinstance(health, dict) else None
        return VibeTradingStatus(reachable=True, version=version, sidecar_running=True)
    return VibeTradingStatus(reachable=False, sidecar_running=False,
                             message="vibe-trading serve not running. POST /plugins/vibe-trading/status/start to launch.")


@router.post("/status/start")
async def start_sidecar():
    ok = await sidecar_manager.ensure_started()
    return {"started": ok, "reachable": ok}


# ── Runs ─────────────────────────────────────────────────────────────────────

@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    stmt = select(VibeTradingRun).order_by(desc(VibeTradingRun.created_at)).limit(100)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_run_row(r) for r in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    # Try local DB first, then proxy to vibe-trading
    return await vibe_client.get_run(run_id)


@router.get("/runs/{run_id}/pine")
async def get_run_pine(run_id: str):
    return await vibe_client.get_run_pine(run_id)


# ── Research ─────────────────────────────────────────────────────────────────

@router.post("/research", response_model=ResearchResponse)
async def do_research(req: ResearchRequest, db: AsyncSession = Depends(get_db)):
    await sidecar_manager.ensure_started()
    context = await build_context_from_signals(req.symbol)
    full_prompt = context + req.prompt

    run = VibeTradingRun(
        run_type="research",
        symbol=req.symbol,
        prompt=req.prompt,
        status="running",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    result = await vibe_client.research(full_prompt, symbol=req.symbol)

    run.status = "failed" if isinstance(result, dict) and "error" in result else "completed"
    run.result_summary = _summarize(result)
    if isinstance(result, dict):
        run.remote_run_id = result.get("run_id") or result.get("id")
    await db.commit()

    return ResearchResponse(
        run_id=run.id,
        remote_run_id=run.remote_run_id,
        status=run.status,
        result=result,
    )


# ── Backtest ─────────────────────────────────────────────────────────────────

@router.post("/backtest", response_model=BacktestResponse)
async def do_backtest(req: BacktestRequest, db: AsyncSession = Depends(get_db)):
    await sidecar_manager.ensure_started()

    run = VibeTradingRun(
        run_type="backtest",
        symbol=req.symbol,
        prompt=req.strategy,
        status="running",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    result = await vibe_client.backtest(req.symbol, req.strategy, req.timeframe)

    run.status = "failed" if isinstance(result, dict) and "error" in result else "completed"
    run.result_summary = _summarize(result)
    if isinstance(result, dict):
        run.remote_run_id = result.get("run_id") or result.get("id")
    await db.commit()

    return BacktestResponse(
        run_id=run.id,
        remote_run_id=run.remote_run_id,
        status=run.status,
        result=result,
    )


# ── Swarm ─────────────────────────────────────────────────────────────────────

@router.get("/swarm/presets")
async def list_swarm_presets():
    await sidecar_manager.ensure_started()
    return await vibe_client.list_swarm_presets()


@router.post("/swarm", response_model=SwarmResponse)
async def run_swarm(req: SwarmRequest, db: AsyncSession = Depends(get_db)):
    await sidecar_manager.ensure_started()
    variables = await build_swarm_variables(req.symbol, req.preset)
    if req.extra:
        variables.update(req.extra)

    run = VibeTradingRun(
        run_type="swarm",
        symbol=req.symbol,
        prompt=f"[{req.preset}] {req.symbol}",
        status="running",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    result = await vibe_client.run_swarm(req.preset, variables)

    run.status = "failed" if isinstance(result, dict) and "error" in result else "completed"
    run.result_summary = _summarize(result)
    if isinstance(result, dict):
        run.remote_run_id = result.get("run_id") or result.get("id")
    await db.commit()

    return SwarmResponse(
        run_id=run.id,
        remote_run_id=run.remote_run_id,
        status=run.status,
        result=result,
    )


# ── Alpha Zoo ──────────────────────────────────────────────────────────────────

@router.get("/alpha/list")
async def list_alphas(
    zoo: Optional[str] = Query(None),
    theme: Optional[str] = Query(None),
    limit: int = Query(50),
):
    await sidecar_manager.ensure_started()
    params = {"limit": limit}
    if zoo:
        params["zoo"] = zoo
    if theme:
        params["theme"] = theme
    return await vibe_client.list_alphas(params)


@router.post("/alpha/bench")
async def bench_alphas(req: AlphaBenchRequest, db: AsyncSession = Depends(get_db)):
    await sidecar_manager.ensure_started()
    payload = req.model_dump(exclude_none=True)

    run = VibeTradingRun(
        run_type="alpha_bench",
        prompt=json.dumps(payload),
        status="running",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    result = await vibe_client.bench_alphas(payload)
    run.status = "failed" if isinstance(result, dict) and "error" in result else "completed"
    run.result_summary = _summarize(result)
    await db.commit()

    return {"run_id": run.id, "result": result}


# ── Shadow Account ────────────────────────────────────────────────────────────

@router.post("/shadow-account", response_model=ShadowAccountResponse)
async def shadow_account(db: AsyncSession = Depends(get_db)):
    """Analyze uploaded broker export — file must be provided as multipart upload."""
    return ShadowAccountResponse(
        status="info",
        result={
            "message": "Upload your broker CSV via the frontend file picker, then POST /shadow-account/analyze"
        },
    )


# ── Scheduled Research ─────────────────────────────────────────────────────────

@router.post("/scheduled")
async def create_scheduled(req: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    await sidecar_manager.ensure_started()
    result = await vibe_client.create_scheduled_run(req.prompt, req.schedule, req.config)

    sched = VibeTradingSchedule(
        remote_job_id=result.get("job_id") if isinstance(result, dict) else None,
        prompt=req.prompt,
        schedule=req.schedule,
        symbol=req.symbol,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)

    return {"id": sched.id, "remote": result}


@router.get("/scheduled")
async def list_scheduled(db: AsyncSession = Depends(get_db)):
    stmt = select(VibeTradingSchedule).order_by(desc(VibeTradingSchedule.created_at))
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_sched_row(r) for r in rows]


@router.delete("/scheduled/{job_id}")
async def cancel_scheduled(job_id: str):
    await sidecar_manager.ensure_started()
    return await vibe_client.delete_scheduled_run(job_id)


# ── Signal Enrichment ──────────────────────────────────────────────────────────

@router.post("/enrich/signal", response_model=EnrichSignalResponse)
async def enrich_signal(req: EnrichSignalRequest):
    """
    Parallel enrichment: Kronos forecast + SMC bias + short Vibe-Trading research.
    """
    import asyncio

    kronos_summary: Optional[str] = None
    smc_bias: Optional[str] = None
    vibe_note: Optional[str] = None

    async def fetch_kronos():
        nonlocal kronos_summary
        try:
            from plugins.KronosForecastPlugin.backend.services.forecast_service import run_forecast_cached
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                fc = await run_forecast_cached(db=db, exchange="bitget", symbol=req.symbol,
                                               timeframe=req.timeframe or "1h")
                if fc and fc.signal:
                    kronos_summary = (
                        f"{fc.signal.direction} {fc.signal.pct_change:+.1f}% "
                        f"confidence={fc.signal.confidence:.0%}: {fc.signal.summary}"
                    )
        except Exception as exc:
            kronos_summary = f"unavailable ({exc})"

    async def fetch_smc():
        nonlocal smc_bias
        try:
            from app.signals.service import SignalService
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                svc = SignalService(db)
                overview = await svc.get_smc_overview(symbol=req.symbol, timeframe=req.timeframe or "1h")
                if overview:
                    smc_bias = str(overview.get("bias", "neutral"))
        except Exception as exc:
            smc_bias = f"unavailable ({exc})"

    async def fetch_vibe():
        nonlocal vibe_note
        try:
            result = await vibe_client.research(
                f"Brief 2-sentence market view for {req.symbol} on {req.timeframe} timeframe.",
                symbol=req.symbol,
            )
            if isinstance(result, dict) and "error" not in result:
                content = result.get("content") or result.get("message") or str(result)[:300]
                vibe_note = content
        except Exception:
            pass

    await asyncio.gather(fetch_kronos(), fetch_smc(), fetch_vibe())

    parts = []
    if kronos_summary:
        parts.append(f"Kronos: {kronos_summary}")
    if smc_bias:
        parts.append(f"SMC Bias: {smc_bias}")
    if vibe_note:
        parts.append(f"Vibe Research: {vibe_note}")

    return EnrichSignalResponse(
        symbol=req.symbol,
        kronos_summary=kronos_summary,
        smc_bias=smc_bias,
        vibe_research=vibe_note,
        combined_note=" | ".join(parts) if parts else None,
    )


# ── MCP Schema (for ~/.vibe-trading/agent.json) ─────────────────────────────

@router.get("/mcp/schema")
async def mcp_schema():
    return _MCP_SCHEMA


# ── Helpers ───────────────────────────────────────────────────────────────────

def _summarize(result: Any, max_len: int = 500) -> Optional[str]:
    if result is None:
        return None
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        s = str(result)
    return s[:max_len]


def _run_row(r: VibeTradingRun) -> dict:
    return {
        "id": r.id,
        "remote_run_id": r.remote_run_id,
        "run_type": r.run_type,
        "symbol": r.symbol,
        "prompt": r.prompt,
        "status": r.status,
        "result_summary": r.result_summary,
        "pine_script": r.pine_script,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _sched_row(r: VibeTradingSchedule) -> dict:
    return {
        "id": r.id,
        "remote_job_id": r.remote_job_id,
        "prompt": r.prompt,
        "schedule": r.schedule,
        "symbol": r.symbol,
        "active": r.active,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
