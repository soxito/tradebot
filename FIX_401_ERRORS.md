# Fix Applied: Provider 401 Unauthorized Errors

## Issue
All providers (Groq, Google Gemini, Mistral, Cerebras, OpenRouter) were returning 401 errors because they were being incorrectly routed through the headroom proxy at `http://127.0.0.1:8787/p/tradebot/v1` instead of their actual endpoints.

## Root Cause
The `AsyncOpenAI` client was reading the `OPENAI_BASE_URL` environment variable even for non-OpenAI providers, causing all requests to go through the headroom proxy which only works with OpenAI's API key.

## Fix Applied
Modified `/plugins/AiMarketAnalyst/backend/services/llm_gateway.py` to explicitly set `base_url` for all providers except OpenAI:

```python
# BEFORE (line 141-157):
headroom_url = os.getenv("OPENAI_BASE_URL") or None
effective_base_url: Optional[str]
if provider.base_url is None and headroom_url:
    effective_base_url = None   # let AsyncOpenAI read OPENAI_BASE_URL
else:
    effective_base_url = provider.base_url

client_kwargs: Dict[str, Any] = {"api_key": api_key}
if effective_base_url is not None:
    client_kwargs["base_url"] = effective_base_url
client = AsyncOpenAI(**client_kwargs)

# AFTER:
if provider.base_url is None:
    # OpenAI: let it use OPENAI_BASE_URL from environment (headroom proxy)
    client_kwargs: Dict[str, Any] = {"api_key": api_key}
else:
    # Other providers: explicitly set their base_url to override env var
    client_kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "base_url": provider.base_url,
    }

client = AsyncOpenAI(**client_kwargs)
```

## Expected Behavior After Fix

| Provider | Endpoint |
|----------|----------|
| OpenAI | `http://127.0.0.1:8787/p/tradebot/v1` (headroom proxy) |
| Groq | `https://api.groq.com/openai/v1` (direct) |
| Google Gemini | `https://generativelanguage.googleapis.com` (direct) |
| Mistral | `https://api.mistral.ai/v1` (direct) |
| Cerebras | `https://api.cerebras.ai/v1` (direct) |
| OpenRouter | `https://openrouter.ai/api/v1` (direct) |

## Next Steps

1. **Restart your backend** to load the code changes:
   ```bash
   cd /Users/sakhilematsimela/Sites/tradebot
   source .venv/bin/activate
   
   # Stop existing backend (Ctrl+C or kill process)
   # Then restart:
   python backend/main.py  # or your start command
   ```

2. **Reset circuit breakers** (they may still be tripped from previous failures):
   ```bash
   curl -X POST http://localhost:8000/plugins/ai-analyst/llm/reset-circuits
   ```

3. **Test the providers**:
   ```bash
   ./scripts/test_providers.sh
   ```

## Verification

Run the config check to verify setup:
```bash
python3 scripts/check_provider_config.py
```

Expected output: All providers show "✅ Will use direct endpoint (no proxy)" except OpenAI.

## Files Modified

- `plugins/AiMarketAnalyst/backend/services/llm_gateway.py` (line 141-156)
- Created `scripts/check_provider_config.py` (verification tool)

## Testing Commands

```bash
# Check provider status
curl http://localhost:8000/plugins/ai-analyst/llm/providers | python3 -m json.tool

# Test specific provider
curl -X POST http://localhost:8000/plugins/ai-analyst/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "test", "model": "groq:llama-3.3-70b-versatile"}'
```

After restart, all providers should work correctly with their own API keys!
