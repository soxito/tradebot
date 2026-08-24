"""
Task control API — the System Monitor page's backend.

GET endpoints are open (read-only, like /jarvis/system-stats). Mutations go
through ``require_local_or_key``: loopback callers are allowed, off-localhost
requires ``X-API-Key`` (``TASKS_API_KEY``), and production always requires it.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from loguru import logger

from app.core.security import require_local_or_key
from app.core.task_supervisor import supervisor

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks():
    """Full supervisor snapshot: tier, tasks, paused banner data."""
    return supervisor.snapshot()


@router.get("/{task_id}")
async def get_task(task_id: str):
    info = supervisor.task_info(task_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return info


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: str,
    force: bool = Query(default=False),
    _: bool = Depends(require_local_or_key),
):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    res = supervisor.pause(task_id, by="user", force=force)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason", "cannot pause"))
    return res


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, _: bool = Depends(require_local_or_key)):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return supervisor.resume(task_id)


@router.post("/{task_id}/start")
async def start_task(task_id: str, _: bool = Depends(require_local_or_key)):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return supervisor.start(task_id)


@router.post("/{task_id}/stop")
async def stop_task(task_id: str, _: bool = Depends(require_local_or_key)):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return supervisor.stop(task_id)


@router.post("/{task_id}/run-now")
async def run_now(task_id: str, _: bool = Depends(require_local_or_key)):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return await supervisor.run_now(task_id)


@router.patch("/{task_id}")
async def set_interval(
    task_id: str,
    interval_seconds: Optional[float] = Body(default=None, embed=True),
    _: bool = Depends(require_local_or_key),
):
    if supervisor.task_info(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    supervisor.set_interval(task_id, interval_seconds)
    return supervisor.task_info(task_id)


@router.post("/pause-all")
async def pause_all(
    category: Optional[str] = Query(default=None),
    _: bool = Depends(require_local_or_key),
):
    """Pause all non-critical tasks (optionally filtered to one category)."""
    paused = []
    for t in supervisor.snapshot()["tasks"]:
        if t["critical"]:
            continue
        if category and t["category"] != category:
            continue
        res = supervisor.pause(t["id"], by="user")
        if res.get("ok"):
            paused.append(t["id"])
    return {"paused": paused}


_PRESETS = {
    "battery_saver": {"research", "learning", "enrichment", "realtime"},
    "balanced": {"research", "learning"},
    "full_power": set(),
}


@router.post("/preset/{name}")
async def apply_preset(name: str, _: bool = Depends(require_local_or_key)):
    """Coarse power presets. battery_saver pauses the most; full_power resumes all."""
    if name not in _PRESETS:
        raise HTTPException(status_code=404, detail="Unknown preset")
    pause_categories = _PRESETS[name]
    paused, resumed = [], []
    for t in supervisor.snapshot()["tasks"]:
        if t["critical"]:
            continue
        should_pause = t["category"] in pause_categories
        if should_pause and not t["paused"]:
            if supervisor.pause(t["id"], by="user").get("ok"):
                paused.append(t["id"])
        elif not should_pause and t["paused"] and t["paused_by"] == "user":
            supervisor.resume(t["id"])
            resumed.append(t["id"])
    logger.info(f"[tasks] preset {name}: paused={paused} resumed={resumed}")
    return {"preset": name, "paused": paused, "resumed": resumed}
