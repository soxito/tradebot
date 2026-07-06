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
        lot, risk_amount = self._resolve_lot(balance, sl_dist)

        mode = "momentum-stop" if strong else "pullback-limit"
        return ScalpEntry(
            side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit,
            lot=lot, confidence=round(confidence, 3),
            reason=f"Real-time {mode} scalp — {bias.reason}",
            order_type=otype,
            confluence=confluence + [f"rt:{mode}", f"mom:{momentum_strength:.2f}"],
            sl_pips=round(sl_dist / pip, 1), tp_pips=round(tp_dist / pip, 1),
            risk_amount=risk_amount,
        )

    # -- entry -----------------------------------------------------------------

    def analyse(
        self,
        candles_by_tf: Dict[str, List[Candle]],
        current_price: float,
        balance: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
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

        if bias.direction in ("buy", "sell"):
            if mom_dir == bias.direction:
                side = bias.direction
                confidence = min(1.0, bias.confidence + 0.15 + mom_strength * 0.2)
                confluence.append("HTF+live-aligned")
            elif mom_dir == "neutral":
                if bias.confidence >= self.min_confidence:
                    side = bias.direction
                    confluence.append("HTF-bias")
            else:
                # Live move opposes the higher-timeframe trend — stand aside.
                return None, bias
        elif mom_dir in ("buy", "sell") and mom_strength >= MOMENTUM_MIN_STANDALONE:
            # No dominant HTF trend, but a clear live move — scalp it in real time.
            side = mom_dir
            confidence = max(self.min_confidence, min(0.9, 0.5 + mom_strength * 0.4))
            confluence.append("live-momentum")

        if side is None or confidence < self.min_confidence:
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
                ), bias
            entry_price = round(raw_entry, 6)
            stop_loss   = round(raw_sl, 6)
            take_profit = round(raw_tp, 6)
            confluence  = confluence + [
                f"zone:{best_signal.get('zone_kind','ob')}",
                f"rr:{best_signal.get('rr',0):.1f}", "in-range",
            ]
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
                risk_amount=risk_amount,
            )
            return entry, bias

        # No in-range zone → engage the current move with a market-adjacent entry.
        entry = self._build_realtime_entry(
            side, current_price, m1, m5, balance, confidence,
            mom_strength, bid, ask, bias, confluence,
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
