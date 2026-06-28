"""Logging configuration for the application."""
from pathlib import Path
import sys

from loguru import logger

from app.core.config import settings


def configure_logging() -> None:
    """Configure Loguru sinks for console and rotating file output."""
    logger.remove()

    log_level = (settings.LOG_LEVEL or "INFO").upper()
    log_json = bool(settings.LOG_JSON)

    logger.add(
        sys.stdout,
        level=log_level,
        serialize=log_json,
        backtrace=settings.DEBUG,
        diagnose=settings.DEBUG,
        enqueue=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
            "{name}:{function}:{line} | {message}"
        ) if not log_json else None,
    )

    log_file = (settings.LOG_FILE_PATH or "").strip()
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_path,
            level=log_level,
            rotation=settings.LOG_ROTATION,
            retention=settings.LOG_RETENTION,
            serialize=log_json,
            backtrace=False,
            diagnose=False,
            enqueue=True,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
                "{name}:{function}:{line} | {message}"
            ) if not log_json else None,
        )
