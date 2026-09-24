"""
Neuro Core — Auto-sync all other brains into the federated store.

This is what makes Neuro the CORE brain and not just another mirror:
every N minutes it pulls the latest from Hermes, Obsidian, AiAnalyst,
PaulKnowledge, AgentDecision/Journal, CryptoPair and the Trade journal,
dedupes by content prefix, and ingests into neuro_episodes.

It also exposes a deciding review: given the Trading Room's tentative
verdict, Neuro checks its own history for that symbol/action and the
federated recall, and can downgrade confidence or veto to HOLD when
its track record says the same setup has been losing.

All best-effort — a single brain being offline never blocks the others.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from app.core.config import settings
from app.neuro_brain import is_enabled
from app.neuro_brain.state_store import ensure_store, ingest_local, search_local, stats as neuro_stats


# dedupe window: don't re-ingest identical content within this many seconds
_DEDUPE_WINDOW_S = 3600 * 6

_seen_hashes: Set[str] = set()
_seen_prune_at: float = 0.0


def _content_hash(content: str) -> str:
    # cheap prefix hash — enough to avoid exact duplicates without crypto cost
    return (content or "")[:80].strip().lower()


def _should_ingest(content: str) -> bool:
    h = _content_hash(content)
    if not h or h in _seen_hashes:
        # still check FTS for recent duplicate (covers restarts)
        try:
            hits = search_local(h[:40], limit=1)
            if hits and any(_content_hash(x.get("content","")) == h for x in hits):
                return False
        except Exception:
            pass
        # if we have seen hash this run, skip
        if h in _seen_hashes:
            return False
    _seen_hashes.add(h)
    # periodic prune of the in-memory set
    global _seen_prune_at
    if time.time() - _seen_prune_at > 3600:
        if len(_seen_hashes) > 5000:
            _seen_hashes.clear()
        _seen_prune_at = time.time()
    return True


async def sync_once(limit_per_brain: int = 30) -> Dict[str, Any]:
    """Pull latest from every brain and mirror into Neuro. Returns counts."""
    if not is_enabled():
        return {"ok": False, "reason": "neuro disabled"}
    ensure_store()
    counts: Dict[str, int] = {"hermes": 0, "vault": 0, "ai_analyst": 0, "paul": 0, "decisions": 0, "journal": 0, "trades": 0, "kronos": 0}
    errors: List[str] = []

    # ── Hermes episodic (FTS5) ──
    try:
        from pathlib import Path
        from app.hermes_bridge import state_path as hermes_path
        from app.hermes_bridge.state_store import ensure_store as hermes_ensure
        import sqlite3
        p = Path(hermes_path())
        hermes_ensure()
        if p.exists():
            conn = sqlite3.connect(str(p))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT ts, kind, symbol, session_id, content, meta_json FROM hermes_episodes ORDER BY ts DESC LIMIT ?", (limit_per_brain,)).fetchall()
                for r in rows:
                    content = (r["content"] or "").strip()
                    if not content or not _should_ingest(content):
                        continue
                    ok = ingest_local(
                        kind=f"sync_hermes_{r['kind']}",
                        content=content[:2800],
                        symbol=(r["symbol"] or "")[:24],
                        session_id=(r["session_id"] or "")[:24],
                        source="hermes-sync",
                        meta={"orig_ts": r["ts"], "orig_kind": r["kind"]},
                    )
                    if ok:
                        counts["hermes"] += 1
            finally:
                conn.close()
    except Exception as e:
        errors.append(f"hermes:{e.__class__.__name__}")

    # ── AgentDecision + Jarvis Journal (Postgres) ──
    try:
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select, desc
        from app.models.database import AgentDecision, JarvisAnalysisJournal
        async with AsyncSessionLocal() as db:
            # decisions
            try:
                rows = (await db.execute(select(AgentDecision).order_by(desc(AgentDecision.created_at)).limit(limit_per_brain))).scalars().all()
                for r in rows:
                    content = f"[{r.symbol} {r.agent_role}:{r.action} {r.confidence:.2f}] {(r.reasoning or '')[:600]}"
                    if not _should_ingest(content):
                        continue
                    if ingest_local(kind="sync_decision", content=content, symbol=r.symbol or "", source="decision-sync", meta={"agent_role": r.agent_role, "action": r.action, "decision_id": r.id}):
                        counts["decisions"] += 1
            except Exception as e:
                errors.append(f"decisions:{e.__class__.__name__}")
            # journal
            try:
                rows = (await db.execute(select(JarvisAnalysisJournal).order_by(desc(JarvisAnalysisJournal.created_at)).limit(limit_per_brain))).scalars().all()
                for r in rows:
                    content = f"[{r.symbol} {r.side} entry={r.entry} SL={r.stop_loss} TP={r.take_profit} conf={r.confidence}] {r.features or ''}"[:2500]
                    if not _should_ingest(content):
                        continue
                    if ingest_local(kind="sync_journal", content=content, symbol=r.symbol or "", source="journal-sync", meta={"journal_id": r.id, "outcome": r.outcome}):
                        counts["journal"] += 1
            except Exception as e:
                errors.append(f"journal:{e.__class__.__name__}")

            # Trades (recent)
            try:
                from app.models.database import Trade
                rows = (await db.execute(select(Trade).order_by(desc(Trade.created_at)).limit(limit_per_brain))).scalars().all()
                for r in rows:
                    content = f"[TRADE {r.symbol} {r.side}/{r.trade_side or ''} {r.order_type} {r.amount}@{r.price} {r.status} pnl={r.pnl}]"
                    if not _should_ingest(content):
                        continue
                    if ingest_local(kind="sync_trade", content=content, symbol=r.symbol or "", source="trade-sync", meta={"trade_id": r.id, "status": r.status}):
                        counts["trades"] += 1
            except Exception as e:
                errors.append(f"trades:{e.__class__.__name__}")

            # AiAnalyst knowledge — real table is ai_agent_knowledge (not ai_knowledge)
            try:
                from sqlalchemy import text
                # Try ORM first if available
                try:
                    from plugins.AiMarketAnalyst.backend.models import AIAgentKnowledge  # type: ignore
                    rows = (await db.execute(select(AIAgentKnowledge).order_by(desc(AIAgentKnowledge.created_at)).limit(limit_per_brain))).scalars().all()  # type: ignore
                    for r in rows:
                        c = getattr(r, "content", "") or ""
                        content = f"[AI-KNOW {getattr(r,'symbol','') or ''} {getattr(r,'kind','') or getattr(r,'category','') or ''}] {c[:800]}"
                        if not _should_ingest(content):
                            continue
                        if ingest_local(kind="sync_ai_analyst", content=content, symbol=getattr(r,"symbol","") or "", source="ai-analyst-sync", meta={"kind": getattr(r,"kind","") or getattr(r,"category","")}):
                            counts["ai_analyst"] += 1
                except Exception:
                    # fallback raw SQL with correct table name
                    rows = (await db.execute(text("SELECT symbol, kind, content FROM ai_agent_knowledge ORDER BY created_at DESC LIMIT :lim"), {"lim": limit_per_brain})).fetchall()
                    for r in rows:
                        sym, kind, c = r[0], r[1], r[2]
                        content = f"[AI-KNOW {sym or ''} {kind or ''}] {(c or '')[:800]}"
                        if not _should_ingest(content):
                            continue
                        if ingest_local(kind="sync_ai_analyst", content=content, symbol=sym or "", source="ai-analyst-sync", meta={"kind": kind}):
                            counts["ai_analyst"] += 1
            except Exception as e:
                # rollback the aborted transaction so the next brain (paul) is not poisoned
                try:
                    await db.rollback()
                except Exception:
                    pass
                errors.append(f"ai_analyst:{e.__class__.__name__}")
            # PaulKnowledge
            try:
                from sqlalchemy import text
                rows = (await db.execute(text("SELECT symbol, topic, content FROM paul_knowledge ORDER BY created_at DESC LIMIT :lim"), {"lim": limit_per_brain})).fetchall()
                for r in rows:
                    sym, topic, c = r[0], r[1], r[2]
                    content = f"[PAUL {sym or ''} {topic or ''}] {(c or '')[:800]}"
                    if not _should_ingest(content):
                        continue
                    if ingest_local(kind="sync_paul", content=content, symbol=sym or "", source="paul-sync", meta={"topic": topic}):
                        counts["paul"] += 1
                # also paul_memory_nodes if any
                try:
                    rows2 = (await db.execute(text("SELECT title, content FROM paul_memory_nodes ORDER BY created_at DESC LIMIT :lim"), {"lim": 10})).fetchall()
                    for r in rows2:
                        title, c = r[0], r[1]
                        content = f"[PAUL-MEMORY {title or ''}] {(c or '')[:800]}"
                        if not _should_ingest(content):
                            continue
                        if ingest_local(kind="sync_paul", content=content, symbol="", source="paul-sync", meta={"title": title}):
                            counts["paul"] += 1
                except Exception:
                    pass
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                errors.append(f"paul:{e.__class__.__name__}")
    except Exception as e:
        try:
            await db.rollback()  # type: ignore
        except Exception:
            pass
        errors.append(f"pg:{e.__class__.__name__}")

    # ── Obsidian Vault — DB first, then filesystem fallback (handles both layouts) ──
    # DB table obsidian_vault_notes is the source of truth if vault DB sync is enabled;
    # filesystem at obsidian-vault/ (and legacy obsidian-vault/tradebot/) covers
    # file-only notes and communities.
    try:
        from sqlalchemy import text
        from app.core.database import AsyncSessionLocal as _ASL3
        async with _ASL3() as _db2:
            try:
                rows = (await _db2.execute(text("SELECT path, note_type, symbol, tags, checksum FROM obsidian_vault_notes ORDER BY updated_at DESC LIMIT :lim"), {"lim": limit_per_brain})).fetchall()
                for r in rows:
                    path, note_type, sym, tags, checksum = r[0], r[1], r[2], r[3], r[4]
                    content = f"[VAULT-DB {path} {note_type} {sym or ''}] {str(tags or '')[:300]}"
                    if not _should_ingest(content):
                        continue
                    if ingest_local(kind="sync_vault", content=content, symbol=sym or "", source="vault-sync", meta={"path": path, "note_type": note_type}):
                        counts["vault"] += 1
            except Exception as e:
                errors.append(f"vault-db:{e.__class__.__name__}")
    except Exception:
        pass
    try:
        from pathlib import Path as _P
        from app.neuro_brain import local_path
        root = _P(local_path()).parents[2] if _P(local_path()).exists() else _P(__file__).resolve().parents[3]
        # Try both layouts: obsidian-vault/ (actual) and obsidian-vault/tradebot (legacy)
        candidates = [root / "obsidian-vault", root / "obsidian-vault" / "tradebot"]
        vault_roots = [p for p in candidates if p.exists()]
        if not vault_roots:
            # also try absolute vault path from env if set
            try:
                from app.core.config import settings as _s
                vp = getattr(_s, "OBSIDIAN_VAULT_PATH", "") or ""
                if vp:
                    import os
                    ap = _P(os.path.expanduser(vp))
                    if ap.exists():
                        vault_roots = [ap]
            except Exception:
                pass
        for vault_root in vault_roots:
            try:
                notes = sorted(vault_root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit_per_brain]
                for p in notes:
                    try:
                        txt = p.read_text(encoding="utf-8", errors="ignore")[:2000]
                        rel = str(p.relative_to(vault_root))
                        # Skip if this path already counted via DB (deduped by content hash, so okay to try)
                        content = f"[VAULT-FS {rel}] {txt[:900]}"
                        if not _should_ingest(content):
                            continue
                        if ingest_local(kind="sync_vault", content=content, symbol="", source="vault-sync", meta={"path": rel}):
                            counts["vault"] += 1
                    except Exception:
                        continue
            except Exception:
                continue
            # Only crawl the first valid root to avoid double-counting same files twice
            if counts["vault"] > 0:
                break
    except Exception as e:
        errors.append(f"vault-fs:{e.__class__.__name__}")

    # ── Kronos forecasts (if any) ──
    try:
        from sqlalchemy import text
        from app.core.database import AsyncSessionLocal as _ASL2
        async with _ASL2() as db:
            rows = (await db.execute(text("SELECT symbol, direction, confidence FROM kronos_forecasts ORDER BY created_at DESC LIMIT :lim"), {"lim": 10})).fetchall()
            for r in rows:
                sym, direction, conf = r[0], r[1], r[2]
                content = f"[KRONOS {sym} {direction} conf={conf}] forecast"
                if not _should_ingest(content):
                    continue
                if ingest_local(kind="sync_kronos", content=content, symbol=sym or "", source="kronos-sync", meta={"direction": direction}):
                    counts["kronos"] += 1
    except Exception:
        pass

    total = sum(counts.values())
    logger.info(f"[neuro-sync] mirrored {total} episodes ({counts})" + (f" errors:{errors}" if errors else ""))
    return {"ok": True, "total": total, "counts": counts, "errors": errors, "stats": neuro_stats()}


# ── Background loop ────────────────────────────────────────────────────────
_sync_task: Optional[asyncio.Task] = None


async def _sync_loop(interval_s: int):
    logger.info(f"[neuro-sync] auto-sync loop started — every {interval_s}s (core brain mirroring all brains)")
    # initial sync shortly after boot so fresh installs backfill immediately
    await asyncio.sleep(8)
    while True:
        try:
            if is_enabled():
                await sync_once()
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[neuro-sync] loop error: {e}")
            await asyncio.sleep(10)


def start_neuro_sync_loop(interval_s: int | None = None) -> bool:
    global _sync_task
    if _sync_task and not _sync_task.done():
        return False
    iv = interval_s or int(getattr(settings, "NEUROGLANCER_PULL_INTERVAL_S", 600) or 600)
    # sync wants to be more frequent than repo pull — default 5 min, never less than 60s
    iv = max(60, min(iv, 600))
    # use dedicated NEUROGLANCER_SYNC_INTERVAL_S if set
    try:
        iv = int(getattr(settings, "NEUROGLANCER_SYNC_INTERVAL_S", iv) or iv)
    except Exception:
        pass
    try:
        loop = asyncio.get_running_loop()
        _sync_task = loop.create_task(_sync_loop(iv))
        return True
    except RuntimeError:
        return False


def stop_neuro_sync_loop() -> bool:
    global _sync_task
    if _sync_task and not _sync_task.done():
        _sync_task.cancel()
        _sync_task = None
        return True
    return False


def sync_loop_status() -> Dict[str, Any]:
    running = bool(_sync_task and not _sync_task.done())
    return {"running": running, "interval_s": int(getattr(settings, "NEUROGLANCER_SYNC_INTERVAL_S", 300) or 300)}


# ── Deciding brain — Neuro as final arbiter ───────────────────────────────
async def deciding_review(
    symbol: str,
    tentative_action: str,
    tentative_confidence: float,
    consensus: Dict[str, Any] | None = None,
    timeframe: str = "1h",
    trigger: str = "scanner",
) -> Dict[str, Any]:
    """
    Neuro reviews the Trading Room's tentative verdict before it goes live.

    Returns {action, confidence, veto, reason, neuro_context}.
    - veto=True → force HOLD (Neuro saw this setup lose repeatedly)
    - otherwise returns possibly downgraded confidence + reason

    This is what makes Neuro the DECIDING brain: every execution, whether via
    orchestrator or JARVIS, passes through here when federation is on.
    """
    tentative = (tentative_action or "hold").lower()
    if not is_enabled() or not getattr(settings, "NEUROGLANCER_FEDERATION_ENABLED", True):
        return {"action": tentative, "confidence": tentative_confidence, "veto": False, "reason": "neuro disabled/federation off"}
    if not getattr(settings, "NEUROGLANCER_DECIDING_ENABLED", True):
        return {"action": tentative, "confidence": tentative_confidence, "veto": False, "reason": "deciding brain off"}

    # Only intervene on actionable directions; HOLD is already safe
    if tentative not in ("buy", "sell"):
        return {"action": tentative, "confidence": tentative_confidence, "veto": False, "reason": "hold — no review needed"}

    try:
        # 1) Historical edge for this symbol+action from neuro_trade_segments
        from app.neuro_brain.state_store import list_segments

        hist = list_segments(limit=200, symbol=symbol)
        relevant = [s for s in hist if (s.get("side") or "").lower() in (tentative, "long" if tentative == "buy" else "short", tentative)]
        # also consider buy/sell mapping
        if tentative == "buy":
            relevant = [s for s in hist if (s.get("side") or "").lower() in ("buy", "long")]
        else:
            relevant = [s for s in hist if (s.get("side") or "").lower() in ("sell", "short")]

        wins = sum(1 for s in relevant if s.get("outcome") == "win")
        losses = sum(1 for s in relevant if s.get("outcome") == "loss")
        total_decided = wins + losses

        # 2) Federated recall — what do the brains say right now?
        recall_snippet = ""
        try:
            from app.neuro_brain.federated_search import brain_recall_context

            recall_snippet = await brain_recall_context(symbol, max_chars=500)
        except Exception:
            pass

        # 3) Heuristic veto / downgrade rules (no LLM cost, deterministic)
        reason_parts: List[str] = []
        veto = False
        new_conf = float(tentative_confidence or 0)

        # Rule A: if Neuro has ≥8 decided samples for this symbol+direction and win rate < 0.28, veto to HOLD
        if total_decided >= 8:
            wr = wins / total_decided if total_decided else 0
            if wr < 0.28:
                veto = True
                reason_parts.append(f"Neuro track record for {symbol} {tentative}: {wins}W/{losses}L ({wr:.0%} over {total_decided}) — veto to HOLD")
            elif wr < 0.38:
                # downgrade confidence heavily
                new_conf = min(new_conf, 0.42)
                reason_parts.append(f"Neuro edge weak for {symbol} {tentative}: {wins}W/{losses}L ({wr:.0%}) — confidence capped to 0.42")

        # Rule B: if federated recall is explicitly bearish while tentative is bullish (or vice versa), downgrade
        low = (recall_snippet or "").lower()
        if recall_snippet:
            bearish_terms = ("bearish", "sell", "short", "distribution", "overbought")
            bullish_terms = ("bullish", "buy", "long", "accumulation", "oversold")
            is_bearish_recall = sum(1 for t in bearish_terms if t in low) > sum(1 for t in bullish_terms if t in low) + 1
            is_bullish_recall = sum(1 for t in bullish_terms if t in low) > sum(1 for t in bearish_terms if t in low) + 1
            if tentative == "buy" and is_bearish_recall:
                new_conf = min(new_conf, 0.45)
                reason_parts.append("Federated recall leans bearish/distribution — confidence capped to 0.45")
            elif tentative == "sell" and is_bullish_recall:
                new_conf = min(new_conf, 0.45)
                reason_parts.append("Federated recall leans bullish/accumulation — confidence capped to 0.45")

        # Rule C: consensus agreement very low → veto (Neuro reinforces risk_manager)
        if consensus is not None:
            try:
                agree = float(consensus.get("agreement") or 0)
                if agree < 0.34:
                    veto = True
                    reason_parts.append(f"Board agreement {agree:.0%} is too low — Neuro veto to HOLD (deciding brain)")
            except Exception:
                pass

        # Rule D: if no history and recall explicitly says HOLD, respect it
        if not reason_parts and recall_snippet and "hold" in low and total_decided == 0 and new_conf > 0.55:
            new_conf = min(new_conf, 0.52)
            reason_parts.append("No Neuro history; federated recall suggests HOLD — confidence capped to 0.52")

        if veto:
            return {
                "action": "hold",
                "confidence": 0.0,
                "veto": True,
                "reason": " | ".join(reason_parts) or "Neuro veto",
                "neuro_context": recall_snippet[:400],
                "history": {"wins": wins, "losses": losses, "total": total_decided},
            }
        if reason_parts:
            return {
                "action": tentative,
                "confidence": round(min(max(new_conf, 0.0), 1.0), 3),
                "veto": False,
                "reason": " | ".join(reason_parts),
                "neuro_context": recall_snippet[:400],
                "history": {"wins": wins, "losses": losses, "total": total_decided},
            }
        return {"action": tentative, "confidence": tentative_confidence, "veto": False, "reason": "Neuro pass — no downgrade", "neuro_context": recall_snippet[:400], "history": {"wins": wins, "losses": losses, "total": total_decided}}
    except Exception as e:
        logger.debug(f"[neuro-decider] review skipped for {symbol}: {e}")
        return {"action": tentative, "confidence": tentative_confidence, "veto": False, "reason": f"review error: {e.__class__.__name__}"}

