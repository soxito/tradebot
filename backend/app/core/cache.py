"""
Shared bounded TTL cache — one registry so caches are visible and evictable.

Small LRU + TTL over an OrderedDict (no new dependency). The module-level
``CACHES`` registry is the point: it gives the System Monitor page a caches
table, the memory watchdog a single ``evict_all()`` lever, and a cheap sweeper.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterator, List, Optional, Tuple

# name -> TTLCache
CACHES: "Dict[str, TTLCache]" = {}
_registry_lock = threading.Lock()


class TTLCache:
    def __init__(self, name: str, maxsize: int = 512, ttl: float = 60.0) -> None:
        self.name = name
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: "OrderedDict[Any, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        with _registry_lock:
            CACHES[name] = self

    def get(self, key: Any, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return default
            expires, value = item
            if expires < now:
                self._data.pop(key, None)
                self.misses += 1
                return default
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        expires = time.monotonic() + (ttl if ttl is not None else self.ttl)
        with self._lock:
            self._data[key] = (expires, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._data.pop(key, None)
        return default if item is None else item[1]

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data.clear()
        return n

    def sweep(self) -> int:
        """Drop expired entries. Returns count removed."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            for k in [k for k, (exp, _) in self._data.items() if exp < now]:
                self._data.pop(k, None)
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._data)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            size = len(self._data)
        total = self.hits + self.misses
        return {
            "name": self.name,
            "size": size,
            "maxsize": self.maxsize,
            "ttl": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }


def get_cache(name: str, maxsize: int = 512, ttl: float = 60.0) -> TTLCache:
    existing = CACHES.get(name)
    return existing if existing is not None else TTLCache(name, maxsize, ttl)


def all_stats() -> List[Dict[str, Any]]:
    return [c.stats() for c in list(CACHES.values())]


def evict_all() -> int:
    """Clear every registered cache (watchdog lever). Returns entries dropped."""
    return sum(c.clear() for c in list(CACHES.values()))


def sweep_all() -> int:
    return sum(c.sweep() for c in list(CACHES.values()))
