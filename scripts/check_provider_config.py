#!/usr/bin/env python3
"""
Quick Provider Configuration Check
==================================
Verifies that provider base URLs are correctly configured.
"""
import json
from pathlib import Path

providers_file = Path("plugins/AiMarketAnalyst/backend/providers.json")

with open(providers_file) as f:
    data = json.load(f)

print("╔════════════════════════════════════════════════════════════════════════════╗")
print("║              PROVIDER BASE URL CONFIGURATION CHECK                         ║")
print("╚════════════════════════════════════════════════════════════════════════════╝\n")

for provider in data["providers"]:
    enabled = "✅" if provider["enabled"] else "⚠️"
    base_url = provider.get("base_url") or "None (uses OPENAI_BASE_URL)"
    
    print(f"{enabled} {provider['label']:20s} ({provider['type']})")
    print(f"   Base URL: {base_url}")
    print(f"   API Key Env: {provider['api_key_env']}")
    
    # Check if this provider will be routed correctly
    if provider["type"] in ("openai_compatible", "openai"):
        if provider.get("base_url") is None:
            print(f"   🔄 Will use headroom proxy (OPENAI_BASE_URL)")
        else:
            print(f"   ✅ Will use direct endpoint (no proxy)")
    elif provider["type"] == "google":
        print(f"   ✅ Uses httpx directly (no proxy)")
    
    print()

print("─" * 80)
print("\n✅ Configuration check complete!")
print("\nExpected behavior:")
print("  • OpenAI: Uses headroom proxy at http://127.0.0.1:8787/p/tradebot/v1")
print("  • Other providers: Use their own base URLs directly")
print()
