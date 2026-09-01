"""
Hermes state store — episodic memory (FTS5 recall-only) + retention.

Delegates to NousResearch/hermes-agent sidecar when reachable (preferred),
falls back to local SQLite FTS5 at `HERMES_STATE_PATH`. Retention is
time-based (HERMES_RETENTION_DAYS=90, prune nightly); scoring stays on
Postgres AgentDecision — FTS5 is recall-only per locked plan.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.hermes_bridge import gateway_url, is_enabled, state_path


_FTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    session_id TEXT,
    content TEXT NOT NULL,
    meta_json TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS hermes_fts USING fts5(
    content, symbol, kind, tokenize='porter unicode61'
);
CREATE INDEX IF NOT EXISTS idx_hermes_episodes_ts ON hermes_episodes(ts);
CREATE INDEX IF NOT EXISTS idx_hermes_episodes_symbol ON hermes_episodes(symbol);
"""


def _db_path() -> Path:
    p = Path(state_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_FTS_SCHEMA)
    # Keep FTS in sync via trigger (insert-only; episodes are immutable)
    conn.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_hermes_fts_insert
    AFTER INSERT ON hermes_episodes BEGIN
        INSERT INTO hermes_fts(rowid, content, symbol, kind)
        VALUES (new.id, new.content, new.symbol, new.kind);
    END;
    """)


def ensure_store() -> Path:
    p = _db_path()
    try:
        conn = sqlite3.connect(str(p))
        try:
            _ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] state ensure failed: {exc}")
    return p


async def ingest_session(result: Dict[str, Any], consensus: Optional[Dict[str, Any]] = None) -> None:
    """Ingest a completed Trading Room session + JARVIS turn into episodic store."""
    if not is_enabled() or not getattr(settings, "HERMES_AUTO_INGEST", True):
        return
    try:
        symbol = (result.get("symbol") or "").upper()
        session_id = result.get("session_id") or result.get("run_id") or ""
        action = result.get("final_action") or result.get("action") or "hold"
        reasoning = (result.get("final_reasoning") or result.get("reasoning") or "")[:1200]
        consensus_s = json.dumps(consensus or {}, default=str)[:800]
        content = f"[{symbol} {action.upper()}] {reasoning} | consensus={consensus_s}"
        if not content.strip():
            return
        # Prefer sidecar ingest (so gateway FTS sees it); fall back to local
        if await _ingest_via_gateway(kind="session", symbol=symbol, session_id=session_id, content=content, meta=result):
            return
        _ingest_local(kind="session", symbol=symbol, session_id=session_id, content=content, meta=result)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] ingest_session skipped: {exc}")


async def ingest_jarvis_turn(symbol: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
    if not is_enabled() or not getattr(settings, "HERMES_AUTO_INGEST", True):
        return
    if not (content or "").strip():
        return
    try:
        if await _ingest_via_gateway(kind="jarvis", symbol=symbol.upper(), content=content[:2000], meta=meta or {}):
            return
        _ingest_local(kind="jarvis", symbol=symbol.upper(), content=content[:2000], meta=meta or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] ingest_jarvis skipped: {exc}")


def _ingest_local(kind: str, symbol: str, content: str, meta: Dict[str, Any], session_id: str = "") -> None:
    try:
        ensure_store()
        conn = sqlite3.connect(str(_db_path()))
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO hermes_episodes(ts, kind, symbol, session_id, content, meta_json) VALUES(?,?,?,?,?,?)",
                (time.time(), kind, symbol, session_id, content, json.dumps(meta, default=str)[:4000]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] local ingest failed: {exc}")


async def _ingest_via_gateway(kind: str, symbol: str, content: str, meta: Dict[str, Any], session_id: str = "") -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.post(
                f"{gateway_url()}/v1/hermes/ingest",
                json={"kind": kind, "symbol": symbol, "session_id": session_id, "content": content, "meta": meta},
            )
            return r.status_code in (200, 201, 202)
    except Exception:
        return False


def search_local(query: str, symbol: Optional[str] = None, limit: int = 6) -> List[Dict[str, Any]]:
    """Local FTS5 recall (used when sidecar unavailable)."""
    if not (query or "").strip():
        return []
    try:
        p = _db_path()
        if not p.exists():
            return []
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            # FTS5 query — escape quotes, add symbol boost
            q = query.replace('"', ' ').strip()[:200]
            # Escape FTS5 special chars; don't inject field filter via MATCH
            import re as _re
            q = _re.sub(r'[/:\-\*\(\)]', ' ', q).strip()
            q = ' '.join(q.split())  # collapse whitespace
            if not q:
                rows = []
            else:
                # Filter by symbol via column, not FTS field
                if symbol:
                    rows = conn.execute(
                        """
                        SELECT e.ts, e.kind, e.symbol, e.content, e.meta_json
                        FROM hermes_fts f JOIN hermes_episodes e ON e.id = f.rowid
                        WHERE hermes_fts MATCH ? AND e.symbol = ?
                        ORDER BY rank LIMIT ?
                        """,
                        (q, symbol.upper(), limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT e.ts, e.kind, e.symbol, e.content, e.meta_json
                        FROM hermes_fts f JOIN hermes_episodes e ON e.id = f.rowid
                        WHERE hermes_fts MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (q, limit),
                    ).fetchall()
            return [
                {"ts": r["ts"], "kind": r["kind"], "symbol": r["symbol"], "content": r["content"], "meta": r["meta_json"]}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] local search failed: {exc}")
        return []


def prune_expired() -> int:
    """Delete episodes older than HERMES_RETENTION_DAYS. Returns rows deleted."""
    try:
        days = int(getattr(settings, "HERMES_RETENTION_DAYS", 90) or 90)
        cutoff = time.time() - days * 86400
        p = _db_path()
        if not p.exists():
            return 0
        conn = sqlite3.connect(str(p))
        try:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM hermes_episodes WHERE ts < ?", (cutoff,))
            conn.commit()
            # Rebuild FTS to drop orphan rows
            conn.execute("INSERT INTO hermes_fts(hermes_fts) VALUES('rebuild')")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] prune failed: {exc}")
        return 0
