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

def _default_soup_base() -> str:
    """HF base id that matches the configured Ollama fallback model (so Soup trains it)."""
    try:
        from app.services.ollama_service import default_soup_base_hf  # type: ignore
        return default_soup_base_hf()
    except Exception:
        return os.getenv("SOUP_DEFAULT_BASE", "").strip() or "Qwen/Qwen2.5-0.5B-Instruct"


def recommendation() -> Dict:
    """Hardware-aware recommendation (best quality for this RAM/VRAM)."""
    # lightweight reimplementation of start.py _soup_recommend_resources() but importable here
    try:
        # try to use start.py helper if importable
        import sys
        sys.path.insert(0, str(ROOT))
        from start import _soup_recommend_resources as _srr  # type: ignore
        rec = _srr()
        rec["base_model"] = _default_soup_base()
        return rec
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
    rec["base_model"] = _default_soup_base()
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

import re
import shutil as _shutil

# ── Hardware probe helpers ────────────────────────────────────────────────────
def get_hardware_snapshot() -> Dict:
    """RAM/VRAM/disk + Apple Silicon flag for compat checks."""
    ram_gb = 8.0
    try:
        if platform.system().lower() == "darwin":
            rr = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            if rr.returncode == 0:
                ram_gb = int(rr.stdout.strip()) / (1024**3)
        elif platform.system().lower() == "linux":
            for ln in Path("/proc/meminfo").read_text().splitlines():
                if ln.startswith("MemTotal:"):
                    ram_gb = int(ln.split()[1]) * 1024 / (1024**3)
                    break
    except Exception:
        pass
    vram_gb: Optional[float] = None
    try:
        if _shutil.which("nvidia-smi"):
            rr = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            if rr.returncode == 0:
                vram_gb = float(rr.stdout.splitlines()[0].strip()) / 1024
    except Exception:
        pass
    # free disk at ROOT and at HF cache dir
    free_disk_gb = 50.0
    hf_cache_disk_gb = 50.0
    try:
        free_disk_gb = _shutil.disk_usage(str(ROOT)).free / (1024**3)
    except Exception:
        pass
    try:
        hf_dir = Path.home() / ".cache" / "huggingface"
        hf_cache_disk_gb = _shutil.disk_usage(str(hf_dir if hf_dir.exists() else ROOT)).free / (1024**3)
    except Exception:
        pass
    is_apple = platform.system().lower() == "darwin" and platform.machine().lower() in ("arm64", "aarch64")
    return {
        "ram_gb": round(float(ram_gb), 1),
        "vram_gb": round(float(vram_gb), 1) if vram_gb is not None else None,
        "free_disk_gb": round(float(free_disk_gb), 1),
        "hf_cache_free_disk_gb": round(float(hf_cache_disk_gb), 1),
        "is_apple_silicon": is_apple,
        "platform": platform.system(),
        "arch": platform.machine(),
    }

# ── Model hardware specs (download + training memory) ─────────────────────────
# download_gb = HF safetensors on disk (bf16)
# train_ram_q4_gb = peak RAM for QLoRA 4-bit SFT (batch auto, seq 2048)
# train_vram_gb = peak VRAM if CUDA, else same as RAM on Apple unified memory
MODEL_SPECS: Dict[str, Dict] = {
    "Qwen/Qwen2.5-0.5B-Instruct": {"params_b": 0.5, "download_gb": 1.0, "train_ram_q4_gb": 4.5, "train_ram_bf16_gb": 6.0, "train_vram_q4_gb": 4.0, "family": "Qwen", "cutoff": "2024-06"},
    "Qwen/Qwen2.5-1.5B-Instruct": {"params_b": 1.5, "download_gb": 3.1, "train_ram_q4_gb": 6.5, "train_ram_bf16_gb": 10.0, "train_vram_q4_gb": 6.0, "family": "Qwen", "cutoff": "2024-06"},
    "Qwen/Qwen2.5-3B-Instruct": {"params_b": 3.0, "download_gb": 6.2, "train_ram_q4_gb": 9.0, "train_ram_bf16_gb": 14.0, "train_vram_q4_gb": 8.0, "family": "Qwen", "cutoff": "2024-06"},
    "Qwen/Qwen2.5-7B-Instruct": {"params_b": 7.0, "download_gb": 15.2, "train_ram_q4_gb": 14.5, "train_ram_bf16_gb": 22.0, "train_vram_q4_gb": 14.0, "family": "Qwen", "cutoff": "2024-06"},
    "Qwen/Qwen2.5-14B-Instruct": {"params_b": 14.0, "download_gb": 28.0, "train_ram_q4_gb": 22.0, "train_ram_bf16_gb": 36.0, "train_vram_q4_gb": 20.0, "family": "Qwen", "cutoff": "2024-06"},
    "meta-llama/Llama-3.2-1B-Instruct": {"params_b": 1.0, "download_gb": 2.5, "train_ram_q4_gb": 6.0, "train_ram_bf16_gb": 9.0, "train_vram_q4_gb": 5.5, "family": "Llama", "cutoff": "2024-12"},
    "meta-llama/Llama-3.2-3B-Instruct": {"params_b": 3.0, "download_gb": 6.5, "train_ram_q4_gb": 9.5, "train_ram_bf16_gb": 14.5, "train_vram_q4_gb": 9.0, "family": "Llama", "cutoff": "2024-12"},
    "meta-llama/Llama-3.1-8B-Instruct": {"params_b": 8.0, "download_gb": 16.0, "train_ram_q4_gb": 16.0, "train_ram_bf16_gb": 24.0, "train_vram_q4_gb": 15.0, "family": "Llama", "cutoff": "2024-03"},
    "microsoft/Phi-3-mini-4k-instruct": {"params_b": 3.8, "download_gb": 7.6, "train_ram_q4_gb": 10.0, "train_ram_bf16_gb": 15.0, "train_vram_q4_gb": 9.5, "family": "Phi", "cutoff": "2024-04"},
    "microsoft/Phi-3-medium-4k-instruct": {"params_b": 14.0, "download_gb": 28.0, "train_ram_q4_gb": 22.0, "train_ram_bf16_gb": 34.0, "train_vram_q4_gb": 20.0, "family": "Phi", "cutoff": "2024-04"},
    "mistralai/Mistral-7B-Instruct-v0.3": {"params_b": 7.0, "download_gb": 14.5, "train_ram_q4_gb": 14.5, "train_ram_bf16_gb": 22.0, "train_vram_q4_gb": 14.0, "family": "Mistral", "cutoff": "2024-05"},
    "google/gemma-2-2b-it": {"params_b": 2.0, "download_gb": 5.0, "train_ram_q4_gb": 7.5, "train_ram_bf16_gb": 11.0, "train_vram_q4_gb": 7.0, "family": "Gemma", "cutoff": "2024-06"},
    "google/gemma-2-9b-it": {"params_b": 9.0, "download_gb": 18.0, "train_ram_q4_gb": 17.0, "train_ram_bf16_gb": 26.0, "train_vram_q4_gb": 16.0, "family": "Gemma", "cutoff": "2024-06"},
    "HuggingFaceTB/SmolLM2-1.7B-Instruct": {"params_b": 1.7, "download_gb": 3.5, "train_ram_q4_gb": 6.5, "train_ram_bf16_gb": 10.0, "train_vram_q4_gb": 6.0, "family": "Smol", "cutoff": "2024-11"},
    "moonshotai/Kimi-VL-A3B-Instruct": {"params_b": 3.0, "download_gb": 9.0, "train_ram_q4_gb": 12.0, "train_ram_bf16_gb": 18.0, "train_vram_q4_gb": 11.0, "family": "Kimi", "cutoff": "2025-01"},
    "moonshotai/Kimi-VL-A3B-Thinking": {"params_b": 3.0, "download_gb": 9.0, "train_ram_q4_gb": 12.0, "train_ram_bf16_gb": 18.0, "train_vram_q4_gb": 11.0, "family": "Kimi", "cutoff": "2025-01"},
    "moonshotai/Kimi-K2-Instruct": {"params_b": 32.0, "download_gb": 64.0, "train_ram_q4_gb": 48.0, "train_ram_bf16_gb": 72.0, "train_vram_q4_gb": 44.0, "family": "Kimi", "cutoff": "2025-06"},
    "moonshotai/Moonlight-16B-A3B-Instruct": {"params_b": 16.0, "download_gb": 32.0, "train_ram_q4_gb": 24.0, "train_ram_bf16_gb": 40.0, "train_vram_q4_gb": 22.0, "family": "Kimi", "cutoff": "2025-02"},
}
# Ollama tags map to HF specs for compatibility
OLLAMA_TAG_TO_HF = {
    "qwen2.5:0.5b": "Qwen/Qwen2.5-0.5B-Instruct",
    "qwen2.5:1.5b": "Qwen/Qwen2.5-1.5B-Instruct",
    "qwen2.5:3b": "Qwen/Qwen2.5-3B-Instruct",
    "qwen2.5:7b": "Qwen/Qwen2.5-7B-Instruct",
    "qwen2.5:14b": "Qwen/Qwen2.5-14B-Instruct",
    "llama3.2:1b": "meta-llama/Llama-3.2-1B-Instruct",
    "llama3.2:3b": "meta-llama/Llama-3.2-3B-Instruct",
    "llama3.1:8b": "meta-llama/Llama-3.1-8B-Instruct",
    "phi3:mini": "microsoft/Phi-3-mini-4k-instruct",
    "mistral:7b": "mistralai/Mistral-7B-Instruct-v0.3",
    "gemma2:2b": "google/gemma-2-2b-it",
    "gemma2:9b": "google/gemma-2-9b-it",
    "kimi-vl-a3b": "moonshotai/Kimi-VL-A3B-Instruct",
    "kimi-vl-a3b-thinking": "moonshotai/Kimi-VL-A3B-Thinking",
    "kimi-k2": "moonshotai/Kimi-K2-Instruct",
    "moonlight-16b-a3b": "moonshotai/Moonlight-16B-A3B-Instruct",
    "kimi:vl-a3b": "moonshotai/Kimi-VL-A3B-Instruct",
    "kimi:k2": "moonshotai/Kimi-K2-Instruct",
}

def _parse_params_b(model_id: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*b", model_id.lower())
    if m:
        try: return float(m.group(1))
        except: pass
    # phi mini etc
    if "phi-3-mini" in model_id.lower() or "phi3:mini" in model_id.lower():
        return 3.8
    if "kimi" in model_id.lower() and "a3b" in model_id.lower():
        return 3.0
    return None

def get_model_spec(model_id: str) -> Dict:
    mid = (model_id or "").strip()
    # ollama tag?
    low = mid.lower()
    if low in OLLAMA_TAG_TO_HF:
        hf = OLLAMA_TAG_TO_HF[low]
        spec = dict(MODEL_SPECS.get(hf, {}))
        spec["id"] = mid
        spec["hf_id"] = hf
        spec["kind"] = "ollama"
        if "params_b" not in spec:
            spec["params_b"] = _parse_params_b(hf) or 1.0
            spec["download_gb"] = round(spec["params_b"] * 2.2, 1)
            spec["train_ram_q4_gb"] = round(spec["params_b"] * 2.0 + 4.0, 1)
            spec["train_ram_bf16_gb"] = round(spec["params_b"] * 3.0 + 5.0, 1)
            spec["train_vram_q4_gb"] = spec["train_ram_q4_gb"] - 1.0
        return spec
    if mid in MODEL_SPECS:
        spec = dict(MODEL_SPECS[mid])
        spec["id"] = mid
        spec["hf_id"] = mid
        spec["kind"] = "hf"
        return spec
    # generic HF id — estimate from params in name
    params = _parse_params_b(mid) or 1.0
    # fallback family detection
    fam = "Unknown"
    for k in ["Qwen","Llama","Mistral","Gemma","Phi","Kimi","Smol"]:
        if k.lower() in low:
            fam = k; break
    download = round(params * 2.2, 1)  # bf16 ~2 bytes/param + overhead
    train_q4 = round(params * 2.0 + 4.5, 1)
    train_bf16 = round(params * 3.0 + 5.5, 1)
    return {
        "id": mid, "hf_id": mid, "kind": "hf", "params_b": params,
        "download_gb": download, "train_ram_q4_gb": train_q4,
        "train_ram_bf16_gb": train_bf16, "train_vram_q4_gb": round(train_q4 - 1.0,1),
        "family": fam, "cutoff": "2024-01", "estimated": True,
    }

def model_compatibility(model_id: str) -> Dict:
    """Full compatibility + resources that will be used for this model on this machine."""
    spec = get_model_spec(model_id)
    hw = get_hardware_snapshot()
    rec = recommendation()
    # Recommended training config for this spec on this HW
    ram = hw["ram_gb"]
    vram = hw["vram_gb"]
    is_apple = hw["is_apple_silicon"]
    free_disk = min(hw["free_disk_gb"], hw["hf_cache_free_disk_gb"])
    # Effective training memory needed under recommended quant
    quant = rec.get("quantization")  # 4bit or None
    # On Apple MPS, 4bit (bitsandbytes) is unsupported — force bf16 logic
    if is_apple and quant == "4bit":
        # training via soup still uses 4bit on Apple? soup does use transformers+bitsandbytes only on CUDA.
        # So effective training will be bf16 on Apple.
        effective_quant = "none"
        ram_needed = spec.get("train_ram_bf16_gb", spec["train_ram_q4_gb"] + 3)
    else:
        effective_quant = quant or "none"
        ram_needed = spec["train_ram_q4_gb"] if quant == "4bit" else spec.get("train_ram_bf16_gb", spec["train_ram_q4_gb"] + 4)
    # stream_layers reduces peak by ~35%
    stream = bool(rec.get("stream_layers"))
    if stream:
        ram_needed = round(ram_needed * 0.65, 1)
    # disk needed = download + 2GB overhead + output (~0.5GB)
    disk_needed = round(spec["download_gb"] + 2.5, 1)
    # Check existence
    is_cached = False
    try:
        cached_ids = {m["id"] for m in list_installed_models()}
        is_cached = spec["hf_id"] in cached_ids or spec["id"] in cached_ids
        # also check HF cache dir directly via spec
        if not is_cached:
            # check if HF cache folder exists
            hf_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{spec['hf_id'].replace('/','--')}"
            if hf_cache_dir.exists():
                is_cached = True
    except Exception:
        pass
    # Disk check
    disk_ok = free_disk >= disk_needed
    # RAM check with generous headroom: need <85% of RAM for ok, <100% for tight
    ram_headroom = ram * 0.85
    ram_tight_headroom = ram * 0.98
    if ram_needed <= ram_headroom:
        ram_level = "ok"
    elif ram_needed <= ram_tight_headroom:
        ram_level = "tight"
    else:
        ram_level = "incompatible"
    # Disk level
    if is_cached:
        disk_level = "ok"
        disk_needed_effective = 0.5  # only output
    else:
        disk_level = "ok" if free_disk >= disk_needed + 2 else "tight" if free_disk >= disk_needed else "incompatible"
        disk_needed_effective = disk_needed
    # Overall
    if ram_level == "incompatible" or disk_level == "incompatible":
        overall = "incompatible"
    elif ram_level == "tight" or disk_level == "tight":
        overall = "tight"
    else:
        overall = "ok"
    compatible = overall != "incompatible"
    warnings = []
    if not disk_ok and not is_cached:
        warnings.append(f"Need {disk_needed} GB free, have {free_disk:.1f} GB — free disk space before downloading")
    if ram_level == "tight":
        warnings.append(f"Tight RAM: need ~{ram_needed} GB, have {ram:.1f} GB — will use QLoRA + streaming and may swap")
    if ram_level == "incompatible":
        warnings.append(f"Not enough RAM: need ~{ram_needed} GB, have {ram:.1f} GB — pick a smaller model or add RAM/swap")
    if is_apple and spec["params_b"] >= 7 and ram < 16:
        warnings.append("7B+ on <16 GB Apple Silicon will be very slow and may OOM — 3B or smaller recommended")
    if spec["params_b"] >= 14 and ram < 24:
        warnings.append("14B needs 24 GB+ — will OOM on this machine")
    if spec["params_b"] >= 16 and ram < 24:
        warnings.append(f"{spec['params_b']:.0f}B (Kimi Moonlight/K2 class) needs 24 GB+ — marked 'needs 24GB+' for a reason")
    if spec["params_b"] >= 32 and ram < 48:
        warnings.append("32B Kimi-K2 needs 48 GB+ — will OOM on this machine")
    if spec.get("estimated"):
        warnings.append(f"Unknown model id — estimates based on ~{spec['params_b']}B params; actual download may differ")
    # Will-training resources
    resources = {
        "quantization": effective_quant,
        "stream_layers": stream,
        "batch_size": rec.get("batch_size"),
        "lora_r": rec.get("lora_r"),
        "lora_alpha": 16,
        "backend": "transformers" if not rec.get("use_mlx") else "mlx",
        "ram_needed_gb": ram_needed,
        "disk_needed_gb": disk_needed_effective,
        "ram_available_gb": ram,
        "vram_available_gb": vram,
        "free_disk_gb": free_disk,
        "is_cached": is_cached,
    }
    return {
        "model": spec["id"],
        "hf_id": spec["hf_id"],
        "kind": spec["kind"],
        "spec": spec,
        "hardware": hw,
        "resources": resources,
        "compatibility": {
            "compatible": compatible,
            "level": overall,
            "ram_level": ram_level,
            "disk_level": disk_level,
            "reason": "Fits with QLoRA" if overall=="ok" else ("Tight but possible with streaming" if overall=="tight" else "Not compatible with this hardware"),
            "warnings": warnings,
            "is_cached": is_cached,
            "disk_ok": disk_ok,
            "ram_ok": ram_level != "incompatible",
        },
        "recommendation": rec,
    }

def model_improvement_detail(model_id: str) -> Dict:
    """What will improve on the base model after fine-tuning tradebot-local."""
    spec = get_model_spec(model_id)
    hf_id = spec["hf_id"]
    fam = spec.get("family","")
    cutoff = spec.get("cutoff","2024-01")
    params = spec.get("params_b", 1.0)
    # Static improvement catalogue per family
    family_strength = {
        "Qwen": "Strong multilingual reasoning, tool-calling and code — good for signal JSON and trade planning",
        "Llama": "Strong general reasoning and instruction following — good for research synthesis",
        "Mistral": "Efficient reasoning, concise outputs — good for low-latency trading decisions",
        "Phi": "Small but capable — fast, good for 4 GB machines, best latency on TradeBot",
        "Gemma": "Google-trained, strong safety and instruction following",
        "Kimi": "MoE architecture — sparse capacity, vision+text if VL, good for pattern recognition",
        "Smol": "Ultra-light — fastest inference, suitable for edge/CI",
    }
    base_note = family_strength.get(fam, "General instruct model")
    # What training adds (recent data)
    training_sources = [
        f"Brain episodes + federated search (~{200 if params<2 else 400} rows recent)",
        "Agent decisions with outcomes (settled trades, PnL)",
        "All stored trades, signals, sentiment scores, news articles",
        "Rug-pull & pre-pump sniper tables",
        "Obsidian vault: decisions, strategies, learning, insights",
        "Interaction buffer: real local-LLM conversations since last train",
        "Live crypto/macro news (web research, last 30 articles)",
    ]
    # Quantitative expectation (heuristic, not a promise)
    if params <= 0.5:
        capability = "Fastest; best for 4-8 GB machines. After fine-tune: +15-25% better TradeBot task adherence on this data vs base, but limited reasoning depth"
    elif params <= 2:
        capability = "Balanced latency/quality. Expect +20-30% task adherence and more reliable JSON signal outputs"
    elif params <= 4:
        capability = "Good quality bump: +25-35% trade reasoning fidelity, better risk handling and news impact analysis"
    else:
        capability = "Highest quality on this hardware: +30-40% fidelity, richest context handling, but slower inference and higher memory"
    return {
        "base_model": hf_id,
        "base_family": fam,
        "base_params_b": params,
        "base_cutoff": cutoff,
        "base_strength": base_note,
        "what_finetune_adds": "LoRA adapter (QLoRA 4-bit where supported) trained on your recent TradeBot data → new tradebot-local:latest image that replaces Ollama fallback",
        "training_sources": training_sources,
        "data_window": "All data currently in SQLite + Brain + Obsidian + live news (no fixed window — ever-growing, deduplicated, culled to last ~5000 rows)",
        "expected_improvement": capability,
        "serving": "After training: `ollama create tradebot-local:latest` from exported GGUF (q4_k_m) — auto-used by agents as local fallback when OLLAMA_FALLBACK_ENABLED=1",
        "base_model_link": f"https://huggingface.co/{hf_id}",
        "soup_docs": "https://github.com/MakazhanAlpamys/Soup",
        "limitations": [
            "Fine-tune does not change the base cutoff date — it adds TradeBot-specific behaviour on top",
            "Quality scales with amount and diversity of recent data — train again after collecting more settled decisions",
            "QLoRA adapters are small (~tens of MB) but GGUF is full size (download_gb)",
        ],
    }

def list_installed_models() -> list:
    """Enumerate Soup adapters + Kronos finetuned checkpoints + HF cache + Ollama models."""
    out = []
    # Ollama-served models — shown first; tradebot-local is flagged as is_trained
    try:
        from app.services.ollama_service import ollama_list_models_sync, TRADEBOT_LOCAL_MODEL_ALT  # type: ignore
        tags = []
        try:
            tags = ollama_list_models_sync(timeout=5.0)
        except Exception:
            tags = []
        # Fallback scan of ~/.ollama/models if daemon unreachable (so dropdown never empty on boot)
        if not tags:
            try:
                ol_models_dir = Path.home() / ".ollama" / "models"
                if ol_models_dir.exists():
                    for mf in ol_models_dir.rglob("manifest*"):
                        # manifest path encodes tag; best-effort extract
                        rel = mf.relative_to(ol_models_dir).as_posix()
                        if ":" in rel or "/" in rel:
                            tags.append(rel)
            except Exception:
                pass
        for tag in tags:
            if not tag or not isinstance(tag, str):
                continue
            is_trained = tag.split(":")[0] == TRADEBOT_LOCAL_MODEL_ALT or tag.startswith("tradebot-local")
            out.append({"id": tag, "source": "ollama",
                        "label": f"{'🧠 ' if is_trained else ''}Ollama: {tag}",
                        "installed": True, "is_trained": is_trained, "is_fallback_preferred": is_trained})
    except Exception:
        pass
    # Training artefacts: recent adapters + GGUFs from soup_jobs
    try:
        jobs_root = ROOT / "data" / "soup_jobs"
        if jobs_root.exists():
            for job_dir in sorted(jobs_root.iterdir(), reverse=True)[:8]:
                if not job_dir.is_dir():
                    continue
                adapter = job_dir / "output"
                if adapter.exists():
                    sz = sum(f.stat().st_size for f in adapter.rglob("*") if f.is_file()) / 1_000_000
                    out.append({"id": f"trained:{job_dir.name}", "source": "trained_adapter",
                                "path": str(adapter), "installed": True, "size_mb": round(sz, 1),
                                "label": f"🏋 Trained: {job_dir.name}"})
                for gguf in job_dir.glob("*.gguf"):
                    out.append({"id": f"gguf:{gguf.name}", "source": "trained_gguf",
                                "path": str(gguf), "installed": True,
                                "size_mb": round(gguf.stat().st_size / 1_000_000, 1),
                                "label": f"📦 GGUF: {gguf.name}"})
    except Exception:
        pass
    # HF cache via huggingface_hub (if available)
    try:
        from huggingface_hub import scan_cache_dir  # type: ignore
        cache = scan_cache_dir()
        for repo in cache.repos:
            rid = getattr(repo, "repo_id", "")
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
