"""Per-IP token-bucket limiter for unauth service endpoints.

In-memory state; restart clears it. Phase 2 may add SQLite persistence.
See docs/AUTH_DESIGN.md §8.2.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    capacity: int
    window_seconds: float
    _state: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        """Return True if this request is allowed; consumes one token.
        False if the bucket is empty for this key.

        Refill rate: `capacity` tokens per `window_seconds`. Tokens
        accrue continuously, capped at `capacity`. Each accepted call
        consumes exactly one token.
        """
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.capacity), now))
            elapsed = now - last
            refilled = min(
                float(self.capacity),
                tokens + (elapsed / self.window_seconds) * self.capacity,
            )
            if refilled < 1.0:
                self._state[key] = (refilled, now)
                return False
            self._state[key] = (refilled - 1.0, now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Test helper. Clears state for one key or all keys."""
        with self._lock:
            if key is None:
                self._state.clear()
            else:
                self._state.pop(key, None)
