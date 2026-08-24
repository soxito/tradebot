"""
System Monitor API — read-only host, event-loop and offload metrics.

Surfaces the Phase 0 measurement work (host resources + event-loop lag probe)
and the Phase 2 offload pool stats so the freeze can be observed and the fix
proven. All endpoints are GET and safe to expose (equivalent to the existing
public ``/jarvis/system-stats``).
"""
from __future__ import annotations

from fastapi import APIRouter
from loguru import logger

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/resources")
async def system_resources():
    """Host + backend process footprint, event-loop lag and offload queue."""
    from app.services.system_resources import host_snapshot, process_snapshot
    from app.core.config import settings

    payload = {
        "tier": settings.PERF_TIER,
        "profile": settings.TRADEBOT_PROFILE or None,
        "host": host_snapshot(),
        "process": process_snapshot(),
    }
    try:
        from app.core.loop_monitor import loop_monitor
        payload["loop_lag"] = loop_monitor.snapshot()
    except Exception as e:
        payload["loop_lag"] = {"available": False, "reason": str(e)}
    try:
        from app.core.offload import stats as offload_stats
        payload["offload"] = offload_stats()
    except Exception as e:
        payload["offload"] = {"available": False, "reason": str(e)}
    return payload


@router.get("/loop-lag")
async def loop_lag():
    """Event-loop lag probe — the decisive freeze metric (p50/p95/max ms)."""
    try:
        from app.core.loop_monitor import loop_monitor
        return loop_monitor.snapshot()
    except Exception as e:
        logger.debug(f"[system] loop-lag error: {e}")
        return {"available": False, "reason": str(e)}


@router.get("/offload")
async def offload_metrics():
    """CPU offload pool — in-flight jobs, heavy queue depth, rejections."""
    try:
        from app.core.offload import stats as offload_stats
        return offload_stats()
    except Exception as e:
        return {"available": False, "reason": str(e)}


@router.get("/caches")
async def cache_metrics():
    """Registered TTL caches — size, hit rate, TTL (for the monitor + watchdog)."""
    try:
        from app.core.cache import all_stats
        return {"caches": all_stats()}
    except Exception as e:
        return {"caches": [], "reason": str(e)}
