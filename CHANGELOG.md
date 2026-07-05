# Changelog

All notable changes to TradeBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses a rolling `main` branch (no semantic version tags yet), so
entries are grouped under `[Unreleased]` and dated milestone headings.

## [Unreleased]

### Added
- **Email (SMTP) alert channel** for the monitoring system, alongside the
  existing Telegram and Discord alerts. `AlertService.notify()` now also emails
  on qualifying events; delivery is non-blocking (SMTP runs in a worker thread
  via `asyncio.to_thread`) and supports STARTTLS (port 587), implicit SSL
  (port 465) and plain SMTP. Configured via `EMAIL_ALERTS_ENABLED`, `SMTP_HOST`,
  `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO` and
  `SMTP_USE_TLS` (documented in `.env.example`). `GET /api/v1/monitoring/status`
  now reports `email_configured`, and email successes/failures are recorded in
  the alert metrics.
- **Crypto market-cap monitor page** (`/market-cap`) for tracking overall
  market capitalization.
- **JARVIS browser extension**: the popup now shows real exchange account
  balances fetched from the backend.
- **Windows + low-end hardware compatibility** (target: Intel Core i5-4300U,
  2-core/4-thread, Intel HD 4400 iGPU, 16 GB RAM).
  - `start.bat` — one-click Windows launcher wrapping the cross-platform
    `start.py` (auto-selects the `py -3` launcher, falls back to `python`).
  - Frontend 3D/WebGL kill-switch: `NEXT_PUBLIC_DISABLE_3D` (auto-enabled on the
    `low` UI tier) plus a per-browser `localStorage` override
    (`tradebot.disable3d`). Skips the Three.js JARVIS robot and the 3D
    force-graph on weak GPUs; the 2D graph stays fully functional.
  - `start.py` now detects real system RAM on Windows (`GlobalMemoryStatusEx`)
    and true physical core count (PowerShell/`wmic`/`psutil`).
  - `start.py --stop` gained a PID-file fallback (`taskkill` / `os.kill`) so
    services can be stopped on Windows where `pgrep`/`pkill` don't exist.

### Changed
- `start.py` resource tuning: on machines with ≤4 physical cores, the ML/BLAS
  thread cap now leaves headroom for the async event loop (e.g. a 2-core/4-thread
  box gets 2 ML threads, not 4). The backend also skips uvicorn `--reload`
  (and its file watchers) on ≤2 physical cores — override with `TRADEBOT_RELOAD=1`.
- The face-vision dependencies (`mediapipe`, `opencv-python-headless`,
  `face_recognition`) are now **optional** and commented out of
  `backend/requirements.txt`. `face_recognition` pulls in `dlib`, which has no
  prebuilt Windows wheel, so shipping it in the core requirements broke
  `pip install` on Windows. The vision endpoints import these libraries lazily,
  so the backend starts fine without them. Enable face-vision explicitly with
  `bash scripts/setup-face-vision.sh`.

### Fixed
- `python start.py --status` no longer crashes on Windows (the Obsidian
  process check now uses a guarded, cross-platform helper).

## 2026-07-05 — Realtime streaming, deep AI analysis & Kronos models

### Added
- **Server-Sent Events (SSE) realtime backbone** replacing UI polling.
  - `EventBus` (Redis pub/sub across workers with an in-memory fallback) and a
    `GET /api/v1/stream/events` endpoint (topics: signals, trades, sniper,
    sentiment, monitor status, price ticks, system alerts).
  - Frontend shares a single `EventSource` across browser tabs via Web Locks
    leader election + `BroadcastChannel`; consumers keep polling only as a
    fallback when the stream is disconnected. Live/Poll badge in the header.
  - Opt-in Web Notifications + Vibration and a Screen Wake Lock for live trading.
- **Position-aware, AI-composed deep analysis** in JARVIS: `/analyze` now
  enriches crypto pairs with volume pressure, live news/sentiment, a Kronos
  forecast, your open position (side, size, entry, live PnL) and a natural-language
  narrative. Every request is captured to all three "brains" for learning.
- **Kronos forecasting models**: all three published Kronos models (mini/small/base)
  are supported with per-model install status and an "Install all" action.
- `start.py` now auto-installs and health-checks Redis (Homebrew or Docker) and
  injects `REDIS_URL` so the EventBus fans out across workers.

### Fixed
- Kronos "No OHLCV data available" — the forecaster now falls back to a keyless
  public `ccxt` fetch and runs a setup self-test, so forecasts work without
  exchange API keys.

## 2026-07-04 — Off-thread rendering & Bitget unified accounts

### Added
- **Web Worker rendering** for the JARVIS UI to keep the main thread responsive:
  the ~980-particle S.O.X orb (OffscreenCanvas worker) and the Three.js robot
  avatar now render off the main thread, with graceful main-thread fallbacks.
- **Adaptive graphics auto-scaling**: a device performance tier (low → ultra)
  derived from CPU cores + memory, with a runtime FPS monitor that downgrades
  quality to stay smooth on weak/thermally-throttled machines.
- **Bitget multi-account + unified-account support**: aggregates USDT/USDC/COIN
  futures balances and positions across product types, main + sub-accounts, with
  a read-only diagnostic script.

## 2026-07-03 — Bitget futures margin & leverage fix

### Fixed
- Bitget error 45117 ("margin mode cannot be adjusted"): the futures order path
  now detects a symbol's existing margin mode from open positions/orders and
  matches it instead of forcing a change while a position or pending order exists.

## Baseline — Core platform (Phases 1–6) + plugins + JARVIS

### Added
- **Backend**: FastAPI (Python 3.13), SQLAlchemy (async) + asyncpg, Redis,
  multi-exchange connectors via `ccxt` (Binance, Bitget, Bybit, OKX, KuCoin,
  Coinbase) plus a native Bitget v2 SDK for spot + futures.
- **TradingView webhook receiver** with signature validation and a signal +
  decision engine (position sizing, stop-loss / take-profit, risk limits).
- **News & sentiment pipeline**: RSS feeds + CryptoPanic scored with VADER +
  TextBlob, written to `sentiment_scores` and surfaced on the dashboard.
- **Frontend**: Next.js 16 + React 19 + TypeScript + Tailwind, TradingView
  Lightweight Charts, multi-exchange wallet balances, trade history and an
  auto-testing connection banner.
- **Standalone plugin architecture** (`plugins/`, never modifies core):
  - `MT5TradingPlugin` — MetaTrader 5 REST bridge integration.
  - `AiMarketAnalyst` — multi-provider LLM router (Groq, OpenRouter, Gemini,
    Mistral, Cerebras, DeepSeek, Together, OpenAI, and custom endpoints) with
    automatic failover.
  - `KronosForecastPlugin` — open-source Kronos ML candle forecasting with a
    heuristic fallback when the model isn't installed.
  - `TelegramSignalNewsPlugin` — Telegram channel ingestion, signal parsing,
    news-to-sentiment, and a sniper auto-trade lifecycle.
  - `ObsidianKnowledgePlugin` — knowledge-base integration.
  - `AgentPaulPlugin` — background "subconscious" agent.
- **JARVIS / Paul assistant**: global voice + chat widget with hands-free
  navigation and click, cost-aware Deepgram STT fallback (free Web Speech
  primary), speaker-ID voice matching, and a WebGL S.O.X room.
- **Monitoring & alerting**: Prometheus metrics, structured logging, and a
  configurable alert service (Telegram + Discord) with a minimum-severity gate
  and a `GET /api/v1/monitoring/status` endpoint.
- **Cross-platform launcher** `start.py` (+ `run-local.sh`) that provisions
  Postgres/Redis (Homebrew or Docker), builds the Python venv, installs
  dependencies, and starts the backend and frontend with resource-aware tuning.
