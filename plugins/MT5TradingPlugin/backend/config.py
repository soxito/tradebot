"""
MT5 Plugin — Configuration

Reads plugin-specific settings from env vars and plugin_settings table.
"""
import logging
import os
import pathlib
from dataclasses import dataclass, field
from urllib.parse import urlparse


logger = logging.getLogger(__name__)

_MT5_DEFAULT_URL = "http://localhost:8092"

# Ports on the loopback interface that belong to *this* application, never to
# an mtapi-io bridge. A buggy auto-detect once wrote the TradeBot API's own port
# into MT5_API_URL, which made every MT5 call hit our own FastAPI app and come
# back as `mtapi-io 404: {"detail":"Not Found"}`. Treat these as never valid.
_SELF_PORTS = {8000, 8080, 3000, 3001, 1448}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _points_at_self(url: str) -> bool:
    """True if `url` targets a local port served by TradeBot itself."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        extra = os.environ.get("BACKEND_PORT", "").strip()
        self_ports = _SELF_PORTS | ({int(extra)} if extra.isdigit() else set())
        return host in _LOCAL_HOSTS and port in self_ports
    except Exception:
        return False


def _validated_mt5_url(raw: str) -> str:
    url = raw.strip() if raw else ""
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = f"http://{url}"
    if url and _points_at_self(url):
        logger.warning(
            "[MT5] MT5_API_URL=%s points at TradeBot's own API port, not an "
            "mtapi-io bridge — ignoring it and using %s instead. "
            "Fix the value in .env.", url, _MT5_DEFAULT_URL,
        )
        return _MT5_DEFAULT_URL
    return url or _MT5_DEFAULT_URL


def _load_mt5_api_url() -> str:
    """Resolve MT5_API_URL: shell env (valid only) → .env file → default."""
    env_val = os.environ.get("MT5_API_URL", "").strip()
    if env_val and (env_val.startswith("http://") or env_val.startswith("https://")):
        if not _points_at_self(env_val):
            return env_val
        # Stale/poisoned value inherited from the parent process env — fall
        # through to .env so a corrected file wins over a bad inherited var.
        logger.warning(
            "[MT5] ignoring inherited MT5_API_URL=%s (TradeBot's own port); "
            "re-reading .env", env_val,
        )
        os.environ.pop("MT5_API_URL", None)
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
