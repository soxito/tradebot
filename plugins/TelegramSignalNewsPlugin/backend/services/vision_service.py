"""Image understanding for the Telegram bot.

Thin adapter over :mod:`plugins.AiMarketAnalyst.backend.services.vision`, which
holds the shared implementation used by every surface (bot, Paul chat, browser
extension). What lives here is only what is Telegram's own: the
``TELEGRAM_VISION_ENABLED`` gate and the plugin's timeout setting.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.vision import (
    DEFAULT_CHART_PROMPT,
    DEFAULT_VISION_TIMEOUT_S,
    VisionRead,
    read_image,
)
from plugins.TelegramSignalNewsPlugin.backend.config import telegram_plugin_config as _cfg

__all__ = ["DEFAULT_CHART_PROMPT", "VisionRead", "analyse_image", "read_chart"]


async def read_chart(
    image_bytes: bytes,
    mime: str,
    question: str,
    db: AsyncSession,
) -> VisionRead | None:
    """Full read of an image — prose plus the structured findings to draw with."""
    if not _cfg.vision_enabled:
        logger.info("[Vision] disabled by TELEGRAM_VISION_ENABLED")
        return None

    return await read_image(
        image_bytes,
        mime,
        question,
        db,
        source="telegram",
        agent_name="telegram-vision",
        # The configured value is a floor, not a ceiling: the default 60s is
        # under what the leading vision model actually takes, and cutting it
        # short also trips the provider breaker for the fallback behind it.
        timeout=max(float(_cfg.vision_timeout_seconds), DEFAULT_VISION_TIMEOUT_S),
    )


async def analyse_image(
    image_bytes: bytes,
    mime: str,
    question: str,
    db: AsyncSession,
) -> str | None:
    """Prose-only read, for callers that have nothing to draw on."""
    read = await read_chart(image_bytes, mime, question, db)
    return read.narrative if read else None
