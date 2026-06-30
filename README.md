# 🤖 TradeBot

**AI-powered crypto trading bot** combining news-driven sentiment analysis with TradingView technical signals to generate actionable trades across multiple exchanges.

## 🏗️ Architecture

- **Backend:** Python 3.12, FastAPI, ccxt, SQLAlchemy, Redis
- **Frontend:** TypeScript, React/Next.js, TradingView Lightweight Charts
- **Exchanges:** Binance, Bitget, Bybit, OKX, KuCoin, Coinbase
- **Sentiment:** CoinGecko, CoinMarketCap, CryptoPanic, Reddit, RSS feeds
- **Infrastructure:** Docker, PostgreSQL, Redis

## 📋 Features

### ✅ Completed (Phases 1-6)
✅ Project scaffold with Docker infrastructure  
✅ FastAPI backend with CORS & health checks  
✅ Next.js frontend with Tailwind CSS  
✅ PostgreSQL & Redis integration  
✅ Environment variable management  
✅ Security (API key validation, webhook signatures)  
✅ **Multi-exchange connector** (Binance, Bitget, Bybit, OKX, KuCoin, Coinbase via ccxt)  
✅ **TradingView webhook receiver** with signal validation  
✅ **News scraping & sentiment analysis** (RSS feeds + CryptoPanic + VADER + TextBlob)  
✅ **Signal & decision engine** with risk management (position sizing, stop-loss, take-profit)  
✅ **Real-time dashboard** with TradingView Lightweight Charts  
✅ **Trade history tracking** with P&L calculations  

### 🔄 Coming Soon (Phases 7-8)
- 🤖 Auto-trading execution with background scheduler
- 🔔 Alert system (Telegram, Discord, email)
- 📊 Advanced monitoring and metrics

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Git

### Installation

1. **Clone the repository**
```bash
git clone <your-repo-url> tradebot
cd tradebot
```

2. **Create environment file**
```bash
cp .env.example .env
```

3. **Configure API keys** (edit `.env`)
```bash
# Add your exchange API keys (testnet keys for development!)
# Add news/sentiment API keys (most have free tiers)
# Generate a secure SECRET_KEY and TRADINGVIEW_WEBHOOK_SECRET
```

4. **Start the services**
```bash
docker-compose up --build
```

5. **Access the application**
- **Frontend Dashboard:** http://localhost:3001
- **Backend API Docs:** http://localhost:8080/docs
- **Health Check:** http://localhost:8080/health
- **PostgreSQL:** localhost:5433 (user: tradebot, db: tradebot)
- **Redis:** localhost:6380

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
├── backend/                 # Python FastAPI
│   ├── app/
│   │   ├── api/            # API routes & webhooks
│   │   ├── core/           # Config, security
│   │   ├── exchanges/      # Exchange connectors
│   │   ├── sentiment/      # News & sentiment analysis
│   │   ├── signals/        # Signal processing
│   │   ├── trading/        # Order execution
│   │   └── models/         # Database models
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               # Next.js/React
│   ├── src/
│   │   ├── components/    # UI components
│   │   ├── pages/         # Next.js pages
│   │   └── styles/        # CSS/Tailwind
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
├── .env.example           # Template (copy to .env)
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

### Coming Soon
- `POST /api/v1/webhooks/tradingview` - TradingView alerts
- `GET /api/v1/signals` - Recent trading signals
- `GET /api/v1/sentiment/{symbol}` - Asset sentiment score
- `GET /api/v1/trades` - Trade history
- `POST /api/v1/orders` - Manual order execution

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

- [x] Phase 1: Infrastructure & scaffold
- [x] Phase 2: Multi-exchange connector
- [x] Phase 3: TradingView integration
- [x] Phase 4: Sentiment analysis pipeline
- [x] Phase 5: Signal & decision engine
- [x] Phase 6: Dashboard with charts
- [ ] Phase 7: Auto-trading execution
- [ ] Phase 8: Monitoring & alerts

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
