"""Cached OHLCV for the signal pipeline.

One cycle used to download nearly identical 200-bar series five to eight
times for the same pair — the cascade fetched four timeframes, then the
strategies, the Pine evaluators and the fib calculator each re-fetched their
own copy. All of it sequential. On a ~300ms round-trip that is seconds of
pure waiting per pair, minutes across the board.

This module keeps the exact same connector semantics (Bitget swap symbols,
ccxt-shaped rows) but every ``(symbol, timeframe, limit)`` is downloaded at
most once per ``_TTL_S`` window and shared by every caller. A single-flight
lock collapses concurrent requests for the same key into one network call.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List

from loguru import logger

from app.exchanges.manager import SupportedExchange, exchange_manager

#: Matches app.services.candles._TTL_S so both caches expire together.
_TTL_S = 20.0

_cache: Dict[str, tuple[float, List[list]]] = {}
_locks: Dict[str, asyncio.Lock] = {}


def _key(symbol: str, timeframe: str, limit: int, exchange: SupportedExchange) -> str:
    return f"{symbol}:{timeframe}:{limit}:{exchange.value}"


async def get_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = 200,
    exchange: SupportedExchange = SupportedExchange.BITGET,
) -> List[list]:
    """OHLCV rows for ``symbol``, served from cache within the TTL window.

    Returns ``[]`` when the connector has nothing — callers keep whatever
    empty-handling they already had.
    """
    key = _key(symbol, timeframe, limit, exchange)
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _TTL_S:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another coroutine may have populated the cache while we waited.
        now = time.time()
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]

        connector = exchange_manager.get_exchange(exchange)
        if not connector:
            return []
        try:
            rows = await connector.get_ohlcv(
                symbol=symbol, timeframe=timeframe, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - callers already guard empties
            logger.debug(f"[CandleSource] {symbol} {timeframe} fetch failed: {exc}")
            return []

        if rows:
            _cache[key] = (time.time(), list(rows))
            # Keep the lock map from growing without bound.
            if len(_locks) > 512:
                for stale in [k for k in _locks if k not in _cache][:256]:
                    _locks.pop(stale, None)
        return list(rows or [])


async def get_many(
    requests: "list[tuple[str, str, int]]",
    exchange: SupportedExchange = SupportedExchange.BITGET,
) -> Dict[str, List[list]]:
    """Fetch several (symbol, timeframe, limit) triples concurrently."""
    async def _one(sym: str, tf: str, lim: int) -> tuple[str, List[list]]:
        return f"{sym}:{tf}", await get_ohlcv(sym, tf, lim, exchange)

    results = await asyncio.gather(*(_one(*r) for r in requests))
    return dict(results)


def clear() -> None:
    """Drop all cached series (used by tests)."""
    _cache.clear()
