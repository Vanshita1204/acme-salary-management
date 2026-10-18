"""A small in-process response cache for the pay-insight views.

Insight numbers only change when data is written, when exchange rates refresh (once a
day) or when a future-dated change takes effect, yet each view scans every active
employee's pay. So an identical request is answered from memory until anything is
written through the API, or until `ttl` seconds pass. The time limit also covers changes
made outside this process, such as the daily rate-refresh job.

One instance serves one process: running several workers would need a shared cache.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class ResponseCache:
    def __init__(
        self,
        ttl: float,
        max_entries: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, CachedResponse]] = OrderedDict()
        self.version = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, key: str) -> CachedResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, response = entry
        if self._clock() - stored_at > self.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return response

    def put(self, key: str, response: CachedResponse, version: int) -> None:
        """Store a response computed while the cache was at `version`. A write that landed
        in the meantime makes it possibly stale, so it is dropped."""
        if not self.enabled or version != self.version:
            return
        self._entries[key] = (self._clock(), response)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate(self) -> None:
        self._entries.clear()
        self.version += 1


class ExpiringValue(Generic[T]):
    """One value computed by a function and remembered for `ttl` seconds (0: never remembered),
    forgotten at once by `invalidate()`. A value computed while a write was landing is not kept."""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self._clock = clock
        self._value: T | None = None
        self._stored_at = 0.0
        self._has_value = False
        self.version = 0

    def get(self, compute: Callable[[], T]) -> T:
        if self.ttl <= 0:
            return compute()
        if self._has_value and self._clock() - self._stored_at <= self.ttl:
            return self._value  # type: ignore[return-value]
        version = self.version
        value = compute()
        if version == self.version:
            self._value, self._stored_at, self._has_value = value, self._clock(), True
        return value

    def invalidate(self) -> None:
        self._has_value = False
        self._value = None
        self.version += 1
