"""Trail a position's stop forward to lock in a growing profit.

Break-even (see :mod:`app.trading.breakeven`) protects a working trade from
turning into a loss; it stops moving once the stop reaches entry. Trailing is
the next step: once a trade is comfortably in profit, the stop follows price up
(or down, for a short), banking a rising fraction of the unrealised gain so a
reversal gives back only part of it rather than all of it.

The two are meant to run together — break-even first, then the trail takes over
past entry — and this returns whichever stop is more protective, always
forward-only. A stop is never moved backward: that would re-widen the risk on a
position that has already been secured.
"""
from __future__ import annotations

from typing import Optional

#: How far to entry, as a fraction of the entry→target distance, before the
#: trail arms. Below this the break-even logic owns the stop; the trail only
#: takes over once the trade has clearly proven itself.
DEFAULT_ARM_FRACTION = 0.5

#: Fraction of the favourable excursion (entry→current) the trailed stop locks.
#: Half keeps the stop clear of ordinary retracement while still securing real
#: profit — the same fraction the scalp bot's own trail uses.
DEFAULT_LOCK_FRACTION = 0.5


def trailing_stop(
    *,
    side: str,
    entry: float,
    current_price: float,
    take_profit: Optional[float],
    current_sl: Optional[float],
    arm_fraction: float = DEFAULT_ARM_FRACTION,
    lock_fraction: float = DEFAULT_LOCK_FRACTION,
) -> Optional[float]:
    """The trailed stop for this position, or None to leave it alone.

    Returns a stop that locks ``lock_fraction`` of the entry→current move once
    price has covered ``arm_fraction`` of the way to the target, and only when
    that improves on the stop already set. The trailed stop can never sit on the
    wrong side of the current price, so it is always a real, placeable level.
    """
    if not entry or entry <= 0 or not current_price or current_price <= 0:
        return None
    if not take_profit or take_profit <= 0:
        return None

    is_long = str(side).lower() in {"long", "buy"}
    span = (take_profit - entry) if is_long else (entry - take_profit)
    if span <= 0:
        return None  # target on the wrong side of entry — nothing coherent to do

    excursion = (current_price - entry) if is_long else (entry - current_price)
    if excursion <= 0:
        return None  # not in profit yet
    if excursion / span < arm_fraction:
        return None  # too early — break-even still owns the stop

    locked = entry + lock_fraction * excursion if is_long \
        else entry - lock_fraction * excursion

    # Never past the current price: a stop there would close instantly.
    if is_long and locked >= current_price:
        return None
    if not is_long and locked <= current_price:
        return None

    # Forward-only: only return it when it tightens the existing stop.
    if current_sl is not None:
        if is_long and locked <= current_sl:
            return None
        if not is_long and locked >= current_sl:
            return None
    return locked


def protective_stop(
    *,
    side: str,
    entry: float,
    current_price: float,
    take_profit: Optional[float],
    current_sl: Optional[float],
) -> Optional[float]:
    """The best of break-even and trailing for this position, forward-only.

    One call for the auto-manage cycle: it moves the stop to entry as soon as
    the trade is working, then keeps advancing it as the trail arms and profit
    grows, and returns None once neither can improve on the stop already set.
    """
    from app.trading.breakeven import breakeven_stop

    candidates = [
        breakeven_stop(
            side=side, entry=entry, current_price=current_price,
            take_profit=take_profit, current_sl=current_sl,
        ),
        trailing_stop(
            side=side, entry=entry, current_price=current_price,
            take_profit=take_profit, current_sl=current_sl,
        ),
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    is_long = str(side).lower() in {"long", "buy"}
    # The most protective stop is the highest for a long, the lowest for a short.
    return max(candidates) if is_long else min(candidates)
