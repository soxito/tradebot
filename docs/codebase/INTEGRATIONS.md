# Integrations

## Core Sections (Required)

### 1) Data Stores

| System | Role | Config | Evidence |
|--------|------|--------|----------|
| PostgreSQL 16 | Primary app DB (async SQLAlchemy + asyncpg) | `DATABASE_URL` | `docker-compose.yml`, `.env.example` |
| SQLite (aiosqlite) | Driver present for alternate/local modes | `DATABASE_URL` may use sqlite | `requirements.txt` |
| Redis 7 | EventBus pub/sub + cache | `REDIS_URL` | `events.py`, compose |

### 2) Crypto Exchanges

Via `ccxt` and dedicated Bitget SDK (`exchanges/`):

| Exchange | Env keys (from `.env.example`) |
|----------|--------------------------------|
| Binance | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| Bitget | `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE` |
| Bybit | `BYBIT_API_KEY`, `BYBIT_API_SECRET` |
| OKX | `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE` |
| KuCoin | `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_PASSPHRASE` |
| Coinbase | `COINBASE_API_KEY`, `COINBASE_API_SECRET` |

Registry: `ExchangeManager` in `backend/app/exchanges/manager.py` (supports testnet flag).

### 3) Market / sentiment data sources

| Source | Purpose | Config |
|--------|---------|--------|
| CryptoPanic | News feed | `CRYPTOPANIC_API_KEY` |
| CoinGecko / CoinMarketCap | Market data | `COINGECKO_API_KEY`, `COINMARKETCAP_API_KEY` |
| RSS / scrapers | News pipeline | `sentiment/` modules |
| Reddit | Social sentiment | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` |
| Twitter/X | Optional social | `TWITTER_BEARER_TOKEN` |

NLP: VADER + TextBlob (+ NLTK data downloaded in Docker image).

### 4) TradingView

- Webhook receiver under signals API.
- Signature validation: HMAC-SHA256 with `TRADINGVIEW_WEBHOOK_SECRET` (`security.py`).
- Production requires signature; non-production may allow missing signature.

### 5) Alerts & observability

| Integration | Config | Evidence |
|-------------|--------|----------|
| Telegram bot alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `.env.example` |
| Discord webhook | `DISCORD_WEBHOOK_URL` | `.env.example` |
| Email SMTP | `EMAIL_ALERTS_ENABLED`, `SMTP_*` | `.env.example` |
| Prometheus metrics | `PROMETHEUS_ENABLED`, metrics middleware | `main.py`, `monitoring/metrics.py` |
| Loguru file logs | `LOG_*` | `.env.example` |

### 6) Voice & vision

| Integration | Role | Notes |
|-------------|------|-------|
| Web Speech API | Primary free STT (browser) | Frontend / extension |
| Deepgram | Fallback STT + optional agents | Budget caps; key stays backend-side |
| MediaPipe / face_recognition | Optional face vision | Lazy import; `scripts/setup-face-vision.sh` |

### 7) Plugin external systems

| Plugin | External dependency |
|--------|---------------------|
| MT5TradingPlugin | mtapi-io REST (`MT5_API_URL`, default `http://localhost:8090`) |
| AiMarketAnalyst | Multi LLM providers (OpenAI, Groq, OpenRouter, Gemini, Mistral, Cerebras, DeepSeek, Together, FreeLLMAPI, custom) via provider config |
| KronosForecastPlugin | Kronos foundation model (local ML weights/scripts) |
| TelegramSignalNewsPlugin | Telegram user session / channels (`tradebot_telegram.session` present in repo tree — treat as sensitive) |
| ObsidianKnowledgePlugin | Obsidian Local REST API (`OBSIDIAN_REST_URL`, token) |
| OpenHumanPlugin | OpenHuman sidecar / MCP |
| OpenManusPlugin | OpenManus MCP sidecar |
| VibeTradingPlugin | Vibe-Trading FastAPI sidecar (docs cite `:8899`) |
| AgentPaulPlugin | Wraps core agents + live-trade; modes paper / TradeBot-execute / PAUL-direct |

### 8) Tunneling

- **ngrok** Python SDK + `ngrok_service` for optional public backend/frontend URLs.
- Controlled by settings + `NgrokConfig` DB row; UI page `/ngrok`.

### 9) Auth model (current)

- Optional `X-API-Key` header validated against `SECRET_KEY` (`verify_api_key`).
- Webhook HMAC for TradingView.
- **No full multi-user OAuth/session system observed in core security module** — many trading endpoints appear open when reachable on the network.
- Plugin manifests declare permission strings (e.g. `mt5.orders.trade`) — enforcement completeness is `[TODO]` without a full permissions middleware audit.

### 10) Credentials handling

- `.env` gitignored; template in `.env.example`.
- README security section: never commit keys; prefer testnet; exchange IP/withdrawal whitelists recommended.
- Known concern: MT5 plugin router has `# TODO: encrypt at rest` for password storage.

### 11) Evidence

- `.env.example`
- `backend/app/core/security.py`
- `backend/app/exchanges/*`
- `backend/app/core/events.py`
- `backend/app/sentiment/*`
- `backend/app/services/ngrok_service.py`
- `plugins/*/plugin.json`
- `docker-compose.yml`
- `README.md` Deepgram / Obsidian sections
