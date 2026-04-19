"""SessionMemory — short-term per-session conversation memory.

Scoped to a `session_id` (from `SourceContext.session_id`), keyed in
memory only (no disk persistence — that's the job of `conversation_db`
for the full conversation and `learned_context` for durable patterns).

Used for the "brighter" / "turn them off" / "and the office" class of
follow-up commands where the next utterance only makes sense if the
resolver remembers what the last one did.

Semantics:

  - Ring buffer of the last N turns per session (default 10).
  - Sessions expire after `idle_ttl_seconds` of inactivity (default 10
    min), so a user who walks away and comes back a day later isn't
    disambiguated by yesterday's commands.
  - Thread-safe. Protected by a single RLock — memory store is tiny
    and lookup is frequent, so a global lock beats per-session locks
    on cognitive overhead.

This is a **separate** store from `conversation_db`. The conversation
DB records every message durably; SessionMemory records only the
resolved-action side of device-control turns, in the exact shape the
resolver needs, and throws it away on idle. Keeping them separate
means a restart doesn't revive stale "turn them off" context, and the
resolver's lookup stays O(1) without hitting SQLite.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


DEFAULT_BUFFER_SIZE = 10
DEFAULT_IDLE_TTL_SECONDS = 600  # 10 min


@dataclass(frozen=True)
class Turn:
    """One resolved device-control turn. Immutable so callers can hold
    a reference without risk of mutation between lookup and use.

    `entities_affected` is a list (not a set) to preserve the order HA
    returned them in — useful when the next utterance references "the
    first one" / "the other one".

    `ha_conversation_id` is HA's own multi-turn context handle. The
    resolver threads the last one back into the next `bridge.process`
    call so follow-ups like "All lights" after "turn off the whole
    house" inherit HA's verb context.
    """

    timestamp: float
    utterance: str
    resolved_area_id: str | None
    entities_affected: tuple[str, ...]
    action_verb: str | None
    service: str | None = None
    service_data: dict[str, Any] = field(default_factory=dict)
    ha_conversation_id: str | None = None


class SessionMemory:
    """In-memory session store with idle TTL.

    Not a singleton — the engine constructs one instance and passes it
    to the resolver. Tests build disposable instances.
    """

    def __init__(
        self,
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        now_fn: "_NowFn | None" = None,
    ) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size must be >= 1")
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be > 0")
        self._buffer_size = buffer_size
        self._ttl = idle_ttl_seconds
        self._now = now_fn or time.time
        self._lock = threading.RLock()
        # session_id -> (last_touched, deque[Turn])
        self._sessions: dict[str, _SessionState] = {}

    # ---- Mutators ----------------------------------------------------

    def record_turn(self, session_id: str, turn: Turn) -> None:
        """Append a turn to the session. Creates the session if new.

        Calling `record_turn` also refreshes the session's idle timer —
        recording IS activity. GC of other expired sessions is
        piggybacked here so we don't need a background sweeper for a
        store this small.
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self._lock:
            state = self._sessions.get(session_id)
            now = self._now()
            if state is None:
                state = _SessionState(deque(maxlen=self._buffer_size), now)
                self._sessions[session_id] = state
            state.turns.append(turn)
            state.last_touched = now
            self._gc_locked(now)

    # ---- Readers -----------------------------------------------------

    def recent_turns(self, session_id: str, limit: int | None = None) -> list[Turn]:
        """Return the most-recent turns for a session, newest last.

        Returns an empty list for unknown or expired sessions. The
        lookup also advances the idle timer — a read of "what did we
        last do" counts as activity, because the user is clearly
        still engaged.
        """
        with self._lock:
            now = self._now()
            state = self._sessions.get(session_id)
            if state is None or self._is_expired(state, now):
                # If expired, drop it opportunistically.
                if state is not None:
                    self._sessions.pop(session_id, None)
                return []
            state.last_touched = now
            turns = list(state.turns)
            if limit is not None and limit >= 0:
                # turns[-0:] returns the full list, so guard limit == 0
                # explicitly rather than relying on slice semantics.
                turns = turns[-limit:] if limit > 0 else []
            return turns

    def last_turn(self, session_id: str) -> Turn | None:
        """Shortcut for 'just the most recent turn' — the common
        lookup for 'turn them off' / 'brighter' resolution."""
        turns = self.recent_turns(session_id, limit=1)
        return turns[-1] if turns else None

    # ---- Admin -------------------------------------------------------

    def forget(self, session_id: str) -> None:
        """Drop a session immediately. Used by 'never mind' and for
        tests."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Drop all sessions. Useful for tests and 'nuke everything'
        admin operations."""
        with self._lock:
            self._sessions.clear()

    def active_session_count(self) -> int:
        """Count of sessions with at least one non-expired turn.

        Opportunistically sweeps expired sessions so the count is
        accurate rather than inflated by stale entries. Cheap enough
        to call from health endpoints.
        """
        with self._lock:
            self._gc_locked(self._now())
            return len(self._sessions)

    # ---- Internals ---------------------------------------------------

    def _is_expired(self, state: _SessionState, now: float) -> bool:
        return (now - state.last_touched) > self._ttl

    def _gc_locked(self, now: float) -> None:
        """Sweep expired sessions. Caller must hold the lock."""
        expired = [sid for sid, s in self._sessions.items() if self._is_expired(s, now)]
        for sid in expired:
            self._sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

@dataclass
class _SessionState:
    turns: "deque[Turn]"
    last_touched: float


# Protocol-free alias; tests use it to pass a fake clock callable.
_NowFn = "type(time.time)"  # noqa: ERA001  — documentation-only alias
