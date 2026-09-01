"""
Thin Hermes sidecar shim for TradeBot backend proxy.

Exposes POST /v1/hermes/ingest, GET /v1/hermes/search, POST /v1/hermes/chat
so backend/app/hermes_bridge can talk to Hermes even before full gateway wiring.

If hermes-agent imports fail, falls back to local SQLite FTS5 at /data/hermes/hermes_state.db.
"""
import os
import json
import sqlite3
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

def _resolve_data_dir() -> Path:
    raw = os.environ.get("HERMES_DATA_DIR", "").strip()
    if raw:
        p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    # Local dev: use project data/hermes, docker: /data/hermes
    for cand in [Path.cwd() / "data" / "hermes", Path(__file__).resolve().parents[1] / "data" / "hermes", Path("/data/hermes")]:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            # test writable
            if os.access(cand, os.W_OK):
                return cand
        except Exception:
            continue
    # last resort: temp
    import tempfile
    return Path(tempfile.gettempdir()) / "hermes"

DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "hermes_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT, symbol TEXT, session_id TEXT, content TEXT, meta_json TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS hermes_fts USING fts5(content, symbol, kind, tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS trg_hermes_fts_insert AFTER INSERT ON hermes_episodes BEGIN INSERT INTO hermes_fts(rowid, content, symbol, kind) VALUES (new.id, new.content, new.symbol, new.kind); END;
"""

def ensure():
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def _repo_info(self):
        # Best-effort: read git commit from integrations/hermes-agent or HERMES_REPO_DIR
        import subprocess
        for cand in [os.environ.get("HERMES_REPO_DIR", ""), str(Path(__file__).resolve().parents[1] / "integrations" / "hermes-agent")]:
            if not cand: continue
            pp = Path(cand)
            if (pp / ".git").exists():
                try:
                    r = subprocess.run(["git", "-C", str(pp), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
                    commit = r.stdout.strip() if r.returncode==0 else None
                    r2 = subprocess.run(["git", "-C", str(pp), "log", "-1", "--format=%ci"], capture_output=True, text=True, timeout=3)
                    date = r2.stdout.strip() if r2.returncode==0 else None
                    return {"cloned": True, "commit": commit, "date": date, "path": str(pp)}
                except Exception:
                    pass
        return {"cloned": False, "path": None}

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/health", "/v1/hermes/health"):
            ensure()
            info = self._repo_info()
            self.send(200, {"ok": True, "db": str(DB_PATH), "repo": info})
            return
        if p.path in ("/version", "/v1/hermes/repo", "/v1/hermes/version"):
            self.send(200, self._repo_info())
            return
        if p.path == "/v1/hermes/search":
            qs = parse_qs(p.query)
            q = (qs.get("q", [""])[0] or "")[:200]
            symbol = (qs.get("symbol", [""])[0] or "")[:20]
            limit = int((qs.get("limit", ["6"])[0] or 6))
            ensure()
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA)
                if q:
                    q_fts = q.replace('"',' ') # simple
                    if symbol:
                        q_fts = f"{q_fts} symbol:{symbol.upper()}"
                    rows = conn.execute("SELECT e.ts, e.kind, e.symbol, e.content FROM hermes_fts f JOIN hermes_episodes e ON e.id=f.rowid WHERE hermes_fts MATCH ? ORDER BY rank LIMIT ?", (q_fts, limit)).fetchall()
                    hits = [{"ts": r["ts"],"kind": r["kind"],"symbol": r["symbol"],"content": r["content"]} for r in rows]
                else:
                    hits=[]
                self.send(200, {"hits": hits})
            finally:
                conn.close()
            return
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        if p.path == "/v1/hermes/ingest":
            ensure()
            conn = sqlite3.connect(str(DB_PATH))
            try:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO hermes_episodes(ts,kind,symbol,session_id,content,meta_json) VALUES(?,?,?,?,?,?)",
                             (time.time(), data.get("kind","session"), (data.get("symbol") or "").upper(), data.get("session_id",""), (data.get("content") or "")[:2000], json.dumps(data.get("meta") or {}, default=str)[:4000]))
                conn.commit()
                self.send(200, {"ok": True})
            finally:
                conn.close()
            return
        if p.path == "/v1/hermes/chat":
            # Minimal echo — full gateway will handle chat via hermes TUI/gateway
            self.send(200, {"ok": True, "reply": f"[hermes sidecar] received {len(data.get('text') or '')} chars. Full gateway chat via `hermes gateway`."})
            return
        if p.path == "/v1/hermes/session_completed":
            ensure()
            result = (data.get("result") or {})
            content = f"[{result.get('symbol','')} {result.get('final_action','')}] {(result.get('final_reasoning') or '')[:800]}"
            conn = sqlite3.connect(str(DB_PATH))
            try:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO hermes_episodes(ts,kind,symbol,content,meta_json) VALUES(?,?,?, ?,?)",
                             (time.time(), "session", (result.get("symbol") or "").upper(), content, json.dumps(data, default=str)[:4000]))
                conn.commit()
            finally:
                conn.close()
            self.send(200, {"ok": True})
            return
        self.send(404, {"error": "not found"})

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass

if __name__ == "__main__":
    ensure()
    port = int(os.environ.get("HERMES_PORT","8011"))
    print(f"[hermes sidecar] listening on :{port} db={DB_PATH}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
