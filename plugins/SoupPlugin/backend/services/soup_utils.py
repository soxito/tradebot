"""Soup plugin — shared path/utils helpers (mirrors start.py constants)."""
from __future__ import annotations
import os
import subprocess
import json
import platform
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[4]  # tradebot/

def _is_windows() -> bool:
    return os.name == "nt"

IS_WINDOWS = _is_windows()
_VENV_BIN = "Scripts" if IS_WINDOWS else "bin"
_EXE = ".exe" if IS_WINDOWS else ""

SOUP_REPO_URL = os.environ.get("SOUP_REPO_URL", "https://github.com/MakazhanAlpamys/Soup.git")
SOUP_REPO_DIR = Path(os.environ.get("SOUP_REPO_DIR", str(ROOT / "integrations" / "soup"))).expanduser().resolve()
SOUP_VERSION_FILE = Path(os.environ.get("SOUP_VERSION_FILE", str(ROOT / "integrations" / "soup.version.json"))).expanduser().resolve()
SOUP_VENV_DIR = Path(os.environ.get("SOUP_VENV_DIR", str(Path.home() / ".tradebot" / "soup-venv"))).expanduser().resolve()
SOUP_VENV_BIN = SOUP_VENV_DIR / _VENV_BIN
SOUP_PY_BIN = SOUP_VENV_BIN / f"python{_EXE}"
SOUP_PIP_BIN = SOUP_VENV_BIN / f"pip{_EXE}"
SOUP_BIN = SOUP_VENV_BIN / f"soup{_EXE}"
SOUP_UI_PORT = int(os.environ.get("SOUP_UI_PORT", "7860"))
SOUP_PORT = int(os.environ.get("SOUP_PORT", "8012"))
SOUP_PLUGIN_DIR = (ROOT / "plugins" / "SoupPlugin").resolve()
SOUP_DATA_DIR = SOUP_PLUGIN_DIR / "backend" / "data" / "soup"

def soup_setup_command() -> str:
    if IS_WINDOWS:
        return "powershell -ExecutionPolicy Bypass -File plugins\\SoupPlugin\\scripts\\setup_soup.ps1"
    return "bash plugins/SoupPlugin/scripts/setup_soup.sh"

def is_repo_cloned() -> bool:
    return (SOUP_REPO_DIR / ".git").exists()

def is_installed() -> bool:
    return SOUP_BIN.exists() or (SOUP_VENV_BIN / "soup").exists() or (SOUP_VENV_BIN / "soup-cli").exists()

def soup_bin_path() -> Optional[str]:
    for p in (SOUP_BIN, SOUP_VENV_BIN / "soup", SOUP_VENV_BIN / "soup-cli"):
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("soup")

def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None

def repo_version() -> Dict:
    if not SOUP_REPO_DIR.exists():
        return {"cloned": False, "commit": None, "branch": None, "remote": SOUP_REPO_URL, "last_pull": None, "path": str(SOUP_REPO_DIR)}
    r = _run(["git", "-C", str(SOUP_REPO_DIR), "rev-parse", "HEAD"])
    commit = r.stdout.strip()[:12] if r and r.returncode == 0 else None
    r2 = _run(["git", "-C", str(SOUP_REPO_DIR), "rev-parse", "--abbrev-ref", "HEAD"])
    branch = r2.stdout.strip() if r2 and r2.returncode == 0 else None
    last_pull = None
    if SOUP_VERSION_FILE.exists():
        try:
            last_pull = json.loads(SOUP_VERSION_FILE.read_text()).get("last_pull") or json.loads(SOUP_VERSION_FILE.read_text()).get("installed_at")
        except Exception:
            pass
    pkg_version = None
    sb = soup_bin_path()
    if sb:
        # Use importlib.metadata instead of --version (soup has no --version flag)
        try:
            import importlib.metadata as _m
            # need to query from venv python, not current
            rr = _run([str(SOUP_PY_BIN), "-c", "import importlib.metadata as m; print(m.version('soup-cli'))"], timeout=5)
            if rr and rr.returncode == 0:
                pkg_version = rr.stdout.strip()[:20]
        except Exception:
            pass
        if not pkg_version:
            rr = _run([sb, "--help"], timeout=5)
            if rr and (rr.stdout or rr.stderr):
                # fallback: parse first line that looks like version
                txt = (rr.stdout or rr.stderr)
                import re as _re
                mm = _re.search(r"soup-cli\s*([\d\.]+)", txt)
                if mm: pkg_version = mm.group(1)
    return {"cloned": True, "commit": commit, "branch": branch, "remote": SOUP_REPO_URL, "last_pull": last_pull, "pkg_version": pkg_version, "path": str(SOUP_REPO_DIR), "venv": str(SOUP_VENV_DIR), "soup_bin": sb}

def remote_commit() -> Optional[str]:
    import shutil
    if not shutil.which("git"):
        return None
    r = _run(["git", "ls-remote", SOUP_REPO_URL, "HEAD"], timeout=10)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    sha = r.stdout.split()[0].strip()
    return sha if len(sha) >= 7 else None

def recommendation() -> Dict:
    """Hardware-aware recommendation (best quality for this RAM/VRAM)."""
    # lightweight reimplementation of start.py _soup_recommend_resources() but importable here
    try:
        # try to use start.py helper if importable
        import sys
        sys.path.insert(0, str(ROOT))
        from start import _soup_recommend_resources as _srr  # type: ignore
        return _srr()
    except Exception:
        pass
    # Fallback heuristic
    import shutil, subprocess as sp
    ram_gb = 8.0
    try:
        if platform.system().lower() == "darwin":
            rr = sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            if rr.returncode == 0:
                ram_gb = int(rr.stdout.strip()) / (1024**3)
        elif platform.system().lower() == "linux":
            for ln in Path("/proc/meminfo").read_text().splitlines():
                if ln.startswith("MemTotal:"):
                    ram_gb = int(ln.split()[1]) * 1024 / (1024**3)
                    break
    except Exception:
        pass
    vram_gb = None
    try:
        if shutil.which("nvidia-smi"):
            rr = sp.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            if rr.returncode == 0:
                vram_gb = float(rr.stdout.splitlines()[0].strip()) / 1024
    except Exception:
        pass
    is_apple = platform.system().lower() == "darwin" and platform.machine().lower() in ("arm64", "aarch64")
    if vram_gb is not None and vram_gb < 6:
        rec = {"quantization": "4bit", "stream_layers": True, "batch_size": "auto", "lora_r": 64, "use_mlx": False, "reason": f"low VRAM {vram_gb:.0f}GB → QLoRA+stream_layers"}
    elif vram_gb is not None and vram_gb < 16:
        rec = {"quantization": "4bit", "stream_layers": False, "batch_size": "auto", "lora_r": 64, "use_mlx": False, "reason": f"{vram_gb:.0f}GB VRAM → QLoRA"}
    elif vram_gb is not None:
        rec = {"quantization": None, "stream_layers": False, "batch_size": 8, "lora_r": 64, "use_mlx": False, "reason": f"{vram_gb:.0f}GB VRAM → bf16 full"}
    elif is_apple and ram_gb >= 32:
        rec = {"quantization": None, "stream_layers": False, "batch_size": 4, "lora_r": 32, "use_mlx": True, "reason": f"Apple Silicon {ram_gb:.0f}GB → MLX"}
    elif ram_gb <= 8:
        rec = {"quantization": "4bit", "stream_layers": True, "batch_size": "auto", "lora_r": 32, "use_mlx": is_apple, "reason": f"{ram_gb:.0f}GB RAM → QLoRA+stream_layers"}
    else:
        rec = {"quantization": "4bit" if ram_gb <= 16 else None, "stream_layers": False, "batch_size": "auto" if ram_gb <= 16 else 8, "lora_r": 64, "use_mlx": is_apple, "reason": f"{ram_gb:.0f}GB RAM → auto"}
    rec["vram_gb"] = vram_gb
    rec["ram_gb"] = round(ram_gb, 1)
    return rec

def kronos_models_agg() -> Dict:
    """Aggregate Kronos model catalogue + finetuned/installed status (6.2)."""
    try:
        from plugins.KronosForecastPlugin.backend.config import KRONOS_MODELS  # type: ignore
        catalogue = list(KRONOS_MODELS)
    except Exception:
        catalogue = [
            {"id": "NeoQuasar/Kronos-mini", "label": "Kronos-mini (4.1M — fastest)", "tokenizer": "NeoQuasar/Kronos-Tokenizer-2k", "max_context": 2048},
            {"id": "NeoQuasar/Kronos-small", "label": "Kronos-small (24.7M — balanced)", "tokenizer": "NeoQuasar/Kronos-Tokenizer-base", "max_context": 512},
            {"id": "NeoQuasar/Kronos-base", "label": "Kronos-base (102.3M — most accurate)", "tokenizer": "NeoQuasar/Kronos-Tokenizer-base", "max_context": 512},
        ]
    # installed check via huggingface_hub cache scan (best-effort)
    installed_ids = set()
    try:
        from huggingface_hub import scan_cache_dir  # type: ignore
        cache = scan_cache_dir()
        for repo in cache.repos:
            rid = getattr(repo, "repo_id", "")
            if rid.startswith("NeoQuasar/Kronos"):
                installed_ids.add(rid)
    except Exception:
        pass
    # vendor presence also counts
    vendor_model = ROOT / "plugins" / "KronosForecastPlugin" / "backend" / "vendor" / "model"
    vendor_finetune = ROOT / "plugins" / "KronosForecastPlugin" / "backend" / "vendor" / "finetune_csv" / "train_sequential.py"
    for m in catalogue:
        m["installed"] = m["id"] in installed_ids or vendor_model.exists()
    return {"catalogue": catalogue, "vendor_model_exists": vendor_model.exists(), "vendor_finetune_exists": vendor_finetune.exists(), "installed_ids": sorted(installed_ids)}

def list_installed_models() -> list:
    """Enumerate Soup adapters + Kronos finetuned checkpoints + HF cache."""
    out = []
    # HF cache via huggingface_hub (if available)
    try:
        from huggingface_hub import scan_cache_dir  # type: ignore
        cache = scan_cache_dir()
        for repo in cache.repos:
            rid = getattr(repo, "repo_id", "")
            # Keep large model repos, not tokenizers alone? include both
            if rid and "/" in rid:
                out.append({"id": rid, "source": "hf_cache", "installed": True, "size_mb": round((repo.size_on_disk or 0) / 1_000_000, 1)})
    except Exception:
        pass
    # Soup adapters under data/soup
    try:
        for p in (SOUP_DATA_DIR, ROOT / "data" / "soup", Path.home() / ".tradebot" / "soup-output"):
            if p.exists():
                for d in p.iterdir():
                    if d.is_dir():
                        sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1_000_000 if d.exists() else 0
                        out.append({"id": f"soup:{d.name}", "source": "soup_adapter", "path": str(d), "installed": True, "size_mb": round(sz, 1)})
    except Exception:
        pass
    # Dedup by id
    seen = {}
    for m in out:
        if m["id"] not in seen:
            seen[m["id"]] = m
        else:
            # keep larger size
            if m.get("size_mb", 0) > seen[m["id"]].get("size_mb", 0):
                seen[m["id"]] = m
    return sorted(seen.values(), key=lambda x: x["id"])
