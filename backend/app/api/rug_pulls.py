"""
Rug Pull API Routes

Endpoints for monitoring tokens that pumped 30%+ (potential rug pulls).
Includes sniper loop controls for auto-scanning and live trade execution.
"""
import json
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.timezone import now_sast
from app.models.database import RugPullToken, RugPullStatus, Signal, SignalSource, Trade
from app.signals.rug_pull_detector import (
    scan_for_pumps,
    update_watched_tokens,
    analyze_token_with_ai,
    run_rug_pull_cycle,
    run_sniper_cycle,
)

router = APIRouter(prefix="/rug-pulls", tags=["rug-pulls"])


@router.get("/")
async def list_rug_pull_tokens(
    status: Optional[str] = Query(None, description="Filter by status: watching, entry_ready, shorted, dumped, survived, expired"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List all tracked rug pull tokens, newest first."""
    lookback_hours = max(int(getattr(settings, "PUMP_MONITOR_PUMPED_RETENTION_HOURS", 24)), 1)
    recent_cutoff = now_sast() - timedelta(hours=lookback_hours)
    recent_filter = or_(
        and_(RugPullToken.updated_at.is_not(None), RugPullToken.updated_at >= recent_cutoff),
        and_(RugPullToken.updated_at.is_(None), RugPullToken.detected_at >= recent_cutoff),
    )

    query = (
        select(RugPullToken)
        .where(recent_filter)
        .order_by(desc(RugPullToken.updated_at), desc(RugPullToken.detected_at))
        .limit(limit)
    )

    if status:
        try:
            status_enum = RugPullStatus(status)
            query = (
                select(RugPullToken)
                .where(recent_filter, RugPullToken.status == status_enum)
                .order_by(desc(RugPullToken.updated_at), desc(RugPullToken.detected_at))
                .limit(limit)
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

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
                "price_change_24h": t.price_change_24h,
                "market_cap": t.market_cap,
                "volume_24h": t.volume_24h,
                "market_cap_rank": t.market_cap_rank,
                "current_price": t.current_price,
                "price_change_since_detection": t.price_change_since_detection,
                "peak_price": t.peak_price,
                "peak_change_pct": t.peak_change_pct,
                "ai_analysis": t.ai_analysis,
                "risk_score": t.risk_score,
                "recommended_entry": t.recommended_entry,
                "recommended_sl": t.recommended_sl,
                "recommended_tp": t.recommended_tp,
                "status": t.status.value if t.status else "watching",
                "trade_id": t.trade_id,
                "detected_at": t.detected_at.isoformat() if t.detected_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in rows
        ],
        "count": len(rows),
        "window_hours": lookback_hours,
    }


@router.get("/stats")
async def rug_pull_stats(db: AsyncSession = Depends(get_db)):
    """Get summary statistics of rug pull tracking."""
    lookback_hours = max(int(getattr(settings, "PUMP_MONITOR_PUMPED_RETENTION_HOURS", 24)), 1)
    recent_cutoff = now_sast() - timedelta(hours=lookback_hours)
    recent_filter = or_(
        and_(RugPullToken.updated_at.is_not(None), RugPullToken.updated_at >= recent_cutoff),
        and_(RugPullToken.updated_at.is_(None), RugPullToken.detected_at >= recent_cutoff),
    )

    result = await db.execute(
        select(
            RugPullToken.status,
            func.count(RugPullToken.id),
        )
        .where(recent_filter)
        .group_by(RugPullToken.status)
    )
    status_counts = {row[0].value if row[0] else "unknown": row[1] for row in result.all()}

    # Top pumps currently being watched
    watching = (await db.execute(
        select(RugPullToken)
        .where(
            recent_filter,
            RugPullToken.status.in_([RugPullStatus.WATCHING, RugPullStatus.ENTRY_READY]),
        )
        .order_by(desc(RugPullToken.price_change_24h))
        .limit(5)
    )).scalars().all()

    # Recently dumped
    dumped = (await db.execute(
        select(RugPullToken)
        .where(recent_filter, RugPullToken.status == RugPullStatus.DUMPED)
        .order_by(desc(RugPullToken.updated_at))
        .limit(5)
    )).scalars().all()

    return {
        "status_counts": status_counts,
        "total": sum(status_counts.values()),
        "window_hours": lookback_hours,
        "top_watching": [
            {"symbol": t.symbol, "pump_pct": t.price_change_24h, "risk_score": t.risk_score}
            for t in watching
        ],
        "recent_dumps": [
            {"symbol": t.symbol, "pump_pct": t.price_change_24h, "drop_pct": t.price_change_since_detection}
            for t in dumped
        ],
    }


@router.post("/scan")
async def trigger_scan(db: AsyncSession = Depends(get_db)):
    """Manually trigger a scan for new pumped tokens."""
    result = await run_rug_pull_cycle(db)
    return result


@router.post("/analyze/{token_id}")
async def analyze_token(token_id: int, db: AsyncSession = Depends(get_db)):
    """Run AI analysis on a specific watched token to find short entry."""
    result = await analyze_token_with_ai(db, token_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.patch("/{token_id}/status")
async def update_token_status(
    token_id: int,
    new_status: str = Query(..., description="New status"),
    db: AsyncSession = Depends(get_db),
):
    """Manually update a token's tracking status."""
    token = (await db.execute(
        select(RugPullToken).where(RugPullToken.id == token_id)
    )).scalar_one_or_none()

    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    try:
        token.status = RugPullStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    await db.commit()
    return {"id": token.id, "symbol": token.symbol, "status": token.status.value}


@router.delete("/{token_id}")
async def delete_token(token_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a token from tracking."""
    token = (await db.execute(
        select(RugPullToken).where(RugPullToken.id == token_id)
    )).scalar_one_or_none()

    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    await db.delete(token)
    await db.commit()
    return {"deleted": True, "symbol": token.symbol}


# ── Sniper Loop Controls ────────────────────────────────────

@router.post("/sniper/start")
async def start_sniper(interval: int = Query(60, ge=30, le=300)):
    """Start the sniper auto-scan loop (scans tokens every N seconds for buying power decline)."""
    from app.core.scheduler import start_sniper_loop
    started = start_sniper_loop(interval)
    if not started:
        return {"status": "already_running", "message": "Sniper loop is already running"}
    return {"status": "started", "interval": interval}


@router.post("/sniper/stop")
async def stop_sniper():
    """Stop the sniper auto-scan loop."""
    from app.core.scheduler import stop_sniper_loop
    stopped = stop_sniper_loop()
    if not stopped:
        return {"status": "not_running", "message": "Sniper loop is not running"}
    return {"status": "stopped"}


@router.get("/sniper/status")
async def sniper_status():
    """Get the current state of the sniper auto-scan loop."""
    from app.core.scheduler import get_sniper_loop_status
    return get_sniper_loop_status()


@router.post("/sniper/run-once")
async def run_sniper_once(db: AsyncSession = Depends(get_db)):
    """Manually trigger a single sniper scan cycle."""
    result = await run_sniper_cycle(db)
    return result


# ── Sniper Signals & Trades ─────────────────────────────────

@router.get("/sniper/signals")
async def get_sniper_signals(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all sniper-related signals with full analysis data.
    Returns signals from rug_pull_detector with enrichment, risk, entry details.
    """
    # Sniper signals have timeframe='rug_pull' and source=SYSTEM
    rows = (await db.execute(
        select(Signal)
        .where(
            Signal.source == SignalSource.SYSTEM,
            Signal.timeframe == "rug_pull",
        )
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )).scalars().all()

    signals = []
    for s in rows:
        # Parse the JSON blobs
        raw = {}
        indicators = {}
        try:
            raw = json.loads(s.raw_data) if s.raw_data else {}
        except Exception:
            pass
        try:
            indicators = json.loads(s.indicators) if s.indicators else {}
        except Exception:
            pass

        # Try to get linked rug pull token for enrichment data
        enrichment = {}
        rug_token = None
        token_id = raw.get("rug_pull_token_id")
        if token_id:
            rug_token = (await db.execute(
                select(RugPullToken).where(RugPullToken.id == token_id)
            )).scalar_one_or_none()

        if rug_token and rug_token.ai_analysis:
            try:
                analysis = json.loads(rug_token.ai_analysis)
                enrichment = analysis.get("enrichment", {})
            except Exception:
                pass

        # Get linked trade if one was executed
        linked_trade = None
        trade_row = (await db.execute(
            select(Trade).where(
                Trade.signal_id == s.id,
                Trade.source == "sniper",
            ).limit(1)
        )).scalar_one_or_none()
        if trade_row:
            linked_trade = {
                "id": trade_row.id,
                "status": trade_row.status,
                "side": trade_row.side,
                "price": trade_row.price,
                "average_price": trade_row.average_price,
                "stop_loss": trade_row.stop_loss,
                "take_profit": trade_row.take_profit,
                "pnl": trade_row.pnl,
                "pnl_percentage": trade_row.pnl_percentage,
                "leverage": trade_row.leverage,
                "created_at": trade_row.created_at.isoformat() if trade_row.created_at else None,
                "closed_at": trade_row.closed_at.isoformat() if trade_row.closed_at else None,
            }

        signals.append({
            "id": s.id,
            "symbol": s.symbol,
            "action": s.action.value if s.action else "sell",
            "price": s.price,
            "confidence": s.confidence,
            "strength": s.strength,
            "status": s.status.value if s.status else "pending",
            "created_at": s.created_at.isoformat() if s.created_at else None,
            # Risk analysis
            "risk_score": raw.get("risk_score"),
            "risk_reasons": raw.get("risk_reasons", []),
            "pump_pct": raw.get("pump_pct"),
            "market_cap": raw.get("market_cap"),
            "volume_24h": raw.get("volume_24h"),
            "ai_agents": raw.get("ai_agents"),
            "market_analysis": raw.get("market_analysis"),
            # Entry levels
            "entry": raw.get("recommended_entry") or indicators.get("entry"),
            "stop_loss": raw.get("recommended_sl") or indicators.get("stop_loss"),
            "take_profit": raw.get("recommended_tp") or indicators.get("take_profit"),
            # Enrichment from pump monitor + sentiment
            "enrichment": enrichment,
            # Rug pull token context
            "rug_token": {
                "id": rug_token.id,
                "symbol": rug_token.symbol,
                "name": rug_token.name,
                "status": rug_token.status.value if rug_token and rug_token.status else None,
                "peak_price": rug_token.peak_price,
                "peak_change_pct": rug_token.peak_change_pct,
                "current_price": rug_token.current_price,
            } if rug_token else None,
            # Linked trade
            "trade": linked_trade,
        })

    return {"signals": signals, "count": len(signals)}


@router.get("/sniper/trades")
async def get_sniper_trades(
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="Filter by trade status: open, closed"),
    db: AsyncSession = Depends(get_db),
):
    """Get all sniper trades with their signal context."""
    query = select(Trade).where(Trade.source == "sniper").order_by(desc(Trade.created_at)).limit(limit)
    if status:
        query = select(Trade).where(
            Trade.source == "sniper", Trade.status == status
        ).order_by(desc(Trade.created_at)).limit(limit)

    rows = (await db.execute(query)).scalars().all()

    trades = []
    for t in rows:
        # Parse raw_response for sniper context
        raw = {}
        try:
            raw = json.loads(t.raw_response) if t.raw_response else {}
        except Exception:
            pass

        trades.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "trade_side": t.trade_side,
            "status": t.status,
            "price": t.price,
            "average_price": t.average_price,
            "amount": t.amount,
            "filled_amount": t.filled_amount,
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            "leverage": t.leverage,
            "margin_mode": t.margin_mode,
            "pnl": t.pnl,
            "pnl_percentage": t.pnl_percentage,
            "fee": t.fee,
            "source": t.source,
            "signal_id": t.signal_id,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            # Sniper-specific from raw_response
            "sniper_entry": raw.get("sniper_entry"),
            "risk_score": raw.get("risk_score"),
            "entry_method": raw.get("entry_method"),
        })

    return {"trades": trades, "count": len(trades)}
