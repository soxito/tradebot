"""WhatsApp Signal & News Plugin database models."""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.models.database import Base


class WhatsAppChannelKind(str, PyEnum):
    """Type of WhatsApp channel/source."""

    SIGNALS = "signals"
    NEWS = "news"
    VOLUME_ALERTS = "volume_alerts"


class WhatsAppSourceType(str, PyEnum):
    """How messages are fetched from WhatsApp."""

    GROUP = "group"
    CONTACT = "contact"
    BROADCAST = "broadcast"
    COMMUNITY = "community"


class SignalStatus(str, PyEnum):
    """Status of a parsed trading signal."""

    ACTIVE = "active"
    FILLED = "filled"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    CLOSED = "closed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SignalDirection(str, PyEnum):
    """Trade direction."""

    BUY = "buy"
    SELL = "sell"
    LONG = "long"
    SHORT = "short"


class SniperTradeStatus(str, PyEnum):
    """Status of a sniper auto-trade."""

    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(str, PyEnum):
    """WhatsApp session status."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    QR_READY = "qr_ready"
    AUTHENTICATED = "authenticated"
    READY = "ready"
    FAILED = "failed"


class WhatsAppPluginSettings(Base):
    """Global plugin settings (single row)."""

    __tablename__ = "whatsapp_plugin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # OpenWA Gateway
    openwa_base_url: Mapped[str] = mapped_column(String(500), default="http://localhost:2785")
    openwa_api_key: Mapped[str] = mapped_column(String(500), default="")
    default_session_name: Mapped[str] = mapped_column(String(100), default="tradebot_whatsapp")

    # Webhook
    webhook_secret: Mapped[str] = mapped_column(String(500), default="")

    # Polling intervals
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    session_health_check_seconds: Mapped[int] = mapped_column(Integer, default=60)

    # LLM fallback
    enable_llm_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[str] = mapped_column(String(100), default="fable-5-high")
    llm_timeout_seconds: Mapped[int] = mapped_column(Integer, default=20)
    openai_api_key: Mapped[str] = mapped_column(String(500), default="")

    # Message processing
    max_messages_per_poll: Mapped[int] = mapped_column(Integer, default=50)
    message_dedupe_ttl_hours: Mapped[int] = mapped_column(Integer, default=24)

    # Sniper defaults
    sniper_enabled_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sniper_mode_default: Mapped[str] = mapped_column(String(20), default="sandbox")
    sniper_position_size_usdt_default: Mapped[float] = mapped_column(Float, default=100.0)
    sniper_max_positions_default: Mapped[int] = mapped_column(Integer, default=5)
    sniper_min_confidence_default: Mapped[float] = mapped_column(Float, default=0.65)
    sniper_min_risk_reward_default: Mapped[float] = mapped_column(Float, default=1.5)

    # Metadata
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )


class WhatsAppSession(Base):
    """WhatsApp session managed via OpenWA Gateway."""

    __tablename__ = "whatsapp_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))

    # OpenWA Gateway fields
    status: Mapped[str] = mapped_column(String(30), default=SessionStatus.DISCONNECTED.value)
    qr_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # base64
    phone_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    battery: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    plugged: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Metadata
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class WhatsAppChannelSource(Base):
    """Configured WhatsApp channel/group/contact to monitor."""

    __tablename__ = "whatsapp_channel_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # Source identification
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default=WhatsAppChannelKind.SIGNALS.value)
    source_type: Mapped[str] = mapped_column(String(20), default=WhatsAppSourceType.GROUP.value)

    # WhatsApp identifiers
    chat_id: Mapped[str] = mapped_column(String(100), index=True)  # e.g., "123456789@g.us"
    chat_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Session association
    session_id: Mapped[str] = mapped_column(String(100), ForeignKey("whatsapp_sessions.session_id"))

    # Processing options
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extract_signals: Mapped[bool] = mapped_column(Boolean, default=True)
    extract_news: Mapped[bool] = mapped_column(Boolean, default=False)
    use_llm_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Deduplication
    last_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_message_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    # Relationships
    session: Mapped["WhatsAppSession"] = relationship("WhatsAppSession", foreign_keys=[session_id])

    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", name="uq_whatsapp_channel_user_chat"),
        Index("ix_whatsapp_channel_session_kind", "session_id", "kind"),
    )


class WhatsAppMessage(Base):
    """Raw WhatsApp message stored for processing and audit."""

    __tablename__ = "whatsapp_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Source
    channel_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("whatsapp_channel_sources.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(100), index=True)

    # WhatsApp message identifiers
    message_id: Mapped[str] = mapped_column(String(100), index=True)  # OpenWA message ID
    from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    sender_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g., "123456789@c.us"
    sender_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Content
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_type: Mapped[str] = mapped_column(String(30), default="text")
    media_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Timestamps
    whatsapp_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())

    # Processing status
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Raw payload for debugging
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    channel_source: Mapped["WhatsAppChannelSource"] = relationship("WhatsAppChannelSource")

    __table_args__ = (
        UniqueConstraint("session_id", "message_id", name="uq_whatsapp_message_session_msg"),
        Index("ix_whatsapp_message_source_processed", "channel_source_id", "processed"),
    )


class WhatsAppParsedSignal(Base):
    """Trading signal extracted from a WhatsApp message."""

    __tablename__ = "whatsapp_parsed_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Source
    channel_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("whatsapp_channel_sources.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("whatsapp_messages.id", ondelete="CASCADE"), index=True
    )
    whatsapp_message_id: Mapped[str] = mapped_column(String(100), index=True)

    # Signal details
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # buy/sell/long/short
    market_type: Mapped[str] = mapped_column(String(10), default="crypto")  # crypto/forex
    leverage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Entry/Exit
    entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_raw: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_raw: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    trailing_sl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Take profits (JSON array)
    take_profits: Mapped[list] = mapped_column(JSON, default=list)
    tp_reached_count: Mapped[int] = mapped_column(Integer, default=0)

    # Confidence & scoring
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    risk_reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(20), default=SignalStatus.ACTIVE.value, index=True
    )

    # Metadata
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())
    posted_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    # Extraction method
    extraction_method: Mapped[str] = mapped_column(String(30), default="regex")  # regex/llm/hybrid
    llm_model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Relationships
    channel_source: Mapped["WhatsAppChannelSource"] = relationship("WhatsAppChannelSource")
    message: Mapped["WhatsAppMessage"] = relationship("WhatsAppMessage")

    __table_args__ = (
        Index("ix_whatsapp_signal_symbol_status", "symbol", "status"),
        Index("ix_whatsapp_signal_posted_at", "posted_at"),
    )


class WhatsAppSniperSettings(Base):
    """Sniper auto-trade configuration for WhatsApp signals."""

    __tablename__ = "whatsapp_sniper_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Global on/off
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Execution mode
    mode: Mapped[str] = mapped_column(String(20), default="sandbox")  # sandbox/live/both
    trade_type: Mapped[str] = mapped_column(String(20), default="market")  # market/limit

    # Position sizing
    position_size_usdt: Mapped[float] = mapped_column(Float, default=100.0)
    max_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_positions_sandbox: Mapped[int] = mapped_column(Integer, default=5)
    max_positions_live: Mapped[int] = mapped_column(Integer, default=3)

    # Risk management
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    margin_mode: Mapped[str] = mapped_column(String(20), default="crossed")
    sniper_offset_pct: Mapped[float] = mapped_column(Float, default=0.5)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.65)
    min_risk_reward: Mapped[float] = mapped_column(Float, default=1.5)

    # Order management
    pending_ttl_minutes: Mapped[int] = mapped_column(Integer, default=30)
    reanalyze: Mapped[bool] = mapped_column(Boolean, default=True)
    execute_sandbox: Mapped[bool] = mapped_column(Boolean, default=True)
    execute_live: Mapped[bool] = mapped_column(Boolean, default=False)
    require_ai_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    execute_immediately: Mapped[bool] = mapped_column(Boolean, default=True)

    # Advanced
    skipped_reanalyze_minutes: Mapped[int] = mapped_column(Integer, default=15)
    tp_trail_pct: Mapped[float] = mapped_column(Float, default=1.5)

    # Channel filtering
    volume_channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    allowed_channel_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Metadata
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )


class WhatsAppSniperTrade(Base):
    """Auto-trade executed from a WhatsApp signal."""

    __tablename__ = "whatsapp_sniper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # References
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("whatsapp_parsed_signals.id", ondelete="CASCADE"), index=True
    )
    channel_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("whatsapp_channel_sources.id", ondelete="CASCADE"), index=True
    )

    # Trade details
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    side: Mapped[str] = mapped_column(String(10))  # buy/sell

    # Order parameters
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    margin_mode: Mapped[str] = mapped_column(String(20), default="crossed")
    position_size_usdt: Mapped[float] = mapped_column(Float)
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Execution
    mode: Mapped[str] = mapped_column(String(20))  # sandbox/live
    exchange: Mapped[str] = mapped_column(String(30), default="bitget")
    status: Mapped[str] = mapped_column(
        String(20), default=SniperTradeStatus.PENDING.value, index=True
    )

    # Order IDs
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # TP/SL order IDs
    tp_order_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    sl_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Result
    filled_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )
    placed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    signal: Mapped["WhatsAppParsedSignal"] = relationship("WhatsAppParsedSignal")
    channel_source: Mapped["WhatsAppChannelSource"] = relationship("WhatsAppChannelSource")

    __table_args__ = (
        Index("ix_whatsapp_sniper_trade_symbol_status", "symbol", "status"),
        Index("ix_whatsapp_sniper_trade_created", "created_at"),
    )


class WhatsAppChannelPreset(Base):
    """Pre-configured channel presets for quick setup."""

    __tablename__ = "whatsapp_channel_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default=WhatsAppChannelKind.SIGNALS.value)

    # Default configuration
    default_config: Mapped[dict] = mapped_column(JSON, default=dict)

    # Metadata
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )