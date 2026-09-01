"""
MT5 Trading Plugin — SQLAlchemy Models

All tables prefixed with 'mt5_' to avoid collisions with core.
References core tables by ID only — no cross-boundary FK constraints.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON,
    Enum as SQLEnum, Index, BigInteger, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase
import enum


class MT5Base(DeclarativeBase):
    """Separate declarative base for MT5 plugin models."""
    pass


# ── Enums ──────────────────────────────────────────────────

class MT5AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
    ERROR = "error"

class MT5AccountType(str, enum.Enum):
    LIVE = "live"
    DEMO = "demo"
    PROP = "prop"

class MT5OrderType(str, enum.Enum):
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"

class MT5OrderStatus(str, enum.Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

class MT5PositionSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"

class MT5DealType(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    BALANCE = "balance"
    CREDIT = "credit"
    COMMISSION = "commission"

class ReplayRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class CopySimStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class CopyMode(str, enum.Enum):
    SIM = "sim"      # paper ledger only
    LIVE = "live"    # real orders on follower accounts

class MT5ScalpSessionStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"


# ── Core MT5 Models ────────────────────────────────────────

class MT5Account(MT5Base):
    """MT5 trading account connected via mtapi-io REST API."""
    __tablename__ = "mt5_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)  # refs core users by ID
    name = Column(String(100), nullable=False)
    server = Column(String(200), nullable=False)
    login = Column(String(50), nullable=False)
    # Password stored encrypted; decrypt at runtime only
    password_encrypted = Column(Text, nullable=False)
    account_type = Column(
        SQLEnum(MT5AccountType, name="mt5_account_type"),
        default=MT5AccountType.DEMO,
        nullable=False,
    )
    status = Column(SQLEnum(MT5AccountStatus), default=MT5AccountStatus.DISCONNECTED)
    balance = Column(Float, default=0.0)
    equity = Column(Float, default=0.0)
    margin = Column(Float, default=0.0)
    free_margin = Column(Float, default=0.0)
    margin_level = Column(Float, nullable=True)
    floating_pnl = Column(Float, default=0.0)
    currency = Column(String(10), default="USD")
    leverage = Column(Integer, default=100)
    last_sync_at = Column(DateTime, nullable=True)
    api_reachable = Column(Boolean, default=False)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MT5Order(MT5Base):
    """Pending order on an MT5 account."""
    __tablename__ = "mt5_orders"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    mt5_ticket = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    order_type = Column(SQLEnum(MT5OrderType), nullable=False)
    volume = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    status = Column(SQLEnum(MT5OrderStatus), default=MT5OrderStatus.PENDING, index=True)
    comment = Column(String(200), nullable=True)
    expiration = Column(DateTime, nullable=True)
    mt5_time_setup = Column(DateTime, nullable=True)
    raw_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MT5Position(MT5Base):
    """Open position on an MT5 account."""
    __tablename__ = "mt5_positions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    mt5_ticket = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    side = Column(SQLEnum(MT5PositionSide), nullable=False)
    volume = Column(Float, nullable=False)
    price_open = Column(Float, nullable=False)
    price_current = Column(Float, nullable=True)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    swap = Column(Float, default=0.0)
    profit = Column(Float, default=0.0)
    commission = Column(Float, default=0.0)
    comment = Column(String(200), nullable=True)
    mt5_time_open = Column(DateTime, nullable=True)
    raw_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MT5Deal(MT5Base):
    """Executed deal (trade history) from MT5."""
    __tablename__ = "mt5_deals"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    mt5_ticket = Column(BigInteger, nullable=False, index=True)
    mt5_order_ticket = Column(BigInteger, nullable=True)
    mt5_position_ticket = Column(BigInteger, nullable=True)
    symbol = Column(String(30), nullable=True, index=True)
    deal_type = Column(SQLEnum(MT5DealType), nullable=False)
    volume = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    profit = Column(Float, default=0.0)
    commission = Column(Float, default=0.0)
    swap = Column(Float, default=0.0)
    fee = Column(Float, default=0.0)
    comment = Column(String(200), nullable=True)
    mt5_time = Column(DateTime, nullable=True)
    raw_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # Uniqueness enforced at DB level — prevents race conditions during concurrent syncs.
        # Matches the constraint added via migration: uq_mt5_deals_account_ticket
        UniqueConstraint("account_id", "mt5_ticket", name="uq_mt5_deals_account_ticket"),
        Index("ix_mt5_deals_account_time", "account_id", "mt5_time"),
        Index("ix_mt5_deals_symbol_time", "symbol", "mt5_time"),
    )


# ── Multi-Account Aggregation ──────────────────────────────

class MT5AccountGroup(MT5Base):
    """Named group of MT5 accounts for aggregated views."""
    __tablename__ = "mt5_account_groups"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class MT5AccountGroupMember(MT5Base):
    """Maps accounts into groups with aggregation weight."""
    __tablename__ = "mt5_account_group_members"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    weight = Column(Float, default=1.0)  # for weighted aggregation
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mt5_group_members_unique", "group_id", "account_id", unique=True),
    )


class MT5AccountSnapshot(MT5Base):
    """Point-in-time snapshot of account or group state (for equity curves)."""
    __tablename__ = "mt5_account_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, nullable=True, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    time = Column(DateTime, nullable=False)
    equity = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    margin = Column(Float, default=0.0)
    free_margin = Column(Float, default=0.0)
    margin_level = Column(Float, nullable=True)
    floating_pnl = Column(Float, default=0.0)
    raw_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mt5_snapshots_group_time", "group_id", "time"),
        Index("ix_mt5_snapshots_account_time", "account_id", "time"),
    )


# ── Trade Replay (Backtesting Bridge) ──────────────────────

class MT5ReplayRun(MT5Base):
    """A replay run that converts real MT5 deals into a performance report."""
    __tablename__ = "mt5_replay_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=True)
    group_id = Column(Integer, nullable=True)
    date_from = Column(DateTime, nullable=False)
    date_to = Column(DateTime, nullable=False)
    symbol_filter = Column(JSON, nullable=True)  # e.g. ["XAUUSD","EURUSD"]
    status = Column(SQLEnum(ReplayRunStatus), default=ReplayRunStatus.QUEUED)
    total_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, nullable=True)
    equity_curve = Column(JSON, nullable=True)  # [{time, equity}]
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MT5ReplayTrade(MT5Base):
    """Individual trade within a replay run."""
    __tablename__ = "mt5_replay_trades"

    id = Column(Integer, primary_key=True, index=True)
    replay_run_id = Column(Integer, nullable=False, index=True)
    time = Column(DateTime, nullable=False)
    symbol = Column(String(30), nullable=False)
    side = Column(String(10), nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, default=0.0)
    fees = Column(Float, default=0.0)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Copy-Trading Simulation ────────────────────────────────

class MT5CopyProfile(MT5Base):
    """Configuration for simulating copy-trading from a source account."""
    __tablename__ = "mt5_copy_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    source_account_id = Column(Integer, nullable=True)
    source_group_id = Column(Integer, nullable=True)
    allocation_mode = Column(String(20), default="fixed_lot")  # fixed_lot|risk_percent|multiplier
    allocation_value = Column(Float, default=0.01)
    max_open_positions = Column(Integer, default=5)
    symbol_whitelist = Column(JSON, nullable=True)
    mode = Column(SQLEnum(CopyMode), default=CopyMode.SIM, nullable=False)
    enabled = Column(Boolean, default=False)
    paper_balance = Column(Float, default=10000.0)  # paper simulation wallet
    paper_equity = Column(Float, default=10000.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MT5CopySimTrade(MT5Base):
    """Simulated copy-trade entry (paper only, no real execution)."""
    __tablename__ = "mt5_copy_sim_trades"

    id = Column(Integer, primary_key=True, index=True)
    copy_profile_id = Column(Integer, nullable=False, index=True)
    source_deal_id = Column(Integer, nullable=True)
    symbol = Column(String(30), nullable=False)
    side = Column(String(10), nullable=False)
    qty_sim = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    pnl_sim = Column(Float, default=0.0)
    status = Column(SQLEnum(CopySimStatus), default=CopySimStatus.OPEN)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mt5_copy_sim_profile_status", "copy_profile_id", "status"),
    )


class MT5CopyFollower(MT5Base):
    """A follower account in a live copy profile — mirrors the source's trades.

    Each follower keeps its own sizing rule and a map of copied tickets
    (source position ticket -> follower ticket) so closes mirror correctly.
    """
    __tablename__ = "mt5_copy_followers"

    id = Column(Integer, primary_key=True, index=True)
    copy_profile_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    allocation_mode = Column(String(20), default="multiplier")  # fixed_lot|risk_percent|multiplier
    allocation_value = Column(Float, default=1.0)
    max_open_positions = Column(Integer, default=10)
    #: source position ticket -> follower position ticket
    copied_tickets = Column(JSON, default=dict)
    last_error = Column(String(500), nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mt5_copy_follower_profile_account", "copy_profile_id", "account_id"),
    )


# ── Autonomous Scalping ────────────────────────────────────

class MT5ScalpSession(MT5Base):
    """An autonomous scalp-bot session for a single account + symbol."""
    __tablename__ = "mt5_scalp_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    lot_size = Column(Float, default=0.01)
    auto_lot = Column(Boolean, default=False)
    risk_per_trade_pct = Column(Float, default=1.0)
    max_daily_loss_pct = Column(Float, default=3.0)
    target_profit_pct = Column(Float, default=1.5)      # per-trade take-profit ceiling
    recovery_enabled = Column(Boolean, default=True)
    use_ai = Column(Boolean, default=True)
    use_kronos = Column(Boolean, default=True)
    timeframe = Column(String(10), default="M5")        # primary scalp timeframe
    status = Column(
        SQLEnum(MT5ScalpSessionStatus, name="mt5_scalp_session_status"),
        default=MT5ScalpSessionStatus.ACTIVE,
        index=True,
    )
    # Live phase for the UI: analyzing | waiting | in_trade | recovery
    phase = Column(String(20), default="analyzing")
    bias_direction = Column(String(10), nullable=True)  # buy | sell | neutral
    bias_confidence = Column(Float, default=0.0)
    trade1_ticket = Column(BigInteger, nullable=True)
    trade2_ticket = Column(BigInteger, nullable=True)   # recovery leg
    total_trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    session_pnl = Column(Float, default=0.0)
    start_equity = Column(Float, default=0.0)
    last_cycle_at = Column(DateTime, nullable=True)
    ai_note = Column(Text, nullable=True)
    error_msg = Column(Text, nullable=True)
    raw_settings = Column(JSON, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    stopped_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_mt5_scalp_sessions_acct_symbol", "account_id", "symbol"),
    )


class MT5ScalpTrade(MT5Base):
    """A single scalp trade opened by a scalp-bot session."""
    __tablename__ = "mt5_scalp_trades"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    side = Column(String(10), nullable=False)           # buy | sell
    lot = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    pnl = Column(Float, default=0.0)
    is_recovery = Column(Boolean, default=False)
    ticket = Column(BigInteger, nullable=True, index=True)
    confidence = Column(Float, default=0.0)
    reason = Column(Text, nullable=True)
    status = Column(String(20), default="open")         # open | closed
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_mt5_scalp_trades_session_status", "session_id", "status"),
    )


# ── Plugin Settings ────────────────────────────────────────

class MT5PluginSetting(MT5Base):
    """Key-value settings for the MT5 plugin."""
    __tablename__ = "mt5_plugin_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── SMC learning loop ──────────────────────────────────────
# Additive tables backing the sniper's learning loop. Every analysis, every
# emitted signal and every realised outcome is persisted here so new signals can
# be grounded in what actually happened last time a similar setup appeared, and
# so factor weights can be recalibrated from realised P&L rather than intuition.
#
# Shared by the MT5 and crypto signal paths — `market` distinguishes them.

class SmcAnalysisRecord(MT5Base):
    """One /strategy/analyze call: the market read and how it was produced."""
    __tablename__ = "mt5_smc_analyses"

    id = Column(Integer, primary_key=True, index=True)
    market = Column(String(10), nullable=False, default="mt5", index=True)  # mt5 | crypto
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    bias = Column(String(12), nullable=True)
    htf_bias = Column(String(12), nullable=True)
    last_price = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)
    rsi = Column(Float, nullable=True)
    volume_z = Column(Float, nullable=True)
    momentum = Column(String(20), nullable=True)
    signal_count = Column(Integer, default=0)
    # Provenance from the analysis router (Phase 1).
    provider_used = Column(String(120), nullable=True)
    tier = Column(String(20), nullable=True)      # primary | cascade | deterministic
    is_degraded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_mt5_smc_analyses_symbol_time", "symbol", "created_at"),
    )


class SmcSignalRecord(MT5Base):
    """A single emitted sniper setup, with the factor scores that produced it."""
    __tablename__ = "mt5_smc_signals"

    id = Column(Integer, primary_key=True, index=True)
    analysis_id = Column(Integer, nullable=True, index=True)   # SmcAnalysisRecord.id
    market = Column(String(10), nullable=False, default="mt5", index=True)
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    side = Column(String(4), nullable=False)                   # buy | sell
    zone_kind = Column(String(30), nullable=True)
    entry = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    rr = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    bias = Column(String(12), nullable=True)
    htf_bias = Column(String(12), nullable=True)
    # Full numeric breakdown, plus a flat {factor: contribution} map so
    # similarity search and weight recalibration avoid re-parsing the nested form.
    score_breakdown = Column(JSON, nullable=True)
    factor_vector = Column(JSON, nullable=True)
    confluence = Column(JSON, nullable=True)
    volume_confirmed = Column(Boolean, default=False)
    # Set once the setup is linked to a real placed trade.
    ticket = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_mt5_smc_signals_symbol_side_time", "symbol", "side", "created_at"),
    )


class SmcOutcome(MT5Base):
    """Realised result of a signal that was actually traded."""
    __tablename__ = "mt5_smc_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(Integer, nullable=False, index=True)    # SmcSignalRecord.id
    market = Column(String(10), nullable=False, default="mt5", index=True)
    symbol = Column(String(30), nullable=False, index=True)
    ticket = Column(BigInteger, nullable=True, index=True)
    # Maximum favourable / adverse excursion in price terms and in R multiples.
    mfe = Column(Float, default=0.0)
    mae = Column(Float, default=0.0)
    mfe_r = Column(Float, default=0.0)
    mae_r = Column(Float, default=0.0)
    r_multiple = Column(Float, default=0.0)
    win = Column(Boolean, default=False)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(30), nullable=True)   # tp | sl | manual | expiry
    time_to_target_s = Column(Integer, nullable=True)
    pnl = Column(Float, default=0.0)
    closed_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_mt5_smc_outcome_signal"),
    )


class SmcFactorWeight(MT5Base):
    """Factor weight recalibrated from realised outcomes.

    `symbol_class` scopes the learning: "*" is the global weight, otherwise the
    instrument symbol. Read back by smc_scoring.score_signal via smc_memory.
    """
    __tablename__ = "mt5_smc_factor_weights"

    id = Column(Integer, primary_key=True, index=True)
    market = Column(String(10), nullable=False, default="mt5")
    symbol_class = Column(String(30), nullable=False, default="*")
    factor = Column(String(60), nullable=False)
    weight = Column(Float, nullable=False)
    default_weight = Column(Float, nullable=False)
    sample_count = Column(Integer, default=0)
    win_contribution = Column(Float, default=0.0)   # mean contribution on winners
    loss_contribution = Column(Float, default=0.0)  # mean contribution on losers
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("market", "symbol_class", "factor",
                         name="uq_mt5_smc_factor_weight"),
    )


class ResearchFinding(MT5Base):
    """A background-research finding with its verifiable source (Phase 4)."""
    __tablename__ = "mt5_research_findings"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(30), nullable=False, index=True)  # calendar | news | sentiment | prediction
    symbol = Column(String(30), nullable=True, index=True)
    headline = Column(String(400), nullable=False)
    body = Column(Text, nullable=True)
    source = Column(String(120), nullable=True)
    source_url = Column(String(600), nullable=True)
    confidence = Column(Float, default=0.0)
    # True when no verifiable source URL backs the finding. Speculative findings
    # must never gate a trade signal on their own.
    speculative = Column(Boolean, default=True, index=True)
    provider_used = Column(String(120), nullable=True)
    published_at = Column(DateTime, nullable=True)
    decay_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_mt5_research_kind_time", "kind", "created_at"),
    )


class SignalResearchJob(MT5Base):
    """One PAIR's research job — every live signal on that instrument at once.

    Research is batched per instrument, not per signal. Three Telegram calls and
    an SMC setup on XAUUSD are four opinions about one market: researching them
    separately burns four times the model budget and yields four unreconciled
    answers. Researched together they corroborate or contradict each other, and
    the disagreement between their entries is itself information — it is what
    produces the two entry plans in `entries`.

    The queue runs a bounded number of these concurrently and the Research page
    renders `stage`/`progress`/`steps` live, so a run is observable rather than
    a black box.
    """
    __tablename__ = "mt5_signal_research_jobs"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    #: Where the signals came from — "telegram+smc" when a pair has several.
    source = Column(String(40), nullable=False, default="manual")
    #: "pair:{SYMBOL}" — the natural key. Not a DB unique constraint: a pair is
    #: legitimately re-researched once its cooldown lapses, or immediately when
    #: a signal arrives that the last run did not see.
    signal_ref = Column(String(80), nullable=False, index=True)
    #: The individual "{source}:{id}" refs folded into this run, and the full
    #: signal payloads the model was shown. Together they are the audit trail
    #: for "which signals produced these entries".
    signal_refs = Column(JSON, nullable=True)
    signals = Column(JSON, nullable=True)
    signal_count = Column(Integer, default=1)

    #: The consensus of the batch — majority direction, or None when the signals
    #: genuinely disagree (which the research is expected to resolve).
    direction = Column(String(8), nullable=True)                    # buy | sell
    entry = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)

    #: The deliverable: exactly two costed entry plans, primary and secondary,
    #: each with its own stop and target. Shape per entry:
    #: {label, side, entry, stop_loss, take_profit, rr, confidence, trigger,
    #:  rationale}. Read by the sniper paths and rendered wherever a pair's
    #: signals are shown.
    entries = Column(JSON, nullable=True)

    status = Column(String(16), nullable=False, default="queued", index=True)
    stage = Column(String(40), nullable=True)
    progress = Column(Float, default=0.0)
    #: [{name, status, detail, ms}] — the per-step audit the UI checklists.
    steps = Column(JSON, nullable=True)

    verdict = Column(String(16), nullable=True)   # bullish|bearish|neutral|stand_aside
    verdict_confidence = Column(Float, nullable=True)
    horizon_hours = Column(Integer, nullable=True)
    rationale = Column(Text, nullable=True)
    #: URLs the prediction was built from. Empty ⇒ the finding is speculative.
    sources = Column(JSON, nullable=True)
    speculative = Column(Boolean, default=True)
    finding_id = Column(Integer, nullable=True, index=True)   # ResearchFinding.id
    provider_used = Column(String(120), nullable=True)
    error = Column(Text, nullable=True)

    queued_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_mt5_srj_status_queued", "status", "queued_at"),
        Index("ix_mt5_srj_symbol_status", "symbol", "status"),
    )
