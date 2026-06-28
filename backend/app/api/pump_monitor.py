"""
Pump Monitor API Routes

Endpoints for monitoring tokens showing pre-pump signals.
Includes monitor loop controls for auto-scanning.
"""
from typing import Optional
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.timezone import now_sast
from app.models.database import PumpToken, PumpStatus
from app.signals.pump_detector import run_pump_monitor_cycle, scan_for_pumps
from app.sentiment.cmc_community import get_cached_cmc_sentiment

router = APIRouter(prefix="/pump-monitor", tags=["pump-monitor"])


def _pumped_cutoff():
    return now_sast() - timedelta(hours=settings.PUMP_MONITOR_PUMPED_RETENTION_HOURS)


def _fresh_pumped_filter(cutoff):
    return or_(
        and_(PumpToken.updated_at.is_not(None), PumpToken.updated_at >= cutoff),
        and_(PumpToken.updated_at.is_(None), PumpToken.detected_at >= cutoff),
    )


def _exclude_stale_pumped_filter(cutoff):
    return or_(
        PumpToken.status != PumpStatus.PUMPED,
        _fresh_pumped_filter(cutoff),
    )


@router.get("/")
async def list_pump_tokens(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List all tracked pre-pump tokens, newest first."""
    cutoff = _pumped_cutoff()
    query = select(PumpToken)

    if status:
        try:
            status_enum = PumpStatus(status)
            if status_enum == PumpStatus.PUMPED:
                query = query.where(PumpToken.status == status_enum, _fresh_pumped_filter(cutoff))
            else:
                query = query.where(PumpToken.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    else:
        query = query.where(_exclude_stale_pumped_filter(cutoff))

    query = query.order_by(desc(PumpToken.detected_at)).limit(limit)

    rows = (await db.execute(query)).scalars().all()

    return {
        "tokens": [
            {
                "id": t.id,
                "coin_id": t.coin_id,
                "symbol": t.symbol,
                "name": t.name,
                "image": t.image,
                "price_at_detection": t.price_at_detection,
                "current_price": t.current_price,
                "price_change_1h": t.price_change_1h,
                "price_change_24h": t.price_change_24h,
                "price_change_7d": t.price_change_7d,
                "volume_24h": t.volume_24h,
                "volume_change_pct": t.volume_change_pct,
                "high_24h": t.high_24h,
                "low_24h": t.low_24h,
                "ath": t.ath,
                "ath_change_pct": t.ath_change_pct,
                "market_cap": t.market_cap,
                "market_cap_rank": t.market_cap_rank,
                # Original 4 indicators
                "volume_spike_score": t.volume_spike_score,
                "price_accel_score": t.price_accel_score,
                "social_score": t.social_score,
                "order_flow_score": t.order_flow_score,
                # New 4 indicators
                "momentum_score": t.momentum_score,
                "btc_relative_score": t.btc_relative_score,
                "volatility_score": t.volatility_score,
                "ath_breakout_score": t.ath_breakout_score,
                "pump_score": t.pump_score,
                # BTC context
                "btc_price_1h_pct": t.btc_price_1h_pct,
                "btc_price_24h_pct": t.btc_price_24h_pct,
                "market_sentiment": t.market_sentiment,
                "is_watchlist": t.is_watchlist or False,
                # Tracking
                "peak_price": t.peak_price,
                "peak_gain_pct": t.peak_gain_pct,
                "gain_since_detection": t.gain_since_detection,
                "trade_id": t.trade_id,
                "signal_id": t.signal_id,
                "status": t.status.value if t.status else "detected",
                "detected_at": t.detected_at.isoformat() if t.detected_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in rows
        ],
        "count": len(rows),
        "window_hours": settings.PUMP_MONITOR_PUMPED_RETENTION_HOURS,
    }


@router.get("/stats")
async def pump_monitor_stats(db: AsyncSession = Depends(get_db)):
    """Get summary statistics of pump monitoring."""
    cutoff = _pumped_cutoff()

    result = await db.execute(
        select(
            PumpToken.status,
            func.count(PumpToken.id),
        )
        .where(_exclude_stale_pumped_filter(cutoff))
        .group_by(PumpToken.status)
    )
    status_counts = {row[0].value if row[0] else "unknown": row[1] for row in result.all()}

    # Top active pump candidates
    active = (await db.execute(
        select(PumpToken)
        .where(PumpToken.status.in_([PumpStatus.DETECTED, PumpStatus.CONFIRMED, PumpStatus.SIGNALLED]))
        .order_by(desc(PumpToken.pump_score))
        .limit(5)
    )).scalars().all()

    # Recently pumped
    pumped = (await db.execute(
        select(PumpToken)
        .where(PumpToken.status == PumpStatus.PUMPED, _fresh_pumped_filter(cutoff))
        .order_by(desc(PumpToken.updated_at))
        .limit(5)
    )).scalars().all()

    return {
        "status_counts": status_counts,
        "total": sum(status_counts.values()),
        "window_hours": settings.PUMP_MONITOR_PUMPED_RETENTION_HOURS,
        "top_candidates": [
            {
                "symbol": t.symbol,
                "pump_score": t.pump_score,
                "gain_pct": t.gain_since_detection,
                "price_change_1h": t.price_change_1h,
                "btc_relative_score": t.btc_relative_score,
                "momentum_score": t.momentum_score,
                "is_watchlist": t.is_watchlist or False,
                "market_sentiment": t.market_sentiment,
            }
            for t in active
        ],
        "recent_pumps": [
            {
                "symbol": t.symbol,
                "peak_gain_pct": t.peak_gain_pct,
                "pump_score": t.pump_score,
                "is_watchlist": t.is_watchlist or False,
            }
            for t in pumped
        ],
        "watchlist_count": sum(1 for t in active if t.is_watchlist),
    }


@router.post("/scan")
async def trigger_scan(db: AsyncSession = Depends(get_db)):
    """Manually trigger a pump scan."""
    result = await scan_for_pumps(db)
    return {"status": "ok", "result": result}


@router.post("/run-cycle")
async def trigger_cycle(db: AsyncSession = Depends(get_db)):
    """Manually trigger a full pump monitor cycle (scan + signal creation)."""
    result = await run_pump_monitor_cycle(db)
    return {"status": "ok", "result": result}


@router.get("/cmc-sentiment")
async def cmc_community_sentiment():
    """Get current CMC community KOL sentiment data."""
    cached = get_cached_cmc_sentiment()
    return {
        "symbols_tracked": len(cached),
        "data": {
            sym: {
                "symbol": s.symbol,
                "mention_count": s.mention_count,
                "avg_sentiment": round(s.avg_sentiment, 3),
                "label": s.signal_label,
                "bullish_count": s.bullish_count,
                "bearish_count": s.bearish_count,
                "neutral_count": s.neutral_count,
                "max_engagement": s.max_engagement,
                "sources": s.sources,
            }
            for sym, s in sorted(cached.items(), key=lambda x: x[1].mention_count, reverse=True)
        },
    }


@router.delete("/{token_id}")
async def delete_pump_token(token_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a pump token record."""
    token = (await db.execute(
        select(PumpToken).where(PumpToken.id == token_id)
    )).scalar_one_or_none()

    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    await db.delete(token)
    await db.commit()
    return {"status": "deleted", "id": token_id}


@router.patch("/{token_id}/status")
async def update_pump_token_status(
    token_id: int,
    new_status: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Manually update a pump token's status."""
    token = (await db.execute(
        select(PumpToken).where(PumpToken.id == token_id)
    )).scalar_one_or_none()

    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    try:
        token.status = PumpStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    await db.commit()
    return {"status": "updated", "id": token_id, "new_status": new_status}


# ── Monitor Loop Controls ──────────────────────────────────

@router.post("/monitor/start")
async def start_monitor(interval: int = Query(default=120)):
    """Start the pump monitor background loop."""
    from app.core.scheduler import start_pump_monitor_loop
    started = start_pump_monitor_loop(interval)
    if not started:
        raise HTTPException(status_code=409, detail="Pump monitor already running")
    return {"status": "started", "interval": interval}


@router.post("/monitor/stop")
async def stop_monitor():
    """Stop the pump monitor background loop."""
    from app.core.scheduler import stop_pump_monitor_loop
    stopped = stop_pump_monitor_loop()
    if not stopped:
        raise HTTPException(status_code=409, detail="Pump monitor is not running")
    return {"status": "stopped"}


@router.get("/monitor/status")
async def monitor_status():
    """Get the pump monitor loop status."""
    from app.core.scheduler import get_pump_monitor_status
    return get_pump_monitor_status()


@router.post("/monitor/run-once")
async def run_once(db: AsyncSession = Depends(get_db)):
    """Run one pump monitor cycle manually."""
    result = await run_pump_monitor_cycle(db)
    return {"status": "ok", "result": result}
