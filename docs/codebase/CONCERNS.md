# Concerns

## Core Sections (Required)

### 1) High-churn / fragile areas (git, last 90 days)

| Churn | Path | Risk |
|------:|------|------|
| 33 | `start.py` | Complex launcher (~4.4k lines) — hard to reason about; easy to break local UX |
| 18 | `backend/app/api/jarvis.py` | ~4.9k-line god module for assistant logic |
| 16 | `frontend/src/services/api.ts` | Large client surface; merge conflicts likely |
| 16 | `jarvis-extension/manifest.json` | Extension packaging / permissions churn |
| 14 | `plugins/MT5TradingPlugin/backend/router.py` | ~2.2k-line plugin router |
| 12 | `frontend/src/components/PaulChat.tsx` | Voice/chat complexity |
| 12 | `frontend/src/pages/mt5-live.tsx` | Live trading UI |
| 10 | `backend/app/core/config.py` | Settings sprawl |

### 2) Size / complexity hotspots

| File | Approx size | Concern |
|------|-------------|---------|
| `backend/app/api/jarvis.py` | ~4949 LOC | Multiple responsibilities in one router module |
| `start.py` | ~4441 LOC | Provisioning + process mgmt + tuning in one script |
| `plugins/MT5TradingPlugin/backend/router.py` | ~2213 LOC | Large HTTP surface |
| `frontend/src/pages/jarvis-room.tsx` | ~1784 LOC | Dense UI page |
| `frontend/src/services/api.ts` | ~1130 LOC | Monolithic API client |

### 3) Security risks

| Risk | Detail | Evidence |
|------|--------|----------|
| Weak/global API auth | `verify_api_key` compares `X-API-Key` to `SECRET_KEY`; many routes do not use it | `security.py`, API modules using only `get_db` |
| Default secrets in examples | `.env.example` placeholders; risk if copied to prod unchanged | `.env.example` |
| Secrets in working tree | Session files like `tradebot_telegram.session` appear in repo root; ensure not committed / leaked | tree listing |
| MT5 password encryption TODO | `password_encrypted=data.password,  # TODO: encrypt at rest` | `plugins/MT5TradingPlugin/backend/router.py:151` |
| Open network attack surface | Live trading + exchange keys on a LAN-reachable API without strong multi-user auth | architecture observation |
| Production webhook rules | Signature required only when `ENVIRONMENT == "production"` | `security.py` |
| Auto-trading safety | Live loop can be configured while `ENABLE_AUTO_TRADING` is false (warning only) | `workers/runtime.py` |

### 4) Technical debt (production code)

| Item | Location | Notes |
|------|----------|-------|
| Open positions not fully queried | `backend/app/trading/decision.py` `# TODO: Query open positions` | Decision quality / risk accuracy |
| MT5 password at rest | MT5 router TODO | Credential storage |
| Decision engine completeness | Roadmap still lists full automation / backtesting | `README.md` roadmap |
| Plugin permission strings | Declared in manifests; enforcement middleware completeness unclear | `plugin.json` permissions |

*(UI "TODO" columns in `jarvis-room.tsx` are product features for goals/todos, not tech debt markers.)*

### 5) CI / quality gates

- **CI does not match the project:** PHP + root npm/`composer` workflow vs Python FastAPI + `frontend/` Next app (`.github/workflows/ci.yml`).
- **No automated backend pytest in CI.**
- **Frontend has no unit tests** despite README mentioning `npm test`.
- ESLint exists for frontend; no backend formatter/linter config found at root.

### 6) Operational concerns

| Issue | Detail |
|-------|--------|
| Huge log files | Scan found multi-MB rotated logs under `backend/logs/` dominating "largest files" metrics |
| Metrics pollution | Scan `total LOC` inflated by `.venv` / site-packages / logs if not excluded carefully |
| Dual port schemes | Local `start.py` vs Docker ports differ (3000 vs 3001, 1448 vs 8080, 5434 vs 5433) — frequent CORS/misconfig source |
| Dual process model | API vs worker split requires correct `START_WORKERS_IN_API` or loops never run |
| Optional heavy deps | Face vision / Kronos / tradingagents can fail installs on some Python versions |
| Session/state files | `.backend.pid`, `.frontend.pid`, large logs at root — clutter for operators |

### 7) Intent vs reality divergences

| Intent (docs) | Reality (code/repo) |
|---------------|---------------------|
| README: backend Python 3.13 | Docker uses 3.12-slim; plugins require `>=3.12`; local may vary |
| README: `npm test` for frontend | No test script or test files found under frontend |
| CI implies PHP app | No PHP application source in this tree |
| README plugin list | Also ships OpenHuman, OpenManus, VibeTrading plugins not always listed in short feature bullets |
| "Never modify core for plugins" | Core still owns large JARVIS/voice surfaces used by the assistant experience |

### 8) Coverage gaps

- Limited tests for core trading execution, exchange adapters, EventBus, plugin loader.
- No load/performance suite detected.
- Security configs (Dependabot, SECURITY.md) not detected by scan.

### 9) Evidence

- Git high-churn section of `docs/codebase/.codebase-scan.txt`
- File line counts via `wc -l` on hotspots
- `backend/app/trading/decision.py` TODO
- `plugins/MT5TradingPlugin/backend/router.py` encrypt TODO
- `.github/workflows/ci.yml`
- `README.md` testing + roadmap sections
- `backend/app/core/security.py`
- `backend/app/workers/runtime.py`
