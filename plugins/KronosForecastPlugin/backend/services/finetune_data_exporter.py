"""
Kronos Forecast Plugin — Fine-tune Data Exporter

Turns the live OHLCV feed (the very same connectors a forecast reads) into the
CSV format the vendored ``finetune_csv`` pipeline expects, so a user can
fine-tune Kronos on the exact instrument/timeframe they trade.

Kronos CSV columns (see finetune_csv/README.md):
    timestamps,open,high,low,close,volume,amount
``amount`` is quote-volume (price × base volume); 0 is acceptable when unknown.
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


async def export_training_data(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    output_path: Path,
) -> Dict:
    """Fetch ``limit`` OHLCV candles and write them as a Kronos-format CSV.

    Reuses ``forecast_service`` connectors (with backward paging) so
    forex/metals/indices and crypto all resolve through the same source the
    forecast uses. Returns a summary dict; raises ``ValueError`` when too few
    candles are available.
    """
    rows: List[list] = await _fetch_ohlcv_paged(exchange, symbol, timeframe, limit)
    if not rows or len(rows) < MIN_ROWS:
        raise ValueError(
            f"Only {len(rows) if rows else 0} candles available for {symbol} "
            f"({timeframe}) — need at least {MIN_ROWS} to fine-tune. Try a longer "
            f"history or a lower timeframe."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamps", "open", "high", "low", "close", "volume", "amount"])
        for r in rows:
            try:
                ts = _fmt_ts(int(r[0]))
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v = float(r[5]) if len(r) > 5 else 0.0
                writer.writerow([ts, o, h, l, c, v, round(v * c, 8)])
                written += 1
            except (TypeError, ValueError, IndexError):
                continue

    logger.info(f"[Kronos-finetune] exported {written} rows for {symbol} {timeframe} → {output_path}")
    return {
        "path": str(output_path),
        "rows": written,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "first_ts": _fmt_ts(int(rows[0][0])),
        "last_ts": _fmt_ts(int(rows[-1][0])),
    }
