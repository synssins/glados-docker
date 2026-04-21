"""Tests for the anti-parrot guard in api_wrapper.

Live bug 2026-04-21: a user asking the same question twice got the
exact same reply verbatim because qwen3:8b sees its own prior answer
in the conversation_store snapshot and copies it. The guard removes
prior identical (user, assistant) pairs before sending to the LLM so
the model has no verbatim anchor to reach for."""

from __future__ import annotations

from glados.core.api_wrapper import _drop_parrot_anchors, _normalize_for_parrot


class TestNormalizeForParrot:
    def test_strips_case_and_trailing_punct(self) -> None:
        assert _normalize_for_parrot("Hi There!") == "hi there"
        assert _normalize_for_parrot("  hello.  ") == "hello"
        assert _normalize_for_parrot("HELLO?") == "hello"

    def test_non_string_empty(self) -> None:
        assert _normalize_for_parrot(None) == ""  # type: ignore[arg-type]
        assert _normalize_for_parrot("") == ""
        assert _normalize_for_parrot("   ") == ""


class TestDropParrotAnchors:
    def test_drops_prior_identical_qa_pair(self) -> None:
        msgs = [
            {"role": "system", "content": "You are GLaDOS."},
            {"role": "user", "content": "Tell me about this house"},  # few-shot
            {"role": "assistant", "content": "This facility..."},      # few-shot
            {"role": "user", "content": "What was life like as a potato?"},
            {"role": "assistant", "content": "A prior answer about potato..."},
            {"role": "user", "content": "Unrelated turn."},
            {"role": "assistant", "content": "Unrelated reply."},
        ]
        out = _drop_parrot_anchors(
            msgs, "What was life like as a potato?", "rid",
        )
        # The prior potato Q/A should be gone; the unrelated turn kept.
        contents = [m.get("content") for m in out if m.get("role") == "user"]
        assert "What was life like as a potato?" not in contents
        assert "Unrelated turn." in contents
        # Few-shot pair preserved.
        assert "Tell me about this house" in contents

    def test_case_insensitive_punctuation_tolerant(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "Hello GLaDOS!"},
            {"role": "assistant", "content": "Hi."},
        ]
        out = _drop_parrot_anchors(msgs, "hello glados", "rid")
        # The "Hello GLaDOS!" pair should be dropped despite case /
        # punctuation mismatch.
        users = [m for m in out if m.get("role") == "user"]
        assert not any("hello glados" in (u["content"] or "").lower() for u in users[1:])

    def test_no_match_returns_unchanged(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
        ]
        out = _drop_parrot_anchors(msgs, "different question", "rid")
        assert out == msgs

    def test_empty_current_message_returns_unchanged(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        assert _drop_parrot_anchors(msgs, "", "rid") == msgs
        assert _drop_parrot_anchors(msgs, "   ", "rid") == msgs

    def test_few_shot_user_at_index_1_preserved(self) -> None:
        """Preprompt few-shot pairs start at index 1 (right after the
        system prompt) — the guard must never strip index 1 even if
        the current user message somehow matches. This covers the
        edge case where the operator phrases a real question the same
        way as a few-shot example."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Tell me about this house"},  # index 1
            {"role": "assistant", "content": "This facility..."},
        ]
        out = _drop_parrot_anchors(
            msgs, "Tell me about this house", "rid",
        )
        # Few-shot at index 1 must NOT be dropped.
        assert len(out) == 3
        assert out[1]["content"] == "Tell me about this house"

    def test_drops_orphan_user_without_reply(self) -> None:
        """An unanswered prior user turn that matches the current
        question should also be dropped — the model can still infer
        the previous question from it even without an answer."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "dup"},  # no reply yet
        ]
        out = _drop_parrot_anchors(msgs, "dup", "rid")
        # The trailing "dup" should be gone; earlier turns kept.
        users = [m for m in out if m.get("role") == "user"]
        assert len(users) == 1
        assert users[0]["content"] == "x"

    def test_multiple_prior_pairs_all_dropped(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "repeated"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "something else"},
            {"role": "assistant", "content": "other"},
            {"role": "user", "content": "repeated"},
            {"role": "assistant", "content": "second answer"},
        ]
        out = _drop_parrot_anchors(msgs, "repeated", "rid")
        # Both "repeated" pairs should be gone.
        users = [m.get("content") for m in out if m.get("role") == "user"]
        assert "repeated" not in users
        assert users == ["seed", "something else"]


class TestDropParrotAnchorsNonStreaming:
    """The non-streaming engine path has its own copy of the guard
    that reads the current question from the LAST user turn in the
    message list (since the engine appends before calling into the
    processor). Same logic, different entry point."""

    def test_drops_prior_pair_when_latest_user_repeats(self) -> None:
        from glados.core.llm_processor import _drop_parrot_anchors as _nsdrop
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Tell me about this house"},   # few-shot
            {"role": "assistant", "content": "This facility..."},       # few-shot
            {"role": "user", "content": "What was life like as a potato?"},
            {"role": "assistant", "content": "Old verbatim answer."},
            {"role": "user", "content": "What was life like as a potato?"},
        ]
        out = _nsdrop(msgs)
        users = [m.get("content") for m in out if m.get("role") == "user"]
        # Only ONE potato question remains — the latest. Prior pair gone.
        potato_count = sum(
            1 for u in users
            if u == "What was life like as a potato?"
        )
        assert potato_count == 1

    def test_keeps_the_latest_user_turn(self) -> None:
        from glados.core.llm_processor import _drop_parrot_anchors as _nsdrop
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "X"},
            {"role": "assistant", "content": "first X answer"},
            {"role": "user", "content": "X"},  # the current question
        ]
        out = _nsdrop(msgs)
        # The last "X" must still be present.
        assert out[-1] == {"role": "user", "content": "X"}
        # The first "X" + its reply should be gone.
        kept_roles_contents = [(m.get("role"), m.get("content")) for m in out]
        assert ("user", "X") in kept_roles_contents
        assert kept_roles_contents.count(("user", "X")) == 1

    def test_preserves_few_shot_at_index_1(self) -> None:
        from glados.core.llm_processor import _drop_parrot_anchors as _nsdrop
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Tell me about this house"},  # few-shot
            {"role": "assistant", "content": "This facility..."},
            {"role": "user", "content": "Tell me about this house"},  # repeat
        ]
        out = _nsdrop(msgs)
        # Few-shot at index 1 must stay. The latest at index 3 is the
        # current question and must also stay.
        assert out[1]["content"] == "Tell me about this house"
        assert out[-1]["content"] == "Tell me about this house"
        # Length unchanged — nothing got dropped because index 1 is
        # protected and the latest can't be dropped.
        assert len(out) == 4

    def test_no_prior_match_returns_unchanged(self) -> None:
        from glados.core.llm_processor import _drop_parrot_anchors as _nsdrop
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "b"},
        ]
        out = _nsdrop(msgs)
        assert out == msgs

    def test_empty_current_returns_unchanged(self) -> None:
        from glados.core.llm_processor import _drop_parrot_anchors as _nsdrop
        # No user messages → no current question → no change
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "r"},
        ]
        out = _nsdrop(msgs)
        assert out == msgs
