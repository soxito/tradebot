"""Telegram Signal & News Plugin SQLAlchemy models."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase

from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


class TelegramBase(DeclarativeBase):
    """Standalone base so plugin tables can be created independently."""

class SourceKind(str, enum.Enum):
    SIGNALS = "signals"
    NEWS = "news"


class PollRunStatus(str, enum.Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class TelegramChannelSource(TelegramBase):
    __tablename__ = "telegram_channel_sources"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    channel_handle = Column(String(200), nullable=False)
    channel_id = Column(String(100), nullable=True)
    source_kind = Column(SQLEnum(SourceKind), nullable=False, index=True)
    provider = Column(String(30), nullable=False, default="auto")
    is_enabled = Column(Boolean, default=True)
    # 'crypto' (default) or 'forex' — controls which price source is used.
    market_type = Column(String(10), nullable=False, default="crypto")

    poll_interval_seconds = Column(Integer, nullable=False, default=300)
    include_keywords_json = Column(JSON, nullable=True)
    exclude_keywords_json = Column(JSON, nullable=True)
    language_hint = Column(String(20), nullable=True)

    last_message_id = Column(String(50), nullable=True)
    last_polled_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=now_utc_naive)
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)

    __table_args__ = (
        Index("ix_telegram_channel_unique", "user_id", "channel_handle", "source_kind", unique=True),
        Index("ix_telegram_channel_user_kind", "user_id", "source_kind"),
    )


class TelegramChannelPreset(TelegramBase):
    __tablename__ = "telegram_channel_presets"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    source_kind = Column(SQLEnum(SourceKind), nullable=False, index=True)
    channels_json = Column(JSON, nullable=False)
    is_public = Column(Boolean, default=True)

    created_at = Column(DateTime, default=now_utc_naive)
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)


class TelegramSubscribedCache(TelegramBase):
    """Cached snapshot of the user's subscribed Telegram channels.

    Discovering channels via Telethon (iter_dialogs) is slow, so we cache the
    result per user and only refresh from Telegram every 30 minutes.
    """
    __tablename__ = "telegram_subscribed_cache"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String(300), nullable=False)
    channel_handle = Column(String(200), nullable=False)
    channel_id = Column(String(100), nullable=True)
    provider = Column(String(30), nullable=False, default="telethon")
    fetched_at = Column(DateTime, nullable=False, default=now_utc_naive, index=True)

    __table_args__ = (
        Index("ix_telegram_subcache_user", "user_id"),
    )


class TelegramIngestMessage(TelegramBase):
    __tablename__ = "telegram_ingest_messages"

    id = Column(Integer, primary_key=True, index=True)
    channel_source_id = Column(Integer, nullable=False, index=True)
    source_kind = Column(SQLEnum(SourceKind), nullable=False, index=True)

    telegram_message_id = Column(String(50), nullable=False)
    posted_at = Column(DateTime, nullable=True)
    author_name = Column(String(200), nullable=True)

    raw_text = Column(Text, nullable=False)
    normalized_text = Column(Text, nullable=True)

    extraction_json = Column(JSON, nullable=True)
    symbols_json = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)

    dedupe_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=now_utc_naive, index=True)

    __table_args__ = (
        Index("ix_telegram_msg_source_mid", "channel_source_id", "telegram_message_id", unique=True),
        Index("ix_telegram_msg_kind_created", "source_kind", "created_at"),
    )


class TelegramPollRun(TelegramBase):
    __tablename__ = "telegram_poll_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)

    status = Column(SQLEnum(PollRunStatus), nullable=False, index=True)
    channels_scanned = Column(Integer, default=0)
    messages_read = Column(Integer, default=0)
    messages_saved = Column(Integer, default=0)

    errors_json = Column(JSON, nullable=True)
    started_at = Column(DateTime, default=now_utc_naive, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class TelegramPluginSettings(TelegramBase):
    """Single-row settings table for Telegram plugin credentials.

    One row (id=1) holds global settings.  Each field maps to the matching
    TelegramPluginConfig attribute.  A non-empty DB value overrides the env var.
    """

    __tablename__ = "telegram_plugin_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Telethon (user account) credentials
    api_id = Column(Integer, nullable=True, default=None)
    api_hash = Column(String(64), nullable=True, default=None)
    phone_number = Column(String(30), nullable=True, default=None)
    # Bot API
    bot_token = Column(String(200), nullable=True, default=None)
    # MCP provider
    mcp_chat_id = Column(String(50), nullable=True, default=None)
    # Label
    label = Column(String(100), nullable=True, default=None)
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)


class SignalStatus(str, enum.Enum):
    ACTIVE = "active"
    FILLED = "filled"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    CLOSED = "closed"


class TelegramParsedSignal(TelegramBase):
    """Structured trading signal parsed from a Telegram channel message.

    Created automatically by the monitor loop whenever an actionable entry
    signal (symbol + direction + entry + TP/SL) is detected in an ingested
    message. Outcome messages (TP hit, SL hit, closed) update the status.
    """

    __tablename__ = "telegram_parsed_signals"

    id = Column(Integer, primary_key=True, index=True)
    channel_source_id = Column(Integer, nullable=False, index=True)
    channel_title = Column(String(200), nullable=True)
    telegram_message_id = Column(String(50), nullable=False)

    symbol = Column(String(40), nullable=False, index=True)
    direction = Column(String(10), nullable=False)  # long | short
    leverage = Column(String(20), nullable=True)

    entry = Column(Float, nullable=True)
    entry_raw = Column(String(120), nullable=True)
    stop_loss = Column(Float, nullable=True)
    stop_loss_raw = Column(String(120), nullable=True)
    take_profits_json = Column(JSON, nullable=True)  # list[float]

    # Trailing stop-loss: set to each TP level as they are crossed.
    # When None, the original stop_loss is used for SL checks.
    trailing_sl = Column(Float, nullable=True)
    # How many TP targets have been crossed (used to advance trailing_sl).
    tp_reached_count = Column(Integer, nullable=False, default=0)
    # 'crypto' or 'forex' — determines which price source is used for lifecycle checks.
    market_type = Column(String(10), nullable=False, default="crypto")

    status = Column(
        SQLEnum(SignalStatus, name="telegram_signal_status"),
        nullable=False,
        default=SignalStatus.ACTIVE,
        index=True,
    )
    confidence = Column(Float, nullable=True)

    raw_text = Column(Text, nullable=False)
    posted_at = Column(DateTime, nullable=True)

    dedupe_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=now_utc_naive, index=True)
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)

    __table_args__ = (
        Index("ix_telegram_signal_symbol_status", "symbol", "status"),
        Index("ix_telegram_signal_channel_created", "channel_source_id", "created_at"),
    )


class SniperTradeStatus(str, enum.Enum):
    PENDING = "pending"      # waiting for price to reach the optimized sniper entry
    PLACED = "placed"        # order placed into the sim account
    SKIPPED = "skipped"      # rejected during re-analysis (stale/poor R:R)
    MISSED = "missed"        # price hit SL/first TP before our entry filled
    FAILED = "failed"        # execution error


class TelegramSniperSettings(TelegramBase):
    """Single-row settings for the Telegram sniper auto-trade engine."""

    __tablename__ = "telegram_sniper_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    enabled = Column(Boolean, nullable=False, default=False)
    mode = Column(String(10), nullable=False, default="paper")  # paper only (sim account)
    trade_type = Column(String(10), nullable=False, default="futures")
    position_size_usdt = Column(Float, nullable=False, default=100.0)
    max_positions = Column(Integer, nullable=False, default=5)
    # Separate active-signal caps for sandbox (demo) and live trading.
    max_positions_sandbox = Column(Integer, nullable=False, default=5)
    max_positions_live = Column(Integer, nullable=False, default=3)
    leverage = Column(Integer, nullable=False, default=10)
    margin_mode = Column(String(10), nullable=False, default="crossed")
    sniper_offset_pct = Column(Float, nullable=False, default=0.3)   # how far to improve entry (%)
    min_confidence = Column(Float, nullable=False, default=0.6)
    min_risk_reward = Column(Float, nullable=False, default=1.2)
    pending_ttl_minutes = Column(Integer, nullable=False, default=120)
    reanalyze = Column(Boolean, nullable=False, default=True)
    # Execution targets (which order books a confirmed signal is placed on).
    execute_sandbox = Column(Boolean, nullable=False, default=True)   # sim account (/trading sandbox)
    execute_live = Column(Boolean, nullable=False, default=False)     # REAL money (opt-in, default off)
    # Only auto-execute when the core AI agents + exchange volume confirm the
    # signal's direction. Unconfirmed signals stay PENDING for manual execution.
    require_ai_confirmation = Column(Boolean, nullable=False, default=True)
    # Place confirmed signals immediately at market (so they appear on /trading
    # right away) instead of waiting for the optimised sniper limit entry.
    execute_immediately = Column(Boolean, nullable=False, default=True)
    # Re-analyse SKIPPED signals on this cadence (minutes). 0 = disabled.
    skipped_reanalyze_minutes = Column(Integer, nullable=False, default=15)
    # Channel ID for volume-alert messages. When set, inbound messages from
    # this channel are parsed for token volume signals and used to trigger
    # re-analysis of matching active/skipped signals.
    volume_channel_id = Column(Integer, nullable=True)
    # Trailing stop percentage used after the final TP is crossed (default 1.5 %).
    tp_trail_pct = Column(Float, nullable=False, default=1.5)
    allowed_channel_ids_json = Column(JSON, nullable=True)  # null = all enabled channels
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)


class TelegramSniperTrade(TelegramBase):
    """A sniper auto-trade derived from a parsed Telegram signal.

    References the parsed signal and (optionally) the sim order it produced by
    ID only — no cross-plugin foreign keys.
    """

    __tablename__ = "telegram_sniper_trades"

    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(Integer, nullable=False, unique=True, index=True)
    channel_title = Column(String(200), nullable=True)

    symbol = Column(String(40), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    leverage = Column(Integer, nullable=True)

    signal_entry = Column(Float, nullable=True)
    sniper_entry = Column(Float, nullable=True)
    live_price_at_plan = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)  # chosen primary target
    position_size_usdt = Column(Float, nullable=True)
    risk_reward = Column(Float, nullable=True)

    status = Column(
        SQLEnum(SniperTradeStatus, name="telegram_sniper_trade_status"),
        nullable=False,
        default=SniperTradeStatus.PENDING,
        index=True,
    )
    reason = Column(Text, nullable=True)
    sim_order_id = Column(Integer, nullable=True)

    # Strategy / TA analysis
    entry_strategy = Column(String(200), nullable=True)
    rsi = Column(Float, nullable=True)
    support = Column(Float, nullable=True)
    resistance = Column(Float, nullable=True)
    volume_warning = Column(Boolean, nullable=False, default=False)

    # AI agent + exchange-volume confirmation (gates auto-execution)
    ai_confirmed = Column(Boolean, nullable=True)        # null = not yet checked
    ai_confirmation_note = Column(Text, nullable=True)
    volume_confirmed = Column(Boolean, nullable=True)
    # Execution tracking
    executed_mode = Column(String(10), nullable=True)   # sandbox | live | none
    live_order_id = Column(String(60), nullable=True)

    created_at = Column(DateTime, default=now_utc_naive, index=True)
    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)

    __table_args__ = (
        Index("ix_telegram_sniper_symbol_status", "symbol", "status"),
    )


class TelegramNewsSentiment(TelegramBase):
    """Tracks which NEWS ingest messages have been scored and pushed into the
    core sentiment system, so auto-trading decisions benefit from channel news.
    """

    __tablename__ = "telegram_news_sentiment"

    id = Column(Integer, primary_key=True, index=True)
    ingest_message_id = Column(Integer, nullable=False, unique=True, index=True)
    channel_source_id = Column(Integer, nullable=True, index=True)
    symbols_json = Column(JSON, nullable=True)
    score = Column(Float, nullable=True)         # -1..1
    magnitude = Column(Float, nullable=True)     # 0..1
    label = Column(String(20), nullable=True)    # bullish/bearish/neutral
    pushed_symbols = Column(Integer, default=0)  # how many sentiment rows written
    created_at = Column(DateTime, default=now_utc_naive, index=True)


class TelegramBotConfig(TelegramBase):
    """Single-row configuration for the Telegram Bot command integration.

    Stores bot token (if not using the shared plugin settings token), webhook
    info, polling mode toggle, security allow-list, and the last processed
    update_id for polling de-duplication.

    Table prefix: ``telegram_`` (plugin convention).
    """

    __tablename__ = "telegram_bot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Optionally override the token stored in TelegramPluginSettings
    bot_token_override = Column(String(300), nullable=True, default=None)

    # Webhook configuration
    webhook_url = Column(String(500), nullable=True, default=None)
    webhook_secret = Column(String(200), nullable=True, default=None)

    # Polling mode — used when webhook is not set (e.g. localhost dev)
    polling_enabled = Column(Boolean, nullable=False, default=False)
    last_update_id = Column(Integer, nullable=True, default=None)

    # Security: comma-separated chat IDs that may send commands.
    # NULL or empty string = accept all (dev convenience only).
    allowed_chat_ids_json = Column(JSON, nullable=True, default=None)

    # AI fallback for unrecognised text messages
    ai_fallback_enabled = Column(Boolean, nullable=False, default=False)

    updated_at = Column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
