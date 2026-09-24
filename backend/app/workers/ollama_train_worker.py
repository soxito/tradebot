"""Ollama training worker — scheduled Soup fine-tune on Brain+trades → GGUF → ollama create."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[3]
OLLAMA_TRAIN_INTERVAL_H = int(os.getenv("OLLAMA_TRAIN_INTERVAL_HOURS", "24"))
OLLAMA_MIN_SAMPLES = int(os.getenv("OLLAMA_MIN_SAMPLES_FOR_TRAIN", "200"))


async def maybe_train_ollama(force: bool = False) -> dict:
    """Called by scheduler. Returns status dict; never raises."""
    if not force and os.getenv("OLLAMA_ENABLED", "1").lower() in ("0", "false", "no", "off"):
        return {"ok": False, "reason": "OLLAMA_ENABLED=false"}
    if not force and os.getenv("AUTO_START_OLLAMA_TRAIN_LOOP", "1").lower() in ("0", "false", "no", "off"):
        return {"ok": False, "reason": "AUTO_START_OLLAMA_TRAIN_LOOP disabled"}

    # Tier gate: minimal/low machines skip training unless forced
    try:
        from app.core.config import settings

        tier = (getattr(settings, "PERF_TIER", "") or "").lower()
        if tier in ("minimal", "low") and not force and os.getenv("TRADEBOT_FORCE_OLLAMA_TRAIN", "").lower() not in ("1", "true", "yes", "on"):
            return {"ok": False, "reason": f"tier {tier} — training skipped (set TRADEBOT_FORCE_OLLAMA_TRAIN=1 to force)"}
    except Exception:
        pass

    try:
        from plugins.SoupPlugin.backend.services.soup_ollama_bridge import (  # type: ignore
            build_sharegpt_dataset_async,
            OllamaBridgeDataDir,
            should_train,
            mark_trained,
            trigger_soup_finetune_and_import,
        )
        from app.services.ollama_service import tier_for_ram  # type: ignore
    except Exception as exc:
        return {"ok": False, "reason": f"bridge import failed: {exc}"}

    # Time gate
    if not force:
        ok, reason = should_train()
        if not ok:
            return {"ok": False, "reason": reason}

    # Build dataset (async variant so trade rows are included) — always include vault + web + pair catalog for self-improvement
    try:
        dataset_path, n_rows = await build_sharegpt_dataset_async(include_pair_catalog=True)
    except Exception as exc:
        return {"ok": False, "reason": f"dataset build failed: {exc}"}

    if n_rows < OLLAMA_MIN_SAMPLES and not force:
        logger.info(f"[ollama-train] only {n_rows} rows < min {OLLAMA_MIN_SAMPLES} — deferring training")
        return {"ok": False, "reason": f"only {n_rows} rows < min {OLLAMA_MIN_SAMPLES}", "rows": n_rows, "path": str(dataset_path)}

    # Trigger soup train in thread pool so event loop stays responsive (can take hours)
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: trigger_soup_finetune_and_import(dataset_path)
        )
    except Exception as exc:
        return {"ok": False, "reason": f"train failed: {exc}", "rows": n_rows}

    if result.get("ok"):
        mark_trained()
        logger.info(f"[ollama-train] success — rows {n_rows} → {result.get('model') or result.get('adapter_dir')}")
    else:
        logger.warning(f"[ollama-train] failed: {result.get('error')} (rows {n_rows})")
    result["rows"] = n_rows
    result["dataset"] = str(dataset_path)
    return result
