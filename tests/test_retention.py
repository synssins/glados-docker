"""Tests for glados.autonomy.agents.retention_agent (Phase C)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from glados.autonomy.agents.retention_agent import RetentionAgent
from glados.core.conversation_db import ConversationDB


def _seed(db: ConversationDB, rows: list[tuple[str, float, int | None]]) -> None:
    """Seed the DB with messages: list of (content, ts, tier)."""
    for content, ts, tier in rows:
        db.append({"role": "user", "content": content}, ts=ts, tier=tier)


class TestAgePrune:
    def test_age_prune_keeps_tier1_and_tier2(self, tmp_path: Path) -> None:
        """tier=1 and tier=2 (device-control audit trail) survive an
        age-based sweep even when the chat around them is pruned."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            t0 = time.time()
            old = t0 - (40 * 86400)  # 40 days old
            _seed(db, [
                ("old chat",      old, 3),
                ("old action",    old, 1),
                ("old disambig",  old, 2),
                ("recent chat",   t0,  3),
            ])
            agent = RetentionAgent(db, max_days=30, max_disk_mb=500)
            agent.sweep_once()

            snap = db.snapshot()
            tiers = sorted(m.tier for m in snap)
            # tier=3 chat is gone; tier=1/2 + recent tier=3 remain.
            assert tiers == [1, 2, 3]
            contents = {m.content for m in snap}
            assert "old chat" not in contents
            assert "old action" in contents
            assert "old disambig" in contents
            assert "recent chat" in contents
        finally:
            db.close()

    def test_max_days_clamps_to_hard_cap(self, tmp_path: Path) -> None:
        """A misconfigured operator setting (max_days=999) gets clamped
        to the hard cap (180). We never keep raw transcripts longer
        than six months."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            agent = RetentionAgent(
                db, max_days=999, hard_cap_days=180, max_disk_mb=500,
            )
            assert agent._max_age_s == 180 * 86400
        finally:
            db.close()

    def test_recent_messages_untouched(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            t0 = time.time()
            _seed(db, [
                ("today chat", t0 - 60, 3),
                ("yesterday chat", t0 - 86400, 3),
            ])
            RetentionAgent(db, max_days=30, max_disk_mb=500).sweep_once()
            assert db.count() == 2
        finally:
            db.close()


class TestSizePrune:
    def test_under_cap_no_prune(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            for i in range(10):
                db.append({"role": "user", "content": f"msg-{i}"},
                          ts=time.time(), tier=3)
            n_before = db.count()
            r = RetentionAgent(db, max_days=30, max_disk_mb=500).sweep_once()
            assert r["size_pruned"] == 0
            assert db.count() == n_before
        finally:
            db.close()

    def test_above_cap_with_no_eligible_rows_warns_but_does_not_delete_action_log(
        self, tmp_path: Path,
    ) -> None:
        """Operator-tightness scenario: db is over cap but only contains
        tier=1 action audit. Don't silently delete the audit trail —
        warn and stop."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            t0 = time.time()
            for i in range(50):
                # tier=1 actions, all very recent
                db.append({"role": "user", "content": f"action-{i}"},
                          ts=t0 - 60, tier=1)
            agent = RetentionAgent(
                db, max_days=30, max_disk_mb=0,  # forced over cap
            )
            r = agent.sweep_once()
            # No tier=3 rows exist, so size_pruned must be 0.
            assert r["size_pruned"] == 0
            # All tier=1 rows still there.
            assert db.count() == 50
        finally:
            db.close()


class TestStatus:
    def test_status_dict_shape(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append({"role": "user", "content": "hi"}, tier=3)
            agent = RetentionAgent(db, max_days=7, max_disk_mb=10)
            status = agent.status()
            assert "max_age_days" in status
            assert status["max_age_days"] == 7
            assert "max_disk_mb" in status
            assert status["max_disk_mb"] == 10
            assert status["db_message_count"] == 1
            assert isinstance(status["db_size_mb"], float)
        finally:
            db.close()
