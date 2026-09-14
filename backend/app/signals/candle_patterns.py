"""Candlestick pattern detectors — the named reversals the cycle screen flags.

The reference chart marks Morning Stars at every major cycle bottom (2015,
2023, 2026) and Inverted Hammers along the way; this module is the code that
finds those on any OHLC series, any timeframe. Detectors are pure: candles in,
events out. A detector that is unsure stays silent — a pattern library that
cries wolf on every bar is worse than no library at all.

Rows are accepted in either shape the repo uses: ccxt lists
``[ms, open, high, low, close, volume]`` or provider dicts
``{time, open, high, low, close}``. Events come back oldest-first with the
bar's own timestamp, a human name, and a direction — enough for a chart
marker and a seat's sentence.

Extended per cheat-sheet taxonomy (Image 1-11):
  single: hammer, hanging_man, inverted_hammer, shooting_star,
          dragonfly_doji, gravestone_doji, long_legged_doji,
          marubozu, spinning_top, doji (generic)
  2-bar:  engulfing, harami, harami_cross, piercing, dark_cloud,
          tweezer_top, tweezer_bottom, kicker
  3-bar:  morning_star, evening_star, morning_doji_star, evening_doji_star,
          three_white_soldiers, three_black_crows,
          three_inside_up/down, three_outside_up/down, falling/rising_three_methods
  gap:    rising_window, falling_window, tasuki_gap (upside/downside)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── Row normalisation ────────────────────────────────────────────────────────


def _row(row: Any) -> Optional[Tuple[int, float, float, float, float]]:
    """(time, open, high, low, close) from either row shape; None for junk."""
    try:
        if isinstance(row, dict):
            ts = int(row.get("time"))
            o, h, l, c = (float(row.get(k)) for k in ("open", "high", "low", "close"))
        else:
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
    except (TypeError, ValueError, IndexError):
        return None
    if h <= 0 or l <= 0 or h < l or o <= 0 or c <= 0:
        return None
    return (ts, o, h, l, c)


def _row_vol(row: Any) -> float:
    try:
        if isinstance(row, dict):
            v = row.get("volume")
            return float(v) if v is not None else 0.0
        return float(row[5]) if len(row) > 5 else 0.0
    except Exception:
        return 0.0


def _series(rows: Sequence[Any]) -> List[Tuple[int, float, float, float, float]]:
    out = [r for r in (_row(row) for row in rows or []) if r is not None]
    out.sort(key=lambda x: x[0])
    return out


# ── Candle anatomy ───────────────────────────────────────────────────────────


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _is_bull(o: float, c: float) -> bool:
    return c > o


def _mid(o: float, c: float) -> float:
    return (o + c) / 2


# ── Single-candle patterns ───────────────────────────────────────────────────

#: A hammer's lower wick must reach at least twice its body, and the body must
#: sit in the top slice of the bar — rejection printed at the bottom.
HAMMER_WICK_RATIO = 2.0
HAMMER_BODY_POS = 0.35


def _is_hammer(o: float, h: float, l: float, c: float) -> bool:
    body = _body(o, c)
    rng = _range(h, l)
    if body == 0:
        return False
    if body / rng > 0.45:
        return False
    lower = _lower_wick(o, l, c)
    upper = _upper_wick(o, h, c)
    return lower >= HAMMER_WICK_RATIO * body and upper <= body * 0.6


def _is_hanging_man(o: float, h: float, l: float, c: float) -> bool:
    """Identical shape to hammer — trend context (uptrend) is applied by caller."""
    return _is_hammer(o, h, l, c)


def _is_inverted_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Small body, long upper wick, little lower wick.

    Classically read as rejection of higher prices — a bearish tell on its own,
    and the reference chart marks them mid-trend as the pause before the next
    leg. Direction here is the wick's, not the trade's.
    """
    body = _body(o, c)
    rng = _range(h, l)
    if body == 0:
        # perfect doji is not an inverted hammer
        return False
    if body / rng > 0.45:
        return False
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    return upper >= HAMMER_WICK_RATIO * body and lower <= body * 0.6


def _is_shooting_star(o: float, h: float, l: float, c: float) -> bool:
    """An inverted hammer that prints after an up-move (context applied later)."""
    return _is_inverted_hammer(o, h, l, c) and not _is_bull(o, c)


def _is_doji(o: float, h: float, l: float, c: float) -> bool:
    return _body(o, c) / _range(h, l) <= 0.1


def _is_dragonfly_doji(o: float, h: float, l: float, c: float) -> bool:
    """Open≈High≈Close, long lower wick. Bullish at bottom. Image 1 dragonfly."""
    if not _is_doji(o, h, l, c):
        return False
    body = _body(o, c)
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    rng = _range(h, l)
    # Upper wick tiny, lower wick dominates (lower >= 2.5 * body and >0.35 rng)
    return upper <= max(body * 1.0, rng * 0.08) and lower >= rng * 0.35


def _is_gravestone_doji(o: float, h: float, l: float, c: float) -> bool:
    """Open≈Low≈Close, long upper wick. Bearish at top."""
    if not _is_doji(o, h, l, c):
        return False
    body = _body(o, c)
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    rng = _range(h, l)
    return lower <= max(body * 1.0, rng * 0.08) and upper >= rng * 0.35


def _is_long_legged_doji(o: float, h: float, l: float, c: float) -> bool:
    """Doji with both wicks long — indecision. Both wicks >0.30 rng."""
    if not _is_doji(o, h, l, c):
        return False
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    rng = _range(h, l)
    return upper >= rng * 0.28 and lower >= rng * 0.28


def _is_marubozu(o: float, h: float, l: float, c: float) -> bool:
    """Strong body, virtually no wicks — decisive. Body/range >0.90."""
    body = _body(o, c)
    rng = _range(h, l)
    if body / rng < 0.90:
        return False
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    return upper <= rng * 0.05 and lower <= rng * 0.05


def _is_bullish_marubozu(o: float, h: float, l: float, c: float) -> bool:
    return _is_marubozu(o, h, l, c) and _is_bull(o, c)


def _is_bearish_marubozu(o: float, h: float, l: float, c: float) -> bool:
    return _is_marubozu(o, h, l, c) and not _is_bull(o, c)


def _is_spinning_top(o: float, h: float, l: float, c: float) -> bool:
    """Small body, both wicks long  — indecision. Tight: wicks each >=0.32 rng."""
    body = _body(o, c)
    rng = _range(h, l)
    ratio = body / rng
    if ratio < 0.12 or ratio > 0.30:
        return False
    upper = _upper_wick(o, h, c)
    lower = _lower_wick(o, l, c)
    # both wicks must dominate and be longer than body
    return upper >= rng * 0.32 and lower >= rng * 0.32 and upper >= body * 1.1 and lower >= body * 1.1


# ── Multi-candle patterns ────────────────────────────────────────────────────

#: A star's body must be small next to the candle before it.
STAR_BODY_RATIO = 0.4


def _is_morning_star(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Three-bar bullish reversal: tall red, small-bodied star gapping/low,
    tall green closing well into the first bar's body."""
    if i < 2:
        return None
    _, o1, h1, l1, c1 = s[i - 2]
    _, o2, h2, l2, c2 = s[i - 1]
    _, o3, h3, l3, c3 = s[i]
    body1, body3 = _body(o1, c1), _body(o3, c3)
    if _is_bull(o1, c1) or not _is_bull(o3, c3):
        return None
    if body1 / _range(h1, l1) < 0.4 or body3 / _range(h3, l3) < 0.4:
        return None
    if _body(o2, c2) > body1 * STAR_BODY_RATIO:
        return None
    if max(o2, c2) > min(o1, c1):
        return None
    mid1 = (o1 + c1) / 2
    if c3 <= mid1:
        return None
    return {"name": "Morning Star", "direction": "bull"}


def _is_evening_star(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Three-bar bearish reversal — the morning star mirrored at a top."""
    if i < 2:
        return None
    _, o1, h1, l1, c1 = s[i - 2]
    _, o2, h2, l2, c2 = s[i - 1]
    _, o3, h3, l3, c3 = s[i]
    body1, body3 = _body(o1, c1), _body(o3, c3)
    if not _is_bull(o1, c1) or _is_bull(o3, c3):
        return None
    if body1 / _range(h1, l1) < 0.4 or body3 / _range(h3, l3) < 0.4:
        return None
    if _body(o2, c2) > body1 * STAR_BODY_RATIO:
        return None
    if min(o2, c2) < max(o1, c1):
        return None
    mid1 = (o1 + c1) / 2
    if c3 >= mid1:
        return None
    return {"name": "Evening Star", "direction": "bear"}


def _is_morning_doji_star(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Morning star with doji star candle — stronger."""
    if i < 2:
        return None
    _, o2, h2, l2, c2 = s[i - 1]
    if not _is_doji(o2, h2, l2, c2):
        return None
    hit = _is_morning_star(s, i)
    if hit:
        hit["name"] = "Morning Doji Star"
    return hit


def _is_evening_doji_star(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    _, o2, h2, l2, c2 = s[i - 1]
    if not _is_doji(o2, h2, l2, c2):
        return None
    hit = _is_evening_star(s, i)
    if hit:
        hit["name"] = "Evening Doji Star"
    return hit


def _is_engulfing(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """A real body that swallows the previous real body — momentum flip."""
    if i < 1:
        return None
    _, o1, _, _, c1 = s[i - 1]
    _, o2, _, _, c2 = s[i]
    if _body(o2, c2) <= _body(o1, c1) * 1.1:
        return None
    if not _is_bull(o1, c1) and _is_bull(o2, c2) and o2 <= c1 and c2 >= o1:
        return {"name": "Bullish Engulfing", "direction": "bull"}
    if _is_bull(o1, c1) and not _is_bull(o2, c2) and o2 >= c1 and c2 <= o1:
        return {"name": "Bearish Engulfing", "direction": "bear"}
    return None


def _is_harami(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Inside small body contained in prior large body — momentum stall."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    body1, body2 = _body(o1, c1), _body(o2, c2)
    if body1 / _range(h1, l1) < 0.4:
        return None
    # small body inside large
    if body2 >= body1 * 0.5:
        return None
    high1, low1 = max(o1, c1), min(o1, c1)
    high2, low2 = max(o2, c2), min(o2, c2)
    if high2 > high1 or low2 < low1:
        return None
    # bullish harami: bear then bull inside, bearish harami: bull then bear
    if not _is_bull(o1, c1) and _is_bull(o2, c2):
        return {"name": "Bullish Harami", "direction": "bull"}
    if _is_bull(o1, c1) and not _is_bull(o2, c2):
        return {"name": "Bearish Harami", "direction": "bear"}
    return None


def _is_harami_cross(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if _body(o1, c1) / _range(h1, l1) < 0.4:
        return None
    if not _is_doji(o2, h2, l2, c2):
        return None
    high1, low1 = max(o1, c1), min(o1, c1)
    high2, low2 = max(o2, c2), min(o2, c2)
    if high2 > high1 or low2 < low1:
        return None
    if not _is_bull(o1, c1):
        return {"name": "Bullish Harami Cross", "direction": "bull"}
    if _is_bull(o1, c1):
        return {"name": "Bearish Harami Cross", "direction": "bear"}
    return None


def _is_piercing(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Bear candle then bull that closes > midpoint of bear. Image 1 piercing."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if _is_bull(o1, c1) or not _is_bull(o2, c2):
        return None
    if _body(o1, c1) / _range(h1, l1) < 0.35:
        return None
    # second opens below prior low (gap or below close), closes into body
    if o2 >= c1:
        return None
    mid1 = (o1 + c1) / 2
    if c2 <= mid1:
        return None
    if c2 >= o1:
        return None  # would be engulfing, not piercing
    return {"name": "Piercing Pattern", "direction": "bull"}


def _is_dark_cloud(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Bull then bear that closes below midpoint. Mirror of piercing. Image 1."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if not _is_bull(o1, c1) or _is_bull(o2, c2):
        return None
    if _body(o1, c1) / _range(h1, l1) < 0.35:
        return None
    if o2 <= c1:
        return None
    mid1 = (o1 + c1) / 2
    if c2 >= mid1:
        return None
    if c2 <= o1:
        return None
    return {"name": "Dark Cloud Cover", "direction": "bear"}


def _is_tweezer_top(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Two highs equal within 0.12*ATR or 0.15% — tweezers pinch."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if not (_is_bull(o1, c1) and not _is_bull(o1, c1) is False):
        pass
    # highs nearly equal
    avg_h = (h1 + h2) / 2
    if avg_h == 0:
        return None
    if abs(h1 - h2) / avg_h > 0.0015 and abs(h1 - h2) > max(_range(h1, l1), _range(h2, l2)) * 0.12:
        return None
    # first bull then bear is most significant
    if _is_bull(o1, c1) and not _is_bull(o2, c2):
        return {"name": "Tweezer Top", "direction": "bear"}
    return None


def _is_tweezer_bottom(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    avg_l = (l1 + l2) / 2
    if avg_l == 0:
        return None
    if abs(l1 - l2) / avg_l > 0.0015 and abs(l1 - l2) > max(_range(h1, l1), _range(h2, l2)) * 0.12:
        return None
    if not _is_bull(o1, c1) and _is_bull(o2, c2):
        return {"name": "Tweezer Bottom", "direction": "bull"}
    return None


def _is_kicker(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Gap kicker: bull marubozu gap up after bear, or bear gap down after bull."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    # bullish kicker: bear then gap up bull marubozu
    if not _is_bull(o1, c1) and _is_bull(o2, c2):
        if o2 > c1 and _is_marubozu(o2, h2, l2, c2):
            return {"name": "Bullish Kicker", "direction": "bull"}
    if _is_bull(o1, c1) and not _is_bull(o2, c2):
        if o2 < c1 and _is_marubozu(o2, h2, l2, c2):
            return {"name": "Bearish Kicker", "direction": "bear"}
    return None


def _is_three_white_soldiers(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    for k in (i - 2, i - 1, i):
        _, o, h, l, c = s[k]
        if not _is_bull(o, c):
            return None
        if _body(o, c) / _range(h, l) < 0.55:
            return None
        if _upper_wick(o, h, c) > _body(o, c) * 0.35:
            return None
    # each close higher, each open inside prior body
    _, o0, _, _, c0 = s[i - 2]
    _, o1, _, _, c1 = s[i - 1]
    _, o2, _, _, c2 = s[i]
    if not (c1 > c0 and c2 > c1):
        return None
    if not (o1 > o0 and o1 < c0 and o2 > o1 and o2 < c1):
        # allow slight tolerance — open near prior close
        if not (abs(o1 - c0) < _body(o0, c0) * 0.5 and abs(o2 - c1) < _body(o1, c1) * 0.5):
            return None
    return {"name": "Three White Soldiers", "direction": "bull"}


def _is_three_black_crows(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    for k in (i - 2, i - 1, i):
        _, o, h, l, c = s[k]
        if _is_bull(o, c):
            return None
        if _body(o, c) / _range(h, l) < 0.55:
            return None
        if _lower_wick(o, l, c) > _body(o, c) * 0.35:
            return None
    _, o0, _, _, c0 = s[i - 2]
    _, o1, _, _, c1 = s[i - 1]
    _, o2, _, _, c2 = s[i]
    if not (c1 < c0 and c2 < c1):
        return None
    if not (o1 < o0 and o1 > c0 and o2 < o1 and o2 > c1):
        if not (abs(o1 - c0) < _body(o0, c0) * 0.5 and abs(o2 - c1) < _body(o1, c1) * 0.5):
            return None
    return {"name": "Three Black Crows", "direction": "bear"}


def _is_three_inside_up(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    # harami then bullish close above prior high
    har = _is_harami(s, i - 1)
    if not har or har["direction"] != "bull":
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    _, o0, _, _, c0 = s[i - 2]
    if not _is_bull(o2, c2):
        return None
    if c2 <= max(o0, c0):
        return None
    return {"name": "Three Inside Up", "direction": "bull"}


def _is_three_inside_down(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    har = _is_harami(s, i - 1)
    if not har or har["direction"] != "bear":
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    _, o0, _, _, c0 = s[i - 2]
    if _is_bull(o2, c2):
        return None
    if c2 >= min(o0, c0):
        return None
    return {"name": "Three Inside Down", "direction": "bear"}


def _is_three_outside_up(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    eng = _is_engulfing(s, i - 1)
    if not eng or eng["direction"] != "bull":
        return None
    _, o2, h2, l2, c2 = s[i]
    if not _is_bull(o2, c2):
        return None
    _, _, _, _, c1 = s[i - 1]
    if c2 <= c1:
        return None
    return {"name": "Three Outside Up", "direction": "bull"}


def _is_three_outside_down(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    eng = _is_engulfing(s, i - 1)
    if not eng or eng["direction"] != "bear":
        return None
    _, o2, h2, l2, c2 = s[i]
    if _is_bull(o2, c2):
        return None
    _, _, _, _, c1 = s[i - 1]
    if c2 >= c1:
        return None
    return {"name": "Three Outside Down", "direction": "bear"}


def _is_falling_three(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Bearish continuation: long bear, 3 small bulls inside range, bear close below."""
    if i < 4:
        return None
    _, o0, h0, l0, c0 = s[i - 4]
    if _is_bull(o0, c0) or _body(o0, c0) / _range(h0, l0) < 0.5:
        return None
    for k in range(i - 3, i):
        _, o, h, l, c = s[k]
        if not _is_bull(o, c):
            return None
        if _body(o, c) / _range(h, l) > 0.45:
            return None
        if max(o, c) > h0 or min(o, c) < l0:
            return None
    _, o4, h4, l4, c4 = s[i]
    if _is_bull(o4, c4) or _body(o4, c4) / _range(h4, l4) < 0.5:
        return None
    if c4 >= c0:
        return None
    return {"name": "Falling Three Methods", "direction": "bear"}


def _is_rising_three(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 4:
        return None
    _, o0, h0, l0, c0 = s[i - 4]
    if not _is_bull(o0, c0) or _body(o0, c0) / _range(h0, l0) < 0.5:
        return None
    for k in range(i - 3, i):
        _, o, h, l, c = s[k]
        if _is_bull(o, c):
            return None
        if _body(o, c) / _range(h, l) > 0.45:
            return None
        if max(o, c) > h0 or min(o, c) < l0:
            return None
    _, o4, h4, l4, c4 = s[i]
    if not _is_bull(o4, c4) or _body(o4, c4) / _range(h4, l4) < 0.5:
        return None
    if c4 <= c0:
        return None
    return {"name": "Rising Three Methods", "direction": "bull"}


def _is_rising_window(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Gap up window — continuation bullish."""
    if i < 1:
        return None
    _, _, h1, _, _ = s[i - 1]
    _, _, _, l2, _ = s[i]
    if l2 > h1:
        return {"name": "Rising Window", "direction": "bull"}
    return None


def _is_falling_window(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    _, _, _, l1, _ = s[i - 1]
    _, _, h2, _, _ = s[i]
    if h2 < l1:
        return {"name": "Falling Window", "direction": "bear"}
    return None


# ── Extra single / 2-bar / multi patterns (Alchemy 50) ───────────────────────


def _is_pin_bar(o: float, h: float, l: float, c: float, direction: str) -> bool:
    """Pin bar: very small body, one wick >=2.4*body. Bull pin long lower, bear pin long upper."""
    body = _body(o, c)
    if body == 0:
        return False
    rng = _range(h, l)
    if body / rng > 0.30:
        return False
    lower = _lower_wick(o, l, c)
    upper = _upper_wick(o, h, c)
    if direction == "bull":
        return lower >= 2.4 * body and upper <= body * 0.85
    else:
        return upper >= 2.4 * body and lower <= body * 0.85


def _is_belt_hold(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Belt hold: gap open == low (bull) or == high (bear) with strong body, no opposite wick."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    body2 = _body(o2, c2)
    rng2 = _range(h2, l2)
    if body2 / rng2 < 0.55:
        return None
    # bullish belt: opens at low (within 0.08 rng) after prior bear
    if _is_bull(o2, c2) and abs(o2 - l2) <= rng2 * 0.08 and _lower_wick(o2, l2, c2) <= body2 * 0.20:
        if not _is_bull(o1, c1):
            return {"name": "Bullish Belt Hold", "direction": "bull"}
    if not _is_bull(o2, c2) and abs(o2 - h2) <= rng2 * 0.08 and _upper_wick(o2, h2, c2) <= body2 * 0.20:
        if _is_bull(o1, c1):
            return {"name": "Bearish Belt Hold", "direction": "bear"}
    return None


def _is_counterattack(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Two opposite long bodies closing equal."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if _body(o1, c1) / _range(h1, l1) < 0.45 or _body(o2, c2) / _range(h2, l2) < 0.45:
        return None
    if abs(c1 - c2) / max(abs(c1), 1e-9) > 0.0012:
        return None
    if _is_bull(o1, c1) and not _is_bull(o2, c2):
        return {"name": "Bearish Counterattack Line", "direction": "bear"}
    if not _is_bull(o1, c1) and _is_bull(o2, c2):
        return {"name": "Bullish Counterattack Line", "direction": "bull"}
    return None


def _is_homing_pigeon(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Homing pigeon: long black then small black inside body. Bullish reversal."""
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if _is_bull(o1, c1) or _is_bull(o2, c2):
        return None
    if _body(o1, c1) / _range(h1, l1) < 0.45:
        return None
    if _body(o2, c2) >= _body(o1, c1) * 0.55:
        return None
    # second body inside first body
    if max(o2, c2) > max(o1, c1) or min(o2, c2) < min(o1, c1):
        return None
    return {"name": "Bullish Homing Pigeon", "direction": "bull"}


def _is_bearish_homing(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 1:
        return None
    _, o1, h1, l1, c1 = s[i - 1]
    _, o2, h2, l2, c2 = s[i]
    if not _is_bull(o1, c1) or not _is_bull(o2, c2):
        return None
    if _body(o1, c1) / _range(h1, l1) < 0.45:
        return None
    if _body(o2, c2) >= _body(o1, c1) * 0.55:
        return None
    if max(o2, c2) > max(o1, c1) or min(o2, c2) < min(o1, c1):
        return None
    return {"name": "Bearish Homing Pigeon", "direction": "bear"}


def _is_abandoned_baby(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Abandoned baby: doji star gapped away from both neighbours. Very rare."""
    if i < 2:
        return None
    _, o1, h1, l1, c1 = s[i - 2]
    _, o2, h2, l2, c2 = s[i - 1]
    _, o3, h3, l3, c3 = s[i]
    if not _is_doji(o2, h2, l2, c2):
        return None
    # gaps on both sides (doji isolated)
    if not _is_bull(o1, c1) and _is_bull(o3, c3):
        # bullish: first bear, doji gaps down, third gaps up
        if h2 < l1 and l2 > h2 and l2 < 1e9:  # h2 < l1 and l3 > h2 -> gaps
            if h2 < min(l1, l3) and l2 < 1e9:  # simplified gap check
                gap1 = l1 > h2
                gap2 = l3 > h2
                if gap1 and gap2:
                    return {"name": "Bullish Abandoned Baby", "direction": "bull"}
                # crypto gaps rare - allow doji below both bodies
                if max(o2, c2) < min(o1, c1) and max(o2, c2) < min(o3, c3):
                    return {"name": "Bullish Abandoned Baby", "direction": "bull"}
    if _is_bull(o1, c1) and not _is_bull(o3, c3):
        if l2 > h1 and l2 > h3:
            return {"name": "Bearish Abandoned Baby", "direction": "bear"}
        if min(o2, c2) > max(o1, c1) and min(o2, c2) > max(o3, c3):
            return {"name": "Bearish Abandoned Baby", "direction": "bear"}
    return None


def _is_tri_star(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    if i < 2:
        return None
    _, o1, h1, l1, c1 = s[i - 2]
    _, o2, h2, l2, c2 = s[i - 1]
    _, o3, h3, l3, c3 = s[i]
    if not (_is_doji(o1, h1, l1, c1) and _is_doji(o2, h2, l2, c2) and _is_doji(o3, h3, l3, c3)):
        return None
    mid = (min(c1, o1) + max(c1, o1)) / 2
    mid2 = (min(c2, o2) + max(c2, o2)) / 2
    mid3 = (min(c3, o3) + max(c3, o3)) / 2
    if mid2 < mid and mid2 < mid3:
        return {"name": "Bullish Tri Star", "direction": "bull"}
    if mid2 > mid and mid2 > mid3:
        return {"name": "Bearish Tri Star", "direction": "bear"}
    return None


def _is_island(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Island: up to 5-bar cluster isolated by gaps on both sides (crypto: below bodies)."""
    # Need at least island in middle: look back up to 7, forward 1
    if i < 4:
        return None
    # island is s[i-3:i] ??? simplified: 3-5 bar island window with gaps at edges
    for island_len in (3, 4, 5):
        if i - island_len < 1:
            continue
        # island window s[a:b] with gap before a and after b-1
        a = i - island_len
        b = i  # last island bar is i-1, current is gap-out bar
        _, _, hl, _, _ = s[a - 1]
        _, _, _, ll, _ = s[a - 1]
        _, o_cur, h_cur, l_cur, c_cur = s[i]
        # gap before island: low of first island > high of previous
        _, _, h_prev, _, _ = s[a - 1]
        _, _, _, l_first, _ = s[a]
        _, _, h_last, _, _ = s[a + island_len - 1]
        _, _, _, l_next, _ = s[i]
        # bullish island: gap down into island, gap up out
        island_high = max(s[k][2] for k in range(a, a + island_len))
        island_low = min(s[k][3] for k in range(a, a + island_len))
        prev_high = s[a - 1][2]
        next_low = s[i][3]
        island_above = island_low > prev_high and next_low > island_high
        # bearish island: gap up into island, gap down out
        prev_low = s[a - 1][3]
        island_bear = island_high < prev_low and island_low < 1e9
        next_high = s[i][2]
        bear_island = island_high < prev_low and next_high < island_low if island_low < 1e9 else False
        # simplified crypto: island bodies isolated
        if island_above and _is_bull(o_cur, c_cur):
            return {"name": "Bullish Island Reversal", "direction": "bull"}
        if bear_island and not _is_bull(o_cur, c_cur):
            return {"name": "Bearish Island Reversal", "direction": "bear"}
        # bodies isolated version for crypto (no true gaps)
        island_body_high = max(max(s[k][1], s[k][4]) for k in range(a, a + island_len))
        island_body_low = min(min(s[k][1], s[k][4]) for k in range(a, a + island_len))
        prev_body_high = max(s[a - 1][1], s[a - 1][4])
        next_body_low = min(s[i][1], s[i][4])
        if island_body_low > prev_body_high and next_body_low > island_body_high and _is_bull(o_cur, c_cur):
            return {"name": "Bullish Island Reversal", "direction": "bull"}
        prev_body_low = min(s[a - 1][1], s[a - 1][4])
        next_body_high = max(s[i][1], s[i][4])
        if island_body_high < prev_body_low and next_body_high < island_body_low and not _is_bull(o_cur, c_cur):
            return {"name": "Bearish Island Reversal", "direction": "bear"}
    return None


def _is_mat_hold(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """5-bar continuation: large body then 3 inside small opposite bodies then breakout large. Mat Hold."""
    if i < 4:
        return None
    _, o0, h0, l0, c0 = s[i - 4]
    if _body(o0, c0) / _range(h0, l0) < 0.50:
        return None
    # middle 3 must be small opposite color and inside first range
    for k in range(i - 3, i):
        _, o, h, l, c = s[k]
        if _is_bull(o, c) == _is_bull(o0, c0):
            return None
        if _body(o, c) / _range(h, l) > 0.45:
            return None
        if max(o, c) > h0 or min(o, c) < l0:
            return None
    _, o4, h4, l4, c4 = s[i]
    if _is_bull(o4, c4) != _is_bull(o0, c0):
        return None
    if _body(o4, c4) / _range(h4, l4) < 0.50:
        return None
    if _is_bull(o4, c4):
        if c4 <= c0:
            return None
        return {"name": "Bullish Mat Hold", "direction": "bull"}
    else:
        if c4 >= c0:
            return None
        return {"name": "Bearish Mat Hold", "direction": "bear"}


def _is_shrinking(s: Sequence[Tuple[int, float, float, float, float]], i: int) -> Optional[Dict[str, Any]]:
    """Three shrinking bodies in same direction — exhaustion precursor."""
    if i < 2:
        return None
    b0 = _body(s[i - 2][1], s[i - 2][4])
    b1 = _body(s[i - 1][1], s[i - 1][4])
    b2 = _body(s[i][1], s[i][4])
    if not (b0 > b1 > b2 and b2 > 0):
        return None
    if not (_is_bull(s[i - 2][1], s[i - 2][4]) == _is_bull(s[i - 1][1], s[i - 1][4]) == _is_bull(s[i][1], s[i][4])):
        return None
    # ranges also shrinking
    r0, r1, r2 = _range(s[i - 2][2], s[i - 2][3]), _range(s[i - 1][2], s[i - 1][3]), _range(s[i][2], s[i][3])
    if not (r0 > r1 > r2):
        return None
    direction = "bull" if _is_bull(s[i][1], s[i][4]) else "bear"
    # shrinking after run signals reversal risk opposite
    return {"name": "Shrinking Candles", "direction": "bear" if direction == "bull" else "bull"}


# ── Aggregation ──────────────────────────────────────────────────────────────

_DEFAULT_PATTERNS = (
    # single
    "hammer", "hanging_man", "inverted_hammer", "shooting_star",
    "doji", "dragonfly_doji", "gravestone_doji", "long_legged_doji",
    "marubozu", "bullish_marubozu", "bearish_marubozu", "spinning_top",
    "bullish_pin_bar", "bearish_pin_bar",
    # 2-bar
    "engulfing", "harami", "harami_cross", "piercing", "dark_cloud",
    "tweezer_top", "tweezer_bottom", "kicker",
    "belt_hold", "counterattack", "homing_pigeon", "bearish_homing", 
    # 3-bar & multi
    "morning_star", "evening_star", "morning_doji_star", "evening_doji_star",
    "three_white_soldiers", "three_black_crows",
    "three_inside_up", "three_inside_down",
    "three_outside_up", "three_outside_down",
    "rising_three", "falling_three",
    "abandoned_baby", "tri_star", "mat_hold", "shrinking", "island",
    # gap/window
    "rising_window", "falling_window",
)

#: Single-candle detectors read context from the bar before them (a hammer
#: only counts as bullish after a down bar; a shooting star after an up bar),
#: so aggregation applies that context inline rather than through a table.


def detect_patterns(
    rows: Sequence[Any],
    *,
    patterns: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every named pattern event in the series, oldest first.

    ``patterns`` selects a subset (default: all). Each event is
    ``{"time", "index", "name", "direction"}`` — timestamps are the bar's own
    (ms for ccxt rows, seconds for provider dicts), passed through untouched.
    """
    wanted = {p.strip().lower() for p in (patterns or _DEFAULT_PATTERNS) if p.strip()}
    s = _series(rows)
    if len(s) < 2:
        return []

    events: List[Dict[str, Any]] = []
    # capture single-bar down/up streak for context gating
    down_streak = [False] * len(s)
    up_streak = [False] * len(s)
    for i in range(1, len(s)):
        down_streak[i] = s[i - 1][4] < s[i - 1][1]
        up_streak[i] = s[i - 1][4] > s[i - 1][1]

    for i in range(len(s)):
        ts = s[i][0]
        o, h, l, c = s[i][1], s[i][2], s[i][3], s[i][4]

        def add(hit: Optional[Dict[str, Any]]) -> None:
            if hit:
                events.append({"time": ts, "index": i, **hit})

        # 3-bar first (most specific)
        if "morning_star" in wanted:
            add(_is_morning_star(s, i))
        if "evening_star" in wanted:
            add(_is_evening_star(s, i))
        if "morning_doji_star" in wanted:
            hit = _is_morning_doji_star(s, i)
            if hit:
                # prefer specific name over generic morning_star duplicate
                if not any(e["index"] == i and e["name"] == "Morning Star" for e in events):
                    add(hit)
                else:
                    # replace generic with doji star
                    for e in events:
                        if e["index"] == i and e["name"] == "Morning Star":
                            e["name"] = hit["name"]
                            break
        if "evening_doji_star" in wanted:
            hit = _is_evening_doji_star(s, i)
            if hit:
                if not any(e["index"] == i and e["name"] == "Evening Star" for e in events):
                    add(hit)
                else:
                    for e in events:
                        if e["index"] == i and e["name"] == "Evening Star":
                            e["name"] = hit["name"]
                            break
        if "three_white_soldiers" in wanted:
            add(_is_three_white_soldiers(s, i))
        if "three_black_crows" in wanted:
            add(_is_three_black_crows(s, i))
        if "three_inside_up" in wanted:
            add(_is_three_inside_up(s, i))
        if "three_inside_down" in wanted:
            add(_is_three_inside_down(s, i))
        if "three_outside_up" in wanted:
            add(_is_three_outside_up(s, i))
        if "three_outside_down" in wanted:
            add(_is_three_outside_down(s, i))
        if "rising_three" in wanted:
            add(_is_rising_three(s, i))
        if "falling_three" in wanted:
            add(_is_falling_three(s, i))

        # 2-bar
        if "engulfing" in wanted:
            add(_is_engulfing(s, i))
        if "harami" in wanted:
            add(_is_harami(s, i))
        if "harami_cross" in wanted:
            # avoid duplicate harami + harami_cross same bar — prefer cross
            harami_hit = _is_harami_cross(s, i)
            if harami_hit:
                # if a harami was just added at same index, replace
                replaced = False
                for e in events:
                    if e["index"] == i and e["name"] in ("Bullish Harami", "Bearish Harami"):
                        e["name"] = harami_hit["name"]
                        replaced = True
                        break
                if not replaced:
                    add(harami_hit)
        if "piercing" in wanted:
            add(_is_piercing(s, i))
        if "dark_cloud" in wanted:
            add(_is_dark_cloud(s, i))
        if "tweezer_top" in wanted:
            add(_is_tweezer_top(s, i))
        if "tweezer_bottom" in wanted:
            add(_is_tweezer_bottom(s, i))
        if "kicker" in wanted:
            add(_is_kicker(s, i))

        # gap/window & alchemy extras
        if "rising_window" in wanted:
            add(_is_rising_window(s, i))
        if "falling_window" in wanted:
            add(_is_falling_window(s, i))
        if "belt_hold" in wanted:
            add(_is_belt_hold(s, i))
        if "counterattack" in wanted:
            add(_is_counterattack(s, i))
        if "homing_pigeon" in wanted:
            add(_is_homing_pigeon(s, i))
        if "bearish_homing" in wanted:
            add(_is_bearish_homing(s, i))
        if "abandoned_baby" in wanted:
            add(_is_abandoned_baby(s, i))
        if "tri_star" in wanted:
            add(_is_tri_star(s, i))
        if "island" in wanted:
            add(_is_island(s, i))
        if "mat_hold" in wanted:
            add(_is_mat_hold(s, i))
        if "shrinking" in wanted:
            add(_is_shrinking(s, i))

        # single-candle (context-gated)
        if i >= 1:
            prev_down = down_streak[i]
            prev_up = up_streak[i]
            # Trend gate: hammer/dragonfly bullish only after down, hanging/gravestone bear after up
            if "hammer" in wanted and _is_hammer(o, h, l, c) and prev_down:
                add({"name": "Hammer", "direction": "bull"})
            if "hanging_man" in wanted and _is_hanging_man(o, h, l, c) and prev_up:
                # deduplicate with hammer — same shape opposite context
                if not any(e["index"] == i and e["name"] == "Hammer" for e in events):
                    add({"name": "Hanging Man", "direction": "bear"})
            if "inverted_hammer" in wanted and _is_inverted_hammer(o, h, l, c):
                add({"name": "Inverted Hammer", "direction": "bull"})
            if "shooting_star" in wanted and _is_shooting_star(o, h, l, c) and prev_up:
                add({"name": "Shooting Star", "direction": "bear"})
            if "doji" in wanted and _is_doji(o, h, l, c):
                # generic doji — skip if a specific doji variant already emitted
                specific = any(e["index"] == i and e["name"] in (
                    "Dragonfly Doji", "Gravestone Doji", "Long-Legged Doji") for e in events)
                if not specific:
                    add({"name": "Doji", "direction": "neutral"})
            if "dragonfly_doji" in wanted and _is_dragonfly_doji(o, h, l, c):
                # prefer specific over generic
                for e in list(events):
                    if e["index"] == i and e["name"] == "Doji":
                        events.remove(e)
                add({"name": "Dragonfly Doji", "direction": "bull"})
            if "gravestone_doji" in wanted and _is_gravestone_doji(o, h, l, c):
                for e in list(events):
                    if e["index"] == i and e["name"] == "Doji":
                        events.remove(e)
                add({"name": "Gravestone Doji", "direction": "bear"})
            if "long_legged_doji" in wanted and _is_long_legged_doji(o, h, l, c):
                for e in list(events):
                    if e["index"] == i and e["name"] == "Doji":
                        events.remove(e)
                # don't duplicate dragonfly/gravestone which are also long-legged
                if not any(e["index"] == i and e["name"] in ("Dragonfly Doji", "Gravestone Doji") for e in events):
                    add({"name": "Long-Legged Doji", "direction": "neutral"})
            if "marubozu" in wanted and _is_marubozu(o, h, l, c):
                add({"name": "Marubozu", "direction": "bull" if _is_bull(o, c) else "bear"})
            if "bullish_marubozu" in wanted and _is_bullish_marubozu(o, h, l, c):
                if not any(e["index"] == i and e["name"] == "Marubozu" for e in events):
                    add({"name": "Bullish Marubozu", "direction": "bull"})
            if "bearish_marubozu" in wanted and _is_bearish_marubozu(o, h, l, c):
                if not any(e["index"] == i and e["name"] == "Marubozu" for e in events):
                    add({"name": "Bearish Marubozu", "direction": "bear"})
            if "spinning_top" in wanted and _is_spinning_top(o, h, l, c):
                add({"name": "Spinning Top", "direction": "neutral"})
            if "bullish_pin_bar" in wanted and _is_pin_bar(o, h, l, c, "bull") and prev_down:
                if not any(e["index"] == i and e["name"] in ("Hammer",) for e in events):
                    add({"name": "Bullish Pin Bar", "direction": "bull"})
            if "bearish_pin_bar" in wanted and _is_pin_bar(o, h, l, c, "bear") and prev_up:
                if not any(e["index"] == i and e["name"] in ("Shooting Star",) for e in events):
                    add({"name": "Bearish Pin Bar", "direction": "bear"})
        elif i == 0:
            if "inverted_hammer" in wanted and _is_inverted_hammer(o, h, l, c):
                add({"name": "Inverted Hammer", "direction": "bull"})
            if "doji" in wanted and _is_doji(o, h, l, c):
                add({"name": "Doji", "direction": "neutral"})

    return events
