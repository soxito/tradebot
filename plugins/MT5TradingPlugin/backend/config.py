"""
MT5 Plugin — Configuration

Reads plugin-specific settings from env vars and plugin_settings table.
"""
import os
import pathlib
from dataclasses import dataclass, field


_MT5_DEFAULT_URL = "http://localhost:8092"


def _validated_mt5_url(raw: str) -> str:
    url = raw.strip() if raw else ""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    if url:
        return f"http://{url}"
    return _MT5_DEFAULT_URL


def _load_mt5_api_url() -> str:
    """Resolve MT5_API_URL: shell env (valid only) → .env file → default."""
    env_val = os.environ.get("MT5_API_URL", "").strip()
    if env_val and (env_val.startswith("http://") or env_val.startswith("https://")):
        return env_val
    try:
        root = pathlib.Path(__file__).resolve().parents[3]
        for env_file in (root / ".env", root / ".env.local"):
            if not env_file.exists():
                continue
            for line in env_file.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("MT5_API_URL="):
                    raw = s.split("=", 1)[1].strip().strip("'\"")
                    validated = _validated_mt5_url(raw)
                    os.environ["MT5_API_URL"] = validated
                    return validated
    except Exception:
        pass
    return _MT5_DEFAULT_URL


@dataclass
class MT5Config:
    """MT5 plugin configuration — loaded from environment."""
    api_url: str = field(default_factory=_load_mt5_api_url)
    poll_interval: int = int(os.getenv("MT5_POLL_INTERVAL", "10"))
    enable_aggregation: bool = os.getenv("MT5_ENABLE_AGGREGATION", "true").lower() == "true"
    enable_copy_sim: bool = os.getenv("MT5_ENABLE_COPY_SIM", "false").lower() == "true"
    enable_trade_replay: bool = os.getenv("MT5_ENABLE_TRADE_REPLAY", "true").lower() == "true"
    overlay_heatmap_enabled: bool = os.getenv("MT5_OVERLAY_HEATMAP", "true").lower() == "true"
    max_accounts_per_user: int = int(os.getenv("MT5_MAX_ACCOUNTS_PER_USER", "5"))


mt5_config = MT5Config()
