"""
Multi-Timeframe Trend-Pullback Confluence (MTPC) Strategy
==========================================================

Philosophy
----------
  1. The 4H defines the macro trend (price above/below 50 EMA + ADX ≥ 20).
  2. The 1H shows price pulling back into a high-probability confluence zone
     (Fibonacci 38.2–61.8% + S/R cluster + 20 MA proximity).
  3. The 15M triggers the entry (engulfing candle, hammer/shooting-star,
     or RSI crossing out of extreme territory).

  Minimum 3 of 5 confluence factors must be satisfied before placing an order.

Confluence factors (0 or 1 each — max 5):
  1. 4H trend: price above/below 50 EMA AND ADX ≥ 20
  2. Price inside Fibonacci golden zone (38.2 – 61.8% retrace of last 1H impulse)
  3. Price within 0.6% of a 1H support/resistance pivot cluster
  4. Price within 0.5% of the 1H 20-bar MA
  5. 15M RSI: crossing out of oversold (<35 → above) or overbought (>65 → below)

Trade parameters
----------------
  Entry   : close of the trigger candle on 15M
  SL      : opposite extreme of the pullback zone + 0.1% buffer,
            capped at 1.5× ATR(14) on 1H
  TP1     : 1:1 R:R (scale out 50% of position)
  TP2     : previous 1H swing high/low (floor: 2.5:1 R:R)

Blockers (no trade generated)
------------------------------
  - 4H RSI > 72 for longs; 4H RSI < 28 for shorts  (overbought/oversold trend)
  - ADX < 18 on 4H (ranging market)
  - 4H score in neutral band (score between –0.12 and +0.12)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from app.exchanges.manager import exchange_manager, SupportedExchange


def _to_py(obj):
    """Recursively convert numpy scalars to native Python types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _to_py(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_py(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.ndarray):
        return [_to_py(v) for v in obj.tolist()]
    return obj
from app.signals.technical import (
    analyze as technical_analyze,
    ohlcv_to_dataframe,
    ema,
    rsi as _rsi_series,
    support_resistance_mtf,
    pivot_highs,
    pivot_lows,
)

# ─── Constants ────────────────────────────────────────────────────────────────

EXCHANGE   = SupportedExchange.BITGET
TREND_TF   = "4h"
SETUP_TF   = "1h"
TRIGGER_TF = "15m"

TF_LIMITS: Dict[str, int] = {TREND_TF: 200, SETUP_TF: 200, TRIGGER_TF: 300}

# Fibonacci golden zone
GOLDEN_LOW  = 0.382
GOLDEN_HIGH = 0.618
FIB_RATIOS  = [0.0, 0.236, 0.382, 0.50, 0.618, 0.786, 1.0]

# Confluence thresholds
MIN_CONFLUENCE     = 3       # out of 5 required
SR_PROXIMITY_PCT   = 0.60   # price within this % of an S/R level
EMA20_PROX_PCT     = 0.50   # price within this % of the 1H 20 MA

# RSI reversal boundaries
RSI_OVERSOLD   = 35.0
RSI_OVERBOUGHT = 65.0

# ADX requirements
ADX_MIN  = 18.0   # below → ranging, no trade
ADX_FULL = 20.0   # above → full trend confirmation

# ─── Fibonacci helpers ────────────────────────────────────────────────────────

def _find_impulse_swing(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Locate the most recent swing high and swing low on the 1H DataFrame.
    Uses pivot_highs / pivot_lows from technical.py.
    Returns {"swing_high", "swing_low", "impulse_range"} or {"error": ...}.
    """
    if len(df) < 20:
        return {"error": "Not enough bars for swing detection"}

    ph = pivot_highs(df["high"], left=5, right=5)
    pl = pivot_lows(df["low"],   left=5, right=5)

    valid_highs = ph.dropna()
    valid_lows  = pl.dropna()

    if valid_highs.empty or valid_lows.empty:
        return {"error": "No pivot points found"}

    swing_high = float(valid_highs.iloc[-1])
    swing_low  = float(valid_lows.iloc[-1])

    if swing_high <= swing_low:
        # Sometimes the last pivot high is older than the last pivot low.
        # Take the broader range of the last 2 highs / lows.
        if len(valid_highs) >= 2:
            swing_high = float(valid_highs.iloc[-2:].max())
        if len(valid_lows) >= 2:
            swing_low = float(valid_lows.iloc[-2:].min())

    if swing_high <= swing_low:
        return {"error": f"Degenerate swing: high={swing_high}, low={swing_low}"}

    rng = swing_high - swing_low
    if rng < 1e-10:
        return {"error": "Zero impulse range"}

    return {
        "swing_high":    round(swing_high, 8),
        "swing_low":     round(swing_low,  8),
        "impulse_range": round(rng,        8),
    }


def _fib_zones(swing_high: float, swing_low: float, direction: str) -> Dict[str, Any]:
    """
    Calculate Fibonacci retracement levels and the golden zone (38.2–61.8%).

    Bullish impulse was swing_low → swing_high; a pullback retraces downward.
      golden_top = swing_high − 38.2% of range  (shallower retrace)
      golden_bot = swing_high − 61.8% of range  (deeper retrace)

    Bearish impulse was swing_high → swing_low; a pullback retraces upward.
      golden_bot = swing_low + 38.2% of range
      golden_top = swing_low + 61.8% of range
    """
    rng = swing_high - swing_low

    if direction == "bull":
        golden_top = swing_high - rng * GOLDEN_LOW    # 38.2% below high
        golden_bot = swing_high - rng * GOLDEN_HIGH   # 61.8% below high
        levels = {
            "0%":    round(swing_low,                8),
            "23.6%": round(swing_high - rng * 0.236, 8),
            "38.2%": round(swing_high - rng * 0.382, 8),
            "50.0%": round(swing_high - rng * 0.500, 8),
            "61.8%": round(swing_high - rng * 0.618, 8),
            "78.6%": round(swing_high - rng * 0.786, 8),
            "100%":  round(swing_high,               8),
        }
    else:  # bear
        golden_bot = swing_low + rng * GOLDEN_LOW    # 38.2% above low
        golden_top = swing_low + rng * GOLDEN_HIGH   # 61.8% above low
        levels = {
            "0%":    round(swing_high,              8),
            "23.6%": round(swing_low + rng * 0.236, 8),
            "38.2%": round(swing_low + rng * 0.382, 8),
            "50.0%": round(swing_low + rng * 0.500, 8),
            "61.8%": round(swing_low + rng * 0.618, 8),
            "78.6%": round(swing_low + rng * 0.786, 8),
            "100%":  round(swing_low,               8),
        }

    return {
        "levels":     levels,
        "golden_bot": round(golden_bot, 8),
        "golden_top": round(golden_top, 8),
    }


def _in_golden_zone(price: float, fib: Dict[str, Any]) -> bool:
    bot = fib.get("golden_bot", 0.0)
    top = fib.get("golden_top", float("inf"))
    return bot <= price <= top


# ─── Candlestick pattern detection ───────────────────────────────────────────

def _engulfing(df: pd.DataFrame) -> Optional[str]:
    """Return 'bull', 'bear', or None."""
    if len(df) < 2:
        return None

    curr, prev = df.iloc[-1], df.iloc[-2]

    c_top  = max(curr["open"], curr["close"])
    c_bot  = min(curr["open"], curr["close"])
    p_top  = max(prev["open"], prev["close"])
    p_bot  = min(prev["open"], prev["close"])
    c_body = c_top - c_bot
    p_body = p_top - p_bot

    if c_body < 1e-10 or p_body < 1e-10:
        return None

    if curr["close"] > curr["open"] and prev["close"] < prev["open"]:
        if c_top >= p_top and c_bot <= p_bot:
            return "bull"

    if curr["close"] < curr["open"] and prev["close"] > prev["open"]:
        if c_bot <= p_bot and c_top >= p_top:
            return "bear"

    return None


def _pin_bar(df: pd.DataFrame, direction: str) -> bool:
    """
    Hammer (bull) or shooting star (bear) on the last candle.
    Wick must be ≥ 2× body; body must close in the upper/lower 40% of range.
    """
    if len(df) < 1:
        return False

    c = df.iloc[-1]
    rng = c["high"] - c["low"]
    if rng < 1e-10:
        return False

    body_top = max(c["open"], c["close"])
    body_bot = min(c["open"], c["close"])
    body     = body_top - body_bot
    lower_w  = body_bot  - c["low"]
    upper_w  = c["high"] - body_top

    if body < 1e-10:
        return False

    if direction == "bull":
        # Hammer: lower wick ≥ 2× body; body close in upper 40% of candle
        body_pos = (body_bot - c["low"]) / rng
        return lower_w >= 2.0 * body and body_pos >= 0.40

    # Shooting star: upper wick ≥ 2× body; body close in lower 40% of candle
    body_pos = (c["high"] - body_top) / rng
    return upper_w >= 2.0 * body and body_pos >= 0.40


def _rsi_cross(df: pd.DataFrame, direction: str) -> bool:
    """True if 15M RSI crossed out of extreme territory on the last bar."""
    if len(df) < 16:
        return False

    rsi_s = _rsi_series(df)
    if rsi_s.dropna().__len__() < 3:
        return False

    curr = float(rsi_s.iloc[-1])
    prev = float(rsi_s.iloc[-2])

    if direction == "bull":
        return prev <= RSI_OVERSOLD and curr > RSI_OVERSOLD
    return prev >= RSI_OVERBOUGHT and curr < RSI_OVERBOUGHT


def _trigger(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    """
    Evaluate 15M chart for a valid entry trigger.
    Returns a dict with 'detected', 'pattern', and 'rsi'.
    """
    pattern_dir = _engulfing(df)
    engulf_ok   = pattern_dir == direction if pattern_dir else False
    pin_ok      = _pin_bar(df, direction)
    rsi_ok      = _rsi_cross(df, direction)

    detected = engulf_ok or pin_ok or rsi_ok

    if engulf_ok:
        name = "bullish_engulfing" if direction == "bull" else "bearish_engulfing"
    elif pin_ok:
        name = "hammer" if direction == "bull" else "shooting_star"
    elif rsi_ok:
        name = "rsi_reversal"
    else:
        name = "none"

    rsi_val = 50.0
    if len(df) >= 16:
        rs = _rsi_series(df)
        last_rsi = rs.dropna().iloc[-1] if not rs.dropna().empty else None
        if last_rsi is not None:
            rsi_val = float(last_rsi)

    return {
        "detected":    detected,
        "pattern":     name,
        "rsi":         round(rsi_val, 1),
        "engulfing":   engulf_ok,
        "pin_bar":     pin_ok,
        "rsi_reversal": rsi_ok,
    }


# ─── Confluence scoring ───────────────────────────────────────────────────────

def _confluence(
    direction:     str,
    trend_ind:     Dict[str, Any],
    setup_ind:     Dict[str, Any],
    trigger_rsi:   float,
    fib:           Dict[str, Any],
    sr_levels:     List[Dict[str, Any]],
    price:         float,
    trigger_df:    pd.DataFrame,
) -> Tuple[int, Dict[str, bool], List[str]]:
    """
    Score the 5 MTPC confluence factors.
    Returns (total_score, factor_breakdown, reason_strings).
    """
    factors: Dict[str, bool] = {}
    reasons: List[str] = []

    # ── Factor 1: 4H trend alignment (50 EMA + ADX ≥ 20) ──────────────────
    ema50_4h = trend_ind.get("ema50")
    adx_4h   = float(trend_ind.get("adx") or 0.0)
    price_4h = float(trend_ind.get("price") or price)

    if ema50_4h and adx_4h >= ADX_FULL:
        f1 = (price_4h > ema50_4h) if direction == "bull" else (price_4h < ema50_4h)
    elif adx_4h >= ADX_MIN:
        f1 = True   # ADX trending but EMA data unavailable — accept
    else:
        f1 = False

    factors["trend_alignment"] = f1
    reasons.append(
        f"{'✓' if f1 else '✗'} 4H trend aligned ({direction.upper()}) | "
        f"ADX={adx_4h:.0f} | EMA50={'above' if price_4h > (ema50_4h or 0) else 'below'}"
    )

    # ── Factor 2: Price in Fibonacci golden zone (38.2–61.8%) ─────────────
    if "error" not in fib:
        f2 = _in_golden_zone(price, fib)
        bot, top = fib["golden_bot"], fib["golden_top"]
        factors["fib_zone"] = f2
        reasons.append(
            f"{'✓' if f2 else '✗'} Fib golden zone {bot:.4f}–{top:.4f} | "
            f"price={price:.4f}"
        )
    else:
        f2 = False
        factors["fib_zone"] = False
        reasons.append(f"✗ Fib: {fib['error']}")

    # ── Factor 3: Price at 1H support/resistance level ─────────────────────
    f3 = False
    nearest_sr: Optional[Dict] = None
    for lvl in sr_levels:
        lvl_p = float(lvl.get("price", 0.0))
        if lvl_p <= 0:
            continue
        pct = abs(price - lvl_p) / price * 100.0
        if pct <= SR_PROXIMITY_PCT:
            f3 = True
            nearest_sr = lvl
            break

    factors["sr_level"] = f3
    if f3 and nearest_sr:
        touches = nearest_sr.get("touches", nearest_sr.get("strength", "?"))
        reasons.append(
            f"✓ Price at 1H {nearest_sr.get('type', 'S/R')} "
            f"({nearest_sr['price']:.4f}, {touches} touches)"
        )
    else:
        reasons.append(f"✗ No 1H S/R within {SR_PROXIMITY_PCT}%")

    # ── Factor 4: Price near 1H 20 MA ──────────────────────────────────────
    ma20_1h = setup_ind.get("ma20")
    if ma20_1h and float(ma20_1h or 0) > 0:
        ma20_val  = float(ma20_1h)
        pct_ma20  = abs(price - ma20_val) / price * 100.0
        f4 = pct_ma20 <= EMA20_PROX_PCT
    else:
        f4 = False
        pct_ma20 = None
        ma20_val = None

    factors["ma20_1h"] = f4
    if f4:
        reasons.append(f"✓ Price near 1H 20 MA ({ma20_val:.4f}, Δ{pct_ma20:.2f}%)")
    else:
        ma_str = f"{ma20_val:.4f}" if ma20_val else "N/A"
        reasons.append(f"✗ 1H 20 MA too far ({ma_str})")

    # ── Factor 5: 15M RSI reversal signal ─────────────────────────────────
    rsi_extreme = (
        (direction == "bull" and trigger_rsi < RSI_OVERSOLD + 5) or
        (direction == "bear" and trigger_rsi > RSI_OVERBOUGHT - 5)
    )
    rsi_cross_detected = _rsi_cross(trigger_df, direction)
    f5 = rsi_extreme or rsi_cross_detected

    factors["rsi_reversal"] = f5
    reasons.append(
        f"{'✓' if f5 else '✗'} 15M RSI {trigger_rsi:.0f} "
        f"({'crossover' if rsi_cross_detected else 'at extreme' if rsi_extreme else 'neutral'})"
    )

    total = sum(1 for v in factors.values() if v)
    return total, factors, reasons


# ─── Trade parameter calculation ─────────────────────────────────────────────

def _calc_trade_params(
    direction:   str,
    entry_price: float,
    swing:       Dict[str, Any],
    atr_1h:      Optional[float],
) -> Dict[str, Any]:
    """
    Compute entry, SL, TP1, TP2 for the MTPC setup.

    SL placement:
      Long : below swing_low − 0.1% buffer (capped at 1.5× ATR from entry)
      Short: above swing_high + 0.1% buffer (capped at 1.5× ATR from entry)

    TP targets:
      TP1 = 1:1 risk (close half the position, move SL to breakeven)
      TP2 = previous swing extreme or 2.5:1 risk as a floor
    """
    buffer = entry_price * 0.001   # 0.1%

    if direction == "bull":
        zone_extreme = swing.get("swing_low", entry_price * 0.99)
        raw_sl = zone_extreme - buffer

        # Cap: don't put SL more than 1.5× ATR away
        if atr_1h:
            max_sl = entry_price - 1.5 * atr_1h
            sl = max(raw_sl, max_sl)          # use the closer (less aggressive) one
        else:
            sl = raw_sl

        sl   = min(sl, entry_price - buffer)  # always at least buffer below entry
        risk = entry_price - sl
        tp1  = entry_price + risk             # 1:1
        tp2_floor = entry_price + risk * 2.5
        tp2  = max(tp2_floor, swing.get("swing_high", tp2_floor))

    else:  # bear
        zone_extreme = swing.get("swing_high", entry_price * 1.01)
        raw_sl = zone_extreme + buffer

        if atr_1h:
            max_sl = entry_price + 1.5 * atr_1h
            sl = min(raw_sl, max_sl)
        else:
            sl = raw_sl

        sl   = max(sl, entry_price + buffer)
        risk = sl - entry_price
        tp1  = entry_price - risk
        tp2_floor = entry_price - risk * 2.5
        tp2  = min(tp2_floor, swing.get("swing_low", tp2_floor))

    rr_tp2 = round(abs(tp2 - entry_price) / risk, 2) if risk > 1e-10 else 0.0
    sl_valid = risk > 1e-10 and (not atr_1h or risk <= 1.5 * atr_1h + 1e-10)

    return {
        "entry":    round(entry_price, 6),
        "sl":       round(sl,          6),
        "tp1":      round(tp1,         6),
        "tp2":      round(tp2,         6),
        "risk_pts": round(risk,        6),
        "rr_tp1":   1.0,
        "rr_tp2":   rr_tp2,
        "atr_1h":   round(atr_1h, 6) if atr_1h else None,
        "sl_valid": sl_valid,
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

async def analyze_mtpc(
    symbol:   str,
    exchange: Optional[SupportedExchange] = None,
) -> Dict[str, Any]:
    """
    Run the full MTPC strategy analysis for one symbol.

    Returns a dict with these top-level keys:
      symbol         : str
      mtpc_state     : "no_data" | "blocked" | "setup_only" | "signal"
      mtpc_action    : "buy" | "sell" | "wait" | "hold"
      direction      : "bull" | "bear" | "neutral"
      confluence     : int  (0–5)
      confluence_ok  : bool (≥ MIN_CONFLUENCE)
      trigger        : {detected, pattern, rsi, engulfing, pin_bar, rsi_reversal}
      fib            : {levels, golden_bot, golden_top} or None
      swing          : {swing_high, swing_low, impulse_range} or None
      factors        : {trend_alignment, fib_zone, sr_level, ma20_1h, rsi_reversal}
      trade_params   : {entry, sl, tp1, tp2, risk_pts, rr_tp1, rr_tp2} or None
      reasons        : [str, ...]
      timeframes     : snapshot per TF
      tf_errors      : [str, ...]
    """
    exch      = exchange or EXCHANGE
    connector = exchange_manager.get_exchange(exch)
    if not connector:
        return {
            "symbol": symbol, "mtpc_state": "no_data",
            "mtpc_action": "hold", "error": "Exchange not initialized",
        }

    # ── 1. Fetch OHLCV + run TA for all three timeframes ──────────────────
    tf_ohlcv: Dict[str, List]       = {}
    tf_ta:    Dict[str, Dict]       = {}
    errors:   List[str]             = []

    for tf, limit in TF_LIMITS.items():
        try:
            ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=limit)
            ta    = technical_analyze(ohlcv, tf)
            if "error" in ta:
                errors.append(f"{tf}: {ta['error']}")
                continue
            tf_ohlcv[tf] = ohlcv
            tf_ta[tf]    = ta
        except Exception as exc:
            errors.append(f"{tf}: {exc}")
            logger.debug(f"[MTPC] {symbol} {tf} error: {exc}")

    if TREND_TF not in tf_ta or SETUP_TF not in tf_ta:
        return {
            "symbol": symbol, "mtpc_state": "no_data",
            "mtpc_action": "hold", "confluence": 0,
            "error": f"Missing critical TF data. Errors: {errors}",
        }

    trend_ta   = tf_ta[TREND_TF]
    setup_ta   = tf_ta[SETUP_TF]
    trigger_ta = tf_ta.get(TRIGGER_TF, {})

    trend_ind   = trend_ta.get("indicators",   {})
    setup_ind   = setup_ta.get("indicators",   {})
    trigger_ind = trigger_ta.get("indicators", {})

    # Use 15M price first, fall back to 1H price
    current_price = float(
        trigger_ind.get("price") or setup_ind.get("price") or 0.0
    )
    if current_price <= 0:
        return {
            "symbol": symbol, "mtpc_state": "no_data",
            "mtpc_action": "hold", "confluence": 0,
            "error": "Could not resolve current price",
        }

    # ── 2. Determine macro direction from 4H score ────────────────────────
    trend_score = float(trend_ta.get("score", 0.0))
    adx_4h      = float(trend_ind.get("adx") or 0.0)
    rsi_4h      = float(trend_ind.get("rsi") or 50.0)

    if trend_score >= 0.12:
        direction = "bull"
    elif trend_score <= -0.12:
        direction = "bear"
    else:
        return {
            "symbol": symbol, "mtpc_state": "blocked",
            "mtpc_action": "hold", "confluence": 0, "direction": "neutral",
            "reasons": [
                f"4H trend score {trend_score:+.3f} is in the neutral band "
                f"(−0.12 to +0.12). No trade."
            ],
        }

    # ── 3. Hard blockers ─────────────────────────────────────────────────
    if adx_4h < ADX_MIN:
        return {
            "symbol": symbol, "mtpc_state": "blocked",
            "mtpc_action": "hold", "confluence": 0, "direction": direction,
            "reasons": [
                f"ADX={adx_4h:.0f} < {ADX_MIN} — ranging market. "
                "Waiting for trend to develop."
            ],
        }

    if direction == "bull" and rsi_4h > 72:
        return {
            "symbol": symbol, "mtpc_state": "blocked",
            "mtpc_action": "hold", "confluence": 0, "direction": direction,
            "reasons": [
                f"4H RSI={rsi_4h:.0f} is overbought (>72). "
                "No new long entries; wait for a pullback."
            ],
        }

    if direction == "bear" and rsi_4h < 28:
        return {
            "symbol": symbol, "mtpc_state": "blocked",
            "mtpc_action": "hold", "confluence": 0, "direction": direction,
            "reasons": [
                f"4H RSI={rsi_4h:.0f} is oversold (<28). "
                "No new short entries; wait for a bounce."
            ],
        }

    # ── 4. Build 1H Fibonacci zones ───────────────────────────────────────
    setup_df = (
        ohlcv_to_dataframe(tf_ohlcv[SETUP_TF])
        if SETUP_TF in tf_ohlcv else pd.DataFrame()
    )
    swing = _find_impulse_swing(setup_df) if not setup_df.empty else {"error": "No 1H data"}
    fib   = (
        _fib_zones(swing["swing_high"], swing["swing_low"], direction)
        if "error" not in swing
        else {"error": swing["error"]}
    )

    # ── 5. 1H support/resistance levels ──────────────────────────────────
    sr_result = support_resistance_mtf(setup_df) if not setup_df.empty else {}
    sr_levels: List[Dict] = sr_result.get("levels", [])

    # ── 6. 15M trigger detection ──────────────────────────────────────────
    trigger_df = (
        ohlcv_to_dataframe(tf_ohlcv[TRIGGER_TF])
        if TRIGGER_TF in tf_ohlcv else pd.DataFrame()
    )
    trig = (
        _trigger(trigger_df, direction)
        if not trigger_df.empty
        else {"detected": False, "pattern": "none", "rsi": 50.0,
              "engulfing": False, "pin_bar": False, "rsi_reversal": False}
    )
    trigger_rsi = float(trig.get("rsi", 50.0))

    # ── 7. Confluence scoring ─────────────────────────────────────────────
    conf_score, factors, conf_reasons = _confluence(
        direction   = direction,
        trend_ind   = trend_ind,
        setup_ind   = setup_ind,
        trigger_rsi = trigger_rsi,
        fib         = fib,
        sr_levels   = sr_levels,
        price       = current_price,
        trigger_df  = trigger_df,
    )
    confluence_ok = conf_score >= MIN_CONFLUENCE

    # ── 8. Final state decision ───────────────────────────────────────────
    if not confluence_ok:
        state  = "setup_only"
        action = "wait"
    elif not trig["detected"]:
        state  = "setup_only"   # confluence met, but waiting on trigger candle
        action = "wait"
    else:
        state  = "signal"
        action = "buy" if direction == "bull" else "sell"

    # ── 9. Trade parameters (only on confirmed signal) ────────────────────
    params: Dict[str, Any] = {}
    if state == "signal":
        atr_1h = float(setup_ind.get("atr") or 0.0) or None
        params = _calc_trade_params(direction, current_price, swing if "error" not in swing else {}, atr_1h)

    # ── 10. Assemble reasons ──────────────────────────────────────────────
    header = {
        "signal":     (
            f"MTPC {action.upper()} — {symbol} | {conf_score}/5 confluence | "
            f"trigger: {trig['pattern']}"
        ),
        "setup_only": (
            f"MTPC setup forming — {symbol} ({direction.upper()}) | "
            f"{conf_score}/5 confluence | "
            + ("waiting for trigger candle" if confluence_ok else f"need {MIN_CONFLUENCE} factors")
        ),
        "blocked":    f"MTPC blocked — {symbol}",
        "no_data":    "MTPC: insufficient data",
    }

    all_reasons: List[str] = [header.get(state, state)] + conf_reasons

    if trig["detected"]:
        all_reasons.append(f"✓ 15M entry trigger: {trig['pattern']}")
    else:
        all_reasons.append(
            "✗ No 15M trigger yet — watching for engulfing candle / pin bar / RSI crossover"
        )

    if errors:
        all_reasons.append(f"Data warnings: {'; '.join(errors)}")

    # ── 11. Compose result ────────────────────────────────────────────────
    return _to_py({
        "symbol":        symbol,
        "mtpc_state":    state,
        "mtpc_action":   action,
        "direction":     direction,
        "confluence":    conf_score,
        "confluence_ok": confluence_ok,
        "min_confluence": MIN_CONFLUENCE,
        "trigger":       trig,
        "fib":           fib   if "error" not in fib   else None,
        "swing":         swing if "error" not in swing else None,
        "factors":       factors,
        "trade_params":  params if params else None,
        "reasons":       all_reasons,
        "timeframes": {
            TREND_TF: {
                "role":      "trend",
                "score":     round(float(trend_ta.get("score", 0.0)), 4),
                "direction": direction,
                "rsi":       round(rsi_4h, 1),
                "adx":       round(adx_4h, 1),
            },
            SETUP_TF: {
                "role":  "setup",
                "score": round(float(setup_ta.get("score", 0.0)), 4),
                "rsi":   round(float(setup_ind.get("rsi") or 50.0), 1),
                "adx":   round(float(setup_ind.get("adx") or 0.0),  1),
                "ma20":  setup_ind.get("ma20"),
            },
            TRIGGER_TF: {
                "role":    "trigger",
                "score":   round(float(trigger_ta.get("score", 0.0) if trigger_ta else 0.0), 4),
                "rsi":     round(trigger_rsi, 1),
                "pattern": trig["pattern"],
            },
        },
        "tf_errors": errors,
    })
