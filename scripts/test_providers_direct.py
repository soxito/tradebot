#!/usr/bin/env python3
"""Quick test to verify AI providers are working after the fix."""
import httpx
import json
import os
from pathlib import Path

# Load .env
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)

def test_provider(label: str, base_url: str, api_key_env: str, model: str):
    """Test a single provider directly."""
    api_key = os.getenv(api_key_env)
    if not api_key:
        print(f"  ⚠️  {api_key_env} not set - skipping")
        return False
    
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say 'OK' if you can hear me"}],
        "max_tokens": 10,
    }
    
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  ✅ {label}: {content.strip()[:50]}")
            return True
    except Exception as e:
        error = str(e)[:100]
        print(f"  ❌ {label}: {error}")
        return False

def main():
    print("╔═══════════════════════════════════════════════════════════════════════════════╗")
    print("║              Testing AI Providers After Fix                                  ║")
    print("╚═══════════════════════════════════════════════════════════════════════════════╝\n")
    
    providers = [
        ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
        ("Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest"),
        ("Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama3.1-8b"),
        ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct"),
    ]
    
    results = []
    for label, base_url, api_key_env, model in providers:
        print(f"Testing {label}...")
        success = test_provider(label, base_url, api_key_env, model)
        results.append((label, success))
        print()
    
    print("─" * 80)
    print("\n📊 Summary:\n")
    passed = sum(1 for _, success in results if success)
    total = len(results)
    print(f"  Passed: {passed}/{total}")
    for label, success in results:
        status = "✅" if success else "❌"
        print(f"  {status} {label}")
    
    if passed == total:
        print("\n🎉 All providers working! The fix is successful!")
    elif passed > 0:
        print(f"\n⚠️  {passed} providers working, {total - passed} failed")
    else:
        print("\n❌ All providers failed - check API keys")

if __name__ == "__main__":
    main()
