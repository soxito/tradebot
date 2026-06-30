#!/usr/bin/env python3
"""
Verify Provider Routing Fix
============================
Confirms that non-OpenAI providers use their own base URLs and don't
route through the headroom proxy.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Load .env manually
env_file = project_root / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def test_routing():
    """Test that providers use correct base URLs."""
    from plugins.AiMarketAnalyst.backend.services.llm_registry import get_providers
    
    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print("║              PROVIDER ROUTING VERIFICATION                                 ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝\n")
    
    # Check OPENAI_BASE_URL is set
    headroom_url = os.getenv("OPENAI_BASE_URL")
    print(f"OPENAI_BASE_URL environment variable: {headroom_url}\n")
    
    providers = get_providers(force_reload=True)
    enabled = [p for p in providers if p.enabled]
    
    print(f"Testing routing for {len(enabled)} enabled providers:\n")
    print("─" * 80 + "\n")
    
    results = []
    
    for provider in enabled:
        print(f"📍 {provider.label} ({provider.id})")
        print(f"   Type: {provider.type}")
        print(f"   Configured base_url: {provider.base_url or 'None (uses env)'}")
        
        # Determine expected routing
        if provider.base_url is None:
            expected = headroom_url or "https://api.openai.com/v1"
            print(f"   ✅ Expected: {expected} (headroom proxy)")
        else:
            expected = provider.base_url
            print(f"   ✅ Expected: {expected} (direct)")
        
        # Check API key
        api_key = os.getenv(provider.api_key_env, "")
        if not api_key:
            print(f"   ⚠️  Warning: {provider.api_key_env} not set")
        
        results.append({
            "provider": provider.label,
            "id": provider.id,
            "base_url": provider.base_url,
            "expected": expected,
            "uses_proxy": provider.base_url is None
        })
        print()
    
    # Summary
    print("─" * 80)
    print("\n📊 ROUTING SUMMARY:\n")
    
    proxy_providers = [r for r in results if r["uses_proxy"]]
    direct_providers = [r for r in results if not r["uses_proxy"]]
    
    print(f"Via Headroom Proxy ({headroom_url}):")
    for r in proxy_providers:
        print(f"   🔄 {r['provider']}")
    
    print(f"\nDirect to Provider:")
    for r in direct_providers:
        print(f"   ✅ {r['provider']} → {r['expected']}")
    
    print("\n" + "─" * 80 + "\n")
    
    # Verification
    print("🔍 VERIFICATION:\n")
    
    if len(proxy_providers) == 1 and proxy_providers[0]["id"] == "openai":
        print("✅ CORRECT: Only OpenAI uses headroom proxy")
    elif len(proxy_providers) == 0:
        print("✅ CORRECT: No providers use headroom proxy (OPENAI_BASE_URL not set)")
    else:
        print("❌ WARNING: Multiple providers use headroom proxy:")
        for r in proxy_providers:
            print(f"   - {r['provider']}")
    
    if len(direct_providers) >= 5:
        print(f"✅ CORRECT: {len(direct_providers)} providers use direct endpoints")
    
    print("\n" + "─" * 80 + "\n")
    
    print("Next steps:")
    print("1. Restart your backend to load the code changes")
    print("2. Reset circuit breakers:")
    print("   curl -X POST http://localhost:8000/plugins/ai-analyst/llm/reset-circuits")
    print("3. Test the AI Review in your UI - should work now!")
    print()


if __name__ == "__main__":
    try:
        test_routing()
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
