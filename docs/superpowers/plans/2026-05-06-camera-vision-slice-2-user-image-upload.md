# Camera Vision — Slice 2: User-Uploaded Chat Images

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operator pastes (Ctrl+V), drags, or picks up to 4 images into the WebUI chat input → images attach to the next message → GLaDOS describes/answers about them while inline thumbnails render in the user's bubble. Two-round flow: round 1 routes through the `llm_vision` slot to produce a description; round 2 sends the description as system context to the chat lane (Qwen3-30B) for persona-quality reply.

**Architecture:** Client-side: chat-input attachment queue with paste/drop/file-picker handlers, thumbnail chips, base-64 data URLs sent on POST. Server-side: `/v1/chat/completions` learns an `images: [data-url]` field on the request body; when present, body-size budgets gate, then a two-round flow runs (VLM round → description; chat round with description as system context → persona reply). Reuses Slice 1's `glados/vision/client.py:describe_images` unchanged.

**Tech Stack:** Vanilla JS in `glados/webui/static/ui.js`, Python `http.server`-based handler in `glados/core/api_wrapper.py`, the existing `_stream_chat_sse_impl` flow, the Slice 1 `vision/client.py`. No new HTTP client. No new auth surface (existing chat-scope user permissions cover it).

**Spec:** [`docs/superpowers/specs/2026-05-05-camera-vision-design.md`](glados-container/docs/superpowers/specs/2026-05-05-camera-vision-design.md) — Feature C.

---

## Slice Boundaries

**This slice ships independently of Slice 3.** It depends ONLY on Slice 1's `glados/vision/client.py` (must be merged first). It does NOT need EventRouter, HAWebSocketHub, audio-root migration, or Slice 1's `look_at_camera` tool.

**Files this slice owns:**
- `glados/core/api_wrapper.py` (modify — `_handle_chat_completions` request-body parsing for `images:` field, body-size limit, two-round dispatch helper)
- `glados/webui/static/ui.js` (modify — chat-input attachment handlers, thumbnail chips, send-with-images on POST, render user-bubble inline thumbnails)
- `glados/webui/static/ui.css` (modify — `.chat-attachment-chip` and user-bubble inline-image styles; reuses `.chat-inline-image` from Slice 1)

**Files this slice does NOT own (and must not modify):**
- Anything under `glados/cameras/` or `glados/vision/client.py` — all Slice 1 surfaces stay frozen.
- `glados/core/builtin_tools.py` — no new tools in Slice 2.
- `glados/events/`, `glados/autonomy/agents/ha_sensor_watcher.py` — Slice 3 territory.

**Auth/RBAC:** No new admin surface. Image attach is gated only by the existing chat scope: any authenticated user who can chat can attach images. Per-user / per-camera ACLs deferred (Phase 2 hook in spec §3).

**Image persistence (MVP rule):** Image bytes do NOT persist in chat history. Specifically:
- The `images:` field on the request lives only for the request lifetime.
- The user's bubble shows thumbnails IN THE LIVE TAB; on page reload, the historical user turn re-renders with text only — the inline thumbnails disappear.
- VLM descriptions DO persist via the existing `[image_descriptions]` system message that's part of the chat history.

**Server-side budgets (per spec §3 "Server-side request budgets"):**
- Max total request body when `images:` present: 25 MB (`413 Request Entity Too Large`).
- Per-image max: 5 MB (`400 Bad Request` with offending image index).
- Image count max: 4 (`400 Bad Request` with received count).

**Out of scope:**
- Persistent thumbnails in chat history (Phase 2 — small image-store keyed by `message_id`).
- Live / streaming image input.
- Image formats beyond JPG/PNG/WebP — HEIC, GIF rejected client-side and server-side.

**Dependencies:** Slice 1 merged (`glados/vision/client.py` available + `llm_vision` slot live at `:11437`). No deploy work in this slice.

---

## File Structure

| File | Responsibility |
|---|---|
| `glados/core/api_wrapper.py` (modify) | (a) Pre-parse `Content-Length` to enforce the 25 MB budget BEFORE reading the body. (b) Extract `images: [data-url]` from the JSON body when present. (c) Validate count ≤ 4 and per-image size ≤ 5 MB. (d) For image-bearing turns, call `describe_images()` from `glados/vision/client.py` to get a description, inject it as a system message, then proceed through the existing chat-stream flow with the user's text. |
| `glados/webui/static/ui.js` (modify) | (a) Paste / drop / file-picker handlers attach images to a per-input attachment queue. (b) Thumbnail chips render with a remove-X. (c) `Send` builds the JSON body with `images: [data-url]`. (d) Streaming reply still consumes `event: image` (Slice 1's chunk type — REUSED unchanged for the assistant bubble) and now also user-side renders the attached thumbnails in the user's bubble immediately on send. |
| `glados/webui/static/ui.css` (modify) | `.chat-attachment-chip` (small thumbnail chip in input area), `.chat-user-bubble .chat-inline-image` (user-bubble image styles, reuses Slice 1's `.chat-inline-image` class). |

---

## Task 1: Server-side body-size guard + `images:` field parsing

**Goal:** Reject oversize requests with explicit cause; parse the new `images:` field cleanly when present; leave the no-images path unchanged.

**Files:**
- Modify: `glados/core/api_wrapper.py:_handle_chat_completions` (around line 4543)
- Test: `tests/core/test_api_wrapper_chat_images_validation.py`

**Acceptance Criteria:**
- [ ] POST to `/v1/chat/completions` with `Content-Length > 26214400` (25 MB + 1) returns `413` with body `{"error": {"message": "request body exceeds 25 MB cap", "type": "invalid_request_error"}}`.
- [ ] POST with valid JSON body but `images: [a, b, c, d, e]` (5 entries) returns `400` with message containing `"max 4 images"` and the actual count.
- [ ] POST with `images: ["data:image/jpeg;base64,<6MB>"]` returns `400` with message containing `"image 0 exceeds 5 MB"`.
- [ ] POST with `images: ["data:image/heic;base64,..."]` returns `400` with message containing `"unsupported image format"`.
- [ ] POST with no `images:` field is unchanged from current behavior (no regression).
- [ ] POST with valid `images: [<3 MB JPG>, <2 MB PNG>]` proceeds into the two-round flow (covered by Task 2).

**Verify:** `pytest tests/core/test_api_wrapper_chat_images_validation.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_api_wrapper_chat_images_validation.py`:

```python
"""Server-side guards on /v1/chat/completions when 'images:' field is present."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest


def _make_handler(body: bytes, content_length: int | None = None) -> MagicMock:
    h = MagicMock()
    h.path = "/v1/chat/completions"
    h.headers = {"Content-Length": str(content_length if content_length is not None else len(body))}
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h._send_json = MagicMock()
    return h


def _data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def test_request_body_over_25mb_returns_413():
    from glados.core.api_wrapper import _GladosHTTPHandler  # adjust to actual class name
    fake_handler = _make_handler(b"", content_length=26 * 1024 * 1024)  # 26 MB
    _GladosHTTPHandler._handle_chat_completions(fake_handler)
    args, kwargs = fake_handler._send_json.call_args
    body, status = args
    assert status == 413
    assert "25" in body["error"]["message"]


def test_more_than_4_images_returns_400():
    from glados.core.api_wrapper import _GladosHTTPHandler
    body_dict = {
        "stream": True,
        "messages": [{"role": "user", "content": "describe these"}],
        "images": [_data_url(b"\xff\xd8jpg")] * 5,
    }
    body = json.dumps(body_dict).encode()
    fake_handler = _make_handler(body)
    with patch("glados.core.api_wrapper._engine", MagicMock(shutdown_event=MagicMock(is_set=lambda: False))):
        _GladosHTTPHandler._handle_chat_completions(fake_handler)
    body_out, status = fake_handler._send_json.call_args.args
    assert status == 400
    assert "max 4 images" in body_out["error"]["message"]
    assert "5" in body_out["error"]["message"]


def test_per_image_over_5mb_returns_400():
    from glados.core.api_wrapper import _GladosHTTPHandler
    big = b"\xff\xd8" + b"a" * (5 * 1024 * 1024 + 100)  # > 5 MB
    body_dict = {
        "stream": True,
        "messages": [{"role": "user", "content": "describe"}],
        "images": [_data_url(big)],
    }
    body = json.dumps(body_dict).encode()
    fake_handler = _make_handler(body)
    with patch("glados.core.api_wrapper._engine", MagicMock(shutdown_event=MagicMock(is_set=lambda: False))):
        _GladosHTTPHandler._handle_chat_completions(fake_handler)
    body_out, status = fake_handler._send_json.call_args.args
    assert status == 400
    assert "image 0 exceeds 5 MB" in body_out["error"]["message"]


def test_unsupported_format_rejected():
    from glados.core.api_wrapper import _GladosHTTPHandler
    body_dict = {
        "stream": True,
        "messages": [{"role": "user", "content": "what is this"}],
        "images": [_data_url(b"\x00\x00\x00\x18ftypheic", mime="image/heic")],
    }
    body = json.dumps(body_dict).encode()
    fake_handler = _make_handler(body)
    with patch("glados.core.api_wrapper._engine", MagicMock(shutdown_event=MagicMock(is_set=lambda: False))):
        _GladosHTTPHandler._handle_chat_completions(fake_handler)
    body_out, status = fake_handler._send_json.call_args.args
    assert status == 400
    assert "unsupported image format" in body_out["error"]["message"]


def test_no_images_field_no_regression():
    from glados.core.api_wrapper import _GladosHTTPHandler
    body_dict = {
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body = json.dumps(body_dict).encode()
    fake_handler = _make_handler(body)
    with patch("glados.core.api_wrapper._engine", MagicMock(shutdown_event=MagicMock(is_set=lambda: False))), \
         patch("glados.core.api_wrapper._stream_chat_sse") as ss, \
         patch("glados.core.api_wrapper._try_local_fastpath", return_value=False), \
         patch("glados.core.api_wrapper._should_run_command_resolver", return_value=False):
        _GladosHTTPHandler._handle_chat_completions(fake_handler)
    # No image gating fired; original chat-stream entry was reached
    assert ss.called
```

> **Note:** the actual handler class name in this repo may differ — `grep "class.*BaseHTTPRequestHandler\|class GLaDOSHTTPHandler\|class _Handler" glados/core/api_wrapper.py` to find it. Adjust the test imports accordingly. The test target is the `_handle_chat_completions` method.

- [ ] **Step 2: Run tests — expect failures**

```bash
pytest tests/core/test_api_wrapper_chat_images_validation.py -v
```

- [ ] **Step 3: Implement the body-size guard**

In `_handle_chat_completions` (currently at line ~4543), insert before `body = self.rfile.read(content_length)`:

```python
        # Body-size guard. When images: field is present a chat request can
        # carry up to 25 MB; without images it's tiny. Single hard cap keeps
        # a stuck client from memory-pinning the worker.
        _MAX_BODY_BYTES = 25 * 1024 * 1024
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            content_length = 0
        if content_length > _MAX_BODY_BYTES:
            self._send_json(
                {"error": {"message": "request body exceeds 25 MB cap", "type": "invalid_request_error"}},
                413,
            )
            return
```

(Replace the existing `content_length = int(...)` line — the new block subsumes it.)

- [ ] **Step 4: Add the `images:` validation helper**

After the JSON-parse block, add:

```python
        # Optional `images:` field — list of data URLs. Validate count,
        # per-image size, and format BEFORE entering the chat flow. None
        # is the no-images path; an empty list also means no images and
        # is treated identically.
        _images_raw = data.get("images") or []
        try:
            image_blobs = _validate_images_field(_images_raw)
        except _ImageValidationError as ve:
            self._send_json(
                {"error": {"message": str(ve), "type": "invalid_request_error"}},
                400,
            )
            return
```

Add the helper + sentinel error class near the top of the file (after the existing imports, before `_GladosHTTPHandler`):

```python
class _ImageValidationError(ValueError):
    """Surfaces a user-facing validation error for the images: field."""


_MAX_IMAGES = 4
_MAX_PER_IMAGE_BYTES = 5 * 1024 * 1024
_SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def _validate_images_field(raw: list) -> list[tuple[bytes, str]]:
    """Validate and decode a list of data-URL image attachments.

    Returns a list of (image_bytes, mime) tuples in input order.

    Raises ``_ImageValidationError`` with a single sentence message on
    any failure — the message is sent verbatim to the client, so it
    must be self-contained.
    """
    import base64

    if not isinstance(raw, list):
        raise _ImageValidationError("images must be an array of data URLs")
    if len(raw) > _MAX_IMAGES:
        raise _ImageValidationError(f"max 4 images per chat turn, got {len(raw)}")

    decoded: list[tuple[bytes, str]] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.startswith("data:"):
            raise _ImageValidationError(f"image {idx} must be a data URL string")
        try:
            head, b64body = entry.split(",", 1)
        except ValueError:
            raise _ImageValidationError(f"image {idx} malformed data URL")
        # head looks like "data:image/jpeg;base64"
        if ";base64" not in head:
            raise _ImageValidationError(f"image {idx} must use base64 encoding")
        mime = head.split(":", 1)[1].split(";", 1)[0].lower()
        if mime not in _SUPPORTED_IMAGE_MIMES:
            raise _ImageValidationError(
                f"unsupported image format {mime!r} at index {idx}; "
                f"supported: {sorted(_SUPPORTED_IMAGE_MIMES)}"
            )
        try:
            blob = base64.b64decode(b64body, validate=True)
        except Exception as exc:
            raise _ImageValidationError(f"image {idx} base64 decode failed: {exc}") from exc
        if len(blob) > _MAX_PER_IMAGE_BYTES:
            raise _ImageValidationError(f"image {idx} exceeds 5 MB ({len(blob)} bytes)")
        decoded.append((blob, mime))

    return decoded
```

- [ ] **Step 5: Pass the `image_blobs` into the chat-stream entry**

For now, route the validated images downstream by stashing them on a thread-local or by extending the `_stream_chat_sse` call signature. Pick the simpler one: add a kwarg.

Find the call at line 4620:

```python
            _stream_chat_sse(self, _engine, user_message, max(_response_timeout, 180.0))
```

Change to:

```python
            _stream_chat_sse(
                self, _engine, user_message, max(_response_timeout, 180.0),
                image_blobs=image_blobs,
            )
```

Update the `_stream_chat_sse` and `_stream_chat_sse_impl` signatures to accept `image_blobs: list[tuple[bytes, str]] | None = None`. For Task 1, the body just stashes them on a local var — Task 2 implements the two-round flow that consumes them.

- [ ] **Step 6: Run the validation tests**

```bash
pytest tests/core/test_api_wrapper_chat_images_validation.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add glados/core/api_wrapper.py tests/core/test_api_wrapper_chat_images_validation.py
git commit -m "feat(chat): images: field parsing + 25MB/5MB/4-image budgets"
```

---

## Task 2: Two-round VLM→chat flow when `images:` present

**Goal:** When `image_blobs` is non-empty, run a VLM round to produce a description, inject it as a system message, then continue through the normal chat-stream flow.

**Files:**
- Modify: `glados/core/api_wrapper.py:_stream_chat_sse_impl` — add the image-bearing branch.
- Test: `tests/core/test_api_wrapper_chat_two_round_vlm.py`

**Acceptance Criteria:**
- [ ] When `image_blobs` has 1 image, `describe_images([blob], <prompt>)` is called once before the chat round.
- [ ] When `image_blobs` has 2 images, `describe_images([blob_a, blob_b], <prompt>)` is called once with BOTH images in a single multimodal call (cheaper than 2 sequential calls and matches the spec).
- [ ] The chat round (round 2) sees a synthetic system message of the form `[image_descriptions] <vlm_output>` injected before the user's text turn.
- [ ] The chat round continues to use the existing chat-lane (Qwen3-30B via `llm_interactive` slot) — NOT the vision slot.
- [ ] VLM failure surfaces as a graceful chat reply containing the cause (e.g. `"I tried to look at the images but the vision endpoint returned 502: timeout"`); the `images:` content is NOT silently dropped.
- [ ] No images → flow is unchanged (the existing chat path). No regression.

**Verify:** `pytest tests/core/test_api_wrapper_chat_two_round_vlm.py -v && pytest tests/core/ -k chat -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_api_wrapper_chat_two_round_vlm.py`:

```python
"""Tests for the two-round VLM→chat flow when images: is present."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest


def test_single_image_calls_describe_once_then_chat():
    from glados.core import api_wrapper

    handler = MagicMock()
    handler.wfile = BytesIO()

    image_blobs = [(b"\xff\xd8jpg", "image/jpeg")]

    captured_messages: list[list[dict]] = []

    def fake_chat_round(handler, engine, messages, **kwargs):
        captured_messages.append(list(messages))
        # Pretend the chat stream completed
        return None

    with patch.object(api_wrapper, "describe_images", return_value="a circuit board with a burnt resistor") as desc, \
         patch.object(api_wrapper, "_run_chat_round", side_effect=fake_chat_round):
        api_wrapper._stream_chat_sse_impl(
            handler, MagicMock(), "what's wrong with this?", 30.0,
            image_blobs=image_blobs,
        )

    desc.assert_called_once()
    args, kwargs = desc.call_args
    assert args[0] == [b"\xff\xd8jpg"]

    # Round-2 chat saw a synthetic system message with the description
    assert captured_messages
    msgs = captured_messages[0]
    sys_descriptions = [m for m in msgs if m["role"] == "system" and "[image_descriptions]" in m["content"]]
    assert len(sys_descriptions) == 1
    assert "burnt resistor" in sys_descriptions[0]["content"]


def test_two_images_single_describe_call_with_both():
    from glados.core import api_wrapper

    handler = MagicMock()
    handler.wfile = BytesIO()
    image_blobs = [(b"a", "image/jpeg"), (b"b", "image/png")]

    with patch.object(api_wrapper, "describe_images", return_value="two boards") as desc, \
         patch.object(api_wrapper, "_run_chat_round", return_value=None):
        api_wrapper._stream_chat_sse_impl(
            handler, MagicMock(), "compare", 30.0,
            image_blobs=image_blobs,
        )

    desc.assert_called_once()
    args, _ = desc.call_args
    assert args[0] == [b"a", b"b"]


def test_vlm_failure_surfaces_in_chat_reply():
    from glados.core import api_wrapper
    from glados.vision.client import VisionClientError

    handler = MagicMock()
    handler.wfile = BytesIO()
    image_blobs = [(b"\xff\xd8jpg", "image/jpeg")]

    captured_messages: list[list[dict]] = []

    def fake_chat_round(handler, engine, messages, **kwargs):
        captured_messages.append(list(messages))

    with patch.object(api_wrapper, "describe_images",
                      side_effect=VisionClientError("vision endpoint http://x failed: timeout")), \
         patch.object(api_wrapper, "_run_chat_round", side_effect=fake_chat_round):
        api_wrapper._stream_chat_sse_impl(
            handler, MagicMock(), "what's this", 30.0,
            image_blobs=image_blobs,
        )

    msgs = captured_messages[0]
    sys_msgs = [m for m in msgs if m["role"] == "system" and "[image_descriptions]" in m["content"]]
    assert len(sys_msgs) == 1
    assert "vision endpoint" in sys_msgs[0]["content"]
    assert "timeout" in sys_msgs[0]["content"]


def test_no_images_path_no_describe_call():
    from glados.core import api_wrapper
    handler = MagicMock()
    handler.wfile = BytesIO()
    with patch.object(api_wrapper, "describe_images") as desc, \
         patch.object(api_wrapper, "_run_chat_round", return_value=None):
        api_wrapper._stream_chat_sse_impl(
            handler, MagicMock(), "hello", 30.0,
            image_blobs=None,
        )
    desc.assert_not_called()
```

> **Note:** the tests reference an extracted helper `_run_chat_round`. The current `_stream_chat_sse_impl` is monolithic; this task encourages a small refactor to extract the chat-round dispatch (the part that actually streams from the LLM upstream) so the image-pre-step can call into it cleanly. If the refactor balloons, alternative: inline the image preflight at the top of `_stream_chat_sse_impl` and don't extract — just monkeypatch `requests.post` for the upstream LLM in tests. Pick whichever is shorter.

- [ ] **Step 2: Run tests — expect failures**

- [ ] **Step 3: Implement the image-preflight in `_stream_chat_sse_impl`**

At the top of `_stream_chat_sse_impl`, after `request_id` setup but before the upstream chat round runs, insert:

```python
    # When the request carries images, run a VLM round first and inject
    # the description as a system message. Failure surfaces in the chat
    # context as a system note so the LLM can apologize gracefully —
    # never silently drop the image content.
    image_description: str | None = None
    if image_blobs:
        from glados.vision.client import describe_images, VisionClientError
        # Single multimodal call covers all attached images in one round.
        try:
            image_description = describe_images(
                [blob for blob, _mime in image_blobs],
                "Describe these images concisely. If they are diagrams or "
                "code, summarize what they show. 2-4 sentences total.",
            )
            logger.info(
                "[{}] vlm preflight: {} image(s), description {} chars",
                request_id, len(image_blobs), len(image_description),
            )
        except VisionClientError as exc:
            image_description = f"(vision lookup failed: {exc})"
            logger.warning(
                "[{}] vlm preflight failed: {}",
                request_id, exc,
            )
```

Then, where the round-2 chat messages are built (the existing `messages` list assembled before the upstream chat POST), inject the synthetic system message:

```python
    # Inject image description as a system message so the chat LLM has
    # textual context for what the user attached. Goes BEFORE the
    # user's turn so the chat lane sees it as scene context.
    if image_description is not None:
        messages.insert(
            -1,  # before the last user message
            {"role": "system", "content": f"[image_descriptions] {image_description}"},
        )
```

(Adjust the `messages.insert(-1, ...)` index based on the actual variable holding the round-1 message list — the goal is to land it immediately before the user's message that came in with the images.)

- [ ] **Step 4: Run tests**

```bash
pytest tests/core/test_api_wrapper_chat_two_round_vlm.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run the full chat-stream regression**

```bash
pytest tests/core/ -k "chat or stream" -v
```

Expected: no regressions in chat-stream tests.

- [ ] **Step 6: Commit**

```bash
git add glados/core/api_wrapper.py tests/core/test_api_wrapper_chat_two_round_vlm.py
git commit -m "feat(chat): two-round VLM-then-chat flow for image-bearing turns"
```

---

## Task 3: WebUI chat-input attachment queue

**Goal:** Operator can paste, drop, or use a file picker to attach 1–4 images. A row of thumbnail chips appears above the chat input. Each chip has a remove-X. Send includes the images in the POST body.

**Files:**
- Modify: `glados/webui/static/ui.js` — chat-input class.

**Acceptance Criteria:**
- [ ] `Ctrl+V` of an image (clipboard) attaches a thumbnail.
- [ ] Drag-drop of an image file onto the chat-input attaches a thumbnail.
- [ ] A "Attach image" button (📎 icon) opens the file picker; multi-select up to remaining quota.
- [ ] Up to 4 thumbnails render as chips with a remove-X button.
- [ ] Selecting a 5th image shows a non-blocking inline message ("Max 4 images per message") and does NOT add it.
- [ ] Per-image > 5 MB OR non-JPG/PNG/WebP: rejected client-side with an inline message ("Only JPG/PNG/WebP up to 5 MB").
- [ ] Send-button click POSTs `{messages, stream, images: [data-url, ...]}` and clears the attachment queue.
- [ ] After send, the user's bubble immediately renders with the inline thumbnails (using `.chat-inline-image` styling — same class as Slice 1's assistant-side images).

**Verify:** Hand-test in browser. Drop image → chip appears → send → user bubble shows thumbnail + GLaDOS replies.

**Steps:**

- [ ] **Step 1: Locate the existing chat-input class**

```bash
grep -n "ChatInput\|chat-input\|class.*Chat\|sendChatMessage\|/api/chat" glados/webui/static/ui.js | head -10
```

Identify the class / module owning the chat-input DOM + the send-button handler. Determine whether it's a class or a flat module.

- [ ] **Step 2: Add an attachment-queue field + DOM**

Inside the chat-input class init, add:

```javascript
this._attachments = [];  // [{dataUrl, mime, sizeBytes, name}]
this._attachmentRowEl = document.createElement('div');
this._attachmentRowEl.className = 'chat-attachment-row';
// Insert above the textarea
this.inputContainerEl.insertBefore(this._attachmentRowEl, this.textareaEl);

// Attach button
this._attachBtn = document.createElement('button');
this._attachBtn.type = 'button';
this._attachBtn.className = 'chat-attach-btn';
this._attachBtn.title = 'Attach image';
this._attachBtn.innerHTML = '&#128206;';  // 📎
this._attachBtn.addEventListener('click', () => this._openFilePicker());
this.actionsEl.appendChild(this._attachBtn);
```

- [ ] **Step 3: Add the attachment validation + add helpers**

```javascript
const _SUPPORTED_MIMES = new Set(['image/jpeg', 'image/png', 'image/webp']);
const _MAX_PER_IMAGE = 5 * 1024 * 1024;
const _MAX_IMAGES = 4;

_addAttachmentFromFile(file) {
  if (this._attachments.length >= _MAX_IMAGES) {
    this._inlineNotice(`Max ${_MAX_IMAGES} images per message.`);
    return;
  }
  if (!_SUPPORTED_MIMES.has(file.type)) {
    this._inlineNotice(`Only JPG / PNG / WebP supported (got ${file.type}).`);
    return;
  }
  if (file.size > _MAX_PER_IMAGE) {
    this._inlineNotice(`Image exceeds 5 MB (got ${(file.size/1024/1024).toFixed(1)} MB).`);
    return;
  }
  const reader = new FileReader();
  reader.onload = (ev) => {
    this._attachments.push({
      dataUrl: ev.target.result,
      mime: file.type,
      sizeBytes: file.size,
      name: file.name || 'pasted image',
    });
    this._renderAttachmentChips();
  };
  reader.readAsDataURL(file);
}

_renderAttachmentChips() {
  this._attachmentRowEl.innerHTML = '';
  this._attachments.forEach((att, idx) => {
    const chip = document.createElement('div');
    chip.className = 'chat-attachment-chip';
    const img = document.createElement('img');
    img.src = att.dataUrl;
    chip.appendChild(img);
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'chat-attachment-remove';
    x.textContent = '×';
    x.addEventListener('click', () => {
      this._attachments.splice(idx, 1);
      this._renderAttachmentChips();
    });
    chip.appendChild(x);
    this._attachmentRowEl.appendChild(chip);
  });
}

_inlineNotice(text) {
  // Reuses existing toast / inline-banner if present; otherwise console + brief flash
  if (typeof this.showToast === 'function') return this.showToast(text);
  console.warn('[chat-input]', text);
}
```

- [ ] **Step 4: Wire paste / drop / file-picker handlers**

```javascript
// Paste
this.textareaEl.addEventListener('paste', (ev) => {
  for (const item of ev.clipboardData.items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const f = item.getAsFile();
      if (f) this._addAttachmentFromFile(f);
      ev.preventDefault();
    }
  }
});

// Drop
this.textareaEl.addEventListener('dragover', (ev) => ev.preventDefault());
this.textareaEl.addEventListener('drop', (ev) => {
  ev.preventDefault();
  for (const f of ev.dataTransfer.files || []) {
    if (f.type.startsWith('image/')) this._addAttachmentFromFile(f);
  }
});

// File picker
_openFilePicker() {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = 'image/jpeg,image/png,image/webp';
  input.addEventListener('change', () => {
    for (const f of input.files) this._addAttachmentFromFile(f);
  });
  input.click();
}
```

- [ ] **Step 5: Update the send handler**

In the existing send-message function, before calling `fetch('/v1/chat/completions', ...)`:

```javascript
const body = {
  // ...existing fields (model, messages, stream)...
};
if (this._attachments.length > 0) {
  body.images = this._attachments.map((a) => a.dataUrl);
}
```

After the POST is dispatched, render the user-bubble inline thumbnails:

```javascript
// Append the user bubble; THEN attach inline thumbnails to it.
const userBubble = renderUserBubble(messageText);
for (const att of this._attachments) {
  const img = document.createElement('img');
  img.className = 'chat-inline-image';  // reuses Slice 1 class
  img.src = att.dataUrl;
  userBubble.bodyEl.appendChild(img);
}
this._attachments = [];
this._renderAttachmentChips();
```

- [ ] **Step 6: Hand-test**

Deploy the container (Slice 1 must already be live). In the browser:

1. Drop an image onto the chat input → chip appears → press Send → user bubble shows the image and GLaDOS describes it.
2. Drop 5 images → 4 attach, 5th shows the "Max 4 images" notice.
3. Drop a HEIC → rejected with the format notice.
4. Drop a 6 MB JPEG → rejected with the size notice.
5. Reload the page after a chat turn that had images → user bubble shows text only (no thumbnails) — confirming the no-persist MVP rule.

- [ ] **Step 7: Commit**

```bash
git add glados/webui/static/ui.js
git commit -m "feat(webui): chat-input image attachment queue with paste/drop/picker"
```

---

## Task 4: WebUI styles for attachment chips + user-bubble images

**Goal:** Match the existing chat aesthetic. Chips are 40×40px thumbnails with a corner remove-X.

**Files:**
- Modify: `glados/webui/static/ui.css`

**Acceptance Criteria:**
- [ ] `.chat-attachment-row` lays out chips horizontally with 6px gaps, wraps at narrow widths.
- [ ] `.chat-attachment-chip` is 40×40px with rounded corners; the inner `<img>` covers the full chip.
- [ ] `.chat-attachment-remove` is a small × button overlaid on the top-right of the chip.
- [ ] `.chat-user-bubble .chat-inline-image` uses the same Slice-1 class — no new image-render rule.

**Verify:** Hand-check after Task 3.

**Steps:**

- [ ] **Step 1: Add the rules**

Append to `glados/webui/static/ui.css`:

```css
.chat-attachment-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 8px;
  min-height: 0;
}
.chat-attachment-row:empty { display: none; }

.chat-attachment-chip {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.2);
}
.chat-attachment-chip img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.chat-attachment-remove {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: rgba(0,0,0,0.7);
  color: #fff;
  border: none;
  font-size: 12px;
  line-height: 14px;
  cursor: pointer;
  padding: 0;
}

.chat-attach-btn {
  background: transparent;
  border: none;
  font-size: 18px;
  cursor: pointer;
  padding: 4px 8px;
}
.chat-attach-btn:hover { opacity: 0.8; }
```

- [ ] **Step 2: Hand-verify**

Visual inspection — chips render right size, remove-X is clickable, no layout overflow at narrow widths.

- [ ] **Step 3: Commit**

```bash
git add glados/webui/static/ui.css
git commit -m "feat(webui): styles for chat attachment chips"
```

---

## Task 5: Live-probe smoke + slice closeout

**Goal:** Confirm the deployed container handles a real image-bearing chat turn end-to-end.

**Files:** None (verification only).

**Acceptance Criteria:**
- [ ] WebUI: drop a known JPEG, type "What is this?", press Send. User bubble shows the thumbnail; GLaDOS reply describes the image accurately.
- [ ] Container logs show `vlm preflight: 1 image(s), description NN chars` followed by the chat-stream round.
- [ ] Drop 5 images → client-side rejects 5th; 4 still send fine.
- [ ] Drop a 6 MB JPEG (force-resize one) → client-side rejects.
- [ ] Manually craft a curl with 5 images to bypass client → server returns 400 with "max 4 images".
- [ ] Manually craft a curl with one 6 MB image → server returns 400 with "image 0 exceeds 5 MB".
- [ ] Manually craft a curl with `Content-Length: 30000000` → server returns 413 (test with a tool that doesn't auto-correct CL).

**Verify:** Operator-witnessed.

**Steps:**

- [ ] **Step 1: Deploy + live-probe in the browser**

```bash
git push origin <branch>
python scripts/deploy_ghcr.py
```

Open WebUI; run the test cases listed in Acceptance Criteria.

- [ ] **Step 2: Curl-side server-budget probes**

```bash
# Build a 5-image payload
python -c "
import base64, json
img = base64.b64encode(b'\xff\xd8jpg').decode()
print(json.dumps({
  'stream': True,
  'messages': [{'role':'user','content':'describe'}],
  'images': [f'data:image/jpeg;base64,{img}'] * 5,
}))
" > /tmp/5img.json

curl -i -X POST -H "Content-Type: application/json" \
     --data @/tmp/5img.json \
     https://glados.denofsyn.com/v1/chat/completions
```

Expect: `HTTP/1.1 400 Bad Request` with body containing `"max 4 images"`.

(The Cloudflare Access cookie must be in the operator's curl session for prod; alternative: probe against the LAN IP container.)

- [ ] **Step 3: Update CHANGES.md and close out**

Append a new entry describing Slice 2. No code commit for the smoke itself.

---

## Self-Review Checklist

- [ ] Spec coverage: §2 Feature C flow (4 numbered steps), §3 file table for `api_wrapper.py` modify + `ui.js` modify, §3 server-side request budgets, §4 error rows for Feature C all map to a task above. ✓
- [ ] No placeholders: every step has actual code or actual commands.
- [ ] Type consistency: `image_blobs` shape `list[tuple[bytes, str]]` is used identically across Tasks 1 and 2; `images: [data-url]` shape is consistent between client (Task 3) and server (Task 1).
- [ ] No reach into Slice 1 territory: the slice does NOT modify `glados/cameras/*`, `glados/vision/client.py`, `builtin_tools.py`. ✓
- [ ] No reach into Slice 3 territory: no EventRouter, HAWebSocketHub, audio-root, or audit-source-tag changes. ✓
- [ ] Image-persistence rule honored: server stores nothing; client clears the attachment queue after send; user-bubble thumbnails live only in the live tab. ✓
- [ ] Auth: chat-scope user permissions cover image attach; no new admin gates. ✓
