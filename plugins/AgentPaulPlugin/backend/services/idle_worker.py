"""
Agent Paul — Idle continuation worker (READ-ONLY research)

OpenHuman's agent "keeps thinking in the background". For this money-handling
assistant the safety boundary is strict: when a conversation has an active goal
and the user goes quiet, JARVIS runs ONE bounded, READ-ONLY research step and
logs progress as a todo. It NEVER places or executes trades.

Allowed tools (all read-only):
  - news_research.fetch_news / news_for_symbol / research_url
  - market_predictor.predict_pair
  - memory_tree.search / knowledge_base.search_knowledge

Explicitly excluded: voice_trade.*, paul_loop.execute/approve, any order placement.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_sast
from plugins.AgentPaulPlugin.backend.services import goals_todos, memory_tree

# Minimum seconds of user silence before idle work kicks in.
IDLE_AFTER_SECONDS = 45

_last_status: dict = {"thinking": False, "goal_id": None, "note": None, "at": None}


def current_status() -> dict:
    return dict(_last_status)


def _set_status(thinking: bool, goal_id: Optional[int] = None, note: Optional[str] = None) -> None:
    _last_status.update({
        "thinking": thinking,
        "goal_id": goal_id,
        "note": note,
        "at": now_sast().isoformat(),
    })


async def run_idle_step(db: AsyncSession, session_key: str) -> dict:
    """Perform ONE read-only research step toward the session's active goal.

    Returns a small status dict. No-op (thinking=False) when there is no active
    goal for the session.
    """
    goal = await goals_todos.get_active_goal(db, session_key)
    if goal is None:
        _set_status(False)
        return {"thinking": False, "reason": "no_active_goal"}

    _set_status(True, goal.id, "researching")
    query = goal.title + (f" {goal.detail}" if goal.detail else "")
    findings: list[str] = []

    # 1. Sweep memory for anything already known about the goal
    try:
        mem = await memory_tree.search(db, query, limit=3)
        findings += [m["summary"] for m in mem if m.get("summary")]
    except Exception as exc:  # noqa
        logger.debug("[Idle] memory step skipped: {}", exc)

    # 2. Pull fresh, relevant news (read-only)
    try:
        from plugins.AgentPaulPlugin.backend.services import news_research
        news = await news_research.web_news_search(query, limit=3)
        findings += [f"News: {n['title']}" for n in (news or [])]
    except Exception as exc:  # noqa
        logger.debug("[Idle] news step skipped: {}", exc)

    # 3. If the goal names an instrument, get a read-only prediction
    try:
        import re
        m = re.search(r"\b([A-Z]{2,6}(?:USDT?|USD))\b", (query or "").upper())
        if m:
            from plugins.AgentPaulPlugin.backend.services.market_predictor import predict_pair
            pred = await predict_pair(db, m.group(1))
            if isinstance(pred, dict) and pred.get("direction"):
                findings.append(
                    f"Prediction {m.group(1)}: {pred.get('direction')} "
                    f"(conf {pred.get('confidence', '?')})"
                )
    except Exception as exc:  # noqa
        logger.debug("[Idle] prediction step skipped: {}", exc)

    if not findings:
        _set_status(False, goal.id, "no new findings")
        return {"thinking": False, "goal_id": goal.id, "findings": 0}

    note = "🔎 Idle research: " + " · ".join(findings[:3])
    # Log progress as a completed jarvis todo + remember the findings (store the
    # raw findings, NOT the prefixed note, so re-surfacing never compounds).
    budget_status = "active"
    try:
        await goals_todos.add_jarvis_note(
            db, goal.id, note[:300], needs_approval=False, session_key=session_key,
        )
        await memory_tree.remember(
            db, content=" · ".join(findings[:3]), title=f"Idle work on: {goal.title}",
            topic="system", source="idle_worker", importance=0.5,
        )
        goal.last_worked_at = now_sast()
        await db.commit()
        # Charge the thread-goal token budget (rough estimate for a read-only
        # sweep). Flips the goal to ``budget_limited`` when exhausted → halts.
        budget_status = await goals_todos.add_spend(db, goal.id, tokens=600) or "active"
    except Exception as exc:  # noqa
        logger.debug("[Idle] persist skipped: {}", exc)

    _set_status(False, goal.id, note[:120])
    logger.info("[Idle] worked goal {}: {}", goal.id, note[:120])
    return {"thinking": False, "goal_id": goal.id, "findings": len(findings),
            "note": note, "budget_status": budget_status}
