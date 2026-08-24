"""
AI Market Analyst Plugin — SQLAlchemy Models

All tables prefixed with 'ai_' to avoid collisions.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON,
    Enum as SQLEnum, Index
)
from sqlalchemy.orm import DeclarativeBase
import enum


class AIBase(DeclarativeBase):
    """Separate declarative base for AI Analyst plugin."""
    pass


# ── Enums ──────────────────────────────────────────────────

class AgentRoleType(str, enum.Enum):
    SCALPER = "scalper"
    SWING = "swing"
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    CUSTOM = "custom"

class ReasoningEffort(str, enum.Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"

class DecisionType(str, enum.Enum):
    ANALYZE = "analyze"
    PROPOSE = "propose"
    PLACE_ATTEMPT = "place_attempt"

class DecisionDirection(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    NONE = "none"

class DecisionStatus(str, enum.Enum):
    DRAFTED = "drafted"
    BLOCKED = "blocked"
    QUEUED = "queued"
    SENT_TO_MT5 = "sent_to_mt5"
    MT5_ACCEPTED = "mt5_accepted"
    MT5_REJECTED = "mt5_rejected"
    CANCELLED = "cancelled"

class TradeMode(str, enum.Enum):
    BUY_ONLY = "buy_only"
    SELL_ONLY = "sell_only"
    BOTH = "both"

class LotMode(str, enum.Enum):
    FIXED = "fixed"
    BOUNDED = "bounded"


# ── Agent Profile ──────────────────────────────────────────

class AIAgent(AIBase):
    """
    AI agent profile created by admin.
    
    Defines personality, instruments, risk profile, GPT settings,
    and allowed tools for the agent runtime.
    """
    __tablename__ = "ai_agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(50), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    role_type = Column(SQLEnum(AgentRoleType), default=AgentRoleType.CUSTOM)
    is_enabled = Column(Boolean, default=True)

    # GPT Configuration
    model = Column(String(50), default="fable-5-high")
    reasoning_effort = Column(SQLEnum(ReasoningEffort), default=ReasoningEffort.MEDIUM)
    verbosity = Column(String(10), default="medium")  # low|medium|high
    max_output_tokens = Column(Integer, nullable=True)

    # Prompts & constraints
    system_prompt = Column(Text, nullable=True)
    guardrails_json = Column(JSON, nullable=True)  # formatting rules, constraints

    # Allowed tools (server-side enforced)
    tools_allowlist_json = Column(JSON, nullable=True)
    # e.g. ["fetch_market_data","compute_indicators","risk_policy_check"]

    # Market scope
    instruments_json = Column(JSON, nullable=True)    # ["XAUUSD","EURUSD"]
    timeframes_json = Column(JSON, nullable=True)     # ["M5","M15","H1","H4"]
    indicators_json = Column(JSON, nullable=True)     # ["RSI","MACD","ATR"]

    # Risk profile
    risk_profile_json = Column(JSON, nullable=True)
    # {max_risk_pct, min_sl_pips, min_tp_pips, max_orders, schedule}

    # Allowed actions
    allowed_actions = Column(String(50), default="analyze")
    # "analyze" | "propose" | "auto-place"

    version = Column(Integer, default=1)
    created_by = Column(Integer, nullable=True)
    updated_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Agent Assignments ──────────────────────────────────────

class AIAgentAssignment(AIBase):
    """Maps users to available agents."""
    __tablename__ = "ai_agent_assignments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    ai_agent_id = Column(Integer, nullable=False, index=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_assignments_unique", "user_id", "ai_agent_id", unique=True),
    )


# ── User Trade Settings ────────────────────────────────────

class AITradeSettings(AIBase):
    """
    Per-user AI trading preferences.
    
    Controls direction mode, lot sizing, paper mode, and account selection.
    Paper mode is default — live trade placement requires explicit opt-in.
    """
    __tablename__ = "ai_trade_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    mode = Column(SQLEnum(TradeMode), default=TradeMode.BOTH)
    lot_mode = Column(SQLEnum(LotMode), default=LotMode.FIXED)
    lot_size = Column(Float, nullable=True, default=0.01)
    lot_min = Column(Float, nullable=True, default=0.01)
    lot_max = Column(Float, nullable=True, default=1.0)
    paper_mode = Column(Boolean, default=True)  # SAFETY: default paper
    auto_place = Column(Boolean, default=False)  # requires elevated permission
    max_open_positions = Column(Integer, default=1)
    max_pending_orders = Column(Integer, default=2)
    max_daily_loss_percent = Column(Float, nullable=True)
    trading_hours_json = Column(JSON, nullable=True)  # [{from:"08:00",to:"16:00"}]
    mt5_account_id = Column(Integer, nullable=True)  # selected MT5 account
    selected_agent_id = Column(Integer, nullable=True)  # selected AI agent
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Trade Decisions (Audit Log) ────────────────────────────

class AITradeDecision(AIBase):
    """
    Immutable audit record of every AI analysis/decision.
    
    Records which agent version produced what decision, with full context.
    Required for explainability and regulatory compliance.
    """
    __tablename__ = "ai_trade_decisions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    ai_agent_id = Column(Integer, nullable=True)
    agent_version = Column(Integer, nullable=True)  # snapshot of agent version at decision time
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(10), nullable=True)
    decision_type = Column(SQLEnum(DecisionType), nullable=False)
    direction = Column(SQLEnum(DecisionDirection), default=DecisionDirection.NONE)
    entry_price = Column(Float, nullable=True)
    sl_price = Column(Float, nullable=True)
    tp_price = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    rationale = Column(Text, nullable=True)
    invalidation = Column(Text, nullable=True)
    signals_json = Column(JSON, nullable=True)
    structured_json = Column(JSON, nullable=True)  # full model output
    status = Column(SQLEnum(DecisionStatus), default=DecisionStatus.DRAFTED, index=True)
    blocked_reasons_json = Column(JSON, nullable=True)
    mt5_order_id = Column(String(50), nullable=True)
    # Request/response logs (secrets redacted)
    request_payload_json = Column(JSON, nullable=True)
    response_payload_json = Column(JSON, nullable=True)
    previous_response_id = Column(String(100), nullable=True)  # for multi-turn
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_ai_decisions_user_symbol", "user_id", "symbol", "created_at"),
    )


# ── Market Snapshots (Cache) ──────────────────────────────

class AIMarketSnapshot(AIBase):
    """Cached market data snapshot for agent context."""
    __tablename__ = "ai_market_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    ohlcv_json = Column(JSON, nullable=True)
    indicators_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_snapshots_symbol_tf", "symbol", "timeframe", "created_at"),
    )


# ── Multi-provider LLM accounts ───────────────────────────

class AILLMProvider(AIBase):
    """A configured AI provider account (with stored API key).

    Powers the unified, failover AI router used across the app: agent decisions,
    Telegram sniper entry analysis, signal generation, and insights.
    """
    __tablename__ = "ai_llm_providers"

    id = Column(Integer, primary_key=True, index=True)
    provider_key = Column(String(40), nullable=False)   # preset id e.g. 'groq', 'openrouter'
    label = Column(String(100), nullable=False)
    type = Column(String(30), nullable=False, default="openai_compatible")
    api_key = Column(String(400), nullable=True)
    base_url = Column(String(300), nullable=True)
    default_model = Column(String(120), nullable=True)
    models_json = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=100)  # lower = tried first
    free_tier = Column(Boolean, nullable=False, default=True)

    #: Dedicate this profile to one task category (a key of TASK_MODEL_CHAINS).
    #: A profile carrying a task serves ONLY that task and is held out of the
    #: general pool, so a slow vision read can never occupy the same rate limit
    #: as the chat path. Unique: two profiles cannot claim the same task, which
    #: is what keeps "one profile, one job" true rather than merely intended.
    #: NULL means the profile is part of the shared pool, as before.
    assigned_task = Column(String(40), nullable=True, unique=True, index=True)

    status = Column(String(20), nullable=False, default="unknown")  # unknown/ok/error
    last_error = Column(Text, nullable=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_model_used = Column(String(120), nullable=True)
    total_calls = Column(Integer, nullable=False, default=0)
    total_errors = Column(Integer, nullable=False, default=0)

    # Usage limits (to never exhaust free monthly tiers). null = unlimited.
    daily_limit = Column(Integer, nullable=True)
    monthly_limit = Column(Integer, nullable=True)
    daily_calls = Column(Integer, nullable=False, default=0)
    monthly_calls = Column(Integer, nullable=False, default=0)
    daily_reset_at = Column(DateTime, nullable=True)
    monthly_reset_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_llm_providers_enabled_priority", "enabled", "priority"),
    )


# ── Router settings (singleton) ────────────────────────────

class AIRouterSettings(AIBase):
    """Global settings for the shared AI router used by agents.

    A single row (id=1). Controls load-balancing strategy, whether the core
    trading agents route through the connected providers, per-agent token
    budgets, and the free-tier reserve buffer. Also master switches for the
    Headroom compression and Graphify knowledge integrations.
    """
    __tablename__ = "ai_router_settings"

    id = Column(Integer, primary_key=True, index=True)
    # priority | round_robin | least_used  (how the next provider is chosen)
    strategy = Column(String(20), nullable=False, default="round_robin")
    # When True, the core /agents pipeline calls the connected providers
    # instead of the local OpenAI key.
    agents_use_providers = Column(Boolean, nullable=False, default=True)
    # Token spend policy for the core agents:
    #   telegram_only — agents only spend tokens on telegram-signal / manual
    #                   analyses (the continuous background pair-scanner is
    #                   skipped so the free daily tier is preserved).
    #   always        — agents analyse on every background scan too.
    agent_token_mode = Column(String(20), nullable=False, default="telegram_only")
    # Hard ceiling on max_tokens any single agent call may request.
    per_agent_max_tokens = Column(Integer, nullable=False, default=800)
    # Stop using a provider once it reaches (limit * (1 - reserve_pct)).
    # e.g. 0.10 keeps a 10% buffer so the free monthly tier is never fully spent.
    reserve_pct = Column(Float, nullable=False, default=0.10)
    # Rotating cursor used by the round_robin strategy.
    round_robin_cursor = Column(Integer, nullable=False, default=0)
    # Integrations
    headroom_enabled = Column(Boolean, nullable=False, default=True)
    graphify_enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Usage event log (per call, per agent, per provider) ────

class AIUsageRecord(AIBase):
    """Append-only log of every routed LLM call.

    Captures token usage (for the provider tab + per-agent tracking) and the
    Headroom compression savings (orig vs compressed chars) for the
    Intelligence page. Aggregated by day/month for the usage dashboards.
    """
    __tablename__ = "ai_usage_records"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    provider_id = Column(Integer, nullable=True, index=True)
    provider_label = Column(String(100), nullable=True)
    agent_name = Column(String(100), nullable=True, index=True)
    agent_role = Column(String(60), nullable=True, index=True)
    model = Column(String(120), nullable=True)
    source = Column(String(40), nullable=True)  # agent | sniper | signal | chat | insight
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    # Headroom compression accounting (characters before/after compression)
    orig_chars = Column(Integer, nullable=False, default=0)
    comp_chars = Column(Integer, nullable=False, default=0)
    success = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_ai_usage_ts_agent", "ts", "agent_role"),
        Index("ix_ai_usage_ts_provider", "ts", "provider_id"),
    )


# ── Agent knowledge store (referenced when creating tasks) ─

class AIAgentKnowledge(AIBase):
    """Durable knowledge an agent stores and references on future tasks.

    Seeded from decision outcomes, insights, and Graphify code-map facts. The
    orchestrator queries the most relevant/weighted rows and injects them into
    the agent prompt so agents 'remember' what worked.
    """
    __tablename__ = "ai_agent_knowledge"

    id = Column(Integer, primary_key=True, index=True)
    agent_role = Column(String(60), nullable=True, index=True)  # null = shared/global
    symbol = Column(String(40), nullable=True, index=True)      # null = applies to all symbols
    kind = Column(String(30), nullable=False, default="insight")  # insight | outcome | graphify | rule
    title = Column(String(160), nullable=True)
    content = Column(Text, nullable=False)
    weight = Column(Float, nullable=False, default=1.0)  # higher = more important
    source = Column(String(60), nullable=True)
    hits = Column(Integer, nullable=False, default=0)  # times referenced
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_ai_knowledge_role_symbol", "agent_role", "symbol"),
    )
