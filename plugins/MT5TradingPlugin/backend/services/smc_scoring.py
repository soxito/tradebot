"""
MT5 Trading Plugin — deterministic, auditable SMC sniper scoring.

Replaces the previous additive bag of boolean confluence bonuses with a numeric
factor model. Every signal carries a breakdown showing which factors fired, the
raw measurement behind each, the weight applied, and the resulting contribution
— so a score can always be explained and reproduced from the candles.

Three factor families (default weights sum to exactly 1.00):

  volume    0.32  relative volume, volume-at-price concentration,
                  delta imbalance, break-of-structure volume confirmation
  structure 0.42  HTF alignment, LTF bias alignment, zone quality,
                  premium/discount depth, liquidity sweep, CHoCH context
  movement  0.20  displacement magnitude, wick rejection, ATR momentum,
                  candle velocity
  risk      0.06  realised risk-reward above the configured minimum

Two hard gates, both enforced by the caller:

  * ``volume_confirmed`` — false when the zone-forming candle did not trade
    above its rolling mean volume, or when a break of structure in the trade's
    direction happened on below-threshold volume. No signal fires without it.
  * ``htf_blocked``      — true when a higher-timeframe bias is supplied and it
    opposes the trade direction. HTF bias gates LTF entries.

All comparisons across the lookback window are numeric ratios, never
qualitative labels.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ── Gates ────────────────────────────────────────────────────────────────────
#: Zone-forming candle must trade at least this multiple of the rolling mean.
VOLUME_CONFIRM_RATIO = 1.0
#: A break of structure must print at least this multiple to count as confirmed.
BOS_VOLUME_CONFIRM_RATIO = 1.15
#: Bars of history used for every rolling volume mean.
VOLUME_LOOKBACK = 20

# ── Default factor weights ───────────────────────────────────────────────────
#: The macro family was added by taking 0.05 proportionally from the other four
#: rather than appending on top, because the weights must sum to 1.00. Every
#: historical score therefore moves by roughly 5% — intended, and worth knowing
#: before comparing a score taken today against one recorded last week.
DEFAULT_WEIGHTS: Dict[str, float] = {
    # volume — 0.285 (0.30 × 0.95)
    "relative_volume": 0.114,
    "volume_at_price": 0.0665,
    "delta_imbalance": 0.057,
    "bos_volume_confirmation": 0.0475,
    # structure — 0.38 (0.40 × 0.95)
    "htf_alignment": 0.095,
    "structure_aligned": 0.0665,
    "zone_quality": 0.057,
    "premium_discount_depth": 0.057,
    "liquidity_sweep": 0.057,
    "choch_context": 0.0475,
    # movement — 0.1805 (0.19 × 0.95)
    "displacement_magnitude": 0.0665,
    "wick_rejection": 0.0475,
    "atr_momentum": 0.03325,
    "candle_velocity": 0.03325,
    # risk — 0.057 (0.06 × 0.95)
    "risk_reward": 0.057,
    # macro — 0.0475 (0.05 × 0.95)
    #
    # The dollar and the fear gauge. Small on purpose: macro is the weather a
    # setup trades in, not the setup. It is also the one family that routinely
    # does not apply — a pair with no USD leg redistributes this weight rather
    # than scoring a zero, so the instrument's currency composition never costs
    # it confidence. See app.services.macro_context.
    "macro_context": 0.0475,
    # fibonacci — 0.05
    #
    # Added the same way macro was: 0.05 taken proportionally from every other
    # family (all scaled by 0.95) rather than appended on top, so weights still
    # sum to exactly 1.00. Credit for price sitting in the auto-detected
    # ZigZag/fib golden zone (0.5-0.618 retracement). Also routinely
    # inapplicable — no confirmed swing yet on a young or choppy series —
    # in which case it redistributes rather than scoring a zero. See
    # backend/app/signals/technical.py:auto_fib_retracement for the shared
    # ZigZag deviation/depth algorithm this mirrors (ported locally below
    # since this plugin does not import from the main backend package).
    "fib_confluence": 0.05,
}

FACTOR_FAMILY: Dict[str, str] = {
    "relative_volume": "volume",
    "volume_at_price": "volume",
    "delta_imbalance": "volume",
    "bos_volume_confirmation": "volume",
    "htf_alignment": "structure",
    "structure_aligned": "structure",
    "zone_quality": "structure",
    "premium_discount_depth": "structure",
    "liquidity_sweep": "structure",
    "choch_context": "structure",
    "displacement_magnitude": "movement",
    "wick_rejection": "movement",
    "atr_momentum": "movement",
    "candle_velocity": "movement",
    "risk_reward": "risk",
    "macro_context": "macro",
    "fib_confluence": "fibonacci",
}


def _fib_confluence_score(price: float, fib: Optional[Dict[str, Any]]) -> Optional[float]:
    """0..1 credit for `price` sitting inside the fib golden zone (0.5-0.618 band).

    `fib` is the {"golden_zone": {"low", "high"}, "direction": "up"|"down"}
    shape produced by SMCStrategyEngine's ZigZag/fib pass (see smc_strategy.py).
    Ported from backend/app/signals/technical.py:fib_confluence_score — same
    math, kept local because this plugin is isolated from the main backend
    package. Returns None when inapplicable (no confirmed swing), not 0, so
    the caller can redistribute the family weight instead of scoring a zero.
    """
    if not fib:
        return None
    zone = fib.get("golden_zone")
    if not zone:
        return None
    low, high = zone.get("low"), zone.get("high")
    if low is None or high is None or high <= low:
        return None
    if price < low or price > high:
        return 0.0
    mid = (low + high) / 2.0
    half_width = (high - low) / 2.0
    if half_width <= 0:
        return 1.0
    dist = abs(price - mid) / half_width
    return round(max(0.0, 1.0 - dist), 4)

#: Confidence is capped below 1.0 — a deterministic model should never claim
#: certainty. Matches the previous engine's ceiling.
MAX_CONFIDENCE = 0.98


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _redistribute(w: Dict[str, float], family: str) -> None:
    """Move an unmeasurable family's weight onto the factors that *can* be scored.

    Mutates ``w`` in place. Without this, an instrument that simply cannot carry
    a family — spot FX with no volume feed, a cross with no dollar leg — would
    be capped below an otherwise identical setup that can. That is a penalty for
    the instrument's data or currency composition, not for the setup, and it
    makes confidence incomparable across a watchlist.
    """
    forfeited = sum(w[k] for k in w if FACTOR_FAMILY.get(k) == family)
    if forfeited <= 0:
        return
    rest = {k: v for k, v in w.items() if FACTOR_FAMILY.get(k) != family}
    rest_total = sum(rest.values())
    if rest_total > 0:
        scale = (rest_total + forfeited) / rest_total
        for k in rest:
            w[k] = w[k] * scale
    for k in w:
        if FACTOR_FAMILY.get(k) == family:
            w[k] = 0.0


@dataclass
class Factor:
    """One scored input: what was measured, how it was weighted, what it added."""
    name: str
    family: str
    raw_value: float      # the underlying measurement (ratio, multiple, count…)
    normalized: float     # raw_value mapped to [-1, 1]
    weight: float
    contribution: float   # normalized × weight — signed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreBreakdown:
    total: float                  # clamped confidence actually used, 0..0.98
    raw_total: float              # exact sum of contributions (audit trail)
    volume_confirmed: bool
    volume_data_available: bool
    htf_blocked: bool
    #: True when the instrument carries no volume series at all, so the volume
    #: gate was not applicable and confidence came from structure/movement/risk
    #: alone. Distinct from volume_confirmed=False, which means volume existed
    #: and failed the test. Callers must surface this — a structure-only setup
    #: is a weaker claim than a volume-confirmed one.
    structure_only: bool = False
    factors: List[Factor] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": round(self.total, 4),
            "raw_total": round(self.raw_total, 4),
            "volume_confirmed": self.volume_confirmed,
            "volume_data_available": self.volume_data_available,
            "structure_only": self.structure_only,
            "htf_blocked": self.htf_blocked,
            "factors": [f.to_dict() for f in self.factors],
            "by_family": self.by_family(),
        }

    def by_family(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for f in self.factors:
            out[f.family] = round(out.get(f.family, 0.0) + f.contribution, 4)
        return out


# ── Volume primitives ────────────────────────────────────────────────────────

def rolling_mean_volume(
    candles: Sequence[Any], index: int, lookback: int = VOLUME_LOOKBACK
) -> float:
    """Mean volume of the `lookback` bars immediately preceding `index`.

    Returns 0.0 when there is no usable volume history — callers must treat
    that as "volume unavailable", not as "volume confirmed".
    """
    if index <= 0:
        return 0.0
    start = max(0, index - lookback)
    vols = [
        float(c.volume) for c in candles[start:index]
        if getattr(c, "volume", 0) and float(c.volume) > 0
    ]
    if len(vols) < 3:
        return 0.0
    return sum(vols) / len(vols)


def relative_volume(
    candles: Sequence[Any], index: int, lookback: int = VOLUME_LOOKBACK
) -> float:
    """Volume of bar `index` as a numeric multiple of its rolling mean.

    1.0 means "exactly average". 0.0 means the comparison could not be made.
    """
    if index < 0 or index >= len(candles):
        return 0.0
    mean = rolling_mean_volume(candles, index, lookback)
    if mean <= 0:
        return 0.0
    vol = float(getattr(candles[index], "volume", 0.0) or 0.0)
    return vol / mean


def volume_at_price(
    candles: Sequence[Any], low: float, high: float, index: int,
    lookback: int = VOLUME_LOOKBACK * 3,
) -> float:
    """Fraction of window volume that traded inside the [low, high] band.

    A zone that absorbed a large share of recent volume is a zone real size was
    worked at, rather than a gap price passed through.
    """
    if high < low:
        low, high = high, low
    start = max(0, index - lookback)
    window = candles[start:index + 1]
    total = 0.0
    inside = 0.0
    for c in window:
        vol = float(getattr(c, "volume", 0.0) or 0.0)
        if vol <= 0:
            continue
        total += vol
        # Overlap of the bar's range with the zone band, as a fraction of the bar.
        bar_low, bar_high = float(c.low), float(c.high)
        span = bar_high - bar_low
        overlap = min(bar_high, high) - max(bar_low, low)
        if overlap <= 0:
            continue
        inside += vol * (overlap / span if span > 0 else 1.0)
    if total <= 0:
        return 0.0
    return inside / total


def delta_imbalance(
    candles: Sequence[Any], index: int, lookback: int = VOLUME_LOOKBACK
) -> float:
    """Signed buy/sell pressure over the lookback, in [-1, 1].

    Uses the standard close-position-within-range proxy: a bar closing at its
    high is fully buy-side volume, at its low fully sell-side. +1 is maximum
    buying pressure, -1 maximum selling pressure.
    """
    start = max(0, index - lookback + 1)
    window = candles[start:index + 1]
    signed = 0.0
    total = 0.0
    for c in window:
        vol = float(getattr(c, "volume", 0.0) or 0.0)
        if vol <= 0:
            continue
        rng = float(c.high) - float(c.low)
        if rng <= 0:
            continue
        bull_frac = (float(c.close) - float(c.low)) / rng
        signed += vol * (2.0 * bull_frac - 1.0)
        total += vol
    if total <= 0:
        return 0.0
    return _clamp(signed / total, -1.0, 1.0)


def relevant_structure_event(
    events: Sequence[Dict[str, Any]], zone_index: int, direction: str
) -> Optional[Dict[str, Any]]:
    """The BOS/CHoCH in `direction` that this zone's setup depends on.

    Prefers the earliest event at or after the zone formed (the displacement
    that validated the zone); falls back to the latest event before it.
    """
    same_dir = [e for e in events if e.get("direction") == direction]
    if not same_dir:
        return None
    after = [e for e in same_dir if int(e.get("index", -1)) >= zone_index]
    if after:
        return min(after, key=lambda e: int(e.get("index", 0)))
    return max(same_dir, key=lambda e: int(e.get("index", 0)))


def bos_volume_confirmed(
    candles: Sequence[Any], event_index: int,
    lookback: int = VOLUME_LOOKBACK,
    threshold: float = BOS_VOLUME_CONFIRM_RATIO,
) -> tuple[bool, float]:
    """Did the break-of-structure bar print above-threshold volume?

    Returns ``(confirmed, measured_ratio)``. A break on thin volume is the
    classic false break, so this gates the signal rather than merely scoring it.

    A ratio of exactly zero means the bar reported no volume at all. In a series
    that does carry volume that is a gap in the feed, not a thin break, and the
    gate has nothing to measure — so it abstains (``True``) rather than
    condemning the break on missing data. The ratio is still returned as 0.0, so
    the factor score gives it no credit either way.
    """
    if event_index < 0 or event_index >= len(candles):
        return False, 0.0
    ratio = relative_volume(candles, event_index, lookback)
    if ratio <= 0:
        return rolling_mean_volume(candles, event_index, lookback) > 0, 0.0
    return ratio >= threshold, ratio


# ── Movement primitives ──────────────────────────────────────────────────────

def displacement_magnitude(candles: Sequence[Any], index: int, atr: float) -> float:
    """Body size of bar `index` expressed in ATR multiples."""
    if atr <= 0 or index < 0 or index >= len(candles):
        return 0.0
    c = candles[index]
    return abs(float(c.close) - float(c.open)) / atr


def wick_rejection_ratio(candle: Any, side: str) -> float:
    """Rejection wick as a fraction of the bar's full range, in [0, 1].

    For a buy the lower wick matters (demand rejected lower prices); for a sell
    the upper wick.
    """
    high, low = float(candle.high), float(candle.low)
    rng = high - low
    if rng <= 0:
        return 0.0
    body_low = min(float(candle.open), float(candle.close))
    body_high = max(float(candle.open), float(candle.close))
    wick = (body_low - low) if side == "buy" else (high - body_high)
    return _clamp(wick / rng)


def atr_momentum(candles: Sequence[Any], atr: float, period: int = 10) -> float:
    """Net price change over `period` bars, in ATR multiples. Signed."""
    if atr <= 0 or len(candles) <= period:
        return 0.0
    return (float(candles[-1].close) - float(candles[-1 - period].close)) / atr


def candle_velocity(candles: Sequence[Any], atr: float, period: int = 5) -> float:
    """Mean body size over the last `period` bars, in ATR multiples."""
    if atr <= 0 or not candles:
        return 0.0
    window = candles[-period:]
    if not window:
        return 0.0
    return sum(abs(float(c.close) - float(c.open)) for c in window) / len(window) / atr


# ── Scoring ──────────────────────────────────────────────────────────────────

def score_signal(
    *,
    side: str,
    zone_index: int,
    zone_kind: str,
    zone_top: float,
    zone_bottom: float,
    entry: float,
    rr: float,
    min_rr: float,
    max_rr: float,
    candles: Sequence[Any],
    atr: float,
    bias: str,
    events: Sequence[Dict[str, Any]],
    sweeps: Dict[str, Any],
    equilibrium: float,
    range_low: float,
    range_high: float,
    choch_index: int = -1,
    htf_bias: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
    macro: Optional[Any] = None,
    fib: Optional[Dict[str, Any]] = None,
) -> ScoreBreakdown:
    """Score one candidate setup. Pure function of the inputs — no I/O.

    ``weights`` overrides the defaults per factor (Phase 3 recalibration feeds
    learned weights in here); unknown keys are ignored and missing keys fall
    back to ``DEFAULT_WEIGHTS``.

    ``macro`` is an ``app.services.macro_context.MacroBias`` — resolved by the
    caller, never fetched here, because this function is pure and is re-entered
    once per bar by ``SMCStrategyEngine.backtest``. ``None``, or a bias that
    reports itself inapplicable, redistributes the macro weight instead of
    scoring a zero.

    ``fib`` is ``{"golden_zone": {"low", "high"}, "direction": "up"|"down"}``
    from the caller's ZigZag/fib pass (SMCStrategyEngine.analyze computes it
    once per call, not once per candidate, same reasoning as macro). ``None``,
    or a result with no confirmed swing yet, redistributes the fib weight.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in DEFAULT_WEIGHTS})

    # Instruments with no volume series at all (spot FX with no futures proxy,
    # synthesised metal crosses) would otherwise forfeit the volume family's
    # 0.32 of the weight and score ~a third lower than an identical setup on a
    # volume-bearing instrument — a penalty for the data feed, not the setup.
    # Redistribute that weight proportionally over the factors that *can* be
    # measured, so confidence stays comparable across instruments.
    #
    # The same reasoning applies one bar at a time. A bar reporting *zero*
    # volume inside a series that otherwise reports volume is a hole in the
    # feed, not evidence that nobody traded: Yahoo's 60m gold series carries 15
    # such bars in 400 (thin hours, rollover, holidays), while its 15m series
    # has none. Scoring that as thin participation gave rel_vol = 0/mean = 0 and
    # hard-rejected the setup, which is why H1 gold could publish no setups at
    # all while M15 on the same instrument published several. Volume that was
    # never reported is unknown, so the setup falls back to structure scoring
    # and is surfaced as structure_only rather than silently binned.
    zone_volume = (
        float(getattr(candles[zone_index], "volume", 0.0) or 0.0)
        if 0 <= zone_index < len(candles) else 0.0
    )
    series_has_volume = rolling_mean_volume(candles, zone_index) > 0
    volume_gap_at_zone = series_has_volume and zone_volume <= 0
    structure_only = (not series_has_volume) or volume_gap_at_zone
    if structure_only:
        _redistribute(w, "volume")

    # The macro family is the other one that routinely cannot be measured: the
    # dollar says nothing about EURGBP, and a dead Yahoo feed says nothing about
    # anything. Same treatment, same reason — the setup is scored on what *can*
    # be measured rather than docked for what cannot.
    macro_applicable = bool(macro is not None and getattr(macro, "applicable", False))
    if not macro_applicable:
        _redistribute(w, "macro")

    # A young or choppy series with no confirmed ZigZag swing yet cannot carry
    # a fib confluence measurement — redistribute rather than score a zero.
    fib_applicable = bool(fib and fib.get("golden_zone"))
    if not fib_applicable:
        _redistribute(w, "fibonacci")

    factors: List[Factor] = []

    def add(name: str, raw: float, normalized: float) -> None:
        weight = w.get(name, 0.0)
        norm = _clamp(normalized, -1.0, 1.0)
        factors.append(Factor(
            name=name,
            family=FACTOR_FAMILY.get(name, "other"),
            raw_value=round(float(raw), 4),
            normalized=round(norm, 4),
            weight=round(weight, 4),
            contribution=round(norm * weight, 4),
        ))

    want_bull = side == "buy"

    # ── Volume ───────────────────────────────────────────────────────────────
    rel_vol = relative_volume(candles, zone_index)
    # False when the bar itself has no reported volume, even if the series does —
    # the measurement is unavailable for THIS setup, which is what callers care
    # about when deciding whether the volume gate is applicable.
    volume_data_available = series_has_volume and not volume_gap_at_zone
    # 1.0× = neutral (0.0), 2.0× or better = full credit.
    add("relative_volume", rel_vol, (rel_vol - 1.0) if rel_vol > 0 else 0.0)

    vap = volume_at_price(candles, zone_bottom, zone_top, zone_index)
    # 20% of window volume inside the zone is already high concentration.
    add("volume_at_price", vap, vap / 0.20)

    delta = delta_imbalance(candles, zone_index)
    aligned_delta = delta if want_bull else -delta
    add("delta_imbalance", delta, aligned_delta)

    # Volume confirmation on the break of structure this setup actually depends
    # on — the displacement that created or validated the zone, i.e. the event
    # in the trade's direction nearest the zone (preferring at-or-after it).
    # Anchoring to the newest break instead would let an unrelated later move
    # invalidate a perfectly good zone.
    want_dir = "bullish" if want_bull else "bearish"
    bos_event = relevant_structure_event(events, zone_index, want_dir)
    if bos_event is not None:
        bos_ok, bos_ratio = bos_volume_confirmed(candles, int(bos_event.get("index", -1)))
    else:
        bos_ok, bos_ratio = True, 0.0  # no break to confirm — not a failure
    add("bos_volume_confirmation", bos_ratio,
        (bos_ratio - 1.0) if bos_ratio > 0 else 0.0)

    # Hard gate. Requires an above-average zone candle and — when a break of
    # structure exists — that the break itself was confirmed.
    #
    # The gate catches breaks on *thin* volume, the classic false break. That
    # test needs a volume series to be thin relative to. On an instrument that
    # publishes none, applying it as a rejection bans the instrument outright
    # rather than filtering its weak setups, so the gate is not applicable and
    # the setup is marked structure_only for the caller to surface.
    volume_confirmed = bool(
        structure_only
        or (volume_data_available and rel_vol >= VOLUME_CONFIRM_RATIO and bos_ok)
    )

    # ── Structure ────────────────────────────────────────────────────────────
    htf_blocked = False
    if htf_bias in ("bullish", "bearish"):
        if htf_bias == want_dir:
            add("htf_alignment", 1.0, 1.0)
        else:
            htf_blocked = True
            add("htf_alignment", -1.0, -1.0)
    else:
        add("htf_alignment", 0.0, 0.0)  # no HTF supplied — neither helps nor hurts

    add("structure_aligned", 1.0 if bias == want_dir else 0.0,
        1.0 if bias == want_dir else 0.0)

    # Zone quality: an FVG is a pure imbalance, an OB is mitigated supply/demand.
    zq = 1.0 if "fvg" in zone_kind else (0.7 if "ob" in zone_kind else 0.3)
    add("zone_quality", zq, zq)

    # Premium/discount depth — how far past equilibrium the entry sits, 0..1.
    if want_bull and (equilibrium - range_low) > 0:
        depth = (equilibrium - entry) / (equilibrium - range_low)
    elif not want_bull and (range_high - equilibrium) > 0:
        depth = (entry - equilibrium) / (range_high - equilibrium)
    else:
        depth = 0.0
    add("premium_discount_depth", depth, depth)

    swept = bool(sweeps.get("swept_lows")) if want_bull else bool(sweeps.get("swept_highs"))
    fb_score = float(sweeps.get("false_break_score", 0.0) or 0.0)
    sweep_norm = (fb_score / 100.0) if swept else 0.0
    add("liquidity_sweep", fb_score if swept else 0.0, sweep_norm)

    # A zone formed after the most recent CHoCH is trading the new character.
    if choch_index >= 0:
        choch_norm = 1.0 if zone_index > choch_index else 0.5
    else:
        choch_norm = 0.0
    add("choch_context", float(choch_index), choch_norm)

    # Fib confluence: full credit when entry sits in the golden zone AND the
    # zone's swing direction agrees with the trade side; a partial penalty
    # (not a full flip) when the zone is there but the swing disagrees, since
    # a golden zone against the trade is still a real level, just the wrong
    # one to be leaning on.
    if fib_applicable:
        fib_conf = _fib_confluence_score(entry, fib) or 0.0
        swing_dir = fib.get("direction")
        if swing_dir in ("up", "down"):
            dir_aligned = (swing_dir == "up") == want_bull
            fib_norm = fib_conf if dir_aligned else -fib_conf * 0.5
        else:
            fib_norm = fib_conf
        add("fib_confluence", fib_conf, fib_norm)

    # ── Macro ────────────────────────────────────────────────────────────────
    # The bias arrives signed for the long side, so a short reads the same
    # dollar with the sign flipped. Skipped entirely when inapplicable — its
    # weight has already been redistributed above, and emitting a zero-weight
    # factor would put a meaningless "macro_context +0.000" row in the UI
    # breakdown and in describe().
    if macro_applicable:
        macro_norm = float(getattr(macro, "normalized", 0.0) or 0.0)
        add("macro_context", macro_norm, macro_norm if want_bull else -macro_norm)

    # ── Movement ─────────────────────────────────────────────────────────────
    disp = displacement_magnitude(candles, zone_index, atr)
    add("displacement_magnitude", disp, disp / 1.5)  # 1.5×ATR body = full credit

    zone_candle = candles[zone_index] if 0 <= zone_index < len(candles) else None
    wick = wick_rejection_ratio(zone_candle, side) if zone_candle is not None else 0.0
    add("wick_rejection", wick, wick / 0.5)  # half the bar as wick = full credit

    mom = atr_momentum(candles, atr)
    aligned_mom = mom if want_bull else -mom
    add("atr_momentum", mom, aligned_mom / 3.0)  # 3×ATR over 10 bars = full credit

    vel = candle_velocity(candles, atr)
    add("candle_velocity", vel, vel / 0.8)

    # ── Risk ─────────────────────────────────────────────────────────────────
    span = max(1e-9, max_rr - min_rr)
    rr_norm = _clamp((rr - min_rr) / span)
    add("risk_reward", rr, rr_norm)

    raw_total = sum(f.contribution for f in factors)
    total = _clamp(raw_total, 0.0, MAX_CONFIDENCE)

    return ScoreBreakdown(
        total=round(total, 4),
        raw_total=round(raw_total, 4),
        volume_confirmed=volume_confirmed,
        volume_data_available=volume_data_available,
        structure_only=structure_only,
        htf_blocked=htf_blocked,
        factors=factors,
    )


def describe(breakdown: ScoreBreakdown, limit: int = 4) -> str:
    """Human-readable reason string naming the highest-contributing factors."""
    ranked = sorted(breakdown.factors, key=lambda f: f.contribution, reverse=True)
    parts = [
        f"{f.name.replace('_', ' ')} {f.contribution:+.3f}"
        for f in ranked[:limit] if abs(f.contribution) >= 0.005
    ]
    if not parts:
        return f"score {breakdown.total:.2f} (no dominant factor)"
    return f"score {breakdown.total:.2f}: " + ", ".join(parts)
