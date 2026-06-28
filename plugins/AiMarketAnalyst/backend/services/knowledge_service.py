"""Agent knowledge store.

Durable facts an agent persists and references on future tasks. Two sources:
  * decision outcomes / insights written by the orchestrator after analysis
  * Graphify code-map facts (architecture/relationships)

``build_knowledge_prompt`` returns a compact block injected into agent prompts so
agents 'remember' what worked. ``store_knowledge`` upserts a fact (dedup by
role+symbol+title) and bumps its weight when reinforced.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import AIAgentKnowledge


async def store_knowledge(
    db: AsyncSession,
    *,
    content: str,
    agent_role: str | None = None,
    symbol: str | None = None,
    kind: str = "insight",
    title: str | None = None,
    weight: float = 1.0,
    source: str | None = None,
) -> AIAgentKnowledge:
    """Insert or reinforce a knowledge fact (dedup by role+symbol+title)."""
    existing = None
    if title:
        existing = (
            await db.execute(
                select(AIAgentKnowledge).where(
                    AIAgentKnowledge.agent_role.is_(agent_role) if agent_role is None
                    else AIAgentKnowledge.agent_role == agent_role,
                    AIAgentKnowledge.symbol.is_(symbol) if symbol is None
                    else AIAgentKnowledge.symbol == symbol,
                    AIAgentKnowledge.title == title,
                )
            )
        ).scalars().first()
    if existing is not None:
        existing.content = content
        existing.weight = min(10.0, (existing.weight or 1.0) + 0.5)  # reinforce
        existing.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(existing)
        return existing
    row = AIAgentKnowledge(
        agent_role=agent_role,
        symbol=symbol,
        kind=kind,
        title=title,
        content=content,
        weight=weight,
        source=source,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def query_knowledge(
    db: AsyncSession,
    *,
    agent_role: str | None = None,
    symbol: str | None = None,
    limit: int = 6,
) -> list[AIAgentKnowledge]:
    """Most relevant/weighted knowledge for an agent + symbol.

    Includes shared rows (null role/symbol) so global rules always apply.
    """
    stmt = select(AIAgentKnowledge)
    if agent_role is not None:
        stmt = stmt.where(or_(AIAgentKnowledge.agent_role == agent_role, AIAgentKnowledge.agent_role.is_(None)))
    if symbol is not None:
        stmt = stmt.where(or_(AIAgentKnowledge.symbol == symbol, AIAgentKnowledge.symbol.is_(None)))
    stmt = stmt.order_by(AIAgentKnowledge.weight.desc(), AIAgentKnowledge.updated_at.desc()).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    # Track reference hits
    for r in rows:
        r.hits = (r.hits or 0) + 1
    if rows:
        await db.commit()
    return rows


def build_knowledge_prompt(rows: list[AIAgentKnowledge]) -> str:
    """Compact prompt block of stored knowledge for injection into agent prompts."""
    if not rows:
        return ""
    lines = ["\n\n# Stored knowledge (reference this when deciding):"]
    for r in rows:
        tag = r.kind.upper()
        scope = r.symbol or "ALL"
        title = f"{r.title}: " if r.title else ""
        lines.append(f"- [{tag}/{scope}] {title}{r.content}")
    return "\n".join(lines)


async def list_knowledge(db: AsyncSession, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = list(
        (
            await db.execute(
                select(AIAgentKnowledge)
                .order_by(AIAgentKnowledge.weight.desc(), AIAgentKnowledge.updated_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )
    return [
        {
            "id": r.id,
            "agent_role": r.agent_role,
            "symbol": r.symbol,
            "kind": r.kind,
            "title": r.title,
            "content": r.content,
            "weight": r.weight,
            "source": r.source,
            "hits": r.hits,
            "created_at": str(r.created_at) if r.created_at else None,
            "updated_at": str(r.updated_at) if r.updated_at else None,
        }
        for r in rows
    ]
