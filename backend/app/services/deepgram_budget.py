"""Deepgram cost-aware fallback budget guard.

A tiny JSON-file usage store (mirroring the voice-brain persistence in
``app/api/jarvis.py``) that tracks Deepgram pre-recorded STT spend and enforces
a monthly + daily spend cap so the prepaid credit lasts for months.

The store is intentionally dependency-free (no DB): it reads/writes a single
JSON document at request time, rolling the month/day counters over when the
ISO period key changes. All public helpers are safe to call concurrently for a
single-process dev backend and degrade gracefully on any I/O error.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from app.core.config import settings


def _usage_path() -> Path:
    """Return the JSON usage-store path.

    Honours the ``DEEPGRAM_USAGE_PATH`` env var (used by tests) and otherwise
    stores the file next to the JARVIS voice-brain data in ``~/.jarvis``.
    """
    override = os.getenv("DEEPGRAM_USAGE_PATH")
    if override:
        return Path(override)
    return Path.home() / ".jarvis" / "deepgram-usage.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def cost_for_seconds(seconds: float) -> float:
    """Convert a clip duration (seconds) into a USD cost at the STT rate."""
    seconds = max(0.0, float(seconds or 0))
    return (seconds / 60.0) * float(settings.DEEPGRAM_STT_RATE_PER_MIN)


def _load() -> Dict[str, Any]:
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # corrupt/partial file → start fresh, never crash
        logger.warning(f"[deepgram-budget] could not read usage store: {e}")
        return {}


def _save(data: Dict[str, Any]) -> None:
    path = _usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[deepgram-budget] could not write usage store: {e}")


def _rolled(data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Return a copy of ``data`` with month/day counters rolled to ``now``."""
    month = dict(data.get("month") or {})
    day = dict(data.get("day") or {})

    if month.get("period") != _month_key(now):
        month = {"period": _month_key(now), "spend": 0.0}
    if day.get("period") != _day_key(now):
        day = {"period": _day_key(now), "spend": 0.0}

    return {
        "total_spend": float(data.get("total_spend") or 0.0),
        "first_use": data.get("first_use"),
        "month": month,
        "day": day,
    }


def can_spend(seconds: float = 0.0) -> bool:
    """Return True if a clip of ``seconds`` may be sent to Deepgram now.

    Enforces the monthly and daily caps (after rolling over period counters)
    and the global ``DEEPGRAM_FALLBACK_ENABLED`` switch. The projected cost of
    the clip itself is included so a single call cannot blow past the cap.
    """
    if not settings.DEEPGRAM_FALLBACK_ENABLED:
        return False

    now = _now()
    data = _rolled(_load(), now)
    projected = cost_for_seconds(seconds)

    # Strictly below the cap: once spend reaches the cap, no further calls are
    # allowed even for a zero-length projection (hard cap, inclusive boundary).
    month_ok = (data["month"]["spend"] + projected) < settings.DEEPGRAM_MONTHLY_CAP_USD
    day_ok = (data["day"]["spend"] + projected) < settings.DEEPGRAM_DAILY_CAP_USD
    return bool(month_ok and day_ok)


def record_usage(seconds: float) -> Dict[str, Any]:
    """Record a completed Deepgram STT call and persist the new totals.

    Returns the same shape as :func:`summary` so callers can echo the budget
    back to the client without a second read.
    """
    now = _now()
    data = _rolled(_load(), now)
    cost = cost_for_seconds(seconds)

    data["month"]["spend"] = round(data["month"]["spend"] + cost, 6)
    data["day"]["spend"] = round(data["day"]["spend"] + cost, 6)
    data["total_spend"] = round(data["total_spend"] + cost, 6)
    if not data.get("first_use"):
        data["first_use"] = now.isoformat()

    _save(data)
    return summary(_data=data, _now_dt=now)


def summary(_data: Dict[str, Any] | None = None, _now_dt: datetime | None = None) -> Dict[str, Any]:
    """Return month/day spend, caps, remaining budget and projected runway.

    ``projected_runway_days`` estimates how long the remaining total credit
    lasts at the average daily burn observed since first use.
    """
    now = _now_dt or _now()
    data = _data if _data is not None else _rolled(_load(), now)

    monthly_cap = float(settings.DEEPGRAM_MONTHLY_CAP_USD)
    daily_cap = float(settings.DEEPGRAM_DAILY_CAP_USD)
    total_credit = float(settings.DEEPGRAM_TOTAL_CREDIT_USD)

    month_spend = round(float(data["month"]["spend"]), 4)
    day_spend = round(float(data["day"]["spend"]), 4)
    total_spend = round(float(data.get("total_spend") or 0.0), 4)

    remaining = round(max(0.0, monthly_cap - month_spend), 4)
    total_remaining = round(max(0.0, total_credit - total_spend), 4)

    # Average daily burn since first use → runway in days.
    projected_runway_days: float | None = None
    first_use = data.get("first_use")
    if total_spend > 0 and first_use:
        try:
            started = datetime.fromisoformat(first_use)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            days_active = max(1.0, (now - started).total_seconds() / 86400.0)
            avg_daily = total_spend / days_active
            if avg_daily > 0:
                projected_runway_days = round(total_remaining / avg_daily, 1)
        except Exception:
            projected_runway_days = None

    return {
        "month_spend": month_spend,
        "day_spend": day_spend,
        "monthly_cap": monthly_cap,
        "daily_cap": daily_cap,
        "remaining": remaining,
        "total_spend": total_spend,
        "total_remaining": total_remaining,
        "projected_runway_days": projected_runway_days,
        "fallback_enabled": bool(settings.DEEPGRAM_FALLBACK_ENABLED),
    }
