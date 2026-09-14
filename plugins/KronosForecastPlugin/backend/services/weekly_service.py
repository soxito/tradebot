"""Weekly combined-model improvement scheduler for the Kronos plugin.

Every Sunday the combined Kronos model is retrained on the latest data, hot-
swapped in as the active model, and the leftover non-combined per-pair jobs are
pruned to free space on the machine.

This module holds no background task of its own — the plugin architecture uses
a FastAPI ``lifespan`` in the (core) ``app/main.py`` which means router
``on_event('startup')`` handlers never fire. Instead ``maybe_run_weekly`` is
invoked idempotently from the already-supervised Telegram monitor loop, and the
day-of-week + once-per-week gate lives here (persisted to a marker file so it
survives restarts).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

_WORK_DIR = Path(__file__).resolve().parents[2] / "backend" / "data" / "finetune"
_MARKER = _WORK_DIR / ".weekly_last.txt"

# Guards against two overlapping cycles + a one-time startup auto-load.
_running = False
_startup_loaded = False


def _read_marker() -> Optional[str]:
    try:
        return _MARKER.read_text().strip() or None
    except OSError:
        return None


def _write_marker(iso_date: str) -> None:
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(iso_date)
    except OSError as exc:  # noqa: BLE001
        logger.debug(f"[Kronos-weekly] could not persist marker: {exc}")


async def ensure_startup_autoload() -> None:
    """Load the newest completed combined model once on first invocation so the
    app + Telegram always forecast through the latest trained combined model."""
    global _startup_loaded
    if _startup_loaded:
        return
    _startup_loaded = True
    try:
        from plugins.KronosForecastPlugin.backend.services import finetune_service
        await finetune_service.finetune_manager.auto_load_latest_combined()
    except Exception as exc:  # noqa: BLE001 — never block the caller's loop
        logger.debug(f"[Kronos-weekly] startup auto-load skipped: {exc}")


async def run_weekly_cycle(force: bool = False) -> Dict:
    """Retrain the combined model, hot-swap it in, then prune non-combined jobs.

    Waits for the training subprocess to finish before pruning (pruning removes
    the per-pair CSVs that a combine reads from). Returns a summary dict.
    """
    global _running
    if _running and not force:
        return {"ok": False, "skipped": "already_running"}
    _running = True
    try:
        from plugins.KronosForecastPlugin.backend.services import finetune_service
        mgr = finetune_service.finetune_manager

        try:
            combine = await mgr.combine_jobs(model_name="combined-weekly")
        except (ValueError, RuntimeError) as exc:
            logger.warning(f"[Kronos-weekly] combine skipped: {exc}")
            return {"ok": False, "error": str(exc)}

        job_id = combine.get("job_id")
        if not job_id:
            return {"ok": False, "error": "combine did not start"}

        # Poll for completion (training can take a while); cap the wait so a
        # stuck trainer never blocks the cycle forever. Auto-load happens inside
        # the pipeline on success.
        deadline = 6 * 60 * 60  # 6h
        waited = 0
        status = "running"
        while waited < deadline:
            await asyncio.sleep(30)
            waited += 30
            job = await mgr.get(job_id, log_lines=0)
            status = (job or {}).get("status", "running")
            if status in {"completed", "failed", "cancelled"}:
                break

        if status != "completed":
            logger.warning(f"[Kronos-weekly] combined job #{job_id} ended as '{status}', skipping prune")
            return {"ok": False, "job_id": job_id, "status": status}

        prune = await mgr.prune_non_combined()
        _write_marker(datetime.now(timezone.utc).date().isoformat())
        logger.success(
            f"[Kronos-weekly] combined model #{job_id} improved + loaded; "
            f"pruned {len(prune.get('removed_job_ids', []))} non-combined job(s)"
        )
        return {"ok": True, "job_id": job_id, "prune": prune,
                "symbols_combined": combine.get("symbols_combined")}
    finally:
        _running = False


async def maybe_run_weekly() -> None:
    """Idempotent gate: run the cycle at most once per Sunday.

    Safe to call every minute from a supervised loop — it returns immediately on
    any non-Sunday or when this week's run already happened.
    """
    await ensure_startup_autoload()
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:  # 6 == Sunday
        return
    today = now.date().isoformat()
    if _read_marker() == today:
        return
    if _running:
        return
    logger.info("[Kronos-weekly] Sunday trigger — improving combined model")
    # Fire-and-forget so the caller's loop is never blocked by training.
    asyncio.create_task(run_weekly_cycle())
    # Stamp immediately so we don't re-trigger while it runs; run_weekly_cycle
    # re-stamps on success (same date) which is harmless.
    _write_marker(today)
