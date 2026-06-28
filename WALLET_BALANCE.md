# 💰 Wallet Balance & P&L Tracking Guide

## ✅ Features Added

Your TradeBot dashboard now includes:

1. **Real-time Wallet Balance Display**
   - Shows balances from all configured exchanges
   - Displays each currency with available/used amounts
   - Auto-refreshes every 30 seconds

2. **Profit & Loss (P&L) Tracking**
   - Tracks total portfolio value
   - Shows P&L in USD and percentage
   - Persistent baseline (stored in browser localStorage)
   - Reset P&L button to set new baseline

3. **Multi-Exchange Support**
   - Displays balances from Bitget, Binance, Bybit, OKX, KuCoin, Coinbase
   - Only exchanges with configured API keys are shown
   - Clear status indicators (Connected/Error)

## 🔧 API Connection Status

### ✅ Fixed Issues

1. **API Import Error** - FIXED
   - Error: "can't access property 'getStatus', apiClient is undefined"
   - Solution: Updated import to use named export `{ apiClient }`
   - Changed `apiClient.getStatus()` to `apiClient.status()`
   - Added default export for backward compatibility

2. **Connection Testing** - WORKING
   - Auto-connection test runs on page load
   - Retries every 10 seconds if disconnected
   - Visual status banner shows connection state
   - All 11 connection tests passing ✅

## 📊 Dashboard Location

The Wallet Balance component is displayed at the top of the dashboard, just below the status cards:

```
┌─────────────────────────────────────────┐
│ Status Cards (API, Exchanges, etc.)    │
├─────────────────────────────────────────┤
│ 💰 Wallet Balance & P&L (NEW!)         │
│  - Total Balance: $X,XXX.XX             │
│  - Total P&L: +$XX.XX                   │
│  - P&L %: +X.XX%                        │
│  - Exchange Balances (Bitget, etc.)    │
├─────────────────────────────────────────┤
│ Trading Chart                           │
└─────────────────────────────────────────┘
```

## 🔑 API Permissions Required

### Bitget API Configuration

Your Bitget API key needs **spot trading read permissions**:

#### Current Error:
```
⚠️ API key needs "spot order read" permissions. 
Update in Bitget dashboard.
```

#### How to Fix:

1. **Login to Bitget**
   - Go to https://www.bitget.com

2. **Navigate to API Management**
   - Account → API Management
   - Find your API key: `bg_7f6acbc2930444c90a32dd0942cef9e6`

3. **Edit Permissions**
   - Click "Edit" or "Modify Permissions"
   - Enable: ☑️ **Spot** → **Read** (required)
   - Enable: ☑️ **Spot** → **Trade** (optional - only if auto-trading)
   - Disable: ☐ **Withdrawal** (recommended for security)

4. **IP Whitelist** (Recommended)
   - Add your server IP to whitelist
   - Enhances security

5. **Save & Test**
   - Save changes
   - Wait 1-2 minutes for propagation
   - Click "Refresh" button in dashboard
   - Balance should appear!

### Required Permissions Summary

| Permission | Required For | Status |
|------------|-------------|--------|
| Spot Read  | View balances, orders | ⚠️ **NEEDED** |
| Spot Trade | Place/cancel orders | Optional (for auto-trading) |
| Futures    | Futures trading | Not needed |
| Withdrawal | Withdraw funds | ❌ **NOT RECOMMENDED** |

## 🧪 Testing Wallet Balance

### 1. Manual API Test

Test if Bitget balance endpoint works:

```bash
curl -s http://localhost:8080/api/v1/exchanges/bitget/balance | jq
```

**Expected (after fixing permissions):**
```json
{
  "exchange": "bitget",
  "balance": {
    "USDT": {
      "free": 1000.50,
      "used": 0,
      "total": 1000.50
    },
    "BTC": {
      "free": 0.001234,
      "used": 0,
      "total": 0.001234
    }
  }
}
```

**Current Error:**
```json
{
  "detail": "bitget {\"code\":\"40014\",\"msg\":\"Incorrect permissions, need spot order read or spot order write permissions\"...}"
}
```

### 2. Check Exchange Status

```bash
curl -s http://localhost:8080/api/v1/exchanges/status | jq
```

**Current Status:**
```json
{
  "exchanges": {
    "bitget": {
      "initialized": true,
      "testnet": true,
      "healthy": true
    }
  },
  "initialized_count": 1
}
```

✅ Bitget is initialized and healthy - just needs correct permissions!

### 3. Frontend Dashboard

1. Open http://localhost:3001
2. Scroll to "Wallet Balance" section
3. You should see:
   - **If permissions OK**: Balance amounts and currencies
   - **If permissions missing**: "⚠️ API key needs spot order read permissions"

## 💡 Using the P&L Tracker

### First Time Setup

1. Open dashboard: http://localhost:3001
2. Fix API permissions (see above)
3. Wallet balance will auto-load
4. **Baseline is automatically set** to current balance
5. P&L starts tracking from this point

### P&L Features

**Total Balance Card** (Yellow)
- Shows current total portfolio value in USD

**Total P&L Card** (Green/Red)
- Green if profit, Red if loss
- Shows dollar amount difference from baseline
- Example: +$125.50 USD

**P&L % Card** (Green/Red)
- Shows percentage change
- Example: +12.55%

### Reset P&L Baseline

If you want to:
- Start fresh tracking
- Set new baseline after deposit/withdrawal
- Reset after a long period

**Click "Reset P&L" button**
- Sets current balance as new baseline
- P&L resets to $0.00 (0.00%)

## 🔄 Auto-Refresh

- Balances refresh every **30 seconds** automatically
- Click "🔄 Refresh" button for manual refresh
- Last update time shown at bottom

## 🌐 Multi-Exchange Support

To add more exchanges:

1. **Edit `.env` file:**
   ```bash
   # Binance
   BINANCE_API_KEY=your_binance_key
   BINANCE_API_SECRET=your_binance_secret
   
   # Bybit
   BYBIT_API_KEY=your_bybit_key
   BYBIT_API_SECRET=your_bybit_secret
   ```

2. **Restart backend:**
   ```bash
   docker-compose up -d --force-recreate --no-deps backend
   ```

3. **Check dashboard:**
   - All configured exchanges appear
   - Each shows separate balance
   - Total combines all exchanges

## 📱 Mobile Responsive

The wallet balance component is fully responsive:
- Desktop: 3-column grid for P&L cards
- Tablet: 2-column grid
- Mobile: Single column

## 🔐 Security Notes

⚠️ **IMPORTANT:**

1. **Never share API keys** - They're in `.env` (gitignored)
2. **Use testnet keys** for development
3. **Enable IP whitelisting** on exchange dashboards
4. **Disable withdrawal permissions** unless absolutely needed
5. **Never commit** `.env` file to version control

## 📋 Troubleshooting

### "No configured exchanges with API credentials"

**Problem:** No exchanges showing in wallet balance

**Solution:**
1. Check `.env` file has API keys
2. Restart backend: `docker-compose up -d --force-recreate --no-deps backend`
3. Check logs: `docker logs tradebot-backend | grep -i bitget`

### "API key needs spot order read permissions"

**Problem:** Permission error from exchange

**Solution:**
1. Login to exchange (Bitget, etc.)
2. API Management → Edit API → Enable "Spot Read" permission
3. Save and wait 1-2 minutes
4. Click "Refresh" in dashboard

### Balance shows $0.00

**Possible causes:**
1. No assets in exchange account
2. Only currencies with zero balance
3. API permissions issue

**Check:**
```bash
curl http://localhost:8080/api/v1/exchanges/bitget/balance
```

### P&L not updating

**Solution:**
- P&L baseline stored in browser localStorage
- Clear browser cache if needed
- Click "Reset P&L" to recalibrate

## ✅ Current Status

**API Connection:** ✅ Working
- All 11 tests passing
- CORS configured correctly
- Auto-connection testing enabled

**Wallet Balance:** ⚠️ Needs Permissions
- Component installed and working
- Auto-refresh enabled (30s)
- Waiting for Bitget API permissions

**Next Steps:**
1. Fix Bitget API permissions (add "Spot Read")
2. Balances will appear automatically
3. P&L tracking starts immediately

---

*Last Updated: 2026-04-10*
*Status: API connection working, awaiting exchange permissions*
