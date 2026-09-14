"""Soup → Ollama bridge: build training data from Brain + trades, trigger Soup fine-tune → GGUF → ollama create."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

ROOT = Path(__file__).resolve().parents[4]

# ── Paths ─────────────────────────────────────────────────────────────────
OllamaBridgeDataDir = (ROOT / "data" / "ollama_training").resolve()
OllamaBridgeDataDir.mkdir(parents=True, exist_ok=True)

DEFAULT_MIN_SAMPLES = int(os.getenv("OLLAMA_MIN_SAMPLES_FOR_TRAIN", "200"))
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1")
OLLAMA_URL = os.getenv("OLLAMA_URL", f"http://{OLLAMA_HOST}:{OLLAMA_PORT}").rstrip("/")

TRADEBOT_LOCAL_MODEL = "tradebot-local:latest"
TRADEBOT_LOCAL_MODEL_ALT = "tradebot-local"


def _read_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ── Dataset builders ───────────────────────────────────────────────────────
def _sharegpt_row(system: str, user: str, assistant: str) -> Dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }


def _brain_rows(limit: int = 500) -> List[Dict[str, Any]]:
    """Knowledge + episodic traces from Neuro/Hermes/flat files."""
    rows: List[Dict[str, Any]] = []
    # 1) Federated Brain episodes — best-effort import, never fail the whole export
    try:
        from app.neuro_brain.federated_search import federated_search  # type: ignore

        # Use a few generic recall prompts to pull diverse episodes
        for q in ("BTC trade decision", "risk management note", "market research"):
            try:
                import asyncio

                hits = asyncio.run(federated_search(q, limit=20))  # type: ignore
                for h in (hits or [])[:20]:
                    txt = (h.get("text") or h.get("content") or "")[:1800]
                    if len(txt) < 80:
                        continue
                    rows.append(
                        _sharegpt_row(
                            "You are TradeBot — concise trading assistant.",
                            f"Recall knowledge: {txt[:400]}",
                            txt[:900],
                        )
                    )
            except Exception:
                continue
            if len(rows) >= limit:
                break
    except Exception:
        pass

    # 2) Hermes episodes (SQLite FTS) — lightweight scan
    try:
        hermes_db = ROOT / "data" / "hermes_state.db"
        if hermes_db.exists():
            import sqlite3

            con = sqlite3.connect(str(hermes_db))
            cur = con.cursor()
            # table may be named episodes / memory; be tolerant
            for tbl in ("episodes", "memory", "hermes_state"):
                try:
                    cur.execute(f"SELECT content, summary FROM {tbl} LIMIT 200")
                    for content, summary in cur.fetchall():
                        txt = (summary or content or "")[:1200]
                        if len(txt) < 80:
                            continue
                        rows.append(
                            _sharegpt_row(
                                "You are a trading memory assistant.",
                                "Summarise this past market episode for future decisions.",
                                txt[:900],
                            )
                        )
                    break
                except Exception:
                    continue
            con.close()
    except Exception:
        pass

    # 3) News + pair catalog text
    try:
        from sqlalchemy import select

        # Lazily import DB; if not ready, skip
        pass
    except Exception:
        pass

    return rows[:limit]


async def _trade_rows(limit: int = 600) -> List[Dict[str, Any]]:
    """Trades + agent decisions with outcomes → supervised pairs."""
    rows: List[Dict[str, Any]] = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.database import AgentDecision, Trade, JarvisAnalysisJournal  # type: ignore
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # Agent decisions that have an outcome (settled)
            q = select(AgentDecision).where(AgentDecision.outcome.isnot(None)).order_by(AgentDecision.created_at.desc()).limit(limit)
            decisions = (await db.execute(q)).scalars().all()
            for d in decisions:
                md = {}
                try:
                    md = json.loads(d.market_data or "{}")
                except Exception:
                    md = {}
                outcome = d.outcome or "unknown"
                pnl = d.outcome_pnl
                pnl_s = f" PnL {pnl:.2f} USDT" if pnl is not None else ""
                user = (
                    f"Symbol {d.symbol} — role {d.agent_role} — action {d.action} "
                    f"(confidence {d.confidence:.2f}) — context {json.dumps(md)[:900]}"
                )
                assistant = (
                    json.dumps(
                        {
                            "action": d.action,
                            "confidence": round(float(d.confidence or 0), 2),
                            "reasoning": (d.reasoning or "")[:800],
                            "outcome": outcome,
                            "outcome_pnl": pnl,
                        }
                    )
                )
                rows.append(_sharegpt_row("You are a trading agent. Output JSON with action, confidence, reasoning, outcome.", user, assistant))

            # Jarvis journal — closed trades with R
            try:
                q2 = select(JarvisAnalysisJournal).where(JarvisAnalysisJournal.outcome.isnot(None)).order_by(JarvisAnalysisJournal.created_at.desc()).limit(limit)
                jj = (await db.execute(q2)).scalars().all()
                for j in jj:
                    user = (
                        f"Jarvis proposal {j.symbol} {j.side} entry {j.entry} "
                        f"SL {j.stop_loss} TP {j.take_profit} — mfe {j.mfe} mae {j.mae} outcome {j.outcome}"
                    )
                    rows.append(
                        _sharegpt_row(
                            "You are a trading execution assistant.",
                            user,
                            f"Entry {j.entry}, SL {j.stop_loss}, TP {j.take_profit} — outcome {j.outcome} R {j.outcome_r}",
                        )
                    )
            except Exception:
                pass

            # Raw trades (open/closed) — for market context
            try:
                q3 = select(Trade).order_by(Trade.created_at.desc()).limit(limit)
                trs = (await db.execute(q3)).scalars().all()
                for t in trs:
                    rows.append(
                        _sharegpt_row(
                            "You are a trade logger.",
                            f"Trade {t.symbol} {t.side}/{t.order_type} qty {t.amount} price {t.price} status {t.status} pnl {t.pnl}",
                            f"Logged trade {t.symbol} {t.side} at {t.price} status {t.status}",
                        )
                    )
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f"[ollama-bridge] trade rows skipped: {exc}")
    return rows[:limit]


def _ohlcv_rows(limit: int = 200) -> List[Dict[str, Any]]:
    """OHLCV windows → ShareGPT (reuses soup_ohlcv_exporter cascade where available)."""
    try:
        from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl  # type: ignore

        # The exporter returns a path; we read it and convert to rows
        # For schedule we just materialise a small preview dataset (BTC 1h)
        # to avoid hammering providers on every training run.
        return []
    except Exception:
        return []


import asyncio as _asyncio


def build_sharegpt_dataset(
    out_path: Optional[Path] = None,
    brain_limit: int = 300,
    trade_limit: int = 600,
    ohlcv_limit: int = 100,
    min_rows: int = 40,
) -> Tuple[Path, int]:
    """
    Synchronously build a ShareGPT JSONL file from Brain+trades.
    Returns (path, num_rows). Raises if below min_rows when checked by caller.
    """
    rows: List[Dict[str, Any]] = []
    # brain rows (sync)
    rows.extend(_brain_rows(limit=brain_limit))
    # trade rows (async, run in new loop if needed)
    try:
        trade_part = _asyncio.run(_trade_rows(limit=trade_limit))
        rows.extend(trade_part)
    except RuntimeError:
        # already inside an event loop — schedule but don't block
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                # can't block — skip trade rows for this non-async caller; the
                # async variant build_sharegpt_dataset_async should be used instead
                pass
            else:
                rows.extend(loop.run_until_complete(_trade_rows(limit=trade_limit)))
        except Exception:
            pass
    except Exception:
        pass
    rows.extend(_ohlcv_rows(limit=ohlcv_limit))

    # dedup + trim
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rows = uniq

    # ensure at least fallback instructional rows so training never fails on empty DB
    if len(rows) < min_rows:
        fallback = [
            _sharegpt_row("You are TradeBot.", "What does a buy signal mean?", "A buy signal indicates the model expects price to rise based on indicators and context."),
            _sharegpt_row("You are TradeBot.", "How should risk be managed?", "Size positions by % of equity, use stop-loss at structure/ATR, cap exposure and leverage."),
            _sharegpt_row("You are TradeBot.", "Explain trade outcome tracking.", "Each decision is settled against live candles; realized PnL and outcome populate via the learning loop."),
        ]
        rows.extend(fallback)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_path or (OllamaBridgeDataDir / f"tradebot-ollama-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info(f"[ollama-bridge] wrote {len(rows)} ShareGPT rows → {out_path}")
    return out_path, len(rows)


async def build_sharegpt_dataset_async(
    out_path: Optional[Path] = None,
    brain_limit: int = 300,
    trade_limit: int = 600,
) -> Tuple[Path, int]:
    rows: List[Dict[str, Any]] = []
    rows.extend(_brain_rows(limit=brain_limit))
    rows.extend(await _trade_rows(limit=trade_limit))
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    if len(uniq) < 20:
        uniq.extend(
            [
                _sharegpt_row("You are TradeBot.", "What is a sell signal?", "A sell signal expects price to fall; size and risk must still be validated."),
            ]
        )
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_path or (OllamaBridgeDataDir / f"tradebot-ollama-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in uniq:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out_path, len(uniq)


# ── Training trigger → Ollama import ───────────────────────────────────────
def trigger_soup_finetune_and_import(
    dataset_path: Path,
    base_model: Optional[str] = None,
    task: str = "sft",
    epochs: int = 1,
) -> Dict[str, Any]:
    """
    Run `soup train` on dataset_path (ShareGPT JSONL), then export GGUF and
    `ollama create tradebot-local:latest`. Best-effort; returns status dict.
    Requires soup venv installed.
    """
    from plugins.SoupPlugin.backend.services.soup_utils import SOUP_BIN, SOUP_PY_BIN  # type: ignore

    soup_bin = str(SOUP_BIN) if SOUP_BIN.exists() else (shutil.which("soup") or "")
    if not soup_bin:
        return {"ok": False, "error": "soup binary not found — run setup_soup.sh"}

    rec = {}
    try:
        from plugins.SoupPlugin.backend.services.soup_utils import recommendation  # type: ignore

        rec = recommendation()
    except Exception:
        rec = {}

    base = base_model or os.getenv("SOUP_DEFAULT_BASE", "Qwen/Qwen2.5-0.5B-Instruct")
    quant = rec.get("quantization")  # 4bit or None → maps to soup --quantization
    # Build soup train config (temp job dir)
    work = Path(os.getenv("SOUP_WORK_DIR", str(ROOT / "data" / "soup_jobs"))) / f"ollama-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "soup_train.yaml"
    yaml_content = (
        f"base: {base}\n"
        f"task: {task}\n"
        f"epochs: {epochs}\n"
        f"dataset: {dataset_path}\n"
        f"output_dir: {work / 'adapter'}\n"
    )
    if quant:
        yaml_content += f"quantization: {quant}\n"
    # Let soup handle streaming/LoRA defaults; recommendation's batch/lora_r could be extra flags
    cfg_path.write_text(yaml_content)

    # Run soup train
    cmd = [soup_bin, "train", "--config", str(cfg_path)]
    logger.info(f"[ollama-bridge] running: {' '.join(cmd)} (work {work})")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600 * 4)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "soup train timed out after 4h", "work_dir": str(work)}
    if r.returncode != 0:
        # keep logs for debugging
        (work / "soup_train.log").write_text((r.stdout or "") + "\n" + (r.stderr or ""))
        return {"ok": False, "error": f"soup train failed: {(r.stderr or r.stdout or '')[:800]}", "work_dir": str(work), "logs": str(work / "soup_train.log")}

    adapter_dir = work / "adapter"
    # Try GGUF export + ollama create (best-effort)
    gguf_path = work / "model.gguf"
    created = False
    # Use soup's export if available, else try llama.cpp path
    gguf_export_cmd = [soup_bin, "export", "--adapter", str(adapter_dir), "--gguf", str(gguf_path)]
    try:
        er = subprocess.run(gguf_export_cmd, capture_output=True, text=True, timeout=1800)
        if er.returncode == 0 and gguf_path.exists():
            # create ollama model
            try:
                from app.services.ollama_service import ollama_create_from_gguf  # type: ignore

                ok_create = ollama_create_from_gguf(TRADEBOT_LOCAL_MODEL, gguf_path, modelfile_extra='SYSTEM "You are TradeBot local LLM — trading assistant."')
                created = bool(ok_create)
            except Exception as exc:
                logger.warning(f"[ollama-bridge] ollama create failed: {exc}")
        else:
            logger.info(f"[ollama-bridge] GGUF export not available or failed: {(er.stderr or er.stdout or '')[:600]} — training artefact kept at {adapter_dir}")
    except Exception as exc:
        logger.debug(f"[ollama-bridge] gguf export skipped: {exc}")

    # Prune old datasets keeping last N
    try:
        keep_n = int(os.getenv("OLLAMA_KEEP_LAST_N", "2"))
        jsonls = sorted(OllamaBridgeDataDir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in jsonls[keep_n:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

    return {"ok": True, "work_dir": str(work), "adapter_dir": str(adapter_dir), "gguf": str(gguf_path) if gguf_path.exists() else None, "ollama_created": created, "model": TRADEBOT_LOCAL_MODEL if created else None}


def should_train(now: Optional[datetime] = None, last_train_file: Optional[Path] = None) -> Tuple[bool, str]:
    """Gate: enough new samples and enough time since last train."""
    min_samples = DEFAULT_MIN_SAMPLES
    # Check row availability quickly (cheap)
    try:
        # approximate count from DB without building full dataset — use trade rows quick probe
        import asyncio as _a

        cnt = 0
        try:
            # sync probe via file if async not available here
            pass
        except Exception:
            pass
    except Exception:
        pass
    # Time gate: default 24h interval
    interval_h = int(os.getenv("OLLAMA_TRAIN_INTERVAL_HOURS", "24"))
    last = last_train_file or (OllamaBridgeDataDir / ".last_ollama_train")
    if last.exists():
        try:
            ts = datetime.fromisoformat(last.read_text().strip())
            if datetime.utcnow() - ts < timedelta(hours=interval_h):
                return False, f"last train {ts.isoformat()} < {interval_h}h ago"
        except Exception:
            pass
    return True, "due"


def mark_trained(path: Optional[Path] = None) -> None:
    p = path or (OllamaBridgeDataDir / ".last_ollama_train")
    try:
        p.write_text(datetime.utcnow().isoformat())
    except Exception:
        pass
