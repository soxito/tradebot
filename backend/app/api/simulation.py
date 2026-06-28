"""
Simulation (Paper Trading) API Routes
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.trading.simulation import SimulationEngine, SmartStopLoss
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.bitget import BitgetConnector
from app.signals.technical import ohlcv_to_dataframe
from app.core.scheduler import (
    start_auto_trade_loop,
    stop_auto_trade_loop,
    get_auto_trade_loop_status,
)
from loguru import logger

router = APIRouter(prefix="/simulation", tags=["simulation"])


# ── Request Schemas ─────────────────────────────────────────

class AddFundsRequest(BaseModel):
    amount: float

class ResetRequest(BaseModel):
    starting_balance: float = 10000.0

class ToggleRequest(BaseModel):
    active: bool

class PlaceOrderRequest(BaseModel):
    symbol: str
    side: str  # buy / sell
    amount: float
    amount_mode: str = "base"  # base (pair qty) / quote (USDT value)
    price: Optional[float] = None  # if None, fetches live price
    order_type: str = "market"
    signal_id: Optional[int] = None
    auto_sl: bool = True  # auto-calculate smart stop-loss
    trade_type: str = "spot"  # spot / futures
    margin_mode: Optional[str] = None  # crossed / isolated
    leverage: Optional[int] = None

class SettingsRequest(BaseModel):
    auto_trade: Optional[bool] = None
    auto_trade_pairs: Optional[List[str]] = None
    auto_trade_timeframe: Optional[str] = None
    auto_trade_max_positions: Optional[int] = None
    auto_trade_risk_pct: Optional[float] = None
    auto_trade_mode: Optional[str] = None  # spot / futures
    auto_trade_leverage: Optional[int] = None
    auto_trade_margin_mode: Optional[str] = None  # crossed / isolated
    auto_trade_amount_mode: Optional[str] = None  # quote (USDT) / base (pair qty)
    auto_trade_pine_script_id: Optional[int] = None  # selected Pine Script for decisions
    enable_ai: Optional[bool] = None  # enable AI agent validation for sim trades
    auto_trade_ai_provider: Optional[str] = None
    tradingagents_llm_provider: Optional[str] = None
    tradingagents_deep_think_llm: Optional[str] = None
    tradingagents_quick_think_llm: Optional[str] = None
    tradingagents_backend_url: Optional[str] = None
    tradingagents_max_debate_rounds: Optional[int] = None
    tradingagents_max_risk_discuss_rounds: Optional[int] = None
    min_confidence: Optional[float] = None  # minimum signal confidence (0.50-1.0)
    margin_size_usdt: Optional[float] = None
    min_entry_gap_pct: Optional[float] = None
    min_pump_pct: Optional[float] = None
    sniper_max_entries: Optional[int] = None


# ── Account Endpoints ───────────────────────────────────────

@router.get("/account")
async def get_account(db: AsyncSession = Depends(get_db)):
    """Get simulation account (creates one if none exists)."""
    return await SimulationEngine.get_account_snapshot(db)


@router.post("/account/toggle")
async def toggle_account(req: ToggleRequest, db: AsyncSession = Depends(get_db)):
    """Activate or deactivate the simulation account."""
    account = await SimulationEngine.toggle_active(db, req.active)
    return await SimulationEngine.get_account_snapshot(db, account)


@router.post("/account/add-funds")
async def add_funds(req: AddFundsRequest, db: AsyncSession = Depends(get_db)):
    """Add virtual funds to the simulation account."""
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    account = await SimulationEngine.add_funds(db, req.amount)
    return await SimulationEngine.get_account_snapshot(db, account)


@router.post("/account/reset")
async def reset_account(req: ResetRequest, db: AsyncSession = Depends(get_db)):
    """Reset the simulation account — closes all positions, resets balance."""
    account = await SimulationEngine.reset_account(db, req.starting_balance)
    return await SimulationEngine.get_account_snapshot(db, account)


@router.post("/account/settings")
async def update_settings(req: SettingsRequest, db: AsyncSession = Depends(get_db)):
    """Update auto-trade settings."""
    account = await SimulationEngine.update_settings(
        db,
        auto_trade=req.auto_trade,
        auto_trade_pairs=req.auto_trade_pairs,
        auto_trade_timeframe=req.auto_trade_timeframe,
        auto_trade_max_positions=req.auto_trade_max_positions,
        auto_trade_risk_pct=req.auto_trade_risk_pct,
        auto_trade_mode=req.auto_trade_mode,
        auto_trade_leverage=req.auto_trade_leverage,
        auto_trade_margin_mode=req.auto_trade_margin_mode,
        auto_trade_amount_mode=req.auto_trade_amount_mode,
        auto_trade_pine_script_id=req.auto_trade_pine_script_id,
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
    )
    return await SimulationEngine.get_account_snapshot(db, account)


# ── Orders & Positions ──────────────────────────────────────

@router.post("/order")
async def place_order(req: PlaceOrderRequest, db: AsyncSession = Depends(get_db)):
    """Place a simulated order (manual or signal-based)."""
    price = req.price

    # Fetch live price if not provided
    if not price:
        try:
            connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            if not connector:
                raise HTTPException(status_code=500, detail="Exchange not initialized")
            ticker = await connector.get_ticker(req.symbol)
            price = ticker.get("last") or ticker.get("close")
            if not price:
                raise HTTPException(status_code=500, detail="Could not fetch price")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Price fetch failed: {e}")

    # Convert USDT amount to base currency if amount_mode is 'quote'
    order_amount = req.amount
    if req.amount_mode == "quote" and price and price > 0:
        order_amount = req.amount / price  # USDT ÷ price = base qty

    # Smart stop-loss
    stop_loss = None
    take_profit = None
    sl_type = None

    if req.auto_sl:
        try:
            connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            ohlcv = await connector.get_ohlcv(symbol=req.symbol, timeframe="1h", limit=200)
            side_for_sl = "long" if req.side == "buy" else "short"
            sl_data = SmartStopLoss.calculate(ohlcv, side_for_sl, price)
            stop_loss = sl_data["stop_loss"]
            take_profit = sl_data["take_profit"]
            sl_type = sl_data["sl_type"]
        except Exception as e:
            logger.warning(f"Smart SL calc failed, using pct fallback: {e}")
            stop_loss = SmartStopLoss.from_pct(price, "long" if req.side == "buy" else "short")
            take_profit = price * (1.04 if req.side == "buy" else 0.96)
            sl_type = "pct"

    # Clamp leverage to exchange limits for futures orders
    effective_leverage = req.leverage
    if req.trade_type == "futures" and req.leverage:
        try:
            connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            _, max_lever = await connector.get_max_leverage(req.symbol)
            effective_leverage = BitgetConnector.clamp_leverage(req.leverage, max_lever)
            if effective_leverage != req.leverage:
                logger.info(
                    f"[SIM API] Leverage clamped for {req.symbol}: "
                    f"{req.leverage}x → {effective_leverage}x (max={max_lever}x)"
                )
        except Exception as e:
            logger.warning(f"[SIM API] Could not fetch max leverage for {req.symbol}: {e}")
            effective_leverage = min(req.leverage, 10)

    result = await SimulationEngine.place_order(
        db=db,
        symbol=req.symbol,
        side=req.side,
        amount=order_amount,
        price=price,
        order_type=req.order_type,
        signal_id=req.signal_id,
        stop_loss=stop_loss,
        take_profit=take_profit,
        sl_type=sl_type,
        trade_type=req.trade_type,
        margin_mode=req.margin_mode,
        leverage=effective_leverage,
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Order failed"))

    return result


@router.get("/leverage-limits")
async def get_leverage_limits():
    """Get per-pair leverage limits from Bitget contracts."""
    try:
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        # Force refresh if cache is empty
        if not BitgetConnector._leverage_cache:
            await connector._refresh_leverage_cache()
        return {
            "limits": {
                sym: {"min": lev[0], "max": lev[1]}
                for sym, lev in BitgetConnector._leverage_cache.items()
                if "/" in sym  # Only return ccxt-style symbols
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
async def get_positions(
    status: str = "open",
    db: AsyncSession = Depends(get_db),
):
    """Get simulation positions (open or closed)."""
    positions = await SimulationEngine.get_positions(db, status)
    return {"positions": positions, "count": len(positions)}


@router.get("/orders")
async def get_orders(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Get simulation order history."""
    orders = await SimulationEngine.get_orders(db, limit)
    return {"orders": orders, "count": len(orders)}


@router.post("/check-sl-tp")
async def check_sl_tp(db: AsyncSession = Depends(get_db)):
    """Check all open positions for stop-loss / take-profit hits."""
    closed = await SimulationEngine.check_stop_loss_take_profit(db)
    return {"closed": closed, "count": len(closed)}


@router.post("/backfill-sl-tp")
async def backfill_sl_tp(db: AsyncSession = Depends(get_db)):
    """Manually trigger SL/TP backfill for all sim positions missing them."""
    result = await SimulationEngine.backfill_sl_tp(db)
    return {"backfilled": result, "count": len(result)}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int, db: AsyncSession = Depends(get_db)):
    """Cancel a sim order — closes associated position and refunds margin."""
    result = await SimulationEngine.cancel_order(db, order_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Cancel failed"))
    return result


@router.post("/positions/{position_id}/close")
async def close_position(position_id: int, db: AsyncSession = Depends(get_db)):
    """Close a single open sim position at market price."""
    result = await SimulationEngine.close_position(db, position_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Close failed"))
    return result


@router.post("/positions/close-all")
async def close_all_positions(db: AsyncSession = Depends(get_db)):
    """Close all open sim positions at market prices."""
    result = await SimulationEngine.close_all_positions(db)
    return result


# ── Auto-Trade ──────────────────────────────────────────────

@router.post("/auto-trade/cycle")
async def run_auto_trade_cycle(db: AsyncSession = Depends(get_db)):
    """
    Run one auto-trade cycle:
    - Checks SL/TP on open positions
    - Generates signals for configured pairs
    - Places orders for actionable signals
    """
    result = await SimulationEngine.auto_trade_cycle(db)
    return result


# ── Auto-Trade Persistent Loop ──────────────────────────────

@router.post("/auto-trade/loop/start")
async def start_loop(interval: int = 60):
    """Start the persistent backend auto-trade loop."""
    started = start_auto_trade_loop(interval)
    if not started:
        return {"status": "already_running", **get_auto_trade_loop_status()}
    return {"status": "started", **get_auto_trade_loop_status()}


@router.post("/auto-trade/loop/stop")
async def stop_loop():
    """Stop the persistent backend auto-trade loop."""
    stopped = stop_auto_trade_loop()
    if not stopped:
        return {"status": "not_running", **get_auto_trade_loop_status()}
    return {"status": "stopped", **get_auto_trade_loop_status()}


@router.get("/auto-trade/loop/status")
async def loop_status():
    """Get the current state of the auto-trade loop."""
    return get_auto_trade_loop_status()
