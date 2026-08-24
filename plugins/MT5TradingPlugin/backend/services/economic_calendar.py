"""
MT5 Trading Plugin — economic calendar window (ForexFactory).

One cached fetch, three consumers:

  * ``/research/calendar``            — the Research & Calendar page
  * ``research_loop.remind_agents_of_calendar`` — the always-injected agent block
  * ``smc_ai.fetch_economic_events``  — the SMC prompt and the scalp gate

Two key-less sources, split by date so they can never duplicate each other:

  * **ForexFactory** (``ff_calendar_thisweek.json``) owns the current week. It
    is what the SMC prompt has always read, and its titles and impact grades
    are the ones the calendar page is built around. It publishes one week and
    only one week — ``ff_calendar_nextweek.json`` is a 404.
  * **TradingView's** public economic-calendar endpoint covers everything
    *after* ForexFactory's last date, out to ~30 days, with forecasts.

ForexFactory's JSON feed carries **no ``actual``** — only title, country, date,
impact, forecast and previous. The website prints released values; the feed
never has. So the same TradingView pull is also walked *backwards* over
ForexFactory's own week to backfill ``actual`` onto rows that have already
printed (see ``_backfill_actuals``). Matching is deliberately strict — a wrong
``actual`` on an NFP row is worse than a blank one.

Every impact level is kept in the cache; callers narrow at the query layer via
``query_calendar``. ``is_fomo`` marks high-impact events — rate decisions, CPI,
NFP, FOMC, GDP — which is what the calendar page highlights and what the agent
reminder is built from.

Nothing here raises. A dead feed yields an empty window and every caller
degrades to "no known events", which is the same state as a quiet week.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

#: Currencies that matter per instrument. Used by the symbol-scoped views
#: (the SMC prompt, the per-symbol agent reminders) — the calendar page itself
#: is deliberately unfiltered.
ECO_CURRENCY_MAP: Dict[str, List[str]] = {
    "XAUUSD": ["USD", "XAU"],
    "XAGUSD": ["USD", "XAG"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "USDCHF": ["USD", "CHF"],
    "AUDUSD": ["AUD", "USD"],
    "NZDUSD": ["NZD", "USD"],
    "USDCAD": ["USD", "CAD"],
    "US30":   ["USD"],
    "NAS100": ["USD"],
    "SP500":  ["USD"],
    "USOIL":  ["USD"],
    "UKOIL":  ["USD"],
    # Crypto has no central bank, but it trades the dollar's macro tape.
    "BTCUSD": ["USD"],
    "ETHUSD": ["USD"],
}

#: Title keywords the SMC path has always used to narrow "high impact" further.
#: Kept for that caller only — the calendar page treats impact as the signal.
#: Both vocabularies, because either source can end up owning a date. When
#: ForexFactory is unreachable TradingView supplies the whole window, and it
#: writes "Non Farm Payrolls" where ForexFactory writes "Non-Farm Employment
#: Change" — on the old list the month's biggest release simply vanished from
#: the agent prompt and the scalp gate.
HIGH_IMPACT_KEYWORDS = (
    "interest rate", "cpi", "inflation", "gdp", "nfp", "non-farm",
    "unemployment", "fomc", "federal reserve", "ecb", "boe", "rba",
    "pmi", "retail sales", "trade balance", "pce", "core cpi",
    # TradingView's wording for the same releases.
    "non farm", "payrolls", "jobless claims", "inflation rate",
    "balance of trade", "employment change",
)

#: ForexFactory's weekly feed — the current week only.
_FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

#: TradingView's public calendar, used to extend past ForexFactory's last day.
_TV_FEED = "https://economic-calendar.tradingview.com/events"
_TV_COUNTRIES = "US,EU,GB,JP,AU,CA,CH,NZ,CN"
_TV_HORIZON_DAYS = 30

#: How far back the TradingView pull starts. ForexFactory publishes Sunday to
#: Friday, so eight days always covers its whole week no matter which day we
#: refresh on — that is the span we need released ``actual`` values for.
_TV_LOOKBACK_DAYS = 8

#: TradingView reports importance as an int; ForexFactory as a word.
_TV_IMPACT = {1: "high", 0: "medium", -1: "low"}

#: The research loop ticks every 15 min; match it so a page refresh in between
#: never costs a network call.
_CACHE_TTL_SECONDS = 900

_cache: List[Dict[str, Any]] = []
_cache_at: float = 0.0
_lock = asyncio.Lock()


def matches_high_impact_keyword(title: str) -> bool:
    low = (title or "").lower()
    return any(kw in low for kw in HIGH_IMPACT_KEYWORDS)


def _parse_utc(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _event(
    *, title: str, currency: str, impact: str, when: datetime, source: str,
    forecast: Any = None, previous: Any = None, actual: Any = None,
) -> Dict[str, Any]:
    return {
        # Stable across refetches so the UI can key rows without reordering.
        "id": f"{currency}:{when.strftime('%Y%m%dT%H%M')}:{title[:60]}",
        "title": title,
        "currency": currency,
        "impact": impact,
        "timestamp": when.isoformat(),
        "date": when.strftime("%Y-%m-%d"),
        "time_utc": when.strftime("%Y-%m-%d %H:%M"),
        "forecast": forecast,
        "previous": previous,
        "actual": actual,
        # Impact is the signal, not the wording. "Fed Press Conference" moves
        # price; "Spanish Unemployment Rate" does not, despite the keyword.
        "is_fomo": impact == "high",
        "source": source,
    }


# --------------------------------------------------------------------------
# Value formatting
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value: Any) -> Optional[float]:
    """The leading number in a feed value. ``"-7.2M"`` → ``-7.2``, ``4.2`` → ``4.2``.

    ForexFactory occasionally ships a range for bond auctions ("3.99|4.6");
    the first figure is the yield, which is the one that matters.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def _tv_suffix(raw: Dict[str, Any]) -> str:
    """ForexFactory-style unit for a TradingView row.

    TradingView carries ``unit`` ("%", "$", "C$") and ``scale`` ("K", "M",
    "B") separately, and its numbers are already scaled. ForexFactory writes
    percentages as "4.2%" and magnitudes as "197K" — it drops the currency
    symbol — so mirror that.
    """
    if raw.get("unit") == "%":
        return "%"
    return str(raw.get("scale") or "")


def _join_unit(value: Any, suffix: str) -> Any:
    number = _num(value)
    if number is None:
        return value
    return f"{_trim(number)}{suffix}"


def _trim(number: float, decimals: Optional[int] = None) -> str:
    """A number as a feed would print it: no trailing zeros, no ``.0``."""
    if decimals is not None:
        return f"{number:.{decimals}f}"
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _style_of(value: Any) -> Optional[Tuple[str, int]]:
    """The unit and decimal places a feed value is written in. ``"-7.2M"`` →
    ``("M", 1)``. ``None`` when there is no number to read a style from."""
    text = str(value or "").strip()
    match = _NUMBER.search(text)
    if not text or match is None:
        return None
    digits = match.group()
    suffix = text[match.end():].strip()
    return suffix, len(digits.split(".")[1]) if "." in digits else 0


def _format_like(number: float, samples: Sequence[Any]) -> str:
    """Print ``number`` the way this event's other values are printed.

    The unit and precision come from ForexFactory's own ``previous`` /
    ``forecast`` strings, so a backfilled ``actual`` lands as "4.3%" beside
    "forecast 4.2% previous 4.2%" rather than as a bare 4.3.
    """
    for sample in samples:
        style = _style_of(sample)
        if style is None:
            continue
        suffix, decimals = style
        # A whole-number style must not swallow a fractional print ("0.4" is
        # not "0"); otherwise the neighbouring values set the precision.
        if decimals == 0 and number != int(number) and abs(number) < 1:
            return f"{_trim(number)}{suffix}"
        return f"{_trim(number, decimals)}{suffix}"
    return _trim(number)


# --------------------------------------------------------------------------
# ForexFactory ↔ TradingView matching
# --------------------------------------------------------------------------

#: Same release, different house style. Applied to both sides before scoring.
_TITLE_ALIASES: Tuple[Tuple[str, str], ...] = (
    (r"non[- ]?farm employment change", "nonfarm payrolls"),
    (r"non[- ]?farm payrolls", "nonfarm payrolls"),
    (r"\bnfp\b", "nonfarm payrolls"),
    (r"adp employment change", "adp nonfarm payrolls"),
    (r"unemployment claims", "initial jobless claims"),
    (r"\bcpi\b", "inflation rate"),
    (r"crude oil inventories", "crude oil stocks change"),
    (r"natural gas storage", "natural gas stocks change"),
    (r"\btrade balance\b", "balance of trade"),
    (r"\bjolts\b", "jolts job openings"),
    (r"\blabor\b", "labour"),
    (r"\bs\.a\b", " "),
)

#: Who compiled the number is house style, not identity — "S&P Global
#: Manufacturing PMI Final" and "Final Manufacturing PMI" are one release.
_VENDORS = re.compile(
    r"\b(s&p global|markit|eia|api|boj|boe|ecb|rba|rbnz|snb|fed|fomc|ism|anz|nab|"
    r"westpac|gfk|zew|ifo|caixin|ratingdog|lloyds|halifax|nationwide|seco|mi|"
    r"challenger|ivey|adp|nfib|redbook|cb|conference board|procure\.ch)\b"
)

#: These *are* identity: mixing up m/m with y/y, or prelim with final, means
#: publishing the wrong number under the right name.
_PERIODS = {
    "mom": "mom", "m/m": "mom", "monthly": "mom",
    "yoy": "yoy", "y/y": "yoy", "annual": "yoy", "annualized": "yoy",
    "qoq": "qoq", "q/q": "qoq",
}
_REVISIONS = {
    "prel": "prelim", "prelim": "prelim", "preliminary": "prelim",
    "flash": "prelim", "advance": "prelim",
    "final": "final", "revised": "final", "second": "final",
}
_STOPWORDS = {"s", "a", "sa", "nsa", "the", "of", "index", "idx"}


def _has_word(text: str, word: str) -> bool:
    return re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(word), text) is not None


def _title_tags(title: str) -> Tuple[frozenset, frozenset, bool]:
    """The parts of a title that must agree exactly: period, revision, core."""
    low = f" {title.lower()} "
    periods = frozenset(v for k, v in _PERIODS.items() if _has_word(low, k))
    revisions = frozenset(v for k, v in _REVISIONS.items() if _has_word(low, k))
    core = "core" in low or "underlying" in low or "ex food" in low
    return periods, revisions, core


def _title_key(title: str) -> str:
    """A title reduced to its release identity, order-independent."""
    low = title.lower()
    for pattern, replacement in _TITLE_ALIASES:
        low = re.sub(pattern, replacement, low)
    low = _VENDORS.sub(" ", low)
    for word in (*_PERIODS, *_REVISIONS):
        low = re.sub(r"(?<![a-z])%s(?![a-z])" % re.escape(word), " ", low)
    low = re.sub(r"\bcore\b|\bunderlying\b", " ", low)
    low = re.sub(r"[^a-z0-9 ]", " ", low)
    return " ".join(sorted(w for w in low.split() if w not in _STOPWORDS))


def _previous_agrees(left: Any, right: Any) -> Optional[bool]:
    """Whether two feeds report the same prior print. ``None`` when unknowable.

    A tie-breaker, not a gate: revisions between publishers are normal, so
    disagreement only vetoes a *weak* title match.
    """
    a, b = _num(left), _num(right)
    if a is None or b is None:
        return None
    return abs(a - b) <= max(0.02, abs(a) * 0.02)


def _best_match(
    event: Dict[str, Any], candidates: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The TradingView row that is the same release, or None.

    Candidates are already same-currency, same-minute. Ambiguity is real at a
    shared timestamp — NFP, the unemployment rate and average hourly earnings
    all print at 13:30 — so the period/revision tags must agree, the names
    must be close, and a disagreeing ``previous`` vetoes anything short of a
    near-exact name.
    """
    periods, revisions, core = _title_tags(event["title"])
    key = _title_key(event["title"])

    scored: List[Tuple[float, Optional[bool], Dict[str, Any]]] = []
    for candidate in candidates:
        c_periods, c_revisions, c_core = _title_tags(candidate["title"])
        # Symmetric on purpose: "Challenger Job Cuts y/y" is a percentage and
        # TradingView's untagged "Challenger Job Cuts" is a headcount. Same
        # release, different quantity — pairing them publishes a wrong number.
        if periods != c_periods:
            continue
        if revisions and c_revisions and revisions != c_revisions:
            continue
        if core != c_core:
            continue
        ratio = SequenceMatcher(None, key, _title_key(candidate["title"])).ratio()
        scored.append((ratio, _previous_agrees(event.get("previous"), candidate.get("previous")), candidate))

    # Corroborated matches first, then by name closeness.
    scored.sort(key=lambda s: (s[1] is True, s[0]), reverse=True)
    for ratio, agrees, candidate in scored:
        if agrees is False and ratio < 0.95:
            continue
        if ratio >= 0.88 or (ratio >= 0.55 and agrees is True):
            return candidate
    return None


def _units_compatible(event: Dict[str, Any], match: Dict[str, Any]) -> bool:
    """Last check before publishing someone else's number under our title.

    A percentage and a level are never the same series, however close the
    names read. Anything the feeds do not label stays permitted — most rows
    carry no unit at all.
    """
    def is_percent(*values: Any) -> Optional[bool]:
        styles = [_style_of(v) for v in values]
        suffixes = [s[0] for s in styles if s is not None]
        return ("%" in suffixes) if suffixes else None

    left = is_percent(event.get("previous"), event.get("forecast"))
    right = is_percent(match.get("previous"), match.get("forecast"), match.get("actual"))
    return left is None or right is None or left == right


def _backfill_actuals(
    ff: Sequence[Dict[str, Any]], tv: Sequence[Dict[str, Any]]
) -> int:
    """Fill ForexFactory's missing ``actual`` from TradingView. Mutates ``ff``.

    ForexFactory's feed has no ``actual`` column at all, so without this every
    released event on the calendar page reads "forecast 4.2% previous 4.2%"
    with nothing to compare against. Forecast and previous are filled too, but
    only where ForexFactory left them blank — where it has a value, it wins.
    """
    by_slot: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for event in tv:
        when = _parse_utc(event["timestamp"])
        if when is None:
            continue
        by_slot.setdefault((event["currency"], when.strftime("%Y%m%d%H%M")), []).append(event)

    filled = 0
    for event in ff:
        if event.get("actual") not in (None, ""):
            continue
        when = _parse_utc(event["timestamp"])
        if when is None:
            continue
        candidates = by_slot.get((event["currency"], when.strftime("%Y%m%d%H%M")))
        if not candidates:
            continue
        match = _best_match(event, candidates)
        if match is None or not _units_compatible(event, match):
            continue

        actual = _num(match.get("actual"))
        if actual is not None:
            event["actual"] = _format_like(
                actual, (event.get("previous"), event.get("forecast"), match.get("actual"))
            )
            filled += 1
        for field in ("forecast", "previous"):
            if event.get(field) in (None, "") and match.get(field) not in (None, ""):
                event[field] = match[field]
    return filled


async def _fetch_forexfactory() -> List[Dict[str, Any]]:
    """The current week. Titles, impact grades, forecast and previous — the
    feed has no ``actual`` column, so released values arrive via
    :func:`_backfill_actuals`."""
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(_FF_FEED, headers={"User-Agent": "TradeBot/1.0"})
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[economic_calendar] ForexFactory fetch failed: {exc}")
        return []

    out: List[Dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()
        when = _parse_utc(raw.get("date") or raw.get("time"))
        if not title or when is None:
            continue
        out.append(_event(
            title=title,
            currency=str(raw.get("country") or raw.get("currency") or "").upper(),
            impact=str(raw.get("impact") or raw.get("type") or "").strip().lower() or "low",
            when=when,
            source="ForexFactory",
            forecast=raw.get("forecast"),
            previous=raw.get("previous"),
            actual=raw.get("actual"),
        ))
    return out


async def _fetch_tradingview() -> List[Dict[str, Any]]:
    """The whole window — a week back for printed ``actual`` values, then out
    to the horizon for forecasts. Public widget endpoint.

    The past half is only ever used to backfill ForexFactory's rows; the merge
    in :func:`fetch_calendar_window` still drops anything inside FF's week.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=_TV_LOOKBACK_DAYS)
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _TV_FEED,
                params={
                    "from": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "to": (now + timedelta(days=_TV_HORIZON_DAYS)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    ),
                    "countries": _TV_COUNTRIES,
                    "minImportance": -1,
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)",
                    # The endpoint is CORS-gated; it serves 403 without these.
                    "Origin": "https://www.tradingview.com",
                    "Referer": "https://www.tradingview.com/",
                },
            )
            resp.raise_for_status()
            rows = (resp.json() or {}).get("result") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[economic_calendar] TradingView fetch failed: {exc}")
        return []

    out: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        when = _parse_utc(raw.get("date"))
        if not title or when is None:
            continue
        # TradingView splits the number from its unit; ForexFactory ships one
        # string ("4.2%", "197K"). Rejoin so both sources render identically.
        suffix = _tv_suffix(raw)
        out.append(_event(
            title=title,
            currency=str(raw.get("currency") or raw.get("country") or "").upper(),
            impact=_TV_IMPACT.get(raw.get("importance"), "low"),
            when=when,
            source="TradingView",
            forecast=_join_unit(raw.get("forecast"), suffix),
            previous=_join_unit(raw.get("previous"), suffix),
            actual=_join_unit(raw.get("actual"), suffix),
        ))
    return out


async def fetch_calendar_window(force: bool = False) -> List[Dict[str, Any]]:
    """~30 days of events from both sources, split by date. Never raises.

    ForexFactory owns its week and TradingView picks up strictly after it, so
    the two can never report the same release twice under different names
    ("FOMC Statement" vs "Fed Interest Rate Decision").
    """
    global _cache, _cache_at

    async with _lock:
        if _cache and not force and (time.time() - _cache_at) < _CACHE_TTL_SECONDS:
            return _cache

        ff, tv = await asyncio.gather(_fetch_forexfactory(), _fetch_tradingview())

        if not ff and tv and _cache:
            # The feed rate-limits, and a 429 must not quietly delete this
            # week's rows — with their impact grades and backfilled actuals —
            # and leave TradingView's naming to stand in for them. Last good
            # copy stands until the feed answers again.
            #
            # Only when TradingView answered: if both feeds are down there is
            # no new window to build, and the whole previous one is served
            # below untouched.
            ff = [e for e in _cache if e.get("source") == "ForexFactory"]
            if ff:
                logger.debug(
                    f"[economic_calendar] ForexFactory unavailable; keeping {len(ff)} cached rows"
                )

        # Hand over at a day boundary, not at ForexFactory's last event. FF
        # publishes whole weeks, so midnight after its final day is the real
        # edge — cutting at the last event would let a TradingView duplicate
        # timed minutes later slip in under its own wording.
        last = max((_parse_utc(e["timestamp"]) for e in ff), default=None)
        cutoff = (
            last.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            if last is not None
            else None
        )

        # Inside FF's week TradingView is not a second set of rows — it is the
        # missing ``actual`` column on the rows already there.
        filled = _backfill_actuals(ff, tv)

        merged: Dict[str, Dict[str, Any]] = {e["id"]: e for e in ff}
        for event in tv:
            when = _parse_utc(event["timestamp"])
            if cutoff is not None and when is not None and when < cutoff:
                continue  # ForexFactory's week — it is the better record there
            merged.setdefault(event["id"], event)

        if not merged:
            # Serve the stale window rather than nothing — a scheduled event
            # from ten minutes ago is still the truth about next Thursday.
            logger.debug("[economic_calendar] no events fetched; keeping previous window")
            return _cache

        _cache = sorted(merged.values(), key=lambda e: e["timestamp"])
        _cache_at = time.time()
        logger.debug(
            f"[economic_calendar] window refreshed: {len(_cache)} events "
            f"({len(ff)} ForexFactory + {len(_cache) - len(ff)} TradingView), "
            f"{filled} actual values backfilled"
        )
        return _cache


def _with_hours_away(event: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Attach time-to-event at *query* time — the cache outlives the number."""
    when = datetime.fromisoformat(event["timestamp"])
    return {**event, "hours_away": round((when - now).total_seconds() / 3600.0, 1)}


def query_calendar(
    events: Sequence[Dict[str, Any]],
    *,
    currencies: Optional[Sequence[str]] = None,
    impact: Optional[str] = None,
    fomo_only: bool = False,
    days: Optional[int] = None,
    lookback_hours: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Filter a window. Pure — the router and the reminder job share it.

    ``days`` bounds the future edge; ``lookback_hours`` bounds the past edge
    (default: keep everything already fetched, so today's earlier releases stay
    visible with their ``actual`` printed).
    """
    now = datetime.now(timezone.utc)
    wanted = {c.upper() for c in currencies} if currencies else None

    out: List[Dict[str, Any]] = []
    for event in events:
        if wanted and event["currency"] not in wanted:
            continue
        if impact and event["impact"] != impact.lower():
            continue
        if fomo_only and not event["is_fomo"]:
            continue

        enriched = _with_hours_away(event, now)
        hours = enriched["hours_away"]
        if days is not None and hours > days * 24:
            continue
        if lookback_hours is not None and hours < -lookback_hours:
            continue
        out.append(enriched)

    out.sort(key=lambda e: e["timestamp"])
    return out[:limit] if limit else out


async def upcoming_fomo(limit: int = 8, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """The next high-impact events, nearest-first. Future only."""
    window = await fetch_calendar_window()
    currencies = None
    if symbol:
        key = (symbol or "").upper().replace("/", "")
        currencies = ECO_CURRENCY_MAP.get(key, [key[:3], key[3:6]])

    return query_calendar(
        window,
        currencies=currencies,
        fomo_only=True,
        lookback_hours=0,  # future only — a reminder about yesterday is noise
        limit=limit,
    )


def currencies_in(events: Sequence[Dict[str, Any]]) -> List[str]:
    """Distinct currency codes present, for the page's filter chips."""
    return sorted({e["currency"] for e in events if e.get("currency")})


def next_event(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The single nearest future FOMO event, or None."""
    now = datetime.now(timezone.utc)
    future = [
        _with_hours_away(e, now)
        for e in events
        if e.get("is_fomo") and datetime.fromisoformat(e["timestamp"]) > now
    ]
    return min(future, key=lambda e: e["hours_away"]) if future else None


def format_for_agents(events: Sequence[Dict[str, Any]]) -> str:
    """The prompt block agents read.

    Absolute UTC only. This text is stored once and re-read for up to 15
    minutes by every agent run, so a relative "in 3h" would be a lie by the
    time it is used.
    """
    if not events:
        return "No high-impact economic events scheduled in the next two weeks."

    lines = [
        "Scheduled high-impact economic events (UTC). Volatility and spread "
        "widen around these — factor them into entries, stops and hold times:"
    ]
    for e in events:
        bits = [f"- {e['time_utc']} UTC | {e['currency']} | {e['title']}"]
        # A released number beats its own forecast — say it first, and say it
        # plainly, so an agent reading this block knows the event is behind us.
        if e.get("actual"):
            bits.append(f"ACTUAL {e['actual']}")
        if e.get("forecast"):
            bits.append(f"forecast {e['forecast']}")
        if e.get("previous"):
            bits.append(f"previous {e['previous']}")
        lines.append(" — ".join(bits))
    lines.append(
        f"(sources: ForexFactory, TradingView; "
        f"compiled {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)"
    )
    return "\n".join(lines)
