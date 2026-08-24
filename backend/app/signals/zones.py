"""Supply/Demand zones and trendline channels — one module, every market.

The desk's charts read two ways: price respecting range boundaries drawn
between accumulation bases (supply/demand), and price travelling inside
trendline channels. Neither existed anywhere in the core engine — the MT5
plugin had adjacent-but-different SMC concepts (order blocks, FVGs) and the
crypto pipeline had only pivot-cluster support/resistance.

Everything here works off the shared ZigZag swing detector so the same call
behaves identically on crypto, FX, metals and indices, mirroring how a human
marks the chart: find where price left quickly (the base it never came back
to), and fit the rails it has been riding.

All public functions take a ``pd.DataFrame`` shaped by
``technical.ohlcv_to_dataframe`` (columns: timestamp, open, high, low, close,
volume) and return plain JSON-safe dicts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.signals.technical import atr as calc_atr, zigzag_pivots

# ── Configuration ─────────────────────────────────────────────────────────────

#: An impulse leg must span at least this multiple of ATR(14) to leave a
#: tradeable base behind it. Lower = more zones, higher = only violent moves.
MIN_LEG_ATR_MULT = 1.5

#: Base window: bars immediately before the impulse departure that define the
#: zone body (consolidation before the move).
BASE_LOOKBACK = 4

#: Maximum zones kept per side, strongest first.
MAX_ZONES_PER_SIDE = 4

#: Channel fits need at least these many zigzag touches on each rail.
MIN_CHANNEL_TOUCHES = 2

#: Minimum coefficient of determination for a trendline fit to be trusted.
MIN_CHANNEL_R2 = 0.60

#: Close beyond the rail by more than this fraction of ATR = breakout.
BREAKOUT_ATR_MULT = 0.30


def _to_py(obj: Any) -> Any:
    """Recursively convert numpy scalars to native Python types."""
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


# ── Supply / Demand ───────────────────────────────────────────────────────────


def detect_supply_demand(
    df: pd.DataFrame,
    min_leg_atr_mult: float = MIN_LEG_ATR_MULT,
    base_lookback: int = BASE_LOOKBACK,
    max_per_side: int = MAX_ZONES_PER_SIDE,
    zz_pivots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect supply/demand zones from impulse-leg origins.

    A demand zone forms when price consolidates (base) and then rallies away
    hard; a supply zone mirrors it for drops. Each zone carries a state:
      fresh  — price has not returned since creation (highest quality)
      tested — price returned and respected the far edge at least once
      broken — price closed through the zone (dead; kept briefly for context)

    Args:
      zz_pivots: precomputed ``zigzag_pivots(df)["pivots"]`` — pass when the
        caller already ran ZigZag so the swing scan happens once, not twice.

    Returns {"zones": [...], "nearest_supply": ..., "nearest_demand": ...}.
    """
    n = len(df)
    out: Dict[str, Any] = {"zones": [], "nearest_supply": None, "nearest_demand": None}
    if n < 40:
        return out

    atr_val = float(calc_atr(df, 14).iloc[-1])
    if not np.isfinite(atr_val) or atr_val <= 0:
        atr_val = float((df["high"] - df["low"]).tail(14).mean() or 0.0)
    if atr_val <= 0:
        return out

    if zz_pivots is None:
        zz_pivots = zigzag_pivots(df).get("pivots") or []
    pivots: List[Dict[str, Any]] = list(zz_pivots)
    if len(pivots) < 2:
        return out

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    ts_ms = pd.to_datetime(df["timestamp"]).astype("int64").to_numpy() // 10**6

    raw_zones: List[Dict[str, Any]] = []

    for prev_pv, cur_pv in zip(pivots, pivots[1:]):
        leg_move = abs(cur_pv["price"] - prev_pv["price"])
        if leg_move < min_leg_atr_mult * atr_val:
            continue

        # The base sits just before the leg's departure point. For a rally
        # (low → high) the origin is the low pivot; for a drop, the high pivot.
        origin_idx = int(prev_pv["idx"])
        base_start = max(0, origin_idx - base_lookback + 1)
        zone_high = float(np.max(highs[base_start:origin_idx + 1]))
        zone_low = float(np.min(lows[base_start:origin_idx + 1]))
        if zone_high <= zone_low:
            continue

        if cur_pv["type"] == "high":
            z_type = "demand"   # rallied away upward → buyable base below
        else:
            z_type = "supply"   # dropped away downward → sellable base above

        raw_zones.append({
            "type": z_type,
            "high": zone_high,
            "low": zone_low,
            "mid": (zone_high + zone_low) / 2.0,
            "created_idx": origin_idx,
            "created_time": int(ts_ms[origin_idx]),
            "departure_idx": int(cur_pv["idx"]),
            "leg_atr_mult": round(leg_move / atr_val, 2),
        })

    # Overlapping same-type zones collapse into the union (strongest edge wins).
    merged: List[Dict[str, Any]] = []
    for z in sorted(raw_zones, key=lambda x: x["created_idx"]):
        hit = None
        for m in merged:
            if m["type"] == z["type"] and z["low"] <= m["high"] and z["high"] >= m["low"]:
                hit = m
                break
        if hit:
            hit["high"] = max(hit["high"], z["high"])
            hit["low"] = min(hit["low"], z["low"])
            hit["mid"] = (hit["high"] + hit["low"]) / 2.0
            hit["leg_atr_mult"] = max(hit["leg_atr_mult"], z["leg_atr_mult"])
            hit["created_idx"] = min(hit["created_idx"], z["created_idx"])
        else:
            merged.append(dict(z))

    # Walk forward once to classify state and count tests.
    current_price = float(closes[-1])
    for z in merged:
        tests = 0
        broken = False
        start = z["departure_idx"] + 1
        for i in range(start, n):
            c = closes[i]
            if z["type"] == "demand":
                if c < z["low"]:
                    broken = True
                    break
                if c <= z["high"]:
                    tests += 1
            else:
                if c > z["high"]:
                    broken = True
                    break
                if c >= z["low"]:
                    tests += 1

        if broken:
            state = "broken"
        elif tests == 0:
            state = "fresh"
        else:
            state = "tested"

        strength = min(1.0, 0.35 * z["leg_atr_mult"]) * (1.0 if state == "fresh" else 0.6 if state == "tested" else 0.15)
        z.update({
            "state": state,
            "touches": tests,
            "strength": round(float(strength), 3),
            "bars_since_creation": n - 1 - z["created_idx"],
            "distance_pct": round(abs(current_price - z["mid"]) / current_price * 100, 3)
            if current_price else None,
        })

    live = [z for z in merged if z["state"] != "broken"]
    by_side: Dict[str, List[Dict[str, Any]]] = {"supply": [], "demand": []}
    for z in live:
        by_side[z["type"]].append(z)
    for side in by_side:
        by_side[side].sort(key=lambda x: x["strength"], reverse=True)
        by_side[side] = by_side[side][:max_per_side]

    zones = by_side["supply"] + by_side["demand"]

    def _nearest(kind: str, want_above: Optional[bool]) -> Optional[Dict[str, Any]]:
        cands = [
            z for z in by_side.get(kind, [])
            if want_above is None
            or (want_above and z["low"] > current_price)
            or (not want_above and z["high"] < current_price)
        ]
        if not cands:
            return None
        return min(cands, key=lambda z: abs(z["mid"] - current_price))

    out = {
        "zones": _to_py(zones),
        "nearest_supply": _to_py(_nearest("supply", want_above=True)),
        "nearest_demand": _to_py(_nearest("demand", want_above=False)),
        "atr": round(atr_val, 8),
        "price": round(current_price, 8),
    }
    return out


# ── Channels ──────────────────────────────────────────────────────────────────


def _fit_line(xs: np.ndarray, ys: np.ndarray) -> Tuple[float, float, float]:
    """Least-squares y = m·x + b → (slope, intercept, r²)."""
    if len(xs) < MIN_CHANNEL_TOUCHES:
        return 0.0, 0.0, 0.0
    m, b = np.polyfit(xs, ys, 1)
    pred = m * xs + b
    ss_res = float(np.sum((ys - pred) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(m), float(b), float(r2)


def detect_channels(
    df: pd.DataFrame,
    max_swings: int = 8,
    breakout_atr_mult: float = BREAKOUT_ATR_MULT,
    zz_pivots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect the trendline channel price has been riding.

    Both rails are fitted through ZigZag swing extremes (not every bar), the
    way a trader would draw them. Classification:
      horizontal  — |slope|·len(df) small relative to channel height
      ascending   — both rails slope up
      descending  — both rails slope down
      expanding/contracting — rails diverge/converge

    A channel is only reported when both rails clear the R² bar, so noise
    never produces a named pattern (same philosophy as detect_triangle).
    """
    out: Dict[str, Any] = {
        "channels": [],
        "active_channel": None,
        "breakout": None,
    }
    n = len(df)
    if n < 50:
        return out

    atr_series = calc_atr(df, 14).dropna()
    atr_val = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    if not np.isfinite(atr_val) or atr_val <= 0:
        return out

    if zz_pivots is None:
        zz = zigzag_pivots(df)
    else:
        zz = {"pivots": list(zz_pivots)}
    pivots: List[Dict[str, Any]] = zz.get("pivots") or []
    hi_pts = [(int(p["idx"]), float(p["price"])) for p in pivots if p["type"] == "high"]
    lo_pts = [(int(p["idx"]), float(p["price"])) for p in pivots if p["type"] == "low"]
    if len(hi_pts) < MIN_CHANNEL_TOUCHES or len(lo_pts) < MIN_CHANNEL_TOUCHES:
        return out

    hi_pts = hi_pts[-max_swings:]
    lo_pts = lo_pts[-max_swings:]

    hx = np.array([p[0] for p in hi_pts], dtype=float)
    hy = np.array([p[1] for p in hi_pts], dtype=float)
    lx = np.array([p[0] for p in lo_pts], dtype=float)
    ly = np.array([p[1] for p in lo_pts], dtype=float)

    hm, hb, hr2 = _fit_line(hx, hy)
    lm, lb, lr2 = _fit_line(lx, ly)

    if hr2 < MIN_CHANNEL_R2 or lr2 < MIN_CHANNEL_R2:
        return out

    last_idx = n - 1
    upper_now = hm * last_idx + hb
    lower_now = lm * last_idx + lb
    width = abs(upper_now - lower_now)
    if width <= 0:
        return out

    slope_span_h = abs(hm) * n
    slope_span_l = abs(lm) * n
    avg_slope_span = (slope_span_h + slope_span_l) / 2.0

    if avg_slope_span < width * 0.10:
        kind = "horizontal"
    elif hm > 0 and lm > 0:
        kind = "ascending"
    elif hm < 0 and lm < 0:
        kind = "descending"
    else:
        kind = "expanding" if width > 0 else "contracting"

    close_now = float(df["close"].iloc[-1])
    breakout = None
    if close_now > upper_now + breakout_atr_mult * atr_val:
        breakout = "up"
    elif close_now < lower_now - breakout_atr_mult * atr_val:
        breakout = "down"

    touches = len(hi_pts) + len(lo_pts)
    position_pct = round(
        max(0.0, min(1.0, (close_now - lower_now) / width)) * 100, 1
    )

    channel = {
        "kind": kind,
        "upper": {"m": round(hm, 8), "b": round(hb, 6)},
        "lower": {"m": round(lm, 8), "b": round(lb, 6)},
        "upper_now": round(upper_now, 8),
        "lower_now": round(lower_now, 8),
        "width_pct": round(width / close_now * 100, 3) if close_now else None,
        "position_pct": position_pct,
        "touches": touches,
        "r2": round(min(hr2, lr2), 3),
        "breakout": breakout,
    }

    out = {
        "channels": [channel],
        "active_channel": channel,
        "breakout": breakout,
        "atr": round(atr_val, 8),
        "price": round(close_now, 8),
    }
    return out


# ── Combined analysis + scoring hooks ─────────────────────────────────────────


def analyze_zones(df: pd.DataFrame) -> Dict[str, Any]:
    """Supply/demand + channels in one call — what chart overlays and the
    room's structure prompt consume. Shares one ZigZag pass between both."""
    zz = zigzag_pivots(df).get("pivots") or []
    sd = detect_supply_demand(df, zz_pivots=zz)
    ch = detect_channels(df, zz_pivots=zz)
    return {
        "supply_demand": sd,
        "channels": ch,
    }


def zone_reaction_score(
    price: float,
    sd: Dict[str, Any],
    channel: Optional[Dict[str, Any]] = None,
) -> Tuple[float, List[str]]:
    """Signed [-1, +1] score for how price sits relative to active zones.

    Bullish: sitting in/on demand (fresh weighted highest), bouncing off a
    channel floor, or breaking out upward through resistance-rail.
    Bearish mirrors it. Mid-range with nothing nearby scores ~0 — a forced
    read is worse than none.

    Returns (score, reasons[]).
    """
    reasons: List[str] = []
    if not price:
        return 0.0, reasons

    score = 0.0

    nearest_supply = sd.get("nearest_supply")
    nearest_demand = sd.get("nearest_demand")

    def _in(z: Dict[str, Any]) -> bool:
        return bool(z and z["low"] <= price <= z["high"])

    def _near(z: Dict[str, Any], tol: float = 0.25) -> bool:
        return bool(z and abs(price - z["mid"]) / price * 100 <= tol)

    if _in(nearest_demand) or _near(nearest_demand):
        s = (nearest_demand or {}).get("strength", 0.3)
        score += 0.7 * s
        st = (nearest_demand or {}).get("state", "?")
        reasons.append(f"At demand zone {st} ({(nearest_demand or {}).get('low'):g}–{(nearest_demand or {}).get('high'):g})")
    if _in(nearest_supply) or _near(nearest_supply):
        s = (nearest_supply or {}).get("strength", 0.3)
        score -= 0.7 * s
        st = (nearest_supply or {}).get("state", "?")
        reasons.append(f"At supply zone {st} ({(nearest_supply or {}).get('low'):g}–{(nearest_supply or {}).get('high'):g})")

    if channel:
        pos = channel.get("position_pct")
        if pos is not None:
            if pos <= 20.0 and channel.get("kind") != "descending":
                score += 0.3
                reasons.append(f"Riding {channel['kind']} channel floor ({pos:.0f}%)")
            elif pos >= 80.0 and channel.get("kind") != "ascending":
                score -= 0.3
                reasons.append(f"Pinned at {channel['kind']} channel ceiling ({pos:.0f}%)")
        bo = channel.get("breakout")
        if bo == "up":
            score += 0.25
            reasons.append("Broke out above channel")
        elif bo == "down":
            score -= 0.25
            reasons.append("Broke down through channel")

    return max(-1.0, min(1.0, score)), reasons


# ── Cross-engine helpers ──────────────────────────────────────────────────────


def candles_to_df(candles: Any) -> pd.DataFrame:
    """Normalise candle rows/objects into the OHLCV frame every zone function
    expects. Accepts ccxt-style lists, dicts, or dataclass-like objects with
    open/high/low/close attributes (e.g. the MT5 plugin's Candle)."""
    rows: List[Dict[str, Any]] = []
    for c in candles or []:
        if isinstance(c, dict):
            acc = lambda k, _c=c: _c.get(k)  # noqa: E731
            ts_raw = _c_time = acc("time") or acc("timestamp") or 0
        else:
            acc = lambda k, _c=c: getattr(_c, k, None)  # noqa: E731
            ts_raw = acc("time") or acc("timestamp") or 0
        try:
            ts = int(ts_raw)
            rows.append({
                "timestamp": pd.Timestamp(ts, unit="ms")
                if ts > 10**12 else pd.Timestamp(ts, unit="s"),
                "open": float(acc("open")),
                "high": float(acc("high")),
                "low": float(acc("low")),
                "close": float(acc("close")),
                "volume": float(acc("volume") or 0.0),
            })
        except (TypeError, ValueError):
            continue
    return pd.DataFrame(rows)


def compact_payload(df_or_candles: Any) -> Dict[str, Any]:
    """Lean zone context for consumers that want the levels without the
    per-zone bookkeeping — room prompts, SMC payloads, signal cards."""
    df = df_or_candles if isinstance(df_or_candles, pd.DataFrame) else candles_to_df(df_or_candles)
    if df.empty:
        return {"supply_demand": {"zones": []}, "channels": {"channels": []}}
    return _to_py(analyze_zones(df))
