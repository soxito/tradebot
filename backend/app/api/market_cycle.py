"""Bitcoin 1064-day cycle API — the calendar read for the page and the app.

Three reads, all advisory:
  • ``GET /cycle/state``     — where the cycle stands today
  • ``GET /cycle/windows``   — green/red boxes for chart overlays
  • ``GET /cycle/calendar``  — a month grid with per-day phase + expectations

Anchors and phase lengths come from room settings (editable on the room
settings page); the endpoints accept explicit overrides so the page can
preview a what-if calendar without persisting anything.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Query

from loguru import logger

router = APIRouter(prefix="/cycle", tags=["market-cycle"])


def _snapshot_payload(snap) -> dict:
    """The snapshot as JSON — the shape the page, badge and agents all read."""
    return {
        "ok": snap.ok,
        "phase": snap.phase,
        "anchor": snap.anchor,
        "day_of_cycle": snap.day_of_cycle,
        "phase_day": snap.phase_day,
        "phase_days_total": snap.phase_days_total,
        "phase_pct": round(snap.phase_pct, 3),
        "projected_top": snap.projected_top,
        "projected_bottom": snap.projected_bottom,
        "days_to_top": snap.days_to_top,
        "days_to_bottom": snap.days_to_bottom,
        "late_phase": snap.late_phase,
        "price": snap.price,
        "cycle_high": snap.cycle_high,
        "cycle_low": snap.cycle_low,
        "validation": snap.validation,
        "as_of": snap.as_of,
    }


def _parse_anchors_raw(raw) -> Optional[list]:
    if not raw:
        return None
    import json

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except (ValueError, TypeError):
        return None


async def _overrides_from_db() -> tuple[Optional[list], Optional[int], Optional[int]]:
    """Async read of the persisted cycle settings (the honest version)."""
    try:
        from app.core.database import AsyncSessionLocal
        from app.agents.execution import get_settings

        async with AsyncSessionLocal() as db:
            s = await get_settings(db)
            return (
                _parse_anchors_raw(getattr(s, "cycle_anchors", None)),
                int(getattr(s, "cycle_bull_days", 0) or 0) or None,
                int(getattr(s, "cycle_bear_days", 0) or 0) or None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[cycle-api] settings unavailable: {exc}")
        return (None, None, None)


@router.get("/state")
async def cycle_state() -> dict:
    """Where the Bitcoin cycle stands right now."""
    from app.services import market_cycle

    anchors, bull, bear = await _overrides_from_db()
    snap = await market_cycle.resolve_cycle_snapshot(
        anchors_override=anchors, bull_days=bull, bear_days=bear
    )
    if snap is None:
        return {"ok": False, "detail": "cycle calendar unavailable"}
    return _snapshot_payload(snap)


@router.get("/windows")
async def cycle_windows() -> dict:
    """Past + projected cycle boxes for chart overlays (green bull / red bear)."""
    from app.services import market_cycle

    anchors, bull, bear = await _overrides_from_db()
    snap = await market_cycle.resolve_cycle_snapshot(
        anchors_override=anchors, bull_days=bull, bear_days=bear
    )
    if snap is None:
        return {"ok": False, "windows": []}
    windows = market_cycle.build_windows(
        anchors or market_cycle.DEFAULT_ANCHORS,
        bull_days=bull or market_cycle.BULL_DAYS,
        bear_days=bear or market_cycle.BEAR_DAYS,
    )
    return {
        "ok": True,
        "phase": snap.phase,
        "windows": [
            {
                "start": w.start,
                "end": w.end,
                "phase": w.phase,
                "projected": w.projected,
            }
            for w in windows
        ],
    }


@router.get("/calendar")
async def cycle_calendar(
    year: Optional[int] = Query(None, ge=2010, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
) -> dict:
    """One month of days: phase, cycle position, and today's expectation."""
    from app.services import market_cycle

    anchors, bull, bear = await _overrides_from_db()
    payload = await market_cycle.resolve_cycle_calendar(
        year, month, anchors_override=anchors
    )
    payload["ok"] = True
    return payload


@router.get("/analogs")
async def cycle_analogs() -> dict:
    """Every cycle's path aligned by day-of-cycle, against the live one."""
    from app.services import market_cycle

    anchors, bull, bear = await _overrides_from_db()
    snap = await market_cycle.resolve_cycle_snapshot(
        anchors_override=anchors, bull_days=bull, bear_days=bear
    )
    if snap is None:
        return {"ok": False, "cycles": [], "current": None}

    bars = await market_cycle.cached_bars()
    payload = market_cycle.cycle_analogs(
        market_cycle.parse_anchors(anchors or market_cycle.DEFAULT_ANCHORS),
        bars or [],
        bull_days=bull or market_cycle.BULL_DAYS,
        bear_days=bear or market_cycle.BEAR_DAYS,
    )
    payload["ok"] = True
    return payload


@router.get("/expectation")
async def cycle_expectation(
    offset: Optional[int] = Query(None, ge=0, le=4000, description="Defaults to today's offset"),
    days: int = Query(30, ge=1, le=120),
    horizon: int = Query(1, ge=1, le=365),
) -> dict:
    """Day-by-day base rates ahead of an offset — the daily prediction table."""
    from app.services import market_cycle

    anchors, bull, bear = await _overrides_from_db()
    snap = await market_cycle.resolve_cycle_snapshot(
        anchors_override=anchors, bull_days=bull, bear_days=bear
    )
    if snap is None:
        return {"ok": False, "rows": []}

    bars = await market_cycle.cached_bars()
    bottoms = market_cycle.parse_anchors(anchors or market_cycle.DEFAULT_ANCHORS)
    start = offset if offset is not None else snap.day_of_cycle
    rows = market_cycle.expectation_series(
        bottoms, bars or [], start, days=days,
        bull_days=bull or market_cycle.BULL_DAYS, bear_days=bear or market_cycle.BEAR_DAYS,
    )
    return {
        "ok": True,
        "start_offset": start,
        "horizon_days": horizon,
        "rows": rows if horizon == 1 else [
            market_cycle.day_expectation(
                bottoms, bars or [], r["offset"], horizon_days=horizon,
                bull_days=bull or market_cycle.BULL_DAYS, bear_days=bear or market_cycle.BEAR_DAYS,
            )
            for r in rows
        ],
    }


@router.get("/bias")
async def cycle_bias(symbol: str = Query(..., min_length=1, max_length=24)) -> dict:
    """The cycle resolved for one symbol — the same read the agents get."""
    from app.services import market_cycle

    snap = await market_cycle.resolve_cycle_snapshot()
    if snap is None:
        return {"applicable": False, "detail": "cycle calendar unavailable"}
    bias = market_cycle.cycle_bias(symbol, snap)
    return {
        "applicable": bias.applicable,
        "normalized": bias.normalized,
        "phase": bias.phase,
        "day_of_cycle": bias.day_of_cycle,
        "reason": bias.reason,
        "lines": bias.lines,
    }
