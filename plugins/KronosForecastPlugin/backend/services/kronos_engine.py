"""
Kronos Forecast Plugin — Model Engine

Thin, lazy, thread-safe wrapper around the open-source Kronos foundation model
(shiyu-coder/Kronos, MIT). Heavy imports (torch + the vendored model package)
happen on first use only, so importing this module never slows backend startup
and never crashes if the model has not been set up yet.

Setup (one-time, installs torch + vendors the model code + downloads weights):
    bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh

If the model is unavailable the engine reports `available == False` and callers
(forecast_service) transparently fall back to a heuristic forecast, so the page
and JARVIS keep working.
"""
from __future__ import annotations

import sys
import json
import threading
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from plugins.KronosForecastPlugin.backend.config import kronos_config, KRONOS_MODELS

# Where the last-selected model is persisted so a hot-swap survives a backend
# reload/restart (otherwise every restart reverts to the config default).
_STATE_FILE = Path(__file__).resolve().parent / ".selected_model.json"


class KronosEngine:
    """Singleton holding the loaded predictor. All inference is synchronous;
    callers must offload to a thread executor."""

    _instance: Optional["KronosEngine"] = None
    _lock = threading.Lock()
    # Serialises inference: the underlying Kronos model keeps a shared mutable
    # RoPE cache (seq_len_cached / cos_cached), so concurrent predict() calls
    # (e.g. /forecast + /sniper + /analyze firing together) corrupt it and raise
    # "tensor a (N) must match tensor b (M)". One inference at a time avoids this.
    _infer_lock = threading.Lock()

    def __init__(self) -> None:
        self._predictor = None
        self._load_attempted = False
        self._load_error: Optional[str] = None
        # The model/tokenizer currently loaded into the predictor (may differ
        # from the config default after a hot-swap via load_model()).
        self._active_model: Optional[str] = None
        self._active_tokenizer: Optional[str] = None
        self._active_max_context: Optional[int] = None

    @classmethod
    def instance(cls) -> "KronosEngine":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = KronosEngine()
        return cls._instance

    # ── availability ────────────────────────────────────────────

    def _ensure_vendor_on_path(self) -> bool:
        """Add the vendored Kronos `model/` package to sys.path if present."""
        # Resolve vendor dir relative to project root (parents[4] = repo root)
        vendor = Path(kronos_config.vendor_dir)
        if not vendor.is_absolute():
            project_root = Path(__file__).resolve().parents[4]
            vendor = (project_root / vendor).resolve()
        model_pkg = vendor / "model"
        if not model_pkg.exists():
            self._load_error = (
                f"Kronos model package not found at {model_pkg}. "
                f"Run: bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh"
            )
            return False
        vendor_str = str(vendor)
        if vendor_str not in sys.path:
            sys.path.insert(0, vendor_str)
        return True

    # ── persisted selection (survives reload/restart) ───────────

    @staticmethod
    def _read_selected() -> Optional[dict]:
        """Return the persisted model selection ({model, tokenizer, max_context})
        if it is valid and still in the published catalogue, else None."""
        try:
            if not _STATE_FILE.exists():
                return None
            data = json.loads(_STATE_FILE.read_text())
            model_id = data.get("model")
            if not model_id or not any(m["id"] == model_id for m in KRONOS_MODELS):
                return None
            return {
                "model": model_id,
                "tokenizer": data.get("tokenizer") or kronos_config.tokenizer_name,
                "max_context": int(data.get("max_context") or kronos_config.max_context),
            }
        except Exception:
            return None

    @staticmethod
    def _write_selected(model_name: str, tokenizer_name: str, max_context: int) -> None:
        try:
            _STATE_FILE.write_text(json.dumps({
                "model": model_name,
                "tokenizer": tokenizer_name,
                "max_context": max_context,
            }))
        except Exception as exc:
            logger.debug(f"[Kronos] could not persist model selection: {exc}")

    def _load(self) -> None:
        """Lazily load tokenizer + model + predictor. Idempotent + thread-safe.

        Honours a persisted selection (from a previous hot-swap) so the chosen
        model survives backend reloads/restarts; falls back to the config default.

        Guarded by a lock so the background warm-up and a concurrent forecast
        (in the executor) don't both load — the second caller waits for the first
        to finish, then sees the ready predictor. Must only be called from a
        worker/background thread (never the event loop), since the load can take
        tens of seconds.
        """
        # Fast path — already loaded, no lock needed.
        if self._predictor is not None:
            return
        with self._lock:
            # Double-check: another thread may have loaded (or already failed).
            if self._predictor is not None or self._load_attempted:
                return
            self._load_attempted = True

            if not self._ensure_vendor_on_path():
                logger.warning(f"[Kronos] {self._load_error}")
                return

            sel = self._read_selected()
            model_name = sel["model"] if sel else kronos_config.model_name
            tokenizer_name = sel["tokenizer"] if sel else kronos_config.tokenizer_name
            max_context = sel["max_context"] if sel else kronos_config.max_context

            try:
                import torch  # noqa: F401  (validates torch is installed)
                from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore

                logger.info(
                    f"[Kronos] Loading {model_name} "
                    f"(tokenizer={tokenizer_name}) on {kronos_config.device}"
                    + (" [restored selection]" if sel else "")
                )
                tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
                model = Kronos.from_pretrained(model_name)
                self._predictor = KronosPredictor(
                    model, tokenizer,
                    device=kronos_config.device,
                    max_context=max_context,
                )
                self._active_model = model_name
                self._active_tokenizer = tokenizer_name
                self._active_max_context = max_context
                logger.success(f"[Kronos] Model loaded and ready ({model_name})")
            except Exception as exc:  # torch missing, weights download failed, etc.
                self._load_error = f"{type(exc).__name__}: {exc}"
                self._predictor = None
                logger.warning(f"[Kronos] Model unavailable — falling back to heuristic. {self._load_error}")

    @property
    def available(self) -> bool:
        # Non-blocking: reflects current state only. Loading happens via the
        # background warm-up or inside predict_paths (executor thread) — never on
        # the event loop, so a ~60s cold-load can't freeze the backend.
        return self._predictor is not None

    @property
    def loading(self) -> bool:
        """True while a load has started but hasn't finished (warm-up in flight)."""
        return self._load_attempted and self._predictor is None and self._load_error is None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    @property
    def active_model(self) -> Optional[str]:
        """The model actually loaded into the predictor (reflects hot-swaps)."""
        return self._active_model

    @property
    def active_tokenizer(self) -> Optional[str]:
        return self._active_tokenizer

    @property
    def active_max_context(self) -> Optional[int]:
        return self._active_max_context

    # ── local install management ────────────────────────────────

    @staticmethod
    def _cached_repos() -> set:
        """Return the set of Hugging Face repo IDs already present in the local
        cache (no network). Empty set if the cache can't be scanned."""
        try:
            from huggingface_hub import scan_cache_dir  # type: ignore
            return {repo.repo_id for repo in scan_cache_dir().repos}
        except Exception:
            return set()

    def is_installed(self, model_id: str, tokenizer_id: str) -> bool:
        """True if both the model and its tokenizer weights are already cached
        locally (so selecting them won't fall back to the heuristic)."""
        cached = self._cached_repos()
        return model_id in cached and tokenizer_id in cached

    def download(self, model_id: str, tokenizer_id: str) -> bool:
        """Download a model + tokenizer weights into the local HF cache.
        Idempotent and network-only (does not load into memory). Thread-safe."""
        try:
            from huggingface_hub import snapshot_download  # type: ignore
            logger.info(f"[Kronos] Downloading {model_id} + {tokenizer_id} ...")
            snapshot_download(model_id)
            snapshot_download(tokenizer_id)
            logger.success(f"[Kronos] Downloaded {model_id}")
            return True
        except Exception as exc:
            logger.warning(f"[Kronos] Download failed for {model_id}: {type(exc).__name__}: {exc}")
            return False

    def download_all(self, models: List[dict]) -> dict:
        """Download every model/tokenizer in the given catalogue into the local
        cache so any variant can be selected instantly with no heuristic fallback.
        Returns {model_id: bool} install results."""
        results: dict = {}
        for m in models:
            results[m["id"]] = self.download(m["id"], m["tokenizer"])
        return results

    def load_model(self, model_name: str, tokenizer_name: str, max_context: int) -> bool:
        """Hot-swap to a different Kronos model variant.  Thread-safe."""
        with self._lock:
            # Ensure the vendored `model/` package is importable. After a backend
            # reload sys.path is reset, so a switch that lands before any forecast
            # (which is what triggers _load) would otherwise fail with
            # "ModuleNotFoundError: No module named 'model'".
            if not self._ensure_vendor_on_path():
                logger.warning(f"[Kronos] Model switch failed: {self._load_error}")
                return False
            try:
                from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore
                logger.info(f"[Kronos] Switching to {model_name} (ctx={max_context})")
                tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
                model = Kronos.from_pretrained(model_name)
                self._predictor = KronosPredictor(
                    model, tokenizer,
                    device=kronos_config.device,
                    max_context=max_context,
                )
                self._active_model = model_name
                self._active_tokenizer = tokenizer_name
                self._active_max_context = max_context
                self._load_error = None
                self._write_selected(model_name, tokenizer_name, max_context)
                logger.success(f"[Kronos] Switched to {model_name}")
                return True
            except Exception as exc:
                self._load_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"[Kronos] Model switch failed: {self._load_error}")
                return False

    # ── inference ───────────────────────────────────────────────

    def predict_paths(
        self,
        df: pd.DataFrame,
        x_timestamp: pd.Series,
        y_timestamp: pd.Series,
        pred_len: int,
        *,
        temperature: float,
        top_p: float,
        samples: int,
    ) -> List[pd.DataFrame]:
        """
        Run `samples` independent stochastic forecast paths.

        `df` must contain columns: open, high, low, close, volume, amount.
        Returns a list of DataFrames (one per sample), each with the predicted
        open/high/low/close/volume indexed by the future timestamps. The caller
        aggregates these into a mean forecast + confidence bands.
        """
        self._load()
        if self._predictor is None:
            raise RuntimeError(self._load_error or "Kronos model not available")

        paths: List[pd.DataFrame] = []
        # Serialise access to the shared predictor — its RoPE cache is mutable
        # instance state and is not safe under concurrent forward passes.
        with self._infer_lock:
            for _ in range(max(1, samples)):
                pred_df = self._predictor.predict(
                    df=df,
                    x_timestamp=x_timestamp,
                    y_timestamp=y_timestamp,
                    pred_len=pred_len,
                    T=temperature,
                    top_p=top_p,
                    sample_count=1,
                )
                paths.append(pred_df)
        return paths


kronos_engine = KronosEngine.instance()


def _warmup_in_background() -> None:
    """Load the model in a daemon thread at import time so the first user
    forecast isn't blocked by the (large) default model's ~60s cold-load.
    Non-blocking: the backend finishes starting while this runs. Idempotent —
    _load() guards against repeat work. Disable with KRONOS_WARMUP=0."""
    import os
    if os.getenv("KRONOS_WARMUP", "1").strip().lower() in ("0", "false", "no"):
        return

    def _run() -> None:
        try:
            logger.info("[Kronos] Background warm-load starting…")
            kronos_engine._load()
            if kronos_engine.available:
                logger.success(f"[Kronos] Warm-load complete ({kronos_engine.active_model})")
        except Exception as exc:
            logger.debug(f"[Kronos] warm-load skipped: {exc}")

    threading.Thread(target=_run, name="kronos-warmup", daemon=True).start()


_warmup_in_background()
