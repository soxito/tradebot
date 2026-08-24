"""
Unified task supervisor — one registry for every background loop.

The app runs ~27 background loops (core scheduler + plugin loops) with no shared
registry, no cost visibility, and no way to pause any of them. This supervisor
is an **adapter registry**, not a rewrite: each loop already exposes an
identical ``start_X`` / ``stop_X`` / ``get_X_status`` triple, so we wrap those
and gain visibility + control at ~5% of the risk of rewriting the loops.

Three distinct stop levels (the "pause without losing the task" contract):
- **paused**   — task kept registered; a ``gate()``-aware loop awaits an Event
                 (state preserved, instant resume). For adapter loops that
                 don't call ``gate()`` we stop the coroutine but remember the
                 paused intent so it is not auto-restarted.
- **stopped**  — calls ``stop_X()``; frees the coroutine and its sessions.
- **interval** — not a stop at all; overrides the loop cadence.

Critical tasks (position monitor, live auto-trade, sniper, Telegram monitor +
bot polling) can never be paused by tier or the watchdog — only explicitly by a
user with ``force=True``. Paused state persists to ``data/task_state.json`` so a
pause survives restarts and the DB being down.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from loguru import logger

from app.core import resource_tier as rt

# State file lives outside the DB: it must be readable before init_db() and
# survive the DB being down (exactly when you want loops paused).
_STATE_DIR = Path(__file__).resolve().parents[2] / "data"
_STATE_FILE = _STATE_DIR / "task_state.json"

# Interval clamps so a bad multiplier/override can't produce a busy-spin or a
# effectively-dead loop.
_MIN_INTERVAL_S = 1.0
_MAX_INTERVAL_S = 24 * 3600.0

PausedBy = Optional[str]  # None | "user" | "tier" | "watchdog"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskSpec:
    id: str
    name: str
    category: str = "other"
    description: str = ""
    source: str = "core"
    default_interval_s: float = 60.0
    critical: bool = False
    autostart: bool = True
    min_tier: str = "minimal"
    start: Optional[Callable[[], Any]] = None
    stop: Optional[Callable[[], Any]] = None
    status: Optional[Callable[[], Dict[str, Any]]] = None
    run_once: Optional[Callable[[], Awaitable[Any]]] = None


@dataclass
class _TaskState:
    paused: bool = False
    paused_by: PausedBy = None
    interval_override: Optional[float] = None
    claimed: bool = False
    gate_event: asyncio.Event = field(default_factory=asyncio.Event)
    recent_cycles: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    cycle_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    last_run: Optional[str] = None
    cumulative_cpu_ms: float = 0.0

    def __post_init__(self) -> None:
        # An un-paused task's gate is "open" (set) so gate() returns instantly.
        if not self.paused:
            self.gate_event.set()


class TaskSupervisor:
    def __init__(self) -> None:
        self._specs: Dict[str, TaskSpec] = {}
        self._state: Dict[str, _TaskState] = {}
        self._persisted: Dict[str, Any] = {"paused": {}, "intervals": {}}
        self._watchdog_task: Optional[asyncio.Task] = None
        self._load_state()

    # ── registration ──────────────────────────────────────────────────────
    def register(self, spec: TaskSpec) -> None:
        self._specs[spec.id] = spec
        if spec.id not in self._state:
            st = _TaskState()
            # Apply any persisted pause / interval delta.
            if spec.id in self._persisted.get("paused", {}):
                st.paused = True
                st.paused_by = self._persisted["paused"][spec.id] or "user"
                st.gate_event.clear()
            ov = self._persisted.get("intervals", {}).get(spec.id)
            if isinstance(ov, (int, float)) and ov > 0:
                st.interval_override = float(ov)
            self._state[spec.id] = st

    def bind(
        self,
        task_id: str,
        *,
        start: Optional[Callable[[], Any]] = None,
        stop: Optional[Callable[[], Any]] = None,
        status: Optional[Callable[[], Dict[str, Any]]] = None,
        run_once: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> None:
        """Late-bind control callables (for plugin loops discovered lazily)."""
        spec = self._specs.get(task_id)
        if spec is None:
            return
        if start is not None:
            spec.start = start
        if stop is not None:
            spec.stop = stop
        if status is not None:
            spec.status = status
        if run_once is not None:
            spec.run_once = run_once

    def claim(self, task_id: str) -> bool:
        """Claim ownership of a loop so a lazily-started plugin loop dedupes.

        Never raises — a router-level dependency depends on this. Critical tasks
        always claim (the guard exists to bind + dedupe, not to gate)."""
        st = self._state.get(task_id)
        if st is None:
            return True
        if st.claimed and not self.is_critical(task_id):
            return False
        st.claimed = True
        return True

    # ── tier-aware interval ───────────────────────────────────────────────
    def interval(self, task_id: str) -> float:
        spec = self._specs.get(task_id)
        st = self._state.get(task_id)
        base = spec.default_interval_s if spec else 60.0
        if st and st.interval_override:
            return _clamp(st.interval_override)
        try:
            from app.core.config import settings
            tier = settings.PERF_TIER
        except Exception:
            tier = "high"
        return _clamp(base * rt.interval_multiplier(tier))

    def set_interval(self, task_id: str, seconds: Optional[float]) -> None:
        st = self._state.get(task_id)
        if st is None:
            return
        st.interval_override = float(seconds) if seconds and seconds > 0 else None
        self._persist()

    # ── Layer B primitives (opt-in from inside a loop body) ────────────────
    async def gate(self, task_id: str) -> None:
        """Cooperative pause point. Awaits until the task is resumed."""
        st = self._state.get(task_id)
        if st is None:
            return
        await st.gate_event.wait()

    @contextlib.asynccontextmanager
    async def cycle(self, task_id: str):
        """Time one loop cycle; record wall/cpu ms, errors, last_run."""
        st = self._state.get(task_id)
        wall0 = time.perf_counter()
        cpu0 = time.thread_time()
        err: Optional[str] = None
        try:
            yield
        except Exception as e:  # noqa: BLE001 — record and re-raise
            err = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            if st is not None:
                wall_ms = (time.perf_counter() - wall0) * 1000.0
                cpu_ms = max(0.0, (time.thread_time() - cpu0) * 1000.0)
                st.cycle_count += 1
                st.cumulative_cpu_ms += cpu_ms
                st.last_run = _now()
                if err:
                    st.error_count += 1
                    st.last_error = err
                st.recent_cycles.append({
                    "at": st.last_run, "wall_ms": round(wall_ms, 1),
                    "cpu_ms": round(cpu_ms, 1), "error": err,
                })

    # ── control ───────────────────────────────────────────────────────────
    def is_critical(self, task_id: str) -> bool:
        spec = self._specs.get(task_id)
        return bool(spec and spec.critical)

    def pause(self, task_id: str, by: str = "user", force: bool = False) -> Dict[str, Any]:
        st = self._state.get(task_id)
        if st is None:
            return {"ok": False, "reason": "unknown task"}
        if self.is_critical(task_id) and not force:
            return {"ok": False, "reason": "critical task requires force"}
        st.paused = True
        st.paused_by = by
        st.gate_event.clear()
        # Adapter loops that don't call gate() are stopped so the pause is real;
        # the paused flag prevents auto-restart.
        spec = self._specs[task_id]
        if spec.stop is not None:
            _safe_call(spec.stop)
        self._persist()
        logger.info(f"[supervisor] paused {task_id} (by={by})")
        return {"ok": True, "paused_by": by}

    def resume(self, task_id: str) -> Dict[str, Any]:
        st = self._state.get(task_id)
        if st is None:
            return {"ok": False, "reason": "unknown task"}
        st.paused = False
        st.paused_by = None
        st.gate_event.set()
        spec = self._specs[task_id]
        if spec.start is not None:
            _safe_call(spec.start)
        self._persist()
        logger.info(f"[supervisor] resumed {task_id}")
        return {"ok": True}

    def start(self, task_id: str) -> Dict[str, Any]:
        spec = self._specs.get(task_id)
        if spec is None or spec.start is None:
            return {"ok": False, "reason": "no start binding"}
        _safe_call(spec.start)
        return {"ok": True}

    def stop(self, task_id: str) -> Dict[str, Any]:
        spec = self._specs.get(task_id)
        if spec is None or spec.stop is None:
            return {"ok": False, "reason": "no stop binding"}
        _safe_call(spec.stop)
        return {"ok": True}

    async def run_now(self, task_id: str) -> Dict[str, Any]:
        spec = self._specs.get(task_id)
        if spec is None or spec.run_once is None:
            return {"ok": False, "reason": "task has no run_once binding"}
        try:
            await spec.run_once()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)[:200]}

    def autostart_set(self, tier: Optional[str] = None) -> List[str]:
        """Task ids that should autostart at the given tier (paused ones excluded)."""
        if tier is None:
            try:
                from app.core.config import settings
                tier = settings.PERF_TIER
            except Exception:
                tier = "high"
        out: List[str] = []
        for tid, spec in self._specs.items():
            st = self._state.get(tid)
            if st and st.paused:
                continue
            if not spec.autostart:
                continue
            if rt.should_autostart(tid, tier):
                out.append(tid)
        return out

    # ── snapshot ──────────────────────────────────────────────────────────
    def task_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        spec = self._specs.get(task_id)
        st = self._state.get(task_id)
        if spec is None or st is None:
            return None
        running = None
        raw_status: Dict[str, Any] = {}
        if spec.status is not None:
            try:
                raw_status = spec.status() or {}
                running = raw_status.get("running")
            except Exception as e:
                raw_status = {"error": str(e)[:120]}
        return {
            "id": spec.id,
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "source": spec.source,
            "critical": spec.critical,
            "min_tier": spec.min_tier,
            "running": running,
            "paused": st.paused,
            "paused_by": st.paused_by,
            "interval_seconds": self.interval(task_id),
            "default_interval_seconds": spec.default_interval_s,
            "interval_override": st.interval_override,
            "cycle_count": st.cycle_count,
            "error_count": st.error_count,
            "last_error": st.last_error,
            "last_run": st.last_run or raw_status.get("last_run"),
            "cumulative_cpu_ms": round(st.cumulative_cpu_ms, 1),
            "recent_cycles": list(st.recent_cycles),
        }

    def snapshot(self) -> Dict[str, Any]:
        try:
            from app.core.config import settings
            tier, profile = settings.PERF_TIER, (settings.TRADEBOT_PROFILE or None)
        except Exception:
            tier, profile = "high", None
        tasks = [self.task_info(tid) for tid in sorted(self._specs)]
        tasks = [t for t in tasks if t]
        paused = [t for t in tasks if t["paused"]]
        return {
            "tier": tier,
            "profile": profile,
            "generated_at": _now(),
            "task_count": len(tasks),
            "paused_count": len(paused),
            "paused": [{"id": t["id"], "paused_by": t["paused_by"]} for t in paused],
            "tasks": tasks,
        }

    # ── persistence ───────────────────────────────────────────────────────
    def _load_state(self) -> None:
        if os.environ.get("TRADEBOT_TASK_STATE_RESET", "").strip() in ("1", "true", "yes"):
            self._persisted = {"paused": {}, "intervals": {}}
            return
        try:
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text())
                if isinstance(data, dict):
                    self._persisted = {
                        "paused": dict(data.get("paused", {})),
                        "intervals": dict(data.get("intervals", {})),
                    }
        except Exception as e:
            logger.debug(f"[supervisor] state load skipped: {e}")

    def _persist(self) -> None:
        paused = {
            tid: (st.paused_by or "user")
            for tid, st in self._state.items() if st.paused
        }
        intervals = {
            tid: st.interval_override
            for tid, st in self._state.items() if st.interval_override
        }
        self._persisted = {"paused": paused, "intervals": intervals}
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps(self._persisted, indent=2))
        except Exception as e:
            logger.debug(f"[supervisor] state persist skipped: {e}")

    # ── memory watchdog ───────────────────────────────────────────────────
    def start_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.ensure_future(self._watchdog_loop())
        logger.info("[supervisor] memory watchdog started (30s, hysteresis)")

    async def stop_watchdog(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(Exception):
                await self._watchdog_task
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        # Categories the watchdog may auto-pause under memory pressure.
        throttle_categories = {"research", "learning", "enrichment"}
        low_streak = 0
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            try:
                from app.services.system_resources import host_snapshot
                host = host_snapshot()
                if not host.get("available"):
                    continue
                mem = host.get("mem_percent", 0.0)
                swap = host.get("swap_percent", 0.0)
                if mem > 80 or swap > 55:
                    self._relieve_memory()
                if mem > 90 or swap > 60:
                    self._watchdog_pause(throttle_categories)
                    low_streak = 0
                elif mem < 70 and swap < 40:
                    low_streak += 1
                    if low_streak >= 3:
                        self._watchdog_restore()
                        low_streak = 0
                else:
                    low_streak = 0
            except Exception as e:  # noqa: BLE001 — never let the watchdog die
                logger.debug(f"[supervisor] watchdog error: {e}")

    def _relieve_memory(self) -> None:
        """Non-destructive relief under early pressure: drop caches, GC, MPS cache."""
        try:
            from app.core.cache import evict_all
            dropped = evict_all()
            if dropped:
                logger.info(f"[supervisor] watchdog evicted {dropped} cache entries")
        except Exception:
            pass
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        try:
            import torch  # noqa
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

    def _watchdog_pause(self, categories: set) -> None:
        for tid, spec in self._specs.items():
            if spec.critical or spec.category not in categories:
                continue
            st = self._state.get(tid)
            if st and not st.paused:
                self.pause(tid, by="watchdog")
                logger.warning(f"[supervisor] watchdog paused {tid} under memory pressure")

    def _watchdog_restore(self) -> None:
        for tid, st in self._state.items():
            if st.paused and st.paused_by == "watchdog":
                self.resume(tid)
                logger.info(f"[supervisor] watchdog restored {tid}")


def _clamp(v: float) -> float:
    return max(_MIN_INTERVAL_S, min(_MAX_INTERVAL_S, float(v)))


def _safe_call(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[supervisor] control call failed: {e}")


# Module-level singleton shared by the lifespan and the API layer.
supervisor = TaskSupervisor()
