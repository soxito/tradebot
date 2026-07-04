"""
Agent Paul — Auto-fetch loop (OpenHuman-style 15-minute memory sync)

A single background task that periodically folds fresh signal into JARVIS's
Memory Tree so a new chat already has today's context:

  1. Ingest live news → PaulKnowledge (market_predictor.ingest_news_to_knowledge)
  2. Import recent knowledge rows → Memory Tree chunk nodes (deduped, scored)
  3. Roll the day's chunks up into a `daily` summary node

Read-only with respect to trading — it never places orders. Started lazily via
``ensure_started()`` from a router dependency (the app uses lifespan, so
router ``on_event('startup')`` does not fire).
"""
from __future__ import annotations

import asyncio

from loguru import logger

INTERVAL_SECONDS = 15 * 60  # 15 minutes


class PaulAutoFetch:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._session_factory = None
        self.last_run_at: str | None = None
        self.last_summary: dict | None = None

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def ensure_started(self, session_factory) -> None:
        self._session_factory = session_factory
        if self.is_running():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())
        self._running = True
        logger.info("🧠 Paul auto-fetch loop started (every {}s)", INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def run_once(self) -> dict:
        """One fetch → memory cycle. Safe to call on demand."""
        from app.core.timezone import now_sast
        from plugins.AgentPaulPlugin.backend.services import memory_tree
        from plugins.AgentPaulPlugin.backend.services.market_predictor import (
            ingest_news_to_knowledge,
        )
        summary = {"news_ingested": 0, "memories_added": 0, "rolled_up": False}
        if self._session_factory is None:
            from app.core.database import AsyncSessionLocal as _S
            self._session_factory = _S
        try:
            async with self._session_factory() as db:
                try:
                    summary["news_ingested"] = await ingest_news_to_knowledge(db, max_items=40)
                except Exception as exc:  # noqa
                    logger.debug("[AutoFetch] news ingest skipped: {}", exc)
                try:
                    summary["memories_added"] = await memory_tree.import_knowledge(db, limit=80)
                except Exception as exc:  # noqa
                    logger.debug("[AutoFetch] memory import skipped: {}", exc)
                try:
                    node = await memory_tree.rollup(db)
                    summary["rolled_up"] = node is not None
                except Exception as exc:  # noqa
                    logger.debug("[AutoFetch] rollup skipped: {}", exc)
            self.last_run_at = now_sast().isoformat()
            self.last_summary = summary
            logger.info("🧠 Paul auto-fetch cycle: {}", summary)
        except Exception as exc:  # noqa
            logger.warning("[AutoFetch] cycle failed: {}", exc)
        return summary

    async def _loop(self) -> None:
        await asyncio.sleep(20)  # let the app finish starting
        while self._running:
            await self.run_once()
            try:
                await asyncio.sleep(INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "interval_seconds": INTERVAL_SECONDS,
            "last_run_at": self.last_run_at,
            "last_summary": self.last_summary,
        }


auto_fetch = PaulAutoFetch()
