"""
Kronos Forecast Plugin — Volume Context

Volume is a **hard precondition** for every forecast, MT5 analysis and sniper
signal in this repo. This module is the single place that answers, for one
symbol:

  • rolling 24h traded volume
  • the most recent *completed* 1h volume
  • how that 1h stacks against the trailing 24h hourly mean (ratio + z-score)
  • the resulting volume regime (DEAD / NORMAL / ELEVATED / CLIMACTIC)
  • volume-price divergence over the last N hourly bars

and, when the answer cannot be established from real data, says so explicitly
(``UNAVAILABLE`` / ``STALE`` / ``INSUFFICIENT``) so callers can return NO_TRADE
instead of guessing.

Grounding rules this module obeys:
  * Every number is derived from OHLCV rows the existing exchange / MT5 / forex
    integrations already return. Nothing is estimated, back-filled or synthesised.
  * A feed that reports zero volume (e.g. the Frankfurter FX daily series, which
    hard-codes ``0.0``) is reported as UNAVAILABLE — not as "no activity".
  * Sub-hour bars are aggregated into whole hours only when the hour is fully
    covered, so a partial hour is never compared against complete ones.

The module is intentionally dependency-light and network-agnostic: callers
inject an async OHLCV fetcher, which keeps it unit-testable and avoids a circular
import with ``forecast_service``.
"""
from __future__ import annotations

import math
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from plugins.KronosForecastPlugin.backend.schemas import VolumeContext

# ── timeframe helpers (kept local so this module has no service imports) ──────

_TF_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800,
}

HOUR_S = 3600

#: Timeframe re-fetched when the caller's own rows cannot yield whole hours
#: (i.e. the forecast runs on 4h/1d/1w bars).
REFETCH_TIMEFRAME = "1h"
#: 48 hourly bars = the 24h window plus head-room for gaps/holidays.
REFETCH_LIMIT = 48


def tf_seconds(timeframe: str) -> int:
    return _TF_SECONDS.get((timeframe or "").lower(), HOUR_S)


# ── Named thresholds (no magic constants) ────────────────────────────────────

#: Half the trailing hourly average. Below this the book is thin enough that
#: fills slip and directional calls do not hold — a liquidity drought.
DEAD_RV = 0.50
#: 1.5× the trailing hourly average. Reused deliberately from the Telegram
#: sniper's existing TA layer (``strategy_analysis.volume_spike_mult = 1.5``)
#: so "elevated volume" means the same thing everywhere in this repo.
ELEVATED_RV = 1.50
#: 2× the elevated threshold. Participation this far above normal is more often
#: a climax/blow-off than the start of a sustainable trend.
CLIMACTIC_RV = 2.50

#: Statistics are computed over whole clock hours, so in a healthy feed the most
#: recent *completed* hour is always 0-3600s old. Staleness is therefore
#: ``HOUR + tolerance``: anything older means at least one hour never arrived.
#: The tolerance is 3 bar periods (time for a few polls to have failed) with a
#: 15-minute floor so fast timeframes aren't tripped by clock skew alone.
STALE_BARS = 3
STALE_FLOOR_S = 900


def stale_after_seconds(bar_seconds: int) -> int:
    """Age (from the close of the last complete hour) at which volume is stale."""
    return HOUR_S + max(STALE_BARS * bar_seconds, STALE_FLOOR_S)

#: A full rolling day of *complete* hours is required before any of the 24h
#: statistics are reported. Anything less is INSUFFICIENT, never extrapolated.
REQUIRED_HOURS = 24

#: Hourly bars used for the volume-price divergence read. Six hours is long
#: enough for a slope to mean something and short enough to describe the move
#: currently in progress.
DIVERGENCE_HOURS = 6

#: A price move under 0.10% across the divergence window sits inside normal
#: spread/noise for a liquid pair — there is no trend to confirm or diverge from.
PRICE_NOISE_PCT = 0.10
#: A volume slope under 2% of mean volume per bar is flat within sampling noise.
VOLUME_SLOPE_NOISE = 0.02


# ── Confidence weights ───────────────────────────────────────────────────────
#
# Regime multiplier applied to the model's own agreement/dispersion score.
#   DEAD      0.55 — thin tape; the model's paths agree about a market that
#                    barely trades, so the agreement is worth roughly half.
#   NORMAL    1.00 — the baseline the model was calibrated against; no change.
#   ELEVATED  1.10 — real participation is behind the move; a modest boost, kept
#                    small so volume can never manufacture conviction on its own.
#   CLIMACTIC 0.80 — blow-off participation; the move is late, so discount it.
REGIME_WEIGHT: Dict[str, float] = {
    "DEAD": 0.55,
    "NORMAL": 1.00,
    "ELEVATED": 1.10,
    "CLIMACTIC": 0.80,
    "UNKNOWN": 1.00,
}

# Divergence multiplier keyed by (forecast direction, divergence label).
#   CONFIRMED_*  = price and volume moving together (participation behind it)
#   EXHAUSTION_* = price moving while volume fades (the move is running dry)
#
#   forecast agrees with a CONFIRMED move   → 1.15  (volume backs the call)
#   forecast rides an EXHAUSTION of its own
#     direction                             → 0.70  (rising price, falling
#                                                    volume = exhaustion)
#   forecast opposes a CONFIRMED move       → 0.80  (volume backs the other way)
#   forecast opposes an EXHAUSTION move     → 1.05  (the opposing side is
#                                                    drying up — mild support)
DIVERGENCE_WEIGHT: Dict[Tuple[str, str], float] = {
    ("up", "CONFIRMED_UP"): 1.15,
    ("up", "EXHAUSTION_UP"): 0.70,
    ("up", "CONFIRMED_DOWN"): 0.80,
    ("up", "EXHAUSTION_DOWN"): 1.05,
    ("down", "CONFIRMED_DOWN"): 1.15,
    ("down", "EXHAUSTION_DOWN"): 0.70,
    ("down", "CONFIRMED_UP"): 0.80,
    ("down", "EXHAUSTION_UP"): 1.05,
}

#: Climactic volume driving price *against* the forecast is the single worst
#: combination available — a capitulation/blow-off in the opposite direction.
#: It replaces (rather than compounds) the regime × divergence product.
CLIMACTIC_AGAINST_WEIGHT = 0.50

#: Below this final confidence the result is reported as LOW_CONFIDENCE and no
#: executable entry is emitted. 0.35 is where the volume penalty has cut the
#: model's own agreement to under half — not a level to risk capital on.
MIN_TRADEABLE_CONFIDENCE = 0.35

#: No stochastic price forecast deserves to be reported as a certainty. Unanimous
#: agreement across a handful of sampled paths measures the sampler, not the
#: market, so the reported confidence is capped strictly below 1 — otherwise an
#: ELEVATED/CONFIRMED boost on a tight sample prints "100% confidence".
MAX_CONFIDENCE = 0.95

#: Model-side weights, unchanged from the original ``_direction_confidence`` so
#: the volume layer is a pure multiplier on top of the previous behaviour.
AGREEMENT_WEIGHT = 0.60      # share of the base score from path agreement
DISPERSION_WEIGHT = 0.40     # share from the (inverted) path dispersion
DISPERSION_SCALE = 40.0      # 2.5% relative std wipes out the dispersion term


OhlcvFetcher = Callable[[str, str, int], Awaitable[Sequence[Sequence[float]]]]


# ── row → hourly aggregation ─────────────────────────────────────────────────

class _Hour:
    """One fully-covered clock hour of OHLCV."""

    __slots__ = ("start", "volume", "close", "bars")

    def __init__(self, start: int) -> None:
        self.start = start
        self.volume = 0.0
        self.close = 0.0
        self.bars = 0


def _to_hours(rows: Sequence[Sequence[float]], bar_seconds: int, now: float) -> List[_Hour]:
    """Aggregate ccxt rows ``[ts_ms, o, h, l, c, v]`` into complete clock hours.

    Only hours covered by the full expected number of bars are returned, and the
    still-running hour is dropped, so ``hours[-1]`` is always the most recent
    *completed* hour. Returns [] when the bars are longer than an hour (the
    caller then re-fetches at 1h rather than pro-rating anything).
    """
    if bar_seconds > HOUR_S:
        return []
    expected = HOUR_S // bar_seconds
    buckets: Dict[int, _Hour] = {}
    for r in rows:
        try:
            ts = int(float(r[0]) / 1000)
            close = float(r[4])
            vol = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        start = ts - (ts % HOUR_S)
        h = buckets.get(start)
        if h is None:
            h = buckets[start] = _Hour(start)
        h.volume += vol
        h.bars += 1
        h.close = close  # rows arrive chronologically; last write wins
    out = [
        h for h in sorted(buckets.values(), key=lambda x: x.start)
        # complete hour, and the hour itself has already elapsed
        if h.bars >= expected and (h.start + HOUR_S) <= now
    ]
    return out


# ── statistics ───────────────────────────────────────────────────────────────

def _z_score(current: float, prior: Sequence[float]) -> Optional[float]:
    """Population z-score of ``current`` against the ``prior`` window."""
    n = len(prior)
    if n < 2:
        return None
    mean = sum(prior) / n
    var = sum((v - mean) ** 2 for v in prior) / n
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return (current - mean) / sd


def _normalised_slope(values: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ``values`` over its index, divided by the mean.

    The result reads as "fraction of average volume gained per bar", so it is
    comparable across symbols of wildly different notional size.
    """
    n = len(values)
    if n < 3:
        return None
    mean_v = sum(values) / n
    if mean_v <= 0:
        return None
    mean_t = (n - 1) / 2.0
    num = sum((i - mean_t) * (v - mean_v) for i, v in enumerate(values))
    den = sum((i - mean_t) ** 2 for i in range(n))
    if den <= 0:
        return None
    return (num / den) / mean_v


def classify_regime(relative_volume: Optional[float]) -> str:
    """Map relative volume onto the four named regimes."""
    if relative_volume is None:
        return "UNKNOWN"
    if relative_volume < DEAD_RV:
        return "DEAD"
    if relative_volume < ELEVATED_RV:
        return "NORMAL"
    if relative_volume < CLIMACTIC_RV:
        return "ELEVATED"
    return "CLIMACTIC"


def classify_divergence(
    price_change_pct: Optional[float], volume_slope_norm: Optional[float]
) -> str:
    """Label the volume-price relationship over the divergence window.

    CONFIRMED_*  — price and volume rising together (participation behind it)
    EXHAUSTION_* — price still moving while volume fades (the move is running dry)
    NEUTRAL      — either leg is inside its noise band
    """
    if price_change_pct is None or volume_slope_norm is None:
        return "UNKNOWN"
    price_up = price_change_pct > PRICE_NOISE_PCT
    price_down = price_change_pct < -PRICE_NOISE_PCT
    vol_up = volume_slope_norm > VOLUME_SLOPE_NOISE
    vol_down = volume_slope_norm < -VOLUME_SLOPE_NOISE
    if price_up and vol_up:
        return "CONFIRMED_UP"
    if price_up and vol_down:
        return "EXHAUSTION_UP"
    if price_down and vol_up:
        return "CONFIRMED_DOWN"
    if price_down and vol_down:
        return "EXHAUSTION_DOWN"
    return "NEUTRAL"


# ── the public builders ──────────────────────────────────────────────────────

def _unavailable(symbol: str, source: str, detail: str, status: str = "UNAVAILABLE") -> VolumeContext:
    return VolumeContext(status=status, symbol=symbol, source=source, detail=detail)


def build_volume_context(
    rows: Sequence[Sequence[float]],
    *,
    symbol: str,
    timeframe: str,
    source: str,
    volume_unit: str = "base",
    now: Optional[float] = None,
    divergence_hours: int = DIVERGENCE_HOURS,
) -> VolumeContext:
    """Derive a :class:`VolumeContext` from OHLCV rows. Pure and deterministic.

    ``rows`` are ccxt-shaped ``[timestamp_ms, open, high, low, close, volume]``
    in chronological order. Returns a non-OK context (with ``detail`` saying
    exactly why) whenever the data cannot support the 24h statistics — the
    caller must treat that as NO_TRADE.
    """
    now = time.time() if now is None else now
    bar_seconds = tf_seconds(timeframe)

    if not rows:
        return _unavailable(symbol, source, f"No OHLCV rows for {symbol} ({timeframe}).")

    hours = _to_hours(rows, bar_seconds, now)
    if not hours:
        return _unavailable(
            symbol, source,
            f"{timeframe} bars cannot be aggregated into whole hours for {symbol}.",
            status="INSUFFICIENT",
        )
    if len(hours) < REQUIRED_HOURS:
        return _unavailable(
            symbol, source,
            f"Only {len(hours)} complete hour(s) of volume for {symbol} — "
            f"{REQUIRED_HOURS} required for a rolling 24h read.",
            status="INSUFFICIENT",
        )

    trailing = hours[-REQUIRED_HOURS:]
    volumes = [h.volume for h in trailing]
    volume_24h = sum(volumes)
    volume_1h = volumes[-1]
    last = trailing[-1]
    age_seconds = int(max(0.0, now - (last.start + HOUR_S)))

    # A feed that reports no volume at all carries no volume — say so rather
    # than reading "0" as a market with no activity.
    if volume_24h <= 0:
        return _unavailable(
            symbol, source,
            f"{symbol} reports zero traded volume over the last 24h — this feed "
            f"does not carry volume data.",
        )

    stale_after = stale_after_seconds(bar_seconds)
    if age_seconds > stale_after:
        return VolumeContext(
            status="STALE", symbol=symbol, source=source, volume_unit=volume_unit,
            volume_24h=volume_24h, volume_1h=volume_1h,
            hourly_mean_24h=volume_24h / REQUIRED_HOURS,
            hours_covered=len(trailing),
            last_bar_time=last.start, age_seconds=age_seconds,
            detail=(
                f"Latest complete hour for {symbol} closed {age_seconds}s ago "
                f"(stale after {stale_after}s)."
            ),
        )

    hourly_mean_24h = volume_24h / REQUIRED_HOURS
    relative_volume = volume_1h / hourly_mean_24h if hourly_mean_24h > 0 else None
    z = _z_score(volume_1h, volumes[:-1])
    regime = classify_regime(relative_volume)

    win = trailing[-max(3, min(divergence_hours, len(trailing))):]
    first_close = win[0].close
    price_change_pct = (
        (win[-1].close - first_close) / first_close * 100.0 if first_close else None
    )
    volume_slope_norm = _normalised_slope([h.volume for h in win])
    divergence = classify_divergence(price_change_pct, volume_slope_norm)

    return VolumeContext(
        status="OK", symbol=symbol, source=source, volume_unit=volume_unit,
        volume_24h=volume_24h,
        volume_1h=volume_1h,
        hourly_mean_24h=hourly_mean_24h,
        relative_volume=round(relative_volume, 4) if relative_volume is not None else None,
        z_score=round(z, 3) if z is not None else None,
        regime=regime,
        divergence=divergence,
        divergence_bars=len(win),
        price_change_pct=round(price_change_pct, 4) if price_change_pct is not None else None,
        volume_slope_norm=round(volume_slope_norm, 4) if volume_slope_norm is not None else None,
        hours_covered=len(trailing),
        last_bar_time=last.start,
        age_seconds=age_seconds,
        detail=(
            f"{regime} volume — last hour {volume_1h:,.4g} vs 24h hourly mean "
            f"{hourly_mean_24h:,.4g} (×{relative_volume:.2f}); {divergence.lower()} "
            f"over {len(win)}h."
        ) if relative_volume is not None else f"{regime} volume.",
    )


async def resolve_volume_context(
    *,
    symbol: str,
    timeframe: str,
    rows: Optional[Sequence[Sequence[float]]] = None,
    fetcher: Optional[OhlcvFetcher] = None,
    volume_unit: str = "base",
    now: Optional[float] = None,
) -> VolumeContext:
    """Resolve the volume context for ``symbol``, re-fetching 1h bars if needed.

    Preference order (cheapest first):
      1. ``rows`` the caller already holds, when their timeframe is ≤ 1h and they
         cover a full rolling day — no extra network call at all.
      2. A dedicated ``1h`` fetch through the injected ``fetcher`` (the same
         exchange/MT5/forex integrations the forecast itself uses).

    Never invents a value: if neither path yields 24 complete hours of real
    volume the returned context is UNAVAILABLE / STALE / INSUFFICIENT.
    """
    primary: Optional[VolumeContext] = None
    if rows:
        primary = build_volume_context(
            rows, symbol=symbol, timeframe=timeframe,
            source=f"ohlcv:{timeframe}", volume_unit=volume_unit, now=now,
        )
        if primary.status == "OK":
            return primary

    if fetcher is None:
        return primary or _unavailable(
            symbol, f"ohlcv:{timeframe}",
            f"No volume source available for {symbol} ({timeframe}).",
        )

    try:
        hourly_rows = await fetcher(symbol, REFETCH_TIMEFRAME, REFETCH_LIMIT)
    except Exception as exc:  # noqa: BLE001 — a failed fetch is NO_TRADE, not a crash
        logger.warning(f"[Kronos/volume] 1h volume fetch failed for {symbol}: {exc}")
        return primary or _unavailable(
            symbol, f"ohlcv:{REFETCH_TIMEFRAME}",
            f"Volume fetch failed for {symbol}: {exc}",
        )

    if not hourly_rows:
        return primary or _unavailable(
            symbol, f"ohlcv:{REFETCH_TIMEFRAME}",
            f"No 1h volume data returned for {symbol}.",
        )
    return build_volume_context(
        hourly_rows, symbol=symbol, timeframe=REFETCH_TIMEFRAME,
        source=f"ohlcv:{REFETCH_TIMEFRAME}", volume_unit=volume_unit, now=now,
    )


# ── confidence: the documented, deterministic scoring function ───────────────

def base_model_confidence(agreement: float, dispersion: float) -> float:
    """Model-only score, unchanged from the pre-volume behaviour.

        base = AGREEMENT_WEIGHT · agreement
             + DISPERSION_WEIGHT · clamp(1 − dispersion · DISPERSION_SCALE, 0, 1)

    ``agreement`` is the fraction of sampled paths ending on the same side as the
    mean; ``dispersion`` is the std of the final closes divided by the anchor
    price.
    """
    spread_term = max(0.0, min(1.0, 1.0 - dispersion * DISPERSION_SCALE))
    return max(0.0, min(1.0, AGREEMENT_WEIGHT * agreement + DISPERSION_WEIGHT * spread_term))


def volume_multiplier(direction: str, ctx: VolumeContext) -> float:
    """Volume-derived multiplier applied to the base model confidence.

        if climactic volume drives price against the forecast:
            multiplier = CLIMACTIC_AGAINST_WEIGHT            (0.50)
        else:
            multiplier = REGIME_WEIGHT[regime] · DIVERGENCE_WEIGHT[(direction, divergence)]

    Every weight is a named constant defined at the top of this module with the
    reason it holds that value. Deterministic: the same context always yields
    the same multiplier.
    """
    if ctx.status != "OK":
        return 0.0
    if is_reversal_risk(direction, ctx):
        return CLIMACTIC_AGAINST_WEIGHT
    regime_w = REGIME_WEIGHT.get(ctx.regime, 1.0)
    div_w = DIVERGENCE_WEIGHT.get((direction, ctx.divergence), 1.0)
    return regime_w * div_w


def is_reversal_risk(direction: str, ctx: VolumeContext) -> bool:
    """True when climactic volume is confirming a move *against* the forecast."""
    if ctx.regime != "CLIMACTIC":
        return False
    if direction == "up":
        return ctx.divergence == "CONFIRMED_DOWN"
    if direction == "down":
        return ctx.divergence == "CONFIRMED_UP"
    return False


def score_confidence(
    direction: str, agreement: float, dispersion: float, ctx: VolumeContext
) -> float:
    """Final confidence.

        confidence = clamp( base_model_confidence(agreement, dispersion)
                            × volume_multiplier(direction, context),
                            0, MAX_CONFIDENCE )

    With a non-OK context the multiplier is 0 — a forecast without resolved
    volume has no confidence at all, which is what forces the NO_TRADE branch.
    The upper clamp is MAX_CONFIDENCE, not 1.0: volume may sharpen a call but it
    can never turn a sampled forecast into a certainty.
    """
    return max(0.0, min(MAX_CONFIDENCE, base_model_confidence(agreement, dispersion)
                        * volume_multiplier(direction, ctx)))


def decide(confidence: float, ctx: VolumeContext) -> str:
    """Map a scored forecast onto OK / LOW_CONFIDENCE / NO_TRADE."""
    if ctx.status != "OK":
        return "NO_TRADE"
    if confidence < MIN_TRADEABLE_CONFIDENCE:
        return "LOW_CONFIDENCE"
    return "OK"


# ── human-readable evidence ──────────────────────────────────────────────────

def _fmt_vol(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.2f}"


#: How each unit is named in user-facing evidence. Spot FX has no volume of its
#: own, so "CME futures volume" says whose activity the number actually measures
#: rather than implying the pair itself printed it.
UNIT_LABEL: Dict[str, str] = {
    "tick": "tick volume",
    "futures": "CME futures volume",
    "base": "volume",
    "unknown": "volume",
}


def volume_evidence_lines(ctx: VolumeContext) -> List[str]:
    """Plain-language volume evidence, safe to show for any status."""
    unit = UNIT_LABEL.get(ctx.volume_unit, "volume")
    if ctx.status != "OK":
        return [f"Volume {ctx.status.lower()}: {ctx.detail}"]
    lines = [
        f"24h {unit} {_fmt_vol(ctx.volume_24h)}; last completed hour "
        f"{_fmt_vol(ctx.volume_1h)} (hourly mean {_fmt_vol(ctx.hourly_mean_24h)}).",
        f"Relative volume ×{ctx.relative_volume:.2f}"
        + (f", z-score {ctx.z_score:+.2f}" if ctx.z_score is not None else "")
        + f" → {ctx.regime} regime.",
    ]
    if ctx.divergence != "NEUTRAL" and ctx.price_change_pct is not None:
        lines.append(
            f"Over the last {ctx.divergence_bars}h price moved "
            f"{ctx.price_change_pct:+.2f}% while volume trended "
            f"{ctx.volume_slope_norm:+.2%}/h → {ctx.divergence}."
        )
    return lines


def direction_rationale(
    direction: str,
    ctx: VolumeContext,
    *,
    agreement: float,
    dispersion: float,
    confidence: float,
    decision: str,
) -> List[str]:
    """Why this direction was chosen, and what volume did to its confidence.

    Every emitted signal carries these lines so no call is unexplained.
    """
    if ctx.status != "OK":
        return [
            f"NO_TRADE — volume context could not be established ({ctx.status}).",
            ctx.detail,
            "A direction is never inferred from price alone.",
        ]

    base = base_model_confidence(agreement, dispersion)
    mult = volume_multiplier(direction, ctx)
    lines = list(volume_evidence_lines(ctx))
    lines.insert(
        0,
        f"Model paths: {agreement * 100:.0f}% agree on '{direction}', dispersion "
        f"{dispersion * 100:.2f}% of price → base confidence {base:.2f}.",
    )

    if is_reversal_risk(direction, ctx):
        lines.append(
            f"REVERSAL RISK: climactic volume is confirming the opposite move — "
            f"confidence forced to {CLIMACTIC_AGAINST_WEIGHT:.2f}× base."
        )
    else:
        lines.append(
            f"Volume adjustment: {ctx.regime} regime ×"
            f"{REGIME_WEIGHT.get(ctx.regime, 1.0):.2f} and {ctx.divergence} ×"
            f"{DIVERGENCE_WEIGHT.get((direction, ctx.divergence), 1.0):.2f} = "
            f"×{mult:.2f}."
        )

    lines.append(f"Final confidence {base:.2f} × {mult:.2f} = {confidence:.2f} → {decision}.")
    if decision == "LOW_CONFIDENCE":
        lines.append(
            f"Below the {MIN_TRADEABLE_CONFIDENCE:.2f} tradeable floor — no entry emitted."
        )
    return lines
