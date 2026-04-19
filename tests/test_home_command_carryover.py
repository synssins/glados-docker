"""P0 2026-04-19 — follow-up turns without a device keyword bypass
Tier 1/2 entirely.

Operator report: after GLaDOS had just acted on the desk lamp, "It's
still too dark. Turn it up more." had no 'light' / 'lamp' / 'turn on'
keyword → `looks_like_home_command` returned False → api_wrapper
skipped Tier 1 and Tier 2 → Tier 3 chitchat hallucinated a status
confirmation without calling a tool.

Fix: `_should_carry_over_home_command` inherits home-command intent
for one follow-up turn when the most recent assistant message resolved
via Tier 1 or Tier 2 within the follow-up window.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from glados.core.conversation_db import ConversationDB
from glados.core.conversation_store import ConversationStore


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class _FakeEngine:
    def __init__(self, store: ConversationStore) -> None:
        self._conversation_store = store


@pytest.fixture
def store_with_db(tmp_path: Path):
    db = ConversationDB(tmp_path / "carryover.db")
    store = ConversationStore(
        initial_messages=[_msg("system", "preprompt")], db=db,
    )
    yield store
    db.close()


class TestShouldCarryOverHomeCommand:
    def test_no_engine_returns_false(self) -> None:
        from glados.core import api_wrapper

        with patch.object(api_wrapper, "_engine", None):
            assert api_wrapper._should_carry_over_home_command() is False

    def test_empty_history_returns_false(self, store_with_db) -> None:
        from glados.core import api_wrapper

        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is False

    def test_recent_tier2_ok_returns_true(self, store_with_db) -> None:
        from glados.core import api_wrapper

        store_with_db.append_multiple(
            [_msg("user", "dim the lamp"),
             _msg("assistant", "Dimmed.")],
            source="webui_chat", tier=2,
        )
        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is True

    def test_recent_tier1_ok_returns_true(self, store_with_db) -> None:
        from glados.core import api_wrapper

        store_with_db.append_multiple(
            [_msg("user", "turn on the lamp"),
             _msg("assistant", "Illuminated.")],
            source="webui_chat", tier=1,
        )
        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is True

    def test_recent_tier3_chitchat_returns_false(self, store_with_db) -> None:
        from glados.core import api_wrapper

        store_with_db.append_multiple(
            [_msg("user", "tell me a joke"),
             _msg("assistant", "No.")],
            source="webui_chat", tier=3,
        )
        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is False

    def test_stale_tier2_beyond_window_returns_false(
        self, store_with_db, monkeypatch,
    ) -> None:
        from glados.core import api_wrapper

        store_with_db.append_multiple(
            [_msg("user", "dim the lamp"),
             _msg("assistant", "Dimmed.")],
            source="webui_chat", tier=2,
        )
        # Shrink the window so we can assert a stale exchange is rejected
        # without actually sleeping through 120s in the test suite.
        monkeypatch.setattr(
            api_wrapper, "_FOLLOWUP_HOME_COMMAND_WINDOW_S", 0.01,
        )
        time.sleep(0.02)
        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is False

    def test_tier2_followed_by_tier3_returns_false(self, store_with_db) -> None:
        """Once a chitchat turn lands in between, the carry-over lease
        expires — the next follow-up shouldn't silently inherit intent
        across an unrelated turn."""
        from glados.core import api_wrapper

        store_with_db.append_multiple(
            [_msg("user", "dim the lamp"),
             _msg("assistant", "Dimmed.")],
            source="webui_chat", tier=2,
        )
        store_with_db.append_multiple(
            [_msg("user", "joke"),
             _msg("assistant", "No.")],
            source="webui_chat", tier=3,
        )
        with patch.object(api_wrapper, "_engine", _FakeEngine(store_with_db)):
            assert api_wrapper._should_carry_over_home_command() is False

    def test_exception_from_store_returns_false(self) -> None:
        """Any exception reading the store must be swallowed so the
        chitchat path isn't broken by a transient DB issue."""
        from glados.core import api_wrapper

        class _BrokenStore:
            def latest_assistant_tier_exchange(self):
                raise RuntimeError("simulated")

        class _BrokenEngine:
            _conversation_store = _BrokenStore()

        with patch.object(api_wrapper, "_engine", _BrokenEngine()):
            assert api_wrapper._should_carry_over_home_command() is False
