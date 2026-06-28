"""
Trading API Routes
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.trading.decision import DecisionEngine
from app.trading.service import TradingService
from app.exchanges.manager import SupportedExchange
from app.signals.service import SignalService
from pydantic import BaseModel


router = APIRouter(prefix="/trading", tags=["trading"])


class ExecuteTradeRequest(BaseModel):
    """Request to execute a trade"""
    signal_id: int
    exchange: SupportedExchange
    dry_run: bool = True


@router.post("/evaluate/{signal_id}")
async def evaluate_signal(
    signal_id: int,
    account_balance: float = Query(10000.0, description="Account balance in USD"),
    exchange: str = Query("binance", description="Target exchange"),
    db: AsyncSession = Depends(get_db),
):
    """
    Evaluate a signal and get trading decision
    Does not execute the trade
    """
    # Get signal
    signal = await SignalService.get_signal(db, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    
    # Evaluate
    decision = await DecisionEngine.evaluate_signal(
        db, signal, account_balance, exchange
    )
    
    return decision.to_dict()


@router.post("/execute")
async def execute_trade(
    request: ExecuteTradeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Execute a trade based on a signal
    
    Safety: Requires ENABLE_AUTO_TRADING=true in config for real execution
    Dry run mode is default and safe for testing
    """
    # Safety check
    if not request.dry_run and not settings.ENABLE_AUTO_TRADING:
        raise HTTPException(
            status_code=403,
            detail="Auto-trading is disabled. Set ENABLE_AUTO_TRADING=true to enable."
        )
    
    # Get and evaluate signal
    signal = await SignalService.get_signal(db, request.signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    
    decision = await DecisionEngine.evaluate_signal(db, signal)
    
    # Execute
    result = await TradingService.execute_decision(
        db=db,
        decision=decision,
        exchange=request.exchange,
        signal_id=request.signal_id,
        dry_run=request.dry_run,
    )
    
    return result


@router.get("/analyze/{symbol}")
async def analyze_symbol(
    symbol: str,
    lookback_hours: int = Query(1, description="Hours to look back"),
    db: AsyncSession = Depends(get_db),
):
    """
    Analyze recent signals for a symbol and return trading recommendation
    """
    decision = await DecisionEngine.should_take_action(
        db, symbol, lookback_hours
    )
    
    if not decision:
        return {
            "symbol": symbol,
            "should_trade": False,
            "reason": "No actionable signals found",
        }
    
    return {
        "symbol": symbol,
        "should_trade": True,
        **decision.to_dict(),
    }


@router.get("/history")
async def get_trade_history(
    exchange: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Get trade execution history"""
    trades = await TradingService.get_trade_history(
        db, exchange, symbol, limit
    )
    
    return {
        "trades": [
            {
                "id": t.id,
                "exchange": t.exchange,
                "symbol": t.symbol,
                "side": t.side,
                "amount": t.amount,
                "price": t.price,
                "status": t.status,
                "created_at": t.created_at.isoformat(),
            }
            for t in trades
        ],
        "count": len(trades),
    }


@router.get("/status")
async def get_trading_status(db: AsyncSession = Depends(get_db)):
    """Get trading system status"""
    # Read min_confidence from live settings (DB-backed, user-configurable)
    from app.trading.live import LiveTradeEngine
    live_settings = await LiveTradeEngine.get_or_create_settings(db)
    min_conf = getattr(live_settings, "min_confidence", 0.90) or 0.90
    return {
        "auto_trading_enabled": settings.ENABLE_AUTO_TRADING,
        "risk_limits": {
            "max_position_size_usd": settings.MAX_POSITION_SIZE_USD,
            "max_risk_per_trade_percent": settings.MAX_RISK_PER_TRADE_PERCENT,
            "max_total_exposure_usd": settings.MAX_TOTAL_EXPOSURE_USD,
            "min_confidence_threshold": min_conf,
        },
        "testnet_mode": True,  # Hardcoded for safety
    }
