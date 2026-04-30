"""Small in-process TTL caches for dashboard read paths (single-process Waitress)."""

from __future__ import annotations

import threading
from typing import Callable, Hashable, TypeVar

from cachetools import TTLCache

T = TypeVar("T")


class TTLStore:
    """Thread-safe get-or-set with TTL eviction."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._cache: TTLCache = TTLCache(maxsize=max(16, int(maxsize)), ttl=float(ttl))
        self._lock = threading.RLock()

    def get_or_set(self, key: Hashable, factory: Callable[[], T]) -> T:
        with self._lock:
            if key in self._cache:
                return self._cache[key]  # type: ignore[return-value]
        val = factory()
        with self._lock:
            self._cache[key] = val
        return val

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def invalidate_prefix(self, prefix: tuple[Hashable, ...]) -> None:
        """Remove keys whose tuple prefix matches (same length, equal elements)."""
        plen = len(prefix)
        if plen == 0:
            return
        with self._lock:
            for k in list(self._cache.keys()):
                if isinstance(k, tuple) and len(k) >= plen and k[:plen] == prefix:
                    self._cache.pop(k, None)
