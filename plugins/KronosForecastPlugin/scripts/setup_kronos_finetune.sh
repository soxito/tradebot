#!/usr/bin/env bash
#
# Kronos Forecast Plugin — fine-tuning setup (macOS / Linux)
#
# Vendors the upstream `finetune_csv/` pipeline (shiyu-coder/Kronos, MIT) into
# backend/vendor/finetune_csv so the plugin can fine-tune Kronos on your own
# OHLCV data. Idempotent: skips if already vendored (pass --force to re-vendor).
#
# The main forecast setup (setup_kronos.sh) is a prerequisite — it installs
# torch and vendors the model/ package that the trainers import.
#
# Usage:
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos_finetune.sh
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos_finetune.sh --force
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"
VENDOR_DIR="$PLUGIN_DIR/backend/vendor"
FINETUNE_DEST="$VENDOR_DIR/finetune_csv"
KRONOS_REPO="https://github.com/shiyu-coder/Kronos.git"

if [ -x "$REPO_ROOT/backend/.venv/bin/python3" ]; then
  PY="$REPO_ROOT/backend/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi
echo "==> Using Python: $PY"

if [ "${1:-}" != "--force" ] && [ -f "$FINETUNE_DEST/train_sequential.py" ]; then
  echo "==> finetune_csv already vendored at $FINETUNE_DEST — skipping (use --force to re-vendor)."
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  echo "!!  Git not found — install Git, then re-run to enable Kronos fine-tuning." >&2
  exit 1
fi

echo "==> Ensuring PyYAML is installed (config loader dependency)..."
"$PY" -c "import yaml" >/dev/null 2>&1 || "$PY" -m pip install pyyaml

echo "==> Vendoring Kronos finetune_csv pipeline into $FINETUNE_DEST ..."
mkdir -p "$VENDOR_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
git clone --depth 1 "$KRONOS_REPO" "$TMP_DIR/Kronos"
rm -rf "$FINETUNE_DEST"
cp -R "$TMP_DIR/Kronos/finetune_csv" "$FINETUNE_DEST"
# Drop the bundled example CSV / images to keep the vendor tree light.
rm -rf "$FINETUNE_DEST/examples" "$FINETUNE_DEST/data" 2>/dev/null || true
echo "    vendored: $(ls "$FINETUNE_DEST")"

echo "==> Done. Fine-tuning is available via the Kronos page 'Finetune' tab and"
echo "    POST /api/v1/plugins/kronos/finetune/start."
