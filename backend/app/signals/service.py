"""
Signal Processing Service
Handles signal validation, storage, and processing
"""
import json
from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.models.database import Signal, SignalStatus, SignalSource, SignalAction
from app.core.timezone import now_sast
from app.models.schemas import SignalCreate
from loguru import logger


class SignalService:
    """Service for processing trading signals"""
    
    @staticmethod
    async def create_signal(db: AsyncSession, signal_data: SignalCreate) -> Signal:
        """
        Create a new signal
        
        Args:
            db: Database session
            signal_data: Signal creation data
        
        Returns:
            Created signal
        """
        signal = Signal(
            source=signal_data.source,
            symbol=signal_data.symbol,
            action=signal_data.action,
            price=signal_data.price,
            timeframe=signal_data.timeframe,
            strength=signal_data.strength,
            confidence=signal_data.confidence,
            raw_data=signal_data.raw_data,
            indicators=signal_data.indicators,
            status=SignalStatus.PENDING,
        )
        
        db.add(signal)
        await db.commit()
        await db.refresh(signal)
        
        logger.info(
            f"📊 New signal created: {signal.source.value} -> "
            f"{signal.action.value} {signal.symbol} @ {signal.price}"
        )
        
        return signal
    
    @staticmethod
    async def get_signal(db: AsyncSession, signal_id: int) -> Optional[Signal]:
        """Get signal by ID"""
        result = await db.execute(select(Signal).where(Signal.id == signal_id))
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_signals(
        db: AsyncSession,
        limit: int = 100,
        status: Optional[SignalStatus] = None,
        symbol: Optional[str] = None,
    ) -> List[Signal]:
        """
        Get signals with filters
        
        Args:
            db: Database session
            limit: Maximum number of results
            status: Filter by status
            symbol: Filter by symbol
        
        Returns:
            List of signals
        """
        query = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
        
        if status:
            query = query.where(Signal.status == status)
        if symbol:
            query = query.where(Signal.symbol == symbol)
        
        result = await db.execute(query)
        return result.scalars().all()
    
    @staticmethod
    async def update_signal_status(
        db: AsyncSession,
        signal_id: int,
        status: SignalStatus,
        error_message: Optional[str] = None,
    ) -> Optional[Signal]:
        """
        Update signal status
        
        Args:
            db: Database session
            signal_id: Signal ID
            status: New status
            error_message: Error message if failed
        
        Returns:
            Updated signal
        """
        signal = await SignalService.get_signal(db, signal_id)
        if not signal:
            return None
        
        signal.status = status
        signal.processed_at = now_sast() if status != SignalStatus.PENDING else None
        if error_message:
            signal.error_message = error_message
        
        await db.commit()
        await db.refresh(signal)
        
        logger.info(f"Signal {signal_id} status updated to {status.value}")
        return signal
    
    @staticmethod
    def validate_tradingview_payload(payload: dict) -> bool:
        """
        Validate TradingView webhook payload
        
        Args:
            payload: Webhook payload
        
        Returns:
            True if valid
        """
        required_fields = ["action", "symbol"]
        return all(field in payload for field in required_fields)
    
    @staticmethod
    async def process_tradingview_signal(
        db: AsyncSession,
        payload: dict
    ) -> Signal:
        """
        Process TradingView webhook signal
        
        Args:
            db: Database session
            payload: TradingView webhook payload
        
        Returns:
            Created signal
        """
        # Extract and normalize data
        signal_data = SignalCreate(
            source=SignalSource.TRADINGVIEW,
            symbol=payload.get("symbol"),
            action=SignalAction(payload.get("action")),
            price=payload.get("price"),
            timeframe=payload.get("timeframe"),
            strength=0.75,  # Default strength for TradingView signals
            confidence=0.80,  # Default confidence
            raw_data=json.dumps(payload),
            indicators=json.dumps(payload.get("indicator_values", {})),
        )
        
        return await SignalService.create_signal(db, signal_data)
