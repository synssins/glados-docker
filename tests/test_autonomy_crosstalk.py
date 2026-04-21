"""Autonomy-lane cross-talk regression — the non-streaming API
scanner must skip assistant messages produced by the autonomy lane.

Background: both interactive and autonomy ``LLMProcessor`` instances
write to the same ``conversation_store``. When the non-streaming
API path polled for its reply (`_get_engine_response`), it content-
matched the user message and then scanned forward for the next
``role="assistant"`` message — which could be an autonomy-produced
reply that interleaved. The caller got autonomy text back.

Fix plumbing:
  `LLMProcessor._lane` → (text, params, lane) tuple on TTS queue
  → `AudioMessage.lane` → `PreparedChunk.lane` →
  `BufferedSpeechPlayer` stamps ``_source="autonomy"`` on the
  conversation-store append for autonomy-lane EOS flushes.
The API's forward scan now skips messages where ``_source ==
"autonomy"``.
"""
from __future__ import annotations

import numpy as np
import pytest

from glados.core.audio_data import AudioMessage


# ── Plumbing: AudioMessage carries lane ───────────────────────────


def test_audio_message_defaults_to_priority_lane() -> None:
    m = AudioMessage(audio=np.array([], dtype=np.float32), text="")
    assert m.lane == "priority"


def test_audio_message_accepts_autonomy_lane() -> None:
    m = AudioMessage(
        audio=np.array([], dtype=np.float32),
        text="atmospheric pressure remains stable",
        is_eos=False,
        lane="autonomy",
    )
    assert m.lane == "autonomy"


# ── PreparedChunk carries lane ─────────────────────────────────────


def test_prepared_chunk_defaults_to_priority_lane() -> None:
    from glados.core.buffered_speech_player import PreparedChunk
    c = PreparedChunk(
        audio_data=np.array([], dtype=np.float32),
        text="hello",
    )
    assert c.lane == "priority"


def test_prepared_chunk_accepts_autonomy_lane() -> None:
    from glados.core.buffered_speech_player import PreparedChunk
    c = PreparedChunk(
        audio_data=np.array([], dtype=np.float32),
        text="hello",
        lane="autonomy",
    )
    assert c.lane == "autonomy"


# ── API forward-scan predicate ─────────────────────────────────────


def _scan_for_reply(
    messages: list[dict],
    user_text: str,
    msg_count_before: int,
) -> str | None:
    """Local re-implementation of the non-streaming API scan logic
    so the test can exercise the predicate without a live engine.
    Mirrors `_get_engine_response` at api_wrapper.py line ~895+."""
    search_start = max(msg_count_before - 2, 0)
    for i in range(len(messages) - 1, search_start - 1, -1):
        msg = messages[i]
        if msg.get("role") != "user":
            continue
        if msg.get("content", "").strip() != user_text.strip():
            continue
        for j in range(i + 1, len(messages)):
            mj = messages[j]
            if mj.get("role") != "assistant" or not mj.get("content"):
                continue
            if mj.get("_source") == "autonomy":
                continue
            return mj["content"]
        break
    return None


def test_scan_skips_autonomy_assistant_message() -> None:
    """Autonomy reply lands between user message and real reply.
    Pre-fix the scanner returned the autonomy text. Post-fix it
    skips and keeps looking (or returns None if no real reply yet)."""
    messages = [
        {"role": "system", "content": "preprompt"},
        {"role": "user", "content": "What's the weather like?"},
        {
            "role": "assistant",
            "content": "atmospheric pressure remains stable",
            "_source": "autonomy",
        },
    ]
    # Real reply has not landed yet — scanner returns None
    reply = _scan_for_reply(messages, "What's the weather like?", 0)
    assert reply is None


def test_scan_picks_real_reply_past_autonomy_interleave() -> None:
    """Autonomy interleaves BEFORE the real reply; scanner must look
    past it and return the real one."""
    messages = [
        {"role": "user", "content": "What's the weather like?"},
        {
            "role": "assistant",
            "content": "atmospheric pressure remains stable",
            "_source": "autonomy",
        },
        {
            "role": "assistant",
            "content": "Current: 76 degrees, clear sky, wind 9 mph.",
            # no _source — defaults to user-reply stream
        },
    ]
    reply = _scan_for_reply(messages, "What's the weather like?", 0)
    assert reply == "Current: 76 degrees, clear sky, wind 9 mph."


def test_scan_handles_multiple_autonomy_before_reply() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "nope", "_source": "autonomy"},
        {"role": "assistant", "content": "nope again", "_source": "autonomy"},
        {"role": "assistant", "content": "Hi there."},
    ]
    assert _scan_for_reply(messages, "hello", 0) == "Hi there."


def test_scan_still_picks_legacy_unstamped_assistant() -> None:
    """Back-compat: messages without a `_source` field (e.g. from
    older container versions or callers that bypass the TTS pipe)
    still count as real replies."""
    messages = [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
    assert _scan_for_reply(messages, "ping", 0) == "pong"


def test_scan_skips_non_assistant_rows_between() -> None:
    """Tool calls / tool results land as assistant/tool rows with
    no `content`. These must not be returned as the reply."""
    messages = [
        {"role": "user", "content": "check the thing"},
        {"role": "assistant", "tool_calls": [{"name": "x"}]},  # no content
        {"role": "tool", "content": "tool output"},
        {"role": "assistant", "content": "I checked it."},
    ]
    assert _scan_for_reply(messages, "check the thing", 0) == "I checked it."


def test_scan_no_user_match_returns_none() -> None:
    """Sanity: an API scan for a user message that doesn't exist
    in the store (e.g. lost queue submission) returns None — the
    caller times out on their side."""
    messages = [
        {"role": "user", "content": "different"},
        {"role": "assistant", "content": "x"},
    ]
    assert _scan_for_reply(messages, "not found", 0) is None
