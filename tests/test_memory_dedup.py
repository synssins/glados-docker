"""Tests for Stage 3 Phase 5: dedup-with-reinforcement.

Repetition of a passive-extracted fact must reinforce the existing
ChromaDB row (bump importance, increment mention_count, update
last_mentioned_at) instead of adding a new duplicate entry.

Contract verified here:
  - write_fact(source='passive') with no similar prior fact writes a
    new entry at importance=passive_base_importance.
  - write_fact(source='passive') with a similar approved entry within
    passive_dedup_threshold reinforces it in place.
  - Reinforcement bumps importance by passive_reinforce_step, capped
    at passive_importance_cap.
  - mention_count increments on each reinforcement.
  - original_importance is preserved (set on first write, carried
    through on every subsequent bump).
  - dedup only fires when the landing status is "approved"; pending
    rows stay distinct so the operator sees each mention.
"""

from __future__ import annotations

import time
from typing import Any

from glados.core.config_store import MemoryConfig
from glados.core.memory_writer import write_fact


class _DedupFakeStore:
    """Minimal MemoryStore double that honours query(where=...) and
    update(metadata_updates=...) so the dedup code path is exercised
    end-to-end without ChromaDB."""

    def __init__(self, canned_match: dict[str, Any] | None = None,
                 canned_distance: float = 0.10) -> None:
        self.semantic: list[dict[str, Any]] = []
        self._canned_match = canned_match
        self._canned_distance = canned_distance
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def add_semantic(self, text: str, metadata: dict[str, Any] | None = None,
                     entry_id: str | None = None) -> str:
        eid = entry_id or f"sem_{len(self.semantic)}"
        meta = dict(metadata or {})
        meta.setdefault("timestamp", time.time())
        self.semantic.append({"id": eid, "document": text, "metadata": meta})
        return eid

    def query(self, text: str, collection: str = "semantic",
              n: int = 5, where: dict[str, Any] | None = None
              ) -> list[dict[str, Any]]:
        # When a canned match is set AND the where clause asks for
        # approved rows, return it; otherwise return nothing. Mirrors
        # the ChromaDB contract: where filters are honoured server-side.
        if self._canned_match is None:
            return []
        if where and where.get("review_status") != "approved":
            return []
        return [dict(self._canned_match, distance=self._canned_distance)]

    def update(self, entry_id: str, collection: str = "semantic",
               *, document: str | None = None,
               metadata_updates: dict[str, Any] | None = None) -> bool:
        self.update_calls.append((entry_id, dict(metadata_updates or {})))
        # Mirror the update onto the canned match so subsequent dedup
        # reads see the bumped values (for chain-reinforcement tests).
        if self._canned_match is not None and \
                self._canned_match.get("id") == entry_id:
            meta = dict(self._canned_match.get("metadata") or {})
            if metadata_updates:
                meta.update(metadata_updates)
            self._canned_match["metadata"] = meta
        return True


# ---------------------------------------------------------------------------
# Dedup-with-reinforcement
# ---------------------------------------------------------------------------

class TestPassiveDedup:
    def test_new_passive_writes_fresh_row(self) -> None:
        """No similar prior fact → standard write path. Metadata carries
        the new Phase 5 fields."""
        store = _DedupFakeStore()
        ok = write_fact(store, "ResidentA likes dark roast coffee", source="passive")
        assert ok is True
        assert len(store.semantic) == 1
        meta = store.semantic[0]["metadata"]
        assert meta["review_status"] == "approved"
        assert meta["mention_count"] == 1
        assert meta["original_importance"] == meta["importance"]

    def test_similar_fact_reinforces_existing(self) -> None:
        """A canned near-duplicate → dedup fires: update() is called,
        no new add_semantic row."""
        existing = {
            "id": "sem_existing",
            "document": "ResidentA likes dark roast coffee",
            "metadata": {
                "review_status": "approved",
                "importance": 0.50,
                "original_importance": 0.50,
                "mention_count": 1,
            },
        }
        store = _DedupFakeStore(canned_match=existing, canned_distance=0.05)
        ok = write_fact(store, "ResidentA prefers dark roast", source="passive")
        assert ok is True
        assert store.semantic == []  # no new row
        assert len(store.update_calls) == 1
        entry_id, updates = store.update_calls[0]
        assert entry_id == "sem_existing"
        assert updates["mention_count"] == 2
        # passive_reinforce_step default 0.05 → 0.50 + 0.05 = 0.55
        assert abs(updates["importance"] - 0.55) < 1e-6
        assert "last_mentioned_at" in updates
        # last_mention_text carries the incoming wording so the Memory
        # UI can offer "Update from latest mention" without re-running
        # the LLM.
        assert updates["last_mention_text"] == "ResidentA prefers dark roast"
        assert updates["original_importance"] == 0.50

    def test_distant_fact_writes_new_row(self) -> None:
        """A canned match OUTSIDE the threshold → no reinforcement,
        new row written."""
        existing = {
            "id": "sem_other",
            "document": "unrelated topic",
            "metadata": {"review_status": "approved", "importance": 0.50},
        }
        cfg = MemoryConfig()
        above_threshold = cfg.passive_dedup_threshold + 0.05
        store = _DedupFakeStore(canned_match=existing,
                                 canned_distance=above_threshold)
        ok = write_fact(store, "ResidentA likes dark roast coffee", source="passive")
        assert ok is True
        assert len(store.semantic) == 1
        assert store.update_calls == []

    def test_importance_caps_at_configured_max(self) -> None:
        """Importance bump cannot exceed passive_importance_cap (0.95)."""
        existing = {
            "id": "sem_hot",
            "document": "ResidentA drinks coffee every morning",
            "metadata": {
                "review_status": "approved",
                "importance": 0.94,  # one bump away from cap
                "original_importance": 0.50,
                "mention_count": 9,
            },
        }
        store = _DedupFakeStore(canned_match=existing, canned_distance=0.02)
        write_fact(store, "ResidentA drinks coffee every morning", source="passive")
        _, updates = store.update_calls[0]
        cfg = MemoryConfig()
        assert updates["importance"] <= cfg.passive_importance_cap + 1e-9
        # 0.94 + 0.05 = 0.99 → clamped to 0.95
        assert updates["importance"] == cfg.passive_importance_cap

    def test_pending_landing_skips_dedup(self) -> None:
        """When the caller forces review_status='pending' (Phase D
        review-queue flow), each mention must be stored distinctly so
        the operator can triage them one-by-one."""
        existing = {
            "id": "sem_pending",
            "document": "ResidentA likes dark roast coffee",
            "metadata": {"review_status": "approved", "importance": 0.50},
        }
        store = _DedupFakeStore(canned_match=existing, canned_distance=0.05)
        ok = write_fact(store, "ResidentA prefers dark roast",
                         source="passive", review_status="pending")
        assert ok is True
        assert len(store.semantic) == 1
        assert store.semantic[0]["metadata"]["review_status"] == "pending"
        assert store.update_calls == []

    def test_explicit_source_never_deduplicates(self) -> None:
        """Dedup applies only to passive source. Operator-initiated
        facts (remember that ...) always write a new row so the
        explicit note is not silently folded into a lookalike."""
        existing = {
            "id": "sem_explicit_hit",
            "document": "ResidentA likes dark roast coffee",
            "metadata": {"review_status": "approved", "importance": 0.90},
        }
        store = _DedupFakeStore(canned_match=existing, canned_distance=0.05)
        ok = write_fact(store, "ResidentA prefers dark roast",
                         source="explicit", importance=0.9)
        assert ok is True
        assert len(store.semantic) == 1
        assert store.update_calls == []
