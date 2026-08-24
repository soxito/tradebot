"""
CPU offload — keep the event loop responsive under heavy synchronous work.

The SMC engine's ``analyze()`` / ``backtest()`` are heavy *pure-Python* routines
that, when called directly inside an ``async def`` handler, block the single
asyncio event loop for the whole app — stalling every background loop and every
HTTP request at once. That is the "whole-app freeze".

This module offloads such work to a **shared, bounded** thread pool. We use a
thread pool (not ``asyncio.to_thread``, whose default executor is unbounded and
shared) so N simultaneous backtests queue rather than each spawning a fresh
pandas working set. We deliberately avoid ``ProcessPoolExecutor``: the engine
carries DB-loaded ``factor_weights`` (must pickle), ``fork()`` on macOS with
torch/MPS resident in the parent is a known crash source, and +N interpreters of
RSS is exactly the memory pressure we are trying to relieve.

The GIL releases roughly every 5 ms, so while a heavy job runs in a worker
thread the event loop keeps servicing everything else — that is the goal:
**loop responsiveness**, not raw throughput.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from loguru import logger


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Heavy-job concurrency scales with the resource tier via the env contract set
# by start.py (TRADEBOT_TIER). Low-memory machines run one backtest at a time.
def _default_heavy_concurrency() -> int:
    override = _int_env("TRADEBOT_HEAVY_JOB_CONCURRENCY", 0)
    if override:
        return override
    tier = (os.environ.get("TRADEBOT_PROFILE") or os.environ.get("TRADEBOT_TIER") or "high").strip().lower()
    return {"minimal": 1, "low": 1, "medium": 1, "high": 2, "ultra": 2}.get(tier, 2)


_MAX_WORKERS = _int_env("TRADEBOT_OFFLOAD_WORKERS", 4)
_HEAVY_CONCURRENCY = _default_heavy_concurrency()
# Reject rather than hang once the heavy queue backs up this far.
_MAX_HEAVY_QUEUE = _int_env("TRADEBOT_HEAVY_QUEUE_MAX", 4)


class OffloadRejected(Exception):
    """Raised when the heavy-job queue is saturated. Callers should map to 503."""


class OffloadTimeout(Exception):
    """Raised when a job exceeds its timeout. Callers should map to 504."""


class CancelToken:
    """Cooperative cancel flag checked inside long CPU loops.

    Threads cannot be force-killed, so heavy routines (e.g. the SMC backtest
    walk-forward loop) poll ``cancelled`` and bail out cleanly when a timeout or
    client disconnect trips it.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError("offload job cancelled")


@dataclass
class _OffloadStats:
    in_flight: int = 0
    heavy_in_flight: int = 0
    heavy_queue_depth: int = 0
    total_jobs: int = 0
    total_heavy: int = 0
    total_rejected: int = 0
    total_timeouts: int = 0
    cumulative_cpu_ms: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "in_flight": self.in_flight,
                "heavy_in_flight": self.heavy_in_flight,
                "heavy_queue_depth": self.heavy_queue_depth,
                "total_jobs": self.total_jobs,
                "total_heavy": self.total_heavy,
                "total_rejected": self.total_rejected,
                "total_timeouts": self.total_timeouts,
                "cumulative_cpu_ms": round(self.cumulative_cpu_ms, 1),
                "max_workers": _MAX_WORKERS,
                "heavy_concurrency": _HEAVY_CONCURRENCY,
                "heavy_queue_max": _MAX_HEAVY_QUEUE,
            }


_stats = _OffloadStats()
_executor: Optional[ThreadPoolExecutor] = None
_heavy_sem: Optional[asyncio.Semaphore] = None
_heavy_waiters = 0
_heavy_waiters_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="offload"
        )
        logger.info(
            f"[offload] thread pool up: workers={_MAX_WORKERS} "
            f"heavy_concurrency={_HEAVY_CONCURRENCY} heavy_queue_max={_MAX_HEAVY_QUEUE}"
        )
    return _executor


def _get_heavy_sem() -> asyncio.Semaphore:
    global _heavy_sem
    if _heavy_sem is None:
        _heavy_sem = asyncio.Semaphore(_HEAVY_CONCURRENCY)
    return _heavy_sem


async def run_cpu(
    fn: Callable[..., Any],
    *args: Any,
    name: str = "cpu",
    heavy: bool = False,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> Any:
    """Run a blocking CPU function off the event loop.

    ``heavy=True`` acquires a bounded semaphore so N simultaneous heavy jobs
    (backtests) queue instead of all spawning at once; the queue is capped and
    overflow raises :class:`OffloadRejected` (→ 503) rather than hanging.
    ``timeout`` bounds a single job (→ :class:`OffloadTimeout` → 504).

    A :class:`CancelToken` is injected as the ``cancel_token`` kwarg **only if**
    the target function accepts it, so existing signatures are untouched.
    """
    loop = asyncio.get_running_loop()
    executor = _get_executor()

    if heavy:
        global _heavy_waiters
        with _heavy_waiters_lock:
            if _heavy_waiters >= _MAX_HEAVY_QUEUE:
                _stats.total_rejected += 1
                raise OffloadRejected(
                    f"{name}: heavy queue full ({_heavy_waiters} waiting)"
                )
            _heavy_waiters += 1
            _stats.heavy_queue_depth = _heavy_waiters

        sem = _get_heavy_sem()
        try:
            await sem.acquire()
        finally:
            with _heavy_waiters_lock:
                _heavy_waiters -= 1
                _stats.heavy_queue_depth = _heavy_waiters
    else:
        sem = None

    token: Optional[CancelToken] = None
    if _accepts_cancel_token(fn):
        token = CancelToken()
        kwargs["cancel_token"] = token

    with _stats._lock:
        _stats.in_flight += 1
        _stats.total_jobs += 1
        if heavy:
            _stats.heavy_in_flight += 1
            _stats.total_heavy += 1

    start = time.perf_counter()
    try:
        future = loop.run_in_executor(executor, lambda: fn(*args, **kwargs))
        if timeout is not None:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout)
            except asyncio.TimeoutError:
                if token is not None:
                    token.cancel()  # let the worker unwind cooperatively
                _stats.total_timeouts += 1
                raise OffloadTimeout(f"{name}: exceeded {timeout}s") from None
        else:
            result = await future
        return result
    except asyncio.CancelledError:
        if token is not None:
            token.cancel()
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        with _stats._lock:
            _stats.in_flight = max(0, _stats.in_flight - 1)
            _stats.cumulative_cpu_ms += elapsed_ms
            if heavy:
                _stats.heavy_in_flight = max(0, _stats.heavy_in_flight - 1)
        if sem is not None:
            sem.release()


def _accepts_cancel_token(fn: Callable[..., Any]) -> bool:
    try:
        import inspect

        params = inspect.signature(fn).parameters
        if "cancel_token" in params:
            return True
        return any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return False


def stats() -> Dict[str, Any]:
    """Current offload metrics — surfaced on the System Monitor page."""
    return _stats.snapshot()


def shutdown() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
