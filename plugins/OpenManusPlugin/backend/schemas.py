"""OpenManusPlugin — Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class OpenManusHealth(BaseModel):
    mcp_reachable: bool
    mcp_url: str
    installed: bool
    install_dir: str
    enabled: bool
    fallback_enabled: bool
    error: Optional[str] = None


class OpenManusInstallStatus(BaseModel):
    installed: bool
    install_dir: str
    version: Optional[str] = None
    error: Optional[str] = None


class OpenManusStartRequest(BaseModel):
    transport: str = "sse"
    port: int = 8765


class OpenManusStartResponse(BaseModel):
    started: bool
    pid: Optional[int] = None
    url: Optional[str] = None
    error: Optional[str] = None


class CallLogRow(BaseModel):
    id: int
    created_at: datetime
    flow: str
    agent_name: Optional[str]
    route_source: str
    provider_label: Optional[str]
    model: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    latency_ms: Optional[float]
    success: bool
    schema_ok: Optional[bool]
    error_msg: Optional[str]

    class Config:
        from_attributes = True


class CallStats(BaseModel):
    total: int
    openmanus_routed: int
    fallback_routed: int
    errors: int
    openmanus_success_rate: float = Field(description="0-1 fraction")
    fallback_rate: float = Field(description="0-1 fraction")
    avg_latency_ms: Optional[float]


class AdapterChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    flow: str = "manual"
    agent_name: Optional[str] = None
    source: str = "api"
    temperature: float = 0.35
    max_tokens: int = 1200
    json_mode: bool = False


class AdapterChatResponse(BaseModel):
    ok: bool
    content: Optional[str]
    route_source: str
    provider: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[dict[str, int]] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
