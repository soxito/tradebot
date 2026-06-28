"""
Pydantic schemas for signals and trading
"""
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from app.models.database import SignalAction, SignalSource, SignalStatus


class TradingViewWebhook(BaseModel):
    """TradingView webhook payload schema"""
    action: SignalAction
    symbol: str = Field(..., description="Trading pair, e.g., BTC/USDT")
    price: Optional[float] = Field(None, description="Current price")
    timeframe: Optional[str] = Field(None, description="Chart timeframe")
    indicator_values: Optional[Dict[str, Any]] = Field(None, description="Indicator values")
    timestamp: Optional[str] = None
    message: Optional[str] = None


class SignalCreate(BaseModel):
    """Signal creation schema"""
    source: SignalSource
    symbol: str
    action: SignalAction
    price: Optional[float] = None
    timeframe: Optional[str] = None
    strength: float = Field(0.5, ge=0.0, le=1.0)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    raw_data: Optional[str] = None
    indicators: Optional[str] = None


class SignalResponse(BaseModel):
    """Signal response schema"""
    id: int
    external_id: Optional[str]
    source: SignalSource
    symbol: str
    action: SignalAction
    price: Optional[float]
    timeframe: Optional[str]
    strength: float
    confidence: float
    status: SignalStatus
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class TradeCreate(BaseModel):
    """Trade creation schema"""
    exchange: str
    symbol: str
    side: str
    order_type: str
    amount: float
    price: Optional[float] = None
    signal_id: Optional[int] = None


class TradeResponse(BaseModel):
    """Trade response schema"""
    id: int
    exchange: str
    symbol: str
    side: str
    order_type: str
    amount: float
    price: Optional[float]
    status: str
    created_at: datetime
    
    class Config:
        from_attributes = True
