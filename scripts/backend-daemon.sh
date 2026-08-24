#!/bin/bash
# Entry point used by the com.tradebot.backend LaunchAgent.
#
# launchd starts this in its own session with a bare environment, so everything
# run-local.sh would have exported has to be rebuilt here. Keep this in sync
# with build_env()/start_backend_bg() in run-local.sh.
#
# Do NOT background anything: launchd tracks the process this script execs, and
# KeepAlive uses its exit to decide when to restart.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/backend/.venv"
BACKEND_PORT="${BACKEND_PORT:-1448}"

# launchd hands us a minimal PATH — homebrew tools (ngrok, node, redis-cli) are
# invoked by the app at runtime and must stay reachable.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Base env, then local overrides — same order run-local.sh uses.
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
if [[ -f "$ROOT/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.local"
  set +a
fi

export TZ=Africa/Johannesburg
export START_WORKERS_IN_API="true"
export AUTO_START_SCHEDULER="true"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "FATAL: $VENV/bin/uvicorn missing — run ./run-local.sh setup first" >&2
  exit 78  # EX_CONFIG: tells launchd this is unrecoverable, don't hot-loop
fi

cd "$ROOT/backend"

ARGS=(app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --loop asyncio)

# Reload defaults OFF, matching start.py: watching plugins/ re-imports
# torch/MLX/pandas on every save and orphans the MT5 auto_manage OS thread, and
# a supervised long-running service wants stability over hot-reload anyway.
# Opt in with TRADEBOT_RELOAD=1 — and then watch only app/, never plugins/.
# --timeout-graceful-shutdown stops reloads hanging forever on open SSE streams.
# After changing code, pick the new version up with:
#   launchctl kickstart -k gui/$(id -u)/com.tradebot.backend
if [[ "${TRADEBOT_RELOAD:-}" =~ ^(1|true|yes|on)$ ]]; then
  ARGS+=(--reload --reload-dir app --timeout-graceful-shutdown 5)
fi

exec "$VENV/bin/uvicorn" "${ARGS[@]}"
