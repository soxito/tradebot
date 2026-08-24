"""
Database Models for TradeBot
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import Column, String, Float, Boolean, DateTime, Text, Enum as SQLEnum, Integer, JSON, ForeignKey, event
from sqlalchemy.orm import DeclarativeBase, validates
import enum

from app.core.timezone import now_sast


def _utcnow():
    """Return current SAST time as naive datetime."""
    return now_sast()


class Base(DeclarativeBase):
    pass


class SignalAction(str, enum.Enum):
    """Signal action enumeration"""
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


class SignalSource(str, enum.Enum):
    """Signal source enumeration"""
    TRADINGVIEW = "tradingview"
    SENTIMENT = "sentiment"
    MANUAL = "manual"
    SYSTEM = "system"
    SMC = "smc"


class SignalStatus(str, enum.Enum):
    """Signal processing status"""
    PENDING = "pending"
    PROCESSING = "processing"
    EXECUTED = "executed"
    FAILED = "failed"
    IGNORED = "ignored"


class Signal(Base):
    """Trading signal model"""
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Signal identification
    external_id = Column(String, unique=True, index=True, nullable=True)
    source = Column(SQLEnum(SignalSource), nullable=False, index=True)
    
    # Trading information
    symbol = Column(String, nullable=False, index=True)  # e.g., BTC/USDT
    action = Column(SQLEnum(SignalAction), nullable=False)
    
    # Price and timing
    price = Column(Float, nullable=True)  # Price at signal generation
    timeframe = Column(String, nullable=True)  # e.g., 1h, 4h, 1d
    
    # Signal strength and confidence
    strength = Column(Float, default=0.5)  # 0.0 to 1.0
    confidence = Column(Float, default=0.5)  # 0.0 to 1.0
    
    # Metadata
    raw_data = Column(Text, nullable=True)  # JSON dump of original signal
    indicators = Column(Text, nullable=True)  # JSON dump of indicator values
    
    # Processing status
    status = Column(SQLEnum(SignalStatus), default=SignalStatus.PENDING, index=True)
    processed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Trade(Base):
    """Trade execution record"""
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Trade identification
    exchange = Column(String, nullable=False, index=True)
    exchange_order_id = Column(String, nullable=True)
    signal_id = Column(Integer, nullable=True, index=True)  # Link to signal
    source = Column(String, default="signal", nullable=False)  # signal / sniper
    
    # Trading details
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)  # buy/sell
    trade_side = Column(String, nullable=True)  # open/close
    order_type = Column(String, nullable=False)  # market/limit
    
    # Amounts and prices
    amount = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
    filled_amount = Column(Float, default=0.0)
    average_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    margin_mode = Column(String, nullable=True)
    leverage = Column(Integer, nullable=True)
    
    # Fees and costs
    fee = Column(Float, default=0.0)
    fee_currency = Column(String, nullable=True)
    cost = Column(Float, nullable=True)  # Total cost in quote currency
    
    # PnL (for closed positions)
    pnl = Column(Float, nullable=True)
    pnl_percentage = Column(Float, nullable=True)
    
    # Status and metadata
    status = Column(String, nullable=False, index=True)  # open, closed, canceled, failed
    raw_response = Column(Text, nullable=True)  # JSON dump of exchange response
    error_message = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    closed_at = Column(DateTime, nullable=True)


class SentimentScore(Base):
    """Sentiment analysis scores"""
    __tablename__ = "sentiment_scores"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Asset information
    symbol = Column(String, nullable=False, index=True)  # e.g., BTC, ETH
    
    # Sentiment scores
    score = Column(Float, nullable=False)  # -1.0 (bearish) to 1.0 (bullish)
    magnitude = Column(Float, default=0.5)  # 0.0 to 1.0 (strength of sentiment)
    
    # Source breakdown
    news_score = Column(Float, nullable=True)
    social_score = Column(Float, nullable=True)
    technical_score = Column(Float, nullable=True)
    
    # Metadata
    sources_count = Column(Integer, default=0)
    raw_data = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    valid_until = Column(DateTime, nullable=True)  # Expiry time for this score


class NewsArticle(Base):
    """Persistent storage for every news article fetched — builds knowledge base."""
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    source = Column(String, nullable=False, index=True)
    url = Column(String, nullable=True)
    category = Column(String, default="general", index=True)  # macro, crypto, forex, stocks, futures
    symbols = Column(Text, nullable=True)  # JSON array: ["BTC", "ETH"]
    reliability = Column(Float, default=0.5)

    # Sentiment analysis snapshot at fetch time
    sentiment_score = Column(Float, nullable=True)  # -1.0 to 1.0
    sentiment_magnitude = Column(Float, nullable=True)
    sentiment_label = Column(String, nullable=True)  # bullish, bearish, neutral

    # Dedup + timestamps
    title_hash = Column(String, nullable=False, index=True)  # For fast dedup
    published_at = Column(DateTime, nullable=True, index=True)
    fetched_at = Column(DateTime, default=_utcnow, nullable=False, index=True)


# ─── Simulation / Paper Trading Models ───────────────────────────


class SimAccount(Base):
    """Simulation account — virtual balance for paper trading"""
    __tablename__ = "sim_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="Default", nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)

    # Virtual balance (quote currency — USDT)
    balance = Column(Float, default=10000.0, nullable=False)
    initial_balance = Column(Float, default=10000.0, nullable=False)

    # Running totals
    total_pnl = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)

    # AI agents
    enable_ai = Column(Boolean, default=False, nullable=False)  # AI agent validation for sim trades
    auto_trade_ai_provider = Column(String, default="orchestrator", nullable=False)  # orchestrator / tradingagents
    tradingagents_llm_provider = Column(String, default="openai", nullable=False)
    tradingagents_deep_think_llm = Column(String, default="gpt-5.4", nullable=False)
    tradingagents_quick_think_llm = Column(String, default="gpt-5.4-mini", nullable=False)
    tradingagents_backend_url = Column(String, nullable=True)
    tradingagents_max_debate_rounds = Column(Integer, default=2, nullable=False)
    tradingagents_max_risk_discuss_rounds = Column(Integer, default=2, nullable=False)

    # Auto-trade settings
    auto_trade = Column(Boolean, default=False)
    auto_trade_pairs = Column(Text, nullable=True)  # JSON list of symbols
    auto_trade_timeframe = Column(String, default="1h")
    auto_trade_max_positions = Column(Integer, default=5)
    auto_trade_risk_pct = Column(Float, default=2.0)  # % of balance per trade
    auto_trade_mode = Column(String, default="spot")  # spot / futures
    auto_trade_leverage = Column(Integer, default=10)
    auto_trade_margin_mode = Column(String, default="crossed")  # crossed / isolated
    auto_trade_amount_mode = Column(String, default="quote")  # quote (USDT) / base (pair qty)
    auto_trade_pine_script_id = Column(Integer, nullable=True)  # selected Pine Script for trade decisions
    margin_size_usdt = Column(Float, default=10.0)  # exact margin per trade in USDT
    min_entry_gap_pct = Column(Float, default=2.0)  # min price gap % before DCA
    min_confidence = Column(Float, default=0.90)  # minimum signal confidence to trade (0.50-1.0)
    sniper_max_entries = Column(Integer, default=1)  # max sniper entries per token (1-10)
    min_pump_pct = Column(Float, default=30.0)  # min 24h % gain to flag as rug pull

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class SimOrder(Base):
    """Simulated order"""
    __tablename__ = "sim_orders"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    signal_id = Column(Integer, nullable=True, index=True)

    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)  # buy / sell
    order_type = Column(String, nullable=False)  # market / limit
    amount = Column(Float, nullable=False)  # base currency qty
    price = Column(Float, nullable=False)  # execution / limit price
    cost = Column(Float, nullable=False)  # amount * price

    # Stop-loss / take-profit attached to this order
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    sl_type = Column(String, nullable=True)  # atr / bb / pct / support

    # Trade type metadata
    trade_type = Column(String, default="spot")  # spot / futures
    margin_mode = Column(String, nullable=True)  # crossed / isolated
    leverage = Column(Integer, nullable=True)

    status = Column(String, default="filled", index=True)  # filled / canceled
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class SimPosition(Base):
    """Open simulated position"""
    __tablename__ = "sim_positions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    order_id = Column(Integer, nullable=True)
    signal_id = Column(Integer, nullable=True)

    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)  # long / short
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)

    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    # Indicates how the SL was set: 'signal' | 'smart' | 'trailing' | None
    sl_type = Column(String, nullable=True)
    trade_type = Column(String, default="spot")  # spot / futures
    margin_mode = Column(String, nullable=True)  # crossed / isolated
    leverage = Column(Integer, nullable=True)

    unrealized_pnl = Column(Float, default=0.0)
    status = Column(String, default="open", index=True)  # open / closed
    closed_at = Column(DateTime, nullable=True)
    realized_pnl = Column(Float, nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class BotStrategy(Base):
    """Bot signal strategy configuration"""
    __tablename__ = "bot_strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    pairs = Column(Text, default="[]")  # JSON list of symbols
    timeframe = Column(String, default="1h")
    indicators = Column(Text, default="[]")  # JSON list of indicator configs
    buy_threshold = Column(Float, default=0.25)
    sell_threshold = Column(Float, default=-0.25)
    stop_loss_pct = Column(Float, default=2.0)
    take_profit_pct = Column(Float, default=4.0)
    trade_type = Column(String, default="spot")
    leverage = Column(Integer, default=1)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PineScript(Base):
    """Saved TradingView Pine Scripts"""
    __tablename__ = "pine_scripts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    strategy_id = Column(Integer, nullable=True, index=True)
    script_type = Column(String, default="indicator")  # indicator / strategy
    code = Column(Text, default="")
    pairs = Column(Text, default="[]")  # JSON list of symbols this script applies to
    is_active = Column(Boolean, default=False)  # Enable for signal pipeline
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ─── Live Auto-Trade Settings ───────────────────────────────


class LiveTradeSettings(Base):
    """Live auto-trade settings (singleton row like SimAccount)."""
    __tablename__ = "live_trade_settings"

    id = Column(Integer, primary_key=True, index=True)
    is_active = Column(Boolean, default=False, nullable=False)    # master toggle
    auto_trade = Column(Boolean, default=False)                   # auto-trade enabled
    dry_run = Column(Boolean, default=True, nullable=False)       # plan orders without sending them
    enable_ai = Column(Boolean, default=True, nullable=False)     # AI agent validation for live trades
    auto_trade_ai_provider = Column(String, default="orchestrator", nullable=False)  # orchestrator / tradingagents
    tradingagents_llm_provider = Column(String, default="openai", nullable=False)
    tradingagents_deep_think_llm = Column(String, default="gpt-5.4", nullable=False)
    tradingagents_quick_think_llm = Column(String, default="gpt-5.4-mini", nullable=False)
    tradingagents_backend_url = Column(String, nullable=True)
    tradingagents_max_debate_rounds = Column(Integer, default=2, nullable=False)
    tradingagents_max_risk_discuss_rounds = Column(Integer, default=2, nullable=False)

    # Trading pairs & parameters
    auto_trade_pairs = Column(Text, nullable=True)                # JSON list of symbols
    auto_trade_timeframe = Column(String, default="1h")
    auto_trade_max_positions = Column(Integer, default=3)
    auto_trade_risk_pct = Column(Float, default=1.0)              # % of balance per trade
    auto_trade_mode = Column(String, default="futures")           # spot / futures
    auto_trade_amount_mode = Column(String, default="quote")      # quote / base
    auto_trade_leverage = Column(Integer, default=10)
    auto_trade_margin_mode = Column(String, default="crossed")    # crossed / isolated
    auto_trade_pine_script_id = Column(Integer, nullable=True)

    # Safety limits
    max_position_size_usdt = Column(Float, default=500.0)
    max_total_exposure_usdt = Column(Float, default=5000.0)
    margin_size_usdt = Column(Float, default=10.0)  # exact margin per trade in USDT
    min_entry_gap_pct = Column(Float, default=2.0)  # min price gap % before DCA
    min_confidence = Column(Float, default=0.90)  # minimum signal confidence to trade (0.50-1.0)
    sniper_max_entries = Column(Integer, default=1)  # max sniper entries per token (1-10)
    sniper_max_positions = Column(Integer, default=5)  # max TOTAL concurrent sniper positions
    min_pump_pct = Column(Float, default=30.0)  # min 24h % gain to flag as rug pull

    # Stats
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ─── Signal Monitoring Pairs ────────────────────────────────


class SignalMonitorPair(Base):
    """User-configured pairs for signal pipeline monitoring."""
    __tablename__ = "signal_monitor_pairs"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, unique=True, index=True)  # e.g. "BTC/USDT"
    is_active = Column(Boolean, default=True, nullable=False)
    source = Column(String, default="user", nullable=False)  # "user" or "trending"
    created_at = Column(DateTime, default=_utcnow, nullable=False)


# ─── AI Agent Models ────────────────────────────────────────


class AgentRole(str, enum.Enum):
    """Agent specialization roles"""
    MARKET_ANALYST = "market_analyst"
    SIGNAL_GENERATOR = "signal_generator"
    RISK_MANAGER = "risk_manager"
    TRADE_EXECUTOR = "trade_executor"
    SENTIMENT_ANALYST = "sentiment_analyst"
    CUSTOM = "custom"


class Agent(Base):
    """AI Trading Agent configuration"""
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # AgentRole value
    description = Column(Text, nullable=True)
    system_prompt = Column(Text, nullable=False)
    model = Column(String, default="fable-5-high", nullable=False)
    temperature = Column(Float, default=0.3, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    pairs = Column(Text, nullable=True)  # comma-separated, null = all
    max_tokens = Column(Integer, default=2000, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AgentDecision(Base):
    """Record of agent decisions / analysis"""
    __tablename__ = "agent_decisions"

    _QUOTA_PHRASES = ("insufficient_quota", "exceeded your current quota", "billing")

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)
    agent_name = Column(String, nullable=False)
    agent_role = Column(String, nullable=False)
    symbol = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)  # buy, sell, hold, skip
    confidence = Column(Float, default=0.0)
    reasoning = Column(Text, nullable=True)
    market_data = Column(Text, nullable=True)  # JSON snapshot of data used
    signal_id = Column(Integer, nullable=True)  # linked signal if applicable
    session_id = Column(String, nullable=True, index=True)  # groups decisions from one orchestration run
    # ── Outcome tracking (learning) ──
    outcome = Column(String, nullable=True)  # win, loss, break_even, null=pending
    outcome_pnl = Column(Float, nullable=True)  # realized PnL from the trade
    outcome_recorded_at = Column(DateTime, nullable=True)
    ai_called = Column(Boolean, default=True)  # True=OpenAI, False=local memory
    memory_context_used = Column(Integer, default=0)  # how many past decisions were injected as context
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    def is_quota_error(self) -> bool:
        """Check if this decision contains a quota error that should not be stored."""
        for value in (self.reasoning, self.market_data):
            low = str(value or "").lower()
            if any(p in low for p in self._QUOTA_PHRASES):
                return True
        return False


class JarvisAnalysisJournal(Base):
    """Every trade proposal JARVIS made, and what price actually did next.

    Why this exists
    ---------------
    ``agent_decisions`` has outcome columns but nothing fills them — an outcome
    is only ever written by a manual API call, so in practice the crypto side of
    the assistant never learned anything. The MT5/SMC path *does* close its loop
    (see ``smc_memory``), and this is the equivalent for everything JARVIS
    proposes: record the setup, let a background loop settle it against real
    candles, and feed the realised hit rate back into the prompt.

    ``outcome`` is one of win / loss / break_even / expired / no_fill / NULL.
    ``no_fill`` and ``expired`` are kept out of the win rate but reported
    alongside it: proposing entries that price never reaches is a real failure
    mode, and hiding it would make the statistics flattering instead of useful.
    """

    __tablename__ = "jarvis_analysis_journal"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    source = Column(String, nullable=False, index=True)   # jarvis_command, telegram, paul_chat
    symbol = Column(String, nullable=False, index=True)
    asset_class = Column(String, nullable=True)           # crypto|fx|metal|index|energy|soft
    timeframe = Column(String, nullable=True)
    side = Column(String, nullable=False)                 # long | short
    # ── The proposal as published to the user ──
    entry = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=True)
    rr1 = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    price_at_analysis = Column(Float, nullable=True)
    price_source = Column(String, nullable=True)
    features = Column(Text, nullable=True)                # JSON: trend, rsi, atr, ema…
    # ── What actually happened ──
    outcome = Column(String, nullable=True, index=True)
    outcome_r = Column(Float, nullable=True)              # realised R multiple
    mfe = Column(Float, nullable=True)                    # max favourable excursion
    mae = Column(Float, nullable=True)                    # max adverse excursion
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)           # sl | tp | expiry | no_fill
    bars_to_outcome = Column(Integer, nullable=True)
    settled_at = Column(DateTime, nullable=True)


# ─── Rug Pull / Pump Detection ──────────────────────────────


class RugPullStatus(str, enum.Enum):
    """Status of a detected pump token"""
    WATCHING = "watching"        # Detected 100%+ pump, monitoring closely
    ENTRY_READY = "entry_ready"  # AI found a good short entry
    COOLING = "cooling"          # High risk — monitoring until risk drops to medium/low
    SHORTED = "shorted"          # Short position opened
    DUMPED = "dumped"            # Token has dumped (rug confirmed)
    SURVIVED = "survived"        # Token held — not a rug pull
    EXPIRED = "expired"          # Monitoring window elapsed, no action taken


class RugPullToken(Base):
    """Tracks tokens that pumped 100%+ — potential rug pulls to short."""
    __tablename__ = "rug_pull_tokens"

    id = Column(Integer, primary_key=True, index=True)

    # Token identification
    coin_id = Column(String, nullable=False, index=True)       # CoinGecko ID
    symbol = Column(String, nullable=False, index=True)        # e.g. "PEPE"
    name = Column(String, nullable=False)
    image = Column(String, nullable=True)

    # Pump metrics at detection time
    price_at_detection = Column(Float, nullable=False)
    price_change_24h = Column(Float, nullable=False)           # % change that triggered detection
    market_cap = Column(Float, nullable=True)
    volume_24h = Column(Float, nullable=True)
    market_cap_rank = Column(Integer, nullable=True)

    # Current tracking
    current_price = Column(Float, nullable=True)
    price_change_since_detection = Column(Float, default=0.0)  # % change since we started watching
    peak_price = Column(Float, nullable=True)                  # highest price seen while watching
    peak_change_pct = Column(Float, default=0.0)               # % from detection to peak

    # AI analysis
    ai_analysis = Column(Text, nullable=True)                  # JSON: AI reasoning / entry suggestions
    risk_score = Column(Float, nullable=True)                  # 0-1: how likely this is a rug pull
    recommended_entry = Column(Float, nullable=True)           # AI-suggested short entry price
    recommended_sl = Column(Float, nullable=True)              # Suggested SL
    recommended_tp = Column(Float, nullable=True)              # Suggested TP

    # Status
    status = Column(SQLEnum(RugPullStatus), default=RugPullStatus.WATCHING, index=True)
    trade_id = Column(Integer, nullable=True)                  # linked Trade if shorted

    # Timestamps
    detected_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    expired_at = Column(DateTime, nullable=True)


# ─── Pre-Pump Monitor Models ─────────────────────────────────


class PumpStatus(str, enum.Enum):
    """Status of a pre-pump detected token"""
    DETECTED = "detected"        # Early pump signals detected
    CONFIRMED = "confirmed"      # Multiple indicators confirm pump building
    SIGNALLED = "signalled"      # Signal created, ready for trade
    TRADED = "traded"            # Position opened
    PUMPED = "pumped"            # Token pumped as predicted
    FADED = "faded"              # Pump signal faded / false positive
    EXPIRED = "expired"          # Monitoring window elapsed


class PumpToken(Base):
    """Tracks tokens showing early pump indicators — potential longs."""
    __tablename__ = "pump_tokens"

    id = Column(Integer, primary_key=True, index=True)

    # Token identification
    coin_id = Column(String, nullable=False, index=True)       # CoinGecko ID
    symbol = Column(String, nullable=False, index=True)        # e.g. "PEPE"
    name = Column(String, nullable=False)
    image = Column(String, nullable=True)

    # Detection metrics
    price_at_detection = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    price_change_1h = Column(Float, nullable=True)             # % change last 1h
    price_change_24h = Column(Float, nullable=True)            # % change last 24h
    volume_24h = Column(Float, nullable=True)
    volume_change_pct = Column(Float, nullable=True)           # volume spike %
    market_cap = Column(Float, nullable=True)
    market_cap_rank = Column(Integer, nullable=True)

    # Extended detection metrics
    price_change_7d = Column(Float, nullable=True)             # % change last 7d
    high_24h = Column(Float, nullable=True)                    # 24h high price
    low_24h = Column(Float, nullable=True)                     # 24h low price
    ath = Column(Float, nullable=True)                         # all-time high
    ath_change_pct = Column(Float, nullable=True)              # % from ATH
    fully_diluted_valuation = Column(Float, nullable=True)     # FDV

    # Pump indicators (0-1 each, combined into pump_score)
    volume_spike_score = Column(Float, default=0.0)            # abnormal volume detected
    price_accel_score = Column(Float, default=0.0)             # price accelerating upward
    social_score = Column(Float, default=0.0)                  # social/trending momentum
    order_flow_score = Column(Float, default=0.0)              # buy-side pressure

    # New advanced indicators (0-1 each)
    momentum_score = Column(Float, default=0.0)                # multi-timeframe momentum consistency
    btc_relative_score = Column(Float, default=0.0)            # outperformance vs BTC
    volatility_score = Column(Float, default=0.0)              # healthy volatility (not just noise)
    ath_breakout_score = Column(Float, default=0.0)            # ATH proximity / breakout signal

    pump_score = Column(Float, default=0.0)                    # combined score (0-1)

    # BTC market context at time of detection
    btc_price_1h_pct = Column(Float, nullable=True)            # BTC 1h% when detected
    btc_price_24h_pct = Column(Float, nullable=True)           # BTC 24h% when detected
    market_sentiment = Column(String, nullable=True)            # "bullish"/"bearish"/"neutral"

    # Watchlist
    is_watchlist = Column(Boolean, default=False, index=True)  # always-monitored coin (BTC/ETH/SOL/XRP)

    # Tracking
    peak_price = Column(Float, nullable=True)                  # highest price seen
    peak_gain_pct = Column(Float, default=0.0)                 # max % gain from detection
    gain_since_detection = Column(Float, default=0.0)          # current % gain from detection

    # Trade link
    trade_id = Column(Integer, nullable=True)
    signal_id = Column(Integer, nullable=True)

    # Status
    status = Column(SQLEnum(PumpStatus), default=PumpStatus.DETECTED, index=True)

    # Timestamps
    detected_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    expired_at = Column(DateTime, nullable=True)


# ─── Strategy Lab Models ───────────────────────────────────


class StrategyRunStatus(str, enum.Enum):
    """Lifecycle status for a Strategy Lab run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StrategyLabPromotionTarget(str, enum.Enum):
    """Promotion destination for a strategy version."""

    SIMULATION = "simulation"
    LIVE = "live"


class StrategyLabVersion(Base):
    """Versioned strategy definition used by the Strategy Lab."""

    __tablename__ = "strategy_lab_versions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, index=True)
    description = Column(Text, nullable=True)
    timeframe = Column(String(20), default="1h", nullable=False)
    pairs = Column(Text, default="[]", nullable=False)  # JSON array
    indicators = Column(Text, default="[]", nullable=False)  # JSON array
    parameters = Column(Text, default="{}", nullable=False)  # JSON object
    risk_constraints = Column(Text, default="{}", nullable=False)  # JSON object
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(100), nullable=True)
    updated_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class StrategyLabRun(Base):
    """Execution record for a Strategy Lab strategy version."""

    __tablename__ = "strategy_lab_runs"

    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, nullable=False, index=True)
    run_mode = Column(String(30), default="simulation", nullable=False)
    status = Column(SQLEnum(StrategyRunStatus), default=StrategyRunStatus.QUEUED, nullable=False, index=True)
    metrics = Column(Text, default="{}", nullable=False)  # JSON object
    notes = Column(Text, nullable=True)
    started_at = Column(DateTime, default=_utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class StrategyLabPromotion(Base):
    """Audit trail for strategy promotions into simulation/live execution."""

    __tablename__ = "strategy_lab_promotions"

    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, nullable=False, index=True)
    target = Column(
        SQLEnum(StrategyLabPromotionTarget),
        default=StrategyLabPromotionTarget.SIMULATION,
        nullable=False,
        index=True,
    )
    approved_by = Column(String(100), nullable=True)
    reason = Column(Text, nullable=True)
    metadata_json = Column(Text, default="{}", nullable=False)  # JSON object
    created_at = Column(DateTime, default=_utcnow, nullable=False)


# ─── Crypto Pair Catalog ───────────────────────────────────
# Single source of truth mapping every Bitget-tradeable pair to its real coin
# name + live market metadata (market cap / 24h volume) and a lightweight
# CoinGecko profile. Seeded from Bitget ccxt markets and enriched from CoinGecko
# by app/services/pair_catalog.py. Lets JARVIS talk about coins by NAME
# ("Bitcoin" instead of "BTCUSDT") and resolve spoken names/tickers to a
# tradeable pair. Auto-created via init_db()'s Base.metadata.create_all.


class CryptoPair(Base):
    """A Bitget-tradeable crypto pair enriched with CoinGecko metadata."""

    __tablename__ = "crypto_pairs"

    id = Column(Integer, primary_key=True, index=True)

    # Identity
    symbol = Column(String, unique=True, index=True, nullable=False)  # "BTC/USDT"
    base = Column(String, index=True, nullable=False)                 # "BTC"
    quote = Column(String, nullable=False)                            # "USDT"

    # CoinGecko linkage + profile
    coingecko_id = Column(String, nullable=True, index=True)          # "bitcoin"
    name = Column(String, index=True, nullable=True)                  # "Bitcoin"
    description = Column(Text, nullable=True)                          # lightweight summary
    categories = Column(JSON, nullable=True)                          # ["Layer 1", ...]
    links = Column(JSON, nullable=True)                               # {homepage, whitepaper, explorer}
    aliases = Column(JSON, nullable=True)                             # learned user aliases (lowercased)

    # Live market metadata
    market_cap = Column(Float, nullable=True)
    market_cap_rank = Column(Integer, nullable=True)
    volume_24h = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    price_change_24h = Column(Float, nullable=True)

    # Status
    tradeable = Column(Boolean, default=True, index=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    enriched_at = Column(DateTime, nullable=True)  # last CoinGecko profile enrich


# ─── Ngrok Tunnel Config ───────────────────────────────────────────────────────


class NgrokConfig(Base):
    """Persistent ngrok configuration/overrides. Only ever one row (id=1)."""

    __tablename__ = "ngrok_config"

    id = Column(Integer, primary_key=True, default=1)
    # Override env defaults; NULL means "use env value"
    authtoken_override = Column(String, nullable=True)
    backend_addr_override = Column(String, nullable=True)
    frontend_addr_override = Column(String, nullable=True)
    enable_on_start = Column(Boolean, nullable=True)  # NULL = honour env NGROK_AUTO_START
    # Metadata
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ─── Trading Room ──────────────────────────────────────────────────────────────


class RoomAgentProfile(Base):
    """Who an agent *is* in the trading room: their human name, seat and brief.

    Kept apart from ``agents`` so the room can be re-skinned (renamed, reseated,
    re-tasked) without touching the prompt/model config the orchestrator runs on.
    """

    __tablename__ = "room_agent_profiles"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), unique=True, nullable=False)

    human_name = Column(String, nullable=False)      # "Sakhile"
    title = Column(String, nullable=False)           # "Market Analyst"
    color = Column(String, default="#94a3b8", nullable=False)
    seat = Column(Integer, default=0, nullable=False)
    # "male" | "female" — picks the body proportions and hair in the 3D room.
    gender = Column(String, default="male", nullable=False)
    # Free-text brief shown in the room and appended to the agent's system prompt.
    tasks = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AgentInstructionRevision(Base):
    """One rewrite of an agent's standing instructions, with the evidence for it.

    Self-improvement edits the prompt an agent runs on, so every change is
    recorded rather than applied silently: what it said before, what it says
    now, and the measured performance that justified the change. That makes a
    bad revision diagnosable and revertable instead of mysterious.
    """

    __tablename__ = "agent_instruction_revisions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False, index=True)

    previous_instructions = Column(Text, nullable=True)
    new_instructions = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)

    # The window that justified the rewrite.
    decisions_reviewed = Column(Integer, default=0, nullable=False)
    win_rate = Column(Float, nullable=True)
    avg_pnl = Column(Float, nullable=True)

    # False once superseded or rolled back, so the active text is unambiguous.
    applied = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class RoomSettings(Base):
    """Execution policy for the trading room. Only ever one row (id=1).

    Defaults are deliberately inert: execution off, and dry-run on so the first
    thing that happens after switching it on is a logged intent, not an order.
    """

    __tablename__ = "room_settings"

    id = Column(Integer, primary_key=True, default=1)

    # ── Master gates ──
    execution_enabled = Column(Boolean, default=False, nullable=False)
    #: Routing, not silence. On: the demo/paper account takes every trade for
    #: real and the live account is neither traded nor managed. Off: demo and
    #: live take the same trade at the same moment, so the demo stays a running
    #: mirror to watch. See ``app.agents.execution.mt5_targets``.
    dry_run = Column(Boolean, default=True, nullable=False)

    # ── Venue routing ──
    allow_sim = Column(Boolean, default=True, nullable=False)
    allow_crypto = Column(Boolean, default=False, nullable=False)
    allow_mt5 = Column(Boolean, default=False, nullable=False)
    mt5_account_id = Column(Integer, nullable=True)  # the live account
    #: Superseded by ``dry_run``, which is now the single switch deciding
    #: whether the live account trades. Kept so old rows and the settings API
    #: stay valid; room execution no longer reads it.
    mt5_live_mode = Column(Boolean, default=False, nullable=False)
    #: The demo account. It trades in both modes — that is what makes the demo
    #: a mirror you can keep watching rather than a record that stops on the
    #: day you arm the live account.
    mt5_demo_account_id = Column(Integer, nullable=True)

    # ── Risk policy ──
    risk_pct = Column(Float, default=1.0, nullable=False)          # % of equity per trade
    max_open_positions = Column(Integer, default=3, nullable=False)
    # 0-1 board agreement / confidence needed before an order clears the gate.
    # 0.40: the seats rarely align above 0.70 on a ranging pair, and holding out
    # for that consensus was skipping setups that then ran — a 40% floor still
    # bars the genuinely split board while letting a real lean through. The two
    # move together; consensus without the matching confidence just relocates
    # the block message.
    min_consensus = Column(Float, default=0.4, nullable=False)     # 0-1 agreement
    min_confidence = Column(Float, default=0.4, nullable=False)
    max_trades_per_day = Column(Integer, default=10, nullable=False)
    max_leverage = Column(Integer, default=10, nullable=False)

    # ── Cadence ──
    # How often a *pinned* pair is re-analysed. Deliberately separate from the
    # rotation cooldown: focus means "keep looking at this one", so it must not
    # be throttled by the gap that stops unpinned pairs being re-run back to
    # back. 300 | 900 | 3600 | 7200 | 14400.
    focus_interval_s = Column(Integer, default=300, nullable=False)
    # The timeframe the board analyses on, and the one the room's chart draws.
    # One setting for both deliberately: an agent arguing a 4h structure under a
    # 1h chart is two different analyses presented as one.
    focus_timeframe = Column(String, default="1h", nullable=False)
    # Whether the room worker starts itself with the API, so the board keeps
    # meeting across restarts without anyone re-arming it by hand.
    worker_enabled = Column(Boolean, default=True, nullable=False)
    # The pinned pair. Persisted because "never stop analysing this one" has to
    # outlive a restart — in-process state alone silently drops the focus.
    focus_symbol = Column(String, nullable=True)

    # ── Bitcoin 1064-day cycle ──
    # The calendar the whole desk reads. Anchors are the cycle bottoms as ISO
    # dates in a JSON list; the bull/bear lengths are the pattern's constants.
    # Defaults live in app.services.market_cycle — an empty column means "use
    # the verified history", not "no cycle".
    cycle_anchors = Column(Text, nullable=True)   # JSON ["2015-01-14", ...]
    cycle_bull_days = Column(Integer, default=1064, nullable=False)
    cycle_bear_days = Column(Integer, default=365, nullable=False)
    # Auto risk reduction: when the calendar is inside the projected-bear (or
    # late-bull caution) window, size new entries at cycle_risk_multiplier of
    # the configured risk. Off by default — inert until asked for.
    cycle_auto_risk = Column(Boolean, default=False, nullable=False)
    cycle_risk_multiplier = Column(Float, default=0.5, nullable=False)

    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
