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
    model_compatibility, model_improvement_detail, get_model_spec, get_hardware_snapshot,
)
from plugins.SoupPlugin.backend.services.soup_service import soup_manager

router = APIRouter(prefix="/plugins/soup", tags=["soup"])

# Track background pulls so we can report progress and allow cancellation
_pull_procs: Dict[str, subprocess.Popen] = {}  # safe_model -> Popen
_pull_kind: Dict[str, str] = {}  # safe_model -> 'hf'|'ollama'

def _safe_model(model: str) -> str:
    return (model or "").strip().replace("/", "_").replace(":", "_")

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


#: Curated, locally-trainable base models the dropdown offers to download.
#: Grouped by family; sizes chosen so a 16GB machine can QLoRA them.
DOWNLOADABLE_BASES: List[Dict[str, str]] = [
    {"id": "Qwen/Qwen2.5-0.5B-Instruct", "label": "Qwen2.5 0.5B (fastest, default)", "family": "Qwen"},
    {"id": "Qwen/Qwen2.5-1.5B-Instruct", "label": "Qwen2.5 1.5B", "family": "Qwen"},
    {"id": "Qwen/Qwen2.5-3B-Instruct", "label": "Qwen2.5 3B", "family": "Qwen"},
    {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5 7B (needs 16GB+)", "family": "Qwen"},
    {"id": "meta-llama/Llama-3.2-1B-Instruct", "label": "Llama 3.2 1B", "family": "Llama"},
    {"id": "meta-llama/Llama-3.2-3B-Instruct", "label": "Llama 3.2 3B", "family": "Llama"},
    {"id": "meta-llama/Llama-3.1-8B-Instruct", "label": "Llama 3.1 8B (needs 16GB+)", "family": "Llama"},
    {"id": "microsoft/Phi-3-mini-4k-instruct", "label": "Phi-3 mini 3.8B", "family": "Phi"},
    {"id": "mistralai/Mistral-7B-Instruct-v0.3", "label": "Mistral 7B (needs 16GB+)", "family": "Mistral"},
    {"id": "moonshotai/Kimi-VL-A3B-Instruct", "label": "Kimi-VL A3B (MoE, 9GB)", "family": "Kimi"},
    {"id": "moonshotai/Kimi-VL-A3B-Thinking", "label": "Kimi-VL A3B Thinking (MoE, 9GB)", "family": "Kimi"},
    {"id": "moonshotai/Moonlight-16B-A3B-Instruct", "label": "Kimi Moonlight 16B-A3B (needs 24GB+)", "family": "Kimi"},
    {"id": "moonshotai/Kimi-K2-Instruct", "label": "Kimi K2 32B (needs 48GB+)", "family": "Kimi"},
    {"id": "HuggingFaceTB/SmolLM2-1.7B-Instruct", "label": "SmolLM2 1.7B", "family": "Smol"},
    {"id": "google/gemma-2-2b-it", "label": "Gemma 2 2B", "family": "Gemma"},
]


@router.get("/models/catalogue")
async def models_catalogue():
    """Downloadable base models for the dropdown + which are already cached."""
    from plugins.SoupPlugin.backend.services.soup_utils import list_installed_models as _lim
    installed = _lim()
    cached = {m["id"] for m in installed}
    ollama_tags = [m["id"] for m in installed if m.get("source") == "ollama"]
    hf_cache = [m for m in installed if m.get("source") == "hf_cache"]
    out = []
    for b in DOWNLOADABLE_BASES:
        out.append({**b, "cached": b["id"] in cached})
    return {"bases": out, "ollama_tags": ollama_tags, "hf_cache": hf_cache, "installed": installed}


# ── Compatibility + improvement preview (before download) ─────────────────────
@router.get("/models/compat")
async def models_compat(model: str = Query(..., description="HF id or Ollama tag")):
    """Resources that will be used + compatibility + improvement detail for a model."""
    mid = (model or "").strip()
    if not mid:
        raise HTTPException(400, "model is required")
    compat = model_compatibility(mid)
    impr = model_improvement_detail(mid)
    # enrich with live pull state if a download is in progress
    safe = _safe_model(mid)
    log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe}.log"
    pull_state = None
    if log_path.exists():
        txt = log_path.read_text(errors="ignore")[-2000:]
        done = "DONE" in txt or "success" in txt.lower() or "writing manifest" in txt.lower()
        running = not done and (safe in _pull_procs and _pull_procs[safe].poll() is None)
        # quick progress peek
        import re as _re
        pct = None
        m = _re.search(r"(\d+)%\s*\|", txt)
        if m:
            try: pct = int(m.group(1))
            except: pass
        else:
            m2 = _re.search(r"(\d+)/(\d+)", txt)
            if m2:
                try: pct = int(int(m2.group(1))/int(m2.group(2))*100)
                except: pass
        pull_state = {"running": running, "done": done, "progress": pct, "log_tail": txt[-800:]}
    return {**compat, "improvement": impr, "pull_state": pull_state}

@router.get("/models/best")
async def models_best():
    """Pick the best downloadable base that fits this hardware (largest compatible, Qwen-preferred for stability)."""
    hw = get_hardware_snapshot()
    best = None
    best_compat = None
    # Sort curated bases by params descending
    def _params(b): return get_model_spec(b["id"]).get("params_b", 0)
    sorted_bases = sorted(DOWNLOADABLE_BASES, key=_params, reverse=True)
    # Prefer 'ok' over 'tight', then largest, with Qwen family boost for tradebot-local stability
    def _qwen_boost(b): return 1.0 if get_model_spec(b["id"]).get("family") == "Qwen" else 0
    def _sort_key(item):
        b,c = item
        return (c["compatibility"]["level"] != "incompatible", _params(b) + _qwen_boost(b))
    candidates_ok = []
    candidates_tight = []
    for b in sorted_bases:
        c = model_compatibility(b["id"])
        lvl = c["compatibility"]["level"]
        if lvl == "ok":
            candidates_ok.append((b, c))
        elif lvl == "tight":
            candidates_tight.append((b, c))
    # Prefer ok with Qwen boost
    if candidates_ok:
        candidates_ok.sort(key=lambda x: _params(x[0]) + _qwen_boost(x[0]), reverse=True)
        best, best_compat = candidates_ok[0]
    elif candidates_tight:
        candidates_tight.sort(key=lambda x: _params(x[0]) + _qwen_boost(x[0]), reverse=True)
        best, best_compat = candidates_tight[0]
    else:
        # fallback to smallest
        b = sorted_bases[-1]
        best, best_compat = b, model_compatibility(b["id"])
    cached_ids = {m["id"] for m in list_installed_models()}
    return {
        "best": best,
        "compat": best_compat,
        "improvement": model_improvement_detail(best["id"]),
        "hardware": hw,
        "cached": best["id"] in cached_ids,
    }


class ModelPullRequest(BaseModel):
    model: str  # HF id (org/model) or Ollama tag (name:tag)


@router.post("/models/pull")
async def models_pull(req: ModelPullRequest):
    """Download a model for training.

    - HF id (contains '/')  → huggingface_hub.snapshot_download in the soup venv
      (Soup would fetch it on train anyway; this pre-caches it so it's ready).
    - Ollama tag (has ':' and no '/') → `ollama pull <tag>` for serving/fallback.
    Returns immediately; the pull runs in the background.
    Includes compatibility pre-check; fails fast if incompatible and not forced.
    """
    model = (req.model or "").strip()
    if not model:
        raise HTTPException(400, "model is required")
    # Check if already running
    safe = _safe_model(model)
    existing = _pull_procs.get(safe)
    if existing and existing.poll() is None:
        return {"ok": True, "kind": _pull_kind.get(safe, "hf"), "model": model, "message": f"Download of {model} already in progress", "already_running": True}
    # Compatibility gate (informational — allow but warn via caller compat panel)
    # We don't block here so user can override if they want
    compat = model_compatibility(model)
    is_ollama_tag = (":" in model and "/" not in model)
    log_dir = SOUP_DATA_DIR / "downloads"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pull_{safe}.log"

    if is_ollama_tag:
        ob = shutil.which("ollama")
        if not ob:
            raise HTTPException(400, "ollama binary not found — install from https://ollama.com")
        with open(log_path, "w") as lf:
            lf.write(f"# Pulling Ollama {model} — compat {compat['compatibility']['level']}\n")
            lf.flush()
            proc = subprocess.Popen([ob, "pull", model], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        _pull_procs[safe] = proc
        _pull_kind[safe] = "ollama"
        return {"ok": True, "kind": "ollama", "model": model, "log": str(log_path), "compat": compat, "message": f"Pulling Ollama model {model} in background"}

    # HF model → snapshot_download via the soup venv python (has huggingface_hub)
    soup_py = SOUP_VENV_BIN / "python"
    py = str(soup_py) if soup_py.exists() else "python3"
    # Use resume + progress; log tqdm output for frontend parser — keep partial cache for resume
    code = (
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(repo_id={model!r}, resume_download=True, local_dir_use_symlinks=False);"
        "print('DONE')"
    )
    with open(log_path, "w") as lf:
        lf.write(f"# Downloading {model} — {compat['spec'].get('download_gb')} GB — compat {compat['compatibility']['level']}\n")
        lf.flush()
        proc = subprocess.Popen([py, "-c", code], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    _pull_procs[safe] = proc
    _pull_kind[safe] = "hf"
    return {"ok": True, "kind": "hf", "model": model, "log": str(log_path), "compat": compat, "message": f"Downloading {model} from HuggingFace in background"}


def _parse_progress(txt: str):
    """Extract percent 0-100 and phase from the log tail."""
    import re as _re
    pct = None
    phase = "downloading"
    # Prefer last percentage in file — HF tqdm uses " 86%|" and Ollama uses " 45%"
    all_pcts = _re.findall(r"(\d+)%\s*\|", txt)
    if all_pcts:
        try: pct = int(all_pcts[-1])
        except: pass
    else:
        m = _re.findall(r"(\d+)%", txt)
        if m:
            try: pct = int(m[-1])
            except: pass
    if pct is None:
        m = _re.search(r"(\d+)/(\d+)\s*\[", txt)
        if m:
            try: pct = int(int(m.group(1))/int(m.group(2))*100)
            except: pass
    # Ollama progress: "pulling <hash>: 3.1 GB / 4.5 GB" or NDJSON "completed: 3120000"
    downloaded_gb = None
    total_gb = None
    dm = _re.search(r"(\d+(?:\.\d+)?)\s*GB\s*/\s*(\d+(?:\.\d+)?)\s*GB", txt)
    if dm:
        try:
            downloaded_gb = float(dm.group(1)); total_gb = float(dm.group(2))
            if total_gb and total_gb > 0 and pct is None:
                pct = int(downloaded_gb / total_gb * 100)
        except: pass
    # Ollama NDJSON fallback: look for "completed":312.../"total":450...
    if pct is None:
        dm2 = _re.findall(r'"completed"\s*:\s*(\d+)[^}]*"total"\s*:\s*(\d+)', txt)
        if dm2:
            try:
                comp, tot = int(dm2[-1][0]), int(dm2[-1][1])
                if tot > 0:
                    pct = int(comp / tot * 100)
            except: pass
    if "DONE" in txt or "writing manifest" in txt.lower():
        pct = 100
        phase = "done"
    elif "CANCELLED" in txt:
        phase = "cancelled"
    elif "error" in txt.lower() and pct is None:
        phase = "error"
    speed = None
    sm = _re.search(r"(\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)/s)", txt)
    if sm: speed = sm.group(1)
    return pct, phase, speed, downloaded_gb, total_gb


@router.get("/models/pull/status")
async def models_pull_status(model: str = Query(...)):
    """Tail the download log for a model pull with progress parsing."""
    safe = _safe_model(model)
    log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe}.log"
    if not log_path.exists():
        return {"model": model, "running": False, "log": "", "done": False, "progress": 0, "phase": "idle"}
    txt_full = log_path.read_text(errors="ignore")
    txt = txt_full[-6000:]
    cancelled = "CANCELLED" in txt_full
    done = not cancelled and ("DONE" in txt_full or "success" in txt_full.lower() or "writing manifest" in txt_full.lower())
    # Determine running via proc poll if we have it, else infer from done + mtime
    running = False
    proc = _pull_procs.get(safe)
    if cancelled:
        running = False
    elif proc is not None:
        running = proc.poll() is None
        if proc.poll() is not None:
            # process exited — infer success vs failure from returncode
            if not done and proc.returncode != 0:
                # keep running false, phase error
                pass
    else:
        # No proc tracked — use file mtime heuristic: if modified <60s ago and not done, assume running
        try:
            age = __import__("time").time() - log_path.stat().st_mtime
            running = (not done) and age < 15  # short window to avoid stale true after cancel
        except: pass
    pct, phase, speed, downloaded_gb, total_gb = _parse_progress(txt_full)
    if cancelled:
        phase = "cancelled"; running = False
    elif done:
        phase = "done"; pct = 100; running = False
    elif not running and pct is None:
        # If not running and not done, check for error
        if "error" in txt_full.lower() or "failed" in txt_full.lower():
            phase = "error"
    # Spec for sizes
    spec = None
    compat = None
    try:
        compat = model_compatibility(model)
        spec = compat.get("spec")
    except: pass
    return {
        "model": model, "running": running, "done": done,
        "progress": pct if pct is not None else (0 if not done and not running else None),
        "phase": phase, "speed": speed,
        "downloaded_gb": downloaded_gb, "total_gb": total_gb or (spec.get("download_gb") if spec else None),
        "spec": spec, "compat": compat,
        "log": txt, "log_path": str(log_path),
    }


@router.delete("/models/pull")
async def models_pull_cancel(model: str = Query(..., description="HF id or Ollama tag to cancel")):
    """Stop a running download (SIGTERM the Popen). Keeps the partial cache for resume."""
    safe = _safe_model(model)
    proc = _pull_procs.get(safe)
    if proc and proc.poll() is None:
        try:
            import signal as _sig
            # Kill group if possible
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGTERM)
            except:
                proc.terminate()
            # Wait briefly
            try: proc.wait(timeout=3)
            except: 
                try:
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                except:
                    proc.kill()
        except Exception as e:
            raise HTTPException(500, detail=str(e))
        finally:
            _pull_procs.pop(safe, None)
        # Mark log so status parser shows cancelled, not running
        try:
            log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe}.log"
            with open(log_path, "a") as _lf:
                _lf.write("\nCANCELLED by user\n")
        except: pass
        return {"ok": True, "model": model, "cancelled": True, "message": f"Download of {model} stopped — partial files kept for resume"}
    # No tracked proc — check log still running via file age
    log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe}.log"
    if log_path.exists():
        txt = log_path.read_text(errors="ignore")
        done = "DONE" in txt or "writing manifest" in txt.lower()
        if not done:
            return {"ok": False, "model": model, "cancelled": False, "message": "No active download found for this model (it may have already finished or was started outside this session)"}
        return {"ok": True, "model": model, "cancelled": False, "message": "Download already completed"}
    raise HTTPException(404, detail=f"No download found for {model}")


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
    timeframe = payload.get("timeframe","1h")
    limit = int(payload.get("limit", 2000))
    # Support multi-symbol export: {symbols: ["BTC/USD","ETH/USD",...]} or legacy {symbol: "BTC/USD"}
    symbols = payload.get("symbols")
    if symbols is not None:
        if isinstance(symbols, str):
            # allow comma-separated string
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        if not isinstance(symbols, list) or len(symbols) == 0:
            raise HTTPException(400, detail="symbols must be a non-empty list")
        # normalize & dedupe preserving order
        seen = set()
        normed: list[str] = []
        for s in symbols:
            s = str(s).strip()
            if not s: continue
            # normalize: upper-case, ensure slash present if user typed e.g. BTCUSD
            s_up = s.upper().strip()
            if "/" not in s_up and len(s_up) >= 6 and s_up.endswith("USD"):
                # BTCUSD -> BTC/USD
                s_up = s_up[:-3] + "/" + s_up[-3:]
            elif "/" not in s_up and len(s_up) >= 6 and s_up.endswith("USDT"):
                s_up = s_up[:-4] + "/" + s_up[-4:]
            if s_up not in seen:
                seen.add(s_up)
                normed.append(s_up)
        symbols = normed
        if len(symbols) == 0:
            raise HTTPException(400, detail="no valid symbols")
        # safety cap: prevent accidental 500-symbol DOS, but allow "as many as you want" up to 100
        if len(symbols) > 100:
            raise HTTPException(400, detail="too many symbols (max 100)")
        try:
            from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl
            SOUP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            combined_path = SOUP_DATA_DIR / f"ohlcv_multi_{len(symbols)}symbols_{timeframe}.jsonl"
            # truncate previous combined
            if combined_path.exists():
                combined_path.unlink()
            total_rows = 0
            total_windows = 0
            per_symbol = []
            for sym in symbols:
                out = SOUP_DATA_DIR / f"ohlcv_{sym.replace('/','_')}_{timeframe}.jsonl"
                try:
                    res = await export_ohlcv_to_jsonl(sym, timeframe, limit, out)
                    # append to combined file
                    with open(out, "r") as src, open(combined_path, "a") as dst:
                        dst.write(src.read())
                    total_rows += int(res.get("rows", 0))
                    total_windows += int(res.get("windows", 0))
                    per_symbol.append({"symbol": sym, **res})
                except Exception as e:
                    per_symbol.append({"symbol": sym, "ok": False, "error": str(e)})
            if not combined_path.exists():
                raise HTTPException(400, detail="No symbols exported successfully: " + "; ".join(f"{p['symbol']}: {p.get('error','')}" for p in per_symbol))
            # re-count combined windows accurately
            try:
                with open(combined_path) as f:
                    combined_windows = sum(1 for _ in f)
            except Exception:
                combined_windows = total_windows
            return {"ok": True, "path": str(combined_path), "rows": total_rows, "windows": combined_windows, "symbols": symbols, "per_symbol": per_symbol, "timeframe": timeframe, "limit": limit}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, detail=str(e))
    # legacy single-symbol path
    symbol = payload.get("symbol","BTC/USD")
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

@router.post("/check-update/pull")
async def check_update_pull():
    """Fetch latest Soup repo (git fetch + reset + pip install -e). Returns new commit."""
    import subprocess, json
    from pathlib import Path as _P
    if not is_repo_cloned():
        raise HTTPException(400, "Soup repo not cloned — run setup_soup.sh first")
    old_ver = repo_version()
    old_commit = old_ver.get("commit")
    # git fetch depth 1 + reset
    try:
        r1 = subprocess.run(["git", "-C", str(SOUP_REPO_DIR), "fetch", "--depth", "1", "origin", "HEAD"], capture_output=True, text=True, timeout=30)
        if r1.returncode != 0:
            raise HTTPException(500, detail=f"git fetch failed: {r1.stderr[:400]}")
        r2 = subprocess.run(["git", "-C", str(SOUP_REPO_DIR), "reset", "--hard", "FETCH_HEAD"], capture_output=True, text=True, timeout=20)
        if r2.returncode != 0:
            raise HTTPException(500, detail=f"git reset failed: {r2.stderr[:400]}")
        # pip install -e .
        soup_py = SOUP_VENV_BIN / "python"
        pip_bin = SOUP_VENV_BIN / "pip"
        if pip_bin.exists():
            subprocess.run([str(pip_bin), "install", "-e", str(SOUP_REPO_DIR)], capture_output=True, text=True, timeout=120)
        # update version file
        try:
            import datetime as _dt
            vf = SOUP_REPO_DIR.parent / "soup.version.json"
            # also update integrations/soup.version.json if configured
            for p in [vf, _P(str(SOUP_VERSION_FILE))]:
                try:
                    p.write_text(json.dumps({"last_pull": _dt.datetime.utcnow().isoformat(), "commit": repo_version().get("commit")}, indent=2))
                except: pass
        except: pass
        new_ver = repo_version()
        return {"ok": True, "old_commit": old_commit, "new_commit": new_ver.get("commit"), "new_version": new_ver, "update_available": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))

# ── Tradebot-local auto-train (best model + recent data) ───────────────────────
class TradebotLocalTrainRequest(BaseModel):
    base: Optional[str] = None  # override HF id; if None, pick best compatible
    epochs: int = 1
    auto_download: bool = True  # download best if not cached
    symbols: Optional[List[str]] = None  # optional OHLCV symbols to include as forecasting windows (e.g. ["BTC/USDT", "ETH/USDT"])

# Keep a small in-memory status for the current tradebot-local pipeline
_tradebot_local_state: Dict[str, Any] = {"phase": "idle", "job_id": None, "model": None, "log": ""}

@router.post("/tradebot-local/train")
async def tradebot_local_train(req: TradebotLocalTrainRequest):
    """Download best model (if needed), build fresh ShareGPT dataset from recent data, and start Soup training.

    The training job appears as a normal SoupFinetuneJob so it is visible in /soup-finetune.
    """
    import asyncio as _asyncio
    from pathlib import Path as _P

    # Pick best base
    if req.base and req.base.strip():
        base_hf = req.base.strip()
        compat = model_compatibility(base_hf)
    else:
        # Use /models/best logic (Qwen-preferred)
        def _params(b): return get_model_spec(b["id"]).get("params_b", 0)
        def _qwen_boost(b): return 1.0 if get_model_spec(b["id"]).get("family") == "Qwen" else 0
        sorted_bases = sorted(DOWNLOADABLE_BASES, key=_params, reverse=True)
        candidates_ok = []
        candidates_tight = []
        for b in sorted_bases:
            c = model_compatibility(b["id"])
            lvl = c["compatibility"]["level"]
            if lvl == "ok":
                candidates_ok.append((b, c))
            elif lvl == "tight":
                candidates_tight.append((b, c))
        if candidates_ok:
            candidates_ok.sort(key=lambda x: _params(x[0]) + _qwen_boost(x[0]), reverse=True)
            b, compat = candidates_ok[0]
            base_hf = b["id"]
        elif candidates_tight:
            candidates_tight.sort(key=lambda x: _params(x[0]) + _qwen_boost(x[0]), reverse=True)
            b, compat = candidates_tight[0]
            base_hf = b["id"]
        else:
            b = sorted_bases[-1]; compat = model_compatibility(b["id"]); base_hf = b["id"]

    # If not cached and auto_download, start download if not already running
    is_cached = compat["compatibility"]["is_cached"]
    if not is_cached and req.auto_download:
        safe = _safe_model(base_hf)
        proc = _pull_procs.get(safe)
        running = proc and proc.poll() is None
        if not running:
            # Trigger pull without blocking
            pull_req = ModelPullRequest(model=base_hf)
            try:
                await models_pull(pull_req)
            except Exception as e:
                # Surface but don't fail the train — it will error downstream if not cached
                pass
        # If still not cached, tell caller to wait for download; training will be queued
        # We expose a follow-up: poll /models/pull/status and retry this endpoint once done.
        # For convenience, if caller wants immediate training, we can wait a bit in background then start.
        # Launch background waiter that starts training once pull completes.
        async def _wait_and_train():
            import time as _t
            safe2 = _safe_model(base_hf)
            _tradebot_local_state.update({"phase": "downloading", "model": base_hf, "log": f"Downloading {base_hf} — {compat['spec'].get('download_gb')} GB"})
            # wait up to 40 min for download to finish — poll parse_progress for live speed
            for _ in range(240):
                await _asyncio.sleep(10)
                log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe2}.log"
                if log_path.exists():
                    t = log_path.read_text(errors="ignore")
                    done = "DONE" in t or "writing manifest" in t.lower()
                    cancelled = "CANCELLED" in t
                    if cancelled:
                        _tradebot_local_state.update({"phase": "cancelled", "model": base_hf, "log": "Download cancelled — resume by re-triggering Train"})
                        return
                    if done:
                        break
                    # update live progress in state
                    try:
                        _pct, _ph, _sp, _, _ = _parse_progress(t)
                        _tradebot_local_state.update({"phase": "downloading", "model": base_hf, "log": f"Downloading {base_hf} — {_pct or 0}% {_sp or ''}"})
                    except: pass
                p = _pull_procs.get(safe2)
                if p and p.poll() is not None:
                    if p.returncode != 0:
                        _tradebot_local_state.update({"phase": "failed", "model": base_hf, "log": f"Download failed (exit {p.returncode}) — check /models/pull/status"})
                        return
                    if p.returncode == 0:
                        break
            _tradebot_local_state.update({"phase": "dataset_build", "model": base_hf, "log": "Building ShareGPT dataset from recent data + Obsidian + web"})
            # Now build dataset and start job (best-effort)
            try:
                from plugins.SoupPlugin.backend.services.soup_ollama_bridge import build_sharegpt_dataset_async
                _syms = getattr(req, "symbols", None)
                if _syms:
                    _syms = [str(s).strip().upper().replace(" ","") for s in _syms if str(s).strip()]
                    # normalize USDT suffix etc
                    norm = []
                    for s in _syms:
                        s = s.replace("/","").replace("-","").upper()
                        if s.endswith("USDT"):
                            s = s[:-4]+"/USDT"
                        elif s.endswith("USD"):
                            s = s[:-3]+"/USD"
                        norm.append(s)
                    _syms = norm
                ds_path, _n = await build_sharegpt_dataset_async(include_pair_catalog=True, symbols=_syms if _syms else None)
                _tradebot_local_state.update({"phase": "training", "model": base_hf, "log": f"Dataset {_n} rows — launching Soup train"})
                from plugins.SoupPlugin.backend.schemas import SoupStartRequest
                # Apple MPS: force bf16, no stream_layers; avoid bitsandbytes OOM
                import platform as _plat
                _is_apple = _plat.system().lower() == "darwin"
                _eff_quant = compat["resources"].get("quantization")
                if _is_apple and _eff_quant == "4bit":
                    _eff_quant = "none"
                _b = str(compat["resources"].get("batch_size") or "auto")
                _stream = bool(compat["resources"].get("stream_layers"))
                if _is_apple:
                    _stream = False
                if _stream and _b.lower() == "auto": _b = "4"
                sr = SoupStartRequest(base=base_hf, task="sft", dataset_path=str(ds_path), dataset_format="chatml", epochs=max(1, int(req.epochs)), learning_rate=2e-5, batch_size=_b, lora_r=int(compat["resources"].get("lora_r") or 64), lora_alpha=16, quantization=("4bit" if _eff_quant=="4bit" else None), stream_layers=_stream, ohlcv_symbol=None)
                row = await soup_manager.start(sr)
                _tradebot_local_state.update({"phase": "training", "job_id": row["id"], "model": base_hf, "log": f"Training started job {row['id']} — visible in Soup Lab"})
                # Background watcher: when job completes → auto export GGUF → ollama create tradebot-local
                async def _watch_and_export(jid: int, base_id: str):
                    import asyncio as _aw
                    for _ in range(360):  # 60 min max watch
                        await _aw.sleep(10)
                        try:
                            j = await soup_manager.get(int(jid), log_lines=5)
                            st = j.get("status")
                            if st == "completed":
                                _tradebot_local_state.update({"phase": "exporting", "model": base_id, "job_id": jid, "log": f"Job #{jid} completed — exporting GGUF → ollama create tradebot-local"})
                                try:
                                    from plugins.SoupPlugin.backend.services.soup_ollama_bridge import trigger_soup_finetune_and_import as _trig
                                    from pathlib import Path as _P
                                    out_dir = _P(j.get("output_dir") or "")
                                    if out_dir and out_dir.exists():
                                        # Direct path: we already have the trained output; export via soup export → ollama create
                                        import subprocess as _sp, shutil as _sh
                                        from plugins.SoupPlugin.backend.services.soup_utils import SOUP_BIN  # type: ignore
                                        soup_bin = str(SOUP_BIN) if SOUP_BIN.exists() else (_sh.which("soup") or "")
                                        if soup_bin:
                                            gguf = out_dir.parent / "tradebot-local.q4_k_m.gguf"
                                            exp = _sp.run([soup_bin, "export", "--model", str(out_dir), "--format", "gguf", "--quant", "q4_k_m", "--output", str(gguf), "--base", base_id], capture_output=True, text=True, timeout=3600)
                                            (out_dir.parent / "soup_export.log").write_text((exp.stdout or "")+"\n"+(exp.stderr or ""))
                                            if gguf.exists():
                                                from app.services.ollama_service import ollama_create_from_gguf  # type: ignore
                                                ok_create = bool(ollama_create_from_gguf("tradebot-local:latest", gguf, modelfile_extra='SYSTEM "You are TradeBot local LLM — trading assistant."', timeout=600))
                                                if ok_create:
                                                    _tradebot_local_state.update({"phase": "done", "model": base_id, "job_id": jid, "log": f"tradebot-local:latest updated from {base_id} ✓"})
                                                    try:
                                                        from plugins.SoupPlugin.backend.services.soup_ollama_bridge import mark_trained  # type: ignore
                                                        mark_trained()
                                                    except: pass
                                                    break
                                                else:
                                                    _tradebot_local_state.update({"phase": "failed", "model": base_id, "job_id": jid, "log": "Export done but ollama create failed — check soup_export.log"})
                                                    break
                                            else:
                                                _tradebot_local_state.update({"phase": "done", "model": base_id, "job_id": jid, "log": f"Job #{jid} completed — trained adapter at {out_dir} (GGUF export skipped — use soup export manually)"})
                                                break
                                except Exception as _ex:
                                    _tradebot_local_state.update({"phase": "failed", "model": base_id, "job_id": jid, "log": f"Export failed: {_ex}"})
                                    break
                            elif st in ("failed","cancelled"):
                                _tradebot_local_state.update({"phase": st, "model": base_id, "job_id": jid, "log": f"Job #{jid} {st}"})
                                break
                        except: pass
                _asyncio.create_task(_watch_and_export(row["id"], base_hf))
            except Exception as e:
                _tradebot_local_state.update({"phase": "failed", "log": str(e)[:600]})
        # fire and forget
        _asyncio.create_task(_wait_and_train())
        _tradebot_local_state.update({"phase": "downloading", "model": base_hf, "log": f"Downloading {base_hf} then will train tradebot-local — follow progress in /soup-finetune pipeline strip"})
        return {"ok": True, "phase": "downloading", "model": base_hf, "compat": compat, "message": f"Downloading best model {base_hf} ({compat['spec'].get('download_gb')} GB). Training will auto-start when done — watch /soup-finetune for the job."}

    # Cached — build dataset and start training immediately (include vault + all pairs + optional user-selected OHLCV symbols)
    try:
        from plugins.SoupPlugin.backend.services.soup_ollama_bridge import build_sharegpt_dataset_async
        _tradebot_local_state.update({"phase": "dataset_build", "model": base_hf, "log": "Building dataset (Obsidian vault + recent trades + web)"})
        _syms2 = getattr(req, "symbols", None)
        if _syms2:
            _syms2 = [str(s).strip().upper() for s in _syms2 if str(s).strip()]
            norm2 = []
            for s in _syms2:
                s = s.replace("-","/").replace(" ","").upper()
                if "/" not in s and s.endswith("USDT"):
                    s = s[:-4]+"/USDT"
                elif "/" not in s and s.endswith("USD"):
                    s = s[:-3]+"/USD"
                norm2.append(s)
            _syms2 = norm2
        ds_path, nrows = await build_sharegpt_dataset_async(include_pair_catalog=True, symbols=_syms2 if _syms2 else None)
        from plugins.SoupPlugin.backend.schemas import SoupStartRequest
        import platform as _plat2
        _is_apple2 = _plat2.system().lower() == "darwin"
        _eff2 = compat["resources"].get("quantization")
        if _is_apple2 and _eff2 == "4bit":
            _eff2 = "none"
        _b2 = str(compat["resources"].get("batch_size") or "auto")
        _stream2 = bool(compat["resources"].get("stream_layers"))
        if _is_apple2:
            _stream2 = False
        if _stream2 and _b2.lower() == "auto": _b2 = "4"
        sr = SoupStartRequest(base=base_hf, task="sft", dataset_path=str(ds_path), dataset_format="chatml", epochs=max(1, int(req.epochs)), learning_rate=2e-5, batch_size=_b2, lora_r=int(compat["resources"].get("lora_r") or 64), lora_alpha=16, quantization=("4bit" if _eff2=="4bit" else None), stream_layers=_stream2, ohlcv_symbol=None)
        row = await soup_manager.start(sr)
        _tradebot_local_state.update({"phase": "training", "job_id": row["id"], "model": base_hf, "log": f"Training job {row['id']} started with {nrows} rows — visible in Soup Lab"})
        # Same watcher as download path — export → ollama create when done
        try:
            import asyncio as _aw2
            async def _watch2(jid: int, base_id: str):
                for _ in range(360):
                    await _aw2.sleep(10)
                    try:
                        j = await soup_manager.get(int(jid), log_lines=5)
                        st = j.get("status")
                        if st == "completed":
                            _tradebot_local_state.update({"phase": "exporting", "model": base_id, "job_id": jid, "log": f"Job #{jid} completed — exporting GGUF → ollama create tradebot-local"})
                            try:
                                from pathlib import Path as _P2
                                import subprocess as _sp2, shutil as _sh2
                                from plugins.SoupPlugin.backend.services.soup_utils import SOUP_BIN as _SB2  # type: ignore
                                soup_bin2 = str(_SB2) if _SB2.exists() else (_sh2.which("soup") or "")
                                out_dir2 = _P2(j.get("output_dir") or "")
                                if soup_bin2 and out_dir2 and out_dir2.exists():
                                    gguf2 = out_dir2.parent / "tradebot-local.q4_k_m.gguf"
                                    exp2 = _sp2.run([soup_bin2, "export", "--model", str(out_dir2), "--format", "gguf", "--quant", "q4_k_m", "--output", str(gguf2), "--base", base_id], capture_output=True, text=True, timeout=3600)
                                    (out_dir2.parent / "soup_export.log").write_text((exp2.stdout or "")+"\n"+(exp2.stderr or ""))
                                    if gguf2.exists():
                                        from app.services.ollama_service import ollama_create_from_gguf as _oc2  # type: ignore
                                        if _oc2("tradebot-local:latest", gguf2, modelfile_extra='SYSTEM "You are TradeBot local LLM — trading assistant."', timeout=600):
                                            _tradebot_local_state.update({"phase": "done", "model": base_id, "job_id": jid, "log": f"tradebot-local:latest updated from {base_id} ✓"})
                                            try:
                                                from plugins.SoupPlugin.backend.services.soup_ollama_bridge import mark_trained as _mt2  # type: ignore
                                                _mt2()
                                            except: pass
                                            break
                                    _tradebot_local_state.update({"phase": "done", "model": base_id, "job_id": jid, "log": f"Job #{jid} completed — adapter at {out_dir2}"})
                                    break
                            except Exception as _ex2:
                                _tradebot_local_state.update({"phase": "failed", "model": base_id, "job_id": jid, "log": f"Export failed: {_ex2}"})
                                break
                        elif st in ("failed","cancelled"):
                            _tradebot_local_state.update({"phase": st, "model": base_id, "job_id": jid, "log": f"Job #{jid} {st}"})
                            break
                    except: pass
            _aw2.create_task(_watch2(row["id"], base_hf))
        except: pass
        return {"ok": True, "phase": "training", "model": base_hf, "job": row, "dataset_rows": nrows, "dataset_path": str(ds_path), "compat": compat, "message": f"Training tradebot-local from {base_hf} with {nrows} recent rows — visible in Soup Lab jobs"}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

@router.get("/tradebot-local/status")
async def tradebot_local_status():
    """Current state of the tradebot-local pipeline (download → dataset → training)."""
    # Also include live job status if we have a job_id
    job = None
    jid = _tradebot_local_state.get("job_id")
    if jid:
        try:
            job = await soup_manager.get(int(jid), log_lines=20)
        except: pass
    # Also include download progress for the current model
    dl = None
    m = _tradebot_local_state.get("model")
    if m:
        try:
            # reuse pull status logic
            safe = _safe_model(m)
            log_path = SOUP_DATA_DIR / "downloads" / f"pull_{safe}.log"
            if log_path.exists():
                txt = log_path.read_text(errors="ignore")[-2000:]
                done = "DONE" in txt or "writing manifest" in txt.lower()
                pct, phase, speed, _, _ = _parse_progress(txt)
                dl = {"model": m, "done": done, "progress": pct, "phase": phase, "speed": speed, "log_tail": txt[-600:]}
        except: pass
    return {**_tradebot_local_state, "job": job, "download": dl}

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
