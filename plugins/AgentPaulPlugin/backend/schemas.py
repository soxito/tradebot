"""
Agent Paul Plugin — Pydantic Schemas
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Settings ───────────────────────────────────────────────


class PaulSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = None  # paper | tradebot_execute | paul_execute
    require_approval: Optional[bool] = None
    kill_switch: Optional[bool] = None
    default_timeframe: Optional[str] = None
    min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    allowed_symbols: Optional[str] = None
    risk_max_position_usdt: Optional[float] = Field(None, ge=0.0)
    risk_max_open_positions: Optional[int] = Field(None, ge=0)
    max_queue_size: Optional[int] = Field(None, ge=0)
    cooldown_minutes: Optional[int] = Field(None, ge=0)
    mt5_default_account_id: Optional[int] = None
    mt5_default_volume: Optional[float] = Field(None, ge=0.0)
    mt5_timeframe: Optional[str] = None
    mt5_min_rr: Optional[float] = Field(None, ge=0.0)


class PaulSettingsResponse(BaseModel):
    enabled: bool
    mode: str
    require_approval: bool
    kill_switch: bool
    default_timeframe: str
    min_confidence: float
    allowed_symbols: Optional[str]
    risk_max_position_usdt: float
    risk_max_open_positions: int
    max_queue_size: int
    cooldown_minutes: int


# ── Decisions ──────────────────────────────────────────────


class PaulDecideRequest(BaseModel):
    symbol: str
    timeframe: Optional[str] = None
    trigger: str = "manual"
    market: str = "crypto"  # crypto | mt5
    account_id: Optional[int] = None  # required for market == 'mt5' (else uses default)


class PaulUnifyRequest(BaseModel):
    outcome: str  # win | loss | break_even | open
    pnl: Optional[float] = None
    notes: Optional[str] = None


class PaulDecisionResponse(BaseModel):
    id: int
    session_id: Optional[str]
    symbol: str
    timeframe: str
    trigger: str
    mode: str
    provenance: str
    action: str
    market: str
    account_id: Optional[int]
    volume: Optional[float]
    confidence: float
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    reasoning: Optional[str]
    acceptance_criteria: Optional[List[Dict[str, Any]]]
    qualify_status: str
    qualify_notes: Optional[str]
    status: str
    signal_id: Optional[int]
    execution_result: Optional[Dict[str, Any]]
    error: Optional[str]
    outcome: Optional[str]
    outcome_pnl: Optional[float]
    unify_notes: Optional[str]
    unified_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
