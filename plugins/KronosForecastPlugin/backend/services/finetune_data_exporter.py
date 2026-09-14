"""
Kronos Forecast Plugin — Fine-tune Data Exporter

Turns the live OHLCV feed (the very same connectors a forecast reads) into the
CSV format the vendored ``finetune_csv`` pipeline expects, so a user can
fine-tune Kronos on the exact instrument/timeframe they trade.

Kronos CSV columns (see finetune_csv/README.md):
    timestamps,open,high,low,close,volume,amount
``amount`` is quote-volume (price × base volume); 0 is acceptable when unknown.

Pattern-enriched mode (default): for every candle we look back at the previous
20 bars and ask the same 50-pattern candle engine + 12-pattern chart engine
that the live trading agents use. The resulting per-bar scores are written as
additional numeric columns so fine-tuning is pattern-aware: the model sees not
just price but also “a hammer just printed with volume” or “an ascending
triangle is pending”. Set ``use_pattern_features=False`` to export the classic
7-column CSV for full backwards compatibility.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from loguru import logger

from plugins.KronosForecastPlugin.backend.services import forecast_service as fs

#: Kronos needs at least one full context window; refuse tiny exports.
MIN_ROWS = 256


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _fetch_ohlcv_paged(
    exchange: str, symbol: str, timeframe: str, target: int
) -> List[list]:
    """Accumulate up to ``target`` candles by walking history backwards.

    A single exchange OHLCV request caps at ~1000 candles, which is far too few
    to fine-tune Kronos (a 512-bar context leaves almost nothing after the
    train/val split). We take the most-recent page via the shared resolver
    (so MT5/forex/crypto all route the same way the forecast does), then — for
    crypto — extend backwards with ccxt ``since`` paging until we hit the target
    or the exchange runs out of history. Best-effort and de-duplicated by
    timestamp; never raises.
    """
    rows = await fs._fetch_ohlcv(exchange, symbol, timeframe, min(target, 1000))
    have: Dict[int, list] = {int(r[0]): r for r in rows} if rows else {}

    # Forex/metals route through Yahoo/MT5 which already return a long window and
    # do not page cleanly with `since`; take whatever the baseline gave us.
    if len(have) >= target or fs._is_forex(symbol):
        merged = sorted(have.values(), key=lambda r: r[0])
        return merged[-target:] if len(merged) > target else merged

    try:
        import ccxt.async_support as ccxt_async  # core backend dep
    except Exception:
        return sorted(have.values(), key=lambda r: r[0])

    tf_ms = fs._tf_seconds(timeframe) * 1000
    variants = [v for v in fs._symbol_variants(symbol) if "/" in v]
    page = 1000
    oldest = min(have) if have else int(time.time() * 1000)

    ex_ids = list(fs._PUBLIC_CCXT_EXCHANGES)
    req = (exchange or "").strip().lower()
    if req in ex_ids:
        ex_ids.remove(req)
        ex_ids.insert(0, req)

    for ex_id in ex_ids:
        if len(have) >= target:
            break
        if not hasattr(ccxt_async, ex_id):
            continue
        client = None
        try:
            client = getattr(ccxt_async, ex_id)({"enableRateLimit": True, "timeout": 15000})
            sym = None
            for v in variants:
                try:
                    probe = await client.fetch_ohlcv(v, timeframe=timeframe, limit=5)
                    if probe:
                        sym = v
                        break
                except Exception:
                    continue
            if sym is None:
                continue
            stagnant = 0
            while len(have) < target and stagnant < 3:
                since = oldest - page * tf_ms
                try:
                    batch = await client.fetch_ohlcv(
                        sym, timeframe=timeframe, since=since, limit=page
                    )
                except Exception:
                    break
                new = 0
                for r in batch or []:
                    ts = int(r[0])
                    if ts not in have:
                        have[ts] = r
                        new += 1
                if not batch or new == 0:
                    stagnant += 1
                else:
                    stagnant = 0
                    oldest = min(have)
        except Exception as exc:
            logger.debug(f"[Kronos-finetune] paging via {ex_id} failed: {exc}")
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    merged = sorted(have.values(), key=lambda r: r[0])
    logger.info(
        f"[Kronos-finetune] paged {len(merged)} candles for {symbol} {timeframe} "
        f"(target {target})"
    )
    return merged[-target:] if len(merged) > target else merged


def _compute_pattern_features(rows: List[list]) -> List[Dict]:
    """Per-bar pattern scores for enrichment.

    Returns list of dicts aligned to ``rows`` with keys:
      candle_bull/candle_bear/candle_neutral (0/1 flags for pattern ending at i)
      candle_pat_count, candle_pat_dir (bull=1, bear=-1, else 0)
      chart_score (-1..1), chart_conf (0..1), chart_bull/bear flags, fib_in_golden (0/1)
    Falls back to zeros per bar on any error — an enrichment must never block export.
    """
    n = len(rows)
    # defaults
    feats: List[Dict] = [
        {
            "candle_bull": 0, "candle_bear": 0, "candle_neutral": 0,
            "candle_pat_count": 0, "candle_pat_dir": 0,
            "chart_score": 0.0, "chart_conf": 0.0, "chart_bull": 0, "chart_bear": 0,
            "fib_in_golden": 0,
        }
        for _ in range(n)
    ]
    try:
        from app.signals.candle_patterns import detect_patterns
    except Exception as exc:
        logger.debug(f"[Kronos-finetune] candle pattern engine unavailable: {exc}")
        return feats
    try:
        events = detect_patterns(rows)
        idx_map: dict[int, list] = {}
        for e in events:
            idx = int(e.get("index", -1))
            if 0 <= idx < n:
                idx_map.setdefault(idx, []).append(e)
        for i in range(n):
            evs = idx_map.get(i, [])
            if not evs:
                continue
            bull = sum(1 for e in evs if e.get("direction") == "bull")
            bear = sum(1 for e in evs if e.get("direction") == "bear")
            neut = sum(1 for e in evs if e.get("direction") == "neutral")
            feats[i]["candle_bull"] = 1 if bull else 0
            feats[i]["candle_bear"] = 1 if bear else 0
            feats[i]["candle_neutral"] = 1 if neut else 0
            feats[i]["candle_pat_count"] = len(evs)
            if bull and not bear:
                feats[i]["candle_pat_dir"] = 1
            elif bear and not bull:
                feats[i]["candle_pat_dir"] = -1
            else:
                feats[i]["candle_pat_dir"] = 0
    except Exception as exc:
        logger.debug(f"[Kronos-finetune] candle pattern scoring skipped: {exc}")

    # chart + fib per bar — heavier, sample every bar but reuse 80-bar slice
    try:
        from app.signals.technical import ohlcv_to_dataframe, auto_fib_retracement
        from app.signals.chart_patterns import analyze_chart_patterns
        import pandas as pd
        df_full = ohlcv_to_dataframe(rows)
        if len(df_full) >= 40:
            # fib In-golden per bar: compute once on rolling? Use latest swing only as approximation per bar -> compute per bar via slice
            for i in range(30, n):
                try:
                    window = df_full.iloc[max(0, i - 79): i + 1]
                    if len(window) < 40:
                        continue
                    cp = analyze_chart_patterns(window)
                    feats[i]["chart_score"] = float(cp.get("score") or 0.0)
                    best = cp.get("best") or {}
                    feats[i]["chart_conf"] = float(best.get("confidence") or 0.0)
                    if cp.get("has_bullish"):
                        feats[i]["chart_bull"] = 1
                    if cp.get("has_bearish"):
                        feats[i]["chart_bear"] = 1
                    # fib golden
                    fib = auto_fib_retracement(window, levels=(0.5, 0.618), extend_lines=False)
                    zone = fib.get("golden_zone")
                    if zone:
                        cp_price = float(window["close"].iloc[-1])
                        if zone["low"] <= cp_price <= zone["high"]:
                            feats[i]["fib_in_golden"] = 1
                except Exception:
                    continue
    except Exception as exc:
        logger.debug(f"[Kronos-finetune] chart/fib pattern scoring skipped: {exc}")
    return feats


async def export_training_data(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    output_path: Path,
    use_pattern_features: bool = True,
) -> Dict:
    """Fetch ``limit`` OHLCV candles and write them as a Kronos-format CSV.

    Reuses ``forecast_service`` connectors (with backward paging) so
    forex/metals/indices and crypto all resolve through the same source the
    forecast uses. When ``use_pattern_features`` is True (default) each row is
    enriched with previous-candle pattern signals so the fine-tuned model learns
    pattern-conditioned dynamics. Returns a summary dict; raises ``ValueError``
    when too few candles are available.
    """
    rows: List[list] = await _fetch_ohlcv_paged(exchange, symbol, timeframe, limit)
    if not rows or len(rows) < MIN_ROWS:
        raise ValueError(
            f"Only {len(rows) if rows else 0} candles available for {symbol} "
            f"({timeframe}) — need at least {MIN_ROWS} to fine-tune. Try a longer "
            f"history or a lower timeframe."
        )

    pattern_feats: List[Dict] | None = None
    enriched = False
    if use_pattern_features:
        try:
            pattern_feats = _compute_pattern_features(rows)
            enriched = True
            bull_bars = sum(1 for f in pattern_feats if f.get("candle_bull") or f.get("candle_bear"))
            chart_bars = sum(1 for f in pattern_feats if abs(f.get("chart_score", 0)) > 0.05)
            logger.info(f"[Kronos-finetune] pattern enrichment: {bull_bars} bars with candle patterns, {chart_bars} with chart_score≠0 (total {len(rows)})")
        except Exception as exc:
            logger.warning(f"[Kronos-finetune] pattern enrichment failed — exporting plain OHLCV: {exc}")
            pattern_feats = None
            enriched = False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        if enriched and pattern_feats:
            writer.writerow(["timestamps", "open", "high", "low", "close", "volume", "amount",
                             "candle_bull", "candle_bear", "candle_pat_count", "candle_pat_dir",
                             "chart_score", "chart_conf", "chart_bull", "chart_bear", "fib_in_golden"])
        else:
            writer.writerow(["timestamps", "open", "high", "low", "close", "volume", "amount"])
        for idx, r in enumerate(rows):
            try:
                ts = _fmt_ts(int(r[0]))
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v = float(r[5]) if len(r) > 5 else 0.0
                if enriched and pattern_feats:
                    f = pattern_feats[idx]
                    writer.writerow([ts, o, h, l, c, v, round(v * c, 8),
                                     f.get("candle_bull", 0), f.get("candle_bear", 0), f.get("candle_pat_count", 0), f.get("candle_pat_dir", 0),
                                     round(float(f.get("chart_score", 0.0)), 4), round(float(f.get("chart_conf", 0.0)), 4),
                                     f.get("chart_bull", 0), f.get("chart_bear", 0), f.get("fib_in_golden", 0)])
                else:
                    writer.writerow([ts, o, h, l, c, v, round(v * c, 8)])
                written += 1
            except (TypeError, ValueError, IndexError):
                continue

    logger.info(f"[Kronos-finetune] exported {written} rows for {symbol} {timeframe} → {output_path} (enriched={enriched})")
    result = {
        "path": str(output_path),
        "rows": written,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "first_ts": _fmt_ts(int(rows[0][0])),
        "last_ts": _fmt_ts(int(rows[-1][0])),
        "enriched": enriched,
        "pattern_coverage": {
            "candle_pattern_bars": sum(1 for f in (pattern_feats or []) if f.get("candle_bull") or f.get("candle_bear")),
            "chart_pattern_bars": sum(1 for f in (pattern_feats or []) if abs(f.get("chart_score", 0)) > 0.05),
        } if enriched else None,
    }
    # also write sidecar json for auditability
    if enriched:
        try:
            import json as _json
            sidecar = output_path.with_suffix(".pattern.json")
            sidecar.write_text(_json.dumps({"symbol": symbol, "timeframe": timeframe, "rows": written, "coverage": result["pattern_coverage"]}, indent=2))
        except Exception:
            pass
    return result
