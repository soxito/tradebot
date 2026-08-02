"""
MT5 Trading Plugin — Smart Money Concepts (SMC) Sniper Strategy Engine.

Pure-Python (stdlib only) implementation of an institutional-style "sniper"
entry model. Designed for high-probability, low-frequency limit entries on
trending instruments such as XAUUSD (gold), FX majors and indices.

The engine is intentionally self-contained and deterministic so it can be:
  • run live to produce pending limit setups + drawable zones, and
  • backtested over historical candles to estimate edge.

Concept summary (all computed from OHLCV, no external data):
  • Swing structure   — fractal pivot highs/lows.
  • Market structure  — BOS (break of structure) / CHoCH (change of character)
                        → directional bias.
  • Dealing range     — premium/discount via fib of the active swing range.
  • Order blocks      — last opposing candle before a displacement leg.
  • Fair value gaps   — 3-candle imbalance (institutional inefficiency).
  • Liquidity         — equal highs/lows + stop-hunt sweeps.
  • Volume / movement — ATR-scaled displacement + volume z-score ("smart money").

Entries are limit orders placed into a discount (longs) / premium (shorts) zone
that is confluent with structure, with SL beyond the protected low/high and TP
at the next opposing liquidity pool, filtered by a minimum reward:risk.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from plugins.MT5TradingPlugin.backend.services import smc_scoring


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Swing:
    index: int
    time: int
    price: float
    kind: str  # "high" | "low"


@dataclass
class Zone:
    """An order block or fair-value-gap zone used for entries / drawing."""
    kind: str          # "bullish_ob" | "bearish_ob" | "bullish_fvg" | "bearish_fvg"
    top: float
    bottom: float
    index: int         # bar index where the zone formed
    time: int

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class Signal:
    side: str                 # "buy" | "sell"
    order_type: str           # "buy_limit" | "sell_limit"
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    confidence: float         # 0..1
    reason: str
    zone_kind: str
    formed_index: int
    formed_time: int
    confluence: List[str] = field(default_factory=list)
    # Laddered take-profits (TP1/TP2/TP3) for scale-out management.
    tp1: float = 0.0          # 1R / nearest liquidity
    tp2: float = 0.0          # 2R / mid liquidity (== take_profit primary)
    tp3: float = 0.0          # 3R / extended liquidity
    in_us_session: bool = False  # zone formed during the US (New York) session
    # Balance-aware position sizing (populated when an account balance is given).
    lot: float = 0.0          # suggested lot size for the configured risk
    risk_amount: float = 0.0  # account-currency amount risked to the stop
    risk_pct: float = 0.0     # % of balance risked
    reward_amount: float = 0.0  # account-currency profit if TP is hit
    risk_exceeds_cap: bool = False  # min lot risks more than the max-loss budget
    # Pip / point geometry — MT5-accurate (populated in _build_signal).
    point_size: float = 0.0   # smallest price increment for the symbol
    pip_size: float = 0.0     # 1 pip = 10 points (conventional fractional-pip)
    contract_size: float = 0.0  # units per 1.0 lot (for currency P&L)
    sl_points: float = 0.0    # |entry-SL| / point_size  → MT5 'points'
    tp_points: float = 0.0    # |TP-entry| / point_size
    sl_pips: float = 0.0      # |entry-SL| / pip_size
    tp_pips: float = 0.0      # |TP-entry| / pip_size
    pip_value: float = 0.0    # account-currency value of 1 pip at `lot`
    # Numeric audit trail for `confidence` — which factors contributed and by
    # how much. See services/smc_scoring.py.
    score_breakdown: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range over the most recent `period` bars."""
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def _rsi(candles: List[Candle], period: int = 14) -> float:
    """Wilder's RSI of the latest bar (0..100)."""
    if len(candles) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = candles[i].close - candles[i - 1].close
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(candles)):
        diff = candles[i].close - candles[i - 1].close
        g = diff if diff > 0 else 0.0
        l = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _volume_zscore(candles: List[Candle], lookback: int = 20) -> float:
    """Z-score of the latest bar's volume vs the prior `lookback` bars."""
    vols = [c.volume for c in candles[-(lookback + 1):-1] if c.volume]
    if len(vols) < 5:
        return 0.0
    mean = sum(vols) / len(vols)
    var = sum((v - mean) ** 2 for v in vols) / len(vols)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return (candles[-1].volume - mean) / std


# ── Structure detection ────────────────────────────────────────────────────────

def _swings(candles: List[Candle], left: int = 2, right: int = 2) -> List[Swing]:
    """Fractal swing highs/lows (confirmed `right` bars after the pivot)."""
    out: List[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i].high
        lo = candles[i].low
        is_high = True
        is_low = True
        for j in range(1, left + 1):
            if candles[i - j].high >= hi or candles[i + j].high >= hi:
                is_high = False
            if candles[i - j].low <= lo or candles[i + j].low <= lo:
                is_low = False
        if is_high:
            out.append(Swing(i, candles[i].time, hi, "high"))
        if is_low:
            out.append(Swing(i, candles[i].time, lo, "low"))
    out.sort(key=lambda s: s.index)
    return out


def _structure_bias(candles: List[Candle], swings: List[Swing]) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Walk swings and close prices to detect BOS / CHoCH and infer current bias.

    Returns (bias, events) where bias ∈ {"bullish","bearish","neutral"}.

    CHoCH events carry an extra key:
      "protected_low"  (bullish CHoCH) — the last swing low that was being
                       held when the character changed; this is the institutional
                       'line in the sand' below which the reversal is invalidated.
      "protected_high" (bearish CHoCH) — mirror for short-side setups.
    """
    events: List[Dict[str, Any]] = []
    bias = "neutral"
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None

    # Index swings by bar for chronological close-break checks.
    swing_at: Dict[int, List[Swing]] = {}
    for s in swings:
        swing_at.setdefault(s.index, []).append(s)

    for i in range(len(candles)):
        close = candles[i].close
        # Register any swing confirmed at this bar.
        for s in swing_at.get(i, []):
            if s.kind == "high":
                last_high = s
            else:
                last_low = s
        # Break detection on close.
        if last_high and close > last_high.price:
            ev = "BOS" if bias == "bullish" else "CHoCH"
            if bias != "bullish":
                bias = "bullish"
            evt: Dict[str, Any] = {
                "index": i, "time": candles[i].time, "type": ev,
                "direction": "bullish", "level": last_high.price,
            }
            # For a bullish CHoCH record the swing low that was held during the
            # reversal — this is the key level that preceded the BOS and must
            # not be violated for the setup to remain valid.
            if ev == "CHoCH" and last_low is not None:
                evt["protected_low"] = last_low.price
                evt["protected_low_index"] = last_low.index
            events.append(evt)
            last_high = None  # consumed
        if last_low and close < last_low.price:
            ev = "BOS" if bias == "bearish" else "CHoCH"
            if bias != "bearish":
                bias = "bearish"
            evt = {
                "index": i, "time": candles[i].time, "type": ev,
                "direction": "bearish", "level": last_low.price,
            }
            if ev == "CHoCH" and last_high is not None:
                evt["protected_high"] = last_high.price
                evt["protected_high_index"] = last_high.index
            events.append(evt)
            last_low = None
    return bias, events


# ── Zone detection (order blocks + fair value gaps) ─────────────────────────────

def _fair_value_gaps(candles: List[Candle], atr: float) -> List[Zone]:
    """
    High-quality 3-candle imbalance zones.

    Filters applied (in order) to remove noise and keep only institutionally
    significant FVGs worth using as entry targets:

    1. Gap size   >= 0.40 × ATR  — weak micro-gaps are excluded.
    2. Body size  >= 0.65 × ATR  — the displacement (middle) candle must show
                                   real conviction, not a doji or small candle.
    3. Volume     >= 1.30 × avg  — the displacement candle must be above-average
                                   volume; skipped when volume data is unavailable.

    Only FVGs passing all three gates are returned, giving the sniper a clean
    set of high-probability imbalance zones and eliminating chart noise.
    """
    out: List[Zone] = []
    min_gap  = atr * 0.40   # raised from 0.15: only significant imbalances
    min_body = atr * 0.65   # displacement candle must show conviction

    # Rolling average volume for institutional activity check.
    vols = [c.volume for c in candles if c.volume and c.volume > 0]
    avg_vol = (sum(vols) / len(vols)) if vols else 0.0
    vol_threshold = avg_vol * 1.30  # 30 % above average = meaningful flow

    for i in range(1, len(candles) - 1):
        prev, mid, nxt = candles[i - 1], candles[i], candles[i + 1]

        # Gate 2: displacement body filter (same for both bullish and bearish)
        body = abs(mid.close - mid.open)
        if body < min_body:
            continue

        # Gate 3: volume filter (only applied when volume data is present)
        if avg_vol > 0 and mid.volume and mid.volume > 0:
            if mid.volume < vol_threshold:
                continue

        # Gate 1 — Bullish FVG: gap between prev.high and next.low
        if nxt.low > prev.high and (nxt.low - prev.high) >= min_gap:
            out.append(Zone("bullish_fvg", top=nxt.low, bottom=prev.high,
                            index=i, time=mid.time))

        # Gate 1 — Bearish FVG: gap between prev.low and next.high
        if nxt.high < prev.low and (prev.low - nxt.high) >= min_gap:
            out.append(Zone("bearish_fvg", top=prev.low, bottom=nxt.high,
                            index=i, time=mid.time))

    return out


def _order_blocks(candles: List[Candle], atr: float) -> List[Zone]:
    """
    Order block = last opposing candle before a displacement leg.

    Bullish OB: a down candle immediately followed by a strong up displacement
    (body > 1.0*ATR) that closes above the down candle's high.
    Bearish OB: mirror.
    """
    out: List[Zone] = []
    disp = atr * 1.0
    for i in range(1, len(candles) - 1):
        c = candles[i]
        nxt = candles[i + 1]
        nxt_body = abs(nxt.close - nxt.open)
        # Bullish OB
        if c.close < c.open and nxt.close > nxt.open and nxt_body >= disp and nxt.close > c.high:
            out.append(Zone("bullish_ob", top=c.high, bottom=c.low,
                            index=i, time=c.time))
        # Bearish OB
        if c.close > c.open and nxt.close < nxt.open and nxt_body >= disp and nxt.close < c.low:
            out.append(Zone("bearish_ob", top=c.high, bottom=c.low,
                            index=i, time=c.time))
    return out


def _liquidity_pools(swings: List[Swing], atr: float) -> Dict[str, List[float]]:
    """Equal-highs (buyside) / equal-lows (sellside) liquidity within ATR tolerance."""
    tol = atr * 0.25
    highs = sorted([s.price for s in swings if s.kind == "high"])
    lows = sorted([s.price for s in swings if s.kind == "low"])

    def cluster(levels: List[float]) -> List[float]:
        pools: List[float] = []
        i = 0
        while i < len(levels):
            j = i
            group = [levels[i]]
            while j + 1 < len(levels) and (levels[j + 1] - levels[i]) <= tol:
                j += 1
                group.append(levels[j])
            if len(group) >= 2:  # at least an equal pair = engineered liquidity
                pools.append(sum(group) / len(group))
            i = j + 1
        return pools

    return {"buyside": cluster(highs), "sellside": cluster(lows)}


# ── False breakout / stop-hunt detection ──────────────────────────────────────

def _detect_liquidity_sweeps(
    candles: List[Candle],
    swings: List[Swing],
    atr: float,
    lookback: int = 5,
) -> Dict[str, Any]:
    """
    Detect liquidity sweeps (stop hunts) and false breakouts in recent bars.

    A liquidity sweep occurs when price briefly violates a swing high/low (hunting
    stops placed beyond those levels) but then closes back inside — a classic
    institutional manipulation pattern that often precedes a sharp reversal.

    Returns a dict with:
      swept_highs     — list of swing-high prices that were swept recently
      swept_lows      — list of swing-low prices that were swept recently
      is_sweep_bar    — True if the LAST bar swept a swing level
      sweep_direction — "up" (swept above swing high) | "down" | None
      rejection_wick  — 0–1 ratio of the dominant wick on the sweep bar
      volume_on_sweep — volume ratio vs 20-bar average (< 0.8 = low-volume sweep)
      false_break_score — 0–100: how likely the last move was a false breakout
    """
    result: Dict[str, Any] = {
        "swept_highs": [], "swept_lows": [], "is_sweep_bar": False,
        "sweep_direction": None, "rejection_wick": 0.0,
        "volume_on_sweep": 1.0, "false_break_score": 0.0,
    }
    if len(candles) < 10 or not swings or atr <= 0:
        return result

    swing_highs = sorted([s.price for s in swings if s.kind == "high"])
    swing_lows  = sorted([s.price for s in swings if s.kind == "low"])

    # Volume baseline
    vols = [c.volume for c in candles[-(20 + 1):-1] if c.volume and c.volume > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0.0

    # Scan the most recent bars for sweep patterns
    recent = candles[-lookback:]
    tol = atr * 0.3   # within 30% of ATR = "touching" the level

    swept_h: List[float] = []
    swept_l: List[float] = []
    score = 0.0

    for bar in recent:
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            continue
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low

        # Sweep above a swing high (close back below it)
        for sh in swing_highs:
            if bar.high > sh + tol and bar.close < sh:
                swept_h.append(round(sh, 6))
                score += 30.0

        # Sweep below a swing low (close back above it)
        for sl_lvl in swing_lows:
            if bar.low < sl_lvl - tol and bar.close > sl_lvl:
                swept_l.append(round(sl_lvl, 6))
                score += 30.0

    result["swept_highs"] = list(dict.fromkeys(swept_h))   # dedup
    result["swept_lows"]  = list(dict.fromkeys(swept_l))

    # Analyse the very last bar specifically
    last = candles[-1]
    last_range = last.high - last.low
    if last_range > 0:
        upper = last.high - max(last.open, last.close)
        lower = min(last.open, last.close) - last.low
        dom_wick = max(upper, lower)
        result["rejection_wick"] = round(dom_wick / last_range, 3)

        # Volume ratio on last bar
        if avg_vol > 0:
            result["volume_on_sweep"] = round((last.volume or 0.0) / avg_vol, 2)

        # Last bar is the sweep bar?
        is_up_sweep   = any(last.high > sh + tol and last.close < sh for sh in swing_highs)
        is_down_sweep = any(last.low  < sl - tol  and last.close > sl for sl in swing_lows)
        result["is_sweep_bar"] = is_up_sweep or is_down_sweep
        if is_up_sweep:
            result["sweep_direction"] = "up"
            score += 20.0
        elif is_down_sweep:
            result["sweep_direction"] = "down"
            score += 20.0

        # Rejection wick adds evidence
        if result["rejection_wick"] > 0.55:
            score += 15.0
        # Low volume on the sweep amplifies false-break probability
        if result["volume_on_sweep"] < 0.8:
            score += 10.0

    result["false_break_score"] = round(min(score, 100.0), 1)
    return result


def _wicked_into_zone(zone: "Zone", candles: List[Candle], atr: float) -> bool:
    """
    Return True when recent bars tested `zone` with a wick but NOT with a close.

    This is a high-probability mitigation signal: institutions pushed into the
    zone to fill orders but did not close inside it — price rejection is likely.
    """
    if not candles or atr <= 0:
        return False
    recent = candles[-8:]
    for bar in recent:
        # Wick enters the zone but close stays outside
        wick_entered = bar.low <= zone.top and bar.high >= zone.bottom
        close_inside = zone.bottom <= bar.close <= zone.top
        if wick_entered and not close_inside:
            return True
    return False




def _dealing_range(swings: List[Swing]) -> Optional[Tuple[float, float]]:
    """Most recent (low, high) swing pair forming the active dealing range."""
    last_high = next((s for s in reversed(swings) if s.kind == "high"), None)
    last_low = next((s for s in reversed(swings) if s.kind == "low"), None)
    if not last_high or not last_low:
        return None
    hi = max(last_high.price, last_low.price)
    lo = min(last_high.price, last_low.price)
    if hi <= lo:
        return None
    return lo, hi


# ── Chart-display zone selector ────────────────────────────────────────────────

def _macro_payload(macro: Optional[Any]) -> Dict[str, Any]:
    """Serialise the macro bias for the API, including when it did not apply.

    Reported either way on purpose: "the dollar was not consulted for this pair,
    and here is why" is information the trader needs, and silence reads as an
    omission rather than a decision.
    """
    if macro is None:
        return {
            "applied": False,
            "reason": "macro context was not consulted for this analysis",
            "lines": [],
        }
    return {
        "applied": bool(getattr(macro, "applicable", False)),
        "normalized": round(float(getattr(macro, "normalized", 0.0) or 0.0), 4),
        "regime": getattr(macro, "regime", "UNKNOWN"),
        "usd_leg": getattr(macro, "usd_leg", "none"),
        "reason": getattr(macro, "reason", ""),
        "lines": list(getattr(macro, "lines", []) or []),
    }


def _best_zones_for_chart(zones: List[Zone]) -> List[Dict[str, Any]]:
    """
    Return the minimal, highest-quality subset of zones for chart drawing.

    Rules (applied independently for FVGs and OBs):
    • Order Blocks  — keep all (rare; each is significant).
    • Bullish FVGs  — keep the 4 largest gaps (strongest imbalances only).
    • Bearish FVGs  — keep the 4 largest gaps (strongest imbalances only).

    Zones are sorted most-recent-first so the chart shows the freshest
    levels prominently even within the capped set.
    """
    def to_dict(z: Zone) -> Dict[str, Any]:
        return {"kind": z.kind, "top": round(z.top, 6), "bottom": round(z.bottom, 6),
                "time": z.time, "index": z.index}

    obs       = [z for z in zones if "ob"  in z.kind]
    bull_fvgs = sorted(
        [z for z in zones if z.kind == "bullish_fvg"],
        key=lambda z: z.top - z.bottom,   # largest gap first
        reverse=True,
    )[:4]
    bear_fvgs = sorted(
        [z for z in zones if z.kind == "bearish_fvg"],
        key=lambda z: z.top - z.bottom,
        reverse=True,
    )[:4]

    display = sorted(obs + bull_fvgs + bear_fvgs, key=lambda z: z.index, reverse=True)
    return [to_dict(z) for z in display]


# ── Engine ──────────────────────────────────────────────────────────────────────

def contract_size_for_symbol(symbol: str) -> float:
    """
    Best-effort contract size (units per 1.0 lot) for currency P&L sizing.

    Used so the backtest can translate price distance into account-currency risk.
    Falls back to 100 (metals/index style) for anything unrecognised.
    """
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU"):            # gold: 100 oz / lot
        return 100.0
    if s.startswith("XAG"):            # silver: 5000 oz / lot
        return 5000.0
    if any(s.startswith(c) for c in ("BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE")):
        return 1.0                      # crypto CFDs: ~1 unit / lot
    # FX majors / minors: 100,000 units (standard lot)
    if len(s) == 6 and s.isalpha():
        return 100000.0
    return 100.0


def point_size_for_symbol(symbol: str) -> float:
    """
    Smallest price increment ("1 point" in MT5 terms) for an instrument.

    This is the exact unit MT5 prints on the order ticket. Example from a live
    XAUUSD ticket: a take-profit 153.02 in price away from entry is shown as
    15302 points (153.02 / 0.01). Matching this keeps the UI 1:1 with MT5.
    """
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU"):            # gold quoted to 2 dp → point 0.01
        return 0.01
    if s.startswith("XAG"):            # silver quoted to 3 dp → point 0.001
        return 0.001
    if any(s.startswith(c) for c in ("BTC", "ETH", "BNB", "SOL", "XRP",
                                     "ADA", "DOGE", "LTC", "AVAX", "LINK")):
        return 0.01                     # crypto CFDs quoted to 2 dp
    if s.endswith("JPY"):               # JPY FX pairs quoted to 3 dp → point 0.001
        return 0.001
    if len(s) == 6 and s.isalpha():     # FX 5-digit broker → point 0.00001
        return 0.00001
    return 0.01                         # generic fallback


def pip_size_for_symbol(symbol: str) -> float:
    """
    Size of 1 'pip' = 10 points (the conventional fractional-pip relationship MT5
    uses for 3/5-digit pricing). For gold this makes 1 pip = 0.10 (10 × 0.01),
    so a 153.02 move = 1530.2 pips = 15302 points.
    """
    return point_size_for_symbol(symbol) * 10.0


class SMCStrategyEngine:
    """Smart Money Concepts sniper engine. Stateless — pass candles per call."""

    def __init__(
        self,
        swing_left: int = 2,
        swing_right: int = 2,
        min_rr: float = 1.5,
        max_rr: float = 3.0,
        sl_buffer_atr: float = 1.0,
        max_signals: int = 6,
        min_confidence: float = 0.6,
        symbol: str = "",                # instrument — drives pip/point geometry
        # ── Portfolio / position-sizing ───────────────────────────────────────
        account_balance: float = 0.0,   # 0 disables sizing (no lot/risk output)
        risk_per_trade_pct: float = 1.0, # % of balance risked per trade
        contract_size: float = 100.0,    # units per 1.0 lot (XAU=100, FX major=100000)
        recovery_enabled: bool = True,    # scale risk up after losses to recover
        max_risk_multiplier: float = 3.0, # cap on recovery risk vs base risk
        max_total_loss: float = 0.0,      # hard account-currency loss floor (0=off)
        daily_profit_target_pct: float = 0.0,  # daily profit goal as % of balance (0=off)
        us_session_only: bool = False,    # only take setups formed in the US session
        require_structure: bool = True,   # require OB/FVG + BOS/CHoCH/strong-weak backing
        # Learned factor weights from realised outcomes (Phase 3). None = the
        # defaults in smc_scoring.DEFAULT_WEIGHTS.
        factor_weights: Optional[Dict[str, float]] = None,
    ) -> None:
        self.swing_left = swing_left
        self.swing_right = swing_right
        # Reward must always be able to exceed risk. min_rr is the floor (a signal
        # is rejected if its reward:risk is below it); max_rr caps how far TP may
        # extend. Clamp max_rr up to min_rr so the cap can never force reward
        # below the floor (which would make reward < risk).
        self.min_rr = max(min_rr, 1.0)
        self.max_rr = max(max_rr, self.min_rr)
        self.sl_buffer_atr = sl_buffer_atr
        self.max_signals = max_signals
        self.min_confidence = min_confidence
        self.symbol = symbol
        self.point_size = point_size_for_symbol(symbol)
        self.pip_size = pip_size_for_symbol(symbol)
        self.account_balance = account_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.contract_size = contract_size
        self.recovery_enabled = recovery_enabled
        self.max_risk_multiplier = max_risk_multiplier
        self.max_total_loss = max_total_loss
        self.daily_profit_target_pct = daily_profit_target_pct
        self.us_session_only = us_session_only
        self.require_structure = require_structure
        self.factor_weights = factor_weights
        # Gate rejections for the current analyse() call. Initialised here as
        # well as per-run because backtest() reaches _build_signal directly.
        self._rejected: List[Dict[str, Any]] = []
        # Higher daily target → more aggressive: surface more setups and relax the
        # confidence gate so more entries are generated to chase the target.
        # (50% → selective/3 signals … 1000% → aggressive/14 signals.)
        if daily_profit_target_pct and daily_profit_target_pct > 0:
            ms, mc = self._selectivity_for_target(daily_profit_target_pct)
            self.max_signals = max(self.max_signals, ms)
            self.min_confidence = min(self.min_confidence, mc)

    # -- daily-target → selectivity --------------------------------------------

    @staticmethod
    def _selectivity_for_target(daily_pct: float) -> Tuple[int, float]:
        """
        Map a daily profit target (%) to (max_signals, min_confidence).

        A bigger target needs more opportunities, so we surface more setups and
        relax the confidence gate. Smaller targets stay highly selective.
        """
        # (target_pct_ceiling, max_signals, min_confidence)
        ladder = [
            (50,   3, 0.72),
            (100,  4, 0.68),
            (200,  6, 0.62),
            (300,  8, 0.58),
            (500,  10, 0.54),
            (750,  12, 0.50),
            (1000, 14, 0.46),
        ]
        for ceiling, ms, mc in ladder:
            if daily_pct <= ceiling:
                return ms, mc
        return 16, 0.44

    # -- US (New York) session helpers -----------------------------------------

    @staticmethod
    def _in_us_session(ts: int) -> bool:
        """
        True when a UTC timestamp falls in the US (New York) session.

        Cash/gold US session ≈ 13:30–20:00 UTC (covers the NY equity open at
        09:30 ET through the close, the highest-liquidity window of the day).
        """
        import datetime as _dt
        t = _dt.datetime.utcfromtimestamp(ts)
        minutes = t.hour * 60 + t.minute
        return (13 * 60 + 30) <= minutes < (20 * 60)

    def _us_session_open(self, candles: List[Candle]) -> Optional[Candle]:
        """The first US-session candle of the most recent trading day in `candles`."""
        import datetime as _dt
        if not candles:
            return None
        last_day = _dt.datetime.utcfromtimestamp(candles[-1].time).date()
        for c in candles:
            d = _dt.datetime.utcfromtimestamp(c.time).date()
            if d == last_day and self._in_us_session(c.time):
                return c
        return None

    # -- position sizing --------------------------------------------------------

    def _position_size(self, entry: float, stop: float, balance: float,
                       risk_multiplier: float = 1.0) -> Tuple[float, float]:
        """
        Return (lot, risk_amount) for a trade given the balance and SL distance.

        risk_amount = balance × risk_pct × risk_multiplier   (account currency)
        lot         = risk_amount / (sl_distance × contract_size)

        The lot places exactly `risk_amount` at risk if price reaches the stop.
        risk_multiplier > 1 is used by the recovery engine after losses.
        """
        if balance <= 0:
            return 0.0, 0.0
        risk_amount = balance * (self.risk_per_trade_pct / 100.0) * risk_multiplier
        sl_dist = abs(entry - stop)
        if sl_dist <= 0 or self.contract_size <= 0:
            return 0.0, round(risk_amount, 2)
        lot = risk_amount / (sl_dist * self.contract_size)
        lot = max(0.01, round(lot, 2))
        # Recompute the *actual* currency risk from the rounded/clamped lot so the
        # figure shown to the user is what the position would really lose at the
        # stop (the broker minimum 0.01 lot can risk more than the % budget on a
        # small account — being honest about this matters for risk management).
        actual_risk = round(lot * sl_dist * self.contract_size, 2)
        return lot, actual_risk

    # -- shared analysis primitives --------------------------------------------

    def _primitives(self, candles: List[Candle]) -> Dict[str, Any]:
        atr = _atr(candles)
        swings = _swings(candles, self.swing_left, self.swing_right)
        bias, events = _structure_bias(candles, swings)
        obs = _order_blocks(candles, atr)
        fvgs = _fair_value_gaps(candles, atr)
        liquidity = _liquidity_pools(swings, atr)
        rng = _dealing_range(swings)

        # ── CHoCH context ─────────────────────────────────────────────────────
        recent_bull_choch = next(
            (e for e in reversed(events)
             if e.get("type") == "CHoCH" and e.get("direction") == "bullish"),
            None,
        )
        recent_bear_choch = next(
            (e for e in reversed(events)
             if e.get("type") == "CHoCH" and e.get("direction") == "bearish"),
            None,
        )

        # ── False breakout / stop-hunt context ────────────────────────────────
        sweeps = _detect_liquidity_sweeps(candles, swings, atr)

        return {
            "atr": atr, "swings": swings, "bias": bias, "events": events,
            "order_blocks": obs, "fvgs": fvgs, "liquidity": liquidity,
            "range": rng, "rsi": _rsi(candles), "vol_z": _volume_zscore(candles),
            # Bullish CHoCH context (used for buy signals)
            "choch_bull_index":        recent_bull_choch["index"]          if recent_bull_choch else -1,
            "choch_bull_protected_low": recent_bull_choch.get("protected_low") if recent_bull_choch else None,
            # Bearish CHoCH context (used for sell signals)
            "choch_bear_index":         recent_bear_choch["index"]           if recent_bear_choch else -1,
            "choch_bear_protected_high": recent_bear_choch.get("protected_high") if recent_bear_choch else None,
            # False breakout / sweep context
            "sweeps": sweeps,
            # candles reference (needed for wicked_into_zone check)
            "_candles": candles,
        }

    # -- signal construction ----------------------------------------------------

    def _reject(
        self, side: str, entry: float, rr: float,
        confidence: float, gate: str, detail: str,
    ) -> None:
        """Record a setup the gates threw out, and why.

        Rejections used to be a bare ``return None``, so an analysis that found
        real structure but filtered every candidate was indistinguishable from
        one that found nothing at all — the panel just showed no signals and the
        user could only conclude the sniper was broken. Gold on H1 does exactly
        this whenever its only candidates are counter-trend buys into a bearish
        H4: the gate is right to block them, but silence is not an answer.
        """
        self._rejected.append({
            "side": side,
            "entry": round(float(entry), 6),
            "rr": round(float(rr), 2),
            "confidence": round(float(confidence), 3),
            "gate": gate,
            "detail": detail,
        })

    def _build_signal(
        self,
        zone: Zone,
        side: str,
        last_price: float,
        atr: float,
        prim: Dict[str, Any],
    ) -> Optional[Signal]:
        rng = prim["range"]
        # When no swing-based dealing range exists yet (e.g. insufficient fractal
        # pivots on very short history), synthesise one from the recent price
        # extremes so the fallback guarantee can still produce at least one signal.
        if not rng:
            candles_ref = prim.get("_candles", [])
            if len(candles_ref) >= 20 and atr > 0:
                win = candles_ref[-60:]
                rng = (min(c.low for c in win), max(c.high for c in win))
            else:
                return None
        if atr <= 0:
            return None
        lo, hi = rng
        equilibrium = (lo + hi) / 2.0
        # Equilibrium tolerance band: zones whose entry sits within this band of
        # the 50% line still qualify as a valid discount (buy) / premium (sell)
        # entry.  Without it, a wide dealing range (common on higher timeframes
        # like H1/H4) causes good zones sitting right at equilibrium to be
        # rejected by a fraction of a point.  8% of the range ≈ the conventional
        # 42-58% "equilibrium zone" used in SMC.
        eq_tolerance = (hi - lo) * 0.08
        buffer = atr * self.sl_buffer_atr
        confluence: List[str] = []

        # ── CHoCH context for this signal direction ───────────────────────────
        choch_index: int   # bar index of most recent CHoCH in this direction
        _used_choch_sl = False  # tracks whether we replaced zone SL with CHoCH SL

        if side == "buy":
            entry = zone.top  # proximal edge of bullish zone (limit fill on retrace)
            if entry >= last_price:
                self._reject(side, entry, 0.0, 0.0, "location",
                             "zone sits at or above price — a buy limit must rest below it")
                return None  # must be a resting BUY LIMIT below price
            if entry > equilibrium + eq_tolerance:
                self._reject(side, entry, 0.0, 0.0, "location",
                             "zone is in premium — buys are only taken at a discount")
                return None  # only buy in discount (incl. equilibrium-zone band)
            # Tag: true discount vs equilibrium-zone entry.
            confluence.append("discount" if entry <= equilibrium else "equilibrium_zone")

            # Initial SL: below zone bottom.
            stop = zone.bottom - buffer
            risk = entry - stop
            if risk <= 0:
                return None

            # Tighter SL alternative: use the CHoCH protected low when it sits
            # between the zone bottom and the entry.  This aligns the stop with
            # the institutional inflection point and improves R/R.
            protected_low = prim.get("choch_bull_protected_low")
            if (protected_low is not None
                    and zone.bottom < protected_low < entry):
                choch_stop = protected_low - buffer
                if choch_stop > stop:  # genuinely tighter
                    stop = choch_stop
                    risk = entry - stop
                    _used_choch_sl = True

            choch_index = prim.get("choch_bull_index", -1)

            # TP = nearest buyside liquidity above, else range high, else R-multiple.
            targets = [p for p in prim["liquidity"]["buyside"] if p > entry + risk]
            tp = min(targets) if targets else max(hi, entry + risk * self.min_rr)
            # Cap target distance so it stays achievable (institutional single-leg).
            tp = min(tp, entry + risk * self.max_rr)
            order_type = "buy_limit"
        else:  # sell
            entry = zone.bottom
            if entry <= last_price:
                self._reject(side, entry, 0.0, 0.0, "location",
                             "zone sits at or below price — a sell limit must rest above it")
                return None  # resting SELL LIMIT above price
            if entry < equilibrium - eq_tolerance:
                self._reject(side, entry, 0.0, 0.0, "location",
                             "zone is in discount — sells are only taken at a premium")
                return None  # only sell in premium (incl. equilibrium-zone band)
            confluence.append("premium" if entry >= equilibrium else "equilibrium_zone")

            stop = zone.top + buffer
            risk = stop - entry
            if risk <= 0:
                return None

            # Tighter SL alternative: CHoCH protected high.
            protected_high = prim.get("choch_bear_protected_high")
            if (protected_high is not None
                    and entry < protected_high < zone.top):
                choch_stop = protected_high + buffer
                if choch_stop < stop:  # genuinely tighter
                    stop = choch_stop
                    risk = stop - entry
                    _used_choch_sl = True

            choch_index = prim.get("choch_bear_index", -1)

            targets = [p for p in prim["liquidity"]["sellside"] if p < entry - risk]
            tp = max(targets) if targets else min(lo, entry - risk * self.min_rr)
            # Cap target distance so it stays achievable (institutional single-leg).
            tp = max(tp, entry - risk * self.max_rr)
            order_type = "sell_limit"

        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr:
            self._reject(side, entry, rr, 0.0, "risk_reward",
                         f"RR {rr:.2f} is below the {self.min_rr:.1f} floor")
            return None
        # Hard guarantee: never emit a setup whose reward does not exceed risk.
        # (min_rr is clamped >= 1.0, but require a strict edge so reward > risk.)
        if reward <= risk:
            return None

        # ── Confluence tagging ────────────────────────────────────────────────
        # Zone-type tags
        if "ob" in zone.kind:
            confluence.append("order_block")
        if "fvg" in zone.kind:
            confluence.append("fair_value_gap")

        # Discount / premium depth bonus
        if side == "buy" and (equilibrium - lo) > 0:
            discount_depth = (equilibrium - entry) / (equilibrium - lo)
            if discount_depth >= 0.3:
                confluence.append("deep_discount")
        elif side == "sell" and (hi - equilibrium) > 0:
            premium_depth = (entry - equilibrium) / (hi - equilibrium)
            if premium_depth >= 0.3:
                confluence.append("deep_premium")

        # ── Strong-high / weak-low proximity (dealing-range extremes) ─────────
        # A buy sitting near the weak low (range low) is institutional demand; a
        # sell near the strong high (range high) is institutional supply. Entries
        # that mitigate at these extremes are the highest-conviction SMC setups.
        rng_size = (hi - lo) or 1e-9
        if side == "buy" and (entry - lo) <= rng_size * 0.25:
            confluence.append("at_weak_low")
        elif side == "sell" and (hi - entry) <= rng_size * 0.25:
            confluence.append("at_strong_high")

        # ── BOS continuation ──────────────────────────────────────────────────
        # If the latest structure event in this direction is a BOS (not a CHoCH),
        # the move is a continuation of the prevailing trend — a valid entry basis.
        last_evt = next(
            (e for e in reversed(prim.get("events", []))
             if e.get("direction") == ("bullish" if side == "buy" else "bearish")),
            None,
        )
        if last_evt and last_evt.get("type") == "BOS":
            confluence.append("bos_continuation")

        # Structure-aligned: current trend bias agrees with the trade direction
        if prim["bias"] == ("bullish" if side == "buy" else "bearish"):
            confluence.append("structure_aligned")

        # ── CHoCH-specific tags ───────────────────────────────────────────────
        if choch_index >= 0:
            confluence.append("choch_reversal")
            if zone.index > choch_index:
                confluence.append("post_choch_zone")

        if _used_choch_sl:
            confluence.append("choch_protected_low" if side == "buy" else "choch_protected_high")

        # ── FVG / OB zone-combination tags ───────────────────────────────────
        if "fair_value_gap" in confluence and "discount" in confluence and side == "buy":
            confluence.append("fvg_in_discount")
        if "fair_value_gap" in confluence and "premium" in confluence and side == "sell":
            confluence.append("fvg_in_premium")
        if "order_block" in confluence and "discount" in confluence and side == "buy":
            confluence.append("ob_in_discount")
        if "order_block" in confluence and "premium" in confluence and side == "sell":
            confluence.append("ob_in_premium")
        if "order_block" in confluence and "structure_aligned" in confluence:
            confluence.append("ob_structure_confluence")
        if prim.get("multi_zone_discount") and side == "buy":
            confluence.append("multi_zone_discount")
        if prim.get("multi_zone_premium") and side == "sell":
            confluence.append("multi_zone_premium")

        # Indicator-based extras
        rsi = prim["rsi"]
        if side == "buy" and rsi < 45:
            confluence.append("rsi_discount")
        if side == "sell" and rsi > 55:
            confluence.append("rsi_premium")
        if prim["vol_z"] > 1.0:
            confluence.append("volume_displacement")

        # ── False breakout / stop-hunt confluence tags ────────────────────────
        sweeps = prim.get("sweeps", {})
        candles_ref = prim.get("_candles", [])

        # Liquidity sweep (stop hunt) into the zone direction
        if side == "buy" and sweeps.get("swept_lows"):
            # A sweep of recent lows just before a buy = stop hunt, now reversal
            confluence.append("sweep_of_lows")
        if side == "sell" and sweeps.get("swept_highs"):
            confluence.append("sweep_of_highs")

        # Zone was wicked into (tested by wick, no close inside) — highest quality
        if _wicked_into_zone(zone, candles_ref, atr):
            confluence.append("wicked_into_zone")

        # High false-break score on the recent sweep bar amplifies confidence
        fb_score = sweeps.get("false_break_score", 0.0)
        if fb_score >= 60:
            confluence.append("strong_false_breakout")
        elif fb_score >= 35:
            confluence.append("possible_false_breakout")

        # Rejection wick at the zone top/bottom (pin bar / hammer)
        rej_wick = sweeps.get("rejection_wick", 0.0)
        if rej_wick >= 0.55:
            confluence.append("rejection_wick")

        # ── Deterministic factor scoring ──────────────────────────────────────
        # Confidence is a weighted sum of measured volume / structure / movement
        # / risk factors, each with a numeric raw value and contribution. See
        # services/smc_scoring.py for the factor definitions and weights.
        breakdown = smc_scoring.score_signal(
            side=side,
            zone_index=zone.index,
            zone_kind=zone.kind,
            zone_top=zone.top,
            zone_bottom=zone.bottom,
            entry=entry,
            rr=rr,
            min_rr=self.min_rr,
            max_rr=self.max_rr,
            candles=prim.get("_candles", []),
            atr=atr,
            bias=prim["bias"],
            events=prim.get("events", []),
            sweeps=sweeps,
            equilibrium=equilibrium,
            range_low=lo,
            range_high=hi,
            choch_index=choch_index,
            htf_bias=prim.get("htf_bias"),
            weights=self.factor_weights,
            macro=prim.get("macro"),
        )

        # Hard gate 1: no signal fires without volume confirmation. The
        # zone-forming candle must have traded above its rolling mean, and any
        # break of structure in this direction must itself be volume-confirmed.
        if not breakdown.volume_confirmed:
            self._reject(side, entry, rr, breakdown.total, "volume",
                         "zone was not volume-confirmed")
            return None

        # Hard gate 2: a higher-timeframe bias that opposes this trade blocks it.
        if breakdown.htf_blocked:
            self._reject(
                side, entry, rr, breakdown.total, "htf_bias",
                f"{prim.get('htf_bias')} higher timeframe opposes this {side}",
            )
            return None

        confidence = round(breakdown.total, 2)

        # ── Human-readable reason ─────────────────────────────────────────────
        factors: List[str] = []
        # Lead with false-breakout context (highest priority narrative)
        if "sweep_of_lows" in confluence:
            factors.append("stop-hunt sweep of lows")
        elif "sweep_of_highs" in confluence:
            factors.append("stop-hunt sweep of highs")
        if "strong_false_breakout" in confluence:
            factors.append("strong false breakout")
        elif "possible_false_breakout" in confluence:
            factors.append("false breakout signal")
        if "wicked_into_zone" in confluence:
            factors.append("wick-tested zone")
        if "rejection_wick" in confluence:
            factors.append("rejection wick")
        # CHoCH context
        if "post_choch_zone" in confluence:
            factors.append("post-CHoCH")
        elif "choch_reversal" in confluence:
            factors.append("CHoCH reversal")
        if "multi_zone_discount" in confluence:
            factors.append("FVG+OB in discount")
        elif "multi_zone_premium" in confluence:
            factors.append("FVG+OB in premium")
        elif "fvg_in_discount" in confluence:
            factors.append("bullish FVG in discount")
        elif "fvg_in_premium" in confluence:
            factors.append("bearish FVG in premium")
        elif "fair_value_gap" in confluence:
            factors.append(zone.kind.replace("_", " "))
        if "ob_structure_confluence" in confluence:
            factors.append("OB+structure")
        elif "order_block" in confluence:
            factors.append("order block")
        if "deep_discount" in confluence:
            factors.append("deep discount")
        elif "deep_premium" in confluence:
            factors.append("deep premium")
        if "bos_continuation" in confluence:
            factors.append("BOS continuation")
        if "at_weak_low" in confluence:
            factors.append("at weak low")
        elif "at_strong_high" in confluence:
            factors.append("at strong high")
        if "structure_aligned" in confluence:
            factors.append("structure aligned")
        if not factors:
            factors.append(zone.kind.replace("_", " "))

        # ── Macro, stated either way ─────────────────────────────────────────
        # The user's requirement is that the signal says it used these features
        # — and says so plainly when it could not. This must land BEFORE the
        # "; RR" marker: MT5SniperChart strips everything from there onward when
        # it builds the spoken summary, so a clause after it is never heard.
        macro = prim.get("macro")
        if macro is not None and getattr(macro, "applicable", False):
            macro_norm = float(getattr(macro, "normalized", 0.0) or 0.0)
            aligned = macro_norm if side == "buy" else -macro_norm
            if aligned > 0.1:
                confluence.append("macro_supportive")
            elif aligned < -0.1:
                confluence.append("macro_headwind")
            else:
                confluence.append("macro_neutral")
            factors.append(f"macro {getattr(macro, 'reason', '')}".strip())
        else:
            confluence.append("macro_na")
            why = getattr(macro, "reason", "") if macro is not None else "macro not consulted"
            factors.append(why or "macro n/a")
        # Join factors; capitalise only the very first word so CHoCH / FVG
        # abbreviations in the middle of the string are preserved as-is.
        joined = ", ".join(factors)
        # Narrative tags first, then the numeric factor attribution so the score
        # is always explainable from the same string.
        reason = (
            f"{joined[0].upper()}{joined[1:]}; RR {rr:.1f}; "
            f"{smc_scoring.describe(breakdown)}"
        )

        # ── Balance-aware position sizing ─────────────────────────────────────
        lot, risk_amount = self._position_size(entry, stop, self.account_balance)
        # Hard per-trade loss cap: shrink the lot so a full stop-out stays within
        # the account's max_total_loss budget. If even the broker-minimum 0.01 lot
        # would exceed the cap, we still report the honest 0.01-lot risk and flag
        # it (so the user always SEES the real $ risk/reward on the signal).
        risk_exceeds_cap = False
        if self.max_total_loss > 0 and risk_amount > self.max_total_loss:
            sl_d = abs(entry - stop)
            if sl_d > 0 and self.contract_size > 0:
                capped_lot = round(self.max_total_loss / (sl_d * self.contract_size), 2)
                if capped_lot >= 0.01:
                    lot = capped_lot
                    risk_amount = round(lot * sl_d * self.contract_size, 2)
                else:
                    lot = 0.01  # broker minimum
                    risk_amount = round(0.01 * sl_d * self.contract_size, 2)
                    risk_exceeds_cap = True
        reward_amount = round(risk_amount * rr, 2) if risk_amount else 0.0

        # ── Pip / point geometry (MT5-accurate) ───────────────────────────────
        #   points    = price distance / smallest increment  (the MT5 ticket unit)
        #   pips      = points / 10                          (fractional-pip convention)
        #   pip_value = pip_size × contract_size × lot       (account-currency / pip)
        # Cross-check: risk_amount ≈ sl_pips × pip_value, reward ≈ tp_pips × pip_value.
        psize = self.point_size or 1e-9
        pipsz = self.pip_size or 1e-9
        sl_dist = abs(entry - stop)
        tp_dist = abs(tp - entry)
        sl_points = round(sl_dist / psize, 1)
        tp_points = round(tp_dist / psize, 1)
        sl_pips = round(sl_dist / pipsz, 1)
        tp_pips = round(tp_dist / pipsz, 1)
        pip_value = round(pipsz * self.contract_size * lot, 4) if lot else 0.0

        # ── Laddered take-profits (TP1 / TP2 / TP3) ───────────────────────────
        # Scale-out targets at 1R / 2R / 3R, snapped to liquidity pools where
        # available so each TP sits on a real level (matches the TP1/2/3 ladder).
        r = risk
        if side == "buy":
            liq = sorted(p for p in prim["liquidity"]["buyside"] if p > entry + r * 0.5)
            tp1 = liq[0] if len(liq) >= 1 else entry + r * 1.0
            tp2 = liq[1] if len(liq) >= 2 else max(tp, entry + r * 2.0)
            tp3 = liq[2] if len(liq) >= 3 else entry + r * 3.0
        else:
            liq = sorted((p for p in prim["liquidity"]["sellside"] if p < entry - r * 0.5), reverse=True)
            tp1 = liq[0] if len(liq) >= 1 else entry - r * 1.0
            tp2 = liq[1] if len(liq) >= 2 else min(tp, entry - r * 2.0)
            tp3 = liq[2] if len(liq) >= 3 else entry - r * 3.0

        in_us = self._in_us_session(zone.time)

        return Signal(
            side=side, order_type=order_type, entry=round(entry, 6),
            stop_loss=round(stop, 6), take_profit=round(tp, 6), rr=round(rr, 2),
            confidence=confidence, reason=reason, zone_kind=zone.kind,
            formed_index=zone.index, formed_time=zone.time, confluence=confluence,
            tp1=round(tp1, 6), tp2=round(tp2, 6), tp3=round(tp3, 6), in_us_session=in_us,
            lot=lot, risk_amount=risk_amount,
            risk_pct=round(risk_amount / self.account_balance * 100, 2) if self.account_balance > 0 else 0.0,
            reward_amount=reward_amount, risk_exceeds_cap=risk_exceeds_cap,
            point_size=self.point_size, pip_size=self.pip_size,
            contract_size=self.contract_size,
            sl_points=sl_points, tp_points=tp_points,
            sl_pips=sl_pips, tp_pips=tp_pips, pip_value=pip_value,
            score_breakdown=breakdown.to_dict(),
        )

    # -- higher-timeframe bias --------------------------------------------------

    def _htf_bias(self, htf_candles: Optional[List[Candle]]) -> Optional[str]:
        """Structural bias of the higher timeframe, or None when unavailable.

        Reuses the same swing + BOS/CHoCH detection as the entry timeframe so
        the two reads are computed identically.
        """
        if not htf_candles or len(htf_candles) < 40:
            return None
        swings = _swings(htf_candles, self.swing_left, self.swing_right)
        bias, _events = _structure_bias(htf_candles, swings)
        return bias if bias in ("bullish", "bearish") else None

    # -- public: live analysis --------------------------------------------------

    def analyze(
        self,
        candles: List[Candle],
        htf_candles: Optional[List[Candle]] = None,
        macro: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Produce drawable zones + currently-valid pending sniper limit setups.

        ``htf_candles`` are higher-timeframe bars for the same instrument. When
        supplied, their structural bias GATES entries: a setup whose direction
        opposes the HTF bias is rejected outright rather than merely penalised.

        ``macro`` is an ``app.services.macro_context.MacroBias`` resolved by the
        caller. This method is synchronous and is re-entered once per bar by
        ``backtest``, so it must never fetch: an omitted bias simply means the
        setup is scored without macro, with the weight redistributed rather than
        forfeited.
        """
        # Fresh ledger per analysis — rejections describe THIS run, and a stale
        # carry-over would explain the previous chart's absence, not this one's.
        self._rejected: List[Dict[str, Any]] = []

        if len(candles) < 40:
            return {"error": "Not enough candles (need >= 40)", "signals": [], "zones": []}

        prim = self._primitives(candles)
        prim["htf_bias"] = self._htf_bias(htf_candles)
        prim["macro"] = macro
        atr = prim["atr"]
        last_price = candles[-1].close
        bias = prim["bias"]

        zones = prim["order_blocks"] + prim["fvgs"]
        # Only consider zones formed reasonably recently (last 120 bars) and untested.
        recent_cut = max(0, len(candles) - 120)
        zones = [z for z in zones if z.index >= recent_cut]

        # ── US (New York) session anchoring ───────────────────────────────────
        # When enabled, generate the day's entries from the US session: keep only
        # zones that formed during the US session (13:30–20:00 UTC), anchored to
        # the session-open candle. This focuses the sniper on the highest-liquidity
        # window and refines entries as the session develops.
        us_open = self._us_session_open(candles)
        all_zones = zones  # keep the full set so US-session mode can fall back
        if self.us_session_only:
            us_zones = [z for z in zones if self._in_us_session(z.time)]
            # Prefer US-session zones when present; if there are none yet, keep
            # the full set so the analysis still produces setups.
            zones = us_zones if us_zones else zones

        # ── Multi-zone-discount / multi-zone-premium detection ─────────────────
        # Check whether BOTH FVG and OB zone types are present in the discount
        # half (and mirror for premium).  This is the scenario described in the
        # user narrative: "several FVGs and an OB are present within this discount,
        # offering potential entry zones for a continuation trade."
        if prim["range"]:
            eq_mid = (prim["range"][0] + prim["range"][1]) / 2.0
            bull_discount = [z for z in zones
                             if z.top <= eq_mid and z.kind.startswith("bullish")]
            prim["multi_zone_discount"] = (
                any("fvg" in z.kind for z in bull_discount) and
                any("ob"  in z.kind for z in bull_discount)
            )
            bear_premium = [z for z in zones
                            if z.bottom >= eq_mid and z.kind.startswith("bearish")]
            prim["multi_zone_premium"] = (
                any("fvg" in z.kind for z in bear_premium) and
                any("ob"  in z.kind for z in bear_premium)
            )
        else:
            prim["multi_zone_discount"] = False
            prim["multi_zone_premium"] = False

        # ── Signal generation (reusable over any zone set) ────────────────────
        # Wrapped in a helper so US-session mode can retry against the full zone
        # set when the session-filtered zones produce nothing actionable.
        def _generate(zone_set: List["Zone"]) -> List[Signal]:
            out: List[Signal] = []
            seen: set = set()
            # Prefer structure-aligned setups first.
            for z in sorted(zone_set, key=lambda x: x.index, reverse=True):
                if z.kind.startswith("bullish"):
                    side = "buy"
                else:
                    side = "sell"
                # Soft bias filter: skip counter-trend unless neutral.
                if bias == "bullish" and side == "sell":
                    continue
                if bias == "bearish" and side == "buy":
                    continue
                sig = self._build_signal(z, side, last_price, atr, prim)
                if not sig:
                    continue
                if sig.confidence < self.min_confidence:
                    # quality gate — skip low-conviction setups
                    self._reject(
                        sig.side, sig.entry, sig.rr, sig.confidence, "confidence",
                        f"scored {sig.confidence:.2f}, below the {self.min_confidence:.2f} floor",
                    )
                    continue
                # Structure gate: the entry decision must be backed by detectable
                # SMC structure — an OB/FVG zone PLUS at least one of BOS / CHoCH /
                # a strong-high or weak-low extreme / trend alignment. Raw zones
                # with no structural context are rejected (relaxed pass can fire).
                if self.require_structure and not (set(sig.confluence) & {
                    "structure_aligned", "choch_reversal", "post_choch_zone",
                    "bos_continuation", "at_weak_low", "at_strong_high",
                    "ob_structure_confluence", "multi_zone_discount", "multi_zone_premium",
                }):
                    continue
                key = (sig.side, round(sig.entry, 4))
                if key in seen:
                    continue
                seen.add(key)
                out.append(sig)
                if len(out) >= self.max_signals:
                    break

            # ── Minimum-signal guarantee ──────────────────────────────────────
            # If the strict filters produced nothing, relax progressively so the
            # user ALWAYS sees at least one actionable setup:
            #   pass 1 — ignore the confidence floor (keep bias + discount rules)
            #   pass 2 — also allow counter-trend zones (best available regardless)
            #   pass 3 — drop the RR floor entirely (show best available setup
            #             even if RR < min_rr, tagged "below_min_rr" so the UI
            #             can warn the user. This guarantees Analyze always returns
            #             something useful regardless of the chosen Min RR.)
            if not out:
                relaxed: List[Signal] = []

                # Pass 1 & 2: keep user's min_rr but relax structure/bias
                for allow_counter in (False, True):
                    for z in sorted(zone_set, key=lambda x: x.index, reverse=True):
                        side = "buy" if z.kind.startswith("bullish") else "sell"
                        if not allow_counter:
                            if bias == "bullish" and side == "sell":
                                continue
                            if bias == "bearish" and side == "buy":
                                continue
                        sig = self._build_signal(z, side, last_price, atr, prim)
                        if not sig:
                            continue
                        sig.confluence.append("relaxed_fallback")
                        relaxed.append(sig)
                    if relaxed:
                        break

                # Pass 3: drop RR floor — show BEST available setup even if below
                # the user's requested Min RR.  This ensures Analyze always returns
                # at least one result so the AI review and chart overlay can fire.
                if not relaxed and self.min_rr > 1.0:
                    saved_min_rr = self.min_rr
                    self.min_rr = 1.0  # temporarily lower floor
                    for allow_counter in (False, True):
                        for z in sorted(zone_set, key=lambda x: x.index, reverse=True):
                            side = "buy" if z.kind.startswith("bullish") else "sell"
                            if not allow_counter:
                                if bias == "bullish" and side == "sell":
                                    continue
                                if bias == "bearish" and side == "buy":
                                    continue
                            sig = self._build_signal(z, side, last_price, atr, prim)
                            if not sig:
                                continue
                            sig.confluence.append("relaxed_fallback")
                            sig.confluence.append("below_min_rr")
                            relaxed.append(sig)
                        if relaxed:
                            break
                    self.min_rr = saved_min_rr  # restore

                relaxed.sort(key=lambda s: s.confidence, reverse=True)
                out = relaxed[: max(1, self.max_signals)]
            return out

        signals = _generate(zones)

        # ── US-session fallback ───────────────────────────────────────────────
        # When US-session anchoring is on but the session-filtered zones yield no
        # setups, retry against the full zone set so the user always sees signals
        # (the US-session reference levels are still returned for context).
        if not signals and self.us_session_only and all_zones is not zones:
            signals = _generate(all_zones)

        signals.sort(key=lambda s: s.confidence, reverse=True)

        # Movement / momentum read for the header.
        atr_pct = (atr / last_price * 100) if last_price else 0.0
        if prim["vol_z"] > 1.5 and atr_pct > 0:
            momentum = "expanding"
        elif prim["vol_z"] < -0.5:
            momentum = "contracting"
        else:
            momentum = "normal"

        return {
            "bias": bias,
            "htf_bias": prim.get("htf_bias"),
            "last_price": round(last_price, 6),
            "atr": round(atr, 6),
            "atr_pct": round(atr_pct, 3),
            "rsi": round(prim["rsi"], 1),
            "volume_z": round(prim["vol_z"], 2),
            "momentum": momentum,
            "equilibrium": round((prim["range"][0] + prim["range"][1]) / 2, 6) if prim["range"] else None,
            "range": {"low": round(prim["range"][0], 6), "high": round(prim["range"][1], 6)} if prim["range"] else None,
            "structure_events": prim["events"][-12:],
            "liquidity": {
                "buyside": [round(p, 6) for p in prim["liquidity"]["buyside"][-6:]],
                "sellside": [round(p, 6) for p in prim["liquidity"]["sellside"][-6:]],
            },
            "zones": _best_zones_for_chart(zones),
            "signals": [s.to_dict() for s in signals],
            # Candidates the gates threw out. Only meaningful when `signals` is
            # empty — that is exactly when the user needs to know the difference
            # between "found nothing" and "found setups and refused them".
            # Highest-conviction first, capped so it stays readable.
            "rejected_signals": sorted(
                self._rejected, key=lambda r: -r["confidence"]
            )[:6],
            "us_session": {
                "enabled": self.us_session_only,
                "open_time": us_open.time if us_open else None,
                "open_price": round(us_open.open, 6) if us_open else None,
                "live_in_session": self._in_us_session(candles[-1].time),
            },
            # ── False breakout / sweep context for the frontend ───────────────
            "false_breakout": {
                k: v for k, v in prim.get("sweeps", {}).items()
                if k != "_candles"  # never serialise the candle list
            },
            # ── Macro context, reported whether or not it applied ─────────────
            "macro": _macro_payload(macro),
        }

    # -- public: backtest -------------------------------------------------------

    def backtest(
        self,
        candles: List[Candle],
        expiry_bars: int = 24,
        max_hold_bars: int = 120,
        warmup: int = 60,
        starting_balance: float = 0.0,
        tp1_r: float = 0.5,
        tp1_frac: float = 0.5,
        trail_start_pct: float = 0.0,   # start trailing right after TP1 (0 = immediate)
        trail_capture_pct: float = 0.90,  # trail is 10% of target_dist behind best price
        min_entry_confidence: float = 0.7,  # only take high-conviction opportunities
    ) -> Dict[str, Any]:
        """
        Walk-forward simulation of the sniper model.

        For every bar we (re)derive zones from the data available up to that bar,
        place at most one resting limit setup, and simulate forward fills + SL/TP.
        Only one trade is active at a time (sniper discipline).

        When a starting balance is supplied (directly or via the engine's
        account_balance), the result also includes a full account-currency equity
        simulation with risk-based position sizing and loss-recovery scaling.
        """
        n = len(candles)
        if n < warmup + 20:
            return {"error": "Not enough candles to backtest (need >= 80)", "trades": [], "stats": {}}

        trades: List[Dict[str, Any]] = []
        i = warmup
        guard = 0
        _last_breakeven_bar: int = -1   # tracks bar where last breakeven re-scan was done
        while i < n - 1 and guard < n * 2:
            guard += 1
            window = candles[max(0, i - 300):i + 1]
            res = self.analyze(window)
            sigs = res.get("signals", [])
            # Backtest discipline: never trade the live "always show at least one"
            # relaxed fallback. Only take genuine setups that cleared the
            # confidence floor + bias/discount rules — this is what keeps the
            # backtested win-rate honest (and high).
            sigs = [s for s in sigs if "relaxed_fallback" not in s.get("confluence", [])]
            if not sigs:
                i += 1
                continue
            sig = sigs[0]
            # ── Take only GOOD opportunities, not every signal ───────────────
            # Require strong conviction: high confidence AND at least two genuine
            # structural confluences (CHoCH/BOS/strong-weak/structure/OB-FVG zone).
            STRONG = {
                "choch_reversal", "post_choch_zone", "bos_continuation",
                "at_weak_low", "at_strong_high", "structure_aligned",
                "multi_zone_discount", "multi_zone_premium",
                "fvg_in_discount", "fvg_in_premium", "ob_structure_confluence",
                "deep_discount", "deep_premium",
            }
            strong_count = len(set(sig.get("confluence", [])) & STRONG)
            if sig.get("confidence", 0) < min_entry_confidence or strong_count < 2:
                i += 1
                continue
            entry = sig["entry"]
            sl = sig["stop_loss"]
            tp = sig["take_profit"]
            side = sig["side"]

            # Try to fill the resting limit within expiry_bars.
            fill_idx = None
            for k in range(i + 1, min(i + 1 + expiry_bars, n)):
                bar = candles[k]
                if bar.low <= entry <= bar.high:
                    fill_idx = k
                    break
            if fill_idx is None:
                i += 1
                continue

            # Resolve outcome with SCALE-OUT + TRAILING-STOP management:
            #   • TP1 at `tp1_r`×risk books `tp1_frac` of the position and moves the
            #     stop to breakeven (a near target hit often → keeps win-rate high).
            #   • Once price is `trail_start_pct` of the way to TP3 (50% profit), a
            #     trailing stop activates and ratchets behind price, locking
            #     `trail_capture_pct` (75%) of the best move. By the time TP2 prints
            #     the trail is already active, so profit is protected and the runner
            #     is left to extend until the trail is hit.
            #   A trade is a WIN once TP1 is booked, a LOSS only if the original
            #   stop is hit before TP1.
            risk = abs(entry - sl)
            tp1 = (entry + tp1_r * risk) if side == "buy" else (entry - tp1_r * risk)
            tp3 = sig.get("tp3") or ((entry + 3 * risk) if side == "buy" else (entry - 3 * risk))
            target_dist = abs(tp3 - entry) or (3 * risk)
            trail_trigger = (entry + trail_start_pct * target_dist) if side == "buy" else (entry - trail_start_pct * target_dist)
            trail_dist = (1.0 - trail_capture_pct) * target_dist  # 25% → lock 75%

            outcome = None
            exit_idx = fill_idx
            booked_r = 0.0
            tp1_hit = False
            trailing = False
            trailed = False
            cur_sl = sl
            best = entry  # best favourable price reached
            actual_exit_price: float | None = None   # real fill price (TP/SL), not candle close
            for k in range(fill_idx, min(fill_idx + max_hold_bars, n)):
                bar = candles[k]
                if not tp1_hit:
                    hit_sl = bar.low <= cur_sl if side == "buy" else bar.high >= cur_sl
                    hit_tp1 = bar.high >= tp1 if side == "buy" else bar.low <= tp1
                    if hit_sl and not hit_tp1:
                        outcome, exit_idx = "loss", k
                        actual_exit_price = cur_sl   # stopped at SL price
                        booked_r = -1.0
                        break
                    if hit_tp1:
                        tp1_hit = True
                        booked_r = tp1_frac * tp1_r   # partial profit locked
                        # Lock runner stop at TP1 price — never return below TP1.
                        # (breakeven used to be the floor; now TP1 is, so the runner
                        #  always exits at a meaningful level, not just entry.)
                        cur_sl = tp1
                if tp1_hit:
                    # Track best excursion; activate + ratchet the trailing stop.
                    best = max(best, bar.high) if side == "buy" else min(best, bar.low)
                    if not trailing:
                        reached = (bar.high >= trail_trigger) if side == "buy" else (bar.low <= trail_trigger)
                        if reached:
                            trailing = True
                    if trailing:
                        new_sl = (best - trail_dist) if side == "buy" else (best + trail_dist)
                        # Floor is TP1 price (not entry) — runner can only improve from TP1 level.
                        cur_sl = max(cur_sl, new_sl, tp1) if side == "buy" else min(cur_sl, new_sl, tp1)
                        trailed = True
                    hit_stop = bar.low <= cur_sl if side == "buy" else bar.high >= cur_sl
                    if hit_stop:
                        runner_r = (cur_sl - entry) / risk if side == "buy" else (entry - cur_sl) / risk
                        booked_r += (1 - tp1_frac) * runner_r
                        outcome, exit_idx = "win", k  # TP1 already booked → win
                        actual_exit_price = cur_sl    # exited at trailing/breakeven stop
                        break
            if outcome is None:
                # Timeout: close whatever is open at the last bar's market price.
                exit_idx = min(fill_idx + max_hold_bars, n) - 1
                last = candles[exit_idx].close
                actual_exit_price = last              # timeout → market close price
                if tp1_hit:
                    booked_r += (1 - tp1_frac) * ((last - entry) / risk if side == "buy" else (entry - last) / risk)
                    outcome = "win" if booked_r > 0 else "breakeven"
                else:
                    fav = (last - entry) if side == "buy" else (entry - last)
                    booked_r = fav / risk if risk > 0 else 0.0
                    outcome = "win" if booked_r > 0 else "loss"

            # Use the actual execution price (TP/SL level) — NOT the candle close.
            # This prevents the misleading display where a SELL at 3998 shows
            # exit=4015 (candle close after breakeven stop) instead of exit=3998.
            exit_price = actual_exit_price if actual_exit_price is not None else candles[exit_idx].close
            r_mult = booked_r
            trades.append({
                "side": side, "entry": entry, "sl": sl, "tp": tp,
                "fill_time": candles[fill_idx].time, "exit_time": candles[exit_idx].time,
                "exit_price": round(exit_price, 6), "outcome": outcome,
                "r_multiple": round(r_mult, 2), "confidence": sig["confidence"],
                "zone_kind": sig["zone_kind"],
                "sl_distance": round(risk, 6),
                "trailed": trailed,
                "confluence": sig.get("confluence", []),
            })
            # ── Re-entry logic ────────────────────────────────────────────────
            # When a trade exits at ~TP1 level (runner barely extended, then
            # reversed back to TP1), the zone that produced the setup is still
            # live. Re-scanning the SAME exit bar lets us find a fresh entry
            # immediately — compounding profit instead of sitting idle.
            # A "minimal-extension win" is one where the runner added little
            # beyond the locked TP1 partial (booked_r between 0.45-0.55R means
            # only TP1 was captured, runner came right back).
            _is_minimal_win = outcome == "win" and (tp1_frac * tp1_r - 0.1) <= booked_r <= (tp1_frac * tp1_r + 0.15)
            if _is_minimal_win and exit_idx != _last_breakeven_bar:
                # Re-scan from the exit bar — might find re-entry at same zone.
                # fill search always starts at i+1, so we can't loop on the same bar.
                _last_breakeven_bar = exit_idx
                i = exit_idx
            else:
                _last_breakeven_bar = -1
                i = exit_idx + 1  # normal advance

        stats = self._stats(trades)

        # ── Account-currency equity simulation (with loss recovery) ───────────
        start_bal = starting_balance if starting_balance > 0 else self.account_balance
        if start_bal > 0:
            bal_stats = self._simulate_balance(trades, start_bal)
            stats.update(bal_stats)

        return {"trades": trades, "stats": stats}

    def _simulate_balance(self, trades: List[Dict[str, Any]], start_balance: float) -> Dict[str, Any]:
        """
        Replay trades as an account-currency equity curve with hard risk control.

        Guarantees:
          • The account NEVER loses more than `max_total_loss` from its starting
            balance. Each trade's risk is capped by the remaining loss budget, and
            trading halts once the floor (start − max_total_loss) is reached. A
            trade whose minimum 0.01 lot would risk more than the budget is skipped
            (it cannot be taken safely), so the floor is honoured truthfully.
          • Per day, once the `daily_profit_target_pct` goal is met, no further risk
            is taken that day (gains are locked). After a loss, risk is scaled up
            (capped) to recover the drawdown and chase the daily target.
        """
        import datetime as _dt

        balance = start_balance
        peak = start_balance
        min_bal = start_balance
        max_dd_cur = 0.0
        base_pct = self.risk_per_trade_pct / 100.0
        loss_cap = self.max_total_loss if self.max_total_loss > 0 else 0.0
        floor = (start_balance - loss_cap) if loss_cap > 0 else None
        daily_target = (start_balance * self.daily_profit_target_pct / 100.0) if self.daily_profit_target_pct > 0 else 0.0

        day_pnl: Dict[str, float] = {}
        day_trades: Dict[str, int] = {}
        skipped = 0

        for t in trades:
            try:
                day = _dt.datetime.utcfromtimestamp(t.get("fill_time", 0)).date().isoformat()
            except Exception:
                day = "—"

            # Hard floor: never trade once the max-loss budget is exhausted.
            budget = (balance - floor) if floor is not None else None
            if floor is not None and budget is not None and budget <= 0:
                t["skipped"] = True; skipped += 1
                continue
            # Lock daily gains once the target is reached.
            if daily_target > 0 and day_pnl.get(day, 0.0) >= daily_target:
                t["skipped"] = True; skipped += 1
                continue

            # ── Risk sizing: steady fixed-fractional, with gentle recovery ────
            # Risk a constant % of the *current* balance. The daily target is a
            # GOAL (used to lock gains once hit + reporting) — it does NOT inflate
            # the per-trade risk, so a losing trade only costs ~risk% (no chasing).
            deficit = max(0.0, peak - balance)
            base_risk = balance * base_pct
            risk_amount = base_risk
            mult = 1.0
            if self.recovery_enabled and base_risk > 0 and deficit > 0:
                # Gentle recovery only when a hard loss-cap is in force (cap mode).
                needed = base_risk + deficit / max(self.max_rr, 1.0)
                mult = min(needed / base_risk, self.max_risk_multiplier)
                risk_amount = base_risk * mult

            # Cap the risk by the remaining loss budget (the floor guarantee).
            if budget is not None:
                risk_amount = min(risk_amount, budget)
            if risk_amount <= 0:
                t["skipped"] = True; skipped += 1
                continue

            # Translate to a tradeable lot. Two sizing models:
            #  • Cap mode (max_total_loss set): model the real broker 0.01 min-lot
            #    risk and SKIP any trade whose min-lot loss would breach the budget
            #    (keeps the hard floor truthful).
            #  • Risk-% mode (no cap): use EXACT fixed-fractional sizing — the loss
            #    is always risk% of the current balance, so the account compounds
            #    and can never be driven negative (the % itself is the protection).
            sl_dist = t.get("sl_distance", 0.0)
            if sl_dist > 0 and self.contract_size > 0:
                raw_lot = risk_amount / (sl_dist * self.contract_size)
                lot = max(0.01, round(raw_lot, 2))
                if floor is not None:
                    actual_risk = lot * sl_dist * self.contract_size
                    if budget is not None and actual_risk > budget + 1e-9:
                        t["skipped"] = True; skipped += 1
                        continue
                    risk_amount = actual_risk
                # else: keep the exact fractional risk_amount (risk% of balance).
            else:
                lot = 0.0

            pnl = t["r_multiple"] * risk_amount
            balance += pnl
            peak = max(peak, balance)
            min_bal = min(min_bal, balance)
            max_dd_cur = min(max_dd_cur, balance - peak)
            day_pnl[day] = day_pnl.get(day, 0.0) + pnl
            day_trades[day] = day_trades.get(day, 0) + 1

            t["risk_amount"] = round(risk_amount, 2)
            t["risk_multiplier"] = round(mult, 2)
            t["lot"] = max(0.01, round(lot, 2)) if lot else 0.0
            t["pnl"] = round(pnl, 2)
            t["balance_after"] = round(balance, 2)
            t["day"] = day
            t["skipped"] = False

        ending = round(balance, 2)
        profit = round(ending - start_balance, 2)
        # Worst observed total loss from the starting balance (>= -loss_cap).
        max_total_loss_seen = round(max(0.0, start_balance - min_bal), 2)
        day_values = list(day_pnl.values())
        days = len(day_values)
        days_hit_target = sum(1 for v in day_values if daily_target > 0 and v >= daily_target)
        worst_day = round(min(day_values), 2) if day_values else 0.0
        best_day = round(max(day_values), 2) if day_values else 0.0
        avg_daily = round(sum(day_values) / days, 2) if days else 0.0
        avg_daily_pct = round(avg_daily / start_balance * 100, 1) if start_balance else 0.0

        # ── Per-day breakdown (ordered, with running balance) ─────────────────
        daily_breakdown: List[Dict[str, Any]] = []
        run_bal = start_balance
        for d in sorted(day_pnl.keys()):
            p = day_pnl[d]
            run_bal += p
            daily_breakdown.append({
                "day": d,
                "profit": round(p, 2),
                "pct": round(p / start_balance * 100, 1) if start_balance else 0.0,
                "trades": day_trades.get(d, 0),
                "balance": round(run_bal, 2),
                "hit_target": bool(daily_target > 0 and p >= daily_target),
            })

        return {
            "starting_balance": round(start_balance, 2),
            "ending_balance": ending,
            "net_profit": profit,
            "net_profit_pct": round((profit / start_balance * 100), 2) if start_balance else 0.0,
            "peak_balance": round(peak, 2),
            "min_balance": round(min_bal, 2),
            "max_drawdown_currency": round(max_dd_cur, 2),
            "recovered": ending >= start_balance,
            "risk_per_trade_pct": round(self.risk_per_trade_pct, 2),
            # Risk-control + daily-target reporting
            "max_loss_cap": round(loss_cap, 2) if loss_cap else 0.0,
            "max_total_loss_seen": max_total_loss_seen,
            "loss_cap_respected": (max_total_loss_seen <= loss_cap + 1e-6) if loss_cap else True,
            "skipped_trades": skipped,
            "daily_target": round(daily_target, 2) if daily_target else 0.0,
            "daily_target_pct": round(self.daily_profit_target_pct, 1) if daily_target else 0.0,
            "trading_days": days,
            "days_hit_target": days_hit_target,
            "best_day": best_day,
            "worst_day": worst_day,
            "avg_daily_profit": avg_daily,
            "avg_daily_pct": avg_daily_pct,
            "daily_breakdown": daily_breakdown,
        }

    @staticmethod
    def _stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(trades)
        if total == 0:
            return {"total": 0, "wins": 0, "losses": 0, "breakevens": 0, "decided": 0,
                    "win_rate": 0.0,
                    "total_r": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0,
                    "max_drawdown_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0}
        wins = [t for t in trades if t["outcome"] == "win"]
        losses = [t for t in trades if t["outcome"] == "loss"]
        breakevens = [t for t in trades if t["outcome"] == "breakeven"]
        decided = len(wins) + len(losses)
        gross_win = sum(t["r_multiple"] for t in wins)
        gross_loss = abs(sum(t["r_multiple"] for t in losses))
        total_r = sum(t["r_multiple"] for t in trades)
        # Equity curve in R for drawdown.
        peak = 0.0
        equity = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t["r_multiple"]
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(breakevens),
            "decided": decided,
            # Win-rate is measured over *decided* trades (wins vs losses);
            # breakeven scratches are protected exits, neither win nor loss.
            "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
            "total_r": round(total_r, 2),
            "expectancy_r": round(total_r / total, 3),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (gross_win if gross_win else 0.0),
            "max_drawdown_r": round(max_dd, 2),
            "avg_win_r": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss_r": round(-gross_loss / len(losses), 2) if losses else 0.0,
        }


def candles_from_payload(rows: List[Dict[str, Any]]) -> List[Candle]:
    """Build sorted Candle list from API/db rows."""
    out: List[Candle] = []
    for r in rows:
        try:
            out.append(Candle(
                time=int(r["time"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume") or 0.0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.time)
    return out


# Singleton with sensible defaults.
smc_engine = SMCStrategyEngine()
