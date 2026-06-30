#!/usr/bin/env python3
"""
Test AI Provider Connections
============================
Tests each configured AI provider to verify API keys and connectivity.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set environment from .env
from dotenv import load_dotenv
load_dotenv()


async def test_providers():
    """Test each enabled provider."""
    from plugins.AiMarketAnalyst.backend.services.llm_gateway import call_model, get_gateway_status, _circuits
    from plugins.AiMarketAnalyst.backend.services.llm_registry import get_providers
    
    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print("║                    AI PROVIDER CONNECTION TEST                             ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝\n")
    
    # Clear any existing circuit breakers
    _circuits.clear()
    print("✅ Circuit breakers cleared\n")
    
    # Load providers
    providers = get_providers(force_reload=True)
    enabled = [p for p in providers if p.enabled]
    
    print(f"Testing {len(enabled)} enabled providers:\n")
    print("─" * 80 + "\n")
    
    test_prompt = "Respond with: OK"
    results = []
    
    for provider in enabled:
        print(f"🔍 Testing {provider.label} ({provider.id})...")
        
        # Check API key
        api_key = os.getenv(provider.api_key_env, "")
        if not api_key:
            print(f"   ❌ FAILED: {provider.api_key_env} not set in environment")
            results.append({
                "provider": provider.label,
                "id": provider.id,
                "status": "FAILED",
                "reason": f"Missing {provider.api_key_env}"
            })
            print()
            continue
        
        # Mask API key for display
        masked_key = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        print(f"   API Key: {masked_key}")
        
        # Test connection with small prompt
        try:
            result = await call_model(
                system_prompt="You are a test assistant.",
                user_prompt=test_prompt,
                model=f"{provider.id}:{provider.models[0] if provider.models and '*' not in provider.models else 'default'}",
                max_tokens=10,
            )
            
            if "error" in result and result["error"]:
                print(f"   ❌ FAILED: {result['error']}")
                results.append({
                    "provider": provider.label,
                    "id": provider.id,
                    "status": "FAILED",
                    "reason": result["error"]
                })
            else:
                print(f"   ✅ SUCCESS")
                print(f"   Model: {result.get('model', 'unknown')}")
                print(f"   Response: {str(result.get('content', {}))[:100]}")
                results.append({
                    "provider": provider.label,
                    "id": provider.id,
                    "status": "SUCCESS",
                    "model": result.get('model')
                })
        
        except Exception as e:
            print(f"   ❌ FAILED: {str(e)[:100]}")
            results.append({
                "provider": provider.label,
                "id": provider.id,
                "status": "FAILED",
                "reason": str(e)[:100]
            })
        
        print()
    
    # Summary
    print("─" * 80)
    print("\n📊 SUMMARY:\n")
    
    success = [r for r in results if r["status"] == "SUCCESS"]
    failed = [r for r in results if r["status"] == "FAILED"]
    
    print(f"✅ Success: {len(success)}/{len(results)}")
    for r in success:
        print(f"   - {r['provider']} ({r.get('model', 'N/A')})")
    
    print(f"\n❌ Failed: {len(failed)}/{len(results)}")
    for r in failed:
        print(f"   - {r['provider']}: {r['reason']}")
    
    print("\n" + "─" * 80 + "\n")
    
    # Circuit breaker status
    status = get_gateway_status(providers)
    circuits_open = [s for s in status if s["circuit"].get("open", False)]
    
    if circuits_open:
        print("⚠️  Circuit breakers open:")
        for s in circuits_open:
            remaining = s["circuit"].get("remaining_s", 0)
            print(f"   - {s['label']}: {remaining}s remaining")
    else:
        print("✅ No circuit breakers open")
    
    print()
    
    # Save results
    results_file = project_root / "provider_test_results.json"
    with open(results_file, "w") as f:
        json.dump({
            "timestamp": str(asyncio.get_event_loop().time()),
            "total": len(results),
            "success": len(success),
            "failed": len(failed),
            "results": results
        }, f, indent=2)
    
    print(f"📝 Results saved to: {results_file}\n")
    
    return len(failed) == 0


if __name__ == "__main__":
    try:
        success = asyncio.run(test_providers())
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
