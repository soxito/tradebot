"""
OpenHumanPlugin — Pydantic Schemas
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class OpenHumanStatus(BaseModel):
    agentmemory_reachable: bool
    openhuman_reachable: bool
    memory_entry_count: int = 0
    message: Optional[str] = None


class MemoryEntryRow(BaseModel):
    id: int
    source: str
    symbol: Optional[str]
    content: str
    tags: Optional[str]
    remote_id: Optional[str]
    synced: bool
    created_at: str


class MemorySyncResponse(BaseModel):
    synced_count: int
    failed_count: int
    details: List[Dict[str, Any]] = []


class ResearchRequest(BaseModel):
    prompt: str
    symbol: Optional[str] = None


class ResearchResponse(BaseModel):
    status: str
    result: Optional[Any] = None
