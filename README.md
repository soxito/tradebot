# 🤖 TradeBot

**AI-powered multi-asset trading platform.** Combines news-driven sentiment analysis, TradingView technical signals, and a multi-agent AI orchestrator to generate and (optionally) execute trades across crypto exchanges and MetaTrader 5 (forex, metals, indices) — with a voice-controlled JARVIS/Paul assistant, a plugin ecosystem for ML forecasting and channel-based signal ingestion, and an Obsidian-backed "knowledge brain".

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

## 📋 Features

### Core platform
- ✅ Multi-exchange connectors (Binance, Bitget, Bybit, OKX, KuCoin, Coinbase) with a native Bitget v2 SDK for spot + futures, plus dedicated spot-price providers for forex/metals (CoinGecko/Open Exchange Rates, Swissquote/gold-api)
- ✅ TradingView webhook receiver with signature validation
- ✅ News + sentiment pipeline (RSS + CryptoPanic → VADER + TextBlob), plus Telegram/WhatsApp channel ingestion with dedupe and structured signal extraction
- ✅ Multi-agent AI orchestrator: collaborative Market+Sentiment → Signal → Risk → Executor pipeline with persisted decision history and per-agent learning stats
- ✅ Signal & strategy engine: technical signals, Pine Script strategies, Smart Money Concepts (order blocks, fair value gaps, premium/discount zones), and a Strategy Lab for versioned strategy runs/promotions
- ✅ Risk management (position sizing, stop-loss, take-profit, exposure limits) across both **simulated (paper)** and **live** trading engines
- ✅ Crypto sniper: rug-pull and pump/pullback detection with an optional auto-trade sniper loop
- ✅ Realtime dashboard: TradingView charts, wallet balances, trade history, live SSE stream (Live/Poll fallback)
- ✅ Multi-account + unified-account Bitget balances/positions (USDT/USDC/COIN futures)
- ✅ Economic calendar, research findings and a background research job queue

### JARVIS / Paul / S.O.X assistant
- 🎙️ Hands-free voice control (free Web Speech primary, cost-aware Deepgram STT fallback) with natural-language command dispatch
- 🧠 Position-aware deep analysis (volume pressure + news + forecast + your open PnL)
- 🤖 3D robot avatar that patrols the page + a ~960-particle S.O.X command-room orb rendered off-thread via Web Workers / OffscreenCanvas
- 👁️ Live face-vision (identity enrollment + lip/face overlay) and a companion browser extension for desktop alerts and reliable mic capture outside the page
- 🎚️ Adaptive graphics that auto-scale quality (low → ultra) to the device and live FPS

### Plugins (standalone — never modify core code, see `plugins/`)
- `MT5TradingPlugin` — MetaTrader 5 REST bridge: multi-account management, live positions/orders, SMC "sniper" charting, scalp-bot automation, copy-trading simulation and a backtesting bridge
- `AiMarketAnalyst` — multi-provider LLM router (Groq, OpenRouter, Gemini, Mistral, Cerebras, DeepSeek, Together, OpenAI, custom) with failover, agent profiles, smart limit-order proposals and a risk policy engine
- `KronosForecastPlugin` — Kronos ML candle forecasting (mini/small/base) with heuristic fallback, rendered as chart overlays
- `TelegramSignalNewsPlugin` — Telegram channel ingestion for trading signals and news, with sniper auto-trade
- `WhatsAppSignalNewsPlugin` — WhatsApp channel ingestion (via an OpenWA gateway) for trading signals and news, with sniper auto-trade
- `ObsidianKnowledgePlugin` — syncs signals, agent decisions, strategy notes and code-community graphs into a local Obsidian vault
- `AgentPaulPlugin` — a PAUL-loop (Plan → Apply/Qualify → Unify) background agent with an approval queue and paper/execute/direct trading modes
- `OpenManusPlugin` — routes AI calls through an OpenManus MCP sidecar with phased fallback to `AiMarketAnalyst`, with compliance audit logging
- `OpenHumanPlugin` — exposes TradeBot as an MCP tool server to a local-first OpenHuman "brain" with persistent memory
- `VibeTradingPlugin` — natural-language backtesting sidecar with a 460-alpha "Alpha Zoo" and multi-agent swarm research

### Platform support
- 🖥️ Cross-platform launcher (`start.py`) for macOS, Linux and Windows, plus a standalone Electron **desktop app**
- 🪟 Windows + low-end hardware auto-tuning (RAM/core detection, ML-thread caps, optional 3D kill-switch)

## 🖥️ Desktop app (macOS / Windows / Linux)

If you just want to *use* TradeBot, download the installer from the
[Releases page](https://github.com/soxito/tradebot/releases) — it needs no
Python, Node, PostgreSQL, Redis or Docker. Everything is bundled, and data lives
in a per-user folder that survives updates.

See **[docs/DESKTOP.md](docs/DESKTOP.md)** for install notes (the builds are not
code-signed yet, so first launch needs one extra click), how it differs from the
server setup, and how to build it yourself.

The rest of this section is for running from source.

## 🚀 Quick Start

The recommended way to run TradeBot from source is the cross-platform launcher
`start.py`. It provisions PostgreSQL and Redis (Homebrew or Docker), builds the
Python virtual environment, installs dependencies, and starts the backend and
frontend with resource-aware tuning.

### Prerequisites
- **Python 3.11+** and **Node.js 18+**
- **PostgreSQL** and **Redis** — provisioned automatically via Homebrew or Docker, or bring your own
- **Git**

### 1. Get the code and configure
```bash
git clone https://github.com/soxito/tradebot.git
cd tradebot
cp .env.example .env   # then edit .env with your own API keys
```

### 2. Start everything (recommended)

**macOS / Linux**
```bash
python start.py         # or: ./run-local.sh
```

**Windows**
```bat
start.bat
```

`start.py` also accepts `--status` (show running services) and `--stop` (stop
them). It auto-detects your hardware and, on low-end machines, reduces ML thread
usage and disables 3D/WebGL rendering. Override behaviours with env vars:
`NEXT_PUBLIC_DISABLE_3D=1` (hard-disable 3D), `TRADEBOT_RELOAD=1` (force backend
hot-reload on low-core CPUs).

### 3. Or run with Docker
```bash
docker-compose up --build
```

### Access the application

| Service | Local (`start.py`) | Docker |
| --- | --- | --- |
| Frontend dashboard | http://localhost:3000 | http://localhost:3001 |
| Backend API docs | http://localhost:1448/docs | http://localhost:8080/docs |
| Health check | http://localhost:1448/health | http://localhost:8080/health |
| PostgreSQL | localhost:5434 | localhost:5433 |
| Redis | localhost:6379 | localhost:6380 |

> Default DB credentials: user `tradebot`, database `tradebot`.

## 📥 Download & Update from GitHub

Repository: **https://github.com/soxito/tradebot**

### First-time download

**Option A — Clone with Git (recommended)**
```bash
# HTTPS
git clone https://github.com/soxito/tradebot.git
cd tradebot

# or SSH (if you have SSH keys set up on GitHub)
git clone git@github.com:soxito/tradebot.git
cd tradebot
```

**Option B — Download ZIP (no Git)**
1. Open https://github.com/soxito/tradebot
2. Click the green **Code** button → **Download ZIP**
3. Unzip it and `cd` into the folder

After downloading, create your local config (this file is **not** in the repo):
```bash
cp .env.example .env   # then edit .env with your own API keys
```

### Update your local files to the latest version

Pull the newest changes from GitHub whenever the project is updated:
```bash
cd tradebot
git pull origin main
```

If you have **local edits** that conflict with the pull, stash them first, update, then re-apply:
```bash
git stash            # temporarily shelve your local changes
git pull origin main # get the latest code
git stash pop        # re-apply your changes (resolve any conflicts)
```

> Your `.env` is git-ignored, so `git pull` never touches your API keys.
> If `.env.example` changed, copy any new variables into your existing `.env`.

### Rebuild after an update

After pulling, rebuild so new dependencies and code take effect:
```bash
# Docker
docker-compose up --build

# or, running locally
./run-local.sh
```

## 🔒 Security

### ⚠️ CRITICAL - Never commit secrets!

- ❌ **NEVER** hardcode API keys, passwords, or secrets in code
- ❌ **NEVER** commit `.env` files to version control
- ✅ **ALWAYS** use environment variables
- ✅ **ALWAYS** use testnet/sandbox keys for development
- ✅ **ALWAYS** enable IP whitelisting on exchanges
- ✅ **ALWAYS** use withdrawal address whitelisting

### Default Safety Settings

Auto-trading is **DISABLED** by default. To enable:
```bash
# In .env
ENABLE_AUTO_TRADING=true
```

Risk limits (configured in `.env`):
- Max position size: $1,000 USD
- Max risk per trade: 2%
- Max total exposure: $10,000 USD

## 📁 Project Structure

```
tradebot/
├── start.py                 # ⭐ Cross-platform launcher (macOS/Linux/Windows)
├── start.bat                # Windows one-click wrapper for start.py
├── run-local.sh             # Bash launcher (macOS/Linux)
├── backend/                 # Python FastAPI
│   ├── app/
│   │   ├── agents/         # Multi-agent orchestrator, specialists, decision memory
│   │   ├── api/            # API routes (jarvis, signals, trading, exchanges, vision, ...)
│   │   ├── core/           # Config, database, scheduler (background loops), EventBus
│   │   ├── exchanges/      # Exchange connectors (ccxt + Bitget v2) + forex/metals providers
│   │   ├── plugins/        # Plugin loader/contracts (mounts plugins/* routers)
│   │   ├── sentiment/      # News & sentiment analysis
│   │   ├── signals/        # Signal processing / technical analysis
│   │   ├── trading/        # Simulation + live trade engines, risk, decisions
│   │   ├── workers/        # Background worker entrypoint (runtime.py)
│   │   └── models/         # Database models (ORM)
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               # Next.js 16 / React 19
│   ├── src/
│   │   ├── components/    # UI components (JARVIS, MT5 charts, face-vision, orb)
│   │   ├── pages/         # Next.js pages (dashboard, signals, mt5-live, jarvis-room, ...)
│   │   ├── utils/         # Device performance / adaptive quality
│   │   └── styles/        # CSS / Tailwind
│   ├── package.json
│   └── Dockerfile
├── plugins/                 # Standalone plugins (never modify core)
│   ├── MT5TradingPlugin/
│   ├── AiMarketAnalyst/
│   ├── KronosForecastPlugin/
│   ├── TelegramSignalNewsPlugin/
│   ├── WhatsAppSignalNewsPlugin/
│   ├── ObsidianKnowledgePlugin/
│   ├── AgentPaulPlugin/
│   ├── OpenManusPlugin/
│   ├── OpenHumanPlugin/
│   └── VibeTradingPlugin/
├── jarvis-extension/        # Browser extension for JARVIS voice
├── desktop/                 # Electron desktop app packaging
├── obsidian-vault/          # Local Obsidian vault synced by ObsidianKnowledgePlugin
├── scripts/                 # Setup & diagnostic scripts
├── docs/                    # Desktop app, architecture and product-spec docs
├── docker-compose.yml
├── .env.example             # Template (copy to .env)
├── CHANGELOG.md
└── README.md
```

## 🧠 Obsidian Knowledge Base

TradeBot ships an `ObsidianKnowledgePlugin` that syncs signals, agent decisions,
strategy notes and code-community graphs into a local Obsidian vault, and can
optionally enrich agent prompts with vault context.

### 1. Install Obsidian

Download the free desktop app from **https://obsidian.md** and install it.

### 2. Open the vault

In Obsidian, choose **"Open folder as vault"** and select:
```
<tradebot-root>/obsidian-vault/tradebot
```

### 3. Install community plugins

Go to **Settings → Community plugins → Browse** and install:

| Plugin | Purpose |
| --- | --- |
| **Dataview** | Query notes like a database (signals, decisions, communities) |
| **Templater** | Auto-fill daily-journal and trade note templates |
| **Local REST API** | Exposes a local HTTP API so TradeBot can push/pull notes |
| **Obsidian Git** | Auto-commit vault changes to Git |

### 4. Configure Local REST API

1. Enable the **Local REST API** plugin and open its settings.
2. Copy the generated **API token**.
3. Add the following to your `.env`:

```env
OBSIDIAN_VAULT_PATH=~/obsidian-vault/tradebot
OBSIDIAN_REST_URL=https://localhost:27124
OBSIDIAN_REST_TOKEN=<paste token here>
OBSIDIAN_AUTO_SYNC_MINUTES=15
OBSIDIAN_EXPORT_DECISIONS=true
OBSIDIAN_EXPORT_SIGNALS=true
OBSIDIAN_EXPORT_COMMUNITIES=true
OBSIDIAN_INJECT_CONTEXT=false   # set true to enrich agent prompts with vault notes
```

> The Local REST API plugin uses a self-signed TLS certificate on port 27124.
> TradeBot's backend accepts it; you can override the URL if you change the port.

### 5. First sync

Trigger the initial sync from the TradeBot UI (**Intelligence → Vault**) or via
the API:
```bash
curl -X POST http://localhost:1448/api/v1/plugins/obsidian-knowledge/sync
```

### 6. Vault structure

```
obsidian-vault/tradebot/
├─ _index.md           ← Dashboard (Dataview queries for today's signals, decisions)
├─ _daily/             ← Auto daily journals (one per day)
├─ signals/            ← One note per trading signal
├─ decisions/          ← One note per agent decision
├─ strategies/         ← Strategy reference notes
├─ communities/        ← Graphify code communities (~176 notes)
└─ trades/             ← Closed trade outcome notes
```

### 7. Explore in Obsidian

- Press **Ctrl+G** (macOS: **Cmd+G**) to open the **Graph View** — see notes linked by symbol, strategy or community.
- Use the **Intelligence → Vault** tab in the TradeBot UI for full-text search, manual sync and clicking nodes in the Brain graph to open linked vault notes.

### Dataview query examples

**Today's signals:**
```dataview
TABLE symbol, action, confidence
FROM "signals"
WHERE contains(file.name, date(today))
SORT confidence DESC
```

**Agent performance by symbol:**
```dataview
TABLE symbol, count(rows) AS decisions, round(average(confidence), 2) AS avg_confidence
FROM "decisions"
GROUP BY symbol
SORT count(rows) DESC
```

**Community map:**
```dataview
TABLE node_count
FROM "communities"
SORT node_count DESC
LIMIT 20
```

> Full setup details are also in [`obsidian-vault/SETUP.md`](obsidian-vault/SETUP.md).

---

## 🛠️ Development

### Backend Development
```bash
# Backend runs with hot-reload by default
cd backend
# Logs: docker-compose logs -f backend
```

### Frontend Development
```bash
# Frontend runs with hot-reload by default
cd frontend
# Logs: docker-compose logs -f frontend
```

### Database Management
```bash
# Access PostgreSQL
docker exec -it tradebot-postgres psql -U tradebot -d tradebot

# Access Redis
docker exec -it tradebot-redis redis-cli
```

## 📊 API Endpoints

### Health & Status
- `GET /health` - System health check
- `GET /api/v1/status` - API module status

### Trading & data
- `POST /api/v1/webhooks/tradingview` - TradingView alerts
- `GET /api/v1/signals` - Recent trading signals
- `POST /api/v1/signals/generate` - Run signal generation (technical + SMC)
- `GET /api/v1/sentiment/{symbol}` - Asset sentiment score
- `GET /api/v1/trades` - Trade history
- `POST /api/v1/orders` - Manual order execution
- `GET /api/v1/exchanges/{exchange}/...` - Balances, tickers, OHLCV, order placement per exchange
- `GET /api/v1/simulation/*` - Paper-trading account, orders, positions, auto-trade loop
- `GET /api/v1/live-trade/*` - Live-trading settings, SL/TP, auto-trade loop
- `GET /api/v1/agents/*` - AI agent CRUD, analyze, decision history & learning stats
- `GET /api/v1/rug-pulls/*` / `GET /api/v1/pump-monitor/*` - Rug-pull & pump sniper detection/auto-trade
- `GET /api/v1/research/*` - Research findings, economic calendar, trading plans
- `GET /api/v1/strategy-lab/*` - Strategy versions, runs, promotions
- `WS /api/v1/vision/face-stream` - Face-vision WebSocket for JARVIS face tracking
- `GET /api/v1/stream/events` - Realtime SSE stream (signals, trades, sniper, ticks, alerts)

> The interactive OpenAPI docs at `/docs` list the full, live endpoint set
> (including plugin routes for MT5, Kronos, Telegram/WhatsApp signals, Agent Paul and the AI analyst).

## 🧪 Testing & Verification

### Automatic Connection Test

The dashboard includes **automatic API connection testing**:

1. Open http://localhost:3001
2. Connection status banner appears at the top:
   - ✅ **Hidden** = Connected successfully
   - ⚠️ **Yellow** = Testing connection...
   - 🔴 **Red** = Connection failed (with retry button)

Features:
- Runs automatically on page load
- Auto-retries every 10 seconds if disconnected
- Manual "Retry Connection" button
- Shows last check timestamp

### Run All Tests

Execute the automated test suite:

```bash
# Run comprehensive connection tests
./test-connection.sh
```

This tests:
- ✅ Backend health & availability
- ✅ CORS configuration
- ✅ API endpoints (status, signals, sentiment, trades)
- ✅ Live data from Bitget
- ✅ Frontend accessibility
- ✅ Docker container status

**Expected output:**
```
✅ All tests passed!
🎉 Your TradeBot is ready!
   Frontend: http://localhost:3001
   Backend:  http://localhost:8080
   API Docs: http://localhost:8080/docs
```

### Manual Testing

**Test backend connectivity:**
```bash
curl http://localhost:8080/health
curl http://localhost:8080/cors-test
```

**Test live data:**
```bash
curl 'http://localhost:8080/api/v1/exchanges/bitget/ohlcv/BTCUSDT?timeframe=1h&limit=5'
```

**View logs:**
```bash
docker-compose logs -f backend   # Backend logs
docker-compose logs -f frontend  # Frontend logs
```

**Check CORS configuration:**
```bash
docker logs tradebot-backend 2>&1 | grep "CORS origins"
```

### Troubleshooting CORS Issues

If you see "CORS header missing" errors:

```bash
# 1. Verify CORS settings in .env
grep CORS_ORIGINS .env
# Should show: CORS_ORIGINS=http://localhost:3001,http://localhost:8080,http://localhost:3000

# 2. Recreate backend container (required after .env changes)
docker-compose up -d --force-recreate --no-deps backend

# 3. Verify CORS loaded correctly
docker logs tradebot-backend 2>&1 | grep "CORS origins"
```

See **[CORS_TEST.md](CORS_TEST.md)** for detailed troubleshooting guide.

### Unit Tests

```bash
# Backend tests
cd backend
pytest

# Frontend tests
cd frontend
npm test
```

## 🎙️ JARVIS voice — cost-aware Deepgram fallback

JARVIS keeps using the **free browser Web Speech API** as its primary speech
recogniser. Deepgram is used **only as a fallback**, and only when JARVIS
genuinely mis-hears a command — so the prepaid Deepgram credit lasts for months.

**How it works**
- A short rolling audio buffer (~8s) of the mic is kept on both the in-page
  widget (`PaulChat.tsx`) and the browser extension (`jarvis-extension/`).
- On a **miss** (low-confidence result, a wake word with no usable command, or
  several misses in a row) the buffered clip is sent **once** to the backend
  endpoint `POST /api/v1/voice/deepgram/stt`, which transcribes it with
  Deepgram pre-recorded STT (Nova, ~$0.0043/min — ~15–20× cheaper than the
  Voice Agent). The raw Deepgram key never leaves the backend.
- A **budget guard** caps spend. When the cap is reached the endpoint returns
  `used_deepgram=false` and JARVIS **silently stays on the free engine** — no
  error is shown.
- **Your voice only**: when you have calibrated a voice profile and enabled
  voice match, the fallback is gated by the same speaker-ID fingerprint as the
  free engine. A clip is sent to Deepgram **only** while the live analyser
  confirms it is your calibrated voice — so a TV or another person nearby is
  never transcribed (or charged). The extension reuses the page's live
  `voice-match-update` signal for the same gate. With voice match off, the
  fallback behaves exactly like the free engine (no speaker restriction).

**Budget settings** (in `.env` / `backend/app/core/config.py`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DEEPGRAM_FALLBACK_ENABLED` | `true` | Master on/off switch for the fallback |
| `DEEPGRAM_MONTHLY_CAP_USD` | `60.0` | Hard monthly spend ceiling (~3 months on $200) |
| `DEEPGRAM_DAILY_CAP_USD` | `5.0` | Daily sub-cap to smooth spend |
| `DEEPGRAM_STT_RATE_PER_MIN` | `0.0043` | Nova pre-recorded $/min, used for cost math |
| `DEEPGRAM_STT_MODEL` | `nova-3` | Pre-recorded model |
| `DEEPGRAM_TOTAL_CREDIT_USD` | `200.0` | Total credit, used for the runway projection |

Usage/runway is reported by `GET /api/v1/voice/deepgram/usage` and surfaced in
the JARVIS settings panel (and on the Voice Agent tab as a cost warning).

**Manual verification checklist**
1. Speak a clear command → it runs via Web Speech; **no** `/voice/deepgram/stt`
   call is made (check the Network tab / backend logs).
2. Mumble or say a wake word with a garbled command → exactly **one**
   `/voice/deepgram/stt` call fires and the recovered command runs.
3. `curl http://localhost:1448/api/v1/voice/deepgram/usage` → `month_spend`,
   `remaining`, and `projected_runway_days` update after a real escalation.
4. Force the cap (set `DEEPGRAM_MONTHLY_CAP_USD=0` and reload the backend) →
   escalation returns `used_deepgram=false` and JARVIS stays on the free engine
   with no error UI; the settings badge shows **Paused (budget)**.

## 📈 Roadmap

- [x] Core platform: infrastructure, multi-exchange connectors, TradingView webhooks
- [x] News & sentiment pipeline
- [x] Multi-agent AI orchestrator with decision memory and risk management
- [x] Realtime dashboard (SSE) with charts, balances and trade history
- [x] JARVIS/S.O.X voice assistant + face-vision + adaptive 3D graphics
- [x] MetaTrader 5 integration (forex/metals/indices) with SMC sniper charting & scalp-bot
- [x] Crypto rug-pull & pump sniper detection with auto-trade loop
- [x] Telegram & WhatsApp channel signal ingestion
- [x] Plugin ecosystem (MT5, AI analyst, Kronos, Telegram, WhatsApp, Obsidian, Agent Paul, OpenManus, OpenHuman, Vibe-Trading)
- [x] Strategy Lab (versioned strategies, runs, promotions)
- [x] Cross-platform + low-end hardware support (Windows, `start.py` auto-tuning) + Electron desktop app
- [x] Auto-trading execution with background scheduler (simulation + live loops)
- [x] Alerting (Telegram, Discord, email) + Prometheus monitoring & metrics
- [ ] Fully automated trade execution across all plugins
- [ ] Expanded backtesting engine coverage across all strategy types

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.

## ⚠️ Disclaimer

**This software is for educational purposes only.**

- Cryptocurrency trading carries significant risk
- Auto-trading can lead to substantial financial losses
- Never trade with funds you cannot afford to lose
- Always start with testnet/paper trading
- The developers are not responsible for any financial losses

## 📝 License

MIT License - see LICENSE file

## 🤝 Contributing

Contributions welcome! Please open an issue first to discuss changes.

---

**Built with 🔥 by TradeBot Architect**
