"""
VibeTradingPlugin — SQLAlchemy Models

Tables track local run metadata so the frontend can display history.
All tables prefixed with `vibe_` per plugin naming convention.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, Float, Boolean
from sqlalchemy.orm import DeclarativeBase


class VibeTradingBase(DeclarativeBase):
    pass


class VibeTradingRun(VibeTradingBase):
    """Track a research/backtest/swarm run initiated through TradeBot."""
    __tablename__ = "vibe_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Remote run_id assigned by the vibe-trading server
    remote_run_id = Column(String(128), nullable=True, index=True)
    run_type = Column(String(32), nullable=False, default="research")
    # research | backtest | swarm | alpha_bench | shadow
    symbol = Column(String(32), nullable=True)
    prompt = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    # pending | running | completed | failed
    result_summary = Column(Text, nullable=True)
    pine_script = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class VibeTradingSchedule(VibeTradingBase):
    """Mirror of scheduled research jobs created on the vibe-trading server."""
    __tablename__ = "vibe_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    remote_job_id = Column(String(128), nullable=True, index=True)
    prompt = Column(Text, nullable=False)
    schedule = Column(String(64), nullable=False)   # cron or ms interval
    symbol = Column(String(32), nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
