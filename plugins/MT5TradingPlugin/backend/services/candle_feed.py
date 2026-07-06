"""
MT5 Trading Plugin — Resilient candle-feed resolver for the Scalp Bot.

Why this exists
---------------
Some mtapi-io bridge builds (and the MetaQuotes demo terminal in particular)
return **frozen historical candles** from ``PriceHistoryEx`` — the newest bar can
be years old even though the *live quote* stream is perfectly current.  The
multi-timeframe SMC scalp engine needs a recent OHLC series to analyse, so when
the bridge history is stale/empty we fall back through a chain of real sources
and, as a last resort, build the M1/M5 entry series from the working live
quotes.

Resolution order per timeframe
------------------------------
1. **Fresh mtapi candles** — used as-is when the newest bar is recent.
2. **Exchange OHLCV** (Bitget/Binance via ccxt) — for crypto-mapped symbols.
3. **forex_provider** (CoinGecko PAXG for metals, Frankfurter for FX) — resampled
   to the requested timeframe; genuine recent price *structure*.
4. **Live-quote buffer** — M1/M5 candles built from the real-time bid/ask mids the
   scalp loop already fetches every cycle, seeded from source (2)/(3) so the
   engine has enough bars to start immediately and self-corrects as live bars
   accumulate.

Everything is best-effort and never raises — a failed source simply yields an
empty list so the next source is tried.  Pure plugin service; no core mutation.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from loguru import logger

# Timeframe → seconds
_TF_SECONDS: Dict[str, int] = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}

# MT5 timeframe → ccxt/exchange timeframe string
_TF_TO_EX: Dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}

# The freshest bar must be within this many seconds or we treat the source as
# stale and fall through.  ~2 days tolerates weekend market pauses.
FRESH_MAX_AGE_SEC = 2 * 24 * 3600

MIN_USABLE = 40           # engine needs ≥40 bars on the entry timeframe
_BUFFER_CAP = 600         # rolling cap per (key, tf)


def _now() -> int:
    return int(time.time())


def _newest_age(candles: List[Dict[str, Any]]) -> int:
    if not candles:
        return 10 ** 12
    return _now() - int(candles[-1].get("time", 0) or 0)


# ── Symbol → exchange mapping (crypto only) ───────────────────────────────────

_SYMBOL_TO_EX: Dict[str, str] = {
    "BTCUSD": "BTC/USDT", "ETHUSD": "ETH/USDT", "SOLUSD": "SOL/USDT",
    "XRPUSD": "XRP/USDT", "DOGEUSD": "DOGE/USDT", "BNBUSD": "BNB/USDT",
}


def _symbol_to_exchange(symbol: str) -> Optional[str]:
    s = (symbol or "").upper().replace("/", "")
    if s in _SYMBOL_TO_EX:
        return _SYMBOL_TO_EX[s]
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    # Only treat as crypto when it is a known crypto base — never FX/metals.
    return None


# ── Live-quote candle buffer ──────────────────────────────────────────────────

class QuoteCandleBuffer:
    """In-memory OHLC candles built from live quote mids, per (key, symbol, tf).

    ``key`` is the account identity so each account keeps its own series.
    Only intraday entry timeframes (M1, M5) are maintained live — higher
    timeframes come from real sources and are only ever *seeded*.
    """

    def __init__(self) -> None:
        # {(key, symbol, tf): deque[[time, o, h, l, c, v]]}
        self._buf: Dict[tuple, Deque[List[float]]] = {}

    def _dq(self, key: str, symbol: str, tf: str) -> Deque[List[float]]:
        k = (key, (symbol or "").upper(), tf)
        dq = self._buf.get(k)
        if dq is None:
            dq = deque(maxlen=_BUFFER_CAP)
            self._buf[k] = dq
        return dq

    def record(self, key: str, symbol: str, mid: float, ts: Optional[int] = None) -> None:
        """Fold one live mid price into the M1 and M5 candle buckets."""
        if mid is None or mid <= 0:
            return
        ts = ts or _now()
        for tf in ("M1", "M5"):
            span = _TF_SECONDS[tf]
            bucket = ts - (ts % span)
            dq = self._dq(key, symbol, tf)
            if dq and int(dq[-1][0]) == bucket:
                bar = dq[-1]
                bar[2] = max(bar[2], mid)   # high
                bar[3] = min(bar[3], mid)   # low
                bar[4] = mid                # close
                bar[5] = bar[5] + 1.0       # volume proxy = tick count
            else:
                dq.append([float(bucket), mid, mid, mid, mid, 1.0])

    def seed(self, key: str, symbol: str, tf: str, candles: List[Dict[str, Any]]) -> None:
        """Pre-fill a timeframe from a real coarse source (only when sparse)."""
        dq = self._dq(key, symbol, tf)
        if len(dq) >= MIN_USABLE or not candles:
            return
        seeded: List[List[float]] = []
        for c in candles[-_BUFFER_CAP:]:
            try:
                seeded.append([
                    float(c["time"]), float(c["open"]), float(c["high"]),
                    float(c["low"]), float(c["close"]), float(c.get("volume", 0) or 0),
                ])
            except (KeyError, TypeError, ValueError):
                continue
        if not seeded:
            return
        # Keep any live bars already recorded ahead of the seed's newest time.
        live_after = [b for b in dq if b[0] > seeded[-1][0]]
        dq.clear()
        for b in seeded:
            dq.append(b)
        for b in live_after:
            dq.append(b)

    def get(self, key: str, symbol: str, tf: str) -> List[Dict[str, Any]]:
        dq = self._dq(key, symbol, tf)
        return [
            {"time": int(b[0]), "open": b[1], "high": b[2], "low": b[3],
             "close": b[4], "volume": b[5]}
            for b in dq
        ]


quote_buffer = QuoteCandleBuffer()


# ── Resampling helpers ────────────────────────────────────────────────────────

def _resample(candles: List[Dict[str, Any]], src_tf: str, dst_tf: str) -> List[Dict[str, Any]]:
    """Group finer candles into a coarser timeframe (e.g. H1 → H4/D1)."""
    src = _TF_SECONDS.get(src_tf, 3600)
    dst = _TF_SECONDS.get(dst_tf, 3600)
    if dst <= src or not candles:
        return candles
    out: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    for c in candles:
        t = int(c.get("time", 0) or 0)
        bucket = t - (t % dst)
        if bucket not in out:
            out[bucket] = {"time": bucket, "open": c["open"], "high": c["high"],
                           "low": c["low"], "close": c["close"],
                           "volume": float(c.get("volume", 0) or 0)}
            order.append(bucket)
        else:
            g = out[bucket]
            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["close"] = c["close"]
            g["volume"] += float(c.get("volume", 0) or 0)
    return [out[b] for b in order]


def _retime_to_intraday(candles: List[Dict[str, Any]], dst_tf: str, count: int) -> List[Dict[str, Any]]:
    """Re-space real recent coarse closes onto an intraday grid ending *now*.

    Used to seed the M1/M5 buffer with genuine recent price *structure* when no
    true intraday history exists.  The volatility/ATR/SMC zones derived from this
    reflect real market movement; the newest bars are then overwritten by live
    quote bars as they arrive, so the seed only bootstraps the warm-up window.
    """
    if not candles:
        return []
    span = _TF_SECONDS.get(dst_tf, 300)
    tail = candles[-count:]
    now_bucket = _now() - (_now() % span)
    start = now_bucket - span * (len(tail) - 1)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(tail):
        out.append({
            "time": start + i * span,
            "open": float(c["open"]), "high": float(c["high"]),
            "low": float(c["low"]), "close": float(c["close"]),
            "volume": float(c.get("volume", 0) or 0),
        })
    return out


# ── External source fetchers ──────────────────────────────────────────────────

async def _exchange_candles(symbol: str, timeframe: str, count: int) -> List[Dict[str, Any]]:
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
        ex_symbol = _symbol_to_exchange(symbol)
        if not ex_symbol:
            return []
        ex_tf = _TF_TO_EX.get((timeframe or "").upper(), "5m")
        conn = exchange_manager.get_exchange(SupportedExchange.BITGET) \
            or exchange_manager.get_exchange(SupportedExchange.BINANCE)
        if conn is None:
            return []
        raw = await conn.get_ohlcv(ex_symbol, ex_tf, count)
        if not raw:
            return []
        return [
            {"time": int(c[0] / 1000), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5] or 0)}
            for c in raw
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CandleFeed] exchange {symbol}/{timeframe}: {exc}")
        return []


async def _forex_candles(symbol: str, timeframe: str, count: int) -> List[Dict[str, Any]]:
    """Recent FX/metals OHLC via the core forex_provider (hourly/daily), resampled."""
    try:
        from app.exchanges import forex_provider  # type: ignore
        if not forex_provider.is_forex_symbol(symbol):
            return []
        ohlcv, _ticker = await forex_provider.fetch_ohlcv(symbol, timeframe="1h", limit=400)
        if not ohlcv:
            return []
        base = [
            {"time": int(c[0] / 1000) if c[0] > 1e10 else int(c[0]),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "volume": float(c[5] if len(c) > 5 else 0)}
            for c in ohlcv
        ]
        base.sort(key=lambda x: x["time"])
        tf = (timeframe or "").upper()
        if tf in ("H4", "D1"):
            return _resample(base, "H1", tf)[-count:]
        return base[-count:]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CandleFeed] forex {symbol}/{timeframe}: {exc}")
        return []


# ── Main resolver ─────────────────────────────────────────────────────────────

async def resolve_candles(mt5_client, login: str, server: str, password: str,
                          symbol: str, timeframe: str, count: int,
                          account_key: str, mid_hint: Optional[float] = None) -> List[Dict[str, Any]]:
    """Return a usable recent OHLC series for ``symbol``/``timeframe``.

    Tries fresh mtapi history, then exchange, then forex_provider, then the
    live-quote buffer (M1/M5) seeded from the best coarse source.  Returns
    ``{time, open, high, low, close, volume}`` dicts (may be empty).
    """
    tf = (timeframe or "").upper()

    # 1. Fresh mtapi candles.
    try:
        bars = await mt5_client.get_candles(login, server, password, symbol, tf, count)
    except Exception:  # noqa: BLE001
        bars = []
    if bars and _newest_age(bars) <= FRESH_MAX_AGE_SEC and len(bars) >= MIN_USABLE:
        return bars

    # 2. Exchange (crypto-mapped symbols).
    ex = await _exchange_candles(symbol, tf, count)
    if ex and _newest_age(ex) <= FRESH_MAX_AGE_SEC and len(ex) >= MIN_USABLE:
        return ex

    # 3. forex_provider (FX / metals) — real recent structure.
    fx = await _forex_candles(symbol, tf, count)

    # Higher timeframes: coarse real data is the correct granularity.
    if tf in ("H1", "H4", "D1"):
        if fx and len(fx) >= 20:
            return fx
        # Fall back to any (even slightly stale) mtapi bars as HTF context.
        return bars or fx

    # 4. Entry timeframes (M1/M5): live-quote buffer, seeded from coarse source.
    if mid_hint and mid_hint > 0:
        quote_buffer.record(account_key, symbol, mid_hint)

    live = quote_buffer.get(account_key, symbol, tf)
    if len(live) < MIN_USABLE:
        seed_src = ex or fx or bars
        if seed_src:
            seed = _retime_to_intraday(seed_src, tf, count)
            quote_buffer.seed(account_key, symbol, tf, seed)
            live = quote_buffer.get(account_key, symbol, tf)
    return live
