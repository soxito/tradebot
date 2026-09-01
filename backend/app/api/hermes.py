"""
Hermes API — search, skills, profile, health + overview for dedicated Hermes page.

Recall-only (9.7), gated by RoomSettings.execution_enabled for skill execution.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, desc, func

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.hermes_bridge import is_enabled, gateway_url, state_path, skills_path, repo_version
from app.hermes_bridge.state_store import prune_expired, search_local, ensure_store
from app.hermes_bridge.skill_registry import is_execution_allowed, list_skills, load_skill_md, get_skill_for_symbol
from app.hermes_bridge.user_model import get_profile, profile_prompt_fragment, upsert_profile
from app.hermes_bridge.gateway_proxy import health_check_sync

router = APIRouter(prefix="/hermes", tags=["hermes"])


def _read_soul_preview(max_chars: int = 1200) -> Dict[str, Any]:
    p = Path(getattr(settings, "SOUL_PATH", "SOUL.md") or "SOUL.md")
    if not p.is_absolute():
        # resolve relative to project root (backend/..)
        candidates = [p, Path("SOUL.md"), Path("../SOUL.md"), Path(__file__).resolve().parents[3] / "SOUL.md"]
        for c in candidates:
            if c.exists():
                p = c
                break
    if not p.exists():
        return {"path": str(p), "exists": False, "preview": ""}
    try:
        text = p.read_text(encoding="utf-8")
        return {"path": str(p), "exists": True, "preview": text[:max_chars], "length": len(text)}
    except Exception:
        return {"path": str(p), "exists": True, "preview": ""}


def _hermes_stats() -> Dict[str, Any]:
    db_path = Path(state_path())
    episodes_total = 0
    episodes_24h = 0
    last_ts: Optional[float] = None
    fts_kb = 0
    try:
        ensure_store()
        if db_path.exists():
            fts_kb = round(db_path.stat().st_size / 1024, 1)
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute("SELECT COUNT(*) FROM hermes_episodes")
                episodes_total = int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT COUNT(*) FROM hermes_episodes WHERE ts > ?", (time.time() - 86400,))
                episodes_24h = int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT MAX(ts) FROM hermes_episodes")
                v = cur.fetchone()[0]
                last_ts = float(v) if v else None
            finally:
                conn.close()
    except Exception:
        pass
    return {
        "episodes_total": episodes_total,
        "episodes_24h": episodes_24h,
        "last_ingest_ts": last_ts,
        "fts_kb": fts_kb,
        "db_path": str(db_path),
        "skills_count": len(list_skills()),
    }


def _features_snapshot(gateway: Dict[str, Any], stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    en = is_enabled()
    return [
        {
            "id": "episodic",
            "label": "Episodic Memory (FTS5)",
            "desc": "90d recall-only store. Every Trading Room session + JARVIS turn is ingested; scoring stays on Postgres.",
            "enabled": en and bool(getattr(settings, "HERMES_AUTO_INGEST", True)),
            "status": "active" if en and stats["episodes_total"] > 0 else ("idle" if en else "disabled"),
            "detail": f"{stats['episodes_total']} episodes · {stats['episodes_24h']} in 24h · {stats['fts_kb']} KB",
        },
        {
            "id": "skills",
            "label": "Skills (agentskills.io)",
            "desc": "Auto-created from winning sessions (agreement≥0.4 or strong momentum). Gated by RoomSettings.execution_enabled.",
            "enabled": en,
            "status": "active" if stats["skills_count"] > 0 else ("ready" if en else "disabled"),
            "detail": f"{stats['skills_count']} skills · execution gate: RoomSettings.execution_enabled",
        },
        {
            "id": "user_model",
            "label": "Trader Profile (user-model)",
            "desc": "Lightweight profile (risk, focus, preferred pairs) injected into specialist prompts. No Honcho dialectic.",
            "enabled": en,
            "status": "active" if en and bool(get_profile()) else ("ready" if en else "disabled"),
            "detail": profile_prompt_fragment()[:120].strip() or "No profile yet — edit below",
        },
        {
            "id": "gateway",
            "label": "Hermes Gateway (sidecar :8011)",
            "desc": "Isolated like TradingAgents :8010. Owns Telegram/Discord; Telegram plugin is now consumer.",
            "enabled": en,
            "status": "connected" if gateway.get("reachable") else ("offline" if en else "disabled"),
            "detail": gateway.get("url") or gateway_url(),
        },
        {
            "id": "ingest",
            "label": "Auto-Ingest",
            "desc": "Every session_completed + JARVIS turn auto-ingests when HERMES_AUTO_INGEST=true.",
            "enabled": en and bool(getattr(settings, "HERMES_AUTO_INGEST", True)),
            "status": "on" if en and getattr(settings, "HERMES_AUTO_INGEST", True) else "off",
            "detail": "room.py:session_completed + jarvis.py:_fire_brain_managers",
        },
        {
            "id": "retention",
            "label": "Retention (90d, recall-only)",
            "desc": "FTS5 pruned nightly; scoring stays on Postgres AgentDecision. POST /hermes/prune to run now.",
            "enabled": en,
            "status": "active" if en else "disabled",
            "detail": f"{getattr(settings, 'HERMES_RETENTION_DAYS', 90)} days · DB: {stats['db_path']}",
        },
        {
            "id": "cron",
            "label": "Cron (disabled by locked scope)",
            "desc": "Hermes cron is intentionally OFF — episodic+skill+user-model only per 9.2.",
            "enabled": bool(getattr(settings, "HERMES_CRON_ENABLED", False)),
            "status": "disabled",
            "detail": "Flip HERMES_CRON_ENABLED=true to enable (optional)",
        },
        {
            "id": "soul",
            "label": "SOUL.md (merged JARVIS/Paul/SOX)",
            "desc": "Single soul, three voice variants (avatarStyle/voiceGender).",
            "enabled": True,
            "status": "active",
            "detail": getattr(settings, "SOUL_PATH", "SOUL.md"),
        },
    ]


@router.get("/health")
async def hermes_health() -> Dict[str, Any]:
    gw = health_check_sync()
    stats = _hermes_stats()
    return {
        "enabled": is_enabled(),
        "gateway": gw,
        "skills_count": stats["skills_count"],
        "episodes_total": stats["episodes_total"],
        "features": _features_snapshot(gw, stats),
        "repo": repo_version(),
    }


@router.get("/repo")
async def hermes_repo() -> Dict[str, Any]:
    return repo_version()


@router.post("/repo/pull")
async def hermes_repo_pull() -> Dict[str, Any]:
    import subprocess, time as _t, json as _j
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[3]
    repo_dir = root / "integrations" / "hermes-agent"
    ver_file = root / "integrations" / "hermes-agent.version.json"
    if not (repo_dir / ".git").exists():
        return {"ok": False, "error": "repo not cloned yet — run start.py to clone"}
    try:
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth=1", "origin"], capture_output=True, timeout=30)
        before = repo_version().get("commit")
        r = subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], capture_output=True, text=True, timeout=60)
        after = repo_version().get("commit")
        updated = before != after
        # Update version file timestamp
        try:
            v = repo_version()
            ver_file.write_text(_j.dumps({**v, "last_pull": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}, indent=2))
        except Exception:
            pass
        return {"ok": r.returncode == 0, "updated": updated, "before": before, "after": after, "output": (r.stdout or r.stderr or "")[:800], "repo": repo_version()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/overview")
async def hermes_overview() -> Dict[str, Any]:
    gw = health_check_sync()
    stats = _hermes_stats()
    features = _features_snapshot(gw, stats)
    soul = _read_soul_preview()
    # ── Agents: Trading Room + JARVIS/Paul/SOX + DB agents ──
    agents: List[Dict[str, Any]] = []
    try:
        from app.agents import room as room_state
        from app.agents.specialists import DEFAULT_AGENTS
        from app.models.database import Agent, AgentDecision
        # persona defaults
        persona_map = {**room_state.PERSONAS, "ceo": room_state.CEO_PERSONA}
        # DB agents
        db_agents: List[Any] = []
        decision_counts: Dict[str, int] = {}
        last_at: Dict[str, str] = {}
        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(Agent))).scalars().all()
                db_agents = rows
                # counts per role in last 24h + total (cheap group)
                for r in rows:
                    cnt = (await db.execute(
                        select(func.count(AgentDecision.id)).where(AgentDecision.agent_role == r.role)
                    )).scalar() or 0
                    decision_counts[r.role] = int(cnt)
                    last = (await db.execute(
                        select(AgentDecision.created_at).where(AgentDecision.agent_role == r.role).order_by(desc(AgentDecision.created_at)).limit(1)
                    )).scalar_one_or_none()
                    if last:
                        last_at[r.role] = str(last)
        except Exception:
            pass
        seen_roles = set()
        for r in db_agents:
            role = r.role
            seen_roles.add(role)
            persona = persona_map.get(role, {})
            # hermes hits: count episodes mentioning role or symbol? simple: episodes_total as proxy
            # more precise: search_local per role would be expensive here — use stats
            hermes_hits = 0
            if stats["episodes_total"] > 0:
                try:
                    hits = search_local(role, None, 1)
                    hermes_hits = len(hits)
                except Exception:
                    hermes_hits = 0
            agents.append({
                "role": role,
                "human_name": persona.get("human_name", r.name),
                "title": persona.get("title", r.role),
                "color": persona.get("color", "#94a3b8"),
                "seat": persona.get("seat", 99),
                "gender": persona.get("gender", "male"),
                "agent_id": r.id,
                "agent_name": r.name,
                "model": r.model,
                "is_active": bool(r.is_active),
                "decisions_total": decision_counts.get(role, 0),
                "last_decision_at": last_at.get(role),
                "hermes_hits": hermes_hits,
                "connected": is_enabled() and bool(r.is_active),
                "source": "db",
            })
        # CEO / JARVIS (not in Agent table, synthesis seat)
        if "ceo" not in seen_roles:
            agents.append({
                "role": "ceo",
                "human_name": persona_map["ceo"]["human_name"],
                "title": persona_map["ceo"]["title"],
                "color": persona_map["ceo"]["color"],
                "seat": persona_map["ceo"]["seat"],
                "agent_id": None,
                "agent_name": "JARVIS (chair)",
                "model": "via AiMarketAnalyst pool",
                "is_active": True,
                "decisions_total": 0,
                "hermes_hits": 0,
                "connected": is_enabled(),
                "source": "persona",
            })
        # Ensure Paul/SOX are visible as aliases of the same soul (merged per 9.3)
        # They don't get separate rows — note in SOUL feature card instead.
        agents.sort(key=lambda a: a["seat"])
    except Exception as e:
        agents = [{"error": str(e)[:200]}]

    execution_allowed = False
    try:
        execution_allowed = await is_execution_allowed()
    except Exception:
        pass

    return {
        "enabled": is_enabled(),
        "gateway": gw,
        "config": {
            "retention_days": int(getattr(settings, "HERMES_RETENTION_DAYS", 90) or 90),
            "auto_ingest": bool(getattr(settings, "HERMES_AUTO_INGEST", True)),
            "cron_enabled": bool(getattr(settings, "HERMES_CRON_ENABLED", False)),
            "gateway_url": gateway_url(),
            "state_path": state_path(),
            "skills_path": skills_path(),
            "soul_path": getattr(settings, "SOUL_PATH", "SOUL.md"),
        },
        "stats": stats,
        "features": features,
        "agents": agents,
        "skills": list_skills(),
        "execution_allowed": execution_allowed,
        "profile": get_profile(),
        "profile_fragment": profile_prompt_fragment(),
        "soul": soul,
        "repo": repo_version(),
    }


@router.get("/search")
async def hermes_search(q: str = Query(..., min_length=2), symbol: Optional[str] = None, limit: int = Query(6, ge=1, le=20)):
    if not is_enabled():
        return {"enabled": False, "hits": []}
    # Prefer gateway search; fallback to local
    try:
        from app.hermes_bridge.memory_recall import _search_via_gateway
        hits = await _search_via_gateway(q, symbol or "", limit)
        if hits:
            return {"enabled": True, "hits": hits, "source": "gateway"}
    except Exception:
        pass
    return {"enabled": True, "hits": search_local(q, symbol, limit), "source": "local"}


@router.get("/skills")
async def hermes_skills() -> Dict[str, Any]:
    return {"enabled": is_enabled(), "skills": list_skills(), "execution_allowed": await is_execution_allowed()}


@router.get("/skills/{slug_or_symbol}")
async def hermes_skill_detail(slug_or_symbol: str) -> Dict[str, Any]:
    """Return one skill's SKILL.md + enriched metadata (for /hermes modal + /jarvis/skill)."""
    loaded = load_skill_md(slug_or_symbol)
    if not loaded:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"skill not found: {slug_or_symbol}")
    entry = loaded["entry"]
    return {
        "enabled": is_enabled(),
        "slug": entry["name"],
        "symbol": entry.get("symbol"),
        "asset_class": entry.get("asset_class"),
        "group": entry.get("group"),
        "linked_agents": entry.get("linked_agents", []),
        "jarvis": entry.get("jarvis"),
        "is_best_trader": entry.get("is_best_trader"),
        "meta": entry.get("meta", {}),
        "frontmatter": entry.get("frontmatter", {}),
        "md": loaded["md"],
        "path": loaded["path"],
    }


@router.post("/skills/{symbol}/evolve")
async def hermes_skill_evolve(symbol: str, force: bool = Query(False, description="Force even with <12 resolved")) -> Dict[str, Any]:
    """Evolve one best-trader skill's Learned block from its symbol's outcomes (B)."""
    if not is_enabled() and not force:
        return {"ok": False, "reason": "hermes disabled (pass force=true to override)"}
    # Gate evolution via RoomSettings as well? Stock skills are informational, but learned writes are allowed.
    try:
        from app.core.database import AsyncSessionLocal
        from app.hermes_bridge.skill_evolution import evolve_skill
        async with AsyncSessionLocal() as db:
            res = await evolve_skill(db, symbol, force=force)
            return {"ok": bool(res.get("changed")), **res}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:300], "symbol": symbol}


@router.get("/profile")
async def hermes_profile_get() -> Dict[str, Any]:
    return {"enabled": is_enabled(), "profile": get_profile(), "fragment": profile_prompt_fragment()}


class ProfilePatch(BaseModel):
    risk_pct: Optional[float] = None
    focus_symbol: Optional[str] = None
    focus_timeframe: Optional[str] = None
    preferred_pairs: Optional[List[str]] = None
    notes: Optional[str] = None


@router.put("/profile")
async def hermes_profile_put(patch: ProfilePatch) -> Dict[str, Any]:
    data = {k: v for k, v in patch.model_dump().items() if v is not None}
    profile = upsert_profile(data)
    return {"profile": profile}


@router.post("/prune")
async def hermes_prune() -> Dict[str, Any]:
    deleted = prune_expired()
    return {"deleted": deleted}
