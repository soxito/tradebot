#!/usr/bin/env bash
# Rebuild the downloadable JARVIS extension zip served by the frontend.
# Run this whenever you edit anything in jarvis-extension/.
#
# ⚠️  TIP: To also bump the version (so the auto-update banner fires for users),
#     use ./scripts/bump-extension.sh instead — it bumps manifest + backend and
#     rebuilds both the legacy and versioned zips in one step.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/jarvis-extension"
VERSION="$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")"
LEGACY="$ROOT/frontend/public/jarvis-extension.zip"
VERSIONED="$ROOT/frontend/public/jarvis-extension-v${VERSION}.zip"
rm -f "$LEGACY" "$VERSIONED"
zip -r -X "$LEGACY" . -x ".*" -x "__MACOSX" >/dev/null
cp "$LEGACY" "$VERSIONED"
echo "Built v${VERSION}:"
echo "  $LEGACY"
echo "  $VERSIONED"
unzip -l "$LEGACY"
