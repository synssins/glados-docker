"""Multi-turn integration tests for ConversationStore + SQLite backing.

These tests verify that the Phase B wiring actually solves the
production failure case: 'Turn off the whole house' followed by 'All
lights' should have the second utterance see the first as prior
context, instead of being processed in isolation.

We test at the ConversationStore + ConversationDB seam — the
api_wrapper integration is verified live in production. Tests here
guard the persistence + idx + ha_conversation_id propagation
contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.core.conversation_db import ConversationDB
from glados.core.conversation_store import ConversationStore


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class TestMultiTurnPersistence:
    def test_tier_appends_survive_restart(self, tmp_path: Path) -> None:
        """Simulate the failure case: container chats, restarts, the
        new process must see prior turns to handle the follow-up."""
        db_path = tmp_path / "conv.db"
        # Original session
        db1 = ConversationDB(db_path)
        store1 = ConversationStore(
            initial_messages=[_msg("system", "preprompt")],
            db=db1,
        )
        store1.append(_msg("user", "turn off the whole house"),
                      source="webui_chat", tier=2,
                      ha_conversation_id="ha-conv-1")
        store1.append(_msg("assistant", "Whole house terminated."),
                      source="webui_chat", tier=2,
                      ha_conversation_id="ha-conv-1")
        db1.close()

        # Restart: new process opens the same DB.
        db2 = ConversationDB(db_path)
        store2 = ConversationStore(
            initial_messages=[_msg("system", "preprompt")],
            db=db2,
        )
        store2.load_from_db()
        snap = store2.snapshot()
        # preprompt + user + assistant
        assert len(snap) == 3
        assert snap[0]["content"] == "preprompt"
        assert snap[1]["content"] == "turn off the whole house"
        assert snap[2]["content"] == "Whole house terminated."
        # HA conv_id propagation works across restart.
        assert store2.latest_ha_conversation_id() == "ha-conv-1"
        db2.close()

    def test_preprompt_is_not_duplicated_on_load(self, tmp_path: Path) -> None:
        """Critical: preprompt set in __init__ must not be re-appended
        from DB rows. The Change 7 invariant (preprompt_count protected
        from compaction) depends on this not happening."""
        db_path = tmp_path / "conv.db"
        db = ConversationDB(db_path)
        try:
            store = ConversationStore(
                initial_messages=[_msg("system", "preprompt")],
                db=db,
            )
            # Append a turn — store has preprompt + 1 turn = 2.
            store.append(_msg("user", "first turn"), tier=3)
            assert len(store) == 2

            # Restart: a new store with the SAME preprompt opens.
            db2 = ConversationDB(db_path)
            store2 = ConversationStore(
                initial_messages=[_msg("system", "preprompt")],
                db=db2,
            )
            store2.load_from_db()
            snap = store2.snapshot()
            # preprompt (from init) + the loaded user message = 2.
            # Critically NOT 3 (would mean preprompt loaded twice).
            assert len(snap) == 2
            assert snap[0]["content"] == "preprompt"
            assert snap[1]["content"] == "first turn"
            assert store2.preprompt_count == 1
            db2.close()
        finally:
            db.close()

    def test_followup_inherits_prior_ha_conv_id(self, tmp_path: Path) -> None:
        """The 'All lights' fix: each Tier 1/2 hit captures HA's
        conversation_id; the next utterance reads it back so HA's
        intent parser maintains its own thread."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            store = ConversationStore(db=db)
            # Turn 1: device command
            store.append(_msg("user", "turn off the whole house"),
                         tier=2, ha_conversation_id="ha-conv-A")
            store.append(_msg("assistant", "Whole house terminated."),
                         tier=2, ha_conversation_id="ha-conv-A")

            # Caller about to issue Turn 2 — checks for prior conv_id
            prior = store.latest_ha_conversation_id()
            assert prior == "ha-conv-A"
            # (In production, bridge.process(text, conversation_id=prior)
            # would pass this forward; HA returns a new or same conv_id.)
        finally:
            db.close()

    def test_tier_metadata_persisted(self, tmp_path: Path) -> None:
        """Audit trail of which tier produced each exchange must
        survive a restart so retention sweepers can keep tier=1/2
        action-control history longer than tier=3 chit-chat."""
        path = tmp_path / "c.db"
        db1 = ConversationDB(path)
        store1 = ConversationStore(db=db1)
        store1.append(_msg("user", "lights"), tier=1)
        store1.append(_msg("user", "chat"), tier=3)
        db1.close()

        db2 = ConversationDB(path)
        try:
            stored = db2.snapshot()
            tiers = {m.content: m.tier for m in stored}
            assert tiers == {"lights": 1, "chat": 3}
        finally:
            db2.close()


class TestCompactionInteraction:
    def test_replace_all_persists(self, tmp_path: Path) -> None:
        """When the compaction agent replaces the message list with a
        compressed version, the DB must reflect the new state — not
        keep ghost rows from before."""
        db = ConversationDB(tmp_path / "c.db")
        try:
            store = ConversationStore(
                initial_messages=[_msg("system", "preprompt")],
                db=db,
            )
            for i in range(5):
                store.append(_msg("user", f"old turn {i}"))
            # Compaction replaces all with preprompt + summary + recent.
            store.replace_all([
                _msg("system", "preprompt"),
                _msg("system", "[summary] earlier discussion"),
                _msg("user", "recent turn"),
            ])
            # In-memory and DB must agree.
            snap = store.snapshot()
            assert len(snap) == 3
            assert snap[1]["content"] == "[summary] earlier discussion"
            # DB layer also.
            stored = db.snapshot()
            assert [m.content for m in stored] == [
                "preprompt", "[summary] earlier discussion", "recent turn",
            ]
        finally:
            db.close()


class TestBackwardCompat:
    def test_no_db_argument_works_unchanged(self, tmp_path: Path) -> None:
        """Existing callers that don't pass `db` get the original
        in-memory-only behavior. Any breakage here would silently
        affect the engine's existing call sites."""
        store = ConversationStore(initial_messages=[_msg("system", "p")])
        store.append(_msg("user", "hi"))
        snap = store.snapshot()
        assert len(snap) == 2
        # latest_ha_conversation_id should return None gracefully
        # (no DB to query).
        assert store.latest_ha_conversation_id() is None

    def test_existing_kwargs_optional(self) -> None:
        """append/append_multiple's new metadata kwargs must be optional."""
        store = ConversationStore()
        # No kwargs — original API.
        store.append(_msg("user", "hi"))
        store.append_multiple([_msg("user", "a"), _msg("assistant", "b")])
        assert len(store) == 3
