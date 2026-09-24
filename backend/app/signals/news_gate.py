"""High-impact news: no new room entries around a release, then a cool-off.

A pair is blocked from ``pre_min`` minutes before a high-impact event on any
of its currencies until ``cooloff_min`` minutes after it. Open positions are
still managed; a fresh OBLIVION signal is taken once the cool-off is over.

Live windows come from the app's economic calendar (ForexFactory +
TradingView); the backtest uses TradingView's high-impact history, cached on
disk, through the same :func:`blocking_event`.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

PRE_MIN_DEFAULT = 30
COOLOFF_MIN_DEFAULT = 60
_HISTORY_FILE = Path(__file__).resolve().parents[2] / "data" / "news_history.json"


def currencies_for(symbol: str) -> List[str]:
    key = (symbol or "").upper().replace("/", "")
    try:
        from plugins.MT5TradingPlugin.backend.services.economic_calendar import ECO_CURRENCY_MAP

        if key in ECO_CURRENCY_MAP:
            return [c for c in ECO_CURRENCY_MAP[key] if len(c) == 3 and c not in ("XAU", "XAG")]
    except Exception:  # noqa: BLE001
        pass
    for quote in ("USDT", "USDC"):
        if key.endswith(quote):
            return ["USD"]
    if key[:3] in ("XAU", "XAG", "BTC", "ETH", "SOL"):
        return ["USD"]
    known = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY", "SEK", "NOK"}
    if len(key) == 6 and key[:3] in known and key[3:] in known:
        return [key[:3], key[3:]]
    if key.startswith(("UK",)):
        return ["GBP"]
    if key.startswith(("FRA", "DE", "GER", "EU")):
        return ["EUR"]
    if key.startswith("AUS"):
        return ["AUD"]
    if key.startswith(("JP", "JPN")):
        return ["JPY"]
    if key.startswith("HK"):
        return ["CNY", "USD"]
    # US indices, stocks and ETFs move on US releases.
    return ["USD"]


def blocking_event(events: Sequence[Tuple[int, str, str]], currencies: Sequence[str], t: int,
                   pre_min: int, cooloff_min: int) -> Optional[Tuple[int, str, str, int]]:
    """The high-impact event whose window covers ``t`` (unix s), with the time
    the window ends, or None. ``events`` = [(ts, currency, title)]. Pure."""
    wanted = set(currencies)
    for ts, cur, title in events:
        if cur not in wanted:
            continue
        if ts - pre_min * 60 <= t <= ts + cooloff_min * 60:
            return ts, cur, title, ts + cooloff_min * 60
        if ts - pre_min * 60 > t:
            break
    return None


# ── live ──────────────────────────────────────────────────────────────────


async def live_block(symbol: str, *, pre_min: int = PRE_MIN_DEFAULT,
                     cooloff_min: int = COOLOFF_MIN_DEFAULT) -> Optional[Dict[str, Any]]:
    """The high-impact event blocking new entries on ``symbol`` right now, or None."""
    try:
        from plugins.MT5TradingPlugin.backend.services.economic_calendar import fetch_calendar_window
    except Exception:  # noqa: BLE001 — plugin-optional
        return None
    try:
        window = await fetch_calendar_window()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[news-gate] calendar unavailable: {exc}")
        return None
    events = sorted(
        (int(datetime.fromisoformat(e["timestamp"]).timestamp()), e["currency"], e["title"])
        for e in window if e.get("impact") == "high"
    )
    hit = blocking_event(events, currencies_for(symbol), int(time.time()), pre_min, cooloff_min)
    if hit is None:
        return None
    ts, cur, title, until = hit
    return {"currency": cur, "title": title, "at": ts, "until": until,
            "reason": f"high-impact {cur} news ({title}) at "
                      f"{time.strftime('%H:%M UTC', time.gmtime(ts))} — no new entries until "
                      f"{time.strftime('%H:%M UTC', time.gmtime(until))}"}


# ── history (backtest) ────────────────────────────────────────────────────


async def history(start_s: int, end_s: int) -> List[Tuple[int, str, str]]:
    """High-impact events between two unix times, cached on disk by month."""
    import httpx

    from plugins.MT5TradingPlugin.backend.services import economic_calendar as ec

    cache: Dict[str, List[List[Any]]] = {}
    if _HISTORY_FILE.exists():
        try:
            cache = json.loads(_HISTORY_FILE.read_text())
        except Exception:  # noqa: BLE001
            cache = {}
    start = datetime.fromtimestamp(start_s, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_s, timezone.utc)
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    changed = False
    async with httpx.AsyncClient(timeout=20.0) as client:
        month = start
        while month <= end:
            key = month.strftime("%Y-%m")
            nxt = (month + timedelta(days=32)).replace(day=1)
            if key not in cache or key == this_month:
                try:
                    resp = await client.get(ec._TV_FEED, params={
                        "from": month.strftime("%Y-%m-%dT00:00:00.000Z"),
                        "to": nxt.strftime("%Y-%m-%dT00:00:00.000Z"),
                        "countries": ec._TV_COUNTRIES, "minImportance": 1,
                    }, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com",
                                "Referer": "https://www.tradingview.com/"})
                    rows = (resp.json() or {}).get("result") or []
                    cache[key] = [[int(datetime.fromisoformat(r["date"].replace("Z", "+00:00")).timestamp()),
                                   str(r.get("currency") or "").upper(), str(r.get("title") or "")]
                                  for r in rows if r.get("date")]
                    changed = True
                    await asyncio.sleep(0.3)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[news-gate] history for {key} unavailable: {exc}")
            month = nxt
    if changed:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(json.dumps(cache))
    out = [tuple(e) for rows in cache.values() for e in rows if start_s <= e[0] <= end_s]
    return sorted(out)  # type: ignore[return-value]
