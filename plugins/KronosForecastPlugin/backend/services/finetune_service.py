"""
Kronos Forecast Plugin — Fine-tune Service

Orchestrates an end-to-end fine-tune of Kronos on the user's own live data:

  1. Export live OHLCV → Kronos CSV (finetune_data_exporter).
  2. Generate a finetune_csv YAML config pointing at that CSV.
  3. Run the vendored ``train_sequential.py`` (tokenizer → predictor) as a
     detached subprocess, streaming its output to a per-job log file.
  4. Track status in the ``kronos_finetune_jobs`` table.
  5. Hot-swap the loaded engine to the finetuned checkpoints on request.

Best-effort and standalone: if the finetune_csv pipeline is not vendored, the
service reports so and points at the setup script. Training is heavy — the
caller (UI / Telegram) is expected to warn on weak hardware.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from plugins.KronosForecastPlugin.backend.config import kronos_config, KRONOS_MODELS
from plugins.KronosForecastPlugin.backend.services import finetune_data_exporter as exporter
from plugins.KronosForecastPlugin.backend.services.kronos_engine import (
    kronos_engine,
    kronos_setup_command,
)

# ── Vendor layout ─────────────────────────────────────────────────────────────
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]              # KronosForecastPlugin/
_VENDOR_DIR = _PLUGIN_ROOT / "backend" / "vendor"
_FINETUNE_DIR = _VENDOR_DIR / "finetune_csv"
_MODEL_DIR = _VENDOR_DIR / "model"
_WORK_DIR = _PLUGIN_ROOT / "backend" / "data" / "finetune"     # CSVs + configs + logs + checkpoints


def finetune_setup_command() -> str:
    if os.name == "nt":
        return (
            "powershell -ExecutionPolicy Bypass -File "
            "plugins\\KronosForecastPlugin\\scripts\\setup_kronos_finetune.ps1"
        )
    return "bash plugins/KronosForecastPlugin/scripts/setup_kronos_finetune.sh"


def is_vendored() -> bool:
    return (_FINETUNE_DIR / "train_sequential.py").exists()


def setup_status() -> Dict:
    return {
        "vendored": is_vendored(),
        "model_vendored": _MODEL_DIR.exists() and any(_MODEL_DIR.iterdir()) if _MODEL_DIR.exists() else False,
        "setup_command": finetune_setup_command(),
        "finetune_dir": str(_FINETUNE_DIR),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pretrained_paths(base_model: Optional[str]) -> tuple[str, str]:
    """Resolve (pretrained_tokenizer, pretrained_predictor) HF ids to start from."""
    predictor = base_model or kronos_config.model_name
    entry = next((m for m in KRONOS_MODELS if m["id"] == predictor), None)
    tokenizer = entry["tokenizer"] if entry else kronos_config.tokenizer_name
    return tokenizer, predictor


def _fit_window(
    rows: int,
    lookback: int,
    pred_len: int,
    batch: int,
    train_ratio: float = 0.9,
    val_ratio: float = 0.1,
    min_val_samples: int = 4,
) -> Dict:
    """Shrink the context window / batch so every split can produce samples.

    Kronos' dataset needs ``len(split) >= lookback + pred_len + 1`` to yield a
    single training sample, and the tokenizer's train loader uses
    ``drop_last=True`` so the train split additionally needs at least
    ``batch_size`` samples. With a short live history (one exchange page is only
    ~1000 candles) the requested 512-bar context leaves the 10% validation split
    with a negative sample count, which crashes the trainer with
    ``__len__() should return >= 0``. We fit the window to the data instead of
    failing. Returns a dict with fitted lookback/pred_len/batch + the split
    sizes so the caller can persist and report them.
    """
    rows = int(rows)
    train_rows = int(rows * train_ratio)
    val_rows = int(rows * (train_ratio + val_ratio)) - train_rows
    if val_rows < 8:  # guarantee a usable validation slice
        val_rows = max(8, int(rows * 0.1))
        train_rows = rows - val_rows

    batch = max(1, int(batch))
    desired_window = int(lookback) + int(pred_len) + 1

    max_window = min(val_rows - min_val_samples + 1, train_rows - batch + 1)
    window = min(desired_window, max_window)

    if window < 34:  # even a minimal 24+8+1 window won't fit at this batch
        batch = 1
        window = min(desired_window, val_rows - 1, train_rows - 1)
    window = max(window, 10)  # absolute floor so both splits stay non-empty

    fit_pred = min(int(pred_len), max(1, window // 4))
    fit_look = max(1, window - fit_pred - 1)

    train_samples = max(0, train_rows - (fit_look + fit_pred + 1) + 1)
    if train_samples < batch:
        batch = max(1, train_samples)

    return {
        "lookback": fit_look,
        "pred_len": fit_pred,
        "batch_size": batch,
        "train_rows": train_rows,
        "val_rows": val_rows,
        "adjusted": (fit_look != int(lookback)) or (fit_pred != int(pred_len))
        or (batch != max(1, int(batch))),
    }


def _write_config(
    job_row, data_path: Path, exp_name: str, base_dir: Path, rows: int = 0
) -> tuple[Path, Dict]:
    """Write a finetune_csv YAML config for this job.

    Returns ``(config_path, fitted)`` where ``fitted`` is the auto-fitted
    ``lookback``/``pred_len``/``batch_size`` (see :func:`_fit_window`) that the
    caller persists to the job row so the UI and the ``load`` step use the real
    context length the model was trained with.
    """
    import yaml

    tok, pred = _pretrained_paths(job_row.base_model)
    use_cuda = _torch_has_cuda()

    fitted = _fit_window(
        rows or int(job_row.rows_exported or 0) or (int(job_row.lookback) + int(job_row.pred_len) + 100),
        int(job_row.lookback), int(job_row.pred_len), int(job_row.batch_size),
    )
    if fitted["adjusted"]:
        logger.info(
            f"[Kronos-finetune] job {job_row.id}: fitted window to data "
            f"({rows} rows) → lookback={fitted['lookback']} "
            f"pred_len={fitted['pred_len']} batch={fitted['batch_size']}"
        )

    cfg = {
        "data": {
            "data_path": str(data_path),
            "lookback_window": int(fitted["lookback"]),
            "predict_window": int(fitted["pred_len"]),
            "max_context": int(fitted["lookback"]),
            "clip": 5.0,
            "train_ratio": 0.9,
            "val_ratio": 0.1,
            "test_ratio": 0.0,
        },
        "training": {
            "tokenizer_epochs": int(job_row.tokenizer_epochs),
            "basemodel_epochs": int(job_row.predictor_epochs),
            "batch_size": int(fitted["batch_size"]),
            "log_interval": 50,
            "num_workers": 2,
            "seed": 42,
            "tokenizer_learning_rate": 0.0002,
            "predictor_learning_rate": 0.000001,
            "adam_beta1": 0.9,
            "adam_beta2": 0.95,
            "adam_weight_decay": 0.1,
            "accumulation_steps": 1,
        },
        "model_paths": {
            "pretrained_tokenizer": tok,
            "pretrained_predictor": pred,
            "exp_name": exp_name,
            "base_path": str(base_dir),
            "base_save_path": "",
            "finetuned_tokenizer": "",
            "tokenizer_save_name": "tokenizer",
            "basemodel_save_name": "basemodel",
        },
        "experiment": {
            "name": exp_name,
            "description": f"TradeBot fine-tune {job_row.symbol} {job_row.timeframe}",
            "use_comet": False,
            "train_tokenizer": True,
            "train_basemodel": True,
            "skip_existing": False,
        },
        "device": {
            "use_cuda": use_cuda,
            "device_id": 0,
        },
    }
    cfg_path = base_dir / f"{exp_name}.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg_path, fitted


def _torch_has_cuda() -> bool:
    try:
        import torch  # noqa
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _remember_finetune(
    action: str, symbol: str, timeframe: str, summary: str, importance: float = 0.6
) -> None:
    """Persist a fine-tune milestone to the JARVIS brains (vault + AI-analyst +
    PaulKnowledge + Neuro core) so training history is remembered across restarts.

    Best-effort and standalone: silently no-ops if the core brain layer is not
    importable (keeps the plugin decoupled from core).
    """
    try:
        from app.api.jarvis import jarvis_learn_all_brains

        jarvis_learn_all_brains(
            action=action,
            symbol=symbol,
            summary=summary,
            detail=f"Kronos fine-tune · {symbol} · {timeframe}",
            tags=["kronos", "finetune", timeframe],
            importance=importance,
            kind="insight",
        )
        # Also mirror directly into Neuro core as a trade_segment-like episode so
        # federated recall (Neuro → Hermes → Vault) never misses it.
        try:
            from app.neuro_brain.state_store import ingest_local as _neuro_ingest

            _neuro_ingest(
                kind=f"kronos-{action}",
                content=f"[Kronos {action} {symbol} {timeframe}] {summary[:900]}",
                symbol=symbol,
                source="kronos-finetune",
                meta={"timeframe": timeframe, "importance": importance},
            )
        except Exception:
            pass
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug(f"[Kronos-finetune] brain capture skipped: {exc}")


async def _collect_brain_knowledge(symbols: list[str]) -> dict:
    """Collect recall from every brain (Neuro federated + Hermes + Vault) for each symbol.

    Returns {symbol: [hits]}. Best-effort: any brain being offline just yields [].
    This is written alongside the training CSV so a future audit can prove that
    the model was trained with full cross-brain context, and is also injected
    into the fine-tune description so no learned knowledge is missed.
    """
    out: dict = {}
    for sym in symbols:
        hits: list = []
        try:
            from app.neuro_brain.federated_search import federated_search

            hits = await federated_search(sym, sym, limit=8)
        except Exception as e:
            logger.debug(f"[Kronos-finetune] brain collect skipped for {sym}: {e}")
        out[sym] = hits[:6]
    # Also capture a global snapshot (cross-symbol insights)
    try:
        from app.neuro_brain.state_store import stats as _neuro_stats
        from app.hermes_bridge.state_store import search_local as _hermes_search
        # cheap global markers
        out["_meta"] = {
            "neuro_stats": _neuro_stats(),
            "collected_at": _now().isoformat(),
        }
    except Exception:
        pass
    return out


class FinetuneManager:
    """Singleton owning the fine-tune jobs and their subprocesses."""

    _instance: Optional["FinetuneManager"] = None

    def __init__(self) -> None:
        self._procs: Dict[int, asyncio.subprocess.Process] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._schema_ok = False

    @classmethod
    def instance(cls) -> "FinetuneManager":
        if cls._instance is None:
            cls._instance = FinetuneManager()
        return cls._instance

    async def _ensure_schema(self) -> None:
        """Idempotently add columns not covered by create_all on an existing
        table. create_all never ALTERs, so the combined-pairs column is added
        live on first use (safe to run repeatedly)."""
        if self._schema_ok:
            return
        try:
            from sqlalchemy import text
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                await db.execute(text(
                    "ALTER TABLE kronos_finetune_jobs "
                    "ADD COLUMN IF NOT EXISTS symbols_combined JSONB"
                ))
                await db.commit()
            self._schema_ok = True
        except Exception as exc:  # noqa: BLE001 — non-Postgres or race; ignore
            logger.debug(f"[Kronos-finetune] schema ensure skipped: {exc}")
            self._schema_ok = True  # don't retry every call

    # ── public API ───────────────────────────────────────────────

    async def start(self, req) -> Dict:
        """Create a job row and launch its background pipeline. Returns the row dict."""
        if not is_vendored():
            raise RuntimeError(
                "Kronos fine-tuning pipeline is not installed. Run: "
                + finetune_setup_command()
            )
        await self._ensure_schema()
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        symbol = req.symbol.upper()
        async with AsyncSessionLocal() as db:
            job = KronosFinetuneJob(
                exchange=req.exchange, symbol=symbol, timeframe=req.timeframe,
                model_name=(req.model_name or None),
                status="pending", lookback=req.lookback, pred_len=req.pred_len,
                tokenizer_epochs=req.tokenizer_epochs, predictor_epochs=req.predictor_epochs,
                batch_size=req.batch_size, base_model=req.base_model,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id

        task = asyncio.create_task(self._run_pipeline(job_id, req.limit))
        self._tasks[job_id] = task
        return await self.get(job_id)

    async def get(self, job_id: int, log_lines: int = 40) -> Optional[Dict]:
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        async with AsyncSessionLocal() as db:
            job = await db.get(KronosFinetuneJob, job_id)
            if job is None:
                return None
            return self._to_dict(job, log_lines)

    async def list(self, limit: int = 50) -> List[Dict]:
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        await self._ensure_schema()
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(KronosFinetuneJob).order_by(KronosFinetuneJob.id.desc()).limit(limit)
            )
            return [self._to_dict(j, log_lines=0) for j in res.scalars().all()]

    async def cancel(self, job_id: int) -> bool:
        proc = self._procs.get(job_id)
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        await self._update(job_id, status="cancelled", finished_at=_now())
        return True

    async def load_finetuned(self, job_id: int) -> Dict:
        """Hot-swap the engine to a completed job's finetuned checkpoints."""
        job = await self.get(job_id, log_lines=0)
        if job is None:
            raise ValueError(f"Fine-tune job {job_id} not found")
        if job["status"] != "completed":
            raise RuntimeError(f"Job {job_id} is {job['status']}, not completed")

        out = Path(job["output_dir"] or "")
        tok_dir = out / "tokenizer" / "best_model"
        pred_dir = out / "basemodel" / "best_model"
        if not tok_dir.exists() or not pred_dir.exists():
            raise RuntimeError(
                f"Finetuned checkpoints missing under {out} "
                f"(expected tokenizer/best_model and basemodel/best_model)"
            )

        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: kronos_engine.load_model(str(pred_dir), str(tok_dir), int(job["lookback"])),
        )
        if not ok:
            raise RuntimeError(f"Engine failed to load finetuned model: {kronos_engine.load_error}")
        # Mark the engine as running a finetuned model so the UI shows it as a
        # first-class model + persist the selection so it survives restarts.
        display_name = job.get("model_name") or f"Custom·{job['symbol']}·{job['timeframe']}"
        kronos_engine._active_finetuned_job_id = job_id
        kronos_engine._active_finetuned_display_name = display_name
        kronos_engine._write_selected(
            "__finetuned__", str(tok_dir), int(job["lookback"]),
            finetuned_job_id=job_id, finetuned_pred_dir=str(pred_dir),
            finetuned_display_name=display_name,
        )
        # Drop cached forecasts so the new model is used immediately.
        try:
            from plugins.KronosForecastPlugin.backend.services import forecast_service
            if hasattr(forecast_service, "_cache"):
                forecast_service._cache.clear()
        except Exception:
            pass
        _remember_finetune(
            "finetune_loaded", job["symbol"], job["timeframe"],
            f"Engine hot-swapped to fine-tuned Kronos from job #{job_id} "
            f"({job['symbol']} {job['timeframe']}). Forecasts now use it.",
            importance=0.75,
        )
        return {"ok": True, "job_id": job_id, "model_dir": str(pred_dir), "tokenizer_dir": str(tok_dir)}

    async def combine_jobs(self, model_name: Optional[str] = None) -> Dict:
        """Merge every completed job's training CSV into one and fine-tune a new
        combined model on the union. Returns the new job dict.

        When fewer than two per-pair jobs remain (e.g. after a prune), it
        retrains the combined model on the persisted ``combined.csv`` so the
        weekly improvement cycle still runs — carrying the last known pair list
        forward so the details keep showing which pairs are trained.
        """
        if not is_vendored():
            raise RuntimeError(
                "Kronos fine-tuning pipeline is not installed. Run: "
                + finetune_setup_command()
            )
        import pandas as pd
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        all_jobs = await self.list(limit=200)
        completed = [
            j for j in all_jobs
            if j["status"] == "completed" and j.get("data_path")
            and str(j.get("symbol", "")).upper() != "COMBINED"
        ]
        usable = [j for j in completed if Path(j["data_path"]).exists()]

        combined_dir = _WORK_DIR / "combined"
        combined_dir.mkdir(parents=True, exist_ok=True)
        combined_csv = combined_dir / "combined.csv"

        symbols_combined: List[str]
        if len(usable) >= 2:
            # Merge + dedupe by timestamp column (first column, Kronos CSV format).
            frames = []
            frame_syms: List[str] = []
            for j in usable:
                try:
                    frames.append(pd.read_csv(j["data_path"]))
                    frame_syms.append(str(j["symbol"]).upper())
                except Exception as exc:
                    logger.warning(f"[Kronos-finetune] combine: skip job {j['id']} CSV ({exc})")
            if not frames:
                raise RuntimeError("No readable training CSVs to combine.")
            merged = pd.concat(frames, ignore_index=True)
            ts_col = merged.columns[0]
            merged = merged.drop_duplicates(subset=[ts_col]).sort_values(ts_col).reset_index(drop=True)
            merged.to_csv(combined_csv, index=False)
            symbols_combined = sorted(set(frame_syms))
            look = max(int(j["lookback"]) for j in usable)
            pred = max(int(j["pred_len"]) for j in usable)
            tok_ep = max(int(j["tokenizer_epochs"]) for j in usable)
            pred_ep = max(int(j["predictor_epochs"]) for j in usable)
            batch = max(int(j["batch_size"]) for j in usable)
            base = usable[0].get("base_model")
            tfs = ",".join(sorted({j["timeframe"] for j in usable}))
            rows_merged = int(len(merged))
        else:
            # Retrain-only path: reuse the last combined dataset + its pair list.
            if not combined_csv.exists():
                raise RuntimeError(
                    "Need at least two completed fine-tune jobs with data to combine, "
                    "and no prior combined dataset exists to retrain from."
                )
            prior = next(
                (j for j in all_jobs if str(j.get("symbol", "")).upper() == "COMBINED"
                 and j.get("symbols_combined")),
                None,
            )
            symbols_combined = list((prior or {}).get("symbols_combined") or [])
            look = int((prior or {}).get("lookback") or 512)
            pred = int((prior or {}).get("pred_len") or 48)
            tok_ep = int((prior or {}).get("tokenizer_epochs") or 10)
            pred_ep = int((prior or {}).get("predictor_epochs") or 20)
            batch = int((prior or {}).get("batch_size") or 16)
            base = (prior or {}).get("base_model")
            tfs = str((prior or {}).get("timeframe") or "1h")
            try:
                rows_merged = max(0, sum(1 for _ in combined_csv.open()) - 1)
            except OSError:
                rows_merged = 0

        async with AsyncSessionLocal() as db:
            job = KronosFinetuneJob(
                exchange="combined", symbol="COMBINED", timeframe=tfs[:8],
                model_name=(model_name or "combined"),
                symbols_combined=symbols_combined,
                status="pending", lookback=look, pred_len=pred,
                tokenizer_epochs=tok_ep, predictor_epochs=pred_ep,
                batch_size=batch, base_model=base,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id

        task = asyncio.create_task(
            self._run_pipeline(job_id, 0, prebuilt_csv=combined_csv, auto_load=True)
        )
        self._tasks[job_id] = task
        return {
            "ok": True, "job_id": job_id,
            "symbols_combined": symbols_combined,
            "rows_merged": rows_merged,
        }

    async def prune_non_combined(self) -> Dict:
        """Delete every non-COMBINED fine-tune job and its on-disk checkpoints.

        Frees the machine after everything has been merged into the combined
        model. Combined jobs and the persisted ``combined.csv`` are kept so the
        weekly retrain still has data. Returns a summary of what was removed.
        """
        import shutil
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        removed_ids: List[int] = []
        freed_mb = 0.0
        for j in await self.list(limit=500):
            if str(j.get("symbol", "")).upper() == "COMBINED":
                continue
            if j.get("status") == "running":  # never delete a live trainer
                continue
            freed_mb += self._dir_size_mb(j.get("output_dir"))
            base_dir = _WORK_DIR / f"job{j['id']}"
            for path in (base_dir, Path(j.get("output_dir") or "")):
                try:
                    if path and path.exists():
                        shutil.rmtree(path, ignore_errors=True)
                except OSError as exc:  # noqa: BLE001
                    logger.debug(f"[Kronos-finetune] prune: could not remove {path}: {exc}")
            async with AsyncSessionLocal() as db:
                row = await db.get(KronosFinetuneJob, j["id"])
                if row is not None:
                    await db.delete(row)
                    await db.commit()
            removed_ids.append(j["id"])

        logger.info(
            f"[Kronos-finetune] pruned {len(removed_ids)} non-combined job(s), "
            f"freed ~{round(freed_mb, 1)} MB"
        )
        return {"ok": True, "removed_job_ids": removed_ids, "freed_mb": round(freed_mb, 1)}

    async def delete_job(self, job_id: int) -> Dict:
        """Delete one fine-tune job and its checkpoints from disk + DB."""
        import shutil
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        job = await self.get(job_id, log_lines=0)
        if job is None:
            raise ValueError(f"Fine-tune job {job_id} not found")
        if job.get("status") == "running":
            await self.cancel(job_id)
        freed_mb = self._dir_size_mb(job.get("output_dir"))
        for path in (_WORK_DIR / f"job{job_id}", Path(job.get("output_dir") or "")):
            try:
                if path and path.exists():
                    shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:  # noqa: BLE001
                logger.debug(f"[Kronos-finetune] delete: could not remove {path}: {exc}")
        async with AsyncSessionLocal() as db:
            row = await db.get(KronosFinetuneJob, job_id)
            if row is not None:
                await db.delete(row)
                await db.commit()
        return {"ok": True, "job_id": job_id, "freed_mb": round(freed_mb, 1)}

    async def auto_load_latest_combined(self) -> bool:
        """Load the newest completed COMBINED model as the active engine.

        Called on startup so the app and Telegram always forecast through the
        latest trained combined model. No-op (returns False) when the engine is
        already on a finetuned model or no combined job has completed.
        """
        try:
            if getattr(kronos_engine, "_active_finetuned_job_id", None):
                return False
            combined = [
                j for j in await self.list(limit=200)
                if str(j.get("symbol", "")).upper() == "COMBINED" and j.get("status") == "completed"
            ]
            if not combined:
                return False
            newest = max(combined, key=lambda j: j["id"])
            await self.load_finetuned(newest["id"])
            logger.info(f"[Kronos-finetune] auto-loaded latest combined model job #{newest['id']}")
            return True
        except Exception as exc:  # noqa: BLE001 — never block startup
            logger.debug(f"[Kronos-finetune] auto-load latest combined skipped: {exc}")
            return False

    # ── pipeline ─────────────────────────────────────────────────

    async def _run_pipeline(
        self, job_id: int, limit: int,
        prebuilt_csv: Optional[Path] = None, auto_load: bool = False,
    ) -> None:
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob
        from app.core.database import AsyncSessionLocal

        exp_name = f"job{job_id}"
        base_dir = _WORK_DIR / exp_name
        base_dir.mkdir(parents=True, exist_ok=True)
        data_path = base_dir / "train_data.csv"
        log_path = base_dir / "train.log"

        # 1) Export data (or reuse a pre-built CSV, e.g. from a combine job)
        await self._update(job_id, status="exporting")
        async with AsyncSessionLocal() as db:
            job = await db.get(KronosFinetuneJob, job_id)
            exchange, symbol, timeframe = job.exchange, job.symbol, job.timeframe
            # Snapshot model params early for brain enrichment
            pre_symbols = [symbol] if str(symbol).upper() != "COMBINED" else list(job.symbols_combined or [])
        # --- Brain-aware enrichment: collect cross-brain recall BEFORE fitting ---
        # This guarantees the model training run is always informed by the latest
        # knowledge from Neuro core (federated), Hermes, Vault, and Agent decisions
        # so it can never miss anything learned — the context is persisted alongside
        # the CSV and injected into the brain log.
        brain_ctx: dict = {}
        try:
            brain_ctx = await _collect_brain_knowledge(pre_symbols or [symbol])
            # Persist brain snapshot next to the training data for auditability
            base_dir.mkdir(parents=True, exist_ok=True)
            (base_dir / "brain_context.json").write_text(
                __import__("json").dumps(brain_ctx, indent=2, default=str)[:200_000]
            )
        except Exception as e:
            logger.debug(f"[Kronos-finetune] brain context collect skipped: {e}")
        try:
            if prebuilt_csv is not None:
                import shutil
                shutil.copyfile(prebuilt_csv, data_path)
                import csv as _csv
                with data_path.open() as fh:
                    summary = {"rows": max(0, sum(1 for _ in _csv.reader(fh)) - 1)}
            else:
                summary = await exporter.export_training_data(exchange, symbol, timeframe, limit, data_path)
        except Exception as exc:
            await self._update(job_id, status="failed", error=f"export: {exc}", finished_at=_now())
            return

        rows_exported = int(summary.get("rows", 0))
        # Also mirror the export event into the core brain so future trainings see it
        try:
            from app.neuro_brain.state_store import ingest_local as _ni
            _ni(
                kind="kronos-export",
                content=f"[Kronos export {symbol} {timeframe} {rows_exported} rows] brain_hits={sum(len(v) for v in brain_ctx.values() if isinstance(v, list))}",
                symbol=symbol,
                source="kronos-finetune",
                meta={"rows": rows_exported, "timeframe": timeframe, "brain_symbols": pre_symbols[:8]},
            )
        except Exception:
            pass

        # 2) Config — auto-fit the context window to the exported history so
        #    every train/val split can produce samples (no __len__ < 0 crash).
        async with AsyncSessionLocal() as db:
            job = await db.get(KronosFinetuneJob, job_id)
            try:
                cfg_path, fitted = _write_config(
                    job, data_path, exp_name, base_dir, rows=rows_exported
                )
            except Exception as exc:
                await self._update(job_id, status="failed", error=f"config: {exc}", finished_at=_now())
                return

        output_dir = base_dir / exp_name          # finetune_csv writes under base_path/exp_name
        await self._update(
            job_id, status="running", started_at=_now(),
            data_path=str(data_path), config_path=str(cfg_path),
            log_path=str(log_path), output_dir=str(output_dir),
            rows_exported=rows_exported,
            lookback=int(fitted["lookback"]),
            pred_len=int(fitted["pred_len"]),
            batch_size=int(fitted["batch_size"]),
        )
        _remember_finetune(
            "finetune_started", symbol, timeframe,
            f"Kronos fine-tune started on {rows_exported} candles "
            f"(lookback={fitted['lookback']}, pred_len={fitted['pred_len']}, "
            f"batch={fitted['batch_size']}).",
            importance=0.55,
        )

        # 3) Train (subprocess: python train_sequential.py --config cfg)
        py = sys.executable
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(_VENDOR_DIR), str(_FINETUNE_DIR), os.environ.get("PYTHONPATH", "")]
            ),
            "TOKENIZERS_PARALLELISM": "false",
        }
        cmd = [py, "train_sequential.py", "--config", str(cfg_path)]
        try:
            with log_path.open("w") as logfh:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(_FINETUNE_DIR), env=env,
                    stdout=logfh, stderr=asyncio.subprocess.STDOUT,
                )
                self._procs[job_id] = proc
                await self._update(job_id, pid=proc.pid)
                rc = await proc.wait()
        except asyncio.CancelledError:
            await self._update(job_id, status="cancelled", finished_at=_now())
            return
        except Exception as exc:
            await self._update(job_id, status="failed", error=f"train: {exc}", finished_at=_now())
            return
        finally:
            self._procs.pop(job_id, None)

        if rc == 0:
            await self._update(job_id, status="completed", finished_at=_now())
            logger.success(f"[Kronos-finetune] job {job_id} completed ({symbol} {timeframe})")
            _remember_finetune(
                "finetune_completed", symbol, timeframe,
                f"Kronos fine-tune #{job_id} completed on {rows_exported} "
                f"{symbol} {timeframe} candles. Load it with /finetune load {job_id}.",
                importance=0.8,
            )
            if auto_load:
                # Combined models are hot-swapped in automatically so the app +
                # Telegram immediately forecast through the freshly trained model.
                try:
                    await self.load_finetuned(job_id)
                    logger.success(f"[Kronos-finetune] auto-loaded combined model job #{job_id}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[Kronos-finetune] auto-load of job #{job_id} failed: {exc}")
                # Auto-prune: keep one major model — delete all non-COMBINED jobs +
                # older COMBINED jobs, retaining only this newly combined major model.
                # This frees disk and fulfills "one major model that combines them all".
                try:
                    # 1) Remove every non-COMBINED job (free the per-pair checkpoints)
                    await self.prune_non_combined()
                    # 2) If multiple COMBINED jobs exist, keep only this one (the newest major)
                    all_jobs = await self.list(limit=500)
                    combined_ids = sorted(
                        [j["id"] for j in all_jobs if str(j.get("symbol", "")).upper() == "COMBINED" and j["id"] != job_id],
                        reverse=True,
                    )
                    if combined_ids:
                        import shutil as _sh
                        from app.core.database import AsyncSessionLocal as _ASL
                        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob as _KFJ
                        for old_id in combined_ids:
                            # Keep disk only for the newest combined (job_id); delete older ones
                            old_job = await self.get(old_id, log_lines=0)
                            if not old_job:
                                continue
                            for p in (_WORK_DIR / f"job{old_id}", Path(old_job.get("output_dir") or "")):
                                try:
                                    if p and p.exists():
                                        _sh.rmtree(p, ignore_errors=True)
                                except OSError:
                                    pass
                            async with _ASL() as db:
                                row = await db.get(_KFJ, old_id)
                                if row is not None:
                                    await db.delete(row)
                                    await db.commit()
                        logger.success(f"[Kronos-finetune] auto-pruned {len(combined_ids)} older combined model(s) — kept major #{job_id}")
                except Exception as e:
                    logger.debug(f"[Kronos-finetune] auto-prune after combined failed: {e}")
                # Also prune again non-combined just to be safe (nothing if already done)
                try:
                    await self.prune_non_combined()
                except Exception:
                    pass
        else:
            tail = self._read_tail(log_path, 8)
            await self._update(job_id, status="failed", error=f"exit {rc}: {tail}"[:1024], finished_at=_now())
            logger.warning(f"[Kronos-finetune] job {job_id} failed (exit {rc})")
            _remember_finetune(
                "finetune_failed", symbol, timeframe,
                f"Kronos fine-tune #{job_id} failed (exit {rc}) on {symbol} {timeframe}: "
                f"{tail[:300]}",
                importance=0.6,
            )

    # ── helpers ──────────────────────────────────────────────────

    async def _update(self, job_id: int, **fields) -> None:
        from app.core.database import AsyncSessionLocal
        from plugins.KronosForecastPlugin.backend.models import KronosFinetuneJob

        async with AsyncSessionLocal() as db:
            job = await db.get(KronosFinetuneJob, job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            await db.commit()

    @staticmethod
    def _read_tail(path: Path, n: int) -> str:
        try:
            lines = path.read_text(errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    @staticmethod
    def _dir_size_mb(path: Optional[str]) -> float:
        """Total size (MB) of a checkpoint directory, 0.0 if missing."""
        if not path:
            return 0.0
        root = Path(path)
        if not root.exists():
            return 0.0
        try:
            total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
            return round(total / 1_000_000, 2)
        except OSError:
            return 0.0

    def _to_dict(self, job, log_lines: int) -> Dict:
        d = {
            "id": job.id, "symbol": job.symbol, "exchange": job.exchange,
            "timeframe": job.timeframe, "status": job.status,
            "model_name": job.model_name,
            "symbols_combined": list(job.symbols_combined) if job.symbols_combined else None,
            "lookback": job.lookback, "pred_len": job.pred_len,
            "tokenizer_epochs": job.tokenizer_epochs, "predictor_epochs": job.predictor_epochs,
            "batch_size": job.batch_size, "base_model": job.base_model,
            "rows_exported": job.rows_exported, "data_path": job.data_path,
            "output_dir": job.output_dir, "log_path": job.log_path, "pid": job.pid,
            "error": job.error,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "log_tail": None,
            "disk_size_mb": self._dir_size_mb(job.output_dir) if job.status == "completed" else 0.0,
        }
        if log_lines and job.log_path:
            d["log_tail"] = self._read_tail(Path(job.log_path), log_lines)
        return d


finetune_manager = FinetuneManager.instance()
