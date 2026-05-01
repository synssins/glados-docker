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


def test_strip_persona_replaces_first_system_message():
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "Add ghostbusters"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=1)
    assert out[0]["role"] == "system"
    assert out[0]["content"] == COMMAND_MODE_SYSTEM_PROMPT
    assert out[1] == msgs[1]


def test_strip_persona_drops_few_shot_pairs():
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "[example user 1]"},
        {"role": "assistant", "content": "[example assistant 1]"},
        {"role": "user", "content": "[example user 2]"},
        {"role": "assistant", "content": "[example assistant 2]"},
        {"role": "user", "content": "Real user message"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=5)
    assert len(out) == 2
    assert out[0]["content"] == COMMAND_MODE_SYSTEM_PROMPT
    assert out[1]["content"] == "Real user message"


def test_strip_persona_preserves_non_persona_system_messages():
    msgs = [
        {"role": "system", "content": "[persona]"},
        {"role": "system", "content": "Weather: 70F clear"},
        {"role": "user", "content": "Hi"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=1)
    assert out[0]["content"] == COMMAND_MODE_SYSTEM_PROMPT
    assert out[1]["content"] == "Weather: 70F clear"
    assert out[2] == msgs[2]


def test_strip_persona_no_preprompt_count_still_replaces_system():
    msgs = [
        {"role": "system", "content": "[persona stuff]"},
        {"role": "user", "content": "Real user"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=0)
    assert out[0]["content"] == COMMAND_MODE_SYSTEM_PROMPT
    assert out[1] == msgs[1]


def test_strip_persona_empty_messages_returns_empty():
    assert _strip_persona_for_command_lane([], preprompt_count=0) == []


def test_strip_persona_drops_only_user_assistant_in_preprompt_range():
    # When there is no leading system message, the helper does not
    # synthesize one — it just drops the few-shot user/assistant pairs
    # within the preprompt index range. This mirrors the behaviour of
    # the legacy strip block: index 0 is kept (would normally be the
    # persona system msg); indices 1..preprompt-1 of role
    # user/assistant are dropped.
    msgs = [
        {"role": "user", "content": "leading_user"},
        {"role": "assistant", "content": "few_shot_a"},
        {"role": "user", "content": "real_user"},
    ]
    out = _strip_persona_for_command_lane(msgs, preprompt_count=2)
    assert out == [
        {"role": "user", "content": "leading_user"},
        {"role": "user", "content": "real_user"},
    ]


def test_command_mode_prompt_is_terse_and_anti_fabrication():
    assert "tool" in COMMAND_MODE_SYSTEM_PROMPT.lower()
    assert "persona" in COMMAND_MODE_SYSTEM_PROMPT.lower()
    assert "claim" in COMMAND_MODE_SYSTEM_PROMPT.lower() or "fabric" in COMMAND_MODE_SYSTEM_PROMPT.lower()
