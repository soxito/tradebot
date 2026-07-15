"""OpenManusPlugin — Configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenManusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENMANUS_", extra="ignore")

    # Whether OpenManus is enabled as primary AI route
    enabled: bool = True

    # OpenManus MCP server host/port (when running run_mcp.py with --transport sse)
    host: str = "127.0.0.1"
    port: int = 8765

    # Transport: "sse" or "stdio" (sse recommended for plugin sidecar)
    mcp_transport: str = "sse"

    # SSE endpoint path
    mcp_path: str = "/sse"

    # Messages endpoint path (for JSON-RPC calls)
    mcp_messages_path: str = "/messages"

    # If True, fall back to existing AiMarketAnalyst router on OpenManus failure
    fallback_enabled: bool = True

    # Timeout for each OpenManus MCP call in seconds
    timeout_s: float = 30.0

    # ── Phase 6: Expanded runtime modes (behind feature flags) ───────────────

    # Enable OpenManus main-agent mode (python main.py) as an additional
    # execution path for complex multi-step tasks.  Off by default until
    # equivalence tests pass; set OPENMANUS_MAIN_MODE_ENABLED=true to activate.
    main_mode_enabled: bool = False

    # Enable OpenManus flow-mode execution (python run_flow.py) for structured
    # pipeline tasks.  Off by default; set OPENMANUS_FLOW_MODE_ENABLED=true.
    flow_mode_enabled: bool = False

    # Minimum OpenManus success rate (0-1) required before allowing stricter
    # cutover mode (Phase 7 promotion gate).  Default 95%.
    promotion_min_success_rate: float = 0.95

    # Maximum fallback rate (0-1) allowed for promotion to stricter mode.
    # Default 5%.
    promotion_max_fallback_rate: float = 0.05

    # Where OpenManus will be cloned/installed
    install_dir: str = "~/.tradebot/openmanus"

    # Python binary used to run OpenManus (should match backend venv)
    python_bin: str = "python3"

    @property
    def sse_base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def sse_url(self) -> str:
        return f"{self.sse_base_url}{self.mcp_path}"

    @property
    def messages_url(self) -> str:
        return f"{self.sse_base_url}{self.mcp_messages_path}"


openmanus_config = OpenManusSettings()
