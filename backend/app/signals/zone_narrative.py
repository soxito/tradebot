"""Describe the zones price is about to trade into, nearest first.

The SMC engine already finds order blocks and fair-value gaps; what it does not
say is which of them price is actually walking towards, or what to do when it
gets there. That is what this adds: the zones ahead of price, ordered by how
soon price would reach them, each with the reaction to watch for.

Only zones price has not already traded through are described. A zone behind
price is history, and presenting it as a level to watch would invite an entry
against a move that has already happened.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Order blocks earn the supply/demand naming: the engine keeps every one it
#: finds precisely because they are rare and significant, while fair-value gaps
#: are common and are better described as levels price reacts at.
_SUPPLY_KINDS = {"bearish_ob"}
_DEMAND_KINDS = {"bullish_ob"}


def _fmt(value: float) -> str:
    a = abs(value)
    if a >= 1000:
        return f"{value:,.2f}"
    if a >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _bounds(zone: Dict[str, Any]) -> Optional[tuple[float, float]]:
    top, bottom = zone.get("top"), zone.get("bottom")
    if not isinstance(top, (int, float)) or not isinstance(bottom, (int, float)):
        return None
    return (float(min(top, bottom)), float(max(top, bottom)))


def _merge_overlapping(zones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fuse zones whose ranges overlap into one.

    The engine finds order blocks and fair-value gaps independently, so the
    same shelf of price is often returned twice. Listed separately they read as
    two levels to watch when there is only one, and the "if the first breaks"
    chaining becomes nonsense — the second zone's range starts below the first
    one's top. The merged zone keeps the outer bounds and the more significant
    kind, since an order block overlapping a gap is still an order block.
    """
    merged: List[Dict[str, Any]] = []
    for zone in sorted(zones, key=lambda z: z["low"]):
        if merged and zone["low"] <= merged[-1]["high"]:
            prev = merged[-1]
            prev["high"] = max(prev["high"], zone["high"])
            prev["low"] = min(prev["low"], zone["low"])
            prev["distance"] = min(prev["distance"], zone["distance"])
            if "ob" in str(zone.get("kind") or "") and "ob" not in str(prev.get("kind") or ""):
                prev["kind"] = zone["kind"]
        else:
            merged.append(dict(zone))
    return merged


def zones_ahead(
    zones: List[Dict[str, Any]], last_price: float, *, limit: int = 3
) -> Dict[str, List[Dict[str, Any]]]:
    """Split ``zones`` into those above and below ``last_price``, nearest first.

    Distinct from the engine's own chart ordering, which is by recency: what
    matters when describing what to watch is which level price meets next.
    Overlapping zones are fused, so one shelf of price is described once.
    """
    above: List[Dict[str, Any]] = []
    below: List[Dict[str, Any]] = []
    for zone in zones or []:
        pair = _bounds(zone)
        if pair is None:
            continue
        low, high = pair
        if low > last_price:
            above.append({**zone, "low": low, "high": high, "distance": low - last_price})
        elif high < last_price:
            below.append({**zone, "low": low, "high": high, "distance": last_price - high})
        # A zone price is currently inside is neither ahead nor behind — it is
        # being tested right now, and belongs to whichever side it resolves to.

    above = sorted(_merge_overlapping(above), key=lambda z: z["distance"])
    below = sorted(_merge_overlapping(below), key=lambda z: z["distance"])
    return {"above": above[:limit], "below": below[:limit]}


def _label(zone: Dict[str, Any], side: str) -> str:
    kind = str(zone.get("kind") or "")
    if side == "above":
        return "Supply Zone" if kind in _SUPPLY_KINDS else "Reaction Zone"
    return "Demand Zone" if kind in _DEMAND_KINDS else "Reaction Zone"


def zone_narrative(
    zones: List[Dict[str, Any]],
    last_price: float,
    *,
    timeframe: str = "",
    limit: int = 2,
) -> str:
    """The levels to watch above and below ``last_price``, or "" when none."""
    if not isinstance(last_price, (int, float)) or last_price <= 0:
        return ""

    ahead = zones_ahead(zones, float(last_price), limit=limit)
    if not ahead["above"] and not ahead["below"]:
        return ""

    tf = f" on the {timeframe.upper()} timeframe" if timeframe else ""
    lines = ["⭐️ Key Levels to Watch"]

    for i, zone in enumerate(ahead["above"]):
        label = _label(zone, "above")
        lines.append(f"\n➡️ {label}: {_fmt(zone['low'])} - {_fmt(zone['high'])}")
        if label == "Supply Zone":
            lines.append(
                f"This is a key supply zone{tf if i == 0 else ''}. If price reaches it, "
                "prioritise waiting for a SELL signal or rejection rather than "
                "chasing the move up."
            )
        elif i == 0:
            lines.append(
                "The first zone overhead. Avoid chasing BUY positions into it — "
                "wait for price action to confirm:"
            )
            lines.append(f"Break & hold above {_fmt(zone['high'])} → prioritise further BUYing")
            lines.append("Strong rejection → potential pullback.")
        else:
            lines.append(
                f"If {_fmt(ahead['above'][i - 1]['high'])} is broken, this becomes the "
                "next zone to watch."
            )

    for i, zone in enumerate(ahead["below"]):
        label = _label(zone, "below")
        lines.append(f"\n➡️ {label}: {_fmt(zone['low'])} - {_fmt(zone['high'])}")
        if label == "Demand Zone":
            lines.append(
                "A key demand zone below. If price trades down into it, look for "
                "buying interest or rejection rather than selling into support."
            )
        elif i == 0:
            lines.append(
                "The first zone below. Losing it opens the door to a deeper "
                "retracement:"
            )
            lines.append(f"Break & hold below {_fmt(zone['low'])} → prioritise SELLing")
            lines.append("Strong bounce → potential continuation higher.")
        else:
            lines.append(
                f"If {_fmt(ahead['below'][i - 1]['low'])} gives way, this becomes the "
                "next zone to watch."
            )

    return "\n".join(lines)
