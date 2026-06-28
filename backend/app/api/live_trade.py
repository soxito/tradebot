"""
Live Auto-Trade API Routes
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings as app_settings
from app.trading.live import LiveTradeEngine
from app.core.scheduler import (
    start_live_auto_trade_loop,
    stop_live_auto_trade_loop,
    get_live_auto_trade_loop_status,
)
from loguru import logger

router = APIRouter(prefix="/live-trade", tags=["live-trade"])


# ── Request Schemas ─────────────────────────────────────────

class LiveSettingsRequest(BaseModel):
    is_active: Optional[bool] = None
    auto_trade: Optional[bool] = None
    dry_run: Optional[bool] = None
    auto_trade_pairs: Optional[List[str]] = None
    auto_trade_timeframe: Optional[str] = None
    auto_trade_max_positions: Optional[int] = None
    auto_trade_risk_pct: Optional[float] = None
    auto_trade_mode: Optional[str] = None
    auto_trade_amount_mode: Optional[str] = None
    auto_trade_leverage: Optional[int] = None
    auto_trade_margin_mode: Optional[str] = None
    auto_trade_pine_script_id: Optional[int] = None
    max_position_size_usdt: Optional[float] = None
    max_total_exposure_usdt: Optional[float] = None
    enable_ai: Optional[bool] = None
    auto_trade_ai_provider: Optional[str] = None
    tradingagents_llm_provider: Optional[str] = None
    tradingagents_deep_think_llm: Optional[str] = None
    tradingagents_quick_think_llm: Optional[str] = None
    tradingagents_backend_url: Optional[str] = None
    tradingagents_max_debate_rounds: Optional[int] = None
    tradingagents_max_risk_discuss_rounds: Optional[int] = None
    min_confidence: Optional[float] = None
    margin_size_usdt: Optional[float] = None
    min_entry_gap_pct: Optional[float] = None
    min_pump_pct: Optional[float] = None
    sniper_max_entries: Optional[int] = None
    sniper_max_positions: Optional[int] = None


# ── Settings Endpoints ──────────────────────────────────────

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Get live auto-trade settings and current exchange state."""
    return await LiveTradeEngine.get_settings_snapshot(db)


@router.post("/settings")
async def update_settings(req: LiveSettingsRequest, db: AsyncSession = Depends(get_db)):
    """Update live auto-trade settings."""
    s = await LiveTradeEngine.update_settings(
        db,
        is_active=req.is_active,
        auto_trade=req.auto_trade,
        dry_run=req.dry_run,
        auto_trade_pairs=req.auto_trade_pairs,
        auto_trade_timeframe=req.auto_trade_timeframe,
        auto_trade_max_positions=req.auto_trade_max_positions,
        auto_trade_risk_pct=req.auto_trade_risk_pct,
        auto_trade_mode=req.auto_trade_mode,
        auto_trade_amount_mode=req.auto_trade_amount_mode,
        auto_trade_leverage=req.auto_trade_leverage,
        auto_trade_margin_mode=req.auto_trade_margin_mode,
        auto_trade_pine_script_id=req.auto_trade_pine_script_id,
        max_position_size_usdt=req.max_position_size_usdt,
        max_total_exposure_usdt=req.max_total_exposure_usdt,
        enable_ai=req.enable_ai,
        auto_trade_ai_provider=req.auto_trade_ai_provider,
        tradingagents_llm_provider=req.tradingagents_llm_provider,
        tradingagents_deep_think_llm=req.tradingagents_deep_think_llm,
        tradingagents_quick_think_llm=req.tradingagents_quick_think_llm,
        tradingagents_backend_url=req.tradingagents_backend_url,
        tradingagents_max_debate_rounds=req.tradingagents_max_debate_rounds,
        tradingagents_max_risk_discuss_rounds=req.tradingagents_max_risk_discuss_rounds,
        min_confidence=req.min_confidence,
        margin_size_usdt=req.margin_size_usdt,
        min_entry_gap_pct=req.min_entry_gap_pct,
        min_pump_pct=req.min_pump_pct,
        sniper_max_entries=req.sniper_max_entries,
        sniper_max_positions=req.sniper_max_positions,
    )
    return await LiveTradeEngine.get_settings_snapshot(db, s)


# ── Auto-Trade ──────────────────────────────────────────────

class UpdateSlTpRequest(BaseModel):
    symbol: str
    side: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@router.post("/update-sl-tp")
async def update_sl_tp(req: UpdateSlTpRequest, db: AsyncSession = Depends(get_db)):
    """Update SL/TP for a specific live position on the exchange."""
    if req.stop_loss is None and req.take_profit is None:
        raise HTTPException(status_code=400, detail="Provide at least one of stop_loss or take_profit")

    from app.exchanges.manager import exchange_manager, SupportedExchange
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if not connector:
        raise HTTPException(status_code=500, detail="Bitget connector not available")

    # Ensure precision cache is loaded
    await connector.get_max_leverage("BTCUSDT")

    bitget_sym = req.symbol.replace("/", "").upper()
    hold_side = req.side.lower()

    try:
        result = await connector.replace_tpsl_orders(
            symbol=bitget_sym,
            hold_side=hold_side,
            new_sl=req.stop_loss,
            new_tp=req.take_profit,
            margin_coin="USDT",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Also update matching Trade DB record if it exists
    from app.models.database import Trade
    from sqlalchemy import select
    display_sym = req.symbol if "/" in req.symbol else bitget_sym.replace("USDT", "/USDT")
    try:
        res = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.symbol == display_sym,
            )
        )
        trade = res.scalar_one_or_none()
        if trade:
            if req.stop_loss is not None:
                trade.stop_loss = req.stop_loss
            if req.take_profit is not None:
                trade.take_profit = req.take_profit
            await db.commit()
    except Exception:
        pass

    return {
        "symbol": display_sym,
        "side": hold_side,
        "stop_loss": req.stop_loss,
        "take_profit": req.take_profit,
        "cancelled": result.get("cancelled", []),
        "placed": result.get("placed", []),
    }


@router.post("/backfill-sl-tp")
async def backfill_sl_tp(db: AsyncSession = Depends(get_db)):
    """Manually trigger SL/TP backfill for all live positions missing them."""
    # First try DB-tracked trades
    db_result = await LiveTradeEngine.backfill_sl_tp(db)
    # Then backfill exchange positions that may not have DB records
    exchange_result = await LiveTradeEngine.backfill_exchange_positions_sl_tp(db)
    combined = db_result + exchange_result
    return {"backfilled": combined, "count": len(combined)}


@router.post("/auto-trade/cycle")
async def run_auto_trade_cycle(db: AsyncSession = Depends(get_db)):
    """Run one live auto-trade cycle manually."""
    settings_row = await LiveTradeEngine.get_or_create_settings(db)
    if not app_settings.ENABLE_AUTO_TRADING and not settings_row.dry_run:
        raise HTTPException(
            status_code=403,
            detail="ENABLE_AUTO_TRADING is disabled. Set it to true in your .env or enable Dry Run in live trade settings.",
        )
    result = await LiveTradeEngine.auto_trade_cycle(db)
    return result


@router.post("/execute-signal/{signal_id}")
async def execute_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Execute a single signal on live using the same logic as auto_trade_cycle.
    Uses configured leverage, smart limit vs market, SL/TP, and records Trade in DB.
    """
    settings_row = await LiveTradeEngine.get_or_create_settings(db)
    if not app_settings.ENABLE_AUTO_TRADING and not settings_row.dry_run:
        raise HTTPException(
            status_code=403,
            detail="ENABLE_AUTO_TRADING is disabled. Set it to true in your .env or enable Dry Run in live trade settings.",
        )
    result = await LiveTradeEngine.execute_signal(db, signal_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/auto-trade/loop/start")
async def start_loop(interval: int = 60):
    """Start the persistent live auto-trade loop."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        settings_row = await LiveTradeEngine.get_or_create_settings(db)

    if not app_settings.ENABLE_AUTO_TRADING and not settings_row.dry_run:
        raise HTTPException(
            status_code=403,
            detail="ENABLE_AUTO_TRADING is disabled. Set it to true in your .env or enable Dry Run in live trade settings.",
        )
    started = start_live_auto_trade_loop(interval)
    if not started:
        return {"status": "already_running", **get_live_auto_trade_loop_status()}
    return {"status": "started", **get_live_auto_trade_loop_status()}


@router.post("/auto-trade/loop/stop")
async def stop_loop():
    """Stop the persistent live auto-trade loop."""
    stopped = stop_live_auto_trade_loop()
    if not stopped:
        return {"status": "not_running", **get_live_auto_trade_loop_status()}
    return {"status": "stopped", **get_live_auto_trade_loop_status()}


@router.get("/auto-trade/loop/status")
async def loop_status():
    """Get the current state of the live auto-trade loop."""
    return get_live_auto_trade_loop_status()


@router.post("/optimize-limit-orders")
async def optimize_limit_orders(db: AsyncSession = Depends(get_db)):
    """
    AI-powered limit order optimizer. Analyzes pending limit orders and
    adjusts entry prices based on current market conditions for better fills.
    """
    from app.agents.orchestrator import AgentOrchestrator
    result = await AgentOrchestrator.analyze_limit_orders(db)
    return result


@router.post("/optimize-open-positions")
async def optimize_open_positions(db: AsyncSession = Depends(get_db)):
    """
    AI-powered SL/TP optimizer for open positions. Analyzes filled positions
    and recalculates stop-loss/take-profit based on current market conditions.
    """
    from app.agents.orchestrator import AgentOrchestrator
    result = await AgentOrchestrator.analyze_open_positions(db)
    return result
