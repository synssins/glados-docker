"""Tests for the autonomy-noise filter in llm_processor.

The autonomy loop writes "Autonomy update. Time: ..." messages to
the shared ConversationStore as user-role turns. Without filtering,
the chat path feeds these back to the LLM on every user message,
which drifts the model from the configured persona into a
telemetry-summariser stance (live bug 2026-04-21: chitchat queries
returning generic "I don't have capability..." instead of GLaDOS).
"""

from __future__ import annotations

from glados.core.llm_processor import _strip_autonomy_noise


class TestStripAutonomyNoise:
    def test_drops_autonomy_update_user_turn(self) -> None:
        msgs = [
            {"role": "system", "content": "You are GLaDOS."},
            {"role": "user", "content": "Autonomy update.\nTime: 2026-04-21T15:07:28\nScene: camera disabled"},
            {"role": "user", "content": "Hello"},
        ]
        out = _strip_autonomy_noise(msgs)
        assert len(out) == 2
        assert out[0]["role"] == "system"
        assert out[1]["content"] == "Hello"

    def test_drops_summary_prefixed_turn(self) -> None:
        msgs = [
            {"role": "user", "content": "[summary] Last hour: nothing of note."},
            {"role": "user", "content": "What's new?"},
        ]
        out = _strip_autonomy_noise(msgs)
        assert [m["content"] for m in out] == ["What's new?"]

    def test_drops_tool_role_turns(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": '{"x": 1}', "tool_call_id": "t1"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "next"},
        ]
        out = _strip_autonomy_noise(msgs)
        # Dropped: the empty-content assistant stub + the tool result.
        # Kept: system, the real assistant reply, and the next user turn.
        roles = [m["role"] for m in out]
        assert "tool" not in roles
        assert len([m for m in out if m.get("role") == "assistant"]) == 1

    def test_keeps_real_user_turns_intact(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "turn off the kitchen lights"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "What was life like as a potato?"},
        ]
        out = _strip_autonomy_noise(msgs)
        assert len(out) == 4  # all four preserved

    def test_preserves_order(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "user", "content": "Autonomy update. noise"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        out = _strip_autonomy_noise(msgs)
        assert [m["content"] for m in out] == ["sys", "one", "two", "three"]

    def test_non_dict_entries_passed_through(self) -> None:
        # Defensive: unexpected shapes shouldn't raise.
        msgs = [
            {"role": "user", "content": "hi"},
            "not a dict",  # type: ignore[list-item]
            {"role": "user", "content": "Autonomy update. drop me"},
        ]
        out = _strip_autonomy_noise(msgs)  # type: ignore[arg-type]
        assert "not a dict" in out
        assert not any(
            isinstance(m, dict) and m.get("content", "").startswith("Autonomy update")
            for m in out
        )
