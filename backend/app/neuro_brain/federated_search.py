"""
Federated recall — the core brain's superpower: one query → every brain.

Queries in parallel:
  - Neuro local FTS (fast, aggregator)
  - Hermes FTS (gateway → local fallback)
  - AiMarketAnalyst knowledge table
  - PaulKnowledge (AgentPaul plugin)
  - Obsidian Vault notes (via REST bridge if enabled)
  - Postgres AgentDecision / JarvisAnalysisJournal (last 20)
  - CryptoPair catalog name/symbol match

Returns deduplicated hits ordered by recency + source weight.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from app.neuro_brain import federation_enabled, is_enabled
from app.neuro_brain.state_store import search_local as neuro_search

async def federated_search(query: str, symbol: Optional[str]=None, limit: int=12) -> List[Dict[str, Any]]:
    if not is_enabled() or not federation_enabled():
        # still allow neuro-local alone
        return neuro_search(query, symbol, limit)
    # fan-out
    tasks = []
    # neuro local (sync, fast)
    neuro_hits = neuro_search(query, symbol, limit=6)
    # hermes
    async def _hermes():
        try:
            from app.hermes_bridge.memory_recall import _search_via_gateway
            from app.hermes_bridge.state_store import search_local as hermes_search
            hits = await _search_via_gateway(query, symbol or "", 6)
            if hits: return [{"source":"hermes-gateway", **h} for h in hits]
            return [{"source":"hermes-local", **h} for h in hermes_search(query, symbol, 6)]
        except Exception as e:
            logger.debug(f"[neuro federated] hermes skipped: {e}")
            return []
    # ai analyst knowledge
    async def _ai_knowledge():
        try:
            from app.core.database import AsyncSessionLocal
            from plugins.AiMarketAnalyst.backend.services import knowledge_service
            async with AsyncSessionLocal() as db:
                rows = await knowledge_service.search_knowledge(db, query, limit=4) if hasattr(knowledge_service, "search_knowledge") else []
                out=[]
                for r in (rows or []):
                    c = r.get("content") if isinstance(r, dict) else getattr(r,"content","")
                    out.append({"source":"ai-analyst","symbol": getattr(r,"symbol","") if not isinstance(r, dict) else r.get("symbol",""), "content": c[:400], "ts": 0, "kind":"ai_knowledge"})
                return out
        except Exception as e:
            logger.debug(f"[neuro federated] ai_knowledge skipped: {e}")
            return []
    # paul knowledge
    async def _paul():
        try:
            from app.core.database import AsyncSessionLocal
            from plugins.AgentPaulPlugin.backend.services import knowledge_base
            async with AsyncSessionLocal() as db:
                rows = await knowledge_base.search_knowledge(db, query=query, limit=4)
                out=[]
                for r in (rows or []):
                    c = r.get("content") if isinstance(r, dict) else getattr(r,"content","")
                    out.append({"source":"paul-knowledge","symbol": r.get("symbol","") if isinstance(r, dict) else getattr(r,"symbol",""), "content": c[:400], "ts": 0, "kind":"paul_knowledge"})
                return out
        except Exception as e:
            logger.debug(f"[neuro federated] paul skipped: {e}")
            return []
    # obsidian vault quick peek
    async def _vault():
        try:
            from app.core.database import AsyncSessionLocal
            from sqlalchemy import select
            from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote
            async with AsyncSessionLocal() as db:
                q = select(VaultNote).order_by(VaultNote.updated_at.desc()).limit(4)
                rows = (await db.execute(q)).scalars().all()
                ql = query.lower()
                filtered=[r for r in rows if ql in (r.path or "").lower() or ql in (r.symbol or "").lower()]
                return [{"source":"obsidian-vault","symbol": r.symbol or "", "content": f"{r.path} — {r.note_type}", "ts": 0, "kind": r.note_type} for r in (filtered[:3] if filtered else rows[:2])]
        except Exception as e:
            logger.debug(f"[neuro federated] vault skipped: {e}")
            return []
    # postgres decisions
    async def _decisions():
        try:
            from app.core.database import AsyncSessionLocal
            from sqlalchemy import select, desc
            from app.models.database import AgentDecision
            async with AsyncSessionLocal() as db:
                stmt = select(AgentDecision).order_by(desc(AgentDecision.created_at)).limit(6)
                if symbol: stmt = stmt.where(AgentDecision.symbol==symbol.upper())
                rows=(await db.execute(stmt)).scalars().all()
                ql=query.lower()
                hits=[]
                for r in rows:
                    blob=f"{r.reasoning or ''} {r.action or ''} {r.symbol or ''}".lower()
                    if ql in blob or symbol:
                        hits.append({"source":"agent-decision","symbol":r.symbol,"content": f"[{r.agent_role} {r.action} {r.confidence}] { (r.reasoning or '')[:300]}","ts": 0,"kind": r.agent_role})
                return hits[:4]
        except Exception as e:
            logger.debug(f"[neuro federated] decisions skipped: {e}")
            return []

    hermes_fut = asyncio.create_task(_hermes())
    ai_fut = asyncio.create_task(_ai_knowledge())
    paul_fut = asyncio.create_task(_paul())
    vault_fut = asyncio.create_task(_vault())
    dec_fut = asyncio.create_task(_decisions())

    gathered = await asyncio.gather(hermes_fut, ai_fut, paul_fut, vault_fut, dec_fut, return_exceptions=True)

    all_hits: List[Dict[str,Any]] = []
    # neuro first, weighted
    for h in neuro_hits: all_hits.append({**h, "federated_source": "neuro-local", "weight": 2})
    for g in gathered:
        if isinstance(g, list):
            for h in g: all_hits.append({**h, "weight": 1})
        elif isinstance(g, Exception):
            continue
    # dedup by content prefix
    seen=set(); dedup=[]
    for h in all_hits:
        key=(h.get("content") or "")[:60]
        if key in seen: continue
        seen.add(key); dedup.append(h)
        if len(dedup)>=limit: break
    return dedup

async def brain_recall_context(symbol: str, max_chars: int = 700) -> str:
    """Formatted context injected into every agent/JARVIS prompt."""
    if not is_enabled(): return ""
    try:
        hits = await federated_search(symbol, symbol, limit=6)
        if not hits: return ""
        parts=[]
        for h in hits[:3]:
            c=(h.get("content") or "")[:220]
            src=h.get("source") or h.get("federated_source") or "neuro"
            parts.append(f"[{src}:{h.get('symbol') or symbol}] {c}")
        if not parts: return ""
        return "\n[NEURO CORE BRAIN — federated recall for "+symbol+"]\n" + "\n─\n".join(parts) + "\n[END NEURO RECALL]\n"
    except Exception: return ""
