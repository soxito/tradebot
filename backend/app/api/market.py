"""
Global crypto market overview — total market cap, volume, TOTAL3 estimate, bull/bear signal.

Data sources:
  CoinGecko /global              — total market cap, volume, BTC/ETH dominance  (cached 60 s)
  Alternative.me /fng            — Fear & Greed index                            (cached 300 s)
  CoinGecko /global/market_cap_chart — 30-day history for divergence panel       (cached 3600 s)

Bull/bear logic (volume-weighted):
  vol_ratio = total_24h_volume / total_market_cap   (activity intensity proxy)
  chg       = market_cap_change_24h_%

  STRONG_BULL → chg > 3    AND vol_ratio > 0.04
  BULL        → chg > 0    AND vol_ratio ≥ 0.02
  WEAK_BULL   → chg > 0    AND vol_ratio < 0.02   (price up, volume thinning = distribution risk)
  NEUTRAL     → |chg| ≤ 0.5
  WEAK_BEAR   → chg < 0    AND vol_ratio < 0.02   (price down, volume thinning = accumulation)
  BEAR        → chg < -1   AND vol_ratio ≥ 0.02
  STRONG_BEAR → chg < -3   AND vol_ratio > 0.04
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Query
from loguru import logger

from app.exchanges.yahoo_provider import (
    fetch_candles as yahoo_candles,
    fetch_quote as yahoo_quote,
    resolve_ticker,
)

router = APIRouter(prefix="/market", tags=["market"])

# ── in-memory cache (simple TTL dict) ────────────────────────────────────────
_cache: Dict[str, Dict[str, Any]] = {}

CG_BASE = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/"


def _cg_headers() -> Dict[str, str]:
    try:
        from app.core.config import settings
        key = getattr(settings, "COINGECKO_API_KEY", "") or ""
    except Exception:
        key = ""
    return {"x-cg-demo-api-key": key} if key else {}


def _cache_get(key: str, ttl: int) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = {"data": data, "ts": time.monotonic()}


# ── bull/bear signal computation ──────────────────────────────────────────────

def _compute_sentiment(
    chg: float,
    vol_ratio: float,
    fg: int,
) -> tuple[str, list[str]]:
    """Return (sentiment_label, reasons_list)."""
    reasons: list[str] = []

    if chg > 3 and vol_ratio > 0.04:
        label = "STRONG_BULL"
        reasons.append(f"Market cap surged +{chg:.1f}% with high-volume confirmation")
    elif chg > 0 and vol_ratio >= 0.02:
        label = "BULL"
        reasons.append(f"Market cap up +{chg:.1f}% on solid volume")
    elif chg > 0 and vol_ratio < 0.02:
        label = "WEAK_BULL"
        reasons.append(
            f"Market cap up +{chg:.1f}% but volume is thinning (possible distribution)"
        )
    elif abs(chg) <= 0.5:
        label = "NEUTRAL"
        reasons.append(f"Market cap flat ({chg:+.2f}%)")
    elif chg < -3 and vol_ratio > 0.04:
        label = "STRONG_BEAR"
        reasons.append(
            f"Market cap crashed {chg:.1f}% on heavy volume (capitulation risk)"
        )
    elif chg < -1 and vol_ratio >= 0.02:
        label = "BEAR"
        reasons.append(f"Market cap down {chg:.1f}% on active selling pressure")
    else:
        label = "WEAK_BEAR"
        reasons.append(
            f"Market cap down {chg:.1f}% but volume is low (possible quiet accumulation)"
        )

    # Fear & Greed addendum
    if fg <= 25:
        reasons.append(
            f"Fear & Greed at {fg} — Extreme Fear (contrarian buyers historically profitable)"
        )
    elif fg <= 45:
        reasons.append(f"Fear & Greed at {fg} — Fear zone")
    elif fg >= 80:
        reasons.append(
            f"Fear & Greed at {fg} — Extreme Greed (watch for overheating / reversal)"
        )
    elif fg >= 60:
        reasons.append(f"Fear & Greed at {fg} — Greed territory")

    return label, reasons


# ── per-day divergence zone ───────────────────────────────────────────────────

def _divergence_zone(cap_chg: float, vol_chg: float) -> str:
    """
    Classify one day's price-volume relationship:

    confirmed_bull  → cap up  + vol up
    distribution    → cap up  + vol down  (sell into strength)
    confirmed_bear  → cap down + vol up   (heavy sell-off)
    accumulation    → cap down + vol down (quiet dip)
    neutral         → tiny cap move
    """
    if abs(cap_chg) < 0.005:
        return "neutral"
    if cap_chg > 0 and vol_chg > 0:
        return "confirmed_bull"
    if cap_chg > 0 and vol_chg <= 0:
        return "distribution"
    if cap_chg < 0 and vol_chg > 0:
        return "confirmed_bear"
    return "accumulation"  # price down, volume down


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def market_overview():
    """
    Real-time global crypto market summary.

    Returns total market cap, TOTAL3 estimate (ex-BTC/ETH), 24h volume,
    dominance percentages, Fear & Greed index, and a volume-weighted
    bull/bear sentiment label with human-readable reasons.

    Cached 60 seconds to respect CoinGecko rate limits.
    """
    cached = _cache_get("overview", 60)
    if cached:
        return cached

    cg_data: Dict = {}
    fg_value, fg_label = 50, "Neutral"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ── CoinGecko /global ───────────────────────────────────────────
        try:
            r = await client.get(f"{CG_BASE}/global", headers=_cg_headers())
            r.raise_for_status()
            cg_data = r.json().get("data", {})
        except Exception as e:
            logger.warning(f"[Market] CoinGecko /global failed: {e}")

        # ── Fear & Greed (own TTL 300 s) ────────────────────────────────
        fg_cached = _cache_get("fear_greed", 300)
        if fg_cached:
            fg_value, fg_label = fg_cached
        else:
            try:
                r2 = await client.get(FNG_URL, params={"limit": 1, "format": "json"})
                r2.raise_for_status()
                fg_item = r2.json().get("data", [{}])[0]
                fg_value = int(fg_item.get("value", 50))
                fg_label = fg_item.get("value_classification", "Neutral")
                _cache_set("fear_greed", (fg_value, fg_label))
            except Exception as e:
                logger.warning(f"[Market] Fear & Greed fetch failed: {e}")

    total_mc   = float(cg_data.get("total_market_cap",  {}).get("usd", 0) or 0)
    total_vol  = float(cg_data.get("total_volume",      {}).get("usd", 0) or 0)
    chg_24h    = float(cg_data.get("market_cap_change_percentage_24h_usd", 0) or 0)
    mc_pct     = cg_data.get("market_cap_percentage", {})
    btc_dom    = float(mc_pct.get("btc", 0) or 0)
    eth_dom    = float(mc_pct.get("eth", 0) or 0)
    active_ccs = int(cg_data.get("active_cryptocurrencies", 0) or 0)

    # TOTAL3 = TOTAL × (100 − btc_dom − eth_dom) / 100
    total3_est = total_mc * (100.0 - btc_dom - eth_dom) / 100.0 if total_mc else 0.0

    # vol/mcap ratio — proxy for activity intensity
    vol_ratio  = total_vol / total_mc if total_mc > 0 else 0.0

    sentiment, reasons = _compute_sentiment(chg_24h, vol_ratio, fg_value)

    result = {
        "total_market_cap_usd":      total_mc,
        "total3_estimate_usd":       total3_est,
        "total_volume_24h_usd":      total_vol,
        "volume_market_cap_ratio":   round(vol_ratio, 5),
        "market_cap_change_24h_pct": round(chg_24h, 3),
        "btc_dominance":             round(btc_dom, 2),
        "eth_dominance":             round(eth_dom, 2),
        "altcoin_dominance":         round(max(0, 100 - btc_dom - eth_dom), 2),
        "active_cryptocurrencies":   active_ccs,
        "fear_greed_value":          fg_value,
        "fear_greed_label":          fg_label,
        "sentiment":                 sentiment,
        "sentiment_reasons":         reasons,
        "updated_at":                datetime.now(timezone.utc).isoformat(),
    }
    _cache_set("overview", result)
    return result


@router.get("/candles")
async def market_candles(
    symbol: str = Query(..., min_length=2, max_length=24),
    timeframe: str = Query(default="H1"),
    limit: int = Query(default=400, ge=10, le=1000),
    basis: str = Query(
        default="spot",
        pattern="^(spot|futures)$",
        description="Metals only: 'spot' anchors to the spot price (default), "
                    "'futures' returns the raw COMEX contract series.",
    ),
):
    """
    Broker-independent OHLCV for any FX pair, metal, index, commodity or crypto.

    Chart fallback for the MT5 workspace: when the MT5 bridge has no history the
    crypto-exchange fallback only covers crypto, so every other instrument drew
    a blank chart.  Yahoo covers all of them from one source.

    Returns ``{symbol, timeframe, source, candles: [{time, open, high, low,
    close, volume}]}`` — an empty ``candles`` list means no source carries it.
    """
    candles = await yahoo_candles(symbol, timeframe, limit)
    source = "yahoo" if candles else "none"

    # Metals arrive from Yahoo as COMEX futures, ~1.4% above spot. Anchor them
    # so the chart, the live line and every quoted price agree; without this the
    # chart would sit a visible $58 above the price shown everywhere else.
    # basis=futures skips that for callers who genuinely want the contract.
    from app.services import market_data

    is_metal = market_data.classify(symbol) == market_data.METAL
    if candles and is_metal:
        if basis == market_data.FUTURES:
            source = f"yahoo:{resolve_ticker(symbol)} (futures)"
        else:
            rows = [
                [c["time"] * 1000, c["open"], c["high"], c["low"], c["close"],
                 c.get("volume") or 0.0]
                for c in candles
            ]
            rows, source = await market_data.anchor_metal_series_to_spot(
                market_data.normalize_symbol(symbol), rows, resolve_ticker(symbol) or symbol
            )
            candles = [
                {"time": int(r[0] / 1000), "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4], "volume": r[5]}
                for r in rows
            ]

    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "source": source,
        "ticker": resolve_ticker(symbol),
        # Tells the client whether the basis toggle is meaningful for this
        # instrument, so the UI can hide it for everything that has one series.
        "basis": basis if is_metal else None,
        "supports_basis": is_metal,
        "candles": candles,
    }


@router.get("/price")
async def market_price(
    symbol: str = Query(..., min_length=2, max_length=24),
    basis: str = Query(
        default="spot",
        pattern="^(spot|futures)$",
        description="Metals only: 'spot' (default) or the COMEX 'futures' contract.",
    ),
):
    """
    Live quote for any FX pair, metal, index, commodity or crypto.

    The chart's live line and forming bar poll a quote rather than candles, so a
    chart drawn from Yahoo needs a Yahoo quote — the crypto-exchange ticker it
    used to fall back to does not list GBPUSD, US30 or spot gold.

    Returns ``{symbol, ticker, source, price}``; ``price`` is null when no
    source carries the symbol.
    """
    # Routed through market_data rather than straight to Yahoo so metals get
    # genuine spot: Yahoo prices gold and silver off the COMEX future, which
    # sits ~1.4% above spot, and a chart's live line must match the price the
    # rest of the app quotes.
    from app.services import market_data

    is_metal = market_data.classify(symbol) == market_data.METAL
    quote = await market_data.get_quote(symbol, basis=basis)
    if quote is not None:
        return {
            "symbol": quote.symbol,
            "ticker": resolve_ticker(symbol) or quote.symbol,
            "source": quote.source,
            "price": quote.price,
            "bid": quote.bid,
            "ask": quote.ask,
            "time": quote.ts,
            "basis": basis if is_metal else None,
            "supports_basis": is_metal,
        }
    fallback = await yahoo_quote(symbol)
    return {
        "symbol": symbol.upper(),
        "ticker": (fallback or {}).get("ticker") or resolve_ticker(symbol),
        "source": "yahoo" if fallback else "none",
        "price": (fallback or {}).get("price"),
        "time": (fallback or {}).get("time"),
    }


@router.get("/history")
async def market_history(days: int = Query(default=30, ge=1, le=90)):
    """
    Daily global market cap + volume history for the volume divergence panel.

    Returns [{date_iso, market_cap_usd, volume_usd, divergence_zone}].
    Cached 3600 seconds (intraday historical data is stable).
    """
    cache_key = f"history_{days}"
    cached = _cache_get(cache_key, 3600)
    if cached:
        return cached

    raw: Dict = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(
                f"{CG_BASE}/global/market_cap_chart",
                params={"vs_currency": "usd", "days": days},
                headers=_cg_headers(),
            )
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            logger.warning(f"[Market] CoinGecko market_cap_chart failed: {e}")
            return []

    mc_points  = raw.get("market_cap", [])   # [[ts_ms, value], ...]
    vol_points = raw.get("volume",     [])

    records: list[Dict[str, Any]] = []
    prev_mc  = None
    prev_vol = None

    for i, (ts_ms, mc) in enumerate(mc_points):
        vol      = vol_points[i][1] if i < len(vol_points) else 0.0
        date_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        cap_chg = (mc - prev_mc) / prev_mc if (prev_mc and prev_mc > 0) else 0.0
        vol_chg = (vol - prev_vol) / prev_vol if (prev_vol and prev_vol > 0) else 0.0

        records.append({
            "date_iso":        date_iso,
            "market_cap_usd":  mc,
            "volume_usd":      vol,
            "divergence_zone": _divergence_zone(cap_chg, vol_chg),
        })

        prev_mc  = mc
        prev_vol = vol

    _cache_set(cache_key, records)
    return records
