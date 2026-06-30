"""
ObsidianKnowledgePlugin — Pydantic Schemas

Request/response models for the REST API.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── VaultNote ───────────────────────────────────────────────────────────────

class VaultNoteBase(BaseModel):
    path: str
    note_type: str = "custom"
    symbol: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_id: Optional[str] = None
    source_table: Optional[str] = None


class VaultNoteCreate(VaultNoteBase):
    content: str  # raw markdown


class VaultNoteResponse(VaultNoteBase):
    id: int
    checksum: Optional[str] = None
    synced_to_obsidian: bool = False
    last_sync_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    frontmatter: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


# ── Vault status ─────────────────────────────────────────────────────────────

class VaultStatusResponse(BaseModel):
    vault_path: str
    vault_exists: bool
    total_notes: int
    notes_by_type: Dict[str, int]
    last_sync_at: Optional[datetime]
    obsidian_rest_connected: bool
    obsidian_rest_url: str


# ── Sync ─────────────────────────────────────────────────────────────────────

class SyncRequest(BaseModel):
    export_decisions: bool = True
    export_signals: bool = True
    export_communities: bool = True
    limit: int = Field(default=100, le=1000)


class SyncResult(BaseModel):
    written: int
    skipped: int
    errors: int
    duration_ms: float
    details: List[str] = Field(default_factory=list)


class SyncResponse(BaseModel):
    success: bool
    result: SyncResult


# ── Note content ─────────────────────────────────────────────────────────────

class NoteContentResponse(BaseModel):
    path: str
    content: str
    frontmatter: Dict[str, Any]
    note_type: str
    symbol: Optional[str]
    tags: List[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


# ── Graph ────────────────────────────────────────────────────────────────────

class VaultGraphNode(BaseModel):
    id: str
    label: str
    note_type: str
    symbol: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class VaultGraphEdge(BaseModel):
    source: str
    target: str
    label: str = "links"


class VaultGraphResponse(BaseModel):
    nodes: List[VaultGraphNode]
    edges: List[VaultGraphEdge]
    total_nodes: int
    total_edges: int


# ── Search ───────────────────────────────────────────────────────────────────

class VaultSearchRequest(BaseModel):
    query: str
    note_type: Optional[str] = None
    symbol: Optional[str] = None
    limit: int = Field(default=20, le=100)


class VaultSearchHit(BaseModel):
    path: str
    note_type: str
    symbol: Optional[str]
    excerpt: str
    score: float


class VaultSearchResponse(BaseModel):
    hits: List[VaultSearchHit]
    total: int


# ── Context (for agent injection) ────────────────────────────────────────────

class VaultContextResponse(BaseModel):
    symbol: str
    context_markdown: str
    notes_used: int
    token_estimate: int
