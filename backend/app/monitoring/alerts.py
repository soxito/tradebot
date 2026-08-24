"""Alert delivery services for Telegram, Discord, and email."""
from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage
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
        # The other path to the user's phone. Same rule as the bot service: a
        # test run must not be able to produce a message about a trade.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            logger.debug("Telegram alert suppressed under test")
            return False
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
    def _email_recipients() -> list[str]:
        return [r.strip() for r in (settings.SMTP_TO or "").split(",") if r.strip()]

    @staticmethod
    def _send_email_sync(subject: str, body: str) -> None:
        host = settings.SMTP_HOST
        port = int(settings.SMTP_PORT or 587)
        username = settings.SMTP_USERNAME
        password = settings.SMTP_PASSWORD
        sender = settings.SMTP_FROM or username or "tradebot@localhost"
        recipients = AlertService._email_recipients()

        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)

        if settings.SMTP_USE_TLS:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(message)
        elif port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(message)

    @staticmethod
    async def send_email(subject: str, body: str) -> bool:
        if not settings.EMAIL_ALERTS_ENABLED:
            return False
        if not settings.SMTP_HOST or not AlertService._email_recipients():
            return False

        try:
            await asyncio.to_thread(AlertService._send_email_sync, subject, body)
            record_alert("email", "success")
            return True
        except Exception as exc:
            logger.warning(f"Email alert failed: {exc}")
            record_alert("email", "failed")
            return False

    @staticmethod
    async def notify(title: str, message: str, level: str = "INFO", details: dict[str, Any] | None = None) -> dict[str, bool]:
        if not _should_send(level):
            return {"telegram": False, "discord": False, "email": False}

        formatted = AlertService._format_message(title, message, level, details)
        telegram_ok = await AlertService.send_telegram(formatted)
        discord_ok = await AlertService.send_discord(formatted)
        email_ok = await AlertService.send_email(f"[{level.upper()}] {title}", formatted)
        return {"telegram": telegram_ok, "discord": discord_ok, "email": email_ok}
