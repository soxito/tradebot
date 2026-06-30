# AI Market Analyst - Provider Configuration

This document explains how to configure multiple AI providers for the AI Market Analyst plugin.

## Current Status

✅ **OpenAI** - Configured and enabled  
⚠️ **Groq** - Disabled (no API key)  
⚠️ **Google Gemini** - Disabled (no API key)  
⚠️ **Mistral** - Disabled (no API key)  
⚠️ **Cerebras** - Disabled (no API key)  
⚠️ **OpenRouter** - Disabled (no API key)

## How to Add Providers

### 1. Get API Keys

Sign up for free/paid accounts at:

- **Groq**: https://console.groq.com/keys
- **Google Gemini**: https://makersuite.google.com/app/apikey
- **Mistral**: https://console.mistral.ai/api-keys/
- **Cerebras**: https://cloud.cerebras.ai/
- **OpenRouter**: https://openrouter.ai/keys

### 2. Add Keys to `.env`

Uncomment and add your keys in `.env`:

```bash
# Additional AI Providers (add your keys here to enable)
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MISTRAL_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CEREBRAS_API_KEY=csk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. Enable Providers

Edit `plugins/AiMarketAnalyst/backend/providers.json` and set `"enabled": true` for each provider:

```json
{
  "id": "groq",
  "label": "Groq",
  "enabled": true  // Change from false to true
}
```

### 4. Reset Circuit Breakers

After adding keys, run:

```bash
python scripts/reset_ai_circuit_breakers.py
```

This clears any "circuit open" errors and reloads the provider registry.

### 5. Restart Backend

```bash
# Restart your FastAPI backend to pick up new environment variables
```

## Routing Strategies

The system supports two routing strategies (configured via `AI_ANALYST_ROUTING_STRATEGY`):

### Round Robin (default)
```bash
AI_ANALYST_ROUTING_STRATEGY=round_robin
```
Distributes requests evenly across all enabled providers.

### Weighted Random
```bash
AI_ANALYST_ROUTING_STRATEGY=weighted_random
```
Selects providers based on their `weight` value in `providers.json`.

## Fallback Behavior

If a provider fails, the system automatically tries the next provider when `AI_ANALYST_ROUTING_FALLBACK=true` (default).

## Circuit Breaker

The system uses a circuit breaker pattern to protect against failing providers:

- **Open Duration**: 300 seconds (5 minutes)
- **Triggers**: 401 errors, rate limits, quota exceeded, invalid API keys
- **Reset**: Run `python scripts/reset_ai_circuit_breakers.py`

## Troubleshooting

### "Circuit Open" Errors

**Cause**: Provider failed multiple times and circuit breaker opened.  
**Fix**: Run `python scripts/reset_ai_circuit_breakers.py`

### 401 Unauthorized Errors

**Cause**: Missing or invalid API key.  
**Fix**: 
1. Check API key in `.env`
2. Verify key is valid at provider's console
3. Ensure provider is enabled in `providers.json`

### Provider Not Used

**Check**:
1. Is `enabled: true` in `providers.json`?
2. Is API key set in `.env`?
3. Has circuit breaker tripped? (check logs)
4. Is provider supported for your model?

## Rate Limits

Default rate limits per provider:

- **OpenAI**: 20 requests/minute
- **Groq**: 30 requests/minute, 14,400/day
- **Google Gemini**: 15 requests/minute
- **Mistral**: 10 requests/minute
- **Cerebras**: 30 requests/minute
- **OpenRouter**: 20 requests/minute

Adjust in `providers.json` under `rate_limits`.

## Model Selection

You can specify a provider and model in your requests:

```python
# Use specific provider
result = await call_model(
    system_prompt="...",
    user_prompt="...",
    model="groq:llama-3.3-70b-versatile"  # provider:model
)

# Let system choose provider for model
result = await call_model(
    system_prompt="...",
    user_prompt="...",
    model="gemini-2.0-flash-exp"  # system finds compatible provider
)
```

## Files

- `plugins/AiMarketAnalyst/backend/providers.json` - Provider registry
- `plugins/AiMarketAnalyst/backend/services/llm_gateway.py` - Gateway logic
- `plugins/AiMarketAnalyst/backend/services/llm_registry.py` - Provider loader
- `scripts/reset_ai_circuit_breakers.py` - Circuit breaker reset utility
