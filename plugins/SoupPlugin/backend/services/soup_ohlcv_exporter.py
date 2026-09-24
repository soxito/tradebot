"""Experimental OHLCV → Soup JSONL exporter.

Converts live OHLCV candles (via Kronos finetune_data_exporter cascade) into
ShareGPT JSONL that Soup can SFT on. This is the "attempt OHLCV via Soup if
possible" path from plan 6.1 — best-effort, side-by-side with Kronos, never
replaces Kronos forecaster.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, Optional

from loguru import logger

async def export_ohlcv_to_jsonl(symbol: str, timeframe: str = "1h", limit: int = 2000, output_path: Path = None, lookback: int = 512, pred_len: int = 48) -> Dict[str, Any]:
    """Export OHLCV for symbol/timeframe to Soup-ready JSONL windows.
    Each JSONL row: {"messages": [{"role":"system","content": ...}, {"role":"user","content": "OHLCV window 512:"}, {"role":"assistant","content": "next 48:"}]}
    Returns {path, rows, windows}.
    Raises on export failure so caller can fallback.
    """
    output_path = Path(output_path or Path(f"/tmp/soup_ohlcv_{symbol.replace('/','_')}.jsonl"))
    # Reuse Kronos data exporter (multi-source OHLCV cascade)
    try:
        from plugins.KronosForecastPlugin.backend.services import finetune_data_exporter as kexp
        from plugins.KronosForecastPlugin.backend.services.finetune_data_exporter import export_training_data
        # Use Kronos exporter to get CSV-style rows, then re-window as text
        # We call export_training_data with a temp CSV to leverage its _fetch_ohlcv_paged + pattern features
        tmp_csv = output_path.with_suffix(".csv")
        # Export via Kronos pipeline (reuses MT5→Yahoo→ccxt cascade, Swissquote anchoring where applicable)
        res = await export_training_data(symbol=symbol, exchange="bitget", timeframe=timeframe, limit=limit, output_path=tmp_csv, use_pattern_features=False)
        # Parse CSV rows
        import csv
        rows = []
        with tmp_csv.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Expect columns: timestamps,open,high,low,close,volume,amount
                rows.append(row)
        if len(rows) < lookback + pred_len + 1:
            raise ValueError(f"Not enough rows ({len(rows)}) for lookback {lookback}+pred {pred_len}")
        # Build windows as chat samples
        output_path.parent.mkdir(parents=True, exist_ok=True)
        windows = 0
        with output_path.open("w") as out:
            for i in range(0, len(rows) - lookback - pred_len + 1):
                ctx = rows[i:i+lookback]
                fut = rows[i+lookback:i+lookback+pred_len]
                # Format as compact CSV lines: time,open,high,low,close,volume
                def fmt(r):
                    return f"{r.get('timestamps') or r.get('time','')},{r.get('open')},{r.get('high')},{r.get('low')},{r.get('close')},{r.get('volume','')}"
                ctx_text = "\n".join(fmt(r) for r in ctx)
                fut_text = "\n".join(fmt(r) for r in fut)
                msg = {
                    "messages": [
                        {"role": "system", "content": "You are an OHLCV forecaster. Given past candles (timestamps,open,high,low,close,volume), predict the next candles exactly in the same CSV format. Only output the predicted candles, no explanation. (Experimental Soup OHLCV bridge — compare with Kronos.)"},
                        {"role": "user", "content": f"Past {lookback} candles for {symbol} {timeframe}:\n{ctx_text}\n\nPredict the next {pred_len} candles (same format):"},
                        {"role": "assistant", "content": fut_text},
                    ]
                }
                out.write(json.dumps(msg, ensure_ascii=False) + "\n")
                windows += 1
                # Cap windows to avoid huge files on large limits: stride 1 gives ~1.5k windows for 2k rows; that's ok for Soup. We keep all for best learning unless huge.
                if windows >= 5000:
                    break
        # Also keep the raw CSV for audit (don't delete tmp_csv)
        logger.info(f"[Soup OHLCV] exported {windows} windows ({len(rows)} rows) for {symbol} {timeframe} → {output_path}")
        return {"path": str(output_path), "csv": str(tmp_csv), "rows": len(rows), "windows": windows, "lookback": lookback, "pred_len": pred_len}
    except Exception as e:
        logger.warning(f"[Soup OHLCV] export failed for {symbol} {timeframe}: {e}")
        raise

async def preview_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 200) -> Dict[str, Any]:
    """Light preview without writing JSONL — for UI validation."""
    try:
        from plugins.KronosForecastPlugin.backend.services.finetune_data_exporter import export_training_data
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "tmp.csv"
            res = await export_training_data(symbol=symbol, exchange="bitget", timeframe=timeframe, limit=limit, output_path=p, use_pattern_features=False)
            return {"ok": True, "preview": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}
