"""
MT5 Plugin — Configuration

Reads plugin-specific settings from env vars and plugin_settings table.
"""
import os
from dataclasses import dataclass


@dataclass
class MT5Config:
    """MT5 plugin configuration — loaded from environment."""

    # mtapi-io REST API base URL
    api_url: str = os.getenv("MT5_API_URL", "http://localhost:8090")

    # Polling interval for account sync (seconds)
    poll_interval: int = int(os.getenv("MT5_POLL_INTERVAL", "10"))

    # Feature toggles
    enable_aggregation: bool = os.getenv("MT5_ENABLE_AGGREGATION", "true").lower() == "true"
    enable_copy_sim: bool = os.getenv("MT5_ENABLE_COPY_SIM", "false").lower() == "true"
    enable_trade_replay: bool = os.getenv("MT5_ENABLE_TRADE_REPLAY", "true").lower() == "true"
    overlay_heatmap_enabled: bool = os.getenv("MT5_OVERLAY_HEATMAP", "true").lower() == "true"

    # Limits
    max_accounts_per_user: int = int(os.getenv("MT5_MAX_ACCOUNTS_PER_USER", "5"))


mt5_config = MT5Config()
