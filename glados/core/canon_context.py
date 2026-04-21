"""
Portal canon retrieval-augmented context for LLM chat (Phase 8.14).

Companion to :class:`glados.core.memory_context.MemoryContext`. Where
``MemoryContext`` surfaces *user* facts (household preferences,
explicit memories), :class:`CanonContext` surfaces *Portal universe*
facts seeded from ``configs/canon/`` by
:mod:`glados.memory.canon_loader`.

Retrieval is scoped via a ChromaDB ``where={"source": "canon"}``
filter so canon and user facts never bleed into each other — and the
existing ``MemoryContext`` review-status filter keeps canon out of
user-fact retrieval without any changes on that side.

Gated by :func:`glados.core.context_gates.needs_canon_context`: the
injection only fires on turns whose user message mentions a Portal-
canon trigger keyword. This keeps the ~400-token canon block off
ordinary household / chitchat turns.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class CanonContextConfig:
    """Tunables for canon retrieval + injection."""
    enabled: bool = True
    max_results: int = 4          # Per-turn cap on canon entries
    max_distance: float = 0.8     # Cosine distance threshold (looser than user-facts)


class CanonContext:
    """
    Retrieves Portal canon entries for the current user message and
    formats them for system-message injection.

    Usage::

        canon = CanonContext(memory_store)
        context_builder.register(
            "canon",
            lambda: canon.as_prompt(interaction_state.last_user_message),
            priority=6,
        )
    """

    def __init__(
        self,
        store: Any | None = None,
        config: CanonContextConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or CanonContextConfig()
        self._lock = threading.Lock()

    def set_store(self, store: Any) -> None:
        with self._lock:
            self._store = store

    def as_prompt(self, query: str = "") -> str | None:
        """Return a system-message string or None.

        Caller is responsible for running
        :func:`needs_canon_context` before invoking; this class does
        not re-gate internally. That way the ContextBuilder lambda
        and the SSE injection both share the same gate and the cost
        of a ChromaDB query is paid only on gated turns.
        """
        if not self._config.enabled:
            return None
        if not query.strip():
            return None
        with self._lock:
            store = self._store
        if store is None:
            return None

        try:
            raw = store.query(
                text=query,
                collection="semantic",
                n=self._config.max_results * 2,  # over-fetch then filter
                where={"source": "canon"},
            )
        except Exception as exc:
            logger.warning("CanonContext: query failed: {}", exc)
            return None

        filtered = [
            r for r in raw
            if r.get("distance", 1.0) <= self._config.max_distance
        ][: self._config.max_results]
        if not filtered:
            return None

        lines = [
            "[canon] Portal-universe facts you may draw on if relevant."
            " Speak them in your own voice; do not quote verbatim; do"
            " not invent details beyond what is written below."
        ]
        for r in filtered:
            doc = (r.get("document") or "").strip()
            if not doc:
                continue
            lines.append(f"- {doc}")

        if len(lines) <= 1:
            return None
        return "\n".join(lines)
