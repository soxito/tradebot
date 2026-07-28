"""WhatsApp Signal & News Plugin Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# ── Base response models ──────────────────────────────────────────────


class WhatsAppBaseResponse(BaseModel):
    """Base response with common fields."""

    success: bool = True
    message: Optional[str] = None


# ── Plugin Settings ───────────────────────────────────────────────────


class WhatsAppPluginSettingsResponse(BaseModel):
    """Plugin settings response (safe - no secrets)."""

    openwa_base_url: Optional[str] = None
    openwa_api_key_set: bool = False
    default_session_name: Optional[str] = None
    webhook_secret_set: bool = False
    poll_interval_seconds: int = 300
    session_health_check_seconds: int = 60
    enable_llm_fallback: bool = False
    llm_model: Optional[str] = None
    max_messages_per_poll: int = 50
    message_dedupe_ttl_hours: int = 24

    # Sniper defaults
    sniper_enabled_default: bool = False
    sniper_mode_default: str = "sandbox"
    sniper_position_size_usdt_default: float = 100.0
    sniper_max_positions_default: int = 5
    sniper_min_confidence_default: float = 0.65
    sniper_min_risk_reward_default: float = 1.5


class WhatsAppPluginSettingsUpdate(BaseModel):
    """Plugin settings update request."""

    openwa_base_url: Optional[str] = None
    openwa_api_key: Optional[str] = None
    default_session_name: Optional[str] = None
    webhook_secret: Optional[str] = None
    poll_interval_seconds: Optional[int] = Field(None, ge=10, le=3600)
    session_health_check_seconds: Optional[int] = Field(None, ge=10, le=600)
    enable_llm_fallback: Optional[bool] = None
    llm_model: Optional[str] = None
    max_messages_per_poll: Optional[int] = Field(None, ge=1, le=200)
    message_dedupe_ttl_hours: Optional[int] = Field(None, ge=1, le=168)

    # Sniper defaults
    sniper_enabled_default: Optional[bool] = None
    sniper_mode_default: Optional[str] = None
    sniper_position_size_usdt_default: Optional[float] = Field(None, ge=1)
    sniper_max_positions_default: Optional[int] = Field(None, ge=1, le=50)
    sniper_min_confidence_default: Optional[float] = Field(None, ge=0, le=1)
    sniper_min_risk_reward_default: Optional[float] = Field(None, ge=0.1)


# ── Connection Test ───────────────────────────────────────────────────


class WhatsAppTestProviderResult(BaseModel):
    """Test result for a single provider."""

    provider: str
    ok: bool
    error: Optional[str] = None
    details: Optional[dict] = None


class WhatsAppTestConnectionResponse(BaseModel):
    """Test connection response."""

    results: List[WhatsAppTestProviderResult]
    any_ok: bool


# ── Session Management ────────────────────────────────────────────────


class WhatsAppSessionResponse(BaseModel):
    """WhatsApp session response."""

    id: str
    name: str
    status: str
    phone: Optional[str] = None
    profile_name: Optional[str] = None
    profile_pic: Optional[str] = None
    qr_code: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WhatsAppSessionCreateRequest(BaseModel):
    """Create session request."""

    name: str = Field(..., min_length=1, max_length=100)
    proxy: Optional[str] = None


class WhatsAppSessionStartRequest(BaseModel):
    """Start session request."""

    session_id: str


class WhatsAppQRResponse(BaseModel):
    """QR code response."""

    session_id: str
    qr_code: str  # base64 encoded image
    qr_data: Optional[str] = None  # raw QR data


class WhatsAppAuthStatusResponse(BaseModel):
    """Authentication status response."""

    authenticated: bool
    session_id: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    status: str


# ── Channel Sources ───────────────────────────────────────────────────


class WhatsAppChannelKind(str):
    """Channel kind constants."""

    SIGNALS = "signals"
    NEWS = "news"
    VOLUME_ALERTS = "volume_alerts"


class WhatsAppSourceType(str):
    """Source type constants."""

    GROUP = "group"
    CONTACT = "contact"
    BROADCAST = "broadcast"
    COMMUNITY = "community"


class WhatsAppChannelSourceCreate(BaseModel):
    """Create channel source request."""

    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., pattern=r"^(signals|news|volume_alerts)$")
    source_type: str = Field(..., pattern=r"^(group|contact|broadcast|community)$")
    chat_id: str = Field(..., min_length=1, description="WhatsApp chat ID (e.g., 123456789@g.us)")
    session_id: Optional[str] = None
    is_active: bool = True
    description: Optional[str] = None

    # Signal parsing settings
    parse_signals: bool = True
    signal_format: Optional[str] = None  # Custom regex format
    default_leverage: Optional[int] = None
    default_tp_levels: Optional[List[float]] = None
    default_sl_pct: Optional[float] = None


class WhatsAppChannelSourceUpdate(BaseModel):
    """Update channel source request."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[str] = Field(None, pattern=r"^(signals|news|volume_alerts)$")
    source_type: Optional[str] = Field(None, pattern=r"^(group|contact|broadcast|community)$")
    chat_id: Optional[str] = None
    session_id: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None
    parse_signals: Optional[bool] = None
    signal_format: Optional[str] = None
    default_leverage: Optional[int] = None
    default_tp_levels: Optional[List[float]] = None
    default_sl_pct: Optional[float] = None


class WhatsAppChannelSourceResponse(BaseModel):
    """Channel source response."""

    id: int
    name: str
    kind: str
    source_type: str
    chat_id: str
    session_id: Optional[str]
    is_active: bool
    description: Optional[str]
    parse_signals: bool
    signal_format: Optional[str]
    default_leverage: Optional[int]
    default_tp_levels: Optional[List[float]]
    default_sl_pct: Optional[float]
    last_message_at: Optional[datetime]
    message_count: int
    created_at: datetime
    updated_at: datetime


# ── Message Ingest ────────────────────────────────────────────────────


class WhatsAppMessageResponse(BaseModel):
    """Ingested message response."""

    id: int
    channel_source_id: int
    channel_name: str
    whatsapp_message_id: str
    from_number: Optional[str]
    from_name: Optional[str]
    text: Optional[str]
    media_type: Optional[str]
    media_url: Optional[str]
    timestamp: datetime
    is_processed: bool
    parsed_signal_id: Optional[int]
    created_at: datetime


class WhatsAppPollRequest(BaseModel):
    """Manual poll request."""

    channel_source_id: Optional[int] = None
    limit: int = Field(default=50, ge=1, le=200)
    since_message_id: Optional[str] = None


class WhatsAppPollResult(BaseModel):
    """Poll result response."""

    polled_sources: int
    new_messages: int
    new_signals: int
    errors: List[str]


class WhatsAppPreviewRequest(BaseModel):
    """Preview channel messages request."""

    limit: int = Field(default=20, ge=1, le=100)
    since_message_id: Optional[str] = None


# ── Parsed Signals ────────────────────────────────────────────────────


class WhatsAppSignalDirection(str):
    """Signal direction constants."""

    BUY = "buy"
    SELL = "sell"
    LONG = "long"
    SHORT = "short"


class WhatsAppSignalStatus(str):
    """Signal status constants."""

    ACTIVE = "active"
    FILLED = "filled"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    CLOSED = "closed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class WhatsAppParsedSignalResponse(BaseModel):
    """Parsed signal response."""

    id: int
    channel_source_id: int
    channel_title: str
    whatsapp_message_id: str
    symbol: str
    direction: str
    leverage: Optional[int]
    entry: Optional[float]
    entry_raw: Optional[str]
    stop_loss: Optional[float]
    stop_loss_raw: Optional[str]
    trailing_sl: Optional[float]
    tp_reached_count: int
    market_type: str
    take_profits: List[float]
    status: str
    confidence: float
    raw_text: str
    posted_at: datetime
    created_at: datetime
    updated_at: datetime


# ── Monitor ───────────────────────────────────────────────────────────


class WhatsAppMonitorStatusResponse(BaseModel):
    """Monitor status response."""

    running: bool
    last_run: Optional[datetime]
    signals_monitored: int
    positions_checked: int
    errors: List[str]


# ── Sniper Settings ───────────────────────────────────────────────────


class WhatsAppSniperSettingsResponse(BaseModel):
    """Sniper settings response."""

    enabled: bool
    mode: str
    trade_type: str
    position_size_usdt: float
    max_positions: int
    max_positions_sandbox: int
    max_positions_live: int
    leverage: int
    margin_mode: str
    sniper_offset_pct: float
    min_confidence: float
    min_risk_reward: float
    pending_ttl_minutes: int
    reanalyze: bool
    execute_sandbox: bool
    execute_live: bool
    require_ai_confirmation: bool
    execute_immediately: bool
    skipped_reanalyze_minutes: int
    tp_trail_pct: float
    volume_channel_id: Optional[int]
    allowed_channel_ids: List[int]


class WhatsAppSniperSettingsUpdate(BaseModel):
    """Sniper settings update request."""

    enabled: Optional[bool] = None
    mode: Optional[str] = Field(None, pattern=r"^(sandbox|live|both)$")
    trade_type: Optional[str] = Field(None, pattern=r"^(spot|futures|both)$")
    position_size_usdt: Optional[float] = Field(None, ge=1)
    max_positions: Optional[int] = Field(None, ge=1, le=50)
    max_positions_sandbox: Optional[int] = Field(None, ge=1, le=50)
    max_positions_live: Optional[int] = Field(None, ge=1, le=20)
    leverage: Optional[int] = Field(None, ge=1, le=125)
    margin_mode: Optional[str] = Field(None, pattern=r"^(cross|isolated)$")
    sniper_offset_pct: Optional[float] = Field(None, ge=0, le=10)
    min_confidence: Optional[float] = Field(None, ge=0, le=1)
    min_risk_reward: Optional[float] = Field(None, ge=0.1)
    pending_ttl_minutes: Optional[int] = Field(None, ge=1, le=1440)
    reanalyze: Optional[bool] = None
    execute_sandbox: Optional[bool] = None
    execute_live: Optional[bool] = None
    require_ai_confirmation: Optional[bool] = None
    execute_immediately: Optional[bool] = None
    skipped_reanalyze_minutes: Optional[int] = Field(None, ge=1, le=1440)
    tp_trail_pct: Optional[float] = Field(None, ge=0, le=50)
    volume_channel_id: Optional[int] = None
    allowed_channel_ids: Optional[List[int]] = None


class WhatsAppSniperTradeResponse(BaseModel):
    """Sniper trade response."""

    id: int
    signal_id: int
    symbol: str
    direction: str
    entry_price: float
    stop_loss: Optional[float]
    take_profits: List[float]
    position_size_usdt: float
    leverage: int
    margin_mode: str
    status: str
    order_id: Optional[str]
    filled_price: Optional[float]
    filled_at: Optional[datetime]
    pnl_usdt: Optional[float]
    pnl_pct: Optional[float]
    reason: Optional[str]
    mode: str
    created_at: datetime
    updated_at: datetime


# ── Prices ────────────────────────────────────────────────────────────


class WhatsAppPricesResponse(BaseModel):
    """Live prices response."""

    prices: dict[str, float]


# ── Signal Analysis ───────────────────────────────────────────────────


class WhatsAppSignalAnalysisResponse(BaseModel):
    """Full signal analysis response."""

    signal_id: int
    symbol: str
    action: str
    confidence: float
    reasoning: str
    ai_calls: int
    technical_analysis: dict
    sentiment_analysis: Optional[dict]
    volume_analysis: Optional[dict]
    risk_metrics: dict
    order_params: Optional[dict]


# ── Volume Monitor ────────────────────────────────────────────────────


class WhatsAppVolumeMonitorResponse(BaseModel):
    """Volume monitor snapshot."""

    signals: List[dict]


# ── Discovered Chats ──────────────────────────────────────────────────


class WhatsAppDiscoveredChatResponse(BaseModel):
    """Discovered chat/channel response."""

    id: str
    name: str
    type: str  # group, contact, broadcast
    participant_count: Optional[int]
    is_read_only: bool
    last_message_at: Optional[datetime]


class WhatsAppSubscribedChatsResponse(BaseModel):
    """Subscribed chats response."""

    provider: str
    total_subscribed: int
    chats: List[WhatsAppDiscoveredChatResponse]


# ── Presets ───────────────────────────────────────────────────────────


class WhatsAppChannelPresetCreate(BaseModel):
    """Create channel preset request."""

    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., pattern=r"^(signals|news|volume_alerts)$")
    description: Optional[str] = None
    chat_ids: List[str] = Field(default_factory=list)
    default_settings: dict = Field(default_factory=dict)


class WhatsAppChannelPresetUpdate(BaseModel):
    """Update channel preset request."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    chat_ids: Optional[List[str]] = None
    default_settings: Optional[dict] = None


class WhatsAppChannelPresetResponse(BaseModel):
    """Channel preset response."""

    id: int
    name: str
    kind: str
    description: Optional[str]
    chat_ids: List[str]
    default_settings: dict
    created_at: datetime
    updated_at: datetime


class WhatsAppApplyPresetRequest(BaseModel):
    """Apply preset request."""

    user_id: str = "0"
    overwrite_existing: bool = False
    verify_on_create: bool = True


class WhatsAppApplyPresetResponse(BaseModel):
    """Apply preset response."""

    created: int
    updated: int
    errors: List[str]


# ── Webhook ───────────────────────────────────────────────────────────


class WhatsAppWebhookReceiveResponse(BaseModel):
    """Webhook receive response."""

    ok: bool
    processed: int
    errors: List[str]