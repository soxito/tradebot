"""
Neuroglancer Core Brain — episodic + trade-segment state store.

Single SQLite file (data/neuro_state.db) with two concerns:
  1. neuro_episodes — federated recall mirror (ingests from every brain).
  2. neuro_trade_segments — per-pair trade recording + analysis (all pairs, all classes).

Mirrors hermes_bridge/state_store.py FTS5 pattern but with broader kind taxonomy.
"""
from __future__ import annotations

import json
import sqlite3
import time
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.neuro_brain import is_enabled, state_path

_NEURO_SCHEMA = """
CREATE TABLE IF NOT EXISTS neuro_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    asset_class TEXT,
    session_id TEXT,
    source TEXT,
    content TEXT NOT NULL,
    meta_json TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS neuro_fts USING fts5(
    content, symbol, kind, tokenize='porter unicode61'
);
CREATE INDEX IF NOT EXISTS idx_neuro_episodes_ts ON neuro_episodes(ts);
CREATE INDEX IF NOT EXISTS idx_neuro_episodes_symbol ON neuro_episodes(symbol);
CREATE INDEX IF NOT EXISTS idx_neuro_episodes_kind ON neuro_episodes(kind);
CREATE INDEX IF NOT EXISTS idx_neuro_episodes_source ON neuro_episodes(source);

CREATE TABLE IF NOT EXISTS neuro_trade_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    exchange TEXT,
    side TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    confidence REAL,
    price_at_signal REAL,
    timeframe TEXT,
    source TEXT,
    agent_role TEXT,
    outcome TEXT,
    outcome_pnl REAL,
    settled_at REAL,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_neuro_trade_symbol ON neuro_trade_segments(symbol);
CREATE INDEX IF NOT EXISTS idx_neuro_trade_outcome ON neuro_trade_segments(outcome);
CREATE INDEX IF NOT EXISTS idx_neuro_trade_ts ON neuro_trade_segments(ts);
"""

def _db_path() -> Path:
    p = Path(state_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_NEURO_SCHEMA)
    conn.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_neuro_fts_insert
    AFTER INSERT ON neuro_episodes BEGIN
        INSERT INTO neuro_fts(rowid, content, symbol, kind)
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
    except Exception as exc:
        logger.debug(f"[neuro] state ensure failed: {exc}")
    return p

# ── Episodic ingest (federated mirror) ────────────────────────────────────
def ingest_local(
    kind: str,
    content: str,
    symbol: str = "",
    asset_class: str = "",
    session_id: str = "",
    source: str = "neuro",
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    if not is_enabled():
        return False
    if not (content or "").strip():
        return False
    try:
        ensure_store()
        conn = sqlite3.connect(str(_db_path()))
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO neuro_episodes(ts, kind, symbol, asset_class, session_id, source, content, meta_json) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), kind, (symbol or "").upper() or None, asset_class or None, session_id or None, source, content[:3000], json.dumps(meta or {}, default=str)[:5000]),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[neuro] ingest_local failed: {exc}")
        return False

async def ingest_episode(kind: str, content: str, symbol: str = "", asset_class: str = "", session_id: str = "", source: str = "neuro", meta: Optional[Dict[str, Any]] = None) -> None:
    # run off event loop
    await asyncio.to_thread(ingest_local, kind, content, symbol, asset_class, session_id, source, meta)

def search_local(query: str, symbol: Optional[str] = None, limit: int = 8) -> List[Dict[str, Any]]:
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
            import re as _re
            q = _re.sub(r'[/:\-\*\(\)"\']', ' ', query.replace('"', ' ')).strip()[:200]
            q = ' '.join(q.split())
            if not q:
                return []
            if symbol:
                rows = conn.execute(
                    "SELECT e.ts, e.kind, e.symbol, e.asset_class, e.source, e.content FROM neuro_fts f JOIN neuro_episodes e ON e.id=f.rowid WHERE neuro_fts MATCH ? AND e.symbol=? ORDER BY rank LIMIT ?",
                    (q, symbol.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT e.ts, e.kind, e.symbol, e.asset_class, e.source, e.content FROM neuro_fts f JOIN neuro_episodes e ON e.id=f.rowid WHERE neuro_fts MATCH ? ORDER BY rank LIMIT ?",
                    (q, limit),
                ).fetchall()
            return [{"ts": r["ts"], "kind": r["kind"], "symbol": r["symbol"], "asset_class": r["asset_class"], "source": r["source"], "content": r["content"]} for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[neuro] search_local failed: {exc}")
        return []

def stats() -> Dict[str, Any]:
    p = _db_path()
    episodes_total = episodes_24h = segments_total = segments_pending = 0
    fts_kb = 0
    last_ts: Optional[float] = None
    by_kind: Dict[str, int] = {}
    try:
        ensure_store()
        if p.exists():
            fts_kb = round(p.stat().st_size / 1024, 1)
            conn = sqlite3.connect(str(p))
            try:
                cur = conn.execute("SELECT COUNT(*) FROM neuro_episodes"); episodes_total = int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT COUNT(*) FROM neuro_episodes WHERE ts > ?", (time.time()-86400,)); episodes_24h = int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT MAX(ts) FROM neuro_episodes"); v=cur.fetchone()[0]; last_ts=float(v) if v else None
                cur = conn.execute("SELECT kind, COUNT(*) FROM neuro_episodes GROUP BY kind")
                for k,c in cur.fetchall(): by_kind[str(k)]=int(c)
                cur = conn.execute("SELECT COUNT(*) FROM neuro_trade_segments"); segments_total=int(cur.fetchone()[0] or 0)
                cur = conn.execute("SELECT COUNT(*) FROM neuro_trade_segments WHERE outcome IS NULL"); segments_pending=int(cur.fetchone()[0] or 0)
            finally:
                conn.close()
    except Exception:
        pass
    return {"episodes_total": episodes_total, "episodes_24h": episodes_24h, "last_ts": last_ts, "fts_kb": fts_kb, "db_path": str(p), "by_kind": by_kind, "segments_total": segments_total, "segments_pending": segments_pending}

def prune_expired(days: int = 90) -> int:
    try:
        cutoff=time.time()-days*86400; p=_db_path()
        if not p.exists(): return 0
        conn=sqlite3.connect(str(p))
        try:
            _ensure_schema(conn)
            cur=conn.execute("DELETE FROM neuro_episodes WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.execute("INSERT INTO neuro_fts(neuro_fts) VALUES('rebuild')"); conn.commit()
            return cur.rowcount
        finally: conn.close()
    except Exception: return 0

# ── Trade segments (record + analyse all pairs) ───────────────────────────
def record_trade_segment(
    symbol: str,
    side: str,
    entry: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    confidence: Optional[float] = None,
    price_at_signal: Optional[float] = None,
    timeframe: str = "",
    asset_class: str = "",
    exchange: str = "",
    source: str = "neuro",
    agent_role: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    if not is_enabled():
        return None
    try:
        ensure_store()
        conn=sqlite3.connect(str(_db_path()))
        try:
            _ensure_schema(conn)
            cur=conn.execute(
                "INSERT INTO neuro_trade_segments(ts, symbol, asset_class, exchange, side, entry, stop_loss, take_profit, confidence, price_at_signal, timeframe, source, agent_role, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), symbol.upper(), asset_class or None, exchange or None, side, float(entry) if entry else None, float(stop_loss) if stop_loss else None, float(take_profit) if take_profit else None, float(confidence) if confidence else None, float(price_at_signal) if price_at_signal else None, timeframe or None, source, agent_role or None, json.dumps(meta or {}, default=str)[:4000]),
            )
            conn.commit()
            seg_id=int(cur.lastrowid)
            # also create an episode so federated search finds it
            ingest_local(kind="trade_segment", content=f"[{symbol.upper()} {side.upper()} {entry}->{take_profit} SL {stop_loss}] conf={confidence} tf={timeframe}", symbol=symbol, asset_class=asset_class, source=source, meta={"segment_id": seg_id, **(meta or {})})
            return seg_id
        finally: conn.close()
    except Exception as exc:
        logger.debug(f"[neuro] record_trade_segment failed: {exc}")
        return None

def list_segments(limit: int = 50, symbol: Optional[str] = None, outcome: Optional[str] = None, asset_class: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        p=_db_path()
        if not p.exists(): return []
        conn=sqlite3.connect(str(p)); conn.row_factory=sqlite3.Row
        try:
            _ensure_schema(conn)
            q="SELECT * FROM neuro_trade_segments WHERE 1=1"
            params=[]
            if symbol: q+=" AND symbol=?"; params.append(symbol.upper())
            if outcome: q+=" AND outcome=?"; params.append(outcome)
            if asset_class: q+=" AND asset_class=?"; params.append(asset_class)
            q+=" ORDER BY ts DESC LIMIT ?"; params.append(limit)
            rows=conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        finally: conn.close()
    except Exception: return []

def analysis_summary(limit: int=200) -> Dict[str, Any]:
    segs=list_segments(limit=limit)
    total=len(segs)
    wins=len([s for s in segs if s.get("outcome")=="win"])
    losses=len([s for s in segs if s.get("outcome")=="loss"])
    pending=len([s for s in segs if not s.get("outcome")])
    win_rate=round(wins/max(1,wins+losses),3) if (wins+losses)>0 else None
    # by pair
    by_symbol: Dict[str, Dict[str,int]]={}
    for s in segs:
        sym=s.get("symbol") or "unknown"; d=by_symbol.setdefault(sym, {"total":0,"wins":0,"losses":0,"pending":0})
        d["total"]+=1
        if s.get("outcome")=="win": d["wins"]+=1
        elif s.get("outcome")=="loss": d["losses"]+=1
        else: d["pending"]+=1
    by_asset: Dict[str,int]={}
    for s in segs: by_asset[s.get("asset_class") or "unknown"]=by_asset.get(s.get("asset_class") or "unknown",0)+1
    recent=segs[:10]
    return {"total":total,"wins":wins,"losses":losses,"pending":pending,"win_rate":win_rate,"by_symbol":by_symbol,"by_asset_class":by_asset,"recent":recent}

def mark_outcome(segment_id: int, outcome: str, pnl: Optional[float]=None) -> bool:
    try:
        conn=sqlite3.connect(str(_db_path()))
        try:
            _ensure_schema(conn)
            conn.execute("UPDATE neuro_trade_segments SET outcome=?, outcome_pnl=?, settled_at=? WHERE id=?", (outcome, pnl, time.time(), segment_id))
            conn.commit()
            return conn.total_changes>0
        finally: conn.close()
    except Exception: return False
