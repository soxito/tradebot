"""
Risk Management Module
Calculates position sizes, validates trades against risk limits
"""
from typing import Dict, Optional
from pydantic import BaseModel
from loguru import logger

from app.core.config import settings
from app.utils.precision import smart_round


class RiskLimits(BaseModel):
    """Risk management limits"""
    max_position_size_usd: float = settings.MAX_POSITION_SIZE_USD
    max_risk_per_trade_percent: float = settings.MAX_RISK_PER_TRADE_PERCENT
    max_total_exposure_usd: float = settings.MAX_TOTAL_EXPOSURE_USD
    
    # Additional risk parameters
    max_daily_trades: int = 50
    max_concurrent_positions: int = 10
    min_confidence_threshold: float = 0.9
    min_strength_threshold: float = 0.3


class RiskCalculator:
    """Calculate position sizes and validate against risk limits"""
    
    def __init__(self, limits: Optional[RiskLimits] = None):
        """
        Initialize risk calculator
        
        Args:
            limits: Custom risk limits (uses defaults if None)
        """
        self.limits = limits or RiskLimits()
        logger.info(f"Risk calculator initialized with limits: {self.limits.dict()}")
    
    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss_price: Optional[float] = None,
        risk_percentage: Optional[float] = None,
    ) -> Dict[str, float]:
        """
        Calculate position size based on account balance and risk
        
        Args:
            account_balance: Total account balance in USD
            entry_price: Entry price for the trade
            stop_loss_price: Stop loss price (optional)
            risk_percentage: Risk per trade as percentage (uses default if None)
        
        Returns:
            Dictionary with position sizing information
        """
        # Use default risk percentage if not provided
        risk_pct = risk_percentage or self.limits.max_risk_per_trade_percent
        
        # Calculate risk amount in USD
        risk_amount_usd = account_balance * (risk_pct / 100.0)
        
        # If stop loss provided, calculate based on risk distance
        if stop_loss_price and entry_price != stop_loss_price:
            risk_distance_pct = abs((entry_price - stop_loss_price) / entry_price)
            position_size_usd = risk_amount_usd / risk_distance_pct
        else:
            # Default: use fixed percentage of balance
            position_size_usd = account_balance * 0.05  # 5% of balance
        
        # Apply maximum position size limit
        position_size_usd = min(position_size_usd, self.limits.max_position_size_usd)
        
        # Calculate quantity
        quantity = position_size_usd / entry_price
        
        return {
            "position_size_usd": round(position_size_usd, 2),
            "quantity": round(quantity, 8),
            "risk_amount_usd": round(risk_amount_usd, 2),
            "risk_percentage": risk_pct,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
        }
    
    def validate_trade(
        self,
        position_size_usd: float,
        current_exposure_usd: float,
        signal_confidence: float,
        signal_strength: float,
    ) -> Dict[str, any]:
        """
        Validate if trade passes risk checks
        
        Args:
            position_size_usd: Proposed position size in USD
            current_exposure_usd: Current total exposure in USD
            signal_confidence: Signal confidence score (0-1)
            signal_strength: Signal strength score (0-1)
        
        Returns:
            Dictionary with validation result and reasons
        """
        is_valid = True
        reasons = []
        
        # Check position size limit
        if position_size_usd > self.limits.max_position_size_usd:
            is_valid = False
            reasons.append(
                f"Position size ${position_size_usd:.2f} exceeds max "
                f"${self.limits.max_position_size_usd:.2f}"
            )
        
        # Check total exposure limit
        total_exposure = current_exposure_usd + position_size_usd
        if total_exposure > self.limits.max_total_exposure_usd:
            is_valid = False
            reasons.append(
                f"Total exposure ${total_exposure:.2f} would exceed max "
                f"${self.limits.max_total_exposure_usd:.2f}"
            )
        
        # Check signal confidence
        if signal_confidence < self.limits.min_confidence_threshold:
            is_valid = False
            reasons.append(
                f"Signal confidence {signal_confidence:.2f} below minimum "
                f"{self.limits.min_confidence_threshold:.2f}"
            )
        
        # Check signal strength
        if signal_strength < self.limits.min_strength_threshold:
            is_valid =False
            reasons.append(
                f"Signal strength {signal_strength:.2f} below minimum "
                f"{self.limits.min_strength_threshold:.2f}"
            )
        
        result = {
            "is_valid": is_valid,
            "reasons": reasons if not is_valid else ["All risk checks passed"],
            "details": {
                "position_size_usd": position_size_usd,
                "current_exposure_usd": current_exposure_usd,
                "total_exposure_usd": total_exposure,
                "signal_confidence": signal_confidence,
                "signal_strength": signal_strength,
            }
        }
        
        if is_valid:
            logger.info(f"✅ Trade validation PASSED")
        else:
            logger.warning(f"❌ Trade validation FAILED: {', '.join(reasons)}")
        
        return result
    
    def calculate_stop_loss(
        self,
        entry_price: float,
        side: str,
        risk_ratio: float = 0.02,  # 2% default
    ) -> float:
        """
        Calculate stop loss price
        
        Args:
            entry_price: Entry price
            side: Trade side ('buy' or 'sell')
            risk_ratio: Risk as ratio of entry price (e.g., 0.02 = 2%)
        
        Returns:
            Stop loss price
        """
        if side.lower() == "buy":
            stop_loss = entry_price * (1 - risk_ratio)
        else:  # sell
            stop_loss = entry_price * (1 + risk_ratio)
        
        return smart_round(stop_loss, entry_price)
    
    def calculate_take_profit(
        self,
        entry_price: float,
        side: str,
        reward_ratio: float = 0.04,  # 4% default (2:1 risk/reward)
    ) -> float:
        """
        Calculate take profit price
        
        Args:
            entry_price: Entry price
            side: Trade side ('buy' or 'sell')
            reward_ratio: Reward as ratio of entry price
        
        Returns:
            Take profit price
        """
        if side.lower() == "buy":
            take_profit = entry_price * (1 + reward_ratio)
        else:  # sell
            take_profit = entry_price * (1 - reward_ratio)
        
        return smart_round(take_profit, entry_price)


#Global risk calculator instance
risk_calculator = RiskCalculator()
