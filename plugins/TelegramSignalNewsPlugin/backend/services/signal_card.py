"""Publish the Signal Generator's call the way a desk publishes it.

The room's own message is a board of opinions — useful for reading, useless for
acting on. What a trader copies into a terminal is a side, a band to fill, a
ladder of targets and a stop, in the instrument's own prices::

    #XAUUSD Sell 4403/4405

    TP 4400
    TP 4397
    ...

    SL 4411

Every number here comes from the agent, but none is published unmodified. A
ladder is only a ladder if each rung is further along than the last and all of
them clear the entry; a stop only protects if it sits the other side of the
band. Models produce plans that fail those tests often enough — a target behind
the entry, a stop price has already passed — that the checks are the point of
this module. A malformed plan is dropped rather than tidied into something that
looks tradeable but never was.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

#: Most rungs a ladder is published with. Beyond this the message is a wall of
#: numbers nobody reads, and the far ones are past any structure the analysis
#: actually identified.
MAX_TAKE_PROFITS = 6

#: Widest an entry band may be, as a fraction of price. A "zone" wider than this
#: is not one order, it is a region — filling it means a different trade at each
#: end, with the published stop only correct for one of them.
MAX_ZONE_WIDTH = 0.004


def _num(value: Any) -> float | None:
    try:
        out = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _tick(price: float) -> int:
    """Decimals to publish at, from the price's own magnitude."""
    if price >= 1000:
        return 2
    if price >= 10:
        return 3
    if price >= 1:
        return 4
    return 6


def _fmt(price: float) -> str:
    text = f"{price:.{_tick(price)}f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class Zone:
    """A level to trade from if price returns to it, with its own plan."""

    side: str
    low: float
    high: float
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)
    note: str = ""


@dataclass
class SignalCard:
    """One publishable signal."""

    symbol: str
    side: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)
    zone: Zone | None = None
    analysis_number: int | None = None
    at: datetime | None = None


def _ordered_targets(
    targets: list[float], *, entry: float, is_long: bool,
) -> list[float]:
    """Targets that actually lie ahead of the entry, in the order they'd be hit.

    Anything level with or behind the entry is dropped: published, it reads as a
    target that filled the moment the trade opened.
    """
    ahead = [t for t in targets if (t > entry if is_long else t < entry)]
    ordered = sorted(set(ahead), reverse=not is_long)
    return ordered[:MAX_TAKE_PROFITS]


def _valid_stop(stop: float | None, *, entry: float, is_long: bool) -> float | None:
    """The stop, if it is on the side of the entry that protects the trade."""
    if stop is None:
        return None
    if is_long and stop >= entry:
        return None
    if not is_long and stop <= entry:
        return None
    return stop


def _entry_band(
    zone: Any, fallback_entry: float | None, price: float | None,
) -> tuple[float, float] | None:
    """The band to fill, from the agent's zone or a single entry price."""
    values = []
    if isinstance(zone, (list, tuple)):
        values = [v for v in (_num(z) for z in zone) if v is not None]
    elif isinstance(zone, dict):
        values = [v for v in (_num(zone.get("low")), _num(zone.get("high"))) if v is not None]

    if len(values) >= 2:
        low, high = min(values), max(values)
        if high - low <= max(low, 1e-9) * MAX_ZONE_WIDTH:
            return low, high
        logger.debug("[SignalCard] entry band {}-{} too wide — narrowing", low, high)
        mid = (low + high) / 2
        half = mid * MAX_ZONE_WIDTH / 2
        return mid - half, mid + half

    single = fallback_entry if fallback_entry is not None else price
    if single is None:
        return None
    # A single price still publishes as a band, because that is what gets
    # filled: a scalp entry quoted to the tick is an order nobody catches.
    half = single * MAX_ZONE_WIDTH / 4
    return single - half, single + half


def build(
    decision: dict[str, Any],
    *,
    symbol: str,
    price: float | None = None,
    analysis_number: int | None = None,
    at: datetime | None = None,
) -> SignalCard | None:
    """Turn one Signal Generator decision into a publishable card, or None.

    None means the plan did not survive its own arithmetic — no side, no levels,
    or levels that contradict each other. Publishing nothing is the right answer
    there: a trader who copies an incoherent plan loses money in a way that
    looks like the desk's fault, because it is.
    """
    side = str(decision.get("action") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    is_long = side == "buy"

    band = _entry_band(
        decision.get("entry_zone"), _num(decision.get("entry_price")), price,
    )
    if band is None:
        logger.debug("[SignalCard] {} has no entry to publish", symbol)
        return None
    entry_low, entry_high = band

    # Targets are measured from the edge they must clear: for a long that is the
    # top of the band, for a short the bottom. Measuring from the middle would
    # publish a first target already inside the entry.
    edge = entry_high if is_long else entry_low
    raw_targets = [t for t in (_num(t) for t in (decision.get("take_profits") or [])) if t]
    targets = _ordered_targets(raw_targets, entry=edge, is_long=is_long)

    stop = _valid_stop(
        _num(decision.get("stop_loss")),
        entry=entry_low if is_long else entry_high,
        is_long=is_long,
    )

    if not targets or stop is None:
        logger.debug(
            "[SignalCard] {} dropped — {} usable target(s), stop {}",
            symbol, len(targets), "ok" if stop else "invalid",
        )
        return None

    return SignalCard(
        symbol=symbol.upper().replace("/", "").replace(":", ""),
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        take_profits=targets,
        zone=_zone(decision.get("reaction_zone")),
        analysis_number=analysis_number,
        at=at,
    )


def _zone(raw: Any) -> Zone | None:
    """The reaction level, if the agent gave a coherent one."""
    if not isinstance(raw, dict):
        return None
    side = str(raw.get("side") or "").strip().lower()
    low, high = _num(raw.get("low")), _num(raw.get("high"))
    if side not in ("buy", "sell") or low is None or high is None:
        return None
    low, high = min(low, high), max(low, high)
    is_long = side == "buy"

    stop = _valid_stop(
        _num(raw.get("stop_loss")), entry=low if is_long else high, is_long=is_long,
    )
    targets = _ordered_targets(
        [t for t in (_num(t) for t in (raw.get("take_profits") or [])) if t],
        entry=high if is_long else low,
        is_long=is_long,
    )
    if stop is None or not targets:
        return None
    return Zone(
        side=side, low=low, high=high, stop_loss=stop,
        take_profits=targets[:3], note=str(raw.get("note") or "").strip(),
    )


def render(card: SignalCard) -> str:
    """The card as Telegram HTML, in the format the channel publishes."""
    esc = html.escape
    side = card.side.capitalize()
    lines = [
        f"#{esc(card.symbol)} <b>{side} {_fmt(card.entry_low)}/{_fmt(card.entry_high)}</b>",
        "",
    ]
    lines += [f"TP {_fmt(tp)}" for tp in card.take_profits]
    lines += ["", f"SL {_fmt(card.stop_loss)}"]

    if card.zone is not None:
        when = (card.at or datetime.utcnow()).strftime("%B %d").upper()
        heading = "Analysis"
        if card.analysis_number:
            heading = f"Analysis {card.analysis_number}"
        zone = card.zone
        icon = "🟢" if zone.side == "buy" else "🔴"
        lines += [
            "",
            f"<b>{heading} 💫 {when} - {esc(card.symbol)}</b>",
            "",
            f"{icon} {zone.side.upper()} ZONE {_fmt(zone.low)} - {_fmt(zone.high)}",
            f"🔸 Stoploss: {_fmt(zone.stop_loss)}",
        ]
        lines += [
            f"🔹 Takeprofit {i}: {_fmt(tp)}"
            for i, tp in enumerate(zone.take_profits, start=1)
        ]
        if zone.note:
            lines += ["", esc(zone.note)[:600]]

    return "\n".join(lines)[:3900]
