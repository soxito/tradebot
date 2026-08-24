"""
Realtime EventBus — publish/subscribe backbone for Server-Sent Events.

Producers (signal, trade, sniper, sentiment, monitor, price services) call
``event_bus.publish(topic, data)``. Consumers (the SSE endpoint) call
``event_bus.subscribe(topics)`` to receive an async stream of matching events.

Fan-out strategy:
- If Redis is reachable, every publish goes to a single Redis pub/sub channel and
  a per-process listener re-broadcasts to that process's local subscriber queues.
  This lets multiple uvicorn workers share one logical stream (exactly-once per
  worker, no duplicate delivery).
- If Redis is unavailable, publish fans out directly to local queues. This keeps
  the single-process dev server fully functional with zero infrastructure.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Dict, Optional, Set

from loguru import logger

from app.core.config import settings

try:  # redis is an optional runtime dependency; degrade gracefully when missing
    import redis.asyncio as redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore


REDIS_CHANNEL = "tradebot:events"

# Per-subscriber queue bound. If a client stalls, we drop oldest events for it
# rather than growing memory without limit.
_QUEUE_MAXSIZE = 1000


class Topics:
    """Canonical event topic names shared by producers and consumers."""

    SIGNAL_NEW = "signal.new"
    TRADE_UPDATE = "trade.update"
    SNIPER_EVENT = "sniper.event"
    SENTIMENT_UPDATE = "sentiment.update"
    MONITOR_STATUS = "monitor.status"
    PRICE_TICK = "price.tick"
    SYSTEM_ALERT = "system.alert"

    # Trading-room agent lifecycle
    AGENT_STARTED = "agent.started"
    AGENT_PROGRESS = "agent.progress"
    AGENT_SPEAKING = "agent.speaking"
    AGENT_COMPLETED = "agent.completed"
    AGENT_STATE = "agent.state"
    SESSION_STARTED = "session.started"
    SESSION_COMPLETED = "session.completed"
    ROOM_FOCUS = "room.focus"
    ROOM_EXECUTION = "room.execution"
    CYCLE_TRANSITION = "cycle.transition"
    WHALE_MOVE = "whale.move"

    ALL: Set[str] = {
        SIGNAL_NEW,
        TRADE_UPDATE,
        SNIPER_EVENT,
        SENTIMENT_UPDATE,
        MONITOR_STATUS,
        PRICE_TICK,
        SYSTEM_ALERT,
        AGENT_STARTED,
        AGENT_PROGRESS,
        AGENT_SPEAKING,
        AGENT_COMPLETED,
        AGENT_STATE,
        SESSION_STARTED,
        SESSION_COMPLETED,
        ROOM_FOCUS,
        ROOM_EXECUTION,
        CYCLE_TRANSITION,
        WHALE_MOVE,
    }


class EventBus:
    """Process-local pub/sub with optional Redis fan-out across workers."""

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._redis: Optional["redis.Redis"] = None
        self._redis_checked = False
        self._redis_ok = False
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── Redis wiring ──────────────────────────────────────────────────────────
    async def _get_redis(self) -> Optional["redis.Redis"]:
        """Return a connected Redis client, or None if Redis is unavailable."""
        if self._redis_checked:
            return self._redis if self._redis_ok else None
        async with self._lock:
            if self._redis_checked:
                return self._redis if self._redis_ok else None
            self._redis_checked = True
            if redis is None:
                logger.info("EventBus: redis package not installed — using in-memory fan-out")
                return None
            try:
                client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
                await client.ping()
                self._redis = client
                self._redis_ok = True
                logger.info("EventBus: Redis fan-out enabled")
                return client
            except Exception as exc:  # noqa: BLE001 - any connection failure → local mode
                logger.info(f"EventBus: Redis unavailable ({exc}) — using in-memory fan-out")
                self._redis_ok = False
                return None

    async def _ensure_listener(self) -> None:
        """Start the Redis→local re-broadcast listener once per process."""
        if self._listener_task is not None and not self._listener_task.done():
            return
        client = await self._get_redis()
        if client is None:
            return
        self._listener_task = asyncio.create_task(self._redis_listener(client))

    async def _redis_listener(self, client: "redis.Redis") -> None:
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(REDIS_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except (ValueError, TypeError):
                    continue
                self._fanout_local(payload)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"EventBus: Redis listener stopped ({exc}); falling back to local")
            self._redis_ok = False
        finally:
            try:
                await pubsub.unsubscribe(REDIS_CHANNEL)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ── Fan-out helpers ───────────────────────────────────────────────────────
    def _fanout_local(self, payload: Dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest event to make room — freshness beats completeness.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ── Public API ────────────────────────────────────────────────────────────
    async def publish(self, topic: str, data: Any) -> None:
        """Publish an event to all subscribers (this worker + peers via Redis)."""
        payload: Dict[str, Any] = {"topic": topic, "data": data, "ts": time.time()}
        client = await self._get_redis()
        if client is not None:
            try:
                await client.publish(REDIS_CHANNEL, json.dumps(payload, default=str))
                return
            except Exception as exc:  # noqa: BLE001 - fall back to local on publish error
                logger.debug(f"EventBus: Redis publish failed ({exc}); local fan-out")
                self._redis_ok = False
        self._fanout_local(payload)

    def emit(self, topic: str, data: Any) -> None:
        """Fire-and-forget publish usable from sync or async contexts.

        Schedules ``publish`` on the running loop; silently drops if no loop is
        running (e.g. called from a non-async worker thread with no loop).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish(topic, data))

    async def subscribe(
        self, topics: Optional[Set[str]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield events matching ``topics`` (or all topics when None)."""
        await self._ensure_listener()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            while True:
                payload = await queue.get()
                if topics is None or payload.get("topic") in topics:
                    yield payload
        finally:
            self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton shared across the application.
event_bus = EventBus()
