"""In-memory abuse limits. The relay stores nothing else; restarting it resets them."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

_MAX_KEYS = 50_000


class Limited(Exception):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiter:
    """Token bucket per key."""

    def __init__(self, per_minute: int) -> None:
        self.rate = per_minute / 60
        self.capacity = float(per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str) -> None:
        if self.rate <= 0:
            return
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            raise Limited("Too many requests", math.ceil((1 - tokens) / self.rate))
        self._buckets[key] = (tokens - 1, now)
        if len(self._buckets) > _MAX_KEYS:
            full_after = self.capacity / self.rate
            self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}


class DailyLimit:
    """At most ``limit`` requests per key per UTC day."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._day = ""
        self._counts: dict[str, int] = {}

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = datetime.now(UTC)
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._counts = day, {}
        count = self._counts.get(key, 0)
        if count >= self.limit:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
            raise Limited("Daily limit reached", math.ceil(midnight - now.timestamp()))
        self._counts[key] = count + 1


class Coalescer:
    """Allows one wake per key per ``interval`` seconds."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, -math.inf) < self.interval:
            return False
        self._last[key] = now
        if len(self._last) > _MAX_KEYS:
            self._last = {k: t for k, t in self._last.items() if now - t < self.interval}
        return True
