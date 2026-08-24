"""Telegram Signal & News Plugin configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


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
class TelegramPluginConfig:
    session_name: str = os.getenv("TELEGRAM_PLUGIN_SESSION_NAME", "tradebot_telegram")

    # Provider credentials
    api_id: int = _as_int(
        "TELEGRAM_API_ID",
        _as_int("TELEGRAM_PLUGIN_API_ID", _as_int("TELETHON_API_ID", 0)),
    )
    api_hash: str = os.getenv(
        "TELEGRAM_API_HASH",
        os.getenv("TELEGRAM_PLUGIN_API_HASH", os.getenv("TELETHON_API_HASH", "")),
    )
    bot_token: str = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        os.getenv("TELEGRAM_PLUGIN_BOT_TOKEN", os.getenv("TELEGRAM_BOT_API_TOKEN", "")),
    )
    mcp_chat_id: str = os.getenv("TELEGRAM_MCP_CHAT_ID", os.getenv("TELEGRAM_PLUGIN_MCP_CHAT_ID", ""))
    mcp_server_url: str = os.getenv(
        "TELEGRAM_MCP_SERVER_URL",
        os.getenv("TELEGRAM_PLUGIN_MCP_SERVER_URL", "https://telegram-mcp.furkankucuk.net"),
    ).rstrip("/")
    mcp_timeout_seconds: int = _as_int(
        "TELEGRAM_MCP_TIMEOUT_SECONDS",
        _as_int("TELEGRAM_PLUGIN_MCP_TIMEOUT_SECONDS", 20),
    )

    # Polling defaults
    poll_limit: int = _as_int("TELEGRAM_PLUGIN_POLL_LIMIT", 50)
    poll_interval_seconds: int = _as_int("TELEGRAM_PLUGIN_POLL_INTERVAL_SECONDS", 300)

    # Extraction strategy
    enable_llm_fallback: bool = _as_bool("TELEGRAM_PLUGIN_ENABLE_LLM_FALLBACK", False)
    llm_model: str = os.getenv("TELEGRAM_PLUGIN_LLM_MODEL", "fable-5-high")
    llm_timeout_seconds: int = _as_int("TELEGRAM_PLUGIN_LLM_TIMEOUT_SECONDS", 20)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # Image understanding (photos sent to the bot). Which model reads the image
    # is no longer set here — ai_router.TASK_MODEL_CHAINS["vision_analysis"]
    # owns that, so the bot, Paul chat and the extension all use one chain.
    vision_enabled: bool = _as_bool("TELEGRAM_VISION_ENABLED", True)
    vision_max_image_bytes: int = _as_int("TELEGRAM_VISION_MAX_IMAGE_BYTES", 8 * 1024 * 1024)
    vision_timeout_seconds: int = _as_int("TELEGRAM_VISION_TIMEOUT_SECONDS", 60)


telegram_plugin_config = TelegramPluginConfig()


def build_config_from_db(settings: object | None) -> "TelegramPluginConfig":
    """Merge DB settings on top of env-var defaults.

    Any non-None, non-empty value in `settings` wins over the env-var default.
    `settings` is a TelegramPluginSettings ORM row (or None if table is empty).
    """
    base = telegram_plugin_config  # env-var baseline

    if settings is None:
        return base

    db_api_id = getattr(settings, "api_id", None)
    db_api_hash = getattr(settings, "api_hash", None) or ""
    db_bot_token = getattr(settings, "bot_token", None) or ""
    db_mcp_chat_id = getattr(settings, "mcp_chat_id", None) or ""

    return TelegramPluginConfig(
        session_name=base.session_name,
        api_id=db_api_id if db_api_id else base.api_id,
        api_hash=db_api_hash if db_api_hash else base.api_hash,
        bot_token=db_bot_token if db_bot_token else base.bot_token,
        mcp_chat_id=db_mcp_chat_id if db_mcp_chat_id else base.mcp_chat_id,
        mcp_server_url=base.mcp_server_url,
        mcp_timeout_seconds=base.mcp_timeout_seconds,
        poll_limit=base.poll_limit,
        poll_interval_seconds=base.poll_interval_seconds,
        enable_llm_fallback=base.enable_llm_fallback,
        llm_model=base.llm_model,
        llm_timeout_seconds=base.llm_timeout_seconds,
        openai_api_key=base.openai_api_key,
    )
