"""
AI Market Analyst — Market Data Service

Fetches OHLCV candles from the existing exchange connectors and caches recent snapshots.
"""
from typing import Dict, List, Optional
from datetime import datetime
from loguru import logger

from app.exchanges.manager import exchange_manager, SupportedExchange


_snapshot_cache: Dict[str, Dict] = {}


async def fetch_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 100,
    exchange_id: Optional[str] = None,
) -> List[Dict]:
    """
    Fetch OHLCV candles via the existing exchange manager.

    Returns list of: {"timestamp": int, "open": float, "high": float,
                       "low": float, "close": float, "volume": float}
    """
    try:
        # Resolve exchange
        exch = None
        if exchange_id:
            try:
                exch_enum = SupportedExchange(exchange_id)
                exch = exchange_manager.get_exchange(exch_enum)
            except ValueError:
                pass

        # Fallback to first available exchange
        if exch is None:
            available = exchange_manager.get_all_exchanges()
            if available:
                exch = exchange_manager.get_exchange(available[0])

        if exch is None:
            logger.warning(f"[MarketData] No exchange available for {symbol}")
            return []

        raw = await exch.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        candles = []
        for row in raw:
            candles.append({
                "timestamp": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0,
            })
        return candles
    except Exception as exc:
        logger.error(f"[MarketData] OHLCV fetch failed for {symbol}/{timeframe}: {exc}")
        return []


async def fetch_ticker(symbol: str, exchange_id: Optional[str] = None) -> Optional[Dict]:
    """Fetch latest ticker for a symbol."""
    try:
        exch = None
        if exchange_id:
            try:
                exch_enum = SupportedExchange(exchange_id)
                exch = exchange_manager.get_exchange(exch_enum)
            except ValueError:
                pass
        if exch is None:
            available = exchange_manager.get_all_exchanges()
            if available:
                exch = exchange_manager.get_exchange(available[0])
        if exch is None:
            return None
        return await exch.fetch_ticker(symbol)
    except Exception as exc:
        logger.error(f"[MarketData] Ticker fetch failed for {symbol}: {exc}")
        return None


def cache_snapshot(symbol: str, timeframe: str, data: Dict):
    """Cache a computed snapshot for dedup / overlay use."""
    key = f"{symbol}:{timeframe}"
    _snapshot_cache[key] = {**data, "_cached_at": datetime.utcnow().isoformat()}


def get_cached_snapshot(symbol: str, timeframe: str) -> Optional[Dict]:
    return _snapshot_cache.get(f"{symbol}:{timeframe}")
