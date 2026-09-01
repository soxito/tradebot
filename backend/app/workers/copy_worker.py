"""
Copy-trading worker — drives every enabled copy profile on a fixed clock.

Each cycle runs one position-diff pass per enabled profile:

  SIM  profiles → mirror source positions into the paper ledger
  LIVE profiles → open/close real orders on each follower account

Runs alongside the room worker and is started by workers.runtime.
"""
from __future__ import annotations

import asyncio

from loguru import logger

from app.core.database import AsyncSessionLocal

_task: asyncio.Task | None = None
_running = False
_interval = 30.0


async def _loop() -> None:
    # Import lazily so worker processes that never touch MT5 don't pay for it.
    from plugins.MT5TradingPlugin.backend.services.copy_executor import run_all_profiles

    logger.info(f"🤝 [COPY WORKER] started (interval {_interval:.0f}s)")
    while _running:
        try:
            async with AsyncSessionLocal() as db:
                results = await run_all_profiles(db)
            if results:
                logger.debug(f"🤝 [COPY WORKER] synced {len(results)} profiles")
        except Exception as e:  # noqa: BLE001 — the loop must survive anything
            logger.error(f"🤝 [COPY WORKER] cycle failed: {e}")

        # Room supervision pass — cheap no-op unless manage_copy_profiles is on.
        try:
            from app.agents import copy_supervisor

            async with AsyncSessionLocal() as db:
                outcome = await copy_supervisor.run_once(db)
            if outcome.get("supervised") and outcome.get("actions"):
                logger.warning(f"🤝 [COPY SUPERVISOR] actions: {outcome['actions']}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"🤝 [COPY SUPERVISOR] cycle failed: {e}")

        await asyncio.sleep(_interval)


def start_copy_worker(interval_seconds: float = 30.0) -> bool:
    """Start the copy worker loop. Returns True if it was started now."""
    global _task, _running, _interval
    if _running:
        return False
    _interval = max(10.0, float(interval_seconds))
    _running = True
    _task = asyncio.ensure_future(_loop())
    return True


def stop_copy_worker() -> None:
    global _task, _running
    _running = False
    if _task is not None:
        _task.cancel()
        _task = None
