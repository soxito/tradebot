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


# ── Single-candle patterns ───────────────────────────────────────────────────

#: A hammer's lower wick must reach at least twice its body, and the body must
#: sit in the top slice of the bar — rejection printed at the bottom.
HAMMER_WICK_RATIO = 2.0
HAMMER_BODY_POS = 0.35


def _is_hammer(o: float, h: float, l: float, c: float) -> bool:
    body = _body(o, c)
    rng = _range(h, l)
    if body / rng > 0.45:
        return False
    lower = _lower_wick(o, l, c)
    upper = _upper_wick(o, h, c)
    return lower >= HAMMER_WICK_RATIO * body and upper <= body * 0.6 and \
        (min(o, c) - l) / rng >= 1 - HAMMER_BODY_POS - 0.3


def _is_inverted_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Small body, long upper wick, little lower wick.

    Classically read as rejection of higher prices — a bearish tell on its own,
    and the reference chart marks them mid-trend as the pause before the next
    leg. Direction here is the wick's, not the trade's.
    """
    body = _body(o, c)
    rng = _range(h, l)
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
    # The star sits below the first bar's body (a gap is rare on crypto — the
    # low placement is the same idea without assuming one).
    if max(o2, c2) > min(o1, c1):
        return None
    # The third bar closes back into at least the midpoint of the first body.
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


# ── Aggregation ──────────────────────────────────────────────────────────────

_DEFAULT_PATTERNS = (
    "morning_star", "evening_star", "inverted_hammer", "hammer",
    "shooting_star", "engulfing",
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
    if len(s) < 3:
        return []

    events: List[Dict[str, Any]] = []
    for i in range(len(s)):
        ts = s[i][0]

        def add(hit: Optional[Dict[str, Any]]) -> None:
            if hit:
                events.append({"time": ts, "index": i, **hit})

        if "morning_star" in wanted:
            add(_is_morning_star(s, i))
        if "evening_star" in wanted:
            add(_is_evening_star(s, i))
        if "engulfing" in wanted:
            add(_is_engulfing(s, i))
        if i >= 1:
            prev_o, prev_c = s[i - 1][1], s[i - 1][4]
            prev_down = prev_c < prev_o
            prev_up = prev_c > prev_o
            if "hammer" in wanted and _is_hammer(*s[i][1:]) and prev_down:
                add({"name": "Hammer", "direction": "bull"})
            if "inverted_hammer" in wanted and _is_inverted_hammer(*s[i][1:]):
                add({"name": "Inverted Hammer", "direction": "bear"})
            if "shooting_star" in wanted and _is_shooting_star(*s[i][1:]) and prev_up:
                add({"name": "Shooting Star", "direction": "bear"})
        elif "inverted_hammer" in wanted and _is_inverted_hammer(*s[i][1:]):
            add({"name": "Inverted Hammer", "direction": "bear"})

    return events
