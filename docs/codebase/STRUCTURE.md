# Project Structure

## Core Sections (Required)

### 1) Top-Level Layout

| Path | Purpose | Evidence |
|------|---------|----------|
| `backend/` | FastAPI app, workers, tests, requirements | tree, `README.md` |
| `frontend/` | Next.js Pages Router UI | `frontend/package.json` |
| `plugins/` | Standalone plugin packages (manifest-driven) | `plugin.json` files, `PluginLoader` |
| `jarvis-extension/` | Chrome extension for JARVIS voice | `jarvis-extension/manifest.json` |
| `scripts/` | Setup, diagnostics, provider tests | `scripts/` |
| `docs/` | Architecture plans, lessons, product specs | `docs/` |
| `obsidian-vault/` | Local Obsidian vault for knowledge plugin | `obsidian-vault/SETUP.md` |
| `graphify-out/` | Generated code-graph artifacts (not app runtime) | `graphify-out/` |
| `start.py` | Cross-platform local launcher | root |
| `run-local.sh` / `start.bat` | Shell/Windows wrappers | root |
| `docker-compose.yml` | Full stack compose | root |
| `.env.example` | Env template | root |
| `.github/` | Agents, prompts, skills, CI | `.github/` |

### 2) Entry Points

| Entry | Role | Evidence |
|-------|------|----------|
| `backend/app/main.py` | FastAPI app (`app`), lifespan, CORS, plugin mount | source |
| `backend/app/workers/runner.py` | Dedicated worker process (`python -m app.workers.runner`) | `docker-compose.yml` command |
| `backend/app/workers/runtime.py` | Starts/stops background loops | source |
| `frontend/src/pages/_app.tsx` | Next.js app shell | source |
| `frontend/src/pages/index.tsx` | Dashboard page | pages tree |
| `start.py` | Provisions DB/Redis/venv and launches processes | README |
| Plugin `backend/router.py` modules | Mounted at `/api/v1` via `PluginLoader` | `loader.py`, `plugin.json` `service_provider` |

### 3) Backend Package Map (`backend/app/`)

| Directory | Responsibility |
|-----------|----------------|
| `api/` | HTTP routers (19 modules + `routes.py` aggregator) |
| `core/` | Config, DB engine, security, logging, scheduler, EventBus, timezone |
| `exchanges/` | Connectors: binance, bitget (+ SDK), bybit, okx, kucoin, coinbase, forex; `manager.py` |
| `models/` | SQLAlchemy models (`database.py`) + Pydantic schemas |
| `signals/` | Signal generation, technicals, pump/rug detectors, pipeline |
| `sentiment/` | News scrapers, VADER/TextBlob analysis, enhanced service |
| `trading/` | Decision engine, risk, live execution, simulation, `TradingService` |
| `agents/` | Agent base, memory, orchestrator, specialists, custom agents |
| `services/` | Pair catalog, ngrok, Deepgram budget |
| `monitoring/` | Prometheus metrics + alerts |
| `plugins/` | Core plugin contracts + loader |
| `workers/` | Background worker runtime |
| `utils/` | Precision helpers, optional headroom compression |

**Core API prefixes** (all under `/api/v1` via `API_V1_PREFIX`):

`/jarvis`, `/exchanges`, `/signals`, `/sentiment`, `/trading`, `/simulation`, `/strategies`, `/live-trade`, `/monitoring`, `/agents`, `/rug-pulls`, `/pump-monitor`, `/strategy-lab`, `/voice`, `/provider-relay`, `/vision`, `/market`, `/stream`, `/ngrok` — see `backend/app/api/routes.py`.

### 4) Frontend Package Map (`frontend/src/`)

| Directory | Responsibility |
|-----------|----------------|
| `pages/` | ~35 Next.js pages (dashboard, trading, MT5, JARVIS room, plugins UI, settings, …) |
| `components/` | Shared UI (charts, PaulChat, Layout, MT5 panels, 3D robot, …) |
| `hooks/` | Deepgram agent, SSE stream, wallet, Kronos, adaptive quality, … |
| `services/` | `api.ts` client, event stream, notifications |
| `store/` | Zustand `useTradeStore` |
| `utils/` | Pricing, voice commands, device performance, TradingView studies |
| `workers/` | Off-thread JARVIS robot / S.O.X orb rendering |
| `three/` | Three.js robot scene |
| `styles/` | Global CSS / Tailwind |

Path alias: `@/*` → `./src/*` (`frontend/tsconfig.json`).

### 5) Plugins (`plugins/`)

Each plugin is a package with `plugin.json` + `backend/` (and optional `tests/`, `frontend/`, `docs/`).

| Package dir | Slug | Role (from manifest description) |
|-------------|------|----------------------------------|
| `AgentPaulPlugin` | `agent-paul` | PAUL loop (Plan → Qualify → Unify), paper/live modes |
| `AiMarketAnalyst` | `ai-analyst` | Multi-provider LLM analysis + risk policy |
| `KronosForecastPlugin` | `kronos` | K-line / OHLCV ML forecasting |
| `MT5TradingPlugin` | `mt5` | MetaTrader 5 via REST bridge |
| `ObsidianKnowledgePlugin` | `obsidian-knowledge` | Vault sync / knowledge export |
| `OpenHumanPlugin` | `openhuman` | OpenHuman personal AI brain bridge |
| `OpenManusPlugin` | `openmanus` | OpenManus agent sidecar / MCP routing |
| `TelegramSignalNewsPlugin` | `telegram` | Telegram signal + news ingestion / sniper |
| `VibeTradingPlugin` | `vibe-trading` | Vibe-Trading sidecar integration |

Discovery: folders under `plugins/` with valid `plugin.json`; routers imported from `service_provider` (typically `backend.router`) and mounted at `/api/v1`.

### 6) Naming Conventions (directories / files)

- Backend: `snake_case` modules (`live_trade.py`, `pump_monitor.py`)
- Frontend pages: `kebab-case` (`jarvis-room.tsx`, `mt5-live.tsx`)
- Frontend components: `PascalCase` (`PaulChat.tsx`, `Layout.tsx`)
- Plugins: `PascalCasePlugin` directories, kebab `slug` in manifest

### 7) Evidence

- `README.md` project structure section
- Directory trees under `backend/app/`, `frontend/src/`, `plugins/`
- `backend/app/api/routes.py`
- `backend/app/plugins/loader.py`
- `plugins/*/plugin.json`
- `frontend/tsconfig.json`
