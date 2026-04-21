"""Phase 8.14 — CanonContext retrieval + formatting.

Uses a stub MemoryStore that records queries and returns canned
results. Covers: ``where={"source":"canon"}`` filter plumbing, max-
result capping, distance thresholding, empty-query no-op, prompt
format (header line + bulleted entries), and graceful degradation
when the store is missing or raises.
"""
from __future__ import annotations

from typing import Any

import pytest

from glados.core.canon_context import CanonContext, CanonContextConfig


class _RecordingStore:
    def __init__(self, results: list[dict[str, Any]] | None = None,
                 raise_on_query: bool = False) -> None:
        self.results = results or []
        self.raise_on_query = raise_on_query
        self.calls: list[dict[str, Any]] = []

    def query(self, text: str, collection: str = "semantic",
              n: int = 5, where: dict | None = None) -> list[dict]:
        self.calls.append({"text": text, "collection": collection,
                           "n": n, "where": where})
        if self.raise_on_query:
            raise RuntimeError("boom")
        return list(self.results)


def _entry(doc: str, distance: float = 0.3, topic: str = "glados_arc") -> dict:
    return {
        "id": f"canon_{topic}_{abs(hash(doc)) & 0xfff:03x}",
        "document": doc,
        "metadata": {"source": "canon", "topic": topic, "review_status": "canon"},
        "distance": distance,
    }


class TestRetrieval:
    def test_returns_none_when_no_store(self) -> None:
        assert CanonContext(store=None).as_prompt("anything") is None

    def test_returns_none_on_empty_query(self) -> None:
        store = _RecordingStore(results=[_entry("fact.")])
        ctx = CanonContext(store=store)
        assert ctx.as_prompt("") is None
        assert ctx.as_prompt("   ") is None
        assert store.calls == []  # gate short-circuits before hitting store

    def test_filters_by_source_canon(self) -> None:
        store = _RecordingStore(results=[_entry("fact.")])
        ctx = CanonContext(store=store)
        ctx.as_prompt("tell me about potato")
        assert len(store.calls) == 1
        assert store.calls[0]["where"] == {"source": "canon"}
        assert store.calls[0]["collection"] == "semantic"

    def test_disabled_config_short_circuits(self) -> None:
        store = _RecordingStore(results=[_entry("fact.")])
        ctx = CanonContext(store=store, config=CanonContextConfig(enabled=False))
        assert ctx.as_prompt("tell me about potato") is None
        assert store.calls == []

    def test_max_results_caps_output(self) -> None:
        store = _RecordingStore(results=[
            _entry(f"fact {i}", distance=0.1 * i) for i in range(10)
        ])
        ctx = CanonContext(store=store, config=CanonContextConfig(max_results=3))
        out = ctx.as_prompt("query")
        assert out is not None
        # header + up to 3 entries
        assert out.count("\n- ") == 3

    def test_distance_filter_drops_far_matches(self) -> None:
        store = _RecordingStore(results=[
            _entry("close", distance=0.2),
            _entry("also close", distance=0.5),
            _entry("too far", distance=0.95),
        ])
        ctx = CanonContext(store=store, config=CanonContextConfig(max_results=5, max_distance=0.8))
        out = ctx.as_prompt("query")
        assert out is not None
        assert "close" in out
        assert "also close" in out
        assert "too far" not in out

    def test_empty_result_returns_none(self) -> None:
        store = _RecordingStore(results=[])
        ctx = CanonContext(store=store)
        assert ctx.as_prompt("query with no matches") is None

    def test_store_exception_is_swallowed(self) -> None:
        store = _RecordingStore(raise_on_query=True)
        ctx = CanonContext(store=store)
        # Must not raise; graceful degradation like memory_context does.
        assert ctx.as_prompt("anything") is None


class TestPromptFormat:
    def test_output_starts_with_canon_header(self) -> None:
        store = _RecordingStore(results=[_entry("The neurotoxin killed the staff.")])
        ctx = CanonContext(store=store)
        out = ctx.as_prompt("neurotoxin?")
        assert out is not None
        first_line = out.splitlines()[0]
        assert first_line.startswith("[canon]")
        # Guard-rail phrasing against verbatim quoting / confabulation.
        assert "own voice" in out or "not quote" in out or "do not invent" in out

    def test_entries_rendered_as_bullets(self) -> None:
        store = _RecordingStore(results=[
            _entry("Alpha fact."),
            _entry("Beta fact."),
        ])
        ctx = CanonContext(store=store)
        out = ctx.as_prompt("query")
        assert out is not None
        assert "- Alpha fact." in out
        assert "- Beta fact." in out

    def test_blank_documents_skipped(self) -> None:
        store = _RecordingStore(results=[
            _entry("Real fact."),
            {"id": "x", "document": "   ", "metadata": {}, "distance": 0.1},
            {"id": "y", "document": "", "metadata": {}, "distance": 0.1},
        ])
        ctx = CanonContext(store=store)
        out = ctx.as_prompt("query")
        assert out is not None
        assert "- Real fact." in out
        # One entry only — no stray blank bullets.
        assert out.count("\n- ") == 1


class TestSetStore:
    def test_can_attach_store_after_construction(self) -> None:
        ctx = CanonContext(store=None)
        assert ctx.as_prompt("anything") is None
        ctx.set_store(_RecordingStore(results=[_entry("Late-bound fact.")]))
        out = ctx.as_prompt("anything")
        assert out is not None and "Late-bound fact." in out
