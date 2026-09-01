"""
Technical Analysis Module (OctoBot-inspired)
Calculates indicators from OHLCV data: Volume MA, Price MA, RSI, MACD, Bollinger Bands,
ADX (trend strength), EMA 50/200 (golden/death cross), Stochastic RSI, ATR.
Scoring normalised to [-1, +1] using evaluator-style approach.
All calculations use pure numpy/pandas — no external TA libraries needed.
"""
import math
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from loguru import logger


def ohlcv_to_dataframe(ohlcv: List[List]) -> pd.DataFrame:
    """Convert raw ccxt OHLCV list to DataFrame.
    Input: [[timestamp_ms, open, high, low, close, volume], ...]
    """
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ────────────────────────────────────────────────────────────────
# Individual indicator functions
# ────────────────────────────────────────────────────────────────

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def volume_ma(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """Volume Simple Moving Average"""
    return sma(df["volume"], period)


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index"""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def stochastic_rsi(df: pd.DataFrame, rsi_period: int = 14, stoch_period: int = 14) -> pd.Series:
    """Stochastic RSI — oscillator of RSI, range 0-100"""
    rsi_vals = rsi(df, rsi_period)
    rsi_min = rsi_vals.rolling(window=stoch_period, min_periods=stoch_period).min()
    rsi_max = rsi_vals.rolling(window=stoch_period, min_periods=stoch_period).max()
    stoch = ((rsi_vals - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)) * 100
    return stoch


def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Dict[str, pd.Series]:
    """MACD line, signal line, histogram"""
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def bollinger_bands(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> Dict[str, pd.Series]:
    """Bollinger Bands: upper, middle, lower, %B"""
    middle = sma(df["close"], period)
    rolling_std = df["close"].rolling(window=period, min_periods=period).std()
    upper = middle + std_dev * rolling_std
    lower = middle - std_dev * rolling_std
    pct_b = (df["close"] - lower) / (upper - lower)
    return {"upper": upper, "middle": middle, "lower": lower, "pct_b": pct_b}


def buy_sell_volume(df: pd.DataFrame) -> Dict[str, pd.Series]:
    """
    Estimate buy/sell volume from candle structure.
    If close > open → more buying pressure; the buy ratio = (close-low)/(high-low)
    """
    price_range = (df["high"] - df["low"]).replace(0, np.nan)
    buy_ratio = (df["close"] - df["low"]) / price_range
    buy_ratio = buy_ratio.fillna(0.5)
    buy_vol = df["volume"] * buy_ratio
    sell_vol = df["volume"] * (1 - buy_ratio)
    return {"buy_volume": buy_vol, "sell_volume": sell_vol, "buy_ratio": buy_ratio}


def pivot_highs(series: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """
    Detect pivot highs (swing highs). A bar is a pivot high if it's the highest
    of `left` bars before and `right` bars after. Returns NaN where no pivot.
    Mirrors TradingView's ta.pivothigh(source, leftbars, rightbars).

    Vectorised: rolling window max + uniqueness check instead of a per-bar
    Python loop with .iloc slicing (the old version dominated profile time).
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    result = pd.Series(np.nan, index=series.index)
    if n <= left + right:
        return result
    win = left + right + 1
    # Rolling max over the full window centred on each candidate bar.
    s = pd.Series(values, index=series.index)
    roll_max = s.rolling(win, center=True).max()
    candidates = np.nonzero((values == roll_max.to_numpy()) & ~np.isnan(roll_max.to_numpy()))[0]
    for i in candidates:
        if i < left or i >= n - right:
            continue
        w = values[i - left: i + right + 1]
        if (w == values[i]).sum() == 1:
            result.iloc[i] = values[i]
    return result


def pivot_lows(series: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """
    Detect pivot lows (swing lows). A bar is a pivot low if it's the lowest
    of `left` bars before and `right` bars after. Returns NaN where no pivot.
    Mirrors TradingView's ta.pivotlow(source, leftbars, rightbars).
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    result = pd.Series(np.nan, index=series.index)
    if n <= left + right:
        return result
    win = left + right + 1
    s = pd.Series(values, index=series.index)
    roll_min = s.rolling(win, center=True).min()
    candidates = np.nonzero((values == roll_min.to_numpy()) & ~np.isnan(roll_min.to_numpy()))[0]
    for i in candidates:
        if i < left or i >= n - right:
            continue
        w = values[i - left: i + right + 1]
        if (w == values[i]).sum() == 1:
            result.iloc[i] = values[i]
    return result


def ichimoku(
    df: pd.DataFrame,
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
) -> Dict[str, Any]:
    """
    Ichimoku Kinko Hyo. Returns the four lines plus where the current close sits
    relative to the cloud.

    The spans are NOT shifted forward here: the standard 26-bar displacement
    projects the cloud into the future, but the question asked of it — is the
    cloud above or below price right now — is about the cloud drawn at the
    current bar, which is the unshifted value.

    Returns tenkan/kijun/senkou_a/senkou_b series plus:
      - cloud_top / cloud_bottom: latest values (None when history is short)
      - position: "above" | "below" | "inside" | None
    """
    high, low, close = df["high"], df["low"], df["close"]

    def midpoint(period: int) -> pd.Series:
        return (high.rolling(period, min_periods=period).max()
                + low.rolling(period, min_periods=period).min()) / 2

    tenkan = midpoint(conversion)
    kijun = midpoint(base)
    senkou_a = (tenkan + kijun) / 2
    senkou_b = midpoint(span_b)

    out: Dict[str, Any] = {
        "tenkan": tenkan, "kijun": kijun,
        "senkou_a": senkou_a, "senkou_b": senkou_b,
        "cloud_top": None, "cloud_bottom": None, "position": None,
    }
    if len(df) < span_b:
        return out

    a, b = senkou_a.iloc[-1], senkou_b.iloc[-1]
    if pd.isna(a) or pd.isna(b):
        return out

    top, bottom = float(max(a, b)), float(min(a, b))
    price = float(close.iloc[-1])
    out["cloud_top"], out["cloud_bottom"] = round(top, 6), round(bottom, 6)
    out["position"] = "above" if price > top else "below" if price < bottom else "inside"
    return out


def detect_triangle(
    df: pd.DataFrame,
    pivot_left: int = 5,
    pivot_right: int = 5,
    flat_tolerance_pct: float = 0.6,
    min_pivots: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Ascending / descending triangle, read off confirmed pivots.

    Deliberately narrow: it only claims a triangle when one side is genuinely
    flat (every pivot within `flat_tolerance_pct` of their mean) and the other
    is monotonically converging toward it. Anything else returns None — an
    unnamed pattern is far better than a named one that isn't there, since the
    name is what a reader will trade against.

    Returns {kind, level, broken, ...} where `level` is the flat boundary and
    `broken` says whether the latest close has closed through it.
    """
    if len(df) < (pivot_left + pivot_right + 10):
        return None

    ph = pivot_highs(df["high"], pivot_left, pivot_right).dropna()
    pl = pivot_lows(df["low"], pivot_left, pivot_right).dropna()
    if len(ph) < min_pivots or len(pl) < min_pivots:
        return None

    highs = [float(v) for v in ph.iloc[-min_pivots:]]
    lows = [float(v) for v in pl.iloc[-min_pivots:]]
    close = float(df["close"].iloc[-1])

    def is_flat(values: List[float]) -> bool:
        mean = sum(values) / len(values)
        if mean == 0:
            return False
        return all(abs(v - mean) / abs(mean) * 100 <= flat_tolerance_pct for v in values)

    rising = all(b > a for a, b in zip(lows, lows[1:]))
    falling = all(b < a for a, b in zip(highs, highs[1:]))

    if is_flat(highs) and rising:
        level = sum(highs) / len(highs)
        return {
            "kind": "ascending", "level": round(level, 6),
            "broken": close > level, "direction": "up" if close > level else None,
        }
    if is_flat(lows) and falling:
        level = sum(lows) / len(lows)
        return {
            "kind": "descending", "level": round(level, 6),
            "broken": close < level, "direction": "down" if close < level else None,
        }
    return None


def support_resistance_mtf(
    df: pd.DataFrame,
    pivot_left: int = 5,
    pivot_right: int = 5,
    zone_merge_pct: float = 0.5,
    max_levels: int = 8,
    min_touches: int = 2,
    include_lines: bool = False,
) -> Dict[str, Any]:
    """
    Support and Resistance Signals MTF — replicates the TradingView indicator logic.

    Algorithm:
    1. Detect pivot highs and pivot lows using left/right bar lookback.
    2. Cluster nearby pivots into S/R zones (merge within zone_merge_pct %).
    3. Score zones by number of touches (more touches = stronger level).
    4. Generate signals: BUY when price bounces off support, SELL at resistance.
    5. Return level lines + signal markers for chart overlay.

    Args:
      include_lines: build per-bar chart-line series. Only chart-overlay
        consumers need these (~max_levels × len(df) dicts); scoring callers
        leave it off so the hot pipeline never pays that cost.

    Returns:
      - levels: list of {price, type, strength, touches, first_time, last_time}
      - signals: list of {time, type, price, level_price}
      - support_lines: series data for chart (empty unless include_lines)
      - resistance_lines: series data for chart (empty unless include_lines)
    """
    highs = df["high"]
    lows = df["low"]
    closes = df["close"]
    timestamps = df["timestamp"]

    # Step 1: Find all pivot points
    ph = pivot_highs(highs, pivot_left, pivot_right)
    pl = pivot_lows(lows, pivot_left, pivot_right)

    # Collect all pivot levels with their timestamps and type
    pivots = []
    for i in range(len(df)):
        if not np.isnan(ph.iloc[i]):
            pivots.append({"price": ph.iloc[i], "type": "resistance", "idx": i})
        if not np.isnan(pl.iloc[i]):
            pivots.append({"price": pl.iloc[i], "type": "support", "idx": i})

    if not pivots:
        return {"levels": [], "signals": [], "support_lines": [], "resistance_lines": []}

    # Step 2: Cluster pivots into S/R zones
    pivots.sort(key=lambda p: p["price"])
    zones: list[dict] = []

    for pv in pivots:
        merged = False
        for zone in zones:
            # Merge if within zone_merge_pct% of zone center
            pct_diff = abs(pv["price"] - zone["center"]) / zone["center"] * 100
            if pct_diff <= zone_merge_pct:
                zone["touches"].append(pv)
                zone["center"] = np.mean([t["price"] for t in zone["touches"]])
                zone["high"] = max(t["price"] for t in zone["touches"])
                zone["low"] = min(t["price"] for t in zone["touches"])
                zone["first_idx"] = min(zone["first_idx"], pv["idx"])
                zone["last_idx"] = max(zone["last_idx"], pv["idx"])
                # Zone type: if mostly resistance pivots → resistance, else support
                res_count = sum(1 for t in zone["touches"] if t["type"] == "resistance")
                sup_count = len(zone["touches"]) - res_count
                zone["type"] = "resistance" if res_count >= sup_count else "support"
                merged = True
                break
        if not merged:
            zones.append({
                "center": pv["price"],
                "high": pv["price"],
                "low": pv["price"],
                "type": pv["type"],
                "touches": [pv],
                "first_idx": pv["idx"],
                "last_idx": pv["idx"],
            })

    # Step 3: Filter by minimum touches and sort by strength
    strong_zones = [z for z in zones if len(z["touches"]) >= min_touches]

    # If not enough zones with min_touches, relax to single-touch
    if len(strong_zones) < 4:
        strong_zones = zones

    # Sort by touch count (strength), take top max_levels
    strong_zones.sort(key=lambda z: len(z["touches"]), reverse=True)
    strong_zones = strong_zones[:max_levels]

    # Build levels
    levels = []
    for z in strong_zones:
        first_ts = int(timestamps.iloc[z["first_idx"]].timestamp()) if z["first_idx"] < len(timestamps) else 0
        last_ts = int(timestamps.iloc[z["last_idx"]].timestamp()) if z["last_idx"] < len(timestamps) else 0
        levels.append({
            "price": round(z["center"], 6),
            "type": z["type"],
            "strength": len(z["touches"]),
            "touches": len(z["touches"]),
            "zone_high": round(z["high"], 6),
            "zone_low": round(z["low"], 6),
            "first_time": first_ts,
            "last_time": last_ts,
        })

    # Step 4: Generate buy/sell signals at S/R bounces
    signals = []
    current_price = closes.iloc[-1]

    # Build S/R line data for chart overlay (horizontal lines extending to current bar)
    support_lines = []
    resistance_lines = []

    if include_lines:
        # One numpy pass for all timestamps instead of a .iloc call per bar.
        ts_array = pd.to_datetime(timestamps).astype("int64").to_numpy() // 10**9
        end_time = int(ts_array[-1])

        for level in levels:
            price = level["price"]
            start_time = level["first_time"]

            mask = ts_array >= start_time
            line_data = [
                {"time": int(t), "value": price}
                for t in ts_array[mask]
            ]

            if level["type"] == "support":
                support_lines.append({
                    "price": price,
                    "strength": level["strength"],
                    "data": line_data,
                })
            else:
                resistance_lines.append({
                    "price": price,
                    "strength": level["strength"],
                    "data": line_data,
                })

    # Generate signals: price approaching or bouncing from S/R levels
    atr_val = atr(df, 14).iloc[-1] if len(df) >= 15 else (highs.iloc[-1] - lows.iloc[-1])
    if np.isnan(atr_val):
        atr_val = (highs.iloc[-1] - lows.iloc[-1])

    for i in range(max(pivot_left + pivot_right + 1, 20), len(df)):
        bar_close = closes.iloc[i]
        bar_low = lows.iloc[i]
        bar_high = highs.iloc[i]
        prev_close = closes.iloc[i - 1]
        prev_low = lows.iloc[i - 1]
        prev_high = highs.iloc[i - 1]
        ts = int(timestamps.iloc[i].timestamp())
        local_atr = atr_val  # Simplified: use latest ATR

        for level in levels:
            price = level["price"]
            tolerance = local_atr * 0.3  # Within 30% of ATR from level

            if level["type"] == "support":
                # Buy signal: price dips near support and bounces (close back above)
                if (prev_low <= price + tolerance and
                    prev_close < price + tolerance and
                    bar_close > price and
                    bar_close > prev_close):
                    signals.append({
                        "time": ts,
                        "type": "buy",
                        "price": round(bar_close, 6),
                        "level_price": price,
                        "level_type": "support_bounce",
                    })
                    break  # One signal per bar

            else:  # resistance
                # Sell signal: price rises near resistance and rejects
                if (prev_high >= price - tolerance and
                    prev_close > price - tolerance and
                    bar_close < price and
                    bar_close < prev_close):
                    signals.append({
                        "time": ts,
                        "type": "sell",
                        "price": round(bar_close, 6),
                        "level_price": price,
                        "level_type": "resistance_reject",
                    })
                    break

    return {
        "levels": levels,
        "signals": signals,
        "support_lines": support_lines,
        "resistance_lines": resistance_lines,
    }


DEFAULT_FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618, 2.618]

_FIB_DEFAULT_COLORS = {
    0: "#787b86",
    0.236: "#f44336",
    0.382: "#81c784",
    0.5: "#4caf50",
    0.618: "#009688",
    0.786: "#64b5f6",
    1: "#787b86",
    1.272: "#81c784",
    1.618: "#2962ff",
    2.618: "#f44336",
}


def zigzag_deviation_pct(df: pd.DataFrame, threshold_multiplier: float = 3.0) -> float:
    """Swing threshold scaled to the instrument's own volatility, as a percent.

    Mirrors the Pine `devThreshold := ta.atr(10) / close * 100 * multiplier`. A
    fixed percentage cannot serve every market: 5% is a routine crypto swing but
    more than gold moves in a week, so a fixed default finds no pivots at all on
    FX and metals — the instruments this bot mostly charts.
    """
    a = atr(df, period=10)
    close = df["close"]
    pct = (a / close * 100.0 * threshold_multiplier).dropna()
    if pct.empty:
        return 5.0
    return max(float(pct.iloc[-1]), 0.01)


def zigzag_pivots(
    df: pd.DataFrame,
    deviation_pct: Optional[float] = None,
    depth: int = 10,
    threshold_multiplier: float = 3.0,
) -> Dict[str, Any]:
    """
    TradingView-style ZigZag pivot detection. Tracks the running price extreme in
    the current direction and confirms/flips a pivot only when price retraces
    `deviation_pct`% from that extreme, with at least `depth` bars between confirmed
    pivots. Distinct from pivot_highs/pivot_lows (symmetric left/right lookback) —
    mirrors the deviation/depth semantics of TradingView's zigzag(deviation, depth).

    `deviation_pct` defaults to an ATR-derived threshold (see zigzag_deviation_pct)
    so the same call works on crypto, FX and metals; pass a number to pin it.

    Returns:
      - pivots: chronological list of {idx, time, price, type: "high"|"low"}
      - last_swing: {start_idx, start_price, end_idx, end_price, direction} | None
        (the most recent confirmed pivot through to the current running extreme)
    """
    highs = df["high"].values
    lows = df["low"].values
    timestamps = df["timestamp"]
    n = len(df)
    if n < 2:
        return {"pivots": [], "last_swing": None}

    if deviation_pct is None:
        deviation_pct = zigzag_deviation_pct(df, threshold_multiplier)

    pivots: List[Dict[str, Any]] = []

    # Phase 1: determine the initial direction by tracking both running extremes
    # from bar 0 until price deviates deviation_pct% from one of them.
    run_high, run_high_idx = highs[0], 0
    run_low, run_low_idx = lows[0], 0
    direction = 0
    ext_idx, ext_price = 0, highs[0]
    start_idx = n

    for i in range(1, n):
        if highs[i] > run_high:
            run_high, run_high_idx = highs[i], i
        if lows[i] < run_low:
            run_low, run_low_idx = lows[i], i

        up_dev = (run_high - run_low) / run_low * 100 if run_low > 0 else 0
        if run_high_idx > run_low_idx and up_dev >= deviation_pct:
            direction = 1
            pivots.append({
                "idx": run_low_idx,
                "time": int(timestamps.iloc[run_low_idx].timestamp()),
                "price": run_low, "type": "low",
            })
            ext_idx, ext_price = run_high_idx, run_high
            start_idx = i + 1
            break

        down_dev = (run_high - run_low) / run_high * 100 if run_high > 0 else 0
        if run_low_idx > run_high_idx and down_dev >= deviation_pct:
            direction = -1
            pivots.append({
                "idx": run_high_idx,
                "time": int(timestamps.iloc[run_high_idx].timestamp()),
                "price": run_high, "type": "high",
            })
            ext_idx, ext_price = run_low_idx, run_low
            start_idx = i + 1
            break

    if direction == 0:
        return {"pivots": [], "last_swing": None}

    # Phase 2: extend the current leg, flipping direction on deviation once the
    # reversal is seen at least `depth` bars past the previous confirmed pivot.
    #
    # The depth guard counts from the current bar, not from the extreme. Counting
    # from the extreme deadlocks: once price reverses, the extreme stops moving,
    # so a reversal rejected as too-early can never age into acceptance and the
    # tracker freezes on a stale swing for the rest of the series.
    for i in range(start_idx, n):
        if direction == 1:
            if highs[i] > ext_price:
                ext_price, ext_idx = highs[i], i
            reversed_ = ext_price > 0 and lows[i] <= ext_price * (1 - deviation_pct / 100)
            if reversed_ and (i - pivots[-1]["idx"]) >= depth:
                pivots.append({
                    "idx": ext_idx, "time": int(timestamps.iloc[ext_idx].timestamp()),
                    "price": ext_price, "type": "high",
                })
                direction = -1
                ext_price, ext_idx = lows[i], i
        else:
            if lows[i] < ext_price:
                ext_price, ext_idx = lows[i], i
            reversed_ = ext_price > 0 and highs[i] >= ext_price * (1 + deviation_pct / 100)
            if reversed_ and (i - pivots[-1]["idx"]) >= depth:
                pivots.append({
                    "idx": ext_idx, "time": int(timestamps.iloc[ext_idx].timestamp()),
                    "price": ext_price, "type": "low",
                })
                direction = 1
                ext_price, ext_idx = highs[i], i

    last_pivot = pivots[-1]
    last_swing = {
        "start_idx": last_pivot["idx"],
        "start_price": last_pivot["price"],
        "end_idx": ext_idx,
        "end_price": ext_price,
        "direction": "up" if direction == 1 else "down",
    }
    return {"pivots": pivots, "last_swing": last_swing}


def auto_fib_retracement(
    df: pd.DataFrame,
    deviation_pct: Optional[float] = None,
    depth: int = 10,
    levels: Optional[Any] = None,
    extend_lines: bool = True,
) -> Dict[str, Any]:
    """
    Auto Fibonacci Retracement — detects the latest ZigZag swing (see zigzag_pivots)
    and computes fib retracement/extension levels off it. Mirrors the TradingView
    "Auto Fib Retracement" Pine Script: deviation/depth-based swing detection,
    per-level enable/color, lines extendable to the current bar.

    `levels` may be omitted (uses DEFAULT_FIB_LEVELS, all enabled), a list of
    floats, a list of {"ratio", "enabled", "color"} dicts, or a {ratio: {enabled,
    color}} mapping (as stored by the frontend's per-level toggle UI).

    Returns overlay-series-compatible data modeled on support_resistance_mtf():
      - swing: {start_time, start_price, end_time, end_price, direction}
      - levels: [{ratio, price, enabled, color, label}, ...]
      - golden_zone: {low, high} (the 0.5-0.618 zone, for confluence scoring)
      - lines: [{ratio, price, color, data}, ...] (enabled levels only)
    """
    zz = zigzag_pivots(df, deviation_pct=deviation_pct, depth=depth)
    swing = zz.get("last_swing")
    if not swing:
        return {"swing": None, "levels": [], "golden_zone": None, "lines": []}

    normalized: List[Dict[str, Any]] = []
    if isinstance(levels, dict):
        for ratio_key, cfg in levels.items():
            cfg = cfg or {}
            normalized.append({
                "ratio": float(ratio_key),
                "enabled": cfg.get("enabled", True),
                "color": cfg.get("color"),
            })
    elif levels:
        for item in levels:
            if isinstance(item, dict):
                normalized.append({
                    "ratio": float(item["ratio"]),
                    "enabled": item.get("enabled", True),
                    "color": item.get("color"),
                })
            else:
                normalized.append({"ratio": float(item), "enabled": True, "color": None})
    else:
        normalized = [{"ratio": r, "enabled": True, "color": None} for r in DEFAULT_FIB_LEVELS]

    start_price = swing["start_price"]
    end_price = swing["end_price"]
    height = end_price - start_price
    direction = swing["direction"]

    timestamps = df["timestamp"]
    start_time = int(timestamps.iloc[swing["start_idx"]].timestamp())
    swing_end_time = int(timestamps.iloc[swing["end_idx"]].timestamp())
    last_bar_idx = len(df) - 1

    out_levels = []
    lines = []
    for lv in normalized:
        ratio = lv["ratio"]
        price = end_price - height * ratio
        color = lv.get("color") or _FIB_DEFAULT_COLORS.get(ratio, "#9598a1")
        out_levels.append({
            "ratio": ratio,
            "price": round(price, 6),
            "enabled": lv["enabled"],
            "color": color,
            "label": f"{ratio * 100:.1f}%",
        })
        if lv["enabled"]:
            end_idx = last_bar_idx if extend_lines else swing["end_idx"]
            line_data = [
                {"time": int(timestamps.iloc[i].timestamp()), "value": round(price, 6)}
                for i in range(swing["end_idx"], end_idx + 1)
            ]
            lines.append({"ratio": ratio, "price": round(price, 6), "color": color, "data": line_data})

    golden_a = end_price - height * 0.5
    golden_b = end_price - height * 0.618
    golden_zone = {"low": round(min(golden_a, golden_b), 6), "high": round(max(golden_a, golden_b), 6)}

    return {
        "swing": {
            "start_time": start_time,
            "start_price": round(start_price, 6),
            "end_time": swing_end_time,
            "end_price": round(end_price, 6),
            "direction": direction,
        },
        "levels": out_levels,
        "golden_zone": golden_zone,
        "lines": lines,
    }


def fib_confluence_score(
    price: float,
    fib_result: Optional[Dict[str, Any]],
    direction: Optional[str] = None,
) -> Optional[float]:
    """
    Score 0..1 for how strongly `price` sits inside the fib golden zone
    (0.5-0.618 retracement band): full credit at the zone midpoint, decaying
    linearly to 0 at the zone edges, 0 outside the zone. Returns None when no
    confirmed swing/golden zone is available (caller should treat as inapplicable
    rather than a neutral 0, so weighting schemes can redistribute correctly).
    """
    if not fib_result:
        return None
    zone = fib_result.get("golden_zone")
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


def adx(df: pd.DataFrame, period: int = 14) -> Dict[str, pd.Series]:
    """
    Average Directional Index — measures trend strength (not direction).
    ADX > 25 = strong trend, < 20 = ranging/weak.
    Also returns +DI and -DI for directional bias.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_vals = tr.ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr_vals.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr_vals.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx_vals = dx.ewm(alpha=1 / period, min_periods=period).mean()

    return {"adx": adx_vals, "plus_di": plus_di, "minus_di": minus_di, "atr": atr_vals}


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — volatility measure"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


# ────────────────────────────────────────────────────────────────
# Advanced indicator functions for improved scoring
# ────────────────────────────────────────────────────────────────

def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative volume weighted by price direction."""
    sign = np.sign(df["close"].diff())
    sign.iloc[0] = 0
    return (sign * df["volume"]).cumsum()


def fisher(df: pd.DataFrame, period: int = 9) -> Tuple[pd.Series, pd.Series]:
    """Fisher Transform — price mapped to a near-Gaussian, turning points sharpened.

    The cycle screen's lower pane: the fisher line and its 1-bar-ago trigger.
    Crosses of the trigger around the ±2 bands are the reversal reads the
    reference chart circles at cycle bottoms.

    Returns ``(fisher, trigger)`` — both aligned to the input index, warm-up
    bars carried forward from the first computed value so the series never has
    leading NaNs for charting.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    mid = (high + low) / 2.0

    raw = pd.Series(0.0, index=df.index)
    for i in range(len(mid)):
        lo = mid.iloc[max(0, i - period + 1): i + 1].min()
        hi = mid.iloc[max(0, i - period + 1): i + 1].max()
        span = hi - lo
        if span <= 0:
            raw.iloc[i] = 0.0
        else:
            x = 2.0 * ((mid.iloc[i] - lo) / span) - 1.0
            prev = raw.iloc[i - 1] if i > 0 else 0.0
            raw.iloc[i] = max(-0.999, min(0.999, 0.66 * x + 0.67 * prev))

    val = pd.Series(0.0, index=df.index)
    for i in range(len(raw)):
        prev = val.iloc[i - 1] if i > 0 else 0.0
        val.iloc[i] = 0.5 * np.log((1.0 + raw.iloc[i]) / (1.0 - raw.iloc[i])) + 0.5 * prev

    fish = val.copy()
    trigger = fish.shift(1)
    if len(fish):
        trigger.iloc[0] = fish.iloc[0]
    return fish, trigger


def detect_divergence(
    price_series: pd.Series,
    indicator_series: pd.Series,
    lookback: int = 50,
    pivot_left: int = 3,
    pivot_right: int = 2,
) -> Dict[str, float]:
    """
    Detect regular bullish/bearish divergence between price and an oscillator.

    Regular Bullish: Price makes lower low, indicator makes higher low → reversal up.
    Regular Bearish: Price makes higher high, indicator makes lower high → reversal down.

    Returns {"bullish": 0.0-1.0, "bearish": 0.0-1.0}
    """
    n = min(lookback, len(price_series))
    if n < pivot_left + pivot_right + 5:
        return {"bullish": 0.0, "bearish": 0.0}

    prices = price_series.iloc[-n:].values
    indicator = indicator_series.iloc[-n:].values
    result = {"bullish": 0.0, "bearish": 0.0}

    # Find swing lows in price for bullish divergence
    price_lows: list = []
    ind_at_lows: list = []
    for i in range(pivot_left, n - pivot_right):
        window = prices[i - pivot_left: i + pivot_right + 1]
        if prices[i] == window.min() and np.sum(window == prices[i]) == 1:
            if not np.isnan(indicator[i]):
                price_lows.append((i, float(prices[i])))
                ind_at_lows.append((i, float(indicator[i])))

    # Find swing highs in price for bearish divergence
    price_highs: list = []
    ind_at_highs: list = []
    for i in range(pivot_left, n - pivot_right):
        window = prices[i - pivot_left: i + pivot_right + 1]
        if prices[i] == window.max() and np.sum(window == prices[i]) == 1:
            if not np.isnan(indicator[i]):
                price_highs.append((i, float(prices[i])))
                ind_at_highs.append((i, float(indicator[i])))

    # Bullish divergence: price lower low + indicator higher low
    if len(price_lows) >= 2:
        pl1, pl2 = price_lows[-2], price_lows[-1]
        il1, il2 = ind_at_lows[-2], ind_at_lows[-1]
        if pl2[1] < pl1[1] and il2[1] > il1[1] and pl1[1] > 0:
            price_pct = (pl1[1] - pl2[1]) / pl1[1]
            ind_diff = abs(il2[1] - il1[1]) / max(abs(il1[1]), 0.01)
            strength = min(1.0, (price_pct * 30 + ind_diff) * 1.5)
            bars_since = n - pl2[0]
            recency = max(0.3, 1.0 - bars_since / (lookback * 0.6))
            result["bullish"] = min(1.0, strength * recency)

    # Bearish divergence: price higher high + indicator lower high
    if len(price_highs) >= 2:
        ph1, ph2 = price_highs[-2], price_highs[-1]
        ih1, ih2 = ind_at_highs[-2], ind_at_highs[-1]
        if ph2[1] > ph1[1] and ih2[1] < ih1[1] and ph1[1] > 0:
            price_pct = (ph2[1] - ph1[1]) / ph1[1]
            ind_diff = abs(ih1[1] - ih2[1]) / max(abs(ih1[1]), 0.01)
            strength = min(1.0, (price_pct * 30 + ind_diff) * 1.5)
            bars_since = n - ph2[0]
            recency = max(0.3, 1.0 - bars_since / (lookback * 0.6))
            result["bearish"] = min(1.0, strength * recency)

    return result


def trend_structure_score(
    df: pd.DataFrame,
    pivot_left: int = 5,
    pivot_right: int = 3,
    lookback: int = 80,
) -> Dict[str, Any]:
    """
    Market structure analysis via swing point patterns.
    HH+HL = uptrend, LH+LL = downtrend.
    Returns score [-1, 1]: positive = uptrend/bullish, negative = downtrend/bearish.
    """
    n = min(lookback, len(df))
    if n < pivot_left + pivot_right + 5:
        return {"score": 0.0, "structure": "insufficient", "details": ""}

    recent = df.iloc[-n:]
    highs = recent["high"].values
    lows = recent["low"].values

    swing_highs: list = []
    swing_lows: list = []
    for i in range(pivot_left, n - pivot_right):
        h_window = highs[i - pivot_left: i + pivot_right + 1]
        if highs[i] == h_window.max() and np.sum(h_window == highs[i]) == 1:
            swing_highs.append(float(highs[i]))
        l_window = lows[i - pivot_left: i + pivot_right + 1]
        if lows[i] == l_window.min() and np.sum(l_window == lows[i]) == 1:
            swing_lows.append(float(lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"score": 0.0, "structure": "insufficient", "details": ""}

    rh = swing_highs[-min(4, len(swing_highs)):]
    rl = swing_lows[-min(4, len(swing_lows)):]

    hh = sum(1 for i in range(1, len(rh)) if rh[i] > rh[i - 1])
    lh = sum(1 for i in range(1, len(rh)) if rh[i] < rh[i - 1])
    hl = sum(1 for i in range(1, len(rl)) if rl[i] > rl[i - 1])
    ll = sum(1 for i in range(1, len(rl)) if rl[i] < rl[i - 1])

    th = hh + lh
    tl = hl + ll
    h_score = (hh - lh) / th if th > 0 else 0.0
    l_score = (hl - ll) / tl if tl > 0 else 0.0
    score = (h_score + l_score) / 2

    if hh > lh and hl > ll:
        structure = "uptrend"
    elif lh > hh and ll > hl:
        structure = "downtrend"
    else:
        structure = "ranging"

    return {"score": round(score, 4), "structure": structure, "details": f"HH={hh} LH={lh} HL={hl} LL={ll}"}


# ────────────────────────────────────────────────────────────────
# Full analysis
# ────────────────────────────────────────────────────────────────

def analyze(ohlcv: List[List], timeframe: str = "1h") -> Dict[str, Any]:
    """
    Run full technical analysis on OHLCV data (OctoBot-inspired evaluator approach).
    Each evaluator produces a score in [-1, +1]. Final score is weighted average.
    Only strong consensus triggers actionable signals.
    """
    if len(ohlcv) < 60:
        return {"error": "Not enough data", "candles": len(ohlcv)}

    df = ohlcv_to_dataframe(ohlcv)

    # ── Indicators ──────────────────────────────────────────
    df["ma5"] = sma(df["close"], 5)
    df["ma10"] = sma(df["close"], 10)
    df["ma20"] = sma(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["vol_ma5"] = volume_ma(df, 5)
    df["vol_ma10"] = volume_ma(df, 10)
    df["rsi"] = rsi(df)
    df["stoch_rsi"] = stochastic_rsi(df)

    macd_data = macd(df)
    df["macd"] = macd_data["macd"]
    df["macd_signal"] = macd_data["signal"]
    df["macd_hist"] = macd_data["histogram"]

    bb = bollinger_bands(df)
    df["bb_upper"] = bb["upper"]
    df["bb_middle"] = bb["middle"]
    df["bb_lower"] = bb["lower"]
    df["bb_pct_b"] = bb["pct_b"]

    bsv = buy_sell_volume(df)
    df["buy_volume"] = bsv["buy_volume"]
    df["sell_volume"] = bsv["sell_volume"]
    df["buy_ratio"] = bsv["buy_ratio"]

    adx_data = adx(df)
    df["adx"] = adx_data["adx"]
    df["plus_di"] = adx_data["plus_di"]
    df["minus_di"] = adx_data["minus_di"]
    df["atr"] = adx_data["atr"]

    # EMA 200 only if we have enough data
    has_ema200 = len(df) >= 200
    if has_ema200:
        df["ema200"] = ema(df["close"], 200)

    # ── Enhanced evaluator scoring (12 evaluators) ────────────
    # Convention: positive = bullish/buy, negative = bearish/sell
    # New: RSI/MACD divergence, trend structure, OBV, confluence gate,
    #      volatility filter, tighter thresholds for loss reduction.
    # ────────────────────────────────────────────────────────────
    df["obv"] = obv(df)
    ts_data = trend_structure_score(df)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    current_price = latest["close"]
    evaluators: Dict[str, float] = {}
    reasons = []

    # --- 1. Trend Structure (HH/HL/LH/LL market structure) ---
    evaluators["trend_structure"] = ts_data["score"]
    if ts_data["structure"] != "insufficient":
        reasons.append(f"Structure: {ts_data['structure']} ({ts_data['details']})")

    # --- 2. ADX Trend Strength + Direction ---
    adx_val = latest["adx"]
    plus_di_val = latest["plus_di"]
    minus_di_val = latest["minus_di"]
    if not np.isnan(adx_val):
        direction = 1 if plus_di_val > minus_di_val else -1
        if adx_val > 25:
            strength = min(1.0, (adx_val - 25) / 20)
            evaluators["adx"] = direction * strength
            reasons.append(f"ADX {adx_val:.0f} ({'strong' if adx_val > 30 else 'moderate'} trend, {'+DI' if direction > 0 else '-DI'} leading)")
        else:
            evaluators["adx"] = direction * 0.15 * (adx_val / 25)
            reasons.append(f"ADX {adx_val:.0f} (weak/ranging)")

    # --- 3. RSI (proper linear interpolation, fixed mild-zone scaling) ---
    rsi_val = latest["rsi"]
    if not np.isnan(rsi_val):
        if rsi_val >= 80:
            evaluators["rsi"] = -1.0
            reasons.append(f"RSI extremely overbought ({rsi_val:.1f})")
        elif rsi_val >= 70:
            evaluators["rsi"] = -0.5 - 0.5 * (rsi_val - 70) / 10
            reasons.append(f"RSI overbought ({rsi_val:.1f})")
        elif rsi_val >= 55:
            evaluators["rsi"] = -0.5 * (rsi_val - 55) / 15
        elif rsi_val >= 45:
            evaluators["rsi"] = 0.0
        elif rsi_val >= 30:
            evaluators["rsi"] = 0.5 * (45 - rsi_val) / 15
            reasons.append(f"RSI oversold zone ({rsi_val:.1f})")
        elif rsi_val >= 20:
            evaluators["rsi"] = 0.5 + 0.5 * (30 - rsi_val) / 10
            reasons.append(f"RSI oversold ({rsi_val:.1f})")
        else:
            evaluators["rsi"] = 1.0
            reasons.append(f"RSI extremely oversold ({rsi_val:.1f})")

    # --- 4. RSI Divergence (strongest reversal signal) ---
    rsi_div = detect_divergence(df["close"], df["rsi"], lookback=50)
    if rsi_div["bullish"] > 0.25:
        evaluators["rsi_divergence"] = rsi_div["bullish"]
        reasons.append(f"RSI bullish divergence ({rsi_div['bullish']:.2f})")
    elif rsi_div["bearish"] > 0.25:
        evaluators["rsi_divergence"] = -rsi_div["bearish"]
        reasons.append(f"RSI bearish divergence ({rsi_div['bearish']:.2f})")
    else:
        evaluators["rsi_divergence"] = 0.0

    # --- 5. Stochastic RSI (linear normalisation) ---
    stoch_val = latest["stoch_rsi"]
    if not np.isnan(stoch_val):
        evaluators["stoch_rsi"] = -(stoch_val - 50) / 50
        if stoch_val >= 80:
            reasons.append(f"StochRSI overbought ({stoch_val:.0f})")
        elif stoch_val <= 20:
            reasons.append(f"StochRSI oversold ({stoch_val:.0f})")

    # --- 6. MACD Histogram (crossover + magnitude) ---
    hist_val = latest["macd_hist"]
    prev_hist = prev["macd_hist"]
    if not np.isnan(hist_val):
        if hist_val > 0 and prev_hist <= 0:
            evaluators["macd"] = 0.8
            reasons.append("MACD bullish crossover")
        elif hist_val < 0 and prev_hist >= 0:
            evaluators["macd"] = -0.8
            reasons.append("MACD bearish crossover")
        else:
            hist_std = df["macd_hist"].iloc[-20:].std()
            if hist_std > 0:
                evaluators["macd"] = max(-1.0, min(1.0, hist_val / (hist_std * 2)))
            else:
                evaluators["macd"] = 0.0
            if abs(evaluators.get("macd", 0)) > 0.3:
                reasons.append(f"MACD histogram {'positive' if hist_val > 0 else 'negative'} ({hist_val:.6f})")

    # --- 7. MACD Divergence ---
    macd_div = detect_divergence(df["close"], df["macd_hist"], lookback=50)
    if macd_div["bullish"] > 0.25:
        evaluators["macd_divergence"] = macd_div["bullish"]
        reasons.append(f"MACD bullish divergence ({macd_div['bullish']:.2f})")
    elif macd_div["bearish"] > 0.25:
        evaluators["macd_divergence"] = -macd_div["bearish"]
        reasons.append(f"MACD bearish divergence ({macd_div['bearish']:.2f})")
    else:
        evaluators["macd_divergence"] = 0.0

    # --- 8. Bollinger Bands (%B linear, replaces parabolic) ---
    pct_b = latest["bb_pct_b"]
    if not np.isnan(pct_b):
        evaluators["bb"] = max(-1.0, min(1.0, -(pct_b - 0.5) * 2))
        if pct_b > 1.0:
            reasons.append("Price above upper BB (overbought)")
        elif pct_b < 0.0:
            reasons.append("Price below lower BB (oversold)")
        elif pct_b > 0.8:
            reasons.append(f"Price near upper BB (%B={pct_b:.2f})")
        elif pct_b < 0.2:
            reasons.append(f"Price near lower BB (%B={pct_b:.2f})")

    # --- 9. MA Crossovers ---
    if not np.isnan(latest["ma5"]) and not np.isnan(latest["ma10"]):
        if latest["ma5"] > latest["ma10"] and prev["ma5"] <= prev["ma10"]:
            evaluators["ma_cross"] = 0.7
            reasons.append("MA5 crossed above MA10 (bullish)")
        elif latest["ma5"] < latest["ma10"] and prev["ma5"] >= prev["ma10"]:
            evaluators["ma_cross"] = -0.7
            reasons.append("MA5 crossed below MA10 (bearish)")
        elif latest["ma5"] > latest["ma10"]:
            evaluators["ma_cross"] = 0.2
        else:
            evaluators["ma_cross"] = -0.2

    # --- 10. EMA 50/200 Golden/Death Cross ---
    if has_ema200 and not np.isnan(latest["ema50"]) and not np.isnan(latest["ema200"]):
        ema50_val = latest["ema50"]
        ema200_val = latest["ema200"]
        prev_ema50 = prev["ema50"] if "ema50" in prev.index else ema50_val
        if ema50_val > ema200_val and prev_ema50 <= ema200_val:
            evaluators["ema_cross"] = 1.0
            reasons.append("Golden cross (EMA50 > EMA200)")
        elif ema50_val < ema200_val and prev_ema50 >= ema200_val:
            evaluators["ema_cross"] = -1.0
            reasons.append("Death cross (EMA50 < EMA200)")
        elif ema50_val > ema200_val:
            evaluators["ema_cross"] = 0.3
        else:
            evaluators["ema_cross"] = -0.3

    # --- 11. OBV Trend (volume-price confirmation) ---
    obv_val = latest["obv"]
    obv_sma20 = sma(df["obv"], 20).iloc[-1]
    if not np.isnan(obv_sma20) and not np.isnan(latest["ma20"]):
        obv_rising = obv_val > obv_sma20
        price_rising = current_price > latest["ma20"]
        if obv_rising and price_rising:
            evaluators["obv_trend"] = 0.6
            reasons.append("OBV confirms uptrend")
        elif not obv_rising and not price_rising:
            evaluators["obv_trend"] = -0.6
            reasons.append("OBV confirms downtrend")
        elif obv_rising and not price_rising:
            evaluators["obv_trend"] = 0.4
            reasons.append("OBV accumulation (bullish divergence)")
        else:
            evaluators["obv_trend"] = -0.4
            reasons.append("OBV distribution (bearish divergence)")

    # --- 12. Volume Pressure ---
    vol_ratio = latest["volume"] / latest["vol_ma5"] if latest["vol_ma5"] > 0 else 1
    buy_ratio_val = latest["buy_ratio"]
    if vol_ratio > 1.5:
        if buy_ratio_val > 0.6:
            evaluators["volume"] = min(0.7, (vol_ratio - 1) * 0.5)
            reasons.append(f"High buy volume ({vol_ratio:.1f}x avg, {buy_ratio_val:.0%} buy)")
        elif buy_ratio_val < 0.4:
            evaluators["volume"] = -min(0.7, (vol_ratio - 1) * 0.5)
            reasons.append(f"High sell volume ({vol_ratio:.1f}x avg, {1 - buy_ratio_val:.0%} sell)")
        else:
            evaluators["volume"] = 0.0
    else:
        evaluators["volume"] = 0.0

    # --- 13. Zone reaction (supply/demand + channel structure) ---
    # Where price sits relative to the bases it left behind and the rails it
    # has been riding. Fresh demand under price is the strongest long context
    # there is; a fresh supply overhead mirrors it for shorts.
    try:
        from app.signals.zones import analyze_zones, zone_reaction_score

        zones_data = analyze_zones(df)
        z_score, z_reasons = zone_reaction_score(
            current_price, zones_data.get("supply_demand", {}),
            (zones_data.get("channels") or {}).get("active_channel"),
        )
        evaluators["zone_reaction"] = z_score
        reasons.extend(z_reasons)
    except Exception as exc:  # noqa: BLE001 — an enrichment must never break scoring
        logger.debug(f"zone_reaction evaluator skipped: {exc}")
        evaluators["zone_reaction"] = 0.0

    # ── Weighted combination (direct convention: positive=buy) ───
    # Divergence signals get highest weights (most predictive for reversals).
    # Trend structure provides directional foundation.
    weights = {
        "trend_structure": 0.12,
        "rsi_divergence": 0.14,
        "macd_divergence": 0.12,
        "macd": 0.08,
        "rsi": 0.08,
        "bb": 0.07,
        "ma_cross": 0.08,
        "ema_cross": 0.06,
        "obv_trend": 0.08,
        "volume": 0.05,
        "adx": 0.07,
        "stoch_rsi": 0.05,
        "zone_reaction": 0.08,
    }

    weighted_sum = 0.0
    total_weight = 0.0
    for name, val in evaluators.items():
        w = weights.get(name, 0.05)
        weighted_sum += val * w
        total_weight += w

    score = weighted_sum / total_weight if total_weight > 0 else 0.0
    score = max(-1.0, min(1.0, score))

    # ── ADX gate: dampen score in choppy/ranging markets ──
    if not np.isnan(adx_val) and adx_val < 20:
        dampen = adx_val / 20
        score *= dampen
        reasons.append(f"Score dampened by weak ADX ({dampen:.2f}x)")

    # ── Volatility filter: high ATR percentile dampens signals & raises bar ──
    # In volatile expansion (2026-08-28 gold sold 1.5% ATR expansion) the desk
    # must not chase. A mid-range tick becomes a sweep magnet, so we dampen more
    # and demand stronger confluence.
    atr_val = latest["atr"]
    atr_pctile = None
    if not np.isnan(atr_val):
        atr_series = df["atr"].dropna()
        if len(atr_series) >= 50:
            atr_pctile = float((atr_series < atr_val).mean())
            if atr_pctile > 0.90:
                score *= 0.60
                reasons.append(f"Extreme volatility (ATR p{atr_pctile * 100:.0f}) — score heavily dampened, waiting for retest")
            elif atr_pctile > 0.80:
                score *= 0.72
                reasons.append(f"High volatility (ATR p{atr_pctile * 100:.0f}) — score dampened, needs level")
            elif atr_pctile > 0.60:
                # elevated but not extreme — mild dampen only if not at a level
                if abs(evaluators.get("zone_reaction", 0)) < 0.25:
                    score *= 0.88
                    reasons.append(f"Elevated volatility (ATR p{atr_pctile * 100:.0f}) without zone confluence — score dampened")

    # ── Bollinger bandwidth regime — squeeze vs expansion ──
    try:
        if not np.isnan(latest["bb_upper"]) and not np.isnan(latest["bb_lower"]) and not np.isnan(latest["bb_middle"]) and latest["bb_middle"] != 0:
            bw = (latest["bb_upper"] - latest["bb_lower"]) / abs(latest["bb_middle"])
            bw_series = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"].abs()
            bw_series = bw_series.replace([np.inf, -np.inf], np.nan).dropna()
            if len(bw_series) >= 50 and not np.isnan(bw):
                bw_pctile = float((bw_series < bw).mean())
                if bw_pctile > 0.85:
                    # Wide bands = expansion — breakout chase risk
                    if abs(score) < 0.55:
                        score *= 0.85
                        reasons.append(f"Bollinger expansion (bandwidth p{bw_pctile*100:.0f}) — chasing needs stronger score")
                elif bw_pctile < 0.15:
                    # Squeeze — coil before move, not a trigger
                    if abs(latest["bb_pct_b"] - 0.5) < 0.25:
                        score *= 0.70
                        reasons.append(f"Bollinger squeeze (bandwidth p{bw_pctile*100:.0f}) mid-band — no edge, waiting for break + retest")
    except Exception:
        pass

    # ── Confluence gate: require multiple evaluators to strongly agree ──
    # Tightened in volatile regime: when ATR is hot, a 4-evaluator lean is noise.
    # 2026-08-28 had 4/7 bullish at composite 0.28 but ADX/MACD/volume all bearish.
    strong_bullish = sum(1 for v in evaluators.values() if v > 0.3)
    strong_bearish = sum(1 for v in evaluators.values() if v < -0.3)
    min_confluence = 4
    if atr_pctile is not None and atr_pctile > 0.80:
        min_confluence = 5  # volatile — need 5 strong evaluators, not 4
        if atr_pctile > 0.90:
            min_confluence = 6
    if score > 0 and strong_bullish < min_confluence:
        # In volatile mode, weak confluence is near-fatal; in calm, it's a haircut
        floor = 0.35 if (atr_pctile is not None and atr_pctile > 0.80) else 0.5
        score *= max(floor, strong_bullish / min_confluence)
        if strong_bullish < 3:
            reasons.append(f"Weak confluence ({strong_bullish} bullish evaluators, need {min_confluence} in this regime)")
        else:
            reasons.append(f"Confluence {strong_bullish}/{min_confluence} bullish — needs more alignment")
    elif score < 0 and strong_bearish < min_confluence:
        floor = 0.35 if (atr_pctile is not None and atr_pctile > 0.80) else 0.5
        score *= max(floor, strong_bearish / min_confluence)
        if strong_bearish < 3:
            reasons.append(f"Weak confluence ({strong_bearish} bearish evaluators, need {min_confluence} in this regime)")
        else:
            reasons.append(f"Confluence {strong_bearish}/{min_confluence} bearish — needs more alignment")

    # ── Level check for volatile mid-range — don't trade air ──
    # If no zone_reaction and BB mid-range, the score may look tradeable but
    # there is no structure to trade *from*. In expansion this is the exact
    # shape that wicked the 2026-08-28 longs.
    try:
        zone = evaluators.get("zone_reaction", 0)
        bb_mid = 0.28 <= float(latest["bb_pct_b"]) <= 0.72 if not np.isnan(latest["bb_pct_b"]) else False
        if abs(zone) < 0.20 and bb_mid and (atr_pctile is None or atr_pctile > 0.55):
            if abs(score) < 0.50:
                score *= 0.60
                reasons.append(f"No level confluence mid-range (%B {float(latest['bb_pct_b']):.2f}, zone {zone:.2f}) — waiting for level")
    except Exception:
        pass

    score = max(-1.0, min(1.0, score))

    # ── Action thresholds — dynamic with volatility ──
    # Calm market: 0.40 is enough (needs decent conviction).
    # Volatile (ATR p80+): 0.45, extreme (>0.90): 0.50 — must earn it.
    # This is the "wait for the right moment" dial: in chop we hold more.
    effective_threshold = 0.40
    if atr_pctile is not None and atr_pctile > 0.90:
        effective_threshold = 0.50
    elif atr_pctile is not None and atr_pctile > 0.80:
        effective_threshold = 0.45
    elif atr_pctile is not None and atr_pctile > 0.60 and abs(evaluators.get("zone_reaction", 0)) < 0.20:
        effective_threshold = 0.43  # elevated vol without level → higher bar

    if score >= effective_threshold:
        action = "buy"
    elif score <= -effective_threshold:
        action = "sell"
    else:
        action = "hold"
        if abs(score) >= 0.30 and abs(score) < effective_threshold:
            reasons.append(f"Score {score:.2f} below volatile threshold {effective_threshold:.2f} — waiting for better entry")

    # ── Confidence: score magnitude + agreement + confluence ──
    total_evals = len(evaluators)
    agreeing = sum(1 for v in evaluators.values() if (v > 0 and score > 0) or (v < 0 and score < 0))
    agreement_ratio = agreeing / total_evals if total_evals > 0 else 0
    score_component = min(1.0, abs(score) / 0.5) * 0.40
    agreement_component = agreement_ratio * 0.35
    confluence_count = max(strong_bullish, strong_bearish)
    confluence_component = min(1.0, confluence_count / 5) * 0.25
    confidence = min(1.0, score_component + agreement_component + confluence_component)
    strength = (score + 1) / 2

    # ── Build result ────────────────────────────────────────
    def safe(v):
        return None if (isinstance(v, float) and np.isnan(v)) else round(float(v), 6)

    indicators = {
        "price": safe(current_price),
        "ma5": safe(latest["ma5"]),
        "ma10": safe(latest["ma10"]),
        "ma20": safe(latest["ma20"]),
        "ema50": safe(latest["ema50"]),
        "rsi": safe(latest["rsi"]),
        "stoch_rsi": safe(latest["stoch_rsi"]),
        "macd": safe(latest["macd"]),
        "macd_signal": safe(latest["macd_signal"]),
        "macd_histogram": safe(latest["macd_hist"]),
        "bb_upper": safe(latest["bb_upper"]),
        "bb_middle": safe(latest["bb_middle"]),
        "bb_lower": safe(latest["bb_lower"]),
        "bb_pct_b": safe(latest["bb_pct_b"]),
        "adx": safe(latest["adx"]),
        "plus_di": safe(latest["plus_di"]),
        "minus_di": safe(latest["minus_di"]),
        "atr": safe(latest["atr"]),
        "volume": safe(latest["volume"]),
        "vol_ma5": safe(latest["vol_ma5"]),
        "vol_ma10": safe(latest["vol_ma10"]),
        "buy_volume": safe(latest["buy_volume"]),
        "sell_volume": safe(latest["sell_volume"]),
        "buy_ratio": safe(latest["buy_ratio"]),
        "volume_ratio": safe(vol_ratio),
    }

    if has_ema200:
        indicators["ema200"] = safe(latest["ema200"])

    # Auto Fib Retracement — reported alongside the indicators so every consumer
    # of analyze() (agents, Jarvis, research) sees the golden zone without
    # recomputing it. Reported only, never folded into `score`: the pipeline
    # already applies fib confluence as its own conditional weight, and doubling
    # it here would count the same evidence twice.
    fib: Optional[Dict[str, Any]] = None
    try:
        fib_result = auto_fib_retracement(df, levels=(0.5, 0.618), extend_lines=False)
        swing = fib_result.get("swing")
        if swing:
            conf = fib_confluence_score(current_price, fib_result, swing["direction"])
            zone = fib_result.get("golden_zone")
            fib = {
                "direction": swing["direction"],
                "swing": swing,
                "golden_zone": zone,
                "confidence": round(conf, 4) if conf is not None else None,
                "in_golden_zone": bool(zone and zone["low"] <= current_price <= zone["high"]),
            }
    except Exception as e:  # noqa: BLE001 - one indicator must not fail the analysis
        logger.warning(f"Auto fib retracement failed on {timeframe}: {e}")

    return {
        "action": action,
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "strength": round(strength, 4),
        "reasons": reasons,
        "indicators": indicators,
        "evaluators": {k: round(v, 4) for k, v in evaluators.items()},
        "fib": fib,
        "timeframe": timeframe,
        "candles_analyzed": len(df),
    }
