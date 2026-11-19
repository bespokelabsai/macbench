"""Per-key token bucket, shared by the board and the proxy."""

from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    """`burst` ops at once, refilled at `rate` per second. A rate or burst of 0 turns it off."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic):
        self.rate = float(rate)
        self.burst = float(burst)
        if self.rate > 0 and 0 < self.burst < 1:
            raise ValueError(f"burst {burst} would never admit an op; use 0 (off) or at least 1")
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0 and self.burst > 0

    def take(self, key: str) -> float:
        """Spend one token for key. Returns 0 if allowed, else seconds until a token is free."""
        if not self.enabled:
            return 0.0
        with self._lock:
            now = self.clock()
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.rate
