"""
Decision Engine
Combines signals from multiple sources and makes trading decisions
"""
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.database import Signal, SentimentScore, Trade, SignalAction
from app.trading.risk import risk_calculator
from app.sentiment.service import SentimentService
from app.core.timezone import now_sast
from loguru import logger


class TradingDecision:
    """Trading decision data class"""
    
    def __init__(
        self,
        action: str,
        symbol: str,
        confidence: float,
        should_execute: bool,
        reasons: List[str],
        signal_sources: List[str],
        position_size: Optional[Dict] = None,
        sentiment_score: Optional[float] = None,
    ):
        self.action = action
        self.symbol = symbol
        self.confidence = confidence
        self.should_execute = should_execute
        self.reasons = reasons
        self.signal_sources = signal_sources
        self.position_size = position_size
        self.sentiment_score = sentiment_score
        self.timestamp = now_sast()
    
    def to_dict(self) -> Dict:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "confidence": self.confidence,
            "should_execute": self.should_execute,
            "reasons": self.reasons,
            "signal_sources": self.signal_sources,
            "position_size": self.position_size,
            "sentiment_score": self.sentiment_score,
            "timestamp": self.timestamp.isoformat(),
        }


class DecisionEngine:
    """
    Core decision engine that combines signals and makes trading decisions
    """
    
    @staticmethod
    async def get_recent_signals(
        db: AsyncSession,
        symbol: str,
        hours: int = 1
    ) -> List[Signal]:
        """Get recent signals for a symbol"""
        cutoff = now_sast() - timedelta(hours=hours)
        result = await db.execute(
            select(Signal)
            .where(Signal.symbol == symbol)
            .where(Signal.created_at >= cutoff)
            .order_by(Signal.created_at.desc())
        )
        return result.scalars().all()
    
    @staticmethod
    async def get_current_exposure(db: AsyncSession, exchange: str) -> float:
        """
        Calculate current total exposure across all open positions
        
        Args:
            db: Database session
            exchange: Exchange name
        
        Returns:
            Total exposure in USD
        """
        result = await db.execute(
            select(func.sum(Trade.amount * Trade.price))
            .where(Trade.exchange == exchange)
            .where(Trade.status == "open")
        )
        exposure = result.scalar() or 0.0
        return float(exposure)
    
    @staticmethod
    async def evaluate_signal(
        db: AsyncSession,
        signal: Signal,
        account_balance: float = 10000.0,  # Default for testing
        exchange: str = "binance",
    ) -> TradingDecision:
        """
        Evaluate a signal and decide whether to execute
        
        Args:
            db: Database session
            signal: Trading signal to evaluate
            account_balance: Available account balance
            exchange: Target exchange
        
        Returns:
            Trading decision
        """
        logger.info(f"🤔 Evaluating signal: {signal.action.value} {signal.symbol}")
        
        reasons = []
        signal_sources = [signal.source.value]
        
        # Get sentiment score for the symbol
        base_symbol = signal.symbol.split("/")[0] if "/" in signal.symbol else signal.symbol
        sentiment = await SentimentService.get_latest_sentiment(db, base_symbol)
        sentiment_score = sentiment.score if sentiment else 0.0
        
        # Calculate combined confidence
        base_confidence = signal.confidence
        
        # Adjust confidence based on sentiment alignment
        if sentiment:
            # If signal and sentiment align, boost confidence
            if (signal.action == SignalAction.BUY and sentiment_score > 0) or \
               (signal.action == SignalAction.SELL and sentiment_score < 0):
                confidence_boost = abs(sentiment_score) * 0.2  # Up to 20% boost
                base_confidence = min(1.0, base_confidence + confidence_boost)
                reasons.append(f"Sentiment aligns with signal (+{confidence_boost:.1%})")
            else:
                confidence_penalty = abs(sentiment_score) * 0.15
                base_confidence = max(0.0, base_confidence - confidence_penalty)
                reasons.append(f"Sentiment conflicts with signal (-{confidence_penalty:.1%})")
        
        # Calculate position size
        position_size = risk_calculator.calculate_position_size(
            account_balance=account_balance,
            entry_price=signal.price or 0,
            stop_loss_price=None,  # Would be calculated from signal
        )
        
        # Get current exposure
        current_exposure = await DecisionEngine.get_current_exposure(db, exchange)
        
        # Validate trade
        validation = risk_calculator.validate_trade(
            position_size_usd=position_size["position_size_usd"],
            current_exposure_usd=current_exposure,
            signal_confidence=base_confidence,
            signal_strength=signal.strength,
        )
        
        should_execute = validation["is_valid"]
        reasons.extend(validation["reasons"])
        
        # Additional checks for sell signals
        if signal.action == SignalAction.SELL:
            # Check if we have an open position to sell
            # TODO: Query open positions
            pass
        
        decision = TradingDecision(
            action=signal.action.value,
            symbol=signal.symbol,
            confidence=base_confidence,
            should_execute=should_execute,
            reasons=reasons,
            signal_sources=signal_sources,
            position_size=position_size if should_execute else None,
            sentiment_score=sentiment_score,
        )
        
        logger.info(
            f"📊 Decision: {'EXECUTE' if should_execute else 'REJECT'} - "
            f"Confidence: {base_confidence:.2%}"
        )
        
        return decision
    
    @staticmethod
    async def should_take_action(
        db: AsyncSession,
        symbol: str,
        lookback_hours: int = 1,
    ) -> Optional[TradingDecision]:
        """
        Analyze recent signals for a symbol and decide if action should be taken
        
        Args:
            db: Database session
            symbol: Trading pair to analyze
            lookback_hours: Hours to look back for signals
        
        Returns:
            Trading decision if action should be taken, None otherwise
        """
        # Get recent signals
        signals = await DecisionEngine.get_recent_signals(db, symbol, lookback_hours)
        
        if not signals:
            logger.info(f"No recent signals for {symbol}")
            return None
        
        # Get the most recent signal
        latest_signal = signals[0]
        
        # Evaluate the signal
        decision = await DecisionEngine.evaluate_signal(db, latest_signal)
        
        return decision if decision.should_execute else None
