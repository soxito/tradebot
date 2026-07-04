"""
Agent Paul — Memory Tree service (OpenHuman-style hierarchical memory)

Sits on top of PaulKnowledge / PaulMemoryNode and provides:
  - importance scoring for new memories
  - content-hash dedupe so the same fact is not stored twice
  - `remember()` — write a chunk node (and optionally a knowledge row)
  - `rollup()` — fold a day's chunks into a single `daily` summary node
    (and weekly rollups from daily nodes), mirroring OpenHuman's Memory Tree
  - `recent_high_importance()` — feed the extension / auto-fetch surfacing
  - `search()` — importance-weighted keyword retrieval for the SuperContext scout

All read-only for the caller's data; no external network calls.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_sast
from plugins.AgentPaulPlugin.backend.models import (
    PaulMemoryNode, PaulMemoryKind, PaulKnowledge,
)

_STOPWORDS = {
    "the", "and", "for", "are", "was", "with", "that", "this", "from", "have",
    "has", "will", "your", "you", "but", "not", "all", "any", "can", "our",
    "what", "when", "how", "why", "who", "a", "an", "is", "it", "to", "of",
    "in", "on", "at", "as", "by", "or", "be", "if", "so", "do",
}

# Signal words that raise a memory's importance (trading-domain relevant).
_HOT_WORDS = {
    "breakout", "liquidation", "hack", "exploit", "sec", "etf", "halt", "rug",
    "pump", "dump", "surge", "crash", "rally", "record", "all-time", "ath",
    "risk", "loss", "profit", "stop", "target", "long", "short", "leverage",
    "fed", "rate", "cpi", "inflation", "war", "ban", "approval",
}


def _checksum(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())[:512]
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _tokens(text: str) -> list[str]:
    return [
        t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    ]


def score_importance(content: str, sentiment: Optional[float] = None,
                     base: float = 0.5) -> float:
    """Heuristic 0..1 importance. Strong sentiment + hot words + length raise it."""
    score = base
    toks = set(_tokens(content))
    hot = len(toks & _HOT_WORDS)
    score += min(0.30, hot * 0.08)
    if sentiment is not None:
        score += min(0.20, abs(float(sentiment)) * 0.20)
    length = len((content or "").strip())
    if length > 400:
        score += 0.05
    if length < 40:
        score -= 0.10
    return max(0.0, min(1.0, round(score, 3)))


def _summarize(content: str, limit: int = 220) -> str:
    """Cheap extractive summary: first sentence(s) up to `limit` chars."""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > 60 else cut).strip() + "…"


async def remember(
    db: AsyncSession,
    *,
    content: str,
    title: Optional[str] = None,
    symbol: Optional[str] = None,
    topic: Optional[str] = None,
    source: Optional[str] = None,
    sentiment: Optional[float] = None,
    importance: Optional[float] = None,
    knowledge_id: Optional[int] = None,
) -> Optional[PaulMemoryNode]:
    """Store a chunk memory node, deduped by content checksum. Returns the node
    (existing or new) or None on empty content."""
    if not content or not content.strip():
        return None
    chk = _checksum(content)
    existing = await db.execute(
        select(PaulMemoryNode).where(PaulMemoryNode.checksum == chk).limit(1)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row  # already remembered
    imp = importance if importance is not None else score_importance(content, sentiment)
    node = PaulMemoryNode(
        kind=PaulMemoryKind.CHUNK,
        title=(title or None),
        content=content.strip(),
        summary=_summarize(content),
        symbol=symbol,
        topic=topic,
        source=source,
        sentiment=sentiment,
        importance=imp,
        checksum=chk,
        day=now_sast().strftime("%Y-%m-%d"),
        knowledge_id=knowledge_id,
    )
    db.add(node)
    await db.commit()
    return node


async def import_knowledge(db: AsyncSession, limit: int = 100) -> int:
    """Fold recent PaulKnowledge rows that aren't yet in the Memory Tree into
    chunk nodes. Idempotent via checksum dedupe."""
    rows = await db.execute(
        select(PaulKnowledge).order_by(desc(PaulKnowledge.created_at)).limit(limit)
    )
    added = 0
    for k in rows.scalars().all():
        node = await remember(
            db,
            content=k.content,
            title=k.title,
            symbol=k.symbol,
            topic=k.topic,
            source=k.source,
            sentiment=k.sentiment,
            importance=k.importance,
            knowledge_id=k.id,
        )
        if node is not None and node.knowledge_id == k.id and node.created_at:
            added += 1
    return added


async def rollup(db: AsyncSession, day: Optional[str] = None) -> Optional[PaulMemoryNode]:
    """Fold a day's chunk nodes into (or refresh) a single `daily` summary node."""
    day = day or now_sast().strftime("%Y-%m-%d")
    chunks = await db.execute(
        select(PaulMemoryNode)
        .where(PaulMemoryNode.kind == PaulMemoryKind.CHUNK, PaulMemoryNode.day == day)
        .order_by(desc(PaulMemoryNode.importance))
    )
    items = chunks.scalars().all()
    if not items:
        return None

    top = items[:12]
    lines = [f"- ({c.importance:.2f}) {c.summary or _summarize(c.content)}" for c in top]
    body = (
        f"Memory summary for {day} — {len(items)} facts learned.\n"
        + "\n".join(lines)
    )
    avg_imp = round(sum(c.importance for c in items) / len(items), 3)

    existing = await db.execute(
        select(PaulMemoryNode).where(
            PaulMemoryNode.kind == PaulMemoryKind.DAILY, PaulMemoryNode.day == day
        ).limit(1)
    )
    node = existing.scalar_one_or_none()
    if node is None:
        node = PaulMemoryNode(kind=PaulMemoryKind.DAILY, day=day)
        db.add(node)
    node.title = f"Daily memory {day}"
    node.content = body
    node.summary = _summarize(body)
    node.importance = avg_imp
    node.topic = "system"
    node.source = "memory_tree"
    node.updated_at = now_sast()

    # link chunks to this daily node
    for c in items:
        c.parent_id = node.id if node.id else c.parent_id
    await db.commit()
    if node.id:
        for c in items:
            c.parent_id = node.id
        await db.commit()
    logger.info(f"[MemoryTree] rolled up {len(items)} chunks into daily {day}")
    return node


async def search(db: AsyncSession, query: str, limit: int = 6,
                 symbol: Optional[str] = None) -> list[dict]:
    """Importance-weighted keyword retrieval over chunk + daily nodes."""
    q_toks = set(_tokens(query))
    stmt = select(PaulMemoryNode).order_by(
        desc(PaulMemoryNode.importance), desc(PaulMemoryNode.created_at)
    ).limit(200)
    if symbol:
        stmt = stmt.where(PaulMemoryNode.symbol == symbol)
    rows = (await db.execute(stmt)).scalars().all()

    scored: list[tuple[float, PaulMemoryNode]] = []
    for n in rows:
        overlap = len(q_toks & set(_tokens(n.content))) if q_toks else 0
        if q_toks and overlap == 0 and n.kind == PaulMemoryKind.CHUNK:
            continue
        rank = overlap + n.importance
        scored.append((rank, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "id": n.id, "kind": n.kind.value, "importance": n.importance,
            "summary": n.summary or _summarize(n.content), "symbol": n.symbol,
            "source": n.source, "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for _, n in scored[:limit]
    ]


async def recent_high_importance(db: AsyncSession, since_minutes: int = 20,
                                 min_importance: float = 0.65,
                                 limit: int = 10) -> list[dict]:
    """New, important chunk memories — surfaced as JARVIS notifications."""
    cutoff = now_sast() - timedelta(minutes=since_minutes)
    rows = await db.execute(
        select(PaulMemoryNode)
        .where(
            PaulMemoryNode.kind == PaulMemoryKind.CHUNK,
            PaulMemoryNode.created_at >= cutoff,
            PaulMemoryNode.importance >= min_importance,
        )
        .order_by(desc(PaulMemoryNode.importance))
        .limit(limit)
    )
    return [
        {
            "id": n.id, "importance": n.importance,
            "title": n.title, "summary": n.summary or _summarize(n.content),
            "symbol": n.symbol, "source": n.source,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows.scalars().all()
    ]


async def stats(db: AsyncSession) -> dict:
    counts = await db.execute(
        select(PaulMemoryNode.kind, func.count(PaulMemoryNode.id))
        .group_by(PaulMemoryNode.kind)
    )
    by_kind = {k.value: c for k, c in counts.all()}
    return {
        "chunks": by_kind.get("chunk", 0),
        "daily": by_kind.get("daily", 0),
        "weekly": by_kind.get("weekly", 0),
        "total": sum(by_kind.values()),
    }
