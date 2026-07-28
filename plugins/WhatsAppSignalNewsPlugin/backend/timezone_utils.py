"""Timezone utilities for WhatsApp plugin."""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc_naive() -> datetime:
    """Return current UTC time as naive datetime (no timezone info)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)