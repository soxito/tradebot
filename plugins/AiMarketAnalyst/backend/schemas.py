"""
AI Market Analyst Plugin — Pydantic Schemas
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


# ── Multi-provider AI accounts ─────────────────────────────

class ModelInfo(BaseModel):
    label: str
    context: int
    params: str
    speed: int
    strengths: List[str] = Field(default_factory=list)
    best_for: str = ""
    vision: bool = False
    reasoning: bool = False
    json_mode: bool = True
    cost: str = "free"
    notes: str = ""


class LLMProviderPreset(BaseModel):
    key: str
    label: str
    type: str
    base_url: str
    default_model: str
    models: List[str] = Field(default_factory=list)
    model_info: Dict[str, ModelInfo] = Field(default_factory=dict)
    free_tier: bool = True
    daily_limit: Optional[int] = None
    monthly_limit: Optional[int] = None
    signup_url: str
    notes: str = ""
    # When True, the UI shows an editable Base URL + free-text Model field
    # (FreeLLMAPI proxy / generic custom OpenAI-compatible endpoints).
    editable_endpoint: bool = False


class AIProviderCreate(BaseModel):
    provider_key: str
    label: Optional[str] = None
    api_key: str
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    type: Optional[str] = None
    enabled: bool = True
    priority: Optional[int] = None
    free_tier: Optional[bool] = None
    daily_limit: Optional[int] = None
    monthly_limit: Optional[int] = None


class AIProviderUpdate(BaseModel):
    label: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    daily_limit: Optional[int] = None
    monthly_limit: Optional[int] = None


class AIProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_key: str
    label: str
    type: str
    api_key_set: bool = False
    base_url: Optional[str]
    default_model: Optional[str]
    models: List[str] = Field(default_factory=list)
    model_info: Dict[str, ModelInfo] = Field(default_factory=dict)
    enabled: bool
    priority: int
    free_tier: bool
    status: str
    last_error: Optional[str]
    last_tested_at: Optional[datetime]
    last_model_used: Optional[str]
    total_calls: int
    total_errors: int
    daily_limit: Optional[int] = None
    monthly_limit: Optional[int] = None
    daily_calls: int = 0
    monthly_calls: int = 0
    daily_reset_at: Optional[datetime] = None
    monthly_reset_at: Optional[datetime] = None


class AIProviderTestResponse(BaseModel):
    ok: bool
    model: Optional[str] = None
    reply: Optional[str] = None
    error: Optional[str] = None


class AIProviderTestAllResult(BaseModel):
    id: int
    label: str
    ok: bool
    model: Optional[str] = None
    error: Optional[str] = None


class AIProviderTestAllResponse(BaseModel):
    tested: int
    ok_count: int
    results: List[AIProviderTestAllResult] = Field(default_factory=list)


class AIChatRequest(BaseModel):
    prompt: str
    system: Optional[str] = None
    json_mode: bool = False
    max_tokens: int = Field(default=600, ge=1, le=4000)


# ── Agent Schemas ──────────────────────────────────────────

class AIAgentCreate(BaseModel):
    name: str = Field(..., max_length=100)
    slug: str = Field(..., max_length=50, pattern=r"^[a-z0-9-]+$")
    description: Optional[str] = None
    role_type: str = "custom"
    model: str = "fable-5-high"
    reasoning_effort: str = "medium"
    verbosity: str = "medium"
    max_output_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    guardrails_json: Optional[Dict] = None
    tools_allowlist_json: Optional[List[str]] = None
    instruments_json: Optional[List[str]] = None
    timeframes_json: Optional[List[str]] = None
    indicators_json: Optional[List[str]] = None
    risk_profile_json: Optional[Dict] = None
    allowed_actions: str = "analyze"

class AIAgentResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    role_type: str
    is_enabled: bool
    model: str
    reasoning_effort: str
    verbosity: str
    max_output_tokens: Optional[int]
    instruments_json: Optional[List[str]]
    timeframes_json: Optional[List[str]]
    indicators_json: Optional[List[str]]
    allowed_actions: str
    version: int
    created_at: datetime

class AIAgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    role_type: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    verbosity: Optional[str] = None
    max_output_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    guardrails_json: Optional[Dict] = None
    tools_allowlist_json: Optional[List[str]] = None
    instruments_json: Optional[List[str]] = None
    timeframes_json: Optional[List[str]] = None
    indicators_json: Optional[List[str]] = None
    risk_profile_json: Optional[Dict] = None
    allowed_actions: Optional[str] = None


# ── Trade Settings ─────────────────────────────────────────

class AITradeSettingsUpdate(BaseModel):
    mode: Optional[str] = None
    lot_mode: Optional[str] = None
    lot_size: Optional[float] = None
    lot_min: Optional[float] = None
    lot_max: Optional[float] = None
    paper_mode: Optional[bool] = None
    auto_place: Optional[bool] = None
    max_open_positions: Optional[int] = None
    max_pending_orders: Optional[int] = None
    max_daily_loss_percent: Optional[float] = None
    trading_hours_json: Optional[List[Dict]] = None
    mt5_account_id: Optional[int] = None
    selected_agent_id: Optional[int] = None

class AITradeSettingsResponse(BaseModel):
    mode: str
    lot_mode: str
    lot_size: Optional[float]
    lot_min: Optional[float]
    lot_max: Optional[float]
    paper_mode: bool
    auto_place: bool
    max_open_positions: int
    max_pending_orders: int
    max_daily_loss_percent: Optional[float]
    mt5_account_id: Optional[int]
    selected_agent_id: Optional[int]


# ── Analysis Request/Response ──────────────────────────────

class AIAnalyzeRequest(BaseModel):
    symbol: str = Field(..., max_length=30)
    timeframe: str = Field(default="H1", max_length=10)

class AIProposeLimitRequest(BaseModel):
    symbol: str = Field(..., max_length=30)
    timeframe: str = Field(default="H1", max_length=10)

class AIPlaceLimitRequest(BaseModel):
    decision_id: int

class AICancelRequest(BaseModel):
    decision_id: int


# ── Decision Response ──────────────────────────────────────

class AIDecisionResponse(BaseModel):
    id: int
    ai_agent_id: Optional[int]
    symbol: str
    timeframe: Optional[str]
    decision_type: str
    direction: str
    entry_price: Optional[float]
    sl_price: Optional[float]
    tp_price: Optional[float]
    confidence: Optional[float]
    rationale: Optional[str]
    invalidation: Optional[str]
    signals_json: Optional[List[Dict]]
    status: str
    blocked_reasons_json: Optional[List[str]]
    mt5_order_id: Optional[str]
    created_at: datetime


# ── Structured Model Output (enforced JSON schema) ────────

class AIModelSignal(BaseModel):
    name: str
    value: str
    weight: float = Field(ge=0, le=1)

class AIModelOutput(BaseModel):
    """
    Strict schema that the AI model MUST return.
    Validated server-side — reject anything that doesn't match.
    """
    action: str = Field(..., pattern=r"^(analyze|propose_limit)$")
    direction: str = Field(..., pattern=r"^(buy|sell|none)$")
    confidence: float = Field(ge=0, le=100)
    levels: Dict[str, Optional[float]]  # {entry, sl, tp}
    timeframe: str
    signals: List[AIModelSignal] = []
    rationale: str = ""
    invalidation: str = ""
    notes: List[str] = []


# ── Chart Overlay (reuse MT5 format) ──────────────────────

class AIOverlayLine(BaseModel):
    price: float
    color: str
    lineWidth: int = 1
    lineStyle: int = 2
    title: str = ""

class AIOverlayResponse(BaseModel):
    proposed_entry: Optional[AIOverlayLine] = None
    sl_line: Optional[AIOverlayLine] = None
    tp_line: Optional[AIOverlayLine] = None
    status: str = "drafted"
    direction: str = "none"
    confidence: Optional[float] = None


# ── LLM Providers / Usage ─────────────────────────────────

class LLMProviderResponse(BaseModel):
    id: str
    label: str
    type: str
    enabled: bool
    models: List[str]
    rate_limits: Dict[str, int] = {}
    circuit: Dict[str, Any] = {}


class LLMUsageBlock(BaseModel):
    usage: Dict[str, int]
    limits: Dict[str, Optional[int]]
    remaining: Dict[str, Optional[int]]


class LLMProviderUsage(LLMUsageBlock):
    id: str
    label: str


class LLMUsageResponse(BaseModel):
    total: LLMUsageBlock
    providers: List[LLMProviderUsage]
