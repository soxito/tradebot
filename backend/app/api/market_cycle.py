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

from datetime import datetime, timezone
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


def _coerce_optional_int(raw: object, default: Optional[int] = None) -> Optional[int]:
    """Coerce a query param that may arrive as literal "undefined"/"null" to None.
    Frontend axios without a request interceptor serializes `undefined` to the
    string "undefined", which Pydantic rejects with 422. This is defense-in-depth
    alongside the frontend interceptor added in the same fix.
    """
    if raw is None:
        return default
    if isinstance(raw, str) and raw.strip().lower() in ("", "undefined", "null", "none"):
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


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
    year: Optional[str] = Query(None, description="Calendar year, 2010-2100"),
    month: Optional[str] = Query(None, description="Calendar month, 1-12"),
) -> dict:
    """One month of days: phase, cycle position, and today's expectation."""
    from app.services import market_cycle

    # Coerce "undefined"/"null" strings from stale frontends to None, and
    # enforce the 2010-2100 / 1-12 ranges manually (we relaxed FastAPI's
    # automatic validation to accept those strings).
    y = _coerce_optional_int(year)
    m = _coerce_optional_int(month)
    if y is not None and not (2010 <= y <= 2100):
        y = None
    if m is not None and not (1 <= m <= 12):
        m = None
    anchors, bull, bear = await _overrides_from_db()
    payload = await market_cycle.resolve_cycle_calendar(
        y, m, anchors_override=anchors
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
    offset: Optional[str] = Query(None, description="Day offset, 0-4000. Defaults to today's offset"),
    days: Optional[str] = Query("30", description="Number of days, 1-120"),
    horizon: Optional[str] = Query("1", description="Horizon, 1-365"),
) -> dict:
    """Day-by-day base rates ahead of an offset — the daily prediction table."""
    from app.services import market_cycle

    off = _coerce_optional_int(offset)
    d = _coerce_optional_int(days, 30)
    h = _coerce_optional_int(horizon, 1)
    # Clamp to the documented ranges instead of 422-ing
    if d is None or not (1 <= d <= 120):
        d = 30
    if h is None or not (1 <= h <= 365):
        h = 1
    if off is not None and not (0 <= off <= 4000):
        off = None
    anchors, bull, bear = await _overrides_from_db()
    snap = await market_cycle.resolve_cycle_snapshot(
        anchors_override=anchors, bull_days=bull, bear_days=bear
    )
    if snap is None:
        return {"ok": False, "rows": []}

    bars = await market_cycle.cached_bars()
    bottoms = market_cycle.parse_anchors(anchors or market_cycle.DEFAULT_ANCHORS)
    start = off if off is not None else snap.day_of_cycle
    rows = market_cycle.expectation_series(
        bottoms, bars or [], start, days=d,
        bull_days=bull or market_cycle.BULL_DAYS, bear_days=bear or market_cycle.BEAR_DAYS,
    )
    return {
        "ok": True,
        "start_offset": start,
        "horizon_days": h,
        "rows": rows if h == 1 else [
            market_cycle.day_expectation(
                bottoms, bars or [], r["offset"], horizon_days=h,
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


@router.get("/candles")
async def cycle_candles(
    timeframe: Optional[str] = Query(default="D1", description="Timeframe: MN1,W1,D1,H4,H1"),
    limit: Optional[str] = Query(default=None, description="Candles, 10-150000. Empty = full genesis"),
) -> dict:
    """Full-history BTC candles for every timeframe — genesis-aware.

    * MN1/W1/D1 → Yahoo `max` + CoinMetrics daily backfill → true 2010 genesis.
    * H4/H1 → Bitget paginated (real) + synthetic daily→hourly for 2010→Bitget-start gap → continuous 2010 line.
    Cached per timeframe (TTL = CYCLE_CACHE_TTL_SECONDS). Large H1 payloads are ~5-6 MB.
    """
    from app.services import market_cycle

    lim = _coerce_optional_int(limit, None)
    tf = (timeframe or "D1").strip().upper()
    # Normalize month aliases
    if tf in ("1M", "MN", "MONTH"):
        tf = "MN1"
    if tf not in ("MN1", "W1", "D1", "H4", "H1"):
        tf = "D1"
    if lim is not None and not (10 <= lim <= 150000):
        lim = None
    bars = await market_cycle.resolve_btc_candles(timeframe=tf, limit=lim)
    candles = [
        {
            "time": int(b["time"]),
            "open": float(b.get("open") or 0.0),
            "high": float(b.get("high") or 0.0),
            "low": float(b.get("low") or 0.0),
            "close": float(b.get("close") or 0.0),
            "volume": float(b.get("volume") or 0.0),
            "synthetic": bool(b.get("synthetic", False)),
        }
        for b in bars
        if b.get("time") and b.get("close")
    ]
    synth = sum(1 for c in candles if c.get("synthetic"))
    earliest = candles[0]["time"] if candles else None
    return {
        "ok": True,
        "symbol": market_cycle._BTC_SYMBOL,
        "timeframe": tf,
        "limit": lim,
        "count": len(candles),
        "earliest": earliest,
        "earliest_iso": datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat() if earliest else None,
        "synthetic_count": synth,
        "candles": candles,
    }


@router.get("/chart")
async def cycle_chart(
    years: Optional[str] = Query(None, description="Years, 1-20. Defaults to the room's cycle_history_years"),
) -> dict:
    """The cycle screen's one payload: monthly candles + boxes + halvings.

    The candle reach follows ``cycle_history_years`` from room settings (or the
    explicit ``years`` query), so the chart brings as many monthly candles as
    the configured calendar spans. Windows are the same green/red boxes the
    page paints; patterns and Fisher ride along from the pattern-overlay
    endpoint so the two stay decoupled and cacheable.
    """
    from app.services import market_cycle

    y = _coerce_optional_int(years)
    if y is not None and not (1 <= y <= 20):
        y = None
    anchors, bull, bear = await _overrides_from_db()
    if y is None:
        y = await _history_years_from_db()
    years = y

    bars = await market_cycle.resolve_monthly_bars(years=years)
    windows = market_cycle.build_windows(
        anchors or market_cycle.DEFAULT_ANCHORS,
        bull_days=bull or market_cycle.BULL_DAYS,
        bear_days=bear or market_cycle.BEAR_DAYS,
        history_cycles=max(1, (years or 15) // 5),
    )

    candles = [
        {
            "time": int(b["time"]),
            "open": float(b.get("open") or 0.0),
            "high": float(b.get("high") or 0.0),
            "low": float(b.get("low") or 0.0),
            "close": float(b.get("close") or 0.0),
            "volume": float(b.get("volume") or 0.0),
        }
        for b in bars
        if b.get("time") and b.get("close")
    ]

    return {
        "ok": True,
        "years": years,
        "symbol": market_cycle._BTC_SYMBOL,
        "timeframe": "MN1",
        "candles": candles,
        "windows": [
            {"start": w.start, "end": w.end, "phase": w.phase, "projected": w.projected}
            for w in windows
        ],
        "anchors": [a.isoformat() for a in market_cycle.parse_anchors(anchors or market_cycle.DEFAULT_ANCHORS)],
        "halvings": list(market_cycle.HALVINGS),
        "bull_days": bull or market_cycle.BULL_DAYS,
        "bear_days": bear or market_cycle.BEAR_DAYS,
    }


async def _history_years_from_db() -> int:
    """The configured candle reach; 15 when the setting is unreadable."""
    try:
        from app.core.database import AsyncSessionLocal
        from app.agents.execution import get_settings

        async with AsyncSessionLocal() as db:
            s = await get_settings(db)
            return max(1, min(20, int(getattr(s, "cycle_history_years", 15) or 15)))
    except Exception as exc:  # noqa: BLE001 — the default is a fine chart
        logger.debug(f"[cycle-api] history years unavailable: {exc}")
        return 15
