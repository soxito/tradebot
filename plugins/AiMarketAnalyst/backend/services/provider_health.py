"""
AI Market Analyst — live provider health registry.

Tracks, per provider label, the three inputs the /mt5-live analysis router ranks on:

  * rolling success rate  — last ``_WINDOW`` outcomes
  * p95 latency           — last ``_LAT_WINDOW`` successful call latencies
  * last-failure age      — how long ago the provider last errored

…plus an **in-flight counter** so the background research loop can tell which
providers are genuinely idle. A provider serving a live analysis is not idle.

State is process-local by default. When Redis is reachable (same optional
dependency and graceful-degradation contract as ``app.core.events``), the
in-flight counters are mirrored to short-TTL Redis keys so the worker process
running the research loop can see calls issued by the API process. Without
Redis the research loop still refuses to touch the top-ranked PRIMARY provider,
so /mt5-live latency is protected either way.

No DB access, no network on the hot path — this module is pure bookkeeping and
is unit-testable in isolation.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Sequence

from loguru import logger

try:  # optional, exactly like app.core.events
    import redis.asyncio as _redis
except ImportError:  # pragma: no cover
    _redis = None  # type: ignore


# ── Tuning ───────────────────────────────────────────────────────────────────
_WINDOW = 50              # outcomes retained per provider
_LAT_WINDOW = 50          # latency samples retained per provider
_FAILURE_DECAY_S = 300.0  # a failure stops hurting the score after 5 min
_LATENCY_CEILING_MS = 20_000.0  # latency at/above this scores 0 on the latency term
_INFLIGHT_TTL_S = 60      # Redis in-flight keys self-heal if a process dies

# Score weights — success rate dominates, then failure recency, then latency.
_W_SUCCESS = 0.50
_W_RECENCY = 0.30
_W_LATENCY = 0.20

_REDIS_PREFIX = "tradebot:ai_inflight:"


def _p95(samples: Sequence[float]) -> float:
    """95th percentile using nearest-rank. Returns 0.0 for an empty sample."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    # nearest-rank: ceil(0.95 * n), 1-indexed
    rank = int(-(-95 * len(ordered) // 100)) or 1
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class _Stats:
    outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_WINDOW))
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=_LAT_WINDOW))
    last_failure_ts: Optional[float] = None
    in_flight: int = 0


@dataclass
class ProviderSnapshot:
    label: str
    score: float
    success_rate: float
    p95_latency_ms: float
    last_failure_age_s: Optional[float]
    in_flight: int
    samples: int


class ProviderHealth:
    """In-process health registry. One shared instance per process."""

    def __init__(self) -> None:
        self._stats: Dict[str, _Stats] = {}
        self._redis: Optional["_redis.Redis"] = None
        self._redis_checked = False

    # -- bookkeeping ---------------------------------------------------------

    def _stat(self, label: str) -> _Stats:
        st = self._stats.get(label)
        if st is None:
            st = _Stats()
            self._stats[label] = st
        return st

    def mark_start(self, label: str) -> None:
        """Called immediately before a provider request leaves the process."""
        self._stat(label).in_flight += 1

    def mark_success(self, label: str, latency_ms: float) -> None:
        st = self._stat(label)
        st.in_flight = max(0, st.in_flight - 1)
        st.outcomes.append(True)
        st.latencies.append(max(0.0, float(latency_ms)))

    def mark_failure(self, label: str) -> None:
        st = self._stat(label)
        st.in_flight = max(0, st.in_flight - 1)
        st.outcomes.append(False)
        st.last_failure_ts = time.time()

    def reset(self) -> None:
        """Drop all state. Used by tests."""
        self._stats.clear()

    # -- scoring -------------------------------------------------------------

    def success_rate(self, label: str) -> float:
        st = self._stats.get(label)
        if not st or not st.outcomes:
            return 1.0  # unknown providers are optimistically tried once
        return sum(1 for ok in st.outcomes if ok) / len(st.outcomes)

    def p95_latency_ms(self, label: str) -> float:
        st = self._stats.get(label)
        return _p95(list(st.latencies)) if st else 0.0

    def last_failure_age_s(self, label: str) -> Optional[float]:
        st = self._stats.get(label)
        if not st or st.last_failure_ts is None:
            return None
        return max(0.0, time.time() - st.last_failure_ts)

    def in_flight(self, label: str) -> int:
        st = self._stats.get(label)
        return st.in_flight if st else 0

    def health_score(self, label: str) -> float:
        """Composite 0..1 health. Higher is healthier.

        A provider with no history scores 1.0 so newly configured providers get
        a chance to prove themselves rather than being permanently ranked last.
        """
        success = self.success_rate(label)

        age = self.last_failure_age_s(label)
        if age is None:
            recency = 1.0
        else:
            recency = min(1.0, age / _FAILURE_DECAY_S)

        p95 = self.p95_latency_ms(label)
        latency = 1.0 - min(1.0, p95 / _LATENCY_CEILING_MS) if p95 > 0 else 1.0

        return round(
            _W_SUCCESS * success + _W_RECENCY * recency + _W_LATENCY * latency, 6
        )

    def rank(self, labels: Iterable[str]) -> List[str]:
        """Order labels healthiest-first.

        Ties preserve the caller's order, which is the DB priority order — so a
        fresh install with no history behaves exactly like the existing
        priority strategy until real data accumulates.
        """
        seq = list(labels)
        order = {lbl: i for i, lbl in enumerate(seq)}
        return sorted(seq, key=lambda l: (-self.health_score(l), order[l]))

    def snapshot(self, labels: Iterable[str]) -> List[ProviderSnapshot]:
        out: List[ProviderSnapshot] = []
        for lbl in labels:
            st = self._stats.get(lbl)
            out.append(ProviderSnapshot(
                label=lbl,
                score=self.health_score(lbl),
                success_rate=round(self.success_rate(lbl), 4),
                p95_latency_ms=round(self.p95_latency_ms(lbl), 1),
                last_failure_age_s=self.last_failure_age_s(lbl),
                in_flight=self.in_flight(lbl),
                samples=len(st.outcomes) if st else 0,
            ))
        return out

    # -- idle detection (cross-process when Redis is up) ---------------------

    async def _get_redis(self) -> Optional["_redis.Redis"]:
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        if _redis is None:
            return None
        try:
            from app.core.config import settings as _settings

            client = _redis.from_url(_settings.REDIS_URL, decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001 — Redis is strictly optional
            logger.debug(f"[provider_health] Redis unavailable, local-only: {exc}")
            self._redis = None
        return self._redis

    async def publish_inflight(self, label: str, delta: int) -> None:
        """Mirror an in-flight change to Redis so other processes can see it."""
        client = await self._get_redis()
        if client is None:
            return
        key = f"{_REDIS_PREFIX}{label}"
        try:
            if delta > 0:
                await client.incr(key)
                await client.expire(key, _INFLIGHT_TTL_S)
            else:
                val = await client.decr(key)
                if val <= 0:
                    await client.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[provider_health] Redis in-flight update skipped: {exc}")

    async def remote_in_flight(self, label: str) -> int:
        client = await self._get_redis()
        if client is None:
            return 0
        try:
            raw = await client.get(f"{_REDIS_PREFIX}{label}")
            return max(0, int(raw or 0))
        except Exception:  # noqa: BLE001
            return 0

    async def idle_providers(
        self,
        labels: Iterable[str],
        *,
        exclude: Iterable[str] = (),
    ) -> List[str]:
        """Labels with zero in-flight requests locally AND remotely, minus `exclude`.

        The research loop passes the PRIMARY analyst label in `exclude`, so the
        provider serving /mt5-live is never borrowed even if it happens to be
        momentarily idle between requests.
        """
        blocked = {e for e in exclude}
        out: List[str] = []
        for lbl in labels:
            if lbl in blocked:
                continue
            if self.in_flight(lbl) > 0:
                continue
            if await self.remote_in_flight(lbl) > 0:
                continue
            out.append(lbl)
        return out


# Shared per-process instance.
provider_health = ProviderHealth()
