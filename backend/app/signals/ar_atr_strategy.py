"""
AR-ATR Trend Multi-Confirmation Strategy
=========================================

A single-timeframe trend-following strategy.  No multi-timeframe analysis.
Four independent confirmations must ALL fire before a signal is generated.

Confirmations
-------------
  1. SuperTrend          — establishes trend direction (bull / bear)
  2. ADX                 — confirms the trend is strong (ADX ≥ threshold, default 25)
  3. Volume > Volume MA  — confirms institutional participation above average
  4. MACD Histogram      — confirms momentum is building in the trend direction

Stop Loss
---------
  Hard stop placed at entry ± (ATR × SL_MULT).
  Default SL_MULT = 1.5.  This is always active from the moment of entry.

Trailing Stop (Take Profit mechanism)
--------------------------------------
  A second stop trails price at ATR × TRAIL_MULT below/above the highest/lowest
  close seen since entry.  It only moves in one direction (tightens as price
  moves favourably, never widens).
  Default TRAIL_MULT = 2.0.

  At the moment a new signal fires (entry = current close), both stops start
  at the same level.  As price moves, the trail tightens while the hard SL
  stays put.  The first stop hit exits the trade.

SuperTrend Algorithm
--------------------
  ATR-based dynamic band, standard Pine-Script / TradingView formula:
    Basic Upper = HL2 + ATR × multiplier
    Basic Lower = HL2 - ATR × multiplier

    Final Upper[i] = (Basic Upper[i] < Final Upper[i-1] or Close[i-1] > Final Upper[i-1])
                     ? Basic Upper[i] : Final Upper[i-1]

    Final Lower[i] = (Basic Lower[i] > Final Lower[i-1] or Close[i-1] < Final Lower[i-1])
                     ? Basic Lower[i] : Final Lower[i-1]

    Direction:
      if Close[i] > Final Upper[i-1]  → direction = 1  (bullish)
      if Close[i] < Final Lower[i-1]  → direction = -1 (bearish)
      else keep previous direction

    SuperTrend line:
      direction == 1  → SuperTrend = Final Lower  (price above → lower band is support)
      direction == -1 → SuperTrend = Final Upper  (price below → upper band is resistance)

Signal states
-------------
  "signal"     — all 4 confirmations aligned → actionable entry
  "watch"      — SuperTrend + ADX pass, volume/MACD partial → setup forming
  "no_signal"  — trend absent or confirmations missing → stand aside
  "blocked"    — hard filter active (e.g. ADX < 15, no clear direction)
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
    ohlcv_to_dataframe,
    sma,
    ema,
    macd as _calc_macd,
    adx as _calc_adx,
)

# ─── Default parameters ───────────────────────────────────────────────────────

# SuperTrend
ST_ATR_PERIOD: int   = 10     # ATR period for SuperTrend bands
ST_MULTIPLIER: float = 3.0    # Band width multiplier

# ADX
ADX_PERIOD:    int   = 14
ADX_THRESHOLD: float = 25.0   # Trend is strong above this
ADX_MIN:       float = 15.0   # Below this → ranging, hard block

# Volume
VOL_MA_PERIOD: int = 20

# MACD
MACD_FAST:   int = 12
MACD_SLOW:   int = 26
MACD_SIGNAL: int = 9

# Trade parameters
SL_MULT:    float = 1.5   # Hard stop loss = entry ± ATR × SL_MULT
TRAIL_MULT: float = 2.0   # Trailing stop = highest/lowest close − ATR × TRAIL_MULT

# Data requirements
MIN_CANDLES: int   = 100
TF_LIMIT:    int   = 300

DEFAULT_EXCHANGE = SupportedExchange.BITGET


# ─── SuperTrend calculation ───────────────────────────────────────────────────

def _supertrend(
    df: pd.DataFrame,
    atr_period: int  = ST_ATR_PERIOD,
    multiplier: float = ST_MULTIPLIER,
) -> pd.DataFrame:
    """
    Compute SuperTrend indicator.

    Returns a DataFrame with columns:
      supertrend   — the SuperTrend line value
      direction    — 1 (bullish) or -1 (bearish)
      upper_band   — Final Upper Band (resistance when bearish)
      lower_band   — Final Lower Band (support when bullish)
      flip         — True on the bar where direction changed
    """
    hl2    = (df["high"] + df["low"]) / 2.0
    close  = df["close"]
    n      = len(df)

    # True Range
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - close.shift(1)).abs()
    tr3 = (df["low"]  - close.shift(1)).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Smoothed ATR (Wilder / RMA — same as standard SuperTrend implementations)
    atr_s = tr.ewm(alpha=1.0 / atr_period, min_periods=atr_period).mean()

    # Basic bands
    basic_upper = hl2 + multiplier * atr_s
    basic_lower = hl2 - multiplier * atr_s

    # Final bands and direction (iterative — must use a loop)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction   = np.full(n, np.nan)
    supertrend  = np.full(n, np.nan)

    close_arr  = close.values
    bu_arr     = basic_upper.values
    bl_arr     = basic_lower.values

    # Seed first valid bar
    first = atr_period - 1
    if first >= n:
        return pd.DataFrame({
            "supertrend": supertrend, "direction": direction,
            "upper_band": final_upper, "lower_band": final_lower,
            "flip": np.zeros(n, dtype=bool),
        }, index=df.index)

    final_upper[first] = bu_arr[first]
    final_lower[first] = bl_arr[first]
    direction[first]   = 1 if close_arr[first] > bu_arr[first] else -1
    supertrend[first]  = final_lower[first] if direction[first] == 1 else final_upper[first]

    for i in range(first + 1, n):
        if np.isnan(bu_arr[i]) or np.isnan(bl_arr[i]):
            final_upper[i] = final_upper[i - 1]
            final_lower[i] = final_lower[i - 1]
            direction[i]   = direction[i - 1]
            supertrend[i]  = supertrend[i - 1]
            continue

        # Final Upper Band
        fu_prev = final_upper[i - 1]
        fu_prev = fu_prev if not np.isnan(fu_prev) else bu_arr[i]
        final_upper[i] = (
            bu_arr[i] if (bu_arr[i] < fu_prev or close_arr[i - 1] > fu_prev)
            else fu_prev
        )

        # Final Lower Band
        fl_prev = final_lower[i - 1]
        fl_prev = fl_prev if not np.isnan(fl_prev) else bl_arr[i]
        final_lower[i] = (
            bl_arr[i] if (bl_arr[i] > fl_prev or close_arr[i - 1] < fl_prev)
            else fl_prev
        )

        # Direction
        prev_dir = direction[i - 1]
        prev_st  = supertrend[i - 1]
        if np.isnan(prev_st):
            prev_st = final_upper[i - 1]

        if prev_dir == -1:   # was bearish
            direction[i] = 1 if close_arr[i] > final_upper[i] else -1
        else:                # was bullish
            direction[i] = -1 if close_arr[i] < final_lower[i] else 1

        # SuperTrend line
        supertrend[i] = (
            final_lower[i] if direction[i] == 1
            else final_upper[i]
        )

    # Detect flips (direction change from prior bar)
    flip = np.zeros(n, dtype=bool)
    for i in range(first + 1, n):
        if not np.isnan(direction[i]) and not np.isnan(direction[i - 1]):
            flip[i] = direction[i] != direction[i - 1]

    return pd.DataFrame({
        "supertrend":  supertrend,
        "direction":   direction,
        "upper_band":  final_upper,
        "lower_band":  final_lower,
        "flip":        flip,
    }, index=df.index)


# ─── Trailing stop calculation ────────────────────────────────────────────────

def _trailing_stop_series(
    close:   pd.Series,
    atr_s:   pd.Series,
    direction: int,       # 1 = long trailing stop, -1 = short trailing stop
    trail_mult: float = TRAIL_MULT,
) -> pd.Series:
    """
    Compute a trailing stop series that:
      - For longs  : starts at close[0] - atr * mult, only moves UP.
      - For shorts : starts at close[0] + atr * mult, only moves DOWN.
    This simulates the worst-case trailing stop for the entire history.
    """
    n       = len(close)
    ts      = np.full(n, np.nan)
    c_arr   = close.values
    atr_arr = atr_s.values

    for i in range(n):
        if np.isnan(atr_arr[i]):
            ts[i] = ts[i - 1] if i > 0 else np.nan
            continue

        raw = (
            c_arr[i] - trail_mult * atr_arr[i]   # long trail
            if direction == 1
            else c_arr[i] + trail_mult * atr_arr[i]  # short trail
        )

        if i == 0 or np.isnan(ts[i - 1]):
            ts[i] = raw
        elif direction == 1:
            ts[i] = max(ts[i - 1], raw)   # only move up
        else:
            ts[i] = min(ts[i - 1], raw)   # only move down

    return pd.Series(ts, index=close.index)


# ─── Single-bar signal evaluation ────────────────────────────────────────────

def _evaluate(
    df:           pd.DataFrame,
    st_df:        pd.DataFrame,
    adx_data:     Dict[str, pd.Series],
    vol_ma_s:     pd.Series,
    macd_data:    Dict[str, pd.Series],
    atr_s:        pd.Series,
    params:       Dict[str, Any],
) -> Dict[str, Any]:
    """
    Evaluate the current bar (last row of df) against all four confirmations.
    Returns the full analysis dict.
    """
    if len(df) < 2:
        return {"error": "Not enough bars"}

    i        = len(df) - 1
    close    = float(df["close"].iloc[i])
    volume   = float(df["volume"].iloc[i])

    # ── SuperTrend ────────────────────────────────────────────────────────
    st_val    = float(st_df["supertrend"].iloc[i])
    st_dir    = float(st_df["direction"].iloc[i])   # 1 or -1
    st_flip   = bool(st_df["flip"].iloc[i])
    upper_b   = float(st_df["upper_band"].iloc[i])
    lower_b   = float(st_df["lower_band"].iloc[i])

    if np.isnan(st_dir):
        return {"error": "SuperTrend not yet computed (insufficient data)"}

    direction = "bull" if st_dir == 1.0 else "bear"
    st_ok     = True   # SuperTrend always gives a direction

    # ── ADX ───────────────────────────────────────────────────────────────
    adx_val    = float(adx_data["adx"].iloc[i])
    plus_di    = float(adx_data["plus_di"].iloc[i])
    minus_di   = float(adx_data["minus_di"].iloc[i])

    adx_ok = not np.isnan(adx_val) and adx_val >= params["adx_threshold"]

    # ── Volume ────────────────────────────────────────────────────────────
    vol_ma_val = float(vol_ma_s.iloc[i])
    vol_ok     = not np.isnan(vol_ma_val) and volume > vol_ma_val
    vol_ratio  = round(volume / vol_ma_val, 2) if (not np.isnan(vol_ma_val) and vol_ma_val > 0) else None

    # ── MACD Histogram ────────────────────────────────────────────────────
    hist      = macd_data["histogram"]
    hist_curr = float(hist.iloc[i])
    hist_prev = float(hist.iloc[i - 1]) if i >= 1 else np.nan
    hist_prev2 = float(hist.iloc[i - 2]) if i >= 2 else np.nan

    # For bull: histogram must be positive AND (rising for 2 bars OR just crossed above 0)
    # For bear: histogram must be negative AND (falling for 2 bars OR just crossed below 0)
    if direction == "bull":
        hist_positive = hist_curr > 0
        hist_rising   = not np.isnan(hist_prev) and hist_curr > hist_prev
        hist_crossed  = (not np.isnan(hist_prev) and hist_prev <= 0 and hist_curr > 0)
        macd_ok       = hist_positive and (hist_rising or hist_crossed)
        macd_momentum = "rising" if hist_rising else ("crossed" if hist_crossed else "flat")
    else:
        hist_negative = hist_curr < 0
        hist_falling  = not np.isnan(hist_prev) and hist_curr < hist_prev
        hist_crossed  = (not np.isnan(hist_prev) and hist_prev >= 0 and hist_curr < 0)
        macd_ok       = hist_negative and (hist_falling or hist_crossed)
        macd_momentum = "falling" if hist_falling else ("crossed" if hist_crossed else "flat")

    # ── ATR for trade params ──────────────────────────────────────────────
    atr_val = float(atr_s.iloc[i])

    # ── Confirmations count ───────────────────────────────────────────────
    confirmations = {
        "supertrend": st_ok,
        "adx":        adx_ok,
        "volume":     vol_ok,
        "macd_hist":  macd_ok,
    }
    score = sum(1 for v in confirmations.values() if v)

    # ── Signal state ──────────────────────────────────────────────────────
    adx_blocked = not np.isnan(adx_val) and adx_val < params["adx_min"]
    if adx_blocked:
        state  = "blocked"
        action = "hold"
    elif score == 4:
        state  = "signal"
        action = "buy" if direction == "bull" else "sell"
    elif score >= 2 and st_ok and adx_ok:
        state  = "watch"
        action = "wait"
    else:
        state  = "no_signal"
        action = "hold"

    # ── Trade parameters ──────────────────────────────────────────────────
    trade_params: Dict[str, Any] = {}
    if state == "signal" and not np.isnan(atr_val):
        if direction == "bull":
            sl         = round(close - params["sl_mult"]  * atr_val, 6)
            trail_init = round(close - params["trail_mult"] * atr_val, 6)
        else:
            sl         = round(close + params["sl_mult"]  * atr_val, 6)
            trail_init = round(close + params["trail_mult"] * atr_val, 6)

        risk_pts = round(abs(close - sl), 6)
        trade_params = {
            "entry":            round(close, 6),
            "sl":               sl,
            "trail_stop_init":  trail_init,
            "trail_note":       (
                "Trail moves with price in your favour; exits when price pulls "
                f"back {params['trail_mult']}× ATR. SuperTrend flip also triggers exit."
            ),
            "atr":              round(atr_val, 6),
            "risk_pts":         risk_pts,
            "sl_mult":          params["sl_mult"],
            "trail_mult":       params["trail_mult"],
            "exit_triggers":    [
                "Price hits hard SL",
                "Price hits trailing stop",
                "SuperTrend flips direction",
            ],
        }

    # ── Build reasons ─────────────────────────────────────────────────────
    headers = {
        "signal":    f"AR-ATR {'BUY' if action == 'buy' else 'SELL'} — all 4 confirmations aligned",
        "watch":     f"AR-ATR WATCH — {score}/4 confirmations, waiting on {'volume + MACD' if not vol_ok and not macd_ok else 'MACD' if not macd_ok else 'volume'}",
        "no_signal": f"AR-ATR NO SIGNAL — only {score}/4 confirmations",
        "blocked":   f"AR-ATR BLOCKED — ADX={adx_val:.1f} (< {params['adx_min']:.0f}), market is ranging",
    }

    reasons = [headers.get(state, state)]
    reasons.append(
        f"{'✓' if st_ok else '✗'} SuperTrend: {direction.upper()} | "
        f"ST line={st_val:.4f} | {'⚡ FLIP this bar' if st_flip else 'stable'}"
    )
    reasons.append(
        f"{'✓' if adx_ok else '✗'} ADX: {adx_val:.1f} "
        f"({'strong' if adx_ok else 'weak/ranging'}, threshold {params['adx_threshold']:.0f}) | "
        f"+DI={plus_di:.1f} −DI={minus_di:.1f}"
    )
    reasons.append(
        f"{'✓' if vol_ok else '✗'} Volume: {volume:,.0f} vs MA({params['vol_ma_period']})={vol_ma_val:,.0f} "
        f"({'above' if vol_ok else 'below'} average{f', {vol_ratio}×' if vol_ratio else ''})"
    )
    reasons.append(
        f"{'✓' if macd_ok else '✗'} MACD Histogram: {hist_curr:+.6f} "
        f"({'positive' if hist_curr > 0 else 'negative'}, {macd_momentum})"
    )

    if state == "signal":
        reasons.append(
            f"SL={trade_params['sl']:.4f} ({params['sl_mult']}×ATR) | "
            f"Trail init={trade_params['trail_stop_init']:.4f} ({params['trail_mult']}×ATR)"
        )
        reasons.append(
            "Exit: hard SL hit OR trailing stop hit OR SuperTrend flip"
        )

    return {
        "direction":      direction,
        "state":          state,
        "action":         action,
        "confirmations":  confirmations,
        "score":          score,
        "indicators": {
            "supertrend":   round(st_val,  6) if not np.isnan(st_val) else None,
            "st_direction": direction,
            "st_flip":      st_flip,
            "upper_band":   round(upper_b, 6) if not np.isnan(upper_b) else None,
            "lower_band":   round(lower_b, 6) if not np.isnan(lower_b) else None,
            "adx":          round(adx_val, 2) if not np.isnan(adx_val) else None,
            "plus_di":      round(plus_di, 2) if not np.isnan(plus_di) else None,
            "minus_di":     round(minus_di, 2) if not np.isnan(minus_di) else None,
            "atr":          round(atr_val, 6) if not np.isnan(atr_val) else None,
            "close":        round(close, 6),
            "volume":       round(volume, 2),
            "vol_ma":       round(vol_ma_val, 2) if not np.isnan(vol_ma_val) else None,
            "vol_ratio":    vol_ratio,
            "macd_hist":    round(hist_curr, 8) if not np.isnan(hist_curr) else None,
            "macd_hist_prev": round(hist_prev, 8) if not np.isnan(hist_prev) else None,
            "macd_momentum": macd_momentum,
        },
        "trade_params": trade_params if trade_params else None,
        "reasons":      reasons,
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

async def analyze_ar_atr(
    symbol:      str,
    timeframe:   str  = "1h",
    exchange:    Optional[SupportedExchange] = None,
    # Overrideable parameters
    st_period:      int   = ST_ATR_PERIOD,
    st_mult:        float = ST_MULTIPLIER,
    adx_threshold:  float = ADX_THRESHOLD,
    adx_min:        float = ADX_MIN,
    vol_ma_period:  int   = VOL_MA_PERIOD,
    sl_mult:        float = SL_MULT,
    trail_mult:     float = TRAIL_MULT,
) -> Dict[str, Any]:
    """
    Run the AR-ATR Trend Multi-Confirmation strategy on a single symbol + timeframe.

    Returns dict keys:
      symbol         : str
      timeframe      : str
      state          : "signal" | "watch" | "no_signal" | "blocked" | "no_data"
      action         : "buy" | "sell" | "wait" | "hold"
      direction      : "bull" | "bear"
      score          : int  (0–4 confirmations met)
      confirmations  : {supertrend, adx, volume, macd_hist}
      indicators     : all computed values for the current bar
      trade_params   : {entry, sl, trail_stop_init, atr, risk_pts} or None
      reasons        : [str, ...]
      params_used    : echo back the parameters used
    """
    exch      = exchange or DEFAULT_EXCHANGE
    connector = exchange_manager.get_exchange(exch)
    if not connector:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "state": "no_data", "action": "hold",
            "error": "Exchange not initialized",
        }

    # ── Fetch OHLCV ───────────────────────────────────────────────────────
    try:
        ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=timeframe, limit=TF_LIMIT)
    except Exception as exc:
        logger.debug(f"[AR-ATR] {symbol} {timeframe} fetch error: {exc}")
        return {
            "symbol": symbol, "timeframe": timeframe,
            "state": "no_data", "action": "hold",
            "error": str(exc),
        }

    if not ohlcv or len(ohlcv) < MIN_CANDLES:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "state": "no_data", "action": "hold",
            "error": f"Not enough candles ({len(ohlcv) if ohlcv else 0} < {MIN_CANDLES})",
        }

    df = ohlcv_to_dataframe(ohlcv)

    # ── Compute all indicators ────────────────────────────────────────────
    params: Dict[str, Any] = {
        "st_period":      st_period,
        "st_mult":        st_mult,
        "adx_threshold":  adx_threshold,
        "adx_min":        adx_min,
        "vol_ma_period":  vol_ma_period,
        "sl_mult":        sl_mult,
        "trail_mult":     trail_mult,
    }

    try:
        st_df     = _supertrend(df, atr_period=st_period, multiplier=st_mult)
        adx_data  = _calc_adx(df, period=14)
        vol_ma_s  = sma(df["volume"], vol_ma_period)
        macd_data = _calc_macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal_period=MACD_SIGNAL)

        # ATR for trade params (use separate standalone ATR, same period as SuperTrend)
        tr1   = df["high"] - df["low"]
        tr2   = (df["high"] - df["close"].shift(1)).abs()
        tr3   = (df["low"]  - df["close"].shift(1)).abs()
        tr    = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1.0 / st_period, min_periods=st_period).mean()

    except Exception as exc:
        logger.error(f"[AR-ATR] indicator computation error: {exc}")
        return {
            "symbol": symbol, "timeframe": timeframe,
            "state": "no_data", "action": "hold",
            "error": f"Indicator computation failed: {exc}",
        }

    # ── Evaluate current bar ──────────────────────────────────────────────
    result = _evaluate(
        df         = df,
        st_df      = st_df,
        adx_data   = adx_data,
        vol_ma_s   = vol_ma_s,
        macd_data  = macd_data,
        atr_s      = atr_s,
        params     = params,
    )

    if "error" in result:
        return {
            "symbol": symbol, "timeframe": timeframe,
            "state": "no_data", "action": "hold",
            **result,
        }

    return _to_py({
        "symbol":      symbol,
        "timeframe":   timeframe,
        "state":       result["state"],
        "action":      result["action"],
        "direction":   result["direction"],
        "score":       result["score"],
        "confirmations": result["confirmations"],
        "indicators":  result["indicators"],
        "trade_params": result["trade_params"],
        "reasons":     result["reasons"],
        "params_used": params,
        "candles_used": len(df),
    })
