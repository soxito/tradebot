"""
AI Market Analyst — Indicator Engine

Computes technical indicators from OHLCV data for injection into agent prompts.
Uses pandas + numpy for lightweight computation — no TA-Lib dependency.
"""
from typing import Dict, List, Optional
import numpy as np
from loguru import logger


def _ema(data: List[float], period: int) -> List[float]:
    """Exponential moving average."""
    if len(data) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(data[:period]) / period]
    for price in data[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """Average True Range."""
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    return round(sum(true_ranges[-period:]) / period, 6)


def _vwap(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> Optional[float]:
    """Volume-weighted average price (cumulative for the window)."""
    if not volumes or len(volumes) != len(closes):
        return None
    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    cum_tp_vol = sum(t * v for t, v in zip(typical, volumes))
    cum_vol = sum(volumes)
    if cum_vol == 0:
        return None
    return round(cum_tp_vol / cum_vol, 6)


def _support_resistance(highs: List[float], lows: List[float], lookback: int = 20) -> Dict:
    """Simple support/resistance from recent swing highs/lows."""
    recent_h = highs[-lookback:] if len(highs) >= lookback else highs
    recent_l = lows[-lookback:] if len(lows) >= lookback else lows
    return {
        "resistance": round(max(recent_h), 6) if recent_h else None,
        "support": round(min(recent_l), 6) if recent_l else None,
    }


def _false_breakout_signals(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    lookback: int = 20,
) -> Dict:
    """
    Detect false breakout / stop-hunt conditions on the most recent bars.

    Indicators computed (all describe the LAST bar unless noted):

    wick_ratio_upper  — upper wick / total range (0–1). High value (>0.6) on a
                        bullish breakout bar = exhaustion / rejection above.
    wick_ratio_lower  — lower wick / total range (0–1). High value on a bearish
                        breakdown bar = demand absorbing supply = false breakdown.
    is_rejection_candle — True when the last bar is a pin-bar / hammer / shooting
                        star: one wick ≥ 2× the candle body, total range > 0.
    close_position    — where the close sits in the bar's range [0=low, 1=high].
                        Close near lows (< 0.35) on a bullish break = bearish close,
                        suggesting the breakout failed intrabar.
    structure_breach_recovered — True when the close of the PREVIOUS bar exceeded
                        the 20-bar high/low but the CURRENT bar closed back inside.
                        Classic single-bar false breakout (stop hunt + reversal).
    volume_breakout_ratio — current bar volume / 20-bar average volume.
                        < 0.8 on a structure break = no institutional conviction.
                        > 1.5 = confirmed breakout with volume.
    sweep_high        — price spiked above the 20-bar high this bar but closed below.
    sweep_low         — price spiked below the 20-bar low this bar but closed above.
    false_breakout_score — 0–100 composite. Higher = stronger false-break evidence.
    """
    result: Dict = {}
    if len(closes) < 5:
        return result

    last_open  = opens[-1]
    last_high  = highs[-1]
    last_low   = lows[-1]
    last_close = closes[-1]
    bar_range  = last_high - last_low

    # Wick ratios
    upper_wick = last_high - max(last_open, last_close)
    lower_wick = min(last_open, last_close) - last_low
    result["wick_ratio_upper"] = round(upper_wick / bar_range, 3) if bar_range > 0 else 0.0
    result["wick_ratio_lower"] = round(lower_wick / bar_range, 3) if bar_range > 0 else 0.0

    # Rejection candle (pin bar)
    body = abs(last_close - last_open)
    is_rejection = (
        bar_range > 0 and body > 0
        and (upper_wick >= body * 2.0 or lower_wick >= body * 2.0)
    )
    result["is_rejection_candle"] = is_rejection

    # Close position in bar range
    result["close_position"] = round((last_close - last_low) / bar_range, 3) if bar_range > 0 else 0.5

    # Structure breach + close back inside (false breakout)
    recent_h = highs[-(lookback + 1):-1] if len(highs) > lookback else highs[:-1]
    recent_l = lows[-(lookback + 1):-1]  if len(lows)  > lookback else lows[:-1]
    prior_high = max(recent_h) if recent_h else last_high
    prior_low  = min(recent_l) if recent_l else last_low

    prev_close = closes[-2] if len(closes) >= 2 else last_close
    result["structure_breach_recovered"] = bool(
        (prev_close > prior_high and last_close <= prior_high) or
        (prev_close < prior_low  and last_close >= prior_low)
    )

    # Sweep: wick penetrates structure, close does not
    result["sweep_high"] = bool(last_high > prior_high and last_close <= prior_high)
    result["sweep_low"]  = bool(last_low  < prior_low  and last_close >= prior_low)

    # Volume breakout confirmation
    vol_window = [v for v in volumes[-(lookback + 1):-1] if v and v > 0]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
    cur_vol = volumes[-1] if volumes and volumes[-1] else 0.0
    result["volume_breakout_ratio"] = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0

    # Composite false-breakout score (0–100)
    score = 0.0
    if result["sweep_high"] or result["sweep_low"]:
        score += 35.0
    if result["structure_breach_recovered"]:
        score += 25.0
    if is_rejection:
        score += 20.0
    # Close near the wrong end of the bar amplifies the score
    cp = result["close_position"]
    if cp < 0.25 or cp > 0.75:
        score += 10.0
    # Low volume on a structure break is suspicious
    if result["volume_breakout_ratio"] < 0.8:
        score += 10.0
    result["false_breakout_score"] = round(min(score, 100.0), 1)

    return result


def compute_indicators(
    ohlcv: List[Dict],
    requested: Optional[List[str]] = None,
) -> Dict:
    """
    Compute requested indicators from OHLCV candle list.

    Each candle: {"open": float, "high": float, "low": float, "close": float, "volume": float}

    Returns: {indicator_name: value, ...}
    """
    if not ohlcv:
        return {}

    opens = [c["open"] for c in ohlcv]
    highs = [c["high"] for c in ohlcv]
    lows = [c["low"] for c in ohlcv]
    closes = [c["close"] for c in ohlcv]
    volumes = [c.get("volume", 0) for c in ohlcv]

    all_indicators = requested or ["RSI", "EMA_20", "EMA_50", "ATR", "VWAP", "STRUCTURE", "FALSE_BREAKOUT"]

    result: Dict = {}
    current_price = closes[-1] if closes else None
    result["price"] = current_price

    for ind in all_indicators:
        ind_upper = ind.upper()
        try:
            if ind_upper == "RSI":
                result["RSI"] = _rsi(closes)
            elif ind_upper.startswith("EMA_"):
                period = int(ind_upper.split("_")[1])
                ema_vals = _ema(closes, period)
                result[ind_upper] = round(ema_vals[-1], 6) if ema_vals else None
            elif ind_upper == "ATR":
                result["ATR"] = _atr(highs, lows, closes)
            elif ind_upper == "VWAP":
                result["VWAP"] = _vwap(highs, lows, closes, volumes)
            elif ind_upper == "STRUCTURE":
                result["structure"] = _support_resistance(highs, lows)
            elif ind_upper.startswith("SMA_"):
                period = int(ind_upper.split("_")[1])
                if len(closes) >= period:
                    result[ind_upper] = round(sum(closes[-period:]) / period, 6)
                else:
                    result[ind_upper] = None
            elif ind_upper == "FALSE_BREAKOUT":
                result["false_breakout"] = _false_breakout_signals(
                    opens, highs, lows, closes, volumes
                )
        except Exception as exc:
            logger.debug(f"Indicator {ind} failed: {exc}")
            result[ind_upper] = None

    return result
