"""
Agent Paul Plugin — SQLAlchemy Models

All tables prefixed with 'paul_' to avoid collisions.
All enum types are explicitly named 'paul_*' because Postgres enum types are
GLOBAL — an unnamed SQLEnum would collide with same-named core/plugin enums.
"""
from datetime import datetime
import enum

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON,
    Enum as SQLEnum, Index,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.timezone import now_sast


def _now():
    """Naive SAST timestamp (plugin tables are TIMESTAMP WITHOUT TIME ZONE)."""
    return now_sast()


class PaulBase(DeclarativeBase):
    """Separate declarative base for the Agent Paul plugin."""
    pass


# ── Enums ──────────────────────────────────────────────────


class PaulMode(str, enum.Enum):
    """Execution authority modes."""
    PAPER = "paper"                      # simulate only, never touches the exchange
    TRADEBOT_EXECUTE = "tradebot_execute"  # PAUL advises, TradeBot live engine executes
    PAUL_EXECUTE = "paul_execute"          # PAUL autonomously executes (still via engine)


class PaulProvenance(str, enum.Enum):
    """Where the trade plan came from."""
    AI = "ai"            # produced by the AI agent orchestrator
    HEURISTIC = "heuristic"  # produced by a local technical heuristic (AI disabled)


class PaulQualify(str, enum.Enum):
    """PAUL Qualify gate result (Execute/Qualify loop)."""
    PASS = "pass"
    CONCERNS = "concerns"
    BLOCKED = "blocked"


class PaulDecisionStatus(str, enum.Enum):
    """Lifecycle of a PAUL decision through Plan → Apply → Unify."""
    PLANNED = "planned"
    QUEUED = "queued"        # awaiting human approval
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"      # hold / blocked by Qualify
    UNIFIED = "unified"      # loop closed, outcome reconciled


# Reuse named-enum instances across columns (one CREATE TYPE per name).
_MODE = SQLEnum(PaulMode, name="paul_mode")
_PROVENANCE = SQLEnum(PaulProvenance, name="paul_provenance")
_QUALIFY = SQLEnum(PaulQualify, name="paul_qualify")
_STATUS = SQLEnum(PaulDecisionStatus, name="paul_decision_status")


# ── Settings (singleton row) ───────────────────────────────


class PaulSettings(PaulBase):
    __tablename__ = "paul_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=True, nullable=False)
    mode = Column(_MODE, default=PaulMode.PAPER, nullable=False)
    require_approval = Column(Boolean, default=True, nullable=False)  # human gate before live execution
    kill_switch = Column(Boolean, default=False, nullable=False)      # hard stop, blocks all decisions

    default_timeframe = Column(String, default="1h", nullable=False)
    min_confidence = Column(Float, default=0.65, nullable=False)      # 0..1 Qualify threshold
    allowed_symbols = Column(Text, nullable=True)                     # CSV; null/empty = all

    risk_max_position_usdt = Column(Float, default=100.0, nullable=False)
    risk_max_open_positions = Column(Integer, default=3, nullable=False)
    max_queue_size = Column(Integer, default=20, nullable=False)
    cooldown_minutes = Column(Integer, default=15, nullable=False)

    # MT5 defaults (used when a decision targets the MT5 market)
    mt5_default_account_id = Column(Integer, nullable=True)
    mt5_default_volume = Column(Float, default=0.01, nullable=False)
    mt5_timeframe = Column(String, default="H1", nullable=False)
    mt5_min_rr = Column(Float, default=2.0, nullable=False)

    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


# ── Decisions (one row per PAUL loop) ──────────────────────


class PaulDecision(PaulBase):
    __tablename__ = "paul_decisions"

    id = Column(Integer, primary_key=True)
    session_id = Column(String, index=True, nullable=True)

    symbol = Column(String, index=True, nullable=False)
    timeframe = Column(String, default="1h", nullable=False)
    trigger = Column(String, default="manual", nullable=False)  # manual / scanner / api

    # Target venue: 'crypto' (Bitget live engine) or 'mt5' (MT5 plugin)
    market = Column(String, default="crypto", nullable=False, index=True)
    account_id = Column(Integer, nullable=True)  # MT5 account id when market == 'mt5'
    volume = Column(Float, nullable=True)        # MT5 lot size

    mode = Column(_MODE, nullable=False)
    provenance = Column(_PROVENANCE, default=PaulProvenance.AI, nullable=False)

    # ── PLAN ──
    action = Column(String, default="hold", nullable=False)  # buy / sell / hold
    confidence = Column(Float, default=0.0, nullable=False)
    entry = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    risk_reward = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    acceptance_criteria = Column(JSON, nullable=True)  # PAUL Given/When/Then list
    plan_json = Column(JSON, nullable=True)            # raw orchestrator / heuristic output

    # ── QUALIFY ──
    qualify_status = Column(_QUALIFY, default=PaulQualify.PASS, nullable=False)
    qualify_notes = Column(Text, nullable=True)

    # ── APPLY ──
    status = Column(_STATUS, default=PaulDecisionStatus.PLANNED, index=True, nullable=False)
    signal_id = Column(Integer, nullable=True)         # core Signal id (reference only, no FK)
    execution_result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)

    # ── UNIFY ──
    outcome = Column(String, nullable=True)            # win / loss / break_even / open
    outcome_pnl = Column(Float, nullable=True)
    unify_notes = Column(Text, nullable=True)
    unified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=_now, nullable=False, index=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


Index("ix_paul_decisions_symbol_status", PaulDecision.symbol, PaulDecision.status)


# ── JARVIS Chat persistence ────────────────────────────────


class PaulConversation(PaulBase):
    """A JARVIS chat session. One active (non-archived) row per session_key."""
    __tablename__ = "paul_conversations"

    id = Column(Integer, primary_key=True)
    session_key = Column(String, index=True, nullable=False)  # browser/client id
    title = Column(String, nullable=True)                     # auto from first message
    archived = Column(Boolean, default=False, nullable=False, index=True)
    message_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=_now, nullable=False, index=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class PaulChatMessage(PaulBase):
    """A single message inside a JARVIS conversation."""
    __tablename__ = "paul_chat_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, index=True, nullable=False)
    role = Column(String, nullable=False)          # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    pathname = Column(String, nullable=True)       # page the user was on
    created_at = Column(DateTime, default=_now, nullable=False, index=True)


Index("ix_paul_chat_messages_conv", PaulChatMessage.conversation_id, PaulChatMessage.created_at)


# ── Learned knowledge (JARVIS long-term memory) ────────────


class PaulKnowledge(PaulBase):
    """Distilled facts / news / research JARVIS has learned over time.

    Sources: chat distillation, RSS news ingestion, online research.
    Used to ground future answers and market predictions.
    """
    __tablename__ = "paul_knowledge"

    id = Column(Integer, primary_key=True)
    kind = Column(String, default="insight", nullable=False, index=True)  # insight|news|research
    source = Column(String, nullable=True)        # feed name / domain / 'chat'
    title = Column(String, nullable=True)
    url = Column(String, nullable=True)
    symbol = Column(String, nullable=True, index=True)  # related pair if any
    topic = Column(String, nullable=True, index=True)   # crypto|stocks|macro|general
    content = Column(Text, nullable=False)
    sentiment = Column(Float, nullable=True)      # -1..1 (news/research)
    importance = Column(Float, default=0.5, nullable=False)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False, index=True)


Index("ix_paul_knowledge_kind_created", PaulKnowledge.kind, PaulKnowledge.created_at)


# ── JARVIS Skills (capability modules JARVIS learns and reuses) ────────────


class PaulSkill(PaulBase):
    """A JARVIS skill — domain knowledge or behaviour JARVIS injects when relevant.

    When the user's message matches any of the trigger_keywords, JARVIS
    prepends the skill's system_prompt_addition to its context so it
    answers in specialist mode. Skills can optionally be powered by a
    specific AI provider.
    """
    __tablename__ = "paul_skills"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    trigger_keywords = Column(JSON, default=list, nullable=False)   # list[str]
    system_prompt_addition = Column(Text, nullable=False)            # injected into system prompt
    ai_provider_id = Column(Integer, nullable=True)                  # refs ai_llm_providers.id
    enabled = Column(Boolean, default=True, nullable=False, index=True)
    is_default = Column(Boolean, default=False, nullable=False)      # shipped with plugin
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


# ── JARVIS Hooks (trigger-based automated actions) ─────────────────────────


class PaulHookTrigger(str, enum.Enum):
    ON_SIGNAL   = "on_signal"          # fires when a new signal is created
    ON_POSITION = "on_position_change" # fires when an MT5 position changes P&L
    ON_PRICE    = "on_price_threshold" # fires when a pair crosses a price
    ON_SCHEDULE = "on_schedule"        # fires on a cron-like schedule
    ON_COMMAND  = "on_voice_command"   # fires when a voice pattern is detected


_HOOK_TRIGGER = SQLEnum(PaulHookTrigger, name="paul_hook_trigger")


class PaulHook(PaulBase):
    """A JARVIS hook — an automated reaction to an event.

    When the trigger condition fires, JARVIS runs action_template (a prompt
    with {{variable}} substitution) against the linked AI provider and either
    speaks the result, executes a trade, or sends an alert.
    """
    __tablename__ = "paul_hooks"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    trigger_type = Column(_HOOK_TRIGGER, nullable=False)
    condition = Column(JSON, default=dict, nullable=False)        # type-specific conditions
    action_template = Column(Text, nullable=False)                # prompt template
    action_type = Column(String(40), default="speak", nullable=False)  # speak|trade|alert
    ai_provider_id = Column(Integer, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False, index=True)
    is_default = Column(Boolean, default=False, nullable=False)
    last_fired_at = Column(DateTime, nullable=True)
    fire_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

