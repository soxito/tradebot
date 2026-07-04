"""
Agent Paul — Subconscious Loop (OpenHuman-style heartbeat)

The agent's idle thread: the part that keeps thinking after you stop typing.
On a periodic tick it (1) evaluates a small set of read-only *system tasks*
(provider health, new memory, system health) and (2) runs *goal continuation*
for every active thread-goal — all logged to an activity feed.

Safety boundary (money-handling assistant): every tick is READ-ONLY. It never
places or executes trades. Write-intent findings are logged as
``awaiting_approval`` todos (handled by the idle worker / goals service), never
auto-executed. One goal is worked at most once per idle period (spacing guard),
so the loop can never self-drive into a runaway.

Config (env):
  PAUL_HEARTBEAT_ENABLED            (default "1")
  PAUL_HEARTBEAT_GOAL_CONTINUATION  (default "1")
  PAUL_HEARTBEAT_TICK_SECONDS       (default 300, min 300)
"""
from __future__ import annotations

import asyncio
import os

from loguru import logger
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_sast


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


TICK_SECONDS = max(300, int(os.getenv("PAUL_HEARTBEAT_TICK_SECONDS", "300") or 300))


async def log_activity(db: AsyncSession, *, kind: str, task_name: str | None = None,
                       decision: str | None = None, state: str = "acted",
                       summary: str | None = None, session_key: str | None = None,
                       goal_id: int | None = None) -> None:
    """Append one entry to the subconscious activity feed."""
    from plugins.AgentPaulPlugin.backend.models import PaulActivity
    db.add(PaulActivity(
        kind=kind, task_name=task_name, decision=decision, state=state,
        summary=(summary or "")[:1000], session_key=session_key, goal_id=goal_id,
    ))
    await db.commit()


async def list_activity(db: AsyncSession, limit: int = 30) -> list[dict]:
    from plugins.AgentPaulPlugin.backend.models import PaulActivity
    rows = (await db.execute(
        select(PaulActivity).order_by(desc(PaulActivity.created_at)).limit(limit)
    )).scalars().all()
    return [{
        "id": a.id, "kind": a.kind, "task_name": a.task_name,
        "decision": a.decision, "state": a.state, "summary": a.summary,
        "session_key": a.session_key, "goal_id": a.goal_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in rows]


# System tasks seeded automatically (read-only). Names mirror OpenHuman defaults.
SYSTEM_TASKS = (
    "Check AI providers for errors or disconnections",
    "Review new memory updates for actionable items",
    "Monitor system health (memory, connections)",
)


class PaulSubconscious:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._session_factory = None
        self.tick_count = 0
        self.failure_count = 0
        self.last_tick_at: str | None = None
        self.last_tick_summary: dict | None = None

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def ensure_started(self, session_factory) -> None:
        self._session_factory = session_factory
        if not _flag("PAUL_HEARTBEAT_ENABLED"):
            logger.info("🫀 Paul subconscious disabled (PAUL_HEARTBEAT_ENABLED=0)")
            return
        if self.is_running():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())
        self._running = True
        logger.info("🫀 Paul subconscious loop started (tick {}s)", TICK_SECONDS)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    # ── system-task evaluation (deterministic, read-only) ──────────────

    async def _eval_system_tasks(self, db: AsyncSession) -> list[dict]:
        from plugins.AgentPaulPlugin.backend.services import memory_tree
        out: list[dict] = []

        # 1. AI provider health
        try:
            from plugins.AiMarketAnalyst.backend.services import ai_router as _ar
            status_fn = getattr(_ar, "provider_status", None) or getattr(_ar, "status", None)
            info = await status_fn(db) if status_fn else None  # type: ignore
            broken = []
            if isinstance(info, dict):
                for name, st in (info.get("providers") or info).items():
                    if isinstance(st, dict) and (st.get("open") or st.get("tripped") or
                                                 st.get("healthy") is False):
                        broken.append(name)
            if broken:
                out.append({"task": SYSTEM_TASKS[0], "decision": "act", "state": "acted",
                            "summary": f"Providers degraded: {', '.join(broken)}"})
            else:
                out.append({"task": SYSTEM_TASKS[0], "decision": "skip", "state": "skipped",
                            "summary": "Providers nominal"})
        except Exception:
            out.append({"task": SYSTEM_TASKS[0], "decision": "skip", "state": "skipped",
                        "summary": "Provider status unavailable"})

        # 2. New high-importance memory
        try:
            hi = await memory_tree.recent_high_importance(db, limit=5)
            if hi:
                out.append({"task": SYSTEM_TASKS[1], "decision": "act", "state": "acted",
                            "summary": f"{len(hi)} new high-importance memories: "
                                       + "; ".join(m["summary"][:60] for m in hi[:2])})
            else:
                out.append({"task": SYSTEM_TASKS[1], "decision": "skip", "state": "skipped",
                            "summary": "Nothing new"})
        except Exception as exc:  # noqa
            out.append({"task": SYSTEM_TASKS[1], "decision": "skip", "state": "skipped",
                        "summary": f"memory unavailable ({exc})"})

        # 3. System health snapshot
        try:
            stats = await memory_tree.stats(db)
            out.append({"task": SYSTEM_TASKS[2], "decision": "act", "state": "acted",
                        "summary": f"memory nodes={stats.get('total', '?')} "
                                   f"chunks={stats.get('chunks', '?')}"})
        except Exception as exc:  # noqa
            out.append({"task": SYSTEM_TASKS[2], "decision": "skip", "state": "skipped",
                        "summary": f"stats unavailable ({exc})"})
        return out

    # ── goal continuation ──────────────────────────────────────────────

    async def _continue_goals(self, db: AsyncSession) -> list[dict]:
        """Run one read-only idle step for each active thread-goal that hasn't
        been worked within the current idle period (spacing = one tick)."""
        from plugins.AgentPaulPlugin.backend.models import PaulGoal
        from plugins.AgentPaulPlugin.backend.services import idle_worker
        results: list[dict] = []
        goals = (await db.execute(
            select(PaulGoal).where(
                PaulGoal.status == "active",
                PaulGoal.scope == "thread",
                PaulGoal.session_key.isnot(None),
            ).limit(8)
        )).scalars().all()
        now = now_sast()
        for g in goals:
            # one-shot suppression per idle period: skip if worked within a tick
            if g.last_worked_at is not None:
                delta = (now - g.last_worked_at).total_seconds()
                if delta < TICK_SECONDS:
                    continue
            try:
                res = await idle_worker.run_idle_step(db, g.session_key)
                results.append({"goal_id": g.id, "session_key": g.session_key, **res})
            except Exception as exc:  # noqa
                logger.debug("[Subconscious] goal {} step failed: {}", g.id, exc)
        return results

    async def run_tick(self) -> dict:
        """One heartbeat tick. Safe to call on demand (Run Now)."""
        if self._session_factory is None:
            from app.core.database import AsyncSessionLocal as _S
            self._session_factory = _S
        summary: dict = {"system_tasks": [], "goal_steps": [], "at": None}
        try:
            async with self._session_factory() as db:
                sys_tasks = await self._eval_system_tasks(db)
                for t in sys_tasks:
                    await log_activity(db, kind="system", task_name=t["task"],
                                       decision=t["decision"], state=t["state"],
                                       summary=t["summary"])
                summary["system_tasks"] = sys_tasks

                if _flag("PAUL_HEARTBEAT_GOAL_CONTINUATION"):
                    steps = await self._continue_goals(db)
                    for s in steps:
                        await log_activity(
                            db, kind="goal", task_name="goal continuation",
                            decision="act" if s.get("findings") else "skip",
                            state="acted" if s.get("findings") else "skipped",
                            summary=s.get("note") or "no new findings",
                            session_key=s.get("session_key"), goal_id=s.get("goal_id"),
                        )
                    summary["goal_steps"] = steps
            self.failure_count = 0
            self.tick_count += 1
            self.last_tick_at = now_sast().isoformat()
            summary["at"] = self.last_tick_at
            self.last_tick_summary = summary
            logger.info("🫀 Subconscious tick #{}: {} sys / {} goal steps",
                        self.tick_count, len(summary["system_tasks"]), len(summary["goal_steps"]))
        except Exception as exc:  # noqa
            self.failure_count += 1
            logger.warning("[Subconscious] tick failed ({}x): {}", self.failure_count, exc)
        return summary

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # let the app finish starting
        while self._running:
            await self.run_tick()
            try:
                await asyncio.sleep(TICK_SECONDS)
            except asyncio.CancelledError:
                break

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "enabled": _flag("PAUL_HEARTBEAT_ENABLED"),
            "goal_continuation": _flag("PAUL_HEARTBEAT_GOAL_CONTINUATION"),
            "tick_seconds": TICK_SECONDS,
            "tick_count": self.tick_count,
            "failure_count": self.failure_count,
            "last_tick_at": self.last_tick_at,
            "last_tick_summary": self.last_tick_summary,
            "system_tasks": list(SYSTEM_TASKS),
        }


subconscious = PaulSubconscious()
