"""Chart pattern detectors — continuation, reversal, bilateral.

Implements the patterns from Images 2,3,4,7,8 (Trade Vision, TMGM, Changelly):
  Reversal: Double Top/Bottom, Triple Top/Bottom, Head & Shoulders / Inverse,
            Rising/Falling Wedge, Expanding Triangle
  Continuation: Flag (bull/bear), Pennant, Rectangle, Falling/Rising Wedge (as continuation)
  Bilateral: Ascending, Descending, Symmetrical Triangles, Expanding

Philosophy matches technical.detect_triangle: be narrow. An unnamed pattern is
far better than a named one that isn't there. Each detector returns None when
criteria are not cleanly met, and a scored dict when they are — volume
confirmation and measurement targets included where applicable.

All functions take pd.DataFrame shaped by technical.ohlcv_to_dataframe and
return JSON-safe dicts. A single `analyze_chart_patterns(df)` aggregates them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from app.signals.technical import atr as calc_atr, pivot_highs, pivot_lows, zigzag_pivots

# ── helpers ────────────────────────────────────────────────────────────────


def _atr(df: pd.DataFrame) -> float:
    try:
        v = float(calc_atr(df, 14).iloc[-1])
        if np.isfinite(v) and v > 0:
            return v
    except Exception:
        pass
    try:
        return float((df["high"] - df["low"]).tail(14).mean() or 0.0)
    except Exception:
        return 0.0


def _swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    ph = pivot_highs(df["high"], left, right)
    pl = pivot_lows(df["low"], left, right)
    highs: List[Tuple[int, float]] = [(int(i), float(ph.iloc[i])) for i in range(len(df)) if not np.isnan(ph.iloc[i])]
    lows: List[Tuple[int, float]] = [(int(i), float(pl.iloc[i])) for i in range(len(df)) if not np.isnan(pl.iloc[i])]
    return highs, lows


def _is_flat(values: List[float], tol_pct: float = 0.6) -> bool:
    if not values:
        return False
    m = sum(values) / len(values)
    if m == 0:
        return False
    return all(abs(v - m) / abs(m) * 100 <= tol_pct for v in values)


def _fit(xs: List[float], ys: List[float]) -> Tuple[float, float, float]:
    if len(xs) < 2:
        return 0.0, 0.0, 0.0
    m, b = np.polyfit(np.array(xs, float), np.array(ys, float), 1)
    pred = m * np.array(xs, float) + b
    ss_res = float(np.sum((np.array(ys, float) - pred) ** 2))
    ss_tot = float(np.sum((np.array(ys, float) - np.mean(ys)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(m), float(b), float(r2)


def _volume_confirm(df: pd.DataFrame, breakout_idx: int, lookback: int = 20) -> bool:
    try:
        vols = df["volume"].to_numpy()
        if breakout_idx < 1 or breakout_idx >= len(vols):
            return False
        avg = float(np.mean(vols[max(0, breakout_idx - lookback):breakout_idx]))
        return float(vols[breakout_idx]) > avg * 1.15 if avg > 0 else False
    except Exception:
        return False


# ── Double Top / Bottom ────────────────────────────────────────────────────


def detect_double_top(df: pd.DataFrame, tol_pct: float = 0.6, min_bars: int = 8, max_bars: int = 45) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(highs) < 2 or len(lows) < 1:
        return None
    # last two highs as candidate tops
    h1_idx, h1_price = highs[-2]
    h2_idx, h2_price = highs[-1]
    bar_gap = h2_idx - h1_idx
    if not (min_bars <= bar_gap <= max_bars):
        return None
    # highs must be nearly equal
    avg = (h1_price + h2_price) / 2
    if abs(h1_price - h2_price) / avg * 100 > tol_pct:
        return None
    # need an interim low (neckline) between them
    interim_lows = [p for p in lows if h1_idx < p[0] < h2_idx]
    if not interim_lows:
        return None
    neckline = min(p[1] for p in interim_lows)
    neck_idx = min(p[0] for p in interim_lows if p[1] == neckline)
    # recent close should be near neckline or broken
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close < neckline - 0.15 * atr if atr else close < neckline
    height = avg - neckline
    if height <= 0:
        return None
    target = neckline - height
    # confidence: symmetry + depth + gap
    sym = 1.0 - min(1.0, abs(h1_price - h2_price) / avg * 100 / tol_pct)
    depth_norm = min(1.0, height / (avg * 0.08)) if avg else 0
    confidence = round(min(0.95, 0.45 + sym * 0.30 + depth_norm * 0.25), 3)
    return {
        "kind": "double_top",
        "category": "reversal",
        "signal": "bearish",
        "neckline": round(neckline, 6),
        "neckline_idx": int(neck_idx),
        "peaks": [{"idx": h1_idx, "price": round(h1_price, 6)}, {"idx": h2_idx, "price": round(h2_price, 6)}],
        "target": round(target, 6),
        "invalidation": round(avg + (atr * 0.35 if atr else avg * 0.01), 6),
        "broken": bool(broken),
        "breakout": "down" if broken else None,
        "height": round(height, 6),
        "confidence": confidence,
        "volume_confirm": _volume_confirm(df, len(df) - 1) if broken else False,
    }


def detect_double_bottom(df: pd.DataFrame, tol_pct: float = 0.6, min_bars: int = 8, max_bars: int = 45) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(lows) < 2 or len(highs) < 1:
        return None
    l1_idx, l1_price = lows[-2]
    l2_idx, l2_price = lows[-1]
    bar_gap = l2_idx - l1_idx
    if not (min_bars <= bar_gap <= max_bars):
        return None
    avg = (l1_price + l2_price) / 2
    if abs(l1_price - l2_price) / abs(avg) * 100 > tol_pct:
        return None
    interim_highs = [p for p in highs if l1_idx < p[0] < l2_idx]
    if not interim_highs:
        return None
    neckline = max(p[1] for p in interim_highs)
    neck_idx = max(p[0] for p in interim_highs if p[1] == neckline)
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close > neckline + 0.15 * atr if atr else close > neckline
    height = neckline - avg
    if height <= 0:
        return None
    target = neckline + height
    sym = 1.0 - min(1.0, abs(l1_price - l2_price) / abs(avg) * 100 / tol_pct)
    depth_norm = min(1.0, height / (abs(avg) * 0.08)) if avg else 0
    confidence = round(min(0.95, 0.45 + sym * 0.30 + depth_norm * 0.25), 3)
    return {
        "kind": "double_bottom",
        "category": "reversal",
        "signal": "bullish",
        "neckline": round(neckline, 6),
        "neckline_idx": int(neck_idx),
        "troughs": [{"idx": l1_idx, "price": round(l1_price, 6)}, {"idx": l2_idx, "price": round(l2_price, 6)}],
        "target": round(target, 6),
        "invalidation": round(avg - (atr * 0.35 if atr else abs(avg) * 0.01), 6),
        "broken": bool(broken),
        "breakout": "up" if broken else None,
        "height": round(height, 6),
        "confidence": confidence,
        "volume_confirm": _volume_confirm(df, len(df) - 1) if broken else False,
    }


# ── Triple Top / Bottom ────────────────────────────────────────────────────


def detect_triple_top(df: pd.DataFrame, tol_pct: float = 0.65, min_gap: int = 8, max_gap: int = 40) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(highs) < 3:
        return None
    h = highs[-3:]
    # each gap in range
    if not (min_gap <= h[1][0] - h[0][0] <= max_gap and min_gap <= h[2][0] - h[1][0] <= max_gap):
        return None
    prices = [p[1] for p in h]
    if not _is_flat(prices, tol_pct):
        return None
    neckline = min((p[1] for p in lows if h[0][0] < p[0] < h[-1][0]), default=None)
    if neckline is None:
        return None
    avg = sum(prices) / 3
    height = avg - neckline
    if height <= 0:
        return None
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close < neckline - 0.15 * atr if atr else close < neckline
    target = neckline - height
    confidence = round(min(0.96, 0.55 + (0.20 if broken else 0) + min(0.20, height / (avg * 0.06))), 3)
    return {
        "kind": "triple_top",
        "category": "reversal",
        "signal": "bearish",
        "neckline": round(neckline, 6),
        "peaks": [{"idx": p[0], "price": round(p[1], 6)} for p in h],
        "target": round(target, 6),
        "broken": bool(broken),
        "breakout": "down" if broken else None,
        "confidence": confidence,
    }


def detect_triple_bottom(df: pd.DataFrame, tol_pct: float = 0.65, min_gap: int = 8, max_gap: int = 40) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(lows) < 3:
        return None
    l = lows[-3:]
    if not (min_gap <= l[1][0] - l[0][0] <= max_gap and min_gap <= l[2][0] - l[1][0] <= max_gap):
        return None
    prices = [p[1] for p in l]
    if not _is_flat(prices, tol_pct):
        return None
    neckline = max((p[1] for p in highs if l[0][0] < p[0] < l[-1][0]), default=None)
    if neckline is None:
        return None
    avg = sum(prices) / 3
    height = neckline - avg
    if height <= 0:
        return None
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close > neckline + 0.15 * atr if atr else close > neckline
    target = neckline + height
    confidence = round(min(0.96, 0.55 + (0.20 if broken else 0) + min(0.20, height / (abs(avg) * 0.06) if avg else 0)), 3)
    return {
        "kind": "triple_bottom",
        "category": "reversal",
        "signal": "bullish",
        "neckline": round(neckline, 6),
        "troughs": [{"idx": p[0], "price": round(p[1], 6)} for p in l],
        "target": round(target, 6),
        "broken": bool(broken),
        "breakout": "up" if broken else None,
        "confidence": confidence,
    }


# ── Head & Shoulders / Inverse ─────────────────────────────────────────────


def detect_head_and_shoulders(df: pd.DataFrame, tol_pct: float = 1.2) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(highs) < 3 or len(lows) < 2:
        return None
    left, head, right = highs[-3], highs[-2], highs[-1]
    # head must be notably higher than both shoulders
    if not (head[1] > left[1] * 1.02 and head[1] > right[1] * 1.02):
        return None
    # shoulders roughly equal
    if abs(left[1] - right[1]) / ((left[1] + right[1]) / 2) * 100 > tol_pct * 1.6:
        return None
    # neckline from interim lows
    left_lows = [p for p in lows if left[0] < p[0] < head[0]]
    right_lows = [p for p in lows if head[0] < p[0] < right[0]]
    if not left_lows or not right_lows:
        return None
    neck_l = min(left_lows, key=lambda x: x[1])
    neck_r = min(right_lows, key=lambda x: x[1])
    # neckline slope: allow up to 8% divergence
    neckline_mid = (neck_l[1] + neck_r[1]) / 2
    # slope check
    neckline_slope = (neck_r[1] - neck_l[1]) / max(1, neck_r[0] - neck_l[0])
    # confidence down-weights steep neckline
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close < neckline_mid - 0.12 * atr if atr else close < neckline_mid
    height = head[1] - neckline_mid
    if height <= 0:
        return None
    target = neckline_mid - height
    # score
    head_prom = (head[1] - max(left[1], right[1])) / head[1]
    sym = 1.0 - min(1.0, abs(left[1] - right[1]) / ((left[1] + right[1]) / 2) * 100 / (tol_pct * 1.6))
    confidence = round(min(0.96, 0.42 + head_prom * 1.2 + sym * 0.18 + (0.18 if broken else 0)), 3)
    return {
        "kind": "head_and_shoulders",
        "category": "reversal",
        "signal": "bearish",
        "neckline": round(neckline_mid, 6),
        "neckline_points": [{"idx": neck_l[0], "price": round(neck_l[1], 6)}, {"idx": neck_r[0], "price": round(neck_r[1], 6)}],
        "peaks": [{"idx": left[0], "price": round(left[1], 6)}, {"idx": head[0], "price": round(head[1], 6)}, {"idx": right[0], "price": round(right[1], 6)}],
        "slope": round(neckline_slope, 6),
        "target": round(target, 6),
        "invalidation": round(head[1] + (atr * 0.25 if atr else head[1] * 0.005), 6),
        "broken": bool(broken),
        "breakout": "down" if broken else None,
        "height": round(height, 6),
        "confidence": confidence,
    }


def detect_inverse_head_and_shoulders(df: pd.DataFrame, tol_pct: float = 1.2) -> Optional[Dict[str, Any]]:
    highs, lows = _swings(df, 3, 3)
    if len(lows) < 3 or len(highs) < 2:
        return None
    left, head, right = lows[-3], lows[-2], lows[-1]
    if not (head[1] < left[1] * 0.98 and head[1] < right[1] * 0.98):
        return None
    if abs(left[1] - right[1]) / abs((left[1] + right[1]) / 2) * 100 > tol_pct * 1.6:
        return None
    left_highs = [p for p in highs if left[0] < p[0] < head[0]]
    right_highs = [p for p in highs if head[0] < p[0] < right[0]]
    if not left_highs or not right_highs:
        return None
    neck_l = max(left_highs, key=lambda x: x[1])
    neck_r = max(right_highs, key=lambda x: x[1])
    neckline_mid = (neck_l[1] + neck_r[1]) / 2
    slope = (neck_r[1] - neck_l[1]) / max(1, neck_r[0] - neck_l[0])
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    broken = close > neckline_mid + 0.12 * atr if atr else close > neckline_mid
    height = neckline_mid - head[1]
    if height <= 0:
        return None
    target = neckline_mid + height
    head_prom = (min(left[1], right[1]) - head[1]) / abs(head[1]) if head[1] != 0 else 0
    sym = 1.0 - min(1.0, abs(left[1] - right[1]) / abs((left[1] + right[1]) / 2) * 100 / (tol_pct * 1.6))
    confidence = round(min(0.96, 0.42 + head_prom * 1.2 + sym * 0.18 + (0.18 if broken else 0)), 3)
    return {
        "kind": "inverse_head_and_shoulders",
        "category": "reversal",
        "signal": "bullish",
        "neckline": round(neckline_mid, 6),
        "neckline_points": [{"idx": neck_l[0], "price": round(neck_l[1], 6)}, {"idx": neck_r[0], "price": round(neck_r[1], 6)}],
        "troughs": [{"idx": left[0], "price": round(left[1], 6)}, {"idx": head[0], "price": round(head[1], 6)}, {"idx": right[0], "price": round(right[1], 6)}],
        "slope": round(slope, 6),
        "target": round(target, 6),
        "invalidation": round(head[1] - (atr * 0.25 if atr else abs(head[1]) * 0.005), 6),
        "broken": bool(broken),
        "breakout": "up" if broken else None,
        "height": round(height, 6),
        "confidence": confidence,
    }


# ── Wedges ───────────────────────────────────────────────────────────────────


def detect_wedge(df: pd.DataFrame, max_swings: int = 6) -> Optional[Dict[str, Any]]:
    """Detect rising/falling wedge from ZigZag highs/lows."""
    if len(df) < 50:
        return None
    try:
        zz = zigzag_pivots(df).get("pivots") or []
    except Exception:
        return None
    hi = [(int(p["idx"]), float(p["price"])) for p in zz if p["type"] == "high"][-max_swings:]
    lo = [(int(p["idx"]), float(p["price"])) for p in zz if p["type"] == "low"][-max_swings:]
    if len(hi) < 2 or len(lo) < 2:
        return None
    hx, hy = [p[0] for p in hi], [p[1] for p in hi]
    lx, ly = [p[0] for p in lo], [p[1] for p in lo]
    hm, hb, hr2 = _fit(hx, hy)
    lm, lb, lr2 = _fit(lx, ly)
    if hr2 < 0.55 or lr2 < 0.55:
        return None
    # both slopes same sign for wedge
    n = len(df) - 1
    upper_now = hm * n + hb
    lower_now = lm * n + lb
    width = abs(upper_now - lower_now)
    if width <= 0:
        return None
    close = float(df["close"].iloc[-1])
    atr = _atr(df)
    # rising wedge: both slopes up, lower steeper (converging)
    # falling wedge: both slopes down, upper steeper (converging)
    kind = None
    signal = None
    breakout = None
    if hm > 0 and lm > 0 and lm > hm:
        kind = "rising_wedge"
        signal = "bearish"  # rising wedge → bearish reversal/breakdown
        if atr and close < lower_now - 0.20 * atr:
            breakout = "down"
        elif not atr and close < lower_now:
            breakout = "down"
    elif hm < 0 and lm < 0 and hm > lm:
        kind = "falling_wedge"
        signal = "bullish"
        if atr and close > upper_now + 0.20 * atr:
            breakout = "up"
        elif not atr and close > upper_now:
            breakout = "up"
    else:
        return None
    # contracting check: width narrowing — slopes converging
    converging = (hm - lm < 0) if kind == "rising_wedge" else (lm - hm > 0)
    if not converging:
        return None
    confidence = round(min(0.92, 0.42 + min(hr2, lr2) * 0.35 + (0.18 if breakout else 0)), 3)
    return {
        "kind": kind,
        "category": "reversal",
        "signal": signal,
        "upper": {"m": round(hm, 8), "b": round(hb, 6), "r2": round(hr2, 3)},
        "lower": {"m": round(lm, 8), "b": round(lb, 6), "r2": round(lr2, 3)},
        "upper_now": round(upper_now, 6),
        "lower_now": round(lower_now, 6),
        "breakout": breakout,
        "broken": breakout is not None,
        "confidence": confidence,
    }


# ── Flags & Pennants ─────────────────────────────────────────────────────────


def detect_flag(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Bull/bear flag: strong pole (>=2.5 ATR) then tight flag channel (2-12 bars)."""
    if len(df) < 40:
        return None
    atr = _atr(df)
    if not atr:
        return None
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    # find pole: max consecutive run in last 30 bars with net move >=2.5 ATR
    best = None
    for start in range(max(0, n - 35), n - 8):
        for end in range(start + 4, min(n - 5, start + 15)):
            move = abs(closes[end] - closes[start])
            if move < 2.5 * atr:
                continue
            is_up = closes[end] > closes[start]
            # flag must be opposite or sideways after pole
            flag_start = end + 1
            flag_end = min(n - 1, flag_start + 12)
            if flag_end - flag_start < 3:
                continue
            # flag channel should be tight and slight counter-trend
            seg_h = highs[flag_start:flag_end + 1]
            seg_l = lows[flag_start:flag_end + 1]
            seg_range = float(np.max(seg_h) - np.min(seg_l))
            pole = move
            if seg_range > pole * 0.45:
                continue  # flag too wide
            # slope of flag: should be slightly negative for bull flag, slightly positive for bear
            xs = list(range(flag_start, flag_end + 1))
            m, _, r2 = _fit(xs, list(closes[flag_start:flag_end + 1]))
            slope_ok = (is_up and m < 0.10 * atr) or (not is_up and m > -0.10 * atr)
            if not slope_ok:
                # still allow near-horizontal flag
                if abs(m) > 0.30 * atr:
                    continue
            # prefer most recent and strongest pole
            score = move / atr - seg_range / pole
            if best is None or score > best["score"]:
                close_now = float(closes[-1])
                # breakout detection
                flag_high = float(np.max(seg_h))
                flag_low = float(np.min(seg_l))
                breakout = None
                if is_up and close_now > flag_high + 0.10 * atr:
                    breakout = "up"
                elif not is_up and close_now < flag_low - 0.10 * atr:
                    breakout = "down"
                best = {
                    "is_up": is_up,
                    "pole": float(pole),
                    "score": score,
                    "flag_start": int(flag_start),
                    "flag_end": int(flag_end),
                    "flag_high": flag_high,
                    "flag_low": flag_low,
                    "breakout": breakout,
                    "r2": float(r2),
                }
    if not best:
        return None
    signal = "bullish" if best["is_up"] else "bearish"
    category = "continuation"
    kind = "bull_flag" if best["is_up"] else "bear_flag"
    # continuation target: pole height projected from breakout point
    flag_mid = (best["flag_high"] + best["flag_low"]) / 2
    if best["is_up"]:
        target = flag_mid + best["pole"] * 0.85
        inval = best["flag_low"] - atr * 0.35
    else:
        target = flag_mid - best["pole"] * 0.85
        inval = best["flag_high"] + atr * 0.35
    confidence = round(min(0.90, 0.38 + (best["pole"] / atr) * 0.04 + (0.18 if best["breakout"] else 0) + min(0.15, best["r2"] * 0.12)), 3)
    return {
        "kind": kind,
        "category": category,
        "signal": signal,
        "pole": round(best["pole"], 6),
        "flag_start": best["flag_start"],
        "flag_end": best["flag_end"],
        "flag_high": round(best["flag_high"], 6),
        "flag_low": round(best["flag_low"], 6),
        "breakout": best["breakout"],
        "broken": best["breakout"] is not None,
        "target": round(target, 6),
        "invalidation": round(inval, 6),
        "confidence": confidence,
    }


def detect_pennant(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Pennant: pole + triangular consolidation (contracting)."""
    if len(df) < 40:
        return None
    atr = _atr(df)
    if not atr:
        return None
    flag = detect_flag(df)
    # pennant is like flag but triangular not channel — check contracting
    # Re-run with triangle detection on flag window if flag found
    if flag and flag["kind"] in ("bull_flag", "bear_flag"):
        start, end = flag["flag_start"], flag["flag_end"]
        sub = df.iloc[start:end + 1]
        if len(sub) < 5:
            return flag
        tri = detect_triangle(sub)
        if tri and tri.get("kind") in ("symmetrical", "ascending", "descending") and not tri.get("broken"):
            flag["kind"] = "bull_pennant" if flag["kind"] == "bull_flag" else "bear_pennant"
            flag["confidence"] = round(min(0.92, flag["confidence"] + 0.07), 3)
        return flag
    # also try direct pennant without flag pre-detect — look for pole + small contracting range
    closes = df["close"].to_numpy()
    n = len(df)
    for start in range(max(0, n - 30), n - 10):
        for end in range(start + 4, min(n - 5, start + 12)):
            move = abs(closes[end] - closes[start])
            if move < 2.2 * atr:
                continue
            is_up = closes[end] > closes[start]
            flag_start = end + 1
            flag_end = min(n - 1, flag_start + 10)
            sub = df.iloc[flag_start:flag_end + 1]
            if len(sub) < 5:
                continue
            tri = detect_triangle(sub)
            if tri and tri.get("kind") in ("symmetrical",):
                flag_high = float(df["high"].iloc[flag_start:flag_end + 1].max())
                flag_low = float(df["low"].iloc[flag_start:flag_end + 1].min())
                close_now = float(closes[-1])
                breakout = None
                if is_up and close_now > flag_high + 0.10 * atr:
                    breakout = "up"
                elif not is_up and close_now < flag_low - 0.10 * atr:
                    breakout = "down"
                pole = float(move)
                flag_mid = (flag_high + flag_low) / 2
                target = flag_mid + pole * 0.80 if is_up else flag_mid - pole * 0.80
                return {
                    "kind": "bull_pennant" if is_up else "bear_pennant",
                    "category": "continuation",
                    "signal": "bullish" if is_up else "bearish",
                    "pole": round(pole, 6),
                    "flag_start": int(flag_start),
                    "flag_end": int(flag_end),
                    "flag_high": round(flag_high, 6),
                    "flag_low": round(flag_low, 6),
                    "breakout": breakout,
                    "broken": breakout is not None,
                    "target": round(target, 6),
                    "confidence": round(0.55 + (0.15 if breakout else 0), 3),
                }
    return None


# ── Rectangle ────────────────────────────────────────────────────────────────


def detect_rectangle(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Horizontal range rectangle: flat highs and flat lows, breakouts either way (bilateral)."""
    if len(df) < 50:
        return None
    # use pivot clustering to find flat
    highs, lows = _swings(df, 4, 4)
    if len(highs) < 3 or len(lows) < 3:
        return None
    h_prices = [p[1] for p in highs[-4:]]
    l_prices = [p[1] for p in lows[-4:]]
    if not (_is_flat(h_prices, 0.65) and _is_flat(l_prices, 0.65)):
        return None
    top = sum(h_prices) / len(h_prices)
    bottom = sum(l_prices) / len(l_prices)
    height = top - bottom
    if height <= 0 or top <= 0 or bottom <= 0:
        return None
    # rectangle width vs height sanity
    # use ATR for scale
    atr = _atr(df)
    if atr and height < atr * 0.6:
        return None
    close = float(df["close"].iloc[-1])
    broken_up = close > top + (0.15 * atr if atr else top * 0.002)
    broken_down = close < bottom - (0.15 * atr if atr else bottom * 0.002)
    breakout = "up" if broken_up else "down" if broken_down else None
    # signal direction follows breakout; otherwise bilateral
    if broken_up:
        signal = "bullish"
        category = "continuation"  # if trend before was up — we treat unilateral on break
        target = top + height
    elif broken_down:
        signal = "bearish"
        category = "continuation"
        target = bottom - height
    else:
        signal = "neutral"
        category = "bilateral"
        target = None
    confidence = round(min(0.88, 0.42 + (0.18 if breakout else 0) + min(0.15, height / (atr * 2) if atr else 0.05)), 3)
    return {
        "kind": "rectangle",
        "category": category,
        "signal": signal,
        "top": round(top, 6),
        "bottom": round(bottom, 6),
        "height": round(height, 6),
        "breakout": breakout,
        "broken": breakout is not None,
        "target": round(target, 6) if target is not None else None,
        "confidence": confidence,
    }


# ── Triangle (upgraded) ────────────────────────────────────────────────────


def detect_triangle(df: pd.DataFrame, pivot_left: int = 5, pivot_right: int = 5, flat_tolerance_pct: float = 0.6, min_pivots: int = 3) -> Optional[Dict[str, Any]]:
    """Ascending / descending / symmetrical / expanding triangle.

    Narrow detector: requires flat side genuinely flat and opposite side
    converging, otherwise returns None.
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
    atr = _atr(df)

    h_flat = _is_flat(highs, flat_tolerance_pct)
    l_flat = _is_flat(lows, flat_tolerance_pct)
    rising = all(b > a for a, b in zip(lows, lows[1:]))
    falling = all(b < a for a, b in zip(highs, highs[1:]))
    # ascending: flat top, rising lows
    if h_flat and rising:
        level = sum(highs) / len(highs)
        broken = close > level + (0.12 * atr if atr else 0)
        return {
            "kind": "ascending",
            "category": "bilateral",
            "signal": "bullish" if broken else "neutral",
            "level": round(level, 6),
            "broken": bool(broken),
            "breakout": "up" if broken else None,
            "direction": "up" if broken else None,
            "confidence": round(0.58 + (0.18 if broken else 0), 3),
        }
    if l_flat and falling:
        level = sum(lows) / len(lows)
        broken = close < level - (0.12 * atr if atr else 0)
        return {
            "kind": "descending",
            "category": "bilateral",
            "signal": "bearish" if broken else "neutral",
            "level": round(level, 6),
            "broken": bool(broken),
            "breakout": "down" if broken else None,
            "direction": "down" if broken else None,
            "confidence": round(0.58 + (0.18 if broken else 0), 3),
        }
    # symmetrical: converging but neither side flat — check linear fit
    if len(highs) >= 3 and len(lows) >= 3:
        hx = list(range(len(highs)))
        lx = list(range(len(lows)))
        hm, _, hr2 = _fit(hx, highs)
        lm, _, lr2 = _fit(lx, lows)
        # symmetrical: falling highs + rising lows, both with decent fit
        if hr2 > 0.45 and lr2 > 0.45 and hm < -0.01 * (sum(highs) / len(highs)) and lm > 0.01 * (sum(lows) / len(lows)):
            broken_up = close > highs[-1] + (0.12 * atr if atr else 0)
            broken_down = close < lows[-1] - (0.12 * atr if atr else 0)
            breakout = "up" if broken_up else "down" if broken_down else None
            return {
                "kind": "symmetrical",
                "category": "bilateral",
                "signal": "bullish" if broken_up else "bearish" if broken_down else "neutral",
                "level": None,
                "broken": breakout is not None,
                "breakout": breakout,
                "direction": breakout,
                "confidence": round(0.48 + (0.22 if breakout else 0), 3),
            }
    # expanding: diverging highs and lows
    if len(highs) >= 3 and len(lows) >= 3:
        hx = list(range(len(highs)))
        lx = list(range(len(lows)))
        hm, _, hr2 = _fit(hx, highs)
        lm, _, lr2 = _fit(lx, lows)
        if hr2 > 0.45 and lr2 > 0.45 and hm > 0 and lm < 0:
            return {
                "kind": "expanding",
                "category": "bilateral",
                "signal": "neutral",
                "level": None,
                "broken": False,
                "breakout": None,
                "direction": None,
                "confidence": 0.42,
            }
    return None


# ── Cup and Handle ─────────────────────────────────────────────────────────


def detect_cup_and_handle(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Cup-and-handle: U-shaped cup (depth 12-35%, width 30-120 bars) + handle pullback 5-15%."""
    if len(df) < 60:
        return None
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    atr = _atr(df)
    if not atr:
        return None
    best = None
    # search for cup: left rim high → bottom low → right rim high ~ equal
    for left in range(0, n - 40):
        left_price = highs[left]
        # find bottom within next 60 bars
        window = min(70, n - left - 12)
        if window < 30:
            continue
        bottom_idx = int(np.argmin(lows[left:left + window]) + left)
        bottom_price = lows[bottom_idx]
        depth = (left_price - bottom_price) / left_price if left_price else 0
        if not (0.08 <= depth <= 0.40):
            continue
        # find right rim after bottom
        handle_start_candidates = range(bottom_idx + 5, min(n - 6, bottom_idx + 40))
        for right in handle_start_candidates:
            right_price = highs[right]
            if abs(right_price - left_price) / left_price > 0.04:
                continue  # rim mismatch
            # cup shape: U not V — check midpoint not too deep spike
            mid = (left + right) // 2
            mid_low = float(np.min(lows[mid - 3:mid + 4]))
            if mid_low > bottom_price + depth * left_price * 0.25:
                continue  # V shape
            # handle: after right rim, small pullback then breakout
            handle_end = min(n - 1, right + 12)
            handle_lows = lows[right:handle_end + 1]
            handle_low = float(np.min(handle_lows)) if len(handle_lows) else right_price
            pullback = (right_price - handle_low) / right_price if right_price else 0
            if not (0.03 <= pullback <= 0.18):
                continue
            close_now = float(closes[-1])
            broken = close_now > right_price + 0.10 * atr
            score = (1.0 - abs(right_price - left_price) / left_price * 12) * 0.35 + (0.25 if 0.12 <= depth <= 0.30 else 0.10) + (0.18 if broken else 0)
            if best is None or score > best["score"]:
                handle_low_idx = int(np.argmin(lows[right:handle_end + 1]) + right) if len(handle_lows) else right
                target = right_price + (right_price - bottom_price) * 0.60
                best = {
                    "left_idx": int(left),
                    "left_price": float(left_price),
                    "bottom_idx": int(bottom_idx),
                    "bottom_price": float(bottom_price),
                    "right_idx": int(right),
                    "right_price": float(right_price),
                    "handle_low": float(handle_low),
                    "handle_low_idx": int(handle_low_idx),
                    "score": float(score),
                    "broken": bool(broken),
                    "target": float(target),
                }
    if not best:
        return None
    confidence = round(min(0.90, 0.42 + best["score"] * 0.42 + (0.16 if best["broken"] else 0)), 3)
    return {
        "kind": "cup_and_handle",
        "category": "continuation",
        "signal": "bullish",
        "left": {"idx": best["left_idx"], "price": round(best["left_price"], 6)},
        "bottom": {"idx": best["bottom_idx"], "price": round(best["bottom_price"], 6)},
        "right": {"idx": best["right_idx"], "price": round(best["right_price"], 6)},
        "handle_low": round(best["handle_low"], 6),
        "handle_low_idx": best["handle_low_idx"],
        "breakout": "up" if best["broken"] else None,
        "broken": bool(best["broken"]),
        "target": round(best["target"], 6),
        "confidence": confidence,
    }


def detect_island_reversal(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Island reversal: gaps on both sides of a 3-5 bar cluster. Bullish island gaps down then up, bearish opposite.
    Uses both true gaps (low > prev high) and body gaps for crypto where true gaps rare."""
    if len(df) < 35:
        return None
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    atr = _atr(df)
    if not atr:
        return None
    n = len(df)
    best = None
    for island_len in (3, 4, 5, 6):
        for a in range(max(8, n - 45), n - island_len - 2):
            b = a + island_len  # gap-out bar index is b
            if b >= n:
                continue
            # island window [a, b-1]
            island_high = float(np.max(highs[a:b]))
            island_low = float(np.min(lows[a:b]))
            prev_high = float(highs[a - 1])
            prev_low = float(lows[a - 1])
            next_high = float(highs[b])
            next_low = float(lows[b])
            next_open = float(opens[b])
            next_close = float(closes[b])
            # bullish: gap down into island, gap up out, next closes bullish
            bull_gap_in = island_low > prev_high + 0.08 * atr or (min(min(opens[a + k], closes[a + k]) for k in range(island_len)) > max(opens[a - 1], closes[a - 1]) + 0.05 * atr)
            bull_gap_out = next_low > island_high + 0.08 * atr or (min(next_open, next_close) > island_high)
            bear_gap_in = island_high < prev_low - 0.08 * atr or (max(max(opens[a + k], closes[a + k]) for k in range(island_len)) < min(opens[a - 1], closes[a - 1]) - 0.05 * atr)
            bear_gap_out = next_high < island_low - 0.08 * atr or (max(next_open, next_close) < island_low)
            is_bull = bull_gap_in and bull_gap_out and next_close > next_open
            is_bear = bear_gap_in and bear_gap_out and next_close < next_open
            if is_bull or is_bear:
                # volume confirmation optional but boosts confidence
                try:
                    vols = df["volume"].to_numpy()
                    avg = float(np.mean(vols[max(0, b - 12):b]))
                    vol_conf = float(vols[b]) > avg * 1.18 if avg > 0 else False
                except Exception:
                    vol_conf = False
                conf = 0.62 + (0.18 if vol_conf else 0) + min(0.12, island_len * 0.02)
                cand = {
                    "kind": "bullish_island" if is_bull else "bearish_island",
                    "category": "reversal",
                    "signal": "bullish" if is_bull else "bearish",
                    "island_start": int(a),
                    "island_end": int(b - 1),
                    "island_high": round(island_high, 6),
                    "island_low": round(island_low, 6),
                    "gap_in": round(prev_high, 6) if is_bull else round(prev_low, 6),
                    "gap_out": round(next_low, 6) if is_bull else round(next_high, 6),
                    "broken": True,
                    "breakout": "up" if is_bull else "down",
                    "target": round(next_close + (island_high - island_low) * (1.6 if is_bull else -1.6), 6),
                    "confidence": round(min(0.92, conf), 3),
                    "volume_confirm": bool(vol_conf),
                }
                if best is None or cand["confidence"] > best["confidence"]:
                    best = cand
    return best


def detect_mat_hold_chart(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Mat hold as chart pattern wrapper around candle mat hold."""
    try:
        from app.signals.candle_patterns import detect_patterns
        rows = [[int(ts.timestamp()*1000) if hasattr(ts, 'timestamp') else int(ts), float(o), float(h), float(l), float(c), float(v)]
                for ts, o, h, l, c, v in zip(df["timestamp"], df["open"], df["high"], df["low"], df["close"], df["volume"])]
        evts = detect_patterns(rows, patterns=["mat_hold"])
        if evts:
            last = evts[-1]
            return {
                "kind": "bullish_mat_hold" if last["direction"]=="bull" else "bearish_mat_hold",
                "category": "continuation",
                "signal": "bullish" if last["direction"]=="bull" else "bearish",
                "time": last["time"],
                "confidence": 0.62,
                "broken": True,
                "breakout": "up" if last["direction"]=="bull" else "down",
            }
    except Exception:
        pass
    return None


# ── Aggregator ─────────────────────────────────────────────────────────────


def analyze_chart_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all chart pattern detectors; return unified payload."""
    patterns: List[Dict[str, Any]] = []
    candidates: List[Tuple[str, Any]] = [
        ("double_top", detect_double_top),
        ("double_bottom", detect_double_bottom),
        ("triple_top", detect_triple_top),
        ("triple_bottom", detect_triple_bottom),
        ("head_and_shoulders", detect_head_and_shoulders),
        ("inverse_head_and_shoulders", detect_inverse_head_and_shoulders),
        ("wedge", detect_wedge),
        ("triangle", detect_triangle),
        ("rectangle", detect_rectangle),
        ("flag", detect_flag),
        ("pennant", detect_pennant),
        ("cup_and_handle", detect_cup_and_handle),
        ("island_reversal", detect_island_reversal),
        ("mat_hold", detect_mat_hold_chart),
    ]
    for name, fn in candidates:
        try:
            hit = fn(df)
            if hit:
                # ensure detector tag
                hit["detector"] = name
                patterns.append(hit)
        except Exception:
            continue

    # Deduplicate overlapping: keep highest confidence per incompatible group
    # Group 1: triple contains double — if triple present drop double same direction
    kinds = {p["kind"] for p in patterns}
    if "triple_top" in kinds and "double_top" in kinds:
        patterns = [p for p in patterns if p["kind"] != "double_top"]
    if "triple_bottom" in kinds and "double_bottom" in kinds:
        patterns = [p for p in patterns if p["kind"] != "double_bottom"]
    # H&S subsumes double top if same region (head == peak)
    if "head_and_shoulders" in kinds:
        patterns = [p for p in patterns if p["kind"] not in ("double_top", "triple_top")]
    if "inverse_head_and_shoulders" in kinds:
        patterns = [p for p in patterns if p["kind"] not in ("double_bottom", "triple_bottom")]
    # flag vs pennant — pennant more specific
    if any(p["kind"] == "bear_pennant" for p in patterns):
        patterns = [p for p in patterns if p["kind"] != "bear_flag"]
    if any(p["kind"] == "bull_pennant" for p in patterns):
        patterns = [p for p in patterns if p["kind"] != "bull_flag"]
    # wedge vs triangle — keep wedge if both
    if any(p["kind"] in ("rising_wedge", "falling_wedge") for p in patterns):
        patterns = [p for p in patterns if p["kind"] not in ("ascending", "descending", "symmetrical", "expanding")]

    # sort by confidence desc
    patterns.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # best directional signal
    best = patterns[0] if patterns else None
    # pattern confluence score [-1,1]: sum directional confidences
    score = 0.0
    for p in patterns:
        sig = p.get("signal", "neutral")
        conf = float(p.get("confidence", 0))
        # only broken/strong patterns contribute fully; pending half
        mult = 1.0 if p.get("broken") else 0.55
        if sig == "bullish":
            score += conf * mult
        elif sig == "bearish":
            score -= conf * mult
    # normalize to [-1,1] (cap at 0.95 scaling)
    if patterns:
        score = max(-1.0, min(1.0, score / 1.4))

    active = [p for p in patterns if p.get("broken")]
    pending = [p for p in patterns if not p.get("broken")]

    return {
        "patterns": patterns,
        "best": best,
        "score": round(score, 4) if patterns else 0.0,
        "count": len(patterns),
        "active": active,
        "pending": pending,
        "has_bullish": any(p.get("signal") == "bullish" for p in patterns),
        "has_bearish": any(p.get("signal") == "bearish" for p in patterns),
        "has_bilateral": any(p.get("category") == "bilateral" for p in patterns),
    }


def chart_pattern_confluence_score(price: float, df: pd.DataFrame) -> Tuple[float, List[str]]:
    """Signed score + reasons for pipeline: nearby pattern boundaries."""
    if price is None or not isinstance(price, (int, float)):
        return 0.0, []
    try:
        res = analyze_chart_patterns(df)
    except Exception:
        return 0.0, []
    score = float(res.get("score", 0.0))
    reasons: List[str] = []
    for p in res.get("patterns", [])[:2]:
        kind = p.get("kind", "?")
        sig = p.get("signal", "?")
        conf = p.get("confidence", 0)
        broken = p.get("broken")
        if broken:
            reasons.append(f"Chart {kind} {sig} broken (conf {conf:.2f})")
        else:
            reasons.append(f"Chart {kind} {sig} pending (conf {conf:.2f})")
    return round(max(-1.0, min(1.0, score)), 4), reasons
