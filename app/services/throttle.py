"""In-process sliding-window rate limiting.

A small thread-safe limiter keyed by an arbitrary string (a client address,
an uploader key). It resets on restart and is not shared between processes.
It limits ``/api/v1/auth/login`` (plus the web login form), failed bearer
token lookups (Phase 6) and the public drop endpoint (Phase 14). Persistent
per-account lockout lives in ``users``, so throttling does not need the
database.

The number of tracked keys is capped: when the cap is reached, keys whose
window has expired are dropped first, then the least recently seen ones, so
a flood of distinct addresses cannot grow memory without bound.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from app.core.errors import TooManyRequestsError

DEFAULT_MAX_KEYS = 10_000


@dataclass
class LimitState:
    allowed: bool
    retry_after_seconds: int


class SlidingWindowThrottle:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock or time.monotonic
        # Insertion order doubles as recency order: a key is moved to the end
        # every time it is checked.
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._attempts)

    def _prune(self, entries: deque[float], now: float) -> deque[float]:
        window_start = now - self.window_seconds
        while entries and entries[0] < window_start:
            entries.popleft()
        return entries

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._attempts.items() if not self._prune(v, now)]:
            del self._attempts[key]
        while len(self._attempts) >= self.max_keys:
            del self._attempts[next(iter(self._attempts))]

    def check(self, key: str) -> LimitState:
        """Record an attempt and report whether it is allowed."""
        now = self._clock()
        with self._lock:
            entries = self._attempts.pop(key, None)
            if entries is None:
                if len(self._attempts) >= self.max_keys:
                    self._evict(now)
                entries = deque()
            self._attempts[key] = entries
            self._prune(entries, now)
            if len(entries) >= self.max_attempts:
                oldest = entries[0]
                retry_after = max(1, int((oldest + self.window_seconds) - now) + 1)
                return LimitState(allowed=False, retry_after_seconds=retry_after)
            entries.append(now)
            return LimitState(allowed=True, retry_after_seconds=0)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


# The login limiter's original name (Phases 2 and 6).
IpLoginThrottle = SlidingWindowThrottle


def assert_login_allowed(throttle: SlidingWindowThrottle, ip: str) -> None:
    """Raise 429 when the sliding window for ``ip`` is exhausted."""
    state = throttle.check(ip)
    if not state.allowed:
        raise TooManyRequestsError(
            "too many login attempts; try again later",
            details={"retry_after_seconds": state.retry_after_seconds},
        )
