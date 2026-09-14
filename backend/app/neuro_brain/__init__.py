"""
Neuroglancer Core Brain — the federated brain that owns all other brains.

This package is side-effect free on import. All I/O via explicit calls.
Pattern mirrors app.hermes_bridge but federates: Hermes + Obsidian + AiMarketAnalyst
+ AgentDecision/JarvisJournal + CryptoPair into one recall surface.
"""
from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path as _P

from app.core.config import settings


def is_enabled() -> bool:
    return bool(getattr(settings, "NEUROGLANCER_ENABLED", False))


def local_path() -> str:
    raw = (getattr(settings, "NEUROGLANCER_LOCAL_PATH", "") or "").strip()
    if raw:
        return raw
    # default: project_integrations / neuroglancer
    return str(_P(__file__).resolve().parents[3] / "integrations" / "neuroglancer")


def state_path() -> str:
    raw = (getattr(settings, "NEUROGLANCER_STATE_PATH", "") or "").strip()
    if raw:
        p = _P(raw)
        return str(p if p.is_absolute() else _P(__file__).resolve().parents[3] / p)
    data_dir = (getattr(settings, "DATA_DIR", "") or "").strip()
    if data_dir:
        import os
        return os.path.join(data_dir, "neuro_state.db")
    # Absolute fallback so uvicorn (cwd=backend) and start.py (cwd=ROOT) share one file
    return str(_P(__file__).resolve().parents[3] / "data" / "neuro_state.db")


def repo_url() -> str:
    return (getattr(settings, "NEUROGLANCER_REPO_URL", "") or "https://github.com/google/neuroglancer.git").strip()


def repo_branch() -> str:
    return (getattr(settings, "NEUROGLANCER_BRANCH", "") or "master").strip()


def federation_enabled() -> bool:
    return bool(getattr(settings, "NEUROGLANCER_FEDERATION_ENABLED", True))


def repo_version() -> dict:
    """Version info for integrations/neuroglancer (mirrors hermes_bridge.repo_version)."""
    root = _P(__file__).resolve().parents[3]
    repo_dir = _P(local_path())
    if not repo_dir.is_absolute():
        repo_dir = root / repo_dir
    ver_file = root / "integrations" / "neuroglancer.version.json"
    out: dict = {
        "cloned": False,
        "commit": None,
        "branch": None,
        "remote": repo_url(),
        "path": str(repo_dir),
    }
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
        # package.json version
        pkg = repo_dir / "package.json"
        if pkg.exists():
            try:
                import re
                m = re.search(r'"version"\s*:\s*"([^"]+)"', pkg.read_text(encoding="utf-8"))
                if m:
                    out["pkg_version"] = m.group(1)
            except Exception:
                pass
        if ver_file.exists():
            try:
                jf = json.loads(ver_file.read_text(encoding="utf-8"))
                out["last_pull"] = jf.get("last_pull")
                out["last_pull_commit"] = jf.get("commit")
            except Exception:
                pass
        r4 = _sp.run(["git", "-C", str(repo_dir), "status", "--porcelain"], capture_output=True, text=True, timeout=5)
        if r4.returncode == 0:
            out["dirty"] = bool(r4.stdout.strip())
    except Exception:
        pass
    return out


__all__ = ["is_enabled", "local_path", "state_path", "repo_url", "repo_branch", "federation_enabled", "repo_version"]
