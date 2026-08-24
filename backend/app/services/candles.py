"""One candle resolver for every surface, every instrument.

The repo grew four different OHLCV paths — the orchestrator's
``market_data.fetch_ohlcv_universal``, the room bridge's forex/crypto split,
the strategy analyser's exchange fetch, and Kronos' own multi-source chain —
and each of them could come back empty on its own. That is how a message like
"No candles available for XAUUSD" reached a user on a day gold was trading
normally: one path failed, and nothing tried the next.

This module tries them all, in the order most likely to serve the instrument,
and only reports failure when every source has been asked. Callers get either
real candles or an honest empty list, never a source-specific accident.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Sequence

from loguru import logger

#: Bars a 200-period average and a fib swing both need room for.
DEFAULT_LIMIT = 220

#: When the asked-for timeframe has no bars anywhere, these are tried in turn
#: and the result is folded up to the requested one. A 4h read built from 1h
#: bars is the same market; refusing to answer at all is not.
_FALLBACK_SOURCE_TF: dict[str, tuple[str, ...]] = {
    "1m": (),
    "3m": ("1m",),
    "5m": ("1m",),
    "15m": ("5m", "1m"),
    "30m": ("15m", "5m"),
    "1h": ("30m", "15m"),
    "2h": ("1h", "30m"),
    "4h": ("1h", "30m"),
    "6h": ("1h",),
    "12h": ("4h", "1h"),
    "1d": ("4h", "1h"),
    "1w": ("1d", "4h"),
}

_TF_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800,
}

#: Short-lived cache so the four consumers of a single analysis (agent context,
#: chart, plan levels, telegram card) share one fetch instead of racing four.
_TTL_S = 20.0
_cache: dict[str, tuple[float, List[list]]] = {}


def timeframe_seconds(timeframe: str) -> int:
    return _TF_SECONDS.get((timeframe or "").strip().lower(), 3600)


def _valid(rows: Any, minimum: int = 5) -> bool:
    """Whether a fetch actually produced a usable series."""
    return bool(rows) and isinstance(rows, (list, tuple)) and len(rows) >= minimum


def _resample(rows: Sequence[Sequence[float]], src_tf: str, dst_tf: str) -> List[list]:
    """Fold finer bars up into ``dst_tf`` buckets on a UTC grid.

    Only ever called when the destination timeframe returned nothing from every
    source, so a slightly coarse bucket boundary is strictly better than the
    alternative of having no candles at all.
    """
    step_ms = timeframe_seconds(dst_tf) * 1000
    src_ms = timeframe_seconds(src_tf) * 1000
    if step_ms <= src_ms:
        return list(rows)

    out: List[list] = []
    bucket: Optional[list] = None
    bucket_start = -1
    for r in rows:
        try:
            ts = int(r[0])
            o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        start = ts - (ts % step_ms)
        if bucket is None or start != bucket_start:
            if bucket is not None:
                out.append(bucket)
            bucket_start = start
            bucket = [start, o, h, l, c, v]
            continue
        bucket[2] = max(bucket[2], h)
        bucket[3] = min(bucket[3], l)
        bucket[4] = c
        bucket[5] += v
    if bucket is not None:
        out.append(bucket)
    return out


# ── Individual sources ───────────────────────────────────────────────────────
# Each returns [] rather than raising: a source that is down is a routing fact,
# not an error the caller should have to handle.


async def _from_kronos(symbol: str, timeframe: str, limit: int) -> List[list]:
    """Kronos' own chain: Yahoo (with CME volume) → forex provider → ccxt.

    First because it is the broadest — it is the only path that reaches keyless
    public exchanges, and the only one that anchors FX and metals onto the
    broker's price scale.
    """
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            _fetch_ohlcv as kronos_fetch,
        )

        return await kronos_fetch("bitget", symbol, timeframe, limit) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Candles] kronos source failed for {} {}: {}", symbol, timeframe, exc)
        return []


async def _from_market_data(symbol: str, timeframe: str, limit: int) -> List[list]:
    """The core universal resolver — Yahoo then forex_provider, metals anchored."""
    try:
        from app.services import market_data

        rows, _ticker = await market_data.fetch_ohlcv_universal(
            symbol, timeframe=timeframe, limit=limit
        )
        return rows or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Candles] market_data source failed for {} {}: {}", symbol, timeframe, exc)
        return []


async def _from_exchange(symbol: str, timeframe: str, limit: int) -> List[list]:
    """The configured crypto connectors, via the strategy analyser's fetch."""
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.strategy_analysis import (
            _fetch_ohlcv as exchange_fetch,
        )

        return await exchange_fetch(symbol, timeframe, limit) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Candles] exchange source failed for {} {}: {}", symbol, timeframe, exc)
        return []


_SOURCES = (
    ("kronos", _from_kronos),
    ("market_data", _from_market_data),
    ("exchange", _from_exchange),
)


# ── Public API ───────────────────────────────────────────────────────────────


async def fetch(
    symbol: str,
    timeframe: str = "1h",
    limit: int = DEFAULT_LIMIT,
    *,
    minimum: int = 5,
    allow_resample: bool = True,
) -> List[list]:
    """Candles for ``symbol`` from whichever feed can actually serve it.

    Returns ccxt-shaped rows ``[ms, open, high, low, close, volume]``, or ``[]``
    only when every source and every fallback timeframe has been exhausted.
    """
    sym = (symbol or "").strip().upper()
    tf = (timeframe or "1h").strip().lower()
    if not sym:
        return []

    key = f"{sym}:{tf}:{limit}"
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL_S:
        return hit[1]

    for name, source in _SOURCES:
        rows = await source(sym, tf, limit)
        if _valid(rows, minimum):
            logger.info(
                "[Candles] {} {} — {} bars via {}", sym, tf, len(rows), name
            )
            _cache[key] = (time.time(), list(rows))
            return list(rows)

    if not allow_resample:
        return []

    # Nothing serves this timeframe. Build it from a finer one rather than
    # telling the user the market has no candles.
    for src_tf in _FALLBACK_SOURCE_TF.get(tf, ()):
        factor = max(1, timeframe_seconds(tf) // timeframe_seconds(src_tf))
        finer = await fetch(
            sym, src_tf, limit * factor, minimum=minimum, allow_resample=False
        )
        if not _valid(finer, minimum):
            continue
        folded = _resample(finer, src_tf, tf)
        if _valid(folded, minimum):
            logger.info(
                "[Candles] {} {} — {} bars folded up from {}",
                sym, tf, len(folded), src_tf,
            )
            _cache[key] = (time.time(), folded[-limit:])
            return folded[-limit:]

    logger.warning(
        "[Candles] no OHLCV for {} {} from any source "
        "(kronos/market_data/exchange) or any finer timeframe", sym, tf,
    )
    return []


async def fetch_or_raise(symbol: str, timeframe: str = "1h", limit: int = DEFAULT_LIMIT) -> List[list]:
    """``fetch`` for callers that would rather handle an exception."""
    rows = await fetch(symbol, timeframe, limit)
    if not rows:
        raise LookupError(f"No OHLCV available for {symbol} {timeframe}")
    return rows
