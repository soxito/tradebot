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
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh              # deps + vendor + self-test
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --predownload # + default model
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --all         # + ALL models
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --test        # ONLY run the OHLCV/forecast self-test
#   bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --no-test     # skip the self-test
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

# ── self-test: prove OHLCV data + a forecast resolve end-to-end ──────────────
# Run from REPO_ROOT so `app.*` and `plugins.*` imports resolve. Uses the
# plugin's own forecast_service, which now falls back to a keyless public ccxt
# exchange when no exchange is configured — so this must NOT print
# "No OHLCV data available" on a fresh machine.
run_self_test() {
  echo "==> Self-test: fetching OHLCV + running a forecast (BTCUSDT 1h)..."
  ( cd "$REPO_ROOT" && "$PY" - <<'PYCODE'
import asyncio, sys, os
sys.path.insert(0, os.getcwd())                      # REPO_ROOT (for plugins.*)
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))  # backend/ (for app.*)

async def main() -> int:
    try:
        import ccxt  # noqa: F401
    except Exception as e:
        print(f"    FAIL: ccxt not importable in the backend venv: {e}")
        return 1

    from plugins.KronosForecastPlugin.backend.services import forecast_service as fs

    try:
        rows = await fs._fetch_ohlcv("binance", "BTCUSDT", "1h", 200)
        if not rows or len(rows) < 5:
            print("    FAIL: No OHLCV data available for this symbol/exchange.")
            print("          (public ccxt fallback returned nothing — check network access)")
            return 2
        last_close = rows[-1][4]
        print(f"    OK: fetched {len(rows)} OHLCV candles (last close ~ {last_close}).")

        try:
            resp = await fs.run_forecast("binance", "BTCUSDT", "1h")
            n = len(getattr(resp, "forecast", []) or [])
            engine = "Kronos model" if getattr(fs.kronos_engine, "available", False) else "heuristic fallback"
            print(f"    OK: forecast produced {n} future candles via {engine}.")
        except Exception as e:
            # Data works even if the model isn't installed; surface but don't hard-fail.
            print(f"    WARN: OHLCV works but run_forecast raised: {e}")

        print("==> Self-test PASSED: OHLCV data is available.")
        return 0
    finally:
        # Best-effort: close any core exchange clients so the test exits cleanly.
        try:
            from app.exchanges.manager import exchange_manager
            for ex_id in list(exchange_manager.get_all_exchanges()):
                ex = exchange_manager.get_exchange(ex_id)
                for attr in ("close", "close_all"):
                    fn = getattr(ex, attr, None)
                    if callable(fn):
                        res = fn()
                        if asyncio.iscoroutine(res):
                            await res
                        break
        except Exception:
            pass

raise SystemExit(asyncio.run(main()))
PYCODE
  )
}

# --test => run ONLY the self-test and exit
if [ "${1:-}" = "--test" ]; then
  run_self_test
  exit $?
fi

echo "==> Installing model dependencies into backend venv..."
"$PY" -m pip install -r "$PLUGIN_DIR/backend/requirements.txt"

# Ensure ccxt is present for the keyless public-OHLCV fallback (normally a core dep).
echo "==> Verifying ccxt is importable (required for OHLCV fallback)..."
if ! "$PY" -c "import ccxt" >/dev/null 2>&1; then
  echo "    ccxt missing — installing..."
  "$PY" -m pip install "ccxt>=4.0.0"
fi

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

# Run the end-to-end self-test unless explicitly skipped.
if [ "${1:-}" != "--no-test" ]; then
  run_self_test || {
    echo "!!  Self-test did not pass. Check network access / exchange reachability above."
    exit 1
  }
fi
