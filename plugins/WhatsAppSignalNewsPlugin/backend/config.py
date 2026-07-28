"""WhatsApp Signal & News Plugin configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class WhatsAppPluginConfig:
    """Configuration for WhatsApp plugin loaded from environment variables."""

    # OpenWA Gateway connection
    openwa_base_url: str = os.getenv("WHATSAPP_OPENWA_BASE_URL", "http://localhost:2785")
    openwa_api_key: str = os.getenv("WHATSAPP_OPENWA_API_KEY", "")
    default_session_name: str = os.getenv("WHATSAPP_DEFAULT_SESSION_NAME", "tradebot_whatsapp")

    # Webhook security
    webhook_secret: str = os.getenv("WHATSAPP_WEBHOOK_SECRET", "")

    # Polling/background intervals
    poll_interval_seconds: int = _as_int("WHATSAPP_POLL_INTERVAL_SECONDS", 300)
    session_health_check_seconds: int = _as_int("WHATSAPP_SESSION_HEALTH_CHECK_SECONDS", 60)

    # Signal processing
    enable_llm_fallback: bool = _as_bool("WHATSAPP_ENABLE_LLM_FALLBACK", False)
    llm_model: str = os.getenv("WHATSAPP_LLM_MODEL", "fable-5-high")
    llm_timeout_seconds: int = _as_int("WHATSAPP_LLM_TIMEOUT_SECONDS", 20)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # Message processing
    max_messages_per_poll: int = _as_int("WHATSAPP_MAX_MESSAGES_PER_POLL", 50)
    message_dedupe_ttl_hours: int = _as_int("WHATSAPP_MESSAGE_DEDUPE_TTL_HOURS", 24)

    # Sniper defaults
    sniper_enabled_default: bool = _as_bool("WHATSAPP_SNIPER_ENABLED_DEFAULT", False)
    sniper_mode_default: str = os.getenv("WHATSAPP_SNIPER_MODE_DEFAULT", "sandbox")
    sniper_position_size_usdt_default: float = float(os.getenv("WHATSAPP_SNIPER_POSITION_SIZE_USDT_DEFAULT", "100"))
    sniper_max_positions_default: int = _as_int("WHATSAPP_SNIPER_MAX_POSITIONS_DEFAULT", 5)
    sniper_min_confidence_default: float = float(os.getenv("WHATSAPP_SNIPER_MIN_CONFIDENCE_DEFAULT", "0.65"))
    sniper_min_risk_reward_default: float = float(os.getenv("WHATSAPP_SNIPER_MIN_RISK_REWARD_DEFAULT", "1.5"))

    def __post_init__(self):
        # Normalize base URL
        object.__setattr__(self, "openwa_base_url", self.openwa_base_url.rstrip("/"))


whatsapp_plugin_config = WhatsAppPluginConfig()


def build_config_from_db(settings: Optional[object]) -> WhatsAppPluginConfig:
    """Merge DB settings on top of env-var defaults.

    Any non-None, non-empty value in `settings` wins over the env-var default.
    """
    base = whatsapp_plugin_config

    if settings is None:
        return base

    return WhatsAppPluginConfig(
        openwa_base_url=getattr(settings, "openwa_base_url", None) or base.openwa_base_url,
        openwa_api_key=getattr(settings, "openwa_api_key", None) or base.openwa_api_key,
        default_session_name=getattr(settings, "default_session_name", None) or base.default_session_name,
        webhook_secret=getattr(settings, "webhook_secret", None) or base.webhook_secret,
        poll_interval_seconds=getattr(settings, "poll_interval_seconds", None) or base.poll_interval_seconds,
        session_health_check_seconds=getattr(settings, "session_health_check_seconds", None) or base.session_health_check_seconds,
        enable_llm_fallback=getattr(settings, "enable_llm_fallback", None) if getattr(settings, "enable_llm_fallback", None) is not None else base.enable_llm_fallback,
        llm_model=getattr(settings, "llm_model", None) or base.llm_model,
        llm_timeout_seconds=getattr(settings, "llm_timeout_seconds", None) or base.llm_timeout_seconds,
        openai_api_key=getattr(settings, "openai_api_key", None) or base.openai_api_key,
        max_messages_per_poll=getattr(settings, "max_messages_per_poll", None) or base.max_messages_per_poll,
        message_dedupe_ttl_hours=getattr(settings, "message_dedupe_ttl_hours", None) or base.message_dedupe_ttl_hours,
        sniper_enabled_default=getattr(settings, "sniper_enabled_default", None) if getattr(settings, "sniper_enabled_default", None) is not None else base.sniper_enabled_default,
        sniper_mode_default=getattr(settings, "sniper_mode_default", None) or base.sniper_mode_default,
        sniper_position_size_usdt_default=getattr(settings, "sniper_position_size_usdt_default", None) or base.sniper_position_size_usdt_default,
        sniper_max_positions_default=getattr(settings, "sniper_max_positions_default", None) or base.sniper_max_positions_default,
        sniper_min_confidence_default=getattr(settings, "sniper_min_confidence_default", None) or base.sniper_min_confidence_default,
        sniper_min_risk_reward_default=getattr(settings, "sniper_min_risk_reward_default", None) or base.sniper_min_risk_reward_default,
    )