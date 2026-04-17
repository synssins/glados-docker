"""Home Assistant WebSocket client + EntityCache.

Stage 3 Phase 1. The HAClient is the single authoritative channel for
all HA interaction from GLaDOS: live entity state mirror, service
calls, and conversation/process for Tier 1 intent resolution.

Design notes:
- One persistent WebSocket connection, in its own asyncio loop running
  in a dedicated thread. Thread-safe `call` API for threaded callers
  (tts_ui, api_wrapper) that need to invoke HA synchronously.
- EntityCache is written only by the WS loop, read by anyone. Reads
  use a short-lived snapshot copy to avoid holding the writer lock.
- Reconnect with exponential backoff. On reconnect, re-run `get_states`
  to resync the full cache — state_changed events during the
  disconnect window are lost by design; resync covers them.
"""

from __future__ import annotations

import threading

from .conversation import ConversationBridge, ConversationResult, classify
from .entity_cache import CandidateMatch, EntityCache, EntityState
from .ws_client import HAClient


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
# Initialized by `server.py` during startup. Accessible from any thread.
# `None` means Stage 3 Phase 1 hasn't initialized yet (or is disabled).

_HA_CLIENT: HAClient | None = None
_BRIDGE: ConversationBridge | None = None
_CACHE: EntityCache | None = None
_LOCK = threading.Lock()


def init_singletons(
    ha_client: HAClient,
    bridge: ConversationBridge,
    entity_cache: EntityCache,
) -> None:
    """Install the process-wide HA singletons. Called from server startup."""
    global _HA_CLIENT, _BRIDGE, _CACHE
    with _LOCK:
        _HA_CLIENT = ha_client
        _BRIDGE = bridge
        _CACHE = entity_cache


def get_client() -> HAClient | None:
    return _HA_CLIENT


def get_bridge() -> ConversationBridge | None:
    return _BRIDGE


def get_cache() -> EntityCache | None:
    return _CACHE


__all__ = [
    "CandidateMatch",
    "ConversationBridge",
    "ConversationResult",
    "EntityCache",
    "EntityState",
    "HAClient",
    "classify",
    "get_bridge",
    "get_cache",
    "get_client",
    "init_singletons",
]
