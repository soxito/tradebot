"""
ObsidianKnowledgePlugin — FastAPI Router

All routes are prefixed at /plugins/obsidian-knowledge by the plugin loader.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings
from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote, NoteType
from plugins.ObsidianKnowledgePlugin.backend.schemas import (
    NoteContentResponse,
    SyncRequest,
    SyncResponse,
    SyncResult,
    VaultContextResponse,
    VaultGraphResponse,
    VaultNoteCreate,
    VaultNoteResponse,
    VaultSearchRequest,
    VaultSearchResponse,
    VaultSearchHit,
    VaultStatusResponse,
)
from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import VaultReader
from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge
from plugins.ObsidianKnowledgePlugin.backend.services.sync_orchestrator import (
    get_vault_sync_status,
    record_manual_sync,
    run_sync,
    start_vault_sync_loop,
    stop_vault_sync_loop,
)

router = APIRouter(prefix="/plugins/obsidian-knowledge", tags=["Obsidian Knowledge"])


# ── DB dependency ─────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Lazy singletons ───────────────────────────────────────────────────────────

def _writer() -> VaultWriter:
    return VaultWriter()


def _reader() -> VaultReader:
    return VaultReader()


# ═══════════════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/status", response_model=VaultStatusResponse, summary="Vault health check")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Return vault path, file counts, and Obsidian REST connectivity."""
    vault_path = obsidian_settings.vault_path
    bridge = get_bridge()
    rest_connected = await bridge.is_available()

    # Count DB records by type
    counts_rows = await db.execute(
        select(VaultNote.note_type, func.count(VaultNote.id))
        .group_by(VaultNote.note_type)
    )
    notes_by_type = {row[0]: row[1] for row in counts_rows}
    total_notes = sum(notes_by_type.values())

    # Last sync time
    last_sync_row = await db.execute(
        select(func.max(VaultNote.updated_at))
    )
    last_sync = last_sync_row.scalar_one_or_none()

    return VaultStatusResponse(
        vault_path=str(vault_path),
        vault_exists=vault_path.exists(),
        total_notes=total_notes,
        notes_by_type=notes_by_type,
        last_sync_at=last_sync,
        obsidian_rest_connected=rest_connected,
        obsidian_rest_url=obsidian_settings.OBSIDIAN_REST_URL or "(not configured)",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Notes CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/notes", summary="List vault notes")
async def list_notes(
    note_type: Optional[str] = Query(None, description="signal | decision | strategy | community | daily"),
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of vault notes from the DB tracking table."""
    q = select(VaultNote).order_by(desc(VaultNote.updated_at))
    if note_type:
        q = q.where(VaultNote.note_type == note_type)
    if symbol:
        q = q.where(VaultNote.symbol == symbol)
    q = q.offset(offset).limit(limit)

    result = await db.execute(q)
    notes = result.scalars().all()

    return {
        "notes": [VaultNoteResponse.model_validate(n) for n in notes],
        "total": len(notes),
        "offset": offset,
        "limit": limit,
    }


@router.get("/notes/content", response_model=NoteContentResponse, summary="Read note markdown")
async def read_note(path: str = Query(..., description="Relative path within vault")):
    """Read the raw markdown content of a specific note."""
    reader = _reader()
    content = reader.read_note(path)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Note not found: {path}")

    from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import _parse_frontmatter
    fm = _parse_frontmatter(content)

    return NoteContentResponse(
        path=path,
        content=content,
        frontmatter=fm,
        note_type=fm.get("type", "custom"),
        symbol=fm.get("symbol"),
        tags=fm.get("tags", []),
        created_at=None,
        updated_at=None,
    )


@router.post("/notes", response_model=NoteContentResponse, summary="Create / update a note")
async def create_note(body: VaultNoteCreate, db: AsyncSession = Depends(get_db)):
    """Write an arbitrary note to the vault."""
    from pathlib import Path as PPath
    writer = _writer()
    path = writer.root / body.path
    path.parent.mkdir(parents=True, exist_ok=True)

    import hashlib
    cs = hashlib.sha256(body.content.encode()).hexdigest()
    path.write_text(body.content, encoding="utf-8")

    # Track in DB
    result = await db.execute(select(VaultNote).where(VaultNote.path == body.path))
    existing = result.scalar_one_or_none()
    if existing:
        existing.checksum = cs
        existing.updated_at = datetime.utcnow()
    else:
        db.add(VaultNote(
            path=body.path,
            note_type=body.note_type,
            symbol=body.symbol,
            tags=body.tags,
            source_id=body.source_id,
            source_table=body.source_table,
            checksum=cs,
        ))
    await db.commit()

    # Try REST push
    bridge = get_bridge()
    await bridge.push_note(body.path, body.content)

    from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import _parse_frontmatter
    fm = _parse_frontmatter(body.content)
    return NoteContentResponse(
        path=body.path,
        content=body.content,
        frontmatter=fm,
        note_type=body.note_type,
        symbol=body.symbol,
        tags=body.tags or [],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Sync
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/sync", response_model=SyncResponse, summary="Trigger vault sync")
async def trigger_sync(
    body: SyncRequest = SyncRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """
    Run a full vault sync: export signals, decisions, and graphify communities.
    For large datasets this may take several seconds.
    """
    result = await run_sync(
        db=db,
        export_decisions=body.export_decisions,
        export_signals=body.export_signals,
        export_communities=body.export_communities,
        limit=body.limit,
    )
    # A manual run is still the most recent sync, so the page's "last sync"
    # must reflect it rather than only ever tracking the timer.
    record_manual_sync(result)
    return SyncResponse(
        success=result["errors"] == 0,
        result=SyncResult(**result),
    )


@router.get("/sync/status", summary="Auto-sync loop state and last run")
async def sync_status():
    """When the vault last actually synced, and when it will next.

    Distinct from `/status`, whose `last_sync_at` is the newest note timestamp:
    a cycle that finds nothing new does not move that, so it drifts ever older
    while the sync is in fact running fine every few minutes.
    """
    return get_vault_sync_status()


@router.post("/sync/start", summary="Start the vault auto-sync loop")
async def sync_start(interval: int = Query(300, ge=60, le=86400)):
    return {"started": start_vault_sync_loop(interval), "interval": interval}


@router.post("/sync/stop", summary="Stop the vault auto-sync loop")
async def sync_stop():
    return {"stopped": stop_vault_sync_loop()}


# ═══════════════════════════════════════════════════════════════════════════════
# Graph
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/graph", response_model=VaultGraphResponse, summary="Vault wikilink graph")
async def get_vault_graph():
    """
    Parse vault .md files for [[wikilinks]] and return a graph of
    notes (nodes) and their link relationships (edges).
    Suitable for rendering in force-graph.
    """
    reader = _reader()
    g = reader.vault_graph()
    return VaultGraphResponse(**g)


# ═══════════════════════════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/search", response_model=VaultSearchResponse, summary="Full-text vault search")
async def search_vault(body: VaultSearchRequest):
    """BM25 full-text search across all vault notes."""
    reader = _reader()
    hits = reader.search_notes(
        query=body.query,
        note_type=body.note_type,
        symbol=body.symbol,
        limit=body.limit,
    )
    return VaultSearchResponse(
        hits=[VaultSearchHit(**h) for h in hits],
        total=len(hits),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Agent context
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/context/{symbol}", response_model=VaultContextResponse,
            summary="Get vault context for agent injection")
async def get_agent_context(symbol: str):
    """
    Return recent vault notes for *symbol* formatted as markdown.
    This endpoint is called by the agent orchestrator to enrich prompts.
    """
    reader = _reader()
    context_md = reader.get_context_for_symbol(symbol)
    token_estimate = len(context_md) // 4

    return VaultContextResponse(
        symbol=symbol,
        context_markdown=context_md,
        notes_used=context_md.count("---") + 1 if context_md else 0,
        token_estimate=token_estimate,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Obsidian REST bridge actions
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/open", summary="Open a note in Obsidian app")
async def open_in_obsidian(path: str = Query(...)):
    """Tell the running Obsidian instance to open a specific note."""
    bridge = get_bridge()
    success = await bridge.open_note(path)
    return {"success": success, "path": path}


# ═══════════════════════════════════════════════════════════════════════════════
# Jarvis self-learning
# ═══════════════════════════════════════════════════════════════════════════════

class JarvisLearnRequest(BaseModel):
    question: str
    answer: str
    page: str = "/"
    tags: list[str] = []


@router.post("/jarvis-learn", summary="Capture a Jarvis Q&A exchange for self-learning")
async def jarvis_learn(body: JarvisLearnRequest, db: AsyncSession = Depends(get_db)):
    """
    Called automatically after every significant Jarvis response.
    Writes the Q&A to the vault as a learning note and pushes it live
    to Obsidian so the brain can reference it in future conversations.
    """
    writer = _writer()
    path, written, cs = writer.write_jarvis_note(
        question=body.question,
        answer=body.answer,
        page=body.page,
        tags=body.tags,
    )
    rel = str(path.relative_to(writer.root))

    # Track in DB
    if written:
        from sqlalchemy import select
        result = await db.execute(select(VaultNote).where(VaultNote.path == rel))
        existing = result.scalar_one_or_none()
        now = datetime.utcnow()
        if existing:
            existing.checksum = cs
            existing.updated_at = now
        else:
            db.add(VaultNote(
                path=rel,
                note_type="jarvis-learning",
                symbol=None,
                tags=body.tags or ["jarvis-learning"],
                checksum=cs,
                created_at=now,
                updated_at=now,
            ))
        await db.commit()

    # Push live to Obsidian
    bridge = get_bridge()
    pushed = False
    if bridge.enabled and path.exists():
        pushed = await bridge.push_note(rel, path.read_text(encoding="utf-8"))

    return {
        "written": written,
        "pushed_to_obsidian": pushed,
        "path": rel,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Learning activity feed — powers the Intelligence brain map learning panel
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/learning-activity", summary="Live learning activity for Intelligence brain")
async def get_learning_activity(db: AsyncSession = Depends(get_db)):
    """
    Returns a structured feed of everything JARVIS has learned, suitable for
    rendering in the Intelligence brain map's learning panel.

    Includes:
    - Counts of learning notes created today vs all-time
    - Recent jarvis-learning note summaries
    - Insights snapshots with stats
    - Top learned topics/symbols
    - Learning velocity (notes per hour today)
    """
    from sqlalchemy import desc as sqldesc, and_
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    # All learning notes
    all_learning = await db.execute(
        select(VaultNote)
        .where(VaultNote.note_type.in_(["jarvis-learning", "insights-snapshot"]))
        .order_by(sqldesc(VaultNote.created_at))
        .limit(50)
    )
    all_notes = all_learning.scalars().all()

    today_notes = [n for n in all_notes if n.created_at and n.created_at >= today_start]
    yesterday_notes = [n for n in all_notes if n.created_at and yesterday_start <= n.created_at < today_start]

    # Recent learning notes (last 8)
    recent_learning = [n for n in all_notes if n.note_type == "jarvis-learning"][:8]
    recent_snapshots = [n for n in all_notes if n.note_type == "insights-snapshot"][:3]

    # Extract topics from path slugs
    import re as _re
    def slug_to_topic(path: str) -> str:
        name = path.split("/")[-1].replace(".md", "")
        # Remove date prefix (2026-06-29-)
        name = _re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)
        return name.replace("-", " ").title()[:50]

    # Learning velocity (notes per hour today)
    hours_elapsed = max(1, (now - today_start).seconds // 3600)
    velocity = len(today_notes) / hours_elapsed

    return {
        "total_learning_notes": len(all_notes),
        "today_count": len(today_notes),
        "yesterday_count": len(yesterday_notes),
        "velocity_per_hour": round(velocity, 2),
        "is_actively_learning": len(today_notes) > 0,
        "recent_learning": [
            {
                "path": n.path,
                "topic": slug_to_topic(n.path),
                "tags": n.tags or [],
                "created_at": str(n.created_at),
            }
            for n in recent_learning
        ],
        "recent_snapshots": [
            {
                "path": n.path,
                "topic": slug_to_topic(n.path),
                "created_at": str(n.created_at),
            }
            for n in recent_snapshots
        ],
        "last_learned_at": str(all_notes[0].created_at) if all_notes else None,
        "last_snapshot_at": str(recent_snapshots[0].created_at) if recent_snapshots else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Live feed — unified activity stream for Intelligence brain map
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/live-feed", summary="Unified live activity feed for the brain map")
async def get_live_feed(
    limit: int = Query(30, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a unified, time-sorted feed of ALL Jarvis + system actions:

    - jarvis-learning   : every Q&A exchange  
    - jarvis-set_tp     : every TP placed on exchange
    - jarvis-set_sl     : every SL placed on exchange
    - jarvis-close      : every position close
    - agent-decision    : agent reasoning decisions
    - decision-outcome  : win/loss/break_even recordings
    - insights-snapshot : full brain state captures

    Suitable for the live ticker in Intelligence / Vault pages.
    """
    from sqlalchemy import desc as sqldesc

    all_notes = await db.execute(
        select(VaultNote)
        .order_by(sqldesc(VaultNote.created_at))
        .limit(limit * 3)   # over-fetch to ensure we get 'limit' after merge
    )
    notes = all_notes.scalars().all()

    feed = []
    for n in notes:
        if not n.created_at:
            continue
        note_type = n.note_type or "unknown"
        tags = n.tags or []

        # Extract symbol from tags
        sym = n.symbol or next(
            (t.upper() for t in tags if len(t) >= 3 and t.upper() not in
             ("JARVIS", "SIGNAL", "TRADE", "DECISION", "OUTCOME", "LEARNING", "SNAPSHOT")),
            None
        )

        feed.append({
            "id": n.id,
            "path": n.path,
            "type": note_type,
            "symbol": sym,
            "tags": tags[:5],
            "timestamp": str(n.created_at),
            "label": _label_from_path(n.path),
        })

    # Sort by timestamp descending, limit
    feed.sort(key=lambda x: x["timestamp"], reverse=True)

    # ── Also scan live-actions/ on disk for notes not yet in DB ───────────────
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import _parse_frontmatter
        import re as _re
        from pathlib import Path as _Path

        writer = VaultWriter()
        live_dir = writer.root / "live-actions"
        if live_dir.exists():
            db_paths = {item["path"] for item in feed}
            for md_file in sorted(live_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
                rel = str(md_file.relative_to(writer.root))
                if rel in db_paths:
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                    fm = _parse_frontmatter(content)
                    ts_stat = md_file.stat().st_mtime
                    from datetime import datetime as _dt
                    ts_iso = _dt.utcfromtimestamp(ts_stat).strftime("%Y-%m-%d %H:%M:%S")
                    feed.append({
                        "id": hash(rel),
                        "path": rel,
                        "type": fm.get("action_type", fm.get("type", "live-action")),
                        "symbol": fm.get("symbol"),
                        "tags": fm.get("tags", []),
                        "timestamp": ts_iso,
                        "label": _label_from_path(rel),
                    })
                except Exception:
                    pass
    except Exception:
        pass

    feed.sort(key=lambda x: x["timestamp"], reverse=True)
    return {
        "feed": feed[:limit],
        "total": len(notes),
    }


def _label_from_path(path: str) -> str:
    """Derive a short human label from a vault note path."""
    import re as _re
    name = path.split("/")[-1].replace(".md", "")
    # Remove date prefix
    name = _re.sub(r"^\d{4}-\d{2}-\d{2}-?", "", name)
    # Remove trailing timestamp suffix
    name = _re.sub(r"-\d{5,}$", "", name)
    return name.replace("-", " ").title()[:50]


# ═══════════════════════════════════════════════════════════════════════════════
# Active signals overlay (for Intelligence brain map)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/signals-overlay", summary="Recent signals for brain map overlay")
async def signals_overlay(db: AsyncSession = Depends(get_db)):
    """
    Returns recent signals and agent decisions formatted for the Intelligence
    brain map overlay — each with symbol, action, confidence, and timestamp.
    The frontend uses this to highlight and pulse relevant brain graph nodes.
    """
    from sqlalchemy import desc as sqldesc
    try:
        from app.models.database import Signal, AgentDecision
        sig_result = await db.execute(
            select(Signal).order_by(sqldesc(Signal.timestamp)).limit(20)
        )
        signals = sig_result.scalars().all()

        dec_result = await db.execute(
            select(AgentDecision).order_by(sqldesc(AgentDecision.created_at)).limit(20)
        )
        decisions = dec_result.scalars().all()

        return {
            "signals": [
                {
                    "id": s.id,
                    "symbol": s.symbol,
                    "action": getattr(s, "action", ""),
                    "confidence": getattr(s, "confidence", None),
                    "source": getattr(s, "source", ""),
                    "timestamp": str(getattr(s, "timestamp", "")),
                }
                for s in signals
            ],
            "decisions": [
                {
                    "id": d.id,
                    "symbol": getattr(d, "symbol", ""),
                    "action": getattr(d, "recommended_action", ""),
                    "agent_role": getattr(d, "agent_role", ""),
                    "confidence": getattr(d, "confidence", None),
                    "ai_called": getattr(d, "ai_called", False),
                    "timestamp": str(getattr(d, "created_at", "")),
                }
                for d in decisions
            ],
        }
    except Exception as exc:
        return {"signals": [], "decisions": [], "error": str(exc)}

# ═══════════════════════════════════════════════════════════════════════════════
# Insights harvest — captures full brain state from /insights page
# ═══════════════════════════════════════════════════════════════════════════════

class InsightsHarvestRequest(BaseModel):
    decisions: list = []
    news_articles: list = []
    sentiments: list = []
    learning_stats: dict = {}
    pipeline_status: dict = {}
    paul_knowledge_stats: dict = {}


@router.post("/insights/harvest", summary="Capture full insights brain snapshot")
async def harvest_insights(
    body: InsightsHarvestRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Called when the user visits /insights.
    Writes a full brain snapshot to the vault so Jarvis can reference the
    complete intelligence state (decisions, news, sentiment, learning) in
    future conversations.

    Also triggers a Jarvis learning note summarising the brain state.
    """
    writer = _writer()

    # 1. Write the comprehensive insights snapshot note
    snap_path, snap_written, snap_cs = writer.write_insights_snapshot(
        decisions=body.decisions[:50],
        news_articles=body.news_articles[:20],
        sentiments=body.sentiments[:20],
        learning_stats=body.learning_stats,
        pipeline_status=body.pipeline_status,
        paul_knowledge_stats=body.paul_knowledge_stats,
    )
    snap_rel = str(snap_path.relative_to(writer.root))

    # 2. Track in DB
    result = await db.execute(select(VaultNote).where(VaultNote.path == snap_rel))
    existing = result.scalar_one_or_none()
    now = datetime.utcnow()
    if existing:
        existing.checksum = snap_cs
        existing.updated_at = now
    else:
        db.add(VaultNote(
            path=snap_rel,
            note_type="insights-snapshot",
            tags=["insights", "snapshot"],
            checksum=snap_cs,
            created_at=now,
            updated_at=now,
        ))
    await db.commit()

    # 3. Push to Obsidian live
    bridge = get_bridge()
    pushed = False
    if bridge.enabled and snap_path.exists():
        pushed = await bridge.push_note(snap_rel, snap_path.read_text(encoding="utf-8"))

    # 4. Write a Jarvis learning note summarising brain state
    ls = body.learning_stats
    pk = body.paul_knowledge_stats
    summary = (
        f"Brain state snapshot: {ls.get('total_decisions',0)} total decisions, "
        f"{ls.get('ai_calls',0)} AI calls, {ls.get('local_pct',0):.1f}% local. "
        f"Knowledge base: {pk.get('knowledge_total',0)} items. "
        f"Sentiment samples: {len(body.sentiments)}. "
        f"Latest news: {len(body.news_articles)} articles."
    )
    learn_path, learn_written, learn_cs = writer.write_jarvis_note(
        question=f"TradeBot brain state snapshot — {now.date()}",
        answer=summary,
        page="/insights",
        tags=["insights", "snapshot", "brain-state"],
    )
    if learn_written and bridge.enabled:
        await bridge.push_note(
            str(learn_path.relative_to(writer.root)),
            learn_path.read_text(encoding="utf-8"),
        )

    return {
        "snapshot_written": snap_written,
        "pushed_to_obsidian": pushed,
        "snapshot_path": snap_rel,
        "learning_note_written": learn_written,
    }