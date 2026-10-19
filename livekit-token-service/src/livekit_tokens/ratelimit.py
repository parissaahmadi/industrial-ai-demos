"""Sliding-window rate limiter, in memory, per key. The clock is passed in, so tests need no sleeping."""

from __future__ import annotations

import math
import threading
from collections import deque

from .errors import RateLimited


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: float) -> None:
        """Count one request or raise ``RateLimited`` (the refused request is not counted)."""
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                wait = max(1, math.ceil(hits[0] + self.window - now))
                raise RateLimited(f"at most {self.limit} tokens per {self.window} s; try again in {wait} s",
                                  retry_after=wait)  # fmt: skip
            hits.append(now)
            if len(self._hits) > 10_000:  # keys of users who went quiet must not pile up forever
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                    del self._hits[k]
