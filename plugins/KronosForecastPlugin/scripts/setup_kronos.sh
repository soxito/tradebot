#!/usr/bin/env bash
#
# Kronos Forecast Plugin — one-time setup
#
# 1. Installs the model dependencies (torch, einops, safetensors, huggingface_hub)
#    into the backend virtualenv.
# 2. Vendors the Kronos `model/` package (KronosTokenizer / Kronos / KronosPredictor)
#    from the upstream MIT-licensed repo into backend/vendor/model.
# 3. (optional) Pre-downloads model weights so the first forecast is fast.
#
# The plugin works WITHOUT this (it falls back to a heuristic forecast); running
# this script upgrades it to the real Kronos foundation model.
#
# Usage:
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh              # deps + vendor
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --predownload # + default model
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --all         # + ALL models
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"
VENDOR_DIR="$PLUGIN_DIR/backend/vendor"
KRONOS_REPO="https://github.com/shiyu-coder/Kronos.git"

# Resolve the backend python (venv preferred)
if [ -x "$REPO_ROOT/backend/.venv/bin/python3" ]; then
  PY="$REPO_ROOT/backend/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi
echo "==> Using Python: $PY"

echo "==> Installing model dependencies into backend venv..."
"$PY" -m pip install -r "$PLUGIN_DIR/backend/requirements.txt"

echo "==> Vendoring Kronos model package into $VENDOR_DIR/model ..."
mkdir -p "$VENDOR_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
git clone --depth 1 "$KRONOS_REPO" "$TMP_DIR/Kronos"
rm -rf "$VENDOR_DIR/model"
cp -R "$TMP_DIR/Kronos/model" "$VENDOR_DIR/model"
cp "$TMP_DIR/Kronos/LICENSE" "$VENDOR_DIR/KRONOS_LICENSE" 2>/dev/null || true
echo "    vendored: $(ls "$VENDOR_DIR/model")"

if [ "${1:-}" = "--predownload" ]; then
  echo "==> Pre-downloading the default model weights from Hugging Face..."
  "$PY" - <<'PYCODE'
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.getenv("VIRTUAL_ENV", "")), ""))
from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
print("Kronos available:", kronos_engine.available, "| error:", kronos_engine.load_error)
PYCODE
elif [ "${1:-}" = "--all" ]; then
  echo "==> Downloading ALL published Kronos models + tokenizers into the local cache..."
  "$PY" - <<'PYCODE'
from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
from plugins.KronosForecastPlugin.backend.config import KRONOS_MODELS
results = kronos_engine.download_all(KRONOS_MODELS)
for model_id, ok in results.items():
    print(("  installed " if ok else "  FAILED   ") + model_id)
ok = sum(1 for v in results.values() if v)
print(f"==> {ok}/{len(results)} Kronos models installed.")
PYCODE
fi

echo "==> Done. Restart the backend, then GET /api/v1/plugins/kronos/status"
echo "    Install every model later via: POST /api/v1/plugins/kronos/models/install-all"
