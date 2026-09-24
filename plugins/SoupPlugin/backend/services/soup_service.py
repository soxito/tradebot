"""Soup plugin — Finetune manager (mirrors Kronos FinetuneManager)."""
from __future__ import annotations
import asyncio
import os
import sys
import json
import signal
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from plugins.SoupPlugin.backend.services.soup_utils import (
    ROOT, SOUP_REPO_DIR, SOUP_VENV_BIN, SOUP_BIN, SOUP_PY_BIN, SOUP_VERSION_FILE,
    SOUP_DATA_DIR, SOUP_UI_PORT, SOUP_PORT,
    soup_setup_command, is_repo_cloned, is_installed, soup_bin_path, repo_version, recommendation,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_WORK_DIR = SOUP_DATA_DIR

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _dir_size_mb(path: Optional[str]) -> float:
    if not path: return 0.0
    p = Path(path)
    if not p.exists(): return 0.0
    try:
        return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1_000_000, 2)
    except Exception:
        return 0.0

def _read_tail(path: Path, n: int) -> str:
    try: return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except Exception: return ""

class SoupManager:
    _instance: Optional["SoupManager"] = None
    def __init__(self):
        self._procs: Dict[int, asyncio.subprocess.Process] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
    @classmethod
    def instance(cls) -> "SoupManager":
        if cls._instance is None: cls._instance = SoupManager()
        return cls._instance

    async def start(self, req) -> Dict:
        """Create job row and launch soup train pipeline."""
        if not is_repo_cloned():
            raise RuntimeError(f"Soup repo not cloned. Run: {soup_setup_command()}")
        sb = soup_bin_path()
        if not sb:
            raise RuntimeError(f"soup binary not found. Run: {soup_setup_command()}")
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        async with AsyncSessionLocal() as db:
            job = SoupFinetuneJob(
                base_model=req.base, task=req.task, template=req.template,
                dataset_format=req.dataset_format, val_split=req.val_split,
                epochs=req.epochs, learning_rate=req.learning_rate, batch_size=req.batch_size,
                lora_r=req.lora_r, lora_alpha=req.lora_alpha, quantization=req.quantization,
                stream_layers=1 if req.stream_layers else 0,
                status="pending",
                ohlcv_symbol=(req.ohlcv_symbol.upper() if req.ohlcv_symbol else None),
                ohlcv_timeframe=req.ohlcv_timeframe,
                ohlcv_rows=req.ohlcv_limit,
            )
            db.add(job); await db.commit(); await db.refresh(job)
            job_id = job.id
        # Persist inline dataset if provided (write to work dir)
        if req.dataset_inline:
            try:
                SOUP_DATA_DIR.mkdir(parents=True, exist_ok=True)
                ds_path = SOUP_DATA_DIR / f"job{job_id}_inline.jsonl"
                with ds_path.open("w") as f:
                    for row in req.dataset_inline:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                # store path in meta_json via update
                async with AsyncSessionLocal() as db:
                    j = await db.get(SoupFinetuneJob, job_id)
                    if j: 
                        j.dataset_path = str(ds_path)
                        j.meta_json = {"inline_rows": len(req.dataset_inline)}
                        await db.commit()
                req.dataset_path = str(ds_path)
            except Exception as e:
                logger.warning(f"[Soup] inline dataset write failed: {e}")
        elif req.dataset_path:
            # validate exists or keep as is
            pass
        # OHLCV experimental path: generate dataset if requested and no dataset_path
        if (req.ohlcv_symbol and not req.dataset_path and not req.dataset_inline):
            try:
                from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl
                SOUP_DATA_DIR.mkdir(parents=True, exist_ok=True)
                ds_path = SOUP_DATA_DIR / f"job{job_id}_ohlcv.jsonl"
                # best-effort async export (runs sync for now, but short)
                res = await export_ohlcv_to_jsonl(req.ohlcv_symbol, req.ohlcv_timeframe, req.ohlcv_limit, ds_path)
                if res and res.get("rows"):
                    async with AsyncSessionLocal() as db:
                        j = await db.get(SoupFinetuneJob, job_id)
                        if j:
                            j.dataset_path = str(ds_path)
                            j.ohlcv_rows = int(res["rows"])
                            j.meta_json = {"ohlcv": res}
                            await db.commit()
                    req.dataset_path = str(ds_path)
                    logger.info(f"[Soup] OHLCV dataset generated: {res}")
            except Exception as e:
                logger.warning(f"[Soup] OHLCV export failed (fallback to empty): {e}")

        task = asyncio.create_task(self._run_pipeline(job_id, req))
        self._tasks[job_id] = task
        return await self.get(job_id)

    async def _run_pipeline(self, job_id: int, req) -> None:
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        exp_name = f"job{job_id}"
        base_dir = SOUP_DATA_DIR / exp_name
        base_dir.mkdir(parents=True, exist_ok=True)
        log_path = base_dir / "train.log"
        output_dir = base_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        # Determine dataset_path (from req or job)
        async with AsyncSessionLocal() as db:
            job = await db.get(SoupFinetuneJob, job_id)
            if not job: return
            dataset_path = getattr(req, "dataset_path", None) or job.dataset_path
            # Persist dataset_path so API shows it even when passed via req
            if getattr(req, "dataset_path", None) and not job.dataset_path:
                job.dataset_path = str(getattr(req, "dataset_path"))
                await db.commit()
            # If still none, try to use job's ohlcv path generation did not happen, leave None
            config_path = base_dir / "soup.yaml"
            # Build soup.yaml
            # Resolve data path: use dataset_path or fallback to empty (soup will error, captured)
            data_train = dataset_path or ""
            # Build YAML content per Soup spec
            quantization = getattr(req, "quantization", None) or job.quantization
            stream = bool(getattr(req, "stream_layers", False) or job.stream_layers)
            # Map to soup YAML: handle batch_size auto, quantization etc
            _raw_batch = getattr(req, "batch_size", job.batch_size) or "auto"
            # Soup 0.75+ forbids stream_layers with batch_size='auto' — coerce to concrete
            if stream and str(_raw_batch).lower() == "auto":
                _raw_batch = "4"
            # Persist the concrete batch back to job so UI reflects reality
            try:
                job.batch_size = str(_raw_batch)
            except: pass
            cfg = {
                "base": getattr(req, "base", None) or job.base_model,
                "task": getattr(req, "task", None) or job.task,
                "data": {
                    "train": data_train,
                    "format": getattr(req, "dataset_format", job.dataset_format) or "alpaca",
                    "val_split": float(getattr(req, "val_split", job.val_split) or 0.1),
                },
                "training": {
                    "epochs": int(getattr(req, "epochs", job.epochs) or 3),
                    "lr": float(getattr(req, "learning_rate", job.learning_rate) or 2e-5),
                    "batch_size": _raw_batch,
                    "lora": {"r": int(getattr(req, "lora_r", job.lora_r) or 64), "alpha": int(getattr(req, "lora_alpha", job.lora_alpha) or 16)},
                },
                "output": str(output_dir),
            }
            if quantization:
                cfg["training"]["quantization"] = quantization
            if stream:
                cfg["training"]["stream_layers"] = True
            # optional template hint → comment
            # Write YAML
            try:
                import yaml
                config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
            except Exception:
                # minimal fallback
                import json as _js
                config_path.write_text(_js.dumps(cfg, indent=2))
            # persist
            job.config_path = str(config_path)
            job.output_dir = str(output_dir)
            job.log_path = str(log_path)
            job.status = "running"
            job.started_at = _now()
            await db.commit()
        # Train via soup binary
        soup_bin = soup_bin_path() or "soup"
        # Determine if we have a real dataset — if data_train is empty, fail fast with helpful error
        if not data_train or not Path(data_train).exists():
            # try inline: if dataset was provided but file missing, we already wrote inline
            if not data_train:
                async with AsyncSessionLocal() as db:
                    j = await db.get(SoupFinetuneJob, job_id)
                    if j:
                        j.status = "failed"
                        j.error = "No dataset provided — upload a JSONL/CSV or set ohlcv_symbol to generate one, or provide dataset_inline."
                        j.finished_at = _now()
                        await db.commit()
                return
        cmd = [soup_bin, "train", "--config", str(config_path), "--yes"]
        env = {**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH","")}
        # Ensure soup venv on PATH
        env["PATH"] = f"{SOUP_VENV_BIN}{os.pathsep}{env.get('PATH','')}"
        try:
            with log_path.open("w") as logfh:
                logfh.write(f"# Soup train: {' '.join(cmd)}\n# config: {config_path}\n# output: {output_dir}\n\n")
                logfh.flush()
                proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), env=env, stdout=logfh, stderr=asyncio.subprocess.STDOUT)
                self._procs[job_id] = proc
                async with AsyncSessionLocal() as db:
                    j = await db.get(SoupFinetuneJob, job_id)
                    if j: j.pid = proc.pid; await db.commit()
                rc = await proc.wait()
        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                j = await db.get(SoupFinetuneJob, job_id)
                if j: j.status="cancelled"; j.finished_at=_now(); await db.commit()
            return
        except Exception as e:
            async with AsyncSessionLocal() as db:
                j = await db.get(SoupFinetuneJob, job_id)
                if j: j.status="failed"; j.error=f"train spawn: {e}"[:1024]; j.finished_at=_now(); await db.commit()
            return
        finally:
            self._procs.pop(job_id, None)
        if rc == 0:
            async with AsyncSessionLocal() as db:
                j = await db.get(SoupFinetuneJob, job_id)
                if j: j.status="completed"; j.finished_at=_now(); await db.commit()
            logger.success(f"[Soup] job {job_id} completed")
        else:
            tail = _read_tail(log_path, 8)
            async with AsyncSessionLocal() as db:
                j = await db.get(SoupFinetuneJob, job_id)
                if j: j.status="failed"; j.error=f"exit {rc}: {tail}"[:1024]; j.finished_at=_now(); await db.commit()
            logger.warning(f"[Soup] job {job_id} failed exit {rc}")

    async def get(self, job_id: int, log_lines: int = 40) -> Optional[Dict]:
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        async with AsyncSessionLocal() as db:
            job = await db.get(SoupFinetuneJob, job_id)
            if not job: return None
            return self._to_dict(job, log_lines)
    async def list(self, limit: int = 50) -> List[Dict]:
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(SoupFinetuneJob).order_by(SoupFinetuneJob.id.desc()).limit(limit))
            return [self._to_dict(j, 0) for j in res.scalars().all()]
    async def cancel(self, job_id: int) -> bool:
        proc = self._procs.get(job_id)
        if proc and proc.returncode is None:
            try: proc.send_signal(signal.SIGTERM)
            except Exception: pass
        task = self._tasks.get(job_id)
        if task: task.cancel()
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        async with AsyncSessionLocal() as db:
            j = await db.get(SoupFinetuneJob, job_id)
            if j and j.status in ("pending","running"):
                j.status="cancelled"; j.finished_at=_now(); await db.commit()
        return True
    async def delete(self, job_id: int) -> Dict:
        await self.cancel(job_id)
        from app.core.database import AsyncSessionLocal
        from plugins.SoupPlugin.backend.models import SoupFinetuneJob
        info = await self.get(job_id, 0)
        freed = _dir_size_mb(info.get("output_dir") if info else None)
        # also delete work dir
        try:
            shutil.rmtree(_WORK_DIR / f"job{job_id}", ignore_errors=True)
        except Exception: pass
        if info and info.get("output_dir"):
            try: shutil.rmtree(info["output_dir"], ignore_errors=True)
            except Exception: pass
        async with AsyncSessionLocal() as db:
            j = await db.get(SoupFinetuneJob, job_id)
            if j: await db.delete(j); await db.commit()
        return {"ok": True, "job_id": job_id, "freed_mb": round(freed,1)}

    def _to_dict(self, job, log_lines: int) -> Dict:
        d = {
            "id": job.id, "base_model": job.base_model, "task": job.task,
            "status": job.status, "epochs": job.epochs, "quantization": job.quantization,
            "stream_layers": bool(job.stream_layers),
            "output_dir": job.output_dir, "log_path": job.log_path, "pid": job.pid,
            "error": job.error, "dataset_path": job.dataset_path,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "ohlcv_symbol": job.ohlcv_symbol, "ohlcv_timeframe": job.ohlcv_timeframe,
            "log_tail": None, "disk_size_mb": _dir_size_mb(job.output_dir) if job.status=="completed" else 0.0,
            "lora_r": job.lora_r, "batch_size": job.batch_size,
        }
        if log_lines and job.log_path:
            d["log_tail"] = _read_tail(Path(job.log_path), log_lines)
        return d

soup_manager = SoupManager.instance()
