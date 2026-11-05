"""A small in-memory limiter for costly actions (like asking the AI). One process only."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindow:
    def __init__(self, limit: int, seconds: float):
        self.limit, self.seconds = limit, seconds
        self._hits: dict[object, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: object) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def retry_after(self, key: object) -> int:
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            return max(1, int(self.seconds - (time.monotonic() - hits[0])) + 1)