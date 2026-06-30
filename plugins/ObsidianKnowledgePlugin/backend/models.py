"""
ObsidianKnowledgePlugin — SQLAlchemy Models

Single table: obsidian_vault_notes
Tracks every .md file written to the vault so we can:
  - dirty-check before overwriting (checksum)
  - query by symbol / type for the context injection service
  - report sync status via the REST API
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, JSON, String, Text,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class ObsidianBase(DeclarativeBase):
    """Declarative base for the Obsidian plugin — discovered by PluginLoader."""
    pass


class NoteType(str, enum.Enum):
    SIGNAL = "signal"
    DECISION = "decision"
    STRATEGY = "strategy"
    COMMUNITY = "community"
    DAILY = "daily"
    CUSTOM = "custom"


class VaultNote(ObsidianBase):
    """Represents one .md file written into the Obsidian vault."""
    __tablename__ = "obsidian_vault_notes"

    id           = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ────────────────────────────────────────────────────────────────
    path         = Column(String(500), unique=True, nullable=False, index=True)
    # path is relative to vault root, e.g. "signals/BTC-USDT/2026-06-29-001.md"

    note_type    = Column(String(30), nullable=False, default=NoteType.CUSTOM)
    # One of: signal | decision | strategy | community | daily | custom

    # ── Source linkage ─────────────────────────────────────────────────────────
    source_id    = Column(String(100), nullable=True, index=True)
    # Primary key of the originating row (Signal.id, AgentDecision.id, …)
    source_table = Column(String(80),  nullable=True)
    # Table name so we know where to look: "signals", "agent_decisions", etc.

    # ── Trading metadata ───────────────────────────────────────────────────────
    symbol       = Column(String(30), nullable=True, index=True)
    tags         = Column(JSON, default=list)
    frontmatter  = Column(JSON, default=dict)   # parsed YAML frontmatter cache

    # ── Content tracking ───────────────────────────────────────────────────────
    checksum     = Column(String(64), nullable=True)
    # SHA-256 hex of the written file content.  If unchanged we skip the write.

    # ── Obsidian REST sync status ───────────────────────────────────────────────
    synced_to_obsidian = Column(Boolean, default=False, nullable=False)
    last_sync_at       = Column(DateTime, nullable=True)

    # ── Timestamps ─────────────────────────────────────────────────────────────
    created_at   = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at   = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<VaultNote {self.note_type}:{self.path}>"
