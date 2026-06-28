# CORS & Connection Testing Guide

## Quick Status Check

### ✅ CORS Configuration
- **Backend CORS Origins**: `http://localhost:3001`, `http://localhost:8080`, `http://localhost:3000`
- **Frontend URL**: `http://localhost:3001`
- **Backend URL**: `http://localhost:8080`

## Automatic Connection Test

The dashboard includes an **automatic connection test** that runs when you load the page:

1. **Open the dashboard**: http://localhost:3001
2. **Connection Status Banner** (top of page):
   - ✅ **Hidden** = Connection successful
   - ⚠️ **Yellow "Testing..."** = Connecting to API
   - 🔴 **Red "Failed"** = Connection error (with retry button)

The connection test:
- Runs automatically on page load
- Retries every 10 seconds if disconnected
- Shows real-time status
- Includes manual "Retry Connection" button

## Manual CORS Testing

### Test 1: Backend Health Check
```bash
curl http://localhost:8080/health
```

Expected output:
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "environment": "development"
}
```

### Test 2: CORS Test Endpoint
```bash
curl http://localhost:8080/cors-test
```

Expected output:
```json
{
  "status": "CORS is working!",
  "timestamp": "2026-04-10T12:16:32.099931",
  "allowed_origins": [
    "http://localhost:3001",
    "http://localhost:8080",
    "http://localhost:3000"
  ]
}
```

### Test 3: CORS Headers Test
```bash
curl -I -H "Origin: http://localhost:3001" http://localhost:8080/api/v1/status
```

Expected headers:
```
HTTP/1.1 200 OK
access-control-allow-origin: http://localhost:3001
access-control-allow-credentials: true
vary: Origin
```

### Test 4: Live Data Test (Bitget)
```bash
curl -s 'http://localhost:8080/api/v1/exchanges/bitget/ohlcv/BTCUSDT?timeframe=1h&limit=5' | jq '{exchange, symbol, count}'
```

Expected output:
```json
{
  "exchange": "bitget",
  "symbol": "BTCUSDT",
  "count": 5
}
```

## Browser Console Testing

Open browser DevTools (F12) on http://localhost:3001 and run:

```javascript
// Test 1: Fetch status
fetch('http://localhost:8080/api/v1/status')
  .then(r => r.json())
  .then(data => console.log('✅ Status:', data))
  .catch(err => console.error('❌ Error:', err));

// Test 2: CORS test
fetch('http://localhost:8080/cors-test')
  .then(r => r.json())
  .then(data => console.log('✅ CORS:', data))
  .catch(err => console.error('❌ Error:', err));
```

## Troubleshooting

### Error: "CORS header 'Access-Control-Allow-Origin' missing"

**Solution 1**: Verify CORS origins in `.env`
```bash
grep CORS_ORIGINS .env
```
Should show: `CORS_ORIGINS=http://localhost:3001,http://localhost:8080,http://localhost:3000`

**Solution 2**: Recreate backend container (required after .env changes)
```bash
docker-compose up -d --force-recreate --no-deps backend
```

**Solution 3**: Check backend logs for CORS configuration
```bash
docker logs tradebot-backend 2>&1 | grep "CORS origins"
```
Should show: `CORS origins: ['http://localhost:3001', 'http://localhost:8080', 'http://localhost:3000']`

### Error: "Failed to fetch" or "Network Error"

**Check 1**: Backend is running
```bash
docker-compose ps backend
```
Should show: `Up X seconds`

**Check 2**: Backend is accessible
```bash
curl http://localhost:8080/health
```

**Check 3**: Frontend is using correct API URL
Check `docker-compose.yml` under `frontend` service:
```yaml
NEXT_PUBLIC_API_URL: http://localhost:8080/api/v1
```

## Service Status

Check all services:
```bash
docker-compose ps
```

Expected output:
```
NAME                STATUS                         PORTS
tradebot-backend    Up X seconds                   0.0.0.0:8080->8000/tcp
tradebot-frontend   Up X seconds                   0.0.0.0:3001->3000/tcp
tradebot-postgres   Up X minutes (healthy)         0.0.0.0:5433->5432/tcp
tradebot-redis      Up X minutes (healthy)         0.0.0.0:6380->6379/tcp
```

## Connection Test Component

The dashboard includes `<ConnectionStatus />` component:

**Features**:
- Automatic connection test on mount
- Auto-retry every 10 seconds on failure
- Visual status indicator
- Manual retry button
- Last checked timestamp

**Location**: Top of dashboard page (fixed position)

**Behavior**:
- **Hidden** when connected (no interference with UI)
- **Yellow banner** while connecting
- **Red banner** when connection fails

## Quick Fix Commands

Restart backend (after .env changes):
```bash
docker-compose up -d --force-recreate --no-deps backend
```

Restart frontend:
```bash
docker-compose restart frontend
```

View backend logs:
```bash
docker logs -f tradebot-backend
```

View frontend logs:
```bash
docker logs -f tradebot-frontend
```

Check all container logs:
```bash
docker-compose logs -f
```
