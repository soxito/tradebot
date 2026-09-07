"""
Kronos Forecast Plugin — chart drawings persistence + agent refinement.

The trader draws overlays (trendlines, S/R levels, rectangles, fibs) on the
/kronos-forecast chart. Those shapes are auto-saved here, one row per
(exchange, symbol, timeframe), so they survive reloads and — crucially — become
*context the trading-room agents can read and sharpen*. ``refine_drawings``
snaps the trader's shapes onto real swing structure (pivot highs/lows, ATR
tolerance, latest swing for fibs) and writes the sharper ``source='agent'``
copies back into the same doc so both the human and the agents share one canvas.

All heavy lifting reuses the core, read-only technical library and the plugin's
own OHLCV fetcher; nothing here mutates core state. Every cross-module import is
guarded so the plugin stays standalone and removable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from plugins.KronosForecastPlugin.backend.models import KronosChartDrawing

# Distinct gold so agent-refined shapes read differently from the user's cyan.
_AGENT_COLOR = "#f5c451"
_AI_SUFFIX = ":ai"


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_row(db, exchange: str, symbol: str, timeframe: str) -> Optional[KronosChartDrawing]:
    from sqlalchemy import select

    res = await db.execute(
        select(KronosChartDrawing).where(
            KronosChartDrawing.exchange == exchange,
            KronosChartDrawing.symbol == symbol,
            KronosChartDrawing.timeframe == timeframe,
        )
    )
    return res.scalar_one_or_none()


async def _read(exchange: str, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await _load_row(db, exchange, symbol, timeframe)
        return list(row.drawings or []) if row else []


async def _write(exchange: str, symbol: str, timeframe: str, drawings: List[Dict[str, Any]]) -> None:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await _load_row(db, exchange, symbol, timeframe)
        if row is None:
            row = KronosChartDrawing(
                exchange=exchange, symbol=symbol, timeframe=timeframe, drawings=drawings,
            )
            db.add(row)
        else:
            row.drawings = drawings
            row.updated_at = datetime.utcnow()
        await db.commit()


# ── Public CRUD ───────────────────────────────────────────────────────────────

async def get_drawings(exchange: str, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    return await _read(exchange, symbol, timeframe)


async def save_drawings(
    exchange: str, symbol: str, timeframe: str, drawings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Overwrite the whole doc (client sends the full user+agent list)."""
    clean = [d for d in (drawings or []) if isinstance(d, dict) and d.get("id") and d.get("type")]
    await _write(exchange, symbol, timeframe, clean)
    return clean


async def delete_drawing(
    exchange: str, symbol: str, timeframe: str, drawing_id: str
) -> List[Dict[str, Any]]:
    """Remove one shape (and any agent shape refined from it) by id."""
    current = await _read(exchange, symbol, timeframe)
    kept = [
        d for d in current
        if d.get("id") != drawing_id and d.get("refined_from") != drawing_id
    ]
    await _write(exchange, symbol, timeframe, kept)
    return kept


async def clear_drawings(exchange: str, symbol: str, timeframe: str) -> None:
    await _write(exchange, symbol, timeframe, [])


# ── Agent refinement ──────────────────────────────────────────────────────────

def _nearest(price: float, candidates: List[float], tol: float) -> Optional[float]:
    """Closest candidate level within ``tol``; None if nothing is close enough."""
    best: Optional[float] = None
    best_d = tol
    for c in candidates:
        d = abs(c - price)
        if d <= best_d:
            best_d = d
            best = c
    return best


async def _structure(exchange: str, symbol: str, timeframe: str):
    """Return (pivot_highs, pivot_lows, atr, last_close) as plain Python values.

    Reuses the core, read-only technical library and the plugin OHLCV fetcher.
    Each pivot is ``(time_sec, price)``. Returns ``None`` on any failure so the
    caller degrades to a no-op refine.
    """
    try:
        from app.signals.technical import ohlcv_to_dataframe, pivot_highs, pivot_lows, atr
        from plugins.KronosForecastPlugin.backend.services import forecast_service
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug(f"[Kronos] drawings refine imports unavailable: {exc}")
        return None

    try:
        rows = await forecast_service._fetch_ohlcv(exchange, symbol, timeframe, 200)
    except Exception as exc:
        logger.debug(f"[Kronos] drawings refine OHLCV failed {symbol}: {exc}")
        return None
    if not rows or len(rows) < 30:
        return None

    df = ohlcv_to_dataframe(rows)
    times = (df["timestamp"].astype("int64") // 10**9).to_numpy()
    ph = pivot_highs(df["high"])
    pl = pivot_lows(df["low"])
    highs = [(int(times[i]), float(v)) for i, v in enumerate(ph.to_numpy()) if v == v]  # v==v drops NaN
    lows = [(int(times[i]), float(v)) for i, v in enumerate(pl.to_numpy()) if v == v]
    try:
        atr_val = float(atr(df).iloc[-1])
    except Exception:
        atr_val = float(df["close"].iloc[-1]) * 0.01
    if not (atr_val == atr_val) or atr_val <= 0:  # NaN or non-positive
        atr_val = float(df["close"].iloc[-1]) * 0.01
    last_close = float(df["close"].iloc[-1])
    return highs, lows, atr_val, last_close


def _refine_one(
    d: Dict[str, Any],
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
    atr_val: float,
    last_close: float,
) -> Optional[Dict[str, Any]]:
    """Produce a sharper ``source='agent'`` copy of a user shape, or None if the
    shape is already on structure / cannot be improved."""
    tol = atr_val * 1.2
    high_px = [p for _, p in highs]
    low_px = [p for _, p in lows]
    all_px = high_px + low_px
    dtype = d.get("type")
    pts = d.get("points") or []

    def agent(points: List[Dict[str, float]], note: str) -> Dict[str, Any]:
        return {
            "id": f"{d['id']}{_AI_SUFFIX}",
            "type": dtype,
            "points": points,
            "color": _AGENT_COLOR,
            "width": max(2, int(d.get("width", 2))),
            "text": d.get("text"),
            "source": "agent",
            "locked": False,
            "agent_note": note,
            "refined_from": d["id"],
        }

    if dtype == "hline" and pts:
        px = float(pts[0]["price"])
        snap = _nearest(px, all_px, tol)
        if snap is not None and abs(snap - px) > tol * 0.05:
            kind = "resistance" if snap in high_px else "support"
            return agent(
                [{"time": pts[0]["time"], "price": snap}],
                f"Snapped level to swing {kind} at {snap:g} (was {px:g}).",
            )
        return None

    if dtype in ("trendline", "ray") and len(pts) >= 2:
        moved = False
        new_pts: List[Dict[str, float]] = []
        for p in pts[:2]:
            px = float(p["price"])
            # Anchor each end to whichever swing (high or low) it sits nearest.
            pool = high_px if (high_px and px >= last_close) else low_px or all_px
            snap = _nearest(px, pool, tol)
            if snap is not None and abs(snap - px) > tol * 0.05:
                new_pts.append({"time": p["time"], "price": snap})
                moved = True
            else:
                new_pts.append({"time": p["time"], "price": px})
        if moved:
            return agent(new_pts, "Anchored trendline ends onto the nearest swing pivots.")
        return None

    if dtype == "rectangle" and len(pts) >= 2:
        p0, p1 = pts[0], pts[1]
        hi_px = max(float(p0["price"]), float(p1["price"]))
        lo_px = min(float(p0["price"]), float(p1["price"]))
        snap_hi = _nearest(hi_px, high_px or all_px, tol) or hi_px
        snap_lo = _nearest(lo_px, low_px or all_px, tol) or lo_px
        if abs(snap_hi - hi_px) > tol * 0.05 or abs(snap_lo - lo_px) > tol * 0.05:
            return agent(
                [
                    {"time": p0["time"], "price": snap_hi},
                    {"time": p1["time"], "price": snap_lo},
                ],
                f"Tightened zone to structure: {snap_lo:g} support / {snap_hi:g} resistance.",
            )
        return None

    if dtype == "fib" and highs and lows:
        # Re-anchor the fib to the most recent significant swing.
        hi_t, hi_p = max(highs, key=lambda x: x[1])
        lo_t, lo_p = min(lows, key=lambda x: x[1])
        # Draw low→high for an up-swing, high→low for a down-swing (time order).
        if hi_t >= lo_t:
            new_pts = [{"time": float(lo_t), "price": lo_p}, {"time": float(hi_t), "price": hi_p}]
        else:
            new_pts = [{"time": float(hi_t), "price": hi_p}, {"time": float(lo_t), "price": lo_p}]
        return agent(new_pts, f"Re-anchored fib to swing {lo_p:g} \u2194 {hi_p:g}.")

    return None


async def refine_drawings(
    exchange: str, symbol: str, timeframe: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Sharpen the trader's shapes onto real swing structure.

    Returns ``(refined_agent_shapes, merged_doc, note)``. The merged doc keeps
    every user shape and replaces all agent shapes with this round's refinements,
    then persists it so the shapes are shared with the trading-room agents.
    """
    current = await _read(exchange, symbol, timeframe)
    user_shapes = [d for d in current if d.get("source") != "agent"]
    if not user_shapes:
        return [], current, "No user drawings to refine yet."

    struct = await _structure(exchange, symbol, timeframe)
    if struct is None:
        return [], current, "Could not load market structure to refine against."
    highs, lows, atr_val, last_close = struct

    refined: List[Dict[str, Any]] = []
    for d in user_shapes:
        if d.get("locked"):
            continue
        try:
            ag = _refine_one(d, highs, lows, atr_val, last_close)
        except Exception as exc:
            logger.debug(f"[Kronos] refine shape {d.get('id')} failed: {exc}")
            ag = None
        if ag is not None:
            refined.append(ag)

    merged = user_shapes + refined
    await _write(exchange, symbol, timeframe, merged)
    note = (
        f"Refined {len(refined)} of {len(user_shapes)} drawing(s) onto swing structure."
        if refined else
        "Your drawings already sit on structure — nothing to sharpen."
    )
    return refined, merged, note


# ── Agent-facing context ──────────────────────────────────────────────────────

async def drawings_context(exchange: str, symbol: str, timeframe: str) -> str:
    """Compact text summary of the trader's chart drawings for the analysis
    pipeline, so agents reuse the human's overlays. Empty string when none."""
    try:
        shapes = await _read(exchange, symbol, timeframe)
    except Exception:
        return ""
    user_shapes = [d for d in shapes if d.get("source") != "agent"]
    if not user_shapes:
        return ""

    levels: List[float] = []
    parts: List[str] = []
    for d in user_shapes:
        t = d.get("type")
        pts = d.get("points") or []
        if t == "hline" and pts:
            levels.append(float(pts[0]["price"]))
        elif t in ("trendline", "ray") and len(pts) >= 2:
            parts.append(
                f"a {t} from {float(pts[0]['price']):g} to {float(pts[1]['price']):g}"
            )
        elif t == "rectangle" and len(pts) >= 2:
            hi = max(float(pts[0]["price"]), float(pts[1]["price"]))
            lo = min(float(pts[0]["price"]), float(pts[1]["price"]))
            parts.append(f"a marked zone {lo:g}-{hi:g}")
        elif t == "fib" and len(pts) >= 2:
            parts.append(
                f"a fib anchored {float(pts[0]['price']):g}\u2194{float(pts[1]['price']):g}"
            )
    if levels:
        levels.sort()
        parts.insert(0, "horizontal levels at " + ", ".join(f"{x:g}" for x in levels))
    if not parts:
        return ""
    return (
        "TRADER CHART DRAWINGS — the human has marked these on the chart; treat "
        "them as levels to respect and reference in your analysis: " + "; ".join(parts) + "."
    )
