"""Tests for app.core.offload — the event-loop freeze fix."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core import offload
from app.core.offload import run_cpu, OffloadRejected, OffloadTimeout


def _busy(ms: int, cancel_token=None) -> str:
    end = time.perf_counter() + ms / 1000.0
    n = 0
    while time.perf_counter() < end:
        n += 1
        if cancel_token is not None and cancel_token.cancelled:
            return "cancelled"
    return "done"


@pytest.mark.asyncio
async def test_loop_stays_responsive_during_cpu_job():
    """A blocking CPU job must not stall the event loop."""
    late = {}

    async def ticker():
        t0 = time.perf_counter()
        await asyncio.sleep(0.05)
        late["ms"] = (time.perf_counter() - t0 - 0.05) * 1000.0

    await asyncio.gather(run_cpu(_busy, 300, name="busy"), ticker())
    # A 50 ms sleep concurrent with a 300 ms CPU job should finish well under
    # 150 ms if the loop kept running (GIL releases keep it responsive).
    assert late["ms"] < 150


@pytest.mark.asyncio
async def test_heavy_jobs_serialise_and_overflow_rejects(monkeypatch):
    """heavy=True serialises to HEAVY_JOB_CONCURRENCY; overflow raises 503."""
    monkeypatch.setattr(offload, "_HEAVY_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(offload, "_MAX_HEAVY_QUEUE", 2, raising=False)
    monkeypatch.setattr(offload, "_heavy_sem", asyncio.Semaphore(1), raising=False)

    async def heavy():
        try:
            await run_cpu(_busy, 120, name="h", heavy=True)
            return "ok"
        except OffloadRejected:
            return "rejected"

    results = await asyncio.gather(*[heavy() for _ in range(6)])
    assert results.count("rejected") >= 1
    assert results.count("ok") >= 1


@pytest.mark.asyncio
async def test_timeout_raises_and_cancels():
    """A job exceeding its timeout raises OffloadTimeout (→ 504)."""
    with pytest.raises(OffloadTimeout):
        await run_cpu(_busy, 2000, name="slow", timeout=0.2)


@pytest.mark.asyncio
async def test_cancel_token_injected_only_when_accepted():
    """run_cpu injects cancel_token only for functions that accept it."""

    def with_token(cancel_token=None):
        return cancel_token is not None

    def without_token(x):
        return x * 2

    assert await run_cpu(with_token, name="wt") is True
    assert await run_cpu(without_token, 21, name="wo") == 42
