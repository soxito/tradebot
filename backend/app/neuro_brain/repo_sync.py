"""
Repo sync + trade analysis loops for the Neuroglancer core brain.
Mirrors hermes + jarvis learning loop scheduling.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, Any

from loguru import logger

from app.core.config import settings
from app.neuro_brain import is_enabled, local_path, repo_branch, repo_url, repo_version
from app.neuro_brain.state_store import ensure_store, analysis_summary


def _version_file() -> Path:
    return Path(__file__).resolve().parents[3] / "integrations" / "neuroglancer.version.json"

async def pull_repo_once() -> Dict[str, Any]:
    repo_dir = Path(local_path())
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        return {"ok": False, "error": "repo not cloned yet — run start.py to clone"}
    before = repo_version().get("commit")
    try:
        # Use asyncio.to_thread to avoid blocking
        def _do():
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth=1", "origin", repo_branch()], capture_output=True, timeout=30)
            r = subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], capture_output=True, text=True, timeout=60)
            return r
        r = await asyncio.to_thread(_do)
        after = repo_version().get("commit")
        updated = before != after
        try:
            v = repo_version()
            _version_file().write_text(json.dumps({**v, "last_pull": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))
        except Exception:
            pass
        return {"ok": r.returncode == 0, "updated": updated, "before": before, "after": repo_version().get("commit"), "output": (r.stdout or r.stderr or "")[:800], "repo": repo_version()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

# ── Lightweight trade settler (mirrors jarvis_learning_loop but for neuro segments) ──
_neuro_trade_task: asyncio.Task | None = None

async def _trade_loop(interval_s: int):
    logger.info(f"[neuro] trade analysis loop started — every {interval_s}s")
    while True:
        try:
            await asyncio.sleep(interval_s)
            if not is_enabled():
                continue
            # attempt to settle pending segments against live candle highs/lows
            # keep it cheap: at most 20 segments per cycle, only those with entry/SL/TP
            from app.neuro_brain.state_store import list_segments, mark_outcome
            pending = list_segments(limit=20, outcome=None)  # actually outcome filter not supported for None; filter manually
            # list_segments with outcome=None returns all — filter in python
            pend = [s for s in pending if not s.get("outcome")]
            if not pend:
                continue
            # for each, fetch candles for symbol and check TP/SL touch
            for seg in pend[:8]:
                try:
                    sym = seg.get("symbol") or ""
                    entry = float(seg.get("entry") or 0)
                    sl = float(seg.get("stop_loss") or 0)
                    tp = float(seg.get("take_profit") or 0)
                    side = (seg.get("side") or "long").lower()
                    tf = seg.get("timeframe") or "1h"
                    if not sym or not entry or not sl or not tp:
                        continue
                    # resolve candles via market_data universal fallback (same as JARVIS settler)
                    from app.services import market_data as md
                    candles = await md.get_candles(sym, tf, limit=30) if hasattr(md, "get_candles") else []
                    if not candles or len(candles)<2:
                        # fallback to yahoo via candles service
                        try:
                            from app.services import candles as cs
                            candles = await cs.fetch(sym, tf)
                        except Exception:
                            continue
                    if not candles:
                        continue
                    # check recent candles after seg.ts
                    hit=None
                    for c in candles[-20:]:
                        # c may be dict or tuple [time,o,h,l,c,v]
                        if isinstance(c, (list,tuple)): h,l = float(c[2]), float(c[3])
                        elif isinstance(c, dict): h,l = float(c.get("high") or c.get("h") or 0), float(c.get("low") or c.get("l") or 0)
                        else: continue
                        if not h or not l: continue
                        if side in ("long","buy"):
                            if l <= sl: hit="loss"; break
                            if h >= tp: hit="win"; break
                        else:
                            if h >= sl: hit="loss"; break
                            if l <= tp: hit="win"; break
                    if hit:
                        mark_outcome(int(seg["id"]), hit)
                        # also mirror into Hermes for federation redundancy
                        try:
                            from app.neuro_brain.state_store import ingest_local
                            ingest_local(kind="trade_settled", content=f"[{sym} {hit.upper()}] {side} {entry}->{tp} SL {sl}", symbol=sym, source="neuro-settler", meta={"segment_id": seg["id"], "outcome": hit})
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[neuro] segment {seg.get('id')} settle skipped: {e}")
                    continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[neuro] trade loop error: {e}")
            await asyncio.sleep(5)

def start_neuro_trade_loop(interval_s: int | None = None) -> bool:
    global _neuro_trade_task
    if _neuro_trade_task and not _neuro_trade_task.done():
        return False
    iv = interval_s or int(getattr(settings, "NEUROGLANCER_TRADE_LOOP_S", 600) or 600)
    try:
        loop = asyncio.get_running_loop()
        _neuro_trade_task = loop.create_task(_trade_loop(iv))
        return True
    except RuntimeError:
        # no running loop yet — caller should retry after startup
        return False

def stop_neuro_trade_loop() -> bool:
    global _neuro_trade_task
    if _neuro_trade_task and not _neuro_trade_task.done():
        _neuro_trade_task.cancel()
        _neuro_trade_task=None
        return True
    return False

def trade_loop_status() -> Dict[str,Any]:
    running = bool(_neuro_trade_task and not _neuro_trade_task.done())
    return {"running": running, "interval_s": int(getattr(settings, "NEUROGLANCER_TRADE_LOOP_S", 600) or 600)}

# ── Repo auto-pull task ────────────────────────────────────────────────────
_neuro_pull_task: asyncio.Task | None = None

async def _pull_loop(interval_s: int):
    logger.info(f"[neuro] auto-pull loop started — every {interval_s}s")
    while True:
        try:
            await asyncio.sleep(interval_s)
            if not is_enabled() or not getattr(settings, "NEUROGLANCER_AUTO_PULL", True):
                continue
            res = await pull_repo_once()
            if res.get("updated"):
                logger.info(f"[neuro] repo updated {res.get('before')}->{res.get('after')}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[neuro] pull loop error: {e}")
            await asyncio.sleep(10)

def start_neuro_pull_loop(interval_s: int | None = None) -> bool:
    global _neuro_pull_task
    if _neuro_pull_task and not _neuro_pull_task.done():
        return False
    iv = interval_s or int(getattr(settings, "NEUROGLANCER_PULL_INTERVAL_S", 21600) or 21600)
    try:
        loop = asyncio.get_running_loop()
        _neuro_pull_task = loop.create_task(_pull_loop(iv))
        return True
    except RuntimeError:
        return False

def stop_neuro_pull_loop() -> bool:
    global _neuro_pull_task
    if _neuro_pull_task and not _neuro_pull_task.done():
        _neuro_pull_task.cancel()
        _neuro_pull_task=None
        return True
    return False

def pull_loop_status() -> Dict[str, Any]:
    running = bool(_neuro_pull_task and not _neuro_pull_task.done())
    return {"running": running, "interval_s": int(getattr(settings, "NEUROGLANCER_PULL_INTERVAL_S", 21600) or 21600)}
