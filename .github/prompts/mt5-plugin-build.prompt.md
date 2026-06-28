---
description: "Build a new MT5 plugin feature: multi-account aggregation, risk overlays, backtesting bridge, copy-trading simulation, live account views, or chart overlays."
agent: "TradeBot Architect"
argument-hint: "Describe the MT5 plugin feature to build (e.g., 'multi-account aggregation with weighted snapshots')"
---

Build or extend the MT5TradingPlugin under `plugins/MT5TradingPlugin/`.

## Context

Use the `/tradebot-plugin-builder` skill for architecture rules and plugin structure.

## Requirements

- All code under `plugins/MT5TradingPlugin/` only
- Never modify `backend/app/` or `frontend/src/`
- Table prefix: `mt5_`
- Route prefix: `/api/v1/plugins/mt5/`
- Connect to MT5 via mtapi-io REST API
- Chart overlays must be delta-based and fast

## Phases

1. Scan existing plugin state (`plugin.json`, models, routes)
2. Design schema additions (migrations)
3. Build services
4. Add API routes
5. Build frontend components
6. Add chart overlays if applicable
7. Write tests and update docs
