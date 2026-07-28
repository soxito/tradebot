# Conventions

## Core Sections (Required)

### 1) Language & Style

| Area | Convention | Evidence |
|------|------------|----------|
| Backend modules | `snake_case.py` | `backend/app/api/live_trade.py`, etc. |
| Backend classes | `PascalCase` | `TradingService`, `PluginLoader`, `ExchangeManager` |
| Backend functions | `snake_case`; private often `_prefix` | `_resolve_plugins_root`, `_maybe_start_ngrok` |
| Backend async | `async def` + SQLAlchemy `AsyncSession` | API routers use `Depends(get_db)` |
| Frontend pages | `kebab-case.tsx` under `pages/` | `jarvis-room.tsx`, `mt5-live.tsx` |
| Frontend components | `PascalCase.tsx` | `PaulChat.tsx`, `Layout.tsx` |
| Frontend hooks | `useX` camelCase | `useDeepgramAgent.ts`, `useWalletBalance.ts` |
| TS imports | Path alias `@/` → `src/` | `tsconfig.json` paths |
| Plugin packages | `*Plugin` directory + `plugin.json` | `plugins/MT5TradingPlugin/` |

### 2) API conventions

- Global API prefix: `/api/v1` (`settings.API_V1_PREFIX`).
- Each router declares its own `APIRouter(prefix="/…", tags=[…])`.
- Core routers aggregated in `backend/app/api/routes.py`.
- Plugins mount at the same `/api/v1` root (plugin routers typically add their own sub-prefix).
- Health: `GET /health` on app root (not under `/api/v1`).
- OpenAPI docs exposed only when `DEBUG` (`/docs`, `/redoc`).

### 3) Error handling & HTTP

- FastAPI `HTTPException` for auth/validation failures (`security.py`).
- Lifespan startup: DB/plugin failures often **logged and non-fatal** so the API can still boot (`main.py`).
- Plugin load: `strict_mode` false by default → failed plugins warn and continue; true → raise.
- Frontend: unhandled promise rejections hooked in `_app.tsx`; connection UX via `ConnectionStatus`.

### 4) Logging

- Library: **loguru** (`configure_logging` in `core/logging.py`, used throughout).
- Emoji-prefixed status messages common in startup (`🚀`, `⚠️`, etc.) — informal but consistent in `main.py`.
- Env-driven: `LOG_LEVEL`, `LOG_JSON`, `LOG_FILE_PATH`, rotation/retention in `.env.example`.

### 5) Configuration

- All runtime config via **environment variables** → `Settings` (`pydantic-settings`).
- Inline comments in `.env` stripped by validator (`strip_inline_env_comments`).
- Docker vs native hostname rewriting for `DATABASE_URL` / `REDIS_URL`.
- Secrets never committed; `.env` gitignored.

### 6) Plugin conventions

From manifests and loader:

- Required manifest fields: `name`, `slug`, `version`, `service_provider`.
- Optional: `description`, `author`, `requires`, `provides`, `permissions`, `settings_keys`.
- Service provider module must export `router`.
- Models module path convention for table init; SQLAlchemy declarative classes ending in `Base` expose `.metadata`.
- README rule: **plugins are standalone — do not modify core for plugin features**.

### 7) Frontend conventions

- Next.js **Pages Router** layout pattern: pages may attach custom layout; global shell uses `Layout`.
- Dynamic import for heavy widgets (`PaulChat`, extension prompt) with `{ ssr: false }`.
- Tailwind utility classes for dark trading UI (`bg-gray-900`, accent tokens like `tradebot-accent`).
- Lucide icons for navigation.
- TypeScript `strict: true`.

### 8) Safety conventions

- `ENABLE_AUTO_TRADING` defaults to **false**.
- Live loops log warnings if started while auto-trading disabled.
- TradingView webhooks require HMAC signature in production; may allow unsigned in non-production.
- API key auth exists (`X-API-Key` compared to `SECRET_KEY`) but is **not applied to all routes** — many endpoints use only `Depends(get_db)`.

### 9) Formatting / lint

| Surface | Tool | Evidence |
|---------|------|----------|
| Frontend | ESLint (`eslint-config-next`) | `frontend/package.json` `lint` script |
| Backend | No root ruff/black config found | scan / tree |
| TypeScript | `strict`, `forceConsistentCasingInFileNames` | `tsconfig.json` |

### 10) Evidence

- `backend/app/main.py`
- `backend/app/api/routes.py`
- `backend/app/core/security.py`
- `backend/app/plugins/loader.py` + `contracts.py`
- `frontend/tsconfig.json`
- `frontend/src/components/Layout.tsx`
- `frontend/src/pages/_app.tsx`
- `.env.example`
- `plugins/*/plugin.json`
- `README.md` (plugin isolation rule)
