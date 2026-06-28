"""Shared timezone utilities for Telegram plugin.

Policy:
- Business timezone is fixed to Africa/Johannesburg (SAST).
- DB DateTime columns are naive (without timezone), so we persist UTC-naive values.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SOUTH_AFRICA_TZ_NAME = "Africa/Johannesburg"
SOUTH_AFRICA_TZ = ZoneInfo(SOUTH_AFRICA_TZ_NAME)


def now_sast() -> datetime:
    """Timezone-aware current datetime in Africa/Johannesburg."""
    return datetime.now(SOUTH_AFRICA_TZ)


def utc_naive_from_sast(dt: datetime | None = None) -> datetime:
    """Convert aware SAST datetime (or now) to UTC-naive for DB writes."""
    value = dt or now_sast()
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def now_utc_naive() -> datetime:
    """UTC-naive now derived from SAST source-of-truth clock."""
    return utc_naive_from_sast(now_sast())
