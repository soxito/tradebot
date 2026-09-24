#!/usr/bin/env python3
"""
Soup sidecar shim for TradeBot.

- Serves /health at 8012 (and 7860 if UI enabled) so start.py / backend can probe.
- Optionally launches `soup ui --host 0.0.0.0 --port 7860` as child.
- Optionally launches `soup serve` when SOUP_SERVE_MODEL is set.
- In Docker, the repo is baked at /app/soup; on host, integrations/soup is volume-mounted.
"""
import os, subprocess, sys, time, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

SOUP_PORT = int(os.environ.get("SOUP_PORT", "8012"))
SOUP_UI_PORT = int(os.environ.get("SOUP_UI_PORT", "7860"))
SOUP_REPO_DIR = Path(os.environ.get("SOUP_REPO_DIR", "/app/soup"))
SOUP_VENV = Path(os.environ.get("SOUP_VENV_DIR", str(Path.home() / ".tradebot" / "soup-venv")))
SOUP_BIN_CANDIDATES = [
    SOUP_VENV / "bin" / "soup",
    Path("/usr/local/bin/soup"),
    Path("/app/soup/.venv/bin/soup"),
]

def find_soup_bin():
    for p in SOUP_BIN_CANDIDATES:
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("soup") or "soup"

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/api/health", "/"):
            payload = {
                "status": "ok",
                "service": "soup",
                "ui_port": SOUP_UI_PORT,
                "api_port": SOUP_PORT,
                "repo": str(SOUP_REPO_DIR),
                "soup_bin": find_soup_bin(),
            }
            # Try to enrich with version
            try:
                import subprocess as sp
                r = sp.run([find_soup_bin(), "--version"], capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    payload["soup_version"] = (r.stdout or r.stderr).strip().splitlines()[0][:120]
            except Exception:
                pass
            # repo commit
            try:
                import subprocess as sp
                rr = sp.run(["git", "-C", str(SOUP_REPO_DIR), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
                if rr.returncode == 0:
                    payload["commit"] = rr.stdout.strip()
            except Exception:
                pass
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[soup-sidecar] {fmt % args}\n")

def maybe_start_ui():
    """Launch soup ui in background if not disabled."""
    if os.environ.get("SOUP_DISABLE_UI", "").lower() in ("1","true","yes","on"):
        return
    soup_bin = find_soup_bin()
    try:
        # Check if already listening
        import socket
        with socket.create_connection(("127.0.0.1", SOUP_UI_PORT), timeout=0.5):
            print(f"[soup-sidecar] UI already on :{SOUP_UI_PORT}", flush=True)
            return
    except Exception:
        pass
    cmd = [soup_bin, "ui", "--host", "0.0.0.0", "--port", str(SOUP_UI_PORT)]
    # Some soup versions require `soup ui` to be `soup-ui`; fallback
    print(f"[soup-sidecar] launching: {' '.join(cmd)}", flush=True)
    try:
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        print(f"[soup-sidecar] ui launch failed: {e}", flush=True)

if __name__ == "__main__":
    # Optionally auto-start UI
    threading.Thread(target=maybe_start_ui, daemon=True).start()
    # Also handle serve model if set
    serve_model = os.environ.get("SOUP_SERVE_MODEL", "").strip()
    if serve_model:
        def _serve():
            time.sleep(3)
            soup_bin = find_soup_bin()
            cmd = [soup_bin, "serve", "--model", serve_model, "--host", "0.0.0.0", "--port", str(SOUP_PORT)]
            print(f"[soup-sidecar] launching serve: {' '.join(cmd)}", flush=True)
            try:
                subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as e:
                print(f"[soup-sidecar] serve launch failed: {e}", flush=True)
        threading.Thread(target=_serve, daemon=True).start()

    addr = ("0.0.0.0", SOUP_PORT)
    print(f"[soup-sidecar] listening on http://{addr[0]}:{addr[1]}/health  (repo={SOUP_REPO_DIR})  ui=:{SOUP_UI_PORT}", flush=True)
    # If SOUP_PORT == SOUP_UI_PORT, just run one server; else health on API port is enough
    try:
        HTTPServer(addr, HealthHandler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
