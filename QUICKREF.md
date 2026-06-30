# TradeBot Quick Reference Card

## 🚀 Quick Commands

### Start/Stop Services
```bash
docker-compose up -d        # Start all services
docker-compose down         # Stop all services
docker-compose down -v      # Stop and remove data
docker-compose restart      # Restart all services
docker-compose ps           # Check status
docker-compose logs -f      # View all logs
docker-compose logs backend # View backend logs only
```

### Access Points
| Service | URL |
|---------|-----|
| Dashboard | http://localhost:3001 |
| API Docs | http://localhost:8080/docs |
| Health Check | http://localhost:8080/health |
| Status | http://localhost:8080/api/v1/status |

### Quick Tests
```bash
# Backend health
curl http://localhost:8080/health

# Exchange status
curl http://localhost:8080/api/v1/exchanges/status

# Create signal
curl -X POST http://localhost:8080/api/v1/signals/ \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USDT","action":"buy","source":"manual","price":50000,"strength":0.8,"confidence":0.75}'

# Get signals
curl http://localhost:8080/api/v1/signals/

# Update sentiment
curl -X POST http://localhost:8080/api/v1/sentiment/update

# Get sentiment
curl http://localhost:8080/api/v1/sentiment/

# Trade history
curl http://localhost:8080/api/v1/trading/history
```

---

## 📁 Key Files

### Configuration
- `.env` - Environment variables (API keys, settings)
- `.env.example` - Template for .env
- `docker-compose.yml` - Service orchestration

### Backend
- `backend/app/main.py` - FastAPI entry point
- `backend/app/core/config.py` - Settings
- `backend/app/api/routes.py` - API router
- `backend/requirements.txt` - Dependencies

### Frontend
- `frontend/src/pages/index.tsx` - Dashboard
- `frontend/src/services/api.ts` - API client
- `frontend/package.json` - Dependencies

---

## 🔧 Configuration

### Environment Variables (.env)

**Required for Testing:**
```env
# Database (default values work)
DATABASE_URL=postgresql+asyncpg://tradebot:tradebot123@postgres:5432/tradebot
REDIS_URL=redis://redis:6379/0

# CORS (frontend URL)
CORS_ORIGINS=http://localhost:3001
```

**Optional for Live Trading:**
```env
# Exchange API Keys
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BITGET_API_KEY=your_key
BITGET_API_SECRET=your_secret
# ... (repeat for other exchanges)

# Sentiment API
CRYPTOPANIC_API_KEY=your_key
MARKETAUX_API_KEY=your_key
GNEWS_API_KEY=your_key
CURRENTS_API_KEY=your_key
COINGECKO_API_KEY=your_key
COINMARKETCAP_API_KEY=your_key

# Freshness controls
SENTIMENT_MAX_AGE_HOURS=2
SENTIMENT_SCORE_VALID_MINUTES=5

# Safety (keep false for testing)
ENABLE_AUTO_TRADING=false

# Risk Limits
MAX_POSITION_SIZE=1000
MAX_TOTAL_EXPOSURE=5000
RISK_PER_TRADE=0.02
```

---

## 📊 API Endpoints

### Health & Status
```bash
GET  /health                    # System health
GET  /api/v1/status             # Module status
```

### Exchanges
```bash
GET  /api/v1/exchanges/status                        # All exchanges
GET  /api/v1/exchanges/{exchange}/balance            # Account balance
GET  /api/v1/exchanges/{exchange}/ticker/{symbol}    # Price ticker
POST /api/v1/exchanges/{exchange}/order              # Place order
```

### Signals
```bash
GET  /api/v1/signals/                     # List signals
POST /api/v1/signals/                     # Create signal
GET  /api/v1/signals/{id}                 # Get signal
POST /api/v1/signals/webhook/tradingview  # TradingView webhook
```

### Sentiment
```bash
GET  /api/v1/sentiment/            # List all sentiment scores
GET  /api/v1/sentiment/{symbol}    # Get symbol sentiment
POST /api/v1/sentiment/analyze     # Analyze custom text
POST /api/v1/sentiment/update      # Fetch latest news
```

### Trading
```bash
POST /api/v1/trading/evaluate/{signal_id}  # Evaluate with risk mgmt
POST /api/v1/trading/execute               # Execute trade (dry-run or live)
GET  /api/v1/trading/analyze/{symbol}      # Analyze symbol
GET  /api/v1/trading/history               # Trade history
GET  /api/v1/trading/status                # Trading system status
```

---

## 🎨 Dashboard Components

### TradingView Chart
- **File**: `frontend/src/components/TradingViewChart.tsx`
- **Features**: Candlestick chart, zoom, pan
- **Data**: Auto-generated sample data (replace with real API data)

### Signal Feed
- **File**: `frontend/src/components/SignalFeed.tsx`
- **Features**: Auto-refresh (10s), status badges, color-coded actions
- **API**: `GET /api/v1/signals/`

### Sentiment Dashboard
- **File**: `frontend/src/components/SentimentDashboard.tsx`
- **Features**: Visual bars, manual refresh, source counts
- **API**: `GET /api/v1/sentiment/`, `POST /api/v1/sentiment/update`

### Trade History
- **File**: `frontend/src/components/TradeHistory.tsx`
- **Features**: Filterable table, P&L tracking, summary stats
- **API**: `GET /api/v1/trading/history`

---

## 🐛 Debugging

### View Logs
```bash
# All logs
docker-compose logs -f

# Specific service
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f postgres
docker-compose logs -f redis

# Last 100 lines
docker-compose logs --tail=100 backend
```

### Database Access
```bash
# Connect to PostgreSQL
docker exec -it tradebot-postgres psql -U tradebot -d tradebot

# Useful queries
SELECT * FROM signals ORDER BY created_at DESC LIMIT 10;
SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;
SELECT * FROM sentiment_scores ORDER BY created_at DESC;

# Exit
\q
```

### Redis Access
```bash
# Connect to Redis
docker exec -it tradebot-redis redis-cli

# Useful commands
KEYS *                # List all keys
GET key_name          # Get value
FLUSHALL              # Clear all data (use with caution!)

# Exit
exit
```

---

## ⚠️ Safety Checks

### Before Enabling Live Trading

1. **Test Dry-Run Mode First**
```bash
curl -X POST http://localhost:8080/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -d '{"signal_id":1,"exchange":"binance","dry_run":true}'
```

2. **Verify Risk Limits in .env**
```env
MAX_POSITION_SIZE=1000        # Max size per position (USD)
MAX_TOTAL_EXPOSURE=5000       # Max total exposure (USD)
RISK_PER_TRADE=0.02           # Risk 2% per trade
MIN_CONFIDENCE_THRESHOLD=0.6  # Only trade signals > 60% confidence
```

3. **Start with Small Test Amount**
```env
# Use testnet first
BINANCE_TESTNET=true

# Then start with minimal balance
# Fund exchange with only $100-500 for testing
```

4. **Enable Auto-Trading**
```env
ENABLE_AUTO_TRADING=true
```

5. **Restart Backend**
```bash
docker-compose restart backend
```

---

## 🔄 Common Workflows

### 1. Test TradingView Integration

**Setup TradingView Alert:**
1. Go to TradingView.com
2. Create alert with webhook URL: `http://your-server:8000/api/v1/signals/webhook/tradingview`
3. Use JSON webhook message:
```json
{
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "price": {{close}},
  "source": "tradingview",
  "timeframe": "{{interval}}",
  "indicator": "MY_STRATEGY",
  "strength": 0.8,
  "confidence": 0.75
}
```

**Test Webhook Locally:**
```bash
curl -X POST http://localhost:8080/api/v1/signals/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "action": "buy",
    "price": 50000,
    "source": "tradingview",
    "timeframe": "1h",
    "indicator": "MY_STRATEGY",
    "strength": 0.8,
    "confidence": 0.75
  }'
```

### 2. Update Sentiment Scores

```bash
# Trigger news scraping
curl -X POST http://localhost:8080/api/v1/sentiment/update

# Check results
curl http://localhost:8080/api/v1/sentiment/BTC
```

### 3. Analyze Trading Decision

```bash
# Get signal ID from dashboard or API
SIGNAL_ID=1

# Evaluate with decision engine
curl -X POST "http://localhost:8080/api/v1/trading/evaluate/${SIGNAL_ID}?account_balance=10000"

# Execute dry-run trade
curl -X POST http://localhost:8080/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -d "{\"signal_id\":${SIGNAL_ID},\"exchange\":\"binance\",\"dry_run\":true}"
```

---

## 📈 Performance Tips

### Backend Optimization
- Use Redis caching for exchange data
- Enable database connection pooling (default)
- Increase worker count: `docker-compose.yml` → `command: uvicorn app.main:app --workers 4`

### Frontend Optimization
- Use React.memo for expensive components
- Implement virtual scrolling for large lists
- Add debouncing to API calls

### Database Optimization
```sql
-- Create indexes for common queries
CREATE INDEX idx_signals_created_at ON signals(created_at DESC);
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_trades_created_at ON trades(created_at DESC);
```

---

## 🚨 Troubleshooting

| Issue | Solution |
|-------|----------|
| Frontend can't connect to backend | Check `NEXT_PUBLIC_API_URL` in `.env`, restart frontend |
| Exchange API errors | Verify API keys in `.env`, check exchange status page |
| Database connection failed | Run `docker-compose down -v && docker-compose up -d` |
| Sentiment returns no data | Add at least one sentiment provider key (`MARKETAUX_API_KEY`, `GNEWS_API_KEY`, `CURRENTS_API_KEY`, or `CRYPTOPANIC_API_KEY`) and lower `SENTIMENT_MAX_AGE_HOURS` only if your providers publish frequently |
| Auto-trading not working | Check `ENABLE_AUTO_TRADING=true` in `.env` |
| Chart not rendering | Clear browser cache, check browser console for errors |

---

## 🎙️ JARVIS Deepgram fallback (cost-aware)

JARVIS uses the **free Web Speech API** first; Deepgram is only called when a
command is **missed**, on a short buffered clip, with a backend spend cap.

```bash
# Current spend / remaining budget / projected runway
curl http://localhost:1448/api/v1/voice/deepgram/usage

# Transcribe a clip (multipart) — returns used_deepgram=false when capped
curl -F file=@clip.webm http://localhost:1448/api/v1/voice/deepgram/stt
```

Caps live in `.env` (`DEEPGRAM_MONTHLY_CAP_USD=60`, `DEEPGRAM_DAILY_CAP_USD=5`,
`DEEPGRAM_STT_RATE_PER_MIN=0.0043`). When the cap is hit JARVIS silently stays
on the free engine. See **[README.md](README.md)** → *JARVIS voice* for details.

---

## 📚 Documentation Links

- **Full Documentation**: [README.md](README.md)
- **Testing Guide**: [TESTING.md](TESTING.md)
- **Build Status**: [STATUS.md](STATUS.md)
- **Completion Summary**: [PHASE_6_COMPLETE.md](PHASE_6_COMPLETE.md)
- **API Docs (Swagger)**: http://localhost:8080/docs
- **API Docs (ReDoc)**: http://localhost:8080/redoc

---

## 📞 Support

For issues or questions:
1. Check logs: `docker-compose logs -f`
2. Review testing guide: [TESTING.md](TESTING.md)
3. Verify configuration: `.env` file
4. Check service status: `docker-compose ps`

---

**Version**: 1.0 (Phase 6 Complete)  
**Last Updated**: January 2024
