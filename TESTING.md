# TradeBot Testing Guide

This document provides step-by-step testing procedures for all implemented features.

## Prerequisites

Ensure all services are running:

```bash
docker-compose ps
```

Expected output:
```
NAME                STATUS                    PORTS
tradebot-backend    Up (healthy)              0.0.0.0:8000->8000/tcp
tradebot-frontend   Up                        0.0.0.0:3001->3000/tcp
tradebot-postgres   Up (healthy)              0.0.0.0:5433->5432/tcp
tradebot-redis      Up (healthy)              0.0.0.0:6380->6379/tcp
```

---

## 1. Backend Health Check

### Test API Connectivity

```bash
curl http://localhost:8080/health
```

**Expected Response:**
```json
{"status":"healthy","version":"0.1.0"}
```

### Check Module Status

```bash
curl http://localhost:8080/api/v1/status
```

**Expected Response:**
```json
{
  "status": "operational",
  "modules": {
    "exchanges": "ready",
    "signals": "ready",
    "sentiment": "ready",
    "trading": "ready"
  }
}
```

---

## 2. Exchange Integration Testing

### Get Exchange Status

```bash
curl http://localhost:8080/api/v1/exchanges/status
```

**Expected Response:**
```json
{
  "exchanges": {
    "binance": "healthy",
    "bitget": "healthy",
    "bybit": "healthy",
    "okx": "healthy",
    "kucoin": "healthy",
    "coinbase": "healthy"
  },
  "initialized_count": 6,
  "total_count": 6
}
```

### Get Balance (requires API key)

```bash
# Will return error if no API key configured (expected)
curl http://localhost:8080/api/v1/exchanges/binance/balance
```

### Get Ticker Price

```bash
# Should work without API key
curl http://localhost:8080/api/v1/exchanges/binance/ticker/BTC/USDT
```

**Expected Response:**
```json
{
  "symbol": "BTC/USDT",
  "last": 50000.00,
  "bid": 49999.50,
  "ask": 50000.50,
  "timestamp": 1234567890
}
```

---

## 3. Signal Processing

### Create a Manual Signal

```bash
curl -X POST http://localhost:8080/api/v1/signals/ \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTC/USDT",
    "action": "buy",
    "source": "manual",
    "price": 50000,
    "strength": 0.8,
    "confidence": 0.75,
    "metadata": {
      "reason": "Testing signal creation"
    }
  }'
```

**Expected Response:**
```json
{
  "id": 1,
  "symbol": "BTC/USDT",
  "action": "buy",
  "source": "manual",
  "price": 50000,
  "strength": 0.8,
  "confidence": 0.75,
  "status": "pending",
  "created_at": "2024-01-15T10:30:00Z"
}
```

### Simulate TradingView Webhook

```bash
curl -X POST http://localhost:8080/api/v1/signals/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "ETHUSDT",
    "action": "sell",
    "price": 3000,
    "source": "tradingview",
    "timeframe": "1h",
    "indicator": "RSI_MACD",
    "strength": 0.85,
    "confidence": 0.80
  }'
```

### List All Signals

```bash
curl "http://localhost:8080/api/v1/signals/?limit=20"
```

### Get Specific Signal

```bash
curl http://localhost:8080/api/v1/signals/1
```

---

## 4. Sentiment Analysis

### Analyze Custom Text

```bash
curl -X POST http://localhost:8080/api/v1/sentiment/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Bitcoin is extremely bullish! To the moon! 🚀"
  }'
```

**Expected Response:**
```json
{
  "score": 0.85,
  "magnitude": 0.92,
  "confidence": 0.88,
  "label": "positive",
  "method": "vader+textblob"
}
```

### Update Sentiment from News Sources

```bash
curl -X POST http://localhost:8080/api/v1/sentiment/update
```

**Expected Response:**
```json
{
  "status": "success",
  "articles_fetched": 25,
  "symbols_analyzed": ["BTC", "ETH", "SOL"],
  "sources": ["coindesk", "cointelegraph", "theblock", "decrypt", "bitcoinmagazine"]
}
```

### Get Sentiment Scores

```bash
curl http://localhost:8080/api/v1/sentiment/
```

**Expected Response:**
```json
{
  "sentiments": [
    {
      "symbol": "BTC",
      "score": 0.35,
      "magnitude": 0.68,
      "sources_count": 12,
      "created_at": "2024-01-15T10:30:00Z"
    },
    {
      "symbol": "ETH",
      "score": 0.22,
      "magnitude": 0.55,
      "sources_count": 8,
      "created_at": "2024-01-15T10:30:00Z"
    }
  ]
}
```

### Get Symbol-Specific Sentiment

```bash
curl http://localhost:8080/api/v1/sentiment/BTC
```

---

## 5. Risk Management & Decision Engine

### Evaluate a Signal

```bash
# Evaluate signal with risk management (requires signal_id from previous steps)
curl -X POST "http://localhost:8080/api/v1/trading/evaluate/1?account_balance=10000"
```

**Expected Response:**
```json
{
  "signal_id": 1,
  "should_execute": true,
  "decision": "BUY",
  "confidence": 0.82,
  "position_size": 250.50,
  "stop_loss": 47500,
  "take_profit": 52500,
  "risk_amount": 125.25,
  "risk_percentage": 1.25,
  "reasons": [
    "Signal confidence above threshold (0.75)",
    "Sentiment aligned with signal direction (+0.35)",
    "Position size within limits",
    "Risk/reward ratio acceptable"
  ]
}
```

### Analyze Symbol (Combines Signals + Sentiment)

```bash
curl http://localhost:8080/api/v1/trading/analyze/BTC/USDT?lookback_hours=24
```

**Expected Response:**
```json
{
  "symbol": "BTC/USDT",
  "signals_count": 5,
  "average_confidence": 0.78,
  "sentiment_score": 0.35,
  "sentiment_magnitude": 0.68,
  "recommendation": "BULLISH",
  "reasons": [
    "3 buy signals vs 2 sell signals",
    "Strong positive sentiment (+0.35)",
    "High signal confidence (avg 0.78)"
  ]
}
```

---

## 6. Trade Execution

### Execute Trade (Dry Run)

```bash
curl -X POST http://localhost:8080/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": 1,
    "exchange": "binance",
    "dry_run": true
  }'
```

**Expected Response (Dry Run):**
```json
{
  "trade_id": 1,
  "status": "dry_run_success",
  "signal_id": 1,
  "exchange": "binance",
  "symbol": "BTC/USDT",
  "side": "buy",
  "amount": 0.005,
  "price": 50000,
  "total": 250,
  "message": "Dry run: Trade would have been executed successfully"
}
```

### Execute Trade (Live) - ⚠️ REQUIRES AUTO_TRADING=true

```bash
# This will FAIL by design unless ENABLE_AUTO_TRADING=true in .env
curl -X POST http://localhost:8080/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": 1,
    "exchange": "binance",
    "dry_run": false
  }'
```

**Expected Response (Safety Block):**
```json
{
  "detail": "Auto-trading is disabled. Set ENABLE_AUTO_TRADING=true to enable live trading."
}
```

### Get Trade History

```bash
curl "http://localhost:8080/api/v1/trading/history?limit=50"
```

**Expected Response:**
```json
{
  "trades": [
    {
      "id": 1,
      "exchange": "binance",
      "symbol": "BTC/USDT",
      "side": "buy",
      "amount": 0.005,
      "price": 50000,
      "total": 250,
      "fee": 0.25,
      "pnl": null,
      "status": "executed",
      "created_at": "2024-01-15T10:30:00Z"
    }
  ],
  "total_count": 1,
  "total_pnl": 0
}
```

### Get Trading System Status

```bash
curl http://localhost:8080/api/v1/trading/status
```

**Expected Response:**
```json
{
  "auto_trading_enabled": false,
  "active_positions": 0,
  "total_trades_today": 5,
  "total_pnl_today": 125.50,
  "risk_limits": {
    "max_position_size": 1000,
    "max_total_exposure": 5000,
    "risk_per_trade": 0.02
  }
}
```

---

## 7. Frontend Dashboard Testing

### Access Dashboard

Open browser: http://localhost:3001

**Expected Elements:**
- ✅ Status cards (API, Exchanges, Auto-Trading, Signals)
- ✅ TradingView chart with candlesticks
- ✅ Signal feed (auto-refreshing)
- ✅ Sentiment dashboard (bullish/bearish indicators)
- ✅ Trade history table with P&L
- ✅ Quick action buttons

### Test Dashboard Features

1. **Chart Interaction**:
   - Hover over candlesticks → See price tooltip
   - Scroll → Zoom in/out
   - Drag → Pan timeline

2. **Signal Feed**:
   - Wait 10 seconds → Feed auto-refreshes
   - Check signal status badges (pending, executed, failed)
   - Verify buy signals are green, sell signals are red

3. **Sentiment Dashboard**:
   - Click "Refresh Sentiment Data" → Triggers `/api/v1/sentiment/update`
   - Check sentiment bars (green = bullish, red = bearish)
   - Verify source counts displayed

4. **Trade History**:
   - Filter by status (All, Executed, Failed)
   - Check P&L calculations (green = profit, red = loss)
   - Verify total P&L summary

---

## 8. API Documentation

### Swagger UI

Open browser: http://localhost:8080/docs

**Features:**
- Interactive API testing
- Request/response schemas
- Authentication testing
- Example payloads

### ReDoc

Open browser: http://localhost:8080/redoc

**Features:**
- Clean API documentation
- Search functionality
- Code examples in multiple languages

---

## 9. Database Inspection

### Access PostgreSQL

```bash
docker exec -it tradebot-postgres psql -U tradebot -d tradebot
```

**Useful Queries:**

```sql
-- List all tables
\dt

-- Check signals
SELECT id, symbol, action, confidence, status, created_at FROM signals ORDER BY created_at DESC LIMIT 10;

-- Check trades
SELECT id, exchange, symbol, side, amount, pnl, status, created_at FROM trades ORDER BY created_at DESC LIMIT 10;

-- Check sentiment scores
SELECT symbol, score, magnitude, sources_count, created_at FROM sentiment_scores ORDER BY created_at DESC;

-- Exit
\q
```

---

## 10. Logs & Debugging

### View Backend Logs

```bash
docker-compose logs -f backend
```

**Look for:**
- ✅ "Application startup complete"
- ✅ "Exchange manager initialized"
- ✅ "Sentiment analyzer initialized"
- ⚠️ Any ERROR or WARNING messages

### View Frontend Logs

```bash
docker-compose logs -f frontend
```

**Look for:**
- ✅ "Ready in X ms"
- ✅ "compiled successfully"
- ⚠️ Build errors or warnings

### View All Logs

```bash
docker-compose logs -f
```

---

## 11. Performance Testing

### Load Test Signal Endpoint

```bash
# Install apache bench (macOS)
brew install httpd

# Send 100 requests with 10 concurrent
ab -n 100 -c 10 -p signal.json -T application/json http://localhost:8080/api/v1/signals/

# signal.json content:
# {"symbol":"BTC/USDT","action":"buy","source":"test","price":50000,"strength":0.8,"confidence":0.75}
```

### Monitor Resource Usage

```bash
docker stats
```

**Expected:**
- Backend: < 200 MB RAM
- Frontend: < 100 MB RAM
- PostgreSQL: < 100 MB RAM
- Redis: < 50 MB RAM

---

## 12. Security Testing

### Test Webhook Signature Validation (Optional)

If you configure `TRADINGVIEW_WEBHOOK_SECRET` in `.env`:

```bash
# Without signature (should fail)
curl -X POST http://localhost:8080/api/v1/signals/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USDT","action":"buy"}'

# With valid signature (should succeed)
# Generate signature: echo -n '{"symbol":"BTC/USDT","action":"buy"}' | openssl dgst -sha256 -hmac "your_secret"
curl -X POST http://localhost:8080/api/v1/signals/webhook/tradingview \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: <generated_signature>" \
  -d '{"symbol":"BTC/USDT","action":"buy"}'
```

---

## 13. Cleanup & Reset

### Stop All Services

```bash
docker-compose down
```

### Remove All Data (Reset Database)

```bash
docker-compose down -v
```

### Restart Fresh

```bash
docker-compose up -d
```

---

## Troubleshooting

### Issue: Frontend not connecting to backend

**Solution:**
```bash
# Check NEXT_PUBLIC_API_URL in .env
cat .env | grep NEXT_PUBLIC_API_URL

# Should be: http://localhost:8080/api/v1
```

### Issue: Exchange API errors

**Solution:**
- Exchange connectors run in testnet mode by default
- Add real API keys to `.env` for live data
- Ensure API keys have correct permissions

### Issue: Sentiment update returns no data

**Solution:**
```bash
# Add at least one sentiment provider key to .env
CRYPTOPANIC_API_KEY=your_key_here
MARKETAUX_API_KEY=your_key_here
GNEWS_API_KEY=your_key_here
CURRENTS_API_KEY=your_key_here

# Keep freshness realistic for your providers
SENTIMENT_MAX_AGE_HOURS=2

# Restart backend
docker-compose restart backend
```

### Issue: Database connection errors

**Solution:**
```bash
# Check PostgreSQL logs
docker-compose logs postgres

# Verify database exists
docker exec -it tradebot-postgres psql -U tradebot -l

# Recreate database
docker-compose down -v
docker-compose up -d
```

---

## Next Steps

Once all tests pass:

1. **Configure Exchange API Keys** (`.env`) for live data
2. **Set up TradingView Alerts** to send webhooks to `http://your-server:8000/api/v1/signals/webhook/tradingview`
3. **Enable Auto-Trading** (`ENABLE_AUTO_TRADING=true`) after thorough testing
4. **Deploy to Production** (see deployment guide)

---

## Test Checklist

- [ ] Backend health check responds
- [ ] All 6 exchanges show "healthy"
- [ ] Can create manual signal
- [ ] TradingView webhook works
- [ ] Sentiment analysis processes text
- [ ] News scraper fetches articles
- [ ] Risk calculator evaluates signals
- [ ] Dry-run trades execute successfully
- [ ] Dashboard loads without errors
- [ ] TradingView chart renders
- [ ] Signal feed auto-refreshes
- [ ] Trade history displays
- [ ] Sentiment dashboard updates
- [ ] API documentation accessible
- [ ] Database stores data correctly
- [ ] Logs show no critical errors

**Test Status: ____/15 Passed**

---

**Last Updated**: Phase 6 Completion  
**Documentation Version**: 1.0
