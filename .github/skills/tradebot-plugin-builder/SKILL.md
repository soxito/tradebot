---
name: tradebot-plugin-builder
description: "Use when: building new plugin for tradebot, extending MT5 plugin, adding AI Market Analyst plugin, creating standalone plugin modules, plugin migrations, plugin services, plugin routes, plugin UI components, chart overlays, risk metrics, backtesting bridge, copy-trading simulation, multi-account aggregation, OpenAI Responses API integration, GPT-5.2 agent system, limit order placement, trade replay, econcompare macro data. Builds standalone plugins that NEVER modify core backend/app or frontend/src code."
argument-hint: "Describe what plugin or plugin feature to build (e.g., 'MT5 multi-account aggregation', 'AI market analyst with GPT-5.2')"
---

# TradeBot Plugin Builder

Build standalone plugins for the TradeBot platform (FastAPI + Next.js) that extend functionality without modifying core code.

## When to Use

- Creating a new plugin under `plugins/`
- Extending an existing plugin (MT5TradingPlugin, AiMarketAnalyst, etc.)
- Adding plugin migrations, models, services, routes, or UI
- Integrating with TradingView charts (overlays, markers, annotations)
- Building AI agent systems (OpenAI Responses API, tool calling)
- Adding risk metrics, heatmaps, backtesting bridges
- Multi-account aggregation or copy-trading simulation

## Non-Negotiable Rules

1. **EXTEND, DON'T REPLACE** — Never overwrite or delete existing files
2. **CORE IS READ-ONLY** — Never modify files in `backend/app/` or `frontend/src/`
3. **PLUGINS ARE STANDALONE** — Must live under `plugins/{PluginName}/`
4. **SCAN FIRST** — Always analyze project structure before coding
5. **DOCUMENT EVERYTHING** — Update `docs/` and `CHANGELOG.md` for major changes
6. **PERFORMANCE IS CRITICAL** — Charts must not hang; use windowed loading, throttling, Web Workers
7. **SECURITY BY DEFAULT** — Validate all AI outputs server-side; never execute raw model text as code

## Project Architecture (Current State)

```
tradebot/
├── backend/                    # FastAPI Python backend (READ-ONLY for plugins)
│   └── app/
│       ├── main.py             # FastAPI app, CORS, lifespan
│       ├── api/                # 13 API routers, 100+ endpoints
│       ├── agents/             # AI agent system (OpenAI, orchestrator, specialists)
│       ├── core/               # config, database, security, scheduler
│       ├── exchanges/          # 6 exchange connectors via ccxt + Bitget native SDK
│       ├── models/             # 26 SQLAlchemy models, Pydantic schemas
│       ├── sentiment/          # VADER + TextBlob, multi-source aggregation
│       ├── signals/            # Signal pipeline, pump/rug-pull detectors
│       └── trading/            # Decision engine, live trading, risk, simulation
├── frontend/                   # Next.js + React + TypeScript (READ-ONLY for plugins)
│   └── src/
│       ├── pages/              # 17+ pages (dashboard, agents, futures, etc.)
│       ├── components/         # TradingViewChart, WalletBalance, SignalFeed, etc.
│       ├── hooks/              # useConnectionTest, useWalletBalance, useZarRate
│       ├── services/api.ts     # Axios client, 50+ endpoint wrappers
│       └── store/              # Zustand (selectedSymbol, exchange, timeframe, tradingMode)
├── plugins/                    # STANDALONE PLUGINS (this is where you build)
│   ├── MT5TradingPlugin/       # MT5 REST integration via mtapi-io
│   └── AiMarketAnalyst/        # AI analysis + smart limit orders
└── docs/                       # Documentation (append-only)
```

### Key Integration Points (Read-Only — Connect Via)

| Core System | Location | Connect Via |
|-------------|----------|-------------|
| OpenAI client | `backend/app/agents/base.py` | Import `AsyncOpenAI` pattern; plugins create own client |
| Agent orchestrator | `backend/app/agents/orchestrator.py` | 4-phase pipeline pattern (Analyst→Signal→Risk→Executor) |
| Exchange connectors | `backend/app/exchanges/manager.py` | `ExchangeManager` singleton; plugin calls existing methods |
| Signal pipeline | `backend/app/signals/pipeline.py` | Emit signals in same schema; plugin registers own generator |
| Risk calculator | `backend/app/trading/risk.py` | `RiskCalculator` pattern; plugin implements own policy engine |
| TradingView chart | `frontend/src/components/TradingViewChart.tsx` | Overlay API via markers/lines; plugin adds overlay data endpoints |
| Zustand store | `frontend/src/store/useTradeStore.ts` | Plugin creates own store slice or separate store |
| Database | `backend/app/core/database.py` | Async SQLAlchemy; plugin adds own models + migrations |
| Scheduler | `backend/app/core/scheduler.py` | Plugin registers own background loops |
| Config | `backend/app/core/config.py` | Plugin reads own env vars; never modifies core Settings |

### Existing Technology Stack

- **Backend:** Python 3.12, FastAPI 0.115, SQLAlchemy 2.0 (async), asyncpg, Redis, OpenAI SDK 1.82
- **Frontend:** React 19, Next.js 16, TypeScript 5.8, Lightweight Charts 4.1, Zustand, Tailwind CSS 3.4
- **Infrastructure:** Docker (postgres:16, redis:7), Prometheus, Loguru
- **AI:** OpenAI gpt-4o-mini / o3 models with circuit breaker pattern

## Plugin Structure Template

Every plugin MUST follow this structure:

```
plugins/{PluginName}/
├── plugin.json               # Manifest: name, version, providers, routes, permissions
├── backend/
│   ├── __init__.py
│   ├── router.py             # FastAPI APIRouter (mounted by plugin loader)
│   ├── models.py             # SQLAlchemy models (plugin-prefixed tables)
│   ├── schemas.py            # Pydantic request/response schemas
│   ├── services/             # Business logic services
│   ├── migrations/           # Alembic migrations (plugin-scoped)
│   └── config.py             # Plugin settings (from env or plugin_settings table)
├── frontend/
│   ├── pages/                # Next.js pages (lazy-loaded)
│   ├── components/           # React components
│   ├── hooks/                # Custom hooks
│   └── store/                # Zustand store slices
├── docs/
│   ├── README.md
│   ├── architecture.md
│   └── changelog.md
└── tests/
    └── test_*.py
```

### plugin.json Schema

```json
{
  "name": "Plugin Display Name",
  "slug": "plugin-slug",
  "version": "1.0.0",
  "description": "What this plugin does",
  "author": "Author Name",
  "requires": { "python": ">=3.12", "tradebot": ">=1.0" },
  "service_provider": "backend.router",
  "provides": {
    "routes": ["backend/router.py"],
    "models": ["backend/models.py"],
    "migrations": ["backend/migrations/"],
    "pages": ["frontend/pages/"],
    "overlays": true,
    "scheduled_jobs": []
  },
  "permissions": [],
  "settings_keys": []
}
```

## Procedure

### Phase 0 — SCAN (Always Do First)

1. **List existing plugins** — `ls plugins/` to see what's already there
2. **Read plugin manifests** — Check each `plugin.json` for capabilities, routes, models
3. **Check for conflicts** — Verify table names, route prefixes, and port bindings don't collide
4. **Inspect core integration points** — Read the specific core files the plugin needs to connect with
5. **Read existing plugin docs** — Check `plugins/{Name}/docs/` and `docs/PLUGINS/`
6. **Output scan report:**
   - Discovered plugins and their state
   - Core hooks available for connection
   - Planned files to create (no coding yet)
   - Potential conflicts or risks

### Phase 1 — Database Schema

1. Create migrations under `plugins/{PluginName}/backend/migrations/`
2. Prefix ALL table names with plugin slug (e.g., `mt5_`, `ai_`)
3. Include proper indexes for time-series queries
4. Add `created_at`, `updated_at` timestamps
5. Use enums for status fields
6. Reference core tables by ID only (no foreign key constraints across boundaries)

### Phase 2 — Services & Business Logic

1. Create services under `plugins/{PluginName}/backend/services/`
2. Each service has a single responsibility
3. Use dependency injection (FastAPI `Depends`)
4. Implement circuit breaker for external API calls
5. Cache expensive computations (Redis, short TTL)
6. All external HTTP calls go through async jobs, NEVER in request handlers
7. Log all significant operations with structured logging

### Phase 3 — API Routes

1. Create router under `plugins/{PluginName}/backend/router.py`
2. Use versioned prefix: `/api/v1/plugins/{slug}/`
3. Return consistent JSON shapes with status codes
4. Apply rate limiting on expensive endpoints
5. Validate all inputs with Pydantic schemas
6. Document endpoints in plugin docs

### Phase 4 — Frontend UI

1. Create pages under `plugins/{PluginName}/frontend/pages/`
2. Follow existing design: dark-first, compact trading terminal aesthetic
3. Use Lightweight Charts for any chart rendering
4. Implement skeleton loaders for async data
5. Use Web Workers for heavy computation (indicator math, heatmaps)
6. Throttle live updates (max 1 update/sec for non-critical data)
7. Server-paginate all tables
8. Chart overlays must be delta-based (update only changes, never full redraw)

### Phase 5 — Chart Overlays (If Applicable)

1. Provide overlay data via API endpoint
2. Format must match TradingView Lightweight Charts markers/lines schema:
   ```json
   { "time": "2024-01-01", "position": "aboveBar", "color": "#2196F3", "shape": "circle", "text": "BUY" }
   ```
3. Overlays are separate layers — never re-render indicator series
4. Cap visible markers to last 200 with "load more" on demand
5. Live overlays update only deltas (new/modified/removed)

### Phase 6 — Testing & Documentation

1. Write tests for critical services (risk policy, order placement, data transforms)
2. Mock external APIs (MT5, OpenAI, exchanges)
3. Update plugin docs: README, architecture, security, changelog
4. Append to `docs/BUILD_LOG.md` and `docs/FIX_LOG.md` if they exist

## OpenAI Integration Pattern (For AI Plugins)

When building AI-powered plugins:

```python
# Use Responses API (NOT Chat Completions) for GPT-5.2+
# Support previous_response_id for multi-turn
# Configure reasoning.effort and text.verbosity
# Implement function calling with server-side tool allowlist
# Validate ALL model outputs against a strict JSON schema
# Never execute raw model output as code
```

Key rules:
- **Tool allowlist enforced server-side** — Model requests tools; server validates against agent's `tools_allowlist_json`
- **Structured output only** — Model must return JSON matching a schema; reject anything else
- **Risk policy runs BEFORE any trade action** — Even if model says "place order", policy engine gates it
- **Audit everything** — Every AI decision stored with agent version, input snapshot, output, and outcome

## Performance Checklist

Before completing any plugin feature, verify:

- [ ] Charts load in <1s (windowed: last 200 points)
- [ ] No `setData()` on every tick — use `update()` for live data
- [ ] Indicator math runs in Web Worker (not main thread)
- [ ] Tables are server-paginated (not client-side filtering of 10k+ rows)
- [ ] External API calls are in async jobs (never in request handlers)
- [ ] Redis caching on expensive queries (30-120s TTL)
- [ ] Overlay updates are delta-based
- [ ] Debounce parameter changes (300ms minimum)

## Security Checklist

- [ ] All AI model outputs validated against schema before use
- [ ] Trade actions require server-side policy check (lot size, SL/TP, max risk)
- [ ] API keys never logged (redact in request/response payloads)
- [ ] Plugin routes behind authentication middleware
- [ ] Rate limiting on AI analysis endpoints
- [ ] Paper mode is default; live trading requires explicit opt-in + permission
