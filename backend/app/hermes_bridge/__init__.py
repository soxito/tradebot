"""
Hermes Bridge — self-aware upgrade for JARVIS / Paul / S.O.X + Trading Room.

Locked scope (Phase 0): episodic + skill + user-model via isolated sidecar
on 8011 (like TradingAgents on 8010). Single SOUL.md. Gateway cutover
immediate. Reuses AiMarketAnalyst pool. Skill execution gated by
RoomSettings.execution_enabled. 90d retention, recall-only.

This package is intentionally side-effect free on import — no I/O, no
sidecar spawn — so `HERMES_ENABLED=false` costs nothing.
"""
from __future__ import annotations

from app.core.config import settings


def is_enabled() -> bool:
    return bool(getattr(settings, "HERMES_ENABLED", False))


def gateway_url() -> str:
    return (getattr(settings, "HERMES_GATEWAY_URL", "") or "").rstrip("/") or "http://127.0.0.1:8011"


def state_path() -> str:
    raw = (getattr(settings, "HERMES_STATE_PATH", "") or "").strip()
    if raw:
        return raw
    data_dir = (getattr(settings, "DATA_DIR", "") or "").strip()
    if data_dir:
        import os
        return os.path.join(data_dir, "hermes_state.db")
    return "data/hermes_state.db"


def skills_path() -> str:
    raw = (getattr(settings, "HERMES_SKILLS_PATH", "") or "").strip()
    if raw:
        return raw
    data_dir = (getattr(settings, "DATA_DIR", "") or "").strip()
    if data_dir:
        import os
        return os.path.join(data_dir, "hermes_skills")
    return "hermes_skills"


def repo_version() -> dict:
    """Repo version from integrations/hermes-agent (cloned by start.py)."""
    import json as _json
    import subprocess as _sp
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[3]
    repo_dir = root / "integrations" / "hermes-agent"
    ver_file = root / "integrations" / "hermes-agent.version.json"
    out: dict = {"cloned": False, "commit": None, "branch": None, "remote": "https://github.com/NousResearch/hermes-agent.git", "path": str(repo_dir)}
    if not repo_dir.exists():
        return out
    out["cloned"] = True
    try:
        r = _sp.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            out["commit"] = r.stdout.strip()[:12]
            out["commit_full"] = r.stdout.strip()
        r2 = _sp.run(["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5)
        if r2.returncode == 0:
            out["branch"] = r2.stdout.strip()
        r3 = _sp.run(["git", "-C", str(repo_dir), "log", "-1", "--format=%ci"], capture_output=True, text=True, timeout=5)
        if r3.returncode == 0:
            out["commit_date"] = r3.stdout.strip()
        # package version
        for cand in [repo_dir / "pyproject.toml", repo_dir / "package.json"]:
            if cand.exists():
                try:
                    import re
                    m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', cand.read_text(encoding="utf-8"))
                    if m:
                        out["pkg_version"] = m.group(1)
                        break
                except Exception:
                    pass
        if ver_file.exists():
            try:
                jf = _json.loads(ver_file.read_text(encoding="utf-8"))
                out["last_pull"] = jf.get("last_pull")
                out["last_pull_commit"] = jf.get("commit")
            except Exception:
                pass
        # dirty check
        r4 = _sp.run(["git", "-C", str(repo_dir), "status", "--porcelain"], capture_output=True, text=True, timeout=5)
        if r4.returncode == 0:
            out["dirty"] = bool(r4.stdout.strip())
    except Exception:
        pass
    return out


__all__ = ["is_enabled", "gateway_url", "state_path", "skills_path", "repo_version"]
