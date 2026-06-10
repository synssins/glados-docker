"""Event->Action engine -- HA executes, GLaDOS decides.

Spec: docs/superpowers/specs/2026-06-09-event-action-engine-design.md
"""
from __future__ import annotations

_router = None


def set_router(router) -> None:
    global _router
    _router = router


def get_router():
    """The process-wide EventRouter, or None before engine init."""
    return _router
