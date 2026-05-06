# Camera Vision + Event-Action Design

**Date:** 2026-05-05
**Author:** Claude (Opus 4.7) with operator (synssins)
**Status:** Spec — pending operator review before plan generation

---

## 1. Overview and scope

Three operator-visible features ship in this design pass, sharing one VLM substrate and one new event-action subsystem.

### Features

**A — On-demand camera vision in chat.** Operator says *"What do you see in the back yard?"* → GLaDOS describes the scene + the snapshot is rendered inline in the chat bubble. Single tool call from the chat LLM. Works for any HA-exposed camera, vendor-agnostic.

**B — Event-triggered vision cascade.** When a HA event fires (e.g. `binary_sensor.front_door_person_detected → on`), the container runs a layered response: instant pre-recorded stall clip ("Someone is approaching the front door"), in parallel snapshot fetch + VLM describe + persona LLM continuation that knows what the stall said and only adds new info ("they're wearing a red hat"). Vendor-agnostic via generic HA entities.

**C — User-supplied image in chat.** Operator pastes (Ctrl+V), drags, or uploads up to 4 images into the WebUI chat input → images attach to their next message → GLaDOS describes/answers about them; thumbnails render inline in the user's bubble. Same VLM substrate; different ingress.

### Shared substrate (third deliverable)

The **event-action system** drafted in `project_event_actions_plan.md` (2026-04-21) becomes the host for Feature B. `configs/events.yaml` rules + a new WebUI Integrations → Events card + the existing two `action_kind`s (`audio_random`, `llm`) get a third: `vision_cascade`. This first concrete consumer of the event-action system is the reason to bundle the infra into this spec.

### Out of scope (deferred or never)

- **Face recognition** (Phase 2). MVP persona-prompt assembly leaves a clean `participants:` hook.
- **Pet / named-object identity** (Phase 2). VLM will say *"a dog"* in MVP, not *"Fritocus Moronicus Maximus"*.
- **License-plate recognition** (Phase 2 enrichment).
- **UniFi-specific entity names / event attributes**. Generic HA entities only. UniFi extras (vehicle subtype, plate string) consumed opportunistically when present, never required.
- **vLLM, WSL2, Docker Desktop** (ruled out by `reference_aibox_serving_constraints.md`).
- **Real-time / streaming video** — snapshots only.
- **Modifying upstream services** (OpenArc, HA, Piper internals).
- **Live / streaming user uploads** — single chat-turn batch only; max 4 images.

### Hard constraints

- TTS stays local Piper, in-container. No "Speaches" — see `feedback_no_speaches.md`.
- No hardcoded HA entities — operator's camera lineup is HA-discovered.
- Pre-recorded stall clips bypass TTS for events (instant playback).
- Speech latency target: time-to-first-audio for events ≤ 500 ms (stall clip plays); LLM continuation 1.5–3 s.
- No silent fallbacks — failures error loudly with cause (`feedback_no_silent_fallback.md`).
- WebUI services tab is the source of truth for `llm_vision` URL/model (`feedback_webui_is_service_truth.md`).

---

## 2. Architecture and data flow

### Three ingress paths, one VLM

```
HA camera (A: chat tool)         ─┐
HA event trigger (B: cascade)    ─┼──→ snapshot bytes ──→ VLM service ──→ description ──→ persona ──→ user
WebUI paste/drop/upload (C: user)─┘                       (llm_vision slot)
```

Persona handling is the only path-specific bit:

- A: chat LLM sees the description as a tool-call result → wraps in persona reply.
- B: persona LLM is told what the stall already said → only adds new info.
- C: chat LLM sees user's text + image-description → answers naturally as a chat turn (two-round design preserves chat-LLM persona quality on a small VLM).

### Lane allocation (operator deployment, not container code)

| GPU | Service | Workload | Notes |
|---|---|---|---|
| AIBox B60 (`:11434`, OpenArc) | chat | Qwen3-30B (text) | Unchanged. |
| AIBox T4 #1 (`:11436`, NSSM `llama-rewriter`) | autonomy + triage + rewriter | Qwen3-4B-Instruct-2507 | Unchanged. Health restored by autonomy fixes shipped on `chat-resolver-gate` branch. |
| **AIBox T4 #2 (`:11437`, NSSM `llamacpp-vision`)** | **vision (NEW)** | Qwen2.5-VL-3B-Instruct + mmproj | New service. Idle today. Lane-separated from autonomy → no compute contention; vision worst-case latency stays at ~1.5–3 s regardless of autonomy load. |

**Realistic per-tick latency** with this layout:

- Feature A end-to-end: ~3–6 s (chat-LLM TTFT + snapshot fetch + VLM + persona decode).
- Feature B time-to-first-audio: ~300 ms (pre-recorded stall). LLM continuation arrives 1.5–4 s later.
- Feature C end-to-end: ~3–5 s (VLM describe + 30B persona).

### Feature A — on-demand chat tool flow

```
1. user: "What do you see in the back yard?"
2. chat path → chat LLM (30B@B60) decides to call look_at_camera(name="back yard")
3. tool handler:
    ├─ resolve "back yard" → camera.backyard_high (HA discovery cache)
    ├─ GET /api/camera_proxy/camera.backyard_high → bytes
    ├─ POST llm_vision: [{image_url}, {text:"describe"}] → description text
    ├─ emit `event: image` SSE chunk `{tool_call_id, image_url: data-url}` onto the chat stream (PARALLEL to the LLM token stream; never enters LLM context)
    └─ return tool result `{description}` (text only) to chat LLM
4. chat LLM continues, persona-wraps the description → SSE text stream
5. WebUI pairs the `event: image` chunk to the in-progress assistant bubble by `tool_call_id` and renders the inline thumbnail
6. WebUI renders text bubble + inline image

**Why split the channel.** A 1080p PNG base64-encodes to ~1–2 MB. If we returned the data URL through the tool result, it would land in chat history and be replayed in every subsequent turn's prompt — context-window-blowing and wasteful. Out-of-band SSE keyed by `tool_call_id` keeps the LLM seeing only `{description}` while the WebUI still gets the bytes for inline render.
```

### Feature B — event-triggered cascade flow

```
1. HA fires binary_sensor.front_door_person_detected → on
2. EventRouter (subscribed to HA WS) matches rule front_door_approach
3. action_kind: vision_cascade fires:
    A. INSTANT (parallel start, t≈0):
       └─ play random pre-recorded clip from `${GLADOS_AUDIO}/front_door_approach/<emotion>/`
          → audio playing in 200–400 ms (bypasses TTS entirely)
    B. CONCURRENT (parallel start, t≈0):
       ├─ GET /api/camera_proxy/<configured camera>
       ├─ POST llm_vision: [image, "describe what is happening"]
       └─ POST llm_interactive (chat lane, 30B): persona prompt with stall awareness
    C. WHEN (B) completes (1.5–4 s after start):
       └─ TTS persona continuation → audio queued to play AFTER stall finishes
4. Debounce: per-rule cooldown_s + per-rule min_clear_s + per-camera lockout
5. WebUI (if connected): receive event:announcement chunk with snapshot for visual record
```

### Feature C — user-uploaded chat flow

```
1. user drags 2 images into chat input + types "What's wrong with this circuit?"
2. WebUI POSTs /api/chat/stream with body {message, history, images: [data-url, data-url]}
3. chat path detects images → routes the FIRST round to llm_vision:
    ├─ Build OpenAI multimodal: [{image_url}×2, {text: user_message}]
    ├─ POST llm_vision → "two images of a circuit board with a burnt resistor near R3..."
    └─ Stash description as a system-context insertion
4. SECOND round goes to llm_interactive (30B@B60) with persona:
    └─ system: "[image_descriptions] <vlm_output>"
       user:   <user_message>
       assistant: streams persona reply
5. WebUI: user bubble shows 2 thumbnails inline; GLaDOS reply streams as text + audio
```

### Vendor-agnostic event source detection

EventRouter subscribes to HA's WebSocket once at boot and listens for state changes on any `binary_sensor.*` matching configured event-rule patterns. Example rule:

```yaml
# configs/events.yaml
- id: front_door_approach
  enabled: true
  source: ha_state
  trigger:
    entity_id: "binary_sensor.front_door_person_detected"
    to_state: "on"
  action_kind: vision_cascade
  category: front_door_approach
  vlm_camera: camera.g4_doorbell_high
  cooldown_s: 60
  min_clear_s: 30
  llm_continuation_prompt: |
    You already said: {stall_text}.
    The camera shows: {vlm_output}.
    Add only NEW information not in the stall, in 1–2 short sentences.
```

Operator wires up rules per their cameras. Works with any HA-supported vendor. UniFi-specific event attributes available via `{{ trigger.attributes.* }}` template tokens for advanced rules, but never required.

---

## 3. Components

### New AIBox-side stand-up (operator deployment, not container code)

| Item | Value |
|---|---|
| NSSM service | `llamacpp-vision` |
| Binary | `C:\llamacpp\llama-server.exe` |
| Model | `Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` + matching `mmproj-Q8_0.gguf` |
| Args | `--model <path> --mmproj <mmproj> --port 11437 --host 0.0.0.0 --gpu-layers 999 --device CUDA1 --ctx-size 8192 --parallel 1 --flash-attn off` |
| Logs | `C:\llamacpp\logs\llamacpp-vision.{stdout,stderr}.log` |
| Shadow port for testing | `:11438` first; verify with HA snapshot probe; promote to `:11437` when verified. |

This is an AIBox prod-write — needs explicit per-action sign-off on the literal `nssm install` command + arg list per `feedback_research_before_prod_writes.md`.

### Container — new modules

| Module | Purpose |
|---|---|
| `glados/cameras/discovery.py` | HA camera-entity discovery + friendly-name → entity_id map; cached, refresh every 60 s; exposes `list_cameras()` + `resolve_camera_name(query: str)`. |
| `glados/cameras/snapshot.py` | `fetch_snapshot(entity_id) -> bytes` via HA `/api/camera_proxy/<entity_id>` with `HA_TOKEN`. |
| `glados/vision/client.py` | OpenAI-multimodal POST to `llm_vision` slot URL; `describe_images(imgs: list[bytes], prompt: str) -> str`; replaces vestigial `glados/vision/__init__.py`. |
| `glados/tools/look_at_camera.py` | Replaces dead `vision_look.py`; takes `camera_name`; calls discovery + snapshot + vision client; returns `{description}` (text only) to chat LLM. Snapshot bytes are pushed to the WebUI out-of-band as a parallel `event: image` SSE chunk keyed by `tool_call_id` — see §2 Feature A. Registered via the existing builtin-tool path (`glados/core/builtin_tools.py`); exposed to the **chat lane only** (autonomy excluded — would inflate autonomy tool budget for no use case); gated by the same chat-shape filter as other chat-only tools. |
| `glados/events/router.py` | Hosts the rule matcher + dispatcher. **Consumes `state_changed` events from a shared HA-WS singleton** (Note A below) — does NOT open a second connection. Matches incoming events against `configs/events.yaml` rules and dispatches actions. |
| `glados/events/actions/vision_cascade.py` | The cascade implementation: pick stall clip, fire snapshot+VLM in parallel, persona LLM continuation with stall-aware prompt, debounce per-rule + per-camera lockout. |
| `glados/events/actions/audio_random.py` | Wraps existing chime/quip random-pick logic for events; reads stall clips from `${GLADOS_AUDIO}/<category>/<emotion>/*.{mp3,wav}` per the audio-root convention (`feedback_audio_paths.md`). Existing `configs/sounds/`-writing callers (TTS-Save-to-library + `sound_categories.yaml` indexer at [config_store.py:882](glados-container/glados/core/config_store.py:882) / [tts_ui.py:2334](glados-container/glados/webui/tts_ui.py:2334)) are tracked separately as a cleanup ticket — NOT bundled into this MVP. |
| `glados/events/actions/llm.py` | Existing `llm_preset` action_kind sketched in `sound_categories.yaml` — concrete implementation. |

### Container — modified existing code

| File | Change |
|---|---|
| `glados/core/api_wrapper.py` | `/api/chat/stream` accepts `images: [data-url]` field; routes image-bearing turns through two-round VLM→chat flow. |
| `glados/webui/tts_ui.py` | SSE schema gains `event: image` chunk type with `{image_url}` payload; round-2 of two-round flow injects VLM description as system context. |
| `glados/webui/static/ui.js` | Chat input: paste/drop/file-picker handlers, image-attachment queue with thumbnails, multipart send; chat-bubble renderer: inline `<img>` for `event: image` chunks and for user-attached images. |
| `glados/webui/static/ui.css` | Image-attachment chip + thumbnail styles; chat-bubble inline-image layout. |

### Container — new configs

| File | Schema |
|---|---|
| `configs/events.yaml` | List of event rules: `id, enabled, source, trigger, action_kind, category, vlm_camera?, llm_continuation_prompt?, cooldown_s, min_clear_s, speaker?`. |
| `${GLADOS_AUDIO}/<category>/<emotion>/*.{mp3,wav}` | Operator-curated stall clips, indexed by `Category/emotion` per the audio-root convention (NOT under `configs/`). Existing `configs/sound_categories.yaml` already declares categories — config-only, no audio bytes. WebUI Stall-Clips uploader writes here. |

### WebUI — new operator surfaces

- **Integrations → Events tab** (new): list of rules from `events.yaml`; add/edit/delete, test-fire button, per-rule cooldown.
- **Integrations → Stall Clips tab** (new or merged with Events): drag-drop MP3 uploader keyed by category; preview/play; remove.
- **Chat input** (existing): paste/drop/upload affordance + thumbnail chips + send-with-images. Max 4 images; JPG/PNG/WebP only; 5 MB per image, 20 MB total.
- **Configuration → Services** (existing, no UI change): `llm_vision` slot already exists; operator sets URL/model after the AIBox stand-up.

### Note A — HA WebSocket sharing strategy

[`glados/autonomy/agents/ha_sensor_watcher.py`](glados-container/glados/autonomy/agents/ha_sensor_watcher.py) already maintains a persistent HA WebSocket connection consuming `state_changed`. The new EventRouter must NOT open a second one.

**MVP plan (this spec):** extract a shared `HAWebSocketHub` singleton owning the connection + reconnect loop. Both `ha_sensor_watcher` and `events.router` register as fan-out consumers of the same incoming event stream. One TCP connection, two consumers. Smallest mutation that keeps both lanes healthy.

**Phase 2 (deferred):** fold `ha_sensor_watcher`'s rule logic into `events.router` so the router is the single subscriber and autonomy becomes a downstream consumer of the router's higher-level events (e.g. `room_occupancy_changed`). Tracked as a follow-up; not in this MVP.

### Auth / RBAC

The auth rebuild from 2026-04-25 (`project_auth_rebuild_complete.md`) is live. New surfaces follow this matrix:

| Surface | Required role |
|---|---|
| Integrations → Events tab (CRUD on `events.yaml` rules + test-fire) | **admin** only |
| Integrations → Stall Clips tab (upload / preview / delete clips under `${GLADOS_AUDIO}`) | **admin** only |
| Chat input image attach (paste / drop / picker, send-with-images) | any authenticated user with chat permission (existing chat scope) |
| `/api/chat/stream` `images:` field (server-side acceptance) | same scope as `/api/chat/stream` text path |
| `look_at_camera` tool invocation by chat LLM | implicit via chat scope; tool itself enforces no per-camera ACL in MVP |

Per-user / per-camera ACLs (e.g. "only admin can ask about the back yard camera") are a Phase 2 hook — out of scope.

### Image data lifecycle

User-attached images (Feature C) and snapshot data URLs from `look_at_camera` (Feature A) are **NOT persisted in conversation history** for MVP. Specifically:

- `images:` field on `/api/chat/stream` lives only for the request lifetime — VLM-described, then dropped. The user's next-turn history contains the operator's text + the assistant's reply + (optionally) the VLM description as a system-context block; the raw image bytes are gone.
- `event: image` SSE chunks render to the in-progress assistant bubble in the live tab. On page reload, the historical assistant turn re-renders with the persona text only — the inline image disappears.
- This means: descriptions persist, image bytes do not. Persistent inline thumbnails on history reload is a Phase 2 hook (small image store keyed by `message_id` under `${GLADOS_AUDIO}/../chat_attachments/` or similar — out of scope here).

### Server-side request budgets

| Endpoint | New limit | Behavior on overflow |
|---|---|---|
| `/api/chat/stream` (when `images:` present) | 25 MB total request body | `413 Request Entity Too Large` with explicit cause; no partial processing. |
| Per-image (server-side double-check of client validation) | 5 MB | `400 Bad Request` with the offending image index. |
| Image count | 4 | `400 Bad Request` listing the count received. |

### Content-Security-Policy

If the WebUI sets a CSP `img-src` directive (audit at plan time — current state likely no CSP), it must include `data:` so inline image data URLs render. If no CSP today, no change needed; flag for the auth/security review pass.

### Cleanup (deletion / rewrite of existing dead code)

- `glados/vision/__init__.py` lazy-stub for `VisionProcessor` — superseded by `glados/vision/client.py`.
- `glados/tools/vision_look.py` — superseded by `glados/tools/look_at_camera.py`.
- `glados/vision/vision_request.py` / `vision_state.py` — vestigial in-process queue path; replaced by direct HTTP calls to `llm_vision` slot.

`glados/autonomy/agents/camera_watcher.py` polling of the disabled `glados-vision` validation service at `:8016` is adjacent technical debt — flagged as a separate follow-up, not bundled into this spec.

### Portability rule (architectural constraint)

The container and WebUI know NOTHING about the backend. All they see:

- `services.llm_vision.url` — any OpenAI-compatible endpoint accepting `messages` with multimodal `content: [{type:"image_url",...}, {type:"text",...}]`.
- `services.llm_vision.model` — any model name accepted by that endpoint.

Operator can point `llm_vision` at: local llama.cpp on AIBox T4 #2 (today's choice), llama.cpp on a different GPU, Ollama with a multimodal model, vLLM, OpenArc with a multimodal model, OpenAI / Anthropic / Mistral cloud APIs, a self-hosted gateway. The container POSTs OpenAI-multimodal JSON, gets text back, hands the text up the chain.

**Concretely:**

- ❌ NO `T4` / `NVIDIA` / `CUDA` / `llama.cpp` strings anywhere in container code or `services.yaml`.
- ❌ NO `:11437` hardcoded; it's just whatever the operator types in WebUI services tab.
- ❌ NO Qwen2.5-VL-specific prompt templates or response parsing — generic OpenAI shape only.
- ❌ NO hardcoded auth shape beyond `Authorization: Bearer <api_key>` (already in slot model via `api_key` field).
- ✅ The "AIBox stand-up" section is documentation of *one possible deployment* of the slot, not container behavior. Everything in `glados/cameras/`, `glados/vision/`, `glados/events/` is endpoint-agnostic.

---

## 4. Error handling

Per `feedback_no_silent_fallback.md`, every failure surfaces with cause.

| Feature | Failure | Behavior |
|---|---|---|
| A | HA snapshot fetch returns non-200 | Tool returns `error: HA snapshot failed: <status> <reason>`; chat LLM relays verbatim. |
| A | Camera-name resolution miss | Tool returns `error: no camera matched "<name>". Available: <list>`. Helps operator discover actual camera names. |
| A | `llm_vision` slot timeout / 5xx | Tool returns `error: vision endpoint <url> failed: <reason>`; chat LLM relays. URL included so operator knows where to look. |
| A | VLM produces empty / suspicious-short response | Tool returns the raw description with `low_confidence: true` flag; chat LLM may add hedge language. NOT silently substituted. |
| B | HA WebSocket disconnect | EventRouter logs `WARNING` once per disconnect, retries with backoff; while disconnected, `Event Router` slot status = `disconnected` with importance=0.3. |
| B | Stall clip directory empty for matched category | Action logs `WARNING [event_id] no stall clips in ${GLADOS_AUDIO}/<category>/<emotion>/`; falls through to LLM-only continuation. Visible failure mode. |
| B | VLM call fails mid-cascade | Stall already played. LLM continuation generated from system message `[scene description failed: <reason>]` — chat LLM produces a graceful "I heard something at the front door but couldn't see what." |
| B | `vlm_camera` entity not in HA states | Action errors at rule-load time (rule fails validation); operator sees error in WebUI Events tab. Rule disabled until fixed. |
| B | Persona LLM (chat lane) timeout for continuation | Stall already played. Continuation skipped; log `WARNING`; surface to Slot Store with importance=0.3. |
| C | Image format unsupported (HEIC, GIF, etc.) | Client-side rejected before send with clear message ("Only JPG/PNG/WebP supported"). |
| C | Per-image > 5 MB or total > 20 MB | Client-side rejected before send. |
| C | VLM endpoint refuses multimodal payload | Server returns `502 vision endpoint <url> failed: <reason>` with explicit cause; WebUI shows it as a system message in chat. |
| C | Request body > 25 MB / per-image > 5 MB / count > 4 | `413` or `400` per the §3 budget table; explicit cause string. |

### Cross-cutting

- **New autonomy slots** for health visibility:
  - `Event Router` — `connected` / `disconnected` / `error`, importance = 0/0.3/0.6.
  - `Vision Endpoint` — pinged every N min via `GET /v1/models`; `healthy` / `unreachable`, importance = 0/0.5.
  Both visible in WebUI System health panel; routine importance keeps them out of autonomy tick prompt per the new R3 `_should_render_slot` filter.
- **Audit logging.** Every event-cascade dispatch + every chat tool call writes to `glados/observability/audit.py` with `source_tag="event_rule"` or `"vision_tool"` for activity-trail visibility per `project_event_actions_plan.md`.
- **Debounce edges.**
  - `cooldown_s` (per-rule): minimum elapsed seconds since last fire of THIS rule. Default 30 s.
  - `min_clear_s` (per-rule): trigger entity must be in **off** state for N seconds before re-arming. Kills oscillation flap. Default 10 s.
  - **Per-camera lockout** (cross-rule): no two `vision_cascade` actions overlap on the same camera. **Lockout key = literal `vlm_camera` entity_id string** (e.g. `camera.front_door_high`). Aliasing two physical-same cameras under different entity IDs is operator-managed (out of scope). Second event dropped, logged with `WARNING [event_id] camera <id> locked by <prior_event_id>`.
- **`vlm_camera` runtime validation.** Rule fails at load time if the entity isn't in HA's discovered camera list. The discovery cache also re-runs validation on its 60 s refresh — if a previously-valid `vlm_camera` disappears (HA integration removed), the rule auto-disables and surfaces in the WebUI Events tab as `[disabled — camera <id> not in HA]`. Re-enables automatically on next refresh once the entity returns.
- **Audio queue ordering for cascades.** The container's existing Speakers / Piper output queue is FIFO and serial; cascade actions enqueue (i) the stall clip, then (ii) the persona LLM continuation, in that order. Continuation playback waits naturally for stall completion via the existing queue — no new sequencing primitive needed. (Plan task: confirm during integration that the Speakers queue exposes a stable `enqueue(category, payload)` entrypoint; if not, lift one.)

---

## 5. Phase 2 hooks

### The merge point

For Features A and B, the persona LLM gets a structured context block instead of raw VLM text:

```
[scene_observation]
camera: front_door
event: person_detected (HA Smart Detect)
vlm_description: "an adult standing on the porch, holding a parcel, dusk lighting"
participants: []                     # ← Phase 2 face-rec drops items here
objects: []                          # ← Phase 2 pet-rec / brand-rec drops items here
plates: []                           # ← Phase 2 LPR drops items here
confidence: 0.87
```

Persona LLM is instructed: *"Compose a single 1–2 sentence reply using only the fields populated above. Do not invent identities or details not present."* When `participants: []` is empty (MVP today), persona says "someone is at the front door"; when face-rec lands and populates `participants: [{name: "Chris", confidence: 0.94}]`, persona says "Chris is at the front door."

**No persona-prompt rewrite required when face-rec ships** — just a new enrichment step before the merge that populates the `participants` field.

### Enrichment service contract (Phase 2 onwards)

Each enrichment is an HTTP service:

```
POST /enrich
Body: { image: <base64>, slot: "participants" | "objects" | "plates" }
Response: { matches: [{name, confidence, bbox?}, ...] }
```

Lives at its own URL slot in `services.yaml` (`face_rec.url`, `pet_rec.url`, etc.) — same operator-configurable pattern as `llm_vision`. Empty slot URL → step skipped silently. Filling it in WebUI services tab activates the enrichment.

---

## 6. Testing

### Unit tests (CI, no external deps)

| Module | Covers |
|---|---|
| `glados/cameras/discovery.py` | HA-states-mock fixture, fuzzy-name resolution, friendly_name vs entity_id, refresh-on-stale. ~10 cases. |
| `glados/cameras/snapshot.py` | mocked HTTP for HA camera_proxy, error paths (404, timeout, non-image content-type). |
| `glados/vision/client.py` | OpenAI-multimodal payload shape (single + multi-image); auth-header passthrough; error mapping; mocks the `llm_vision` HTTP. |
| `glados/tools/look_at_camera.py` | name resolution + snapshot + VLM call wired together with all three mocked; happy path, miss, snapshot-fail, VLM-fail. |
| `glados/events/router.py` | rule loading + validation (valid/invalid `events.yaml`), trigger matching, `cooldown_s` + `min_clear_s` + per-camera lockout edges. |
| `glados/events/actions/vision_cascade.py` | stall clip selection, parallel fan-out (mocked sleeps), persona prompt with `{stall_text}` substitution, error pathways. |
| WebUI client (`ui.js`) | image-attachment queue (add/remove/reorder), format/size validation, multi-image POST shape (hand-tested + small jest-style if any exist). |

### Integration smokes (operator-demand, live AIBox + HA)

- **Vision smoke**: `look_at_camera("back yard")` end-to-end against the live VLM service + the operator's HA. Verify text references plausible scene contents AND the snapshot URL is renderable.
- **Event cascade smoke**: simulate a HA state change via `POST /api/services/...` to fire `binary_sensor.test_event`; confirm the stall plays + LLM continuation arrives within budget.
- **Multi-image upload smoke**: WebUI side, drop 3 images, confirm preview thumbnails + send + GLaDOS reply describes all three.

### Live-probe verification post-deploy

Same shape as the autonomy/TTS investigation in this session — explicit timing + body-bytes probes against the live container. The autonomy-probe diagnostic stays useful as a regression watch for "are we hitting the right size budgets."

### What we do NOT test

- VLM output quality (the model is a black box; we test plumbing).
- Pre-recorded clip "fits the moment" (operator-curated content).
- HA's own integration (we test handling correctness; HA breakage isn't our problem).

---

## 7. Tracked follow-ups (out of MVP, captured for the next round)

1. **Face-rec / pet-rec / LPR enrichment services** — Phase 2.
2. **Live video streaming** — single-snapshot only in MVP.
3. **Operator-curated camera aliases** beyond HA `friendly_name`.
4. **VLM model auto-discovery** in WebUI services tab.
5. **Auto-rotation between cameras** in event cascade ("show me the most-relevant snapshot from any camera").
6. **Cleanup of orphaned `glados-vision` validation service URL** in `camera_watcher` autonomy agent.
7. **Plugin-triage chat-shape gate** — originally-deferred fix from the 2026-05-05 investigation.
8. **`autonomy-probe` / `autonomy-probe-gate` diagnostic log lines** — leave in for now; revert when the operator is satisfied with autonomy lane health.

---

## Appendix A — Decision log (this session)

- **Vision lane** = T4 #2 separated, not consolidated on T4 #1. Reason: avoids compute contention with autonomy/triage lane; keeps event-trigger response real-time. Pre-fix consolidation analysis showed worst-case 8–11 s for vision when 4B was busy; Option B (separate) keeps vision at 1.5–3 s regardless of 4B load.
- **D1 (strip MCP tools from autonomy entirely) — REJECTED.** Autonomy needs to be able to call HA tools directly for the kitchen-presence-aware-lighting use case the operator described; escalating every transition to chat round-trip is too slow.
- **D3 (per-tool allowlist) — REJECTED.** Artificial limit, future-fragile.
- **D4 (move autonomy to T4 #2) — REJECTED in favor of the in-session autonomy fixes** (R1 severity gate + R3 passive-slot filter + round-2 tool-result filter) which restored T4 #1 lane health without moving autonomy.
- **Two-round chat for Feature C** = chosen over single-call against a vision-capable chat lane. Reason: preserves Qwen3-30B persona quality on the chat lane; VLM 3B is a describer, not a stylist.
- **Bundle event-action infra** with vision spec rather than two parallel specs. Reason: vision_cascade is the first concrete consumer of the event-action system; designing them together prevents schema-vs-consumer drift.
- **Face-rec / pet-rec deferred** to Phase 2 with the `participants:` / `objects:` hook pre-baked into persona prompt — drops in cleanly later without persona rewrite.

## Appendix B — Related work shipped in this session

- `chat-resolver-gate` branch carries 6 fix commits ending at `fb89684`; `0bd46e3` is this spec doc on top:
  - Tier 1+2 home-command resolver gated on `looks_like_home_command` (saves ~5 s on chat-flavored questions).
  - Autonomy MCP context-resource cull (small win).
  - R1 severity gate + R3 passive-slot filter for autonomy tool-filter.
  - Round-2 (role=tool) extension so the tool-filter applies on every autonomy turn.
  - `[autonomy-probe]` / `[autonomy-probe-gate]` diagnostic log lines (revertable when satisfied).

These are operator-deployed and live as of 2026-05-06; this vision spec assumes they are merged. The `chat-resolver-gate` branch is open as a PR candidate.
