"""Chat / autonomy priority coordination.

Single-GPU deployments share one Ollama instance between the user-
facing chat path (Tier 1 / Tier 2 / Tier 3 / persona rewriter) and
the background autonomy loop. Without coordination, an autonomy tick
landing at the same moment as a user chat can exhaust Tier 2's 25-
second disambiguator budget (observed 2026-04-19) and fall the user
through to the slow Tier 3 path.

This module exposes a single process-wide primitive:

    with chat_in_flight():
        # call Ollama on behalf of the user

    if is_chat_in_flight():
        # caller is the autonomy loop — yield this tick

Chat-path callers always proceed; autonomy callers check the flag and
back off. A small grace window after chat finishes stays "busy" so
a rapid series of user turns doesn't let autonomy wedge in between.

The module is deliberately simple: a counter + a last-busy timestamp
guarded by a lock. No external state, no config knobs — operators
who want more elaborate priority can still set `OLLAMA_AUTONOMY_URL`
to split hardware.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


# How long the "chat is busy" state lingers after the last chat call
# returns. Smooths over the gap between a user's first chat turn and
# any follow-up Tier 2 / rewriter call the same turn triggers.
_GRACE_AFTER_CHAT_S: float = 2.0


_lock = threading.Lock()
_active_chats: int = 0
_last_chat_end_ts: float = 0.0


def _now() -> float:
    return time.monotonic()


def is_chat_in_flight() -> bool:
    """True when any chat-path caller is currently holding the gate,
    or when the last such caller finished within the grace window.

    Autonomy callers should check this BEFORE making an Ollama request
    and skip the tick when it returns True.
    """
    with _lock:
        if _active_chats > 0:
            return True
        return (_now() - _last_chat_end_ts) < _GRACE_AFTER_CHAT_S


@contextmanager
def chat_in_flight() -> Iterator[None]:
    """Context manager chat-path callers wrap around their Ollama call.

    Safe to nest (Tier 1 fast-path calls through to the rewriter, which
    also grabs the lock). Each enter increments, each exit decrements;
    the last exit stamps the grace-window start time.
    """
    global _active_chats, _last_chat_end_ts
    with _lock:
        _active_chats += 1
    try:
        yield
    finally:
        with _lock:
            _active_chats -= 1
            if _active_chats == 0:
                _last_chat_end_ts = _now()
