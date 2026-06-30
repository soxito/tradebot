#!/bin/bash
# Start TradeBot Backend with AI Market Analyst Plugin
set -e

cd "$(dirname "$0")/.."

echo "╔════════════════════════════════════════════════════════════════════════════╗"
echo "║              Starting TradeBot Backend                                    ║"
echo "╚════════════════════════════════════════════════════════════════════════════╝"
echo ""

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "❌ Error: .env file not found!"
    echo "   Please create .env with your API keys"
    exit 1
fi

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "📌 Python version: $python_version"

# Check for required API keys
echo ""
echo "🔑 Checking API Keys..."
source .env

check_key() {
    key_name=$1
    if [ -z "${!key_name}" ]; then
        echo "   ⚠️  $key_name not set (provider will be disabled)"
    else
        echo "   ✅ $key_name configured"
    fi
}

check_key "OPENAI_API_KEY"
check_key "GROQ_API_KEY"
check_key "GOOGLE_API_KEY"
check_key "MISTRAL_API_KEY"
check_key "CEREBRAS_API_KEY"
check_key "OPENROUTER_API_KEY"

echo ""
echo "🚀 Starting backend on http://localhost:8000"
echo "   Press Ctrl+C to stop"
echo ""
echo "─────────────────────────────────────────────────────────────────────────────"
echo ""

# Navigate to backend directory and start
cd backend
uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
