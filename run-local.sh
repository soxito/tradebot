#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════
# run-local.sh — Run tradebot with your choice of DB backend
#
#   ./run-local.sh start             Interactive prompt: brew or docker?
#   ./run-local.sh start --brew      Postgres+Redis via Homebrew
#   ./run-local.sh start --docker    Postgres+Redis via Docker containers
#   ./run-local.sh stop              Stop everything
#
# Backend (FastAPI) and Frontend (Next.js) always run natively.
# ══════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ── Colours ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ── Paths ──
PYTHON="${PYTHON:-/opt/homebrew/bin/python3}"
VENV="$ROOT/backend/.venv"
PG_BIN="/opt/homebrew/opt/postgresql@16/bin"
DB_MODE_FILE="$ROOT/.db-mode"  # persists last chosen mode
BACKEND_PORT="${BACKEND_PORT:-1448}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"

# ══════════════════════════════════════════════════════════
# Usage
# ══════════════════════════════════════════════════════════
usage() {
  echo ""
  echo -e "${BOLD}TradeBot Local Runner${NC}"
  echo ""
  echo -e "${CYAN}Usage:${NC}"
  echo "  $0 start [--brew|--docker]   Start everything (pick DB mode)"
  echo "  $0 stop                      Stop backend + frontend + DB"
  echo "  $0 backend [--brew|--docker] Start backend only"
  echo "  $0 frontend                  Start frontend only"
  echo "  $0 db [--brew|--docker]      Start Postgres + Redis only"
  echo "  $0 setup                     One-time: venv, pip, npm install"
  echo "  $0 logs                      Tail backend log"
  echo "  $0 status                    Show what's running"
  echo "  $0 migrate                   Copy data: Docker postgres → brew postgres"
  echo ""
  echo -e "${YELLOW}DB Modes:${NC}"
  echo "  --brew     Homebrew services (pg port 5434, redis port 6379)"
  echo "  --docker   Docker containers  (pg port 5433, redis port 6380)"
  echo ""
  exit 1
}

# ══════════════════════════════════════════════════════════
# Mode selection
# ══════════════════════════════════════════════════════════
resolve_mode() {
  local mode="${1:-}"

  # Explicit flag
  if [[ "$mode" == "--brew" ]]; then echo "brew"; return; fi
  if [[ "$mode" == "--docker" ]]; then echo "docker"; return; fi

  # Saved from last run
  if [[ -f "$DB_MODE_FILE" ]]; then
    local saved
    saved=$(cat "$DB_MODE_FILE")
    echo -e "${YELLOW}Last used: ${BOLD}$saved${NC}" >&2
  fi

  # Interactive prompt
  echo "" >&2
  echo -e "${BOLD}Where should Postgres + Redis run?${NC}" >&2
  echo "" >&2
  echo -e "  ${GREEN}1)${NC} ${BOLD}Homebrew${NC}  — brew services (native, no Docker needed)" >&2
  echo -e "  ${CYAN}2)${NC} ${BOLD}Docker${NC}    — docker compose containers" >&2
  echo "" >&2
  read -rp "Choose [1/2]: " choice
  case "$choice" in
    1|brew)   echo "brew" ;;
    2|docker) echo "docker" ;;
    *)
      echo -e "${RED}Invalid choice${NC}" >&2
      exit 1
      ;;
  esac
}

save_mode() {
  echo "$1" > "$DB_MODE_FILE"
}

# ══════════════════════════════════════════════════════════
# DB management
# ══════════════════════════════════════════════════════════
ensure_db_brew() {
  echo -e "${CYAN}▶ Starting Postgres + Redis (Homebrew)...${NC}"
  brew services start postgresql@16 2>/dev/null || true

  # Ensure Redis is installed before trying to start it.
  if ! command -v redis-server &>/dev/null && ! brew list redis &>/dev/null 2>&1; then
    echo -e "${YELLOW}  Redis not found — installing via Homebrew...${NC}"
    brew install redis || {
      echo -e "${RED}  ✗ brew install redis failed — install it manually then re-run.${NC}"
      exit 1
    }
  fi
  brew services start redis 2>/dev/null || true

  # Wait for PG
  for _ in {1..15}; do
    "$PG_BIN/pg_isready" -p 5434 -q 2>/dev/null && break
    sleep 1
  done
  if ! "$PG_BIN/pg_isready" -p 5434 -q 2>/dev/null; then
    echo -e "${RED}✗ PostgreSQL failed to start on port 5434${NC}"
    echo "  Check: cat /opt/homebrew/var/log/postgresql@16.log"
    exit 1
  fi

  # Wait for Redis
  for _ in {1..10}; do
    redis-cli ping &>/dev/null && break
    sleep 1
  done
  if ! redis-cli ping &>/dev/null; then
    echo -e "${RED}✗ Redis not responding${NC}"
    exit 1
  fi

  echo -e "${GREEN}✓ Postgres (localhost:5434) + Redis (localhost:6379)${NC}"
}

ensure_db_docker() {
  echo -e "${CYAN}▶ Starting Postgres + Redis (Docker)...${NC}"
  if ! docker info &>/dev/null; then
    echo -e "${RED}✗ Docker is not running. Start Docker Desktop or use --brew instead.${NC}"
    exit 1
  fi

  docker compose -f docker-compose.db.yml up -d

  # Wait for PG
  echo -n "  Waiting for Postgres..."
  for _ in {1..30}; do
    if docker compose -f docker-compose.db.yml exec -T postgres pg_isready -U tradebot -q 2>/dev/null; then
      break
    fi
    echo -n "."
    sleep 1
  done
  echo ""
  if ! docker compose -f docker-compose.db.yml exec -T postgres pg_isready -U tradebot -q 2>/dev/null; then
    echo -e "${RED}✗ PostgreSQL container failed to start${NC}"
    docker compose -f docker-compose.db.yml logs postgres --tail 10
    exit 1
  fi

  # Wait for Redis
  for _ in {1..10}; do
    docker compose -f docker-compose.db.yml exec -T redis redis-cli ping &>/dev/null && break
    sleep 1
  done

  echo -e "${GREEN}✓ Postgres (localhost:5433) + Redis (localhost:6380)${NC}"
}

ensure_db() {
  local mode="$1"
  if [[ "$mode" == "docker" ]]; then
    ensure_db_docker
  else
    ensure_db_brew
  fi
}

stop_db() {
  local mode="${1:-}"
  if [[ -z "$mode" && -f "$DB_MODE_FILE" ]]; then
    mode=$(cat "$DB_MODE_FILE")
  fi

  if [[ "$mode" == "docker" ]]; then
    echo -e "${CYAN}  Stopping Docker Postgres + Redis...${NC}"
    docker compose -f docker-compose.db.yml down 2>/dev/null || true
    echo -e "${GREEN}  ✓ Docker DB stopped${NC}"
  elif [[ "$mode" == "brew" ]]; then
    echo -e "${CYAN}  Stopping Homebrew Postgres + Redis...${NC}"
    brew services stop postgresql@16 2>/dev/null || true
    brew services stop redis 2>/dev/null || true
    echo -e "${GREEN}  ✓ Brew DB stopped${NC}"
  else
    # Try both
    brew services stop postgresql@16 2>/dev/null || true
    brew services stop redis 2>/dev/null || true
    docker compose -f docker-compose.db.yml down 2>/dev/null || true
    echo -e "${GREEN}  ✓ DB stopped${NC}"
  fi
}

# ══════════════════════════════════════════════════════════
# Environment
# ══════════════════════════════════════════════════════════
build_env() {
  local mode="$1"

  # Load base .env
  if [[ -f "$ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
  fi

  # Load mode-specific overrides
  if [[ "$mode" == "docker" ]]; then
    if [[ -f "$ROOT/.env.docker" ]]; then
      set -a
      # shellcheck disable=SC1091
      source "$ROOT/.env.docker"
      set +a
    fi
  else
    if [[ -f "$ROOT/.env.local" ]]; then
      set -a
      # shellcheck disable=SC1091
      source "$ROOT/.env.local"
      set +a
    fi
  fi

  export TZ=Africa/Johannesburg
}

# ══════════════════════════════════════════════════════════
# Python venv
# ══════════════════════════════════════════════════════════
ensure_venv() {
  if [[ ! -d "$VENV" ]]; then
    echo -e "${YELLOW}⚙ Creating Python venv...${NC}"
    "$PYTHON" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
}

# ══════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════
setup() {
  echo -e "${CYAN}▶ One-time setup...${NC}"

  ensure_venv
  pip install --upgrade pip
  pip install -r "$ROOT/backend/requirements.txt"
  python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab'); nltk.download('stopwords'); nltk.download('vader_lexicon')" 2>/dev/null || true

  # Kronos ML forecaster — vendor the model + install torch (one-time).
  # Idempotent + best-effort: the plugin falls back to a heuristic forecast if
  # this is skipped or fails. Opt out with TRADEBOT_SKIP_KRONOS_SETUP=1.
  local kronos_setup="$ROOT/plugins/KronosForecastPlugin/scripts/setup_kronos.sh"
  local kronos_model="$ROOT/plugins/KronosForecastPlugin/backend/vendor/model"
  if [[ "${TRADEBOT_SKIP_KRONOS_SETUP:-}" =~ ^(1|true|yes|on)$ ]]; then
    echo -e "${YELLOW}  ⏭ Kronos setup skipped (TRADEBOT_SKIP_KRONOS_SETUP set)${NC}"
  elif [[ -d "$kronos_model" && -n "$(ls -A "$kronos_model" 2>/dev/null)" ]]; then
    echo -e "${GREEN}  ✓ Kronos model already set up (vendored)${NC}"
  elif [[ -f "$kronos_setup" ]]; then
    echo -e "${CYAN}  ▶ Setting up Kronos ML forecaster (torch + vendored model)...${NC}"
    bash "$kronos_setup" --no-test || \
      echo -e "${YELLOW}  ! Kronos setup did not complete — heuristic fallback stays active${NC}"
  fi

  cd "$ROOT/frontend"
  npm install
  cd "$ROOT"

  echo -e "${GREEN}✓ Setup complete${NC}"
}

# ══════════════════════════════════════════════════════════
# Backend / Frontend
# ══════════════════════════════════════════════════════════
start_backend() {
  local mode="$1"
  ensure_venv
  build_env "$mode"
  export START_WORKERS_IN_API="true"
  export AUTO_START_SCHEDULER="true"
  echo -e "${CYAN}▶ Starting backend (FastAPI) on port ${BACKEND_PORT}...${NC}"
  cd "$ROOT/backend"
  exec uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload
}

start_backend_bg() {
  local mode="$1"
  ensure_venv
  build_env "$mode"
  export START_WORKERS_IN_API="true"
  export AUTO_START_SCHEDULER="true"
  echo -e "${CYAN}▶ Starting backend (FastAPI) on port ${BACKEND_PORT} (background)...${NC}"
  cd "$ROOT/backend"
  # Detach into a session of its own. nohup alone only blocks SIGHUP — the
  # process stays in this shell's process group and dies with the terminal.
  # --reload-dir keeps the watcher off data/ and logs/; --timeout-graceful-shutdown
  # stops reloads hanging forever on open SSE streams.
  # macOS has no setsid(1), so call setsid(2) via python and exec over it —
  # execv keeps the pid, so $! below is still the uvicorn process.
  nohup "$VENV/bin/python" -c \
    'import os,sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
    "$VENV/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
    --loop asyncio --reload --reload-dir app --reload-dir ../plugins \
    --timeout-graceful-shutdown 5 \
    > "$ROOT/backend.log" 2>&1 &
  echo $! > "$ROOT/.backend.pid"
  disown 2>/dev/null || true
  echo -e "${GREEN}✓ Backend PID $(cat "$ROOT/.backend.pid") — log: backend.log${NC}"
}

start_frontend() {
  build_env "${1:-brew}"
  echo -e "${CYAN}▶ Starting frontend (Next.js) on port ${FRONTEND_PORT}...${NC}"
  cd "$ROOT/frontend"
  export NEXT_PUBLIC_API_URL="http://localhost:${BACKEND_PORT}/api/v1"
  exec npm run dev -- -p "$FRONTEND_PORT"
}

start_frontend_bg() {
  build_env "${1:-brew}"
  echo -e "${CYAN}▶ Starting frontend (Next.js) on port ${FRONTEND_PORT} (background)...${NC}"
  cd "$ROOT/frontend"
  export NEXT_PUBLIC_API_URL="http://localhost:${BACKEND_PORT}/api/v1"
  nohup npm run dev -- -p "$FRONTEND_PORT" > "$ROOT/frontend.log" 2>&1 &
  echo $! > "$ROOT/.frontend.pid"
  echo -e "${GREEN}✓ Frontend PID $(cat "$ROOT/.frontend.pid") — log: frontend.log${NC}"
}

# ══════════════════════════════════════════════════════════
# Stop
# ══════════════════════════════════════════════════════════
stop_all() {
  echo -e "${CYAN}▶ Stopping services...${NC}"
  for pidfile in "$ROOT/.backend.pid" "$ROOT/.frontend.pid"; do
    if [[ -f "$pidfile" ]]; then
      pid=$(cat "$pidfile")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        echo -e "${YELLOW}  Stopped PID $pid${NC}"
      fi
      rm -f "$pidfile"
    fi
  done
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  pkill -f "next dev" 2>/dev/null || true
  echo -e "${GREEN}✓ Backend + Frontend stopped${NC}"

  read -rp "Also stop Postgres + Redis? [y/N] " yn
  case "$yn" in
    [yY]) stop_db ;;
  esac
}

# ══════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════
show_status() {
  echo ""
  echo -e "${BOLD}TradeBot Status${NC}"
  echo -e "────────────────────────────────────"

  # DB mode
  if [[ -f "$DB_MODE_FILE" ]]; then
    echo -e "  DB mode:   ${BOLD}$(cat "$DB_MODE_FILE")${NC}"
  else
    echo -e "  DB mode:   ${YELLOW}not set${NC}"
  fi

  # Backend
  if [[ -f "$ROOT/.backend.pid" ]] && kill -0 "$(cat "$ROOT/.backend.pid")" 2>/dev/null; then
    echo -e "  Backend:   ${GREEN}running${NC} (PID $(cat "$ROOT/.backend.pid"))"
  elif pgrep -f "uvicorn app.main:app" &>/dev/null; then
    echo -e "  Backend:   ${GREEN}running${NC}"
  else
    echo -e "  Backend:   ${RED}stopped${NC}"
  fi

  # Frontend
  if [[ -f "$ROOT/.frontend.pid" ]] && kill -0 "$(cat "$ROOT/.frontend.pid")" 2>/dev/null; then
    echo -e "  Frontend:  ${GREEN}running${NC} (PID $(cat "$ROOT/.frontend.pid"))"
  elif pgrep -f "next dev" &>/dev/null; then
    echo -e "  Frontend:  ${GREEN}running${NC}"
  else
    echo -e "  Frontend:  ${RED}stopped${NC}"
  fi

  # Postgres
  local pg_status="${RED}stopped${NC}"
  if "$PG_BIN/pg_isready" -p 5434 -q 2>/dev/null; then
    pg_status="${GREEN}running${NC} (brew, port 5434)"
  fi
  if docker compose -f docker-compose.db.yml exec -T postgres pg_isready -U tradebot -q 2>/dev/null; then
    pg_status="${GREEN}running${NC} (docker, port 5433)"
  fi
  echo -e "  Postgres:  $pg_status"

  # Redis
  local redis_status="${RED}stopped${NC}"
  if redis-cli ping &>/dev/null 2>&1; then
    redis_status="${GREEN}running${NC} (port 6379)"
  fi
  if redis-cli -p 6380 ping &>/dev/null 2>&1; then
    redis_status="${GREEN}running${NC} (docker, port 6380)"
  fi
  echo -e "  Redis:     $redis_status"

  echo ""
}

# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════
CMD="${1:-}"
FLAG="${2:-}"

case "$CMD" in
  setup)    setup ;;
  status)   show_status ;;

  db)
    MODE=$(resolve_mode "$FLAG")
    save_mode "$MODE"
    ensure_db "$MODE"
    ;;

  backend)
    MODE=$(resolve_mode "$FLAG")
    save_mode "$MODE"
    ensure_db "$MODE"
    start_backend "$MODE"
    ;;

  frontend)
    start_frontend "$FLAG"
    ;;

  start)
    MODE=$(resolve_mode "$FLAG")
    save_mode "$MODE"
    ensure_db "$MODE"
    sleep 2
    start_backend_bg "$MODE"
    sleep 3
    start_frontend_bg "$MODE"
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════${NC}"
    echo -e "${GREEN}  DB mode:  ${BOLD}$MODE${NC}"
    echo -e "${GREEN}  Backend:  http://localhost:${BACKEND_PORT}${NC}"
    echo -e "${GREEN}  API:      http://localhost:${BACKEND_PORT}/api/v1${NC}"
    echo -e "${GREEN}  Frontend: http://localhost:${FRONTEND_PORT}${NC}"
    echo -e "${GREEN}═══════════════════════════════════════${NC}"
    echo -e "${YELLOW}  Logs: tail -f backend.log frontend.log${NC}"
    echo -e "${YELLOW}  Stop: ./run-local.sh stop${NC}"
    ;;

  stop)     stop_all ;;
  logs)     tail -f "$ROOT/backend.log" ;;

  migrate)
    echo -e "${CYAN}▶ Migrating data: Docker postgres (5433) → Brew postgres (5434)...${NC}"
    PGPASSWORD=tradebot_password "$PG_BIN/pg_dump" -h localhost -p 5433 -U tradebot tradebot \
      | "$PG_BIN/psql" -h localhost -p 5434 -U tradebot tradebot
    echo -e "${GREEN}✓ Migration complete${NC}"
    ;;

  *)        usage ;;
esac
