"""
Timezone utility — all datetimes use South Africa Standard Time (SAST, UTC+2).
"""
from datetime import datetime, timezone, timedelta

SAST = timezone(timedelta(hours=2))  # Africa/Johannesburg


def now_sast() -> datetime:
    """Return current SAST time as naive datetime (no tzinfo).
    Compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(SAST).replace(tzinfo=None)
