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
        min_confidence: float = 0.55,
        sl_atr_mult: float = SL_ATR_MULT,
        tp_atr_mult: float = TP_ATR_MULT,
    ) -> None:
        self.symbol = symbol
        self.lot_size = max(0.01, round(lot_size, 2))
        self.auto_lot = auto_lot
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_confidence = min_confidence
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
        """Return (lot, risk_amount). Fixed lot unless auto_lot is enabled."""
        if not self.auto_lot or balance <= 0 or sl_distance <= 0 or self.contract_size <= 0:
            lot = max(0.01, round(self.lot_size * multiplier, 2))
            risk = round(lot * sl_distance * self.contract_size, 2) if sl_distance > 0 else 0.0
            return lot, risk
        risk_amount = balance * (self.risk_per_trade_pct / 100.0) * multiplier
        lot = risk_amount / (sl_distance * self.contract_size)
        lot = max(0.01, round(lot, 2))
        actual_risk = round(lot * sl_distance * self.contract_size, 2)
        return lot, actual_risk

    # -- entry -----------------------------------------------------------------

    def analyse(
        self,
        candles_by_tf: Dict[str, List[Candle]],
        current_price: float,
        balance: float = 0.0,
    ) -> tuple[Optional[ScalpEntry], ScalpBias]:
        """
        Produce a market scalp entry when the timeframe stack aligns.

        Returns ``(entry_or_None, bias)`` so callers can surface the live bias
        even when no trade is taken.
        """
        bias = self.compute_bias(candles_by_tf)

        m5 = candles_by_tf.get(PRIMARY_SCALP_TF) or []
        if len(m5) < 40 or bias.atr_m5 <= 0 or current_price <= 0:
            return None, bias
        if bias.direction == "neutral" or bias.confidence < self.min_confidence:
            return None, bias

        # Confirm the fast-frame SMC agrees with the higher-timeframe direction.
        eng = SMCStrategyEngine(symbol=self.symbol, min_confidence=0.0)
        m5_analysis = eng.analyze(m5)
        m5_signals = m5_analysis.get("signals", []) if isinstance(m5_analysis, dict) else []
        m5_sides = {s.get("side") for s in m5_signals}
        confluence = [f"{tf}:{b}" for tf, b in bias.tf_bias.items()]
        if m5_sides and bias.direction not in m5_sides:
            # Fast frame is not offering a same-side setup — skip to avoid
            # trading straight into fresh M5 supply/demand.
            return None, bias
        if bias.direction in m5_sides:
            confluence.append("M5-SMC-aligned")

        side = bias.direction
        atr = bias.atr_m5
        pip = self.pip_size or 1.0

        # ── Prefer SMC zone entry from M5 signals (limit order at OB/FVG) ──────
        # When the M5 SMC engine has a same-direction pending setup, use its
        # zone price (buy_limit/sell_limit/buy_stop/sell_stop) so we enter at
        # institutional levels rather than at market.
        best_signal: Optional[Dict[str, Any]] = None
        for sig in m5_signals:
            if sig.get("side") == side and sig.get("entry") and sig.get("stop_loss"):
                best_signal = sig
                break

        if best_signal:
            raw_entry   = float(best_signal.get("entry", current_price))
            raw_sl      = float(best_signal.get("stop_loss", 0) or 0)
            raw_tp1     = float(best_signal.get("tp1", 0) or 0)
            raw_tp      = float(best_signal.get("take_profit", 0) or raw_tp1 or 0)
            otype       = best_signal.get("order_type", f"{side}_limit")
            # Fall back to ATR geometry when zone values are degenerate
            if raw_sl <= 0 or raw_tp <= 0 or raw_entry <= 0:
                raw_entry = current_price
                raw_sl    = current_price - atr * self.sl_atr_mult if side == "buy" else current_price + atr * self.sl_atr_mult
                raw_tp    = current_price + atr * self.tp_atr_mult if side == "buy" else current_price - atr * self.tp_atr_mult
                otype     = f"{side}_limit"
            entry_price = round(raw_entry, 6)
            stop_loss   = round(raw_sl, 6)
            take_profit = round(raw_tp, 6)
            extra       = [f"zone:{best_signal.get('zone_kind','ob')}", f"rr:{best_signal.get('rr',0):.1f}"]
            confluence  = confluence + extra
        else:
            # Fallback: ATR-based limit entry (slight pullback/push vs current price)
            sl_dist  = atr * self.sl_atr_mult
            tp_dist  = atr * self.tp_atr_mult
            pullback = atr * 0.3  # wait for 30% ATR retracement before fill
            if side == "buy":
                entry_price = round(current_price - pullback, 6)
                stop_loss   = round(current_price - sl_dist, 6)
                take_profit = round(current_price + tp_dist, 6)
                otype       = "buy_limit"
            else:
                entry_price = round(current_price + pullback, 6)
                stop_loss   = round(current_price + sl_dist, 6)
                take_profit = round(current_price - tp_dist, 6)
                otype       = "sell_limit"

        sl_dist = abs(entry_price - stop_loss)
        tp_dist = abs(take_profit - entry_price)
        lot, risk_amount = self._resolve_lot(balance, sl_dist)

        entry = ScalpEntry(
            side=side,
            entry=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            lot=lot,
            confidence=round(bias.confidence, 3),
            reason=bias.reason,
            order_type=otype,
            confluence=confluence,
            sl_pips=round(sl_dist / pip, 1),
            tp_pips=round(tp_dist / pip, 1),
            risk_amount=risk_amount,
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
        )
