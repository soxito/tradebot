"""Is this stop far enough away to survive normal noise?

A stop is wrong in two different ways, and only one of them is usually checked.
The obvious error is a stop on the wrong side of the entry — the signal card
already rejects those. The expensive error is a stop on the *right* side but too
close: inside the pair's ordinary hourly range, or just under the swing low
every stop-hunt in that market reaches for.

That is not a hypothetical. A published gold plan bought 4498.35–4505.50 with
the stop at 4475 — roughly 26 points under the entry, well inside gold's own
hourly range. Price swept to 4452 within the hour, took the stop, and then ran
to 4535, straight through every target on the card. The direction was right; the
stop was the whole loss.

So this module answers one question — *how far does a stop have to be from the
entry before it is measuring the trade rather than the noise?* — from two
independent floors:

  volatility  the stop must clear a multiple of ATR, because a stop inside one
              average bar is hit by an average bar
  structure   the stop must sit beyond the swing the market last turned at,
              plus a buffer, because that swing is where the resting liquidity
              is and price reaches through it before it reverses

Widening a stop widens the risk *per lot*, never the risk per trade: the
position sizer derives volume from the stop distance, so a stop pushed out here
comes back as a smaller position, not a bigger loss. That relationship is what
makes this safe to apply automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

#: Multiples of ATR. ``MIN`` is the volatility floor a stop must clear; ``MAX``
#: is where a stop stops being protection and becomes an unsized bet — beyond
#: it the plan is reported as unsound rather than quietly widened further.
MIN_ATR_MULTIPLE = 1.5
MAX_ATR_MULTIPLE = 4.0

#: Clearance beyond the structural swing, in ATR. Price routinely trades a few
#: ticks through a swing low to trip the stops resting under it; a stop sitting
#: exactly on that swing is the one the sweep is aiming at.
SWING_BUFFER_ATR = 0.4

#: How many bars back to look for the swing the stop must clear.
SWING_LOOKBACK = 24


@dataclass(frozen=True)
class StopAssessment:
    """Where the stop should be, and why it moved."""

    stop: float
    proposed: Optional[float]
    #: Minimum entry→stop distance this market justifies right now.
    floor_distance: float
    widened: bool
    #: ``ok`` | ``widened`` | ``unsound`` — ``unsound`` means even the floor is
    #: further than a sane plan should risk, so the caller should not publish.
    verdict: str
    reason: str

    @property
    def distance(self) -> float:
        return abs(self.stop - (self.proposed if self.proposed else self.stop))


def _closes(candles: Sequence[Sequence[float]]) -> list[float]:
    return [float(c[4]) for c in candles if c and len(c) > 4]


def atr(candles: Sequence[Sequence[float]], period: int = 14) -> Optional[float]:
    """Average true range over *candles* (OHLCV rows), or None if too short.

    True range rather than close-to-close: a stop is hit by the *wick*, so the
    measure that sizes it has to include the wick too.
    """
    if not candles or len(candles) < period + 1:
        return None
    trs: list[float] = []
    for prev, cur in zip(candles[-(period + 1):-1], candles[-period:]):
        high, low, prev_close = float(cur[2]), float(cur[3]), float(prev[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return None
    return sum(trs) / len(trs)


def swing_extreme(
    candles: Sequence[Sequence[float]], *, is_long: bool, lookback: int = SWING_LOOKBACK,
) -> Optional[float]:
    """The low a long must sit under (or the high a short must sit over).

    Taken from the wicks, not the bodies: the liquidity rests where price has
    actually traded, which is exactly where the wick reached.
    """
    window = list(candles)[-lookback:]
    if not window:
        return None
    try:
        return min(float(c[3]) for c in window) if is_long \
            else max(float(c[2]) for c in window)
    except (IndexError, TypeError, ValueError):
        return None


def required_distance(
    *,
    entry: float,
    is_long: bool,
    atr_value: Optional[float],
    swing: Optional[float],
    atr_multiple: float = MIN_ATR_MULTIPLE,
) -> float:
    """The minimum entry→stop distance, from volatility and structure together.

    Returns 0.0 when there is nothing to measure with — no ATR and no swing —
    because inventing a floor from a guess would be worse than leaving the
    agent's own number alone.
    """
    floors = []
    if atr_value and atr_value > 0:
        floors.append(atr_value * atr_multiple)
        if swing:
            beyond = (entry - swing) if is_long else (swing - entry)
            if beyond > 0:
                floors.append(beyond + atr_value * SWING_BUFFER_ATR)
    elif swing:
        beyond = (entry - swing) if is_long else (swing - entry)
        if beyond > 0:
            # No ATR to buffer with — 5% of the structural distance is a
            # deliberately small, purely proportional clearance.
            floors.append(beyond * 1.05)
    return max(floors) if floors else 0.0


def assess(
    *,
    entry: float,
    proposed_stop: Optional[float],
    is_long: bool,
    candles: Optional[Sequence[Sequence[float]]] = None,
    atr_value: Optional[float] = None,
    swing: Optional[float] = None,
    atr_multiple: float = MIN_ATR_MULTIPLE,
) -> Optional[StopAssessment]:
    """Where this trade's stop belongs, given how the market is actually moving.

    ``None`` when there is no entry to measure from. Otherwise the returned
    stop is always on the protective side of the entry, and is the proposed one
    whenever that already clears both floors.
    """
    if not entry or entry <= 0:
        return None

    if atr_value is None and candles:
        atr_value = atr(candles)
    if swing is None and candles:
        swing = swing_extreme(candles, is_long=is_long)

    floor = required_distance(
        entry=entry, is_long=is_long, atr_value=atr_value, swing=swing,
        atr_multiple=atr_multiple,
    )

    proposed = proposed_stop if (proposed_stop and proposed_stop > 0) else None
    # A stop on the wrong side of the entry protects nothing; treat it as absent
    # so the floor supplies a real one rather than propagating the contradiction.
    if proposed is not None:
        if (is_long and proposed >= entry) or (not is_long and proposed <= entry):
            proposed = None

    current = abs(entry - proposed) if proposed is not None else 0.0

    if floor <= 0:
        if proposed is None:
            return None
        return StopAssessment(
            stop=proposed, proposed=proposed, floor_distance=0.0, widened=False,
            verdict="ok", reason="no volatility or structure reference available",
        )

    # A floor beyond the sane ceiling means the market is too disturbed for the
    # entry as written — say so instead of publishing a stop nobody should take.
    if atr_value and floor > atr_value * MAX_ATR_MULTIPLE:
        floor = atr_value * MAX_ATR_MULTIPLE
        unsound = True
    else:
        unsound = False

    if proposed is not None and current >= floor:
        return StopAssessment(
            stop=proposed, proposed=proposed, floor_distance=floor, widened=False,
            verdict="ok",
            reason=(
                f"stop is {current:.5g} from entry, clearing the "
                f"{floor:.5g} this market justifies"
            ),
        )

    stop = entry - floor if is_long else entry + floor
    detail = (
        f"stop was {current:.5g} from entry — inside the {floor:.5g} floor "
        f"(ATR {atr_value:.5g})" if atr_value and proposed is not None
        else f"no usable stop given; placing one {floor:.5g} from entry"
    )
    return StopAssessment(
        stop=stop, proposed=proposed, floor_distance=floor, widened=True,
        verdict="unsound" if unsound else "widened",
        reason=detail + (
            " — capped at the sane ceiling; treat the setup as unsound" if unsound else ""
        ),
    )


def reward_risk(
    *, entry: float, stop: float, target: Optional[float], is_long: bool,
) -> Optional[float]:
    """R:R for the plan as written, or None when it cannot be computed."""
    if not target or not entry or not stop:
        return None
    risk = abs(entry - stop)
    reward = (target - entry) if is_long else (entry - target)
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk
