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
from typing import Dict, List, Any, Optional
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
    """
    result = pd.Series(np.nan, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] == window.max() and (window == series.iloc[i]).sum() == 1:
            result.iloc[i] = series.iloc[i]
    return result


def pivot_lows(series: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """
    Detect pivot lows (swing lows). A bar is a pivot low if it's the lowest
    of `left` bars before and `right` bars after. Returns NaN where no pivot.
    Mirrors TradingView's ta.pivotlow(source, leftbars, rightbars).
    """
    result = pd.Series(np.nan, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] == window.min() and (window == series.iloc[i]).sum() == 1:
            result.iloc[i] = series.iloc[i]
    return result


def support_resistance_mtf(
    df: pd.DataFrame,
    pivot_left: int = 5,
    pivot_right: int = 5,
    zone_merge_pct: float = 0.5,
    max_levels: int = 8,
    min_touches: int = 2,
) -> Dict[str, Any]:
    """
    Support and Resistance Signals MTF — replicates the TradingView indicator logic.

    Algorithm:
    1. Detect pivot highs and pivot lows using left/right bar lookback.
    2. Cluster nearby pivots into S/R zones (merge within zone_merge_pct %).
    3. Score zones by number of touches (more touches = stronger level).
    4. Generate signals: BUY when price bounces off support, SELL at resistance.
    5. Return level lines + signal markers for chart overlay.

    Returns:
      - levels: list of {price, type, strength, touches, first_time, last_time}
      - signals: list of {time, type, price, level_price}
      - support_lines: series data for chart
      - resistance_lines: series data for chart
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

    for level in levels:
        price = level["price"]
        start_time = level["first_time"]
        end_time = int(timestamps.iloc[-1].timestamp())

        line_data = []
        for i in range(len(df)):
            ts = int(timestamps.iloc[i].timestamp())
            if ts >= start_time:
                line_data.append({"time": ts, "value": price})

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

    # ── Volatility filter: high ATR percentile dampens signals ──
    atr_val = latest["atr"]
    if not np.isnan(atr_val):
        atr_series = df["atr"].dropna()
        if len(atr_series) >= 50:
            atr_pctile = (atr_series < atr_val).mean()
            if atr_pctile > 0.85:
                score *= 0.80
                reasons.append(f"High volatility (ATR p{atr_pctile * 100:.0f}) — score dampened")

    # ── Confluence gate: require multiple evaluators to strongly agree ──
    strong_bullish = sum(1 for v in evaluators.values() if v > 0.3)
    strong_bearish = sum(1 for v in evaluators.values() if v < -0.3)
    min_confluence = 4
    if score > 0 and strong_bullish < min_confluence:
        score *= max(0.5, strong_bullish / min_confluence)
        if strong_bullish < 3:
            reasons.append(f"Weak confluence ({strong_bullish} bullish evaluators)")
    elif score < 0 and strong_bearish < min_confluence:
        score *= max(0.5, strong_bearish / min_confluence)
        if strong_bearish < 3:
            reasons.append(f"Weak confluence ({strong_bearish} bearish evaluators)")

    score = max(-1.0, min(1.0, score))

    # ── Action thresholds (tighter than before for loss reduction) ──
    if score >= 0.40:
        action = "buy"
    elif score <= -0.40:
        action = "sell"
    else:
        action = "hold"

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

    return {
        "action": action,
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "strength": round(strength, 4),
        "reasons": reasons,
        "indicators": indicators,
        "evaluators": {k: round(v, 4) for k, v in evaluators.items()},
        "timeframe": timeframe,
        "candles_analyzed": len(df),
    }
