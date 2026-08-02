"""
Yahoo Finance OHLCV provider — universal chart fallback for MT5-style symbols.

Why this exists
---------------
The MT5 charts fall back to a crypto exchange (Bitget/Binance/…) when the MT5
bridge has no history.  Crypto exchanges only carry crypto, so every FX pair,
index and commodity rendered an empty chart whenever the bridge was down —
XAUUSD was the sole exception because it maps to XAU/USDT.

Yahoo's public chart endpoint covers all of it from one source, with no API
key: FX (``EURUSD=X``), metals and energy futures (``GC=F``, ``CL=F``), indices
(``^NDX``) and crypto (``BTC-USD``).  Pairs Yahoo has no ticker for — the metal
crosses such as XAUEUR — are synthesised from their two USD legs.

This is a *chart* source only.  Prices are broker-independent, so orders still
go through MT5 at the broker's own quotes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
# Yahoo rejects requests without a browser-ish UA.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)"}

# ── Symbol resolution ────────────────────────────────────────────────────────
# Explicit tickers for instruments whose Yahoo name isn't derivable from the
# broker symbol.  FX pairs are handled generically below (EURUSD → EURUSD=X).
YAHOO_TICKERS: Dict[str, str] = {
    # Metals — spot metal has no Yahoo ticker; the front-month future tracks it.
    "XAUUSD": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Indices — the front-month FUTURE where Yahoo lists one, not the cash index.
    #
    # An MT5 index CFD is quoted off the future, so the future is what matches
    # the user's fills: cash ^DJI sat 1.25% below YM=F when this was measured.
    # The cash index also only prints during its own exchange session — ^DJI was
    # 16.6h stale mid-Europe-session — which froze both the candles and the live
    # line, while the future trades ~23h and stays current. Using the future also
    # means price and volume come from one series.
    "US30": "YM=F", "DJ30": "YM=F", "DOW": "YM=F",
    "US500": "ES=F", "SPX500": "ES=F", "SP500": "ES=F",
    "NAS100": "NQ=F", "USTEC": "NQ=F", "NDX": "NQ=F",
    "US2000": "RTY=F",
    "JPN225": "NKD=F", "NIKKEI": "NKD=F",
    # Indices Yahoo lists no future for stay on cash — stale outside their own
    # session and carrying no volume, which the forecast gate reports honestly
    # rather than papering over.
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DAX": "^GDAXI",
    "UK100": "^FTSE", "FTSE100": "^FTSE",
    "FRA40": "^FCHI", "CAC40": "^FCHI",
    "EU50": "^STOXX50E", "ESP35": "^IBEX", "SUI20": "^SSMI",
    "ITA40": "FTSEMIB.MI",
    "AUS200": "^AXJO", "HK50": "^HSI",
    "CHINA50": "000001.SS",
    # Macro gauges — read as context, never traded. Yahoo's chart endpoint 404s
    # on both DX=F and VX=F, so the "prefer the future" rule above cannot apply
    # here; DX-Y.NYB and ^VIX are the only series that serve. Both carry zero
    # volume, like ^GDAXI/^FTSE above — which is fine, because nothing routes
    # them through the volume gate: they are inputs to a signal, not the
    # instrument being traded.
    "DXY": "DX-Y.NYB", "USDX": "DX-Y.NYB", "DOLLARINDEX": "DX-Y.NYB",
    "VIX": "^VIX", "VOLATILITYINDEX": "^VIX",
    # Energy & softs
    #
    # The XTI/XBR/XNG spellings are what most MT5 brokers actually list energy
    # under — they look like FX pairs (three-letter base + USD) but no currency
    # matches, so without these aliases they fell through the FX branch below
    # and were treated as crypto.
    "USOIL": "CL=F", "WTI": "CL=F", "CRUDE": "CL=F",
    "XTIUSD": "CL=F", "WTIUSD": "CL=F", "OILUSD": "CL=F",
    "UKOIL": "BZ=F", "BRENT": "BZ=F", "XBRUSD": "BZ=F", "BRENTUSD": "BZ=F",
    "NGAS": "NG=F", "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COCOA": "CC=F", "COFFEE": "KC=F", "SUGAR": "SB=F",
    "COTTON": "CT=F", "WHEAT": "ZW=F",
    "CORN": "ZC=F", "SOYBEAN": "ZS=F", "SOYBEANS": "ZS=F",
    "SOYBEANOIL": "ZL=F", "OATS": "ZO=F",
}

# Crypto bases quoted in USD/USDT map to Yahoo's `-USD` pairs.
_CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX",
    "LTC", "LINK", "DOT", "TRX", "BCH", "XLM", "ATOM", "UNI",
}

# ISO codes Yahoo serves as `<PAIR>=X`.
_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "TRY", "ZAR",
    "MXN", "SGD", "HKD", "CNH", "CNY", "THB", "ILS", "RUB",
    "INR", "KRW", "BRL", "PHP", "IDR", "MYR",
}

# Metals priced per ounce — used to synthesise crosses like XAUEUR.
_METAL_BASES = {"XAU": "GC=F", "XAG": "SI=F", "XPT": "PL=F", "XPD": "PA=F"}

# CME currency futures — the volume proxy for spot FX.
#
# Spot FX is decentralised: there is no consolidated tape, so Yahoo (like every
# other spot feed) reports zero volume. The SMC engine gates setups on volume
# confirmation, which would reject every FX setup outright. CME futures are the
# standard published proxy for FX flow, so we take price from the spot pair and
# borrow contract volume from the matching future.
#
# MT5 supplies its own tick volume for FX, so this only applies to Yahoo-sourced
# candles — a live bridge always wins.
_FX_FUTURES: Dict[str, str] = {
    "EUR": "6E=F", "GBP": "6B=F", "JPY": "6J=F", "AUD": "6A=F",
    "CAD": "6C=F", "CHF": "6S=F", "NZD": "6N=F", "MXN": "6M=F",
    # Thinner CME contracts. They print far fewer hours than the majors, which
    # is itself the honest read — a pair whose future barely trades has a thin
    # tape, and the engine sees that as a dead/stale regime rather than a
    # confident setup. Currencies with no liquid listed future (TRY, SGD, HKD…)
    # are deliberately absent: no volume is better evidence than wrong volume.
    "ZAR": "6Z=F", "BRL": "6L=F", "SEK": "SEK=F", "NOK": "NOK=F",
    "CNH": "CNH=F", "CNY": "CNH=F",
}

# Indices need no equivalent map: unlike spot FX — which has no volume of its
# own and must borrow its future's — an index symbol resolves *straight* to its
# future in YAHOO_TICKERS above, so price and volume come from the same series.
# The ones Yahoo lists no future for keep pointing at cash and report no volume,
# which the forecast's volume gate treats as NO_TRADE.

# Stand-ins for tickers with holes in Yahoo's history on some intervals.
# USDCNH=X serves intraday fine but its daily series is entirely null except
# one bar; onshore USDCNY tracks it closely enough for a chart.
_TICKER_FALLBACKS = {"USDCNH=X": "USDCNY=X"}

# Broker decorations to strip: EURUSD.m, XAUUSD_i, US30.cash, GBPUSDpro …
_SUFFIXES = (".CASH", ".SPOT", ".PRO", ".ECN", ".RAW", ".MICRO", ".M", ".I", ".C", ".R", ".Z", ".A")


def normalize(symbol: str) -> str:
    """Broker symbol → bare uppercase name (EUR/USD.m → EURUSD)."""
    # Underscore is the other separator brokers decorate with (EURUSD_i,
    # XAUUSD_pro). Fold it onto "." so one strip handles both — deleting it the
    # way "-" is deleted would glue the marker on (EURUSD_I → EURUSDI) and leave
    # a name nothing resolves.
    s = (symbol or "").upper().strip().replace("/", "").replace(" ", "").replace("_", ".")
    if "." not in s:
        s = s.replace("-", "")
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    # Any remaining dot is a broker decoration this list hasn't seen — brokers
    # invent their own (.STP, .SB, .PRIME). The name is what precedes it, so cut
    # there rather than making the list chase every broker.
    if "." in s:
        head = s.split(".", 1)[0]
        if 3 <= len(head) <= 12:
            s = head
    s = s.replace("-", "")
    # Trailing lowercase-origin markers already uppercased (EURUSDM, EURUSDPRO).
    for suf in ("MICRO", "PRO", "ECN"):
        if len(s) > 6 and s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def resolve_ticker(symbol: str) -> Optional[str]:
    """Map an MT5-style symbol to a Yahoo ticker, or None if unsupported."""
    s = normalize(symbol)
    if not s:
        return None
    if s in YAHOO_TICKERS:
        return YAHOO_TICKERS[s]
    # Crypto: BTCUSD / BTCUSDT → BTC-USD
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and s[: -len(quote)] in _CRYPTO_BASES:
            return f"{s[: -len(quote)]}-USD"
    # FX: two ISO codes back to back
    if len(s) == 6 and s[:3] in _CURRENCIES and s[3:] in _CURRENCIES:
        return f"{s}=X"
    return None


def supports(symbol: str) -> bool:
    """True when ``fetch_candles`` can produce bars for *symbol*.

    Wider than ``resolve_ticker`` on purpose: a cross like XAUEUR has no Yahoo
    ticker of its own but is synthesised from its two USD legs by
    ``_cross_legs``.
    """
    return bool(resolve_ticker(symbol) or _cross_legs(symbol))


def supports_non_crypto(symbol: str) -> bool:
    """True for a *non-crypto* instrument Yahoo can serve — the routing question.

    This is what the forecast and analysis gates actually want to ask. Asking
    ``resolve_ticker`` alone answered "unsupported" for synthesised crosses and
    sent XAUEUR/XAGEUR down the crypto path, where no connector lists them, so
    they died on a Bitget lookup with the data one division away.

    Crypto is excluded on both branches — Yahoo's ``-USD`` names are its crypto
    tickers, and a crypto cross (BTCEUR) synthesises from a ``-USD`` leg. Both
    belong on the exchange connectors, which carry deeper history and real
    traded volume.
    """
    ticker = resolve_ticker(symbol)
    if ticker:
        return not ticker.endswith("-USD")
    legs = _cross_legs(symbol)
    return bool(legs) and not legs[0].endswith("-USD")


def _fx_volume_tickers(symbol: str) -> List[str]:
    """
    Futures whose volume stands in for this spot FX pair's flow.

    A pair with a USD leg maps to that currency's single contract. A cross has
    no contract of its own, so both legs are used and the *lower* of the two
    volumes is taken — a cross can only be as active as its least active leg.

    Returns [] unless every non-USD leg has a listed future. Half a cross (say
    the EUR side of EURPLN) measures one currency's activity, not the pair's,
    and passing that off as volume confirmation would be inventing evidence.
    """
    s = normalize(symbol)
    if len(s) != 6:
        return []
    base, quote = s[:3], s[3:]
    if base not in _CURRENCIES or quote not in _CURRENCIES:
        return []
    needed = [c for c in (base, quote) if c != "USD"]
    legs = [_FX_FUTURES.get(c) for c in needed]
    return [t for t in legs if t] if all(legs) else []


def _cross_legs(symbol: str) -> Optional[Tuple[str, str]]:
    """
    For a pair Yahoo has no ticker for, return the two USD legs it can be
    synthesised from: XAUEUR → (GC=F, EURUSD=X), i.e. base/USD ÷ quote/USD.
    """
    s = normalize(symbol)
    if len(s) != 6:
        return None
    base, quote = s[:3], s[3:]
    if quote == "USD" or quote not in _CURRENCIES:
        return None
    base_ticker = _METAL_BASES.get(base) or (f"{base}-USD" if base in _CRYPTO_BASES else None)
    if not base_ticker:
        return None
    return base_ticker, f"{quote}USD=X"


# ── Timeframes ───────────────────────────────────────────────────────────────
# MT5 timeframe → (Yahoo interval, history range).
# Yahoo has no 4h bar, so H4 is folded from 60m by _bucket().  Ranges respect
# Yahoo's intraday lookback limits (1m ≈ 7d, other intraday ≈ 60d, 60m ≈ 730d).
_TF_MAP: Dict[str, Tuple[str, str]] = {
    "M1":  ("1m",  "7d"),
    "M5":  ("5m",  "60d"),
    "M15": ("15m", "60d"),
    "M30": ("30m", "60d"),
    "H1":  ("60m", "730d"),
    # H2/H4/H6/H12 all fold out of the same 60m series by _bucket(). Without
    # them a "/forecast EURUSD 2h" found no Yahoo bar, fell back to the daily
    # Frankfurter series and then failed the volume gate — the user asked for
    # 2h and got NO_TRADE rather than a forecast.
    "H2":  ("60m", "730d"),
    "H4":  ("60m", "730d"),
    "H6":  ("60m", "730d"),
    "H12": ("60m", "730d"),
    "D1":  ("1d",  "10y"),
    "W1":  ("1wk", "10y"),
}
_TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H2": 7200, "H4": 14400, "H6": 21600, "H12": 43200,
    "D1": 86400, "W1": 604800,
}

#: Yahoo intervals that sit on a clean UTC grid, so _bucket() may fold them.
#: Daily and weekly bars are stamped at each exchange's session open instead —
#: bucketing those would merge sessions.
_INTRADAY_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "60m"})

# ── Response cache (Yahoo rate-limits aggressively) ──────────────────────────
_cache: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 30.0  # seconds — one poll cycle of the chart


def _cache_get(key: str) -> Optional[List[Dict]]:
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit["ts"]) < _CACHE_TTL:
        return hit["data"]
    return None


async def _fetch_series(
    client: httpx.AsyncClient, ticker: str, interval: str, rng: str
) -> List[Dict[str, Any]]:
    """Raw Yahoo bars for one ticker as [{time, open, high, low, close, volume}]."""
    # Yahoo throttles bursts with 429/422; one short backoff clears it, and
    # without the retry a transient blip blanks the chart for that pair.
    for attempt in range(2):
        r = await client.get(
            f"{YF_BASE}/{ticker}",
            params={"interval": interval, "range": rng, "includePrePost": "false"},
            headers=_HEADERS,
        )
        if r.status_code in (422, 429, 502, 503) and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        break
    r.raise_for_status()
    result = (r.json().get("chart") or {}).get("result") or []
    if not result:
        return []
    node = result[0]
    stamps = node.get("timestamp") or []
    quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []

    bars: List[Dict[str, Any]] = []
    for i, ts in enumerate(stamps):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        # Yahoo pads gaps (market closed, missing print) with nulls.
        if None in (o, h, l, c):
            continue
        v = vols[i] if i < len(vols) else None
        bars.append({
            "time": int(ts), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c),
            "volume": float(v) if v is not None else None,
        })
    return bars


def _bucket(bars: List[Dict[str, Any]], bucket_seconds: int) -> List[Dict[str, Any]]:
    """
    Fold bars onto absolute epoch boundaries.

    Does double duty: builds H4 candles out of 60m bars, and snaps every other
    intraday timeframe onto clean period starts. The latter matters because
    Yahoo stamps the still-forming candle with the live tick time, which
    otherwise lands a stray off-grid bar at the right edge of the chart.
    """
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_bucket: Optional[int] = None
    for b in bars:
        bucket = b["time"] - (b["time"] % bucket_seconds)
        if bucket != current_bucket:
            if current:
                out.append(current)
            current_bucket = bucket
            current = {**b, "time": bucket}
        else:
            assert current is not None
            current["high"] = max(current["high"], b["high"])
            current["low"] = min(current["low"], b["low"])
            current["close"] = b["close"]
            if b["volume"] is not None:
                current["volume"] = (current["volume"] or 0) + b["volume"]
    if current:
        out.append(current)
    return out


def _attach_fx_volume(
    bars: List[Dict[str, Any]], legs: List[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """
    Fill in spot-FX volume from the matching currency futures, matched by time.

    With two legs (a cross) the lower volume wins — the pair is only as liquid
    as its least active side. Bars with no matching future bar keep volume None
    rather than a zero, so the engine can tell "no data" from "no trading".
    """
    if not legs:
        return bars
    maps = [{b["time"]: b["volume"] for b in leg if b.get("volume")} for leg in legs]
    for b in bars:
        vals = [m.get(b["time"]) for m in maps]
        present = [v for v in vals if v]
        # A cross needs both legs; a single-leg pair needs its one.
        if len(present) == len(maps):
            b["volume"] = min(present)
    return bars


def _synthesize(base: List[Dict[str, Any]], quote: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cross rate = base/USD ÷ quote/USD, inner-joined on timestamp."""
    q_by_time = {b["time"]: b for b in quote}
    out: List[Dict[str, Any]] = []
    for b in base:
        q = q_by_time.get(b["time"])
        if not q or not q["close"] or not q["open"] or not q["high"] or not q["low"]:
            continue
        out.append({
            "time": b["time"],
            "open": b["open"] / q["open"],
            # Dividing by the quote leg inverts the extremes: the cross is
            # highest when the base is high *and* the quote leg is low.
            "high": b["high"] / q["low"],
            "low": b["low"] / q["high"],
            "close": b["close"] / q["close"],
            "volume": b["volume"],
        })
    return out


async def fetch_candles(symbol: str, timeframe: str = "H1", limit: int = 400) -> List[Dict[str, Any]]:
    """
    OHLCV for *symbol* on *timeframe*, newest last.  Returns [] when the symbol
    is unsupported or Yahoo has no data — callers treat that as "no source".
    """
    tf = (timeframe or "H1").upper()
    if tf not in _TF_MAP:
        tf = "H1"
    interval, rng = _TF_MAP[tf]

    cache_key = f"{normalize(symbol)}|{tf}|{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    ticker = resolve_ticker(symbol)
    legs = None if ticker else _cross_legs(symbol)
    if not ticker and not legs:
        return []

    bucket_seconds = _TF_SECONDS[tf]
    # Fold on the *source* interval, not the target size: H6/H12 come out of the
    # same 60m grid H4 does, so a size-based cut-off would leave them unbucketed.
    may_bucket = interval in _INTRADAY_INTERVALS
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            if ticker:
                bars = await _fetch_series(client, ticker, interval, rng)
                alt = _TICKER_FALLBACKS.get(ticker)
                if len(bars) < 40 and alt:
                    bars = await _fetch_series(client, alt, interval, rng)
                if may_bucket:
                    bars = _bucket(bars, bucket_seconds)
                # Spot FX prints no volume — borrow it from the currency futures.
                vol_tickers = _fx_volume_tickers(symbol) if ticker.endswith("=X") else []
                if vol_tickers and not any(b.get("volume") for b in bars):
                    legs = []
                    for vt in vol_tickers:
                        leg = await _fetch_series(client, vt, interval, rng)
                        legs.append(_bucket(leg, bucket_seconds) if may_bucket else leg)
                    bars = _attach_fx_volume(bars, legs)
            else:
                assert legs is not None
                base_bars = await _fetch_series(client, legs[0], interval, rng)
                quote_bars = await _fetch_series(client, legs[1], interval, rng)
                # Both legs must sit on the same grid before the join: a metal
                # future and an FX pair open at different times, so their raw
                # daily stamps never match and the cross came out empty.
                bars = _synthesize(
                    _bucket(base_bars, bucket_seconds),
                    _bucket(quote_bars, bucket_seconds),
                )
    except Exception as e:  # noqa: BLE001 — any provider failure is just "no data"
        logger.warning(f"[Yahoo] {symbol} ({ticker or legs}) {tf} failed: {e}")
        return []

    bars = bars[-limit:] if limit and limit > 0 else bars
    _cache[cache_key] = {"data": bars, "ts": time.monotonic()}
    return bars


async def fetch_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Live quote for *symbol* — ``{price, ticker, time}``, or None.

    Reads ``meta.regularMarketPrice`` off the chart endpoint, which is the same
    tick Yahoo shows on its own quote page. Needed because the chart's live line
    and forming bar poll a *quote*, not candles: once Yahoo is the primary
    candle source, a chart drawn from it must have a matching live price rather
    than falling back to a crypto exchange's ticker for a symbol that exchange
    does not list.

    Crosses Yahoo has no ticker for (XAUEUR and friends) return None — their
    candles are synthesised from two legs and no single quote represents them.
    """
    ticker = resolve_ticker(symbol)
    if not ticker:
        return None
    cache_key = f"quote:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[0] if cached else None

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(
                f"{YF_BASE}/{ticker}",
                params={"interval": "1m", "range": "1d"},
                headers=_HEADERS,
            )
            r.raise_for_status()
            result = (r.json().get("chart") or {}).get("result") or []
    except Exception as e:  # noqa: BLE001 — a failed quote is just "no price"
        logger.warning(f"[Yahoo] quote {ticker} for {symbol} failed: {e}")
        return None

    meta = (result[0].get("meta") or {}) if result else {}
    try:
        price = float(meta.get("regularMarketPrice"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    quote = {"price": price, "ticker": ticker,
             "time": int(meta.get("regularMarketTime") or time.time())}
    # Same 30s TTL as the candle cache; boxed in a list so a miss and a
    # cached-None are distinguishable.
    _cache[cache_key] = {"data": [quote], "ts": time.monotonic()}
    return quote


