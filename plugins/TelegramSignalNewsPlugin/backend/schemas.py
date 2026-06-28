"""Telegram Signal & News Plugin Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SourceKindLiteral = Literal["signals", "news"]
ProviderLiteral = Literal["auto", "telethon", "bot_api", "telegram_mcp"]
MethodsTestModeLiteral = Literal["binding", "invoke_readonly"]


class TelegramChannelSourceCreate(BaseModel):
    user_id: str = Field(default="0", max_length=32)
    title: str | None = Field(default=None, max_length=200)
    channel_handle: str = Field(..., max_length=200)
    source_kind: SourceKindLiteral
    provider: ProviderLiteral = "auto"
    market_type: Literal["crypto", "forex"] = "crypto"

    poll_interval_seconds: int = Field(default=300, ge=60, le=3600)
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    language_hint: str | None = Field(default=None, max_length=20)

    verify_on_create: bool = True


class TelegramChannelSourceUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    source_kind: SourceKindLiteral | None = None
    provider: ProviderLiteral | None = None
    is_enabled: bool | None = None
    market_type: Literal["crypto", "forex"] | None = None

    poll_interval_seconds: int | None = Field(default=None, ge=60, le=3600)
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    language_hint: str | None = Field(default=None, max_length=20)


class TelegramChannelSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    channel_handle: str
    channel_id: str | None
    source_kind: SourceKindLiteral
    provider: ProviderLiteral | str
    is_enabled: bool
    market_type: str = "crypto"

    poll_interval_seconds: int
    include_keywords: list[str] | None
    exclude_keywords: list[str] | None
    language_hint: str | None

    last_message_id: str | None
    last_polled_at: datetime | None
    last_error: str | None

    created_at: datetime
    updated_at: datetime


class TelegramDiscoveredChannelResponse(BaseModel):
    title: str
    channel_handle: str
    channel_id: str | None = None
    provider: ProviderLiteral | str


class TelegramSubscribedChannelsResponse(BaseModel):
    provider: ProviderLiteral | str
    total_subscribed: int = Field(default=0, ge=0)
    channels: list[TelegramDiscoveredChannelResponse] = Field(default_factory=list)


class TelegramMethodDescriptor(BaseModel):
    name: str
    namespace: str | None = None
    provider_supported: list[str] = Field(default_factory=list)
    binding: str | None = None
    notes: str | None = None


class TelegramMethodsCatalogResponse(BaseModel):
    source_url: str
    total_methods: int = Field(ge=0)
    fetched_at: datetime
    methods: list[TelegramMethodDescriptor] = Field(default_factory=list)


class TelegramMethodsTestRequest(BaseModel):
    provider: ProviderLiteral = "auto"
    refresh: bool = False
    limit: int | None = Field(default=None, ge=1, le=5000)
    mode: MethodsTestModeLiteral = "binding"


class TelegramMethodTestResult(BaseModel):
    method: str
    provider: str
    ok: bool
    status: Literal["supported", "unsupported", "error"]
    message: str


class TelegramMethodsTestSummary(BaseModel):
    total_methods: int = Field(ge=0)
    tested_methods: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unsupported: int = Field(ge=0)


class TelegramMethodsTestResponse(BaseModel):
    source_url: str
    provider: str
    mode: MethodsTestModeLiteral
    readonly_allowlist: list[str] = Field(default_factory=list)
    summary: TelegramMethodsTestSummary
    results: list[TelegramMethodTestResult] = Field(default_factory=list)


class TelegramChannelPresetCreate(BaseModel):
    slug: str = Field(..., max_length=100, pattern=r"^[a-z0-9-]+$")
    name: str = Field(..., max_length=200)
    description: str | None = None
    source_kind: SourceKindLiteral
    channels: list[str] = Field(default_factory=list)
    is_public: bool = True


class TelegramChannelPresetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    source_kind: SourceKindLiteral | None = None
    channels: list[str] | None = None
    is_public: bool | None = None


class TelegramChannelPresetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None
    source_kind: SourceKindLiteral
    channels: list[str]
    is_public: bool
    created_at: datetime
    updated_at: datetime


class TelegramApplyPresetRequest(BaseModel):
    user_id: str = Field(default="0", max_length=32)
    overwrite_existing: bool = False
    verify_on_create: bool = True


class TelegramApplyPresetResponse(BaseModel):
    preset_id: int
    created_count: int
    skipped_count: int


class TelegramPollRequest(BaseModel):
    user_id: str = Field(default="0", max_length=32)
    channel_source_ids: list[int] | None = None
    limit_per_channel: int = Field(default=50, ge=1, le=200)


class TelegramPollResult(BaseModel):
    poll_run_id: int
    status: Literal["success", "partial", "failed"]
    channels_scanned: int
    messages_read: int
    messages_saved: int
    errors: list[dict[str, Any]]


class TelegramPreviewRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class TelegramIngestMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_source_id: int
    source_kind: SourceKindLiteral
    telegram_message_id: str
    posted_at: datetime | None
    author_name: str | None
    raw_text: str
    extraction_json: dict[str, Any] | None
    symbols_json: list[str] | None
    confidence: float | None
    created_at: datetime


class TelegramStatusResponse(BaseModel):
    plugin: str
    version: str
    providers: list[dict[str, Any]]
    channels_total: int
    channels_enabled: int
    messages_total: int


class TelegramExtractionResult(BaseModel):
    source_kind: SourceKindLiteral
    direction: Literal["buy", "sell", "neutral", "unknown"] = "unknown"
    symbols: list[str] = Field(default_factory=list)
    levels: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_signal: bool = False
    is_news: bool = False
    summary: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    provider: Literal["rules", "llm", "rules+llm"] = "rules"


class TelegramPluginSettingsUpdate(BaseModel):
    """Payload accepted by PUT /plugins/telegram/settings."""

    api_id: int | None = None
    api_hash: str | None = None
    phone_number: str | None = None
    bot_token: str | None = None
    mcp_chat_id: str | None = None
    label: str | None = None


class TelegramPluginSettingsResponse(BaseModel):
    """Settings returned by GET /plugins/telegram/settings."""

    api_id: int | None = None
    # api_hash and bot_token masked for security
    api_hash_set: bool = False
    phone_number: str | None = None
    bot_token_set: bool = False
    mcp_chat_id: str | None = None
    label: str | None = None


class TelegramTestProviderResult(BaseModel):
    """Single provider result for test-connection."""

    provider: str
    ok: bool
    message: str


class TelegramTestConnectionResponse(BaseModel):
    """Response from POST /plugins/telegram/test-connection."""

    results: list[TelegramTestProviderResult]
    any_ok: bool
    levels: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_signal: bool = False
    is_news: bool = False
    summary: str = ""


# ── Telethon account authentication ─────────────────────────────────────────

class TelegramAuthStartRequest(BaseModel):
    """POST /plugins/telegram/auth/start"""

    phone_number: str = Field(..., min_length=7, max_length=20)


class TelegramAuthStartResponse(BaseModel):
    phone_code_hash: str
    message: str


class TelegramAuthCompleteRequest(BaseModel):
    """POST /plugins/telegram/auth/complete"""

    phone_number: str = Field(..., min_length=7, max_length=20)
    phone_code_hash: str
    code: str = Field(..., min_length=3, max_length=20)
    password: str | None = None  # 2FA password, if needed


class TelegramAuthCompleteResponse(BaseModel):
    success: bool
    requires_2fa: bool = False
    message: str
    account: dict[str, Any] | None = None


class TelegramAuthStatusResponse(BaseModel):
    authenticated: bool
    provider: str = "telethon"
    phone_number: str | None = None
    username: str | None = None
    first_name: str | None = None


# ── Parsed signals & monitor ────────────────────────────────────────────────

SignalStatusLiteral = Literal["active", "filled", "tp_hit", "sl_hit", "closed"]


class TelegramParsedSignalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_source_id: int
    channel_title: str | None
    telegram_message_id: str
    symbol: str
    direction: str
    leverage: str | None
    entry: float | None
    entry_raw: str | None
    stop_loss: float | None
    stop_loss_raw: str | None
    trailing_sl: float | None = None
    tp_reached_count: int = 0
    market_type: str = "crypto"
    take_profits: list[float] = Field(default_factory=list)
    status: SignalStatusLiteral | str
    confidence: float | None
    raw_text: str
    posted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TelegramMonitorStatusResponse(BaseModel):
    running: bool
    interval_seconds: int
    poll_interval_seconds: int | None = None
    last_run: str | None = None
    last_poll: str | None = None
    last_result: dict[str, Any] | None = None
    last_error: str | None = None


# ── Sniper auto-trade engine ────────────────────────────────────────────────

SniperStatusLiteral = Literal["pending", "placed", "skipped", "missed", "failed"]


class TelegramSniperSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    mode: str
    trade_type: str
    position_size_usdt: float
    max_positions: int
    max_positions_sandbox: int = 5
    max_positions_live: int = 3
    leverage: int
    margin_mode: str
    sniper_offset_pct: float
    min_confidence: float
    min_risk_reward: float
    pending_ttl_minutes: int
    reanalyze: bool
    execute_sandbox: bool = True
    execute_live: bool = False
    require_ai_confirmation: bool = True
    execute_immediately: bool = True
    skipped_reanalyze_minutes: int = 15
    tp_trail_pct: float = 1.5
    volume_channel_id: int | None = None
    allowed_channel_ids: list[int] | None = None


class TelegramSniperSettingsUpdate(BaseModel):
    enabled: bool | None = None
    trade_type: Literal["spot", "futures"] | None = None
    position_size_usdt: float | None = Field(default=None, gt=0)
    max_positions: int | None = Field(default=None, ge=1, le=50)
    max_positions_sandbox: int | None = Field(default=None, ge=0, le=100)
    max_positions_live: int | None = Field(default=None, ge=0, le=100)
    leverage: int | None = Field(default=None, ge=1, le=125)
    margin_mode: Literal["crossed", "isolated"] | None = None
    sniper_offset_pct: float | None = Field(default=None, ge=0, le=10)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    min_risk_reward: float | None = Field(default=None, ge=0, le=20)
    pending_ttl_minutes: int | None = Field(default=None, ge=1, le=1440)
    reanalyze: bool | None = None
    execute_sandbox: bool | None = None
    execute_live: bool | None = None
    require_ai_confirmation: bool | None = None
    execute_immediately: bool | None = None
    skipped_reanalyze_minutes: int | None = Field(default=None, ge=0, le=1440)
    tp_trail_pct: float | None = Field(default=None, ge=0.1, le=20.0)
    volume_channel_id: int | None = None
    allowed_channel_ids: list[int] | None = None


class TelegramSniperTradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    signal_id: int
    channel_title: str | None
    symbol: str
    direction: str
    leverage: int | None
    signal_entry: float | None
    sniper_entry: float | None
    live_price_at_plan: float | None
    stop_loss: float | None
    take_profit: float | None
    position_size_usdt: float | None
    risk_reward: float | None
    status: SniperStatusLiteral | str
    reason: str | None
    sim_order_id: int | None
    entry_strategy: str | None = None
    rsi: float | None = None
    support: float | None = None
    resistance: float | None = None
    volume_warning: bool = False
    ai_confirmed: bool | None = None
    ai_confirmation_note: str | None = None
    volume_confirmed: bool | None = None
    executed_mode: str | None = None
    live_order_id: str | None = None
    created_at: datetime
    updated_at: datetime
