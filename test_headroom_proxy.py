#!/usr/bin/env python3
"""
Test script to verify headroom-proxy connectivity from within the backend container.
Run this after starting docker-compose to verify the fix works.
"""
import os
import asyncio
import httpx

async def test_headroom_proxy():
    # Use the Docker service name (same as what backend uses)
    proxy_url = os.getenv("HEADROOM_PROXY_URL", "http://headroom-proxy:8787")
    
    print(f"Testing headroom-proxy at: {proxy_url}")
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Test health endpoint
        try:
            resp = await client.get(f"{proxy_url}/health")
            print(f"Health check: {resp.status_code} - {resp.json()}")
        except Exception as e:
            print(f"Health check FAILED: {e}")
            return False
        
        # Test chat completions endpoint with a minimal request
        try:
            resp = await client.post(
                f"{proxy_url}/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "nvidia/nemotron-3-ultra-550b-a55b",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 10,
                }
            )
            print(f"Chat completions: {resp.status_code}")
            if resp.status_code == 401:
                print("  (Expected 401 with test key - endpoint is reachable)")
            elif resp.status_code == 200:
                print(f"  Success: {resp.json()}")
            else:
                print(f"  Response: {resp.text[:200]}")
        except Exception as e:
            print(f"Chat completions FAILED: {e}")
            return False
    
    return True

if __name__ == "__main__":
    result = asyncio.run(test_headroom_proxy())
    print(f"\n{'✅ All tests passed' if result else '❌ Tests failed'}")
