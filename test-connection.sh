#!/bin/bash
# Auto Connection Test Script for TradeBot
# Tests backend connectivity, CORS setup, and data endpoints

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🧪 TradeBot Connection Test Suite"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

BACKEND_URL="http://localhost:8080"
FRONTEND_URL="http://localhost:3001"
FAILED=0

# Test function
test_endpoint() {
    local name="$1"
    local url="$2"
    local origin="$3"
    
    echo -n "Testing $name... "
    
    if [ -z "$origin" ]; then
        response=$(curl -s -w "\n%{http_code}" "$url" 2>/dev/null)
    else
        response=$(curl -s -w "\n%{http_code}" -H "Origin: $origin" "$url" 2>/dev/null)
    fi
    
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')
    
    if [ "$http_code" = "200" ]; then
        echo -e "${GREEN}✅ PASS${NC} (HTTP $http_code)"
        if [ ! -z "$origin" ]; then
            # Check for CORS headers
            cors_header=$(curl -s -I -H "Origin: $origin" "$url" 2>/dev/null | grep -i "access-control-allow-origin" || echo "")
            if [ ! -z "$cors_header" ]; then
                echo "   CORS header: $cors_header"
            else
                echo -e "   ${YELLOW}⚠️  No CORS header in response${NC}"
            fi
        fi
        return 0
    else
        echo -e "${RED}❌ FAIL${NC} (HTTP $http_code)"
        FAILED=$((FAILED + 1))
        return 1
    fi
}

echo "📡 Backend Connectivity Tests"
echo "────────────────────────────────────────"

# Test 1: Health Check
test_endpoint "Health Check" "$BACKEND_URL/health"

# Test 2: Root Endpoint
test_endpoint "Root Endpoint" "$BACKEND_URL/"

# Test 3: CORS Test Endpoint
test_endpoint "CORS Test" "$BACKEND_URL/cors-test"

# Test 4: API Status
test_endpoint "API Status" "$BACKEND_URL/api/v1/status"

echo ""
echo "🌐 CORS Configuration Tests"
echo "────────────────────────────────────────"

# Test 5: CORS from localhost:3001
test_endpoint "CORS from Frontend Origin" "$BACKEND_URL/api/v1/status" "$FRONTEND_URL"

# Test 6: Signals endpoint with CORS
test_endpoint "Signals with CORS" "$BACKEND_URL/api/v1/signals/?limit=5" "$FRONTEND_URL"

echo ""
echo "📊 Data Endpoint Tests"
echo "────────────────────────────────────────"

# Test 7: Bitget OHLCV Data
test_endpoint "Bitget BTC/USDT Data" "$BACKEND_URL/api/v1/exchanges/bitget/ohlcv/BTCUSDT?timeframe=1h&limit=5"

# Test 8: Sentiment Data
test_endpoint "Sentiment Data" "$BACKEND_URL/api/v1/sentiment/"

# Test 9: Signals Data
test_endpoint "Signals Data" "$BACKEND_URL/api/v1/signals/?limit=10"

# Test 10: Trade History
test_endpoint "Trade History" "$BACKEND_URL/api/v1/trading/history?limit=10"

echo ""
echo "🖥️  Frontend Connectivity Test"
echo "────────────────────────────────────────"

# Test 11: Frontend is running
test_endpoint "Frontend Homepage" "$FRONTEND_URL/"

echo ""
echo "🐳 Docker Container Status"
echo "────────────────────────────────────────"

containers=$(docker-compose ps --format json 2>/dev/null | jq -r '.Name + " | " + .State' | column -t -s "|" || echo "Unable to get container status")
echo "$containers"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ All tests passed!${NC}"
    echo ""
    echo "🎉 Your TradeBot is ready!"
    echo "   Frontend: $FRONTEND_URL"
    echo "   Backend:  $BACKEND_URL"
    echo "   API Docs: $BACKEND_URL/docs"
else
    echo -e "${RED}❌ $FAILED test(s) failed${NC}"
    echo ""
    echo "🔧 Troubleshooting:"
    echo "   1. Check if all containers are running: docker-compose ps"
    echo "   2. View backend logs: docker logs tradebot-backend"
    echo "   3. Verify CORS settings in .env: grep CORS_ORIGINS .env"
    echo "   4. See CORS_TEST.md for detailed troubleshooting"
    exit 1
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
