"""Closed candles, and what they say about the move in progress.

Two problems this solves for the agents.

The first is that the last bar a feed returns is usually still forming. Handing
it over as though it had closed makes an agent reason about a high, low and
close that will all change before the candle ends — and the "current candle vs
the previous ones" comparison it is being asked to make becomes a comparison of
a partial bar against complete ones. So the forming bar is separated out and
labelled rather than silently mixed in.

The second is depth. A read taken from a handful of bars describes the last few
hours, not the market: the same close is a breakout or a failed retest
depending on the twenty bars before it. ``movement_summary`` therefore measures
the current bar against a whole window of closed ones, and always returns a
usable read — a thin window is reported as thin, never as an absent signal.
"""
from __future__ import annotations

from time import time as _now
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Candle length in seconds, for deciding whether the last bar has closed.
_TF_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}


def timeframe_seconds(timeframe: str, default: int = 3600) -> int:
    return _TF_SECONDS.get((timeframe or "").strip().lower(), default)


def _epoch(row: Sequence[Any]) -> Optional[int]:
    """Bar open time in seconds, from either a ms or a second stamp."""
    try:
        ts = int(row[0])
    except (IndexError, TypeError, ValueError):
        return None
    return ts // 1000 if ts > 1e11 else ts


def split_closed(
    ohlcv: Sequence[Sequence[Any]],
    timeframe: str,
    *,
    now: float | None = None,
) -> Tuple[List[Sequence[Any]], Optional[Sequence[Any]]]:
    """Split ``ohlcv`` into ``(closed, forming)``.

    The final bar counts as still forming when its own period has not elapsed.
    On a stale feed every bar has closed, and ``forming`` is None — that is a
    real state (a market that is shut), not an error.
    """
    rows = [r for r in (ohlcv or []) if r and _epoch(r) is not None]
    if not rows:
        return [], None

    span = timeframe_seconds(timeframe)
    clock = _now() if now is None else now
    last_open = _epoch(rows[-1])
    if last_open is not None and last_open + span > clock:
        return list(rows[:-1]), rows[-1]
    return list(rows), None


def _row(bar: Sequence[Any]) -> Dict[str, Any]:
    return {
        "time": bar[0],
        "open": float(bar[1]), "high": float(bar[2]),
        "low": float(bar[3]), "close": float(bar[4]),
        "volume": float(bar[5]) if len(bar) > 5 and bar[5] is not None else 0.0,
    }


def movement_summary(
    closed: Sequence[Sequence[Any]],
    forming: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Measure the current bar against the closed window behind it.

    Always returns a dict. When the window is too thin to say anything the
    figures are still reported and ``enough_history`` is False, so a caller can
    qualify the read instead of having no read at all.
    """
    bars = [_row(b) for b in closed or []]
    if not bars:
        return {"candles": 0, "enough_history": False,
                "note": "no closed candles available"}

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    opens = [b["open"] for b in bars]
    vols = [b["volume"] for b in bars]

    win_high, win_low = max(highs), min(lows)
    span = win_high - win_low
    net = closes[-1] - opens[0]

    ups = sum(1 for o, c in zip(opens, closes) if c > o)
    downs = sum(1 for o, c in zip(opens, closes) if c < o)

    # Consecutive same-direction closes at the right edge — the run in progress.
    streak, direction = 0, 0
    for o, c in zip(reversed(opens), reversed(closes)):
        step = 1 if c > o else -1 if c < o else 0
        if step == 0 or (direction and step != direction):
            break
        direction = step
        streak += 1

    # Structure: are the swing extremes advancing or retreating?
    half = max(1, len(bars) // 2)
    higher_high = max(highs[half:]) > max(highs[:half])
    higher_low = min(lows[half:]) > min(lows[:half])
    if higher_high and higher_low:
        structure = "higher highs and higher lows"
    elif not higher_high and not higher_low:
        structure = "lower highs and lower lows"
    else:
        structure = "mixed — no clean swing sequence"

    avg_body = sum(abs(c - o) for o, c in zip(opens, closes)) / len(bars)
    avg_range = sum(h - l for h, l in zip(highs, lows)) / len(bars)
    avg_vol = (sum(vols) / len(vols)) if any(vols) else 0.0

    out: Dict[str, Any] = {
        "candles": len(bars),
        "enough_history": len(bars) >= 28,
        "first_time": bars[0]["time"],
        "last_time": bars[-1]["time"],
        "window_high": round(win_high, 8),
        "window_low": round(win_low, 8),
        "window_range": round(span, 8),
        "net_change": round(net, 8),
        "net_change_pct": round(net / opens[0] * 100, 4) if opens[0] else None,
        "up_candles": ups,
        "down_candles": downs,
        "streak": streak,
        "streak_direction": "up" if direction > 0 else "down" if direction < 0 else "flat",
        "structure": structure,
        "avg_body": round(avg_body, 8),
        "avg_range": round(avg_range, 8),
        "avg_volume": round(avg_vol, 6),
        "last_closed": bars[-1],
    }
    if not out["enough_history"]:
        out["note"] = (
            f"only {len(bars)} closed candles available — the read is shallower "
            "than the 28-candle floor and should be qualified"
        )

    if forming is not None:
        cur = _row(forming)
        body = abs(cur["close"] - cur["open"])
        out["current_candle"] = cur
        out["current_vs_window"] = {
            "position_in_range_pct": (
                round((cur["close"] - win_low) / span * 100, 2) if span > 0 else None
            ),
            "body_vs_avg": round(body / avg_body, 2) if avg_body > 0 else None,
            "volume_vs_avg": round(cur["volume"] / avg_vol, 2) if avg_vol > 0 else None,
            "breaks_window_high": cur["high"] > win_high,
            "breaks_window_low": cur["low"] < win_low,
            "direction": (
                "up" if cur["close"] > cur["open"]
                else "down" if cur["close"] < cur["open"] else "flat"
            ),
            "note": "this candle is still forming — its high, low and close will change",
        }
    return out
