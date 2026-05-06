"""Unit tests for the event:image SSE channel — Task 6.

Tests _write_image_sse_event directly (Option A), plus a source-invariant
check that the LLM history entry never contains the base64 data URL.
"""

from __future__ import annotations

import base64
import io
import json
import re

import pytest

from glados.core.api_wrapper import _write_image_sse_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHandler:
    """Minimal handler stub with a writable wfile BytesIO."""

    def __init__(self):
        self.wfile = io.BytesIO()

    def written(self) -> bytes:
        return self.wfile.getvalue()


FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 128
FAKE_TOOL_CALL_ID = "call_abc123"


# ---------------------------------------------------------------------------
# _write_image_sse_event — frame format
# ---------------------------------------------------------------------------

def test_write_image_sse_event_produces_event_image_frame():
    handler = _FakeHandler()
    _write_image_sse_event(
        handler,
        tool_call_id=FAKE_TOOL_CALL_ID,
        image_bytes=FAKE_JPEG,
        mime="image/jpeg",
    )
    raw = handler.written().decode("utf-8")
    # Must start with the event: image line
    assert raw.startswith("event: image\n")
    # Must have exactly one data: line
    assert raw.count("data: ") == 1
    # Must end with double newline
    assert raw.endswith("\n\n")


def test_write_image_sse_event_payload_has_tool_call_id():
    handler = _FakeHandler()
    _write_image_sse_event(
        handler,
        tool_call_id=FAKE_TOOL_CALL_ID,
        image_bytes=FAKE_JPEG,
        mime="image/jpeg",
    )
    raw = handler.written().decode("utf-8")
    data_line = [l for l in raw.splitlines() if l.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload["tool_call_id"] == FAKE_TOOL_CALL_ID


def test_write_image_sse_event_payload_has_correct_data_url():
    handler = _FakeHandler()
    _write_image_sse_event(
        handler,
        tool_call_id=FAKE_TOOL_CALL_ID,
        image_bytes=FAKE_JPEG,
        mime="image/jpeg",
    )
    raw = handler.written().decode("utf-8")
    data_line = [l for l in raw.splitlines() if l.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    image_url = payload["image_url"]
    assert image_url.startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(image_url.split(",", 1)[1])
    assert decoded == FAKE_JPEG


def test_write_image_sse_event_frame_is_single_sse_chunk():
    """Exactly one event:image frame — no stray newlines before/after."""
    handler = _FakeHandler()
    _write_image_sse_event(
        handler,
        tool_call_id="call_z",
        image_bytes=b"\x00\x01\x02",
        mime="image/png",
    )
    raw = handler.written().decode("utf-8")
    # SSE frame: "event: image\ndata: ...\n\n" — three logical lines then empty
    lines = raw.split("\n")
    # lines[-1] and lines[-2] should both be empty (trailing \n\n)
    assert lines[-1] == ""
    assert lines[-2] == ""
    # Only one data: line
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) == 1


def test_write_image_sse_event_non_jpeg_mime():
    """mime parameter flows through correctly."""
    handler = _FakeHandler()
    _write_image_sse_event(
        handler,
        tool_call_id="call_png",
        image_bytes=b"\x89PNG",
        mime="image/png",
    )
    raw = handler.written().decode("utf-8")
    data_line = [l for l in raw.splitlines() if l.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload["image_url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# LLM history invariant — data URL NEVER enters the messages list
# ---------------------------------------------------------------------------

def test_llm_history_uses_result_not_emission_source_check():
    """Source-level invariant: the messages.append line in api_wrapper.py
    uses ``_result`` (the JSON description string), NOT ``_emission``.

    This is verified by reading the source and asserting the construction
    of the tool-role history entry does not reference _emission at all.

    If this test fails, someone has accidentally wired the image bytes
    into the LLM conversation history.
    """
    import inspect
    import glados.core.api_wrapper as _mod

    src = inspect.getsource(_mod)

    # Find the messages.append line for tool role entries
    # Pattern: messages.append({"role": "tool", "tool_call_id": ..., "content": ...})
    append_pattern = re.compile(
        r'messages\.append\(\{"role":\s*"tool"[^}]+\}\)',
        re.DOTALL,
    )
    matches = append_pattern.findall(src)
    assert matches, "Could not find messages.append tool-role line in api_wrapper.py"

    for match in matches:
        # None of these lines should reference _emission
        assert "_emission" not in match, (
            f"LLM history append references _emission (data URL may leak): {match!r}"
        )
        # All of these lines should reference _result (the JSON description)
        assert "_result" in match, (
            f"LLM history append does not use _result: {match!r}"
        )


def test_no_base64_in_look_at_camera_tool_result_source_check():
    """The _look_at_camera function in builtin_tools.py must return a
    plain JSON description string — no base64 in the return value path.

    Checks the source of invoke_image_yielding_tool to ensure the
    first element of the returned tuple comes from json.dumps({...description...})
    and not from any base64-encoding call.
    """
    import inspect
    import glados.core.builtin_tools as _mod

    src = inspect.getsource(_mod._look_at_camera)

    # The function must return a json.dumps({"description": ...}) as the
    # first element (what goes to the LLM)
    assert 'json.dumps({"description":' in src or '"description"' in src, (
        "_look_at_camera return value should contain a description key"
    )

    # The base64 encoding (if any) must NOT appear in _look_at_camera itself;
    # it lives only in _write_image_sse_event in api_wrapper.py
    assert "base64" not in src, (
        "_look_at_camera must not call base64 — encoding belongs in _write_image_sse_event"
    )
