"""Live Forex price fetcher (no API key required).

Uses the free, CDN-hosted currency API at:
  https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json

All rates are expressed relative to USD (1 USD = X units of the target currency).
To get EURUSD: 1 / data["usd"]["eur"]
To get XAUUSD: 1 / data["usd"]["xau"]
To get USDJPY:  data["usd"]["jpy"]
To get GBPJPY: (1 / data["usd"]["gbp"]) * data["usd"]["jpy"]

Results are cached for 60 seconds to avoid hammering the CDN.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


# ── Module-level in-memory cache (rates dict + timestamp) ────────────────────
_cache: dict[str, Any] = {}
_cache_ts: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)
_FETCH_LOCK = asyncio.Lock()

# ISO 4217 currency codes that should be treated as Forex by the price service.
FOREX_CURRENCIES: frozenset[str] = frozenset({
    "EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY",
    "XAU", "XAG", "XPT", "NOK", "SEK", "DKK", "SGD", "HKD",
    "MXN", "ZAR", "TRY", "PLN", "CZK", "HUF", "RUB", "INR",
    "CNY", "TWD", "KRW", "THB", "MYR", "IDR", "PHP", "VND",
})

# Known Forex pair symbols (6-char, no slash).
_FOREX_PAIRS: frozenset[str] = frozenset(
    f"{b}{q}"
    for b in FOREX_CURRENCIES
    for q in FOREX_CURRENCIES
    if b != q
)

# CDN URL — always returns the latest daily rates.
_API_URL = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
)
# Fallback URL in case the CDN is unavailable.
_FALLBACK_URL = "https://latest.currency-api.pages.dev/v1/currencies/usd.json"


async def _fetch_rates() -> dict[str, float]:
    """Fetch (and cache) the latest rates from the free currency API."""
    global _cache, _cache_ts

    async with _FETCH_LOCK:
        now = now_utc_naive()
        if _cache_ts is not None and (now - _cache_ts) < _CACHE_TTL and _cache:
            return _cache  # type: ignore[return-value]

        import aiohttp

        for url in (_API_URL, _FALLBACK_URL):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            rates: dict[str, float] = data.get("usd", {})
                            if rates:
                                _cache = rates
                                _cache_ts = now
                                logger.debug(
                                    "Forex rates refreshed ({} symbols)", len(rates)
                                )
                                return rates
            except Exception as exc:  # noqa: BLE001
                logger.warning("Forex price fetch failed from {}: {}", url, exc)

        # Return stale cache rather than nothing if both URLs fail.
        return _cache  # type: ignore[return-value]


def _parse_pair(symbol: str) -> tuple[str, str] | None:
    """Split 'EURUSD' → ('EUR', 'USD').  Returns None if not a Forex pair."""
    sym = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
    if len(sym) < 6:
        return None
    # Try 3+3 first (most Forex pairs), then 3+4 (e.g. USDXXX handled below)
    for base_len in (3, 4):
        base = sym[:base_len]
        quote = sym[base_len:]
        if base in FOREX_CURRENCIES and quote in FOREX_CURRENCIES:
            return base, quote
    return None


def is_forex_pair(symbol: str) -> bool:
    """Return True when the symbol looks like a Forex pair (e.g. EURUSD, XAUUSD)."""
    return _parse_pair(symbol) is not None


# ── Swissquote live feed (primary source) ───────────────────────────────────
# Public, no-key BBO feed used by the Swissquote trading platform. Returns real
# broker bid/ask per liquidity profile, so it is the true tradeable forex price
# (the currency-api CDN below is only a once-a-day fallback reference rate).
_SWISSQUOTE_URL = (
    "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{base}/{quote}"
)
_sq_cache: dict[str, tuple[datetime, float]] = {}
_SQ_TTL = timedelta(seconds=5)


async def _fetch_swissquote(base: str, quote: str) -> float | None:
    """Return the Swissquote mid price for base/quote, or None."""
    key = f"{base}{quote}"
    now = now_utc_naive()
    cached = _sq_cache.get(key)
    if cached and (now - cached[0]) < _SQ_TTL:
        return cached[1]

    url = _SWISSQUOTE_URL.format(base=base, quote=quote)
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Swissquote fetch {}/{} failed: {}", base, quote, exc)
        return None

    if not isinstance(data, list):
        return None

    # Prefer the tightest ("Prime"/"Elite") profile, else the first with a price.
    best_bid = best_ask = None
    priority = {"elite": 0, "prime": 1, "premium": 2, "standard": 3}
    best_rank = 99
    for platform in data:
        for sp in platform.get("spreadProfilePrices", []) or []:
            bid = sp.get("bid")
            ask = sp.get("ask")
            if not bid or not ask:
                continue
            rank = priority.get(str(sp.get("spreadProfile", "")).lower(), 5)
            if rank < best_rank:
                best_rank = rank
                best_bid, best_ask = float(bid), float(ask)

    if best_bid is None or best_ask is None:
        return None

    mid = round((best_bid + best_ask) / 2.0, 8)
    _sq_cache[key] = (now, mid)
    return mid


async def get_forex_price(symbol: str) -> float | None:
    """Return the current mid-price for *symbol* (e.g. 'EURUSD', 'XAUUSD', 'GBPJPY').

    Order of sources: Swissquote live BBO feed (real tradeable price) → the free
    currency-api reference rate. Returns None when neither can price it.
    """
    parsed = _parse_pair(symbol)
    if parsed is None:
        return None

    base, quote = parsed

    # 1) Swissquote — the actual live forex price (per the user's requirement).
    sq = await _fetch_swissquote(base, quote)
    if sq is not None and sq > 0:
        return sq

    # 2) Fallback: free daily reference rates.
    try:
        rates = await _fetch_rates()
        if not rates:
            return None

        # rates[x] = amount of x you get for 1 USD  →  1 USD = rates['eur'] EUR
        def to_usd(ccy: str) -> float | None:
            """Return how many USD 1 unit of *ccy* is worth."""
            r = rates.get(ccy.lower())
            if r is None or r == 0:
                return None
            return 1.0 / r  # 1/rates['eur'] = USD per 1 EUR

        base_in_usd = to_usd(base) if base != "USD" else 1.0
        quote_in_usd = to_usd(quote) if quote != "USD" else 1.0

        if base_in_usd is None or quote_in_usd is None or quote_in_usd == 0:
            return None

        # How many quote units you get per 1 base unit.
        price = base_in_usd / quote_in_usd
        return round(price, 8)

    except Exception as exc:  # noqa: BLE001
        logger.warning("get_forex_price({}) failed: {}", symbol, exc)
        return None
