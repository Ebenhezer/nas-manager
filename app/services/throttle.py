"""Per-IP login rate limiting.

A small in-process sliding-window limiter keyed by client address. It is
deliberately simple and thread-safe; it resets on restart and only limits
``/api/v1/auth/login`` (plus the web login form). Persistent per-account
lockout lives in ``users``, so throttling does not need the database.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from app.core.errors import TooManyRequestsError


@dataclass
class LimitState:
    allowed: bool
    retry_after_seconds: int


class IpLoginThrottle:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> deque[float]:
        window_start = now - self.window_seconds
        entries = self._attempts[ip]
        while entries and entries[0] < window_start:
            entries.popleft()
        return entries

    def check(self, ip: str) -> LimitState:
        """Record an attempt and report whether it is allowed."""
        now = self._clock()
        with self._lock:
            entries = self._prune(ip, now)
            if len(entries) >= self.max_attempts:
                oldest = entries[0]
                retry_after = max(1, int((oldest + self.window_seconds) - now) + 1)
                return LimitState(allowed=False, retry_after_seconds=retry_after)
            entries.append(now)
            return LimitState(allowed=True, retry_after_seconds=0)

    def clear(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


def assert_login_allowed(throttle: IpLoginThrottle, ip: str) -> None:
    """Raise 429 when the sliding window for ``ip`` is exhausted."""
    state = throttle.check(ip)
    if not state.allowed:
        raise TooManyRequestsError(
            "too many login attempts; try again later",
            details={"retry_after_seconds": state.retry_after_seconds},
        )
