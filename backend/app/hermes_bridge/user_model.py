"""
Hermes user model — lightweight trader profile (episodic+skill+user-model scope).

No Honcho dialectic, no periodic nudges (locked 9.2). Injected into specialist
prompts via with_completeness wrapper.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.hermes_bridge import is_enabled, state_path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_user_profile (
    id INTEGER PRIMARY KEY CHECK (id=1),
    updated_at REAL NOT NULL,
    profile_json TEXT NOT NULL
);
"""


def _db_path() -> Path:
    p = Path(state_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def get_profile() -> Dict[str, Any]:
    try:
        p = _db_path()
        if not p.exists():
            return {}
        conn = sqlite3.connect(str(p))
        try:
            _ensure(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT profile_json FROM hermes_user_profile WHERE id=1").fetchone()
            if not row:
                return {}
            return json.loads(row["profile_json"] or "{}")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] get_profile skipped: {exc}")
        return {}


def upsert_profile(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge patch into stored profile. No LLM, no nudge."""
    if not is_enabled():
        # Still allow read, but don't write when disabled
        return get_profile()
    try:
        current = get_profile()
        merged = {**current, **patch}
        # Keep it small
        if len(json.dumps(merged)) > 8000:
            # Drop oldest keys (heuristic: keep risk/focus keys)
            for k in list(merged.keys()):
                if k not in {"risk_pct", "focus_symbol", "focus_timeframe", "preferred_pairs", "session_hours"} and len(json.dumps(merged)) > 6000:
                    merged.pop(k, None)
                else:
                    break
        conn = sqlite3.connect(str(_db_path()))
        try:
            _ensure(conn)
            conn.execute(
                "INSERT OR REPLACE INTO hermes_user_profile(id, updated_at, profile_json) VALUES(1,?,?)",
                (time.time(), json.dumps(merged, default=str)),
            )
            conn.commit()
        finally:
            conn.close()
        return merged
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] upsert_profile skipped: {exc}")
        return get_profile()


def profile_prompt_fragment(max_chars: int = 400) -> str:
    """Compact fragment to append to specialist prompts."""
    prof = get_profile()
    if not prof:
        return ""
    bits: list[str] = []
    if prof.get("risk_pct"):
        bits.append(f"Trader risk: {prof['risk_pct']}% per trade")
    if prof.get("focus_symbol"):
        bits.append(f"Focus: {prof['focus_symbol']}")
    if prof.get("preferred_pairs"):
        try:
            pairs = prof["preferred_pairs"]
            if isinstance(pairs, list) and pairs:
                bits.append(f"Preferred: {', '.join(str(x) for x in pairs[:4])}")
        except Exception:
            pass
    if prof.get("notes"):
        bits.append(str(prof["notes"])[:120])
    if not bits:
        return ""
    txt = " | ".join(bits)
    return f"\n[TRADER PROFILE] {txt[:max_chars]}\n"
