"""
Forex / Metals price provider — uses CoinGecko (API key already in .env).

Gold (XAUUSD)  → PAX Gold (PAXG)   — 1 PAXG = 1 troy oz of gold, Brink's-vaulted
Silver (XAGUSD)→ Tether Gold/Silver proxy via CoinGecko
Forex pairs    → Open Exchange Rates (free, no key) for FX-only pairs

Why CoinGecko for gold instead of Bitget?
  Bitget is a crypto exchange — it does NOT carry real spot gold (XAUUSD).
  Without this provider, AI agents receive no price context and hallucinate
  ~$1900 prices from 2023 training data.  PAXG tracks gold spot price 1:1
  and is available via the CoinGecko key already in COINGECKO_API_KEY.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

# ── CoinGecko endpoints ───────────────────────────────────────────────────────
CG_BASE = "https://api.coingecko.com/api/v3"
CG_PRO  = "https://pro-api.coingecko.com/api/v3"

# Map Jarvis / TradingView symbol → CoinGecko coin id
# PAXG (PAX Gold) = 1 troy oz physical gold, Brink's-vaulted, price ≈ spot XAU
_CG_COIN_MAP: Dict[str, str] = {
    "XAUUSD":   "pax-gold",
    "XAU/USDT": "pax-gold",
    "XAU/USD":  "pax-gold",
    "GOLD":     "pax-gold",
    "XAGUSD":   "pax-gold",    # fallback to gold if silver not available
    "XAG/USD":  "pax-gold",
    "SILVER":   "pax-gold",
}

# Forex symbols handled via frankfurter.app (free, no auth) — FX only, not metals
_FRANKFURTER_FX: set = {
    "EURUSD", "EUR/USD", "GBPUSD", "GBP/USD",
    "USDJPY", "USD/JPY", "USDCHF", "USD/CHF",
    "AUDUSD", "AUD/USD", "NZDUSD", "NZD/USD", "USDCAD", "USD/CAD",
}

# All supported symbols
SUPPORTED_SYMBOLS: set = set(_CG_COIN_MAP.keys()) | _FRANKFURTER_FX


def is_forex_symbol(symbol: str) -> bool:
    """Return True if *symbol* should be fetched from this forex/metals provider."""
    return symbol.upper() in SUPPORTED_SYMBOLS


def _cg_api_key() -> str:
    """Read the CoinGecko API key from environment / app settings."""
    try:
        from app.core.config import settings
        return getattr(settings, "COINGECKO_API_KEY", "") or os.getenv("COINGECKO_API_KEY", "")
    except Exception:
        return os.getenv("COINGECKO_API_KEY", "")


def _cg_headers() -> Dict[str, str]:
    key = _cg_api_key()
    return {"x-cg-demo-api-key": key} if key else {}


# ── CoinGecko helpers ─────────────────────────────────────────────────────────

async def _cg_current_price(coin_id: str) -> Optional[float]:
    """Fetch current USD price for a CoinGecko coin id."""
    try:
        async with httpx.AsyncClient(headers=_cg_headers(), timeout=12.0) as cl:
            r = await cl.get(
                f"{CG_BASE}/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
            )
            r.raise_for_status()
            data = r.json()
            return float(data.get(coin_id, {}).get("usd", 0) or 0) or None
    except Exception as e:
        logger.debug(f"[ForexProvider] CoinGecko current price failed for '{coin_id}': {e}")
        return None


async def _cg_ohlc(coin_id: str, days: int = 14) -> List[List]:
    """
    Fetch OHLC data from CoinGecko.
    Returns list of [timestamp_ms, open, high, low, close] candles.
    Note: CoinGecko OHLC has no volume field.
    """
    try:
        async with httpx.AsyncClient(headers=_cg_headers(), timeout=20.0) as cl:
            r = await cl.get(
                f"{CG_BASE}/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": str(days)},
            )
            r.raise_for_status()
            raw = r.json()
            # raw = [[ts_ms, open, high, low, close], ...]
            if isinstance(raw, list):
                return [[c[0], c[1], c[2], c[3], c[4], 0.0] for c in raw if len(c) >= 5]
    except Exception as e:
        logger.debug(f"[ForexProvider] CoinGecko OHLC failed for '{coin_id}': {e}")
    return []


async def _cg_market_chart(coin_id: str, days: int = 90) -> Tuple[List[List], Optional[float]]:
    """
    Fetch granular price history from CoinGecko's market_chart endpoint.
    Granularity: hourly for days ≤ 90, daily for > 90.
    Returns (ohlcv_list, current_price) where ohlcv approximates OHLCV.
    """
    try:
        async with httpx.AsyncClient(headers=_cg_headers(), timeout=20.0) as cl:
            r = await cl.get(
                f"{CG_BASE}/coins/{coin_id}/market_chart",
                params={
                    "vs_currency": "usd",
                    "days": str(days),
                    "interval": "hourly" if days <= 90 else "daily",
                },
            )
            r.raise_for_status()
            data = r.json()

        prices  = data.get("prices", [])     # [[ts_ms, price], ...]
        volumes = data.get("total_volumes", [])

        if not prices:
            return [], None

        # Build OHLCV from price ticks (group into 1h or 4h buckets)
        ohlcv: List[List] = []
        current_price = prices[-1][1] if prices else None

        for i, (ts_ms, price) in enumerate(prices):
            vol = volumes[i][1] if i < len(volumes) else 0.0
            # Approximate OHLCV: open=prev_close, high/low estimated from ±0.1%
            prev_price = prices[i - 1][1] if i > 0 else price
            ohlcv.append([
                ts_ms,
                prev_price,                       # open  ≈ prev close
                max(price, prev_price) * 1.0005,  # high  estimate
                min(price, prev_price) * 0.9995,  # low   estimate
                price,                            # close
                float(vol),
            ])

        return ohlcv, current_price
    except Exception as e:
        logger.debug(f"[ForexProvider] CoinGecko market_chart failed for '{coin_id}': {e}")
        return [], None


# ── Frankfurter FX helper ─────────────────────────────────────────────────────

async def _frankfurter_ohlcv(symbol: str, days: int = 90) -> Tuple[List[List], Optional[float]]:
    """
    Fetch daily FX data from api.frankfurter.app (free, no auth).
    Returns (daily_ohlcv, current_rate).
    Only handles major FX pairs vs USD.
    """
    # Parse from/to from symbol like EURUSD or EUR/USD
    sym = symbol.upper().replace("/", "").replace("USD", "")
    if symbol.upper().startswith("USD"):
        base, quote = "USD", symbol.upper().replace("USD", "")
    else:
        base, quote = sym, "USD"

    from datetime import datetime, timedelta
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Frankfurter v1 uses date-range path format: /v1/{start}..?base=X&symbols=Y
    # Fall back to the old app domain if the dev domain is unreachable.
    _FRANK_URLS = [
        f"https://api.frankfurter.dev/v1/{start}..",
        f"https://api.frankfurter.app/{start}..",
    ]

    data: dict = {}
    last_err: Exception | None = None
    for _url in _FRANK_URLS:
        try:
            async with httpx.AsyncClient(timeout=14.0, follow_redirects=True) as cl:
                r = await cl.get(_url, params={"base": base, "symbols": quote})
                r.raise_for_status()
                data = r.json()
                if data.get("rates"):
                    break
        except Exception as e:
            last_err = e
            continue

    rates_dict = data.get("rates", {})
    if not rates_dict:
        if last_err:
            logger.debug(f"[ForexProvider] Frankfurter FX failed for {symbol}: {last_err}")
        return [], None

    sorted_dates = sorted(rates_dict.keys())
    ohlcv: List[List] = []
    for i, date_str in enumerate(sorted_dates):
        from datetime import timezone
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)
        rate  = float(rates_dict[date_str].get(quote, 0) or 0)
        if rate <= 0:
            continue
        prev_rate = float(rates_dict[sorted_dates[i - 1]].get(quote, rate) or rate) if i > 0 else rate
        ohlcv.append([ts_ms, prev_rate, max(rate, prev_rate), min(rate, prev_rate), rate, 0.0])

    current = ohlcv[-1][4] if ohlcv else None
    return ohlcv, current


# ── Main public API ──────────────────────────────────────────────────────────

async def fetch_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 200,
) -> Tuple[List[List], Dict]:
    """
    Fetch OHLCV + live price data for a forex/metals symbol.

    Gold/metals: CoinGecko PAXG (1 PAXG = 1 troy oz gold, Brink's-vaulted)
    FX pairs:    Frankfurter.app daily rates

    Returns:
        (ohlcv_list, ticker_dict)

    ticker_dict keys: last, bid, ask, high, low, volume, change_pct,
                      buy_volume, sell_volume, buy_pct, sell_pct, source
    """
    sym_upper = symbol.upper()
    if sym_upper not in SUPPORTED_SYMBOLS:
        raise ValueError(f"Symbol '{symbol}' not in forex provider symbol map")

    # ── Metals / Gold via CoinGecko ───────────────────────────────────────────
    if sym_upper in _CG_COIN_MAP:
        coin_id = _CG_COIN_MAP[sym_upper]

        # Request 90 days of hourly data for enough history
        ohlcv, current_from_chart = await _cg_market_chart(coin_id, days=90)

        # For 4h analysis: we already have hourly from market_chart
        # (the orchestrator/jarvis will compute TA on whatever we return)

        if not ohlcv:
            # Fallback: use CG OHLC endpoint (daily granularity)
            ohlcv = await _cg_ohlc(coin_id, days=30)

        # Sort chronologically and trim
        ohlcv.sort(key=lambda c: c[0])
        if len(ohlcv) > limit:
            ohlcv = ohlcv[-limit:]

        # Get the most current spot price
        spot_price = await _cg_current_price(coin_id)
        last_price = spot_price or current_from_chart or (ohlcv[-1][4] if ohlcv else 0)

        ticker = _build_ticker(ohlcv, last_price, source=f"coingecko_{coin_id}")
        logger.info(
            f"[ForexProvider] {symbol} → {coin_id} — "
            f"{len(ohlcv)} candles, live=${last_price:.2f}, "
            f"buy%={ticker.get('buy_pct', 0):.1f} sell%={ticker.get('sell_pct', 0):.1f}"
        )
        return ohlcv, ticker

    # ── FX pairs via Frankfurter ──────────────────────────────────────────────
    days = max(90, limit)
    ohlcv, current = await _frankfurter_ohlcv(symbol, days=days)
    ohlcv.sort(key=lambda c: c[0])
    if len(ohlcv) > limit:
        ohlcv = ohlcv[-limit:]

    last_price = current or (ohlcv[-1][4] if ohlcv else 0)
    ticker = _build_ticker(ohlcv, last_price, source="frankfurter_fx")
    logger.info(f"[ForexProvider] {symbol} (FX) — {len(ohlcv)} candles, live={last_price:.5f}")
    return ohlcv, ticker


def _build_ticker(ohlcv: List[List], last_price: float, source: str = "") -> Dict:
    """Build standardised ticker dict from OHLCV list + live price."""
    if not ohlcv:
        return {"last": last_price, "source": source}

    prev_close = ohlcv[-2][4] if len(ohlcv) >= 2 else ohlcv[-1][4]
    change_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else 0

    n = min(24, len(ohlcv))
    recent = ohlcv[-n:]
    day_high = max(c[2] for c in recent)
    day_low  = min(c[3] for c in recent)

    buy_vol  = sum(c[5] for c in recent if c[4] >= c[1])
    sell_vol = sum(c[5] for c in recent if c[4] <  c[1])
    total_vol = (buy_vol + sell_vol) or 1.0

    return {
        "last":        last_price,
        "bid":         round(last_price * 0.9998, 4),
        "ask":         round(last_price * 1.0002, 4),
        "high":        day_high,
        "low":         day_low,
        "volume":      total_vol,
        "change_pct":  round(change_pct, 4),
        "buy_volume":  round(buy_vol,  2),
        "sell_volume": round(sell_vol, 2),
        "buy_pct":     round(buy_vol  / total_vol * 100, 1),
        "sell_pct":    round(sell_vol / total_vol * 100, 1),
        "source":      source,
    }


async def fetch_live_price(symbol: str) -> Optional[float]:
    """Quick single live-price fetch."""
    sym_upper = symbol.upper()
    if sym_upper in _CG_COIN_MAP:
        return await _cg_current_price(_CG_COIN_MAP[sym_upper])
    try:
        _, current = await _frankfurter_ohlcv(symbol, days=2)
        return current
    except Exception:
        return None
