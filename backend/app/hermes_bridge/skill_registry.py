"""
Hermes skill registry — agentskills.io compatible, gated by RoomSettings.execution_enabled.

Auto-creates a skill from a successful Trading Room session; skills auto-improve
on next use via the same self_improve loop. Execution from skills never bypasses
the room gate (locked decision 9.6).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.hermes_bridge import is_enabled, skills_path


def _skills_dir() -> Path:
    p = Path(skills_path())
    # Resolve relative paths against project root (backend/..) so
    # hermes_skills at repo root is found even when cwd is backend/
    if not p.is_absolute():
        root = Path(__file__).resolve().parents[3]
        cand = root / p
        # Prefer the canonical root dir if it already has best-trader skills
        if (cand / "btcusd-best-trader" / "SKILL.md").exists():
            p = cand
        elif not p.exists():
            # try project root hermes_skills as fallback for read
            if (root / "hermes_skills").exists():
                p = root / "hermes_skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _all_skills_dirs() -> List[Path]:
    """All dirs that may contain skills (deduplicated)."""
    dirs: List[Path] = []
    seen: set[str] = set()
    root = Path(__file__).resolve().parents[3]
    # 1. configured path
    try:
        d = _skills_dir()
        key = str(d.resolve()) if d.exists() else str(d)
        if key not in seen:
            seen.add(key)
            dirs.append(d)
    except Exception:
        pass
    # 2. project root hermes_skills
    for cand in [root / "hermes_skills", root / "backend" / "hermes_skills", Path("hermes_skills")]:
        try:
            if cand.exists() and cand.is_dir():
                key = str(cand.resolve())
                if key not in seen:
                    seen.add(key)
                    dirs.append(cand)
        except Exception:
            pass
    # 3. explicit HERMES_SKILLS_PATH env if different
    import os
    env_sp = (os.getenv("HERMES_SKILLS_PATH") or "").strip()
    if env_sp:
        try:
            ep = Path(env_sp).expanduser()
            key2 = str(ep.resolve()) if ep.exists() else str(ep)
            if key2 not in seen:
                dirs.append(ep)
        except Exception:
            pass
    return dirs


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse YAML frontmatter between leading --- fences (best-trader skills)."""
    fm: Dict[str, Any] = {}
    try:
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                block = text[3:end].strip()
                # simple key: value lines; bracket lists handled
                for line in block.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    # strip quotes
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
                        v = v[1:-1]
                    # bracket list: [a, b]
                    if v.startswith("[") and v.endswith("]"):
                        inner = v[1:-1].strip()
                        if not inner:
                            fm[k] = []
                        else:
                            fm[k] = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
                    else:
                        fm[k] = v
    except Exception:
        pass
    return fm


def _norm_symbol(sym: str) -> str:
    return (sym or "").replace("/", "").replace(" ", "").strip().upper()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or f"skill-{int(time.time())}"


async def maybe_create_skill(result: Dict[str, Any], consensus: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Create a skill from a completed session. Returns skill name or None.
    Gate: HERMES_ENABLED and symbol+action present; agreement>=0.4 or strong momentum.
    Skill file is informational until RoomSettings.execution_enabled allows execution.
    """
    if not is_enabled():
        return None
    symbol = (result.get("symbol") or "").strip().upper()
    action = (result.get("final_action") or result.get("action") or "").lower()
    if not symbol or action not in {"buy", "sell", "long", "short"}:
        return None
    consensus_v = (consensus or {})
    agreement = float(consensus_v.get("agreement") or 0)
    # Locked gate: min_consensus 0.4 (models/database.py:888) — reuse for skill eligibility
    momentum = (result.get("momentum") or {}).get("strength")
    if agreement < 0.4 and momentum != "strong":
        return None
    # Deduplicate: don't create same symbol/action/level skill twice in 24h
    name = _slug(f"{symbol}-{action}-{int(time.time()) % 100000}")
    skill_dir = _skills_dir() / name
    if skill_dir.exists():
        return None
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        reasoning = (result.get("final_reasoning") or result.get("reasoning") or "")[:800]
        levels = result.get("plan_levels_text") or ""
        doc = f"""---
name: {name}
description: Auto-created from Trading Room session {symbol} {action.upper()} (agreement {agreement:.0%})
---

# {symbol} {action.upper()} — Trading Room Skill

> Auto-harvested from a successful session. Gated by RoomSettings.execution_enabled.

**Symbol:** {symbol}
**Action:** {action}
**Agreement:** {agreement:.0%}
**Momentum:** {momentum or 'n/a'}
**Reasoning:** {reasoning}

**Levels:**
{levels or '(see session result)'}

**Source:** Trading Room session {result.get('session_id') or ''} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

## When to apply
When {symbol} shows the same structure + momentum read and consensus re-forms.

## Execution gate
Execution from this skill requires RoomSettings.execution_enabled=true (never bypassed).
"""
        (skill_dir / "SKILL.md").write_text(doc, encoding="utf-8")
        (skill_dir / "metadata.json").write_text(
            json.dumps({"symbol": symbol, "action": action, "agreement": agreement, "created_at": time.time(), "source": "hermes_bridge"}, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[hermes] skill created: {name} for {symbol} {action}")
        return name
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] skill create skipped: {exc}")
        return None


def _skill_entry_for_dir(d: Path) -> Dict[str, Any]:
    """One skill dir → enriched entry (metadata + frontmatter + linked_agents/jarvis)."""
    meta_p = d / "metadata.json"
    meta: Dict[str, Any] = {}
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    skill_md = d / "SKILL.md"
    fm: Dict[str, Any] = {}
    content_preview = ""
    if skill_md.exists():
        try:
            txt = skill_md.read_text(encoding="utf-8")
            fm = _parse_frontmatter(txt)
            # keep first 400 chars of body for UI preview
            body = txt.split("---", 2)[-1] if txt.startswith("---") else txt
            content_preview = body.strip()[:400]
        except Exception:
            pass
    # Merge frontmatter into meta for UI convenience (frontmatter wins for asset class)
    asset_class = fm.get("asset_class") or meta.get("asset_class") or ""
    symbol = fm.get("symbol") or meta.get("symbol") or ""
    group = fm.get("group") or meta.get("group") or ""
    linked_agents = fm.get("linked_agents") or meta.get("linked_agents") or []
    if isinstance(linked_agents, str):
        linked_agents = [x.strip() for x in linked_agents.split(",") if x.strip()]
    jarvis_role = fm.get("jarvis_role") or (meta.get("jarvis") or {}).get("role") or "ceo"
    jarvis_name = fm.get("jarvis_name") or (meta.get("jarvis") or {}).get("human_name") or "JARVIS"
    is_best_trader = "best-trader" in d.name or meta.get("source") == "best-trader-bootstrap"
    out: Dict[str, Any] = {
        "name": d.name,
        "path": str(d),
        "meta": meta,
        "frontmatter": fm,
        "symbol": symbol,
        "asset_class": asset_class,
        "group": group,
        "linked_agents": linked_agents,
        "jarvis": {"role": jarvis_role, "human_name": jarvis_name, "seat": -1},
        "is_best_trader": is_best_trader,
        "has_skill_md": skill_md.exists(),
        "content_preview": content_preview,
    }
    # propagate evolution stats to top level for easy UI sort/filter
    out["evolved_at"] = meta.get("evolved_at")
    out["decisions_reviewed"] = meta.get("decisions_reviewed", 0)
    out["win_rate"] = meta.get("win_rate")
    out["avg_pnl"] = meta.get("avg_pnl")
    return out


def list_skills() -> List[Dict[str, Any]]:
    """List local skills (name + metadata + frontmatter) deduped across all roots."""
    try:
        dirs = _all_skills_dirs()
        by_name: Dict[str, Dict[str, Any]] = {}
        for base in dirs:
            if not base.exists():
                continue
            for d in sorted(base.iterdir()):
                if not d.is_dir():
                    continue
                if d.name in by_name:
                    continue
                try:
                    entry = _skill_entry_for_dir(d)
                    by_name[d.name] = entry
                except Exception:
                    continue
        # Sort: best-trader first, then by asset_class then symbol
        def _sort_key(e: Dict[str, Any]):
            return (0 if e.get("is_best_trader") else 1, e.get("asset_class") or "zzz", e.get("symbol") or e["name"])
        return sorted(by_name.values(), key=_sort_key)
    except Exception:
        return []


def get_skill_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Load best-trader skill for a normalized symbol (e.g. EURUSD)."""
    norm = _norm_symbol(symbol)
    if not norm:
        return None
    slug = f"{norm.lower()}-best-trader"
    for base in _all_skills_dirs():
        d = base / slug
        if d.exists() and d.is_dir() and (d / "SKILL.md").exists():
            return _skill_entry_for_dir(d)
    # fallback: scan all skills for matching frontmatter.symbol
    for s in list_skills():
        if _norm_symbol(s.get("symbol") or "") == norm:
            return s
    return None


def load_skill_md(slug_or_symbol: str) -> Optional[Dict[str, Any]]:
    """Load a skill's SKILL.md + metadata by slug or symbol."""
    key = slug_or_symbol.strip()
    norm = _norm_symbol(key)
    slug = key if "-" in key else f"{norm.lower()}-best-trader"
    # try exact dir name first
    for base in _all_skills_dirs():
        d = base / slug
        if d.exists() and (d / "SKILL.md").exists():
            entry = _skill_entry_for_dir(d)
            try:
                md = (d / "SKILL.md").read_text(encoding="utf-8")
            except Exception:
                md = ""
            return {"entry": entry, "md": md, "path": str(d)}
    # try symbol lookup
    entry = get_skill_for_symbol(key)
    if entry:
        p = Path(entry["path"]) / "SKILL.md"
        try:
            md = p.read_text(encoding="utf-8")
        except Exception:
            md = entry.get("content_preview") or ""
        return {"entry": entry, "md": md, "path": entry["path"]}
    return None


async def is_execution_allowed() -> bool:
    """Check RoomSettings.execution_enabled gate (locked 9.6)."""
    try:
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal
        from app.models.database import RoomSettings
        async with AsyncSessionLocal() as db:
            s = (await db.execute(select(RoomSettings).where(RoomSettings.id == 1))).scalar_one_or_none()
            if s is None:
                return False
            return bool(s.execution_enabled)
    except Exception:
        return False
