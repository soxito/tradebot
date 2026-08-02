"""Spot prices for precious metals — XAUUSD, XAGUSD, XPTUSD, XPDUSD.

Why this exists
---------------
Yahoo has no spot metal ticker at all, so ``yahoo_provider`` maps gold to the
COMEX future ``GC=F``. A future is not spot: it carries the cost of carry to
expiry, and the contract Yahoo serves is not always the front month. Measured
together on the same tick, ``GC=F`` printed 4105.10 while spot gold was 4047.31
— a 1.4% gap, about $58/oz. Quoting that as "the gold price" is simply the
wrong number, and it is wrong in a way that compounds: a trade proposal built
from futures candles puts entry, stop and target ~$58 above where the user's
broker would actually fill them.

Sources, in the order they are tried:

1. **Swissquote** public BBO — a real broker's streaming spot quote, with bid
   and ask and a timestamp, no API key. This is the closest thing available to
   what the user's own MT5 broker would show them.
2. **gold-api.com** — keyless spot for all four metals; a straightforward
   single-value backstop when Swissquote is unreachable.
3. **CoinGecko PAX Gold** — gold only. PAXG is a claim on one troy ounce of
   allocated gold and tracks spot within ~0.2%. Last resort, because it is a
   token price rather than a metal price and can drift from its peg.

Yahoo futures remain in the chain behind all of these, but the caller labels
them honestly rather than passing them off as spot.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import httpx
from loguru import logger

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)"}
_TIMEOUT = 8.0

#: Instrument name → the metal's ISO 4217 code.
_METAL_CODES: Dict[str, str] = {
    "XAUUSD": "XAU", "XAU": "XAU", "GOLD": "XAU",
    "XAGUSD": "XAG", "XAG": "XAG", "SILVER": "XAG",
    "XPTUSD": "XPT", "XPT": "XPT", "PLATINUM": "XPT",
    "XPDUSD": "XPD", "XPD": "XPD", "PALLADIUM": "XPD",
}

#: CoinGecko tokens that track a metal one-for-one. Gold only — no silver token
#: is liquid or reliably pegged enough to quote as a silver price.
_CG_TOKENS = {"XAU": "pax-gold"}

_SWISSQUOTE = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument"
_GOLD_API = "https://api.gold-api.com/price"

#: Spot moves continuously; a short cache keeps a chat turn that mentions gold
#: twice down to one upstream call without the number going stale.
_TTL = 10.0
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

#: Sanity band. A metal quote outside this range means the upstream changed
#: shape or returned another instrument, and a wrong number stated confidently
#: is worse than no number.
_SANITY: Dict[str, tuple[float, float]] = {
    "XAU": (200.0, 100_000.0),
    "XAG": (2.0, 2_000.0),
    "XPT": (100.0, 50_000.0),
    "XPD": (100.0, 50_000.0),
}


def metal_code(symbol: str) -> Optional[str]:
    """``XAUUSD`` → ``XAU``, or None when *symbol* is not a spot metal."""
    s = (symbol or "").upper().replace("/", "").replace("-", "").strip()
    return _METAL_CODES.get(s)


def is_spot_metal(symbol: str) -> bool:
    return metal_code(symbol) is not None


def _sane(code: str, price: float) -> bool:
    low, high = _SANITY.get(code, (0.0, float("inf")))
    return low <= price <= high


async def _from_swissquote(client: httpx.AsyncClient, code: str) -> Optional[Dict[str, Any]]:
    """Broker BBO — bid, ask and the tick's own timestamp."""
    resp = await client.get(f"{_SWISSQUOTE}/{code}/USD", headers=_HEADERS)
    resp.raise_for_status()
    blocks = resp.json()
    if not isinstance(blocks, list) or not blocks:
        return None

    block = blocks[0]
    profiles = block.get("spreadProfilePrices") or []
    if not profiles:
        return None
    # The tightest profile is the closest to the underlying market.
    best = min(profiles, key=lambda p: float(p.get("askSpread") or 1e9))
    bid, ask = float(best.get("bid") or 0), float(best.get("ask") or 0)
    if bid <= 0 or ask <= 0:
        return None

    price = (bid + ask) / 2
    if not _sane(code, price):
        return None
    ts_ms = block.get("ts")
    return {
        "price": price, "bid": bid, "ask": ask,
        "source": "swissquote-spot",
        "ts": int(ts_ms / 1000) if ts_ms else int(time.time()),
    }


async def _from_gold_api(client: httpx.AsyncClient, code: str) -> Optional[Dict[str, Any]]:
    resp = await client.get(f"{_GOLD_API}/{code}", headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    price = float(data.get("price") or 0)
    if price <= 0 or not _sane(code, price):
        return None
    return {"price": price, "bid": None, "ask": None,
            "source": "gold-api-spot", "ts": int(time.time())}


async def _from_coingecko(client: httpx.AsyncClient, code: str) -> Optional[Dict[str, Any]]:
    token = _CG_TOKENS.get(code)
    if not token:
        return None
    resp = await client.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": token, "vs_currencies": "usd"},
        headers=_HEADERS,
    )
    resp.raise_for_status()
    price = float(((resp.json() or {}).get(token) or {}).get("usd") or 0)
    if price <= 0 or not _sane(code, price):
        return None
    return {"price": price, "bid": None, "ask": None,
            "source": f"coingecko:{token}", "ts": int(time.time())}


async def fetch_spot(symbol: str) -> Optional[Dict[str, Any]]:
    """Live spot price for a metal — ``{price, bid, ask, source, ts}`` or None.

    Returns None rather than falling back to a futures price: the caller can
    decide to use futures and say so, but this function only ever answers with
    genuine spot.
    """
    code = metal_code(symbol)
    if not code:
        return None

    hit = _cache.get(code)
    if hit and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for fetch in (_from_swissquote, _from_gold_api, _from_coingecko):
            try:
                quote = await fetch(client, code)
            except Exception as exc:  # noqa: BLE001 — try the next source
                logger.debug("[Metals] {} via {}: {}", code, fetch.__name__, exc)
                continue
            if quote:
                _cache[code] = (time.monotonic(), quote)
                return quote

    logger.warning("[Metals] no spot source could price {}", code)
    return None


async def fetch_spot_many(symbols) -> Dict[str, Dict[str, Any]]:
    """Spot for several metals at once, keyed by the symbol passed in."""
    wanted = [s for s in symbols if is_spot_metal(s)]
    if not wanted:
        return {}
    results = await asyncio.gather(
        *(fetch_spot(s) for s in wanted), return_exceptions=True
    )
    return {
        sym: res
        for sym, res in zip(wanted, results)
        if isinstance(res, dict)
    }
