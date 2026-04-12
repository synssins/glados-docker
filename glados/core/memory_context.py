"""
ChromaDB-backed memory context for LLM prompt injection.

Replaces the flat facts.jsonl reader with semantic vector search.
On each request, queries ChromaDB with the current user message
and injects only the top N most relevant facts/summaries.

Architecture:
  Write path: CompactionAgent → MemoryStore.add_semantic()
  Read path:  MemoryContext.as_prompt(query) → ChromaDB cosine search → top N results

Platform note: Uses pathlib and env-resolved paths — works on Windows and Linux.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class MemoryContextConfig:
    """Configuration for ChromaDB-backed memory context injection."""
    enabled: bool = True
    max_results: int = 5          # Max facts to inject per request
    max_distance: float = 0.7     # Cosine distance threshold (0=identical, 1=unrelated)
    min_importance: float = 0.0   # Min importance score (0-1) to include
    include_age: bool = True       # Include how old the fact is in prompt
    fallback_to_recent: bool = True  # If query yields nothing, inject most recent N facts


class MemoryContext:
    """
    Semantic memory context — queries ChromaDB with current user message.

    Instead of dumping all facts into context, retrieves only the
    top N facts most semantically relevant to the current conversation turn.
    Typical injection: 50-150 tokens vs 200-400 for flat-file approach.

    Usage:
        store = MemoryStore(host="localhost", port=8000)
        memory = MemoryContext(store, config)
        context_builder.register("memory", lambda: memory.as_prompt(last_message), priority=7)
    """

    def __init__(
        self,
        store: Any | None = None,  # MemoryStore — optional for graceful degradation
        config: MemoryContextConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or MemoryContextConfig()
        self._lock = threading.Lock()
        self._last_query: str = ""

    def set_store(self, store: Any) -> None:
        """Set the MemoryStore instance (called after ChromaDB connects)."""
        with self._lock:
            self._store = store

    def as_prompt(self, query: str = "") -> str | None:
        """
        Build a memory context prompt for the given query.

        Args:
            query: The current user message to use as semantic search query.
                   If empty, falls back to recent facts if configured.

        Returns:
            Formatted string for injection as system message, or None if no
            relevant memories found.
        """
        if not self._config.enabled:
            return None

        with self._lock:
            store = self._store

        if store is None:
            return None

        # Try semantic query first
        results = []
        if query.strip():
            try:
                raw = store.query(
                    text=query,
                    collection="semantic",
                    n=self._config.max_results,
                )
                # Filter by distance threshold
                results = [
                    r for r in raw
                    if r.get("distance", 1.0) <= self._config.max_distance
                ]
            except Exception as exc:
                logger.warning("MemoryContext: query failed: {}", exc)

        # Fallback to most recent facts if query empty or no results
        if not results and self._config.fallback_to_recent:
            try:
                raw = store.query(
                    text="GLaDOS home assistant memory facts",
                    collection="semantic",
                    n=self._config.max_results,
                )
                results = raw[:self._config.max_results]
            except Exception as exc:
                logger.warning("MemoryContext: fallback query failed: {}", exc)

        if not results:
            return None

        lines = ["[memory] Relevant facts from long-term memory:"]
        now = time.time()

        for result in results:
            doc = result.get("document", "").strip()
            if not doc:
                continue

            meta = result.get("metadata", {})
            importance = float(meta.get("importance", 0.5))

            if importance < self._config.min_importance:
                continue

            line = f"- {doc}"

            if self._config.include_age:
                ts = float(meta.get("timestamp", 0))
                if ts > 0:
                    line += f" [{self._format_age(now - ts)}]"

            lines.append(line)

        if len(lines) <= 1:
            return None

        return "\n".join(lines)

    @staticmethod
    def _format_age(seconds: float) -> str:
        """Format elapsed seconds as human-readable age."""
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds / 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds / 3600)}h ago"
        days = int(seconds / 86400)
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{int(days / 7)}w ago"
        return f"{int(days / 30)}mo ago"
