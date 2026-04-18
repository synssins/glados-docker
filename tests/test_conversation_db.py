"""Tests for glados.core.conversation_db (Phase B SQLite backing)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from glados.core.conversation_db import ConversationDB, StoredMessage


def _msg(role: str, content: str, **extra) -> dict:
    out = {"role": role, "content": content}
    out.update(extra)
    return out


class TestSchema:
    def test_open_creates_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conv.db"
        db = ConversationDB(db_path)
        try:
            assert db_path.exists()
        finally:
            db.close()

    def test_idempotent_open(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conv.db"
        ConversationDB(db_path).close()
        # Second open against the same file must not raise / not duplicate
        # tables. Reopening is the restart-resilience case.
        db = ConversationDB(db_path)
        try:
            assert db.count() == 0
        finally:
            db.close()


class TestAppendAndSnapshot:
    def test_append_assigns_monotonic_idx(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("system", "preprompt"))
            db.append(_msg("user", "hi"))
            db.append(_msg("assistant", "hello"))
            snap = db.snapshot()
            assert [m.idx for m in snap] == [0, 1, 2]
            assert [m.role for m in snap] == ["system", "user", "assistant"]
            assert [m.content for m in snap] == ["preprompt", "hi", "hello"]
        finally:
            db.close()

    def test_append_many_atomic(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            ids = db.append_many([
                _msg("user", "one"),
                _msg("assistant", "two"),
                _msg("user", "three"),
            ])
            assert len(ids) == 3
            assert db.count() == 3
        finally:
            db.close()

    def test_extra_fields_round_trip(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("tool", "result-blob",
                           tool_call_id="call-123", name="HassTurnOff"))
            snap = db.snapshot()
            assert len(snap) == 1
            chat = snap[0].to_chat_message()
            assert chat["tool_call_id"] == "call-123"
            assert chat["name"] == "HassTurnOff"
        finally:
            db.close()

    def test_internal_underscore_fields_dropped(self, tmp_path: Path) -> None:
        """`_origin`, `_principal` etc. on the queue items are runtime
        flags, not part of the LLM message contract. They should NOT
        round-trip through SQLite."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("user", "hi", _origin="webui_chat", _enqueued_at=99.0))
            snap = db.snapshot()
            chat = snap[0].to_chat_message()
            assert "_origin" not in chat
            assert "_enqueued_at" not in chat
        finally:
            db.close()

    def test_tool_calls_json_round_trip(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            tc = [{"id": "x", "function": {"name": "y", "arguments": "{}"}}]
            db.append(_msg("assistant", "", tool_calls=tc))
            snap = db.snapshot()
            assert snap[0].tool_calls == tc
            assert snap[0].to_chat_message()["tool_calls"] == tc
        finally:
            db.close()

    def test_persistence_across_reopen(self, tmp_path: Path) -> None:
        """The whole point of Phase B: surviving container restarts."""
        path = tmp_path / "conv.db"
        db1 = ConversationDB(path)
        db1.append(_msg("user", "remember me"))
        db1.append(_msg("assistant", "I will."))
        db1.close()

        db2 = ConversationDB(path)
        try:
            snap = db2.snapshot()
            assert [m.content for m in snap] == ["remember me", "I will."]
        finally:
            db2.close()

    def test_snapshot_limit_returns_most_recent(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            for i in range(10):
                db.append(_msg("user", f"msg-{i}"))
            recent = db.snapshot(limit=3)
            assert [m.content for m in recent] == ["msg-7", "msg-8", "msg-9"]
        finally:
            db.close()


class TestMetadata:
    def test_source_principal_tier_persisted(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("user", "hi"),
                      source="webui_chat", principal="alice",
                      tier=1, ha_conversation_id="ha-abc")
            snap = db.snapshot()
            m = snap[0]
            assert m.source == "webui_chat"
            assert m.principal == "alice"
            assert m.tier == 1
            assert m.ha_conversation_id == "ha-abc"
        finally:
            db.close()

    def test_latest_ha_conversation_id(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            assert db.latest_ha_conversation_id() is None
            db.append(_msg("user", "hi"), ha_conversation_id="ha-1")
            db.append(_msg("assistant", "hello"))  # no ha_conv_id
            db.append(_msg("user", "again"), ha_conversation_id="ha-2")
            assert db.latest_ha_conversation_id() == "ha-2"
        finally:
            db.close()

    def test_messages_since_filters_by_ts(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            t0 = 1000.0
            db.append(_msg("user", "old"), ts=t0)
            db.append(_msg("user", "newer"), ts=t0 + 10)
            db.append(_msg("user", "newest"), ts=t0 + 20)
            recent = db.messages_since(t0 + 5)
            assert [m.content for m in recent] == ["newer", "newest"]
        finally:
            db.close()


class TestReplaceConversation:
    def test_replace_resets_idx(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append_many([_msg("user", "old1"), _msg("assistant", "old2")])
            db.replace_conversation([
                _msg("system", "new-summary"),
                _msg("user", "new1"),
            ])
            snap = db.snapshot()
            assert [m.idx for m in snap] == [0, 1]
            assert [m.content for m in snap] == ["new-summary", "new1"]
        finally:
            db.close()


class TestRetention:
    def test_prune_before_protects_tier_actions_by_default(self, tmp_path: Path) -> None:
        """Tier 1 and Tier 2 exchanges are device-control audit trail.
        prune_before defaults to keeping them so a long retention sweep
        doesn't lose the last six months of 'turn off the lights' history."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            t0 = 1000.0
            db.append(_msg("user", "old chat"), ts=t0, tier=3)
            db.append(_msg("user", "old action"), ts=t0, tier=1)
            db.append(_msg("user", "old disambig"), ts=t0, tier=2)
            db.append(_msg("user", "fresh"), ts=t0 + 1000, tier=3)

            n = db.prune_before(t0 + 500)
            assert n == 1  # only the tier=3 chat row removed

            snap = db.snapshot()
            kinds = {m.tier for m in snap}
            assert kinds == {1, 2, 3}  # action + disambig + fresh remain
        finally:
            db.close()

    def test_prune_before_unprotected(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("user", "old"), ts=100.0, tier=1)
            db.append(_msg("user", "fresh"), ts=999999.0, tier=1)
            n = db.prune_before(500.0, protect_tier=False)
            assert n == 1
            assert db.count() == 1
        finally:
            db.close()

    def test_disk_size_returns_positive(self, tmp_path: Path) -> None:
        db = ConversationDB(tmp_path / "c.db")
        try:
            for i in range(50):
                db.append(_msg("user", f"msg-{i} " * 10))
            assert db.disk_size_bytes() > 0
        finally:
            db.close()


class TestConcurrency:
    def test_concurrent_writers(self, tmp_path: Path) -> None:
        """Multiple threads appending must not corrupt the table.
        api_wrapper request handlers + tool_executor + audio loops
        all write concurrently in production."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            n_threads = 6
            n_per_thread = 25

            def writer(tid: int) -> None:
                for i in range(n_per_thread):
                    db.append(_msg("user", f"t{tid}-i{i}"))

            threads = [threading.Thread(target=writer, args=(t,))
                       for t in range(n_threads)]
            for t in threads: t.start()
            for t in threads: t.join()

            assert db.count() == n_threads * n_per_thread
            # All idx values unique within the conversation.
            snap = db.snapshot()
            idxs = [m.idx for m in snap]
            assert len(set(idxs)) == len(idxs)
        finally:
            db.close()


class TestMultiConversation:
    def test_separate_conversations_have_independent_idx(self, tmp_path: Path) -> None:
        """When per-principal conversation_id is added in the future
        (parked open question in the plan), each conversation must
        track its own idx sequence."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            db.append(_msg("user", "alice-1"), conversation_id="alice")
            db.append(_msg("user", "alice-2"), conversation_id="alice")
            db.append(_msg("user", "bob-1"), conversation_id="bob")

            alice = db.snapshot(conversation_id="alice")
            bob = db.snapshot(conversation_id="bob")
            assert [m.idx for m in alice] == [0, 1]
            assert [m.idx for m in bob] == [0]
        finally:
            db.close()
