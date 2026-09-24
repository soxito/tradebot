#!/usr/bin/env python3
"""Train tradebot-local (0.5B) on ALL app pairs.

Builds one ShareGPT dataset from every app source — Brain, trades, signals,
sentiment, news, Obsidian vault, the local-LLM interaction buffer, live web
research, name/metadata knowledge for **every** tradeable pair on the app
(crypto_pairs, ~2000+), and OHLCV forecasting windows for the liquid majors +
the four macro symbols (CADJPY, XAUUSD, US30, XAGUSD) — then runs the real
soup fine-tune → GGUF → `ollama create tradebot-local:latest`.

Base is Qwen/Qwen2.5-0.5B-Instruct: the largest base that reliably completes
LoRA on this 16GB Apple machine (1.5B hits an MPS backward-gradient bug).

Memory: MPS OOMs if max_length/batch are large, so we force a small, concrete
footprint and unload Ollama models first to free unified memory. A recent
dataset is reused so a retry skips the slow OHLCV re-fetch.

Run:  ./backend/.venv/bin/python scripts/run_soup_training.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
# plugins.* needs repo root; app.* needs backend/ on the path.
for p in (str(ROOT), str(BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Small, safe MPS footprint — set BEFORE torch/soup import so the child inherits.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("SOUP_MAX_LENGTH", "2048")
os.environ.setdefault("SOUP_BATCH_SIZE", "1")

BASE = os.getenv("SOUP_TRAIN_BASE", "Qwen/Qwen2.5-0.5B-Instruct")
EPOCHS = int(os.getenv("SOUP_TRAIN_EPOCHS", "1"))
DATA_DIR = ROOT / "data" / "ollama_training"


def _free_unified_memory() -> None:
    """Unload any resident Ollama models so MPS has room for training."""
    try:
        r = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=15)
        for line in (r.stdout or "").splitlines()[1:]:
            parts = line.split()
            name = parts[0] if parts else ""
            if name and name != "NAME":
                subprocess.run(["ollama", "stop", name], capture_output=True, text=True, timeout=30)
                print(f"[train] unloaded ollama model {name} to free memory", flush=True)
    except Exception as exc:
        print(f"[train] ollama unload skipped: {exc}", flush=True)


def _reuse_dataset() -> "Path | None":
    explicit = os.getenv("SOUP_REUSE_DATASET")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    max_age = int(os.getenv("SOUP_REUSE_MAX_AGE", "7200"))
    try:
        cands = sorted(DATA_DIR.glob("tradebot-ollama-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cands and (time.time() - cands[0].stat().st_mtime) < max_age:
            return cands[0]
    except Exception:
        pass
    return None


def _count_rows(p: Path) -> int:
    try:
        with p.open(encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
    except Exception:
        return 0


def _sanitize_dataset(src: Path) -> tuple[Path, int]:
    """Re-apply _fit_row at the current SOUP_MAX_LENGTH so no row's assistant
    target can be truncated to empty (a reused dataset was fit for a different
    budget). Writes a sibling *.fit.jsonl and returns (path, kept_rows).
    """
    import json
    from plugins.SoupPlugin.backend.services.soup_ollama_bridge import _fit_row
    dst = src.with_suffix(".fit.jsonl")
    kept = 0
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for ln in fin:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = _fit_row(json.loads(ln))
            except Exception:
                row = None
            if row is None:
                continue
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            kept += 1
    print(f"[train] sanitized dataset for max_length={os.environ['SOUP_MAX_LENGTH']}: "
          f"{kept} rows → {dst}", flush=True)
    return dst, kept


async def _build() -> tuple[Path, int]:
    from plugins.SoupPlugin.backend.services.soup_ollama_bridge import (
        build_sharegpt_dataset_async,
        LIQUID_OHLCV_SYMBOLS,
    )
    print(f"[train] building dataset — {len(LIQUID_OHLCV_SYMBOLS)} OHLCV symbols "
          f"+ full pair catalogue + all app sources …", flush=True)
    ds_path, n = await build_sharegpt_dataset_async(
        symbols=LIQUID_OHLCV_SYMBOLS,
        include_pair_catalog=True,     # every tradeable pair on the app
        ohlcv_timeframe="1h",
        ohlcv_limit=1500,
        ohlcv_windows_per_symbol=60,   # bounded so all-symbol build stays fast
        ohlcv_lookback=24,
        ohlcv_pred_len=8,
    )
    print(f"[train] dataset ready: {n} rows → {ds_path}", flush=True)
    return ds_path, n


def main() -> int:
    reuse = _reuse_dataset()
    if reuse is not None:
        ds_path, n = reuse, _count_rows(reuse)
        print(f"[train] reusing recent dataset: {n} rows → {ds_path}", flush=True)
    else:
        ds_path, n = asyncio.run(_build())
    if n < 20:
        print(f"[train] too few rows ({n}) — aborting.", flush=True)
        return 2

    # Guarantee every row fits the current max_length before training.
    ds_path, n = _sanitize_dataset(ds_path)
    if n < 20:
        print(f"[train] too few rows after sanitize ({n}) — aborting.", flush=True)
        return 2

    _free_unified_memory()

    from plugins.SoupPlugin.backend.services.soup_ollama_bridge import (
        trigger_soup_finetune_and_import,
    )
    print(f"[train] starting soup fine-tune  base={BASE}  epochs={EPOCHS}  "
          f"max_length={os.environ['SOUP_MAX_LENGTH']}  batch={os.environ['SOUP_BATCH_SIZE']}",
          flush=True)
    res = trigger_soup_finetune_and_import(
        dataset_path=ds_path, base_model=BASE, task="sft", epochs=EPOCHS,
    )
    print(f"[train] result: {res}", flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
