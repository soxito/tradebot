# Architecture

## Core Sections (Required)

### 1) System Overview

TradeBot is a **modular crypto (and MT5 forex) trading platform** with:

1. A **FastAPI core** for market data, signals, sentiment, risk, simulation, and live trading.
2. A **Next.js dashboard** with charts, wallets, strategy tools, and a global JARVIS/Paul assistant.
3. A **plugin system** that mounts additional routers/models without editing core packages (convention stated in README).
4. **Background workers** for schedulers, auto-trade loops, sniper, pump monitor, and pair catalog sync.
5. **Realtime fan-out** via SSE + Redis pub/sub (`EventBus`), with in-process fallback.

**Stated intent (README):** AI-powered trading bot combining news-driven sentiment with TradingView technical signals across multiple exchanges, plus voice-assisted operations.

### 2) Runtime Topology

```
Browser / JARVIS extension
        │
        ▼
  Next.js frontend (:3000 local / :3001 docker)
        │  HTTP + SSE
        ▼
  FastAPI backend (:1448 local / :8080 docker)
        │
        ├── PostgreSQL (async SQLAlchemy)
        ├── Redis (EventBus + cache)
        ├── Exchange APIs (ccxt + Bitget SDK)
        ├── Plugin routers (MT5, AI, Telegram, …)
        └── Optional: ngrok tunnels, Deepgram STT, Obsidian REST

  Worker process (compose: tradebot-worker)
        └── same codebase, START_WORKERS_IN_API=true
            runs scheduler + trading loops
```

Compose services: `postgres`, `redis`, `backend` (API-only workers disabled), `worker` (loops enabled), `frontend`.

### 3) Layering Pattern

Organization is **layer-oriented within core**, **feature-oriented for plugins**:

| Layer | Location | Role |
|-------|----------|------|
| HTTP | `backend/app/api/*` | Routers, request parsing, DB session injection |
| Domain services | `trading/`, `signals/`, `sentiment/`, `agents/`, `services/` | Business logic |
| Integrations | `exchanges/*`, plugin services | External APIs |
| Persistence | `models/database.py` + plugin models | SQLAlchemy tables |
| Infrastructure | `core/*`, `monitoring/*`, `workers/*` | Config, bus, metrics, loops |
| UI | `frontend/src/pages` + `components` | Presentation |
| Client API | `frontend/src/services/api.ts` | Typed/axios access to backend |

### 4) Primary Data Flows

#### A) Signal → decision → (sim or live) trade

1. Signals enter via TradingView webhook (`/api/v1/signals` + signature validation), generators/strategies, Telegram plugin, or monitors.
2. Decision logic in `trading/decision.py` + risk in `trading/risk.py`.
3. `TradingService.execute_decision(...)` places orders (`dry_run=True` by default safety posture) via `ExchangeManager`.
4. Rows stored as `Signal` / `Trade` (and sim tables for paper).
5. Events published on `EventBus` for SSE clients.

#### B) Realtime UI updates

1. Backend `EventBus` (`core/events.py`) publishes topics over Redis when available.
2. Clients subscribe via `/api/v1/stream/events` (SSE).
3. Frontend `useEventStream` / `notifications` surface live signals, trades, ticks, alerts.

#### C) Plugin mount (startup)

1. `main.py` lifespan: `init_db()`, optional plugin table create, background worker start (or skip if API-only).
2. If `PLUGIN_AUTO_MOUNT`, `PluginLoader.mount_routers(app, api_prefix="/api/v1")`.
3. Each plugin imports `router` from `service_provider` module path derived from package name + manifest.

#### D) JARVIS voice

1. Browser Web Speech is primary STT (client-side).
2. On low-confidence / miss, buffered audio posts to `/api/v1/voice/deepgram/stt`.
3. Budget guard (`deepgram_budget` service + config caps) may refuse paid STT.
4. Commands execute against trading/analysis endpoints; global UI via `PaulChat` in `Layout`.

### 5) Background Workers

Started by `start_background_workers` when allowed (`START_WORKERS_IN_API` or dedicated runner):

| Loop flag (settings) | Function |
|----------------------|----------|
| `AUTO_START_SCHEDULER` | General scheduler |
| `AUTO_START_SIM_AUTO_TRADE_LOOP` | Simulation auto-trade |
| `AUTO_START_LIVE_AUTO_TRADE_LOOP` | Live auto-trade (warns if `ENABLE_AUTO_TRADING` false) |
| `AUTO_START_POSITION_MONITOR_LOOP` | Open position monitor |
| `AUTO_START_SNIPER_LOOP` | Sniper cycle |
| `AUTO_START_PUMP_MONITOR_LOOP` | Pump monitor |
| `AUTO_START_PAIR_CATALOG_SYNC_LOOP` | Crypto pair catalog refresh |

API process also may start pair catalog + price tick loops independently for JARVIS market context.

### 6) Domain Models (core)

Defined in `backend/app/models/database.py` (representative):

- Trading: `Signal`, `Trade`, `BotStrategy`, `PineScript`, `LiveTradeSettings`, `SignalMonitorPair`
- Simulation: `SimAccount`, `SimOrder`, `SimPosition`
- Sentiment: `SentimentScore`, `NewsArticle`
- Agents: `Agent`, `AgentDecision`
- Markets: `CryptoPair`, `PumpToken`, `RugPullToken`
- Strategy lab: `StrategyLabVersion`, `StrategyLabRun`, `StrategyLabPromotion`
- Infra: `NgrokConfig`

Plugins own additional SQLAlchemy bases/tables initialized via `init_plugin_tables`.

### 7) Frontend Architecture Notes

- **Pages Router** (not App Router): `frontend/src/pages/*`.
- Global chrome: `Layout` sidebar nav (~30 routes) + floating `PaulChat` + connection banner.
- State: Zustand for UI prefs; SWR/axios for server state.
- Heavy 3D paths lazy-loaded / workerized for low-end hardware (`NEXT_PUBLIC_DISABLE_3D`, adaptive quality hooks).

### 8) Design Patterns Observed

| Pattern | Where |
|---------|-------|
| Plugin / service provider via manifest | `PluginLoader`, `plugin.json` |
| Manager / registry | `ExchangeManager`, LLM registry in AiMarketAnalyst |
| Strategy modules | `signals/*_strategy.py`, MT5 SMC/scalp strategies |
| Pub/sub event bus | `core/events.py` |
| Settings singleton | `settings` from pydantic-settings |
| Dry-run / kill-switch safety | `ENABLE_AUTO_TRADING`, Paul kill_switch settings |

### 9) Evidence

- `backend/app/main.py`
- `backend/app/api/routes.py`
- `backend/app/workers/runtime.py`
- `backend/app/plugins/loader.py`
- `backend/app/core/events.py`
- `backend/app/exchanges/manager.py`
- `backend/app/trading/service.py`
- `backend/app/models/database.py`
- `frontend/src/components/Layout.tsx`
- `docker-compose.yml`
- `README.md`
- `docs/backend-target-architecture-waves-1-3.md` (target evolution; may lag code)
