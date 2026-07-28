# Technology Stack

## Core Sections (Required)

### 1) Runtime Summary

| Area | Value | Evidence |
|------|-------|----------|
| Primary language (backend) | Python 3.11+ (local); Docker image `python:3.12-slim`; README cites 3.13 | `README.md`, `backend/Dockerfile`, plugin `requires.python: ">=3.12"` |
| Primary language (frontend) | TypeScript / React 19 | `frontend/package.json`, `frontend/tsconfig.json` |
| Package managers | `pip` (backend), `npm` (frontend) | `backend/requirements.txt`, `frontend/package-lock.json` |
| Module/build system | FastAPI + uvicorn; Next.js 16 (Pages Router) | `backend/app/main.py`, `frontend/package.json` scripts |
| Orchestration | Docker Compose (postgres, redis, backend, worker, frontend) | `docker-compose.yml` |
| Local launcher | `start.py` / `run-local.sh` / `start.bat` | repo root |

### 2) Production Frameworks and Dependencies

| Dependency | Version | Role in system | Evidence |
|------------|---------|----------------|----------|
| fastapi | 0.115.0 | HTTP API framework | `backend/requirements.txt` |
| uvicorn[standard] | 0.34.0 | ASGI server | `backend/requirements.txt` |
| pydantic / pydantic-settings | 2.11.0 / 2.9.1 | Settings + request validation | `backend/requirements.txt`, `backend/app/core/config.py` |
| sqlalchemy | 2.0.41 | Async ORM | `backend/requirements.txt`, `backend/app/models/database.py` |
| asyncpg / aiosqlite / psycopg2-binary | 0.30.0 / 0.22.1 / 2.9.10 | Postgres + SQLite drivers | `backend/requirements.txt` |
| redis | 5.2.1 | Cache + EventBus pub/sub | `backend/requirements.txt`, `backend/app/core/events.py` |
| ccxt | 4.4.86 | Multi-exchange trading API | `backend/requirements.txt` |
| sse-starlette | 2.1.3 | Server-Sent Events | `backend/requirements.txt` |
| loguru | 0.7.3 | Logging | `backend/requirements.txt` |
| prometheus-client | 0.21.1 | Metrics | `backend/requirements.txt` |
| openai | 1.82.0 | LLM client (AI analyst / agents) | `backend/requirements.txt` |
| textblob / vaderSentiment / nltk | various | Sentiment NLP | `backend/requirements.txt` |
| next | ^16.2.3 | Frontend framework | `frontend/package.json` |
| react / react-dom | ^19.1.0 | UI runtime | `frontend/package.json` |
| axios / swr | ^1.7.2 / ^2.2.5 | HTTP + data fetching | `frontend/package.json` |
| zustand | ^4.5.2 | Client state | `frontend/package.json` |
| lightweight-charts | ^4.1.3 | Trading charts | `frontend/package.json` |
| three / react-force-graph-* | various | 3D JARVIS room + graphs | `frontend/package.json` |
| recharts | ^2.12.7 | Dashboard charts | `frontend/package.json` |
| @deepgram/agents | ^0.1.1 | Voice agent client | `frontend/package.json` |
| tailwindcss | ^3.4.4 | Styling | `frontend/package.json` devDeps |

Optional / best-effort (not hard core deps): face vision (`mediapipe`, `opencv`, `face_recognition`), `headroom-ai`, `tradingagents` — installed via scripts / `start.py` when available.

### 3) Development Toolchain

| Tool | Purpose | Evidence |
|------|---------|----------|
| pytest / pytest-asyncio | Backend unit tests | `backend/requirements.txt`, `backend/tests/` |
| eslint + eslint-config-next | Frontend lint | `frontend/package.json` |
| typescript | Typecheck | `frontend/tsconfig.json` (`strict: true`) |
| GitHub Actions | CI workflow present | `.github/workflows/ci.yml` |
| Docker / Docker Compose | Containerized run | `docker-compose.yml`, Dockerfiles |

### 4) Key Commands

```bash
# Recommended local all-in-one
python start.py          # or ./run-local.sh / start.bat
python start.py --status
python start.py --stop

# Docker
docker-compose up --build

# Backend only (typical after start.py venv)
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 1448  # port may vary; Docker uses 8000→8080

# Frontend only
cd frontend && npm install && npm run dev

# Tests
cd backend && pytest
# Plugin tests live under plugins/*/tests/

# Frontend lint / build
cd frontend && npm run lint && npm run build

# Connection smoke test
./test-connection.sh
```

### 5) Environment and Config

- Config sources: `.env` (gitignored), `.env.example`, `backend/app/core/config.py` (`pydantic-settings` `Settings`)
- Required / important env vars: `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, exchange keys (`BINANCE_*`, `BITGET_*`, …), `TRADINGVIEW_WEBHOOK_SECRET`, `ENABLE_AUTO_TRADING` (default false), plugin vars (`MT5_*`, `AI_ANALYST_*`, `OBSIDIAN_*`), Deepgram budget vars (see README)
- Deployment/runtime constraints:
  - Compose maps backend `8000→8080`, frontend `3000→3001`, Postgres `5432→5433`, Redis `6379→6380`
  - Local `start.py` uses different ports (frontend 3000, backend often 1448, Postgres 5434) — see README table
  - Config rewrites Docker hostnames (`postgres`/`redis`) to localhost ports when running natively (`_reroute_unresolvable_host` in `config.py`)
  - Worker process: `python -m app.workers.runner` with `START_WORKERS_IN_API=true` in compose

### 6) Evidence

- `backend/requirements.txt`
- `frontend/package.json`
- `backend/Dockerfile`, `frontend/Dockerfile`
- `docker-compose.yml`
- `.env.example`
- `backend/app/core/config.py`
- `README.md`

## Extended Sections

### Infrastructure images

| Service | Image / base | Evidence |
|---------|--------------|----------|
| postgres | `postgres:16-alpine` | `docker-compose.yml` |
| redis | `redis:7-alpine` | `docker-compose.yml` |
| backend/worker | `python:3.12-slim` | `backend/Dockerfile` |
| frontend | `node:22-alpine` | `frontend/Dockerfile` |

### Timezone

Default app TZ in compose: `Africa/Johannesburg` (`docker-compose.yml`).
