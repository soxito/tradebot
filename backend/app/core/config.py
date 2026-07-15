"""
Application Configuration
Loads settings from environment variables
"""
import secrets
import re as _re
from typing import List
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _reroute_unresolvable_host(url: str, service: str, native_host: str, native_ports: List[str]) -> str:
    """Reroute a Docker service hostname to a native host when it can't resolve.

    docker-compose `.env` files target service names like ``postgres`` / ``redis``.
    Those only resolve inside the compose network. When the backend runs natively
    (Homebrew / docker-on-host / ``start.py``), the name fails DNS resolution and
    asyncpg/redis raise ``socket.gaierror`` / ``[Errno 11001] getaddrinfo failed``.
    In that case we swap the host for ``native_host`` and probe the candidate host
    ports, picking the first that is actually listening — Homebrew and the
    docker-exposed mapping use different ports (e.g. Postgres 5434 vs 5433), so
    probing lets the SAME .env work in Docker, Homebrew, and docker-on-host setups.
    """
    import socket
    from urllib.parse import urlsplit, urlunsplit

    if not url:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    if parts.hostname != service:
        return url
    try:
        socket.getaddrinfo(service, None)
        return url  # resolvable (inside Docker) — keep as-is
    except OSError:
        pass  # not resolvable (running natively) — reroute below

    # Pick the first candidate port that is actually accepting connections so we
    # target the real local service regardless of how it was started.
    chosen_port = native_ports[0]
    for port in native_ports:
        try:
            with socket.create_connection((native_host, int(port)), timeout=0.5):
                chosen_port = port
                break
        except OSError:
            continue

    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    netloc = f"{userinfo}{native_host}:{chosen_port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))



class Settings(BaseSettings):
    """Application settings from environment variables"""
    
    # Application
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = "change-me-in-production-to-a-secure-random-string"
    
    # API
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:8000,http://localhost:8080"
    
    # Database
    # Use 127.0.0.1 instead of localhost: on Windows, asyncpg resolves
    # "localhost" to ::1 (IPv6) while PostgreSQL only listens on 127.0.0.1,
    # producing [Errno 11001] getaddrinfo failed.
    DATABASE_URL: str = "postgresql+asyncpg://tradebot:tradebot_password@127.0.0.1:5434/tradebot"
    
    # Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    
    # Exchange API Keys (NEVER commit real keys!)
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""
    
    BITGET_API_KEY: str = ""
    BITGET_API_SECRET: str = ""
    BITGET_PASSPHRASE: str = ""
    
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    
    OKX_API_KEY: str = ""
    OKX_API_SECRET: str = ""
    OKX_PASSPHRASE: str = ""
    
    KUCOIN_API_KEY: str = ""
    KUCOIN_API_SECRET: str = ""
    KUCOIN_PASSPHRASE: str = ""
    
    COINBASE_API_KEY: str = ""
    COINBASE_API_SECRET: str = ""
    
    # News & Sentiment API Keys
    COINGECKO_API_KEY: str = ""
    COINMARKETCAP_API_KEY: str = ""
    CRYPTOPANIC_API_KEY: str = ""
    MARKETAUX_API_KEY: str = ""
    GNEWS_API_KEY: str = ""
    CURRENTS_API_KEY: str = ""
    FRED_API_KEY: str = ""
    TWITTER_BEARER_TOKEN: str = ""
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""

    # Sentiment freshness controls
    SENTIMENT_MAX_AGE_HOURS: int = 2
    SENTIMENT_SCORE_VALID_MINUTES: int = 5
    SENTIMENT_AUTO_REFRESH_ON_READ: bool = True
    
    # TradingView Webhook
    TRADINGVIEW_WEBHOOK_SECRET: str = "change-me-to-secure-webhook-secret"

    # Voice / Deepgram
    DEEPGRAM_API_KEY: str = ""

    # Deepgram cost-aware fallback (pre-recorded STT) budget guard
    DEEPGRAM_FALLBACK_ENABLED: bool = True
    DEEPGRAM_MONTHLY_CAP_USD: float = 60.0      # hard monthly spend ceiling
    DEEPGRAM_DAILY_CAP_USD: float = 5.0         # daily sub-cap to smooth spend
    DEEPGRAM_STT_RATE_PER_MIN: float = 0.0043   # Nova pre-recorded $/min
    DEEPGRAM_STT_MODEL: str = "nova-3"          # pre-recorded model
    DEEPGRAM_TOTAL_CREDIT_USD: float = 200.0    # total credit, for runway projection

    # Monitoring & Alerts
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    DISCORD_WEBHOOK_URL: str = ""
    # Email (SMTP) alerts (optional)
    EMAIL_ALERTS_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""            # falls back to SMTP_USERNAME when blank
    SMTP_TO: str = ""             # comma-separated recipient list
    SMTP_USE_TLS: bool = True     # STARTTLS on 587; set False + port 465 for SSL
    PROMETHEUS_ENABLED: bool = True
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    LOG_FILE_PATH: str = "logs/tradebot.log"
    LOG_ROTATION: str = "10 MB"
    LOG_RETENTION: str = "14 days"
    ALERTS_ENABLED: bool = True
    ALERTS_MIN_LEVEL: str = "WARNING"
    
    # Trading Configuration
    ENABLE_AUTO_TRADING: bool = False  # Safety first - disabled by default
    MAX_POSITION_SIZE_USD: float = 1000.0
    MAX_RISK_PER_TRADE_PERCENT: float = 2.0
    MAX_TOTAL_EXPOSURE_USD: float = 10000.0

    # AI Agent Configuration
    ENABLE_AI_AGENTS: bool = False  # Must be explicitly enabled
    AI_MEMORY_LOOKBACK: int = 50  # Past decisions to consider for learning
    AI_MIN_MEMORY_FOR_LOCAL: int = 15  # Min decisions before agents can decide locally without OpenAI
    AI_LOCAL_CONFIDENCE_THRESHOLD: float = 0.70  # Min confidence from memory to skip OpenAI

    # Runtime bootstrap controls
    START_WORKERS_IN_API: bool = False  # Keep API startup side-effect free by default
    AUTO_START_SCHEDULER: bool = True
    SENTIMENT_PIPELINE_INTERVAL_SECONDS: int = 300
    SIGNALS_PIPELINE_INTERVAL_SECONDS: int = 180
    AUTO_START_SIM_AUTO_TRADE_LOOP: bool = False
    AUTO_START_LIVE_AUTO_TRADE_LOOP: bool = False
    AUTO_START_POSITION_MONITOR_LOOP: bool = False
    AUTO_START_SNIPER_LOOP: bool = False
    AUTO_START_PUMP_MONITOR_LOOP: bool = False
    # Crypto-pair catalog (names + market cap/volume) sync loop. Enabled by
    # default and also started at API boot so JARVIS always has coin names.
    AUTO_START_PAIR_CATALOG_SYNC_LOOP: bool = True
    PAIR_CATALOG_REFRESH_MINUTES: int = 15   # fast market cap/volume refresh
    PAIR_CATALOG_FULL_SYNC_HOURS: int = 6    # slow full enrich (names + profiles)

    # Loop intervals (seconds)
    SIM_AUTO_TRADE_LOOP_INTERVAL_SECONDS: int = 60
    LIVE_AUTO_TRADE_LOOP_INTERVAL_SECONDS: int = 60
    POSITION_MONITOR_INTERVAL_SECONDS: int = 900
    SNIPER_LOOP_INTERVAL_SECONDS: int = 60
    PUMP_MONITOR_LOOP_INTERVAL_SECONDS: int = 120
    PUMP_MONITOR_PUMPED_RETENTION_HOURS: int = 24

    # Realtime price-tick fan-out (SSE). Broadcasts live prices for symbols with
    # open positions / active signals to all stream subscribers, replacing
    # per-client price polling. Only runs while at least one client is connected.
    AUTO_START_PRICE_TICK_LOOP: bool = True
    PRICE_TICK_INTERVAL_SECONDS: int = 5
    PRICE_TICK_MAX_SYMBOLS: int = 30

    # ── ngrok tunnel ──────────────────────────────────────────────────────────
    # Auth token from https://dashboard.ngrok.com/authtokens
    NGROK_AUTHTOKEN: str = ""
    # Auto-start ngrok on backend startup (set True to enable hybrid auto-start)
    NGROK_AUTO_START: bool = False
    # Local backend address to forward (e.g. http://localhost:1448)
    NGROK_BACKEND_ADDR: str = "http://localhost:1448"
    # Local frontend address to forward (e.g. http://localhost:3000)
    NGROK_FRONTEND_ADDR: str = "http://localhost:3000"
    # OAuth provider — always "google". Field kept for config snapshot visibility.
    NGROK_OAUTH_PROVIDER: str = "google"

    # Plugin system
    PLUGIN_AUTO_MOUNT: bool = True
    PLUGIN_STRICT_MODE: bool = False
    PLUGINS_DIR: str = "plugins"

    # Obsidian Knowledge Vault integration
    OBSIDIAN_VAULT_PATH: str = "~/obsidian-vault/tradebot"
    OBSIDIAN_REST_URL: str = ""           # e.g. https://localhost:27124
    OBSIDIAN_REST_TOKEN: str = ""
    OBSIDIAN_AUTO_SYNC_MINUTES: int = 15
    OBSIDIAN_EXPORT_DECISIONS: bool = True
    OBSIDIAN_EXPORT_SIGNALS: bool = True
    OBSIDIAN_EXPORT_COMMUNITIES: bool = True
    OBSIDIAN_INJECT_CONTEXT: bool = False
    OBSIDIAN_CONTEXT_NOTES_LIMIT: int = 5
    OBSIDIAN_CONTEXT_TOKEN_BUDGET: int = 800

    @model_validator(mode='before')
    @classmethod
    def strip_inline_env_comments(cls, values: dict) -> dict:
        """Strip inline shell comments from .env string values before type coercion.

        A .env line like::

            SOME_BOOL=false   # enable this in production

        is loaded as the raw string ``'false   # enable this in production'``.
        Pydantic cannot parse that as a bool (or int, etc.), so we strip
        everything from the first ``\\s+#`` onwards before field validators run.
        """
        cleaned: dict = {}
        for key, val in values.items():
            if isinstance(val, str):
                # Strip inline comment: keep only the part before ` #` or `\t#`
                stripped = _re.sub(r'[ \t]+#.*$', '', val).strip()
                cleaned[key] = stripped
            else:
                cleaned[key] = val
        return cleaned

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Normalise the database host for the current runtime.

        - Windows: 'localhost' -> '127.0.0.1' (asyncpg resolves localhost to ::1
          but PostgreSQL listens on 127.0.0.1, causing errno 11001 getaddrinfo).
        - Docker service name 'postgres' used while running natively can't resolve
          -> reroute to the native Postgres (Homebrew/start.py default 5434).
        """
        import os
        if os.name == "nt" and "localhost" in v:
            v = v.replace("localhost", "127.0.0.1")
        # Probe 5434 (Homebrew), 5433 (docker-exposed), 5432 (default) in order.
        v = _reroute_unresolvable_host(v, "postgres", "127.0.0.1", ["5434", "5433", "5432"])
        return v

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def normalize_redis_url(cls, v: str) -> str:
        """Ensure REDIS_URL carries a valid scheme and a resolvable host.

        - A bare 'host:port' in .env makes redis-py raise 'Redis URL must specify
          one of the following schemes' -> prepend 'redis://' when missing.
        - Windows: 'localhost' -> '127.0.0.1'.
        - Docker service name 'redis' used while running natively can't resolve
          -> reroute to the native Redis (127.0.0.1:6379).
        """
        if v and "://" not in v:
            v = f"redis://{v}"
        import os
        if os.name == "nt" and "localhost" in v:
            v = v.replace("localhost", "127.0.0.1")
        # Probe 6379 (Homebrew/native), 6380 (docker-exposed) in order.
        v = _reroute_unresolvable_host(v, "redis", "127.0.0.1", ["6379", "6380"])
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]
    
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env", "../../.env"),  # search backend/, project root, grandparent
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()

# ── Startup safety checks ─────────────────────────────────
_DEFAULT_SECRET = "change-me-in-production-to-a-secure-random-string"
_DEFAULT_WEBHOOK = "change-me-to-secure-webhook-secret"

if settings.SECRET_KEY == _DEFAULT_SECRET:
    if settings.ENVIRONMENT == "production":
        raise RuntimeError(
            "SECRET_KEY must be set to a secure random value in production. "
            "Run: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
        )
    # Auto-generate for dev/test so the app doesn't run with the known default
    settings.SECRET_KEY = secrets.token_urlsafe(48)

if settings.TRADINGVIEW_WEBHOOK_SECRET == _DEFAULT_WEBHOOK:
    if settings.ENVIRONMENT == "production":
        raise RuntimeError(
            "TRADINGVIEW_WEBHOOK_SECRET must be set in production."
        )
