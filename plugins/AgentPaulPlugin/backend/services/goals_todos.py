"""
Agent Paul — Goals & Todos service (OpenHuman-style goal system + kanban)

Two goal tiers (OpenHuman parity):
  • scope="account" — long-term goals (session_key NULL), capped ~8, reviewed by
    the Reflect agent (``reflect``) against recent memory.
  • scope="thread"  — one durable "completion contract" per conversation with an
    optional token budget (→ ``budget_limited`` when idle-work spend exceeds it).

Plus a kanban todo board (OpenHuman statuses: todo / in_progress /
awaiting_approval / ready / blocked / done / rejected) that JARVIS and the user
build together. Pure DB + one optional LLM call (Reflect) — no trade execution.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_sast
from plugins.AgentPaulPlugin.backend.models import (
    PaulGoal, PaulGoalStatus, PaulTodo,
    PAUL_TODO_STATUSES, PAUL_TODO_STATUS_ALIASES,
)

_GOAL_STATUSES = {s.value for s in PaulGoalStatus}
_TODO_STATUSES = set(PAUL_TODO_STATUSES)

# OpenHuman caps long-term goals at ~8 (≈500 tokens of MEMORY_GOALS).
ACCOUNT_GOAL_CAP = 8


def _norm_todo_status(raw: Optional[str], default: str = "todo") -> str:
    if not raw:
        return default
    raw = PAUL_TODO_STATUS_ALIASES.get(raw, raw)
    return raw if raw in _TODO_STATUSES else default


def _goal_dict(g: PaulGoal) -> dict:
    return {
        "id": g.id, "title": g.title, "detail": g.detail,
        "status": g.status or "active", "scope": g.scope or "thread",
        "session_key": g.session_key, "priority": g.priority,
        "token_budget": g.token_budget, "spent_tokens": g.spent_tokens or 0,
        "progress": g.progress, "reflection": g.reflection,
        "last_worked_at": g.last_worked_at.isoformat() if g.last_worked_at else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }


def _todo_dict(t: PaulTodo) -> dict:
    return {
        "id": t.id, "goal_id": t.goal_id, "session_key": t.session_key,
        "title": t.title, "detail": t.detail, "status": t.status or "todo",
        "order_index": t.order_index, "created_by": t.created_by,
        "needs_approval": t.needs_approval, "approval_mode": t.approval_mode,
        "acceptance_criteria": t.acceptance_criteria or [],
        "execution_plan": t.execution_plan or [],
        "outcome": t.outcome, "blocker_reason": t.blocker_reason,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# ── Goals ──────────────────────────────────────────────────

async def list_goals(db: AsyncSession, session_key: Optional[str] = None,
                     scope: Optional[str] = None, include_done: bool = True) -> list[dict]:
    stmt = select(PaulGoal).order_by(asc(PaulGoal.priority), desc(PaulGoal.updated_at))
    if session_key:
        stmt = stmt.where(PaulGoal.session_key == session_key)
    if scope:
        stmt = stmt.where(PaulGoal.scope == scope)
    if not include_done:
        stmt = stmt.where(PaulGoal.status.in_(("active", "paused", "budget_limited")))
    rows = (await db.execute(stmt)).scalars().all()
    return [_goal_dict(g) for g in rows]


async def get_active_goal(db: AsyncSession, session_key: str) -> Optional[PaulGoal]:
    rows = await db.execute(
        select(PaulGoal)
        .where(PaulGoal.session_key == session_key, PaulGoal.status == "active")
        .order_by(asc(PaulGoal.priority), desc(PaulGoal.updated_at))
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def create_goal(db: AsyncSession, data: dict) -> dict:
    scope = data.get("scope") or ("account" if not data.get("session_key") else "thread")
    goal = PaulGoal(
        title=(data.get("title") or "Untitled goal")[:200],
        detail=data.get("detail"),
        scope=scope,
        session_key=data.get("session_key"),
        priority=int(data.get("priority", 3)),
        token_budget=data.get("token_budget"),
    )
    if data.get("status") in _GOAL_STATUSES:
        goal.status = data["status"]
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    if scope == "account":
        await _enforce_account_cap(db)
    return _goal_dict(goal)


async def _enforce_account_cap(db: AsyncSession) -> None:
    """Keep long-term (account) goals ≤ ACCOUNT_GOAL_CAP; retire lowest-priority extras."""
    rows = (await db.execute(
        select(PaulGoal)
        .where(PaulGoal.scope == "account", PaulGoal.status == "active")
        .order_by(asc(PaulGoal.priority), desc(PaulGoal.updated_at))
    )).scalars().all()
    for extra in rows[ACCOUNT_GOAL_CAP:]:
        extra.status = "abandoned"
        extra.reflection = (extra.reflection or "") + " [retired: over goal cap]"
    if len(rows) > ACCOUNT_GOAL_CAP:
        await db.commit()


async def update_goal(db: AsyncSession, goal_id: int, data: dict) -> dict:
    goal = (await db.execute(select(PaulGoal).where(PaulGoal.id == goal_id))).scalar_one_or_none()
    if goal is None:
        raise ValueError(f"goal {goal_id} not found")
    for field in ("title", "detail", "session_key", "reflection", "scope"):
        if field in data:
            setattr(goal, field, data[field])
    if "priority" in data:
        goal.priority = int(data["priority"])
    if "progress" in data:
        goal.progress = max(0.0, min(1.0, float(data["progress"])))
    if "token_budget" in data:
        goal.token_budget = data["token_budget"]
    if data.get("status") in _GOAL_STATUSES:
        goal.status = data["status"]
    goal.updated_at = now_sast()
    await db.commit()
    await db.refresh(goal)
    return _goal_dict(goal)


async def delete_goal(db: AsyncSession, goal_id: int) -> None:
    goal = (await db.execute(select(PaulGoal).where(PaulGoal.id == goal_id))).scalar_one_or_none()
    if goal is not None:
        await db.delete(goal)
        await db.commit()


async def add_spend(db: AsyncSession, goal_id: int, tokens: int) -> Optional[str]:
    """Accumulate idle-work token spend; flip to ``budget_limited`` when over budget.

    Returns the resulting status (or None if no goal). This enforces the OpenHuman
    thread-goal token budget: once exhausted, idle continuation halts.
    """
    goal = (await db.execute(select(PaulGoal).where(PaulGoal.id == goal_id))).scalar_one_or_none()
    if goal is None:
        return None
    goal.spent_tokens = (goal.spent_tokens or 0) + max(0, int(tokens))
    if goal.token_budget and goal.spent_tokens >= goal.token_budget and goal.status == "active":
        goal.status = "budget_limited"
        goal.reflection = (goal.reflection or "") + \
            f" [budget_limited @ {goal.spent_tokens}/{goal.token_budget} tok]"
    goal.updated_at = now_sast()
    await db.commit()
    return goal.status


# ── Reflect agent (OpenHuman goals_agent) ──────────────────

_BOOTSTRAP_GOALS = [
    {"title": "Protect capital — enforce risk limits on every setup", "priority": 1,
     "detail": "Confirm SL/position size before any trade idea is surfaced."},
    {"title": "Surface high-conviction setups with clear R:R", "priority": 2,
     "detail": "Prefer setups with >=2:1 reward:risk and confluence."},
    {"title": "Keep market memory fresh (news + on-chain + graph)", "priority": 3,
     "detail": "Ingest news and roll up daily digests so context stays current."},
]


async def reflect(db: AsyncSession) -> dict:
    """Review long-term (account) goals against recent memory and make minimal,
    justified changes — bootstrapping a starter set if none exist.

    Mirrors OpenHuman's ``goals_agent`` Reflect step. Uses the failover LLM via
    ai_router.db_chat; degrades gracefully to a deterministic bootstrap.
    """
    existing = await list_goals(db, scope="account", include_done=False)
    memory_ctx = ""
    try:
        from plugins.AgentPaulPlugin.backend.services.memory_tree import recent_high_importance
        hi = await recent_high_importance(db, limit=12)
        memory_ctx = "\n".join(f"- {m['summary'][:160]}" for m in hi)
    except Exception:  # pragma: no cover - memory optional
        pass

    proposal: Optional[dict] = None
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
        sys = (
            "You are JARVIS's goals reviewer for a crypto/forex trading co-pilot. "
            "Given the current long-term goals and recent memory highlights, output STRICT JSON "
            '{"add":[{"title":"","detail":"","priority":3}],"retire":[<goal_id>,...],'
            '"reflection":"one sentence"}. Keep the active set <= 8, stable, and only propose '
            "changes clearly justified by the memory. Never suggest placing trades."
        )
        usr = (
            f"CURRENT GOALS:\n{json.dumps(existing, default=str)[:1500]}\n\n"
            f"RECENT MEMORY:\n{memory_ctx[:1200] or '(none)'}\n\nReturn JSON only."
        )
        raw = await db_chat(db, [{"role": "system", "content": sys},
                                 {"role": "user", "content": usr}], json_mode=True)
        proposal = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:  # pragma: no cover - LLM optional
        logger.debug(f"[goals.reflect] LLM unavailable, bootstrapping: {e}")

    added, retired, reflection = [], [], None
    if proposal and isinstance(proposal, dict):
        for item in (proposal.get("add") or [])[:ACCOUNT_GOAL_CAP]:
            if not item.get("title"):
                continue
            g = await create_goal(db, {
                "title": item["title"], "detail": item.get("detail"),
                "priority": int(item.get("priority", 3)), "scope": "account",
            })
            added.append(g)
        for gid in (proposal.get("retire") or []):
            try:
                await update_goal(db, int(gid), {"status": "achieved"})
                retired.append(int(gid))
            except Exception:
                pass
        reflection = proposal.get("reflection")

    # Deterministic bootstrap when there are no long-term goals at all.
    if not existing and not added:
        for seed in _BOOTSTRAP_GOALS:
            g = await create_goal(db, {**seed, "scope": "account"})
            added.append(g)
        reflection = reflection or "Bootstrapped starter long-term goals."

    await _enforce_account_cap(db)
    return {
        "added": added, "retired": retired,
        "reflection": reflection or "No changes needed.",
        "active_count": len(await list_goals(db, scope="account", include_done=False)),
    }


# ── Todos ──────────────────────────────────────────────────

async def list_todos(db: AsyncSession, session_key: Optional[str] = None,
                     goal_id: Optional[int] = None) -> list[dict]:
    stmt = select(PaulTodo).order_by(asc(PaulTodo.order_index), asc(PaulTodo.created_at))
    if session_key:
        stmt = stmt.where(PaulTodo.session_key == session_key)
    if goal_id is not None:
        stmt = stmt.where(PaulTodo.goal_id == goal_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [_todo_dict(t) for t in rows]


async def open_todos_for_goal(db: AsyncSession, goal_id: int, limit: int = 10) -> list[dict]:
    rows = await db.execute(
        select(PaulTodo)
        .where(PaulTodo.goal_id == goal_id, PaulTodo.status.notin_(("done", "rejected")))
        .order_by(asc(PaulTodo.order_index))
        .limit(limit)
    )
    return [_todo_dict(t) for t in rows.scalars().all()]


async def create_todo(db: AsyncSession, data: dict) -> dict:
    todo = PaulTodo(
        goal_id=data.get("goal_id"),
        session_key=data.get("session_key"),
        title=(data.get("title") or "Untitled task")[:300],
        detail=data.get("detail"),
        order_index=int(data.get("order_index", 0)),
        created_by=data.get("created_by", "user"),
        needs_approval=bool(data.get("needs_approval", False)),
        approval_mode=data.get("approval_mode",
                               "required" if data.get("needs_approval") else "not_required"),
        acceptance_criteria=data.get("acceptance_criteria"),
        execution_plan=data.get("execution_plan"),
        outcome=data.get("outcome"),
        status=_norm_todo_status(data.get("status")),
    )
    db.add(todo)
    await db.commit()
    await db.refresh(todo)
    return _todo_dict(todo)


async def update_todo(db: AsyncSession, todo_id: int, data: dict) -> dict:
    todo = (await db.execute(select(PaulTodo).where(PaulTodo.id == todo_id))).scalar_one_or_none()
    if todo is None:
        raise ValueError(f"todo {todo_id} not found")
    for field in ("title", "detail", "goal_id", "session_key", "approval_mode",
                  "acceptance_criteria", "execution_plan", "outcome", "blocker_reason"):
        if field in data:
            setattr(todo, field, data[field])
    if "order_index" in data:
        todo.order_index = int(data["order_index"])
    if "needs_approval" in data:
        todo.needs_approval = bool(data["needs_approval"])
    if "status" in data:
        todo.status = _norm_todo_status(data["status"], default=todo.status or "todo")
    todo.updated_at = now_sast()
    await db.commit()
    await db.refresh(todo)
    return _todo_dict(todo)


async def delete_todo(db: AsyncSession, todo_id: int) -> None:
    todo = (await db.execute(select(PaulTodo).where(PaulTodo.id == todo_id))).scalar_one_or_none()
    if todo is not None:
        await db.delete(todo)
        await db.commit()


async def add_jarvis_note(db: AsyncSession, goal_id: int, note: str,
                          needs_approval: bool = False,
                          session_key: Optional[str] = None) -> dict:
    """JARVIS appends a task under a goal (used by idle continuation).

    Unsolicited write intent → ``awaiting_approval`` (OpenHuman approval gate).
    Read-only observations → ``done`` (already actioned).
    """
    return await create_todo(db, {
        "goal_id": goal_id,
        "session_key": session_key,
        "title": note[:300],
        "status": "awaiting_approval" if needs_approval else "done",
        "created_by": "jarvis",
        "needs_approval": needs_approval,
        "approval_mode": "required" if needs_approval else "not_required",
    })


async def prompt_block(db: AsyncSession, session_key: str) -> Optional[str]:
    """A compact goals/todos block to inject into the JARVIS system prompt."""
    if not session_key:
        return None
    goal = await get_active_goal(db, session_key)
    if goal is None:
        return None
    todos = await open_todos_for_goal(db, goal.id, limit=8)
    lines = [
        "\n## Active Goal (work toward this across the conversation)",
        f"🎯 {goal.title} — progress {int(goal.progress * 100)}%",
    ]
    if goal.detail:
        lines.append(f"   {goal.detail[:300]}")
    if todos:
        lines.append("Open todos:")
        for t in todos:
            flag = " (needs approval)" if t["needs_approval"] else ""
            lines.append(f"  - [{t['status']}] {t['title']}{flag}")
    lines.append(
        "When you make progress, propose concrete next todos. Never place or execute "
        "trades from goal/idle work without explicit user confirmation."
    )
    return "\n".join(lines)
