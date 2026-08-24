"""Read an economic release the way a desk would: beat, miss, and what it means.

The calendar service already fetches the numbers — actual, forecast, previous.
What was missing is the step between a number and a decision: 0.2% against a
0.3% forecast is a *miss*, a miss on inflation is *dovish*, and a dovish print
is dollar-negative and therefore gold-positive.

Two things make that chain safe to automate. First, direction is per-indicator
and explicit: higher CPI is dollar-positive, higher unemployment is
dollar-negative, and anything not in the table is reported without an
interpretation rather than guessed at. Second, nothing is said about an event
whose actual has not printed — a forecast on its own is not a result, and
reading one as though it were is how a calendar entry becomes a fake headline.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

#: Indicators where a higher reading strengthens the currency (hot inflation
#: and strong activity both argue for tighter policy), and those where it
#: weakens it. Matched as substrings against the release title, longest first
#: so "core cpi" is not shadowed by "cpi".
_HIGHER_IS_STRONGER = (
    "core cpi", "core pce", "core ppi", "cpi", "ppi", "pce", "inflation rate",
    "gdp", "retail sales", "non-farm", "non farm", "nfp", "payrolls",
    "employment change", "industrial production", "durable goods",
    "interest rate", "pmi", "consumer confidence", "consumer sentiment",
    "trade balance",
)
_HIGHER_IS_WEAKER = (
    "unemployment rate", "jobless claims", "initial claims", "continuing claims",
    # Feeds word the weekly claims print several ways ("Unemployment Claims" on
    # ForexFactory); matching the bare noun catches all of them, and no
    # currency-positive release is called "claims".
    "claims",
)

#: Currencies whose strength moves gold inversely. Gold is priced in dollars,
#: so only the dollar leg carries this relationship.
_GOLD_INVERSE = "USD"

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_SUFFIXES = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_value(raw: Any) -> Optional[float]:
    """Turn ``"197K"`` / ``"4.2%"`` / ``"-0.3"`` into a number, or None.

    Units are stripped rather than converted between: a release is only ever
    compared against its own forecast and previous, which carry the same unit.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if not text or text in ("-", "—", "n/a", "N/A"):
        return None
    match = _NUM.search(text)
    if not match:
        return None
    value = float(match.group())
    tail = text[match.end():].strip().lower()
    for suffix, factor in _SUFFIXES.items():
        if tail.startswith(suffix):
            return value * factor
    return value


def _direction(title: str) -> Optional[int]:
    """+1 when a higher reading is currency-positive, -1 when negative."""
    low = (title or "").lower()
    for keyword in sorted(_HIGHER_IS_WEAKER, key=len, reverse=True):
        if keyword in low:
            return -1
    for keyword in sorted(_HIGHER_IS_STRONGER, key=len, reverse=True):
        if keyword in low:
            return 1
    return None


def read_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One release, scored against its forecast. None when it cannot be read.

    A release with no actual has not happened yet, and one with no forecast has
    nothing to be measured against — both are skipped rather than narrated.
    """
    actual = parse_value(event.get("actual"))
    forecast = parse_value(event.get("forecast"))
    if actual is None or forecast is None:
        return None

    title = str(event.get("title") or "")
    direction = _direction(title)

    if actual > forecast:
        surprise = "above"
    elif actual < forecast:
        surprise = "below"
    else:
        surprise = "inline"

    # A beat only helps the currency when a higher print is the strong side.
    bias = 0
    if direction is not None and surprise != "inline":
        bias = direction if surprise == "above" else -direction

    return {
        "title": title,
        "currency": str(event.get("currency") or "").upper(),
        "actual": event.get("actual"),
        "forecast": event.get("forecast"),
        "previous": event.get("previous"),
        "surprise": surprise,
        "bias": bias,
        "date": event.get("date"),
        "interpretable": direction is not None,
    }


def _sentence(read: Dict[str, Any]) -> str:
    """The plain-language reading under one release line."""
    subject = read["title"]
    if read["surprise"] == "inline":
        return f"→ {subject} landed exactly on forecast, leaving expectations unchanged."
    if not read["interpretable"]:
        # Honest about the limit: the number is real, the direction is not ours
        # to assume for a release we have no rule for.
        return (
            f"→ {subject} came in {read['surprise']} forecast; its policy read "
            "depends on the detail rather than the headline number."
        )
    if read["bias"] > 0:
        return (
            f"→ {subject} came in {read['surprise']} expectations, pointing to "
            "firmer pressure and a more hawkish path."
        )
    return (
        f"→ {subject} came in {read['surprise']} expectations, suggesting softer "
        "pressure than anticipated and a more dovish path."
    )


def release_narrative(
    events: Sequence[Dict[str, Any]], *, currency: str = "USD", limit: int = 6
) -> str:
    """The desk read of today's ``currency`` releases, or "" when none printed."""
    reads = [
        r for e in events or []
        if (r := read_event(e)) and r["currency"] == currency.upper()
    ]
    if not reads:
        return ""
    if len(reads) > limit:
        # Trim the ones carrying no policy read first — a storage number
        # padding the list costs the reader attention and tells them nothing.
        keep = [r for r in reads if r["interpretable"]][:limit]
        reads = keep or reads[:limit]

    date = next((r["date"] for r in reads if r.get("date")), "")
    headline = reads[0]["title"]
    lines = [
        f"🌐 {currency.upper()} NEWS: {headline}" + (f" – {date}" if date else ""),
        "",
        "📊 Market context",
        "",
    ]

    for read in reads:
        lines.append(
            f"• {read['title']}: {read['actual']} vs {read['forecast']} Forecast "
            f"vs {read['previous']} Previous"
        )
        lines.append(_sentence(read))
        lines.append("")

    scored = [r for r in reads if r["interpretable"] and r["bias"]]
    lines.append("✔️ Overall Insight:")
    lines.append("")

    if not scored:
        lines.append("→ Nothing in this batch moves the policy read on its own.")
        return "\n".join(lines)

    net = sum(r["bias"] for r in scored)
    # "Every reading" has to mean every reading the reader can see above, not
    # just the ones we scored — a summary that contradicts its own list is
    # worse than no summary.
    if len(reads) > 1 and all(r["surprise"] == "below" for r in reads):
        lines.append("→ Every reading came in below forecast.")
    elif len(reads) > 1 and all(r["surprise"] == "above" for r in reads):
        lines.append("→ Every reading came in above forecast.")

    if net > 0:
        lines.append("→ This lifts inflation and policy pressure, favouring a tighter Fed.")
        lines.append(f"→ {currency.upper()} Bullish")
        if currency.upper() == _GOLD_INVERSE:
            lines.append("→ Gold Bearish")
    elif net < 0:
        lines.append("→ This reduces inflation pressure and supports a more dovish Fed.")
        lines.append(f"→ {currency.upper()} Bearish")
        if currency.upper() == _GOLD_INVERSE:
            lines.append("→ Gold Bullish")
    else:
        # A genuine split must read as a split, not be forced to a side.
        lines.append("→ The readings pull in opposite directions and net out flat.")
        lines.append(f"→ {currency.upper()} Neutral")

    return "\n".join(lines)


async def latest_release_read(symbol: str = "XAUUSD", *, lookback_hours: int = 24) -> str:
    """Today's released numbers for whichever currencies drive ``symbol``.

    Never raises: a calendar outage is missing context, not a failed answer.
    """
    from loguru import logger

    try:
        from plugins.MT5TradingPlugin.backend.services.economic_calendar import (
            ECO_CURRENCY_MAP, fetch_calendar_window, query_calendar,
        )
    except ImportError as exc:
        logger.debug("[Release] calendar unavailable: {}", exc)
        return ""

    try:
        events = await fetch_calendar_window()
        currencies = ECO_CURRENCY_MAP.get(
            (symbol or "").upper().replace("/", ""), ["USD"]
        )
        # Metals are a currency leg on the map but never a release currency.
        currencies = [c for c in currencies if c not in ("XAU", "XAG")] or ["USD"]
        recent = query_calendar(
            events, currencies=currencies, days=1, lookback_hours=lookback_hours
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Release] calendar read failed for {}: {}", symbol, exc)
        return ""

    for currency in currencies:
        if text := release_narrative(recent, currency=currency):
            return text
    return ""
