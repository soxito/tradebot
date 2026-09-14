"""Soup plugin — FastAPI router mounted at /api/v1/plugins/soup."""
from __future__ import annotations
import os, json, subprocess, shutil
from pathlib import Path
from typing import Optional, List, Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from pydantic import BaseModel

from plugins.SoupPlugin.backend.schemas import SoupStartRequest
from plugins.SoupPlugin.backend.services.soup_utils import (
    ROOT, SOUP_REPO_DIR, SOUP_VENV_BIN, SOUP_BIN, SOUP_UI_PORT, SOUP_PORT,
    soup_setup_command, is_repo_cloned, is_installed, soup_bin_path, repo_version, remote_commit, recommendation,
    kronos_models_agg, list_installed_models, SOUP_DATA_DIR,
)
from plugins.SoupPlugin.backend.services.soup_service import soup_manager

router = APIRouter(prefix="/plugins/soup", tags=["soup"])

# ── Status / version ────────────────────────────────────────────────────────
@router.get("/status")
async def status():
    ver = repo_version()
    rec = recommendation()
    remote = remote_commit()
    local_commit = (ver.get("commit") or "")
    upd = bool(remote and local_commit and remote[:7] != local_commit[:7])
    kronos = kronos_models_agg()
    models = list_installed_models()
    sb = soup_bin_path()
    # also enumerate kronos finetuned jobs for unified model view (6.2)
    kronos_finetuned: List[Dict[str,Any]] = []
    try:
        from plugins.KronosForecastPlugin.backend.services.finetune_service import finetune_manager as kfm  # type: ignore
        kjobs = await kfm.list(limit=50)  # type: ignore
        for j in kjobs:
            if (j.get("status") == "completed"):
                kronos_finetuned.append({"id": f"finetuned:{j['id']}", "label": f"Kronos finetuned #{j['id']} ({j.get('symbol')} {j.get('timeframe')})", "job": j})
    except Exception:
        pass
    return {
        "installed": is_installed(),
        "soup_bin_exists": bool(sb and Path(sb).exists()),
        "soup_bin": sb,
        "repo_cloned": is_repo_cloned(),
        "version": ver,
        "recommendation": rec,
        "remote_commit": remote,
        "update_available": upd,
        "setup_command": soup_setup_command(),
        "models": models,
        "kronos_models": kronos.get("catalogue", []),
        "kronos_finetuned": kronos_finetuned,
        "kronos_vendor_finetune_exists": kronos.get("vendor_finetune_exists", False),
        "soup_data_dir": str(SOUP_DATA_DIR),
        "ui_url": f"http://127.0.0.1:{SOUP_UI_PORT}",
        "api_url": f"http://127.0.0.1:{SOUP_PORT}",
    }

@router.get("/models")
async def models():
    """Unified model inventory (Kronos + Soup + finetuned). See 6.2."""
    ver = repo_version(); kronos = kronos_models_agg(); inst = list_installed_models()
    kronos_finetuned = []
    try:
        from plugins.KronosForecastPlugin.backend.services.finetune_service import finetune_manager as kfm
        for j in await kfm.list(limit=100):
            if j.get("status") == "completed":
                kronos_finetuned.append({"id": f"finetuned:{j['id']}", "label": f"Kronos finetuned #{j['id']} ({j['symbol']} {j['timeframe']})", "installed": True, "source": "kronos_finetune", "job": j})
    except Exception:
        pass
    return {"kronos": kronos.get("catalogue", []), "kronos_finetuned": kronos_finetuned, "installed": inst, "version": ver, "recommendation": recommendation()}

@router.get("/recipes")
async def recipes():
    """Soup training recipes/templates (mirrors `soup recipes list`)."""
    # Static list mirroring Soup docs; dynamic fetch if binary available
    static = ["chat","code","tool-calling","medical","reasoning","vision","kto","orpo","simpo","ipo","bco","rlhf","pretrain","moe","longcontext","embedding","audio","tts","unlearning","raft","classification"]
    sb = soup_bin_path()
    if sb and Path(sb).exists():
        try:
            proc = subprocess.run([sb, "recipes", "list"], capture_output=True, text=True, timeout=8)
            if proc.returncode == 0 and proc.stdout.strip():
                # parse json or lines
                try:
                    j = json.loads(proc.stdout)
                    if isinstance(j, list):
                        return {"recipes": j}
                except Exception:
                    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
                    if lines: return {"recipes": lines}
        except Exception:
            pass
    return {"recipes": static}

# ── Finetune CRUD ──────────────────────────────────────────────────────────
@router.post("/finetune/start")
async def finetune_start(req: SoupStartRequest):
    try:
        row = await soup_manager.start(req)
        return row
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/finetune/jobs")
async def finetune_jobs(limit: int = 50):
    return {"jobs": await soup_manager.list(limit=limit)}

@router.get("/finetune/jobs/{job_id}")
async def finetune_job(job_id: int, log_lines: int = Query(50)):
    row = await soup_manager.get(job_id, log_lines=log_lines)
    if not row: raise HTTPException(404, f"job {job_id} not found")
    return row

@router.delete("/finetune/jobs/{job_id}")
async def finetune_cancel(job_id: int, delete: bool = Query(False, description="if true, delete checkpoints + row; else cancel")):
    if delete:
        return await soup_manager.delete(job_id)
    await soup_manager.cancel(job_id)
    return {"ok": True, "job_id": job_id, "status": "cancelled"}

@router.post("/finetune/cancel/{job_id}")
async def finetune_cancel_post(job_id: int):
    await soup_manager.cancel(job_id)
    return {"ok": True}

# ── Dataset upload (6.2/6.3) ───────────────────────────────────────────────
@router.post("/data/upload")
async def data_upload(request: Request):
    """Accept JSONL/JSON/CSV upload (multipart or json). Returns stored path."""
    SOUP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ctype = request.headers.get("content-type","")
    if "application/json" in ctype:
        try:
            body = await request.json()
            samples = body.get("samples") or body.get("data") or body
            if isinstance(samples, dict): samples = [samples]
            if not isinstance(samples, list): raise ValueError("samples must be list")
            out = SOUP_DATA_DIR / "uploaded.jsonl"
            with out.open("w") as f:
                for s in samples:
                    f.write(json.dumps(s, ensure_ascii=False)+"\n")
            return {"ok": True, "path": str(out), "rows": len(samples), "format": "alpaca"}
        except Exception as e:
            raise HTTPException(400, detail=str(e))
    # multipart
    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(400, detail="no file")
        content = await file.read()  # type: ignore
        name = getattr(file, "filename", "uploaded.jsonl") or "uploaded.jsonl"
        out = SOUP_DATA_DIR / Path(name).name
        out.write_bytes(content)
        lines = 0
        try:
            if out.suffix == ".jsonl":
                lines = sum(1 for _ in out.open())
            elif out.suffix == ".csv":
                import csv
                lines = max(0, sum(1 for _ in out.open()) - 1)
        except Exception:
            pass
        return {"ok": True, "path": str(out), "rows": lines, "filename": name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=str(e))

# ── OHLCV experimental bridge ─────────────────────────────────────────────
@router.post("/ohlcv/export")
async def ohlcv_export(payload: dict):
    symbol = payload.get("symbol","BTC/USD")
    timeframe = payload.get("timeframe","1h")
    limit = int(payload.get("limit", 2000))
    try:
        from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl
        SOUP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = SOUP_DATA_DIR / f"ohlcv_{symbol.replace('/','_')}_{timeframe}.jsonl"
        res = await export_ohlcv_to_jsonl(symbol, timeframe, limit, out)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

@router.get("/ohlcv/preview")
async def ohlcv_preview(symbol: str = Query("BTC/USD"), timeframe: str = Query("1h"), limit: int = Query(200)):
    from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import preview_ohlcv
    res = await preview_ohlcv(symbol, timeframe, limit)
    if not res.get("ok"):
        raise HTTPException(400, detail=res.get("error"))
    return res

# ── UI proxy (proxied auth, token never leaks to browser — 6.5) ────────────
@router.api_route("/ui/proxy", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
@router.api_route("/ui/proxy/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
async def ui_proxy(request: Request, path: str = ""):
    """Proxy to Soup Gradio UI on :7860 with server-side auth injection."""
    import httpx
    ui_base = f"http://127.0.0.1:{SOUP_UI_PORT}"
    url = f"{ui_base}/{path}" if path else ui_base + "/"
    # forward query string
    if request.url.query:
        url += ("&" if "?" in url else "?") + request.url.query
    # build headers, inject soup token if present in env (SOUP_UI_TOKEN etc.)
    headers = {}
    for k,v in request.headers.items():
        lk = k.lower()
        if lk in ("host","content-length","connection"): continue
        headers[k] = v
    # Server-side token: check SOUP_UI_TOKEN, GRADIO_TOKEN, etc.
    token = os.environ.get("SOUP_UI_TOKEN") or os.environ.get("GRADIO_TOKEN") or os.environ.get("SOUP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.request(request.method, url, headers=headers, content=body)
            # Filter hop-by-hop
            h = {k:v for k,v in resp.headers.items() if k.lower() not in ("connection","keep-alive","transfer-encoding","content-encoding","content-length")}
            return Response(content=resp.content, status_code=resp.status_code, headers=h, media_type=resp.headers.get("content-type"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Soup UI proxy error: {e}")

@router.get("/ui/status")
async def ui_status():
    """Report whether Soup UI is reachable (proxied health)."""
    import httpx, socket
    # port probe
    try:
        with socket.create_connection(("127.0.0.1", SOUP_UI_PORT), timeout=1.0):
            port_open = True
    except Exception:
        port_open = False
    health = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"http://127.0.0.1:{SOUP_UI_PORT}/", follow_redirects=True)
            health = {"status_code": r.status_code, "ok": r.status_code < 400}
    except Exception as e:
        health = {"error": str(e)}
    # also check soup api/health sidecar on :8012
    api_health = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"http://127.0.0.1:{SOUP_PORT}/health")
            api_health = r.json() if r.headers.get("content-type","").startswith("application/json") else {"status_code": r.status_code}
    except Exception as e:
        api_health = {"error": str(e)}
    return {"ui_port": SOUP_UI_PORT, "api_port": SOUP_PORT, "port_open": port_open, "ui_health": health, "api_health": api_health, "proxy_path": "/api/v1/plugins/soup/ui/proxy"}

# ── Serve / health convenience ────────────────────────────────────────────
@router.get("/health")
async def health():
    sb = soup_bin_path()
    ver = repo_version()
    return {"ok": True, "service": "soup", "installed": is_installed(), "repo_cloned": is_repo_cloned(), "soup_bin": sb, "version": ver, "ui_port": SOUP_UI_PORT, "api_port": SOUP_PORT}

@router.post("/serve/start")
async def serve_start(payload: dict):
    """Start `soup serve --model <path>` detached (outputs to soup-serve.log)."""
    model = (payload.get("model") or payload.get("path") or "").strip()
    if not model: raise HTTPException(400, detail="model path required")
    sb = soup_bin_path()
    if not sb: raise HTTPException(400, detail=f"soup not installed: {soup_setup_command()}")
    import subprocess, time as _t
    log = ROOT / "soup-serve.log"
    cmd = [sb, "serve", "--model", model, "--host", "0.0.0.0", "--port", str(SOUP_PORT)]
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(ROOT))
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    # wait briefly
    for _ in range(8):
        _t.sleep(1)
        try:
            import socket as _s
            with _s.create_connection(("127.0.0.1", SOUP_PORT), timeout=0.5):
                return {"ok": True, "model": model, "api_url": f"http://127.0.0.1:{SOUP_PORT}/v1/chat/completions", "log": str(log)}
        except Exception:
            continue
    return {"ok": True, "model": model, "api_url": f"http://127.0.0.1:{SOUP_PORT}/v1/chat/completions", "log": str(log), "note": "still booting"}

@router.get("/check-update")
async def check_update():
    """Check remote vs local commit (always, no side effects)."""
    ver = repo_version()
    remote = remote_commit()
    local = ver.get("commit") or ""
    return {"local": local, "remote": remote, "update_available": bool(remote and local and remote[:7] != local[:7]), "version": ver, "remote_commit": remote}

# ── Kronos unified finetune overview (for 6.2 "all models installed" table) ─
@router.get("/kronos/overview")
async def kronos_overview():
    kronos = kronos_models_agg()
    models = list_installed_models()
    try:
        from plugins.KronosForecastPlugin.backend.services.finetune_service import finetune_manager as kfm
        kjobs = await kfm.list(limit=20)
        setups = []
        try:
            from plugins.KronosForecastPlugin.backend.services.finetune_service import setup_status as ksetup
            setups = ksetup()
        except Exception:
            pass
        return {"kronos": kronos, "soup_models": models, "kronos_jobs": kjobs, "kronos_setup": setups}
    except Exception as e:
        return {"kronos": kronos, "soup_models": models, "error": str(e)}
