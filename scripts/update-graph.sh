#!/usr/bin/env bash
# update-graph.sh — Re-extract changed source files and refresh the knowledge graph.
# Run this any time you add, modify, or delete Python/TypeScript/frontend files.
#
# Usage:
#   ./scripts/update-graph.sh          # incremental (only changed files)
#   ./scripts/update-graph.sh --full   # full rebuild from scratch
#
# The graph is used by GitHub Copilot to answer architecture questions without
# reading raw source files — keeping token usage 60-95% lower.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GRAPHIFY="$HOME/.local/share/uv/tools/graphifyy/bin/graphify"
export PATH="$HOME/.local/share/uv/tools/graphifyy/bin:$HOME/.local/bin:$PATH"

if ! command -v graphify &>/dev/null && [ ! -x "$GRAPHIFY" ]; then
    echo "graphify not found — installing..."
    uv tool install "graphifyy[openai]" --quiet
fi

MODE="${1:-}"

if [ "$MODE" = "--full" ]; then
    echo "🔄  Full graph rebuild..."
    graphify . --no-viz
else
    echo "🔄  Incremental graph update (changed files only)..."
    graphify . --update --no-viz
fi

echo ""
echo "✅  Graph updated: graphify-out/graph.json"
echo "    Communities: $(python3 -c "import json; g=json.load(open('graphify-out/graph.json')); print(len(set(n.get('community','?') for n in g.get('nodes',[]))))" 2>/dev/null || echo '?')"
echo ""
echo "Tip: in Copilot Chat, type  graphify query \"<question>\"  to query the graph."
