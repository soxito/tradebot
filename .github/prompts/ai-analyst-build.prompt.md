---
description: "Build or extend the AI Market Analyst plugin with GPT-5.2 Responses API, agent profiles, smart limit orders, risk policy engine, and TradingView chart overlays."
agent: "TradeBot Architect"
argument-hint: "Describe the AI analyst feature to build (e.g., 'agent runtime with tool calling and risk gates')"
---

Build or extend the AiMarketAnalyst plugin under `plugins/AiMarketAnalyst/`.

## Context

Use the `/tradebot-plugin-builder` skill for architecture rules and plugin structure.

## Requirements

- All code under `plugins/AiMarketAnalyst/` only
- Never modify `backend/app/` or `frontend/src/`
- Table prefix: `ai_`
- Route prefix: `/api/v1/plugins/ai-analyst/`
- Use OpenAI Responses API (NOT Chat Completions) for GPT-5.2
- Support `previous_response_id` for multi-turn analysis
- Configure `reasoning.effort` and `text.verbosity` per agent profile
- Implement function calling with server-side tool allowlist
- All AI outputs validated against strict JSON schema
- Risk policy engine runs BEFORE any trade action
- Paper mode is default; auto-place requires elevated permission

## Phases

1. Scan existing plugin state
2. Design schema (ai_agents, ai_trade_decisions, ai_trade_settings)
3. Build OpenAI Responses client wrapper
4. Build agent runtime (prompt assembly, tool calling, output validation)
5. Build risk policy engine
6. Build MT5 order gateway (calls existing MT5 plugin services)
7. Add admin agent builder UI
8. Add user terminal panel UI
9. Add chart overlays for proposed orders
10. Write tests and update docs
