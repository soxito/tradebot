"""
Application Configuration
Loads settings from environment variables
"""
import os
import secrets
import re as _re
from pathlib import Path
from typing import List, Optional
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _desktop_dir(var: str) -> str:
    """Read a desktop-mode directory override from the environment.

    The packaged desktop app (see ``desktop/main.js``) has no project checkout to
    write into — the app bundle is read-only on macOS and under ``Program Files``
    on Windows. Electron therefore passes the per-user data directory and the
    location of the exported frontend through the environment. Returns "" when
    unset, which is what every non-desktop entrypoint (``start.py``, Docker,
    pytest) sees, leaving their behaviour untouched.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return raw


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
    # Echo every SQL statement to stdout. Deliberately NOT tied to DEBUG: it is
    # a firehose (a few KB per statement) and the app runs a dozen background
    # loops, so with it on the process writes stdout faster than a terminal or a
    # log collector drains it. Once that pipe's buffer fills, the next write
    # BLOCKS — the process stays alive with an idle event loop but stops serving
    # HTTP entirely, which looks exactly like a crash. Turn it on deliberately,
    # for a single debugging session, and turn it back off.
    SQL_ECHO: bool = False
    SECRET_KEY: str = "change-me-in-production-to-a-secure-random-string"
    # System Monitor task-control API key. Off-localhost mutations require this
    # in the X-API-Key header; loopback callers are allowed without it (GETs are
    # always open). Empty in dev; must be set in production.
    TASKS_API_KEY: str = ""
    
    # API
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:8000,http://localhost:8080"
    
    # Database
    # Use 127.0.0.1 instead of localhost: on Windows, asyncpg resolves
    # "localhost" to ::1 (IPv6) while PostgreSQL only listens on 127.0.0.1,
    # producing [Errno 11001] getaddrinfo failed.
    DATABASE_URL: str = "postgresql+asyncpg://tradebot:tradebot_password@127.0.0.1:5434/tradebot"

    # Connection pooling. Off by default: NullPool has masked sessions held
    # across slow awaits, so enabling a pool needs those audited first. Enable
    # with TRADEBOT_DB_POOL_ENABLED=1. SQLite always uses NullPool regardless.
    TRADEBOT_DB_POOL_ENABLED: bool = False
    TRADEBOT_DB_POOL_SIZE: int = 10
    
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

    # OpenAI (used for Whisper STT / TTS fallback, and OpenAI-backed AI agents)
    OPENAI_API_KEY: str = ""

    # TradingAgents sidecar service. The multi-agent LLM framework runs in its
    # own process (integrations/TradingAgents/.venv) so its heavy dependency
    # stack (langchain, langgraph, pandas 3.x) never loads into this backend.
    TRADINGAGENTS_SERVICE_URL: str = "http://127.0.0.1:8010"
    # When True the backend spawns the sidecar as a child process at startup
    # (and stops it on shutdown). Set False if you run it via docker-compose
    # or manually — then only the URL above matters.
    TRADINGAGENTS_SERVICE_AUTOSTART: bool = True
    # Proxy timeout for starting/reading runs through to the sidecar. A full
    # analysis takes minutes; these bound only the HTTP hop.
    TRADINGAGENTS_PROXY_TIMEOUT_START: int = 30
    TRADINGAGENTS_PROXY_TIMEOUT_READ: int = 60

    # NVIDIA NIM hosted speech (fast, cloud) — tried first for /voice/stt and
    # /voice/tts, ahead of the slow local MLX engine below. Uses the same
    # nvapi- key already configured for other NVIDIA-backed AI providers.
    NVIDIA_API_KEY: str = ""
    NVIDIA_ASR_ENABLED: bool = True
    # Hosted Parakeet CTC 1.1B ASR invocation URL (build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr).
    # NVCF gives each hosted function its own subdomain; if this ever 404s,
    # re-copy the curl example from that model's "API" tab and update this.
    NVIDIA_ASR_INVOKE_URL: str = "https://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions"
    NVIDIA_ASR_LANGUAGE: str = "en-US"
    NVIDIA_TTS_ENABLED: bool = True
    # Magpie TTS Multilingual NVCF function-id (build.nvidia.com/nvidia/magpie-tts-multilingual),
    # reached via Riva gRPC at grpc.nvcf.nvidia.com, not plain REST.
    NVIDIA_TTS_FUNCTION_ID: str = "877104f7-e885-42b9-8de8-f6e4c6303969"
    NVIDIA_TTS_VOICE: str = "Magpie-Multilingual.EN-US.Aria"
    NVIDIA_TTS_LANGUAGE: str = "en-US"
    NVIDIA_TTS_SAMPLE_RATE_HZ: int = 22050
    NVIDIA_SPEECH_TIMEOUT_MS: int = 8000

    # Local speech engine (huggingface/speech-to-speech, Parakeet-TDT STT +
    # Qwen3-TTS) — runs as its own sibling service (see speech_engine/). Tried
    # after NVIDIA NIM (offline/private fallback — much slower on CPU/MPS),
    # before finally falling back to OpenAI.
    SPEECH_ENGINE_ENABLED: bool = False
    SPEECH_ENGINE_URL: str = "http://localhost:8790"
    SPEECH_ENGINE_TIMEOUT_MS: int = 4000

    # Deepgram cost-aware fallback (pre-recorded STT) budget guard
    DEEPGRAM_FALLBACK_ENABLED: bool = True
    DEEPGRAM_MONTHLY_CAP_USD: float = 60.0      # hard monthly spend ceiling
    DEEPGRAM_DAILY_CAP_USD: float = 5.0         # daily sub-cap to smooth spend
    DEEPGRAM_STT_RATE_PER_MIN: float = 0.0043   # Nova pre-recorded $/min
    DEEPGRAM_STT_MODEL: str = "nova-3"          # pre-recorded model
    DEEPGRAM_TOTAL_CREDIT_USD: float = 200.0    # total credit, for runway projection

    # Bitcoin 1064-day market cycle (editable at runtime via room settings)
    CYCLE_BULL_DAYS: int = 1064                 # bottom → projected top
    CYCLE_BEAR_DAYS: int = 365                  # projected top → next bottom
    CYCLE_HISTORY_YEARS: int = 15               # years of monthly candles the cycle screen loads
    CYCLE_RECHECK_INTERVAL_SECONDS: int = 3600  # detector loop tick
    CYCLE_CACHE_TTL_SECONDS: int = 900          # snapshot resolver cache

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
    # Closed candles agents analyse for market movement. Floored at 28 everywhere
    # (never less than the user's minimum) and has no hard upper cap — raise
    # AGENT_ANALYSIS_CANDLES via env for deeper reads across every agent.
    AGENT_ANALYSIS_CANDLES: int = 120
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
    # SMC background research (economic calendar + news + sentiment). Uses only
    # IDLE AI providers — never the one serving live /mt5-live analysis.
    AUTO_START_RESEARCH_LOOP: bool = True
    # Per-signal research: every Telegram/sniper/SMC/core signal is researched
    # into a prediction. Concurrency is the cap on how many signals are worked on
    # at once — it exists to leave AI provider capacity for live analysis.
    AUTO_START_SIGNAL_RESEARCH_QUEUE: bool = True
    # Obsidian vault export. Keeps the knowledge base current without anyone
    # having to remember to press Sync.
    AUTO_START_VAULT_SYNC_LOOP: bool = True
    VAULT_SYNC_INTERVAL_SECONDS: int = 300   # 5 minutes
    SIGNAL_RESEARCH_CONCURRENCY: int = 5
    SIGNAL_RESEARCH_SCAN_SECONDS: int = 180
    # Crypto-pair catalog (names + market cap/volume) sync loop. Enabled by
    # default and also started at API boot so JARVIS always has coin names.
    AUTO_START_PAIR_CATALOG_SYNC_LOOP: bool = True
    PAIR_CATALOG_REFRESH_MINUTES: int = 15   # fast market cap/volume refresh
    PAIR_CATALOG_FULL_SYNC_HOURS: int = 6    # slow full enrich (names + profiles)
    # JARVIS self-learning: settles the trade proposals JARVIS published against
    # what price actually did, so its confidence is measured, not asserted.
    AUTO_START_JARVIS_LEARNING_LOOP: bool = True
    JARVIS_JOURNAL_EXPIRY_HOURS: int = 72     # unresolved after this → closed out
    JARVIS_LEARNING_MIN_SAMPLES: int = 8      # below this a win rate is noise
    JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE: int = 40  # caps upstream candle fetches
    # Agent-decision learning: settles agent_decisions.outcome from closed
    # trades in both order books and auto-runs the revision-tracked
    # self-improve pass for seats with enough resolved samples.
    AUTO_START_DECISION_LEARNING_LOOP: bool = True
    DECISION_LEARNING_INTERVAL_SECONDS: int = 900
    # Trading room: keeps the agent board in session on the focused pair, the
    # newest signal, or a rotation — so the room has fresh decisions even when
    # nobody has the page open. Off by default; it spends AI tokens per cycle.
    AUTO_START_ROOM_WORKER: bool = False
    ROOM_WORKER_INTERVAL_SECONDS: int = 300
    ROOM_WORKER_COOLDOWN_SECONDS: int = 1800  # min gap before re-analysing a pair
    # Seed only. Once the room has settings the timeframe is chosen there
    # (room_settings.focus_timeframe) so the board and the room's chart agree.
    ROOM_WORKER_TIMEFRAME: str = "1h"
    # Copy-trading worker: mirrors enabled copy profiles (sim + live followers)
    # on a fast clock. Individual profiles are gated by their own enabled flag.
    COPY_WORKER_INTERVAL_SECONDS: int = 30

    # ── Resource tiering (env contract from start.py; .env still wins) ─────────
    # start.py derives these from the machine and injects them via setdefault, so
    # explicit .env values always take precedence. PERF_TIER is the resolved
    # value, exposed for the config snapshot and the System Monitor page.
    TRADEBOT_TIER: str = ""
    TRADEBOT_PROFILE: str = ""
    TRADEBOT_RAM_GB: float = 0.0
    TRADEBOT_PHYSICAL_CORES: int = 0
    TRADEBOT_SIDECARS: str = ""
    PERF_TIER: str = "high"

    # Loop intervals (seconds)
    SIM_AUTO_TRADE_LOOP_INTERVAL_SECONDS: int = 60
    LIVE_AUTO_TRADE_LOOP_INTERVAL_SECONDS: int = 60
    POSITION_MONITOR_INTERVAL_SECONDS: int = 900
    SNIPER_LOOP_INTERVAL_SECONDS: int = 60
    PUMP_MONITOR_LOOP_INTERVAL_SECONDS: int = 120
    PUMP_MONITOR_PUMPED_RETENTION_HOURS: int = 24
    RESEARCH_LOOP_INTERVAL_SECONDS: int = 900  # 15 min — free news feeds
    JARVIS_LEARNING_INTERVAL_SECONDS: int = 900  # 15 min — settle proposals
    DECISION_LEARNING_INTERVAL_SECONDS: int = 900  # 15 min — settle agent decisions

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

    # Agent-Reach integration (github.com/Panniantong/Agent-Reach) — gives
    # agents web read/search, YouTube and GitHub research. Off by default:
    # the CLI must be installed on the host (`agent-reach install --env=auto`)
    # before this does anything. ENABLED gates the client + on-demand tools;
    # INJECT_CONTEXT separately gates always-on prompt injection into
    # trading-decision paths (orchestrator/JARVIS), which adds latency.
    AGENT_REACH_ENABLED: bool = False
    AGENT_REACH_BIN: str = "agent-reach"
    AGENT_REACH_TIMEOUT_S: float = 15.0
    AGENT_REACH_CONTEXT_TOKEN_BUDGET: int = 800
    AGENT_REACH_INJECT_CONTEXT: bool = False

    # ── Hermes Agent (NousResearch/hermes-agent) ─────────────────────
    # Self-aware upgrade: episodic + skill + user-model (locked scope per plan).
    # Runs as isolated sidecar on 8011 (like TradingAgents on 8010). Vault stays
    # as async exporter only; hermes_state.db is recall source (90d retention,
    # recall-only — scoring stays on Postgres AgentDecision).
    HERMES_ENABLED: bool = False
    HERMES_GATEWAY_URL: str = "http://127.0.0.1:8011"
    HERMES_STATE_PATH: str = ""              # default: DATA_DIR/hermes_state.db or ./data/hermes_state.db
    HERMES_SKILLS_PATH: str = ""             # default: DATA_DIR/hermes_skills or ./hermes_skills
    HERMES_RETENTION_DAYS: int = 90
    HERMES_CRON_ENABLED: bool = False        # episodic+skill+user-model only — no Honcho/nudge by default
    HERMES_AUTO_INGEST: bool = True          # ingest every Trading Room session + JARVIS turn
    HERMES_GATEWAY_AUTOSTART: bool = False   # start sidecar as child process (like TradingAgents)
    SOUL_PATH: str = "SOUL.md"               # merged JARVIS/Paul/SOX directive

    # ── Desktop (packaged Electron app) ───────────────────────
    # Both are populated from TRADEBOT_DATA_DIR / TRADEBOT_STATIC_DIR by the
    # model validator below rather than declared as env fields, so the names the
    # Electron shell sets stay decoupled from the field names used here.
    DATA_DIR: str = ""
    STATIC_DIR: str = ""

    @property
    def is_desktop(self) -> bool:
        """True when running as the packaged desktop app."""
        return bool(self.DATA_DIR)

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

    @model_validator(mode='after')
    def apply_resource_tier(self) -> "Settings":
        """Scale loop intervals, autostart flags and concurrency caps to the
        machine's resource tier.

        Only touches fields the user did **not** pin explicitly (absent from
        ``model_fields_set``), so anything set in ``.env`` survives untouched.
        Falls back *up* to ``high`` when no tier is provided — never guess low
        and silently disable features.
        """
        from app.core.resource_tier import (
            resolve_tier, interval_multiplier, should_autostart, tier_index,
        )

        tier = resolve_tier(self.TRADEBOT_PROFILE, self.TRADEBOT_TIER)
        self.PERF_TIER = tier
        mult = interval_multiplier(tier)
        pinned = self.model_fields_set

        def _set(name: str, value) -> None:
            if name not in pinned:
                setattr(self, name, value)

        if mult != 1.0:
            _interval_fields = [
                "SENTIMENT_PIPELINE_INTERVAL_SECONDS",
                "SIGNALS_PIPELINE_INTERVAL_SECONDS",
                "POSITION_MONITOR_INTERVAL_SECONDS",
                "SNIPER_LOOP_INTERVAL_SECONDS",
                "PUMP_MONITOR_LOOP_INTERVAL_SECONDS",
                "RESEARCH_LOOP_INTERVAL_SECONDS",
                "JARVIS_LEARNING_INTERVAL_SECONDS",
                "VAULT_SYNC_INTERVAL_SECONDS",
                "SIGNAL_RESEARCH_SCAN_SECONDS",
                "PRICE_TICK_INTERVAL_SECONDS",
                "SIM_AUTO_TRADE_LOOP_INTERVAL_SECONDS",
                "LIVE_AUTO_TRADE_LOOP_INTERVAL_SECONDS",
            ]
            for name in _interval_fields:
                if name in pinned:
                    continue
                current = getattr(self, name, None)
                if isinstance(current, int) and current > 0:
                    setattr(self, name, int(round(current * mult)))

        # Autostart gating — disable non-critical loops below their min tier.
        # Critical tasks (position_monitor, live_auto_trade, sniper) always
        # autostart regardless of tier.
        _autostart_map = {
            "AUTO_START_RESEARCH_LOOP": "research_loop",
            "AUTO_START_SIGNAL_RESEARCH_QUEUE": "signal_research_queue",
            "AUTO_START_VAULT_SYNC_LOOP": "vault_sync",
            "AUTO_START_JARVIS_LEARNING_LOOP": "jarvis_learning",
            "AUTO_START_PRICE_TICK_LOOP": "price_tick",
            "AUTO_START_PUMP_MONITOR_LOOP": "pump_monitor",
            "AUTO_START_SIM_AUTO_TRADE_LOOP": "sim_auto_trade",
        }
        for flag, task_id in _autostart_map.items():
            if flag in pinned:
                continue
            if getattr(self, flag, False) and not should_autostart(task_id, tier):
                setattr(self, flag, False)

        # Concurrency / volume caps — lower on weak machines, never raise.
        idx = tier_index(tier)  # minimal=0 … ultra=4
        _research_conc_cap = {0: 1, 1: 1, 2: 2, 3: 5, 4: 5}.get(idx, 5)
        if "SIGNAL_RESEARCH_CONCURRENCY" not in pinned:
            self.SIGNAL_RESEARCH_CONCURRENCY = min(self.SIGNAL_RESEARCH_CONCURRENCY, _research_conc_cap)
        _settle_cap = {0: 8, 1: 12, 2: 20, 3: 40, 4: 40}.get(idx, 40)
        if "JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE" not in pinned:
            self.JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE = min(
                self.JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE, _settle_cap
            )
        _tick_symbol_cap = {0: 8, 1: 12, 2: 20, 3: 30, 4: 30}.get(idx, 30)
        if "PRICE_TICK_MAX_SYMBOLS" not in pinned:
            self.PRICE_TICK_MAX_SYMBOLS = min(self.PRICE_TICK_MAX_SYMBOLS, _tick_symbol_cap)

        # Hermes: disable entirely on minimal/low, gate cron on medium+
        if tier_index(tier) <= 1 and "HERMES_ENABLED" not in pinned:
            self.HERMES_ENABLED = False
        if tier_index(tier) <= 2 and "HERMES_CRON_ENABLED" not in pinned:
            self.HERMES_CRON_ENABLED = False

        return self

    @model_validator(mode='after')
    def apply_desktop_mode(self) -> "Settings":
        """Point the app at per-user storage when running as the desktop app.

        The desktop build ships no PostgreSQL and no Redis. Redis already
        degrades to in-memory fan-out on its own (``core/events.py``), so only
        the database needs redirecting — to a SQLite file under the user's data
        directory. ``DATABASE_URL`` set explicitly in the environment still wins,
        so a power user can point the desktop app at a real Postgres.
        """
        self.DATA_DIR = _desktop_dir("TRADEBOT_DATA_DIR")
        self.STATIC_DIR = _desktop_dir("TRADEBOT_STATIC_DIR")

        if self.DATA_DIR and not os.environ.get("DATABASE_URL", "").strip():
            db_path = Path(self.DATA_DIR) / "tradebot.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # as_posix(): SQLAlchemy's SQLite URL parser treats backslashes as
            # literal path characters, so a Windows path must use forward slashes.
            self.DATABASE_URL = f"sqlite+aiosqlite:///{db_path.as_posix()}"

        return self

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
