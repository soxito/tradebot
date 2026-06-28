"""Alert delivery services for Telegram and Discord."""
from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from app.core.config import settings
from app.monitoring.metrics import record_alert


_LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _should_send(level: str) -> bool:
    if not settings.ALERTS_ENABLED:
        return False
    current = _LEVEL_ORDER.get((level or "INFO").upper(), 20)
    minimum = _LEVEL_ORDER.get((settings.ALERTS_MIN_LEVEL or "WARNING").upper(), 30)
    return current >= minimum


class AlertService:
    @staticmethod
    def _format_message(title: str, message: str, level: str = "INFO", details: dict[str, Any] | None = None) -> str:
        parts = [f"[{level.upper()}] {title}", message]
        if details:
            for key, value in details.items():
                parts.append(f"- {key}: {value}")
        return "\n".join(parts)

    @staticmethod
    async def send_telegram(text: str) -> bool:
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return False

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            record_alert("telegram", "success")
            return True
        except Exception as exc:
            logger.warning(f"Telegram alert failed: {exc}")
            record_alert("telegram", "failed")
            return False

    @staticmethod
    async def send_discord(text: str) -> bool:
        if not settings.DISCORD_WEBHOOK_URL:
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(settings.DISCORD_WEBHOOK_URL, json={"content": text})
                response.raise_for_status()
            record_alert("discord", "success")
            return True
        except Exception as exc:
            logger.warning(f"Discord alert failed: {exc}")
            record_alert("discord", "failed")
            return False

    @staticmethod
    async def notify(title: str, message: str, level: str = "INFO", details: dict[str, Any] | None = None) -> dict[str, bool]:
        if not _should_send(level):
            return {"telegram": False, "discord": False}

        formatted = AlertService._format_message(title, message, level, details)
        telegram_ok = await AlertService.send_telegram(formatted)
        discord_ok = await AlertService.send_discord(formatted)
        return {"telegram": telegram_ok, "discord": discord_ok}
