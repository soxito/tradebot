# 🤖 TradeBot

**AI-powered multi-asset trading platform.** Combines news-driven sentiment analysis, TradingView technical signals, and a multi-agent AI orchestrator to generate and (optionally) execute trades across crypto exchanges and MetaTrader 5 (forex, metals, indices) — with a voice-controlled JARVIS/Paul assistant, a plugin ecosystem for ML forecasting and channel-based signal ingestion, and an Obsidian-backed "knowledge brain".

> [!TIP]
> ### 💝 Love TradeBot? Keep it alive!
> This project is built and maintained by one developer. If TradeBot helps you trade smarter, **please consider a donation** — it directly funds servers, API costs, and new features.
>
> **USDT (BEP-20):** `0xB87C2384988ab0Afb47f893f785aa636bD30FCE6` · **BTC:** `18TDkAgJaJZwXyuZs9hJ5FhyknwDRxk9iU`
>
> 👉 Full details in the [Donate section](#-donate--support-the-project) below. Every contribution counts! 🙏

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-complete-ui-guide--every-menu-tab">UI Guide</a> •
  <a href="#-features">Features</a> •
  <a href="#%EF%B8%8F-desktop-app">Desktop App</a> •
  <a href="#-configuration">Configuration</a> •
  <a href="#%EF%B8%8F-troubleshooting">Troubleshooting</a> •
  <a href="#-donate--support-the-project">💝 Donate</a>
</p>

---

## 💝 Donate — Support the Project

TradeBot is developed and maintained by a single developer in spare time. If this project saves you time, makes you money, or you simply believe in where it's heading — **your donation directly funds development**: server costs, exchange/API fees for testing, LLM credits, new features, bug fixes, and faster releases.

Every contribution, big or small, keeps the bots running and the roadmap moving. Thank you! 🙏

### USDT (BEP-20 / BNB Smart Chain)

```
0xB87C2384988ab0Afb47f893f785aa636bD30FCE6
```

Network: **BNB Smart Chain (BEP-20)** — send USDT (BSC) only. Other BEP-20 tokens also work at this address.

### Bitcoin (BTC)

```
18TDkAgJaJZwXyuZs9hJ5FhyknwDRxk9iU
```

Network: **Bitcoin mainnet**

> ⚠️ Double-check the address against the official repository before sending. Crypto transactions are irreversible.
>
> 💬 Donated? Open an issue titled "Donation" if you'd like your name on the supporters list!

---

## 🏗️ Architecture

- **Backend:** Python 3.13, FastAPI, `ccxt` + native Bitget v2 SDK, SQLAlchemy (async) + asyncpg, Redis
- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind, TradingView Lightweight Charts, Three.js, react-force-graph-3d/2d
- **Exchanges:** Binance, Bitget (spot + futures), Bybit, OKX, KuCoin, Coinbase — plus MetaTrader 5 (forex/metals/indices) via the `MT5TradingPlugin`
- **AI agents:** multi-agent orchestrator (Market+Sentiment → Signal → Risk → Executor pipeline) with per-agent decision memory to skip redundant LLM calls, backed by OpenAI and a multi-provider LLM router (`AiMarketAnalyst`)
- **Sentiment & news:** CryptoPanic, RSS feeds, Reddit, Telegram and WhatsApp channels → VADER + TextBlob scoring
- **Realtime:** Server-Sent Events over a Redis pub/sub `EventBus` (in-memory fallback)
- **Assistant:** JARVIS / Paul / S.O.X — global voice + chat widget, 3D robot avatar, WebGL command room, and live face-vision
- **Background workers:** a central scheduler drives sentiment, signal generation, sim/live auto-trade loops, position monitoring, the crypto sniper (pump/rug detection), pair-catalog sync, research, and vault sync — all independently toggleable
- **Infrastructure:** PostgreSQL, Redis (Homebrew or Docker), Docker Compose; optional Electron desktop packaging

---

## 📖 Complete UI Guide — Every Menu Tab

The sidebar (`frontend/src/components/Layout.tsx`) contains every page below. A floating **PAUL/JARVIS assistant** is available on all pages via voice or chat, plus a collapsible **Voice Extension** button at the bottom of the sidebar that opens the browser-extension install guide.

### 📊 Market & Dashboard

| Tab | URL | What it does |
| --- | --- | --- |
| **Dashboard** | `/` | Home overview: API status, active exchanges, auto-trading DRY-RUN state, wallet balances, price chart (TradingView widget ↔ custom lightweight chart toggle), live signal feed and recent trade history. |
| **Trading** | `/trading` | The main manual + automated trading console: symbol/exchange/timeframe selector, chart with strategy & Pine-script overlays, order form (buy/sell, market/limit, spot/futures, leverage, margin mode, auto SL/TP), SIM account panel (positions/orders/closed, add funds, reset, auto-trade loop), and LIVE Bitget futures panel (open orders, inline SL/TP editing, close-all, AI order optimization). Prominent SIM vs LIVE badge. |
| **Futures** | `/futures` | Bitget futures management view — positions, leverage and margin settings (wraps the `BitgetFutures` component). |
| **Trade History** | `/history` | All executed, pending and failed trades across engines in one table. |
| **Market Cap** | `/market-cap` | TOTAL3 macro monitor: market cap/volume/change cards, BTC & ETH dominance, Fear & Greed index, bull/bear signal banner, dollar/macro context gauges, TOTAL/TOTAL2/TOTAL3 TradingView chart and a 30-day volume-divergence regime panel. Refreshes every 60 s. |
| **Trending** | `/trending` | CoinGecko trending tokens board with sparklines, Top Gainers/Losers tables, and auto-monitoring badges — trending pairs auto-sync into the signal pipeline every ~3 minutes. |
| **Bitcoin Cycle** | `/bitcoin-cycle` | The 1064-day Bitcoin cycle dashboard: live bull/bear phase, day-of-cycle, projected top/bottom countdowns, cycle-aligned comparison chart of past cycles, base-rate expectation table, month calendar grid, whale-watch panel (holder flows, large transfers), and validation of projected vs actual tops/bottoms. |

### 📡 Signals & Detection

| Tab | URL | What it does |
| --- | --- | --- |
| **Signals** | `/signals` | Autonomous batch signal generator: pick monitored Bitget pairs (searchable, SPOT/FUT badges), choose timeframe and execution venue, then run technical analysis. Expandable result cards show action/confidence/score bars/indicator badges (RSI, MACD, Bollinger, volume, sentiment) with per-card Execute buttons and confidence gating. Also shows the TradingView webhook URL. |
| **Telegram Signals** | `/telegram-signals` | Telegram channel ingestion hub with 9 tabs: Active Signals (live-price tracking, TP-hit highlighting, sandbox/live execution), Forex Signals, Trailing SL, Sniper Auto-Trade, Execute Signals, Volume Monitor, Connect AI, Strategy Scan and Raw Messages. Auto-polls every 60 s. |
| **Smart Money Concepts** | `/smart-money-concepts` | SMC workbench: pair/timeframe/exchange config, entry-strategy selector (best-limit → aggressive/market/custom SL/TP), Generate Active Pair / Generate All, latest-signal panel with entry candidates, order-flow & BTC-news confirmation badges, AI reasoning, plus SMC signals & sniper position tables. |
| **Sniper Signals** | `/sniper-signals` | Review feed of sniper-generated setups: entry decisions, risk analysis, rug-token context, AI agent reasoning, linked trade results and volume-gated Kronos forecasts. Tabs for Signals / Trades / Positions; "Generate New Signals" runs one sniper cycle. |
| **Pump Monitor** | `/pump-monitor` | Deep pump detection using 8 indicators (volume spike, price acceleration, social, order flow, momentum, BTC-relative, volatility, ATH breakout). Start/Stop auto-scanner, status pipeline cards (Watchlist → Detected → Confirmed → Signalled → Traded → Pumped/Faded) and expandable token score breakdowns. |
| **Rug Pulled** | `/rug-pulled` | Rug-pull/dump tracker: lists tokens that pumped ≥30 % with buying-power-decline analysis, AI short-entry recommendations (entry/SL/TP with Fibonacci levels), sniper loop controls and Survived/Dumped marking. |
| **Delistings** | `/delistings` | Exchange delisting warnings so you can exit affected pairs before removal dates. |
| **Kronos Forecast** | `/kronos-forecast` | Kronos foundation-model candle forecasting studio: crypto + forex/metals pair search, horizon/samples/temperature sliders, hot-swappable model chips, predicted path with p10–p90 confidence bands, forecast signal panel with NO_TRADE gating, JARVIS analysis, and executable Sniper Entries (paper/live). |
| **Sentiment** | `/sentiment` | Sentiment dashboard: news/social/on-chain sentiment feeds plus an AI sentiment-agents status strip. |

### 🤖 AI Agents & Intelligence

| Tab | URL | What it does |
| --- | --- | --- |
| **AI Agents** | `/agents` | Hub for the multi-agent pipeline (Market Analyst, Signal Generator, Risk Manager, Sentiment Analyst, Trade Executor, Position Reviewer): enable toggles, win-rate stats, expandable system prompts, create/edit modal, Run Agent Analysis panel, Position Monitor loop (HOLD/CLOSE/ADVERTISE reviews) and Recent Decisions with outcome rating. |
| **AI Providers** | `/ai-providers` | Manage which LLM brains agents use: per-key usage bars vs free-tier caps, model picker, test button, task-assignment panel ("one key per job": charts, fast turns, deep reasoning, JARVIS chat) and routing strategy (Priority / Round robin / Least used). |
| **Custom Agents** | `/custom-agents` | Rule-based fallback engine that validates trades when LLMs are unavailable: per-role learning stats (decisions, win rate, PnL, accuracy), Test Custom Pipeline runner and explainer. |
| **Agent Paul** | `/agent-paul` | Control room for the PAUL loop (Plan → Apply & Qualify → Unify) autonomous agent: approval queue, kill switch, Paper vs Bot-executes vs PAUL-executes modes, risk-policy gate, Decision Console, audit history, JARVIS Skills editor (keyword-triggered prompts) and Hooks editor (event triggers like `on_signal`). |
| **Intelligence** | `/intelligence` | Living 2D/3D brain map: force-directed knowledge/code graph inside an animated cyber-brain silhouette, coloured by community, with Live Brain Feed, top symbols, node-linked vault notes, search/filters/layout persistence and a Brain-AI provider modal. |
| **Vault** | `/vault` | Obsidian Knowledge Vault browser: sync status, note-type stats, full-text search, filterable note list (signal/decision/strategy/community/trade), markdown preview with "Open in Obsidian" links and a live JARVIS activity strip. |
| **Insights** | `/insights` | Insights & data: Overview (news stats, sentiment grid, learning stats, vault snapshot capture), News & RSS filter feed, AI Decisions log, per-symbol Sentiment breakdowns and AI Learning performance tables. |
| **Research & Calendar** | `/research` | Background research board: findings cards (calendar/news/sentiment/prediction), Fear & Greed gauge, high-impact economic-calendar month grid with actual/forecast values and countdowns, per-pair Signal Research job board, plus research-loop start/stop controls. |
| **AI Analyst** | `/ai-analysis` | On-demand AI market analyst: enter symbol + timeframe → Analyze (direction, confidence, entry/SL/TP, rationale, invalidation, weighted signal chips) or Propose Limit Order with one-click placement. Shows a PAPER MODE badge when active. |
| **AI Profiles** | `/ai-agents-admin` | Admin CRUD for AI Analyst agent profiles: name, slug, role, model, reasoning effort, system prompt, instruments/timeframes/indicators, version badges and enable/disable toggles. |

### 🏦 MT5 (Forex/Metals)

| Tab | URL | What it does |
| --- | --- | --- |
| **MT5 Live** | `/mt5-live` | Full MetaTrader account management: account tabs with LIVE/DEMO/PROP badges, broker/server picker with connection tester, equity/balance/P&L metric cards, open positions (close / close-all), pending orders, synced deal history, manual order ticket, real-time candlestick chart with quick buy/sell, and an Auto-Manage loop that updates TP/SL from SMC+AI signals. |
| **MT5 Replay** | `/mt5-replay` | Trade-replay backtester: launch replays over historical MT5 trades (date range, symbol filter) and review metrics — total trades, net P&L, win rate, max drawdown, Sharpe ratio. |
| **MT5 Copy Sim** | `/mt5-copy-sim` | Copy-trading simulator: create profiles against a source MT5 account (fixed-lot / risk-% / multiplier allocation, max positions) and browse simulated copied trades with P&L. |

### 👥 Trading Room (Multi-Agent Boardroom)

| Tab | URL | What it does |
| --- | --- | --- |
| **Trading Room** | `/trading-room` | 3D agent boardroom (Three.js): each AI agent has a seat/avatar wired into the orchestrator SSE pipeline, JARVIS chairs meetings as CEO, live debate speech bubbles, consensus verdict tallies, back-wall OHLCV chart and wall screens with live prices/news/calendar. Voice announcements toggle and worker start/stop. |
| **Room Settings** | `/trading-room-settings` | Roster + execution policy editor: danger-gated "let agents execute trades" and dry-run toggles, venue selection (Simulation / Bitget / MT5 accounts), risk limits, cadence, Bitcoin 1064-day-cycle risk multiplier card, and editable agent roster (rename, recolor, role/model/pairs/system prompt) with a recommended-models preset button. |

### 🧪 Labs, Tools & System

| Tab | URL | What it does |
| --- | --- | --- |
| **Strategies** | `/strategies` | Two-tab strategy manager: Signal Bot Config (8 indicators incl. RSI/MACD/Bollinger/StochRSI/ADX/Auto-Fib, thresholds, SL/TP, AI-generate & AI-improve actions, AI chart analysis) and Pine Scripts (create/edit/delete Pine code with TradingView usage instructions). |
| **Binary Engine** | `/binary-engine` | JARVIS voice-recognition studio: real-time 16-band FFT spectrum, speaker identification with 3-second voice calibration, Voice Brain Vault sync, codec/plugin catalogue (VAD, noise suppression, Deepgram Nova) and Deepgram credit balance. |
| **JARVIS Room** | `/jarvis-room` | S.O.X Command Room: particle-orb robot reacting to listening/thinking/talking states, draggable panels (market widgets, face vision, goals/todos kanban, unified accounts/equity monitor with active positions), voice/chat commands via PAUL. |
| **Vibe Trading** | `/vibe-trading` | Natural-language quant lab (external sidecar service): Research prompts, NL backtests with Pine Script export, multi-agent swarm presets, 460-alpha "Alpha Zoo" browser and recent-run history. |
| **OpenHuman** | `/openhuman-hub` | Personal-AI-brain hub (Early Beta) with 11 tabs: Brain memory tree, Tiny Place social world, Subconscious loops, deep Research console, joint-mode Agents, Integrations/MCP, Workflows, Kronos forecasts, SMC analysis, live Signals and privacy Settings. |
| **Ngrok** | `/ngrok` | Public tunnel manager: per-tunnel state cards with copy/open URLs, Google OAuth enforced badge, start/stop/restart and config panel (authtoken, addresses, auto-start). |
| **System Monitor** | `/system-monitor` | Host health: CPU/RAM/swap bars, backend memory, event-loop lag (p50/p95/max), power presets (Battery saver / Balanced / Full power) and background-task tables with pause/resume/stop controls. Polls every 3 s. |
| **Settings** | `/settings` | Global settings with parallel LIVE and SIM tabs: master switches (Live Trading, Auto-Trade, Dry Run, AI Agents), orchestrator mode (Built-in vs TradingAgents with LLM/debate config), timeframe, min-confidence, leverage/margin, risk limits, auto-trade pair selector with delisting warnings and Pine-strategy weighting. Includes Swagger/health/API quick links. |
| **WhatsApp** (under Settings) | `/whatsapp` | WhatsApp signal bot dashboard: QR session connect, stats overview (channels/signals/sniper trades), gateway & webhook settings, channel manager, captured signals feed and sniper auto-trade config. |

> 🔌 The sidebar's bottom **Voice Extension** button opens the install guide for the JARVIS browser extension (desktop alerts + reliable mic capture outside the page).

---

## 📋 Features

### Core platform
- ✅ Multi-exchange connectors (Binance, Bitget, Bybit, OKX, KuCoin, Coinbase) with a native Bitget v2 SDK for spot + futures, plus dedicated spot-price providers for forex/metals
- ✅ TradingView webhook receiver with signature validation
- ✅ News + sentiment pipeline (RSS + CryptoPanic → VADER + TextBlob), plus Telegram/WhatsApp channel ingestion with dedupe and structured signal extraction
- ✅ Multi-agent AI orchestrator: Market+Sentiment → Signal → Risk → Executor pipeline with persisted decision history and per-agent learning stats
- ✅ Signal & strategy engine: technical signals, Pine Script strategies, Smart Money Concepts (order blocks, FVGs, premium/discount zones), Strategy Lab
- ✅ Risk management (position sizing, SL/TP, exposure limits) across simulated (paper) and live engines
- ✅ Crypto sniper: rug-pull and pump/pullback detection with optional auto-trade loop
- ✅ Realtime dashboard with SSE stream, charts, balances and trade history
- ✅ Dedicated spot metals provider (XAUUSD/XAGUSD/XPTUSD/XPDUSD): Swissquote + gold-api cascade
- ✅ Yahoo Finance OHLCV universal fallback for FX/indices/commodities
- ✅ Universal market-data service resolving any symbol to a live price
- ✅ Macro context engine: DXY + VIX regime classification feeding every trade proposal
- ✅ Analysis journal: self-calibrating accuracy track-record of every AI proposal
- ✅ Agent-Reach research integration (Jina Reader, Exa, yt-dlp, GitHub CLI)
- ✅ Ngrok tunnel management with enforced Google OAuth

### Plugins (standalone — never modify core code, see `plugins/`)
| Plugin | Description |
| --- | --- |
| `MT5TradingPlugin` | MetaTrader 5 REST bridge: multi-account management, SMC sniper charting, autonomous scalp bot (multi-timeframe SMC + Kronos ML + AI gate, SMC-guided recovery legs, hot-config updates), copy-trading simulation and backtesting bridge |
| `AiMarketAnalyst` | Multi-provider LLM router (Groq, OpenRouter, Gemini, Mistral, Cerebras, DeepSeek, Together, OpenAI, custom) with failover, agent profiles, smart limit-order proposals and a risk policy engine |
| `KronosForecastPlugin` | Kronos ML candle forecasting (mini/small/base) with heuristic fallback, rendered as chart overlays |
| `TelegramSignalNewsPlugin` | Telegram channel ingestion for trading signals and news, with sniper auto-trade |
| `WhatsAppSignalNewsPlugin` | WhatsApp channel ingestion (via an OpenWA gateway) for trading signals and news, with sniper auto-trade |
| `ObsidianKnowledgePlugin` | Syncs signals, agent decisions, strategy notes and code-community graphs into a local Obsidian vault |
| `AgentPaulPlugin` | PAUL-loop (Plan → Apply/Qualify → Unify) background agent with approval queue and paper/execute/direct trading modes |
| `OpenManusPlugin` | Routes AI calls through an OpenManus MCP sidecar with phased fallback, with compliance audit logging |
| `OpenHumanPlugin` | Exposes TradeBot as an MCP tool server to a local-first OpenHuman brain with persistent memory |
| `VibeTradingPlugin` | Natural-language backtesting sidecar with a 460-alpha Alpha Zoo and multi-agent swarm research |

---

## 🖥️ Desktop app (macOS / Windows / Linux)

If you just want to *use* TradeBot without installing anything else, download the installer from the [Releases page](https://github.com/soxito/tradebot/releases). It needs no Python, Node, PostgreSQL, Redis or Docker — everything is bundled, and your data lives in a per-user folder that survives updates.

See **[docs/DESKTOP.md](docs/DESKTOP.md)** for install notes (builds are not code-signed yet — first launch needs one extra click).

The rest of this README covers running from source.

## 🚀 Quick Start

The recommended way to run TradeBot from source is the cross-platform launcher **`start.py`**. On first run it will:

1. Check/install PostgreSQL and Redis (via Homebrew on macOS, or Docker anywhere)
2. Create the Python virtual environment and install backend dependencies
3. Install frontend dependencies and start both services
4. Auto-detect your hardware and tune resource usage accordingly

### Prerequisites

| Tool | Version | Install |
| --- | --- | --- |
| Python | 3.11+ ([python.org](https://www.python.org/downloads/) or `brew install python`) | Windows: check *"Add to PATH"* during install |
| Node.js | 18+ ([nodejs.org](https://nodejs.org) or `brew install node`) | Use LTS release |
| Git | any recent | [git-scm.com](https://git-scm.com) |
| PostgreSQL & Redis | any recent | **Auto-provisioned** by `start.py` via Homebrew/Docker |

<details>
<summary><strong>Platform-specific notes</strong></summary>

- **macOS:** if you don't have Homebrew yet: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- **Windows:** use `start.bat`. Run terminals as Administrator only if required.
- **Linux (Debian/Ubuntu):** `sudo apt update && sudo apt install -y python3 python3-pip python3-venv nodejs npm git postgresql redis-server`
</details>

### 1. Get the code and configure

```bash
git clone https://github.com/soxito/tradebot.git
cd tradebot
cp .env.example .env   # then edit .env with your own API keys
```

> ⚠️ `.env` holds your secrets and is **git-ignored** — never commit it.
>
> 💡 **Minimum viable setup:** TradeBot runs fine with **no API keys at all** — market data comes from public endpoints and trading defaults to paper mode. Add exchange keys only when ready to trade live, and add LLM keys (`OPENAI_API_KEY` etc.) when you want AI analysis.

### 2. Start everything

**macOS / Linux**
```bash
python start.py         # or: ./run-local.sh
```

**Windows**
```bat
start.bat
```

First launch takes several minutes (dependency installs + builds). Subsequent launches are much faster.

`start.py` accepts `--status` (show running services) and `--stop` (stop them). Useful overrides:

```bash
NEXT_PUBLIC_DISABLE_3D=1   # hard-disable 3D/WebGL rendering (weak GPUs / VMs)
TRADEBOT_RELOAD=1          # force backend hot-reload on low-core CPUs
```

### 3. Verify it's running

1. **Health check:** http://localhost:1448/health → JSON payload
2. **API docs:** http://localhost:1448/docs → Swagger UI
3. **Dashboard:** http://localhost:3000 → green connection banner = good

### 4. Or run with Docker instead

```bash
docker-compose up --build        # foreground
docker-compose up --build -d     # background
docker-compose down              # stop
```

### Access points

| Service | Local (`start.py`) | Docker |
| --- | --- | --- |
| Frontend dashboard | http://localhost:3000 | http://localhost:3001 |
| Backend API docs | http://localhost:1448/docs | http://localhost:8080/docs |
| Health check | http://localhost:1448/health | http://localhost:8080/health |
| PostgreSQL | localhost:5434 | localhost:5433 |
| Redis | localhost:6379 | localhost:6380 |

> Default DB credentials: user `tradebot`, database `tradebot`.

### First-run checklist

1. ✅ Confirm the connection banner is green
2. ✅ Visit `/intelligence` to watch the AI orchestrator come online
3. ✅ Leave `ENABLE_AUTO_TRADING=false` until you've observed signals for days
4. ✅ Configure exchange keys in `.env`, restart (`python start.py --stop && python start.py`)
5. ✅ Optionally set up the [Obsidian knowledge vault](#-obsidian-knowledge-base)

> 💝 If everything works smoothly, consider [donating to keep development going](#-donate--support-the-project)!

## 🔄 Updating an Existing Installation

```bash
cd tradebot
git pull origin main

# if local edits conflict:
git stash && git pull origin main && git stash pop

# rebuild
docker-compose up --build    # Docker
./run-local.sh               # or locally (macOS/Linux) / start.bat (Windows)
```

> Your `.env` is git-ignored — `git pull` never touches your keys. If `.env.example` gained new variables, copy them over.

---

## 🔒 Security

### ⚠️ CRITICAL — Never commit secrets!

- ❌ **NEVER** hardcode API keys, passwords, or secrets in code
- ❌ **NEVER** commit `.env` files or paste real keys into `.env.example`
- ✅ **ALWAYS** use environment variables and testnet/sandbox keys for development
- ✅ **ALWAYS** enable IP whitelisting and disable withdrawal permissions on exchange keys

### Default Safety Settings

Auto-trading is **DISABLED** by default (`ENABLE_AUTO_TRADING=false` in `.env`).

Risk limits: max position $1,000 · max risk per trade 2 % · max total exposure $10,000.

---

## ⚙️ Configuration Reference (`.env`)

Copy `.env.example` → `.env` and fill in what you need:

```env
# Database & core
DATABASE_URL=postgresql+asyncpg://tradebot:tradebot_password@postgres:5432/tradebot
REDIS_URL=redis://redis:6379/0
CORS_ORIGINS=http://localhost:3000,http://localhost:8080
SECRET_KEY=change-me-in-production-to-a-secure-random-string

# Exchange API keys (all optional)
BINANCE_API_KEY=
BINANCE_API_SECRET=
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_PASSPHRASE=
BYBIT_API_KEY=
BYBIT_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=

# AI / LLM providers (optional — enables AI analysis)
OPENAI_API_KEY=
NVIDIA_API_KEY=            # free frontier models from build.nvidia.com ("nvapi-..." key)
CRYPTOPANIC_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=

# Alerts (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=
EMAIL_ALERTS_ENABLED=false
```

Full variable reference: [`backend/app/core/config.py`](backend/app/core/config.py) and comments in [`.env.example`](.env.example).

---

## 🧠 Obsidian Knowledge Base

TradeBot ships an `ObsidianKnowledgePlugin` that syncs signals, agent decisions, strategy notes and code-community graphs into a local Obsidian vault.

### Setup

1. Install Obsidian from **https://obsidian.md**, then **"Open folder as vault"** → `<tradebot-root>/obsidian-vault/tradebot`
2. Install community plugins (**Settings → Community plugins → Browse**):

| Plugin | Purpose |
| --- | --- |
| **Dataview** | Query notes like a database |
| **Templater** | Auto-fill daily-journal and trade templates |
| **Local REST API** | Lets TradeBot push/pull notes |
| **Obsidian Git** | Auto-commit vault changes |

3. Enable **Local REST API**, copy its token into `.env`:

```env
OBSIDIAN_VAULT_PATH=~/obsidian-vault/tradebot
OBSIDIAN_REST_URL=https://localhost:27124
OBSIDIAN_REST_TOKEN=<paste token here>
OBSIDIAN_AUTO_SYNC_MINUTES=15
OBSIDIAN_EXPORT_DECISIONS=true
OBSIDIAN_EXPORT_SIGNALS=true
OBSIDIAN_INJECT_CONTEXT=false   # true enriches agent prompts with vault notes
```

4. Trigger first sync from the UI (**Intelligence → Vault**) or:

```bash
curl -X POST http://localhost:1448/api/v1/plugins/obsidian-knowledge/sync
```

### Vault structure

```
obsidian-vault/tradebot/
├─ _index.md           ← Dataview dashboard
├─ _daily/             ← Auto daily journals
├─ signals/            ← One note per signal
├─ decisions/          ← One note per agent decision
├─ strategies/         ← Strategy references
├─ communities/        ← Code communities (~176 notes)
└─ trades/             ← Closed trade outcomes
```

Press **Cmd/Ctrl+G** in Obsidian for Graph View; use the `/vault` page in-app for search and sync.

> More detail: [`obsidian-vault/SETUP.md`](obsidian-vault/SETUP.md).

---

## 🛠️ Development

```bash
# Backend manually
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload --port 1448

# Frontend manually
cd frontend && npm install && npm run dev

# Tests
cd backend && pytest
cd frontend && npm test

# Logs
docker-compose logs -f backend
tail -f logs/tradebot.log

# DB shells
docker exec -it tradebot-postgres psql -U tradebot -d tradebot
docker exec -it tradebot-redis redis-cli
```

## 📊 API Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /health` · `GET /api/v1/status` | Health & module status |
| `POST /api/v1/webhooks/tradingview` | TradingView alerts (signature-validated) |
| `GET /api/v1/signals` · `POST /api/v1/signals/generate` | Signal feed & generation |
| `GET /api/v1/sentiment/{symbol}` | Asset sentiment score |
| `GET /api/v1/trades` · `POST /api/v1/orders` | History & manual orders |
| `GET /api/v1/exchanges/{exchange}/...` | Balances, tickers, OHLCV per exchange |
| `GET /api/v1/simulation/*` | Paper trading & sim auto-trade loop |
| `GET /api/v1/live-trade/*` | Live settings, SL/TP, auto-trade loop |
| `GET /api/v1/agents/*` | Agent CRUD, analyze, decisions, room settings |
| `GET /api/v1/rug-pulls/*` · `/pump-monitor/*` | Sniper detection/auto-trade |
| `GET /api/v1/research/*` · `/strategy-lab/*` | Research findings & strategy runs |
| `WS /api/v1/vision/face-stream` | Face-vision WebSocket |
| `GET /api/v1/stream/events` | Realtime SSE stream (signals, trades, ticks) |

> Full live endpoint set at `/docs`, including plugin routes.

## 🛠️ Troubleshooting

<details>
<summary><strong>Port already in use?</strong></summary>

Stop previous instances with `python start.py --stop`, or check `lsof -i :3000`. Docker mode uses ports 3001/8080 instead.
</details>

<details>
<summary><strong>CORS errors?</strong></summary>

```bash
grep CORS_ORIGINS .env                                   # verify origins
docker-compose up -d --force-recreate --no-deps backend  # recreate after .env change
```
See [CORS_TEST.md](CORS_TEST.md).
</details>

<details>
<summary><strong>PostgreSQL/Redis won't start?</strong></summary>

Ensure Docker Desktop is running — `start.py` falls back to Docker automatically if native services fail.
</details>

<details>
<summary><strong>Reset database?</strong></summary>

```bash
docker compose -f docker-compose.db.yml down -v   # destructive!
```
</details>

<details>
<summary><strong>3D/JARVIS avatar slow?</strong></summary>

Set `NEXT_PUBLIC_DISABLE_3D=1` or let adaptive graphics scale down automatically.
</details>

More help: open a [GitHub issue](https://github.com/soxito/tradebot/issues) with logs attached.

## 📈 Roadmap

- [x] Core platform, multi-exchange connectors, TradingView webhooks
- [x] News & sentiment pipeline + multi-agent orchestrator with decision memory
- [x] Realtime dashboard (SSE) + JARVIS/S.O.X assistant with face-vision & adaptive 3D
- [x] MT5 integration with SMC sniper charting & scalp-bot
- [x] Crypto rug-pull & pump sniper detection with auto-trade loop
- [x] Telegram & WhatsApp channel signal ingestion
- [x] Full plugin ecosystem + Strategy Lab + Electron desktop app + CI/CD releases
- [ ] Fully automated trade execution across all plugins
- [ ] Expanded backtesting engine coverage

See [CHANGELOG.md](CHANGELOG.md) for detailed history.

## ⚠️ Disclaimer

**This software is for educational purposes only.**

- Cryptocurrency trading carries significant risk
- Auto-trading can lead to substantial financial losses
- Never trade with funds you cannot afford to lose
- Always start with testnet/paper trading
- The developers are not responsible for any financial losses

## 📝 License

MIT License — see LICENSE file.

## 🤝 Contributing

Contributions welcome! Please open an issue first to discuss changes.

---

## 💝 Support TradeBot's Development

If TradeBot earned its place on your machine, please consider giving back — donations fund hosting, API testing costs, LLM credits, and faster feature delivery.

| Currency | Network | Address |
| --- | --- | --- |
| **USDT / any BEP-20 token** | BNB Smart Chain (BEP-20) | `0xB87C2384988ab0Afb47f893f785aa636bD30FCE6` |
| **Bitcoin (BTC)** | Bitcoin mainnet | `18TDkAgJaJZwXyuZs9hJ5FhyknwDRxk9iU` |

Every satoshi and gwei counts. Thank you for keeping this project alive! ❤️🙏

---

**Built with 🔥 by TradeBot Architect** · Star ⭐ the repo · Share it · [Donate 💝](#-donate--support-the-project)
