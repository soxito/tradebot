import pytest

from plugins.AiMarketAnalyst.backend.services.llm_usage import InMemoryUsageStore


@pytest.mark.asyncio
async def test_in_memory_usage_increments():
    store = InMemoryUsageStore()
    key = "ai_analyst:test:minute"
    count = await store.incr(key, 60)
    assert count == 1
    assert await store.get(key) == 1
