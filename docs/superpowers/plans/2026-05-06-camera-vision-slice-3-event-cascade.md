# Camera Vision — Slice 3: Event-Triggered Vision Cascade

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a configured Home Assistant event fires (e.g. `binary_sensor.front_door_person_detected → on`), the container responds in a layered cascade: an instant pre-recorded stall clip plays on the configured speaker, in parallel a snapshot is fetched + VLM-described, and a stall-aware persona LLM continuation is queued to play after the stall finishes. Operator wires up rules in a new admin-only Integrations → Events tab; pre-recorded clips live under `${GLADOS_AUDIO}/<category>/<emotion>/` per the audio-root convention.

**Architecture:** Three substrate components plus three actions. **HAWebSocketHub** is a new shared singleton owning the HA WebSocket connection — both the existing `ha_sensor_watcher` and the new `EventRouter` register as fan-out consumers (eliminates the second-connection problem). **EventRouter** loads `configs/events.yaml`, matches incoming `state_changed` events against rule patterns, and dispatches to one of three actions: `audio_random` (pure stall-clip play), `llm` (text-only LLM round), or `vision_cascade` (the layered cascade). Per-rule `cooldown_s` + `min_clear_s` and per-camera lockout machinery gate dispatch. New audit `Origin.EVENT_RULE` + `Origin.VISION_TOOL` values surface activity. Two new admin-only WebUI surfaces: the Events tab (CRUD on rules + test-fire) and the Stall Clips tab (upload / preview / delete clips at `${GLADOS_AUDIO}/<category>/<emotion>/`).

**Tech Stack:** Python 3.12, the existing `websockets` async client (already used by `ha_sensor_watcher`), pydantic for `events.yaml` schema, `httpx` for HA service calls, the existing audit pipeline at `glados/observability/audit.py`, the existing media-player play pattern at `ha_sensor_watcher.py:1185`. Reuses Slice 1's `glados/cameras/discovery.py` + `snapshot.py` + `glados/vision/client.py` unchanged.

**Spec:** [`docs/superpowers/specs/2026-05-05-camera-vision-design.md`](glados-container/docs/superpowers/specs/2026-05-05-camera-vision-design.md) — Feature B + the shared event-action subsystem (substrate from `project_event_actions_plan.md`).

---

## Slice Boundaries

**This slice ships independently of Slices 1 and 2 in code but depends on Slice 1's `glados/cameras/*` and `glados/vision/client.py` modules being merged.** It does NOT modify the chat path, the chat-input UI, or anything Slice 2 owns.

**Files this slice owns:**
- `glados/ha_ws/__init__.py`, `glados/ha_ws/hub.py` (new — extracted shared WS singleton)
- `glados/events/__init__.py`, `glados/events/config.py`, `glados/events/router.py` (new)
- `glados/events/actions/__init__.py`, `actions/audio_random.py`, `actions/llm.py`, `actions/vision_cascade.py` (new)
- `glados/observability/audit.py` (modify — add `Origin.EVENT_RULE` + `Origin.VISION_TOOL`)
- `glados/autonomy/agents/ha_sensor_watcher.py` (modify — refactor to consume the hub instead of owning the WS connection)
- `glados/core/engine.py` (modify — wire EventRouter at startup; pass shared hub to ha_sensor_watcher)
- `glados/webui/tts_ui.py` (modify — admin-only `/api/integrations/events` REST + `/api/integrations/stall_clips` upload/list/delete)
- `glados/webui/static/ui.js`, `glados/webui/static/ui.css` (modify — new Events tab + Stall Clips tab)
- `configs/events.yaml` (new — empty starter file with one disabled-by-default example rule)

**Files this slice does NOT own:**
- `glados/cameras/*`, `glados/vision/client.py` — Slice 1 surfaces stay frozen.
- `glados/core/api_wrapper.py` chat-stream path — Slice 1 + Slice 2 only.
- `glados/core/builtin_tools.py` — no new tools in Slice 3.

**Auth/RBAC:**
- `/api/integrations/events` (CRUD on rules) — admin only.
- `/api/integrations/stall_clips` (upload / list / delete) — admin only.
- The Events test-fire button is admin only.
- 401 for unauthenticated callers; 403 for authenticated non-admin callers.

**Audio-root rule:** Stall clips live at `${GLADOS_AUDIO}/<category>/<emotion>/*.{mp3,wav}`. NEVER under `configs/`. The category names mirror `configs/sound_categories.yaml` (which stays config-only — declares categories, doesn't store bytes). Existing `configs/sounds/`-writing call sites elsewhere in the repo (TTS Save-to-library at `tts_ui.py:2334`, `config_store.py:882`) are tracked as a SEPARATE follow-up — NOT bundled into this slice (per spec §3 cleanup note).

**Out of scope:**
- Cleanup of the existing `configs/sounds/` writers — separate follow-up.
- Phase 2 hooks (`participants:`, `objects:`, `plates:` enrichment).
- Cleanup of `glados/autonomy/agents/camera_watcher.py`'s dead `:8016` polling — adjacent debt, separate follow-up.

**Dependencies:** Slice 1 merged + live (`llm_vision` slot at `:11437`, `glados/cameras/*` available, `glados/vision/client.py` available). No deploy work in this slice beyond the deploy at the end.

---

## File Structure

| File | Responsibility |
|---|---|
| `glados/ha_ws/hub.py` | `HAWebSocketHub` — singleton process-wide async WS client to HA. Owns the connection + auth + reconnect-with-backoff loop. Exposes `subscribe(consumer: Callable[[dict], None])` returning a token to unregister; consumers receive raw `state_changed` event dicts. Replaces the in-line WS code currently inside `ha_sensor_watcher`. |
| `glados/events/config.py` | Pydantic models for `events.yaml`: `EventRule`, `EventTrigger`, `EventsConfig`. Loader function `load_events_config(path) -> EventsConfig`. Validates `vlm_camera` exists in HA discovery at load time. |
| `glados/events/router.py` | `EventRouter` — registers as a hub consumer, matches incoming `state_changed` events against `EventsConfig` rules, dispatches to action handlers. Owns the per-rule `cooldown_s` / `min_clear_s` state machines + the per-camera lockout map. |
| `glados/events/actions/audio_random.py` | `AudioRandomAction` — picks a random clip from `${GLADOS_AUDIO}/<category>/<emotion>/`, plays it via HA `media_player.play_media` on the configured speaker. Errors loud (no silent fallback). |
| `glados/events/actions/llm.py` | `LLMAction` — runs a single LLM round through the operator-configured slot, queues result to TTS. The simpler text-only action that the existing `sound_categories.yaml` already sketches. |
| `glados/events/actions/vision_cascade.py` | `VisionCascadeAction` — orchestrates the full cascade: kick stall (audio_random), in parallel fetch snapshot + describe via VLM, then run persona LLM continuation prompt with `{stall_text}` + `{vlm_output}` substitution and queue result to TTS. |
| `glados/observability/audit.py` (modify) | Add `Origin.EVENT_RULE` + `Origin.VISION_TOOL` constants. Both flow into the existing `audit()` queue so the activity-trail viewer has them. |
| `glados/autonomy/agents/ha_sensor_watcher.py` (modify) | Drop the in-line WS connection management (`_ws_thread_entry`, `_ws_main`, `_process_ws_message`); accept a hub reference at init and `hub.subscribe(self._handle_state_change)` in the new lifecycle hook. The state-handling logic stays unchanged — only the transport changes. |
| `glados/core/engine.py` (modify) | Construct `HAWebSocketHub` once at startup; pass it to `ha_sensor_watcher` AND construct `EventRouter` against the same hub. Both lifecycle-managed. |
| `glados/webui/tts_ui.py` (modify) | Add admin-gated `/api/integrations/events` (GET/POST/PUT/DELETE rule CRUD), `/api/integrations/events/test_fire` (POST simulated trigger), `/api/integrations/stall_clips` (GET list, POST upload, DELETE remove). Reuses the existing auth-decorator pattern. |
| `glados/webui/static/ui.js` (modify) | Two new tab views: `IntegrationsEvents` and `IntegrationsStallClips`. Both admin-gated. |
| `glados/webui/static/ui.css` (modify) | Styles for the new tab views — list rows, edit dialog, drop-target for upload. |
| `configs/events.yaml` (new) | Starter file: one example rule with `enabled: false` so nothing fires until the operator wires up real cameras. |

---

## Task 1: Audit Origin extensions

**Goal:** Add `EVENT_RULE` and `VISION_TOOL` to the `Origin` constants so the audit log has the new source tags.

**Files:**
- Modify: `glados/observability/audit.py`
- Test: `tests/observability/test_audit_origins.py`

**Acceptance Criteria:**
- [ ] `Origin.EVENT_RULE == "event_rule"` and `Origin.VISION_TOOL == "vision_tool"`.
- [ ] Both appear in `Origin.ALL`.
- [ ] An `AuditEvent(origin=Origin.EVENT_RULE, ...)` round-trips through the audit pipeline without falling back to `Origin.UNKNOWN`.

**Verify:** `pytest tests/observability/test_audit_origins.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/observability/test_audit_origins.py`:

```python
"""Tests for the new audit Origin values."""

from __future__ import annotations

import pytest

from glados.observability.audit import AuditEvent, Origin


def test_event_rule_origin_constant():
    assert Origin.EVENT_RULE == "event_rule"
    assert Origin.EVENT_RULE in Origin.ALL


def test_vision_tool_origin_constant():
    assert Origin.VISION_TOOL == "vision_tool"
    assert Origin.VISION_TOOL in Origin.ALL


def test_audit_event_does_not_normalize_event_rule():
    ev = AuditEvent(ts=0.0, origin=Origin.EVENT_RULE, kind="tool_call")
    line = ev.to_json_line()
    assert '"origin":"event_rule"' in line
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/observability/test_audit_origins.py -v
```

- [ ] **Step 3: Add the constants**

In `glados/observability/audit.py`, in the `Origin` class around line 40, add:

```python
    EVENT_RULE = "event_rule"       # EventRouter rule fire — Slice 3 spec
    VISION_TOOL = "vision_tool"     # look_at_camera tool dispatch — used by Slice 1
```

And add them to `Origin.ALL`:

```python
    ALL = frozenset({
        WEBUI_CHAT, API_CHAT, VOICE_MIC, TEXT_STDIN,
        AUTONOMY, DISCORD, MQTT_CMD, EVENT_RULE, VISION_TOOL, UNKNOWN,
    })
```

- [ ] **Step 4: Run tests + commit**

```bash
pytest tests/observability/test_audit_origins.py -v
git add glados/observability/audit.py tests/observability/test_audit_origins.py
git commit -m "feat(observability): add event_rule and vision_tool audit origins"
```

---

## Task 2: HAWebSocketHub — extract shared singleton

**Goal:** Pull the HA WebSocket connection management out of `ha_sensor_watcher` into a standalone, multi-consumer hub. Both `ha_sensor_watcher` and the new `EventRouter` register as fan-out consumers of the same connection.

**Files:**
- Create: `glados/ha_ws/__init__.py`, `glados/ha_ws/hub.py`
- Modify: `glados/autonomy/agents/ha_sensor_watcher.py` — replace inline WS with hub subscription.
- Modify: `glados/core/engine.py` — construct hub once; pass to `ha_sensor_watcher`.
- Test: `tests/ha_ws/test_hub.py`

**Acceptance Criteria:**
- [ ] `HAWebSocketHub(ha_ws_url, ha_token)` opens ONE connection, authenticates, subscribes to `state_changed`, and dispatches each event to all registered consumers.
- [ ] `hub.subscribe(callback)` returns an opaque token; `hub.unsubscribe(token)` removes the callback. Subsequent events do not deliver to unsubscribed consumers.
- [ ] If a consumer raises, the hub logs at WARNING and continues delivering to OTHER consumers — one bad consumer does not crash the loop.
- [ ] WS disconnect triggers exponential backoff reconnect (same logic as the current `ha_sensor_watcher` implementation).
- [ ] After refactor, `ha_sensor_watcher` no longer opens its own WS — it gets one through the hub. Existing `ha_sensor_watcher` tests pass with the hub mocked.
- [ ] Engine starts cleanly; `ha_sensor_watcher` continues to deliver state-changed events as before (covered by Task 10's deploy smoke).

**Verify:** `pytest tests/ha_ws/test_hub.py tests/autonomy/test_ha_sensor_watcher.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/ha_ws/__init__.py` (empty) and `tests/ha_ws/test_hub.py`:

```python
"""Unit tests for HAWebSocketHub."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from glados.ha_ws.hub import HAWebSocketHub


def test_subscribe_returns_token_and_token_unsubscribes():
    hub = HAWebSocketHub(ha_ws_url="ws://x", ha_token="t")
    cb = MagicMock()
    token = hub.subscribe(cb)
    assert token in hub._consumers
    hub.unsubscribe(token)
    assert token not in hub._consumers


def test_dispatch_calls_every_consumer():
    hub = HAWebSocketHub(ha_ws_url="ws://x", ha_token="t")
    a = MagicMock()
    b = MagicMock()
    hub.subscribe(a)
    hub.subscribe(b)
    event = {"event_type": "state_changed", "data": {"entity_id": "x"}}
    hub._dispatch_event(event)
    a.assert_called_once_with(event)
    b.assert_called_once_with(event)


def test_consumer_exception_does_not_break_others():
    hub = HAWebSocketHub(ha_ws_url="ws://x", ha_token="t")
    boom = MagicMock(side_effect=RuntimeError("oops"))
    ok = MagicMock()
    hub.subscribe(boom)
    hub.subscribe(ok)
    hub._dispatch_event({"event_type": "state_changed", "data": {}})
    boom.assert_called_once()
    ok.assert_called_once()  # still received the event


@pytest.mark.asyncio
async def test_ws_loop_calls_dispatch_on_each_event():
    """End-to-end: a fake WS yields auth_required → auth_ok → result → events,
    and the hub should call dispatch for each event message."""
    # ... full async test stub: fake `websockets.connect` context manager
    # whose .recv() yields the canned messages; assert dispatch fires per
    # event. Skipped in this skeleton — see implementation in Step 3.
    pass
```

- [ ] **Step 2: Implement `glados/ha_ws/__init__.py` + `hub.py`**

Create `glados/ha_ws/__init__.py`:

```python
"""Shared HA WebSocket hub — one connection, many consumers."""

from .hub import HAWebSocketHub

__all__ = ["HAWebSocketHub"]
```

Create `glados/ha_ws/hub.py` (extract from `ha_sensor_watcher.py:391-481`):

```python
"""Process-wide HA WebSocket singleton.

Owns one persistent WS connection to HA's ``/api/websocket`` endpoint
and fans incoming ``state_changed`` events out to all registered
consumers. Consumers receive raw event dicts (the HA shape) — the hub
does no filtering or interpretation.

Why a hub: ``ha_sensor_watcher`` already maintained a WS connection.
The Slice 3 ``EventRouter`` would otherwise open a SECOND one. Sharing
keeps one TCP connection, one auth handshake, and one reconnect loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any, Callable

from loguru import logger

EventCallback = Callable[[dict[str, Any]], None]


class HAWebSocketHub:
    """One HA WebSocket connection, multiple consumers."""

    def __init__(
        self,
        ha_ws_url: str,
        ha_token: str,
        *,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._ha_ws_url = ha_ws_url
        self._ha_token = ha_token
        self._max_backoff_s = max_backoff_s

        self._consumers: dict[str, EventCallback] = {}
        self._consumers_lock = threading.Lock()

        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = False
        self._consecutive_errors = 0

    # ------------------------- public API -------------------------

    def subscribe(self, callback: EventCallback) -> str:
        """Register a consumer; return an opaque token."""
        token = uuid.uuid4().hex
        with self._consumers_lock:
            self._consumers[token] = callback
        logger.info("HAWebSocketHub: consumer subscribed (total={})", len(self._consumers))
        return token

    def unsubscribe(self, token: str) -> None:
        with self._consumers_lock:
            self._consumers.pop(token, None)

    def start(self) -> None:
        """Spawn the WS daemon thread."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_thread_entry,
            name="HAWebSocketHub",
            daemon=True,
        )
        self._ws_thread.start()
        logger.success("HAWebSocketHub: thread started")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=timeout_s)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------- internals -------------------------

    def _dispatch_event(self, event: dict[str, Any]) -> None:
        """Call every registered consumer; one bad consumer doesn't break others."""
        with self._consumers_lock:
            consumers = list(self._consumers.values())
        for cb in consumers:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001 — broad on purpose
                logger.warning(
                    "HAWebSocketHub: consumer raised {}: {}",
                    type(exc).__name__, exc,
                )

    def _ws_thread_entry(self) -> None:
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        try:
            self._ws_loop.run_until_complete(self._ws_main())
        except Exception as exc:
            logger.error("HAWebSocketHub: WS thread crashed: {}", exc)
        finally:
            self._ws_loop.close()

    async def _ws_main(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            logger.error("HAWebSocketHub: 'websockets' not installed")
            return

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                logger.info("HAWebSocketHub: connecting to {}", self._ha_ws_url)
                async with connect(self._ha_ws_url) as ws:
                    auth_msg = json.loads(await ws.recv())
                    if auth_msg.get("type") != "auth_required":
                        logger.error("HAWebSocketHub: bad first message: {}", auth_msg)
                        await asyncio.sleep(5)
                        continue
                    await ws.send(json.dumps({"type": "auth", "access_token": self._ha_token}))
                    auth_result = json.loads(await ws.recv())
                    if auth_result.get("type") != "auth_ok":
                        logger.error("HAWebSocketHub: auth failed: {}", auth_result)
                        await asyncio.sleep(30)
                        continue
                    logger.success("HAWebSocketHub: authenticated")
                    self._connected = True
                    self._consecutive_errors = 0
                    backoff = 1.0

                    await ws.send(json.dumps({
                        "id": 1, "type": "subscribe_events", "event_type": "state_changed",
                    }))
                    sub = json.loads(await ws.recv())
                    if not sub.get("success"):
                        logger.error("HAWebSocketHub: subscribe failed: {}", sub)
                        await asyncio.sleep(5)
                        continue
                    logger.success("HAWebSocketHub: subscribed state_changed")

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") != "event":
                            continue
                        ev = msg.get("event") or {}
                        if ev.get("event_type") != "state_changed":
                            continue
                        # Dispatch on a worker thread to avoid blocking the WS loop
                        # on slow consumers. Use loop.run_in_executor so exceptions
                        # are caught at dispatch time (see _dispatch_event).
                        await self._ws_loop.run_in_executor(None, self._dispatch_event, ev)

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                self._consecutive_errors += 1
                logger.warning(
                    "HAWebSocketHub: WS lost ({}), reconnecting in {:.0f}s", exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)

        self._connected = False
        logger.info("HAWebSocketHub: WS shutting down")
```

- [ ] **Step 3: Refactor `ha_sensor_watcher.py` to use the hub**

In `glados/autonomy/agents/ha_sensor_watcher.py`:

1. Add a constructor parameter `ha_ws_hub: HAWebSocketHub | None = None`. When non-None, use it; when None, fall back to the existing inline behavior (preserves backward compat for tests).
2. Replace `_ws_thread_entry` + `_ws_main` + parts of `_process_ws_message` with a `_handle_state_change(event_dict)` callback that takes the same shape the hub delivers (`{"event_type": "state_changed", "data": {...}}`).
3. In `start()`, when `ha_ws_hub` is provided, call `self._ws_token = self._ha_ws_hub.subscribe(self._handle_state_change)`.
4. In `stop()`, call `self._ha_ws_hub.unsubscribe(self._ws_token)` if set.
5. Keep the existing inline-WS code paths gated on `self._ha_ws_hub is None` so single-process tests don't break. (Long-term: remove the inline path; for this slice, leaving it preserves the existing test surface.)

Edits are mechanical: extract `_process_ws_message`'s body into a method that takes the event dict directly (it already does — line 488 onwards).

- [ ] **Step 4: Wire the hub into engine startup**

In `glados/core/engine.py`, where `ha_sensor_watcher` is constructed (search for `HomeAssistantSensorSubagent`), construct the hub first:

```python
        # Slice 3 — shared HA WebSocket hub. EventRouter (added below)
        # consumes the same connection.
        from glados.ha_ws import HAWebSocketHub
        self._ha_ws_hub = HAWebSocketHub(
            ha_ws_url=cfg.ha_ws_url,  # adjust to actual cfg field name
            ha_token=cfg.ha_token,
        )
        self._ha_ws_hub.start()
```

Pass `ha_ws_hub=self._ha_ws_hub` to the `HomeAssistantSensorSubagent` constructor. EventRouter binding to the same hub lands in Task 10 (engine wiring); for this task, just construct + start the hub and pass it to ha_sensor_watcher.

In engine shutdown (the `Engine.shutdown` or equivalent method), call `self._ha_ws_hub.stop()` after stopping the subagent.

- [ ] **Step 5: Run tests**

```bash
pytest tests/ha_ws/test_hub.py tests/autonomy/ -k sensor -v
```

Expected: 3+ hub tests pass; ha_sensor_watcher tests still pass with the hub-or-inline branch.

- [ ] **Step 6: Commit**

```bash
git add glados/ha_ws/ tests/ha_ws/
git add glados/autonomy/agents/ha_sensor_watcher.py glados/core/engine.py
git commit -m "feat(ha_ws): extract HAWebSocketHub for multi-consumer fan-out"
```

---

## Task 3: `events.yaml` schema + loader

**Goal:** Pydantic models + a loader for the `configs/events.yaml` rule file. Validation surfaces malformed rules at load time with line-pinpointed errors.

**Files:**
- Create: `glados/events/__init__.py`, `glados/events/config.py`
- Create: `configs/events.yaml` (starter, all rules disabled)
- Test: `tests/events/test_config.py`

**Acceptance Criteria:**
- [ ] `EventRule` accepts the spec's fields: `id, enabled, source, trigger{entity_id, to_state}, action_kind, category, vlm_camera?, llm_continuation_prompt?, cooldown_s, min_clear_s, speaker?`.
- [ ] `action_kind` must be one of `audio_random`, `llm`, `vision_cascade`; anything else is a validation error.
- [ ] `cooldown_s` and `min_clear_s` default to 30.0 and 10.0 respectively per spec §4.
- [ ] `vision_cascade` rules require `vlm_camera`; missing it is a validation error.
- [ ] `load_events_config(path)` returns `EventsConfig(rules=[...])`; missing file returns an empty config (NOT an error — operator may not have set up rules yet).
- [ ] Duplicate `id`s raise a validation error.

**Verify:** `pytest tests/events/test_config.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/events/__init__.py` (empty) and `tests/events/test_config.py`:

```python
"""Tests for events.yaml schema + loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from glados.events.config import (
    EventsConfig,
    EventRule,
    load_events_config,
    EventConfigError,
)


def _write(tmp_path, body: str) -> Path:
    p = tmp_path / "events.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_loader_returns_empty_when_file_missing(tmp_path):
    cfg = load_events_config(tmp_path / "no.yaml")
    assert isinstance(cfg, EventsConfig)
    assert cfg.rules == []


def test_loader_parses_minimum_audio_random_rule(tmp_path):
    p = _write(tmp_path, """
        - id: doorbell_chime
          enabled: true
          source: ha_state
          trigger:
            entity_id: binary_sensor.doorbell
            to_state: 'on'
          action_kind: audio_random
          category: doorbell_chime
    """)
    cfg = load_events_config(p)
    assert len(cfg.rules) == 1
    r = cfg.rules[0]
    assert r.id == "doorbell_chime"
    assert r.enabled is True
    assert r.action_kind == "audio_random"
    assert r.cooldown_s == 30.0  # default
    assert r.min_clear_s == 10.0  # default


def test_vision_cascade_requires_vlm_camera(tmp_path):
    p = _write(tmp_path, """
        - id: front_door_approach
          enabled: true
          source: ha_state
          trigger:
            entity_id: binary_sensor.front_door_person_detected
            to_state: 'on'
          action_kind: vision_cascade
          category: front_door_approach
    """)
    with pytest.raises(EventConfigError) as exc:
        load_events_config(p)
    assert "vlm_camera" in str(exc.value)


def test_invalid_action_kind_rejected(tmp_path):
    p = _write(tmp_path, """
        - id: x
          enabled: true
          source: ha_state
          trigger: {entity_id: binary_sensor.x, to_state: 'on'}
          action_kind: shoot_lasers
          category: x
    """)
    with pytest.raises(EventConfigError):
        load_events_config(p)


def test_duplicate_ids_rejected(tmp_path):
    p = _write(tmp_path, """
        - id: x
          enabled: true
          source: ha_state
          trigger: {entity_id: binary_sensor.x, to_state: 'on'}
          action_kind: audio_random
          category: x
        - id: x
          enabled: true
          source: ha_state
          trigger: {entity_id: binary_sensor.x, to_state: 'on'}
          action_kind: audio_random
          category: x
    """)
    with pytest.raises(EventConfigError) as exc:
        load_events_config(p)
    assert "duplicate" in str(exc.value).lower()
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Implement the schema**

Create `glados/events/__init__.py`:

```python
"""Event-action subsystem — rules, router, actions."""

from .config import EventsConfig, EventRule, load_events_config, EventConfigError

__all__ = ["EventsConfig", "EventRule", "load_events_config", "EventConfigError"]
```

Create `glados/events/config.py`:

```python
"""Pydantic schemas + loader for ``configs/events.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class EventConfigError(ValueError):
    """Surface a malformed events.yaml with the rule-id (when known) inline."""


_ACTION_KINDS = {"audio_random", "llm", "vision_cascade"}


class EventTrigger(BaseModel):
    entity_id: str
    to_state: str


class EventRule(BaseModel):
    id: str
    enabled: bool = True
    source: Literal["ha_state"] = "ha_state"
    trigger: EventTrigger
    action_kind: str
    category: str
    vlm_camera: str | None = None
    llm_continuation_prompt: str | None = None
    cooldown_s: float = 30.0
    min_clear_s: float = 10.0
    speaker: str | None = None  # HA media_player entity_id; defaults to global

    @field_validator("action_kind")
    @classmethod
    def _action_kind_valid(cls, v: str) -> str:
        if v not in _ACTION_KINDS:
            raise ValueError(
                f"action_kind must be one of {sorted(_ACTION_KINDS)}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _vision_cascade_needs_camera(self) -> "EventRule":
        if self.action_kind == "vision_cascade" and not self.vlm_camera:
            raise ValueError(
                f"rule id={self.id!r}: vision_cascade requires vlm_camera"
            )
        return self


class EventsConfig(BaseModel):
    rules: list[EventRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_ids(self) -> "EventsConfig":
        seen: set[str] = set()
        for r in self.rules:
            if r.id in seen:
                raise ValueError(f"duplicate rule id {r.id!r}")
            seen.add(r.id)
        return self


def load_events_config(path: str | Path) -> EventsConfig:
    """Load and validate ``events.yaml``. Missing file → empty config.

    Raises ``EventConfigError`` on parse / validation failure with a
    one-sentence cause inline.
    """
    p = Path(path)
    if not p.exists():
        return EventsConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        raise EventConfigError(f"events.yaml YAML parse failed: {exc}") from exc

    if not isinstance(raw, list):
        raise EventConfigError("events.yaml top-level must be a list of rules")

    try:
        return EventsConfig(rules=raw)
    except ValidationError as exc:
        raise EventConfigError(f"events.yaml validation failed: {exc}") from exc
```

Create starter `configs/events.yaml`:

```yaml
# Event rules — Slice 3 deliverable.
# Each rule subscribes to an HA state-changed transition and dispatches
# an action when matched. See:
#   docs/superpowers/specs/2026-05-05-camera-vision-design.md (Feature B).
#
# Rules ship disabled by default. Operator enables them in WebUI →
# Integrations → Events after wiring up cameras and stall clips.

# Example (disabled until operator configures):
# - id: front_door_approach
#   enabled: false
#   source: ha_state
#   trigger:
#     entity_id: binary_sensor.front_door_person_detected
#     to_state: 'on'
#   action_kind: vision_cascade
#   category: front_door_approach
#   vlm_camera: camera.front_door_high
#   cooldown_s: 60
#   min_clear_s: 30
#   llm_continuation_prompt: |
#     You already said: {stall_text}.
#     The camera shows: {vlm_output}.
#     Add only NEW information not in the stall, in 1-2 short sentences.
```

- [ ] **Step 4: Run tests + commit**

```bash
pytest tests/events/test_config.py -v
git add glados/events/__init__.py glados/events/config.py configs/events.yaml tests/events/__init__.py tests/events/test_config.py
git commit -m "feat(events): events.yaml schema + loader with validation"
```

---

## Task 4: EventRouter — match + dispatch + debounce

**Goal:** A class that consumes the hub, matches incoming `state_changed` events against `EventsConfig` rules, applies cooldown / min_clear / per-camera-lockout, and dispatches to an action handler.

**Files:**
- Create: `glados/events/router.py`
- Test: `tests/events/test_router.py`

**Acceptance Criteria:**
- [ ] A rule with `trigger.entity_id` matching the event's `entity_id` AND `trigger.to_state == new_state.state` matches; otherwise no match.
- [ ] Disabled rules (`enabled: false`) do not match.
- [ ] Once dispatched, a rule does not re-fire within `cooldown_s` seconds (a second matching event is silently ignored).
- [ ] `min_clear_s`: after a rule fires, the trigger entity must spend at least `min_clear_s` consecutive seconds in NOT the trigger state before re-arming. Oscillation flap (off→on→off→on faster than `min_clear_s`) does not trigger a second fire.
- [ ] **Per-camera lockout:** if rule X (with `vlm_camera=camera.front_door_high`) is dispatching, a second incoming match on camera.front_door_high (regardless of which rule) is dropped with a warning log naming both rules.
- [ ] Lockout key is the literal `vlm_camera` string per spec §4.
- [ ] An `audit()` event with `Origin.EVENT_RULE` is logged on every fire, with `tool=<action_kind>` and `params={"rule_id": id, ...}`.

**Verify:** `pytest tests/events/test_router.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/events/test_router.py` covering each acceptance criterion. Use a fake `ActionDispatcher` that records calls instead of running real actions:

```python
"""Tests for EventRouter — rule matching, cooldown, lockout, dispatch."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from glados.events.config import EventsConfig, EventRule, EventTrigger
from glados.events.router import EventRouter


def _make_rule(**overrides) -> EventRule:
    base = dict(
        id="r",
        enabled=True,
        source="ha_state",
        trigger=EventTrigger(entity_id="binary_sensor.x", to_state="on"),
        action_kind="audio_random",
        category="cat",
        cooldown_s=0.1,
        min_clear_s=0.1,
    )
    base.update(overrides)
    return EventRule(**base)


def _state_event(entity_id: str, new_state: str, old_state: str = "off") -> dict:
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old_state},
            "new_state": {"state": new_state},
        },
    }


def test_matching_event_dispatches_action():
    cfg = EventsConfig(rules=[_make_rule()])
    dispatch = MagicMock()
    r = EventRouter(cfg, action_dispatch=dispatch)
    r.handle_state_change(_state_event("binary_sensor.x", "on"))
    dispatch.assert_called_once()
    args, _ = dispatch.call_args
    assert args[0].id == "r"


def test_disabled_rule_does_not_dispatch():
    cfg = EventsConfig(rules=[_make_rule(enabled=False)])
    dispatch = MagicMock()
    r = EventRouter(cfg, action_dispatch=dispatch)
    r.handle_state_change(_state_event("binary_sensor.x", "on"))
    dispatch.assert_not_called()


def test_to_state_mismatch_does_not_dispatch():
    cfg = EventsConfig(rules=[_make_rule()])
    dispatch = MagicMock()
    r = EventRouter(cfg, action_dispatch=dispatch)
    r.handle_state_change(_state_event("binary_sensor.x", "off"))
    dispatch.assert_not_called()


def test_cooldown_blocks_second_fire(monkeypatch):
    cfg = EventsConfig(rules=[_make_rule(cooldown_s=10.0, min_clear_s=0.0)])
    dispatch = MagicMock()
    r = EventRouter(cfg, action_dispatch=dispatch)
    r.handle_state_change(_state_event("binary_sensor.x", "on"))
    r.handle_state_change(_state_event("binary_sensor.x", "on"))  # within cooldown
    assert dispatch.call_count == 1


def test_min_clear_blocks_oscillation_flap():
    cfg = EventsConfig(rules=[_make_rule(cooldown_s=0.0, min_clear_s=10.0)])
    dispatch = MagicMock()
    r = EventRouter(cfg, action_dispatch=dispatch)
    # Initial fire
    r.handle_state_change(_state_event("binary_sensor.x", "on"))
    # Quick off then on — flap
    r.handle_state_change(_state_event("binary_sensor.x", "off", old_state="on"))
    r.handle_state_change(_state_event("binary_sensor.x", "on", old_state="off"))
    assert dispatch.call_count == 1


def test_per_camera_lockout_drops_second_event():
    cfg = EventsConfig(rules=[
        _make_rule(id="r1", action_kind="vision_cascade",
                   vlm_camera="camera.front_door_high",
                   trigger=EventTrigger(entity_id="binary_sensor.a", to_state="on")),
        _make_rule(id="r2", action_kind="vision_cascade",
                   vlm_camera="camera.front_door_high",
                   trigger=EventTrigger(entity_id="binary_sensor.b", to_state="on")),
    ])
    # Make dispatch slow so r1 holds the lockout while r2 arrives
    in_progress = {"r1": False}
    def slow_dispatch(rule, event):
        in_progress[rule.id] = True
        # In real life, action runs in a background thread. Here we just
        # simulate the lockout by NOT releasing until we say so.
    r = EventRouter(cfg, action_dispatch=slow_dispatch)
    # Begin r1
    r.handle_state_change(_state_event("binary_sensor.a", "on"))
    # r1 still holding camera.front_door_high
    r.handle_state_change(_state_event("binary_sensor.b", "on"))
    # Only one rule actually entered dispatch
    assert sum(1 for v in in_progress.values() if v) == 1


def test_audit_event_logged_on_fire():
    from glados.observability.audit import AuditEvent
    cfg = EventsConfig(rules=[_make_rule()])
    dispatch = MagicMock()
    audit_log = []
    r = EventRouter(cfg, action_dispatch=dispatch, audit_hook=audit_log.append)
    r.handle_state_change(_state_event("binary_sensor.x", "on"))
    assert len(audit_log) == 1
    ev: AuditEvent = audit_log[0]
    assert ev.origin == "event_rule"
    assert ev.tool == "audio_random"
    assert ev.params["rule_id"] == "r"
```

- [ ] **Step 2: Implement `glados/events/router.py`**

```python
"""EventRouter — match + dispatch + debounce."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from loguru import logger

from glados.events.config import EventRule, EventsConfig
from glados.observability.audit import AuditEvent, Origin, audit


ActionDispatch = Callable[[EventRule, dict[str, Any]], None]
AuditHook = Callable[[AuditEvent], None]


class EventRouter:
    """Consumes raw state_changed events; dispatches matched rules.

    Caller registers ``handle_state_change`` as a hub consumer.
    Dispatch runs synchronously in the calling thread — actions
    that do real work (audio, VLM, LLM) MUST hand off to a worker
    thread or async pool inside their handler. The router only owns
    rule-match + debounce state.
    """

    def __init__(
        self,
        config: EventsConfig,
        *,
        action_dispatch: ActionDispatch,
        audit_hook: AuditHook | None = None,
    ) -> None:
        self._config = config
        self._dispatch = action_dispatch
        self._audit_hook = audit_hook or audit
        self._lock = threading.Lock()
        # Per-rule state
        self._last_fire_at: dict[str, float] = {}     # cooldown
        self._last_clear_at: dict[str, float] = {}    # min_clear
        # Per-camera lockout
        self._camera_in_use: dict[str, str] = {}      # camera_entity_id -> rule_id

    def update_config(self, config: EventsConfig) -> None:
        """Hot-swap the rule set. Locks.

        State (cooldowns, lockouts) is preserved for rules that still exist;
        removed rules are forgotten."""
        with self._lock:
            self._config = config
            keep = {r.id for r in config.rules}
            self._last_fire_at = {k: v for k, v in self._last_fire_at.items() if k in keep}
            self._last_clear_at = {k: v for k, v in self._last_clear_at.items() if k in keep}

    # -------------------- main entry --------------------

    def handle_state_change(self, event: dict[str, Any]) -> None:
        """Hub callback. ``event`` shape: {event_type, data: {entity_id, old_state, new_state}}."""
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        new_state = (data.get("new_state") or {}).get("state")
        old_state = (data.get("old_state") or {}).get("state")
        if not entity_id or new_state is None:
            return

        now = time.time()
        with self._lock:
            for rule in self._config.rules:
                if not rule.enabled:
                    continue
                if rule.trigger.entity_id != entity_id:
                    # Track clear-time on transitions away from trigger state
                    # for rules whose trigger entity matches but state doesn't.
                    continue
                if rule.trigger.to_state != new_state:
                    # Entity transitioned to non-trigger state — record clear time
                    self._last_clear_at[rule.id] = now
                    continue

                # Cooldown gate
                last_fire = self._last_fire_at.get(rule.id, 0.0)
                if (now - last_fire) < rule.cooldown_s:
                    logger.debug(
                        "EventRouter: rule {} cooldown ({:.1f}s remaining)",
                        rule.id, rule.cooldown_s - (now - last_fire),
                    )
                    continue

                # min_clear gate — only applies if we fired before
                if last_fire > 0:
                    last_clear = self._last_clear_at.get(rule.id, 0.0)
                    if last_clear < last_fire:
                        # Never went clear since last fire — flap
                        logger.debug(
                            "EventRouter: rule {} min_clear: never went clear since last fire",
                            rule.id,
                        )
                        continue
                    if (now - last_clear) < rule.min_clear_s:
                        logger.debug(
                            "EventRouter: rule {} min_clear: cleared {:.1f}s ago, need {}",
                            rule.id, now - last_clear, rule.min_clear_s,
                        )
                        continue

                # Per-camera lockout
                if rule.vlm_camera:
                    holder = self._camera_in_use.get(rule.vlm_camera)
                    if holder and holder != rule.id:
                        logger.warning(
                            "EventRouter: rule {} dropped — camera {} locked by rule {}",
                            rule.id, rule.vlm_camera, holder,
                        )
                        continue
                    self._camera_in_use[rule.vlm_camera] = rule.id

                self._last_fire_at[rule.id] = now

                # Audit BEFORE dispatch so a hung action still leaves a trail
                self._audit_hook(AuditEvent(
                    ts=now,
                    origin=Origin.EVENT_RULE,
                    kind="tool_call",
                    tool=rule.action_kind,
                    params={"rule_id": rule.id, "entity_id": entity_id, "category": rule.category},
                    entity_ids=[entity_id] + ([rule.vlm_camera] if rule.vlm_camera else []),
                ))

                # Dispatch — under lock is fine because action_dispatch should
                # hand off to a worker thread immediately. (See action handlers.)
                try:
                    self._dispatch(rule, event)
                except Exception as exc:
                    logger.error("EventRouter: rule {} dispatch raised {}", rule.id, exc)
                    if rule.vlm_camera:
                        self._camera_in_use.pop(rule.vlm_camera, None)

    def release_camera(self, vlm_camera: str, rule_id: str) -> None:
        """Action handlers call this when their cascade finishes (success
        or failure). Releases the per-camera lockout key."""
        with self._lock:
            holder = self._camera_in_use.get(vlm_camera)
            if holder == rule_id:
                self._camera_in_use.pop(vlm_camera, None)
```

- [ ] **Step 3: Run tests + commit**

```bash
pytest tests/events/test_router.py -v
git add glados/events/router.py tests/events/test_router.py
git commit -m "feat(events): EventRouter with cooldown + min_clear + per-camera lockout"
```

---

## Task 5: `audio_random` action — pre-recorded clip play

**Goal:** Pick a random clip from `${GLADOS_AUDIO}/<category>/<emotion>/` and play it on the configured HA media_player. Returns the chosen clip's text content (operator-curated stall text) so the cascade can pass it to the persona LLM.

**Files:**
- Create: `glados/events/actions/__init__.py`, `glados/events/actions/audio_random.py`
- Test: `tests/events/test_action_audio_random.py`

**Acceptance Criteria:**
- [ ] `play_random(category, emotion="neutral", speaker=...)` picks one file from `${GLADOS_AUDIO}/<category>/<emotion>/*.{mp3,wav}` uniformly at random.
- [ ] If `<emotion>` directory is empty, falls through to `${GLADOS_AUDIO}/<category>/neutral/`. If THAT is empty, raises `AudioRandomError` with the path.
- [ ] If `${GLADOS_AUDIO}/<category>/` doesn't exist at all, raises `AudioRandomError` with the path.
- [ ] Posts to HA `/api/services/media_player/play_media` with `entity_id=speaker` and a media URL pointing at the file (reuses the existing `_play_maintenance_wav` pattern).
- [ ] Returns the played file's stem (e.g. `"someone_at_door"` → operator can use as `{stall_text}` substitution).
- [ ] No silent fallback — every failure raises with cause.

**Verify:** `pytest tests/events/test_action_audio_random.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/events/test_action_audio_random.py`:

```python
"""Tests for the audio_random action — pre-recorded clip play."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from glados.events.actions.audio_random import play_random, AudioRandomError


def _make_audio_root(tmp_path):
    root = tmp_path / "audio"
    (root / "front_door_approach" / "neutral").mkdir(parents=True)
    (root / "front_door_approach" / "neutral" / "someone_arrived.mp3").write_bytes(b"id3")
    (root / "front_door_approach" / "neutral" / "you_have_a_visitor.mp3").write_bytes(b"id3")
    return root


def test_picks_a_clip_and_posts_to_ha(tmp_path, monkeypatch):
    root = _make_audio_root(tmp_path)
    monkeypatch.setenv("GLADOS_AUDIO", str(root))
    with patch("glados.events.actions.audio_random._play_via_ha") as p:
        text = play_random(
            category="front_door_approach",
            emotion="neutral",
            speaker="media_player.kitchen",
            ha_url="http://ha:8123",
            ha_token="t",
        )
    assert text in {"someone_arrived", "you_have_a_visitor"}
    p.assert_called_once()


def test_falls_back_to_neutral_when_emotion_empty(tmp_path, monkeypatch):
    root = _make_audio_root(tmp_path)
    (root / "front_door_approach" / "amused").mkdir()  # empty
    monkeypatch.setenv("GLADOS_AUDIO", str(root))
    with patch("glados.events.actions.audio_random._play_via_ha"):
        text = play_random(
            category="front_door_approach",
            emotion="amused",  # empty dir
            speaker="media_player.kitchen",
            ha_url="http://ha:8123",
            ha_token="t",
        )
    assert text in {"someone_arrived", "you_have_a_visitor"}


def test_missing_category_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GLADOS_AUDIO", str(tmp_path))
    with pytest.raises(AudioRandomError) as exc:
        play_random(
            category="nope",
            speaker="media_player.kitchen",
            ha_url="http://ha:8123",
            ha_token="t",
        )
    assert "nope" in str(exc.value)


def test_empty_neutral_dir_raises(tmp_path, monkeypatch):
    root = tmp_path / "audio"
    (root / "doorbell" / "neutral").mkdir(parents=True)
    monkeypatch.setenv("GLADOS_AUDIO", str(root))
    with pytest.raises(AudioRandomError) as exc:
        play_random(
            category="doorbell",
            speaker="media_player.kitchen",
            ha_url="http://ha:8123",
            ha_token="t",
        )
    assert "no stall clips" in str(exc.value).lower()
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Implement**

Create `glados/events/actions/__init__.py`:

```python
"""Event-action handlers."""
```

Create `glados/events/actions/audio_random.py`:

```python
"""Random pre-recorded clip play.

Clips live at ``${GLADOS_AUDIO}/<category>/<emotion>/*.{mp3,wav}`` per
the audio-root convention (NEVER under ``configs/``).
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import httpx
from loguru import logger


class AudioRandomError(RuntimeError):
    """Clip selection or HA play_media failed."""


def _audio_root() -> Path:
    return Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))


def _list_clips(category: str, emotion: str) -> list[Path]:
    cat_dir = _audio_root() / category
    if not cat_dir.is_dir():
        raise AudioRandomError(f"audio category not found: {cat_dir}")
    emo_dir = cat_dir / emotion
    clips: list[Path] = []
    if emo_dir.is_dir():
        clips = [p for p in emo_dir.iterdir() if p.suffix.lower() in {".mp3", ".wav"}]
    return clips


def play_random(
    *,
    category: str,
    speaker: str,
    ha_url: str,
    ha_token: str,
    emotion: str = "neutral",
    serve_host: str | None = None,
    serve_port: int = 8052,
) -> str:
    """Pick a random clip + POST to HA media_player.play_media.

    Returns the chosen clip's stem (filename without extension), which
    callers can use as ``{stall_text}`` for downstream substitution
    (operator names files semantically: ``someone_arrived.mp3`` →
    ``"someone_arrived"``).
    """
    clips = _list_clips(category, emotion)
    if not clips and emotion != "neutral":
        logger.info(
            "AudioRandom: emotion {} empty for {}, falling back to neutral",
            emotion, category,
        )
        clips = _list_clips(category, "neutral")
    if not clips:
        raise AudioRandomError(
            f"no stall clips in {_audio_root()}/{category}/{emotion}/ "
            f"(or fallback ./neutral/)"
        )

    chosen = random.choice(clips)
    _play_via_ha(chosen, speaker=speaker, ha_url=ha_url, ha_token=ha_token,
                 serve_host=serve_host, serve_port=serve_port)
    return chosen.stem


def _play_via_ha(
    clip: Path,
    *,
    speaker: str,
    ha_url: str,
    ha_token: str,
    serve_host: str | None,
    serve_port: int,
) -> None:
    """Post HA's media_player/play_media for the chosen clip.

    Replicates the pattern in ha_sensor_watcher._play_maintenance_wav
    — copy clip into the HA-serve directory, build the media URL, POST.
    """
    import shutil
    serve_dir = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files")) / "glados_ha"
    serve_dir.mkdir(parents=True, exist_ok=True)
    dest = serve_dir / clip.name
    shutil.copy2(clip, dest)

    from glados.core.tls import is_tls_active
    proto = "https" if is_tls_active() else "http"
    host = serve_host or os.environ.get("GLADOS_SERVE_HOST", "glados.local")
    media_url = f"{proto}://{host}:{serve_port}/{clip.name}"

    try:
        httpx.post(
            f"{ha_url.rstrip('/')}/api/services/media_player/play_media",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
            json={
                "entity_id": [speaker],
                "media_content_id": media_url,
                "media_content_type": "music",
            },
            timeout=10.0,
        )
        logger.success("AudioRandom: played {} on {}", clip.name, speaker)
    except Exception as exc:
        raise AudioRandomError(f"HA play_media failed: {exc}") from exc
```

- [ ] **Step 4: Commit**

```bash
pytest tests/events/test_action_audio_random.py -v
git add glados/events/actions/__init__.py glados/events/actions/audio_random.py tests/events/test_action_audio_random.py
git commit -m "feat(events): audio_random action plays clips via HA media_player"
```

---

## Task 6: `llm` action — text-only LLM round

**Goal:** Run a single LLM round through the operator-configured slot, queue the result to TTS. Used for "describe-the-event" rules that don't need vision.

**Files:**
- Create: `glados/events/actions/llm.py`
- Test: `tests/events/test_action_llm.py`

**Acceptance Criteria:**
- [ ] `run_llm_action(rule, event, *, tts_queue)` calls the LLM at the operator-configured slot (default `llm_interactive`) with `rule.llm_continuation_prompt` as the system prompt and the event metadata as the user message.
- [ ] LLM result is enqueued to `tts_queue` for playback through the existing TTS path.
- [ ] LLM failure logs WARNING; no fallback text plays (per the no-silent-fallback rule).

**Verify:** `pytest tests/events/test_action_llm.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/events/test_action_llm.py`:

```python
"""Tests for the llm action."""

from __future__ import annotations

import queue
from unittest.mock import patch, MagicMock

import pytest

from glados.events.actions.llm import run_llm_action
from glados.events.config import EventRule, EventTrigger


def _rule() -> EventRule:
    return EventRule(
        id="r", enabled=True, source="ha_state",
        trigger=EventTrigger(entity_id="binary_sensor.x", to_state="on"),
        action_kind="llm", category="x",
        llm_continuation_prompt="Describe in 1 sentence: {event}",
    )


def test_llm_result_goes_to_tts_queue():
    q: queue.Queue[str] = queue.Queue()
    with patch("glados.events.actions.llm.llm_call", return_value="someone arrived"):
        run_llm_action(_rule(), {"data": {"entity_id": "binary_sensor.x"}}, tts_queue=q)
    assert q.get_nowait() == "someone arrived"


def test_llm_failure_does_not_enqueue():
    q: queue.Queue[str] = queue.Queue()
    with patch("glados.events.actions.llm.llm_call", return_value=None):
        run_llm_action(_rule(), {"data": {"entity_id": "binary_sensor.x"}}, tts_queue=q)
    assert q.empty()
```

- [ ] **Step 2: Implement**

Create `glados/events/actions/llm.py`:

```python
"""Text-only LLM round for an event rule."""

from __future__ import annotations

import json
import queue as _queue
from typing import Any

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call
from glados.events.config import EventRule


def run_llm_action(
    rule: EventRule,
    event: dict[str, Any],
    *,
    tts_queue: "_queue.Queue[str]",
    slot: str = "llm_interactive",
) -> None:
    """Run a single-shot LLM call and enqueue the result to TTS."""
    config = LLMConfig.for_slot(slot, timeout=20.0)
    system = rule.llm_continuation_prompt or "Describe the event in one short sentence."
    user = f"event: {json.dumps(event.get('data', {}))}"

    out = llm_call(config, system, user, max_tokens=128)
    if not out:
        logger.warning("EventLLMAction: rule {} got empty/null LLM response", rule.id)
        return
    tts_queue.put(out.strip())
```

- [ ] **Step 3: Test + commit**

```bash
pytest tests/events/test_action_llm.py -v
git add glados/events/actions/llm.py tests/events/test_action_llm.py
git commit -m "feat(events): llm action — single LLM round to TTS"
```

---

## Task 7: `vision_cascade` action — the layered cascade

**Goal:** Implement the full cascade: kick stall (audio_random) instantly, in parallel fetch snapshot + describe via VLM, then run persona LLM continuation with stall-aware prompt and queue the result to TTS. Releases the per-camera lockout when done.

**Files:**
- Create: `glados/events/actions/vision_cascade.py`
- Test: `tests/events/test_action_vision_cascade.py`

**Acceptance Criteria:**
- [ ] On dispatch, `play_random(category=rule.category, ...)` is called first (within ~10 ms — instant).
- [ ] In parallel: `fetch_snapshot(rule.vlm_camera)` AND `describe_images([snapshot], ...)` chain runs.
- [ ] Once VLM returns, an LLM continuation runs with `rule.llm_continuation_prompt.format(stall_text=..., vlm_output=...)` substitution.
- [ ] LLM continuation result enqueues to TTS.
- [ ] After all stages complete (success OR failure), `router.release_camera(rule.vlm_camera, rule.id)` is called.
- [ ] Snapshot fetch failure: stall already played; LLM continuation receives `vlm_output="(scene description failed: <reason>)"` so it can apologize gracefully.
- [ ] VLM failure: same as above.
- [ ] LLM failure: stall already played; nothing else queues; WARNING log; lockout released.

**Verify:** `pytest tests/events/test_action_vision_cascade.py -v`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/events/test_action_vision_cascade.py`:

```python
"""Tests for vision_cascade action."""

from __future__ import annotations

import queue
import time
from unittest.mock import patch, MagicMock

import pytest

from glados.events.actions.vision_cascade import run_vision_cascade
from glados.events.config import EventRule, EventTrigger
from glados.cameras.snapshot import CameraSnapshotError
from glados.vision.client import VisionClientError


def _rule() -> EventRule:
    return EventRule(
        id="front_door_approach",
        enabled=True,
        source="ha_state",
        trigger=EventTrigger(entity_id="binary_sensor.front_door_person_detected", to_state="on"),
        action_kind="vision_cascade",
        category="front_door_approach",
        vlm_camera="camera.front_door_high",
        llm_continuation_prompt=(
            "You said: {stall_text}. Camera shows: {vlm_output}. "
            "Add only NEW info in 1 sentence."
        ),
    )


def test_happy_path_stall_then_continuation():
    q: queue.Queue[str] = queue.Queue()
    release = MagicMock()
    with patch("glados.events.actions.vision_cascade.play_random",
               return_value="someone_arrived") as stall, \
         patch("glados.events.actions.vision_cascade.fetch_snapshot",
               return_value=b"\xff\xd8jpg"), \
         patch("glados.events.actions.vision_cascade.describe_images",
               return_value="an adult holding a parcel"), \
         patch("glados.events.actions.vision_cascade.llm_call",
               return_value="they're carrying a brown box"):
        run_vision_cascade(
            _rule(), {}, tts_queue=q,
            ha_url="http://ha:8123", ha_token="t", speaker="media_player.kitchen",
            release_camera=release,
            wait_for_completion=True,  # synchronous mode for testing
        )
    stall.assert_called_once()
    out = q.get_nowait()
    assert "brown box" in out
    release.assert_called_once_with("camera.front_door_high", "front_door_approach")


def test_snapshot_failure_continuation_apologizes():
    q: queue.Queue[str] = queue.Queue()
    release = MagicMock()
    with patch("glados.events.actions.vision_cascade.play_random", return_value="someone_arrived"), \
         patch("glados.events.actions.vision_cascade.fetch_snapshot",
               side_effect=CameraSnapshotError("404")), \
         patch("glados.events.actions.vision_cascade.describe_images") as desc, \
         patch("glados.events.actions.vision_cascade.llm_call",
               return_value="couldn't see anything") as llm:
        run_vision_cascade(
            _rule(), {}, tts_queue=q,
            ha_url="http://ha:8123", ha_token="t", speaker="media_player.kitchen",
            release_camera=release,
            wait_for_completion=True,
        )
    desc.assert_not_called()
    # LLM saw the failure context
    sys_arg = llm.call_args.args[1]
    assert "scene description failed" in sys_arg or "scene description failed" in llm.call_args.args[2]
    release.assert_called_once()


def test_lockout_released_on_full_failure():
    q: queue.Queue[str] = queue.Queue()
    release = MagicMock()
    with patch("glados.events.actions.vision_cascade.play_random",
               side_effect=Exception("boom")), \
         patch("glados.events.actions.vision_cascade.fetch_snapshot",
               side_effect=CameraSnapshotError("x")):
        try:
            run_vision_cascade(
                _rule(), {}, tts_queue=q,
                ha_url="http://ha:8123", ha_token="t", speaker="media_player.kitchen",
                release_camera=release,
                wait_for_completion=True,
            )
        except Exception:
            pass
    release.assert_called_once_with("camera.front_door_high", "front_door_approach")
```

- [ ] **Step 2: Implement**

Create `glados/events/actions/vision_cascade.py`:

```python
"""vision_cascade — instant stall + parallel snapshot+VLM + persona continuation.

Cascade timeline (approximate):
    t=0     : stall clip starts (audio_random)
    t=0     : snapshot fetch starts in parallel
    t≈800ms : snapshot bytes back; VLM call starts
    t≈3s    : VLM description back; LLM continuation starts
    t≈4s    : LLM result queued to TTS (plays after stall finishes)
"""

from __future__ import annotations

import queue as _queue
import threading
from typing import Any, Callable

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call
from glados.cameras.snapshot import fetch_snapshot, CameraSnapshotError
from glados.events.actions.audio_random import play_random, AudioRandomError
from glados.events.config import EventRule
from glados.vision.client import describe_images, VisionClientError


ReleaseCameraFn = Callable[[str, str], None]


def run_vision_cascade(
    rule: EventRule,
    event: dict[str, Any],
    *,
    tts_queue: "_queue.Queue[str]",
    ha_url: str,
    ha_token: str,
    speaker: str,
    release_camera: ReleaseCameraFn,
    wait_for_completion: bool = False,
) -> None:
    """Kick the cascade.

    By default this returns immediately and runs the cascade on a daemon
    thread. ``wait_for_completion=True`` is for tests — runs synchronously
    so assertions can inspect the post-completion state.
    """
    runner = lambda: _run_cascade_inner(
        rule, event, tts_queue=tts_queue,
        ha_url=ha_url, ha_token=ha_token, speaker=speaker,
        release_camera=release_camera,
    )
    if wait_for_completion:
        runner()
    else:
        threading.Thread(target=runner, name=f"vc-{rule.id}", daemon=True).start()


def _run_cascade_inner(
    rule: EventRule,
    event: dict[str, Any],
    *,
    tts_queue: "_queue.Queue[str]",
    ha_url: str,
    ha_token: str,
    speaker: str,
    release_camera: ReleaseCameraFn,
) -> None:
    stall_text = "(stall failed)"
    vlm_output: str | None = None
    snapshot_error: str | None = None

    try:
        try:
            stall_text = play_random(
                category=rule.category,
                speaker=rule.speaker or speaker,
                ha_url=ha_url, ha_token=ha_token,
            )
        except (AudioRandomError, Exception) as exc:
            logger.warning("vision_cascade {} stall failed: {}", rule.id, exc)

        # Snapshot + VLM (sequential — VLM needs the bytes)
        if rule.vlm_camera:
            try:
                snap = fetch_snapshot(rule.vlm_camera, ha_url=ha_url, ha_token=ha_token)
            except CameraSnapshotError as exc:
                snapshot_error = str(exc)
                snap = None
                logger.warning("vision_cascade {} snapshot failed: {}", rule.id, exc)

            if snap:
                try:
                    vlm_output = describe_images(
                        [snap], "Describe what is happening in 1-2 sentences.",
                    )
                except VisionClientError as exc:
                    snapshot_error = str(exc)
                    logger.warning("vision_cascade {} vlm failed: {}", rule.id, exc)

        # LLM continuation
        if rule.llm_continuation_prompt:
            vlm_text_for_prompt = (
                vlm_output if vlm_output is not None
                else f"(scene description failed: {snapshot_error or 'unknown'})"
            )
            try:
                system = rule.llm_continuation_prompt.format(
                    stall_text=stall_text, vlm_output=vlm_text_for_prompt,
                )
                config = LLMConfig.for_slot("llm_interactive", timeout=20.0)
                continuation = llm_call(
                    config, system,
                    f"event: rule={rule.id} category={rule.category}",
                    max_tokens=128,
                )
                if continuation:
                    tts_queue.put(continuation.strip())
                else:
                    logger.warning(
                        "vision_cascade {} continuation: empty LLM response", rule.id,
                    )
            except Exception as exc:
                logger.warning(
                    "vision_cascade {} continuation failed: {}", rule.id, exc,
                )
    finally:
        if rule.vlm_camera:
            try:
                release_camera(rule.vlm_camera, rule.id)
            except Exception as exc:
                logger.error(
                    "vision_cascade {} release_camera raised {}", rule.id, exc,
                )
```

- [ ] **Step 3: Test + commit**

```bash
pytest tests/events/test_action_vision_cascade.py -v
git add glados/events/actions/vision_cascade.py tests/events/test_action_vision_cascade.py
git commit -m "feat(events): vision_cascade action — stall + snapshot+VLM + LLM continuation"
```

---

## Task 8: WebUI — Integrations → Events tab (admin-only)

**Goal:** Admin-only tab listing rules from `events.yaml`, with add/edit/delete + a test-fire button per rule.

**Files:**
- Modify: `glados/webui/tts_ui.py` — new admin-gated REST endpoints `/api/integrations/events` (GET/POST/PUT/DELETE) + `/api/integrations/events/<id>/test_fire` (POST).
- Modify: `glados/webui/static/ui.js` — new `IntegrationsEventsTab` view.
- Modify: `glados/webui/static/ui.css` — list-row + edit-dialog styles.
- Test: `tests/webui/test_integrations_events_api.py`

**Acceptance Criteria:**
- [ ] GET `/api/integrations/events` returns the current `events.yaml` rule list as JSON. Admin-only — non-admin gets 403.
- [ ] POST `/api/integrations/events` with a new rule creates it; persists to disk via the existing config-store save path; new rule loads in `EventRouter` (hot-swap via `EventRouter.update_config`).
- [ ] PUT `/api/integrations/events/<id>` updates an existing rule.
- [ ] DELETE `/api/integrations/events/<id>` removes the rule.
- [ ] POST `/api/integrations/events/<id>/test_fire` simulates a `state_changed` event matching the rule's trigger and dispatches it through the router (so test-fires honor cooldown/lockout — operator can verify clip + LLM end-to-end).
- [ ] Validation errors surface as 400 with the cause from `EventConfigError`.
- [ ] Tab UI: list of rules with id, action_kind, enabled-toggle, edit/delete/test-fire buttons. Edit dialog shows all rule fields with inline help.

**Verify:** `pytest tests/webui/test_integrations_events_api.py -v` plus a hand-test of the tab.

**Steps:**

- [ ] **Step 1: Write the failing API tests**

Create `tests/webui/test_integrations_events_api.py` covering each endpoint with admin and non-admin auth fixtures. Reuse the existing auth-test patterns (grep `tests/webui/` for an existing admin-only endpoint test for the pattern):

```python
"""Tests for /api/integrations/events admin REST."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def test_get_events_admin_returns_rules(admin_client, sample_events_yaml):
    resp = admin_client.get("/api/integrations/events")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["rules"], list)


def test_get_events_non_admin_forbidden(viewer_client):
    resp = viewer_client.get("/api/integrations/events")
    assert resp.status_code == 403


def test_post_creates_rule(admin_client):
    new_rule = {
        "id": "test_rule",
        "enabled": True,
        "source": "ha_state",
        "trigger": {"entity_id": "binary_sensor.x", "to_state": "on"},
        "action_kind": "audio_random",
        "category": "doorbell_chime",
    }
    resp = admin_client.post("/api/integrations/events", json=new_rule)
    assert resp.status_code == 201
    listing = admin_client.get("/api/integrations/events").json()
    assert any(r["id"] == "test_rule" for r in listing["rules"])


def test_post_invalid_returns_400(admin_client):
    resp = admin_client.post("/api/integrations/events", json={"id": "bad", "action_kind": "shoot_lasers"})
    assert resp.status_code == 400
    assert "action_kind" in resp.json()["error"]


def test_test_fire_dispatches_to_router(admin_client, mock_event_router):
    admin_client.post("/api/integrations/events/test_rule/test_fire")
    mock_event_router.handle_state_change.assert_called_once()
```

(Adjust the fixture names — `admin_client`, `viewer_client`, `mock_event_router`, `sample_events_yaml` — to match the conventions used in this repo's existing webui tests. Run `ls tests/webui/` and read one existing test file to learn the pattern.)

- [ ] **Step 2: Implement the REST endpoints in `tts_ui.py`**

Find the existing admin-gated route registration pattern and add `/api/integrations/events` GET/POST/PUT/DELETE + `/api/integrations/events/<id>/test_fire` POST. Each endpoint:

1. Calls the existing admin-auth decorator (grep `tts_ui.py` for `@require_admin` or equivalent).
2. Reads/writes `configs/events.yaml` via the loader.
3. After write, calls `event_router.update_config(...)` to hot-swap.

Pseudo-pattern (adjust to actual auth decorator + handler shape):

```python
@require_admin  # whatever the actual decorator is
def get_integrations_events(handler):
    cfg = load_events_config(_events_yaml_path())
    handler._send_json({"rules": [r.model_dump(exclude_none=True) for r in cfg.rules]}, 200)

@require_admin
def post_integrations_events(handler, body):
    try:
        rule = EventRule.model_validate(body)
    except ValidationError as exc:
        handler._send_json({"error": str(exc)}, 400)
        return
    cfg = load_events_config(_events_yaml_path())
    if any(r.id == rule.id for r in cfg.rules):
        handler._send_json({"error": f"id {rule.id!r} already exists"}, 409)
        return
    cfg.rules.append(rule)
    _save_events_yaml(cfg)
    _engine.event_router.update_config(cfg)
    handler._send_json(rule.model_dump(exclude_none=True), 201)
```

Implement PUT, DELETE, test_fire similarly. test_fire posts a synthetic `state_changed` event to the router:

```python
@require_admin
def post_test_fire(handler, rule_id):
    cfg = load_events_config(_events_yaml_path())
    rule = next((r for r in cfg.rules if r.id == rule_id), None)
    if not rule:
        handler._send_json({"error": "no such rule"}, 404)
        return
    fake_event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": rule.trigger.entity_id,
            "old_state": {"state": "off"},
            "new_state": {"state": rule.trigger.to_state},
        },
    }
    _engine.event_router.handle_state_change(fake_event)
    handler._send_json({"ok": True}, 200)
```

- [ ] **Step 3: Build the WebUI tab**

In `ui.js`, add an `IntegrationsEventsTab` view that:
- Fetches `/api/integrations/events` on mount.
- Renders one row per rule: `[enabled toggle] [id] [action_kind] [edit] [delete] [test-fire]`.
- Edit opens a dialog with all rule fields; on save, PUT.
- Delete shows a "Are you sure?" confirm; on yes, DELETE.
- Test-fire button POSTs `/test_fire`; shows a transient "fired" toast.

The tab is shown in the sidebar only when the user has admin role (reuse the existing role-gating in the sidebar).

- [ ] **Step 4: Hand-test + commit**

```bash
pytest tests/webui/test_integrations_events_api.py -v
git add glados/webui/tts_ui.py glados/webui/static/ui.js glados/webui/static/ui.css tests/webui/test_integrations_events_api.py
git commit -m "feat(webui): admin Integrations → Events tab with CRUD + test-fire"
```

---

## Task 9: WebUI — Stall Clips tab (admin-only)

**Goal:** Admin-only tab to upload, preview, and delete stall clips at `${GLADOS_AUDIO}/<category>/<emotion>/`.

**Files:**
- Modify: `glados/webui/tts_ui.py` — new admin-gated `/api/integrations/stall_clips` endpoints.
- Modify: `glados/webui/static/ui.js` — new `IntegrationsStallClipsTab` view.
- Modify: `glados/webui/static/ui.css` — drop-target + clip-row styles.
- Test: `tests/webui/test_integrations_stall_clips_api.py`

**Acceptance Criteria:**
- [ ] GET `/api/integrations/stall_clips?category=X&emotion=Y` returns `[{filename, size, url}]` for clips in that directory. 404 if directory doesn't exist.
- [ ] POST `/api/integrations/stall_clips` (multipart) accepts an MP3 or WAV file with `category`, `emotion`, `filename` form fields; writes to `${GLADOS_AUDIO}/<category>/<emotion>/<filename>`.
- [ ] DELETE `/api/integrations/stall_clips/<category>/<emotion>/<filename>` removes the file.
- [ ] All endpoints admin-only (403 for non-admin).
- [ ] Path traversal rejected: `../`, absolute paths, etc. → 400.
- [ ] Per-file upload size cap: 10 MB. Larger → 413.
- [ ] UI: drop-target accepts MP3/WAV; preview plays the clip; delete confirms before removing.

**Verify:** `pytest tests/webui/test_integrations_stall_clips_api.py -v`

**Steps:**

- [ ] **Step 1: Write failing API tests**

Create `tests/webui/test_integrations_stall_clips_api.py` covering: list, upload happy path, oversize reject, path traversal reject, non-admin reject, delete, missing-directory list. Use `tmp_path` and monkeypatch `GLADOS_AUDIO` to isolate.

- [ ] **Step 2: Implement endpoints**

Pattern follows Task 8's admin-gated routes. Path safety is critical — validate `category` and `emotion` against a regex like `^[A-Za-z0-9_-]+$` and reject any path component containing `..`, `/`, or `\`.

```python
import re
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]+\.(mp3|wav)$")

def _safe_clip_path(category: str, emotion: str, filename: str) -> Path:
    if not (_SAFE_NAME.match(category) and _SAFE_NAME.match(emotion) and _SAFE_FILENAME.match(filename)):
        raise ValueError("unsafe path component")
    return _audio_root() / category / emotion / filename
```

Body cap of 10 MB enforced in the multipart parser; reject oversize with 413.

- [ ] **Step 3: Build the UI tab**

`IntegrationsStallClipsTab`:
- Top: dropdowns for category + emotion (free-text with the existing categories from `sound_categories.yaml` as suggestions).
- List of clips for the selected (category, emotion).
- Drag-target accepts MP3/WAV; uploads via POST.
- Each row has play/delete buttons.

- [ ] **Step 4: Hand-test + commit**

```bash
pytest tests/webui/test_integrations_stall_clips_api.py -v
git add glados/webui/tts_ui.py glados/webui/static/ui.js glados/webui/static/ui.css tests/webui/test_integrations_stall_clips_api.py
git commit -m "feat(webui): admin Stall Clips uploader at GLADOS_AUDIO/<cat>/<emo>/"
```

---

## Task 10: Engine wiring + autonomy health slots + deploy + smoke

**Goal:** Construct `EventRouter` at startup; bind it as a hub consumer; expose `Event Router` and `Vision Endpoint` health slots in autonomy. Deploy + run live-probe smoke.

**Files:**
- Modify: `glados/core/engine.py` — construct EventRouter; bind hub consumer.
- Modify: `glados/autonomy/<slot-store-or-equivalent>` — register `Event Router` and `Vision Endpoint` slots with importance per spec §4.
- Modify: `docs/CHANGES.md` — append entry describing Slice 3.

**Acceptance Criteria:**
- [ ] On engine start: `EventRouter` is constructed from `configs/events.yaml`; subscribed to the hub; the slots register.
- [ ] Engine startup log lines:
  ```
  HAWebSocketHub: thread started
  HAWebSocketHub: authenticated
  HAWebSocketHub: subscribed state_changed
  EventRouter: loaded N rules from configs/events.yaml
  EventRouter: subscribed to HAWebSocketHub
  ```
- [ ] Live deploy: container starts cleanly; `/health` returns 200.
- [ ] Live smoke (with one example rule enabled + a stall clip uploaded):
  - Operator triggers the trigger entity in HA (e.g. `service: input_boolean.toggle`).
  - Stall clip plays on the configured speaker within ~500 ms.
  - LLM continuation plays after the stall finishes (~3-5 s later).
  - Audit log line `kind: tool_call origin: event_rule rule_id: ...` appears.

**Verify:** Operator-witnessed.

**Steps:**

- [ ] **Step 1: Wire EventRouter at engine start**

In `glados/core/engine.py`, after the `HAWebSocketHub` start (Task 2), construct:

```python
        # Slice 3 — EventRouter
        from glados.events.config import load_events_config
        from glados.events.router import EventRouter
        from glados.events.actions.audio_random import play_random
        from glados.events.actions.llm import run_llm_action
        from glados.events.actions.vision_cascade import run_vision_cascade

        events_cfg_path = Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "events.yaml"
        events_cfg = load_events_config(events_cfg_path)

        def _dispatch_action(rule, event):
            if rule.action_kind == "audio_random":
                try:
                    play_random(
                        category=rule.category,
                        speaker=rule.speaker or _default_speaker,
                        ha_url=cfg.ha_url, ha_token=cfg.ha_token,
                    )
                except Exception as exc:
                    logger.warning("audio_random {} failed: {}", rule.id, exc)
            elif rule.action_kind == "llm":
                try:
                    run_llm_action(rule, event, tts_queue=self.tts_queue)
                except Exception as exc:
                    logger.warning("llm action {} failed: {}", rule.id, exc)
            elif rule.action_kind == "vision_cascade":
                run_vision_cascade(
                    rule, event,
                    tts_queue=self.tts_queue,
                    ha_url=cfg.ha_url, ha_token=cfg.ha_token,
                    speaker=rule.speaker or _default_speaker,
                    release_camera=self.event_router.release_camera,
                )

        self.event_router = EventRouter(events_cfg, action_dispatch=_dispatch_action)
        self._ha_ws_hub.subscribe(self.event_router.handle_state_change)
        logger.success(
            "EventRouter: loaded {} rules from {}",
            len(events_cfg.rules), events_cfg_path,
        )
```

- [ ] **Step 2: Add health slots**

Find the autonomy slot-store registration site (grep `slot_store.register` or `_register_slot`). Add:

```python
        # Slice 3 — visibility into event-action subsystem health
        slot_store.register(
            "Event Router",
            getter=lambda: ("connected" if self._ha_ws_hub.connected else "disconnected"),
            importance=lambda: 0.0 if self._ha_ws_hub.connected else 0.3,
        )
        slot_store.register(
            "Vision Endpoint",
            getter=lambda: _probe_vision_endpoint_health(),
            importance=lambda: 0.0 if _probe_vision_endpoint_health() == "healthy" else 0.5,
        )
```

`_probe_vision_endpoint_health()` does a `GET <llm_vision.url>/v1/models` with a short timeout, cached for 5 minutes. Returns `"healthy"` or `"unreachable"`.

Both slots use the R3 passive-slot importance gate from the chat-resolver-gate work — `importance=0` keeps them out of autonomy tick prompt when healthy, surfaces them in the WebUI System health panel always.

- [ ] **Step 3: Deploy**

```bash
git push origin <branch>
python scripts/deploy_ghcr.py
```

- [ ] **Step 4: Live smoke**

1. Upload a stall clip via WebUI Stall Clips tab to `front_door_approach/neutral/someone_arrived.mp3`.
2. Add a rule via WebUI Events tab: `id=test_doorbell, action_kind=audio_random, trigger={entity_id: input_boolean.test_event, to_state: on}, category=front_door_approach, speaker=media_player.kitchen, enabled=true`.
3. In HA, toggle `input_boolean.test_event` to on.
4. Listen for the clip on `media_player.kitchen` within ~500 ms.
5. Check container logs for `EventRouter: rule test_doorbell` audit line.

If audio_random works, swap the rule to `action_kind=vision_cascade, vlm_camera=<a real camera entity_id>, llm_continuation_prompt=<spec example>` and repeat. Confirm:
- Stall plays first.
- VLM call appears in logs.
- LLM continuation plays after stall finishes.

- [ ] **Step 5: Update CHANGES.md + close out**

Append a new Change entry summarizing Slice 3. No code commit needed for the smoke itself.

---

## Self-Review Checklist

- [ ] Spec coverage:
  - §2 Feature B cascade flow → Tasks 4 + 7
  - §2 vendor-agnostic event source detection (events.yaml) → Tasks 3 + 8
  - §3 EventRouter + actions table → Tasks 4–7
  - §3 HAWebSocketHub note → Task 2
  - §3 Auth/RBAC → Tasks 8 + 9 (admin-only)
  - §3 Audio root convention → Task 5 + Task 9 (`${GLADOS_AUDIO}/<cat>/<emo>/`)
  - §4 cross-cutting: cooldown / min_clear / per-camera lockout → Task 4; vlm_camera revalidation → Task 4 (`update_config` retains state for kept rules); audio queue ordering → Tasks 5 + 7 (relies on existing serial Speaker queue + HA media_player serializing per-speaker); audit `event_rule` source tag → Task 1; new autonomy slots → Task 10
- [ ] No placeholders: every step has actual code or actual commands (the WebUI tasks 8 + 9 reference existing-pattern test/auth fixtures rather than inlining them — flagged with explicit "grep for the pattern" steps).
- [ ] Type consistency: `EventRule`, `EventsConfig`, `EventRouter.update_config`, `release_camera` signatures match across Tasks 3, 4, 7. `tts_queue` is `queue.Queue[str]` consistently.
- [ ] No reach into Slice 1 territory: `glados/cameras/`, `glados/vision/client.py` are reused unchanged. ✓
- [ ] No reach into Slice 2 territory: `/api/chat/stream` `images:` field, chat-input UI, two-round VLM-then-chat are untouched. ✓
- [ ] Audio rule honored: every `${GLADOS_AUDIO}/<category>/<emotion>/` path is correct; no `configs/sounds/` paths in active code. ✓
- [ ] Existing `configs/sounds/` writers (TTS Save-to-library) NOT touched — flagged as separate follow-up. ✓
