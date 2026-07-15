#!/usr/bin/env bash
# Setup Vibe-Trading for TradeBot
# Usage: bash scripts/setup-vibe-trading.sh
set -e

VENV="backend/.venv"
PYTHON="$VENV/bin/python"

if [ ! -d "$VENV" ]; then
  echo "[VibeTradingSetup] ERROR: backend/.venv not found. Run ./run-local.sh first."
  exit 1
fi

echo "[VibeTradingSetup] Installing vibe-trading-ai into $VENV..."
"$PYTHON" -m pip install --quiet "vibe-trading-ai>=0.1.11"

# Verify
if "$VENV/bin/vibe-trading" --version >/dev/null 2>&1; then
  echo "[VibeTradingSetup] vibe-trading CLI installed: $("$VENV/bin/vibe-trading" --version 2>&1 | head -1)"
else
  echo "[VibeTradingSetup] WARNING: vibe-trading CLI not found in PATH. May need shell restart."
fi

# Create agent/.env if it doesn't exist
AGENT_ENV="agent/.env"
if [ ! -f "$AGENT_ENV" ]; then
  if [ -f "agent/.env.example" ]; then
    echo "[VibeTradingSetup] Copying agent/.env.example → agent/.env"
    cp agent/.env.example "$AGENT_ENV"
    echo "[VibeTradingSetup] Edit $AGENT_ENV to set your LLM provider and API key."
  else
    echo "[VibeTradingSetup] No agent/.env.example found. Create agent/.env with your LLM config."
  fi
fi

echo "[VibeTradingSetup] Done! Start the sidecar: POST /api/v1/plugins/vibe-trading/status/start"
echo "[VibeTradingSetup] Or manually: vibe-trading serve --port 8899"
