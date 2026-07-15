#!/usr/bin/env bash
# Setup OpenHuman for TradeBot
# Usage: bash scripts/setup-openhuman.sh
set -e

echo "[OpenHumanSetup] Installing OpenHuman desktop app via Homebrew..."

# Check brew
if ! command -v brew &>/dev/null; then
  echo "[OpenHumanSetup] ERROR: Homebrew not found. Install from https://brew.sh"
  exit 1
fi

brew tap tinyhumansai/core 2>/dev/null || true
brew install openhuman

echo "[OpenHumanSetup] OpenHuman installed. Launch it from Applications or run: open -a OpenHuman"
echo ""

# Install agentmemory (shared memory backend)
VENV="backend/.venv"
PYTHON="$VENV/bin/python"
if [ -d "$VENV" ]; then
  echo "[OpenHumanSetup] Installing agentmemory shared memory backend..."
  "$PYTHON" -m pip install --quiet agentmemory || echo "[OpenHumanSetup] WARNING: agentmemory install failed (optional)"
fi

echo "[OpenHumanSetup] Done!"
echo ""
echo "Next steps:"
echo "  1. Launch OpenHuman from Applications"
echo "  2. In OpenHuman Settings, set: memory.backend = \"agentmemory\""
echo "  3. In OpenHuman Settings → MCP Servers, paste the manifest from:"
echo "     GET http://localhost:1448/api/v1/plugins/openhuman/mcp/schema"
echo "  4. Start agentmemory: agentmemory serve"
