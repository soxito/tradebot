"""OpenManusPlugin — SQLAlchemy models."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum as SQLEnum, Float, Integer, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class OpenManusBase(DeclarativeBase):
    pass


class RouteSource(str, enum.Enum):
    openmanus = "openmanus"
    fallback = "fallback"
    error = "error"


class OpenManusCallLog(OpenManusBase):
    """Audit log for every AI call that goes through the OpenManus adapter."""

    __tablename__ = "openmanus_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    # Call metadata
    flow: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    """Which flow triggered this call (jarvis, kronos, smc, scalp_ensemble, etc.)."""

    agent_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Routing outcome
    route_source: Mapped[RouteSource] = mapped_column(
        SQLEnum(RouteSource, name="openmanus_route_source"),
        nullable=False,
        default=RouteSource.openmanus,
    )
    """Whether the call was served by OpenManus or fell back to the existing router."""

    provider_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Token accounting
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Timing
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Outcome
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_msg: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Schema conformance — did the response pass validation?
    schema_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
