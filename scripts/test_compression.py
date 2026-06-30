#!/usr/bin/env python3
"""
Token Compression Test for All Providers
=========================================
Tests that headroom compression is applied to all providers.
Sends a verbose prompt and measures compression savings.
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

# Enable compression logging
os.environ["HEADROOM_LOG_SAVINGS"] = "true"


async def test_compression():
    """Test compression across all providers."""
    from plugins.AiMarketAnalyst.backend.services.llm_gateway import call_model
    from plugins.AiMarketAnalyst.backend.services.llm_registry import get_providers
    
    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print("║              TOKEN COMPRESSION TEST - ALL PROVIDERS                        ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝\n")
    
    # Long verbose prompt to test compression
    system_prompt = """You are a financial analyst. Your job is to analyze market data and provide 
    comprehensive investment recommendations. You should consider various factors including technical 
    indicators, fundamental analysis, market sentiment, global economic conditions, sector performance, 
    and risk management principles. Always provide detailed explanations for your recommendations."""
    
    user_prompt = """Analyze the following scenario in detail: A technology company has just released 
    its quarterly earnings report showing revenue growth of 15% year-over-year, but profit margins have 
    declined by 2% due to increased R&D spending. The stock is currently trading at a P/E ratio of 25, 
    which is below the industry average of 30. The company announced a new product launch scheduled for 
    next quarter. Market sentiment is mixed, with some analysts bullish on the new product while others 
    are concerned about increased competition. What is your investment recommendation and why? Please 
    provide a detailed analysis including risk factors, potential upside, and recommended position sizing."""
    
    providers = get_providers(force_reload=True)
    enabled = [p for p in providers if p.enabled]
    
    print(f"Testing compression on {len(enabled)} enabled providers\n")
    print(f"Test prompt size: ~{len(system_prompt) + len(user_prompt)} characters\n")
    print("─" * 80 + "\n")
    
    results = []
    
    for provider in enabled:
        print(f"🔍 Testing {provider.label} ({provider.id})...")
        print(f"   Type: {provider.type}")
        
        # Check API key
        api_key = os.getenv(provider.api_key_env, "")
        if not api_key:
            print(f"   ⚠️  SKIP: {provider.api_key_env} not set")
            print()
            continue
        
        # Test with compression enabled
        try:
            print(f"   📊 Calling with compression enabled...")
            result = await call_model(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=f"{provider.id}:{provider.models[0] if provider.models and '*' not in provider.models else 'default'}",
                max_tokens=50,  # Small response to save costs
            )
            
            if "error" in result and result["error"]:
                print(f"   ❌ FAILED: {result['error']}")
                results.append({
                    "provider": provider.label,
                    "status": "FAILED",
                    "error": result["error"]
                })
            else:
                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                
                print(f"   ✅ SUCCESS")
                print(f"   Model: {result.get('model', 'unknown')}")
                print(f"   Tokens: {prompt_tokens} prompt + {completion_tokens} completion = {prompt_tokens + completion_tokens} total")
                
                # Estimate original tokens (rough estimate: 1 token ≈ 4 chars)
                estimated_original = (len(system_prompt) + len(user_prompt)) // 4
                savings_pct = 0
                if estimated_original > 0:
                    savings_pct = 100 * (1 - prompt_tokens / estimated_original)
                
                if savings_pct > 0:
                    print(f"   💰 Estimated compression: {savings_pct:.0f}% reduction")
                
                results.append({
                    "provider": provider.label,
                    "status": "SUCCESS",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "estimated_savings_pct": savings_pct
                })
        
        except Exception as e:
            print(f"   ❌ ERROR: {str(e)[:200]}")
            results.append({
                "provider": provider.label,
                "status": "ERROR",
                "error": str(e)[:200]
            })
        
        print()
    
    # Summary
    print("─" * 80)
    print("\n📊 COMPRESSION TEST SUMMARY:\n")
    
    success = [r for r in results if r["status"] == "SUCCESS"]
    failed = [r for r in results if r["status"] in ("FAILED", "ERROR")]
    
    print(f"✅ Success: {len(success)}/{len(results)}")
    for r in success:
        savings = r.get("estimated_savings_pct", 0)
        savings_str = f" ({savings:.0f}% compression)" if savings > 0 else ""
        print(f"   - {r['provider']}: {r['prompt_tokens']} tokens{savings_str}")
    
    if failed:
        print(f"\n❌ Failed: {len(failed)}/{len(results)}")
        for r in failed:
            error = r.get("error", "Unknown error")
            print(f"   - {r['provider']}: {error[:100]}")
    
    print("\n" + "─" * 80 + "\n")
    
    if success:
        avg_savings = sum(r.get("estimated_savings_pct", 0) for r in success) / len(success)
        print(f"📈 Average compression across successful providers: {avg_savings:.0f}%")
        print(f"💡 Compression is working! Look for '[Headroom]' log lines above for details.")
    else:
        print("⚠️  No successful tests - check provider API keys and connectivity")
    
    print()
    
    # Save results
    results_file = project_root / "compression_test_results.json"
    with open(results_file, "w") as f:
        json.dump({
            "test_type": "compression",
            "prompt_size": len(system_prompt) + len(user_prompt),
            "total_providers": len(results),
            "success": len(success),
            "failed": len(failed),
            "results": results
        }, f, indent=2)
    
    print(f"📝 Results saved to: {results_file}\n")
    
    return len(failed) == 0


if __name__ == "__main__":
    try:
        success = asyncio.run(test_compression())
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
