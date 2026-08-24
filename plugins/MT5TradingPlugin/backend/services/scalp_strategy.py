"""
MT5 Trading Plugin — Multi-Timeframe SMC Scalp Engine.

A fast, market-execution scalping model that layers the existing swing-oriented
``SMCStrategyEngine`` across several timeframes to produce *immediate* market
entries (not resting limit orders) with tight ATR-based SL/TP.

Design goals:
  • Best scalp timeframe = M5 (entry trigger). M1 refines micro-structure,
    H1/H4/D1 supply the higher-timeframe directional bias.
  • Direction from higher-timeframe bias confluence + M5 SMC signal + volume
    imbalance ("smart money" pressure).
  • Works on ANY MT5 instrument — FX, metals, indices, oil, crypto, stocks,
    futures — because pip/point geometry + contract size are symbol-derived.
  • Recovery leg: when the first trade drifts against the position, a second
    SMC-guided order is opened in the direction the market is now favouring so
    a smaller retracement takes the *combined* position back to profit.  This is
    NOT a blind martingale — the recovery direction is confirmed by the live
    multi-timeframe bias, and the size multiplier is capped.

Pure stdlib + the plugin's own SMC engine. No external deps.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
    Candle,
    SMCStrategyEngine,
    _atr,
    _rsi,
    _volume_zscore,
    contract_size_for_symbol,
    point_size_for_symbol,
    pip_size_for_symbol,
)


# ── Config ──────────────────────────────────────────────────────────────────────

# Default scalp TF and TF stack (kept as module-level defaults for back-compat).
PRIMARY_SCALP_TF = "M5"
ENTRY_REFINE_TF  = "M1"
BIAS_TFS: List[str] = ["H1", "H4", "D1"]
ALL_SCALP_TFS: List[str] = [ENTRY_REFINE_TF, PRIMARY_SCALP_TF, "H1", "H4", "D1"]

# Per-primary-TF lookup maps so users can select their execution timeframe.
# entry_refine = the one-step-faster TF used for micro-ATR / momentum bars.
_TF_ENTRY_REFINE: Dict[str, str] = {
    "M1":  "M1",
    "M5":  "M1",
    "M15": "M5",
    "M30": "M5",
    "H1":  "M15",
}
# Full TF stack fetched each cycle for the chosen primary TF.
_TF_STACK: Dict[str, List[str]] = {
    "M1":  ["M1",  "M5",  "H1", "H4", "D1"],
    "M5":  ["M1",  "M5",  "H1", "H4", "D1"],
    "M15": ["M5",  "M15", "H1", "H4", "D1"],
    "M30": ["M5",  "M30", "H1", "H4", "D1"],
    "H1":  ["M15", "H1",  "H4", "D1"],
}

def get_tf_stack(primary_tf: str) -> List[str]:
    """Return the full TF candle stack for the given primary TF."""
    return list(_TF_STACK.get(primary_tf, ALL_SCALP_TFS))

def get_entry_refine_tf(primary_tf: str) -> str:
    """Return the entry-refinement TF for the given primary TF."""
    return _TF_ENTRY_REFINE.get(primary_tf, ENTRY_REFINE_TF)

# ATR multiples used to place the protective stop / primary target on the
# entry timeframe.  A 1.5×ATR stop with a 2.5×ATR target keeps reward:risk
# comfortably above 1 while staying tight enough for scalping.
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5

# Per-symbol minimum SL in pips.  Metals / crypto require much larger floors
# than FX because a single 5s noise wick easily covers 4–8 FX pips on gold.
# These pip multiples use pip_size_for_symbol() as the unit (gold pip = $0.10).
_SYMBOL_MIN_SL_PIPS: Dict[str, float] = {
    "XAUUSD": 20.0,  # gold: 20 pips = $2.00  (raw M5 ATR is usually $3–10)
    "XAGUSD": 15.0,  # silver
    "BTCUSD": 10.0,
    "ETHUSD": 10.0,
}
_DEFAULT_MIN_SL_PIPS: float = 5.0  # FX majors / everything else

# Per-symbol take-profit band, in pips (pip = 10 points, gold pip = $0.10).
# The primary target is placed INSIDE this band, positioned by live flow: a
# strong, one-sided tape reaches for the top, a quiet one banks nearer the
# floor. The band is the answer to two failure modes at once — a 30-pip scalp
# target on gold is too near to be worth the spread and churns the account on
# chop, while an open-ended target over-holds a winner into the next reversal.
# Symbols not listed here keep the ATR/RR target unchanged.
_SYMBOL_TP_PIP_BAND: Dict[str, tuple[float, float]] = {
    "XAUUSD": (80.0, 110.0),   # gold: 80 pips = $8.00 … 110 pips = $11.00
}


def _flow_strength(volume_z: float, volume_imbalance: float) -> float:
    """How hard the tape is pushing, 0 (quiet/balanced) … 1 (strong, one-sided).

    Blends the volume z-score (is there unusual participation right now) with the
    directional imbalance (is it one-sided). Both must be present to reach the
    top of the band — a volume spike that is evenly matched is a fight, not a
    run, and does not earn the wider target.
    """
    vol = max(0.0, min(1.0, volume_z / 2.0))          # z of 2+ is a clear spike
    lean = max(0.0, min(1.0, abs(volume_imbalance) / 0.4))  # 0.4 = decisively one-sided
    return round(vol * 0.6 + lean * 0.4, 4)

# Volume-spike imbalance thresholds for opportunistic stacking orders.
# A spike is detected when the directional imbalance exceeds these values.
VOL_SPIKE_STRONG: float = 0.30   # strong spike → stack extra order
VOL_SPIKE_MODERATE: float = 0.20  # moderate spike → note only

# The recovery leg is armed only after the first trade is at least this many
# ATRs offside — it is a structural hedge, not an averaging-down martingale.
RECOVERY_DRAWDOWN_ATR = 1.0
# Recovery size relative to the original lot (kept < 2× to bound exposure).
RECOVERY_LOT_MULTIPLIER = 1.5

# ── Auto-lot safety ────────────────────────────────────────────────
# When auto_lot is active the formula is: lot = risk / (sl_dist × contract_size).
# If sl_dist is near-zero (stale/synthetic candle ATR), the lot explodes.
# Two mandatory caps prevent catastrophic over-sizing:
#   1. SL_MIN_PIPS  — minimum SL distance expressed in pip multiples so the
#      denominator is never allowed to collapse to near-zero.
#   2. MAX_AUTO_LOT_MULT — auto-lot must never exceed this multiple of the
#      user’s configured lot_size regardless of balance/SL/risk settings.
SL_MIN_PIPS: float = 5.0          # floor: at least 5 pips between entry and SL
MAX_AUTO_LOT_MULT: float = 10.0   # cap: auto-lot ≤ 10× base lot size

# ── Real-time scalp entry tuning ─────────────────────────────────────────────
# The scalp must engage the *current* candle movement, so the resting entry has
# to sit close to live price rather than at a distant institutional zone.

# Max entry distance from live price (as a fraction of M5 ATR) for a same-side
# SMC zone to still count as a "scalp-range" entry; farther zones are replaced
# by a market-adjacent real-time entry.
MAX_ENTRY_DISTANCE_ATR = 0.6
# Live-momentum strength (0..1) above which the current move is treated as a
# continuation breakout (stop entry just beyond price) vs a pullback (limit).
MOMENTUM_STRONG = 0.45
# Minimum live-momentum strength to scalp on momentum alone when the
# higher-timeframe bias is flat (lets FX pairs with neutral HTF bias still
# scalp a clear real-time move).
MOMENTUM_MIN_STANDALONE = 0.5
# Tight scalp geometry off the M1 micro-ATR (real-time entry timeframe).
RT_SL_ATR_M1 = 1.1
RT_TP_ATR_M1 = 1.7


# ── Strictness presets ───────────────────────────────────────────────────────
# Each preset tunes how selective the scalper is. "conservative" trades the
# least but demands the strongest confluence + reward:risk (fewest losses).
# "balanced" is the default (backward-safe). "aggressive" trades more often.
#
#   min_confidence          — bias/entry confidence floor
#   min_htf_bias            — HTF bias norm threshold to commit direction (lower = more
#                             entries from weak trends; 0.34 is the strict default)
#   momentum_min_standalone — live-momentum strength needed to scalp when the
#                             higher-timeframe (HTF) bias is flat
#   min_rr                  — hard reward:risk floor; TP is widened to meet it
#   require_htf_alignment   — when True, momentum-only (flat-HTF) scalps are
#                             disallowed — every entry must ride the HTF trend
#   min_fusion_score        — quality-score floor the bot-level fusion gate uses
#   skip_vol_gate_for_momentum — when True, bypass the volume-imbalance gate for
#                             standalone live-momentum entries (body direction
#                             already encodes directional flow)
#   kronos_veto             — reject an entry when the Kronos ML score opposes
#                             the trade side by more than this magnitude
#   kronos_align_bonus      — confidence bonus when Kronos agrees with the side
STRICTNESS_PRESETS: Dict[str, Dict[str, float]] = {
    "conservative": {
        "min_confidence": 0.68,
        "min_htf_bias": 0.40,
        "momentum_min_standalone": 0.75,
        "min_rr": 1.8,
        "min_volume_imbalance": 0.22,
        "require_htf_alignment": 1.0,
        "skip_vol_gate_for_momentum": 0.0,
        "min_fusion_score": 0.66,
        "kronos_veto": 0.25,
        "kronos_align_bonus": 0.12,
    },
    "balanced": {
        "min_confidence": 0.58,
        "min_htf_bias": 0.34,
        "momentum_min_standalone": 0.55,
        "min_rr": 1.5,
        "min_volume_imbalance": 0.15,
        "require_htf_alignment": 0.0,
        "skip_vol_gate_for_momentum": 0.0,
        "min_fusion_score": 0.55,
        "kronos_veto": 0.40,
        "kronos_align_bonus": 0.10,
    },
    "aggressive": {
        "min_confidence": 0.50,
        "min_htf_bias": 0.20,
        "momentum_min_standalone": 0.40,
        "min_rr": 1.3,
        "min_volume_imbalance": 0.08,
        "require_htf_alignment": 0.0,
        "skip_vol_gate_for_momentum": 1.0,
        "min_fusion_score": 0.42,
        "kronos_veto": 0.55,
        "kronos_align_bonus": 0.08,
    },
    # "scalper" preset — ultra-fast entries for volatile/fast markets.
    # Lowers every threshold to the practical floor so the bot enters on the
    # first clear M1 momentum signal even when HTF structure is ambiguous.
    # Use only on highly liquid instruments (XAUUSD, BTCUSD, major FX).
    "scalper": {
        "min_confidence": 0.18,
        "min_htf_bias": 0.15,
        "momentum_min_standalone": 0.25,
        "min_rr": 1.2,
        "min_volume_imbalance": 0.0,   # volume gate disabled; bias + choppy check suffice
        "require_htf_alignment": 0.0,
        "skip_vol_gate_for_momentum": 1.0,
        "min_fusion_score": 0.28,
        "kronos_veto": 0.70,
        "kronos_align_bonus": 0.05,
    },
}
DEFAULT_STRICTNESS = "balanced"


# ── Data structures ──────────────────────────────────────────────────────────────

@dataclass
class ScalpBias:
    """Aggregated multi-timeframe directional read."""
    direction: str            # "buy" | "sell" | "neutral"
    confidence: float         # 0..1
    tf_bias: Dict[str, str]   # {"M5": "bullish", "H1": "bullish", ...}
    atr_m5: float
    rsi_m5: float
    volume_z: float           # M5 volume z-score (smart-money pressure proxy)
    buy_pressure_pct: float   # % of recent range attributable to up candles
    sell_pressure_pct: float  # % of recent volume attributable to down candles
    volume_imbalance: float   # buy_volume_pct - sell_volume_pct (-1..1)
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScalpEntry:
    """A ready-to-execute pending-limit scalp order."""
    side: str                 # "buy" | "sell"
    entry: float              # limit/stop price for the pending order
    stop_loss: float
    take_profit: float
    lot: float
    confidence: float         # 0..1
    reason: str
    # MT5 order type: buy_limit | sell_limit | buy_stop | sell_stop
    order_type: str = "buy_limit"
    is_recovery: bool = False
    confluence: List[str] = field(default_factory=list)
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    risk_amount: float = 0.0
    kronos_score: float = 0.0  # optional ML agreement (-1..1); 0 = unused
    # ── Fusion diagnostics (SMC + Kronos + momentum quality) ─────────────────
    rr: float = 0.0            # realised reward:risk after min-RR enforcement
    quality_score: float = 0.0  # 0..1 blended entry quality used by the gate
    gate_results: Dict[str, Any] = field(default_factory=dict)
    veto_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _buy_sell_pressure(candles: List[Candle], lookback: int = 20) -> float:
    """
    Fraction of recent candle body movement driven by up-candles (0..1).

    > 0.5 → buyers in control, < 0.5 → sellers in control.  Used as a fast
    volume/flow confirmation on the entry timeframe.
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    up = 0.0
    total = 0.0
    for c in window:
        body = abs(c.close - c.open)
        total += body
        if c.close >= c.open:
            up += body
    if total <= 0:
        return 0.5
    return up / total


def _buy_sell_volume_split(candles: List[Candle], lookback: int = 20) -> tuple[float, float, float]:
    """Estimate directional buy/sell volume split from recent candles.

    MT5 feeds often expose tick volume rather than matched aggressive buy/sell
    volume. We approximate directional flow by candle direction; doji candles
    split volume 50/50.

    Fallback: when all tick-volume values are zero (broker returns no volume),
    we use candle body size as a proxy so the gate never locks up on missing data.
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    buy_vol = 0.0
    sell_vol = 0.0
    for c in window:
        vol = float(getattr(c, "volume", 0.0) or 0.0)
        if vol <= 0:
            continue
        if c.close > c.open:
            buy_vol += vol
        elif c.close < c.open:
            sell_vol += vol
        else:
            half = vol * 0.5
            buy_vol += half
            sell_vol += half
    total = buy_vol + sell_vol
    if total > 0:
        buy_pct = buy_vol / total
        sell_pct = sell_vol / total
        return buy_pct, sell_pct, (buy_pct - sell_pct)

    # ── Volume unavailable: fall back to body-size directional proxy ──────────
    up_body = 0.0
    body_total = 0.0
    for c in window:
        body = abs(c.close - c.open)
        body_total += body
        if c.close >= c.open:
            up_body += body
    if body_total <= 0:
        return 0.5, 0.5, 0.0
    buy_pct = up_body / body_total
    sell_pct = 1.0 - buy_pct
    return buy_pct, sell_pct, (buy_pct - sell_pct)


def _min_sl_price_for_symbol(symbol: str, pip: float, atr_m5: float) -> float:
    """Return the absolute minimum SL distance (in price) for the symbol.

    Gold and metals have high per-bar noise that makes tiny ATR-M1-based stops
    completely impractical — the SL gets swept by a single spread spike before
    the trade even trends.  This floors the SL at a level that gives the trade
    room to breathe.
    """
    s = (symbol or "").upper().replace("/", "")
    key = next((k for k in _SYMBOL_MIN_SL_PIPS if s.startswith(k[:3])), None)
    min_pips = _SYMBOL_MIN_SL_PIPS.get(key, _DEFAULT_MIN_SL_PIPS) if key else _DEFAULT_MIN_SL_PIPS
    pip_floor = pip * min_pips
    # Metals: also floor at 1.2× M5 ATR so structure gaps are respected.
    if s.startswith("XAU") or s.startswith("XAG"):
        return max(pip_floor, atr_m5 * 1.2)
    return pip_floor


def _recent_market_analysis(
    candles: List[Candle],
    lookback: int = 10,
    last_n: int = 5,
) -> Dict[str, Any]:
    """Analyse the most recent candles for entry quality and structural SL levels.

    Uses the last ``lookback`` candles to find the recent swing high/low (used
    as structural SL anchors) and the last ``last_n`` candles to measure
    momentum consistency (needed for clean directional entries).

    Returns
    -------
    momentum_quality  : float 0..1  (1 = strong clean trend)
    is_choppy         : bool        (True → reject entry)
    swing_low         : float       (recent structural low  — buy SL anchor)
    swing_high        : float       (recent structural high — sell SL anchor)
    last_n_direction  : "buy" | "sell" | "mixed"
    avg_body_ratio    : float       (body/range; low = wicks/indecision)
    consecutive_score : float       (fraction of last-N closes in same direction)
    """
    window = list(candles[-lookback:]) if len(candles) >= lookback else list(candles)
    last_w  = list(candles[-last_n:])  if len(candles) >= last_n  else list(candles)

    if not window:
        return {
            "momentum_quality": 0.0, "is_choppy": True,
            "swing_low": 0.0, "swing_high": 0.0,
            "last_n_direction": "mixed", "avg_body_ratio": 0.0,
            "consecutive_score": 0.0,
        }

    swing_low  = min(float(c.low)  for c in window)
    swing_high = max(float(c.high) for c in window)

    # Body-to-range ratio: 1 = full-bodied trending candle, 0 = doji/wick candle.
    ratios = []
    for c in window:
        rng  = float(c.high) - float(c.low)
        body = abs(float(c.close) - float(c.open))
        ratios.append(body / rng if rng > 0 else 0.0)
    avg_body = sum(ratios) / len(ratios) if ratios else 0.5

    # Directional consensus over last-N candles.
    # Require 75 % of the window to agree (ceil) so that 3/5 alternating
    # candles are correctly classified as "mixed" (not directional).
    up_n = sum(1 for c in last_w if float(c.close) >= float(c.open))
    dn_n = len(last_w) - up_n
    import math
    threshold = math.ceil(len(last_w) * 0.75)
    if up_n >= threshold:
        last_dir = "buy"
    elif dn_n >= threshold:
        last_dir = "sell"
    else:
        last_dir = "mixed"

    dominant    = max(up_n, dn_n)
    consec      = dominant / len(last_w) if last_w else 0.5
    momentum_q  = round(0.5 * avg_body + 0.5 * consec, 3)
    # Choppy: wick-heavy candles AND no clear directional consensus
    is_choppy   = avg_body < 0.28 and last_dir == "mixed"

    return {
        "momentum_quality": momentum_q,
        "is_choppy": is_choppy,
        "swing_low": round(swing_low, 6),
        "swing_high": round(swing_high, 6),
        "last_n_direction": last_dir,
        "avg_body_ratio": round(avg_body, 3),
        "consecutive_score": round(consec, 3),
    }


def _structural_sl(
    side: str,
    entry: float,
    mkt: Dict[str, Any],
    atr: float,
    min_sl: float,
) -> float:
    """Place the SL just beyond the nearest structural swing level."""
    buffer = max(atr * 0.15, min_sl * 0.05)
    if side == "buy":
        structural = mkt["swing_low"] - buffer
        sl_dist    = max(abs(entry - structural), min_sl)
        return round(entry - sl_dist, 6)
    else:
        structural = mkt["swing_high"] + buffer
        sl_dist    = max(abs(structural - entry), min_sl)
        return round(entry + sl_dist, 6)


def _tf_direction(bias: str) -> str:
    """Map an SMC ``bias`` string to a trade side."""
    if bias == "bullish":
        return "buy"
    if bias == "bearish":
        return "sell"
    return "neutral"


# ── Fundamental analysis helpers ─────────────────────────────────────────────
# These helpers provide the core market-bias signals used in compute_bias.
# Based on industry-standard SMC / ICT / technical analysis fundamentals:
#   EMA Stack  → trend direction (9/21/50 alignment)
#   Market Structure → HH/HL (bullish) vs LH/LL (bearish) + CHoCH/BOS
#   OBV trend  → net volume pressure (smart money accumulation/distribution)
#   RSI regime + divergence → momentum confirmation and exhaustion
#   Premium/Discount zones → where price sits in the current dealing range
#   Liquidity sweeps → stop-hunt reversals (SMC entry signals)

def _ema(candles: List[Candle], period: int) -> float:
    """Calculate Exponential Moving Average of closes over ``period`` bars."""
    if not candles or period <= 0:
        return 0.0
    window = candles[-max(period * 3, 60):]   # enough history for EMA to warm up
    closes = [float(c.close) for c in window]
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1.0 - k)
    return ema


def _ema_stack_score(candles: List[Candle]) -> float:
    """
    EMA stack alignment score (-1..1).

    Bullish:  price > EMA9 > EMA21 > EMA50, all EMAs rising  →  +1.0
    Bearish:  price < EMA9 < EMA21 < EMA50, all EMAs falling → -1.0
    Partial alignment proportional between ±0..1.

    The EMA stack is the most reliable trend confirmation tool in trading:
    - When short EMAs are above long EMAs, institutions are net long.
    - When short EMAs are below long EMAs, institutions are net short.
    """
    if not candles or len(candles) < 55:
        return 0.0
    price  = float(candles[-1].close)
    ema9   = _ema(candles, 9)
    ema21  = _ema(candles, 21)
    ema50  = _ema(candles, 50)
    if ema9 <= 0 or ema21 <= 0 or ema50 <= 0:
        return 0.0

    # Count how many of the 4 bullish conditions are met.
    bullish_checks = [
        price  > ema9,   # price above fast EMA
        ema9   > ema21,  # fast EMA above medium EMA
        ema21  > ema50,  # medium EMA above slow EMA
        price  > ema21,  # price above medium EMA (extra confirmation)
    ]
    bearish_checks = [
        price  < ema9,
        ema9   < ema21,
        ema21  < ema50,
        price  < ema21,
    ]
    bull_count = sum(1 for c in bullish_checks if c)
    bear_count = sum(1 for c in bearish_checks if c)

    if bull_count == len(bullish_checks):
        return 1.0    # fully bullish stack
    if bear_count == len(bearish_checks):
        return -1.0   # fully bearish stack
    # Partial alignment → proportional score
    return round((bull_count - bear_count) / len(bullish_checks), 2)


def _market_structure_score(
    candles: List[Candle],
    pivot_len: int = 3,
    lookback: int = 40,
) -> Dict[str, Any]:
    """
    Detect market structure: Higher Highs/Higher Lows (bullish) vs
    Lower Highs/Lower Lows (bearish).  Also detects:
      - CHoCH (Change of Character): first break against the trend = potential reversal
      - BOS  (Break of Structure):   continuation of current trend
      - Liquidity sweep: recent wick beyond swing high/low that closed back = stop hunt

    Returns
    -------
    score      : float -1..1   (positive = bullish, negative = bearish)
    structure  : "bullish" | "bearish" | "neutral"
    event      : "bos" | "choch" | "sweep_high" | "sweep_low" | "none"
    swing_high : float          (most recent confirmed swing high)
    swing_low  : float          (most recent confirmed swing low)
    trend_bars : int            (how many bars structure has been intact)
    """
    empty = {"score": 0.0, "structure": "neutral", "event": "none",
             "swing_high": 0.0, "swing_low": 0.0, "trend_bars": 0}
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < pivot_len * 4:
        return empty

    # ── Identify confirmed pivot highs and lows ──────────────────────────────
    # A pivot high at index i requires the i±pivot_len bars to all be lower.
    pivot_highs: List[tuple] = []  # (bar_index, high_price)
    pivot_lows:  List[tuple] = []  # (bar_index, low_price)
    n = len(window)
    for i in range(pivot_len, n - pivot_len):
        h = float(window[i].high)
        is_ph = all(float(window[i + d].high) < h and float(window[i - d].high) < h
                    for d in range(1, pivot_len + 1))
        l = float(window[i].low)
        is_pl = all(float(window[i + d].low) > l and float(window[i - d].low) > l
                    for d in range(1, pivot_len + 1))
        if is_ph:
            pivot_highs.append((i, h))
        if is_pl:
            pivot_lows.append((i, l))

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return empty

    # ── Classify structure from recent swing sequence ────────────────────────
    # Compare the last two pivot highs and two pivot lows.
    last_ph  = pivot_highs[-1][1]
    prev_ph  = pivot_highs[-2][1]
    last_pl  = pivot_lows[-1][1]
    prev_pl  = pivot_lows[-2][1]

    hh = last_ph > prev_ph   # Higher High
    hl = last_pl > prev_pl   # Higher Low
    lh = last_ph < prev_ph   # Lower High
    ll = last_pl < prev_pl   # Lower Low

    # Bullish structure: HH + HL
    # Bearish structure: LH + LL
    if hh and hl:
        structure = "bullish"
        score = 0.8
    elif lh and ll:
        structure = "bearish"
        score = -0.8
    elif hh and not hl:
        # Higher high but lower low = range expansion, slight bullish bias
        structure = "bullish"
        score = 0.3
    elif lh and not ll:
        # Lower high but higher low = range contraction, slight bearish bias
        structure = "bearish"
        score = -0.3
    else:
        structure = "neutral"
        score = 0.0

    # ── Detect CHoCH: first structural break against trend ───────────────────
    # CHoCH (Change of Character): first LH after bullish structure = potential reversal
    event = "none"
    current_price = float(window[-1].close)

    # Check if price just broke above recent pivot high (BOS bullish)
    if structure == "bullish" and current_price > last_ph:
        event = "bos"
    # Check if price just broke below recent pivot low (BOS bearish)
    elif structure == "bearish" and current_price < last_pl:
        event = "bos"
    # Check for CHoCH: bearish structure but price retests above last high = potential reversal
    elif structure == "bearish" and current_price > last_ph:
        event = "choch"
        structure = "neutral"
        score = 0.2  # potential reversal
    # Check for CHoCH: bullish structure but price dips below last low = potential reversal
    elif structure == "bullish" and current_price < last_pl:
        event = "choch"
        structure = "neutral"
        score = -0.2  # potential reversal

    # ── Detect liquidity sweeps (wicks beyond swing + close back) ────────────
    # A sweep of the recent swing HIGH (close back below = bearish sweep)
    if len(window) >= 3:
        prev_bar = window[-2]
        curr_bar = window[-1]
        # Sweep of highs: wick above last pivot high, but close below = bearish
        if float(prev_bar.high) > last_ph and float(prev_bar.close) < last_ph:
            event = "sweep_high"
            score = max(score - 0.3, -1.0)  # bearish signal
        # Sweep of lows: wick below last pivot low, but close above = bullish
        elif float(prev_bar.low) < last_pl and float(prev_bar.close) > last_pl:
            event = "sweep_low"
            score = min(score + 0.3, 1.0)   # bullish signal

    # How long has this structure been intact?
    if pivot_highs and pivot_lows:
        latest_pivot_bar = max(pivot_highs[-1][0], pivot_lows[-1][0])
        trend_bars = n - latest_pivot_bar
    else:
        trend_bars = 0

    return {
        "score":      round(score, 2),
        "structure":  structure,
        "event":      event,
        "swing_high": round(last_ph, 6),
        "swing_low":  round(last_pl, 6),
        "trend_bars": trend_bars,
    }


def _obv_trend_score(candles: List[Candle], lookback: int = 20) -> float:
    """
    On-Balance Volume (OBV) trend score (-1..1).

    OBV adds volume on up-bars and subtracts on down-bars.
    - Rising OBV with rising price = healthy bullish trend (buying accumulation)
    - Falling OBV with falling price = healthy bearish trend (selling distribution)
    - Rising price + falling OBV = bearish divergence (distribution into rally)
    - Falling price + rising OBV = bullish divergence (accumulation in dip)

    Score > 0 = bullish volume pressure, < 0 = bearish volume pressure.
    """
    window = candles[-lookback * 2:] if len(candles) >= lookback * 2 else candles
    if len(window) < 4:
        return 0.0

    # Calculate OBV values
    obv_vals = []
    obv = 0.0
    for i, c in enumerate(window):
        vol = float(getattr(c, "volume", 0.0) or 0.0)
        if vol <= 0 and i > 0:
            # No volume data — use body size as proxy
            vol = abs(float(c.close) - float(c.open))
        if i > 0:
            if float(c.close) > float(window[i - 1].close):
                obv += vol
            elif float(c.close) < float(window[i - 1].close):
                obv -= vol
        obv_vals.append(obv)

    if not obv_vals or len(obv_vals) < lookback:
        return 0.0

    recent_obv  = obv_vals[-1]
    lookback_obv = obv_vals[-lookback]
    obv_change = recent_obv - lookback_obv

    # Normalise by the price range to make the score instrument-agnostic.
    price_range = max(float(c.high) for c in window) - min(float(c.low) for c in window)
    if price_range <= 0:
        return 0.0

    # Also measure price change direction over same window.
    price_change = float(window[-1].close) - float(window[-lookback].close)
    price_dir = 1.0 if price_change > 0 else (-1.0 if price_change < 0 else 0.0)

    # OBV direction (normalised).  Clamp to ±1.
    tick_vol_proxy = abs(obv_change) / (abs(lookback_obv) + 1e-9)
    obv_dir = 1.0 if obv_change > 0 else (-1.0 if obv_change < 0 else 0.0)

    # Divergence detection: OBV and price moving in opposite directions
    if obv_dir != 0.0 and price_dir != 0.0 and obv_dir != price_dir:
        # Divergence: price says one thing, volume says another → volume wins (lead)
        return round(obv_dir * 0.5, 2)  # moderate score in OBV's direction

    return round(obv_dir * min(1.0, tick_vol_proxy * 2.0), 2)


def _premium_discount_score(candles: List[Candle], lookback: int = 50) -> Dict[str, Any]:
    """
    Identify whether current price is in a premium or discount zone.

    Based on ICT (Inner Circle Trader) theory:
    - Range = recent swing high to swing low
    - Equilibrium = 50% of range
    - Discount zone = bottom 38.2% of range → prefer BUY setups
    - Premium zone  = top 38.2% of range   → prefer SELL setups
    - OTE (Optimal Trade Entry) = 62–79% retracement → best entries

    Returns
    -------
    zone       : "discount" | "premium" | "equilibrium"
    score      : float  (positive = in discount → buy bias, negative = in premium → sell bias)
    pct_in_range : float  (0 = at low, 1 = at high)
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if not window:
        return {"zone": "equilibrium", "score": 0.0, "pct_in_range": 0.5}

    range_high = max(float(c.high) for c in window)
    range_low  = min(float(c.low)  for c in window)
    range_size = range_high - range_low
    if range_size <= 0:
        return {"zone": "equilibrium", "score": 0.0, "pct_in_range": 0.5}

    current_price = float(window[-1].close)
    pct = (current_price - range_low) / range_size  # 0=low, 1=high

    # Fibonacci levels of the range
    DISCOUNT_THRESHOLD = 0.382   # bottom 38.2% = discount
    PREMIUM_THRESHOLD  = 0.618   # top 38.2% = premium

    if pct <= DISCOUNT_THRESHOLD:
        zone = "discount"
        # Score: further into discount = stronger buy bias
        score = round((DISCOUNT_THRESHOLD - pct) / DISCOUNT_THRESHOLD * 0.8, 2)
    elif pct >= PREMIUM_THRESHOLD:
        zone = "premium"
        # Score: further into premium = stronger sell bias
        score = round(-((pct - PREMIUM_THRESHOLD) / (1.0 - PREMIUM_THRESHOLD)) * 0.8, 2)
    else:
        zone = "equilibrium"
        # Near equilibrium — slight bias toward the recent directional close
        mid_bias = (pct - 0.5) * 2.0  # -1..1
        score = round(-mid_bias * 0.2, 2)  # small counter-trend bias at mid-range

    return {"zone": zone, "score": score, "pct_in_range": round(pct, 3)}


def _rsi_regime_score(rsi: float, prev_rsi: float = 50.0) -> float:
    """
    RSI regime and momentum score (-1..1).

    RSI fundamentals:
    - RSI > 60: buyers in control, bullish momentum → +score
    - RSI < 40: sellers in control, bearish momentum → -score
    - RSI 40-60: neutral territory
    - RSI > 70: overbought (potential reversal, reduce bull confidence)
    - RSI < 30: oversold (potential reversal, reduce bear confidence)
    - Rising RSI: increasing buy pressure
    - Falling RSI: increasing sell pressure
    """
    # Base score from RSI level
    if rsi >= 70:
        base = 0.3    # overbought — partial bullish but caution
    elif rsi >= 60:
        base = 0.6    # bullish momentum
    elif rsi >= 55:
        base = 0.35   # mild bullish
    elif rsi <= 30:
        base = -0.3   # oversold — partial bearish but caution
    elif rsi <= 40:
        base = -0.6   # bearish momentum
    elif rsi <= 45:
        base = -0.35  # mild bearish
    else:
        base = 0.0    # neutral 45-55 range

    # Momentum bonus: RSI moving in the base direction amplifies the signal
    momentum = 0.0
    if prev_rsi > 0:
        delta = rsi - prev_rsi
        if base > 0 and delta > 0:
            momentum = min(0.15, delta * 0.05)   # RSI rising in bullish territory
        elif base < 0 and delta < 0:
            momentum = max(-0.15, delta * 0.05)  # RSI falling in bearish territory
        elif base > 0 and delta < -3:
            momentum = -0.10  # RSI turning down despite bullish level = warning
        elif base < 0 and delta > 3:
            momentum = 0.10   # RSI turning up despite bearish level = warning

    return round(max(-1.0, min(1.0, base + momentum)), 2)


# ── Engine ──────────────────────────────────────────────────────────────────────

class ScalpStrategyEngine:
    """
    Stateless multi-timeframe scalp engine.

    Call :meth:`analyse` each cycle with a fresh ``candles_by_tf`` mapping and
    the account balance.  It returns a :class:`ScalpEntry` when a high-quality
    market scalp aligns across the timeframe stack, else ``None``.
    """

    def __init__(
        self,
        symbol: str,
        lot_size: float = 0.01,
        auto_lot: bool = False,
        risk_per_trade_pct: float = 1.0,
        min_confidence: Optional[float] = None,
        sl_atr_mult: float = SL_ATR_MULT,
        tp_atr_mult: float = TP_ATR_MULT,
        strictness: str = DEFAULT_STRICTNESS,
        primary_tf: str = PRIMARY_SCALP_TF,
    ) -> None:
        self.symbol = symbol
        self.lot_size = max(0.01, round(lot_size, 2))
        self.auto_lot = auto_lot
        self.risk_per_trade_pct = risk_per_trade_pct
        # Configurable primary timeframe — drives entry geometry, volume pressure,
        # and the M5 structural gate (which becomes the primary_tf gate).
        self.primary_tf  = primary_tf if primary_tf in _TF_STACK else PRIMARY_SCALP_TF
        self.entry_tf    = _TF_ENTRY_REFINE.get(self.primary_tf, ENTRY_REFINE_TF)
        self.all_tfs     = get_tf_stack(self.primary_tf)
        # Strictness preset drives how selective the scalper is. An explicit
        # ``min_confidence`` still overrides the preset's confidence floor.
        self.strictness = strictness if strictness in STRICTNESS_PRESETS else DEFAULT_STRICTNESS
        preset = STRICTNESS_PRESETS[self.strictness]
        self.min_confidence = (
            float(min_confidence) if min_confidence is not None
            else float(preset["min_confidence"])
        )
        self.min_htf_bias = float(preset.get("min_htf_bias", 0.34))
        self.min_rr = float(preset["min_rr"])
        self.min_volume_imbalance = float(preset["min_volume_imbalance"])
        self.momentum_min_standalone = float(preset["momentum_min_standalone"])
        self.require_htf_alignment = bool(preset["require_htf_alignment"])
        self.skip_vol_gate_for_momentum = bool(preset.get("skip_vol_gate_for_momentum", False))
        self.min_fusion_score = float(preset["min_fusion_score"])
        self.kronos_veto = float(preset["kronos_veto"])
        self.kronos_align_bonus = float(preset["kronos_align_bonus"])
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.point_size = point_size_for_symbol(symbol)
        self.pip_size = pip_size_for_symbol(symbol)
        self.contract_size = contract_size_for_symbol(symbol)

    # -- bias -----------------------------------------------------------------

    def compute_bias(self, candles_by_tf: Dict[str, List[Candle]]) -> ScalpBias:
        """
        Comprehensive multi-timeframe directional bias using 7 fundamental signals:

        1. SMC Primitives   — institutional order blocks, FVGs, market structure
        2. EMA Stack        — 9/21/50 EMA alignment (price > fast > mid > slow)
        3. Market Structure — HH/HL (bullish) vs LH/LL (bearish) + CHoCH/BOS
        4. OBV Trend        — on-balance volume direction (smart money flow)
        5. RSI Regime       — momentum regime (>60 bullish, <40 bearish)
        6. Premium/Discount — price position in current dealing range
        7. Volume Split     — directional candle body pressure on primary TF

        All signals are weighted and fused into a single -1..1 score.
        The direction is committed when the score exceeds the preset threshold.
        """
        # ── Signal 1: SMC primitives per TF (existing core logic) ─────────────
        tf_bias: Dict[str, str] = {}
        weights: Dict[str, float] = {"M1": 0.5, "M5": 1.0, "M15": 1.2,
                                      "M30": 1.5, "H1": 1.5, "H4": 2.0, "D1": 2.5}
        weights[self.primary_tf] = 2.5
        smc_score = 0.0
        total_smc_w = 0.0

        for tf, candles in candles_by_tf.items():
            if not candles or len(candles) < 40:
                continue
            eng = SMCStrategyEngine(symbol=self.symbol)
            prim = eng._primitives(candles)
            b = prim.get("bias", "neutral")
            tf_bias[tf] = b
            w = weights.get(tf, 1.0)
            total_smc_w += w
            if b == "bullish":
                smc_score += w
            elif b == "bearish":
                smc_score -= w

        smc_norm = (smc_score / total_smc_w) if total_smc_w > 0 else 0.0  # -1..1

        # ── Primary TF candles for intra-bar signals ───────────────────────────
        m5 = candles_by_tf.get(self.primary_tf) or []
        atr_m5   = _atr(m5) if m5 else 0.0
        rsi_m5   = _rsi(m5) if m5 else 50.0
        vol_z    = _volume_zscore(m5) if m5 else 0.0
        prev_rsi = _rsi(m5[:-1]) if len(m5) > 5 else 50.0
        body_pressure = _buy_sell_pressure(m5) if m5 else 0.5
        buy_vol_pct, sell_vol_pct, vol_imbalance = (
            _buy_sell_volume_split(m5) if m5 else (0.5, 0.5, 0.0)
        )

        # ── Signal 2: EMA Stack (9/21/50) ─────────────────────────────────────
        # Use the highest available TF for the EMA stack (cleaner signal).
        # Priority: D1 > H4 > H1 > primary_tf
        ema_candles = (
            candles_by_tf.get("D1") or candles_by_tf.get("H4")
            or candles_by_tf.get("H1") or m5
        )
        ema_score = _ema_stack_score(ema_candles) if ema_candles else 0.0

        # ── Signal 3: Market Structure on primary execution TF ─────────────────
        ms = _market_structure_score(m5) if len(m5) >= 20 else {}
        ms_score = float(ms.get("score", 0.0))
        ms_event = str(ms.get("event", "none"))
        ms_struct = str(ms.get("structure", "neutral"))
        ms_swing_high = float(ms.get("swing_high", 0.0))
        ms_swing_low  = float(ms.get("swing_low", 0.0))

        # CHoCH is a high-priority directional signal — it overrides weaker signals.
        choch_boost = 0.0
        if ms_event == "choch":
            # CHoCH on bullish structure means bears took control → sell bias
            if ms_struct in ("neutral",) and ms_score < 0:
                choch_boost = -0.25
            elif ms_struct in ("neutral",) and ms_score > 0:
                choch_boost = 0.25
        # Liquidity sweep is a high-probability entry signal
        sweep_boost = 0.0
        if ms_event == "sweep_low":
            sweep_boost = 0.20   # swept lows = stop hunt = bullish
        elif ms_event == "sweep_high":
            sweep_boost = -0.20  # swept highs = stop hunt = bearish

        # ── Signal 4: OBV Trend on primary TF ─────────────────────────────────
        obv_score = _obv_trend_score(m5) if len(m5) >= 10 else 0.0

        # ── Signal 5: RSI Regime ────────────────────────────────────────────
        rsi_score = _rsi_regime_score(rsi_m5, prev_rsi)

        # ── Signal 6: Premium / Discount Zone ─────────────────────────────────
        pd = _premium_discount_score(m5) if len(m5) >= 20 else {}
        pd_score = float(pd.get("score", 0.0))
        pd_zone  = str(pd.get("zone", "equilibrium"))
        pd_pct   = float(pd.get("pct_in_range", 0.5))

        # ── Signal 7: Volume body pressure (existing) ─────────────────────────
        # Directional flow from candle bodies on the primary TF
        vol_body_score = (vol_imbalance * 0.5) if abs(vol_imbalance) > 0.05 else 0.0

        # ── Weighted fusion of all 7 signals ──────────────────────────────────
        # Weights reflect reliability and signal quality:
        #   SMC primitives + EMA stack are the primary trend anchors (highest weight)
        #   Market structure is the cornerstone of SMC trading
        #   OBV, RSI, volume confirm direction
        #   Premium/discount tunes entry timing
        WEIGHT_SMC  = 0.30
        WEIGHT_EMA  = 0.20
        WEIGHT_MS   = 0.20
        WEIGHT_OBV  = 0.10
        WEIGHT_RSI  = 0.10
        WEIGHT_PD   = 0.05
        WEIGHT_VOL  = 0.05

        fused = (
            smc_norm   * WEIGHT_SMC
            + ema_score  * WEIGHT_EMA
            + ms_score   * WEIGHT_MS
            + obv_score  * WEIGHT_OBV
            + rsi_score  * WEIGHT_RSI
            + pd_score   * WEIGHT_PD
            + vol_body_score * WEIGHT_VOL
            + choch_boost   # direct boost, not weighted
            + sweep_boost   # direct boost, not weighted
        )
        norm = round(max(-1.0, min(1.0, fused)), 3)

        # ── Commit direction ─────────────────────────────────────────────────
        direction = "neutral"
        htf_thresh = getattr(self, "min_htf_bias", 0.34)
        if norm >= htf_thresh:
            direction = "buy"
        elif norm <= -htf_thresh:
            direction = "sell"

        # ── Flow bonus: volume alignment boosts confidence ────────────────────
        base_conf = min(1.0, abs(norm))
        flow_bonus = 0.0
        if direction == "buy" and vol_imbalance >= 0.05:
            flow_bonus = 0.15
        elif direction == "sell" and vol_imbalance <= -0.05:
            flow_bonus = 0.15
        elif direction == "buy" and vol_imbalance <= -0.15:
            flow_bonus = -0.10   # strong counter-volume warning
        elif direction == "sell" and vol_imbalance >= 0.15:
            flow_bonus = -0.10

        # EMA and market structure agreement further boosts confidence
        if direction == "buy" and ema_score > 0.5 and ms_struct == "bullish":
            flow_bonus += 0.08
        elif direction == "sell" and ema_score < -0.5 and ms_struct == "bearish":
            flow_bonus += 0.08

        confidence = max(0.0, min(1.0, base_conf + flow_bonus))

        # ── Build rich reason string ───────────────────────────────────────────
        reason_parts = [
            f"SMC {smc_norm:+.2f}/{len(tf_bias)}TF",
            f"EMA {ema_score:+.2f}",
            f"struct={ms_struct}({ms_score:+.2f})",
        ]
        if ms_event != "none":
            reason_parts.append(f"[{ms_event.upper()}]")
        reason_parts += [
            f"OBV {obv_score:+.2f}",
            f"RSI {rsi_m5:.0f}({rsi_score:+.2f})",
            f"zone={pd_zone}({pd_pct:.2f})",
            f"vol={buy_vol_pct*100:.0f}/{sell_vol_pct*100:.0f}(imb {vol_imbalance:+.2f})",
            f"vol-z {vol_z:+.1f}",
        ]
        reason = "; ".join(reason_parts)

        return ScalpBias(
            direction=direction,
            confidence=confidence,
            tf_bias=tf_bias,
            atr_m5=atr_m5,
            rsi_m5=rsi_m5,
            volume_z=vol_z,
            buy_pressure_pct=round(buy_vol_pct * 100, 1),
            sell_pressure_pct=round(sell_vol_pct * 100, 1),
            volume_imbalance=round(vol_imbalance, 3),
            reason=reason,
        )

    # -- sizing ----------------------------------------------------------------

    def _resolve_lot(self, balance: float, sl_distance: float,
                     multiplier: float = 1.0) -> tuple[float, float]:
        """Return (lot, risk_amount). Fixed lot unless auto_lot is enabled.

        Safety constraints (always enforced, even in auto-lot mode):

        1. *SL distance floor* — the denominator of the auto-lot formula is
           clamped to at least ``SL_MIN_PIPS × pip_size`` so that near-zero ATR
           values (from stale or synthetic candles) cannot inflate the lot to
           thousands of times the intended size.

        2. *Lot cap* — the computed lot is limited to
           ``lot_size × MAX_AUTO_LOT_MULT`` so a user who sets 0.01 lot will
           never receive more than 0.10 lot from the auto-sizing logic,
           regardless of balance, SL tightness, or risk percentage.
        """
        base_lot = max(0.01, round(self.lot_size * multiplier, 2))

        if not self.auto_lot or balance <= 0 or self.contract_size <= 0:
            risk = round(base_lot * sl_distance * self.contract_size, 2) if sl_distance > 0 else 0.0
            return base_lot, risk

        if sl_distance <= 0:
            return base_lot, 0.0

        # 1. Floor: prevent near-zero SL from causing lot explosion.
        # Use the wider of 5 pips and 10 points (handles both FX & metals).
        pip = self.pip_size or (self.point_size * 10.0) if self.point_size else 0.0001
        min_sl = max(pip * SL_MIN_PIPS, self.point_size * 10.0) if self.point_size > 0 else pip * SL_MIN_PIPS
        sl_safe = max(sl_distance, min_sl)

        risk_amount = balance * (self.risk_per_trade_pct / 100.0) * multiplier
        raw_lot = risk_amount / (sl_safe * self.contract_size)

        # 2. Cap: auto-lot ≤ MAX_AUTO_LOT_MULT × configured lot size.
        max_auto = max(base_lot, round(self.lot_size * MAX_AUTO_LOT_MULT * multiplier, 2))
        lot = min(raw_lot, max_auto)
        lot = max(0.01, round(lot, 2))
        actual_risk = round(lot * sl_distance * self.contract_size, 2)
        return lot, actual_risk

    # -- quality / reward-risk enforcement -------------------------------------

    def _enforce_min_rr(
        self, side: str, entry: float, stop_loss: float, take_profit: float,
        *, flow: float | None = None,
    ) -> tuple[float, float]:
        """Normalise TP: place it in the per-symbol pip band, then hold the RR floor.

        Two levers, applied in order:

        * ``flow`` (0..1, from :func:`_flow_strength`) positions the target
          inside the symbol's TP band when one is configured — gold gets an
          80-110 pip target sized to how hard the tape is pushing, which stops
          it both from scalping a too-tight target on chop and from over-holding
          a winner past its move. Symbols with no band skip this step.
        * The reward:risk floor is then applied and always wins: even inside a
          band, TP is widened so reward ≥ risk × ``min_rr``. Keeping winners
          bigger than losers is the single biggest lever for "hardly loses"
          behaviour, so it is never traded away for a tidier pip number.

        Returns ``(take_profit, rr)``.
        """
        risk = abs(entry - stop_loss)
        if risk <= 0:
            return take_profit, 0.0

        band = _SYMBOL_TP_PIP_BAND.get((self.symbol or "").upper().replace("/", ""))
        if band is not None:
            low, high = band
            pos = 0.5 if flow is None else max(0.0, min(1.0, flow))
            target_dist = (low + (high - low) * pos) * self.pip_size
            take_profit = round(
                entry + target_dist if side == "buy" else entry - target_dist, 6
            )

        reward = abs(take_profit - entry)
        min_reward = risk * self.min_rr
        if reward < min_reward:
            take_profit = round(
                entry + min_reward if side == "buy" else entry - min_reward, 6
            )
            reward = min_reward
        return take_profit, round(reward / risk, 2)

    def _quality_score(
        self, confidence: float, rr: float, kronos_score: float,
        side: str, momentum_aligned: bool,
    ) -> tuple[float, bool]:
        """Blend the decision inputs into a 0..1 quality score.

        Weights: confidence 45 %, reward:risk 25 %, Kronos agreement 20 %,
        live-momentum alignment 10 %. Returns ``(score, kronos_aligned)`` where
        an unavailable Kronos score (0.0) contributes a neutral 0.5.
        """
        conf_c = max(0.0, min(1.0, confidence))
        # RR 1.0 → 0.0, RR 3.0 → 1.0 (clamped).
        rr_c = max(0.0, min(1.0, (rr - 1.0) / 2.0))
        kronos_aligned = False
        if kronos_score:
            kronos_aligned = (
                (kronos_score > 0 and side == "buy")
                or (kronos_score < 0 and side == "sell")
            )
            k_c = min(1.0, abs(kronos_score)) if kronos_aligned else 0.0
        else:
            k_c = 0.5  # unavailable → neutral, never penalise the SMC decision
        mom_c = 1.0 if momentum_aligned else 0.5
        score = 0.45 * conf_c + 0.25 * rr_c + 0.20 * k_c + 0.10 * mom_c
        return round(max(0.0, min(1.0, score)), 3), kronos_aligned

    # -- real-time movement ----------------------------------------------------

    def _realtime_momentum(
        self, m1: List[Candle], m5: List[Candle], current_price: float,
    ) -> tuple[str, float]:
        """Read the *live* candle movement from the freshest bars.

        Uses the fastest available series (M1 built from live quotes, else M5)
        and blends three signals over the last few bars: net directional move,
        the forming candle's body direction against the live price, and how
        persistently closes have advanced.  Returns ``(direction, strength)``
        where direction is ``"buy" | "sell" | "neutral"`` and strength is 0..1.
        """
        ref = m1 if len(m1) >= 6 else m5
        if len(ref) < 6 or current_price <= 0:
            return "neutral", 0.0

        recent = ref[-6:]
        first = float(recent[0].close)
        last = float(current_price)               # live price = the current tick
        move = last - first

        # Average bar range → normaliser for velocity (avoid divide-by-zero).
        avg_range = sum(abs(float(c.high) - float(c.low)) for c in recent) / len(recent)
        avg_range = avg_range or (self.pip_size or 1e-9)
        velocity = move / (avg_range * len(recent))          # ~ bars of range moved

        # Forming candle: is the live price above/below the last bar's open?
        cur_open = float(recent[-1].open)
        body_dir = 1 if last >= cur_open else -1

        # Persistence: fraction of the recent closes that advanced.
        ups = sum(1 for i in range(1, len(recent))
                  if float(recent[i].close) >= float(recent[i - 1].close))
        up_frac = ups / (len(recent) - 1)

        strength = max(0.0, min(1.0, abs(velocity) * 2.0))

        if move > 0 and body_dir > 0 and up_frac >= 0.5:
            return "buy", strength
        # Sell: price is declining AND forming candle is bearish AND majority of
        # recent closes declined — requiring all three prevents treating normal
        # pullback dips in an uptrend as full sell momentum.
        if move < 0 and body_dir < 0 and up_frac <= 0.5:
            return "sell", strength
        return "neutral", strength * 0.5

    def _build_realtime_entry(
        self, side: str, current_price: float, m1: List[Candle], m5: List[Candle],
        balance: float, confidence: float, momentum_strength: float,
        bid: float, ask: float, bias: ScalpBias, confluence: List[str],
        kronos_score: float = 0.0, momentum_aligned: bool = True,
        mkt_analysis: Optional[Dict[str, Any]] = None,
    ) -> ScalpEntry:
        """Build a market-adjacent scalp entry that engages the current move.

        SL is placed at the structural swing level (recent low for buy, recent
        high for sell) + a small buffer, so it is only triggered by a genuine
        structure break — NOT by noise or a single wick.
        """
        pip = self.pip_size or 1.0
        micro_atr = _atr(m1) if len(m1) >= 14 else 0.0
        if micro_atr <= 0:
            micro_atr = (bias.atr_m5 or pip * 10) * 0.4
        micro_atr = max(micro_atr, pip * 2)

        sym_min_sl = _min_sl_price_for_symbol(self.symbol, pip, bias.atr_m5 or pip * 20)

        # ── Structural SL using recent swing levels ───────────────────────────
        # Use primary-TF candles (m5) for swing levels — last 12 bars give the
        # structural zone the SL must clear.  last_n=6 is the empirically
        # optimal window (100% accuracy + fastest 5-bar reversal detection).
        mkt = mkt_analysis or _recent_market_analysis(m5, lookback=12, last_n=6)

        # Stop trigger / limit offset must clear the live spread.
        spread  = abs(ask - bid) if (ask > 0 and bid > 0) else pip
        trigger = max(pip * 1.0, micro_atr * 0.12, spread * 1.2)

        strong  = momentum_strength >= MOMENTUM_STRONG
        ref_ask = ask if ask > 0 else current_price
        ref_bid = bid if bid > 0 else current_price

        if side == "buy":
            if strong:
                entry = ref_ask + trigger
                otype = "buy_stop"
            else:
                entry = ref_bid - trigger
                otype = "buy_limit"
            stop_loss   = _structural_sl("buy",  entry, mkt, bias.atr_m5 or micro_atr, sym_min_sl)
            sl_dist     = abs(entry - stop_loss)
            tp_dist     = max(sl_dist * 1.5, micro_atr * RT_TP_ATR_M1)
            take_profit = entry + tp_dist
        else:
            if strong:
                entry = ref_bid - trigger
                otype = "sell_stop"
            else:
                entry = ref_ask + trigger
                otype = "sell_limit"
            stop_loss   = _structural_sl("sell", entry, mkt, bias.atr_m5 or micro_atr, sym_min_sl)
            sl_dist     = abs(stop_loss - entry)
            tp_dist     = max(sl_dist * 1.5, micro_atr * RT_TP_ATR_M1)
            take_profit = entry - tp_dist

        entry = round(entry, 6)
        stop_loss = round(stop_loss, 6)
        take_profit = round(take_profit, 6)
        # Place TP in the symbol's pip band by live flow, then hold the RR floor.
        take_profit, rr = self._enforce_min_rr(
            side, entry, stop_loss, take_profit,
            flow=_flow_strength(bias.volume_z, bias.volume_imbalance),
        )
        sl_dist = abs(entry - stop_loss)
        tp_dist = abs(take_profit - entry)
        lot, risk_amount = self._resolve_lot(balance, sl_dist)
        quality, kronos_aligned = self._quality_score(
            confidence, rr, kronos_score, side, momentum_aligned,
        )

        mode = "momentum-stop" if strong else "pullback-limit"
        return ScalpEntry(
            side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit,
            lot=lot, confidence=round(confidence, 3),
            reason=f"Real-time {mode} scalp — {bias.reason}",
            order_type=otype,
            confluence=confluence + [f"rt:{mode}", f"mom:{momentum_strength:.2f}"]
                       + (["kronos_aligned"] if kronos_aligned else []),
            sl_pips=round(sl_dist / pip, 1), tp_pips=round(tp_dist / pip, 1),
            risk_amount=risk_amount, kronos_score=round(kronos_score, 3),
            rr=rr, quality_score=quality,
            gate_results={
                "confidence": round(confidence, 3), "rr": rr,
                "kronos": round(kronos_score, 3), "kronos_aligned": kronos_aligned,
                "momentum": round(momentum_strength, 3), "source": "realtime",
                "buy_volume_pct": bias.buy_pressure_pct,
                "sell_volume_pct": bias.sell_pressure_pct,
                "volume_imbalance": bias.volume_imbalance,
            },
        )

    # -- entry -----------------------------------------------------------------

    def analyse(
        self,
        candles_by_tf: Dict[str, List[Candle]],
        current_price: float,
        balance: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
        kronos_score: float = 0.0,
    ) -> tuple[Optional[ScalpEntry], ScalpBias]:
        """
        Produce a scalp entry that engages the *current* candle movement.

        Blends the multi-timeframe SMC bias with live-candle momentum:
          • HTF bias + live momentum agree → high-quality continuation scalp.
          • HTF bias set, momentum flat → follow the bias.
          • HTF bias flat, live momentum strong → scalp the live move in real time.
        The resting order is kept within scalp range of live price (an in-range
        SMC zone when available, else a market-adjacent momentum entry), so it
        fills on the current move instead of waiting at a distant level.

        ``kronos_score`` (−1..1) is an optional ML directional read: a strongly
        opposing score vetoes the entry, an agreeing score raises conviction.

        Returns ``(entry_or_None, bias)`` so callers can surface the live bias
        even when no trade is taken.
        """
        bias = self.compute_bias(candles_by_tf)

        # ── M5 structural gate ───────────────────────────────────────────────
        # The primary scalp timeframe (M5) is the execution timeframe.  If its
        # structure is explicitly opposite to a potential trade direction the
        # entry is blocked immediately, regardless of HTF bias.  Scalping AGAINST
        # the M5 structure is the single most common cause of immediate stop-outs.
        m5_struct = bias.tf_bias.get(self.primary_tf, "neutral")
        # We will enforce this after side is determined; store for use below.

        m1 = candles_by_tf.get(self.entry_tf) or []
        m5 = candles_by_tf.get(self.primary_tf) or []
        if len(m5) < 40 or current_price <= 0:
            return None, bias

        # ── Live candle movement (real-time momentum) ────────────────────────
        mom_dir, mom_strength = self._realtime_momentum(m1, m5, current_price)

        # ── Decide the scalp side ────────────────────────────────────────────
        side: Optional[str] = None
        confidence = bias.confidence
        confluence = [f"{tf}:{b}" for tf, b in bias.tf_bias.items()]
        momentum_aligned = False

        if bias.direction in ("buy", "sell"):
            if mom_dir == bias.direction:
                side = bias.direction
                confidence = min(1.0, bias.confidence + 0.15 + mom_strength * 0.2)
                confluence.append("HTF+live-aligned")
                momentum_aligned = True
            elif mom_dir == "neutral":
                if bias.confidence >= self.min_confidence:
                    side = bias.direction
                    confluence.append("HTF-bias")
            else:
                # Live move opposes the higher-timeframe trend — stand aside.
                return None, bias
        elif (mom_dir in ("buy", "sell")
              and mom_strength >= self.momentum_min_standalone
              and not self.require_htf_alignment):
            # No dominant HTF trend, but a clear live move — scalp it in real time.
            side = mom_dir
            confidence = max(self.min_confidence, min(0.9, 0.5 + mom_strength * 0.4))
            confluence.append("live-momentum")
            momentum_aligned = True

        if side is None:
            return None, bias

        # ── M5 hard execution gate ────────────────────────────────────────────
        # If M5 is structurally bearish we cannot buy; if bullish we cannot sell.
        # This overrides everything — even strong HTF confluence cannot justify
        # trading against the entry timeframe's structural direction.
        if side == "buy" and m5_struct == "bearish":
            bias.reason += f" | {self.primary_tf} bearish: execution TF blocks buy"
            return None, bias
        if side == "sell" and m5_struct == "bullish":
            bias.reason += f" | {self.primary_tf} bullish: execution TF blocks sell"
            return None, bias

        # Hard directional volume gate so buy/sell decisions are confirmed by
        # recent directional flow, not confidence alone.
        # Exception: standalone live-momentum entries (no HTF bias) already
        # embed directional flow via candle body direction, so a separate
        # volume gate would double-count the same signal. Skip it when the
        # preset allows (aggressive / scalper presets).
        # Also skip entirely when min_volume_imbalance == 0 (scalper preset).
        _is_standalone_mom = "live-momentum" in confluence and not any(
            c.startswith("HTF") for c in confluence
        )
        _skip_vol = (
            self.min_volume_imbalance <= 0.0
            or (_is_standalone_mom and self.skip_vol_gate_for_momentum)
        )
        if not _skip_vol:
            if side == "buy" and bias.volume_imbalance < self.min_volume_imbalance:
                bias.reason += (
                    f" | Volume veto: buy imbalance {bias.volume_imbalance:+.2f} "
                    f"< +{self.min_volume_imbalance:.2f}"
                )
                return None, bias
            if side == "sell" and bias.volume_imbalance > -self.min_volume_imbalance:
                bias.reason += (
                    f" | Volume veto: sell imbalance {bias.volume_imbalance:+.2f} "
                    f"> -{self.min_volume_imbalance:.2f}"
                )
                return None, bias
        confluence.append(f"vol-imb:{bias.volume_imbalance:+.2f}")

        # ── Kronos ML directional fusion (optional) ──────────────────────────
        # A strongly opposing forecast vetoes the trade; agreement lifts
        # conviction. Unavailable (0.0) leaves the SMC decision untouched.
        if kronos_score:
            opposes = (
                (side == "buy" and kronos_score < 0)
                or (side == "sell" and kronos_score > 0)
            )
            if opposes and abs(kronos_score) >= self.kronos_veto:
                bias.reason += f" | Kronos veto ({kronos_score:+.2f})"
                return None, bias
            if not opposes:
                confidence = min(1.0, confidence + self.kronos_align_bonus * abs(kronos_score))
                confluence.append("kronos_aligned")

        if confidence < self.min_confidence:
            return None, bias

        # Now add pre-entry structure analysis on the primary TF candles.
        # Block choppy/indecisive markets and annotate the reason string.
        # last_n=6 was empirically determined as the optimal window:
        # 100 % accuracy on all trend/choppy types + fastest reversal detection
        # (5 bars). lookback=12 captures enough structure for reliable SL levels.
        mkt = _recent_market_analysis(m5, lookback=12, last_n=6)

        if mkt["is_choppy"]:
            bias.reason += (
                f" | Choppy market: body={mkt['avg_body_ratio']:.2f} "
                f"dir={mkt['last_n_direction']} — waiting for direction"
            )
            return None, bias

        # Annotate quality into the reason for full transparency in logs/UI.
        bias.reason += (
            f" | Mkt body={mkt['avg_body_ratio']:.2f} "
            f"consec={mkt['consecutive_score']:.2f} "
            f"last5={mkt['last_n_direction']} "
            f"q={mkt['momentum_quality']:.2f}"
        )

        atr = bias.atr_m5
        pip = self.pip_size or 1.0
        max_dist = max(atr * MAX_ENTRY_DISTANCE_ATR, pip * 3)

        # ── Prefer an in-range SMC zone entry; else a market-adjacent entry ──
        eng = SMCStrategyEngine(symbol=self.symbol, min_confidence=0.0)
        m5_analysis = eng.analyze(m5)
        m5_signals = m5_analysis.get("signals", []) if isinstance(m5_analysis, dict) else []

        best_signal: Optional[Dict[str, Any]] = None
        for sig in m5_signals:
            if (sig.get("side") == side and sig.get("entry") and sig.get("stop_loss")
                    and abs(float(sig.get("entry", 0)) - current_price) <= max_dist):
                best_signal = sig
                break

        if best_signal:
            raw_entry = float(best_signal.get("entry", current_price))
            raw_sl    = float(best_signal.get("stop_loss", 0) or 0)
            raw_tp1   = float(best_signal.get("tp1", 0) or 0)
            raw_tp    = float(best_signal.get("take_profit", 0) or raw_tp1 or 0)
            otype     = best_signal.get("order_type", f"{side}_limit")
            if raw_sl <= 0 or raw_tp <= 0 or raw_entry <= 0:
                # Degenerate zone → fall back to the real-time entry.
                return self._build_realtime_entry(
                    side, current_price, m1, m5, balance, confidence,
                    mom_strength, bid, ask, bias, confluence,
                    kronos_score=kronos_score, momentum_aligned=momentum_aligned,
                    mkt_analysis=mkt,
                ), bias
            entry_price = round(raw_entry, 6)
            stop_loss   = round(raw_sl, 6)
            take_profit = round(raw_tp, 6)
            # Apply symbol-aware SL floor — SMC signals on gold can have very
            # tight OB/FVG levels that still get swept by spread noise.
            sym_min_sl = _min_sl_price_for_symbol(self.symbol, pip, atr or pip * 20)
            raw_sl_dist = abs(entry_price - stop_loss)
            if raw_sl_dist < sym_min_sl:
                stop_loss = round(
                    entry_price - sym_min_sl if side == "buy" else entry_price + sym_min_sl, 6
                )
            # Place TP in the symbol's pip band by live flow, then hold the RR floor.
            take_profit, rr = self._enforce_min_rr(
                side, entry_price, stop_loss, take_profit,
                flow=_flow_strength(bias.volume_z, bias.volume_imbalance),
            )
            quality, kronos_aligned = self._quality_score(
                confidence, rr, kronos_score, side, momentum_aligned,
            )
            confluence  = confluence + [
                f"zone:{best_signal.get('zone_kind','ob')}",
                f"rr:{rr:.1f}", "in-range",
            ] + (["kronos_aligned"] if kronos_aligned else [])
            sl_dist = abs(entry_price - stop_loss)
            tp_dist = abs(take_profit - entry_price)
            lot, risk_amount = self._resolve_lot(balance, sl_dist)
            entry = ScalpEntry(
                side=side, entry=entry_price, stop_loss=stop_loss,
                take_profit=take_profit, lot=lot,
                confidence=round(confidence, 3),
                reason=f"SMC zone scalp — {bias.reason}",
                order_type=otype, confluence=confluence,
                sl_pips=round(sl_dist / pip, 1), tp_pips=round(tp_dist / pip, 1),
                risk_amount=risk_amount, kronos_score=round(kronos_score, 3),
                rr=rr, quality_score=quality,
                gate_results={
                    "confidence": round(confidence, 3), "rr": rr,
                    "kronos": round(kronos_score, 3), "kronos_aligned": kronos_aligned,
                    "momentum": round(mom_strength, 3), "source": "smc_zone",
                    "buy_volume_pct": bias.buy_pressure_pct,
                    "sell_volume_pct": bias.sell_pressure_pct,
                    "volume_imbalance": bias.volume_imbalance,
                },
            )
            return entry, bias

        # No in-range zone → engage the current move with a market-adjacent entry.
        entry = self._build_realtime_entry(
            side, current_price, m1, m5, balance, confidence,
            mom_strength, bid, ask, bias, confluence,
            kronos_score=kronos_score, momentum_aligned=momentum_aligned,
            mkt_analysis=mkt,
        )
        return entry, bias

    # -- recovery --------------------------------------------------------------

    def build_recovery(
        self,
        original_side: str,
        original_lot: float,
        current_price: float,
        candles_by_tf: Dict[str, List[Candle]],
        balance: float = 0.0,
    ) -> Optional[ScalpEntry]:
        """
        Construct an SMC-guided recovery leg when the first trade is offside.

        The recovery is only produced when the live multi-timeframe bias favours
        the *opposite* direction of the losing trade — i.e. the market has
        genuinely turned — and it is sized ``RECOVERY_LOT_MULTIPLIER`` × the
        original lot so a modest continuation nets the combined position positive.
        """
        bias = self.compute_bias(candles_by_tf)
        recovery_side = "sell" if original_side == "buy" else "buy"

        if bias.direction != recovery_side or bias.confidence < self.min_confidence:
            return None
        if bias.atr_m5 <= 0 or current_price <= 0:
            return None

        primary_candles = candles_by_tf.get(self.primary_tf) or []
        atr = bias.atr_m5
        sl_dist = atr * self.sl_atr_mult
        tp_dist = atr * self.tp_atr_mult
        pullback = sl_dist * 0.3
        if recovery_side == "buy":
            entry_price = round(current_price - pullback, 6)
            stop_loss   = round(current_price - sl_dist, 6)
            take_profit = round(current_price + tp_dist, 6)
            otype       = "buy_limit"
        else:
            entry_price = round(current_price + pullback, 6)
            stop_loss   = round(current_price + sl_dist, 6)
            take_profit = round(current_price - tp_dist, 6)
            otype       = "sell_limit"

        lot = max(0.01, round(original_lot * RECOVERY_LOT_MULTIPLIER, 2))
        _, risk_amount = self._resolve_lot(balance, sl_dist)
        pip = self.pip_size or 1.0

        # Recovery leg: band by flow too, RR floor still authoritative.
        take_profit, rr = self._enforce_min_rr(
            recovery_side, entry_price, stop_loss, take_profit,
            flow=_flow_strength(bias.volume_z, bias.volume_imbalance),
        )
        quality, _ = self._quality_score(bias.confidence, rr, 0.0, recovery_side, True)

        return ScalpEntry(
            side=recovery_side,
            entry=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            lot=lot,
            confidence=round(bias.confidence, 3),
            reason=f"Recovery leg — market turned {recovery_side}: {bias.reason}",
            order_type=otype,
            is_recovery=True,
            confluence=[f"{tf}:{b}" for tf, b in bias.tf_bias.items()] + ["recovery"],
            sl_pips=round(sl_dist / pip, 1),
            tp_pips=round(tp_dist / pip, 1),
            risk_amount=risk_amount,
            rr=rr, quality_score=quality,
            gate_results={"confidence": round(bias.confidence, 3), "rr": rr,
                          "source": "recovery"},
        )

    def build_spike_stack_entry(
        self,
        side: str,
        current_price: float,
        candles_by_tf: Dict[str, List[Candle]],
        balance: float,
        bias: ScalpBias,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> Optional["ScalpEntry"]:
        """Build an additional stack order when a directional volume spike is detected.

        A spike stack rides a strong momentum surge by placing a second pending
        order at the continuation level — further from current price than the
        primary entry but aiming for a larger TP.  Only valid when the volume
        imbalance clearly confirms the direction.

        The SL is the same symbol-aware floor as the primary entry; the TP is
        extended to 3× SL to give the continuation room to run.
        """
        m5 = candles_by_tf.get(self.primary_tf) or []
        m1 = candles_by_tf.get(self.entry_tf) or []
        atr = bias.atr_m5 or (_atr(m5) if m5 else 0.0)
        pip = self.pip_size or 1.0
        if atr <= 0 or current_price <= 0:
            return None

        # Spike entry sits 0.4× ATR beyond current price (riding the breakout)
        offset = atr * 0.4
        sym_min_sl = _min_sl_price_for_symbol(self.symbol, pip, atr)
        sl_dist = max(sym_min_sl, atr * self.sl_atr_mult)
        # Wider TP for spike: 3× SL (captures the extended momentum move)
        tp_dist = sl_dist * 3.0

        spread = abs(ask - bid) if (ask > 0 and bid > 0) else pip
        ref_ask = ask if ask > 0 else current_price
        ref_bid = bid if bid > 0 else current_price

        if side == "buy":
            entry = round(ref_ask + offset, 6)
            stop_loss = round(entry - sl_dist, 6)
            take_profit = round(entry + tp_dist, 6)
            otype = "buy_stop"
        else:
            entry = round(ref_bid - offset, 6)
            stop_loss = round(entry + sl_dist, 6)
            take_profit = round(entry - tp_dist, 6)
            otype = "sell_stop"

        take_profit, rr = self._enforce_min_rr(
            side, entry, stop_loss, take_profit,
            flow=_flow_strength(bias.volume_z, bias.volume_imbalance),
        )
        sl_dist = abs(entry - stop_loss)
        tp_dist = abs(take_profit - entry)
        lot, risk_amount = self._resolve_lot(balance, sl_dist, multiplier=0.8)
        quality, _ = self._quality_score(bias.confidence, rr, 0.0, side, True)

        return ScalpEntry(
            side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit,
            lot=lot, confidence=round(bias.confidence, 3),
            reason=(
                f"Volume spike stack ({side}) imb={bias.volume_imbalance:+.2f} — "
                f"{bias.reason}"
            ),
            order_type=otype,
            confluence=[
                f"{tf}:{b}" for tf, b in bias.tf_bias.items()
            ] + [f"spike:{bias.volume_imbalance:+.2f}", "stack"],
            sl_pips=round(sl_dist / pip, 1), tp_pips=round(tp_dist / pip, 1),
            risk_amount=risk_amount, rr=rr, quality_score=quality,
            gate_results={
                "confidence": round(bias.confidence, 3), "rr": rr,
                "volume_imbalance": bias.volume_imbalance, "source": "spike_stack",
                "buy_volume_pct": bias.buy_pressure_pct,
                "sell_volume_pct": bias.sell_pressure_pct,
            },
        )
