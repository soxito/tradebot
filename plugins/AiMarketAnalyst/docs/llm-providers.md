# LLM Providers

Configure multiple LLM providers for the AI Market Analyst plugin and track request usage across minute/day/month windows.

## Provider Config

Set one of the following:

- `AI_ANALYST_PROVIDERS_JSON` (JSON string)
- `AI_ANALYST_PROVIDERS_FILE` (path to JSON file)

A sample file is available at:

- `plugins/AiMarketAnalyst/docs/providers.sample.json`

### JSON Shape

```json
{
  "providers": [
    {
      "id": "provider_a",
      "label": "Provider A",
      "type": "openai_compatible",
      "base_url": "https://api.provider-a.example",
      "api_key_env": "PROVIDER_A_API_KEY",
      "models": ["model-a", "model-b"],
      "rate_limits": {"minute": 60, "day": 5000, "month": 100000},
      "weight": 1.0,
      "enabled": true
    },
    {
      "id": "provider_b",
      "label": "Provider B",
      "type": "anthropic",
      "api_key_env": "PROVIDER_B_API_KEY",
      "models": ["model-c"],
      "rate_limits": {"minute": 30, "day": 2000},
      "weight": 0.8,
      "enabled": true
    }
  ]
}
```

### Supported Types

- `openai` / `openai_compatible`
- `anthropic`
- `google`

## Routing

- `AI_ANALYST_ROUTING_STRATEGY`: `round_robin` or `weighted_random`
- `AI_ANALYST_ROUTING_FALLBACK`: `true` to try the next provider on error
- `AI_ANALYST_PROVIDER_TIMEOUT_S`: per-request timeout in seconds
- `AI_ANALYST_PROVIDER_MAX_RETRIES`: retry attempts per request

To pin a provider per agent, set the model as `provider_id:model_name`.

## Usage Endpoints

- `GET /plugins/ai-analyst/llm/providers`
- `GET /plugins/ai-analyst/llm/usage`

## Redis

Usage counters prefer Redis (`AI_ANALYST_REDIS_URL` or `REDIS_URL`) and fall back to in-memory tracking if Redis is unavailable.
