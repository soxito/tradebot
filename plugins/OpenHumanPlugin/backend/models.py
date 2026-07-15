"""
OpenHumanPlugin — SQLAlchemy Models

Tables track memory entries synced to agentmemory and research cache.
All tables prefixed with `openhuman_`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean
from sqlalchemy.orm import DeclarativeBase


class OpenHumanBase(DeclarativeBase):
    pass


class OpenHumanMemoryEntry(OpenHumanBase):
    """Record of data pushed to agentmemory."""
    __tablename__ = "openhuman_memory_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False)       # "signal" | "forecast" | "position" | "trade"
    symbol = Column(String(32), nullable=True, index=True)
    content = Column(Text, nullable=False)
    tags = Column(String(256), nullable=True)
    # ID assigned by agentmemory (if synced)
    remote_id = Column(String(128), nullable=True)
    synced = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
