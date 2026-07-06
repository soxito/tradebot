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

# Best scalp timeframe (entry trigger) + the confluence stack, ordered
# fastest → slowest.  M5 is the primary; M1 confirms micro-structure; H1/H4/D1
# give the directional bias the entry must not fight.
PRIMARY_SCALP_TF = "M5"
ENTRY_REFINE_TF = "M1"
BIAS_TFS: List[str] = ["H1", "H4", "D1"]
ALL_SCALP_TFS: List[str] = [ENTRY_REFINE_TF, PRIMARY_SCALP_TF, "H1", "H4", "D1"]

# ATR multiples used to place the protective stop / primary target on the
# entry timeframe.  A 1.5×ATR stop with a 2.5×ATR target keeps reward:risk
# comfortably above 1 while staying tight enough for scalping.
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5

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
#   momentum_min_standalone — live-momentum strength needed to scalp when the
#                             higher-timeframe (HTF) bias is flat
#   min_rr                  — hard reward:risk floor; TP is widened to meet it
#   require_htf_alignment   — when True, momentum-only (flat-HTF) scalps are
#                             disallowed — every entry must ride the HTF trend
#   min_fusion_score        — quality-score floor the bot-level fusion gate uses
#   kronos_veto             — reject an entry when the Kronos ML score opposes
#                             the trade side by more than this magnitude
#   kronos_align_bonus      — confidence bonus when Kronos agrees with the side
STRICTNESS_PRESETS: Dict[str, Dict[str, float]] = {
    "conservative": {
        "min_confidence": 0.68,
        "momentum_min_standalone": 0.75,
        "min_rr": 1.8,
        "require_htf_alignment": 1.0,
        "min_fusion_score": 0.66,
        "kronos_veto": 0.25,
        "kronos_align_bonus": 0.12,
    },
    "balanced": {
        "min_confidence": 0.58,
        "momentum_min_standalone": 0.55,
        "min_rr": 1.5,
        "require_htf_alignment": 0.0,
        "min_fusion_score": 0.55,
        "kronos_veto": 0.40,
        "kronos_align_bonus": 0.10,
    },
    "aggressive": {
        "min_confidence": 0.50,
        "momentum_min_standalone": 0.45,
        "min_rr": 1.3,
        "require_htf_alignment": 0.0,
        "min_fusion_score": 0.45,
        "kronos_veto": 0.55,
        "kronos_align_bonus": 0.08,
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


def _tf_direction(bias: str) -> str:
    """Map an SMC ``bias`` string to a trade side."""
    if bias == "bullish":
        return "buy"
    if bias == "bearish":
        return "sell"
    return "neutral"


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
    ) -> None:
        self.symbol = symbol
        self.lot_size = max(0.01, round(lot_size, 2))
        self.auto_lot = auto_lot
        self.risk_per_trade_pct = risk_per_trade_pct
        # Strictness preset drives how selective the scalper is. An explicit
        # ``min_confidence`` still overrides the preset's confidence floor.
        self.strictness = strictness if strictness in STRICTNESS_PRESETS else DEFAULT_STRICTNESS
        preset = STRICTNESS_PRESETS[self.strictness]
        self.min_confidence = (
            float(min_confidence) if min_confidence is not None
            else float(preset["min_confidence"])
        )
        self.min_rr = float(preset["min_rr"])
        self.momentum_min_standalone = float(preset["momentum_min_standalone"])
        self.require_htf_alignment = bool(preset["require_htf_alignment"])
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
        """Blend the multi-timeframe SMC bias into a single directional read."""
        tf_bias: Dict[str, str] = {}
        # Weight faster timeframes lower than the higher-timeframe context so the
        # scalp trades WITH the dominant trend but triggers on the fast frame.
        weights = {"M1": 0.5, "M5": 1.0, "H1": 1.5, "H4": 2.0, "D1": 2.5}
        score = 0.0
        total_w = 0.0

        for tf, candles in candles_by_tf.items():
            if not candles or len(candles) < 40:
                continue
            eng = SMCStrategyEngine(symbol=self.symbol)
            prim = eng._primitives(candles)
            b = prim.get("bias", "neutral")
            tf_bias[tf] = b
            w = weights.get(tf, 1.0)
            total_w += w
            if b == "bullish":
                score += w
            elif b == "bearish":
                score -= w

        m5 = candles_by_tf.get(PRIMARY_SCALP_TF) or []
        atr_m5 = _atr(m5) if m5 else 0.0
        rsi_m5 = _rsi(m5) if m5 else 50.0
        vol_z = _volume_zscore(m5) if m5 else 0.0
        pressure = _buy_sell_pressure(m5) if m5 else 0.5

        direction = "neutral"
        norm = (score / total_w) if total_w > 0 else 0.0  # -1..1
        # Require a meaningful alignment before committing to a side.
        if norm >= 0.34:
            direction = "buy"
        elif norm <= -0.34:
            direction = "sell"

        # Fold fast-frame flow into confidence: agreement with the volume/flow
        # pressure boosts conviction, disagreement dampens it.
        base_conf = min(1.0, abs(norm))
        flow_bonus = 0.0
        if direction == "buy" and pressure > 0.55:
            flow_bonus = 0.15
        elif direction == "sell" and pressure < 0.45:
            flow_bonus = 0.15
        elif direction != "neutral":
            flow_bonus = -0.1
        confidence = max(0.0, min(1.0, base_conf + flow_bonus))

        reason = (
            f"HTF bias {norm:+.2f} across {len(tf_bias)} TFs; "
            f"M5 flow {pressure*100:.0f}% buy, vol-z {vol_z:+.1f}, RSI {rsi_m5:.0f}"
        )
        return ScalpBias(
            direction=direction, confidence=confidence, tf_bias=tf_bias,
            atr_m5=atr_m5, rsi_m5=rsi_m5, volume_z=vol_z,
            buy_pressure_pct=round(pressure * 100, 1), reason=reason,
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
    ) -> tuple[float, float]:
        """Guarantee reward:risk ≥ ``self.min_rr`` by widening TP if needed.

        Keeping reward comfortably above risk is the single biggest lever for
        "hardly loses" behaviour: even a sub-50 % win rate stays net-positive
        when winners pay more than losers. Returns ``(take_profit, rr)``.
        """
        risk = abs(entry - stop_loss)
        if risk <= 0:
            return take_profit, 0.0
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
        if move < 0 and body_dir < 0 and up_frac <= 0.5:
            return "sell", strength
        return "neutral", strength * 0.5

    def _build_realtime_entry(
        self, side: str, current_price: float, m1: List[Candle], m5: List[Candle],
        balance: float, confidence: float, momentum_strength: float,
        bid: float, ask: float, bias: ScalpBias, confluence: List[str],
        kronos_score: float = 0.0, momentum_aligned: bool = True,
    ) -> ScalpEntry:
        """Build a market-adjacent scalp entry that engages the current move.

        Strong live momentum → a **stop** order just beyond price (rides the
        breakout continuation).  Moderate momentum → a **limit** order a hair
        inside price (fills on the next micro-pullback, then rides the move).
        Geometry is tight, driven by the M1 micro-ATR so SL/TP suit scalping.
        """
        pip = self.pip_size or 1.0
        micro_atr = _atr(m1) if len(m1) >= 14 else 0.0
        if micro_atr <= 0:
            micro_atr = (bias.atr_m5 or pip * 10) * 0.4
        micro_atr = max(micro_atr, pip * 2)

        sl_dist = max(pip * 4, micro_atr * RT_SL_ATR_M1)
        tp_dist = max(sl_dist * 1.5, micro_atr * RT_TP_ATR_M1)

        # Stop trigger / limit offset must clear the live spread so the order
        # rests on the correct side of the market and is immediately valid.
        spread = abs(ask - bid) if (ask > 0 and bid > 0) else pip
        trigger = max(pip * 1.0, micro_atr * 0.12, spread * 1.2)

        strong = momentum_strength >= MOMENTUM_STRONG
        ref_ask = ask if ask > 0 else current_price
        ref_bid = bid if bid > 0 else current_price

        if side == "buy":
            if strong:
                entry = ref_ask + trigger          # buy_stop above the ask → continuation
                otype = "buy_stop"
            else:
                entry = ref_bid - trigger          # buy_limit just below → micro-pullback
                otype = "buy_limit"
            stop_loss = entry - sl_dist
            take_profit = entry + tp_dist
        else:
            if strong:
                entry = ref_bid - trigger          # sell_stop below the bid → continuation
                otype = "sell_stop"
            else:
                entry = ref_ask + trigger          # sell_limit just above → micro-pullback
                otype = "sell_limit"
            stop_loss = entry + sl_dist
            take_profit = entry - tp_dist

        entry = round(entry, 6)
        stop_loss = round(stop_loss, 6)
        take_profit = round(take_profit, 6)
        # Guarantee reward:risk ≥ the strictness floor before sizing.
        take_profit, rr = self._enforce_min_rr(side, entry, stop_loss, take_profit)
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

        m1 = candles_by_tf.get(ENTRY_REFINE_TF) or []
        m5 = candles_by_tf.get(PRIMARY_SCALP_TF) or []
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
                ), bias
            entry_price = round(raw_entry, 6)
            stop_loss   = round(raw_sl, 6)
            take_profit = round(raw_tp, 6)
            # Enforce the reward:risk floor (widen TP if the zone target is tight).
            take_profit, rr = self._enforce_min_rr(side, entry_price, stop_loss, take_profit)
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
                },
            )
            return entry, bias

        # No in-range zone → engage the current move with a market-adjacent entry.
        entry = self._build_realtime_entry(
            side, current_price, m1, m5, balance, confidence,
            mom_strength, bid, ask, bias, confluence,
            kronos_score=kronos_score, momentum_aligned=momentum_aligned,
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

        # Only hedge when structure actually favours the reverse direction.
        if bias.direction != recovery_side or bias.confidence < self.min_confidence:
            return None
        if bias.atr_m5 <= 0 or current_price <= 0:
            return None

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

        # Enforce the reward:risk floor on the recovery leg too.
        take_profit, rr = self._enforce_min_rr(recovery_side, entry_price, stop_loss, take_profit)
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
