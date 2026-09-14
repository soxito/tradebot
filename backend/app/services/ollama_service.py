"""
Ollama local-LLM service — resource-tiered model selection, lifecycle and chat.

Single import site for everything Ollama:
  - catalogue: tier → best model per RAM/VRAM/GPU
  - runtime: probe, pull, serve health, chat completion
  - training bridge helpers (export → GGUF → ollama create) are in
    plugins/SoupPlugin/backend/services/soup_ollama_bridge.py

Env contract (all optional, .env wins):
  OLLAMA_ENABLED, OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_MS,
  OLLAMA_FALLBACK_ENABLED, OLLAMA_AUTO_PULL, OLLAMA_MANAGED
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

# ── Defaults (override via env) ────────────────────────────────────────────
OLLAMA_DEFAULT_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_DEFAULT_MODEL_ENV = os.getenv("OLLAMA_MODEL", "").strip()
OLLAMA_TIMEOUT_MS = int(os.getenv("OLLAMA_TIMEOUT_MS", "15000"))
OLLAMA_FALLBACK_ENABLED = os.getenv("OLLAMA_FALLBACK_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
OLLAMA_AUTO_PULL = os.getenv("OLLAMA_AUTO_PULL", "1").strip().lower() not in ("0", "false", "no", "off")
OLLAMA_MANAGED = os.getenv("OLLAMA_MANAGED", "0").strip().lower() in ("1", "true", "yes", "on")

# ── Lightweight runtime knobs (keep the laptop responsive under Docker) ─────
# num_ctx caps the KV-cache: the model default (131072) alone reserves ~18 GB on
# a 3B model and swaps a 16 GB machine to a freeze. 4096 is plenty for a fallback.
# keep_alive unloads the model quickly after a (rare) fallback call to free RAM.
# num_thread caps CPU cores so Docker + other services keep headroom (0 = auto).
def ollama_num_ctx() -> int:
    try:
        return max(512, int(os.getenv("OLLAMA_NUM_CTX", "4096")))
    except Exception:
        return 4096


def ollama_keep_alive() -> str:
    return (os.getenv("OLLAMA_KEEP_ALIVE", "30s") or "30s").strip()


def ollama_num_thread() -> int:
    try:
        return max(0, int(os.getenv("OLLAMA_NUM_THREAD", "0")))  # 0 = Ollama auto
    except Exception:
        return 0


def _chat_options(temperature: float, max_tokens: int) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "temperature": temperature,
        "num_predict": max_tokens,
        "num_ctx": ollama_num_ctx(),
    }
    nt = ollama_num_thread()
    if nt > 0:
        opts["num_thread"] = nt
    return opts

# ── Tier → model catalogue (RAM-aware) ─────────────────────────────────────
# Smallest that still works well for trading reasoning; Q4_K_M is default quant.
# Keep tags exactly as ollama library expects (name:tag).
TIER_MODEL_CATALOGUE: Dict[str, List[str]] = {
    # ultra / 64GB+ : biggest we can comfortably run
    "ultra": ["qwen2.5:14b", "llama3.1:8b", "gemma2:9b", "mistral:7b"],
    "high": ["llama3.1:8b", "qwen2.5:7b", "mistral:7b", "gemma2:9b"],
    "medium": ["llama3.2:3b", "qwen2.5:3b", "phi3:mini", "qwen2.5:1.5b"],
    "low": ["qwen2.5:1.5b", "phi3:mini", "llama3.2:1b", "qwen2.5:0.5b"],
    "minimal": ["qwen2.5:0.5b", "llama3.2:1b", "phi3:mini"],
}

# RAM (GB) → tier suggestion for Ollama (stricter than UI tier, disk+RAM matters)
RAM_TIER_THRESHOLDS: List[Tuple[float, str]] = [
    (48, "ultra"),
    (24, "high"),
    (12, "medium"),
    (6, "low"),
    (0, "minimal"),
]

# Training artefact tag pushed into Ollama after Soup fine-tune
TRADEBOT_LOCAL_MODEL = "tradebot-local:latest"
TRADEBOT_LOCAL_MODEL_ALT = "tradebot-local"

# ── Ollama tag → HuggingFace instruct base (what Soup fine-tunes from) ──────
# Soup trains from a HF id, not an Ollama tag. This maps the served fallback
# model back to its HF base so a fine-tune produces a tradebot-local that is
# architecturally identical to the model Ollama serves.
OLLAMA_TO_HF: Dict[str, str] = {
    "qwen2.5:14b": "Qwen/Qwen2.5-14B-Instruct",
    "qwen2.5:7b": "Qwen/Qwen2.5-7B-Instruct",
    "qwen2.5:3b": "Qwen/Qwen2.5-3B-Instruct",
    "qwen2.5:1.5b": "Qwen/Qwen2.5-1.5B-Instruct",
    "qwen2.5:0.5b": "Qwen/Qwen2.5-0.5B-Instruct",
    "llama3.1:8b": "meta-llama/Llama-3.1-8B-Instruct",
    "llama3.2:3b": "meta-llama/Llama-3.2-3B-Instruct",
    "llama3.2:1b": "meta-llama/Llama-3.2-1B-Instruct",
    "gemma2:9b": "google/gemma-2-9b-it",
    "mistral:7b": "mistralai/Mistral-7B-Instruct-v0.3",
    "phi3:mini": "microsoft/Phi-3-mini-4k-instruct",
}
SOUP_FALLBACK_BASE = "Qwen/Qwen2.5-0.5B-Instruct"


def hf_base_for_ollama(tag: str) -> str:
    """Map an Ollama model tag to its HuggingFace instruct base id."""
    t = (tag or "").strip().lower().split("#")[0].strip()
    if t.startswith("tradebot-local"):  # already fine-tuned → retrain from its base
        return SOUP_FALLBACK_BASE
    return OLLAMA_TO_HF.get(t, SOUP_FALLBACK_BASE)


def default_soup_base_hf() -> str:
    """HF base id Soup should fine-tune so the result matches the Ollama fallback.

    Priority: SOUP_DEFAULT_BASE (explicit HF override) > OLLAMA_MODEL (tag→HF) >
    tier auto-pick (tag→HF) > SOUP_FALLBACK_BASE.
    """
    explicit = os.getenv("SOUP_DEFAULT_BASE", "").strip()
    if explicit:
        return explicit
    try:
        tag = resolve_model()
    except Exception:
        tag = os.getenv("OLLAMA_MODEL", "").strip()
    return hf_base_for_ollama(tag) if tag else SOUP_FALLBACK_BASE


def ollama_url() -> str:
    return (os.getenv("OLLAMA_URL", OLLAMA_DEFAULT_URL) or OLLAMA_DEFAULT_URL).rstrip("/")


def ollama_timeout_s() -> float:
    try:
        return max(2.0, int(os.getenv("OLLAMA_TIMEOUT_MS", str(OLLAMA_TIMEOUT_MS))) / 1000.0)
    except Exception:
        return 15.0


def is_ollama_enabled() -> bool:
    return (os.getenv("OLLAMA_ENABLED", "1" if OLLAMA_ENABLED else "0").strip().lower() not in ("0", "false", "no", "off"))


def is_fallback_enabled() -> bool:
    return (os.getenv("OLLAMA_FALLBACK_ENABLED", "1" if OLLAMA_FALLBACK_ENABLED else "0").strip().lower() not in ("0", "false", "no", "off"))


# ── Resource detection helpers (lightweight, no psutil required) ───────────
def _vram_gb() -> float:
    """Best-effort VRAM in GB (NVIDIA via nvidia-smi, else 0)."""
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                vals = [float(x.strip()) for x in r.stdout.splitlines() if x.strip().replace(".", "", 1).isdigit()]
                if vals:
                    return max(vals) / 1024.0
        except Exception:
            pass
    return 0.0


def _is_apple_silicon() -> bool:
    return platform.system().lower() == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _ram_gb_fallback() -> float:
    try:
        from start import detect_resources  # type: ignore

        res = detect_resources()
        return float(res.get("ram_gb", 8))
    except Exception:
        pass
    try:
        import os as _os

        return _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024**3)
    except Exception:
        return 8.0


# ── Tier → model resolution ────────────────────────────────────────────────
def tier_for_ram(ram_gb: float, vram_gb: float = 0) -> str:
    """Pick Ollama tier from RAM (+ VRAM bump). Never invent a tier."""
    # VRAM effectively adds headroom so a 16GB RAM + 8GB VRAM laptop can use high tier
    effective = ram_gb + min(vram_gb * 0.5, 8)
    for threshold, tier in RAM_TIER_THRESHOLDS:
        if effective >= threshold:
            return tier
    return "minimal"


def candidates_for_tier(tier: str) -> List[str]:
    return list(TIER_MODEL_CATALOGUE.get(tier, TIER_MODEL_CATALOGUE["medium"]))


def pick_best_model_for_resources(
    ram_gb: Optional[float] = None,
    vram_gb: Optional[float] = None,
    tier: Optional[str] = None,
) -> str:
    """
    Resolve best model for this machine.
    Priority: explicit OLLAMA_MODEL env → tier arg → RAM+VRAM derived tier → medium default.
    """
    explicit = (os.getenv("OLLAMA_MODEL", "") or OLLAMA_DEFAULT_MODEL_ENV or "").strip()
    if explicit:
        return explicit
    if tier and tier in TIER_MODEL_CATALOGUE:
        return candidates_for_tier(tier)[0]
    if ram_gb is None:
        ram_gb = _ram_gb_fallback()
    if vram_gb is None:
        vram_gb = _vram_gb()
    derived = tier_for_ram(float(ram_gb), float(vram_gb))
    return candidates_for_tier(derived)[0]


def resolve_model(
    ram_gb: Optional[float] = None,
    vram_gb: Optional[float] = None,
    tier: Optional[str] = None,
) -> str:
    """Alias kept for callers that already have tier."""
    return pick_best_model_for_resources(ram_gb=ram_gb, vram_gb=vram_gb, tier=tier)


# ── Ollama HTTP helpers ────────────────────────────────────────────────────
def _tags_url() -> str:
    return f"{ollama_url()}/api/tags"


def _show_url() -> str:
    return f"{ollama_url()}/api/show"


def _pull_url() -> str:
    return f"{ollama_url()}/api/pull"


def _chat_url() -> str:
    return f"{ollama_url()}/api/chat"


def _generate_url() -> str:
    return f"{ollama_url()}/api/generate"


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def ollama_available(timeout: float = 2.0) -> bool:
    """Liveness probe — hits /api/tags with short timeout."""
    if not is_ollama_enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(_tags_url())
            return r.status_code < 500
    except Exception:
        return False


def ollama_available_sync(timeout: float = 2.0) -> bool:
    if not is_ollama_enabled():
        return False
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(_tags_url())
            return r.status_code < 500
    except Exception:
        return False


def ollama_list_models_sync(timeout: float = 5.0) -> List[str]:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(_tags_url())
            if r.status_code >= 400:
                return []
            data = r.json()
            return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []


async def ollama_list_models(timeout: float = 5.0) -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(_tags_url())
            if r.status_code >= 400:
                return []
            data = r.json()
            return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []


def ensure_model_pulled_sync(model: str, timeout: float = 600.0) -> bool:
    """Blocking pull (used by start.py). Streams NDJSON until done."""
    models = ollama_list_models_sync(timeout=5)
    if any(model == m or model.split(":")[0] == m.split(":")[0] for m in models):
        # Already have same family — treat as present to avoid re-pull every boot
        if model in models:
            return True
        # If we have base without tag match, still consider present for minimal tier
    try:
        with httpx.Client(timeout=None) as c:
            with c.stream("POST", _pull_url(), json={"name": model}, timeout=timeout) as r:
                if r.status_code >= 400:
                    logger.warning(f"[ollama] pull {model} failed: {r.status_code}")
                    return False
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        if evt.get("error"):
                            logger.warning(f"[ollama] pull {model} error: {evt['error']}")
                            return False
                    except Exception:
                        continue
                return True
    except Exception as exc:
        logger.warning(f"[ollama] pull {model} exception: {exc}")
        return False


async def ollama_pull(model: str, timeout: float = 600.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", _pull_url(), json={"name": model}, timeout=timeout) as r:
                if r.status_code >= 400:
                    return False
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        if evt.get("error"):
                            return False
                    except Exception:
                        continue
                return True
    except Exception as exc:
        logger.warning(f"[ollama] async pull {model} failed: {exc}")
        return False


# ── Chat completion (OpenAI-shaped convenience) ────────────────────────────
async def ollama_chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    json_mode: bool = False,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Call Ollama's /api/chat. Returns dict with content, usage-ish, model, done_reason.
    Raises on failure so callers can circuit-break.
    """
    mdl = model or resolve_model()
    t = timeout if timeout is not None else ollama_timeout_s()
    payload: Dict[str, Any] = {
        "model": mdl,
        "messages": messages,
        "stream": False,
        "keep_alive": ollama_keep_alive(),
        "options": _chat_options(temperature, max_tokens),
    }
    if json_mode:
        payload["format"] = "json"

    async with httpx.AsyncClient(timeout=t) as c:
        r = await c.post(_chat_url(), json=payload)
        r.raise_for_status()
        data = r.json()
        msg = (data.get("message") or {})
        content = msg.get("content") or data.get("response") or ""
        return {
            "content": content,
            "model": data.get("model") or mdl,
            "done_reason": data.get("done_reason") or data.get("done"),
            "prompt_eval_count": data.get("prompt_eval_count"),
            "eval_count": data.get("eval_count"),
            "raw": data,
        }


def ollama_chat_sync(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    json_mode: bool = False,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    mdl = model or resolve_model()
    t = timeout if timeout is not None else ollama_timeout_s()
    payload: Dict[str, Any] = {
        "model": mdl,
        "messages": messages,
        "stream": False,
        "keep_alive": ollama_keep_alive(),
        "options": _chat_options(temperature, max_tokens),
    }
    if json_mode:
        payload["format"] = "json"
    with httpx.Client(timeout=t) as c:
        r = c.post(_chat_url(), json=payload)
        r.raise_for_status()
        data = r.json()
        msg = (data.get("message") or {})
        content = msg.get("content") or data.get("response") or ""
        return {"content": content, "model": data.get("model") or mdl, "raw": data}


# ── GGUF → Ollama create (used after Soup training) ───────────────────────
def ollama_create_from_gguf(
    model_name: str,
    gguf_path: Path,
    modelfile_extra: str = "",
    timeout: float = 300.0,
) -> bool:
    """
    Create an Ollama model from a GGUF file via `ollama create`.
    Requires `ollama` binary on PATH. Returns True on success.
    """
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        logger.warning("[ollama] ollama binary not found on PATH — cannot create model")
        return False
    if not gguf_path.exists():
        logger.warning(f"[ollama] GGUF not found: {gguf_path}")
        return False
    # Write ephemeral Modelfile
    modelfile = gguf_path.parent / "Modelfile"
    try:
        modelfile.write_text(f"FROM {gguf_path}\n" + (modelfile_extra or "") + "\n")
        r = subprocess.run(
            [ollama_bin, "create", model_name, "-f", str(modelfile)],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.warning(f"[ollama] create {model_name} failed: {r.stderr[:600]}")
            return False
        logger.info(f"[ollama] created model {model_name} from {gguf_path.name}")
        return True
    except Exception as exc:
        logger.warning(f"[ollama] create {model_name} exception: {exc}")
        return False
    finally:
        try:
            if modelfile.exists():
                modelfile.unlink()
        except Exception:
            pass


def ollama_create_from_modelfile(
    model_name: str, modelfile_content: str, timeout: float = 300.0
) -> bool:
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return False
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "Modelfile"
            mf.write_text(modelfile_content)
            r = subprocess.run(
                [ollama_bin, "create", model_name, "-f", str(mf)],
                capture_output=True, text=True, timeout=timeout,
            )
            return r.returncode == 0
    except Exception as exc:
        logger.warning(f"[ollama] create from modelfile failed: {exc}")
        return False


# ── Status snapshot (for /status endpoints) ────────────────────────────────
async def ollama_status_snapshot() -> Dict[str, Any]:
    enabled = is_ollama_enabled()
    url = ollama_url()
    available = await ollama_available() if enabled else False
    models = await ollama_list_models() if available else []
    preferred = resolve_model()
    has_local = any(m.startswith("tradebot-local") for m in models)
    return {
        "enabled": enabled,
        "url": url,
        "available": available,
        "fallback_enabled": is_fallback_enabled(),
        "preferred_model": preferred,
        "models": models,
        "has_tradebot_local": has_local,
        "tradebot_local_tag": TRADEBOT_LOCAL_MODEL if has_local else None,
        "timeout_ms": int(ollama_timeout_s() * 1000),
    }


def ollama_status_snapshot_sync() -> Dict[str, Any]:
    enabled = is_ollama_enabled()
    url = ollama_url()
    available = ollama_available_sync() if enabled else False
    models = ollama_list_models_sync() if available else []
    preferred = resolve_model()
    has_local = any(m.startswith("tradebot-local") for m in models)
    return {
        "enabled": enabled,
        "url": url,
        "available": available,
        "fallback_enabled": is_fallback_enabled(),
        "preferred_model": preferred,
        "models": models,
        "has_tradebot_local": has_local,
        "tradebot_local_tag": TRADEBOT_LOCAL_MODEL if has_local else None,
        "timeout_ms": int(ollama_timeout_s() * 1000),
    }

# Back-compat alias used by older Soup code paths
OllamaService = None  # type: ignore
