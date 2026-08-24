"""
Event-loop lag probe — the decisive metric for the whole-app freeze.

A tiny task wakes every ``interval`` seconds and records how late it actually
woke (``actual_wake - expected_wake``). When a synchronous CPU job (e.g. an
un-offloaded backtest) blocks the single asyncio loop, that lag spikes into
*seconds*. After the offload work (Phase 2) it should stay < 50 ms.

Keeps p50 / p95 / max over a rolling window so the System Monitor page and the
verification runs can prove responsiveness without a profiler.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from loguru import logger

_INTERVAL_S = 1.0
# 5 minutes of 1 s samples.
_WINDOW = 300


class LoopMonitor:
    def __init__(self, interval_s: float = _INTERVAL_S, window: int = _WINDOW) -> None:
        self.interval_s = interval_s
        self._samples: Deque[float] = deque(maxlen=window)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.max_ever_ms = 0.0

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval_s
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                break
            now = loop.time()
            lag_ms = max(0.0, (now - expected) * 1000.0)
            self._samples.append(lag_ms)
            if lag_ms > self.max_ever_ms:
                self.max_ever_ms = lag_ms
            expected = now + self.interval_s

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        logger.info("[loop_monitor] event-loop lag probe started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def snapshot(self) -> Dict[str, Any]:
        samples = sorted(self._samples)
        n = len(samples)
        if n == 0:
            return {"available": False, "samples": 0}

        def pct(p: float) -> float:
            idx = min(n - 1, int(round((p / 100.0) * (n - 1))))
            return round(samples[idx], 2)

        return {
            "available": True,
            "samples": n,
            "p50_ms": pct(50),
            "p95_ms": pct(95),
            "max_ms": round(samples[-1], 2),
            "max_ever_ms": round(self.max_ever_ms, 2),
            "window_seconds": int(n * self.interval_s),
        }


# Module-level singleton so the API layer and lifespan share one probe.
loop_monitor = LoopMonitor()
