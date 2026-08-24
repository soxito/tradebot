"""Read a live trade the way a trader watching the screen would.

The board decides once, at the moment it meets. Everything that happens to the
trade afterwards — the pullback, the sweep, the reversal — used to be handled by
one rule: the stop is where the stop is. That rule loses two different trades.

  *The trade that was right.* A gold long entered at 4501 with the stop at 4475
  was stopped out when price swept to 4451, and then ran to 4541 — through every
  target on the card. Nothing had broken; the sweep took the resting liquidity
  under an obvious swing and reversed. A stop sitting inside that pool is not
  protection, it is the target.

  *The trade that stopped being right.* The opposite case: the higher timeframe
  actually turns, and the position sits there bleeding towards a stop that is
  now just a slower way of being wrong, when closing at once was cheaper.

Telling those apart is the whole job, and it comes down to one question: **has
the structure that justified the trade actually broken, or has price only
reached through a level to take the orders resting there?** A break shows up on
the higher timeframe and in momentum. A sweep shows up as a wick through a swing
that price immediately trades back above, while the higher timeframe is untouched.

This module answers that question and nothing else — no orders, no messages, no
database. :mod:`app.agents.guardian` acts on what it says.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

#: Within this many ATR of the stop, the trade is "under threat" and worth a
#: verdict. Further away there is nothing to decide.
NEAR_STOP_ATR = 1.2

#: A stop may never be pushed further than this multiple of the risk the trade
#: was opened with. Widening is a way to survive a sweep, not a way to turn a
#: measured loss into an unmeasured one.
MAX_WIDEN_MULTIPLE = 3.0

#: Pivot detection: a swing needs this many bars either side that do not exceed
#: it. Two is the standard fractal and is what most desks draw.
PIVOT_STRENGTH = 2

#: A lower wick this many times the candle body marks a rejection — the shape a
#: sweep leaves behind and a genuine breakdown does not.
SWEEP_WICK_RATIO = 1.5


@dataclass
class GuardVerdict:
    """What to do about a trade that is currently under threat."""

    #: ``intact`` | ``sweep_risk`` | ``invalidated`` | ``working``
    verdict: str
    #: ``hold`` | ``widen_stop`` | ``advance_stop`` | ``close``
    action: str
    reasons: List[str] = field(default_factory=list)
    suggested_stop: Optional[float] = None
    #: Fraction of the position to close so that widening the stop keeps the
    #: money at risk roughly where it started. None when nothing is to be cut.
    reduce_fraction: Optional[float] = None
    confidence: float = 0.0

    def summary(self) -> str:
        return "; ".join(self.reasons) or self.verdict


# ── Primitives ───────────────────────────────────────────────────────────────

def _f(row: Sequence[float], i: int) -> float:
    return float(row[i])


def atr(candles: Sequence[Sequence[float]], period: int = 14) -> Optional[float]:
    from app.trading.stop_quality import atr as _atr
    return _atr(candles, period)


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1 - k)
    return out


def pivots(
    candles: Sequence[Sequence[float]], *, lows: bool, strength: int = PIVOT_STRENGTH,
) -> List[float]:
    """Fractal swing highs or lows, oldest first.

    A pivot low is a bar whose low is not exceeded by ``strength`` bars either
    side. Unlike "the lowest low in N bars" this finds the levels price actually
    turned at, which is where the resting liquidity sits.
    """
    idx = 3 if lows else 2
    out: List[float] = []
    rows = list(candles)
    for i in range(strength, len(rows) - strength):
        value = _f(rows[i], idx)
        window = rows[i - strength: i + strength + 1]
        hit = (
            all(value <= _f(r, idx) for r in window) if lows
            else all(value >= _f(r, idx) for r in window)
        )
        # Two bars that share the same extreme are one swing, not two. Counting
        # them twice makes a flat base look like "the same low again" and reads
        # as a structure that is going nowhere when it is simply resting.
        if hit and (not out or out[-1] != value):
            out.append(value)
    return out


def structure_bias(candles: Sequence[Sequence[float]]) -> str:
    """``bullish`` | ``bearish`` | ``neutral`` from the swing sequence and trend.

    Higher highs *and* higher lows is an uptrend; the mirror is a downtrend.
    The swing sequence alone is too easily flipped by one messy pivot inside a
    range, and a wrong reading here closes a good trade — so the price/EMA50
    relation has to agree before a direction is claimed at all. Disagreement is
    reported as ``neutral``, which is the honest answer for a market that is
    chopping, and which this module treats as "not evidence against the trade".
    """
    highs = pivots(candles, lows=False)[-3:]
    lows = pivots(candles, lows=True)[-3:]
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    swings = (
        "bullish" if (highs[-1] > highs[-2] and lows[-1] > lows[-2])
        else "bearish" if (highs[-1] < highs[-2] and lows[-1] < lows[-2])
        else "neutral"
    )

    closes = [_f(c, 4) for c in candles]
    slow = ema(closes, 50) or ema(closes, 21)
    if slow is None:
        return swings
    trend = "bullish" if closes[-1] > slow else "bearish"
    return swings if swings == trend else "neutral"


def broke_structure(
    candles: Sequence[Sequence[float]], *, is_long: bool, atr_value: Optional[float] = None,
) -> bool:
    """Has price *closed* beyond the last swing the market turned at?

    This is the difference between a break and a raid, stated as arithmetic: a
    wick through the level is liquidity being taken, whereas a close beyond it —
    with room to spare, so a tick over does not count — is the level failing.
    """
    swings = pivots(candles, lows=is_long)
    if not swings or not candles:
        return False
    level = swings[-1]
    margin = (atr_value if atr_value is not None else (atr(candles) or 0.0)) * 0.25
    close = _f(candles[-1], 4)
    return close < level - margin if is_long else close > level + margin


def momentum_bias(candles: Sequence[Sequence[float]]) -> str:
    """``bullish`` | ``bearish`` | ``neutral`` from the fast/slow EMA relation."""
    closes = [_f(c, 4) for c in candles]
    fast, slow = ema(closes, 9), ema(closes, 21)
    if fast is None or slow is None:
        return "neutral"
    spread = (fast - slow) / slow if slow else 0.0
    if spread > 0.0005:
        return "bullish"
    if spread < -0.0005:
        return "bearish"
    return "neutral"


def looks_like_sweep(
    candles: Sequence[Sequence[float]], *, is_long: bool, level: float,
) -> bool:
    """Did price reach through *level* and immediately trade back?

    The shape: a bar whose wick pierces the level while its close comes back on
    the original side, with the wick clearly longer than the body. That is stops
    being taken, not a market that has changed its mind.
    """
    for row in list(candles)[-4:]:
        o, h, l, c = _f(row, 1), _f(row, 2), _f(row, 3), _f(row, 4)
        body = abs(c - o) or 1e-9
        if is_long:
            if l < level <= c and (min(o, c) - l) / body >= SWEEP_WICK_RATIO:
                return True
        else:
            if h > level >= c and (h - max(o, c)) / body >= SWEEP_WICK_RATIO:
                return True
    return False


def protective_level(
    candles: Sequence[Sequence[float]], *, is_long: bool, atr_value: Optional[float],
) -> Optional[float]:
    """Where a stop belongs so an ordinary sweep cannot reach it.

    Beyond the last swing the higher timeframe turned at, plus a buffer — the
    level the market has to actually break for the trade to be wrong.
    """
    swings = pivots(candles, lows=is_long)
    if not swings:
        return None
    anchor = swings[-1]
    buffer = (atr_value or 0.0) * 0.5
    return anchor - buffer if is_long else anchor + buffer


# ── The verdict ──────────────────────────────────────────────────────────────

def assess(
    *,
    side: str,
    entry: float,
    stop: Optional[float],
    take_profits: Optional[Sequence[float]] = None,
    price: float,
    ltf_candles: Sequence[Sequence[float]],
    htf_candles: Sequence[Sequence[float]],
) -> GuardVerdict:
    """What has become of this trade since it was opened.

    ``ltf_candles`` are the bars the trade is being managed on (M5/M15) and
    ``htf_candles`` the ones that justified it (H1/H4). The higher timeframe
    gets the casting vote on whether the idea is still alive, because that is
    the timeframe the idea was formed on; the lower one only says how the
    current move is behaving.
    """
    is_long = str(side).lower() in {"long", "buy"}
    reasons: List[str] = []

    if not entry or not price:
        return GuardVerdict(verdict="intact", action="hold",
                            reasons=["no price to assess against"])

    atr_ltf = atr(ltf_candles) or 0.0
    atr_htf = atr(htf_candles) or atr_ltf
    htf_bias = structure_bias(htf_candles)
    ltf_mom = momentum_bias(ltf_candles)
    with_trade = "bullish" if is_long else "bearish"
    against_trade = "bearish" if is_long else "bullish"

    excursion = (price - entry) if is_long else (entry - price)
    targets = [t for t in (take_profits or []) if t]
    final_tp = (max(targets) if is_long else min(targets)) if targets else None

    # ── The idea is dead: the timeframe it was formed on has turned, and the
    # move happening now agrees. Waiting for the stop only pays more for the
    # same information.
    htf_broken = broke_structure(htf_candles, is_long=is_long, atr_value=atr_htf)
    if (
        htf_bias == against_trade
        and htf_broken
        and ltf_mom == against_trade
        and excursion < 0
    ):
        reasons.append(
            f"the higher timeframe has closed through its last swing and reads "
            f"{htf_bias} with momentum agreeing — the setup that justified this "
            "trade is gone"
        )
        return GuardVerdict(
            verdict="invalidated", action="close", reasons=reasons, confidence=0.75,
        )

    # ── The trade is working: never take it off early while the move it was
    # opened for is still running. Securing it is the only intervention.
    if excursion > 0:
        from app.trading.trailing import protective_stop

        advanced = protective_stop(
            side="buy" if is_long else "sell", entry=entry, current_price=price,
            take_profit=final_tp, current_sl=stop,
        )
        if advanced is not None:
            reasons.append(
                f"trade is {excursion:.5g} in front and {htf_bias or 'neutral'} on the "
                "higher timeframe — securing it rather than closing it"
            )
            return GuardVerdict(
                verdict="working", action="advance_stop", reasons=reasons,
                suggested_stop=advanced, confidence=0.6,
            )
        reasons.append("in profit with the move intact — holding for the ladder")
        return GuardVerdict(verdict="working", action="hold", reasons=reasons,
                            confidence=0.5)

    # ── Under threat: is the stop about to be taken by a sweep? ──────────────
    if stop is None or atr_ltf <= 0:
        return GuardVerdict(verdict="intact", action="hold",
                            reasons=["nothing to protect against yet"])

    distance = abs(price - stop)
    if distance > NEAR_STOP_ATR * atr_ltf:
        return GuardVerdict(
            verdict="intact", action="hold",
            reasons=[f"price is {distance / atr_ltf:.1f} ATR clear of the stop"],
        )

    # The higher timeframe still supports the trade — so what is happening now
    # is a raid on the liquidity under the plan, not the market changing side.
    if htf_bias == against_trade and htf_broken:
        reasons.append(
            f"price is within {distance / atr_ltf:.1f} ATR of the stop, and the "
            f"higher timeframe has closed through its last swing into a {htf_bias} "
            "structure — this is a break, not a sweep"
        )
        return GuardVerdict(verdict="invalidated", action="close", reasons=reasons,
                            confidence=0.6)

    level = protective_level(htf_candles, is_long=is_long, atr_value=atr_htf)
    if level is None:
        return GuardVerdict(verdict="intact", action="hold",
                            reasons=["no higher-timeframe swing to move the stop behind"])

    # Only ever outward, and only within the widening ceiling.
    original_risk = abs(entry - stop)
    proposed_risk = abs(entry - level)
    if proposed_risk <= original_risk:
        return GuardVerdict(
            verdict="intact", action="hold",
            reasons=["the structural stop is no further out than the one already set"],
        )
    ceiling = original_risk * MAX_WIDEN_MULTIPLE
    capped = False
    if proposed_risk > ceiling:
        proposed_risk = ceiling
        level = entry - ceiling if is_long else entry + ceiling
        capped = True

    # Cut size in the same proportion the risk grew, so surviving the sweep
    # costs the same money the trade was sized to lose in the first place.
    reduce_fraction = max(0.0, 1.0 - (original_risk / proposed_risk))

    reasons.append(
        f"stop is {distance / atr_ltf:.1f} ATR away with the higher timeframe still "
        f"{htf_bias or 'neutral'}"
    )
    if looks_like_sweep(ltf_candles, is_long=is_long, level=stop):
        reasons.append("price wicked through the level and traded straight back — a sweep")
    reasons.append(
        f"moving the stop behind the higher-timeframe swing to {level:.5g}"
        + (" (capped at the widening ceiling)" if capped else "")
        + f" and cutting {reduce_fraction:.0%} of the position to keep the risk unchanged"
    )
    return GuardVerdict(
        verdict="sweep_risk", action="widen_stop", reasons=reasons,
        suggested_stop=level, reduce_fraction=reduce_fraction or None,
        confidence=0.65,
    )
