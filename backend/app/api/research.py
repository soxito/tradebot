"""
Research API Routes — background research, news/FOMO prediction, economic calendar.

Read side for the research loop that already runs in the MT5 plugin
(``plugins.MT5TradingPlugin.backend.services.research_loop``). The loop
collects the economic calendar, the news feeds and the Fear & Greed index every
15 minutes and writes findings into ``mt5_research_findings``; until now nothing
exposed any of it.

Plugin imports are lazy and guarded so a trimmed deploy without the MT5 plugin
still boots and simply reports the subsystem as unavailable.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter(prefix="/research", tags=["research"])


class StartResearchRequest(BaseModel):
    """Interval floors at 120s inside the loop — the free feeds are not ours to hammer."""
    interval: int = 900


def _research_loop():
    """The loop module, or None when the MT5 plugin is not installed."""
    try:
        from plugins.MT5TradingPlugin.backend.services import research_loop

        return research_loop
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research-api] research_loop unavailable: {exc}")
        return None


def _economic_calendar():
    """The calendar module, or None when the MT5 plugin is not installed."""
    try:
        from plugins.MT5TradingPlugin.backend.services import economic_calendar

        return economic_calendar
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research-api] economic_calendar unavailable: {exc}")
        return None


def _serialize_finding(row: Any) -> Dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "symbol": row.symbol,
        "headline": row.headline,
        "body": row.body,
        "source": row.source,
        "source_url": row.source_url,
        "confidence": round(float(row.confidence or 0.0), 3),
        # True when no source URL backs the finding. Such a finding is shown but
        # can never gate a trade signal on its own — the UI badges it.
        "speculative": bool(row.speculative),
        "provider_used": row.provider_used,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "decay_at": row.decay_at.isoformat() if row.decay_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/findings")
async def get_findings(
    kind: Optional[str] = Query(None, description="calendar | news | sentiment | prediction"),
    symbol: Optional[str] = Query(None, description="Filter to one instrument"),
    include_speculative: bool = Query(True, description="False returns gating findings only"),
    limit: int = Query(60, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
):
    """Findings that have not yet decayed, newest first."""
    loop = _research_loop()
    if loop is None:
        return {"findings": [], "count": 0, "available": False}

    kinds = [k.strip() for k in kind.split(",") if k.strip()] if kind else None
    if include_speculative:
        rows = await loop.active_findings(db, symbol=symbol, kinds=kinds, limit=limit)
    else:
        rows = await loop.gating_findings(db, symbol=symbol, limit=limit)
        if kinds:
            rows = [r for r in rows if r.kind in kinds]

    findings = [_serialize_finding(r) for r in rows]
    return {
        "findings": findings,
        "count": len(findings),
        "verified": sum(1 for f in findings if not f["speculative"]),
        "speculative": sum(1 for f in findings if f["speculative"]),
        "available": True,
    }


@router.get("/calendar")
async def get_calendar(
    # The window itself reaches ~30 days; leave headroom rather than 422 on a
    # horizon the data can actually serve.
    days: int = Query(14, ge=1, le=60, description="Future horizon in days"),
    currency: Optional[str] = Query(None, description="Comma-separated codes, e.g. USD,EUR"),
    impact: Optional[str] = Query(None, description="high | medium | low"),
    fomo_only: bool = Query(False, description="Only rate decisions, CPI, NFP, FOMC, GDP, PMI"),
    refresh: bool = Query(False, description="Bypass the 15-minute cache"),
):
    """The economic calendar window — this week and next week, all currencies."""
    eco = _economic_calendar()
    if eco is None:
        return {"events": [], "count": 0, "currencies": [], "available": False}

    window = await eco.fetch_calendar_window(force=refresh)
    currencies = [c.strip() for c in currency.split(",") if c.strip()] if currency else None
    events = eco.query_calendar(
        window,
        currencies=currencies,
        impact=impact,
        fomo_only=fomo_only,
        days=days,
    )
    return {
        "events": events,
        "count": len(events),
        # Every code in the unfiltered window, so the page can build its chips
        # without a second request.
        "currencies": eco.currencies_in(window),
        "next_event": eco.next_event(window),
        "source": "ForexFactory",
        "available": True,
    }


@router.get("/calendar/upcoming")
async def get_upcoming(
    limit: int = Query(8, ge=1, le=50),
    symbol: Optional[str] = Query(None, description="Scope to one instrument's currencies"),
):
    """The next high-impact events, nearest-first — future only."""
    eco = _economic_calendar()
    if eco is None:
        return {"events": [], "count": 0, "available": False}

    events = await eco.upcoming_fomo(limit=limit, symbol=symbol)
    return {"events": events, "count": len(events), "available": True}


@router.get("/reminder")
async def get_agent_reminder():
    """The exact calendar block currently injected into every agent prompt."""
    loop = _research_loop()
    if loop is None:
        return {"content": "", "events": [], "available": False}

    reminder = await loop.current_agent_reminder()
    return {**reminder, "available": True}


@router.get("/status")
async def get_status():
    """Research loop state: running, interval, last cycle result."""
    from app.core.scheduler import get_research_loop_status

    return get_research_loop_status()


@router.post("/run")
async def run_cycle(db: AsyncSession = Depends(get_db)):
    """Run one research cycle now — collect, store, publish, remind."""
    loop = _research_loop()
    if loop is None:
        raise HTTPException(status_code=503, detail="Research subsystem not available")

    return await loop.run_research_cycle(db)


@router.post("/start")
async def start_loop(request: StartResearchRequest):
    """Start the background research loop."""
    from app.core.scheduler import start_research_loop

    started = start_research_loop(request.interval)
    return {"started": started, "interval": request.interval}


@router.post("/stop")
async def stop_loop():
    """Stop the background research loop."""
    from app.core.scheduler import stop_research_loop

    return {"stopped": stop_research_loop()}
