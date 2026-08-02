"""
Universal live market data — one symbol resolver and one quote path for every
asset class the platform touches.

Why this exists
---------------
Live prices used to be assembled inline, twice: once in the Paul chat prompt
builder and once in the JARVIS command handler.  Both filtered on ``/USDT``,
so only crypto ever reached the model.  The prompt then told the model to never
quote a price that wasn't in that list, which made "what is gold doing?"
*structurally* unanswerable — the refusal was the prompt working as written,
not the model being unhelpful.  Meanwhile ``yahoo_provider`` already knew how
to price XAUUSD, every FX cross, the indices, oil and the softs; nothing was
wired to it.

The second failure came from the same blind spot in the other direction: the
command dispatcher appended ``USDT`` to any bare symbol, turning ``GBPUSD``
into ``GBPUSDUSDT``, which matched no forex map and no Bitget pair.

This module is the single place that answers three questions — *what instrument
is this string*, *what is it worth right now*, and *where did that number come
from* — so neither mistake can be made in one caller and not the other.

Priority for a quote is broker-first for anything a broker actually quotes:
an MT5 account's own bid/ask is what the user's fills happen at, so when that
account is live it wins over a broker-independent reference price.  Yahoo is
the universal fallback, then the crypto exchanges, then CoinGecko/Frankfurter.

Metals are the exception, and the reason is worth stating: Yahoo has no spot
metal ticker at all, so it prices gold off the COMEX future.  Measured on one
tick, ``GC=F`` read 4105.10 against spot gold at 4047.31 — 1.4% apart, because
a future carries the cost of carry to expiry.  For XAU/XAG/XPT/XPD a genuine
spot feed (``metals_provider``) therefore runs ahead of Yahoo, and the candle
series is anchored onto the spot level so a trade proposal's entry, stop and
target land where the user's broker would actually fill them.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx
from loguru import logger

from app.exchanges import yahoo_provider
from app.exchanges.yahoo_provider import (
    YAHOO_TICKERS,
    _CRYPTO_BASES,
    _CURRENCIES,
)

# ── Asset classification ─────────────────────────────────────────────────────
# Classified off the *Yahoo ticker*, not a hand-kept symbol list, so a new entry
# in YAHOO_TICKERS is categorised automatically instead of silently landing in
# "unknown" months later.  test_classify_covers_every_yahoo_ticker_key locks it.
_METAL_TICKERS = {"GC=F", "SI=F", "PL=F", "PA=F"}
_ENERGY_TICKERS = {"CL=F", "BZ=F", "NG=F"}
_SOFT_TICKERS = {"CC=F", "KC=F", "SB=F", "CT=F", "ZW=F"}

CRYPTO = "crypto"
FX = "fx"
METAL = "metal"
INDEX = "index"
ENERGY = "energy"
SOFT = "soft"
UNKNOWN = "unknown"

#: Quote suffixes that can be glued onto a base.  Order matters — longest first,
#: so ``USDT`` is stripped before ``USD`` would match its tail.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD")

#: Plain-English names users type instead of the instrument code.
_ALIASES: Dict[str, str] = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "BULLION": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD",
    "PLATINUM": "XPTUSD", "PALLADIUM": "XPDUSD",
    "OIL": "USOIL", "WTI": "USOIL", "CRUDE": "USOIL",
    "BRENT": "UKOIL",
    "GAS": "NGAS", "NATGAS": "NGAS", "NATURALGAS": "NGAS",
    "NASDAQ": "NAS100", "NDX": "NAS100", "USTEC": "NAS100",
    "DOW": "US30", "DJIA": "US30", "DJ30": "US30",
    "SPX": "US500", "SP500": "US500", "SPX500": "US500",
    "DAX": "GER40", "FTSE": "UK100", "NIKKEI": "JPN225",
    "BITCOIN": "BTCUSDT", "ETHEREUM": "ETHUSDT", "SOLANA": "SOLUSDT",
    "RIPPLE": "XRPUSDT", "DOGECOIN": "DOGEUSDT", "CARDANO": "ADAUSDT",
}

#: Uppercased English that is shaped like a ticker but never is one.  Without
#: this, "CAN YOU CHECK MY PNL" yields three "symbols" and three wasted fetches.
_STOPWORDS = frozenset({
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "ANY", "CAN",
    "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS",
    "HOW", "ITS", "NEW", "NOW", "OLD", "SEE", "TWO", "WHO", "BOY", "DID",
    "MAN", "MEN", "PUT", "SAY", "SHE", "TOO", "USE", "WHY", "WITH", "THIS",
    "THAT", "THEY", "HAVE", "FROM", "WILL", "WHAT", "WHEN", "MAKE", "LIKE",
    "TIME", "JUST", "KNOW", "TAKE", "INTO", "YOUR", "GOOD", "SOME", "THEM",
    "THAN", "THEN", "LOOK", "ONLY", "OVER", "ALSO", "BACK", "WELL", "MUCH",
    "WANT", "GIVE", "MOST", "TELL", "DOES", "SHOW", "NEED", "HERE", "MINE",
    "BUY", "SELL", "LONG", "SHORT", "HOLD", "OPEN", "CLOSE", "STOP", "LOSS",
    "PROFIT", "ENTRY", "EXIT", "TRADE", "TRADES", "PRICE", "PRICES", "CHART",
    "NEWS", "MARKET", "MARKETS", "ANALYSIS", "ANALYSE", "ANALYZE", "SIGNAL",
    "PNL", "ROI", "USD", "EUR", "GBP", "JPY", "SIR", "PLEASE", "THANKS",
    "TP", "SL", "RR", "AI", "LLM", "API", "MT5", "SMC", "OK", "YES", "NO",
})

# Quotes are cached briefly so a chat turn that mentions the same instrument
# twice, or two near-simultaneous turns, cost one upstream call.  Yahoo keeps
# its own 30s cache underneath, so a Yahoo-sourced price can be ~40s old in the
# worst case — Quote.age_s carries that so the prompt can state it honestly
# rather than implying tick freshness.
_TTL = 10.0
_cache: Dict[str, Tuple[float, "Quote"]] = {}

# MT5 symbol lists change rarely; re-asking per quote would cost a round trip
# for every instrument the broker doesn't list.
_MT5_SYMBOLS_TTL = 600.0
_mt5_symbols: Dict[int, Tuple[float, set]] = {}

#: At most this many upstream fetches run concurrently.
_CONCURRENCY = 6

#: Hard wall-clock ceiling for a batch of quotes.  A chat turn must not stall
#: behind a hung provider — partial prices beat a spinning cursor.
_BATCH_BUDGET_S = 4.0

#: ccxt/interval spelling → the MT5-style timeframe ``yahoo_provider`` speaks.
_TF_TO_MT5: Dict[str, str] = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "H1", "4h": "H4", "1d": "D1", "1w": "W1",
}


@dataclass(frozen=True)
class Quote:
    """A live price with its provenance attached.

    ``source`` and ``age_s`` are not decoration: they are rendered into the
    model's prompt so it can say *where* a number came from and *how old* it is
    instead of presenting every figure with identical false confidence.
    """

    symbol: str
    price: float
    source: str          # "mt5:<server>" | "yahoo:GC=F" | "bitget" | "coingecko:pax-gold"
    ts: int
    asset_class: str
    change_pct: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    age_s: int = 0


# ── Symbol normalisation ─────────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """Bare uppercase instrument name, with a doubled quote suffix undone.

    ``yahoo_provider.normalize`` strips broker decorations (``XAUUSD.m``,
    ``US30.cash``).  On top of that this repairs ``GBPUSDUSDT`` → ``GBPUSD``:
    the old dispatcher appended ``USDT`` unconditionally, and those strings are
    still in the database and in users' habits.  Repairing them here means the
    bad input heals itself wherever it turns up rather than needing a migration.

    Only stripped when what remains is a *non-crypto* instrument in its own
    right — ``BTCUSDT`` keeps its quote, because ``BTC`` alone is not a pair.
    """
    s = yahoo_provider.normalize(symbol)
    if not s:
        return ""
    for suffix in _QUOTE_SUFFIXES:
        if not s.endswith(suffix) or len(s) <= len(suffix):
            continue
        stem = s[: -len(suffix)]
        # Only a stem that is itself a complete non-crypto instrument means the
        # quote was spurious.  resolve_ticker's "-USD" spellings are crypto.
        if stem in YAHOO_TICKERS:
            return stem
        ticker = yahoo_provider.resolve_ticker(stem)
        if ticker and not ticker.endswith("-USD"):
            return stem
        break
    return s


def classify(symbol: str) -> str:
    """Asset class for *symbol* — crypto | fx | metal | index | energy | soft."""
    s = normalize_symbol(symbol)
    if not s:
        return UNKNOWN

    ticker = YAHOO_TICKERS.get(s)
    if ticker:
        if ticker in _METAL_TICKERS:
            return METAL
        if ticker in _ENERGY_TICKERS:
            return ENERGY
        if ticker in _SOFT_TICKERS:
            return SOFT
        return INDEX

    # Crypto quoted against a stable/fiat leg.
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and s[: -len(suffix)] in _CRYPTO_BASES:
            return CRYPTO
    if s in _CRYPTO_BASES:
        return CRYPTO

    if len(s) == 6 and s[:3] in _CURRENCIES and s[3:] in _CURRENCIES:
        return FX
    # Metal crosses (XAUEUR) — synthesised from two USD legs by yahoo_provider.
    if len(s) == 6 and s[:3] in {"XAU", "XAG", "XPT", "XPD"}:
        return METAL

    # Anything left carrying a stable quote is an altcoin we simply don't have
    # a hardcoded base for; the catalog decides at fetch time.
    if s.endswith(("USDT", "USDC")):
        return CRYPTO
    return UNKNOWN


def is_universal_symbol(symbol: str) -> bool:
    """True for a non-crypto instrument this platform can price and chart.

    Two-tier, mirroring ``KronosForecastPlugin.forecast_service._is_forex``:
    ``forex_provider`` knows a handful of majors plus gold, and Yahoo covers
    every remaining FX cross, index and commodity — including the crosses it
    synthesises from two USD legs (XAUEUR).  Crypto is excluded on purpose: it
    belongs on the exchange connectors, which carry deeper history and real
    traded volume.
    """
    s = normalize_symbol(symbol)
    if not s:
        return False
    try:
        from app.exchanges.forex_provider import is_forex_symbol

        if is_forex_symbol(s):
            return True
    except Exception:  # noqa: BLE001 — provider optional, fall through to Yahoo
        pass
    try:
        return yahoo_provider.supports_non_crypto(s)
    except Exception:  # noqa: BLE001
        return False


def canonicalize_for_analysis(token: str) -> Tuple[str, str]:
    """``(symbol, asset_class)`` for a raw token pulled out of a command.

    This replaces the dispatcher's old ``+= "USDT"``.  Crypto deliberately keeps
    its *bare* base — ``pair_catalog._candidate_symbols`` already expands a bare
    ticker into every canonical form, so gluing a quote on first only creates
    strings the catalog then has to unpick.
    """
    raw = (token or "").strip().upper()
    if not raw:
        return "", UNKNOWN

    aliased = _ALIASES.get(raw, raw)
    s = normalize_symbol(aliased)
    if not s:
        return "", UNKNOWN

    cls = classify(s)
    if cls == CRYPTO:
        for suffix in _QUOTE_SUFFIXES:
            if s.endswith(suffix) and len(s) > len(suffix):
                return s[: -len(suffix)], CRYPTO
        return s, CRYPTO
    return s, cls


#: The optional ``_x`` tail is the broker marker in ``EURUSD_i`` / ``EURUSD_pro``.
#: ``_`` is a word character, so ``\b`` never fired between the pair and the
#: marker and the whole token was skipped — the one spelling in the MT5 matrix
#: that named no instrument at all. ``normalize_symbol`` strips the tail.
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{1,11}(?:_[A-Za-z0-9]{1,5})?\b")


def extract_symbols(text: str, *, limit: int = 8) -> List[str]:
    """Instruments named in *text*, in order of first appearance.

    Deliberately conservative: a token has to be a known alias, a known Yahoo
    instrument, a currency pair, or carry a crypto quote.  Anything else is
    dropped, because a false positive costs a network fetch and a misleading
    line in the model's prompt, whereas a false negative just means the model
    uses a tool or asks.
    """
    if not text:
        return []

    out: List[str] = []
    seen: set = set()
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0).upper()
        if raw in _STOPWORDS or len(raw) < 2:
            continue

        candidate = _ALIASES.get(raw)
        if candidate is None:
            s = normalize_symbol(raw)
            if not s:
                continue
            cls = classify(s)
            if cls == UNKNOWN:
                continue
            # A bare 3-letter currency code ("USD", "EUR") is a leg, not a pair,
            # and a bare crypto base is only a symbol once quoted.
            if cls == CRYPTO and s in _CRYPTO_BASES:
                candidate = f"{s}USDT"
            elif cls == CRYPTO and not s.endswith(("USDT", "USDC", "USD")):
                continue
            else:
                candidate = s
        else:
            candidate = normalize_symbol(candidate)

        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


# ── Quote sources ────────────────────────────────────────────────────────────

async def _mt5_quote(symbol: str, db: Any) -> Optional[Quote]:
    """Broker bid/ask from a live MT5 account, or None.

    Preferred for non-crypto because it is the price the user's orders actually
    fill at; a reference feed can sit a few pips away from their broker.
    """
    if db is None:
        return None
    try:
        from sqlalchemy import select

        from plugins.MT5TradingPlugin.backend.models import MT5Account
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
    except Exception:  # noqa: BLE001 — plugin not installed
        return None

    try:
        accounts = (await db.execute(select(MT5Account))).scalars().all()
    except Exception:  # noqa: BLE001
        return None

    for acct in accounts[:3]:
        if not getattr(acct, "api_reachable", False):
            continue
        try:
            known = _mt5_symbols.get(acct.id)
            if known is None or (time.monotonic() - known[0]) > _MT5_SYMBOLS_TTL:
                symbols = await asyncio.wait_for(
                    mt5_client.get_symbols(
                        acct.login, acct.server, acct.password_encrypted
                    ),
                    timeout=3.0,
                )
                known = (time.monotonic(), {yahoo_provider.normalize(s) for s in symbols})
                _mt5_symbols[acct.id] = known
            if symbol not in known[1]:
                continue

            data = await asyncio.wait_for(
                mt5_client.get_symbol_price(
                    acct.login, acct.server, acct.password_encrypted, symbol
                ),
                timeout=2.5,
            )
        except Exception:  # noqa: BLE001 — a dead account is just "no quote"
            continue

        bid, ask = float(data.get("bid") or 0), float(data.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        return Quote(
            symbol=symbol,
            price=(bid + ask) / 2,
            source=f"mt5:{acct.server}",
            ts=int(time.time()),
            asset_class=classify(symbol),
            bid=bid,
            ask=ask,
            age_s=0,
        )
    return None


async def _metals_spot_quote(symbol: str) -> Optional[Quote]:
    """Genuine spot for gold, silver, platinum and palladium.

    Yahoo prices metals off the COMEX future, which carries cost of carry to
    expiry: measured on the same tick, ``GC=F`` read 4105.10 against spot gold
    at 4047.31. Spot is what the user's broker quotes and what "the gold price"
    means, so it goes ahead of Yahoo for these four instruments.
    """
    try:
        from app.exchanges import metals_provider

        spot = await metals_provider.fetch_spot(symbol)
    except Exception:  # noqa: BLE001
        return None
    if not spot:
        return None
    return Quote(
        symbol=symbol,
        price=float(spot["price"]),
        source=spot.get("source", "spot"),
        ts=int(spot.get("ts") or time.time()),
        asset_class=METAL,
        bid=spot.get("bid"),
        ask=spot.get("ask"),
        age_s=max(0, int(time.time()) - int(spot.get("ts") or time.time())),
    )


async def _yahoo_quote(symbol: str) -> Optional[Quote]:
    """Reference price from Yahoo — covers FX, metals, indices, energy, softs.

    For metals this returns the *future*, not spot; ``_metals_spot_quote`` runs
    first, and the source label says which one answered.
    """
    try:
        quote = await yahoo_provider.fetch_quote(symbol)
    except Exception:  # noqa: BLE001
        return None
    if not quote:
        return None
    ts = int(quote.get("time") or time.time())
    return Quote(
        symbol=symbol,
        price=float(quote["price"]),
        source=f"yahoo:{quote.get('ticker', '?')}",
        ts=ts,
        asset_class=classify(symbol),
        age_s=max(0, int(time.time()) - ts),
    )


async def _crypto_quote(symbol: str) -> Optional[Quote]:
    """Crypto last price: exchange connector, then Binance, then CoinGecko."""
    base = symbol
    for suffix in _QUOTE_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break

    try:
        from app.exchanges.manager import SupportedExchange, exchange_manager

        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector is not None:
            ticker = await asyncio.wait_for(
                connector.exchange.fetch_ticker(f"{base}/USDT:USDT"), timeout=3.0
            )
            last = ticker.get("last") or ticker.get("close")
            if last:
                return Quote(
                    symbol=f"{base}USDT",
                    price=float(last),
                    source="bitget",
                    ts=int(time.time()),
                    asset_class=CRYPTO,
                    change_pct=(
                        float(ticker["percentage"])
                        if ticker.get("percentage") is not None
                        else None
                    ),
                )
    except Exception:  # noqa: BLE001
        pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/24hr",
                params={"symbol": f"{base}USDT"},
            )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("lastPrice"):
                return Quote(
                    symbol=f"{base}USDT",
                    price=float(data["lastPrice"]),
                    source="binance",
                    ts=int(time.time()),
                    asset_class=CRYPTO,
                    change_pct=(
                        float(data["priceChangePercent"])
                        if data.get("priceChangePercent")
                        else None
                    ),
                )
    except Exception:  # noqa: BLE001
        pass
    return None


async def _forex_provider_quote(symbol: str) -> Optional[Quote]:
    """Last-resort non-crypto price (CoinGecko PAXG / Frankfurter daily FX)."""
    try:
        from app.exchanges.forex_provider import fetch_live_price, is_forex_symbol

        if not is_forex_symbol(symbol):
            return None
        price = await fetch_live_price(symbol)
    except Exception:  # noqa: BLE001
        return None
    if not price:
        return None
    return Quote(
        symbol=symbol,
        price=float(price),
        source="forex_provider",
        ts=int(time.time()),
        asset_class=classify(symbol),
    )


#: Price bases a caller may ask for. Only metals have a meaningful distinction —
#: everything else ignores it, because there is no second series to choose from.
SPOT = "spot"
FUTURES = "futures"


async def get_quote(
    symbol: str, *, db: Any = None, basis: str = SPOT
) -> Optional[Quote]:
    """Live price for any instrument, or None when no source can serve it.

    ``basis`` only changes metals. ``spot`` is what a broker quotes and what
    "the gold price" means; ``futures`` is the COMEX contract, which trades
    above spot by the cost of carry and is what a futures trader watches.
    Both are legitimate — they are simply different instruments, so the caller
    picks and the ``source`` on the result says which one answered.
    """
    s = normalize_symbol(symbol)
    if not s:
        return None

    cache_key = f"{s}:{basis}"
    hit = _cache.get(cache_key)
    if hit and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]

    cls = classify(s)
    if cls == CRYPTO:
        sources = (lambda: _crypto_quote(s),)
    elif cls == METAL and basis == FUTURES:
        # Explicitly asked for the contract — do not substitute spot for it.
        sources = (lambda: _yahoo_quote(s),)
    elif cls == METAL:
        # Broker first, then genuine spot. Yahoo's metal tickers are futures and
        # sit ~1.4% above spot, so they are the last resort here, not the first.
        sources = (
            lambda: _mt5_quote(s, db),
            lambda: _metals_spot_quote(s),
            lambda: _yahoo_quote(s),
            lambda: _forex_provider_quote(s),
        )
    else:
        # Broker first: its bid/ask is where this user's orders actually fill.
        sources = (
            lambda: _mt5_quote(s, db),
            lambda: _yahoo_quote(s),
            lambda: _forex_provider_quote(s),
        )

    # Built lazily so a source that is never reached is never even constructed.
    quote: Optional[Quote] = None
    for make_source in sources:
        try:
            quote = await make_source()
        except Exception:  # noqa: BLE001
            quote = None
        if quote is not None:
            break

    if quote is not None:
        _cache[cache_key] = (time.monotonic(), quote)
    return quote


async def live_price(symbol: str, *, db: Any = None) -> Optional[float]:
    """Just the number, from whichever source actually carries the instrument.

    The one call site for "what is this worth right now". Reaching for the
    crypto connector directly — which several callers used to do — sends gold,
    FX and indices to an exchange that has never listed them; it answers with an
    ERROR log line per attempt, so a single open XAU/USD position produced a
    steady drip of errors that looked like a fault in the exchange integration.

    Pass ``db`` wherever a session is available: it is what lets the non-crypto
    path reach the live MT5 account, whose bid/ask is where the user's orders
    actually fill, instead of falling back to a reference feed.
    """
    quote = await get_quote(symbol, db=db)
    return float(quote.price) if quote and quote.price else None


async def get_quotes(
    symbols: Sequence[str], *, db: Any = None
) -> Dict[str, Quote]:
    """Quotes for many symbols, bounded by concurrency and a wall-clock budget.

    Returns only what arrived in time.  A partial answer keeps the chat turn
    responsive; a complete one that arrives after the model has already timed
    out is worth nothing.
    """
    wanted = [s for s in dict.fromkeys(normalize_symbol(s) for s in symbols) if s]
    if not wanted:
        return {}

    sem = asyncio.Semaphore(_CONCURRENCY)
    out: Dict[str, Quote] = {}

    async def _one(sym: str) -> None:
        async with sem:
            quote = await get_quote(sym, db=db)
            if quote is not None:
                out[sym] = quote

    tasks = [asyncio.create_task(_one(s)) for s in wanted]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=_BATCH_BUDGET_S
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        logger.debug(
            "[MarketData] quote batch hit the {}s budget; returning {}/{}",
            _BATCH_BUDGET_S, len(out), len(wanted),
        )
    return out


# ── Prompt block ─────────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    return f"{price:,.8f}".rstrip("0").rstrip(".")


def _fmt_quote(quote: Quote) -> str:
    change = (
        f" ({quote.change_pct:+.2f}%/24h)" if quote.change_pct is not None else ""
    )
    age = f", {quote.age_s}s ago" if quote.age_s >= 5 else ""
    return f"  {quote.symbol}: {_fmt_price(quote.price)}{change}  [{quote.source}{age}]"


async def _top_crypto_lines(limit: int) -> List[str]:
    """Most-traded USDT pairs, for the ambient crypto context the chat relies on."""
    lines: List[str] = []
    try:
        from app.exchanges.manager import SupportedExchange, exchange_manager

        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector is not None:
            tickers = await asyncio.wait_for(
                connector.exchange.fetch_tickers(), timeout=6.0
            )
            rows = [(s, t) for s, t in tickers.items() if "/USDT" in s]
            rows.sort(key=lambda x: float(x[1].get("quoteVolume") or 0), reverse=True)
            for sym, ticker in rows[:limit]:
                last = ticker.get("last") or ticker.get("close")
                if not last:
                    continue
                change = ticker.get("percentage")
                change_s = f" ({float(change):+.2f}%/24h)" if change is not None else ""
                clean = sym.split(":")[0].replace("/", "")
                lines.append(f"  {clean}: {_fmt_price(float(last))}{change_s}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MarketData] bitget bulk tickers skipped: {}", exc)

    if lines:
        return lines

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/24hr")
        payload = resp.json()
        rows = [
            t for t in (payload if isinstance(payload, list) else [])
            if isinstance(t, dict) and str(t.get("symbol", "")).endswith("USDT")
        ]
        rows.sort(key=lambda x: float(x.get("quoteVolume") or 0), reverse=True)
        for item in rows[:limit]:
            price, change = item.get("lastPrice"), item.get("priceChangePercent")
            if not price:
                continue
            change_s = f" ({float(change):+.2f}%/24h)" if change else ""
            lines.append(f"  {item['symbol']}: {_fmt_price(float(price))}{change_s}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MarketData] binance fallback skipped: {}", exc)
    return lines


async def price_block(
    symbols: Optional[Iterable[str]] = None,
    *,
    db: Any = None,
    include_top_crypto: bool = False,
    max_lines: int = 60,
) -> str:
    """The live-price section injected into a model's system prompt.

    Named instruments come first and are never crowded out by the ambient
    crypto list — the thing the user actually asked about has to survive any
    truncation.
    """
    named = list(symbols or [])
    quotes = await get_quotes(named, db=db) if named else {}

    lines: List[str] = []
    for sym in dict.fromkeys(normalize_symbol(s) for s in named):
        quote = quotes.get(sym)
        if quote is not None:
            lines.append(_fmt_quote(quote))
    mentioned_count = len(lines)

    missing = [
        s for s in dict.fromkeys(normalize_symbol(x) for x in named)
        if s and s not in quotes
    ]

    if include_top_crypto and mentioned_count < max_lines:
        top = await _top_crypto_lines(max_lines - mentioned_count)
        existing = {ln.strip().split(":")[0] for ln in lines}
        lines.extend(ln for ln in top if ln.strip().split(":")[0] not in existing)

    lines = lines[:max_lines]

    if not lines:
        # No numbers is not the same as no capability — say which it is, or the
        # model concludes it has no market access at all and refuses forever.
        return (
            "## ⚠️ Live Market Prices — this fetch failed\n"
            "The price fetch failed on this request. This is a transient network "
            "problem, NOT a missing capability: you DO have live market data for "
            "every asset class — crypto, FX, metals, indices, energy, softs.\n"
            "Do not quote a specific price from memory — training data is months "
            "or years stale. Instead, retry the fetch (use the `price_lookup` "
            "tool if available), or tell the user the fetch just failed and offer "
            "to retry. Never tell them the platform lacks a live feed."
        )

    header = (
        f"## ⚡ LIVE Market Prices — {len(lines)} instruments, fetched just now\n"
        "Each line carries its source and age. These are current; anything you "
        "remember from training is months or years stale, so prefer these.\n"
    )
    footer = (
        "\nIf the user asks about an instrument that is NOT listed above, FETCH IT — "
        "call the `price_lookup` tool when tools are available, otherwise emit the "
        "tool directive. You have live market data for every asset class: crypto, "
        "FX (majors and crosses), metals, indices, energy and softs.\n"
        "Never say the platform has no live feed for something, and never say you "
        "cannot query external market data — both are false. If a fetch fails, say "
        "it failed and offer to retry. Never invent a number."
    )
    if missing:
        footer += (
            f"\nFetch failed this turn for: {', '.join(missing)} — retry rather "
            "than guessing or declaring the instrument unsupported."
        )
    return header + "\n".join(lines) + footer


# ── OHLCV ────────────────────────────────────────────────────────────────────

#: Refuse to anchor when the futures/spot gap exceeds this. A real cost-of-carry
#: basis on a metal is well under 5%; anything larger means the wrong contract or
#: a bad tick, and shifting a series by a bogus amount is worse than not shifting.
_MAX_BASIS_PCT = 5.0


async def anchor_metal_series_to_spot(
    symbol: str, rows: List[List], ticker_name: str
) -> Tuple[List[List], str]:
    """Shift a metal futures series onto the spot price level.

    Yahoo serves metals as COMEX futures, which sit above spot by the cost of
    carry to expiry — 1.4% for gold when this was measured. That matters here
    because ``_analysis_from_series`` derives entry, stop and target from these
    candles: quoting spot at 4047 while proposing an entry of 4105 would be both
    incoherent and unfillable at the user's broker.

    So the whole OHLC series is shifted by the current spot-minus-futures basis.
    Volume and bar shape are untouched, so ATR, swing structure and the volume
    gate all still measure the real, liquid futures market — only the price
    *level* is restated in spot terms, anchored on the most recent bar.

    The honest caveat: basis is not constant historically (it decays toward zero
    at expiry), so bars far from the present are approximate. The bars that
    matter for level-finding are the recent ones, and those become correct.

    Returns the series unchanged, with an unmodified source label, whenever spot
    cannot be fetched or the gap fails the sanity check.
    """
    try:
        spot = await _metals_spot_quote(symbol)
    except Exception:  # noqa: BLE001
        spot = None
    if spot is None or not rows:
        return rows, f"yahoo:{ticker_name} (futures)"

    futures_last = float(rows[-1][4])
    if futures_last <= 0:
        return rows, f"yahoo:{ticker_name} (futures)"

    basis = spot.price - futures_last
    if abs(basis) / futures_last * 100 > _MAX_BASIS_PCT:
        logger.warning(
            "[MarketData] {} spot {} vs futures {} is {:.1f}% apart — leaving the "
            "series on futures rather than shifting it",
            symbol, spot.price, futures_last, abs(basis) / futures_last * 100,
        )
        return rows, f"yahoo:{ticker_name} (futures)"

    shifted = [
        [r[0], r[1] + basis, r[2] + basis, r[3] + basis, r[4] + basis, r[5]]
        for r in rows
    ]
    logger.info(
        "[MarketData] {} anchored to spot {} from {} (basis {:+.2f})",
        symbol, round(spot.price, 4), spot.source, basis,
    )
    return shifted, f"{spot.source} (anchored from {ticker_name})"


async def fetch_ohlcv_universal(
    symbol: str, timeframe: str = "4h", limit: int = 200, *, basis: str = SPOT
) -> Tuple[List[List], Dict]:
    """Candles + ticker for any non-crypto instrument.

    ``basis`` only affects metals: ``spot`` anchors the series onto the spot
    level (the default, and what a broker fills at), ``futures`` leaves the raw
    COMEX contract series alone for anyone who actually wants to trade it.

    Returns the exact ``(ohlcv, ticker)`` shape ``forex_provider.fetch_ohlcv``
    returns, so callers can swap providers without touching the analysis that
    consumes it.  Yahoo is tried first (it covers every FX cross, index and
    commodity, and puts real CME volume on spot FX); ``forex_provider`` is the
    fallback for the majors plus gold when Yahoo is rate-limited.

    Returns ``([], {})`` rather than raising — an unpriceable symbol is a
    routing outcome for the caller to handle, not an exception.
    """
    s = normalize_symbol(symbol)
    if not s:
        return [], {}

    mt5_tf = _TF_TO_MT5.get((timeframe or "").strip().lower())
    if mt5_tf:
        try:
            bars = await yahoo_provider.fetch_candles(s, mt5_tf, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MarketData] Yahoo candles {} {}: {}", s, timeframe, exc)
            bars = []
        if bars:
            rows: List[List] = []
            for bar in bars:
                try:
                    volume = bar.get("volume")
                    rows.append([
                        int(bar["time"]) * 1000,
                        float(bar["open"]), float(bar["high"]),
                        float(bar["low"]), float(bar["close"]),
                        float(volume) if volume is not None else 0.0,
                    ])
                except (KeyError, TypeError, ValueError):
                    continue
            if rows:
                from app.exchanges.forex_provider import _build_ticker

                ticker_name = yahoo_provider.resolve_ticker(s) or s
                source = f"yahoo:{ticker_name}"

                if classify(s) == METAL:
                    if basis == FUTURES:
                        source = f"yahoo:{ticker_name} (futures)"
                    else:
                        rows, source = await anchor_metal_series_to_spot(
                            s, rows, ticker_name
                        )

                live = await get_quote(s, basis=basis)
                last = live.price if live else rows[-1][4]
                ticker = _build_ticker(rows, last, source=source)
                logger.info(
                    "[MarketData] {} {} — {} bars from {}, live={}",
                    s, timeframe, len(rows), source, last,
                )
                return rows, ticker

    try:
        from app.exchanges.forex_provider import fetch_ohlcv as forex_fetch_ohlcv
        from app.exchanges.forex_provider import is_forex_symbol

        if is_forex_symbol(s):
            return await forex_fetch_ohlcv(s, timeframe=timeframe, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MarketData] forex_provider fallback {}: {}", s, exc)
    return [], {}
