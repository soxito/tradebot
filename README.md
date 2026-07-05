# 🤖 TradeBot

**AI-powered crypto trading bot** combining news-driven sentiment analysis with TradingView technical signals to generate actionable trades across multiple exchanges.

## 🏗️ Architecture

- **Backend:** Python 3.13, FastAPI, `ccxt` + native Bitget v2 SDK, SQLAlchemy (async) + asyncpg, Redis
- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind, TradingView Lightweight Charts, Three.js, react-force-graph-3d
- **Exchanges:** Binance, Bitget (spot + futures), Bybit, OKX, KuCoin, Coinbase
- **Sentiment & news:** CryptoPanic, RSS feeds, Reddit → VADER + TextBlob scoring
- **Realtime:** Server-Sent Events over a Redis pub/sub `EventBus` (in-memory fallback)
- **Assistant:** JARVIS / Paul — global voice + chat widget with a WebGL S.O.X room
- **Infrastructure:** PostgreSQL, Redis (Homebrew or Docker), Docker Compose

## 📋 Features

### Core platform
- ✅ Multi-exchange connectors (Binance, Bitget, Bybit, OKX, KuCoin, Coinbase) with a native Bitget v2 SDK for spot + futures
- ✅ TradingView webhook receiver with signature validation
- ✅ News + sentiment pipeline (RSS + CryptoPanic → VADER + TextBlob)
- ✅ Signal & decision engine with risk management (position sizing, stop-loss, take-profit, exposure limits)
- ✅ Realtime dashboard: TradingView charts, wallet balances, trade history, live SSE stream (Live/Poll fallback)
- ✅ Multi-account + unified-account Bitget balances/positions (USDT/USDC/COIN futures)

### JARVIS / Paul assistant
- 🎙️ Hands-free voice control (free Web Speech primary, cost-aware Deepgram STT fallback)
- 🧠 Position-aware deep analysis (volume pressure + news + forecast + your open PnL)
- 🤖 3D robot avatar + ~980-particle S.O.X orb rendered off-thread via Web Workers / OffscreenCanvas
- 🎚️ Adaptive graphics that auto-scale quality (low → ultra) to the device and live FPS

### Plugins (standalone — never modify core code, see `plugins/`)
- `MT5TradingPlugin` — MetaTrader 5 REST bridge
- `AiMarketAnalyst` — multi-provider LLM router (Groq, OpenRouter, Gemini, Mistral, Cerebras, DeepSeek, Together, OpenAI, custom) with failover
- `KronosForecastPlugin` — Kronos ML candle forecasting (mini/small/base) with heuristic fallback
- `TelegramSignalNewsPlugin` — Telegram signal ingestion, news→sentiment, and sniper auto-trade
- `ObsidianKnowledgePlugin` — knowledge-base integration
- `AgentPaulPlugin` — background "subconscious" agent

### Platform support
- 🖥️ Cross-platform launcher (`start.py`) for macOS, Linux and Windows
- 🪟 Windows + low-end hardware auto-tuning (RAM/core detection, ML-thread caps, optional 3D kill-switch)

## 🚀 Quick Start

The recommended way to run TradeBot locally is the cross-platform launcher
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
│   │   ├── agents/         # Background agents
│   │   ├── api/            # API routes, webhooks, SSE stream
│   │   ├── core/           # Config, security, EventBus
│   │   ├── exchanges/      # Exchange connectors (ccxt + Bitget v2)
│   │   ├── sentiment/      # News & sentiment analysis
│   │   ├── signals/        # Signal processing
│   │   ├── trading/        # Order execution
│   │   └── models/         # Database models
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               # Next.js 16 / React 19
│   ├── src/
│   │   ├── components/    # UI components (JARVIS, charts, orb)
│   │   ├── pages/         # Next.js pages
│   │   ├── utils/         # Device performance / adaptive quality
│   │   └── styles/        # CSS / Tailwind
│   ├── package.json
│   └── Dockerfile
├── plugins/                # Standalone plugins (never modify core)
│   ├── MT5TradingPlugin/
│   ├── AiMarketAnalyst/
│   ├── KronosForecastPlugin/
│   ├── TelegramSignalNewsPlugin/
│   ├── ObsidianKnowledgePlugin/
│   └── AgentPaulPlugin/
├── jarvis-extension/       # Browser extension for JARVIS voice
├── scripts/                # Setup & diagnostic scripts
├── docker-compose.yml
├── .env.example           # Template (copy to .env)
├── CHANGELOG.md
└── README.md
```

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
- `GET /api/v1/sentiment/{symbol}` - Asset sentiment score
- `GET /api/v1/trades` - Trade history
- `POST /api/v1/orders` - Manual order execution
- `GET /api/v1/stream/events` - Realtime SSE stream (signals, trades, sniper, ticks, alerts)

> The interactive OpenAPI docs at `/docs` list the full, live endpoint set
> (including plugin routes for MT5, Kronos, Telegram signals and the AI analyst).

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
- [x] Signal & decision engine with risk management
- [x] Realtime dashboard (SSE) with charts, balances and trade history
- [x] JARVIS voice assistant + adaptive 3D graphics
- [x] Plugin ecosystem (MT5, AI analyst, Kronos, Telegram, Obsidian, Agent Paul)
- [x] Cross-platform + low-end hardware support (Windows, `start.py` auto-tuning)
- [x] Auto-trading execution with background scheduler (simulation + live loops)
- [x] Alerting (Telegram, Discord, email) + Prometheus monitoring & metrics
- [ ] Fully automated trade execution across all plugins
- [ ] Backtesting engine

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
