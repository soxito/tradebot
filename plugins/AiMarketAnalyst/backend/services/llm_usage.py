"""AI Market Analyst - LLM Usage Tracking"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from loguru import logger

from plugins.AiMarketAnalyst.backend.config import ai_analyst_config
from plugins.AiMarketAnalyst.backend.services.llm_registry import LLMProvider

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - dependency present in runtime
    redis = None  # type: ignore


WINDOWS = {
    "minute": 60,
    "day": 86400,
    "month": 2592000,
}
PREFIX = "ai_analyst:llm:usage"


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._data: Dict[str, tuple[int, float]] = {}

    def _purge(self) -> None:
        now = time.time()
        expired = [k for k, (_, exp) in self._data.items() if exp <= now]
        for key in expired:
            self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        self._purge()
        now = time.time()
        count, exp = self._data.get(key, (0, now + ttl))
        count += 1
        self._data[key] = (count, now + ttl)
        return count

    async def get(self, key: str) -> int:
        self._purge()
        count, _ = self._data.get(key, (0, 0))
        return count


class RedisUsageStore:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Optional["redis.Redis"] = None

    async def _get_client(self) -> "redis.Redis":
        if self._client is not None:
            return self._client
        if redis is None:
            raise RuntimeError("redis package not available")
        client = redis.Redis.from_url(self._redis_url, decode_responses=True)
        await client.ping()
        self._client = client
        return client

    async def incr(self, key: str, ttl: int) -> int:
        client = await self._get_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, ttl)
        return int(count)

    async def get(self, key: str) -> int:
        client = await self._get_client()
        value = await client.get(key)
        return int(value or 0)


_memory_store = InMemoryUsageStore()
_redis_store: Optional[RedisUsageStore] = None
_store_is_redis = False


async def _get_store() -> InMemoryUsageStore | RedisUsageStore:
    global _redis_store, _store_is_redis
    if _store_is_redis and _redis_store is not None:
        return _redis_store
    if redis is None:
        return _memory_store
    if _redis_store is None:
        _redis_store = RedisUsageStore(ai_analyst_config.redis_url)
    try:
        await _redis_store._get_client()
        _store_is_redis = True
        return _redis_store
    except Exception as exc:
        logger.warning(f"[AI-Analyst] Redis unavailable, using memory store: {exc}")
        _store_is_redis = False
        return _memory_store


def _provider_key(provider_id: str, window: str) -> str:
    return f"{PREFIX}:{provider_id}:{window}"


def _total_key(window: str) -> str:
    return f"{PREFIX}:total:{window}"


async def increment_usage(provider_id: str) -> None:
    store = await _get_store()
    for window, ttl in WINDOWS.items():
        await store.incr(_provider_key(provider_id, window), ttl)
        await store.incr(_total_key(window), ttl)


async def get_usage(provider_id: str) -> Dict[str, int]:
    store = await _get_store()
    usage: Dict[str, int] = {}
    for window in WINDOWS:
        usage[window] = await store.get(_provider_key(provider_id, window))
    return usage


async def get_total_usage() -> Dict[str, int]:
    store = await _get_store()
    usage: Dict[str, int] = {}
    for window in WINDOWS:
        usage[window] = await store.get(_total_key(window))
    return usage


def _remaining(count: int, limit: Optional[int]) -> Optional[int]:
    if limit is None or limit <= 0:
        return None
    return max(0, limit - count)


def _limit_for(provider: LLMProvider, window: str) -> Optional[int]:
    limit = provider.rate_limits.get(window)
    if limit is None:
        return None
    return int(limit)


def _sum_remaining(remainings: List[Optional[int]]) -> Optional[int]:
    if any(r is None for r in remainings):
        return None
    return sum(r or 0 for r in remainings)


async def get_usage_snapshot(providers: List[LLMProvider]) -> Dict:
    total_usage = await get_total_usage()

    providers_summary = []
    per_window_remaining: Dict[str, List[Optional[int]]] = {w: [] for w in WINDOWS}

    for provider in providers:
        usage = await get_usage(provider.id)
        limits = {w: _limit_for(provider, w) for w in WINDOWS}
        remaining = {w: _remaining(usage[w], limits[w]) for w in WINDOWS}

        for window in WINDOWS:
            per_window_remaining[window].append(remaining[window])

        providers_summary.append(
            {
                "id": provider.id,
                "label": provider.label,
                "usage": usage,
                "limits": limits,
                "remaining": remaining,
            }
        )

    total_limits = {
        w: _sum_remaining(per_window_remaining[w]) for w in WINDOWS
    }

    total_remaining = {
        w: _remaining(total_usage[w], total_limits[w]) for w in WINDOWS
    }

    return {
        "total": {
            "usage": total_usage,
            "limits": total_limits,
            "remaining": total_remaining,
        },
        "providers": providers_summary,
    }
