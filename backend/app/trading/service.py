"""
Trading Execution Service
Handles order placement and position management
"""
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Trade, Signal, SignalStatus
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.base import OrderSide, OrderType
from app.trading.decision import TradingDecision
from app.signals.service import SignalService
from app.monitoring.alerts import AlertService
from app.monitoring.metrics import record_trade_execution
from loguru import logger


class TradingService:
    """Service for executing trades"""
    
    @staticmethod
    async def execute_decision(
        db: AsyncSession,
        decision: TradingDecision,
        exchange: SupportedExchange,
        signal_id: Optional[int] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a trading decision
        
        Args:
            db: Database session
            decision: Trading decision to execute
            exchange: Target exchange
            signal_id: ID of the signal that triggered this decision
            dry_run: If True, simulate execution without placing real order
        
        Returns:
            Execution result
        """
        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}Executing {decision.action} "
            f"{decision.symbol} on {exchange.value}"
        )
        
        # Get exchange connector
        connector = exchange_manager.get_exchange(exchange)
        if not connector:
            error_msg = f"Exchange {exchange.value} not initialized"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        # Validate decision
        if not decision.should_execute:
            return {
                "success": False,
                "error": "Decision should not be executed",
                "reasons": decision.reasons,
            }
        
        # Validate position size
        if not decision.position_size:
            return {"success": False, "error": "No position size calculated"}
        
        result = {}
        
        if dry_run:
            # Simulate execution
            logger.info(
                f"[DRY RUN] Would place {decision.action} order: "
                f"{decision.position_size['quantity']} {decision.symbol} "
                f"@ market price"
            )
            
            result = {
                "success": True,
                "dry_run": True,
                "action": decision.action,
                "symbol": decision.symbol,
                "quantity": decision.position_size["quantity"],
                "estimated_cost_usd": decision.position_size["position_size_usd"],
                "confidence": decision.confidence,
            }
            record_trade_execution(exchange.value, decision.action, "dry_run")
        else:
            # Execute real order
            try:
                # Determine order side
                side = OrderSide.BUY if decision.action == "buy" else OrderSide.SELL
                
                # Place market order
                order = await connector.create_order(
                    symbol=decision.symbol,
                    side=side,
                    order_type=OrderType.MARKET,
                    amount=decision.position_size["quantity"],
                )
                
                # Save trade to database
                trade = Trade(
                    exchange=exchange.value,
                    exchange_order_id=order.get("id"),
                    signal_id=signal_id,
                    symbol=decision.symbol,
                    side=decision.action,
                    order_type="market",
                    amount=decision.position_size["quantity"],
                    price=order.get("price"),
                    filled_amount=order.get("filled", 0),
                    average_price=order.get("average"),
                    status=order.get("status", "open"),
                    raw_response=str(order),
                )
                
                db.add(trade)
                await db.commit()
                await db.refresh(trade)
                
                # Update signal status
                if signal_id:
                    await SignalService.update_signal_status(
                        db, signal_id, SignalStatus.EXECUTED
                    )
                
                logger.info(f"✅ Order executed: {order.get('id')}")
                record_trade_execution(exchange.value, decision.action, "live")
                await AlertService.notify(
                    title="Trade executed",
                    message=f"{decision.action.upper()} {decision.symbol} executed on {exchange.value}",
                    level="WARNING",
                    details={
                        "exchange_order_id": order.get("id"),
                        "quantity": decision.position_size["quantity"],
                    },
                )
                
                result = {
                    "success": True,
                    "dry_run": False,
                    "trade_id": trade.id,
                    "exchange_order_id": order.get("id"),
                    "order": order,
                }
            
            except Exception as e:
                logger.error(f"❌ Order execution failed: {e}")
                await AlertService.notify(
                    title="Trade execution failed",
                    message=f"{decision.action.upper()} {decision.symbol} failed on {exchange.value}",
                    level="ERROR",
                    details={"error": str(e)},
                )
                
                # Update signal status to failed
                if signal_id:
                    await SignalService.update_signal_status(
                        db, signal_id, SignalStatus.FAILED, error_message=str(e)
                    )
                
                result = {
                    "success": False,
                    "error": str(e),
                }
        
        return result
    
    @staticmethod
    async def get_trade_history(
        db: AsyncSession,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ):
        """Get trade history with filters"""
        from sqlalchemy import select, desc
        
        query = select(Trade).order_by(desc(Trade.created_at)).limit(limit)
        
        if exchange:
            query = query.where(Trade.exchange == exchange)
        if symbol:
            query = query.where(Trade.symbol == symbol)
        
        result = await db.execute(query)
        return result.scalars().all()
