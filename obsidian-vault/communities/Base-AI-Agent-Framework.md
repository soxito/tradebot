---
type: community
name: Base AI Agent Framework
node_count: 42
synced: 2026-09-08
tags:
  - community
  - Base-AI-Agent-Framework
---

            # Base AI Agent Framework

            > Auto-generated from graphify knowledge graph. Last synced: 2026-09-08.


            ## Key Nodes (42 total)
            - `base.py`
- `_trip_circuit()`
- `_is_quota_or_auth_error()`
- `_get_client()`
- `._try_provider_chat()`
- `Base Agent — foundation for all AI trading agents. Each agent wraps an OpenAI ch`
- `Trip the circuit breaker — suppress OpenAI calls for `cooldown` seconds.`
- `Detect 429, quota exhaustion, or auth errors that should trip the breaker.`
- `Route this agent's call through the connected telegram-signals providers.`
- `AILLMProvider`
- `AIRouterSettings`
- `AIUsageRecord`
- `A configured AI provider account (with stored API key).      Powers the unified,`
- `Global settings for the shared AI router used by agents.      A single row (id=1`
- `Append-only log of every routed LLM call.      Captures token usage (for the pro`
- `add_provider()`
- `ai_router.py`
- `_cb_open()`
- `_cb_trip()`
- `_chars()`
- `get_router_settings()`
- `_reset_usage_windows()`
- `_is_capped()`
- `_order_providers()`
- `_call_openai_compatible()`
- `get_enabled_providers()`
- `db_chat()`
- `test_provider()`
- `parse_json_content()`
- `has_enabled_providers()`
- … and 12 more nodes


            ## Notes
            _Add human observations and links here._
