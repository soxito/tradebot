"""
OpenHumanPlugin — Configuration
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenHumanSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENHUMAN_", extra="ignore")

    # OpenHuman desktop app local JSON-RPC endpoint
    # Actual port used by OpenHuman: 7788
    api_url: str = "http://127.0.0.1:7788"

    # agentmemory is provided by OpenHuman's built-in memory_tree_db.
    # We point the agentmemory URL to OpenHuman itself so memory routes
    # just call OpenHuman's /health and /rpc endpoints.
    agentmemory_url: str = "http://127.0.0.1:7788"

    # Expose live MCP SSE endpoint for OpenHuman to subscribe to
    mcp_sse_enabled: bool = True

    # Request timeout in seconds
    request_timeout: int = 5


openhuman_config = OpenHumanSettings()
