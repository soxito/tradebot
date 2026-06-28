---
description: "Use when: creating or editing files in plugins/ folder, building plugin services, plugin migrations, plugin models, plugin routes, plugin components. Enforces standalone plugin architecture rules."
applyTo: "plugins/**"
---

# Plugin Development Conventions

## Absolute Rules

1. **NEVER modify core files** — `backend/app/` and `frontend/src/` are read-only
2. **NEVER delete existing files** — Only create new or append at safe anchors
3. **Prefix all DB tables** with plugin slug (e.g., `mt5_`, `ai_`)
4. **No foreign key constraints** across plugin ↔ core boundaries — reference by ID only
5. **Plugin must be removable** with zero side effects on core
6. **All external API calls** go through async jobs, NEVER in request handlers
7. **Validate all AI outputs** server-side against a strict schema before use

## File Organization

```
plugins/{PluginName}/
├── plugin.json            # Manifest (required)
├── backend/
│   ├── __init__.py
│   ├── router.py          # FastAPI APIRouter → /api/v1/plugins/{slug}/
│   ├── models.py          # SQLAlchemy models
│   ├── schemas.py         # Pydantic schemas
│   ├── services/          # Business logic
│   ├── migrations/        # Alembic migrations
│   └── config.py          # Plugin-scoped settings
├── frontend/
│   ├── pages/             # Next.js pages
│   ├── components/        # React components
│   ├── hooks/             # Custom hooks
│   └── store/             # Zustand store slices
├── docs/                  # Plugin docs
└── tests/                 # Plugin tests
```

## Naming Conventions

- Table names: `{slug}_{entity}` (e.g., `mt5_accounts`, `ai_trade_decisions`)
- Route prefix: `/api/v1/plugins/{slug}/`
- Env vars: `{SLUG}_SETTING_NAME` (e.g., `MT5_API_URL`, `AI_OPENAI_API_KEY`)
- Service classes: `{Plugin}{Purpose}Service` (e.g., `MT5AggregationService`)

## Performance Rules

- Charts: windowed loading (last 200 points), use `update()` not `setData()` for live ticks
- Tables: server-paginated, never client-filter 10k+ rows
- Overlays: delta-based updates only, cap visible markers at 200
- Heavy math: Web Workers (not main thread)
- Caching: Redis with 30–120s TTL on expensive queries
- Debounce parameter changes: 300ms minimum

## Security Rules

- Trade actions require server-side policy check before execution
- API keys: never logged, redact in request/response payloads
- Paper mode is default; live trading requires explicit opt-in
- Rate limit AI analysis endpoints
- Plugin routes must be behind authentication middleware
