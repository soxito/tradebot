"""
AI Market Analyst Plugin — Configuration
"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class AIAnalystConfig:
    # OpenAI defaults (users can override per-agent)
    default_model: str = os.getenv("AI_ANALYST_MODEL", "fable-5-high")
    default_reasoning_effort: str = os.getenv("AI_ANALYST_REASONING", "medium")
    default_max_tokens: int = int(os.getenv("AI_ANALYST_MAX_TOKENS", "4096"))

    # Provider registry & routing
    providers_file: str = os.getenv(
        "AI_ANALYST_PROVIDERS_FILE",
        "plugins/AiMarketAnalyst/backend/providers.json",
    )
    providers_json: str = os.getenv("AI_ANALYST_PROVIDERS_JSON", "")
    routing_strategy: str = os.getenv("AI_ANALYST_ROUTING_STRATEGY", "round_robin")
    routing_fallback: bool = os.getenv("AI_ANALYST_ROUTING_FALLBACK", "true").lower() == "true"
    provider_timeout_s: int = int(os.getenv("AI_ANALYST_PROVIDER_TIMEOUT_S", "30"))
    provider_max_retries: int = int(os.getenv("AI_ANALYST_PROVIDER_MAX_RETRIES", "1"))
    redis_url: str = os.getenv(
        "AI_ANALYST_REDIS_URL",
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    )

    # Safety defaults
    max_daily_decisions: int = int(os.getenv("AI_ANALYST_MAX_DAILY", "50"))
    min_sl_distance_pct: float = float(os.getenv("AI_ANALYST_MIN_SL_PCT", "0.3"))
    max_lot_size: float = float(os.getenv("AI_ANALYST_MAX_LOT", "1.0"))
    min_lot_size: float = float(os.getenv("AI_ANALYST_MIN_LOT", "0.01"))
    max_risk_per_trade_pct: float = float(os.getenv("AI_ANALYST_MAX_RISK_PCT", "2.0"))

    # Indicator defaults
    default_indicators: List[str] = field(default_factory=lambda: [
        "RSI", "EMA_20", "EMA_50", "ATR", "VWAP"
    ])

    # Rate limiting (requests to OpenAI per minute)
    openai_rpm_limit: int = int(os.getenv("AI_ANALYST_RPM", "20"))


ai_analyst_config = AIAnalystConfig()
