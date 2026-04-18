"""Tests for Stage 3 Phase D: passive memory review queue.

These tests use a fake MemoryStore that records writes/reads in
memory rather than hitting a real ChromaDB. The contract verified
here:

  - write_fact() with source='passive' assigns review_status='pending'
    automatically (so passive extraction never silently enters RAG)
  - write_fact() with source='explicit' assigns review_status='approved'
  - write_fact(review_status=...) overrides the auto-derivation
  - MemoryContext filters retrieved results so review_status='pending'
    facts are NOT injected into the LLM prompt (the whole point of
    the review queue: hold facts until the operator promotes them)
  - Legacy facts (no review_status field) ARE returned by RAG (they
    predate Phase D — must not be excluded silently)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from glados.core.memory_context import MemoryContext, MemoryContextConfig
from glados.core.memory_writer import write_fact


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeMemoryStore:
    """Records every add_semantic call; supports query() returning a
    canned list. Mirrors the real MemoryStore method shapes."""

    def __init__(self) -> None:
        self.semantic: list[dict[str, Any]] = []  # full entries
        self._query_returns: list[dict[str, Any]] = []

    def add_semantic(self, text: str, metadata: dict[str, Any] | None = None,
                     entry_id: str | None = None) -> str:
        eid = entry_id or f"sem_{len(self.semantic)}"
        meta = dict(metadata or {})
        meta.setdefault("timestamp", time.time())
        self.semantic.append({"id": eid, "document": text, "metadata": meta})
        return eid

    def list_by_status(self, status: str, collection: str = "semantic",
                       limit: int = 100) -> list[dict[str, Any]]:
        return [
            e for e in self.semantic
            if e["metadata"].get("review_status") == status
        ][:limit]

    def query(self, text: str, collection: str = "semantic",
              n: int = 5, where: dict[str, Any] | None = None
              ) -> list[dict[str, Any]]:
        # Returns whatever was set as canned response; ignores the query
        # text (we're testing filtering, not similarity).
        return [dict(e, distance=0.1) for e in self._query_returns][:n]


# ---------------------------------------------------------------------------
# write_fact review_status assignment
# ---------------------------------------------------------------------------

class TestWriteFactReviewStatus:
    def test_explicit_default_approved(self) -> None:
        """User-typed 'remember that...' is high-trust — auto-approved
        and immediately RAG-eligible."""
        store = _FakeMemoryStore()
        ok = write_fact(store, "Chris likes dark roast",
                         source="explicit", importance=0.9)
        assert ok is True
        assert store.semantic[0]["metadata"]["review_status"] == "approved"

    def test_passive_default_approved(self) -> None:
        """Phase 5 refinement: passive-extracted facts default to
        'approved' and enter RAG immediately. Repetition reinforces via
        ChromaDB dedup instead of queueing for operator promotion. See
        test_memory_dedup.py for the reinforcement contract and
        test_passive_status_pending_override for the legacy flow."""
        store = _FakeMemoryStore()
        ok = write_fact(store, "Chris went to bed at 10pm",
                         source="passive", importance=0.6)
        assert ok is True
        assert store.semantic[0]["metadata"]["review_status"] == "approved"

    def test_passive_status_pending_override(self) -> None:
        """Operator can restore the Phase D review-queue flow by passing
        review_status='pending' (or setting MemoryConfig.passive_default_status).
        New facts then wait for operator promotion before entering RAG."""
        store = _FakeMemoryStore()
        ok = write_fact(store, "Chris went to bed at 10pm",
                         source="passive", importance=0.6,
                         review_status="pending")
        assert ok is True
        assert store.semantic[0]["metadata"]["review_status"] == "pending"

    def test_compaction_default_approved(self) -> None:
        """Compaction summaries are operator-curated artifacts of
        prior conversation — trust by default."""
        store = _FakeMemoryStore()
        write_fact(store, "Today's discussion summary...",
                    source="compaction", importance=0.5)
        assert store.semantic[0]["metadata"]["review_status"] == "approved"

    def test_explicit_review_status_overrides_default(self) -> None:
        """Caller can force a status; useful for migrations or tests."""
        store = _FakeMemoryStore()
        write_fact(store, "test", source="explicit",
                   review_status="rejected")
        assert store.semantic[0]["metadata"]["review_status"] == "rejected"

    def test_empty_fact_not_written(self) -> None:
        store = _FakeMemoryStore()
        ok = write_fact(store, "   ", source="explicit")
        assert ok is False
        assert store.semantic == []


# ---------------------------------------------------------------------------
# MemoryContext RAG filter
# ---------------------------------------------------------------------------

class TestMemoryContextFiltering:
    def test_pending_facts_excluded_from_rag(self) -> None:
        """The whole point of the review queue: a 'pending' fact must
        NOT show up in MemoryContext.as_prompt() output. Otherwise the
        LLM would treat unreviewed extractions as ground truth."""
        store = _FakeMemoryStore()
        store._query_returns = [
            {"id": "1", "document": "approved fact",
             "metadata": {"review_status": "approved", "timestamp": time.time()}},
            {"id": "2", "document": "pending fact",
             "metadata": {"review_status": "pending", "timestamp": time.time()}},
            {"id": "3", "document": "rejected fact",
             "metadata": {"review_status": "rejected", "timestamp": time.time()}},
        ]
        ctx = MemoryContext(store=store, config=MemoryContextConfig())
        prompt = ctx.as_prompt("anything")
        assert prompt is not None
        assert "approved fact" in prompt
        assert "pending fact" not in prompt
        assert "rejected fact" not in prompt

    def test_legacy_facts_without_review_status_still_included(self) -> None:
        """Pre-Phase-D facts have no review_status field. They MUST
        still surface in RAG — silently dropping them would hide
        operator-curated knowledge that's been there for months."""
        store = _FakeMemoryStore()
        store._query_returns = [
            {"id": "old", "document": "legacy fact (no status field)",
             "metadata": {"timestamp": time.time()}},
        ]
        ctx = MemoryContext(store=store, config=MemoryContextConfig())
        prompt = ctx.as_prompt("anything")
        assert prompt is not None
        assert "legacy fact" in prompt

    def test_all_pending_returns_no_prompt(self) -> None:
        """If every retrieved candidate is filtered out, return None
        (skip the system message entirely) rather than an empty
        '[memory] ...' header."""
        store = _FakeMemoryStore()
        store._query_returns = [
            {"id": str(i), "document": f"fact-{i}",
             "metadata": {"review_status": "pending", "timestamp": time.time()}}
            for i in range(5)
        ]
        ctx = MemoryContext(store=store, config=MemoryContextConfig())
        prompt = ctx.as_prompt("anything")
        assert prompt is None
