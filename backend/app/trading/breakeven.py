"""Move a position's stop to break-even once the trade is working.

The Telegram sniper does this on its own ladder: the stop jumps to the entry
price at the TP3 milestone and holds there until the final target. Orders from
the trading room carry a single take-profit, so there is no TP3 to key off —
the equivalent milestone is *progress toward the one target the order has*.

Both end in the same place: once a trade has proved itself, it must not be
allowed to turn into a loss.
"""
from __future__ import annotations

from typing import Optional

#: Fraction of the entry→target distance price must cover before the stop is
#: moved up. Two-thirds keeps it out of ordinary noise while still arming well
#: before the target is reached.
DEFAULT_TRIGGER_FRACTION = 0.66


def breakeven_stop(
    *,
    side: str,
    entry: float,
    current_price: float,
    take_profit: Optional[float],
    current_sl: Optional[float],
    trigger_fraction: float = DEFAULT_TRIGGER_FRACTION,
) -> Optional[float]:
    """The new stop-loss for this position, or None to leave it alone.

    Returns ``entry`` once price has covered ``trigger_fraction`` of the way to
    the target, and only when that is an improvement on the stop already set —
    a stop is never moved backwards, which would widen the risk on a position
    that has already been protected.
    """
    if not entry or entry <= 0 or not current_price or current_price <= 0:
        return None
    if not take_profit or take_profit <= 0:
        return None

    is_long = str(side).lower() in {"long", "buy"}
    span = (take_profit - entry) if is_long else (entry - take_profit)
    if span <= 0:
        return None  # target on the wrong side of entry — nothing coherent to do

    progress = ((current_price - entry) if is_long else (entry - current_price)) / span
    if progress < trigger_fraction:
        return None

    if current_sl is not None:
        # Already at or beyond break-even — leave it.
        if is_long and current_sl >= entry:
            return None
        if not is_long and current_sl <= entry:
            return None
    return entry
