"""Unit tests for the command-lane routing helpers in api_wrapper.

Two pure helpers split out so the wiring inside _stream_chat_sse_impl
(which needs a live ``Glados`` + ``APIHandler``) is testable in isolation:

  - ``_select_command_lane`` chooses (url, model, is_command_lane)
    based on the route classifier and the configured llm_commands
    endpoint. Empty/blank URL falls back to llm_interactive so
    deployments that haven't set up the dedicated lane keep working.
  - ``_strip_persona_for_command_lane`` rewrites the messages list:
    replaces the persona system message with the command-mode minimal
    instruction and drops the persona's few-shot user/assistant pairs.
"""

from glados.core.api_wrapper import (
    COMMAND_MODE_SYSTEM_PROMPT,
    _select_command_lane,
    _strip_persona_for_command_lane,
)
from glados.core.config_store import ServiceEndpoint


# ── _select_command_lane ─────────────────────────────────────────


def test_command_route_with_commands_url_returns_commands_lane():
    cmds = ServiceEndpoint(url="http://aibox:11434", model="qwen2.5-coder-7b-instruct")
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=True,
        interactive_url="http://other:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=cmds,
    )
    assert url == "http://aibox:11434"
    assert model == "qwen2.5-coder-7b-instruct"
    assert is_cmd_lane is True


def test_command_route_empty_url_falls_back_to_interactive():
    cmds = ServiceEndpoint(url="", model="")
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=True,
        interactive_url="http://interactive:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=cmds,
    )
    assert url == "http://interactive:11434"
    assert model == "qwen3-14b"
    assert is_cmd_lane is False


def test_command_route_blank_url_falls_back_to_interactive():
    cmds = ServiceEndpoint(url="   ", model="qwen2.5-coder-7b-instruct")
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=True,
        interactive_url="http://interactive:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=cmds,
    )
    assert url == "http://interactive:11434"
    assert model == "qwen3-14b"
    assert is_cmd_lane is False


def test_non_command_route_uses_interactive_even_when_commands_set():
    cmds = ServiceEndpoint(url="http://aibox:11434", model="qwen2.5-coder-7b-instruct")
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=False,
        interactive_url="http://interactive:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=cmds,
    )
    assert url == "http://interactive:11434"
    assert model == "qwen3-14b"
    assert is_cmd_lane is False


def test_command_route_url_set_no_model_inherits_interactive_model():
    cmds = ServiceEndpoint(url="http://aibox:11434", model="")
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=True,
        interactive_url="http://other:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=cmds,
    )
    assert url == "http://aibox:11434"
    assert model == "qwen3-14b"
    assert is_cmd_lane is True


def test_none_commands_endpoint_falls_back_to_interactive():
    url, model, is_cmd_lane = _select_command_lane(
        is_command_route=True,
        interactive_url="http://interactive:11434",
        interactive_model="qwen3-14b",
        commands_endpoint=None,
    )
    assert url == "http://interactive:11434"
    assert is_cmd_lane is False


# ── _strip_persona_for_command_lane ──────────────────────────────


def test_strip_persona_replaces_first_system_and_drops_user_turns():
    # Strip is called BEFORE the current user message is appended at
    # the call site, so any user/assistant turns the helper sees are
    # prior history — which gets dropped on the command lane.
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "earlier turn — should be dropped"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=1)
    assert len(out) == 1
    assert out[0] == {"role": "system", "content": COMMAND_MODE_SYSTEM_PROMPT}


def test_strip_persona_drops_all_history_not_just_few_shots():
    # Critical: prior real conversation history (not just persona
    # few-shots) must also be dropped. Live failure prior to this:
    # qwen2.5-7b copied the text-reply pattern from prior fabricated
    # assistant turns and never emitted tool_calls.
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "[example user 1]"},
        {"role": "assistant", "content": "[example assistant 1]"},
        # ↓ real history (NOT few-shot) — must also be dropped on command lane
        {"role": "user", "content": "What is the forecast today?"},
        {"role": "assistant", "content": "It's 52 degrees."},
        {"role": "user", "content": "Add Ghostbusters."},
        {"role": "assistant", "content": "Adding has been initiated."},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=3)
    assert len(out) == 1
    assert out[0]["content"] == COMMAND_MODE_SYSTEM_PROMPT


def test_strip_persona_preserves_non_persona_system_messages():
    msgs = [
        {"role": "system", "content": "[persona]"},
        {"role": "system", "content": "Weather: 70F clear"},
        {"role": "user", "content": "earlier turn — dropped"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=1)
    assert out == [
        {"role": "system", "content": COMMAND_MODE_SYSTEM_PROMPT},
        {"role": "system", "content": "Weather: 70F clear"},
    ]


def test_strip_persona_no_preprompt_count_still_replaces_system():
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "earlier — dropped"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=0)
    assert out == [{"role": "system", "content": COMMAND_MODE_SYSTEM_PROMPT}]


def test_strip_persona_empty_messages_returns_empty():
    assert _strip_persona_for_command_lane([], preprompt_count=0) == []


def test_strip_persona_drops_user_assistant_even_without_leading_system():
    # When there is no leading persona system message, the helper does
    # not synthesise one — but it still drops every user/assistant
    # turn so the command lane stays history-free.
    msgs = [
        {"role": "user", "content": "leading_user"},
        {"role": "assistant", "content": "earlier_response"},
        {"role": "user", "content": "real_user"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=2)
    assert out == []


def test_command_mode_prompt_is_terse_and_anti_fabrication():
    assert "tool" in COMMAND_MODE_SYSTEM_PROMPT.lower()
    assert "persona" in COMMAND_MODE_SYSTEM_PROMPT.lower()
    assert "claim" in COMMAND_MODE_SYSTEM_PROMPT.lower() or "fabric" in COMMAND_MODE_SYSTEM_PROMPT.lower()
