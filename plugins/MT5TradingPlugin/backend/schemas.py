"""
MT5 Trading Plugin — Pydantic Schemas

Request/response models for all MT5 plugin API endpoints.
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ── Account Schemas ────────────────────────────────────────

class MT5AccountCreate(BaseModel):
    name: str = Field(..., max_length=100)
    server: str = Field(..., max_length=200)
    login: str = Field(..., max_length=50)
    password: str = Field(..., min_length=1)
    account_type: str = Field(default="demo", pattern=r"^(live|demo|prop)$")

class MT5AccountResponse(BaseModel):
    id: int
    name: str
    server: str
    login: str
    status: str
    account_type: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: Optional[float]
    floating_pnl: float
    currency: str
    leverage: int
    api_reachable: bool
    last_sync_at: Optional[datetime]
    created_at: datetime

class MT5AccountUpdate(BaseModel):
    name: Optional[str] = None
    server: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None
    account_type: Optional[str] = None


# ── Order Schemas ──────────────────────────────────────────

class MT5OrderResponse(BaseModel):
    id: int
    account_id: int
    mt5_ticket: int
    symbol: str
    order_type: str
    volume: float
    price: float
    sl: Optional[float]
    tp: Optional[float]
    status: str
    comment: Optional[str]
    expiration: Optional[datetime]
    created_at: datetime

class MT5PlaceOrderRequest(BaseModel):
    account_id: int
    symbol: str
    order_type: str = Field(..., pattern=r"^(buy_limit|sell_limit|buy_stop|sell_stop)$")
    volume: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    expiration: Optional[datetime] = None

class MT5ModifyOrderRequest(BaseModel):
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    expiration: Optional[datetime] = None


# ── Position Schemas ───────────────────────────────────────

class MT5PositionResponse(BaseModel):
    id: int
    account_id: int
    mt5_ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    price_current: Optional[float]
    sl: Optional[float]
    tp: Optional[float]
    swap: float
    profit: float
    commission: float
    comment: Optional[str]
    mt5_time_open: Optional[datetime]
    created_at: datetime
    # Computed risk metrics
    rr_ratio: Optional[float] = None
    risk_pips: Optional[float] = None
    reward_pips: Optional[float] = None


# ── Deal Schemas ───────────────────────────────────────────

class MT5DealResponse(BaseModel):
    id: int
    account_id: int
    mt5_ticket: int
    symbol: Optional[str]
    deal_type: str
    volume: Optional[float]
    price: Optional[float]
    profit: float
    commission: float
    swap: float
    mt5_time: Optional[datetime]


# ── Group Schemas ──────────────────────────────────────────

class MT5GroupCreate(BaseModel):
    name: str = Field(..., max_length=100)
    account_ids: List[int] = Field(default_factory=list)
    is_default: bool = False

class MT5GroupResponse(BaseModel):
    id: int
    name: str
    is_default: bool
    accounts: List[MT5AccountResponse] = []
    # Aggregated totals
    total_balance: float = 0.0
    total_equity: float = 0.0
    total_floating_pnl: float = 0.0
    total_margin: float = 0.0
    created_at: datetime

class MT5GroupMemberUpdate(BaseModel):
    account_ids: List[int]
    weights: Optional[List[float]] = None


# ── Snapshot Schemas ───────────────────────────────────────

class MT5SnapshotResponse(BaseModel):
    time: str  # ISO date for TradingView chart compatibility
    equity: float
    balance: float
    floating_pnl: float


# ── Replay Schemas ─────────────────────────────────────────

class MT5ReplayRequest(BaseModel):
    account_id: Optional[int] = None
    group_id: Optional[int] = None
    date_from: datetime
    date_to: datetime
    symbol_filter: Optional[List[str]] = None

class MT5ReplayResponse(BaseModel):
    id: int
    status: str
    total_trades: int
    total_pnl: float
    max_drawdown: float
    win_rate: float
    sharpe_ratio: Optional[float]
    equity_curve: Optional[List[Dict[str, Any]]]
    created_at: datetime


# ── Copy-Trading Schemas ───────────────────────────────────

class MT5CopyProfileCreate(BaseModel):
    name: str = Field(..., max_length=100)
    source_account_id: Optional[int] = None
    source_group_id: Optional[int] = None
    allocation_mode: str = "fixed_lot"
    allocation_value: float = 0.01
    max_open_positions: int = 5
    symbol_whitelist: Optional[List[str]] = None

class MT5CopyProfileResponse(BaseModel):
    id: int
    name: str
    source_account_id: Optional[int]
    source_group_id: Optional[int]
    allocation_mode: str
    allocation_value: float
    max_open_positions: int
    symbol_whitelist: Optional[List[str]]
    enabled: bool
    paper_balance: float
    paper_equity: float
    created_at: datetime

class MT5CopySimTradeResponse(BaseModel):
    id: int
    symbol: str
    side: str
    qty_sim: float
    entry_time: Optional[datetime]
    entry_price: Optional[float]
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    pnl_sim: float
    status: str


# ── Risk Metrics Schemas ───────────────────────────────────

class RiskRatioResponse(BaseModel):
    """R:R overlay data for a single position."""
    position_id: int
    symbol: str
    side: str
    entry: float
    sl: Optional[float]
    tp: Optional[float]
    risk_pips: Optional[float]
    reward_pips: Optional[float]
    rr_ratio: Optional[float]

class ExposureHeatmapCell(BaseModel):
    symbol: str
    side: str
    notional: float
    margin_used: float
    account_id: Optional[int] = None

class PnLHeatmapCell(BaseModel):
    bucket: str  # e.g. "Mon", "09:00"
    avg_pnl: float
    win_rate: float
    trade_count: int

class MT5RiskOverviewResponse(BaseModel):
    positions_rr: List[RiskRatioResponse] = []
    exposure_heatmap: List[ExposureHeatmapCell] = []
    pnl_by_hour: List[PnLHeatmapCell] = []
    pnl_by_weekday: List[PnLHeatmapCell] = []


# ── Chart Overlay Schemas ──────────────────────────────────

class ChartMarker(BaseModel):
    """TradingView Lightweight Charts marker format."""
    time: str
    position: str = "aboveBar"  # aboveBar | belowBar | inBar
    color: str = "#2196F3"
    shape: str = "circle"  # circle | square | arrowUp | arrowDown
    text: str = ""

class ChartPriceLine(BaseModel):
    """TradingView Lightweight Charts price line."""
    price: float
    color: str = "#4CAF50"
    lineWidth: int = 1
    lineStyle: int = 2  # 0=solid 1=dotted 2=dashed 3=lg-dashed
    title: str = ""

class MT5OverlayResponse(BaseModel):
    """All overlay data for the chart."""
    orders: List[ChartPriceLine] = []
    positions: List[ChartPriceLine] = []
    sl_tp_lines: List[ChartPriceLine] = []
    execution_markers: List[ChartMarker] = []


# ── OHLCV / Candles Schemas ──────────────────────────────

class MT5CandleResponse(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

class MT5CandlesResponse(BaseModel):
    symbol: str
    timeframe: str
    candles: List[MT5CandleResponse]

class MT5PriceResponse(BaseModel):
    symbol: str
    bid: float
    ask: float
    time: int


# ── Trading Schemas ───────────────────────────────────────────────

class MT5PlaceMarketOrderRequest(BaseModel):
    """Place a market (Buy/Sell) or pending order."""
    account_id: int
    symbol: str
    operation: str = Field(..., pattern=r"^(buy|sell|buy_limit|sell_limit|buy_stop|sell_stop)$")
    volume: float = Field(..., gt=0)
    price: Optional[float] = None   # required for pending orders
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None

class MT5ClosePositionRequest(BaseModel):
    account_id: int
    ticket: int
    volume: Optional[float] = None  # None = full close

class MT5TradeResultResponse(BaseModel):
    success: bool
    ticket: Optional[int] = None
    message: Optional[str] = None
    raw: Optional[Any] = None


# ── Symbol Info Schemas ────────────────────────────────────────────

class MT5SymbolInfo(BaseModel):
    name: str
    digits: Optional[int] = None
    tick_size: Optional[float] = None
    contract_size: Optional[float] = None
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None
    volume_step: Optional[float] = None
    currency_base: Optional[str] = None
    currency_profit: Optional[str] = None


# ── Equity / Stats Schemas ─────────────────────────────────────────

class MT5EquityPoint(BaseModel):
    time: int    # Unix seconds
    equity: float
    balance: Optional[float] = None


# ── SMC Sniper Strategy Schemas ────────────────────────────────────

class MT5SmcSignal(BaseModel):
    side: str
    order_type: str
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    confidence: float
    reason: str
    zone_kind: str
    formed_index: int
    formed_time: int
    confluence: List[str] = []
    # Laddered take-profits + US session tag
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    in_us_session: bool = False
    # Balance-aware position sizing (0 when no account balance was supplied)
    lot: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    reward_amount: float = 0.0
    risk_exceeds_cap: bool = False
    # Pip / point geometry (MT5-accurate)
    point_size: float = 0.0
    pip_size: float = 0.0
    contract_size: float = 0.0
    sl_points: float = 0.0
    tp_points: float = 0.0
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    pip_value: float = 0.0


class MT5SmcZone(BaseModel):
    kind: str
    top: float
    bottom: float
    time: int
    index: int


class MT5SmcAnalyzeResponse(BaseModel):
    symbol: str
    timeframe: str
    bias: Optional[str] = None
    last_price: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    rsi: Optional[float] = None
    volume_z: Optional[float] = None
    momentum: Optional[str] = None
    equilibrium: Optional[float] = None
    range: Optional[Dict[str, float]] = None
    structure_events: List[Dict[str, Any]] = []
    liquidity: Dict[str, List[float]] = {}
    zones: List[MT5SmcZone] = []
    signals: List[MT5SmcSignal] = []
    us_session: Optional[Dict[str, Any]] = None
    ai: Optional[Dict[str, Any]] = None
    kronos: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class MT5BacktestRequest(BaseModel):
    account_id: int
    symbol: str
    timeframe: str = Field(default="H1")
    count: int = Field(default=600, ge=80, le=1000)
    min_rr: float = Field(default=1.5, ge=1.0, le=10.0)
    max_rr: float = Field(default=3.0, ge=1.0, le=10.0)
    sl_buffer_atr: float = Field(default=1.0, ge=0.0, le=3.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    expiry_bars: int = Field(default=24, ge=2, le=200)


class MT5BacktestResponse(BaseModel):
    symbol: str
    timeframe: str
    stats: Dict[str, Any]
    trades: List[Dict[str, Any]]
    error: Optional[str] = None
    ai: Optional[Dict[str, Any]] = None


class MT5SmcPlaceRequest(BaseModel):
    account_id: int
    symbol: str
    side: str = Field(..., pattern=r"^(buy|sell)$")
    entry: float = Field(..., gt=0)
    stop_loss: float = Field(..., gt=0)
    take_profit: float = Field(..., gt=0)
    volume: float = Field(..., gt=0)
    comment: Optional[str] = Field(default="SMC sniper")


class MT5CandleInput(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = 0.0


class MT5SmcAnalyzeDataRequest(BaseModel):
    """Run SMC analysis on caller-supplied candles (source-agnostic)."""
    symbol: str
    timeframe: str = Field(default="H1")
    min_rr: float = Field(default=1.5, ge=1.0, le=10.0)
    max_rr: float = Field(default=3.0, ge=1.0, le=10.0)
    sl_buffer_atr: float = Field(default=1.0, ge=0.0, le=3.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    use_ai: bool = Field(default=True)
    # Portfolio / position-sizing (0 balance disables lot/risk output)
    account_balance: float = Field(default=0.0, ge=0.0)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.05, le=20.0)
    contract_size: float = Field(default=0.0, ge=0.0)  # 0 = auto from symbol
    max_total_loss: float = Field(default=0.0, ge=0.0)        # hard loss cap ($)
    daily_profit_target_pct: float = Field(default=0.0, ge=0.0, le=1000.0)
    us_session_only: bool = Field(default=False)
    candles: List[MT5CandleInput] = Field(default_factory=list)


class MT5BacktestDataRequest(BaseModel):
    """Backtest the SMC model on caller-supplied candles (source-agnostic)."""
    symbol: str
    timeframe: str = Field(default="H1")
    min_rr: float = Field(default=1.5, ge=1.0, le=10.0)
    max_rr: float = Field(default=3.0, ge=1.0, le=10.0)
    sl_buffer_atr: float = Field(default=1.0, ge=0.0, le=3.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    expiry_bars: int = Field(default=24, ge=2, le=200)
    # Portfolio / equity simulation
    starting_balance: float = Field(default=0.0, ge=0.0)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.05, le=20.0)
    contract_size: float = Field(default=0.0, ge=0.0)  # 0 = auto from symbol
    recovery_enabled: bool = Field(default=True)
    max_risk_multiplier: float = Field(default=3.0, ge=1.0, le=10.0)
    max_total_loss: float = Field(default=0.0, ge=0.0)        # hard loss cap ($)
    daily_profit_target_pct: float = Field(default=0.0, ge=0.0, le=1000.0)
    use_ai: bool = Field(default=True)  # AI backtest analysis
    candles: List[MT5CandleInput] = Field(default_factory=list)
