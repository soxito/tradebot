#!/bin/bash
# Test AI Provider Connections via API
# ====================================
# Tests each provider through the backend REST API

echo "╔════════════════════════════════════════════════════════════════════════════╗"
echo "║                    AI PROVIDER CONNECTION TEST                             ║"
echo "╚════════════════════════════════════════════════════════════════════════════╝"
echo ""

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"

# Check if backend is running
echo "🔍 Checking if backend is running..."
if ! curl -s -f "$BACKEND_URL/health" > /dev/null 2>&1; then
    echo "❌ Backend is not running at $BACKEND_URL"
    echo ""
    echo "Please start your backend first:"
    echo "  cd /Users/sakhilematsimela/Sites/tradebot"
    echo "  source .venv/bin/activate"
    echo "  python backend/main.py  # or your start command"
    echo ""
    exit 1
fi
echo "✅ Backend is running"
echo ""

# Reset circuit breakers
echo "🔄 Resetting circuit breakers..."
if curl -s -X POST "$BACKEND_URL/plugins/ai-analyst/llm/reset-circuits" > /tmp/reset_result.json 2>&1; then
    echo "✅ Circuit breakers reset"
    echo ""
    
    # Show enabled providers
    echo "📋 Enabled providers:"
    cat /tmp/reset_result.json | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data.get('enabled_providers', []):
    print(f\"   ✅ {p['label']} ({p['id']})\")
"
    echo ""
else
    echo "⚠️  Could not reset circuit breakers (endpoint may not be available yet)"
    echo ""
fi

# Get provider status
echo "🔍 Checking provider status..."
curl -s "$BACKEND_URL/plugins/ai-analyst/llm/providers" > /tmp/providers.json 2>&1

if [ -f /tmp/providers.json ]; then
    echo ""
    python3 -c "
import json, sys
try:
    with open('/tmp/providers.json') as f:
        providers = json.load(f)
    
    print('─' * 80)
    print('Provider Status:')
    print('─' * 80)
    
    for p in providers:
        status = '✅' if p['enabled'] else '⚠️'
        circuit = p.get('circuit', {})
        circuit_status = '🔴 OPEN' if circuit.get('open') else '✅ OK'
        
        print(f\"{status} {p['label']:20s} Circuit: {circuit_status}\")
        
        if circuit.get('open'):
            remaining = circuit.get('remaining_s', 0)
            reason = circuit.get('reason', 'Unknown')
            print(f\"   Reason: {reason}\")
            print(f\"   Retry in: {remaining}s\")
        
        if p.get('models') and '*' not in p['models']:
            models = p['models'][:2]
            print(f\"   Models: {', '.join(models)}...\")
        print()
    
    print('─' * 80)
    
except json.JSONDecodeError as e:
    print('❌ Error parsing provider status:', e)
    sys.exit(1)
"
else
    echo "❌ Could not fetch provider status"
    exit 1
fi

# Get overall status
echo ""
echo "📊 Overall System Status:"
curl -s "$BACKEND_URL/plugins/ai-analyst/status" | python3 -m json.tool 2>/dev/null || echo "Could not fetch system status"

echo ""
echo "─────────────────────────────────────────────────────────────────────────────"
echo ""
echo "✅ Test complete!"
echo ""
echo "To test a specific provider, try:"
echo "  curl -X POST $BACKEND_URL/plugins/ai-analyst/ai/chat \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"test\", \"model\": \"groq:llama-3.3-70b-versatile\"}'"
echo ""
