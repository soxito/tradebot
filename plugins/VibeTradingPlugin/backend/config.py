"""
VibeTradingPlugin — Configuration

All settings read from env with sane defaults.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class VibeTradingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIBE_TRADING_", extra="ignore")

    # URL of the running vibe-trading serve instance
    url: str = "http://127.0.0.1:8899"

    # Optional Bearer token (set API_AUTH_KEY in vibe-trading agent/.env)
    api_auth_key: str = ""

    # Whether to auto-start the sidecar on first request
    auto_start: bool = True

    # Pass VIBE_TRADING_ENABLE_SCHEDULER=1 to enable scheduled research
    enable_scheduler: bool = False

    # Request timeout for normal calls (seconds)
    request_timeout: int = 60

    # Request timeout for long-running research/backtest/swarm (seconds)
    long_timeout: int = 300


vibe_config = VibeTradingSettings()
