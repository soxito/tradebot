"""
JARVIS Assistant API
====================
Positions monitor + voice-command executor for all connected crypto exchanges.

Endpoints:
  GET  /jarvis/positions              → all open positions (all exchanges)
  POST /jarvis/command                → parse & execute a voice command
  GET  /jarvis/portfolio              → portfolio summary (total PnL, equity)
  POST /jarvis/voice-brain/sync       → persist voice fingerprint + vocabulary to vault
  GET  /jarvis/voice-brain/load       → restore voice fingerprint + vocabulary from vault
  POST /jarvis/voice-brain/identify   → compare submitted frequency bands → confidence score
"""
from __future__ import annotations

import json
import re
import asyncio
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel

from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.forex_provider import is_forex_symbol, fetch_ohlcv as forex_fetch_ohlcv
from app.services import market_data

router = APIRouter(prefix="/jarvis", tags=["jarvis"])


# ── Triple-brain learning capture ────────────────────────────────────────────
# Every JARVIS action, command, and chat response is persisted to THREE brains:
#   1. Obsidian vault file + VaultNote DB row  → /vault list + /intelligence live feed
#   2. AI Analyst knowledge store              → /intelligence knowledge panel
#   3. PaulKnowledge long-term memory          → recalled into future chat context
# Fire-and-forget: never blocks the response.
def jarvis_brain_capture(
    action: str,
    symbol: str = "",
    summary: str = "",
    detail: str = "",
    tags: Optional[List[str]] = None,
    order_id: str = "",
    importance: float = 0.5,
) -> None:
    """Backward-compatible wrapper — delegates to jarvis_learn_all_brains."""
    jarvis_learn_all_brains(
        action=action, symbol=symbol, summary=summary, detail=detail,
        tags=tags, order_id=order_id, importance=importance,
    )


async def _knowledge_capture(
    action: str, symbol: str, summary: str, detail: str, importance: float
) -> None:
    """Inner coroutine — writes the action to the PaulKnowledge brain."""
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AgentPaulPlugin.backend.services import knowledge_base

        text = f"JARVIS {action}" + (f" {symbol}" if symbol else "")
        text += f": {summary}".rstrip(": ")
        if detail:
            text += f" — {detail[:300]}"
        async with AsyncSessionLocal() as db:
            await knowledge_base.record_knowledge(
                db,
                kind="insight",
                content=text[:1000],
                source="jarvis-command",
                symbol=symbol or None,
                topic=action,
                importance=importance,
            )
    except Exception as e:  # pragma: no cover - best effort
        logger.debug(f"[JARVIS brain] knowledge write skipped: {e}")


async def _ai_analyst_capture(
    action: str, symbol: str, summary: str, detail: str,
    kind: str = "insight", importance: float = 0.5
) -> None:
    """Write to the AI Analyst knowledge brain (powers /intelligence panel)."""
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services import knowledge_service
        title = f"JARVIS {action}" + (f" — {symbol}" if symbol else "")
        body = summary
        if detail:
            body += f"\n\n{detail[:600]}"
        async with AsyncSessionLocal() as db:
            await knowledge_service.store_knowledge(
                db,
                content=body[:1200],
                agent_role="jarvis",
                symbol=symbol or None,
                kind=kind,
                title=title,
                weight=max(1.3, importance * 2.0),  # floor at 1.3 to stay visible
                source="jarvis",
            )
    except Exception as e:
        logger.debug(f"[JARVIS brain] AI-analyst store skipped: {e}")


async def _vault_capture_with_db(
    action: str, symbol: str, summary: str, detail: str,
    tags: Optional[List[str]] = None, order_id: str = "",
) -> None:
    """Write a vault file AND register a VaultNote DB row so the note appears
    in the /vault list and the /intelligence live feed."""
    try:
        from datetime import datetime as _dt
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
        from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge
        from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote
    except Exception:
        return

    try:
        writer = VaultWriter()
        path, written, cs = writer.write_action_note(
            action_type=f"jarvis-{action}",
            symbol=symbol,
            summary=summary[:200],
            detail=detail,
            tags=tags or ["jarvis", action],
            agent_role="jarvis",
            order_id=order_id or "",
        )
        rel = str(path.relative_to(writer.root))
    except Exception as exc:
        logger.debug(f"[JARVIS brain] vault write skipped: {exc}")
        return

    # Register in DB so /vault + /intelligence live feed pick it up.
    try:
        async with AsyncSessionLocal() as db:
            existing = (
                await db.execute(select(VaultNote).where(VaultNote.path == rel))
            ).scalar_one_or_none()
            now = _dt.utcnow()
            if existing:
                existing.checksum = cs
                existing.updated_at = now
            else:
                db.add(VaultNote(
                    path=rel,
                    note_type=f"jarvis-{action}",
                    symbol=symbol or None,
                    tags=tags or ["jarvis", action],
                    checksum=cs,
                    created_at=now,
                    updated_at=now,
                ))
            await db.commit()
    except Exception as exc:
        logger.debug(f"[JARVIS brain] vault DB register skipped: {exc}")

    # Best-effort live push to Obsidian app.
    try:
        bridge = get_bridge()
        if getattr(bridge, "enabled", False):
            await bridge.push_note(rel, path.read_text(encoding="utf-8"))
    except Exception:
        pass


def jarvis_learn_all_brains(
    action: str,
    symbol: str = "",
    summary: str = "",
    detail: str = "",
    tags: Optional[List[str]] = None,
    order_id: str = "",
    importance: float = 0.5,
    kind: str = "insight",
) -> None:
    """Persist any JARVIS event to ALL three knowledge brains:
    vault (file + DB row), AI Analyst store, and PaulKnowledge.
    Fire-and-forget — never blocks the caller."""
    try:
        loop = asyncio.get_running_loop()
        _tags = list(set((tags or []) + ["jarvis", action] + ([symbol] if symbol else [])))
        loop.create_task(
            _vault_capture_with_db(action, symbol, summary, detail, _tags, order_id or "")
        )
        loop.create_task(
            _ai_analyst_capture(action, symbol, summary, detail, kind, importance)
        )
        loop.create_task(
            _knowledge_capture(action, symbol, summary, detail, importance)
        )
    except Exception as e:
        logger.debug(f"[JARVIS brain] learn_all_brains scheduling skipped: {e}")


# ── Multi-Model Brain Management Network ─────────────────────────────────────
# ALL available enabled AI providers act as dedicated brain managers that run
# CONCURRENTLY in the background after every JARVIS analysis.  No hardcoded
# provider — the brain dynamically uses whatever providers you have configured
# in the Connect AI tab.  Add a new provider there: auto-included next cycle.
#
#   slot 0  BRAIN CONSOLIDATOR
#     Fuses all parallel task model outputs into a cross-model brain-map entry.
#     "Models talking to each other": highest-priority provider reads what every
#     task model said and synthesizes a single, high-importance brain entry.
#
#   slot 1  BRAIN INDEXER
#     Extracts structured signal patterns (BIAS / KEY_LEVELS / SMC / STRENGTH)
#     from the synthesized narrative into a searchable structured index.
#
#   slot 2  BRAIN CRITIC
#     Adversarially challenges the consolidator output: finds missed risks,
#     ignored counter-signals, and over-confidence.  Balances the brain view.
#
#   slot 3  BRAIN RESEARCHER
#     Enriches analysis with macro context, correlated assets, historical
#     precedents, and on-chain / funding / sentiment context.
#
#   slot 4  BRAIN NEWS ORGANISER
#     Curates fresh market headlines into structured BULLISH / BEARISH /
#     BREAKING / FOMO / MACRO / SENTIMENT briefings stored at importance 0.70.
#
# Communication flow (models talk to each other via the brain layer):
#
#   ┌──────────────────────────────────────────────────────────────┐
#   │  TASK MODELS (analysis cycle)                                │
#   │  o3 / NVIDIA / GPT-4o / Cerebras / Gemini / all providers  │
#   │     ↓  produce: market, volume, news, synthesis outputs      │
#   ├──────────────────────────────────────────────────────────────┤
#   │  DYNAMIC MULTI-PROVIDER BRAIN NETWORK (all roles concurrent) │
#   │  slot 0: CONSOLIDATOR  → fuses ALL task outputs (prio-1)    │
#   │  slot 1: INDEXER       → extracts structured patterns        │
#   │  slot 2: CRITIC        → adversarial review of slot-0 map   │
#   │  slot 3: RESEARCHER    → macro/historical context note       │
#   │  slot 4: NEWS ORGANISER→ market news briefing curation       │
#   │                                                              │
#   │  NEW provider in Connect AI → auto-included, no code change  │
#   │  Pool wraps around: slots spread fairly if <5 providers      │
#   ├──────────────────────────────────────────────────────────────┤
#   │  NEXT ANALYSIS CYCLE  ← brain_recall_context()              │
#   │     task models receive enriched context from prior brains   │
#   └──────────────────────────────────────────────────────────────┘
# ─────────────────────────────────────────────────────────────────────────────

# ── Dynamic Brain Role Pool ───────────────────────────────────────────────────
# The brain network dynamically uses ALL available enabled AI providers.
# Each brain role is assigned to a different provider from the pool using a
# deterministic spread so every configured provider contributes to thinking:
#
#   slot 0 → brain_consolidator    (synthesize all task model outputs)
#   slot 1 → brain_indexer         (extract structured signal patterns)
#   slot 2 → brain_critic          (adversarial validation / challenge)
#   slot 3 → brain_researcher      (deep macro + historical context)
#   slot 4 → brain_news_organiser  (market news briefing curation)
#
# Adding a new AI provider in the Connect AI tab → it is automatically
# included in the next brain cycle with zero code changes.
# Pool wraps around: if only 2 providers are enabled, slots 0,1 each get
# dedicated models, slots 2-4 reuse them in a fair round-robin manner.
# ─────────────────────────────────────────────────────────────────────────────

_BRAIN_ROLE_SLOTS: Dict[str, int] = {
    "brain_consolidator":  0,  # synthesis of all task outputs
    "brain_indexer":       1,  # structured pattern extraction
    "brain_critic":        2,  # adversarial review
    "brain_researcher":    3,  # deep macro/historical context
    "brain_news_organiser":4,  # news curation
}


async def _dedicated_brain_provider(db, role: str):
    """The profile dedicated to this brain role, if one is set.

    Returns None when the role has no profile, so the caller falls back to the
    shared slot-spread pool. That keeps the brain running while the roles are
    still being configured — the requirement is surfaced in the settings UI
    rather than enforced by refusing to think.
    """
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            dedicated_profile_for, _cb_open, _is_capped, get_router_settings,
        )
        p = await dedicated_profile_for(db, role)
        if p is None or not (p.api_key and p.base_url):
            return None
        settings = await get_router_settings(db)
        if _cb_open(p.id) or _is_capped(p, settings.reserve_pct):
            return None
        return p
    except Exception as e:
        logger.debug(f"[JARVIS brain-{role}] dedicated lookup failed: {e}")
        return None


def _brain_load(p) -> tuple:
    """Sort key: healthy first, then least-used.

    Health leads because low usage is not always a good sign — a provider that
    rejects everything accrues no calls and would otherwise rank as the
    emptiest key on the shelf, so "least loaded" would send every unkeyed brain
    role straight at the one thing known to be broken.

    Within a health tier, raw call counts are not comparable across different
    caps: 200 calls against a 10k/day tier has far more headroom left than 200
    against a 250/day one. A capped provider is ranked by the *fraction* of its
    allowance spent, an uncapped one by absolute calls scaled onto the same 0-1
    axis, so a busy uncapped key cannot always look cheapest.
    """
    unhealthy = 1 if (getattr(p, "status", "") or "") == "error" else 0
    daily, monthly = (p.daily_calls or 0), (p.monthly_calls or 0)
    if p.daily_limit:
        primary = daily / max(1, p.daily_limit)
    elif p.monthly_limit:
        primary = monthly / max(1, p.monthly_limit)
    else:
        # No published cap: treat 1000 calls/day as a full load so the number
        # lands on the same scale as the ratios above.
        primary = min(1.0, daily / 1000.0)
    # Ties broken by absolute recent volume, then id so ordering is stable
    # within a cycle rather than shuffling between concurrent roles.
    return (unhealthy, round(primary, 4), daily, monthly, p.id)


async def _get_brain_pool(db) -> list:
    """Available shared-pool providers, least-used first.

    Skips circuit-open and usage-capped providers so the brain always uses
    working, headroom-positive providers. Dedicated profiles are already
    excluded by ``get_enabled_providers``, so this is strictly the shared pool.

    Ordered by remaining headroom rather than priority: a brain role with no key
    of its own has to borrow from the pool, and borrowing from whichever key is
    already busiest is how the roles that *do* have keys end up waiting behind
    rate limits they never caused. The slot spread in :func:`_brain_call` still
    applies on top, so distinct roles keep landing on distinct providers when
    the pool is large enough.
    """
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            get_enabled_providers, get_router_settings, _cb_open, _is_capped,
        )
        settings = await get_router_settings(db)
        providers = await get_enabled_providers(db)
        usable = [
            p for p in providers
            if p.api_key and p.base_url
            and not _cb_open(p.id)
            and not _is_capped(p, settings.reserve_pct)
        ]
        return sorted(usable, key=_brain_load)
    except Exception as e:
        logger.debug(f"[JARVIS brain-pool] pool build failed: {e}")
        return []


async def _brain_call(
    role: str,
    messages: list,
    db,
    max_tokens: int = 400,
    temperature: float = 0.28,
) -> Optional[str]:
    """Call a brain manager using dynamic provider assignment.

    Each role is mapped to a slot index.  The provider at that slot in the
    ordered pool handles the call.  If that provider fails, falls back to
    db_chat() so the brain is NEVER blocked by a single model being down.
    Returns the response text or None.  Never raises.
    """
    from datetime import datetime as _dt

    slot = _BRAIN_ROLE_SLOTS.get(role, 0)

    try:
        # A profile dedicated to this brain role wins outright. The roles run
        # concurrently and adversarially, so sharing one key serialises the
        # cycle behind a single rate limit and lets the critic review the
        # consolidator on the very model that wrote it.
        provider = await _dedicated_brain_provider(db, role)

        if provider is None:
            # No key of its own — borrow the least-loaded shared key. The pool
            # arrives sorted by remaining headroom, and the slot index still
            # spreads concurrent roles across different providers rather than
            # sending every unkeyed role at whichever one is currently cheapest.
            pool = await _get_brain_pool(db)
            if not pool:
                # No providers available — brain is idle this cycle
                return None
            provider = pool[slot % len(pool)]
            logger.debug(
                f"[JARVIS brain-{role}] no dedicated key — borrowing "
                f"{provider.label!r} (load rank {slot % len(pool) + 1}/{len(pool)}, "
                f"{provider.daily_calls or 0} calls today)"
            )
        model = provider.default_model or ""
        if not model:
            try:
                from plugins.AiMarketAnalyst.backend.services.provider_presets import get_preset
                key = (provider.label or "").lower().split()[0]
                preset = get_preset(key)
                model = (preset or {}).get("default_model", "") if preset else ""
            except Exception:
                pass
        if not model:
            logger.debug(f"[JARVIS brain-{role}] {provider.label!r} has no default model — skip")
            return None

        # ── Try targeted provider ─────────────────────────────────────────────
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            _call_openai_compatible, _reset_usage_windows, _cb_trip, get_router_settings,
        )
        from plugins.AiMarketAnalyst.backend.models import AIUsageRecord as _AIRecord

        now = _dt.utcnow()
        _reset_usage_windows(provider, now)
        orig_chars = sum(len(str(m.get("content", ""))) for m in messages)

        try:
            content, usage, routed_via = await _call_openai_compatible(
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=False,
            )
            provider.total_calls = (provider.total_calls or 0) + 1
            provider.daily_calls = (provider.daily_calls or 0) + 1
            provider.monthly_calls = (provider.monthly_calls or 0) + 1
            provider.status = "ok"
            provider.last_error = None
            provider.last_model_used = routed_via or model
            provider.last_tested_at = now
            db.add(_AIRecord(
                provider_id=provider.id, provider_label=provider.label,
                agent_name=f"jarvis-brain-{role}", agent_role="jarvis",
                model=routed_via or model, source="jarvis-brain",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                orig_chars=orig_chars, comp_chars=orig_chars, success=True,
            ))
            await db.commit()
            logger.debug(
                f"[JARVIS brain-{role}] {provider.label}/{model} "
                f"(slot {slot}/{len(pool)} providers in pool)"
            )
            return content.strip() if content else None

        except Exception as exc:
            provider.total_errors = (provider.total_errors or 0) + 1
            provider.status = "error"
            provider.last_error = str(exc)[:300]
            db.add(_AIRecord(
                provider_id=provider.id, provider_label=provider.label,
                agent_name=f"jarvis-brain-{role}", agent_role="jarvis",
                model=model, source="jarvis-brain",
                orig_chars=orig_chars, comp_chars=orig_chars, success=False,
            ))
            await db.commit()
            _cb_trip(provider.id)
            logger.debug(
                f"[JARVIS brain-{role}] {provider.label!r} failed: {str(exc)[:120]} "
                "→ falling back to db_chat"
            )

        # ── db_chat fallback: any available provider ──────────────────────────
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
        fallback = await db_chat(
            db, messages, temperature=temperature, max_tokens=max_tokens,
            agent_name=f"jarvis-brain-{role}-fallback", source="jarvis-brain",
        )
        if fallback.get("ok") and fallback.get("content"):
            logger.debug(
                f"[JARVIS brain-{role}] fallback succeeded via {fallback.get('provider')}"
            )
            return str(fallback["content"]).strip()

    except Exception as e:
        logger.debug(f"[JARVIS brain-{role}] call skipped: {e}")
    return None


async def _brain_consolidate_outputs(
    task_outputs: Dict[str, str],
    symbol: str,
    db,
) -> None:
    """Brain Consolidator (Mistral): fuse all task model outputs into one entry.

    Reads what every task model said about this symbol and synthesizes a
    unified, high-importance brain-map entry.  This is how the task models
    'talk to each other' — Mistral mediates their collective intelligence.
    Fire-and-forget; never awaited directly by the analysis pipeline.
    """
    try:
        task_labels = _JARVIS_TASK_LABELS  # noqa: F821 — defined later in same file
        parts: List[str] = []
        for task_key, text in task_outputs.items():
            if text and len(text) > 30:
                label = task_labels.get(task_key, task_key)
                parts.append(f"[{label}]\n{text[:600]}")

        if not parts:
            return

        combined = "\n\n".join(parts)
        msgs = [
            {
                "role": "system",
                "content": (
                    "You are a trading intelligence consolidator. Multiple specialized AI models "
                    f"have analyzed {symbol or 'a financial instrument'} from different angles. "
                    "Synthesize their collective outputs into ONE concise brain-map entry. "
                    "Cover: (1) consensus directional bias, (2) key price levels, "
                    "(3) critical risk factor, (4) any notable model disagreement. "
                    "Write 3-5 tight sentences. No filler. Pure signal."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Synthesize these parallel AI analyses of {symbol or 'the market'} "
                    f"(from {len(parts)} specialized models):\n\n{combined}"
                ),
            },
        ]

        summary = await _brain_call("brain_consolidator", msgs, db, max_tokens=360, temperature=0.22)
        if summary:
            model_names = ", ".join(
                task_labels.get(t, t) for t in task_outputs.keys() if task_outputs.get(t)
            )
            jarvis_learn_all_brains(
                action="brain_consolidation",
                symbol=symbol,
                summary=f"Cross-model brain map [{symbol or 'market'}]: {summary[:180]}",
                detail=(
                    f"BRAIN CONSOLIDATOR (multi-provider slot 0) fused {len(parts)} model outputs: "
                    f"{model_names}.\n\nConsolidated brain map:\n{summary}"
                ),
                tags=["jarvis", "brain-consolidation", "cross-model-sync",
                      symbol or "market"],
                importance=0.85,   # elevated — cross-model synthesis is highest value
                kind="insight",
            )
            logger.debug(
                f"[JARVIS brain-consolidator] {symbol}: wrote {len(summary)}-char "
                "cross-model brain map"
            )
    except Exception as e:
        logger.debug(f"[JARVIS brain-consolidator] {symbol}: skipped: {e}")


async def _brain_index_patterns(
    symbol: str,
    narrative: str,
    db,
) -> None:
    """Brain Indexer (Gemma 4 via OpenRouter): extract structured signal patterns.

    Reads the synthesized JARVIS narrative and produces a machine-readable
    signal index (bias, levels, SMC pattern, setup) stored in the brain stores
    for fast structured recall on the next analysis cycle.
    Fire-and-forget; never awaited directly by the analysis pipeline.
    """
    if not narrative or len(narrative) < 50:
        return
    try:
        msgs = [
            {
                "role": "system",
                "content": (
                    "You are a signal pattern indexer for a trading brain system. "
                    "Extract and structure the tradeable information from the analysis. "
                    "Reply ONLY in this exact format (one value per line):\n"
                    "BIAS: [bullish/bearish/neutral]\n"
                    "KEY_LEVELS: [comma-separated key prices]\n"
                    "SMC_PATTERN: [order block/FVG/BOS/CHoCH/liquidity sweep/none]\n"
                    "SIGNAL_STRENGTH: [1-10]\n"
                    "RISK_FACTOR: [single biggest risk in ≤10 words]\n"
                    "TRADE_SETUP: [entry approach in one line]\n"
                    "No explanation. No extra text. Just the 6 lines."
                ),
            },
            {
                "role": "user",
                "content": f"Index this {symbol or 'market'} analysis:\n\n{narrative[:900]}",
            },
        ]

        pattern_index = await _brain_call(
            "brain_indexer", msgs, db, max_tokens=180, temperature=0.08
        )
        if pattern_index:
            jarvis_learn_all_brains(
                action="brain_signal_index",
                symbol=symbol,
                summary=f"Signal index [{symbol or 'market'}]: {pattern_index[:120]}",
                detail=(
                    f"BRAIN INDEXER (multi-provider slot 1) structured signal patterns:\n\n{pattern_index}"
                ),
                tags=["jarvis", "brain-index", "signal-pattern", symbol or "market"],
                importance=0.75,
                kind="signal",
            )
            logger.debug(f"[JARVIS brain-indexer] {symbol}: wrote pattern index")
    except Exception as e:
        logger.debug(f"[JARVIS brain-indexer] {symbol}: skipped: {e}")


async def _brain_recall_context(symbol: str, max_chars: int = 600) -> str:
    """Pull recent brain consolidations AND news briefings for `symbol`.

    Returns a formatted context string prepended to task model system prompts,
    giving them:
      - Prior cross-model consolidated brain maps (Mistral consolidator)
      - Structured signal patterns (Mistral indexer)
      - Latest organised market news briefings (Mistral news organiser)

    Returns empty string when no relevant prior brain entries exist.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AgentPaulPlugin.backend.services import knowledge_base

        async with AsyncSessionLocal() as db:
            # Query 1: symbol-specific brain intelligence
            rows_sym = await knowledge_base.search_knowledge(
                db,
                query=f"{symbol} consolidated brain signal bias news",
                limit=6,
            )
            # Query 2: recent market news briefings (general + symbol-specific)
            rows_news = await knowledge_base.search_knowledge(
                db,
                query="market news briefing BULLISH BEARISH BREAKING FOMO MACRO",
                limit=4,
            )

        all_rows = list(rows_sym or []) + list(rows_news or [])
        if not all_rows:
            return ""

        brain_parts: List[str] = []
        news_parts: List[str] = []
        consumed = 0

        seen: set = set()
        for row in all_rows:
            content: str = (
                row.get("content") if isinstance(row, dict) else getattr(row, "content", "")
            ) or ""
            key = content[:40]
            if key in seen:
                continue
            seen.add(key)

            is_brain = (
                "brain_consolidat" in content.lower()
                or "brain_signal" in content.lower()
                or "cross-model" in content.lower()
                or (symbol and symbol.upper() in content.upper() and "BRAIN" in content.upper())
            )
            is_news = (
                "brain_news" in content.lower()
                or "BULLISH:" in content
                or "BEARISH:" in content
                or "BREAKING:" in content
                or "FOMO:" in content
                or "MACRO:" in content
                or "SENTIMENT:" in content
            )

            if is_brain and content:
                brain_parts.append(content[:200])
                consumed += 200
            elif is_news and content:
                news_parts.append(content[:220])
                consumed += 220

            if consumed >= max_chars:
                break

        sections: List[str] = []
        if brain_parts:
            sections.append("PRIOR INTELLIGENCE:\n" + "\n─\n".join(brain_parts[:2]))
        if news_parts:
            sections.append("LATEST MARKET NEWS (organised by JARVIS brain):\n" + "\n─\n".join(news_parts[:2]))

        if not sections:
            return ""

        joined = "\n\n".join(sections)
        return (
            f"\n[JARVIS BRAIN MEMORY — context for {symbol}]\n"
            f"{joined}\n"
            f"[END BRAIN MEMORY]\n"
        )
    except Exception as e:
        logger.debug(f"[JARVIS brain-recall] {symbol}: skipped: {e}")
        return ""


async def _brain_critique_analysis(
    symbol: str,
    consolidated_summary: str,
    db,
) -> None:
    """Brain Critic (slot 2 provider): adversarially challenge the consolidated output.

    A different provider from the consolidator challenges the brain map,
    identifying over-confident claims, ignored counter-signals, and missed
    risks.  Stored so JARVIS maintains a balanced perspective.
    Fire-and-forget; never blocks the analysis pipeline.
    """
    if not consolidated_summary or len(consolidated_summary) < 50:
        return
    try:
        msgs = [
            {
                "role": "system",
                "content": (
                    "You are an adversarial trading critic embedded in JARVIS's brain. "
                    "Your job is to CHALLENGE and find what was missed or overstated. "
                    "Do NOT repeat what the analysis said. Instead: "
                    "(1) Identify the biggest weakness or ignored risk, "
                    "(2) Name one counter-scenario that invalidates the bias, "
                    "(3) Flag any over-confidence or critical missing context. "
                    "3-4 tight sentences. Pure critique, no filler."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Critically challenge this {symbol or 'market'} brain analysis:\n\n"
                    f"{consolidated_summary[:800]}"
                ),
            },
        ]
        critique = await _brain_call(
            "brain_critic", msgs, db, max_tokens=200, temperature=0.42
        )
        if critique:
            jarvis_learn_all_brains(
                action="brain_critique",
                symbol=symbol,
                summary=f"Brain critique [{symbol or 'market'}]: {critique[:160]}",
                detail=(
                    f"BRAIN CRITIC (multi-provider slot 2) adversarial review:\n\n{critique}"
                ),
                tags=["jarvis", "brain-critique", "adversarial", symbol or "market"],
                importance=0.72,
                kind="insight",
            )
            logger.debug(f"[JARVIS brain-critic] {symbol}: wrote adversarial critique")
    except Exception as e:
        logger.debug(f"[JARVIS brain-critic] {symbol}: skipped: {e}")


async def _brain_deep_research(
    symbol: str,
    context_brief: str,
    db,
) -> None:
    """Brain Researcher (slot 3 provider): deep macro + historical context note.

    Uses a dedicated provider to enrich JARVIS's brain with:
    - Macro/sector context for the asset
    - Correlated assets showing similar or diverging patterns
    - Historical precedent for the current setup
    - On-chain / funding / sentiment enrichment

    Stored at importance 0.73 so it enriches future analysis cycles.
    Fire-and-forget.
    """
    if not symbol:
        return
    try:
        user_content = (
            f"Research context for {symbol}:\n\n"
            f"{context_brief[:600] if context_brief else 'No prior context.'}"
        )
        # Agent-Reach live search grounds this brain in real, fetched sources
        # instead of pure model recall. Fire-and-forget already, so the added
        # latency is invisible to the user. No-op unless AGENT_REACH_ENABLED.
        from app.core.config import settings as _app_settings
        if _app_settings.AGENT_REACH_ENABLED:
            try:
                from app.services import agent_reach_client
                live_research = await agent_reach_client.research_summary_for_symbol(
                    symbol, token_budget=600
                )
                if live_research:
                    user_content += f"\n\nLive web research for {symbol}:\n{live_research}"
            except Exception as _ar_exc:
                logger.debug(f"[JARVIS brain-researcher] Agent-Reach context skipped: {_ar_exc}")

        msgs = [
            {
                "role": "system",
                "content": (
                    "You are a deep-research specialist embedded in JARVIS's trading brain. "
                    "Given a symbol and recent analysis context, produce a rich research note: "
                    "(1) Macro/sector context affecting this asset right now, "
                    "(2) Correlated assets (BTC dominance, DXY, S&P500, sector ETFs) "
                    "showing similar or diverging patterns, "
                    "(3) One key historical precedent for this setup, "
                    "(4) On-chain / funding / whale / social sentiment context. "
                    "3-5 tight sentences. Actionable context. No filler."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ]
        research = await _brain_call(
            "brain_researcher", msgs, db, max_tokens=280, temperature=0.35
        )
        if research:
            jarvis_learn_all_brains(
                action="brain_research",
                symbol=symbol,
                summary=f"Deep research [{symbol}]: {research[:160]}",
                detail=(
                    f"BRAIN RESEARCHER (multi-provider slot 3) contextual note:\n\n{research}"
                ),
                tags=["jarvis", "brain-research", "macro-context", symbol],
                importance=0.73,
                kind="insight",
            )
            logger.debug(f"[JARVIS brain-researcher] {symbol}: wrote research note")
    except Exception as e:
        logger.debug(f"[JARVIS brain-researcher] {symbol}: skipped: {e}")


async def _brain_openhuman_sync(
    symbol: str,
    brain_summary: str,
    db,
) -> None:
    """OpenHuman Brain Sync: update OpenHuman's cognitive state from the brain.

    Writes the latest consolidated brain output to the OpenHuman plugin's
    memory store so it can adapt JARVIS's tone and awareness to the current
    market context (bullish/bearish/uncertain cognitive state).
    Fire-and-forget; gracefully skips if OpenHumanPlugin is not installed.
    """
    if not brain_summary:
        return
    try:
        from plugins.OpenHumanPlugin.backend.services.memory_sync_service import (
            push_jarvis_brain_state,
        )
        await push_jarvis_brain_state(
            symbol=symbol,
            content=brain_summary[:800],
        )
        logger.debug(f"[JARVIS brain-openhuman] {symbol}: synced to OpenHuman brain")
    except ImportError:
        pass  # OpenHumanPlugin not installed — silent
    except Exception as e:
        logger.debug(f"[JARVIS brain-openhuman] {symbol}: skipped: {e}")


def _fire_brain_managers(
    task_outputs: Dict[str, str],
    symbol: str,
) -> None:
    """Launch ALL brain managers concurrently after a full analysis cycle.

    Uses ALL available enabled AI providers dynamically — each manager gets a
    different provider slot so the brain leverages every configured model:

    • brain_consolidator  (slot 0): fuses all task outputs → cross-model brain map
    • brain_indexer       (slot 1): extracts structured signal patterns
    • brain_critic        (slot 2): adversarial review of consolidator output
    • brain_researcher    (slot 3): deep macro + historical context note
    • OpenHuman sync      (post):   updates OpenHuman cognitive state

    Adding a new AI provider in Connect AI tab → automatically included next cycle.
    All managers run fire-and-forget — this function returns immediately.
    """
    synthesis_text = (
        task_outputs.get("synthesis")
        or task_outputs.get("market_analysis")
        or ""
    )
    try:
        loop = asyncio.get_running_loop()

        async def _run_all_managers() -> None:
            from app.core.database import AsyncSessionLocal

            # Phase 1: consolidator + indexer run concurrently
            # Each gets its OWN DB session to prevent SQLAlchemy interleaving.
            async def _consolidate():
                async with AsyncSessionLocal() as _db:
                    await _brain_consolidate_outputs(task_outputs, symbol, _db)

            async def _index():
                async with AsyncSessionLocal() as _db:
                    await _brain_index_patterns(symbol, synthesis_text, _db)

            await asyncio.gather(_consolidate(), _index(), return_exceptions=True)

            # Phase 2: critic + researcher + OpenHuman run on the synthesis text
            # (They use the synthesis as input context, not the raw task map.)
            async def _critique():
                async with AsyncSessionLocal() as _db:
                    await _brain_critique_analysis(symbol, synthesis_text, _db)

            async def _research():
                async with AsyncSessionLocal() as _db:
                    await _brain_deep_research(symbol, synthesis_text, _db)

            async def _openhuman():
                async with AsyncSessionLocal() as _db:
                    await _brain_openhuman_sync(symbol, synthesis_text, _db)

            await asyncio.gather(
                _critique(), _research(), _openhuman(), return_exceptions=True
            )

        loop.create_task(_run_all_managers())
        logger.debug(
            f"[JARVIS brain-network] {symbol}: launched 5 brain managers "
            "(consolidator \u00b7 indexer \u00b7 critic \u00b7 researcher \u00b7 OpenHuman) "
            "— ALL available providers auto-assigned across slots"
        )
    except Exception as e:
        logger.debug(f"[JARVIS brain-network] {symbol}: manager launch skipped: {e}")


# ── Brain News Collector ──────────────────────────────────────────────────────
# Idle Mistral (open-mistral-nemo) continuously curates the latest market news
# from all integrated sources (CoinMarketCap, Yahoo Finance, crypto RSS feeds,
# sentiment DB, etc.) and organises them into structured briefings stored in
# all three brain stores.
#
# News feed sources used:
#   • Existing DB (sentiment/EnhancedSentimentService) — real-time article pool
#   • CoinMarketCap news API (latest crypto news, market cap news)
#   • Yahoo Finance RSS (stocks, macro, commodities)
#   • CryptoPanic aggregator (crypto fear/greed, FOMO signals)
#   • CoinDesk / CoinTelegraph RSS feeds
#   • AlphaVantage market news (stocks, forex, crypto)
#
# Brain news entry format (stored in paul_knowledge):
#   topic = "brain_news_briefing"
#   kind  = "news"
#   tags  = ["brain-news", category, symbol, source]
#   importance = 0.70  (high — news context enriches JARVIS analysis)
# ─────────────────────────────────────────────────────────────────────────────

_NEWS_SOURCES = [
    # RSS feeds — format: (label, url, category)
    ("CoinDesk",        "https://www.coindesk.com/arc/outboundfeeds/rss/",   "crypto"),
    ("CoinTelegraph",   "https://cointelegraph.com/rss",                      "crypto"),
    ("CryptoNews",      "https://cryptonews.com/news/feed/",                  "crypto"),
    ("Yahoo Finance",   "https://finance.yahoo.com/news/rssindex",            "stocks"),
    ("Reuters Finance", "https://feeds.reuters.com/reuters/businessNews",     "macro"),
    ("Bloomberg Crypto","https://feeds.bloomberg.com/crypto/news.rss",        "crypto"),
]

# Track when news was last collected to avoid duplicate runs
_brain_news_last_run: float = 0.0
_BRAIN_NEWS_MIN_INTERVAL = 25 * 60  # minimum 25 minutes between runs


async def _fetch_rss_news(label: str, url: str, max_items: int = 6) -> List[Dict[str, str]]:
    """Fetch and parse an RSS feed, return list of {title, url, source, summary}."""
    import aiohttp
    import xml.etree.ElementTree as ET

    articles: List[Dict[str, str]] = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url, headers={"User-Agent": "JARVIS-Brain/1.0"}) as resp:
                if resp.status != 200:
                    return articles
                text = await resp.text()
        # Parse RSS/Atom
        root = ET.fromstring(text)
        ns_map = {"atom": "http://www.w3.org/2005/Atom"}
        # Try RSS 2.0 format
        items = root.findall(".//item")
        if not items:
            # Try Atom format
            items = root.findall(".//atom:entry", ns_map)
        for item in items[:max_items]:
            title = (
                item.findtext("title")
                or item.findtext("atom:title", namespaces=ns_map)
                or ""
            ).strip()
            link = (
                item.findtext("link")
                or (item.find("atom:link", ns_map).get("href") if item.find("atom:link", ns_map) is not None else "")
                or ""
            ).strip()
            desc = (
                item.findtext("description")
                or item.findtext("summary")
                or item.findtext("atom:summary", namespaces=ns_map)
                or ""
            ).strip()
            if title:
                articles.append({
                    "title": title[:180],
                    "url": link[:200],
                    "source": label,
                    "summary": desc[:300] if desc else "",
                })
    except Exception as e:
        logger.debug(f"[JARVIS brain-news] RSS {label}: {e}")
    return articles


async def _fetch_coinmarketcap_news(max_items: int = 8) -> List[Dict[str, str]]:
    """Fetch latest news from CoinMarketCap public content API."""
    import aiohttp
    articles: List[Dict[str, str]] = []
    try:
        url = "https://api.coinmarketcap.com/content/v3/news?start=1&limit=10"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url, headers={"User-Agent": "JARVIS-Brain/1.0"}) as resp:
                if resp.status != 200:
                    return articles
                data = await resp.json()
        items = data.get("data", {}).get("list") or data.get("data", [])
        if not isinstance(items, list):
            return articles
        for item in items[:max_items]:
            title = (item.get("title") or item.get("headline") or "").strip()
            url_l = (item.get("url") or item.get("sourceUrl") or "").strip()
            subtitle = (item.get("subtitle") or item.get("description") or "").strip()
            if title:
                articles.append({
                    "title": title[:180],
                    "url": url_l[:200],
                    "source": "CoinMarketCap",
                    "summary": subtitle[:300],
                })
    except Exception as e:
        logger.debug(f"[JARVIS brain-news] CoinMarketCap: {e}")
    return articles


async def _fetch_cryptopanic_news(max_items: int = 8) -> List[Dict[str, str]]:
    """Fetch FOMO and breaking crypto news from CryptoPanic public API."""
    import aiohttp
    articles: List[Dict[str, str]] = []
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=free&public=true&kind=news"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url, headers={"User-Agent": "JARVIS-Brain/1.0"}) as resp:
                if resp.status != 200:
                    return articles
                data = await resp.json()
        for item in (data.get("results") or [])[:max_items]:
            title = (item.get("title") or "").strip()
            url_l = (item.get("url") or "").strip()
            slug = item.get("slug", "")
            votes = item.get("votes") or {}
            # FOMO signal: high positive votes
            fomo_score = votes.get("positive", 0) - votes.get("negative", 0)
            if title:
                articles.append({
                    "title": title[:180],
                    "url": url_l[:200],
                    "source": "CryptoPanic",
                    "summary": f"FOMO score: {fomo_score:+d}" if fomo_score != 0 else "",
                    "fomo_score": str(fomo_score),
                })
    except Exception as e:
        logger.debug(f"[JARVIS brain-news] CryptoPanic: {e}")
    return articles


async def _fetch_db_recent_news(limit: int = 15) -> List[Dict[str, str]]:
    """Pull the most recent articles from the existing sentiment/news DB."""
    articles: List[Dict[str, str]] = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.sentiment.enhanced_service import EnhancedSentimentService
        async with AsyncSessionLocal() as db:
            rows = await EnhancedSentimentService.get_articles(
                db, hours=3, limit=limit
            )
        for r in rows:
            title = (r.get("title") or "").strip()
            if title:
                articles.append({
                    "title": title[:180],
                    "url": (r.get("url") or "")[:200],
                    "source": r.get("source", "DB"),
                    "summary": (r.get("content") or "")[:250],
                    "sentiment_label": r.get("sentiment_label") or "neutral",
                    "symbol": r.get("symbol") or "",
                })
    except Exception as e:
        logger.debug(f"[JARVIS brain-news] DB news: {e}")
    return articles


async def _brain_collect_and_organise_news(symbols: Optional[List[str]] = None) -> None:
    """Brain News Collector: Idle Mistral gathers, organises, and stores market news.

    Pulls from ALL integrated news sources concurrently:
    - CoinMarketCap news API  (latest crypto market news)
    - CryptoPanic             (FOMO signals, breaking news, sentiment)
    - RSS feeds               (CoinDesk, CoinTelegraph, Yahoo Finance, Reuters)
    - Internal sentiment DB   (articles already scraped and scored)

    Mistral organises the raw headlines into structured briefings:
    - BULLISH / BEARISH category
    - Breaking news highlights
    - FOMO and fear signals
    - Macro market events

    Briefings are stored in all three brain stores at importance=0.70 so JARVIS
    reads them as market context during analysis.  Fire-and-forget — never blocks.
    """
    global _brain_news_last_run
    import time
    now = time.time()
    if (now - _brain_news_last_run) < _BRAIN_NEWS_MIN_INTERVAL:
        logger.debug("[JARVIS brain-news] skipped — ran recently")
        return
    _brain_news_last_run = now

    try:
        # ── 1. Fetch all sources concurrently ─────────────────────────────────
        results = await asyncio.gather(
            _fetch_coinmarketcap_news(max_items=8),
            _fetch_cryptopanic_news(max_items=8),
            _fetch_db_recent_news(limit=12),
            *[_fetch_rss_news(lbl, url, max_items=5) for lbl, url, _ in _NEWS_SOURCES],
            return_exceptions=True,
        )

        all_articles: List[Dict[str, str]] = []
        for res in results:
            if isinstance(res, list):
                all_articles.extend(res)
            # exceptions silently dropped — best-effort

        if not all_articles:
            logger.debug("[JARVIS brain-news] no articles collected from any source")
            return

        logger.debug(f"[JARVIS brain-news] collected {len(all_articles)} articles across all sources")

        # ── 2. Deduplicate by title ──────────────────────────────────────────
        seen_titles: set = set()
        unique_articles: List[Dict[str, str]] = []
        for a in all_articles:
            key = a.get("title", "").lower()[:60]
            if key not in seen_titles:
                seen_titles.add(key)
                unique_articles.append(a)

        # ── 3. Build prompt for Mistral (organiser role) ─────────────────────
        news_block = "\n".join(
            f"[{a.get('source','?')}] {a.get('title','')} {('| '+a.get('summary',''))[:100] if a.get('summary') else ''}"
            for a in unique_articles[:30]
        )
        symbols_note = f"Focus on: {', '.join(symbols)}" if symbols else "Cover all major assets."

        msgs = [
            {
                "role": "system",
                "content": (
                    "You are JARVIS's Brain News Organiser. You receive raw market headlines "
                    "from multiple sources and produce a structured briefing stored in JARVIS's "
                    "long-term brain memory for use in future market analysis.\n\n"
                    "Output format (EXACTLY this structure — no extra text):\n"
                    "BREAKING: [1-2 biggest breaking news items if any]\n"
                    "BULLISH: [top bullish signals — max 3 items]\n"
                    "BEARISH: [top bearish signals — max 3 items]\n"
                    "FOMO: [high-momentum / FOMO signals — max 2 items]\n"
                    "MACRO: [macro / stock market / USD / Fed news — max 2 items]\n"
                    "SENTIMENT: [overall market sentiment: BULLISH/BEARISH/NEUTRAL with 1-line reason]\n"
                    "Keep each item to one line. Total output: max 15 lines. Pure signal, no filler."
                ),
            },
            {
                "role": "user",
                "content": f"{symbols_note}\n\nLatest headlines ({len(unique_articles)} items):\n\n{news_block}",
            },
        ]

        # ── 4. Call best available brain provider (news organiser role, dynamic) ──
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            briefing = await _brain_call(
                "brain_news_organiser", msgs, db,
                max_tokens=400, temperature=0.18,
            )

        if not briefing:
            logger.debug("[JARVIS brain-news] brain_news_organiser returned empty briefing")
            return

        # ── 5. Write structured briefing to all 3 brain stores ───────────────
        symbol_tag = ",".join(symbols[:5]) if symbols else "market"
        source_list = ", ".join(sorted({a.get("source", "?") for a in unique_articles}))
        detail_full = (
            f"BRAIN NEWS ORGANISER (multi-provider slot 4) — {len(unique_articles)} articles from: {source_list}\n\n"
            f"{briefing}\n\n"
            f"--- Sample headlines ---\n"
            + "\n".join(f"• [{a.get('source','?')}] {a.get('title','')}" for a in unique_articles[:8])
        )

        jarvis_learn_all_brains(
            action="brain_news_briefing",
            symbol=symbol_tag,
            summary=f"Market news briefing: {briefing[:180]}",
            detail=detail_full[:1400],
            tags=["jarvis", "brain-news", "market-briefing", symbol_tag],
            importance=0.70,
            kind="news",
        )
        logger.info(
            f"[JARVIS brain-news] ✅ wrote organised briefing "
            f"({len(unique_articles)} articles → {len(briefing)} char briefing)"
        )

    except Exception as e:
        logger.debug(f"[JARVIS brain-news] collector error: {e}")


def _fire_brain_news_collection(symbols: Optional[List[str]] = None) -> None:
    """Fire-and-forget: launch the brain news collector in the background.
    Called after every JARVIS analysis so the brain stays up-to-date.
    Respects a minimum interval to avoid hammering news APIs.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_brain_collect_and_organise_news(symbols))
        logger.debug("[JARVIS brain-news] news collection task launched")
    except Exception as e:
        logger.debug(f"[JARVIS brain-news] launch skipped: {e}")





# ── Voice brain models ────────────────────────────────────────────────────────

class VoiceProfile(BaseModel):
    bands: List[float]                   # 12 frequency-band energies (0–1)
    bandStdDev: Optional[List[float]] = None
    centroid: float = 0.0
    sessions: int = 0
    calibratedAt: Optional[float] = None


class VoiceBrainSyncRequest(BaseModel):
    vocabulary: Dict[str, int]           # {word: count}
    profile: Optional[VoiceProfile] = None
    sessions: int = 0


class VoiceBrainIdentifyRequest(BaseModel):
    bands: List[float]                   # current frame's 12-band energies
    centroid: Optional[float] = None


# ── Voice brain: vault note paths ─────────────────────────────────────────────
# These notes are PERMANENT — never deleted, only updated with new data.

def _voice_vault_path() -> Path:
    """Return the fixed vault path for the voice identity note."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings
        return obsidian_settings.vault_path / "voice-memory" / "voice-profile.md"
    except Exception:
        return Path.home() / ".jarvis" / "voice-profile.md"


def _voice_data_path() -> Path:
    """Return the machine-readable JSON data file (next to the vault note)."""
    return _voice_vault_path().with_suffix(".json")


# ── Voice binary comparison engine ────────────────────────────────────────────

def _band_match_confidence(current: List[float], stored: VoiceProfile) -> float:
    """
    Compare a real-time frequency-band frame against the stored voice profile.

    Returns 0–1 where 1.0 means every band is within 1σ of the profile mean.
    Uses per-band standard-deviation tolerances so natural voice variation
    (mic distance, time of day, cold) does not reject the real user, while
    TV / background noise with a different spectral shape scores near zero.
    """
    if not stored.bands or not current:
        return 1.0  # no profile yet — accept everything
    bands_n = min(len(current), len(stored.bands))
    std_dev = stored.bandStdDev or [0.25] * bands_n   # generous fallback
    score = 0.0
    for i in range(bands_n):
        dev       = abs(current[i] - stored.bands[i])
        tolerance = std_dev[i] * 3.0 + 0.05           # 3σ window + fixed floor
        score    += max(0.0, 1.0 - dev / tolerance)
    return score / bands_n


# ── Voice Brain endpoints ─────────────────────────────────────────────────────

@router.post("/voice-brain/sync")
async def voice_brain_sync(req: VoiceBrainSyncRequest):
    """
    Persist voice fingerprint + vocabulary to the Obsidian vault.

    The vault note is written at voice-memory/voice-profile.md and is NEVER
    deleted — only merged (new counts always accumulate, never decrease).
    A machine-readable JSON sidecar is written alongside the note so the
    load endpoint can restore exact data without parsing markdown.
    """
    now        = datetime.now(timezone.utc)
    data_path  = _voice_data_path()
    note_path  = _voice_vault_path()

    # ── Load existing data (merge-in new counts) ──────────────────────────────
    existing: Dict[str, Any] = {}
    if data_path.exists():
        try:
            existing = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    # Merge vocabulary: take the MAX count per word (accumulate, never shrink)
    merged_vocab: Dict[str, int] = dict(existing.get("vocabulary", {}))
    for word, count in req.vocabulary.items():
        merged_vocab[word] = max(merged_vocab.get(word, 0), count)

    # Merge profile: if a new profile is supplied, blend it with the stored one
    # using an exponential moving average so old learning doesn't vanish.
    stored_profile = existing.get("profile", None)
    if req.profile and req.profile.bands:
        if stored_profile and stored_profile.get("bands"):
            alpha   = 0.10   # 10 % new, 90 % old — conservative, stable
            s_bands = stored_profile["bands"]
            n_bands = req.profile.bands
            n       = min(len(s_bands), len(n_bands))
            merged_bands = [s_bands[i] * (1 - alpha) + n_bands[i] * alpha for i in range(n)]
            # Update std-dev similarly
            s_std = stored_profile.get("bandStdDev") or [0.1] * n
            n_std = req.profile.bandStdDev or [0.1] * n
            merged_std  = [s_std[i] * (1 - alpha) + n_std[i] * alpha for i in range(n)]
            merged_centroid = (
                stored_profile.get("centroid", 0) * (1 - alpha) +
                req.profile.centroid * alpha
            )
            new_profile = {
                "bands":       merged_bands,
                "bandStdDev":  merged_std,
                "centroid":    merged_centroid,
                "sessions":    existing.get("sessions", 0) + 1,
                "calibratedAt": now.timestamp(),
            }
        else:
            # First save — store as-is
            new_profile = req.profile.model_dump()
            new_profile["sessions"] = 1
    else:
        new_profile = stored_profile

    total_words = len(merged_vocab)
    top_words   = sorted(merged_vocab.items(), key=lambda x: -x[1])[:50]
    sessions    = (existing.get("sessions", 0) + 1) if req.profile else existing.get("sessions", 0)

    # ── Write JSON sidecar (machine-readable, never deleted) ──────────────────
    data_out = {
        "vocabulary": merged_vocab,
        "profile":    new_profile,
        "sessions":   sessions,
        "total_words": total_words,
        "updated":    now.isoformat(),
    }
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data_out, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[JARVIS voice-brain] synced: {total_words} words, sessions={sessions}")

    # ── Write human-readable vault note ───────────────────────────────────────
    band_table = ""
    if new_profile and new_profile.get("bands"):
        b  = new_profile["bands"]
        sd = new_profile.get("bandStdDev", ["-"] * len(b))
        band_labels = [
            "<80Hz", "80-160", "160-320", "320-640",
            "640Hz-1.3k", "1.3-2.5k", "2.5-5k", "5-10k",
            "10-16k", "16-20k", "20k+", "ultra",
        ]
        def _fmt(v: Any) -> str:
            try: return f"{float(v):.4f}"
            except (TypeError, ValueError): return "-"

        rows = "\n".join(
            f"| {band_labels[i] if i < len(band_labels) else i} "
            f"| {b[i]:.4f} "
            f"| {_fmt(sd[i])} |"
            for i in range(len(b))
        )
        band_table = (
            "\n## Voice Frequency Fingerprint\n"
            "| Band | Mean Energy | Std Dev |\n"
            "| ---- | ----------- | ------- |\n"
            + rows
            + f"\n\nSpectral centroid: **{new_profile.get('centroid', 0):.4f}**  "
            f"Sessions: **{sessions}**\n"
        )

    top_table = "\n".join(
        f"| {w} | {c} |" for w, c in top_words
    )

    note = (
        f"---\n"
        f"type: voice-identity\n"
        f"updated: {now.isoformat()}\n"
        f"sessions: {sessions}\n"
        f"words_learned: {total_words}\n"
        f"tags:\n  - jarvis\n  - voice\n  - identity\n"
        f"---\n\n"
        f"# JARVIS Voice Identity\n\n"
        f"> This note is the permanent voice memory for JARVIS.  "
        f"It is **never deleted** — only improved over time as you speak.\n\n"
        f"Updated: {now.strftime('%Y-%m-%d %H:%M UTC')}  "
        f"| Words learned: **{total_words}**  "
        f"| Voice sessions: **{sessions}**\n"
        f"{band_table}\n"
        f"## Top Learned Words (speech vocabulary)\n\n"
        f"| Word | Times Spoken |\n"
        f"| ---- | ------------ |\n"
        f"{top_table}\n\n"
        f"## How This Works\n\n"
        f"JARVIS builds a 12-band frequency fingerprint of your voice from the Web Audio API.\n"
        f"Each time you speak a confirmed command, the fingerprint is updated with a 10 % blend\n"
        f"(exponential moving average) so your natural day-to-day variation is captured without\n"
        f"overwriting previous learning. The vocabulary table accumulates word counts across all\n"
        f"sessions — words you say often get higher recognition priority.\n\n"
        f"*Machine-readable data is stored alongside this note in `voice-profile.json`.*\n"
    )

    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(note, encoding="utf-8")

    return {
        "ok": True,
        "words_total": total_words,
        "sessions": sessions,
        "vault_path": str(note_path),
        "top_words": dict(top_words[:10]),
    }


@router.get("/voice-brain/load")
async def voice_brain_load():
    """
    Load voice fingerprint + vocabulary from the permanent vault note.
    Returns stored data for merging with the browser's local state.
    """
    data_path = _voice_data_path()
    if not data_path.exists():
        return {"ok": True, "vocabulary": {}, "profile": None, "sessions": 0}
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
        return {"ok": True, **data}
    except Exception as e:
        logger.warning(f"[JARVIS voice-brain] load error: {e}")
        return {"ok": False, "vocabulary": {}, "profile": None, "sessions": 0}


@router.post("/voice-brain/identify")
async def voice_brain_identify(req: VoiceBrainIdentifyRequest):
    """
    Voice binary engine — compare a real-time frequency-band frame against
    the stored voice profile and return an identification confidence score.

    Returns:
      confidence: 0.0–1.0  (≥0.55 = likely the stored speaker)
      match: bool
      sessions: int         (how many sessions have improved the model)
    """
    data_path = _voice_data_path()
    if not data_path.exists():
        return {"confidence": 1.0, "match": True, "sessions": 0,
                "message": "No profile stored yet — accepting all speakers."}
    try:
        data    = json.loads(data_path.read_text(encoding="utf-8"))
        raw     = data.get("profile")
        if not raw or not raw.get("bands"):
            return {"confidence": 1.0, "match": True, "sessions": 0}
        profile = VoiceProfile(**raw)
        conf    = _band_match_confidence(req.bands, profile)
        return {
            "confidence": round(conf, 4),
            "match":      conf >= 0.55,
            "sessions":   data.get("sessions", 0),
        }
    except Exception as e:
        logger.warning(f"[JARVIS voice-brain] identify error: {e}")
        return {"confidence": 1.0, "match": True, "sessions": 0}


# ── Response / Request models ──────────────────────────────────────────────────

class Position(BaseModel):
    exchange: str
    symbol: str          # normalised, e.g. "BTCUSDT"
    raw_symbol: str      # as returned by exchange, e.g. "BTC/USDT:USDT"
    side: str            # "long" | "short"
    size: float
    entry_price: float
    mark_price: float
    pnl: float
    pnl_pct: float
    leverage: Optional[float] = None
    margin_mode: Optional[str] = None
    notional: Optional[float] = None
    liquidation_price: Optional[float] = None


class PortfolioSummary(BaseModel):
    total_positions: int
    total_pnl: float
    total_notional: float
    positions: List[Position]


# ── Unified monitor models ─────────────────────────────────────────────────────

class CryptoAccountSummary(BaseModel):
    exchange: str
    currency: str = "USDT"
    total: float = 0.0   # total equity / wallet balance
    free: float = 0.0    # available (not used as margin)
    used: float = 0.0    # margin in use


class MT5AccountSummary(BaseModel):
    account_id: int
    name: str
    login: str
    server: str
    balance: float
    equity: float
    floating_pnl: float
    margin: float
    free_margin: float
    currency: str
    leverage: int
    positions: List[Dict[str, Any]] = []
    position_count: int = 0


class UnifiedMonitorResponse(BaseModel):
    # Crypto (all exchanges)
    crypto_positions: List[Position] = []
    crypto_accounts: List[CryptoAccountSummary] = []   # per-exchange balances
    crypto_total_pnl: float = 0.0
    crypto_total_notional: float = 0.0
    # MT5 (all accounts)
    mt5_accounts: List[MT5AccountSummary] = []
    mt5_total_balance: float = 0.0
    mt5_total_equity: float = 0.0
    mt5_total_floating_pnl: float = 0.0
    mt5_position_count: int = 0
    # Grand total
    total_position_count: int = 0
    total_pnl: float = 0.0
    fetched_at: str = ""


class PositionAnalysis(BaseModel):
    ticket: int
    symbol: str
    side: str
    account_id: int
    analysis_text: str
    has_suggestion: bool
    sl_suggestion: Optional[float] = None
    tp_suggestion: Optional[float] = None
    ai_verdict: Optional[str] = None
    analyzed_at: str = ""


class AnalyzePositionsResponse(BaseModel):
    account_id: int
    positions_analyzed: int
    analyses: List[PositionAnalysis] = []
    summary: str = ""
    analyzed_at: str = ""


class CommandRequest(BaseModel):
    command: str
    exchange: Optional[str] = None  # e.g. "bybit"; if None → auto-detect


class CommandResult(BaseModel):
    ok: bool
    action: str
    detail: str
    speech: str          # human-readable sentence for TTS
    order: Optional[Dict[str, Any]] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _norm_symbol(raw: str) -> str:
    """
    'BTC/USDT:USDT' → 'BTCUSDT'
    'GWEI/USDT'     → 'GWEIUSDT'
    """
    base = raw.split(":")[0]          # strip :SETTLE suffix
    return base.replace("/", "")


def _match_symbol(query: str, raw: str) -> bool:
    """Return True if the user's query matches the exchange symbol."""
    q = query.upper().replace("/", "").replace(":", "").replace(" ", "")
    r = raw.upper().split(":")[0].replace("/", "")   # BTC/USDT:USDT → BTCUSDT
    return q == r or q in r or r.startswith(q)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val or 0)
        return v if v == v else default   # NaN guard
    except Exception:
        return default


# ── Extension version / update check ─────────────────────────────────────────

# Fallback version only — the real version is ALWAYS read live from
# jarvis-extension/manifest.json (see _ext_version()). Keep this in sync so a
# missing manifest never advertises a stale version.
_EXT_VERSION = "3.6.9"
_EXT_RELEASED = "2026-08-08"
_EXT_CHANGELOG = [
    "JARVIS no longer stops talking before finishing — only your calibrated voice can interrupt him now",
    "Fix mic hand-off: in-page JARVIS takes over when the extension speech engine stalls (no more stuck 'Starting…')",
    "Stable mic ownership: stop page<->extension flapping that left voice deaf",
    "Chart-page wake watchdog keeps voice listening alive on heavy WebGL pages",
    "Fix read-aloud silently dropped when pageSpeaking stuck true",
    "Fix accounts not shown in popup — lastUnifiedData now cached in background and used for instant account balance display on popup open and in 10s auto-refresh",
    "Fix accounts loading + trades not read aloud",
    "bump to v3.6.2",
    "JARVIS Memory Tree — the assistant now folds news, positions and trades into a scored, hierarchical long-term memory every 15 minutes",
    "SuperContext — on the first message of a chat JARVIS auto-sweeps its memory, news and brain-map for your exact question and pre-loads the answer",
    "Goals & Todos — set a durable goal and JARVIS builds a kanban with you and works it in the background (read-only research, never auto-trades)",
    "Proactive memory alerts — the extension now surfaces newly-learned high-importance facts as desktop notifications",
    "Camera mouth-movement now gates hearing in real time — JARVIS only listens while it sees your lips move, so its own TTS voice can never be self-transcribed while the camera is live",
    "Unknown-face lockout restored — a stranger can't drive JARVIS, but only once you've enrolled your own face (unenrolled never blocks)",
    "TTS self-hearing fixed for real — the page now passes the exact words it speaks so JARVIS never transcribes its own AI voice",
    "Fix: enabling Face Vision no longer stops JARVIS from hearing you (face is now additive-only, never mutes voice)",
    "JARVIS no longer transcribes its own voice while reading to you (self-echo guard + echo-tail window)",
    "Background sound and faint/other-room voices no longer wake JARVIS — only real near-mic user speech",
    "Face Vision toggle now reliably turns the camera on/off and remembers its state",
    "Face Vision camera now opens in a tab so the browser reliably asks for permission",
    "Live camera preview with lip/face overlay in the extension tab and JARVIS Room",
    "Enroll your face from the camera tab or the Room; popup mirrors live status",
    "Read-aloud on change now uses real coin names (BTCUSDT → Bitcoin)",
    "Correct up/down direction + change from the previous reading",
    "3D JARVIS robot avatar on every page",
    "Universal voice — extension speaks with your chosen chat voice",
    "Avatar style picker (cyan/purple/gold/crimson/emerald)",
    "Robot reacts to voice: listening, thinking, talking animations",
    "Unified monitor: crypto + MT5 accounts in real time",
    "15-minute automatic position analysis with AI/SMC",
    "On-demand analysis from popup with JARVIS speech",
    "Auto-update detection when TradeBot opens",
]


def _ext_dir() -> Optional[Path]:
    """
    Locate the ``jarvis-extension`` directory robustly.

    Walks up from this file until it finds a folder containing a
    ``jarvis-extension`` directory (project root), so the lookup never breaks if
    the module is moved. Returns ``None`` if it cannot be found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "jarvis-extension"
        if candidate.is_dir():
            return candidate
    return None


def _ext_version() -> str:
    """Return the live extension version from manifest.json (fallback constant)."""
    import json as _json
    ext_dir = _ext_dir()
    if ext_dir is not None:
        manifest_path = ext_dir / "manifest.json"
        try:
            if manifest_path.exists():
                v = _json.loads(manifest_path.read_text()).get("version")
                if v:
                    return str(v)
        except Exception:
            pass
    return _EXT_VERSION


@router.get("/extension-version")
async def get_extension_version():
    """
    Returns the latest JARVIS extension version info.

    The extension polls this endpoint on TradeBot startup and every 24 hours.
    If the installed version differs from `version`, a banner is shown.
    """
    # Read version directly from manifest so backend and ZIP always agree
    manifest_version = _ext_version()

    return {
        "version": manifest_version,
        "released_at": _EXT_RELEASED,
        "changelog": _EXT_CHANGELOG,
        "install_path": "/jarvis-extension",
        "download_url": f"/api/v1/jarvis/extension-download",
        "download_versioned_url": f"/api/v1/jarvis/extension-download?v={manifest_version}",
        "instructions": "Reload the extension in chrome://extensions after updating the files.",
    }


@router.get("/extension-download")
async def download_extension(v: Optional[str] = None):
    """
    Download the latest JARVIS extension as a versioned ZIP file.

    Always packages the CURRENT files from the jarvis-extension/ directory,
    so the downloaded ZIP is always up to date. The `v` query param is ignored
    server-side (it exists purely to bust browser caches when version changes).

    Filename format: jarvis-extension-v{version}.zip
    """
    import io as _io
    import zipfile as _zipfile
    from fastapi.responses import StreamingResponse

    ext_dir = _ext_dir()
    if ext_dir is None or not ext_dir.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "Extension directory not found")

    # Read version from manifest for the filename (always the live value)
    version = _ext_version()

    # Build the ZIP in memory from current files
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(ext_dir.rglob("*")):
            if item.is_file():
                # Skip hidden files and caches
                if any(part.startswith(".") for part in item.parts):
                    continue
                arcname = item.relative_to(ext_dir)
                zf.write(item, arcname)

    buf.seek(0)
    filename = f"jarvis-extension-v{version}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Extension-Version": version,
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


# ── System resource stats (CPU / RAM) ─────────────────────────────────────────

@router.get("/ai-task-status")
async def get_ai_task_status():
    """
    Returns the current task-to-model routing map for the whole app.

    Shows each Jarvis analysis task (market_analysis, news_context, volume_analysis,
    synthesis, news_position), the preferred provider+model for each, and whether
    that provider is currently enabled and healthy in the DB. Used by the Jarvis Room
    AI Models panel and by the Connect AI tab quick-setup guide.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            get_enabled_providers, _cb_open,
        )

        async with AsyncSessionLocal() as db:
            enabled_providers = await get_enabled_providers(db)

        task_rows = []
        for task, prefs in _JARVIS_TASK_MODELS.items():
            # Walk the chain to find the first available + non-circuit-open provider
            resolved_provider = None
            resolved_model = None
            resolved_frag = None
            for frag, model in prefs:
                candidate = next(
                    (p for p in enabled_providers
                     if frag.lower() in (p.label or "").lower()
                     and not _cb_open(p.id)),
                    None,
                )
                # Skip if provider is disabled, circuit-open, or known-error
                if candidate and candidate.enabled and candidate.status != "error":
                    resolved_provider = candidate
                    resolved_model = model
                    resolved_frag = frag
                    break

            # Primary (first in chain) is always shown as "preferred"
            primary_frag  = prefs[0][0] if prefs else ""
            primary_model = prefs[0][1] if prefs else ""
            # Active is whichever is the first live provider in the chain
            active = resolved_provider
            cb_open = active and _cb_open(active.id)
            task_rows.append({
                "task": task,
                "label": _JARVIS_TASK_LABELS.get(task, task),
                # primary preference
                "preferred_provider_fragment": primary_frag,
                "preferred_model": primary_model,
                # currently active (may differ if primary is rate-limited)
                "active_provider_fragment": resolved_frag,
                "active_model": resolved_model,
                "using_fallback": resolved_frag != primary_frag if resolved_frag else False,
                # status
                "provider_configured": active is not None,
                "provider_label": active.label if active else None,
                "provider_enabled": bool(active and active.enabled),
                "provider_status": active.status if active else "not_configured",
                "circuit_open": bool(cb_open),
                "last_error": (active.last_error or None) if active else None,
                # full ordered chain for the UI
                "fallback_chain": [
                    {"provider_fragment": f, "model": m} for f, m in prefs
                ],
            })

        # Also return all enabled providers for the UI
        provider_rows = [
            {
                "id": p.id,
                "label": p.label,
                "provider_key": p.provider_key,
                "enabled": p.enabled,
                "status": p.status,
                "priority": p.priority,
                "default_model": p.default_model,
                "last_tested_at": p.last_tested_at.isoformat() if p.last_tested_at else None,
                "circuit_open": _cb_open(p.id),
            }
            for p in enabled_providers
        ]

        # ── Persist current routing config to all three brains (fire-and-forget) ──
        # Captures the active task→model map so JARVIS memory always knows which
        # AI is handling each task, including any fallback activations.
        _routing_lines = []
        for t in task_rows:
            fb_flag = " [FALLBACK]" if t.get("using_fallback") else ""
            _routing_lines.append(
                f"{t['label']}: {t.get('provider_label','?')} / {t.get('active_model','?')}"
                f"{fb_flag} (chain: {' → '.join(e['model'][:28] for e in t.get('fallback_chain',[]))})"
            )
        jarvis_learn_all_brains(
            action="ai_task_routing",
            summary="Current JARVIS AI task→model routing configuration",
            detail="\n".join(_routing_lines),
            tags=["jarvis", "ai-routing", "model-config", "task-models"],
            importance=0.55,
        )

        return {
            "ok": True,
            "tasks": task_rows,
            "providers": provider_rows,
            "provider_count": len(enabled_providers),
        }
    except Exception as exc:
        logger.warning(f"[JARVIS] ai-task-status error: {exc}")
        # Return the static task map even when DB is unreachable
        return {
            "ok": False,
            "tasks": [
                {
                    "task": task,
                    "label": _JARVIS_TASK_LABELS.get(task, task),
                    "preferred_provider_fragment": prefs[0][0] if prefs else "",
                    "preferred_model": prefs[0][1] if prefs else "",
                    "active_provider_fragment": None,
                    "active_model": None,
                    "using_fallback": False,
                    "provider_configured": False,
                    "provider_label": None,
                    "provider_enabled": False,
                    "provider_status": "unknown",
                    "circuit_open": False,
                    "last_error": None,
                    "fallback_chain": [
                        {"provider_fragment": f, "model": m} for f, m in prefs
                    ],
                }
                for task, prefs in _JARVIS_TASK_MODELS.items()
            ],
            "providers": [],
            "provider_count": 0,
            "error": str(exc),
        }


@router.get("/brain-activity")
async def get_brain_activity():
    """Return recent brain manager activity for the JARVIS Room Brain Network panel.

    Returns the last N brain entries (consolidations, signal indexes, news briefings)
    from paul_knowledge so the frontend can display a live wiring feed.
    Also returns brain manager metadata (model, last run time, entry counts).
    """
    try:
        from datetime import datetime as _dt
        from app.core.database import AsyncSessionLocal
        from plugins.AgentPaulPlugin.backend.services import knowledge_base

        async with AsyncSessionLocal() as db:
            # Recent brain entries (all types)
            rows = await knowledge_base.search_knowledge(
                db,
                query="brain_consolidation brain_signal_index brain_news_briefing jarvis brain",
                limit=20,
            )

        # Categorise entries
        consolidations, indexes, news_briefings, other = [], [], [], []
        for r in rows:
            topic = r.get("kind") or ""
            content = r.get("content") or ""
            title = r.get("title") or ""
            ts = r.get("ts") or ""
            entry = {
                "title": title[:80] if title else content[:60],
                "preview": content[:120],
                "ts": ts,
                "source": r.get("source") or "",
            }
            if "brain_consolidat" in content.lower() or "brain_consolidat" in title.lower():
                consolidations.append(entry)
            elif "brain_signal_index" in content.lower() or "brain_indexer" in content.lower():
                indexes.append(entry)
            elif "brain_news" in content.lower() or "news briefing" in content.lower() or r.get("kind") == "news":
                news_briefings.append(entry)
            else:
                other.append(entry)

        last_consolidation = consolidations[0]["ts"] if consolidations else None
        last_index = indexes[0]["ts"] if indexes else None
        last_news = news_briefings[0]["ts"] if news_briefings else None

        return {
            "ok": True,
            "brain_managers": [
                {
                    "role": "brain_consolidator",
                    "label": "Brain Consolidator",
                    "model": "Mistral open-mistral-nemo",
                    "provider": "Mistral",
                    "description": "Fuses all task model outputs into cross-model brain maps",
                    "last_run": last_consolidation,
                    "entry_count": len(consolidations),
                },
                {
                    "role": "brain_indexer",
                    "label": "Brain Indexer",
                    "model": "Mistral open-mistral-nemo",
                    "provider": "Mistral",
                    "description": "Extracts structured signal patterns (BIAS/LEVELS/SMC)",
                    "last_run": last_index,
                    "entry_count": len(indexes),
                },
                {
                    "role": "brain_news_organiser",
                    "label": "Brain News Organiser",
                    "model": "Mistral open-mistral-nemo",
                    "provider": "Mistral",
                    "description": "Organises market news from CoinMarketCap, Yahoo, RSS feeds",
                    "last_run": last_news,
                    "entry_count": len(news_briefings),
                    "news_sources": [lbl for lbl, _, _ in _NEWS_SOURCES] + ["CoinMarketCap", "CryptoPanic", "Sentiment DB"],
                },
            ],
            "recent_activity": (consolidations + indexes + news_briefings)[:12],
            "totals": {
                "consolidations": len(consolidations),
                "signal_indexes": len(indexes),
                "news_briefings": len(news_briefings),
                "total": len(rows),
            },
        }
    except Exception as exc:
        logger.debug(f"[JARVIS] brain-activity error: {exc}")
        return {
            "ok": False,
            "brain_managers": [],
            "recent_activity": [],
            "totals": {"consolidations": 0, "signal_indexes": 0, "news_briefings": 0, "total": 0},
            "error": str(exc),
        }


@router.post("/brain-news-collect")
async def trigger_brain_news_collection():
    """Manually trigger the brain news collector (idle Mistral fetches & organises news).
    Also triggered automatically after every JARVIS analysis."""
    _fire_brain_news_collection(None)
    return {"ok": True, "message": "Brain news collection launched in background"}


@router.get("/system-stats")
async def get_system_stats():
    """
    Live host resource usage for the JARVIS Room HUD.

    Delegates to ``app.services.system_resources`` — the single source of truth
    shared with the System Monitor page — so the two can never drift.
    Degrades gracefully (``available: False``) when psutil is missing.
    """
    from app.services.system_resources import host_snapshot, process_snapshot

    host = host_snapshot()
    if not host.get("available"):
        return {
            "available": False,
            "reason": host.get("reason", "unavailable"),
            "cpu_percent": 0.0,
            "cpu_count": 0,
            "mem_percent": 0.0,
            "mem_used": 0,
            "mem_total": 0,
        }
    proc = process_snapshot()
    host["proc_cpu_percent"] = proc.get("cpu_percent", 0.0) if proc.get("available") else 0.0
    host["proc_mem"] = proc.get("rss", 0) if proc.get("available") else 0
    return host


# ── Crypto pair catalog endpoints ──────────────────────────────────────────────
# Backed by app/services/pair_catalog.py (the `crypto_pairs` table). These let
# JARVIS, the extension and the frontend use REAL coin names and resolve spoken
# token names/tickers to a tradeable Bitget pair.

@router.get("/pairs")
async def list_pairs(
    q: Annotated[Optional[str], Query(description="Search by symbol / ticker / name")] = None,
    limit: Annotated[int, Query(description="Max rows to return")] = 50,
):
    """Searchable catalog list (symbol, name, market cap, 24h volume, rank)."""
    try:
        from app.services import pair_catalog
        rows = await pair_catalog.search_pairs(q or "", limit=limit)
        return {"ok": True, "count": len(rows), "pairs": rows}
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs error: {e}")
        return {"ok": False, "count": 0, "pairs": [], "error": str(e)}


@router.get("/pairs/names")
async def pair_names():
    """
    Compact ``{symbol: name}`` map for the extension + frontend.

    Keyed by BOTH ``BTC/USDT`` and ``BTCUSDT`` so monitor payloads (which use the
    glued form) map straight to a coin name.
    """
    try:
        from app.services import pair_catalog
        names = await pair_catalog.get_name_map()
        return {"ok": True, "count": len(names), "names": names}
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs/names error: {e}")
        return {"ok": False, "count": 0, "names": {}, "error": str(e)}


@router.get("/pairs/resolve")
async def resolve_pair(
    q: Annotated[str, Query(description="Token name, ticker or symbol to resolve")],
):
    """
    Resolve a token/name/symbol to a tradeable Bitget pair with live metadata,
    or return ``ok:false`` with the closest suggestion when it isn't found.
    """
    try:
        from app.services import pair_catalog
        pair, suggestion = await pair_catalog.resolve_with_suggestion(q)
        if pair is None:
            return {"ok": False, "query": q, "suggestion": suggestion}

        result = pair_catalog.pair_to_dict(pair, full=True)
        # Overlay a fresh (cached ≤60s) live market snapshot.
        try:
            snap = await pair_catalog.get_market_snapshot(pair.symbol)
            if snap:
                for k in ("market_cap", "market_cap_rank", "volume_24h", "price", "price_change_24h", "name"):
                    if snap.get(k) is not None:
                        result[k] = snap[k]
        except Exception:
            pass
        result["ok"] = True
        result["query"] = q
        return result
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs/resolve error: {e}")
        return {"ok": False, "query": q, "error": str(e)}


# ── Positions endpoint ─────────────────────────────────────────────────────────

@router.get("/positions", response_model=List[Position])
async def get_all_positions(
    exchange: Annotated[Optional[str], Query(description="Filter by exchange name")] = None,
):
    """
    Return all open futures/swap positions across every initialised exchange.
    Only positions with contracts > 0 are included.
    """
    all_positions: List[Position] = []

    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()

    if exchange:
        try:
            single = SupportedExchange(exchange.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    for ex_enum in ex_list:
        connector = exchange_manager.get_exchange(ex_enum)
        if not connector:
            continue
        ex_name = ex_enum.value
        try:
            raw_list = await connector.exchange.fetch_positions()
        except BaseException as e:
            if _is_network_error(e):
                logger.warning(f"[JARVIS] {ex_name} unreachable (DNS/network): {e}")
            else:
                logger.warning(f"[JARVIS] fetch_positions({ex_name}): {e}")
            continue

        for p in raw_list:
            contracts = _safe_float(p.get("contracts"))
            if contracts <= 0:
                continue

            raw_sym = p.get("symbol", "")
            entry   = _safe_float(p.get("entryPrice"))
            mark    = _safe_float(p.get("markPrice")) or entry
            pnl     = _safe_float(p.get("unrealizedPnl"))
            pnl_pct = _safe_float(p.get("percentage"))

            all_positions.append(Position(
                exchange=ex_name,
                symbol=_norm_symbol(raw_sym),
                raw_symbol=raw_sym,
                side=str(p.get("side") or "long").lower(),
                size=contracts,
                entry_price=entry,
                mark_price=mark,
                pnl=pnl,
                pnl_pct=pnl_pct,
                leverage=p.get("leverage"),
                margin_mode=p.get("marginMode"),
                notional=p.get("notional"),
                liquidation_price=p.get("liquidationPrice"),
            ))

    return all_positions


@router.get("/portfolio", response_model=PortfolioSummary)
async def get_portfolio():
    """Aggregate portfolio snapshot — total PnL, total notional, all positions."""
    positions = await get_all_positions()
    return PortfolioSummary(
        total_positions=len(positions),
        total_pnl=sum(p.pnl for p in positions),
        total_notional=sum(_safe_float(p.notional) for p in positions),
        positions=positions,
    )


@router.get("/unified-monitor", response_model=UnifiedMonitorResponse)
async def get_unified_monitor(
    sync: bool = Query(default=False, description="Sync live MT5 balance/positions before returning (slower)"),
):
    """
    Unified real-time monitor for all configured crypto exchanges AND MT5 accounts.

    Returns:
    - crypto_positions: all open futures/swap positions across every exchange
    - mt5_accounts: all MT5 accounts with balance, equity, and open positions
    - Grand totals: combined PnL, position count

    The JARVIS extension polls this endpoint every 10 seconds (sync=false, fast,
    cached). The popup's manual refresh button calls it with sync=true to pull
    live balance/positions from the mtapi-io bridge.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Crypto positions ─────────────────────────────────────────────────────
    crypto_positions: List[Position] = []
    try:
        crypto_positions = await get_all_positions()
    except Exception as exc:
        logger.warning(f"[JARVIS unified-monitor] crypto positions failed: {exc}")

    # ── Crypto exchange balances ──────────────────────────────────────────────
    # Fetch USDT balance from every connected exchange so the popup shows real
    # wallet/equity alongside MT5 accounts. Each exchange is isolated — one
    # failure never drops the others.
    crypto_accounts: List[CryptoAccountSummary] = []
    try:
        ex_list_bal = exchange_manager.get_all_exchanges()
        for ex_enum in ex_list_bal:
            connector = exchange_manager.get_exchange(ex_enum)
            if not connector:
                continue
            try:
                bal = await connector.get_balance(currency="USDT")
                # CCXT returns {currency: {free, used, total}} or {free, used, total}
                if isinstance(bal, dict):
                    usdt = bal.get("USDT") or bal  # ccxt full vs single-currency
                    crypto_accounts.append(CryptoAccountSummary(
                        exchange=ex_enum.value.capitalize(),
                        currency="USDT",
                        total=float(usdt.get("total") or 0),
                        free=float(usdt.get("free") or 0),
                        used=float(usdt.get("used") or 0),
                    ))
            except Exception as bal_exc:
                logger.debug(f"[JARVIS unified-monitor] balance fetch skipped for {ex_enum.value}: {bal_exc}")
    except Exception as exc:
        logger.debug(f"[JARVIS unified-monitor] crypto balance fetch failed: {exc}")

    # ── MT5 accounts + positions ─────────────────────────────────────────────
    mt5_accounts: List[MT5AccountSummary] = []
    mt5_total_balance = 0.0
    mt5_total_equity = 0.0
    mt5_total_floating = 0.0
    mt5_pos_count = 0

    try:
        from app.core.database import AsyncSessionLocal
        from plugins.MT5TradingPlugin.backend.models import MT5Account, MT5Position
        from sqlalchemy import select as sa_select

        async with AsyncSessionLocal() as db:
            accounts_result = await db.execute(sa_select(MT5Account))
            mt5_accts = accounts_result.scalars().all()

            logger.debug(f"[JARVIS unified-monitor] found {len(mt5_accts)} MT5 account(s) in DB")


            for acct in mt5_accts:
                # Each account is isolated — one failure never drops the others.
                try:
                    # Optional live sync (only when ?sync=true — the manual refresh
                    # button). The 10s auto-poll never syncs, keeping it fast.
                    if sync and acct.api_reachable:
                        try:
                            from plugins.MT5TradingPlugin.backend.services.sync_service import MT5SyncService
                            await MT5SyncService.sync_account(db, acct)
                            await db.commit()
                            await db.refresh(acct)
                        except Exception as sync_exc:
                            logger.debug(f"[JARVIS unified-monitor] live sync skipped for acct {acct.id}: {sync_exc}")

                    # Read cached account data (fast). Live balance/equity is
                    # synced separately by the MT5 plugin when the mtapi-io bridge
                    # is up — we never block the auto-poll on a broker round-trip.
                    pos_result = await db.execute(
                        sa_select(MT5Position).where(MT5Position.account_id == acct.id)
                    )
                    mt5_positions = pos_result.scalars().all()

                    pos_list = [
                        {
                            "ticket": p.mt5_ticket,
                            "symbol": p.symbol,
                            "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                            "volume": float(p.volume or 0),
                            "price_open": float(p.price_open or 0),
                            "price_current": float(p.price_current or 0),
                            "sl": float(p.sl) if p.sl else None,
                            "tp": float(p.tp) if p.tp else None,
                            "profit": float(p.profit or 0),
                            "swap": float(p.swap or 0),
                        }
                        for p in mt5_positions
                    ]

                    floating = float(acct.floating_pnl or 0) or sum(p["profit"] for p in pos_list)
                    acct_balance = float(acct.balance or 0)
                    acct_equity = float(acct.equity or 0)

                    mt5_accounts.append(MT5AccountSummary(
                        account_id=acct.id,
                        name=acct.name or f"Account {acct.id}",
                        login=str(acct.login or ""),
                        server=acct.server or "",
                        balance=acct_balance,
                        equity=acct_equity,
                        floating_pnl=floating,
                        margin=float(acct.margin or 0),
                        free_margin=float(acct.free_margin or 0),
                        currency=acct.currency or "USD",
                        leverage=int(acct.leverage or 1),
                        positions=pos_list,
                        position_count=len(pos_list),
                    ))
                    mt5_total_balance += acct_balance
                    mt5_total_equity += acct_equity
                    mt5_total_floating += floating
                    mt5_pos_count += len(pos_list)
                except Exception as acct_exc:
                    logger.warning(
                        f"[JARVIS unified-monitor] account {acct.id} ({acct.name}) failed: {acct_exc}"
                    )
                    # Still surface the account with cached/zero data so the user
                    # SEES it exists (the whole point — never hide a connected account).
                    try:
                        mt5_accounts.append(MT5AccountSummary(
                            account_id=acct.id,
                            name=acct.name or f"Account {acct.id}",
                            login=str(acct.login or ""),
                            server=acct.server or "",
                            balance=float(acct.balance or 0),
                            equity=float(acct.equity or 0),
                            floating_pnl=float(acct.floating_pnl or 0),
                            margin=float(acct.margin or 0),
                            free_margin=float(acct.free_margin or 0),
                            currency=acct.currency or "USD",
                            leverage=int(acct.leverage or 1),
                            positions=[],
                            position_count=0,
                        ))
                        mt5_total_balance += float(acct.balance or 0)
                        mt5_total_equity += float(acct.equity or 0)
                    except Exception:
                        pass
    except Exception as exc:
        import traceback
        logger.warning(
            f"[JARVIS unified-monitor] MT5 data failed: {exc}\n{traceback.format_exc()}"
        )

    crypto_pnl = sum(p.pnl for p in crypto_positions)
    total_pnl = crypto_pnl + mt5_total_floating
    total_positions = len(crypto_positions) + mt5_pos_count

    return UnifiedMonitorResponse(
        crypto_positions=crypto_positions,
        crypto_accounts=crypto_accounts,
        crypto_total_pnl=crypto_pnl,
        crypto_total_notional=sum(_safe_float(p.notional) for p in crypto_positions),
        mt5_accounts=mt5_accounts,
        mt5_total_balance=mt5_total_balance,
        mt5_total_equity=mt5_total_equity,
        mt5_total_floating_pnl=mt5_total_floating,
        mt5_position_count=mt5_pos_count,
        total_position_count=total_positions,
        total_pnl=total_pnl,
        fetched_at=now,
    )


@router.get("/analyze-positions", response_model=AnalyzePositionsResponse)
async def analyze_open_positions(
    account_id: int = Query(..., description="MT5 account ID"),
    speak: bool = Query(default=True, description="Include spoken summary for JARVIS TTS"),
):
    """
    Run SMC + AI analysis on ALL open MT5 positions for the given account.

    Called automatically every 15 minutes by the JARVIS extension alarm,
    and on-demand when the user clicks 'Analyze Now' in the popup.

    Returns per-position analysis with TP/SL suggestions and an AI verdict,
    plus a spoken summary that JARVIS can read aloud.
    """
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    analyses: List[PositionAnalysis] = []

    try:
        from app.core.database import AsyncSessionLocal
        from plugins.MT5TradingPlugin.backend.models import MT5Account, MT5Position
        from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
            SMCStrategyEngine, Candle
        )
        from plugins.MT5TradingPlugin.backend.services.mt5_client import MT5Client
        from sqlalchemy import select as sa_select

        async with AsyncSessionLocal() as db:
            acct = await db.get(MT5Account, account_id)
            if not acct:
                return AnalyzePositionsResponse(
                    account_id=account_id,
                    positions_analyzed=0,
                    summary=f"Account {account_id} not found.",
                    analyzed_at=now_str,
                )

            pos_result = await db.execute(
                sa_select(MT5Position).where(MT5Position.account_id == account_id)
            )
            positions = pos_result.scalars().all()

            if not positions:
                return AnalyzePositionsResponse(
                    account_id=account_id,
                    positions_analyzed=0,
                    summary="No open positions to analyze.",
                    analyzed_at=now_str,
                )

            client = MT5Client(acct)
            analyzed_symbols = set()

            for pos in positions:
                sym = pos.symbol
                if sym in analyzed_symbols:
                    # Already analyzed this symbol (duplicate positions)
                    continue
                analyzed_symbols.add(sym)

                try:
                    # Fetch H1 candles for SMC analysis
                    raw_candles = await client.get_candles(sym, "H1", 200)
                    candles = [
                        Candle(
                            time=int(c.get("time", 0)),
                            open=float(c.get("open", 0)),
                            high=float(c.get("high", 0)),
                            low=float(c.get("low", 0)),
                            close=float(c.get("close", 0)),
                            volume=float(c.get("volume", 0)),
                        )
                        for c in (raw_candles or [])
                        if c.get("close")
                    ]

                    if len(candles) < 40:
                        analyses.append(PositionAnalysis(
                            ticket=pos.mt5_ticket or 0,
                            symbol=sym,
                            side=pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                            account_id=account_id,
                            analysis_text=f"Insufficient data for {sym} (< 40 bars).",
                            has_suggestion=False,
                            analyzed_at=now_str,
                        ))
                        continue

                    engine = SMCStrategyEngine(
                        symbol=sym,
                        min_rr=2.0,
                        max_rr=10.0,
                        min_confidence=0.55,
                        account_balance=float(acct.balance or 0),
                    )
                    result = engine.analyze(candles)
                    bias = result.get("bias", "neutral")
                    signals = result.get("signals", [])
                    fb = result.get("false_breakout", {})

                    pos_side = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
                    profit = float(pos.profit or 0)
                    pnl_sign = "+" if profit >= 0 else ""

                    # Build analysis text
                    parts = [f"{sym} ({pos_side.upper()}) — Bias: {bias}."]
                    if fb.get("false_break_score", 0) >= 60:
                        if fb.get("sweep_high"):
                            parts.append("⚠ Sweep of highs detected — potential reversal risk.")
                        elif fb.get("sweep_low"):
                            parts.append("⚠ Sweep of lows detected — watch for continuation.")

                    sl_sug = tp_sug = None
                    ai_verdict = None
                    has_sug = False

                    if signals:
                        top = signals[0]
                        aligned = (
                            (pos_side in ("buy", "long") and top["side"] == "buy") or
                            (pos_side in ("sell", "short") and top["side"] == "sell")
                        )
                        if aligned:
                            tp_sug = top.get("take_profit")
                            sl_sug = top.get("stop_loss")
                            has_sug = bool(tp_sug or sl_sug)
                            conf_pct = int((top.get("confidence", 0)) * 100)
                            ai_verdict = f"{conf_pct}% confidence — {top.get('reason', '')[:80]}"
                            parts.append(
                                f"SMC signal aligned: RR {top.get('rr', 0):.1f}. "
                                f"Suggested TP: {tp_sug:.2f if tp_sug else 'N/A'}, "
                                f"SL: {sl_sug:.2f if sl_sug else 'N/A'}."
                            )
                        else:
                            parts.append(
                                f"SMC signal opposing current position "
                                f"({top['side'].upper()} setup in {bias} market)."
                            )

                    parts.append(f"Current P&L: {pnl_sign}{profit:.2f} {acct.currency or 'USD'}.")

                    analyses.append(PositionAnalysis(
                        ticket=pos.mt5_ticket or 0,
                        symbol=sym,
                        side=pos_side,
                        account_id=account_id,
                        analysis_text=" ".join(parts),
                        has_suggestion=has_sug,
                        sl_suggestion=sl_sug,
                        tp_suggestion=tp_sug,
                        ai_verdict=ai_verdict,
                        analyzed_at=now_str,
                    ))
                except Exception as exc:
                    logger.warning(f"[JARVIS analyze] {sym} failed: {exc}")
                    analyses.append(PositionAnalysis(
                        ticket=pos.mt5_ticket or 0,
                        symbol=sym,
                        side="unknown",
                        account_id=account_id,
                        analysis_text=f"Analysis failed for {sym}: {str(exc)[:80]}",
                        has_suggestion=False,
                        analyzed_at=now_str,
                    ))

        # Build spoken summary
        with_suggestions = [a for a in analyses if a.has_suggestion]
        if analyses:
            summary_parts = [f"Analysis complete for {len(analyses)} position(s)."]
            for a in analyses:
                summary_parts.append(a.analysis_text)
            if with_suggestions:
                summary_parts.append(
                    f"{len(with_suggestions)} position(s) have TP/SL suggestions."
                )
            summary = " ".join(summary_parts)
        else:
            summary = "No positions were analyzed."

        # Capture to JARVIS brain
        jarvis_brain_capture(
            "analyze-positions",
            summary=f"Analyzed {len(analyses)} positions for account {account_id}",
            detail=summary[:500],
            importance=0.6,
        )

        return AnalyzePositionsResponse(
            account_id=account_id,
            positions_analyzed=len(analyses),
            analyses=analyses,
            summary=summary,
            analyzed_at=now_str,
        )

    except Exception as exc:
        logger.error(f"[JARVIS analyze-positions] failed: {exc}")
        return AnalyzePositionsResponse(
            account_id=account_id,
            positions_analyzed=0,
            summary=f"Analysis failed: {str(exc)[:120]}",
            analyzed_at=now_str,
        )


# ── Command endpoint ───────────────────────────────────────────────────────────

@router.post("/command", response_model=CommandResult)
async def execute_command(req: CommandRequest):
    """
    Parse and execute a Jarvis voice command.

    Supported patterns
    ──────────────────
    • "take 1000% profit on GWEIUSDT"      → set TP at entry × 11
    • "take profit at 0.025 on GWEIUSDT"   → set TP at absolute price
    • "set stop loss at 5% on ETHUSDT"     → set SL at entry × 0.95
    • "close BTCUSDT" / "close my BTCUSDT position"
    • "what are my positions" / "show positions"
    • "how is BTCUSDT doing"               → status for that symbol
    """
    cmd = (req.command or "").strip().lower()
    logger.info(f"[JARVIS] command received: {cmd!r}")
    ex = req.exchange
    try:
        result = await _dispatch(cmd, ex)
        # ── Brain capture for EVERY request ───────────────────────────────────
        # Trades, TP/SL edits, analysis and position reviews get a rich capture;
        # everything else (errors, queries, chit-chat, unknown commands) still
        # gets logged so NOTHING JARVIS is ever asked is lost to the brains.
        _CAPTURE_ACTIONS = (
            "set_tp", "set_sl", "close", "execute",
            "analyze", "position_status", "list_positions",
        )
        captured = False
        if result.ok and result.action in _CAPTURE_ACTIONS:
            try:
                sym = result.order.get("symbol", "") if result.order else ""
                if not sym:
                    import re as _re_sym
                    # Only match real trading pairs (must end in USD/USDT) so
                    # English words like "WHAT" are never mistaken for a symbol.
                    _m = _re_sym.search(r"\b([A-Z]{2,8}USDT?)\b", (cmd or "").upper())
                    sym = _m.group(1) if _m else ""
                # Trades/edits are higher-importance learnings than read-only queries.
                _imp = 0.8 if result.action in ("set_tp", "set_sl", "close", "execute") else 0.4
                jarvis_brain_capture(
                    action=result.action,
                    symbol=sym,
                    summary=(result.speech or result.detail or "")[:200],
                    detail=result.detail or "",
                    tags=["jarvis", result.action, sym],
                    order_id=result.order.get("id", "") if result.order else "",
                    importance=_imp,
                )
                captured = True
            except Exception:
                pass
        # Catch-all: log every remaining request (failures, queries, unknown, …)
        if not captured:
            try:
                import re as _re_sym2
                _m2 = _re_sym2.search(r"\b([A-Z]{2,8}USDT?)\b", (cmd or "").upper())
                _sym2 = _m2.group(1) if _m2 else ""
                jarvis_learn_all_brains(
                    action=result.action or "command",
                    symbol=_sym2,
                    summary=(cmd or "")[:200],
                    detail=(result.speech or result.detail or "")[:600],
                    tags=["jarvis", "request", result.action or "command",
                          "ok" if result.ok else "error"],
                    importance=0.3 if result.ok else 0.35,
                )
            except Exception:
                pass
        return result
    except BaseException as e:
        friendly = "Sorry Sir, an internal error occurred."
        logger.error(f"[JARVIS] unhandled error in execute_command: {e}")
        return CommandResult(ok=False, action="error", detail=friendly, speech=friendly)


@router.get("/skill")
async def get_jarvis_skill(symbol: str = Query(..., min_length=2, description="Pair symbol, e.g. EURUSD or BTC/USDT")):
    """Best-trader skill recall for JARVIS chat (B). Returns SKILL.md + linked agents + JARVIS chair.

    Used by PaulChat `/skill SYMBOL` and by the Trading Room chair injection preview.
    Plain `GET /jarvis/skill?symbol=EURUSD` → 200 with skill, 404 if not bootstrapped
    (hint to run bootstrap).
    """
    norm = (symbol or "").replace("/", "").strip().upper()
    if not norm:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        from app.hermes_bridge.skill_registry import load_skill_md, get_skill_for_symbol
        loaded = load_skill_md(norm)
        if not loaded:
            # try canonicalize via market_data (handles BTC/USD → BTCUSD)
            try:
                from app.services import market_data as _md
                canon, _ = _md.canonicalize_for_analysis(norm)
                if canon:
                    loaded = load_skill_md(canon)
            except Exception:
                pass
        if not loaded:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"best-trader skill not found for {norm} — run python scripts/bootstrap_hermes_best_trader_skills.py")
        entry = loaded["entry"]
        # Enrich with SOUL preview (so chat shows same soul the room sees)
        soul_preview = ""
        try:
            from pathlib import Path as _P
            from app.core.config import settings as _s
            sp = _P(getattr(_s, "SOUL_PATH", "SOUL.md") or "SOUL.md")
            if not sp.is_absolute():
                for cand in [sp, _P("SOUL.md"), _P("../SOUL.md"), _P(__file__).resolve().parents[3] / "SOUL.md"]:
                    if cand.exists():
                        sp = cand
                        break
            if sp.exists():
                soul_preview = sp.read_text(encoding="utf-8")[:600]
        except Exception:
            pass
        return {
            "symbol": entry.get("symbol") or norm,
            "normalized": norm,
            "asset_class": entry.get("asset_class"),
            "group": entry.get("group"),
            "linked_agents": entry.get("linked_agents", []),
            "jarvis": entry.get("jarvis"),
            "is_best_trader": entry.get("is_best_trader"),
            "meta": entry.get("meta", {}),
            "frontmatter": entry.get("frontmatter", {}),
            "md": loaded["md"],
            "path": loaded["path"],
            "soul_preview": soul_preview,
        }
    except Exception as e:
        # Re-raise HTTPException intact
        if "HTTPException" in type(e).__name__:
            raise
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e)[:300])


async def _dispatch(cmd: str, ex: Optional[str]) -> CommandResult:  # noqa: C901
    """Inner dispatcher — all pattern matching happens here."""

    # ── execute / open / place a new position ─────────────────────────────────
    # Must come FIRST so the AI page-handler never gets a chance to hallucinate.
    #
    # Handles forms like:
    #   execute VELVETUSDT short 2 lot at 1.7000; set SL 1.7500; TP1 1.5500; TP2 1.4500
    #   open BTCUSDT long 0.5 contracts at market
    #   short ETHUSDT 1 at 3200; sl 3400; tp 2800
    #   go long SOLUSDT 10
    _exe_m = (
        re.search(
            r'(?:execute|open|place|trade|enter)\s+'
            r'(\w+)\s+'                                       # symbol
            r'(long|short|buy|sell)\s+'                       # side
            r'\$?(\d+(?:\.\d+)?)\s*(?:lots?|contracts?|x)?\s*'  # size ($ OK)
            r'(?:at\s+\$?([\d.]+|market))?',                  # optional price
            cmd,
        )
        or re.search(                                         # "short SYMBOL 2 at 1.70"
            r'(?:^|\b)(long|short|buy|sell)\s+'
            r'(\w{3,15})\s+'
            r'\$?(\d+(?:\.\d+)?)\s*(?:lots?|contracts?|x)?\s*'
            r'(?:at\s+\$?([\d.]+|market))?',
            cmd,
        )
        or re.search(                                         # "go long SYMBOL 2"
            r'go\s+(long|short)\s+(?:on\s+)?(\w{3,15})\s+\$?(\d+(?:\.\d+)?)',
            cmd,
        )
    )
    if _exe_m:
        # Group indices differ between the three patterns above.
        # Detect by checking if group[1] is a direction word.
        _SIDES = {"long", "short", "buy", "sell"}
        g = _exe_m.groups()
        if g[1] and g[1].lower() in _SIDES:
            # Pattern 1: execute SYMBOL SIDE SIZE [at PRICE]
            sym_raw, side_raw, size_raw = g[0], g[1], g[2]
            price_raw = g[3] if len(g) > 3 else None
        else:
            # Pattern 2/3: SIDE SYMBOL SIZE [at PRICE]  or  go SIDE SYMBOL SIZE
            side_raw, sym_raw, size_raw = g[0], g[1], g[2]
            price_raw = g[3] if len(g) > 3 else None

        symbol_exec  = sym_raw.upper()
        side_exec    = side_raw.lower().replace("buy", "long").replace("sell", "short")
        # Strip any leading '$' from size/price (e.g. "$2 lots" → 2)
        size_exec    = float(str(size_raw).lstrip("$"))
        price_str_raw = str(price_raw).lstrip("$") if price_raw else None
        price_exec   = None if (not price_str_raw or price_str_raw.lower() == "market") else float(price_str_raw)

        # ── Extract SL ──────────────────────────────────────────────────────
        sl_m   = re.search(r'(?:set\s+)?(?:sl|stop[\s-]?loss)[;:\s]+([\d.]+)', cmd)
        sl_val = float(sl_m.group(1)) if sl_m else None

        # ── Extract TP1 / TP ────────────────────────────────────────────────
        tp1_m   = re.search(r'tp1?[;:\s]+([\d.]+)', cmd)
        tp1_val = float(tp1_m.group(1)) if tp1_m else None

        # ── Extract TP2 ──────────────────────────────────────────────────────
        tp2_m   = re.search(r'tp2[;:\s]+([\d.]+)', cmd)
        tp2_val = float(tp2_m.group(1)) if tp2_m else None

        return await _execute_order(
            symbol=symbol_exec,
            side=side_exec,
            size=size_exec,
            price=price_exec,
            sl_price=sl_val,
            tp1_price=tp1_val,
            tp2_price=tp2_val,
            ex_name=ex,
        )

    # ── analyse ALL open positions with news context ───────────────────────────
    # MUST be checked BEFORE the general _ana_m block.  Without this guard,
    # "analyse current positions" matches the _ana_m pattern and JARVIS tries
    # to resolve "CURRENT" as a Bitget trading pair — returning a nonsense
    # "Did you mean CETUS?" error instead of the intended portfolio review.
    #
    # Catches phrases like:
    #   "analyse current positions"         "analyze my positions"
    #   "with coming news analyse positions" "how will today's news impact my positions"
    #   "news impact on positions"          "positions and today's news"
    _NEWS_POS_PAT = re.compile(
        r'(?:'
        # "analyse [my [current]] positions"  — allow up to 3 modifier words
        # e.g. "analyse my current open positions"
        r'(?:analys[ei]|analyze|assess|review|check)\s+'
            r'(?:(?:my|all|open|current|the|latest|active|live|existing|today[\w]*)\s+){0,3}positions?'
        # "news impact on [my] positions"
        r'|(?:news|headlines?|market\s+news)\s+(?:impact|affect|effect)\s+'
            r'(?:on\s+)?(?:(?:my|current|open|the)\s+){0,2}positions?'
        # "how will today's news impact my positions"
        r'|how\s+will\s+(?:\w+\s+){0,5}news\s+(?:impact|affect)\s+'
            r'(?:(?:my|current|open|the)\s+){0,2}positions?'
        # "positions and/with today's news"
        r'|positions?\s+(?:and|with|given|considering)\s+(?:\w+\s+){0,3}news'
        r')',
        re.IGNORECASE,
    )
    if _NEWS_POS_PAT.search(cmd):
        return await _analyze_positions_with_news(cmd)

    # ── market analysis / monitor / sniper commands ────────────────────────────
    # These MUST be intercepted here so the AI page-handler CANNOT hallucinate
    # a fake execution.  We do real on-chain analysis and PROPOSE a trade —
    # the user must then say the explicit execute command to actually place it.
    _ana_m = (
        re.search(
            r'(?:monitor|watch|analyze|analyse|scan|sniper?|check)\s+'
            r'(\w{2,12})',
            cmd,
        )
        or re.search(
            r'find\s+(?:(?:more|a|some)\s+)?(?:buy|sell|long|short)\s+entr(?:y|ies)'
            r'(?:.*(?:for|on|in)\s+(\w{2,12}))?',
            cmd,
        )
    )
    if _ana_m:
        # Extract symbol — prefer explicit mention in command
        _sym_m = re.search(
            r'\b((?:BTC|ETH|SOL|BNB|XRP|DOGE|ADA|MATIC|AVAX|DOT|LINK|GWEI|VELVET|'
            r'PEPE|SHIB|WIF|BONK|FLOKI|[A-Z]{2,10})USDT?)\b',
            cmd.upper(),
        )
        # The regexes above are TOKEN FINDERS only — canonicalisation belongs to
        # market_data. This used to append "USDT" unconditionally, which turned
        # GBPUSD into GBPUSDUSDT and made every FX pair, metal and index fail the
        # forex gate below and dead-end on a Bitget lookup.
        _raw_token = ""
        if _sym_m:
            _raw_token = _sym_m.group(1)
        elif _ana_m.lastindex and _ana_m.group(_ana_m.lastindex):
            _raw_token = _ana_m.group(_ana_m.lastindex).upper()

        sym_candidate, _ = market_data.canonicalize_for_analysis(_raw_token)

        if sym_candidate:
            return await _analyze_symbol(sym_candidate, cmd, ex, deep=_wants_deep_research(cmd))
        return CommandResult(
            ok=False, action="analyze",
            detail="Which symbol should I analyse? E.g. 'monitor SOLUSDT'",
            speech="Which symbol should I analyse, Sir?",
        )

    # ── take / set TP by percentage ───────────────────────────────────────────
    # (re.search patterns below — keep separated so each can fail independently)
    m = re.search(
        r'(?:take|set)\s+(?:a\s+)?(\d+(?:\.\d+)?)\s*%\s*'
        r'(?:profit|return|roi|tp|take[\s-]profit)(?:\s+on\s+(\w+))?',
        cmd,
    )
    if m:
        pct    = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        if not symbol:
            return _err("set_tp", "Could not determine symbol from command")
        return await _set_tp_pct(symbol, pct, ex)

    # ── set TP at absolute price ───────────────────────────────────────────────
    m = re.search(
        r'(?:set\s+)?(?:tp|take[\s-]profit)\s+at\s+([\d.]+)(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        price  = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_tp_price(symbol, price, ex)

    # ── set SL by percentage ───────────────────────────────────────────────────
    m = re.search(
        r'set\s+(?:a\s+)?(?:stop[\s-]loss|sl)\s+at\s+(\d+(?:\.\d+)?)\s*%(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        pct    = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_sl_pct(symbol, pct, ex)

    # ── set SL at absolute price ───────────────────────────────────────────────
    m = re.search(
        r'(?:set\s+)?(?:stop[\s-]loss|sl)\s+at\s+([\d.]+)(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        price  = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_sl_price(symbol, price, ex)

    # ── close position ─────────────────────────────────────────────────────────
    m = re.search(r'close(?:\s+my)?\s+(\w+)(?:\s+position)?', cmd)
    if m:
        symbol = m.group(1).upper()
        return await _close_position(symbol, ex)

    # ── list all positions ─────────────────────────────────────────────────────
    if re.search(r'(?:show|list|what(?:\s+are)?|get)\s+(?:my\s+)?(?:open\s+)?positions?', cmd):
        return await _list_positions()

    # ── status for a specific symbol ───────────────────────────────────────────
    m = re.search(r'how\s+is\s+(\w+)(?:\s+doing)?', cmd)
    if m:
        symbol = m.group(1).upper()
        return await _position_status(symbol, ex)

    # ── latest news / market update ────────────────────────────────────────────
    # Catches: "what's the latest news", "market update", "what's happening",
    # "any crypto news", "give me the news", "market overview", etc.
    _NEWS_Q = re.compile(
        r'(?:'
        r'what(?:\'?s?\s+(?:the\s+)?(?:latest|happening|going\s+on|new(?:s)?))'
        r'|(?:(?:give|show|tell)\s+(?:me\s+)?(?:the\s+)?(?:latest\s+)?news)'
        r'|(?:(?:latest|recent|current|today(?:\'?s?)?|live|real[\s-]time)\s+'
        r'(?:news|update|headlines|market))'
        r'|(?:market\s+(?:update|overview|summary|news|brief))'
        r'|(?:any\s+(?:news|updates?|headlines?))'
        r'|(?:crypto\s+(?:news|update))'
        r'|(?:what\s+is\s+(?:happening|going\s+on)\s+(?:in\s+)?(?:the\s+)?market)'
        r')',
        re.IGNORECASE,
    )
    if _NEWS_Q.search(cmd):
        return await _handle_live_news_query(cmd)

    return CommandResult(
        ok=False, action="unknown",
        detail=f"Command not recognised: {cmd!r}",
        speech=f"Sorry Sir, I didn't understand that command.",
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _err(action: str, msg: str) -> CommandResult:
    return CommandResult(ok=False, action=action, detail=msg, speech=msg)


async def _fetch_live_prices_brief(symbols: list[str] | None = None) -> str:
    """Live prices for the news brief — delegates to the shared resolver.

    Used to assemble crypto tickers inline, filtered on "/USDT", which is why
    the brief could never mention gold, an index or an FX pair. market_data
    serves every asset class from one priority chain, so this is now a thin
    adapter that keeps the existing call site and return shape.
    """
    return await market_data.price_block(
        symbols or [], include_top_crypto=True, max_lines=60
    )


async def _fetch_live_news_brief() -> str:
    """Fetch the latest market news from live sources for the news brief.

    Pulls from: CryptoPanic (FOMO/breaking), CoinMarketCap, RSS feeds.
    Returns a formatted block of real headlines.
    """
    import httpx as _hx
    import xml.etree.ElementTree as ET

    headlines: List[str] = []

    # CryptoPanic (public, no auth needed)
    try:
        async with _hx.AsyncClient(timeout=6.0) as c:
            r = await c.get(
                "https://cryptopanic.com/api/v1/posts/?auth_token=free&public=true&kind=news",
                headers={"User-Agent": "JARVIS/1.0"},
            )
            for item in (r.json().get("results") or [])[:6]:
                title = (item.get("title") or "").strip()
                if title:
                    votes = item.get("votes") or {}
                    fomo = votes.get("positive", 0) - votes.get("negative", 0)
                    fomo_tag = f" [+{fomo}]" if fomo > 2 else ""
                    headlines.append(f"[CryptoPanic]{fomo_tag} {title[:140]}")
    except Exception:
        pass

    # CoinMarketCap news (public)
    try:
        async with _hx.AsyncClient(timeout=6.0) as c:
            r = await c.get(
                "https://api.coinmarketcap.com/content/v3/news?start=1&limit=6",
                headers={"User-Agent": "JARVIS/1.0"},
            )
            data = r.json()
            items = data.get("data", {}).get("list") or data.get("data", []) or []
            for item in items[:5]:
                title = (item.get("title") or item.get("headline") or "").strip()
                if title:
                    headlines.append(f"[CoinMarketCap] {title[:140]}")
    except Exception:
        pass

    # RSS fallback (CoinDesk)
    if len(headlines) < 4:
        try:
            async with _hx.AsyncClient(timeout=6.0) as c:
                r = await c.get(
                    "https://www.coindesk.com/arc/outboundfeeds/rss/",
                    headers={"User-Agent": "JARVIS/1.0"},
                )
                root = ET.fromstring(r.text)
                for item in root.findall(".//item")[:6]:
                    title = (item.findtext("title") or "").strip()
                    if title:
                        headlines.append(f"[CoinDesk] {title[:140]}")
        except Exception:
            pass

    if headlines:
        return "LIVE NEWS HEADLINES (just fetched from public APIs):\n" + "\n".join(
            f"  - {h}" for h in headlines[:12]
        )
    return "LIVE NEWS: Unable to fetch right now."


async def _handle_live_news_query(cmd: str) -> CommandResult:
    """Handle 'latest news' / 'market update' queries with LIVE data only.

    Fetches live prices AND live news before calling AI, so the AI CANNOT
    hallucinate stale training-data prices or make up outdated headlines.

    The live data brief is passed to the AI as the user message context, making
    it impossible for the AI to substitute training data.
    """
    from datetime import datetime as _dt

    # Fetch live data concurrently
    prices_brief, news_brief = await asyncio.gather(
        _fetch_live_prices_brief(),
        _fetch_live_news_brief(),
        return_exceptions=True,
    )
    if isinstance(prices_brief, Exception):
        prices_brief = "LIVE PRICES: fetch failed."
    if isinstance(news_brief, Exception):
        news_brief = "LIVE NEWS: fetch failed."

    now_str = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    data_brief = (
        f"{prices_brief}\n\n"
        f"{news_brief}\n\n"
        f"[Data fetched at: {now_str}]\n\n"
        "STRICT RULE: Use ONLY the prices and news above. "
        "Your training data is months/years old and has WRONG prices. "
        "Address the user as 'Sir'. Compose a concise market snapshot "
        "(5-6 bullet points: crypto prices/moves, key news, macro context, "
        "1 actionable insight). Keep under 150 words."
    )

    # Ask AI to compose the brief using the live data
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
        resp = await db_chat(
            db,
            [
                {
                    "role": "system",
                    "content": (
                        "You are JARVIS, an elite trading analyst. "
                        "You ONLY use the LIVE PRICES and NEWS provided — never training data. "
                        "Training data has severely outdated prices (e.g. BTC at $27k when it is now much higher). "
                        "If a price is not in the provided data, say you don't have a live feed for it. "
                        "Compose a thorough, JARVIS-style market brief covering key price levels, "
                        "notable news, market sentiment, and what traders should watch. "
                        "Write at least 150 words. Never truncate mid-sentence. Address user as 'Sir'."
                    ),
                },
                {"role": "user", "content": data_brief},
            ],
            temperature=0.35,
            max_tokens=1200,
            agent_name="jarvis-live-news",
            source="jarvis-command",
        )

    if resp.get("ok") and resp.get("content"):
        brief_text = str(resp["content"]).strip()
    else:
        # AI failed — compose from raw live data (no hallucination possible)
        brief_text = f"Sir, here's the live market data as of {now_str}:\n\n{prices_brief}\n\n{news_brief}"

    # Fire news collection to update brains
    _fire_brain_news_collection()

    return CommandResult(
        ok=True,
        action="market_news",
        detail=brief_text,
        speech=brief_text[:600],
        order={
            "prices_fetched": "live",
            "news_fetched": "live",
            "timestamp": now_str,
        },
    )


async def _execute_order(
    symbol: str,
    side: str,        # "long" | "short"
    size: float,
    price: Optional[float],   # None → market order
    sl_price: Optional[float],
    tp1_price: Optional[float],
    tp2_price: Optional[float],
    ex_name: Optional[str],
) -> CommandResult:
    """
    Place a new futures position on Bitget (or configured exchange).

    Uses the native Bitget SDK so the order IS actually submitted to the exchange.
    Preset TP1 and SL are attached to the entry order.  TP2 is placed as a
    separate TPSL plan order after the entry order is submitted.
    """
    # ── Find a connector ──────────────────────────────────────────────────────
    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    if not ex_list:
        return _err("execute", "No exchange configured — check your API credentials.")

    connector = exchange_manager.get_exchange(ex_list[0])
    if not connector:
        return _err("execute", f"Exchange {ex_list[0].value} not initialised.")

    nc = getattr(connector, "native_client", None)
    if not nc:
        return _err("execute", "Native Bitget client not available. Check BITGET_PASSPHRASE.")

    # ── Map side to Bitget API direction ──────────────────────────────────────
    # long position = buy to open;  short position = sell to open
    bitget_side  = "buy" if side == "long" else "sell"
    close_side   = "sell" if side == "long" else "buy"
    order_type   = "limit" if price else "market"
    size_str     = _fmt_size(size)
    price_str    = _round_price(price) if price else None
    sl_str       = _round_price(sl_price) if sl_price else None
    tp1_str      = _round_price(tp1_price) if tp1_price else None

    logger.info(
        f"[JARVIS] execute_order: {symbol} {side} {size_str} @ "
        f"{price_str or 'market'} | SL={sl_str} TP1={tp1_str}"
    )

    # ── Error codes where we auto-retry with progressively smaller sizes ─────
    # 40921 = exceeds max position level for tier (existing positions near limit)
    # 40762 = exceeds available balance
    # 45110 = below Bitget's 5 USDT minimum notional
    # 40809 = size out of allowed range
    _SIZE_ERROR_CODES = {'40921', '40762', '45110', '40809', '40810'}

    async def _place_entry(use_size: str, note: str = "") -> tuple:
        """Try to place the entry order. Returns (result_dict, order_id, note)."""
        r = await nc.place_futures_order(
            symbol=symbol,
            margin_coin="USDT",
            side=bitget_side,
            order_type=order_type,
            size=use_size,
            price=price_str,
            trade_side="open",
            preset_stop_loss_price=sl_str,
            preset_stop_surplus_price=tp1_str,
        )
        oid = (r.get("data") or {}).get("orderId", "unknown")
        return r, oid, note

    result    = None
    order_id  = "unknown"
    auto_note = ""   # set if we auto-resized

    try:
        result, order_id, auto_note = await _place_entry(size_str)
    except BaseException as e:
        err_msg  = _friendly_exchange_error(e)
        raw      = str(e)
        code_m   = re.search(r'\[(\d+)\]', raw)
        err_code = code_m.group(1) if code_m else ""

        if err_code not in _SIZE_ERROR_CODES:
            logger.error(f"[JARVIS] execute_order failed [{err_code}]: {e}")
            return _err("execute", err_msg)

        # ── Auto-size: fetch equity, try halved / 1 % / minimum ──────────────
        logger.warning(f"[JARVIS] size error [{err_code}] — attempting auto-resize")

        equity = 0.0
        try:
            bal = await nc.get_futures_accounts(product_type="USDT-FUTURES")
            for acc in (bal.get("data") or []):
                eq = float(acc.get("equity") or acc.get("usdtEquity") or 0)
                if eq > equity:
                    equity = eq
        except BaseException as be:
            logger.warning(f"[JARVIS] could not fetch equity: {be}")

        # Candidate sizes: half, quarter, 1% portfolio, minimum=1
        ref_price = price or 1.0
        pct1_size = max(1.0, math.floor(equity * 0.01 / ref_price)) if equity > 0 else 1.0
        raw_candidates = [math.floor(size / 2), math.floor(size / 4), pct1_size, 1.0]
        candidates: List[float] = sorted(
            {c for c in raw_candidates if 1.0 <= c < size},
            reverse=True,
        ) or [1.0]

        last_err = err_msg
        placed   = False
        for candidate in candidates:
            cand_str = _fmt_size(candidate)
            logger.info(f"[JARVIS] auto-resize: trying {cand_str} contracts")
            try:
                result, order_id, _ = await _place_entry(cand_str)
                size = candidate
                auto_note = (
                    f"\n⚠️ Requested {size_str} contracts failed ({err_msg}). "
                    f"Auto-resized to **{cand_str} contracts**"
                    + (f" (≈1% of {equity:.2f} USDT equity)" if equity > 0 else "")
                    + "."
                )
                logger.info(f"[JARVIS] auto-sized order placed: {order_id} ({cand_str} contracts)")
                placed = True
                break
            except BaseException as e2:
                last_err = _friendly_exchange_error(e2)
                logger.warning(f"[JARVIS] size {cand_str} also failed: {e2}")

        if not placed:
            if err_code == '40921':
                friendly = (
                    f"{symbol} position level is at its maximum for your account tier. "
                    f"Your existing {symbol} positions are filling the tier's notional limit. "
                    f"Close some existing {symbol} positions first, then retry. "
                    f"(Bitget [{err_code}])"
                )
            else:
                friendly = (
                    f"Could not place order even at minimum size (1 contract). "
                    f"Last error: {last_err}"
                )
            return _err("execute", friendly)

    logger.info(f"[JARVIS] entry order placed: {order_id}")

    # ── TP2 placed as a fire-and-forget background task ───────────────────────
    # We do NOT await this — response returns immediately after the entry order.
    tp2_id = ""
    if tp2_price:
        tp2_str  = _round_price(tp2_price)
        tp2_size = _fmt_size(max(1.0, size / 2))
        _nc = nc  # capture for closure

        async def _bg_tp2() -> None:
            try:
                r2 = await _nc.place_futures_tpsl_order(
                    symbol=symbol,
                    margin_coin="USDT",
                    plan_type="pos_profit",
                    trigger_price=tp2_str,
                    size=tp2_size,
                    side=close_side,
                    trigger_type="fill_price",
                )
                oid2 = (r2.get("data") or {}).get("orderId", "")
                logger.info(f"[JARVIS] TP2 plan order placed in background: {oid2}")
            except BaseException as e2:
                logger.warning(f"[JARVIS] TP2 background placement failed: {e2}")

        asyncio.create_task(_bg_tp2())  # fire-and-forget
        tp2_id = "pending"

    # ── Build confirmation message (keep short for fast TTS) ─────────────────
    price_label = f"at {price}" if price else "at market"
    sl_part  = f", SL {sl_price}" if sl_price else ""
    tp1_part = f", TP1 {tp1_price}" if tp1_price else ""
    tp2_part = f", TP2 {tp2_price}" if tp2_price else ""
    speech = (
        f"{symbol} {side} {size} {price_label} — submitted. "
        f"ID {order_id}.{sl_part}{tp1_part}{tp2_part}"
    )
    detail = (
        f"Order {order_id} | {symbol} {side} {size}x {price_label}"
        + (f" | SL={sl_price}" if sl_price else "")
        + (f" | TP1={tp1_price}" if tp1_price else "")
        + (f" | TP2={tp2_price}" + (f" id={tp2_id}" if tp2_id else " (pending)") if tp2_price else "")
        + auto_note
    )

    return CommandResult(
        ok=True, action="execute",
        detail=detail,
        speech=speech,
        order={
            "id": order_id, "symbol": symbol, "side": side,
            "size": size, "price": price,
            "sl": sl_price, "tp1": tp1_price,
            "tp2": tp2_price, "tp2_id": tp2_id,
        },
    )


def _ema(closes: List[float], period: int) -> float:
    """Exponential Moving Average over `closes` list (last value returned)."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1 - k)
    return val


def _rsi(closes: List[float], period: int = 14) -> float:
    """RSI(period) using Wilder smoothing, returns 0–100."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Average True Range (Wilder), in price units. 0.0 when not computable.

    Stops and targets must scale with how much the instrument actually moves.
    A fixed percentage is wrong in both directions at once: 1.5% is a huge stop
    on XAUUSD and a meaningless one on a low-volatility FX pair, so the same
    code produces setups that are untradeable on one instrument and instantly
    stopped out on another.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(n - period, n):
        prev_close = closes[i - 1]
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        ))
    return sum(trs) / len(trs) if trs else 0.0


def _directional_bias(
    current: float,
    ema50: float,
    ema200: float,
    rsi: float,
    trend: str,
    buy_pct: float = 0.0,
    sell_pct: float = 0.0,
) -> tuple[str, float, List[str]]:
    """Weigh every signal together and return (bias, confidence 0–1, reasons).

    Replaces a chain of last-writer-wins overrides in which each indicator
    silently discarded the previous one — volume beat RSI, which beat the trend.
    That produced the two classic failure modes:

      * A textbook uptrend with RSI 75 was reported as SHORT, because in a
        strong trend RSI sits overbought for long stretches. Overbought is
        confirmation there, not a reversal signal, so RSI is only allowed to
        argue for mean reversion when price is actually ranging.
      * Retail buy/sell sentiment overrode the entire technical picture. It is
        now one weighted input among several.

    Confidence reflects how much the signals agree, so a coin-flip setup can be
    reported as one instead of being presented with the same certainty as a
    high-conviction one.
    """
    score = 0.0
    weight = 0.0
    reasons: List[str] = []

    # ── Trend structure (heaviest weight) ────────────────────────────────────
    if trend == "uptrend":
        score += 2.0; weight += 2.0
        reasons.append("price and EMA50 above EMA200")
    elif trend == "downtrend":
        score -= 2.0; weight += 2.0
        reasons.append("price and EMA50 below EMA200")
    elif ema200 > 0:
        tilt = 0.6 if current > ema200 else -0.6
        score += tilt; weight += 0.6
        reasons.append("ranging, price " + ("above" if tilt > 0 else "below") + " EMA200")

    # ── Momentum (EMA50 vs EMA200 separation) ────────────────────────────────
    if ema200 > 0 and ema50 > 0:
        sep = (ema50 - ema200) / ema200
        if abs(sep) > 0.002:
            score += max(-1.0, min(1.0, sep * 40)); weight += 1.0

    # ── RSI ──────────────────────────────────────────────────────────────────
    if rsi >= 70:
        if trend == "uptrend":
            # Confirmation, not a reversal — but conviction is trimmed because
            # entries chased at these levels have poor reward-to-risk.
            score += 0.4; weight += 1.0
            reasons.append(f"RSI {rsi:.0f} overbought — trend strength, poor entry")
        else:
            score -= 1.2; weight += 1.2
            reasons.append(f"RSI {rsi:.0f} overbought in a range — mean reversion")
    elif rsi <= 30:
        if trend == "downtrend":
            score -= 0.4; weight += 1.0
            reasons.append(f"RSI {rsi:.0f} oversold — trend strength, poor entry")
        else:
            score += 1.2; weight += 1.2
            reasons.append(f"RSI {rsi:.0f} oversold in a range — mean reversion")
    else:
        # Mild directional tilt away from the midpoint.
        score += (rsi - 50) / 50 * 0.5; weight += 0.5

    # ── Order-flow / sentiment split (when the venue reports it) ─────────────
    if buy_pct or sell_pct:
        total = buy_pct + sell_pct
        if total > 0:
            flow = (buy_pct - sell_pct) / total          # −1 … 1
            score += flow * 1.0; weight += 1.0
            if abs(flow) > 0.2:
                reasons.append(
                    f"order flow {buy_pct:.0f}% buy / {sell_pct:.0f}% sell"
                )

    if weight <= 0:
        return "long" if current > ema200 else "short", 0.0, reasons

    normalised = score / weight                            # −1 … 1
    bias = "long" if normalised >= 0 else "short"
    return bias, round(min(1.0, abs(normalised)), 2), reasons


def _build_setup(
    bias: str,
    current: float,
    swing_high: float,
    swing_low: float,
    atr: float,
) -> Optional[Dict[str, float]]:
    """Build an entry / stop / target set, or None when no sane one exists.

    Returns None rather than a broken setup. The previous version computed
    reward with ``abs()``, so a take-profit that had landed on the *wrong side*
    of the entry — which happens whenever the recent range is tighter than the
    fixed percentage offsets — still reported a healthy positive R:R. A losing
    setup presented as a 2:1 winner is worse than no setup at all.
    """
    dp = _price_dp(current)
    # Fall back to a fraction of the recent range when ATR is unavailable, so a
    # short history degrades the stop distance rather than the whole analysis.
    rng = max(swing_high - swing_low, 0.0)
    unit = atr if atr > 0 else rng * 0.25
    if unit <= 0:
        return None

    # A pullback entry sits away from current price, so a target measured only
    # from the entry can land on the wrong side of where price is trading right
    # now — "buy the dip at 4321, take profit at 4369" while price is already
    # 4376 is not a trade, it is an instruction to sell into a level the market
    # has passed. Targets are therefore held beyond current price as well as
    # beyond the entry.
    clearance = unit * 0.5

    if bias == "long":
        # Wait for a pullback toward support, but never below the swing low and
        # never above current price (that would fill instantly at a worse level).
        entry = min(current, swing_low + unit * 0.5)
        sl    = entry - unit * 1.5
        tp1   = max(entry + unit * 1.5, current + clearance)
        tp2   = max(swing_high, tp1 + unit * 1.5)
        if not (sl < entry < tp1 < tp2):
            return None
        risk, reward1, reward2 = entry - sl, tp1 - entry, tp2 - entry
    else:
        entry = max(current, swing_high - unit * 0.5)
        sl    = entry + unit * 1.5
        tp1   = min(entry - unit * 1.5, current - clearance)
        tp2   = min(swing_low, tp1 - unit * 1.5)
        if not (sl > entry > tp1 > tp2):
            return None
        risk, reward1, reward2 = sl - entry, entry - tp1, entry - tp2

    if risk <= 0:
        return None

    return {
        "entry": round(entry, dp),
        "sl":    round(sl, dp),
        "tp1":   round(tp1, dp),
        "tp2":   round(tp2, dp),
        "rr1":   round(reward1 / risk, 1),
        "rr2":   round(reward2 / risk, 1),
        "atr":   round(unit, dp),
    }


# ── Deep-research helpers (volume · news · AI narrative) ─────────────────────
# These power JARVIS's rich, human, multi-tool pair analysis.  Each is fully
# self-contained and NEVER raises — a failure just omits that data section so
# the core proposal is always returned.

def _wants_deep_research(cmd: str) -> bool:
    """True when the user asked JARVIS to go deep — search news, scrape, research."""
    s = (cmd or "").lower()
    return bool(re.search(
        r"\b(news|headline|sentiment|research|deep|thorough|everything|"
        r"in[\s-]?depth|full|scrape|search|internet|web|fundament)\w*",
        s,
    ))


async def _crypto_volume_analysis(connector, ccxt_sym: str, ohlcv: list) -> Optional[Dict[str, Any]]:
    """Compute buy/sell volume pressure from OHLCV plus 24h ticker volume.

    Returns None on any failure (volume section simply omitted)."""
    try:
        vols   = [float(c[5]) for c in ohlcv if len(c) > 5]
        closes = [float(c[4]) for c in ohlcv]
        opens  = [float(c[1]) for c in ohlcv]
        if len(vols) < 5:
            return None
        last_vol = vols[-1]
        avg_vol  = sum(vols[-20:]) / min(len(vols), 20)
        # Up-candle vs down-candle volume over the last 20 candles → pressure proxy.
        buy_vol = sell_vol = 0.0
        for o, c, v in zip(opens[-20:], closes[-20:], vols[-20:]):
            if c >= o:
                buy_vol += v
            else:
                sell_vol += v
        tot = buy_vol + sell_vol
        buy_pct  = round(buy_vol / tot * 100, 1) if tot else 50.0
        sell_pct = round(100 - buy_pct, 1)

        quote_vol_24h = None
        try:
            ticker = await connector.exchange.fetch_ticker(f"{ccxt_sym}:USDT")
        except Exception:
            try:
                ticker = await connector.exchange.fetch_ticker(ccxt_sym)
            except Exception:
                ticker = None
        if ticker:
            quote_vol_24h = ticker.get("quoteVolume") or ticker.get("baseVolume")

        spike = round(last_vol / avg_vol, 2) if avg_vol else 1.0
        return {
            "buy_pressure_pct": buy_pct,
            "sell_pressure_pct": sell_pct,
            "last_candle_volume": round(last_vol, 4),
            "avg_volume_20": round(avg_vol, 4),
            "volume_spike_x": spike,
            "quote_volume_24h": quote_vol_24h,
        }
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] volume analysis skipped: {e}")
        return None


async def _fetch_pair_news(base: str, coin_name: Optional[str], deep: bool) -> Dict[str, Any]:
    """Fetch recent news for a token and (when deep) trigger a live internet scrape
    that stores fresh articles in the DB so JARVIS learns from captured data.

    Returns {articles, count, avg_sentiment, sentiment_label, scraped}."""
    result: Dict[str, Any] = {
        "articles": [], "count": 0, "avg_sentiment": 0.0,
        "sentiment_label": "neutral", "scraped": False,
    }
    try:
        from app.core.database import AsyncSessionLocal
        from app.sentiment.enhanced_service import EnhancedSentimentService

        async with AsyncSessionLocal() as db:
            articles = await EnhancedSentimentService.get_articles(
                db, symbol=base, hours=48, limit=15
            )
            # DEEP: if stored coverage is thin, scrape the live internet sources,
            # store + score them (learning), then re-query for this token.
            if deep and len(articles) < 4:
                try:
                    await asyncio.wait_for(
                        EnhancedSentimentService.run_full_cycle(db, max_age_hours=48),
                        timeout=30,
                    )
                    result["scraped"] = True
                    articles = await EnhancedSentimentService.get_articles(
                        db, symbol=base, hours=48, limit=15
                    )
                except Exception as e:
                    logger.debug(f"[JARVIS] live news scrape skipped: {e}")

            # Fallback: obscure tokens are rarely tagged by exact symbol, so do a
            # broad text search on the coin name/base so the user still gets any
            # relevant headlines they explicitly asked for.
            if not articles:
                term = (coin_name or base or "").strip()
                if term:
                    try:
                        articles = await EnhancedSentimentService.get_articles(
                            db, search=term, hours=48, limit=8
                        )
                    except Exception:
                        articles = []

        scores = [
            a.get("sentiment_score") for a in articles
            if isinstance(a.get("sentiment_score"), (int, float))
        ]
        avg = round(sum(scores) / len(scores), 3) if scores else 0.0
        label = "bullish" if avg > 0.1 else "bearish" if avg < -0.1 else "neutral"
        result.update({
            "articles": articles, "count": len(articles),
            "avg_sentiment": avg, "sentiment_label": label,
        })
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] news fetch skipped: {e}")
    return result


async def _find_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the user's open position for `symbol` (any exchange) as a compact
    dict, or None. Matches on the normalised base+quote so BTCUSDT ≡ BTC/USDT."""
    try:
        want = symbol.upper().replace("/", "").replace(":USDT", "")
        want_base = want.replace("USDT", "").replace("USDC", "")
        positions = await get_all_positions()
        for p in positions:
            have = (p.symbol or "").upper().replace("/", "")
            have_base = have.replace("USDT", "").replace("USDC", "")
            if have == want or have_base == want_base:
                return {
                    "exchange": p.exchange,
                    "symbol": p.symbol,
                    "side": p.side,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                    "pnl": p.pnl,
                    "pnl_pct": p.pnl_pct,
                    "leverage": p.leverage,
                    "liquidation_price": p.liquidation_price,
                }
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] open-position lookup skipped: {e}")
    return None


# ── Task-specific model routing ──────────────────────────────────────────────
# Each task maps to an ORDERED list of (provider_label_fragment, model_id) pairs.
# _task_chat() tries each in sequence — skipping unavailable / rate-limited
# providers — before falling back to the standard priority router.  This ensures
# Jarvis always returns an answer even when any single provider is down.
#
# Task roles & rationale:
#   REASONING-FIRST MODEL SELECTION POLICY
#   ==========================================
#   Primary = best accuracy/reasoning for the task (quality over speed)
#   Secondary = best available fallback when primary is rate-limited or down
#   Tertiary = last-resort fallback, always different provider family
#
#   GitHub Models was retired by GitHub (every endpoint answers HTTP 410
#   `github_models_retirement_brownout`), so it has been removed from every
#   chain below — leaving it in cost each task an attempt that could only fail.
#
#   Nemotron 3 Ultra 550B used to lead every reasoning task on the grounds that
#   it is the largest model NVIDIA serves free. In practice it could not answer
#   inside a normal request deadline at all, so it timed out on every call and
#   tripped the provider breaker — taking the rest of NVIDIA's catalog down with
#   it. Size is not useful if nothing ever waits long enough to read the answer.
#
#   The chains now pick on the shape of the task instead (mirroring
#   ai_router.TASK_MODEL_CHAINS): GLM-5.2 where the reasoning genuinely needs to
#   be deep and someone will wait for it, Nemotron 3.5 Lightning where the task
#   is context-hungry or latency-sensitive. Both carry a 1M window, so the old
#   128K context ceiling on this list is gone.
#
#   market_analysis – deep technical + SMC bias read (needs frontier reasoning)
#                     primary:   GLM-5.2               (753B, long-horizon reasoning)
#                     secondary: NVIDIA Nemotron 120B  (same provider, verified JSON)
#                     tertiary:  Cerebras / Groq 120B   (fast free fallbacks)
#
#   news_context    – RAG-optimised news summarisation (needs large context + speed)
#                     primary:   Nemotron 3.5 Lightning (1M ctx, fastest 30B MoE)
#                     secondary: NVIDIA Nemotron 120B  (deeper same-provider fallback)
#                     tertiary:  Cohere Command A       (256K ctx, RAG-tuned)
#                     then:      Gemini 2.5 Flash / Groq 120B
#
#   volume_analysis – quantitative buy/sell pressure (needs speed + no rate-limit cap)
#                     primary:   Nemotron 3.5 Lightning  (NVIDIA NIM, fast 30B MoE, 1M ctx)
#                     secondary: Groq gpt-oss-120B       (same model, high daily quota)
#                     tertiary:  Cerebras gpt-oss-120B   (wafer speed, only if configured)
#
#   synthesis       – final decisive JARVIS narrative (needs deepest reasoning)
#                     primary:   GLM-5.2               (long-horizon, decisive)
#                     secondary: NVIDIA Nemotron 120B / Groq 120B
#
#   news_position   – map many headlines to many open positions (context-hungry)
#                     primary:   Nemotron 3.5 Lightning (1M ctx holds any book)
#                     secondary: NVIDIA Nemotron 120B, then the 1M Gemini models
#
# NOTE on news_position and context: this maps every headline onto every open
# position, so it was specced as "1M ctx REQUIRED". The primary now genuinely
# has 1M. The Gemini fallbacks stay because a context overflow is a 400 like any
# other failure, and the chain should still land somewhere that can hold it.

_JARVIS_TASK_MODELS: Dict[str, list] = {
    "market_analysis": [
        ("nvidia",  "z-ai/glm-5.2"),                     # PRIMARY – long-horizon reasoning, 1M ctx
        ("nvidia",  "nvidia/nemotron-3-super-120b-a12b"),# same-provider fallback, verified JSON
        ("cerebras","gpt-oss-120b"),                     # wafer-speed 120B fallback
        ("groq",    "openai/gpt-oss-120b"),              # fast 120B last-resort fallback
    ],
    "news_context": [
        ("nvidia",  "nvidia/nemotron-3.5-lightning-30b-a3b"),  # PRIMARY – fast, 1M ctx
        ("nvidia",  "nvidia/nemotron-3-super-120b-a12b"),# deeper same-provider fallback
        ("cohere",  "command-a-03-2025"),                # RAG-tuned, 256K
        ("gemini",  "gemini-2.5-flash"),                 # highest quality Gemini (1M)
        ("groq",    "openai/gpt-oss-120b"),              # fast free last resort
    ],
    "volume_analysis": [
        ("nvidia",  "nvidia/nemotron-3.5-lightning-30b-a3b"),  # PRIMARY – fast 30B MoE, 1M ctx
        ("groq",    "openai/gpt-oss-120b"),              # same model, high daily quota fallback
        ("cerebras","gpt-oss-120b"),                     # wafer speed, only if configured
    ],
    "synthesis": [
        ("nvidia",  "z-ai/glm-5.2"),                     # PRIMARY – decisive long-horizon narrative
        ("nvidia",  "nvidia/nemotron-3-super-120b-a12b"),# same-provider fallback
        ("groq",    "openai/gpt-oss-120b"),              # fast free fallback
    ],
    "news_position": [
        ("nvidia",  "nvidia/nemotron-3.5-lightning-30b-a3b"),  # PRIMARY – 1M ctx holds any book
        ("nvidia",  "nvidia/nemotron-3-super-120b-a12b"),# same-provider fallback
        ("gemini",  "gemini-2.5-flash"),                 # 1M ctx – catches a prompt too big for 128K
        ("cohere",  "command-a-03-2025"),                # 256K RAG-quality fallback
        ("gemini",  "gemini-3.1-flash-lite"),            # 1M lite, 500/day quota fallback
    ],
}

# Human-readable labels for the task map (used by the Jarvis Room AI panel)
_JARVIS_TASK_LABELS: Dict[str, str] = {
    "market_analysis": "Market Analysis",
    "news_context":    "News Context",
    "volume_analysis": "Volume Analysis",
    "synthesis":       "Final Synthesis",
    "news_position":   "News → Positions",
}


async def _task_chat(
    task: str,
    db,
    messages: list,
    *,
    temperature: float = 0.35,
    max_tokens: int = 800,
    json_mode: bool = False,
    source: str = "jarvis",
    brain_context: str = "",
) -> Dict[str, Any]:
    """Route an analysis call to the best available model for `task`.

    Walks the ordered fallback chain in `_JARVIS_TASK_MODELS`, trying each
    (provider, model) in sequence.  Only the targeted provider's circuit breaker
    is touched on failure — other providers are never affected.  After exhausting
    all preferences it falls back to the standard priority router so Jarvis always
    returns an answer even when every preferred provider is rate-limited or down.

    brain_context: optional prior intelligence recalled from the brain managers
    (Mistral consolidator + Gemma indexer outputs from previous cycles).  When
    provided it is injected as an additional system message so the task model
    has memory of what prior cross-model analysis found for this symbol.
    """
    from plugins.AiMarketAnalyst.backend.services.ai_router import (
        db_chat, call_targeted_provider,
    )

    agent = f"jarvis-{task.replace('_', '-')}"
    prefs: list = _JARVIS_TASK_MODELS.get(task, [])
    primary_frag  = prefs[0][0] if prefs else ""
    primary_model = prefs[0][1] if prefs else ""

    # Inject brain-manager context if available — task model gets memory of
    # prior cross-model consolidated intelligence from Mistral/Gemma brain managers.
    enriched_messages = list(messages)
    if brain_context:
        # Append brain memory as a final system message before the user turn
        sys_insertion = {
            "role": "system",
            "content": brain_context,
        }
        # Insert just before the last user message to maximise relevance
        last_user_idx = next(
            (i for i in range(len(enriched_messages) - 1, -1, -1)
             if enriched_messages[i].get("role") == "user"),
            None,
        )
        if last_user_idx is not None:
            enriched_messages.insert(last_user_idx, sys_insertion)
        else:
            enriched_messages.append(sys_insertion)

    for idx, (pref_label, pref_model) in enumerate(prefs):
        resp = await call_targeted_provider(
            db,
            provider_label_fragment=pref_label,
            model=pref_model,
            messages=enriched_messages,   # brain context already injected
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            agent_name=agent,
            source=source,
        )
        if resp.get("ok") and resp.get("content"):
            routed = resp.get('routed_via') or pref_model
            logger.debug(
                f"[JARVIS task-chat] {task}: {resp.get('provider')} / {routed}"
                + (" (brain-enriched)" if brain_context else "")
            )
            # Persist fallback activation to all brains so JARVIS memory tracks it
            if idx > 0:
                jarvis_learn_all_brains(
                    action="ai_task_fallback",
                    summary=(
                        f"Task '{_JARVIS_TASK_LABELS.get(task, task)}' routed to fallback "
                        f"[{pref_label}] {pref_model} (primary [{primary_frag}] {primary_model} unavailable)"
                    ),
                    detail=(
                        f"Fallback chain position: {idx + 1}/{len(prefs)}. "
                        f"Responding model: {routed}. "
                        f"This is a quality-aware fallback — primary model was rate-limited or down."
                    ),
                    tags=["jarvis", "ai-routing", "fallback", task, pref_label],
                    importance=0.5,
                )
            return resp
        logger.debug(
            f"[JARVIS task-chat] {task}: '{pref_label}/{pref_model}' "
            f"unavailable ({resp.get('error', 'no content')}); trying next"
        )

    # All chain preferences exhausted — standard priority routing as final fallback
    logger.debug(f"[JARVIS task-chat] {task}: all chain preferences failed; using standard routing")
    jarvis_learn_all_brains(
        action="ai_task_exhausted",
        summary=f"Task '{_JARVIS_TASK_LABELS.get(task, task)}' exhausted all preferred models, using standard router",
        detail=f"All {len(prefs)} preferred models in the chain were unavailable. Falling back to standard priority routing.",
        tags=["jarvis", "ai-routing", "exhausted", task],
        importance=0.45,
    )
    return await db_chat(
        db,
        enriched_messages,             # keep brain context even in fallback
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        agent_name=agent,
        source=source,
        # JARVIS can be given a profile of its own. Unset, this is the same
        # standard priority routing as before.
        task="jarvis_chat",
    )


async def _compose_ai_narrative(brief: str, symbol: str = "") -> Optional[str]:
    """Turn the raw research brief into a natural, human, decisive analysis.

    Routes to the market_analysis task model (GitHub o3 / NVIDIA Nemotron) first
    for deep technical interpretation; falls back to synthesis (Groq 120B MoE),
    then to the standard priority router when both are unavailable.

    Brain-enriched: recalls prior Mistral/Gemma brain consolidations for `symbol`
    and injects them as additional context so task models have memory of what
    previous cross-model analysis found.

    After producing the narrative, fires Mistral + Gemma brain managers to
    consolidate and index the new findings — models talk to each other here.
    Returns None if no AI provider responds so the caller can fall back to template.
    """
    try:
        from app.core.database import AsyncSessionLocal

        # ── Recall prior brain intelligence for this symbol ──────────────────
        # Brain managers (Mistral + Gemma) may have written consolidated entries
        # from prior analyses — pull them to enrich the task model prompts.
        brain_ctx = ""
        if symbol:
            try:
                brain_ctx = await _brain_recall_context(symbol, max_chars=480)
            except Exception:
                brain_ctx = ""

        msgs = [
            {
                "role": "system",
                "content": (
                    "You are JARVIS, an elite crypto trading analyst speaking to your "
                    "principal (address him as 'Sir'). Write a thorough, confident, human "
                    "analysis — never robotic or list-only. Weave the technicals, volume "
                    "flow, the Sox ML forecast and the news/sentiment into one coherent, "
                    "detailed read of the pair. Cover: (1) trend direction and key levels, "
                    "(2) what the volume and order-flow tell you, (3) the Sox forecast "
                    "alignment with price action, (4) news/sentiment impact, (5) a clear "
                    "directional bias with specific entry zone, stop-loss and take-profit "
                    "levels derived from the data you were given. "
                    "Quote entry, stop-loss and take-profit levels ONLY as they appear in the "
                    "brief. If the brief says there is no clean setup, say so plainly and "
                    "explain what you are waiting for — never substitute levels of your own. "
                    "Where the brief gives a confidence figure, reflect it honestly: a "
                    "low-confidence read must be described as marginal, not as a conviction call. "
                    "If the brief flags insufficient history for an indicator, do not draw "
                    "trend conclusions from it. "
                    "If the brief says the user ALREADY HOLDS AN OPEN POSITION on this pair, "
                    "add a dedicated 'Position:' section giving a direct recommendation — "
                    "hold, add, reduce, close, or move the stop / take-profit — "
                    "and say why, referencing his live PnL and liquidation price. "
                    "Be specific with every number you were given. Do NOT invent data you "
                    "were not given. Write at least 200 words and never truncate mid-sentence."
                ),
            },
            {"role": "user", "content": brief},
        ]

        async with AsyncSessionLocal() as db:
            # Task models receive brain context from prior Mistral/Gemma consolidations
            resp = await _task_chat(
                "market_analysis", db, msgs,
                temperature=0.4, max_tokens=1800,
                brain_context=brain_ctx,
            )
            if not (resp.get("ok") and resp.get("content")):
                resp = await _task_chat(
                    "synthesis", db, msgs,
                    temperature=0.4, max_tokens=1800,
                    brain_context=brain_ctx,
                )
            if resp.get("ok") and resp.get("content"):
                narrative = str(resp["content"]).strip()
                # ── Fire brain managers async (models talk to each other) ────
                # Mistral consolidates + indexes the fresh narrative.
                # ALSO triggers the news collector so brain stays current.
                if symbol and narrative:
                    _fire_brain_managers(
                        {"market_analysis": brief, "synthesis": narrative},
                        symbol,
                    )
                    # News collection: idle Mistral organises latest market news
                    # into brain briefings for enriching future analyses
                    _fire_brain_news_collection([symbol] if symbol else None)
                return narrative
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] AI narrative skipped: {e}")
    return None


async def _analysis_from_series(
    symbol: str, ohlcv: List[List], ticker: Dict, *,
    deep: bool = False, timeframe: str = "4h",
) -> CommandResult:
    """Turn a candle series into a trade proposal.

    Split out of ``_analyze_symbol`` so every non-crypto route — the forex/metals
    providers and the universal Yahoo fallback — produces an identical proposal
    from whichever source could serve the instrument, instead of each route
    carrying its own copy of the indicator maths.

    NEVER places an order: the result carries the exact command the user must
    say to execute it.
    """
    closes  = [float(c[4]) for c in ohlcv]
    highs   = [float(c[2]) for c in ohlcv]
    lows    = [float(c[3]) for c in ohlcv]
    current = closes[-1]
    buy_pct  = ticker.get("buy_pct", 0)
    sell_pct = ticker.get("sell_pct", 0)
    buy_vol  = ticker.get("buy_volume", 0)
    sell_vol = ticker.get("sell_volume", 0)
    # Report where the number actually came from. This used to be hardcoded to
    # "yahoo_finance_live" regardless of whether Yahoo, CoinGecko or Frankfurter
    # served it, which made the provenance line actively misleading.
    price_source = ticker.get("source") or "unknown"

    ema50  = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi    = _rsi(closes, 14)
    atr    = _atr(highs, lows, closes, 14)
    # _ema falls back to the last close when the history is too short, which
    # makes EMA200 identical to price and silently neuters every trend test.
    # Track that so the read is qualified rather than quietly wrong.
    ema200_valid = len(closes) >= 200

    swing_high = max(highs[-20:])
    swing_low  = min(lows[-20:])

    if not ema200_valid:
        trend = "ranging"
    elif current > ema200 and ema50 > ema200:
        trend = "uptrend"
    elif current < ema200 and ema50 < ema200:
        trend = "downtrend"
    else:
        trend = "ranging"

    rsi_label = "overbought" if rsi > 70 else "oversold" if rsi < 30 else f"neutral ({rsi:.0f})"

    # The spoken read of the same numbers, so every surface that shows an
    # analysis — Telegram, Paul chat, voice — leads with the same words rather
    # than each one paraphrasing the raw indicator dump differently.
    try:
        from app.signals.narrative import narrative_summary

        narrative = narrative_summary(
            ohlcv, symbol=symbol, timeframe=timeframe, trend=trend,
            swing_high=swing_high, swing_low=swing_low,
        )
    except Exception as _nexc:  # noqa: BLE001 — prose is never worth losing the analysis
        logger.debug("[JARVIS] narrative skipped for {}: {}", symbol, _nexc)
        narrative = ""

    bias, confidence, reasons = _directional_bias(
        current, ema50, ema200 if ema200_valid else 0.0, rsi, trend, buy_pct, sell_pct,
    )

    setup = _build_setup(bias, current, swing_high, swing_low, atr)
    side, side_label = ("long", "BUY") if bias == "long" else ("short", "SHORT")

    volume_line = (
        f"Live Volume Split: BUY {buy_pct:.0f}% ({buy_vol:,.0f}) / "
        f"SELL {sell_pct:.0f}% ({sell_vol:,.0f})\n"
    )
    header = (
        (f"{narrative}\n\n" if narrative else "")
        + f"{symbol} | {trend.upper()} | RSI {rsi:.0f} ({rsi_label})\n"
        f"EMA50={ema50:.4g}  EMA200={ema200:.4g}{'' if ema200_valid else ' (insufficient history)'}"
        f"  Current={current:.4g}\n"
        f"Swing Hi={swing_high:.4g}  Swing Lo={swing_low:.4g}  ATR14={atr:.4g}\n"
        f"{volume_line}"
        f"Bias: {side_label} · confidence {confidence:.0%}"
        + (f" · {'; '.join(reasons)}" if reasons else "")
        + "\n"
    )

    if not setup:
        # No setup with the stop and both targets on the correct side of
        # entry — say so instead of inventing one with a fabricated R:R.
        msg = (
            f"{header}\nNo clean {side_label} setup right now, Sir — the recent range is too "
            f"tight relative to volatility for a stop and targets that make sense. "
            f"I'd wait for a clearer structure."
        )
        return CommandResult(
            ok=True, action="analyze", detail=msg,
            speech=(
                f"{symbol}: {trend}, RSI {rsi:.0f}. I have a {side_label.lower()} lean at "
                f"{confidence:.0%} confidence, but no clean setup — the range is too tight "
                f"for a sensible stop, Sir."
            ),
            order={
                "symbol": symbol, "side": side, "setup": None,
                "current_price": current, "rsi": rsi, "trend": trend,
                "confidence": confidence,
                "price_source": price_source,
            },
        )

    entry, sl, tp1, tp2 = setup["entry"], setup["sl"], setup["tp1"], setup["tp2"]
    rr1, rr2 = setup["rr1"], setup["rr2"]

    confirm_cmd = (
        f"execute {symbol} {side} 5 lot at {entry}; "
        f"set SL {sl}; TP1 {tp1}; TP2 {tp2}"
    )

    # Macro context — the dollar/VIX weather this instrument trades in. Read
    # here so /analyze, the analyze_symbol tool and Jarvis chat all state it,
    # and so the journal can later learn whether it predicted anything.
    macro_bias = None
    try:
        from app.services.macro_context import resolve_macro_bias

        macro_bias = await resolve_macro_bias(symbol)
    except Exception as _mexc:  # noqa: BLE001 — context is never a gate
        logger.debug("[JARVIS] macro context skipped for {}: {}", symbol, _mexc)

    macro_applies = bool(macro_bias is not None and getattr(macro_bias, "applicable", False))
    macro_norm = float(getattr(macro_bias, "normalized", 0.0) or 0.0) if macro_applies else 0.0
    # Signed for the side actually proposed, so a short with a bid dollar reads
    # as aligned rather than opposed.
    macro_aligned = macro_norm if side == "long" else -macro_norm
    macro_reason = getattr(macro_bias, "reason", "") if macro_bias is not None else "not consulted"

    # Journal the call so the learning loop can settle it against real candles
    # later and hold this confidence figure to account. Best-effort by design —
    # a journalling failure must never cost the user their analysis.
    try:
        from app.core.database import AsyncSessionLocal
        from app.services import analysis_journal

        async with AsyncSessionLocal() as _jdb:
            await analysis_journal.record_proposal(
                _jdb,
                source="jarvis_command",
                symbol=symbol,
                asset_class=market_data.classify(symbol),
                timeframe="4h",
                side=side,
                entry=entry, stop_loss=sl, take_profit=tp1, tp2=tp2,
                rr1=rr1, confidence=confidence,
                price_at_analysis=current, price_source=price_source,
                features={
                    "trend": trend, "rsi": round(rsi, 2), "atr": setup["atr"],
                    "ema50": round(ema50, 6), "ema200": round(ema200, 6),
                    "ema200_valid": ema200_valid,
                    "buy_pct": buy_pct, "sell_pct": sell_pct,
                    # Macro, recorded whether or not it applied. Settled rows
                    # then answer the question the fixed 0.05 weight only
                    # guesses at: did the dollar read actually predict anything?
                    "macro_applied": macro_applies,
                    "macro_aligned": round(macro_aligned, 4),
                    "macro_regime": getattr(macro_bias, "regime", "UNKNOWN") if macro_bias else "UNKNOWN",
                    "macro_usd_leg": getattr(macro_bias, "usd_leg", "none") if macro_bias else "none",
                },
            )
    except Exception as _jexc:  # noqa: BLE001
        logger.debug("[JARVIS] journal skipped for {}: {}", symbol, _jexc)

    if macro_applies:
        stance = (
            "supports" if macro_aligned > 0.1
            else "opposes" if macro_aligned < -0.1
            else "is neutral for"
        )
        macro_line = f"\nMACRO: {macro_reason} — {stance} this {side_label.lower()}.\n"
    else:
        macro_line = f"\nMACRO: not applied ({macro_reason}).\n"

    detail = (
        f"{header}"
        f"\nPROPOSED {side_label} SETUP (LIVE DATA via {price_source} — NOT EXECUTED)\n"
        f"Entry : {entry}  |  SL : {sl}  |  TP1 : {tp1} (R:R {rr1}x)  |  TP2 : {tp2} (R:R {rr2}x)\n"
        f"{macro_line}"
        f"\nTo execute say:\n  \"{confirm_cmd}\""
    )

    speech = (
        f"{symbol} live analysis: {trend}, RSI {rsi:.0f}, {rsi_label}. "
        f"Current price {current}. Buy pressure {buy_pct:.0f}%, sell pressure {sell_pct:.0f}%. "
        f"Proposed {side_label.lower()} entry at {entry}, SL {sl}, TP1 {tp1}, "
        f"at {confidence:.0%} confidence. "
        f"This is a proposal — say the execute command to confirm, Sir."
    )

    return CommandResult(
        ok=True, action="analyze",
        detail=detail,
        speech=speech,
        order={
            "symbol": symbol, "side": side, "proposed_entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "rr1": rr1, "rr2": rr2,
            "current_price": current,
            "rsi": rsi, "trend": trend,
            "atr": setup["atr"],
            "confidence": confidence,
            "bias_reasons": reasons,
            "ema50": round(ema50, 6), "ema200": round(ema200, 6),
            "ema200_valid": ema200_valid,
            "buy_volume_pct": buy_pct, "sell_volume_pct": sell_pct,
            "buy_volume": buy_vol, "sell_volume": sell_vol,
            "price_source": price_source,
            "macro_applied": macro_applies,
            "macro_aligned": round(macro_aligned, 4),
            "macro_reason": macro_reason,
            "confirm_command": confirm_cmd,
            "WARNING": "NOT EXECUTED — say the confirm_command to place the order",
        },
    )


async def _analyze_symbol(symbol: str, original_cmd: str, ex_name: Optional[str], deep: bool = False) -> CommandResult:
    """
    Real-data market analysis for `symbol`.

    Non-crypto (XAUUSD, GBPUSD, US30, USOIL …) is served by the universal
    market-data resolver: Yahoo covers every FX cross, metal, index and
    commodity, with the forex/metals providers behind it.  Crypto goes to
    Bitget via the pair catalog.

    IMPORTANT: This function NEVER places orders.  It returns a proposal
    with the exact Jarvis command the user must say to execute it.
    """
    # ── Route: FX, metals, indices, energy, softs ────────────────────────────
    # Two-tier guard: the forex provider knows a few majors plus gold, Yahoo
    # knows everything else.  Checking only the former is what previously sent
    # GBPUSD and XAUUSD down the crypto path to die on a Bitget lookup.
    if market_data.is_universal_symbol(symbol):
        try:
            ohlcv, ticker = await market_data.fetch_ohlcv_universal(
                symbol, timeframe="4h", limit=200
            )
        except Exception as e:
            if _is_network_error(e):
                return _err("analyze", "Network unreachable — cannot fetch live price.")
            return _err("analyze", f"Price fetch failed: {e}")

        if not ohlcv or len(ohlcv) < 20:
            return _err("analyze", f"Not enough historical data for {symbol}.")
        return await _analysis_from_series(symbol, ohlcv, ticker, deep=deep)

    # ── Route: Crypto symbols via Bitget ─────────────────────────────────────
    # Resolve the token → canonical Bitget pair + REAL coin name so JARVIS can
    # talk about "Bitcoin" (not "BTCUSDT") and never surfaces a raw ccxt
    # "does not have market" error for a token that simply needs resolving.
    coin_name: Optional[str] = None
    _input_token = symbol  # the raw token before canonicalisation
    try:
        from app.services import pair_catalog
        resolved_pair, suggestion = await pair_catalog.resolve_with_suggestion(symbol)
        if resolved_pair is None:
            token = symbol.replace("USDT", "").replace("USDC", "") or symbol
            if suggestion:
                msg = f"I couldn't find a Bitget pair for {token}. Did you mean {suggestion}?"
                return CommandResult(ok=False, action="analyze", detail=msg, speech=msg)

            # Last resort before giving up: the catalog only knows Bitget's USDT
            # swaps, so anything listed elsewhere — an index, a commodity, an FX
            # cross the guard above didn't recognise — reaches here still
            # perfectly priceable. Try the universal resolver on the bare token
            # rather than declaring the instrument unsupported.
            _bare = market_data.normalize_symbol(token)
            try:
                _ohlcv, _ticker = await market_data.fetch_ohlcv_universal(
                    _bare, timeframe="4h", limit=200
                )
            except Exception:  # noqa: BLE001 — a failed rescue is just no rescue
                _ohlcv, _ticker = [], {}
            if len(_ohlcv) >= 20:
                logger.info("[JARVIS] {} rescued from the Bitget dead-end", _bare)
                return await _analysis_from_series(_bare, _ohlcv, _ticker, deep=deep)

            msg = f"I couldn't find a Bitget-tradeable pair for {token}, Sir."
            return CommandResult(ok=False, action="analyze", detail=msg, speech=msg)
        coin_name = resolved_pair.name or resolved_pair.base
        # Canonical glued form for downstream ccxt normalisation (e.g. BTCUSDT).
        symbol = f"{resolved_pair.base}{resolved_pair.quote}"
        # Learn the user's bare token as an alias so it resolves instantly next
        # time (learn_alias no-ops when it equals the symbol/base/name).
        try:
            await pair_catalog.learn_alias(pair_catalog._strip_quote(_input_token), resolved_pair.symbol)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[JARVIS] pair resolution skipped: {e}")

    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    connector = exchange_manager.get_exchange(ex_list[0]) if ex_list else None
    if not connector:
        return _err("analyze", "No exchange configured for analysis.")

    # Normalise: SOLUSDT → SOL/USDT  (ccxt format)
    base   = symbol.replace("USDT", "").replace("USDC", "")
    ccxt_sym = f"{base}/USDT"
    # Spoken/display label: the real coin name when known, else the symbol.
    display_name = coin_name or symbol

    # ── Fetch 4H OHLCV (200 candles) ─────────────────────────────────────────
    try:
        ohlcv = await connector.exchange.fetch_ohlcv(
            f"{ccxt_sym}:USDT", timeframe="4h", limit=200
        )
    except Exception:
        try:
            ohlcv = await connector.exchange.fetch_ohlcv(
                ccxt_sym, timeframe="4h", limit=200
            )
        except BaseException as e:
            if _is_network_error(e):
                return _err("analyze", "Exchange unreachable — check network.")
            return _err("analyze", _friendly_exchange_error(e))

    if not ohlcv or len(ohlcv) < 20:
        return _err("analyze", f"Not enough data for {symbol}.")

    closes  = [float(c[4]) for c in ohlcv]
    highs   = [float(c[2]) for c in ohlcv]
    lows    = [float(c[3]) for c in ohlcv]
    current = closes[-1]

    # ── Technical indicators ──────────────────────────────────────────────────
    ema50  = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi    = _rsi(closes, 14)
    atr    = _atr(highs, lows, closes, 14)
    # _ema returns the last close when the history is too short, which makes
    # EMA200 equal to price and silently defeats every trend comparison below.
    ema200_valid = len(closes) >= 200

    # Recent swing high / low (last 20 candles)
    swing_high = max(highs[-20:])
    swing_low  = min(lows[-20:])

    # Trend determination
    if not ema200_valid:
        trend = "ranging"
    elif current > ema200 and ema50 > ema200:
        trend = "uptrend"
    elif current < ema200 and ema50 < ema200:
        trend = "downtrend"
    else:
        trend = "ranging"

    rsi_label = "overbought" if rsi > 70 else "oversold" if rsi < 30 else f"neutral ({rsi:.0f})"

    # Weighted confluence — see _directional_bias. Spot crypto has no venue
    # buy/sell split here, so order flow simply does not contribute.
    bias, confidence, reasons = _directional_bias(
        current, ema50, ema200 if ema200_valid else 0.0, rsi, trend,
    )
    side, side_label = ("long", "BUY") if bias == "long" else ("short", "SHORT")

    setup = _build_setup(bias, current, swing_high, swing_low, atr)

    header = (
        f"{display_name} ({symbol}) | {trend.upper()} | RSI {rsi:.0f} ({rsi_label})\n"
        f"EMA50={ema50:.4g}  EMA200={ema200:.4g}{'' if ema200_valid else ' (insufficient history)'}"
        f"  Current={current:.4g}\n"
        f"Swing Hi={swing_high:.4g}  Swing Lo={swing_low:.4g}  ATR14={atr:.4g}\n"
        f"Bias: {side_label} · confidence {confidence:.0%}"
        + (f" · {'; '.join(reasons)}" if reasons else "")
        + "\n"
    )

    if not setup:
        # Better to say there is nothing here than to publish a setup whose
        # stop or targets sit on the wrong side of the entry.
        msg = (
            f"{header}\nNo clean {side_label} setup right now, Sir — the recent range is too "
            f"tight relative to volatility for a stop and targets that make sense. "
            f"I'd wait for a clearer structure."
        )
        return CommandResult(
            ok=True, action="analyze", detail=msg,
            speech=(
                f"{display_name}: {trend}, RSI {rsi:.0f}. I have a {side_label.lower()} lean at "
                f"{confidence:.0%} confidence, but no clean setup — the range is too tight "
                f"for a sensible stop, Sir."
            ),
            order={
                "symbol": symbol, "side": side, "setup": None,
                "current_price": current, "rsi": rsi, "trend": trend,
                "confidence": confidence,
            },
        )

    entry, sl, tp1, tp2 = setup["entry"], setup["sl"], setup["tp1"], setup["tp2"]
    rr1, rr2 = setup["rr1"], setup["rr2"]

    # ── Build response ────────────────────────────────────────────────────────
    confirm_cmd = (
        f"execute {symbol} {side} 5 lot at {entry}; "
        f"set SL {sl}; TP1 {tp1}; TP2 {tp2}"
    )

    detail = (
        f"{header}"
        f"\nPROPOSED {side_label} SETUP (REAL DATA — NOT EXECUTED)\n"
        f"Entry : {entry}  |  SL : {sl}  |  TP1 : {tp1} (R:R {rr1}x)  |  TP2 : {tp2} (R:R {rr2}x)\n"
        f"\nTo execute say:\n  \"{confirm_cmd}\""
    )

    speech = (
        f"{display_name} analysis: {trend}, RSI {rsi:.0f}, {rsi_label}. "
        f"Proposed {side_label.lower()} entry at {entry}, SL {sl}, "
        f"TP1 {tp1}, at {confidence:.0%} confidence. "
        f"This is a proposal — say the execute command to confirm, Sir."
    )

    # ── Kronos ML forecast (optional plugin — graceful, never breaks) ────────
    kronos_info = None
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service as _kronos
        _ex_id = ex_list[0].value if ex_list else "bitget"
        _fc = await _kronos.run_forecast_cached(_ex_id, ccxt_sym, "4h", pred_len=12)
        if _fc and _fc.signal:
            s = _fc.signal
            kronos_info = {
                "direction": s.direction, "pct_change": s.pct_change,
                "confidence": s.confidence, "target_price": s.target_price,
                "engine": _fc.engine,
            }
            detail += (
                f"\n\nSOX ML FORECAST ({_fc.engine}) — next 12×4h\n"
                f"Direction: {s.direction.upper()}  |  {s.pct_change:+.2f}%  "
                f"|  Target {s.target_price:.4g}  |  {int(s.confidence*100)}% confidence"
            )
            speech += (
                f" Sox forecasts {s.direction} {s.pct_change:+.1f} percent "
                f"over the next two days at {int(s.confidence*100)} percent confidence."
            )
    except Exception as _ke:
        logger.debug(f"[JARVIS] Kronos forecast skipped: {_ke}")

    # ── Volume flow + News/sentiment + AI narrative (deep research) ──────────
    # Always add volume + stored news; only trigger a fresh internet scrape when
    # the user explicitly asked for news/research (keyword-gated for speed).
    volume_info = await _crypto_volume_analysis(connector, ccxt_sym, ohlcv)
    news_info = await _fetch_pair_news(base, coin_name, deep)
    position_info = await _find_open_position(symbol)

    if volume_info:
        detail += (
            f"\n\nVOLUME FLOW — buy {volume_info['buy_pressure_pct']:.0f}% / "
            f"sell {volume_info['sell_pressure_pct']:.0f}%"
            f"  (last candle {volume_info['volume_spike_x']:.1f}× the 20-bar average"
            + (f", 24h vol {volume_info['quote_volume_24h']:,.0f}"
               if isinstance(volume_info.get('quote_volume_24h'), (int, float)) else "")
            + ")"
        )
        speech += (
            f" Volume is {volume_info['buy_pressure_pct']:.0f} percent buy-side."
        )

    news_lines: List[str] = []
    for a in (news_info.get("articles") or [])[:4]:
        sc = a.get("sentiment_score")
        lbl = a.get("sentiment_label") or (
            "BULLISH" if (sc or 0) > 0.1 else "BEARISH" if (sc or 0) < -0.1 else "NEUTRAL"
        )
        src = a.get("source") or ""
        title = (a.get("title") or "")[:130]
        if title:
            news_lines.append(f"[{str(lbl).upper()}] {title}" + (f" — {src}" if src else ""))
    if news_lines:
        detail += (
            f"\n\nNEWS & SENTIMENT ({news_info['count']} headlines, "
            f"{news_info['sentiment_label'].upper()})\n" + "\n".join(f"• {l}" for l in news_lines)
        )
    elif deep:
        detail += "\n\nNEWS & SENTIMENT — no fresh headlines found for this pair."

    # ── Your open position on this pair (if any) ────────────────────────────
    pos_brief = ""
    if position_info:
        _pdir = str(position_info.get("side", "")).upper()
        _pnl = position_info.get("pnl") or 0
        _pnl_pct = position_info.get("pnl_pct") or 0
        _arrow = "▲" if _pnl >= 0 else "▼"
        detail += (
            f"\n\nYOUR OPEN POSITION — {_pdir} {position_info.get('size')} @ "
            f"{position_info.get('entry_price')} (mark {position_info.get('mark_price')})\n"
            f"PnL {_arrow} {abs(_pnl):.2f} USDT ({_pnl_pct:+.2f}%)"
            + (f"  ·  liq {position_info.get('liquidation_price')}"
               if position_info.get("liquidation_price") else "")
        )
        pos_brief = (
            f"\nUSER ALREADY HOLDS AN OPEN POSITION on this pair: {_pdir} size "
            f"{position_info.get('size')} entered at {position_info.get('entry_price')}, "
            f"mark {position_info.get('mark_price')}, live PnL {_pnl:+.2f} USDT ({_pnl_pct:+.2f}%)"
            + (f", leverage {position_info.get('leverage')}x" if position_info.get("leverage") else "")
            + (f", liquidation {position_info.get('liquidation_price')}"
               if position_info.get("liquidation_price") else "")
            + ". Advise specifically what to do with THIS position."
        )

    # ── AI-composed human narrative (the natural JARVIS voice) ──────────────
    _kronos_line = ""
    if kronos_info:
        _kronos_line = (
            f"Sox ML forecast: {kronos_info['direction']} "
            f"{kronos_info['pct_change']:+.2f}% (target {kronos_info['target_price']:.6g}, "
            f"{int(kronos_info['confidence'] * 100)}% confidence)."
        )
    brief = (
        f"Pair: {display_name} ({symbol}) on {ex_list[0].value if ex_list else 'bitget'}, 4h chart.\n"
        f"Price {current:.6g}. Trend {trend}. RSI {rsi:.0f} ({rsi_label}). "
        f"EMA50 {ema50:.6g}, EMA200 {ema200:.6g}. "
        f"Swing high {swing_high:.6g}, swing low {swing_low:.6g}.\n"
        + (f"Volume: buy {volume_info['buy_pressure_pct']:.0f}% / sell {volume_info['sell_pressure_pct']:.0f}%, "
           f"last candle {volume_info['volume_spike_x']:.1f}x avg.\n" if volume_info else "")
        + (_kronos_line + "\n" if _kronos_line else "")
        + (f"News sentiment: {news_info['sentiment_label']} across {news_info['count']} recent headlines.\n"
           if news_info['count'] else "News: no fresh headlines for this pair.\n")
        + (("Headlines:\n" + "\n".join(f"- {l}" for l in news_lines) + "\n") if news_lines else "")
        + f"My proposed setup: {side_label} — entry {entry}, SL {sl}, TP1 {tp1} (R:R {rr1}x), "
        f"TP2 {tp2} (R:R {rr2}x)."
        + pos_brief
    )
    narrative = await _compose_ai_narrative(brief, symbol=symbol)

    if narrative:
        levels_block = (
            f"PROPOSED {side_label} SETUP (real data — NOT executed)\n"
            f"Entry {entry}  |  SL {sl}  |  TP1 {tp1} (R:R {rr1}x)  |  TP2 {tp2} (R:R {rr2}x)\n"
            f"To execute say:  \"{confirm_cmd}\""
        )
        detail = f"{narrative}\n\n{levels_block}"
        speech = narrative[:520].replace("\n", " ")

    # ── Learn: persist this research + narrative to all three brains ────────
    try:
        jarvis_learn_all_brains(
            action="deep_analysis" if deep else "analysis",
            symbol=symbol,
            summary=(narrative or speech or "")[:200],
            detail=detail[:1200],
            tags=["jarvis", "analysis", base, trend],
            importance=0.6 if deep else 0.45,
        )
    except Exception:
        pass

    # ── Brain network: fire idle managers to cross-pollinate learnings ───────
    # If _compose_ai_narrative did NOT already fire brain managers (e.g. no AI
    # response), fire them now with whatever data we have so Mistral and Gemma
    # still learn from the technical brief + volume/news data.
    if not narrative:
        _fire_brain_managers(
            {
                "market_analysis": brief[:800] if brief else "",
                "volume_analysis": (
                    f"Volume: buy {volume_info['buy_pressure_pct']:.0f}% / "
                    f"sell {volume_info['sell_pressure_pct']:.0f}%"
                    if volume_info else ""
                ),
                "news_context": (
                    f"News sentiment: {news_info['sentiment_label']} "
                    f"({news_info['count']} headlines)"
                    if news_info else ""
                ),
            },
            symbol,
        )

    return CommandResult(
        ok=True, action="analyze",
        detail=detail,
        speech=speech,
        order={
            "symbol": symbol, "side": side, "proposed_entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "rsi": rsi, "trend": trend, "ema50": round(ema50, 6),
            "ema200": round(ema200, 6), "confirm_command": confirm_cmd,
            "kronos": kronos_info,
            "volume": volume_info,
            "news": [
                {
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "url": a.get("url"),
                    "sentiment_score": a.get("sentiment_score"),
                    "sentiment_label": a.get("sentiment_label"),
                }
                for a in (news_info.get("articles") or [])[:6]
            ],
            "news_count": news_info.get("count", 0),
            "sentiment_label": news_info.get("sentiment_label"),
            "sentiment_score": news_info.get("avg_sentiment"),
            "position": position_info,
            "narrative": narrative,
            "deep": deep,
            "WARNING": "NOT EXECUTED — say the confirm_command to place the order",
        },
    )


def _price_dp(price: float) -> int:
    """Return appropriate decimal places for a price (e.g. 73.8 → 3, 0.00023 → 7)."""
    if price == 0:
        return 4
    mag = math.floor(math.log10(abs(price)))
    if mag >= 3:
        return 1
    if mag >= 1:
        return 3
    if mag >= -1:
        return 4
    return max(4, 2 - mag)


def _is_network_error(e: BaseException) -> bool:
    """Return True for DNS / socket / network errors so we can show a friendly message."""
    msg = str(e).lower()
    network_signals = (
        "nodename nor servname",   # macOS/Linux DNS failure
        "name or service not known",
        "getaddrinfo failed",
        "errno 8",
        "errno 11001",             # Windows DNS
        "connection refused",
        "timed out",
        "network error",
        "cannot connect",
        "ssl:",
    )
    return any(s in msg for s in network_signals)


async def _find_position(symbol: str, ex_name: Optional[str]):
    """Find a position and its connector by normalised symbol.

    Returns (connector, raw_position_dict) or (None, None) if not found.
    Never raises — network/exchange errors are swallowed with a warning.
    """
    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    for ex_enum in ex_list:
        connector = exchange_manager.get_exchange(ex_enum)
        if not connector:
            continue
        try:
            raw_list = await connector.exchange.fetch_positions()
            for p in raw_list:
                if _safe_float(p.get("contracts")) <= 0:
                    continue
                if _match_symbol(symbol, p.get("symbol", "")):
                    return connector, p
        except BaseException as e:   # BaseException catches OSError, asyncio errors, etc.
            if _is_network_error(e):
                logger.warning(
                    f"[JARVIS] {ex_enum.value} unreachable (DNS/network): {e}"
                )
            else:
                logger.warning(f"[JARVIS] _find_position({ex_enum.value}): {e}")
    return None, None


def _friendly_exchange_error(e: BaseException) -> str:
    """Parse a raw exchange error and return a human-readable string."""
    raw = str(e)
    try:
        import json as _json
        # ccxt wraps responses as "bitget {'code':'...','msg':'...'}"
        # strip leading exchange name if present
        json_start = raw.find("{")
        if json_start != -1:
            d = _json.loads(raw[json_start:])
            msg = d.get("msg") or d.get("message") or d.get("error")
            if msg:
                return str(msg)
    except Exception:
        pass
    return raw


def _round_price(price: float, decimals: int = 5) -> str:
    """Round a price to `decimals` places and return as string.

    Defaults to 5 dp (Bitget USDT-FUTURES standard for sub-$1 contracts).
    """
    rounded = round(price, decimals)
    fmt = f"{{:.{decimals}f}}".format(rounded).rstrip("0").rstrip(".")
    return fmt or "0"


def _fmt_size(size: float) -> str:
    """Format contract size for Bitget: '211' not '211.0'."""
    i = int(size)
    return str(i) if float(i) == size else str(size)


def _bitget_margin_mode(raw: Optional[str]) -> str:
    """Normalise margin mode for Bitget native API."""
    if not raw:
        return "crossed"
    return "isolated" if str(raw).lower() == "isolated" else "crossed"

def _bitget_sym(raw_sym: str) -> str:
    """'GWEI/USDT:USDT' → 'GWEIUSDT'  (Bitget native API format)."""
    return raw_sym.split(":")[0].replace("/", "")


async def _set_tp_pct(symbol: str, pct: float, ex_name: Optional[str]) -> CommandResult:
    """Set take-profit for a percentage ROI.

    For LONG:  TP = entry × (1 + pct/100)  — price goes up
    For SHORT: TP = entry × (1 - pct/100÷leverage) — price goes down
               Uses position leverage; capped so tp_price > 0.
    """
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("set_tp", f"No open position found for {symbol}")

    entry    = _safe_float(pos.get("entryPrice"))
    mark     = _safe_float(pos.get("markPrice")) or entry   # use mark for trigger distance check
    side     = str(pos.get("side") or "long").lower()
    leverage = _safe_float(pos.get("leverage")) or 10.0

    if entry <= 0:
        return _err("set_tp", f"Cannot determine entry price for {symbol}")

    if side == "short":
        # For SHORT: profitable when price drops. TP must be BELOW mark.
        # pct is interpreted as target ROI on margin → price_drop = pct/leverage.
        price_drop_pct = pct / leverage
        tp_price = round(mark * (1 - price_drop_pct / 100), 8)
        if price_drop_pct >= 100 or tp_price <= 0:
            msg = (
                f"A {pct:.0f}% ROI on a {leverage:.0f}x short would require the "
                f"price to drop {price_drop_pct:.0f}% — not achievable. "
                f"Try something under {leverage * 90:.0f}%."
            )
            return CommandResult(ok=False, action="set_tp", detail=msg, speech=msg)
    else:
        price_rise_pct = pct / leverage
        tp_price = round(mark * (1 + price_rise_pct / 100), 8)

    return await _set_tp_price(symbol, tp_price, ex_name, _connector=connector, _pos=pos)


async def _set_tp_price(
    symbol: str,
    price: float,
    ex_name: Optional[str],
    *,
    _connector=None,
    _pos=None,
) -> CommandResult:
    """Place a take-profit TPSL order at an absolute price.

    Uses connector.place_tpsl_order (mark_price, no extraneous side param).
    """
    if _connector is None:
        _connector, _pos = await _find_position(symbol, ex_name)
    if _pos is None:
        return _err("set_tp", f"No open position found for {symbol}")

    raw_sym    = _pos.get("symbol", symbol)
    info       = _pos.get("info", {})
    side       = str(_pos.get("side") or "long").lower()
    size       = abs(_safe_float(_pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"

    # Hedge-mode detection: holdSide is in the ccxt info dict
    hold_side_raw = info.get("holdSide") or _pos.get("holdSide")
    is_hedge      = bool(hold_side_raw)
    hold_side     = str(hold_side_raw or side).lower()
    plan_type     = "profit_plan" if is_hedge else "pos_profit"

    # ── Bitget connector.place_tpsl_order (proven, mark_price, no side param) ──
    if hasattr(_connector, "place_tpsl_order"):
        try:
            bsym   = _bitget_sym(raw_sym)
            result = await _connector.place_tpsl_order(
                symbol        = bsym,
                margin_coin   = "USDT",
                plan_type     = plan_type,
                trigger_price = float(price),
                hold_side     = hold_side,
                size          = _fmt_size(size) if is_hedge else None,
            )
            oid    = result.get("orderId", "") or result.get("clientOid", "")
            speech = (
                f"Take profit set at {price} for {symbol}. Order ID {oid}."
                if oid else f"Take profit set at {price} for {symbol}."
            )
            return CommandResult(
                ok=True, action="set_tp",
                detail=f"TP @ {price} for {symbol} ({size} contracts, {close_side})",
                speech=speech,
                order={"id": oid, "price": price, "symbol": symbol},
            )
        except BaseException as e:
            err_msg = _friendly_exchange_error(e)
            logger.error(f"[JARVIS] set_tp (bitget connector) failed: {e}")
            return _err("set_tp", err_msg)

    # ── Generic ccxt fallback ──────────────────────────────────────────────────
    try:
        order = await _connector.exchange.create_order(
            symbol=raw_sym, type="TAKE_PROFIT_MARKET", side=close_side, amount=size,
            params={"stopPrice": price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )
        speech = f"Take profit set at {price} for {symbol}."
        return CommandResult(
            ok=True, action="set_tp",
            detail=f"TP @ {price} for {symbol} ({size} contracts, {close_side})",
            speech=speech,
            order={"id": order.get("id"), "price": price, "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] set_tp (ccxt) failed: {e}")
        return _err("set_tp", err_msg)


async def _set_sl_pct(symbol: str, pct: float, ex_name: Optional[str]) -> CommandResult:
    """Set stop-loss at a percentage loss.

    For LONG:  SL = entry × (1 - pct/100)  — price drops, stops out
    For SHORT: SL = entry × (1 + pct/100÷leverage) — price rises, stops out
    """
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("set_sl", f"No open position found for {symbol}")

    entry    = _safe_float(pos.get("entryPrice"))
    mark     = _safe_float(pos.get("markPrice")) or entry
    side     = str(pos.get("side") or "long").lower()
    leverage = _safe_float(pos.get("leverage")) or 10.0

    if entry <= 0:
        return _err("set_sl", f"Cannot determine entry price for {symbol}")

    if side == "short":
        price_rise_pct = pct / leverage
        sl_price = round(mark * (1 + price_rise_pct / 100), 8)  # SL above mark for short
    else:
        price_drop_pct = pct / leverage
        sl_price = round(mark * (1 - price_drop_pct / 100), 8)  # SL below mark for long
        if sl_price <= 0:
            sl_price = round(mark * 0.001, 8)

    return await _set_sl_price(symbol, sl_price, ex_name, _connector=connector, _pos=pos)


async def _set_sl_price(
    symbol: str,
    price: float,
    ex_name: Optional[str],
    *,
    _connector=None,
    _pos=None,
) -> CommandResult:
    """Place a stop-loss TPSL order at an absolute price.

    Uses connector.place_tpsl_order (mark_price, no extraneous side param).
    Validates the SL price direction before calling the exchange.
    """
    if _connector is None:
        _connector, _pos = await _find_position(symbol, ex_name)
    if _pos is None:
        return _err("set_sl", f"No open position found for {symbol}")

    raw_sym    = _pos.get("symbol", symbol)
    info       = _pos.get("info", {})
    side       = str(_pos.get("side") or "long").lower()
    size       = abs(_safe_float(_pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"
    mark       = _safe_float(_pos.get("markPrice")) or _safe_float(_pos.get("entryPrice")) or 0

    # Validate SL direction relative to current mark price
    if mark > 0:
        if side == "long" and price >= mark:
            msg = (f"SL price {price} must be BELOW current mark {mark:.5f} for a long. "
                   f"Try a price under {mark:.5f}.")
            return CommandResult(ok=False, action="set_sl", detail=msg, speech=msg)
        if side == "short" and price <= mark:
            msg = (f"SL price {price} must be ABOVE current mark {mark:.5f} for a short. "
                   f"Try a price over {mark:.5f}.")
            return CommandResult(ok=False, action="set_sl", detail=msg, speech=msg)

    # Hedge-mode detection
    hold_side_raw = info.get("holdSide") or _pos.get("holdSide")
    is_hedge      = bool(hold_side_raw)
    hold_side     = str(hold_side_raw or side).lower()
    plan_type     = "loss_plan" if is_hedge else "pos_loss"

    # ── Bitget connector.place_tpsl_order (proven, mark_price, no side param) ──
    if hasattr(_connector, "place_tpsl_order"):
        try:
            bsym   = _bitget_sym(raw_sym)
            result = await _connector.place_tpsl_order(
                symbol        = bsym,
                margin_coin   = "USDT",
                plan_type     = plan_type,
                trigger_price = float(price),
                hold_side     = hold_side,
                size          = _fmt_size(size) if is_hedge else None,
            )
            oid    = result.get("orderId", "") or result.get("clientOid", "")
            speech = (
                f"Stop loss set at {price} for {symbol}. Order ID {oid}."
                if oid else f"Stop loss set at {price} for {symbol}."
            )
            return CommandResult(
                ok=True, action="set_sl",
                detail=f"SL @ {price} for {symbol} ({size} contracts, {close_side})",
                speech=speech,
                order={"id": oid, "price": price, "symbol": symbol},
            )
        except BaseException as e:
            err_msg = _friendly_exchange_error(e)
            logger.error(f"[JARVIS] set_sl (bitget connector) failed: {e}")
            return _err("set_sl", err_msg)

    # ── Generic ccxt fallback ──────────────────────────────────────────────────
    try:
        order = await _connector.exchange.create_order(
            symbol=raw_sym, type="STOP_MARKET", side=close_side, amount=size,
            params={"stopPrice": price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )
        speech = f"Stop loss set at {price} for {symbol}."
        return CommandResult(
            ok=True, action="set_sl",
            detail=f"SL @ {price} for {symbol} ({size} contracts, {close_side})",
            speech=speech,
            order={"id": order.get("id"), "price": price, "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] set_sl (ccxt) failed: {e}")
        return _err("set_sl", err_msg)


async def _close_position(symbol: str, ex_name: Optional[str]) -> CommandResult:
    """Market-close an open position."""
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("close", f"No open position found for {symbol}")

    raw_sym    = pos.get("symbol", symbol)
    side       = str(pos.get("side") or "long").lower()
    size       = abs(_safe_float(pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"
    pnl        = _safe_float(pos.get("unrealizedPnl"))

    # ── Bitget native client ──────────────────────────────────────────────────
    # For one-way mode, the native API's `tradeSide:'close'` parameter conflicts
    # with unilateral position handling.  Use ccxt directly — it has built-in
    # Bitget swap handling (position mode + reduceOnly resolution).
    # Skip native client for close and go straight to ccxt.

    # ── ccxt close (handles one-way + hedge mode automatically) ──────────────
    try:
        order = await connector.exchange.create_order(
            symbol=raw_sym,
            type="market",
            side=close_side,
            amount=size,
            params={
                "reduceOnly": True,
                "positionSide": "one_way" if not pos.get("holdSide") else side.upper(),
            },
        )
        sign   = "profit" if pnl >= 0 else "loss"
        speech = (
            f"{symbol} position closed. "
            f"{sign.capitalize()} of {abs(pnl):.2f} USDT."
        )
        return CommandResult(
            ok=True, action="close",
            detail=f"Closed {symbol} {size} @ market | PnL {pnl:+.2f} USDT",
            speech=speech,
            order={"id": order.get("id"), "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] close (ccxt) failed: {e}")
        return _err("close", err_msg)


async def _list_positions() -> CommandResult:
    positions = await get_all_positions()
    if not positions:
        return CommandResult(
            ok=True, action="list_positions",
            detail="No open positions.",
            speech="You have no open positions, Sir.",
        )
    lines = []
    for p in positions:
        sign = "up" if p.pnl >= 0 else "down"
        lines.append(
            f"{p.symbol} {p.side.upper()} | "
            f"entry {p.entry_price} → mark {p.mark_price} | "
            f"PnL {p.pnl:+.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
    speech_parts = [f"{p.symbol} is {('up' if p.pnl>=0 else 'down')} {abs(p.pnl_pct):.1f}%" for p in positions]
    speech = f"You have {len(positions)} open position{'s' if len(positions)>1 else ''}. " + ", ".join(speech_parts) + "."
    return CommandResult(
        ok=True, action="list_positions",
        detail="\n".join(lines),
        speech=speech,
    )


async def _analyze_positions_with_news(cmd: str) -> CommandResult:
    """
    Fetch every open position + recent news articles, match headlines to each
    position by token symbol, then call the AI router for a real qualitative
    impact analysis.  Falls back to a structured table if AI is unavailable.

    Called when the user says things like:
      "analyse current positions"
      "with coming news analyse my positions"
      "how will today's news impact my open positions"
    """
    # 1. Fetch all open positions ───────────────────────────────────────────
    positions = await get_all_positions()
    if not positions:
        msg = "You have no open positions, Sir. Nothing to analyse against the news."
        return CommandResult(ok=True, action="news_position_analysis", detail=msg, speech=msg)

    # 2. Fetch recent news articles from the DB (each article already has
    #    a pre-parsed 'symbols' list, 'sentiment_score', 'sentiment_label')
    articles: List[Dict[str, Any]] = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.sentiment.enhanced_service import EnhancedSentimentService
        async with AsyncSessionLocal() as db:
            articles = await EnhancedSentimentService.get_articles(db, hours=24, limit=50)
    except Exception as e:
        logger.warning(f"[JARVIS] news fetch for position analysis failed: {e}")

    # 3. Match articles to each position by base-token symbol ───────────────
    #    An article's 'symbols' field is a list like ["BTC", "ETH", "PEPE"].
    position_bases: Dict[str, str] = {}   # base → full symbol  e.g. "UNI" → "UNIUSDT"
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "")
        position_bases[base.upper()] = p.symbol

    pos_articles: Dict[str, List[Dict]] = {b: [] for b in position_bases}
    general_articles: List[Dict] = []

    for art in articles:
        syms_raw: List[str] = art.get("symbols") or []
        syms_up = [s.upper() for s in syms_raw]
        matched_bases = [b for b in position_bases if b in syms_up]
        if matched_bases:
            for b in matched_bases:
                pos_articles[b].append(art)
        else:
            general_articles.append(art)

    # 4. Build a compact prompt for the AI ──────────────────────────────────
    position_prompt_lines: List[str] = []
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "").upper()
        pnl_arrow = "▲" if p.pnl >= 0 else "▼"
        line = (
            f"- {base} {p.side.upper()} | "
            f"entry ${p.entry_price:.6g} → mark ${p.mark_price:.6g} | "
            f"PnL {pnl_arrow} {abs(p.pnl):.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
        arts = pos_articles.get(base, [])[:3]
        for a in arts:
            score = a.get("sentiment_score")
            label = (
                a.get("sentiment_label")
                or ("BULLISH" if (score or 0) > 0.1 else "BEARISH" if (score or 0) < -0.1 else "NEUTRAL")
            )
            line += f"\n  [{label}] {(a.get('title') or '')[:120]}"
        if not arts:
            line += "\n  (no specific headlines today)"
        position_prompt_lines.append(line)

    general_headlines_text = ""
    if general_articles:
        general_headlines_text = "\n\nGeneral market headlines:\n" + "\n".join(
            f"- {(a.get('title') or '')[:120]}" for a in general_articles[:6]
        )

    total_arts = len(articles)
    prompt_body = (
        "My open trading positions with today's matching news:\n"
        + "\n".join(position_prompt_lines)
        + general_headlines_text
        + f"\n\n({total_arts} total headlines from the last 24 hours)\n\n"
        "Task: For EACH position, write one clear sentence explaining how today's news "
        "may help or hurt that trade.  For positions with no specific news, briefly "
        "note if the general headlines are bullish or bearish for the overall market. "
        "End with a 1-sentence overall portfolio risk note.  Be direct and concise."
    )

    # 5. Ask the AI router for a real qualitative analysis ──────────────────
    # Routes to the news_position task model (Gemini Flash, 1M-token context)
    # so large batches of headlines + multiple positions fit in one call.
    ai_detail: Optional[str] = None
    try:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            resp = await _task_chat(
                "news_position",
                db,
                [
                    {
                        "role": "system",
                        "content": (
                            "You are JARVIS, a sharp trading assistant. "
                            "Give factual, direct and thorough analysis — no filler phrases. "
                            "Cover each position's alignment with current news, risk, and recommendation. "
                            "Never truncate mid-sentence."
                        ),
                    },
                    {"role": "user", "content": prompt_body},
                ],
                max_tokens=1600,
                temperature=0.25,
            )
            if resp.get("ok") and resp.get("content"):
                ai_detail = str(resp["content"]).strip()
    except Exception as e:
        logger.warning(f"[JARVIS] AI analysis for news/positions failed: {e}")

    # 6. Return AI response when available ──────────────────────────────────
    if ai_detail:
        n_pos = len(positions)
        speech = (
            f"News impact analysis for your {n_pos} open position{'s' if n_pos > 1 else ''}, Sir. "
            + ai_detail[:480].replace("\n", " ")
        )
        # Fire brain managers with the news/position synthesis — idle Mistral + Gemma
        # learn from every completed news_position analysis cycle too.
        _fire_brain_managers(
            {"news_position": ai_detail, "news_context": prompt_body[:500]},
            symbol="portfolio",
        )
        return CommandResult(
            ok=True, action="news_position_analysis",
            detail=ai_detail,
            speech=speech,
        )

    # 7. Structured fallback (AI unavailable) ───────────────────────────────
    detail_parts: List[str] = []
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "").upper()
        arts = pos_articles.get(base, [])
        pnl_arrow = "▲" if p.pnl >= 0 else "▼"
        line = (
            f"{p.symbol} {p.side.upper()} | "
            f"entry {p.entry_price:.6g} → mark {p.mark_price:.6g} | "
            f"PnL {pnl_arrow} {abs(p.pnl):.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
        if arts:
            for a in arts[:2]:
                score = a.get("sentiment_score")
                label = (
                    a.get("sentiment_label")
                    or ("BULLISH" if (score or 0) > 0.1 else "BEARISH" if (score or 0) < -0.1 else "NEUTRAL")
                )
                line += f"\n  [{label}] {(a.get('title') or '')[:100]}"
        else:
            line += "\n  No specific news today"
        detail_parts.append(line)

    if general_articles:
        detail_parts.append(
            "\nGeneral market headlines:\n"
            + "\n".join(f"  • {(a.get('title') or '')[:100]}" for a in general_articles[:5])
        )

    detail = (
        f"News Impact — {len(positions)} positions · {total_arts} headlines (last 24 h)\n\n"
        + "\n\n".join(detail_parts)
    )
    speech = (
        f"I matched {total_arts} headlines against your {len(positions)} positions. "
        "AI analysis is unavailable — check the details panel for the headline breakdown."
    )
    return CommandResult(ok=True, action="news_position_analysis", detail=detail, speech=speech)


async def _position_status(symbol: str, ex_name: Optional[str]) -> CommandResult:
    # Resolve the token → real coin name (so JARVIS says "Bitcoin", not "BTC").
    coin_name = symbol
    try:
        from app.services import pair_catalog
        rp = await pair_catalog.resolve(symbol)
        if rp is not None:
            coin_name = rp.name or rp.base
    except Exception:
        pass

    try:
        connector, pos = await _find_position(symbol, ex_name)
    except BaseException as e:
        friendly = "Exchange connection failed — please check your network."
        return CommandResult(ok=True, action="position_status", detail=friendly, speech=friendly)

    if pos is None:
        # No open position — give a live market update instead of a dead-end,
        # using the catalog's cached market cap / volume / price snapshot.
        try:
            from app.services import pair_catalog
            snap = await pair_catalog.get_market_snapshot(symbol)
        except Exception:
            snap = None
        if snap and snap.get("price") is not None:
            chg = snap.get("price_change_24h")
            cap = snap.get("market_cap")
            vol = snap.get("volume_24h")
            dir_txt = ""
            if chg is not None:
                dir_txt = f" {'up' if chg >= 0 else 'down'} {abs(chg):.2f} percent over 24 hours"
            speech = (
                f"{coin_name} is trading at {snap['price']:.6g}{dir_txt}. "
                + (f"Market cap {_fmt_usd_short(cap)}. " if cap else "")
                + (f"24 hour volume {_fmt_usd_short(vol)}. " if vol else "")
                + "You have no open position on it, Sir."
            )
            detail = (
                f"{coin_name} ({snap.get('symbol', symbol)}) | price {snap['price']:.6g}"
                + (f" | 24h {chg:+.2f}%" if chg is not None else "")
                + (f" | mcap {_fmt_usd_short(cap)}" if cap else "")
                + (f" | vol {_fmt_usd_short(vol)}" if vol else "")
                + " | no open position"
            )
            return CommandResult(ok=True, action="position_status", detail=detail, speech=speech)
        return CommandResult(
            ok=True, action="position_status",
            detail=f"No open position found for {coin_name}",
            speech=f"You have no open position on {coin_name}, Sir.",
        )

    entry   = _safe_float(pos.get("entryPrice"))
    mark    = _safe_float(pos.get("markPrice")) or entry
    pnl     = _safe_float(pos.get("unrealizedPnl"))
    pnl_pct = _safe_float(pos.get("percentage"))
    side    = str(pos.get("side") or "long")
    direction = "up" if pnl >= 0 else "down"
    speech = (
        f"{coin_name} {side} position is {direction} {abs(pnl_pct):.2f} percent. "
        f"PnL {'plus' if pnl>=0 else 'minus'} {abs(pnl):.2f} USDT. "
        f"Entry {entry:.6g}, current {mark:.6g}."
    )
    return CommandResult(
        ok=True, action="position_status",
        detail=f"{coin_name} ({symbol}) {side} | entry {entry} | mark {mark} | PnL {pnl:+.2f} USDT ({pnl_pct:+.2f}%)",
        speech=speech,
    )


def _fmt_usd_short(v: Optional[float]) -> str:
    """Human-readable short USD, e.g. 1.17T, 42.7B, 903M, 12.3K."""
    try:
        n = float(v or 0)
    except Exception:
        return "$0"
    a = abs(n)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"${n / div:.2f}{suf}"
    return f"${n:.0f}"
