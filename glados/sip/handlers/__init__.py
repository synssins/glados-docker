"""IVR handlers for SIP inbound calls.

Each handler is a pure async function ``render(ctx) -> str`` that
formats a one-paragraph response from current system state. NO LLM
calls — handlers are deterministic, fast, predictable. Free-form is
what the ``0`` drop-key in the IVR is for.

Adding a handler:
1. Create ``glados/sip/handlers/my_handler.py`` with an async
   ``render(ctx)`` function.
2. Add an item to ``configs/sip.yaml`` ``inbound.ivr_menu.items``
   with ``handler: my_handler``.
3. Register it in ``HANDLERS`` below.

The ``HandlerContext`` carries the data sources a handler needs.
``call_session`` constructs the real one with live HA / audit /
doorbell handles; tests inject mocks.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class _HasFetchStates(Protocol):
    async def fetch_states(self) -> list[Any]: ...


@dataclass
class HandlerContext:
    """Injected data sources for IVR handlers.

    All fields are optional — a handler that doesn't need a particular
    source just doesn't read it. Tests can construct a context with
    only the fields the handler under test reads.
    """
    ha_states: list[dict[str, Any]] | None = None    # snapshot of HA /api/states
    audit_recent: list[dict[str, Any]] | None = None  # recent audit-log rows
    doorbell_events: list[dict[str, Any]] | None = None  # recent doorbell events


HandlerFn = Callable[[HandlerContext], Awaitable[str]]


# Lazy imports to avoid a hard dependency cycle if any handler ever
# needs to import from the parent package.
def _get_handlers() -> dict[str, HandlerFn]:
    from glados.sip.handlers import (
        doorbell_recent,
        door_locks,
        house_status,
        security_state,
    )
    return {
        "house_status": house_status.render,
        "security_state": security_state.render,
        "door_locks": door_locks.render,
        "doorbell_recent": doorbell_recent.render,
    }


def get_handler(name: str) -> HandlerFn:
    """Look up a handler by name; raise KeyError if unknown."""
    handlers = _get_handlers()
    if name not in handlers:
        raise KeyError(
            f"unknown IVR handler {name!r}; available: {sorted(handlers)}"
        )
    return handlers[name]


__all__ = ["HandlerContext", "HandlerFn", "get_handler"]
