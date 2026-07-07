"""
MT5 Plugin — Configuration

Reads plugin-specific settings from env vars and plugin_settings table.
"""
import os
from dataclasses import dataclass, field


_MT5_DEFAULT_URL = "http://localhost:8092"


def _validated_mt5_url(raw: str) -> str:
    """Return a valid MT5 API URL, falling back to the default if the value is
    empty or missing the http(s) scheme (which causes httpx to raise
    'Request URL is missing an http:// or https:// protocol').
    """
    url = raw.strip() if raw else ""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    if url:
        # Non-empty but schemeless — prepend http://
        return f"http://{url}"
    return _MT5_DEFAULT_URL


@dataclass
class MT5Config:
    """MT5 plugin configuration — loaded from environment."""

    # mtapi-io REST API base URL.
    # Default is port 8092 — the project's standard host port for the mtapi-io
    # bridge (matches the `mt5rest` Docker container `8092:80` mapping and the
    # MT5_API_URL start.py injects). The raw `mtapiio/mt5rest` image docs use
    # 8090, but this project standardised on 8092; keeping 8090 here caused the
    # client to hit a dead port whenever MT5_API_URL wasn't explicitly set,
    # breaking account sync (no positions/orders, order placement failing).
    api_url: str = field(
        default_factory=lambda: _validated_mt5_url(
            os.getenv("MT5_API_URL", _MT5_DEFAULT_URL)
        )
    )

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
