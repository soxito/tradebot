"""
MT5 Trading Plugin — economic calendar window (ForexFactory).

One cached fetch, three consumers:

  * ``/research/calendar``            — the Research & Calendar page
  * ``research_loop.remind_agents_of_calendar`` — the always-injected agent block
  * ``smc_ai.fetch_economic_events``  — the SMC prompt and the scalp gate

Two key-less sources, split by date so they can never duplicate each other:

  * **ForexFactory** (``ff_calendar_thisweek.json``) owns the current week. It
    is what the SMC prompt has always read and it carries ``actual`` values as
    releases print. It publishes one week and only one week —
    ``ff_calendar_nextweek.json`` is a 404.
  * **TradingView's** public economic-calendar endpoint covers everything
    *after* ForexFactory's last date, out to ~30 days, with forecasts.

Every impact level is kept in the cache; callers narrow at the query layer via
``query_calendar``. ``is_fomo`` marks high-impact events — rate decisions, CPI,
NFP, FOMC, GDP — which is what the calendar page highlights and what the agent
reminder is built from.

Nothing here raises. A dead feed yields an empty window and every caller
degrades to "no known events", which is the same state as a quiet week.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

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
HIGH_IMPACT_KEYWORDS = (
    "interest rate", "cpi", "inflation", "gdp", "nfp", "non-farm",
    "unemployment", "fomc", "federal reserve", "ecb", "boe", "rba",
    "pmi", "retail sales", "trade balance", "pce", "core cpi",
)

#: ForexFactory's weekly feed — the current week only.
_FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

#: TradingView's public calendar, used to extend past ForexFactory's last day.
_TV_FEED = "https://economic-calendar.tradingview.com/events"
_TV_COUNTRIES = "US,EU,GB,JP,AU,CA,CH,NZ,CN"
_TV_HORIZON_DAYS = 30

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


async def _fetch_forexfactory() -> List[Dict[str, Any]]:
    """The current week, with ``actual`` filled in as releases print."""
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


async def _fetch_tradingview(after: Optional[datetime]) -> List[Dict[str, Any]]:
    """Everything after ``after``, out to the horizon. Public widget endpoint."""
    now = datetime.now(timezone.utc)
    start = max(after, now) if after else now
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
        out.append(_event(
            title=title,
            currency=str(raw.get("currency") or raw.get("country") or "").upper(),
            impact=_TV_IMPACT.get(raw.get("importance"), "low"),
            when=when,
            source="TradingView",
            forecast=raw.get("forecast"),
            previous=raw.get("previous"),
            actual=raw.get("actual"),
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

        ff = await _fetch_forexfactory()

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

        tv = await _fetch_tradingview(cutoff)

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
            f"({len(ff)} ForexFactory + {len(_cache) - len(ff)} TradingView)"
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
        if e.get("forecast"):
            bits.append(f"forecast {e['forecast']}")
        if e.get("previous"):
            bits.append(f"previous {e['previous']}")
        lines.append(" — ".join(bits))
    lines.append(f"(source: ForexFactory; compiled {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")
    return "\n".join(lines)
