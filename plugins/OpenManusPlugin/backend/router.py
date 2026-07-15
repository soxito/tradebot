"""OpenManusPlugin — FastAPI Router.

Routes under /plugins/openmanus.

Provides:
 - GET  /status          — OpenManus reachability + install status
 - GET  /health          — quick liveness probe
 - POST /install         — clone/update OpenManus
 - POST /start           — start MCP server subprocess
 - POST /stop            — stop MCP server subprocess
 - POST /chat            — single-shot adapter chat (for testing)
 - GET  /calls           — call log (audit/telemetry)
 - GET  /calls/stats     — aggregated routing stats
 - GET  /compliance      — Phase 5: routing compliance report
 - GET  /config          — Phase 6: runtime mode flags + promotion gates
 - GET  /runbook         — Phase 7: operational runbook and promotion status
"""
from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.OpenManusPlugin.backend.config import openmanus_config
from plugins.OpenManusPlugin.backend.models import OpenManusCallLog, RouteSource
from plugins.OpenManusPlugin.backend.schemas import (
    AdapterChatRequest,
    AdapterChatResponse,
    CallLogRow,
    CallStats,
    OpenManusHealth,
    OpenManusInstallStatus,
    OpenManusStartRequest,
    OpenManusStartResponse,
)
from plugins.OpenManusPlugin.backend.services import install_service
from plugins.OpenManusPlugin.backend.services.adapter import openmanus_chat
from plugins.OpenManusPlugin.backend.services.openmanus_client import mcp_health

router = APIRouter(prefix="/plugins/openmanus", tags=["OpenManus"])


# ── DB dependency ──────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Status / Health ────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    """Quick liveness probe — does not require DB."""
    reachable = await mcp_health()
    return {"ok": reachable, "mcp_url": openmanus_config.sse_url}


@router.get("/status", response_model=OpenManusHealth)
async def status():
    """Full status: MCP reachability + install info + config flags."""
    reachable = await mcp_health()
    installed = install_service.is_installed()
    error: Optional[str] = None
    if not reachable and installed and install_service.server_running():
        error = "MCP server process is running but not responding"

    return OpenManusHealth(
        mcp_reachable=reachable,
        mcp_url=openmanus_config.sse_url,
        installed=installed,
        install_dir=str(install_service._install_dir()),
        enabled=openmanus_config.enabled,
        fallback_enabled=openmanus_config.fallback_enabled,
        error=error,
    )


@router.get("/install/status", response_model=OpenManusInstallStatus)
async def install_status():
    """Return whether OpenManus is installed and its commit hash."""
    installed = install_service.is_installed()
    return OpenManusInstallStatus(
        installed=installed,
        install_dir=str(install_service._install_dir()),
        version=install_service.installed_version() if installed else None,
    )


# ── Install / Start / Stop ────────────────────────────────────────────────────

@router.post("/install")
async def install():
    """Clone or update OpenManus from GitHub.

    This is an async long-running operation.
    """
    result = await install_service.install()
    return result


@router.post("/start", response_model=OpenManusStartResponse)
async def start(request: OpenManusStartRequest = OpenManusStartRequest()):
    """Start the OpenManus MCP server as a background subprocess."""
    result = await install_service.start_mcp_server(
        transport=request.transport,
        port=request.port,
    )
    return OpenManusStartResponse(
        started=result.get("ok", False),
        pid=result.get("pid"),
        url=result.get("url"),
        error=result.get("error"),
    )


@router.post("/stop")
async def stop():
    """Stop the managed OpenManus MCP server subprocess."""
    stopped = install_service.stop_mcp_server()
    return {"stopped": stopped}


# ── Adapter Chat (manual / test) ──────────────────────────────────────────────

@router.post("/chat", response_model=AdapterChatResponse)
async def adapter_chat(
    request: AdapterChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Route a chat through the OpenManus adapter.

    Primarily for testing the adapter from the frontend or curl.
    Logs to openmanus_call_log.
    """
    result = await openmanus_chat(
        db=db,
        messages=request.messages,
        flow=request.flow,
        agent_name=request.agent_name,
        source=request.source,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        json_mode=request.json_mode,
    )
    return AdapterChatResponse(
        ok=result["ok"],
        content=result.get("content"),
        route_source=result.get("route_source", "error"),
        provider=result.get("provider"),
        model=result.get("model"),
        usage=result.get("usage"),
        latency_ms=result.get("latency_ms"),
        error=result.get("error"),
    )


# ── Audit Log ─────────────────────────────────────────────────────────────────

@router.get("/calls", response_model=list[CallLogRow])
async def get_calls(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    flow: Optional[str] = Query(None),
    route_source: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return recent OpenManus adapter call log entries."""
    q = select(OpenManusCallLog).order_by(OpenManusCallLog.id.desc())
    if flow:
        q = q.where(OpenManusCallLog.flow == flow)
    if route_source:
        try:
            rs = RouteSource(route_source)
            q = q.where(OpenManusCallLog.route_source == rs)
        except ValueError:
            pass
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [CallLogRow.model_validate(r) for r in rows]


@router.get("/calls/stats", response_model=CallStats)
async def get_call_stats(db: AsyncSession = Depends(get_db)):
    """Return aggregate routing statistics."""
    total_q = await db.execute(select(func.count(OpenManusCallLog.id)))
    total = total_q.scalar() or 0

    om_q = await db.execute(
        select(func.count(OpenManusCallLog.id))
        .where(OpenManusCallLog.route_source == RouteSource.openmanus)
    )
    om_count = om_q.scalar() or 0

    fb_q = await db.execute(
        select(func.count(OpenManusCallLog.id))
        .where(OpenManusCallLog.route_source == RouteSource.fallback)
    )
    fb_count = fb_q.scalar() or 0

    err_q = await db.execute(
        select(func.count(OpenManusCallLog.id))
        .where(OpenManusCallLog.route_source == RouteSource.error)
    )
    err_count = err_q.scalar() or 0

    lat_q = await db.execute(
        select(func.avg(OpenManusCallLog.latency_ms))
        .where(OpenManusCallLog.success.is_(True))
    )
    avg_lat = lat_q.scalar()

    return CallStats(
        total=total,
        openmanus_routed=om_count,
        fallback_routed=fb_count,
        errors=err_count,
        openmanus_success_rate=round(om_count / max(1, total), 4),
        fallback_rate=round(fb_count / max(1, total), 4),
        avg_latency_ms=round(avg_lat, 2) if avg_lat is not None else None,
    )


# ── Phase 5: Compliance report ────────────────────────────────────────────────

@router.get("/compliance")
async def get_compliance_report():
    """Phase 5 — Check routing compliance across all plugin files.

    Scans for direct `_call_openai_compatible` / `get_enabled_providers` usage
    outside the allowlisted files. Returns a pass/fail report.
    """
    import subprocess, sys
    check_script = (
        __file__  # this file is plugins/OpenManusPlugin/backend/router.py
        .replace("backend/router.py", "")
        .replace("backend\\router.py", "")
    )
    # Build path to the compliance script
    import os
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    script = os.path.join(repo_root, "scripts", "check_openmanus_compliance.py")

    if not os.path.exists(script):
        return {"ok": False, "error": "Compliance script not found", "violations": []}

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        passed = result.returncode == 0
        return {
            "ok": passed,
            "output": result.stdout.strip(),
            "errors": result.stderr.strip() or None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Phase 6: Runtime config and mode flags ────────────────────────────────────

@router.get("/config")
async def get_runtime_config():
    """Phase 6 — Return current runtime config and mode flags.

    Shows which OpenManus runtime modes are enabled and the Phase 7
    promotion gate thresholds.
    """
    return {
        "routing": {
            "enabled": openmanus_config.enabled,
            "fallback_enabled": openmanus_config.fallback_enabled,
            "timeout_s": openmanus_config.timeout_s,
            "mcp_url": openmanus_config.sse_url,
        },
        "runtime_modes": {
            "mcp_mode": True,  # always on when enabled=True
            "main_mode_enabled": openmanus_config.main_mode_enabled,
            "flow_mode_enabled": openmanus_config.flow_mode_enabled,
        },
        "promotion_gates": {
            "min_success_rate": openmanus_config.promotion_min_success_rate,
            "max_fallback_rate": openmanus_config.promotion_max_fallback_rate,
        },
        "env_overrides": {
            "OPENMANUS_ENABLED": "Set to 'false' to disable OpenManus routing (instant rollback)",
            "OPENMANUS_FALLBACK_ENABLED": "Set to 'false' to disable fallback (stricter mode)",
            "OPENMANUS_MAIN_MODE_ENABLED": "Set to 'true' to enable main-agent mode (Phase 6)",
            "OPENMANUS_FLOW_MODE_ENABLED": "Set to 'true' to enable flow-mode (Phase 6)",
            "TRADEBOT_SKIP_OPENMANUS_SETUP": "Set to '1' to skip install at startup",
        },
    }


# ── Phase 7: Operational runbook and promotion status ─────────────────────────

@router.get("/runbook")
async def get_runbook(db: AsyncSession = Depends(get_db)):
    """Phase 7 — Operational runbook with current promotion readiness.

    Computes current OpenManus success rate and fallback rate against the
    promotion gate thresholds, and returns the full operational runbook.
    """
    # Compute current stats
    total_q = await db.execute(select(func.count(OpenManusCallLog.id)))
    total = total_q.scalar() or 0

    om_q = await db.execute(
        select(func.count(OpenManusCallLog.id))
        .where(OpenManusCallLog.route_source == RouteSource.openmanus)
    )
    om_count = om_q.scalar() or 0

    fb_q = await db.execute(
        select(func.count(OpenManusCallLog.id))
        .where(OpenManusCallLog.route_source == RouteSource.fallback)
    )
    fb_count = fb_q.scalar() or 0

    success_rate = round(om_count / max(1, total), 4)
    fallback_rate = round(fb_count / max(1, total), 4)

    promotion_ready = (
        total >= 50  # minimum sample size
        and success_rate >= openmanus_config.promotion_min_success_rate
        and fallback_rate <= openmanus_config.promotion_max_fallback_rate
    )

    return {
        "promotion_status": {
            "ready": promotion_ready,
            "total_calls": total,
            "openmanus_success_rate": success_rate,
            "fallback_rate": fallback_rate,
            "min_success_rate_required": openmanus_config.promotion_min_success_rate,
            "max_fallback_rate_allowed": openmanus_config.promotion_max_fallback_rate,
            "min_sample_size": 50,
        },
        "current_mode": {
            "routing": "openmanus-primary" if openmanus_config.enabled else "fallback-only",
            "fallback": "enabled" if openmanus_config.fallback_enabled else "disabled (strict)",
        },
        "rollback": {
            "instant_disable": "Set OPENMANUS_ENABLED=false and restart backend",
            "restart_cmd": "lsof -ti :1448 | xargs kill -9; ./run-local.sh backend --brew",
        },
        "install": {
            "clone": "POST /api/v1/plugins/openmanus/install",
            "start": "POST /api/v1/plugins/openmanus/start",
            "health": "GET /api/v1/plugins/openmanus/health",
        },
        "monitoring": {
            "stats": "GET /api/v1/plugins/openmanus/calls/stats",
            "log": "GET /api/v1/plugins/openmanus/calls?limit=50",
            "compliance": "GET /api/v1/plugins/openmanus/compliance",
        },
        "promotion": {
            "how_to_promote": (
                "When promotion_status.ready=true, set OPENMANUS_FALLBACK_ENABLED=false "
                "in .env to enter stricter mode (OpenManus failure → error, no fallback). "
                "Monitor for 24h before permanent promotion."
            ),
        },
    }
