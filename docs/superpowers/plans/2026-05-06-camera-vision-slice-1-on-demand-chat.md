# Camera Vision — Slice 1: On-Demand Chat Camera Vision

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operator says *"What do you see in the back yard?"* in chat → GLaDOS describes the scene + the snapshot renders inline in the chat bubble. Single chat-LLM tool call. Vendor-agnostic via the existing `llm_vision` service slot. Ships independently of slices 2 and 3.

**Architecture:** New chat-only built-in tool `look_at_camera(camera_name)` registered in `glados/core/builtin_tools.py`. Dispatch resolves the friendly camera name against an HA-discovery cache, fetches a JPEG via HA's `/api/camera_proxy/<id>`, POSTs OpenAI-multimodal `[{image_url}, {text}]` to the operator-configured `llm_vision` slot, and returns `{description}` (text only) to the chat LLM. Snapshot bytes are emitted as a parallel `event: image` SSE chunk keyed by `tool_call_id` — never enter LLM context. WebUI pairs the SSE chunk to the in-progress assistant bubble and renders an inline thumbnail.

**Tech Stack:** Python 3.12, `requests`/`urllib`, loguru, the existing pydantic-based `cfg.services.llm_vision` slot, the existing chat SSE pipeline in `glados/core/api_wrapper.py`, vanilla JS in `glados/webui/static/ui.js`, pytest with `responses`/`unittest.mock` for HTTP mocking. AIBox-side: llama.cpp + Qwen2.5-VL-3B-Instruct under NSSM.

**Spec:** [`docs/superpowers/specs/2026-05-05-camera-vision-design.md`](glados-container/docs/superpowers/specs/2026-05-05-camera-vision-design.md) — Feature A.

---

## Slice Boundaries

**This slice produces a working behavior on its own** — operator can ask the chat about any HA camera and get a text + thumbnail answer. Nothing in slices 2 or 3 is required to deploy this.

**Files this slice owns (no other slice modifies them):**
- `glados/vision/client.py` (new)
- `glados/cameras/__init__.py`, `discovery.py`, `snapshot.py` (new)
- `glados/core/builtin_tools.py` (modify — add `TOOL_LOOK_AT_CAMERA` + dispatch alongside existing tools)
- `glados/core/api_wrapper.py` (modify — `_stream_chat_sse_impl`: add `event: image` emission for `look_at_camera` tool calls only)
- `glados/webui/static/ui.js` (modify — handle `event: image` SSE chunks, render inline thumbnails in assistant bubbles)
- `glados/webui/static/ui.css` (modify — `.chat-inline-image` rules)

**Files this slice DELETES (full removal):**
- `glados/tools/vision_look.py`
- `glados/vision/__init__.py` (rewritten — drops the lazy-stub)
- `glados/vision/vision_config.py`, `vision/vision_request.py`, `vision/vision_state.py` (vestigial in-process VLM queue path)

**Files this slice REWIRES (because they import the deleted modules):**
- `glados/tools/__init__.py` (drop `VisionLook` import + registry entries)
- `glados/core/engine.py` (drop `VisionConfig`/`VisionState`/`VisionProcessor` plumbing — lines around 36, 256, 374, 407, 486, 945, 1039)
- `glados/core/llm_processor.py` (drop `VisionState` parameter — line 25, 242; drop `vision_look`-name filter at lines 857, 872)
- `glados/autonomy/loop.py` (drop `VisionState` parameter — lines 16, 32)
- `glados/vision/constants.py` (drop the `vision_look` mention in `SYSTEM_PROMPT_VISION_HANDLING`)

**Out of scope (deferred to other slices):**
- User-attached chat images (Slice 2)
- Event-triggered vision cascade, EventRouter, HAWebSocketHub, audio-root migration, audit `event_rule` source (Slice 3)
- `glados/autonomy/agents/camera_watcher.py` cleanup of the dead `:8016` polling — flagged as adjacent debt in the spec, separate follow-up.

**Dependencies:** Operator-side AIBox stand-up of `llamacpp-vision` on `:11437` (Task 1 below). This is a production write requiring per-action sign-off per the operator's research-before-prod-writes rule. Container code can be developed and unit-tested without it; live-probe verification (Task 10) requires it.

---

## File Structure

| File | Responsibility |
|---|---|
| `glados/vision/client.py` | OpenAI-multimodal POST to the `llm_vision` slot. One function: `describe_images(images: list[bytes], prompt: str, *, mime: str = "image/jpeg") -> str`. Pure I/O wrapper — no caching, no retries, no fallbacks. Errors raise `VisionClientError`. |
| `glados/cameras/__init__.py` | Re-exports: `CameraDiscovery`, `CameraSnapshotError`, `fetch_snapshot`. |
| `glados/cameras/discovery.py` | `CameraDiscovery` — singleton (per process) that caches the `camera.*` entity list from HA's `/api/states` with a 60-second TTL. Provides `list_cameras()` (returns `[(entity_id, friendly_name)]`) and `resolve_camera_name(query: str) -> str | None` (case-insensitive substring + token match against friendly names and entity-id-without-prefix). Lazy: refreshes on access if cache stale. |
| `glados/cameras/snapshot.py` | `fetch_snapshot(entity_id: str) -> bytes` — GET `<HA_URL>/api/camera_proxy/<entity_id>` with `Authorization: Bearer <HA_TOKEN>`, returns body bytes. Raises `CameraSnapshotError` on non-200, non-image content-type, or timeout. |
| `glados/core/builtin_tools.py` | Adds `TOOL_LOOK_AT_CAMERA` constant, the OpenAI tool definition, the `_look_at_camera(args)` dispatch, and a new helper `invoke_image_yielding_tool(tool_name, args) -> tuple[str, ImageEmission \| None]` so the SSE handler can pull image bytes out without leaking them into the LLM-bound JSON return. The existing `invoke_builtin_tool` signature is unchanged; image-yielding tools use the new entry point. |
| `glados/core/api_wrapper.py:_stream_chat_sse_impl` | After dispatch resolves a tool call, if the tool name is in the image-yielding set, write an `event: image\ndata: {...}\n\n` SSE chunk before appending the `{"role":"tool", ...}` message to the LLM history. Uses `tool_call_id` to key the chunk so the WebUI can pair it to the correct turn. |
| `glados/webui/static/ui.js` | Chat SSE consumer learns a new event type `image` — appends an `<img class="chat-inline-image">` to the in-progress assistant bubble keyed by `tool_call_id`. |
| `glados/webui/static/ui.css` | `.chat-inline-image { max-width: 320px; max-height: 240px; border-radius: 6px; margin: 8px 0; }` plus a 1px GLaDOS-orange border to match existing chat-bubble accents. |

---

## Task 1: AIBox stand-up of `llamacpp-vision` (operator-deploy task)

**Goal:** A reachable OpenAI-compatible VLM endpoint at `http://aibox:11437/v1/chat/completions` serving Qwen2.5-VL-3B.

**Files (none in this repo — operator-side):**
- `C:\llamacpp\llama-server.exe` (existing CUDA-enabled binary)
- `C:\llamacpp\models\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf`, `mmproj-Q8_0.gguf`
- `C:\llamacpp\nssm\llamacpp-vision\start_vision.bat` (new)
- NSSM service `llamacpp-vision`

**Acceptance Criteria:**
- [ ] NSSM service `llamacpp-vision` is `Running`.
- [ ] `Invoke-WebRequest http://aibox:11437/v1/models` returns 200 with the Qwen2.5-VL model id.
- [ ] A test multimodal POST to `:11438` (shadow port) returns a non-empty description for a known JPEG.
- [ ] Promote shadow `:11438` → live `:11437` only after the shadow probe passes.

**Verify:** Live shadow probe with a known JPEG fixture; promote to `:11437` only after pass.

**Steps:**

- [ ] **Step 1: Get operator sign-off on the literal NSSM commands**

Per the research-before-prod-writes rule, the operator must explicitly approve these mutations BEFORE execution. Present the exact commands:

```cmd
:: Download model files (one-time)
:: Place at C:\llamacpp\models\:
::   Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf
::   mmproj-Q8_0.gguf

:: Create start_vision.bat at C:\llamacpp\nssm\llamacpp-vision\start_vision.bat
@echo off
"C:\llamacpp\llama-server.exe" ^
  --model "C:\llamacpp\models\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf" ^
  --mmproj "C:\llamacpp\models\mmproj-Q8_0.gguf" ^
  --port 11438 ^
  --host 0.0.0.0 ^
  --gpu-layers 999 ^
  --device CUDA1 ^
  --ctx-size 8192 ^
  --parallel 1 ^
  --flash-attn off

:: NSSM install (full args — never invoke 'nssm install <name>' alone, that opens a GUI)
nssm install llamacpp-vision-shadow C:\llamacpp\nssm\llamacpp-vision\start_vision.bat
nssm set llamacpp-vision-shadow AppDirectory C:\llamacpp
nssm set llamacpp-vision-shadow AppStdout C:\llamacpp\logs\llamacpp-vision.stdout.log
nssm set llamacpp-vision-shadow AppStderr C:\llamacpp\logs\llamacpp-vision.stderr.log
nssm set llamacpp-vision-shadow Start SERVICE_AUTO_START
nssm start llamacpp-vision-shadow
```

Wait for explicit operator approval before proceeding.

- [ ] **Step 2: Stand up the SHADOW service on `:11438` first**

Run the commands from Step 1 with `--port 11438` and service name `llamacpp-vision-shadow`. Tail the log:

```cmd
type C:\llamacpp\logs\llamacpp-vision.stderr.log
```

Expected: `HTTP server listening on 0.0.0.0:11438` within ~30 seconds.

- [ ] **Step 3: Probe the shadow service**

```bash
curl -s http://aibox:11438/v1/models | python -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])"
```

Expected output: `Qwen2.5-VL-3B-Instruct` (or whatever id llama.cpp surfaces).

Multimodal probe with a fixture JPEG:

```bash
B64=$(base64 -w0 ./tests/fixtures/cameras/sample_doorbell.jpg)
curl -s -X POST http://aibox:11438/v1/chat/completions -H "Content-Type: application/json" -d @- <<EOF
{"model":"Qwen2.5-VL-3B-Instruct","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,$B64"}},{"type":"text","text":"What do you see?"}]}]}
EOF
```

Expected: 200 with a non-empty `choices[0].message.content` describing the fixture image.

- [ ] **Step 4: Promote shadow → live only after the probe passes**

```cmd
nssm stop llamacpp-vision-shadow
nssm remove llamacpp-vision-shadow confirm
:: edit start_vision.bat: change --port 11438 to --port 11437
nssm install llamacpp-vision C:\llamacpp\nssm\llamacpp-vision\start_vision.bat
nssm set llamacpp-vision AppDirectory C:\llamacpp
nssm set llamacpp-vision AppStdout C:\llamacpp\logs\llamacpp-vision.stdout.log
nssm set llamacpp-vision AppStderr C:\llamacpp\logs\llamacpp-vision.stderr.log
nssm set llamacpp-vision Start SERVICE_AUTO_START
nssm start llamacpp-vision
```

- [ ] **Step 5: Configure the WebUI `llm_vision` slot**

In WebUI → Configuration → Services, set:
- `llm_vision.url` = `http://aibox:11437`
- `llm_vision.model` = `Qwen2.5-VL-3B-Instruct`

Save. The WebUI is the source of truth for service URLs (`feedback_webui_is_service_truth.md`).

- [ ] **Step 6: Verify live endpoint via WebUI**

Re-run the multimodal curl from Step 3 against `:11437` (not `:11438`). Expect identical-shape success.

- [ ] **Step 7: No commit (operator-deploy task only)**

This task ships no container code. Move to Task 2.

---

## Task 2: `glados/vision/client.py` — VLM HTTP client

**Goal:** A pure I/O function `describe_images(images, prompt) -> str` that POSTs OpenAI-multimodal to the `llm_vision` slot and returns the description text.

**Files:**
- Create: `glados/vision/client.py`
- Test: `tests/vision/test_client.py`

**Acceptance Criteria:**
- [ ] `describe_images([jpeg_bytes], "describe this")` returns the upstream `choices[0].message.content` string.
- [ ] Multi-image call POSTs `content` with N `image_url` parts in order, then one `text` part.
- [ ] `Authorization: Bearer <api_key>` header passes through when the slot has `api_key`.
- [ ] Non-200 upstream raises `VisionClientError` with `status` and `body[:500]`.
- [ ] Timeout raises `VisionClientError` with the URL in the message.
- [ ] No retries, no fallbacks (per the no-silent-fallback rule).

**Verify:** `pytest tests/vision/test_client.py -v` → all tests pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/vision/__init__.py` (empty) and `tests/vision/test_client.py`:

```python
"""Unit tests for glados/vision/client.py — VLM client."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch, MagicMock

import pytest

from glados.vision.client import describe_images, VisionClientError


@pytest.fixture
def fake_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _ok_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _err_response(status: int, body: str = "boom") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    return resp


def _stub_slot(url: str = "http://aibox:11437", model: str = "qwen-vl", api_key: str | None = None):
    return MagicMock(url=url, model=model, api_key=api_key)


def test_single_image_returns_description(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("a cat")) as post:
        result = describe_images([fake_jpeg], "what is in this image?")
    assert result == "a cat"
    args, kwargs = post.call_args
    body = kwargs["json"]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[-1] == {"type": "text", "text": "what is in this image?"}
    assert body["model"] == "qwen-vl"


def test_multi_image_preserves_order(fake_jpeg):
    img2 = b"\xff\xd8\xff\xe0two"
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("two things")):
        describe_images([fake_jpeg, img2], "compare")
    # Re-grab the call
    from glados.vision import client as _c
    last_call = _c.requests.post.call_args  # type: ignore[attr-defined]
    parts = last_call.kwargs["json"]["messages"][0]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 2
    assert base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == fake_jpeg
    assert base64.b64decode(image_parts[1]["image_url"]["url"].split(",", 1)[1]) == img2


def test_api_key_passed_in_authorization(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot(api_key="secret")), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("ok")) as post:
        describe_images([fake_jpeg], "x")
    headers = post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret"


def test_no_api_key_no_authorization(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot(api_key=None)), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("ok")) as post:
        describe_images([fake_jpeg], "x")
    headers = post.call_args.kwargs["headers"]
    assert "Authorization" not in headers


def test_non_200_raises(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_err_response(502, "upstream gone")):
        with pytest.raises(VisionClientError) as exc:
            describe_images([fake_jpeg], "x")
    assert "502" in str(exc.value)
    assert "upstream gone" in str(exc.value)


def test_timeout_raises(fake_jpeg):
    import requests
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", side_effect=requests.Timeout("slow")):
        with pytest.raises(VisionClientError) as exc:
            describe_images([fake_jpeg], "x")
    assert "http://aibox:11437" in str(exc.value)


def test_empty_image_list_raises(fake_jpeg):
    with pytest.raises(ValueError):
        describe_images([], "x")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/vision/test_client.py -v
```

Expected: ImportError / collection failure (module doesn't exist yet).

- [ ] **Step 3: Implement the client**

Create `glados/vision/client.py`:

```python
"""OpenAI-multimodal client for the ``llm_vision`` service slot.

Single function: ``describe_images(images, prompt) -> str``. Pure I/O.
No retries, no fallbacks (per ``feedback_no_silent_fallback.md``).
The slot URL/model/api_key live in ``cfg.services.llm_vision`` and are
operator-configurable via the WebUI services tab — never hardcoded.
"""

from __future__ import annotations

import base64
from typing import Any

import requests
from loguru import logger


class VisionClientError(RuntimeError):
    """Surfaces every VLM-call failure with the URL and cause inline."""


def _get_slot() -> Any:
    # Local import — cfg load pulls in much of the engine; keep it lazy
    # so test mocks can replace this hook without dragging in the world.
    from glados.core.config_store import cfg
    return cfg.services.llm_vision


def _b64_data_url(image: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


def describe_images(
    images: list[bytes],
    prompt: str,
    *,
    mime: str = "image/jpeg",
    timeout: float = 30.0,
) -> str:
    """POST OpenAI-multimodal to ``llm_vision``; return the model's text."""
    if not images:
        raise ValueError("describe_images requires at least one image")

    slot = _get_slot()
    url = slot.url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if slot.api_key:
        headers["Authorization"] = f"Bearer {slot.api_key}"

    parts: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": _b64_data_url(img, mime)}}
        for img in images
    ]
    parts.append({"type": "text", "text": prompt})

    body = {
        "model": slot.model,
        "messages": [{"role": "user", "content": parts}],
        "stream": False,
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise VisionClientError(f"vision endpoint {slot.url} failed: {exc}") from exc

    if resp.status_code != 200:
        raise VisionClientError(
            f"vision endpoint {slot.url} returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise VisionClientError(f"vision endpoint {slot.url} bad response: {exc}") from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/vision/test_client.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/vision/client.py tests/vision/__init__.py tests/vision/test_client.py
git commit -m "feat(vision): VLM client for the llm_vision slot"
```

---

## Task 3: `glados/cameras/discovery.py` — HA camera discovery cache

**Goal:** A 60-second-TTL cache of HA's `camera.*` entities with friendly-name resolution.

**Files:**
- Create: `glados/cameras/__init__.py`, `glados/cameras/discovery.py`
- Test: `tests/cameras/test_discovery.py`

**Acceptance Criteria:**
- [ ] `list_cameras()` returns `[(entity_id, friendly_name), ...]` from a single mocked `/api/states` call.
- [ ] Repeated calls within 60 s use the cache (single HTTP call total).
- [ ] After 60 s, the next call refreshes from HA.
- [ ] `resolve_camera_name("back yard")` returns `camera.backyard_high` when friendly_name is `Backyard High`.
- [ ] `resolve_camera_name("nonexistent")` returns `None`.
- [ ] HA returning non-200 raises `CameraDiscoveryError` with the status.

**Verify:** `pytest tests/cameras/test_discovery.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cameras/__init__.py` (empty) and `tests/cameras/test_discovery.py`:

```python
"""Unit tests for glados/cameras/discovery.py."""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from glados.cameras.discovery import (
    CameraDiscovery,
    CameraDiscoveryError,
)


HA_STATES = [
    {"entity_id": "camera.backyard_high", "attributes": {"friendly_name": "Backyard High"}},
    {"entity_id": "camera.front_door_high", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen"}},
]


def _ok_states() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = HA_STATES
    return resp


def test_list_cameras_filters_to_camera_domain():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=60.0)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        cams = disco.list_cameras()
    assert ("camera.backyard_high", "Backyard High") in cams
    assert ("camera.front_door_high", "Front Door") in cams
    assert all(eid.startswith("camera.") for eid, _ in cams)


def test_repeated_calls_within_ttl_use_cache():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=60.0)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()) as g:
        disco.list_cameras()
        disco.list_cameras()
        disco.list_cameras()
    assert g.call_count == 1


def test_call_after_ttl_refreshes():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=0.01)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()) as g:
        disco.list_cameras()
        time.sleep(0.05)
        disco.list_cameras()
    assert g.call_count == 2


def test_resolve_camera_name_friendly_match():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("back yard") == "camera.backyard_high"
        assert disco.resolve_camera_name("BACKYARD") == "camera.backyard_high"
        assert disco.resolve_camera_name("front door") == "camera.front_door_high"


def test_resolve_camera_name_entity_id_match():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("backyard_high") == "camera.backyard_high"


def test_resolve_camera_name_miss_returns_none():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("garage") is None


def test_ha_non_200_raises():
    err = MagicMock()
    err.status_code = 401
    err.text = "unauthorized"
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="bad")
    with patch("glados.cameras.discovery.requests.get", return_value=err):
        with pytest.raises(CameraDiscoveryError) as exc:
            disco.list_cameras()
    assert "401" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/cameras/test_discovery.py -v
```

Expected: collection failure.

- [ ] **Step 3: Implement the module**

Create `glados/cameras/__init__.py`:

```python
"""HA camera discovery + snapshot helpers."""

from .discovery import CameraDiscovery, CameraDiscoveryError
from .snapshot import fetch_snapshot, CameraSnapshotError

__all__ = [
    "CameraDiscovery",
    "CameraDiscoveryError",
    "CameraSnapshotError",
    "fetch_snapshot",
]
```

Create `glados/cameras/discovery.py`:

```python
"""HA camera-entity discovery cache.

Uses HA's HTTP ``/api/states`` (NOT WebSocket) — this slice ships
without the shared HAWebSocketHub that Slice 3 introduces. Polling is
fine: cameras change rarely and a 60-second TTL is well within the
operator's expectation of 'operator added a new camera, want to ask
about it'.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests
from loguru import logger


class CameraDiscoveryError(RuntimeError):
    """HA states fetch failed."""


@dataclass(frozen=True)
class _CamEntry:
    entity_id: str
    friendly_name: str


def _normalize(text: str) -> str:
    return text.casefold().replace("_", " ").replace(".", " ").strip()


class CameraDiscovery:
    """Per-process cache of HA camera entities.

    Construct once (the chat tool dispatch caches the singleton) and
    reuse across requests. Thread-safe — list_cameras() guards the
    refresh path with a lock.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        *,
        ttl_s: float = 60.0,
        timeout_s: float = 5.0,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cache: list[_CamEntry] = []
        self._fetched_at: float = 0.0

    def _fetch(self) -> list[_CamEntry]:
        url = f"{self._ha_url}/api/states"
        headers = {"Authorization": f"Bearer {self._ha_token}"}
        try:
            resp = requests.get(url, headers=headers, timeout=self._timeout_s)
        except requests.RequestException as exc:
            raise CameraDiscoveryError(f"HA states unreachable at {self._ha_url}: {exc}") from exc
        if resp.status_code != 200:
            raise CameraDiscoveryError(
                f"HA states returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            states = resp.json()
        except ValueError as exc:
            raise CameraDiscoveryError(f"HA states bad JSON: {exc}") from exc

        cams: list[_CamEntry] = []
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("camera."):
                continue
            friendly = s.get("attributes", {}).get("friendly_name") or eid
            cams.append(_CamEntry(entity_id=eid, friendly_name=friendly))
        return cams

    def list_cameras(self) -> list[tuple[str, str]]:
        """Return ``[(entity_id, friendly_name), ...]``. Refreshes if TTL elapsed."""
        with self._lock:
            now = time.time()
            if not self._cache or (now - self._fetched_at) >= self._ttl_s:
                self._cache = self._fetch()
                self._fetched_at = now
            return [(c.entity_id, c.friendly_name) for c in self._cache]

    def resolve_camera_name(self, query: str) -> str | None:
        """Return the entity_id whose friendly_name or short id best matches ``query``.

        Match strategy (in order):
          1. Exact normalized friendly_name match.
          2. Substring match against normalized friendly_name.
          3. Substring match against entity_id stripped of ``camera.`` prefix
             (so 'backyard_high' resolves directly).
        First hit wins. ``None`` if nothing matches.
        """
        cams = self.list_cameras()
        q = _normalize(query)
        if not q:
            return None

        # Pass 1: exact friendly_name (normalized)
        for eid, friendly in cams:
            if _normalize(friendly) == q:
                return eid

        # Pass 2: substring against friendly_name
        for eid, friendly in cams:
            if q in _normalize(friendly):
                return eid

        # Pass 3: substring against entity_id sans prefix
        for eid, _friendly in cams:
            short = _normalize(eid.replace("camera.", "", 1))
            if q in short:
                return eid

        return None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/cameras/test_discovery.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/cameras/__init__.py glados/cameras/discovery.py tests/cameras/__init__.py tests/cameras/test_discovery.py
git commit -m "feat(cameras): HA camera discovery cache with name resolution"
```

---

## Task 4: `glados/cameras/snapshot.py` — HA snapshot fetch

**Goal:** Fetch JPEG bytes from HA's `/api/camera_proxy/<entity_id>`.

**Files:**
- Create: `glados/cameras/snapshot.py`
- Test: `tests/cameras/test_snapshot.py`

**Acceptance Criteria:**
- [ ] `fetch_snapshot("camera.backyard_high")` returns bytes from a mocked 200 response.
- [ ] Non-200 raises `CameraSnapshotError` with status + entity_id.
- [ ] Non-image content-type raises `CameraSnapshotError`.
- [ ] Timeout raises `CameraSnapshotError` with the URL.

**Verify:** `pytest tests/cameras/test_snapshot.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cameras/test_snapshot.py`:

```python
"""Unit tests for glados/cameras/snapshot.py."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
import requests

from glados.cameras.snapshot import fetch_snapshot, CameraSnapshotError


def _ok_jpeg(body: bytes = b"\xff\xd8\xff\xe0jpeg") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.content = body
    return resp


def test_returns_bytes_on_200():
    with patch("glados.cameras.snapshot.requests.get", return_value=_ok_jpeg(b"abc")):
        out = fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert out == b"abc"


def test_non_200_raises():
    err = MagicMock()
    err.status_code = 404
    err.headers = {}
    err.text = "not found"
    with patch("glados.cameras.snapshot.requests.get", return_value=err):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.missing", ha_url="http://ha:8123", ha_token="t")
    assert "404" in str(exc.value)
    assert "camera.missing" in str(exc.value)


def test_non_image_content_type_raises():
    bad = MagicMock()
    bad.status_code = 200
    bad.headers = {"Content-Type": "text/html"}
    bad.content = b"<html>"
    with patch("glados.cameras.snapshot.requests.get", return_value=bad):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert "text/html" in str(exc.value)


def test_timeout_raises():
    with patch("glados.cameras.snapshot.requests.get", side_effect=requests.Timeout("slow")):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert "http://ha:8123" in str(exc.value)
```

- [ ] **Step 2: Run tests — expect collection failure**

```bash
pytest tests/cameras/test_snapshot.py -v
```

- [ ] **Step 3: Implement**

Create `glados/cameras/snapshot.py`:

```python
"""HA camera-snapshot fetch.

Single function: ``fetch_snapshot(entity_id, *, ha_url, ha_token) -> bytes``.
Errors raise ``CameraSnapshotError`` with the URL and cause.
"""

from __future__ import annotations

import requests
from loguru import logger


class CameraSnapshotError(RuntimeError):
    """HA snapshot fetch failed."""


def fetch_snapshot(
    entity_id: str,
    *,
    ha_url: str,
    ha_token: str,
    timeout_s: float = 8.0,
) -> bytes:
    """GET HA's ``/api/camera_proxy/<entity_id>``; return body bytes."""
    base = ha_url.rstrip("/")
    url = f"{base}/api/camera_proxy/{entity_id}"
    headers = {"Authorization": f"Bearer {ha_token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s)
    except requests.RequestException as exc:
        raise CameraSnapshotError(
            f"HA snapshot unreachable at {ha_url} for {entity_id}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise CameraSnapshotError(
            f"HA snapshot for {entity_id} returned {resp.status_code}: {resp.text[:200]}"
        )

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("image/"):
        raise CameraSnapshotError(
            f"HA snapshot for {entity_id} returned non-image content-type {ctype!r}"
        )

    return resp.content
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/cameras/test_snapshot.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/cameras/snapshot.py tests/cameras/test_snapshot.py
git commit -m "feat(cameras): HA snapshot fetch via camera_proxy"
```

---

## Task 5: `look_at_camera` built-in tool

**Goal:** Register the OpenAI tool definition + dispatch in `glados/core/builtin_tools.py`. Returns `{description}` text only; image bytes flow out of band.

**Files:**
- Modify: `glados/core/builtin_tools.py`
- Test: `tests/core/test_builtin_tools_look_at_camera.py`

**Acceptance Criteria:**
- [ ] `is_image_yielding_tool("look_at_camera")` returns True; for unrelated tool names, False.
- [ ] `invoke_image_yielding_tool("look_at_camera", {"camera_name": "back yard"})` returns `(json_str_with_description, ImageEmission(image_bytes, mime, ...))`.
- [ ] Camera-name miss returns a JSON `{"error": "no camera matched..."}` plus `ImageEmission=None`.
- [ ] Snapshot failure returns a JSON `{"error": "HA snapshot failed: ..."}` plus `ImageEmission=None`.
- [ ] VLM failure returns `{"error": "vision endpoint <url> failed: ..."}` plus `ImageEmission=None`.
- [ ] Tool definition appears in `get_builtin_tool_definitions()` output, after the existing two tools.
- [ ] Existing two tools' tests still pass — no regressions.

**Verify:** `pytest tests/core/test_builtin_tools_look_at_camera.py tests/core/ -k builtin -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_builtin_tools_look_at_camera.py`:

```python
"""Unit tests for the look_at_camera builtin tool."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from glados.core.builtin_tools import (
    TOOL_LOOK_AT_CAMERA,
    get_builtin_tool_definitions,
    invoke_image_yielding_tool,
    is_image_yielding_tool,
)


def test_predicate_matches_only_look_at_camera():
    assert is_image_yielding_tool("look_at_camera") is True
    assert is_image_yielding_tool("search_entities") is False
    assert is_image_yielding_tool("anything") is False


def test_definition_present_in_builtin_list():
    names = [d["function"]["name"] for d in get_builtin_tool_definitions()]
    assert TOOL_LOOK_AT_CAMERA in names


def test_happy_path_returns_description_and_image():
    fake_jpeg = b"\xff\xd8\xff\xe0pic"
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot", return_value=fake_jpeg), \
         patch("glados.core.builtin_tools.describe_images", return_value="a tabby cat"):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert parsed == {"description": "a tabby cat"}
    assert emission is not None
    assert emission.image_bytes == fake_jpeg
    assert emission.mime == "image/jpeg"


def test_missing_camera_returns_error_no_emission():
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = None
    fake_disco.list_cameras.return_value = [
        ("camera.front_door_high", "Front Door"),
        ("camera.backyard_high", "Backyard High"),
    ]
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "garage"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "garage" in parsed["error"]
    assert "Front Door" in parsed["error"]  # available list helps the LLM relay
    assert emission is None


def test_snapshot_failure_returns_error_no_emission():
    from glados.cameras.snapshot import CameraSnapshotError
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot",
               side_effect=CameraSnapshotError("404 not found")):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "404" in parsed["error"]
    assert emission is None


def test_vlm_failure_returns_error_no_emission():
    from glados.vision.client import VisionClientError
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot", return_value=b"\xff\xd8jpg"), \
         patch("glados.core.builtin_tools.describe_images",
               side_effect=VisionClientError("vision endpoint http://x failed: timeout")):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "http://x" in parsed["error"]
    assert emission is None
```

- [ ] **Step 2: Run tests — expect failure (TOOL_LOOK_AT_CAMERA not exported)**

```bash
pytest tests/core/test_builtin_tools_look_at_camera.py -v
```

- [ ] **Step 3: Add the tool to `glados/core/builtin_tools.py`**

After the existing tool constants (around line 40), add:

```python
TOOL_LOOK_AT_CAMERA = "look_at_camera"

_IMAGE_YIELDING_TOOL_NAMES: frozenset[str] = frozenset({TOOL_LOOK_AT_CAMERA})


def is_image_yielding_tool(tool_name: str) -> bool:
    """Router predicate. Returns True if the tool emits image bytes
    out-of-band (via SSE event:image) in addition to its text result."""
    return tool_name in _IMAGE_YIELDING_TOOL_NAMES
```

In `get_builtin_tool_definitions()`, append after the existing two tool dicts:

```python
        {
            "type": "function",
            "function": {
                "name": TOOL_LOOK_AT_CAMERA,
                "description": (
                    "Look at a Home Assistant camera and describe what is "
                    "visible. Use this when the user asks 'what do you see' "
                    "or asks about a specific camera by name. The function "
                    "fetches a snapshot, runs a vision model, and returns a "
                    "short scene description. The snapshot itself is rendered "
                    "in the UI separately — the chat reply should reference "
                    "the description naturally."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": (
                                "Friendly name or partial entity_id of the "
                                "camera (e.g. 'back yard', 'front door', "
                                "'backyard_high'). Case-insensitive."
                            ),
                        },
                    },
                    "required": ["camera_name"],
                },
            },
        },
```

Add the dataclass + dispatch (insert after the existing `_get_entity_details` helpers, before `invoke_builtin_tool`):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageEmission:
    """Out-of-band image bytes emitted alongside a tool's text result.

    The SSE handler picks this up and writes an ``event: image`` chunk
    keyed by ``tool_call_id`` so the WebUI can render the snapshot in
    the assistant bubble. The bytes never enter LLM context.
    """

    image_bytes: bytes
    mime: str
    tool_name: str


# Cached per-process discovery instance. First call constructs from cfg;
# subsequent calls reuse the same cache. Tests patch this hook.
_camera_discovery: object = None


def _get_camera_discovery() -> object:
    global _camera_discovery
    if _camera_discovery is None:
        from glados.core.config_store import cfg
        from glados.cameras.discovery import CameraDiscovery
        _camera_discovery = CameraDiscovery(
            ha_url=cfg.ha_url,
            ha_token=cfg.ha_token,
        )
    return _camera_discovery


def _look_at_camera(arguments: dict[str, Any]) -> tuple[str, "ImageEmission | None"]:
    """Resolve camera → snapshot → VLM. Returns (json_text_result, optional_image_emission).

    Errors are returned as JSON ``{"error": "..."}`` strings (visible to
    the chat LLM, which relays them) — never raised. Emission is None on
    any failure path so the SSE handler doesn't push a phantom image.
    """
    from glados.cameras.snapshot import fetch_snapshot, CameraSnapshotError
    from glados.vision.client import describe_images, VisionClientError
    from glados.core.config_store import cfg

    name = (arguments or {}).get("camera_name", "").strip()
    if not name:
        return json.dumps({"error": "camera_name is required"}), None

    disco = _get_camera_discovery()
    try:
        entity_id = disco.resolve_camera_name(name)
    except Exception as exc:
        return json.dumps({"error": f"camera discovery failed: {exc}"}), None

    if not entity_id:
        try:
            avail = ", ".join(f"{f} ({eid})" for eid, f in disco.list_cameras())
        except Exception:
            avail = "(camera list unavailable)"
        return (
            json.dumps({"error": f'no camera matched "{name}". Available: {avail}'}),
            None,
        )

    try:
        image_bytes = fetch_snapshot(
            entity_id, ha_url=cfg.ha_url, ha_token=cfg.ha_token,
        )
    except CameraSnapshotError as exc:
        return json.dumps({"error": f"HA snapshot failed: {exc}"}), None

    try:
        description = describe_images([image_bytes], "Describe what you see in this image in 1-3 sentences.")
    except VisionClientError as exc:
        return json.dumps({"error": str(exc)}), None

    return (
        json.dumps({"description": description}),
        ImageEmission(image_bytes=image_bytes, mime="image/jpeg", tool_name=TOOL_LOOK_AT_CAMERA),
    )


def invoke_image_yielding_tool(
    tool_name: str, arguments: dict[str, Any],
) -> tuple[str, "ImageEmission | None"]:
    """Dispatch entry point for tools that emit out-of-band image bytes.

    Returns ``(json_result_string, optional_emission)``. The
    ``json_result_string`` is what gets appended to the LLM's
    conversation as the ``{"role":"tool", ...}`` content. The emission
    (if any) is what the SSE handler turns into an ``event: image``
    chunk for the WebUI.
    """
    if tool_name == TOOL_LOOK_AT_CAMERA:
        return _look_at_camera(arguments)
    return json.dumps({"error": f"unknown image-yielding tool: {tool_name}"}), None
```

Also update the `__all__` list at module bottom:

```python
__all__ = [
    "TOOL_GET_ENTITY_DETAILS",
    "TOOL_LOOK_AT_CAMERA",
    "TOOL_SEARCH_ENTITIES",
    "ImageEmission",
    "get_builtin_tool_definitions",
    "invoke_builtin_tool",
    "invoke_image_yielding_tool",
    "is_builtin_tool",
    "is_image_yielding_tool",
]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/core/test_builtin_tools_look_at_camera.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run the full builtin-tools regression**

```bash
pytest tests/core/ -k builtin -v
```

Expected: existing tests still pass + the 6 new tests pass.

- [ ] **Step 6: Commit**

```bash
git add glados/core/builtin_tools.py tests/core/test_builtin_tools_look_at_camera.py
git commit -m "feat(tools): look_at_camera built-in tool with image-yielding dispatch"
```

---

## Task 6: SSE `event: image` emission in `_stream_chat_sse_impl`

**Goal:** When the chat LLM calls `look_at_camera`, write an `event: image` SSE chunk to the response stream BEFORE appending the tool result to LLM history. The chunk carries `{tool_call_id, image_url}` (a base64 data URL).

**Files:**
- Modify: `glados/core/api_wrapper.py` — the `_stream_chat_sse_impl` tool-dispatch block at lines 2902–2944.
- Test: `tests/core/test_api_wrapper_chat_image_sse.py`

**Acceptance Criteria:**
- [ ] When the LLM emits a `look_at_camera` tool call, the response stream contains a chunk of the form `event: image\ndata: {"tool_call_id": "...", "image_url": "data:image/jpeg;base64,..."}\n\n`.
- [ ] When the LLM emits any other tool call (e.g. `search_entities`), no `event: image` chunk is written.
- [ ] The `{"role":"tool", ...}` history entry contains ONLY the JSON `{"description": "..."}` — never the data URL or raw bytes.
- [ ] Errors from `look_at_camera` (camera miss, snapshot fail, VLM fail) write the JSON-encoded `{"error": "..."}` to history and emit NO `event: image` chunk.

**Verify:** `pytest tests/core/test_api_wrapper_chat_image_sse.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test**

The test exercises `_stream_chat_sse_impl` with a fake handler that captures `wfile.write` calls, simulates the upstream Ollama emitting a single `look_at_camera` tool call in round 1 then completing in round 2.

Create `tests/core/test_api_wrapper_chat_image_sse.py`:

```python
"""Tests for the event:image SSE side-channel introduced for look_at_camera."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest


def _captured_chunks(write_buffer: BytesIO) -> list[str]:
    """Split buffer on '\\n\\n' and return non-empty SSE frames."""
    raw = write_buffer.getvalue().decode("utf-8", errors="replace")
    return [f for f in raw.split("\n\n") if f.strip()]


def _frames_with_event(frames: list[str], event_name: str) -> list[str]:
    return [f for f in frames if f.startswith(f"event: {event_name}\n")]


def test_look_at_camera_emits_event_image_and_strips_bytes_from_history():
    from glados.core import api_wrapper

    handler = MagicMock()
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": "0"}

    fake_jpeg = b"\xff\xd8\xff\xe0pic"
    captured_history: list[dict] = []

    def fake_invoke(tool_name, args):
        from glados.core.builtin_tools import ImageEmission
        return (
            json.dumps({"description": "a cat on a porch"}),
            ImageEmission(image_bytes=fake_jpeg, mime="image/jpeg", tool_name="look_at_camera"),
        )

    # Patch the dispatch + an upstream-Ollama mock that emits one tool_call
    # round-1 then a final assistant message round-2.
    with patch.object(api_wrapper, "invoke_image_yielding_tool", side_effect=fake_invoke), \
         patch.object(api_wrapper, "is_image_yielding_tool", return_value=True), \
         patch.object(api_wrapper, "_chat_round_stream",
                      side_effect=api_wrapper._fake_two_round_stream("look_at_camera",
                                                                     {"camera_name": "back yard"})), \
         patch.object(api_wrapper, "_capture_assistant_history", side_effect=captured_history.append):
        api_wrapper._stream_chat_sse_impl(handler, MagicMock(), "what do you see in the back yard?", 30.0)

    frames = _captured_chunks(handler.wfile)
    image_frames = _frames_with_event(frames, "image")
    assert len(image_frames) == 1, f"expected exactly one event:image frame, got: {frames}"
    body = image_frames[0].split("\n", 1)[1].removeprefix("data: ")
    payload = json.loads(body)
    assert payload["image_url"].startswith("data:image/jpeg;base64,")
    assert payload["tool_call_id"]

    # And the LLM history should NEVER carry the data URL
    assert all("data:image/jpeg" not in json.dumps(m) for m in captured_history), \
        "data URL leaked into LLM history"


def test_non_image_tool_emits_no_event_image():
    from glados.core import api_wrapper

    handler = MagicMock()
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": "0"}

    with patch.object(api_wrapper, "is_image_yielding_tool", return_value=False), \
         patch.object(api_wrapper, "_chat_round_stream",
                      side_effect=api_wrapper._fake_two_round_stream("search_entities",
                                                                     {"query": "lights"})):
        api_wrapper._stream_chat_sse_impl(handler, MagicMock(), "find the kitchen lights", 30.0)

    frames = _captured_chunks(handler.wfile)
    assert _frames_with_event(frames, "image") == []


def test_look_at_camera_error_emits_no_event_image():
    from glados.core import api_wrapper

    handler = MagicMock()
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": "0"}

    def fake_invoke(tool_name, args):
        return json.dumps({"error": "no camera matched 'garage'"}), None

    with patch.object(api_wrapper, "invoke_image_yielding_tool", side_effect=fake_invoke), \
         patch.object(api_wrapper, "is_image_yielding_tool", return_value=True), \
         patch.object(api_wrapper, "_chat_round_stream",
                      side_effect=api_wrapper._fake_two_round_stream("look_at_camera",
                                                                     {"camera_name": "garage"})):
        api_wrapper._stream_chat_sse_impl(handler, MagicMock(), "what's in the garage", 30.0)

    frames = _captured_chunks(handler.wfile)
    assert _frames_with_event(frames, "image") == []
```

> **Note for the implementer:** the test patches helpers (`_chat_round_stream`, `_capture_assistant_history`, `_fake_two_round_stream`) that don't exist yet. Their purpose is to let the test stub out the upstream Ollama round trips and inspect emitted frames + history without standing up a real LLM. Add these as test helpers inside `api_wrapper.py` ONLY if extracting them simplifies the test; otherwise refactor the test to monkeypatch `urllib.request.urlopen` directly against fake streamed JSON-lines responses. The acceptance criteria are what matters — the test mechanism is incidental.

If extracting test helpers feels too invasive, simpler alternative: write an integration-shaped test that patches `urllib.request.urlopen` with two fake `read()`-able responses (round 1 with the tool-call delta, round 2 with the final content) — closer to how the real path runs. Pick whichever is cleaner; both are acceptable. Document the choice in the commit message.

- [ ] **Step 2: Add the SSE-image emission helper near the top of `api_wrapper.py`**

Near other SSE-write helpers, add:

```python
def _write_image_sse_event(handler: Any, *, tool_call_id: str, image_bytes: bytes, mime: str) -> None:
    """Write one ``event: image`` SSE frame keyed by tool_call_id.

    The data URL is base64-encoded inline. The frame is ALWAYS written
    BEFORE the ``{"role":"tool"}`` history entry is appended, so the
    WebUI can pair it to the in-progress assistant turn before any
    follow-up text deltas arrive.
    """
    import base64
    payload = json.dumps({
        "tool_call_id": tool_call_id,
        "image_url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}",
    })
    handler.wfile.write(f"event: image\ndata: {payload}\n\n".encode())
```

- [ ] **Step 3: Modify the tool-dispatch block at lines 2902–2944**

In `_stream_chat_sse_impl`, replace the current `is_builtin_tool` branch with image-yielding branch first:

```python
                from glados.core.builtin_tools import (
                    invoke_builtin_tool, is_builtin_tool,
                    invoke_image_yielding_tool, is_image_yielding_tool,
                )
                if is_image_yielding_tool(_tool_name):
                    _log_chat_tool_call.debug(
                        "[{}] tool path: image-yielding builtin", request_id,
                    )
                    _result, _emission = invoke_image_yielding_tool(_tool_name, _tool_args)
                    if _emission is not None:
                        try:
                            _write_image_sse_event(
                                handler,
                                tool_call_id=_tc_id,
                                image_bytes=_emission.image_bytes,
                                mime=_emission.mime,
                            )
                        except Exception as _ee:
                            # Don't fail the chat turn if SSE write hiccups —
                            # log and proceed; the description still reaches
                            # the LLM and the user gets a text answer.
                            logger.warning(
                                "[{}] event:image emission failed: {}",
                                request_id, _ee,
                            )
                elif is_builtin_tool(_tool_name):
                    _log_chat_tool_call.debug(
                        "[{}] tool path: builtin", request_id,
                    )
                    _result = invoke_builtin_tool(_tool_name, _tool_args)
                elif _tool_name.startswith("mcp."):
                    _log_chat_tool_call.debug(
                        "[{}] tool path: mcp", request_id,
                    )
                    _result = glados.mcp_manager.call_tool(_tool_name, _tool_args, timeout=30)
                else:
                    _result = "error: only MCP tools supported in streaming chat"
                    _log_chat_tool_call.warning(
                        "[{}] tool path: unsupported (name={!r})",
                        request_id, _tool_name,
                    )
```

The rest of the block — `_tool_ms`, `_result_text`, the `messages.append({"role": "tool", ...})` line — remains unchanged. The data URL is in the SSE frame; the LLM history sees only the `{"description": "..."}` JSON string.

- [ ] **Step 4: Run the new test + the existing chat-stream regression**

```bash
pytest tests/core/test_api_wrapper_chat_image_sse.py tests/core/ -k chat -v
```

Expected: new tests pass + no chat regressions.

- [ ] **Step 5: Commit**

```bash
git add glados/core/api_wrapper.py tests/core/test_api_wrapper_chat_image_sse.py
git commit -m "feat(chat): event:image SSE channel for look_at_camera tool"
```

---

## Task 7: WebUI — render `event: image` chunks inline

**Goal:** When the chat SSE stream emits an `event: image` chunk, append an `<img>` to the in-progress assistant bubble keyed by `tool_call_id`.

**Files:**
- Modify: `glados/webui/static/ui.js` — chat SSE consumer.

**Acceptance Criteria:**
- [ ] During a streamed chat reply, an `event: image` chunk causes a single `<img class="chat-inline-image">` to render in the assistant's bubble.
- [ ] Multiple chunks with different `tool_call_id`s in one turn render as multiple inline images.
- [ ] The same `tool_call_id` arriving twice (paranoid guard for duplicate frames) renders only once.
- [ ] No image renders if the chunk is malformed (missing `image_url`).
- [ ] Reloading the page does NOT re-render the inline image (the `images: do not persist` MVP rule).

**Verify:** Hand-test with the live system after Task 1 deploy. Open the chat, ask "What do you see in the back yard?" — confirm the snapshot appears inline below the description.

**Steps:**

- [ ] **Step 1: Locate the existing SSE event handler in `ui.js`**

```bash
grep -n "EventSource\|onmessage\|event: attitude\|event: metrics\|addEventListener('message'" glados/webui/static/ui.js | head -10
```

Identify the chat-stream consumer. It will be parsing SSE frames either via `EventSource` (which dispatches by `event:` name automatically) or via a manual fetch + reader loop. The pattern in this repo currently uses fetch + ReadableStream + manual parsing because the chat path includes early POST body. Confirm the parser already special-cases `event: attitude` and `event: metrics` — extending it for `event: image` is a single new branch.

- [ ] **Step 2: Add the inline-image renderer**

Inside the assistant-bubble class (or wherever the streamed bubble's DOM lives), expose a method:

```javascript
// In the assistant-bubble component
appendInlineImage(toolCallId, imageUrl) {
  if (this._inlineImageIds && this._inlineImageIds.has(toolCallId)) return;
  if (!this._inlineImageIds) this._inlineImageIds = new Set();
  this._inlineImageIds.add(toolCallId);
  const img = document.createElement('img');
  img.className = 'chat-inline-image';
  img.src = imageUrl;
  img.alt = 'Camera snapshot';
  img.dataset.toolCallId = toolCallId;
  this.bodyEl.appendChild(img);
}
```

- [ ] **Step 3: Extend the SSE parser to dispatch `event: image`**

In the existing event-name dispatch, add a branch:

```javascript
} else if (eventName === 'image') {
  let payload;
  try { payload = JSON.parse(eventData); } catch (e) { return; }
  if (!payload || typeof payload.image_url !== 'string') return;
  if (currentAssistantBubble) {
    currentAssistantBubble.appendInlineImage(
      payload.tool_call_id || `inline-${Date.now()}`,
      payload.image_url,
    );
  }
}
```

`currentAssistantBubble` is whatever the existing parser uses to track the in-progress assistant bubble for `event: attitude` and text deltas — reuse it.

- [ ] **Step 4: Hand-verify against the deployed container**

Once Task 1 (AIBox stand-up) is complete and the WebUI services tab points at `:11437`:

1. Deploy the container with `python scripts/deploy_ghcr.py`.
2. Open the WebUI chat.
3. Ask: "What do you see in the back yard?" (substitute a camera you have).
4. Expected: text reply describes the scene; inline thumbnail renders below the description, ~320px wide, with rounded corners.

Network tab should show ONE chat-stream POST whose response contains an `event: image` frame. The image bytes do NOT appear in any subsequent request bodies (round-2 should not have them).

- [ ] **Step 5: Commit**

```bash
git add glados/webui/static/ui.js
git commit -m "feat(webui): render event:image SSE chunks as inline thumbnails"
```

---

## Task 8: WebUI inline-image styles

**Goal:** Visual treatment for `.chat-inline-image` matching existing chat-bubble accents.

**Files:**
- Modify: `glados/webui/static/ui.css`

**Acceptance Criteria:**
- [ ] `.chat-inline-image` renders at max 320×240 with rounded 6px corners and 8px vertical margin.
- [ ] Plays well with both the assistant-bubble background and the user-bubble background (test in dark mode if applicable).

**Verify:** Hand-check after Task 7.

**Steps:**

- [ ] **Step 1: Add the CSS rule**

Append near the existing chat-bubble styles:

```css
.chat-inline-image {
  display: block;
  max-width: 320px;
  max-height: 240px;
  width: auto;
  height: auto;
  margin: 8px 0;
  border-radius: 6px;
  border: 1px solid var(--glados-orange, #ff9d00);
  object-fit: cover;
}
```

- [ ] **Step 2: Hand-verify**

Deploy + look. Adjust max-width / border color if needed to match site style.

- [ ] **Step 3: Commit**

```bash
git add glados/webui/static/ui.css
git commit -m "feat(webui): inline chat image styles"
```

---

## Task 9: Cleanup of dead vision-queue code

**Goal:** Remove the lazy-stub `VisionProcessor`, the in-process `VisionRequest`/`VisionState`/`VisionConfig` queue path, the `vision_look` legacy tool, and rewire the call sites that import them.

**Files:**
- Delete: `glados/tools/vision_look.py`
- Delete: `glados/vision/vision_request.py`, `glados/vision/vision_state.py`, `glados/vision/vision_config.py`
- Rewrite: `glados/vision/__init__.py` (drop the lazy stub; re-export the new client only)
- Modify: `glados/tools/__init__.py` (drop `VisionLook` import + registry entries)
- Modify: `glados/core/engine.py` (drop `VisionConfig`/`VisionState`/`VisionProcessor` plumbing; the lazy-stub instantiation at line ~945 was already dead but the surrounding constructor params + thread_configs entry need to go)
- Modify: `glados/core/llm_processor.py` (drop `VisionState` parameter at lines 25, 242; drop the two `vision_look`-name filter rules at lines 857, 872)
- Modify: `glados/autonomy/loop.py` (drop `VisionState` parameter at lines 16, 32)
- Modify: `glados/vision/constants.py` (drop the `vision_look` mention in `SYSTEM_PROMPT_VISION_HANDLING`)
- Test: full suite re-runs cleanly.

**Acceptance Criteria:**
- [ ] Grep for `VisionProcessor`, `VisionRequest`, `VisionState`, `VisionConfig`, `vision_look` returns ZERO results in `glados/` (test files retained only if they cover the new client/cameras modules).
- [ ] Module imports succeed with no `vision/__init__.py` lazy stub: `python -c "import glados.vision"` exits 0 with no warnings.
- [ ] `pytest -q` passes the full ~1157-test suite (no test count regression).
- [ ] Engine starts cleanly (`docker compose up` against the new image — covered by Task 10's smoke).

**Verify:** `pytest -q && python -c "import glados.vision; import glados.engine"`

**Steps:**

- [ ] **Step 1: Survey all consumers (one ripgrep pass)**

```bash
grep -rn "VisionConfig\|VisionRequest\|VisionState\|VisionProcessor\|vision_look\|vision_request\|vision_state" glados/ --include="*.py" | grep -v __pycache__
```

Expected hits before cleanup: ~25 lines across engine.py, llm_processor.py, autonomy/loop.py, tools/__init__.py, vision/__init__.py, vision/constants.py, vision/vision_request.py, vision/vision_state.py, vision/vision_config.py, tools/vision_look.py.

- [ ] **Step 2: Delete the dead modules**

```bash
git rm glados/tools/vision_look.py
git rm glados/vision/vision_request.py
git rm glados/vision/vision_state.py
git rm glados/vision/vision_config.py
```

- [ ] **Step 3: Rewrite `glados/vision/__init__.py`**

```python
"""Vision module — VLM HTTP client only.

The container is pure middleware: vision inference runs upstream at
``cfg.services.llm_vision``. This module's only job is to POST
multimodal payloads there and parse the text response.
"""

from .client import describe_images, VisionClientError

__all__ = ["VisionClientError", "describe_images"]
```

- [ ] **Step 4: Rewire `glados/tools/__init__.py`**

Drop the `vision_look` import and registry entries:

```python
# Remove this line:
from .vision_look import tool_definition as vision_look_def, VisionLook

# Remove from tool_definitions list:
    vision_look_def,

# Remove from tool_classes dict:
    "vision_look": VisionLook,
```

- [ ] **Step 5: Rewire `glados/core/engine.py`**

Remove the `from ..vision import VisionConfig, VisionState` import (line ~36).

Remove the `vision: VisionConfig | None = None` field (line ~256).

Remove the `vision_config: VisionConfig | None = None` constructor param (line ~374) and its docstring blurb (line ~407).

Remove the `self.vision_state: VisionState | None = VisionState() if self.vision_config else None` line (~486).

Remove the dead `from ..vision import VisionProcessor` block at line ~945 entirely (the lazy-stub raised `ImportError`, so this branch was already unreachable).

Remove the `thread_configs["VisionProcessor"]` entry at line ~1039.

Remove any other references to `self.vision_config`, `self.vision_state`, `self.vision_processor` that surface in the diff.

- [ ] **Step 6: Rewire `glados/core/llm_processor.py`**

Drop the `from ..vision.vision_state import VisionState` import (line ~25).

Drop the `vision_state: VisionState | None = None` constructor param (line ~242) and any uses (the parameter was passed-through only; if any method consumed it, the dead branch can be removed).

Remove the two filter rules that strip `vision_look` from the tool list (lines 857, 872) — the tool no longer exists, so the filter is dead.

- [ ] **Step 7: Rewire `glados/autonomy/loop.py`**

Drop the `from ..vision.vision_state import VisionState` import (line ~16) and the `vision_state: VisionState | None` constructor param (line ~32).

Trace through to any caller passing `vision_state=...` and remove the kwarg.

- [ ] **Step 8: Update `glados/vision/constants.py`**

Drop the `- When a user asks for detailed visual inspection or verification, call the \`vision_look\` tool...` line from `SYSTEM_PROMPT_VISION_HANDLING`. The new `look_at_camera` tool's own description (in `builtin_tools.py`) is sufficient guidance — the chat LLM doesn't need a separate system-prompt nudge.

If `SYSTEM_PROMPT_VISION_HANDLING` becomes empty after the line is removed, delete the constant entirely and remove its consumers (grep for callers).

- [ ] **Step 9: Confirm zero lingering references**

```bash
grep -rn "VisionConfig\|VisionRequest\|VisionState\|VisionProcessor\|vision_look\|vision_request\|vision_state" glados/ --include="*.py" | grep -v __pycache__
```

Expected: no results (or only references to the new `glados/cameras/snapshot.py` if you grep too loosely — adjust regex to be exact).

- [ ] **Step 10: Run the full test suite**

```bash
pytest -q
```

Expected: prior count (1157 passed / 5 skipped) ± the test files this slice added. If anything breaks, the failure trace will name the consumer that still needs rewiring — fix and re-run.

- [ ] **Step 11: Commit**

```bash
git add -A glados/ tests/
git commit -m "chore(vision): remove dead in-process VLM queue path + vision_look tool"
```

---

## Task 10: Live-probe smoke + slice closeout

**Goal:** Confirm the deployed container handles a real chat turn against the live `:11437` endpoint and a real HA camera.

**Files:** None (verification only).

**Acceptance Criteria:**
- [ ] Container deploys via `python scripts/deploy_ghcr.py` cleanly; `/health` returns 200 on 8015 + 8052.
- [ ] WebUI chat: "What do you see in the back yard?" returns a text description AND an inline image.
- [ ] Container logs show ONE `look_at_camera` invocation with `tool_path: image-yielding builtin` and a non-error result.
- [ ] No tracebacks in the logs from the camera/discovery, snapshot, or vision_client paths during the probe.
- [ ] Asking about a non-existent camera ("garage" when no garage camera exists) returns a graceful error referencing the actual available list.

**Verify:** Manual operator-witnessed.

**Steps:**

- [ ] **Step 1: Push + deploy**

```bash
git push origin <branch>
# wait for GHA build (or invoke the LAN runner)
python scripts/deploy_ghcr.py
```

- [ ] **Step 2: Probe**

In WebUI chat: ask `"What do you see in the back yard?"`. Capture:

- The text reply.
- Whether the inline image rendered.
- The container log line for `look_at_camera`.

```bash
ssh docker-host "docker logs glados --tail 50 | grep look_at_camera"
```

- [ ] **Step 3: Negative test**

Ask: `"What's in the garage?"` (assuming no garage camera). Expect a chat reply with the form `"I couldn't find a camera matching 'garage'. Available: ..."`.

- [ ] **Step 4: If it works, mark slice done**

Update `docs/CHANGES.md` with a new entry describing Slice 1. No code commit needed for the smoke itself — it's verification.

- [ ] **Step 5: If anything fails, debug per the bug-fix pattern**

Don't speculate — pull logs, re-read the request body that hit the engine, walk the layers in order: WebUI → /v1/chat/completions → tool dispatch → cameras/discovery → cameras/snapshot → vision/client → SSE emission → WebUI render. Fix one layer, re-verify, then look at what's next.

---

## Self-Review Checklist (run after writing implementation code)

Before opening the PR for this slice:

- [ ] Spec coverage: every acceptance criterion in §2 (Feature A flow), §3 (file table for slice-1 modules), and §4 (error pathways A) maps to a task above. ✓ verified during plan-write.
- [ ] No placeholders: every step has actual code or actual commands. ✓
- [ ] Type consistency: `ImageEmission` shape, `_get_camera_discovery` hook name, `is_image_yielding_tool` predicate all match between task 5, task 6, and the test stubs. ✓
- [ ] No reach into Slice 2 / Slice 3 territory: this plan does NOT touch `/api/chat/stream` `images:` field, EventRouter, HAWebSocketHub, audio root, audit `event_rule` source. ✓
- [ ] Cleanup is contained: no rewiring beyond the dead-vision-code surface. The `camera_watcher` dead `:8016` polling remains untouched (out-of-scope follow-up).
- [ ] AIBox stand-up has explicit operator-sign-off step before any NSSM mutation. ✓
- [ ] Audio paths: this slice ships no audio, so no `${GLADOS_AUDIO}` work — confirms the audio-paths memory rule is not at risk here. ✓
