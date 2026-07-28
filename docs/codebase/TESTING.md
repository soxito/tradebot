# Testing

## Core Sections (Required)

### 1) Frameworks

| Layer | Runner | Evidence |
|-------|--------|----------|
| Backend core | `pytest` + `pytest-asyncio` | `backend/requirements.txt`, `backend/tests/` |
| Plugins | `pytest` (colocated) | `plugins/*/tests/` |
| Frontend | **No unit test suite found** (`npm test` claimed in README but no test script/files in `package.json` tree) | `frontend/package.json` scripts: `dev`, `build`, `start`, `lint` only |
| Smoke / ops | Shell scripts | `test-connection.sh`, `scripts/test_*.py` |

No root `pytest.ini` / `pyproject.toml` detected; pytest discovery relies on defaults.

### 2) Test file locations

**Backend (`backend/tests/`):**

| File | Focus (from name / header) |
|------|----------------------------|
| `test_main.py` | Basic API / TestClient |
| `test_signal_decision_guards.py` | Decision safety guards |
| `test_rug_pull_sniper_cycle.py` | Rug-pull sniper cycle |
| `test_pump_monitor_retention.py` | Pump monitor retention |
| `test_deepgram_budget.py` | Deepgram spend caps |

Also ad-hoc scripts: `backend/test_signals.py`, `backend/sanity_test.py`, `backend/find_signals.py`.

**Plugins:**

| Plugin | Tests |
|--------|-------|
| AiMarketAnalyst | `test_llm_registry.py`, `test_llm_usage.py`, `test_provider_presets.py` |
| MT5TradingPlugin | `test_smc_strategy.py`, `test_scalp_strategy.py`, `conftest.py` |
| TelegramSignalNewsPlugin | `test_extractor.py`, `test_telegram_provider.py`, `test_ingest_helpers.py`, `conftest.py` |

Other plugins (AgentPaul, Kronos, Obsidian, OpenHuman, OpenManus, VibeTrading): **no `tests/` directory found**.

### 3) How to run

```bash
# Core backend
cd backend
pytest

# Specific plugin package (from repo root; import paths may require PYTHONPATH=.)
pytest plugins/AiMarketAnalyst/tests
pytest plugins/MT5TradingPlugin/tests
pytest plugins/TelegramSignalNewsPlugin/tests

# Connection smoke (running stack required)
./test-connection.sh

# Provider / routing diagnostics
scripts/test_ai_providers.py
scripts/test_providers_direct.py
scripts/verify_routing_fix.py
```

### 4) Patterns observed

- FastAPI `TestClient` used in `test_main.py`.
- Plugin tests import concrete service modules (e.g. `get_providers(force_reload=True)`).
- Domain-focused unit tests around risk/guards and strategy logic rather than full exchange E2E.
- No enforced coverage threshold found in config files.

### 5) CI status

`.github/workflows/ci.yml` currently runs:

- PHP 8.3 setup + `composer install` + `composer test`
- Node 22 + `npm ci` + `npm run build` **at repository root**

There is **no root `composer.json` / root `package.json`** in this monorepo layout (frontend lives under `frontend/`). So the committed CI workflow appears **misaligned with the actual stack** (leftover PHP/Node monorepo template). Backend `pytest` is not invoked in that workflow.

### 6) Manual / product verification

Documented in README:

- Dashboard connection banner auto-test
- Deepgram fallback checklist
- CORS troubleshooting (`CORS_TEST.md`)
- Live OHLCV curl against Bitget

### 7) Evidence

- `backend/tests/*`
- `plugins/*/tests/*`
- `backend/requirements.txt` (pytest deps)
- `frontend/package.json` (no test script)
- `.github/workflows/ci.yml`
- `test-connection.sh`
- `README.md` testing section
- `TESTING.md` (repo root — operational testing notes; separate from this file)
