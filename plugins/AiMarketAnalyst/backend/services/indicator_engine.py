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

    all_indicators = requested or ["RSI", "EMA_20", "EMA_50", "ATR", "VWAP", "STRUCTURE"]

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
        except Exception as exc:
            logger.debug(f"Indicator {ind} failed: {exc}")
            result[ind_upper] = None

    return result
