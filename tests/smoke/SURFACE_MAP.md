# SURFACE_MAP.md — GLaDOS Container

**Phase 1 deliverable.** What is here was found by reading the source at
`C:\src\glados-container` (local clone of the GitHub repo
`synssins/glados-docker`). Anything not derived from a file:line citation
is flagged explicitly so the operator can correct it before Phase 2.

- **Branch:** `feature/smoke-tests-2026-05-05` (created from `main` at `896cc82`)
- **Source path:** `C:\src\glados-container`
- **Target deployment under test:** `glados.example.com` (the operator's Docker host;
  smoke runs from any machine that can reach it)
- **Date written:** 2026-05-05

---

## 0. Open questions — operator review

These need answers before TEST_PLAN.md is sensible. **Stopping here per the
Phase 1 instruction. Do NOT proceed to Phase 2 until these are resolved.**

1. **TLS state on glados.example.com.** Compose says ports 8015/8052/5051 are
   "TLS when cert mounted, plain HTTP otherwise". Which mode is the live
   container running in right now? The smoke runner needs to know whether
   to default to `http://` or `https://`. *(I will not probe to find out —
   that's an unauthorized network action.)*
2. **WebUI auth state.** The `auth.enabled` flag in `config.example.yaml`
   defaults to `true`, and the auth rebuild from 2026-04-25 is live. Most
   useful read endpoints behind 8052 require a session cookie. **How
   should smoke obtain a session?** Options:
   (a) the smoke suite reads a service-account username + password from
   an env var and POSTs `/login` once at suite start, caches the cookie;
   (b) a smoke-only API token is added (out of scope for this task);
   (c) we restrict 8052 probes to the public endpoints only
   (`/health`, `/api/health/aggregate`, `/api/health/public`,
   `/api/auth/status`) and skip everything else.
   Recommendation: **(c)** for first iteration — public endpoints already
   cover Tier 1 health needs.
3. **Docker daemon access.** `scripts/deploy_ghcr.py` already SSHes into
   the docker host using credentials in `C:\src\SESSION_STATE.md`. May the
   smoke suite reuse that path to call `docker inspect glados` and
   `docker logs --since <ts> glados` for Tier 1 / Tier 2 log probes? Or
   restrict to network-only and rely on `/api/logs/tail` (auth-gated) and
   `/health` for "is anything broken" detection?
4. **Sentinel utterance for Tier 3 (later).** What text should the canonical
   end-to-end utterance be so it does NOT trigger any HA automation?
   (Tier 3 is scaffolded skipped; the question only matters when fixtures
   get recorded.)
5. **The original prompt referred to the source path as `C:\src\glados-docker`.**
   The actual local checkout is at `C:\src\glados-container`. The remote on
   GitHub is `synssins/glados-docker`. Confirmed in this session — using
   `glados-container` throughout.
6. **Bitfocus Companion / Stream Deck and Hue / BiFrost.** The original
   prompt listed these as integrations to map. Neither exists in this
   repo (zero references in source). Confirming: should they be removed
   from TEST_PLAN.md scope, or are they served by a separate component
   the smoke suite should also cover?
7. **Wake word.** The original prompt asked about wake-word components.
   This container has no wake-word code — it is middleware that sits
   downstream of HA's voice pipeline. HA does wake + (optionally) STT
   externally. Confirming: smoke does not test wake word.
8. **MQTT.** Phase 2 of Stage 3 (MQTT peer bus) is documented in
   `docs/Stage 3.md` but **not yet wired in code** (no `paho`/`gmqtt`
   import, no `glados/mqtt/` module, no runtime client). Smoke should
   skip MQTT until it lands. Confirming.

---

## 1. Container topology

### 1.1 Compose stack
**Single service, single container.** From `docker/compose.yml:34-79`.

- Service name: `glados`
- Container name: `glados` (bare, no compose-project prefix —
  `compose.yml:40`)
- Image: `ghcr.io/synssins/glados-docker:latest`
- Restart policy: `unless-stopped`
- Watchtower-excluded via label.
- No `compose.override.yml` in repo. No multi-service stack.

### 1.2 Ports

| Host port | Container port | Service                                     | Notes |
|----------:|---------------:|---------------------------------------------|-------|
| **8015**  | 8015           | OpenAI-compatible API + GLaDOS endpoints    | TLS conditional |
| **8052**  | 8052           | Admin WebUI (login, dashboard, config)      | TLS conditional |
| **5051**  | 5051           | HA audio file server (static WAV)           | TLS conditional |
| —         | 18015          | Internal loopback healthcheck (plain HTTP)  | NOT exposed; container-only |

- TLS toggles automatically when `/app/certs/cert.pem` and `key.pem` are
  mounted. The Dockerfile healthcheck (`Dockerfile:108-113`) deliberately
  hits `127.0.0.1:18015` because that's protocol-stable — smoke probes
  from the network cannot reach 18015.
- `Dockerfile:106` — `EXPOSE 8015 8052`. (5051 is exposed via
  compose, not Dockerfile — `compose.yml:44`.)

### 1.3 Volumes
Five named volumes (`compose.yml:46-53`). On the host they will appear
prefixed by the compose project name (e.g. `glados_glados_logs`); the bare
forms below are the names declared in compose:

| Volume          | Container path     | Contents |
|-----------------|--------------------|----------|
| `glados_configs`| `/app/configs`     | YAML config; `glados_config.yaml`, `services.yaml`, `mqtt.yaml`, etc. |
| `glados_data`   | `/app/data`        | ChromaDB store, learned-context SQLite, conversation history |
| `glados_audio`  | `/app/audio_files` | Generated TTS, chimes, archives |
| `glados_logs`   | `/app/logs`        | Loguru file sinks, audit JSONL, per-plugin stderr |
| `glados_certs`  | `/app/certs`       | Let's Encrypt cert/key |
| (bind mount)    | `/var/run/docker.sock:ro` | Docker socket — used by WebUI Logs page (`compose.yml:53`) |

### 1.4 Process model
`glados/server.py` is the entrypoint (`Dockerfile:115` —
`python -m glados.server`):

- Main thread runs `glados.core.api_wrapper.main` after the WebUI thread
  is launched (`server.py:393-416`).
- The API wrapper is a `BaseHTTPRequestHandler` subclass at
  `glados/core/api_wrapper.py` — **not** Flask, not FastAPI, not Litestar.
  Routing is hand-rolled in `do_GET`/`do_POST` (`api_wrapper.py:3371,
  3445`).
- The WebUI runs on a daemon thread (`server.py:394-401`) launched from
  `glados.webui.tts_ui:run_webui`. It is also a `BaseHTTPRequestHandler`,
  with `_PUBLIC_PATHS` / `_PUBLIC_PREFIXES` gating auth (`tts_ui.py:511-520`).
- The HA WS client + Tier 2 disambiguator + persona rewriter +
  CommandResolver all bootstrap in a background helper before the API
  thread starts (`server.py:156-372`).
- `glados/api/app.py` exists and uses Litestar (`@get`/`@post`), but its
  routes are merged into the `/v1/audio/*` paths served by api_wrapper —
  it appears to define the route signatures rather than host the listener
  on a separate port. **Confirming this in TEST_PLAN.md before relying
  on the api_wrapper-only listener model.**

---

## 2. HTTP surface — port by port

Every route below was confirmed by reading the source at the cited line.
"Mutates" means the route changes container state (memory writes, config
saves, plugin reloads) — smoke must NOT call these except on dedicated
mutating-tests-only branches.

### 2.1 Port 8015 — API (`glados/core/api_wrapper.py`)

#### Read-only (smoke-safe)
| Method | Path                                 | Where                       | Description |
|--------|--------------------------------------|-----------------------------|-------------|
| GET    | `/health`                            | `api_wrapper.py:3374, 4504` | `{"status": "ok", "engine": "running"}` (200) or `"starting"`/`"stopping"` (503). No auth. |
| GET    | `/v1/models`                         | `api_wrapper.py:3372`       | Hardcoded single-entry model list `{"id": "glados", …}`. Does NOT enumerate the four LLM slots. |
| GET    | `/v1/voices` *or* `/v1/audio/voices` | `api_wrapper.py:3392`       | TTS voice list. Backed by `glados.TTS.list_available_voices()` scanning `/app/models/TTS/`. Default container has only `["glados"]`. |
| GET    | `/entities`                          | `api_wrapper.py:3376`       | HA entity snapshot from in-process EntityCache. |
| GET    | `/api/attitudes`                     | `api_wrapper.py:3378`       | Emotion / attitude preset list. |
| GET    | `/api/announcement-settings`         | `api_wrapper.py:3380`       | Announcement config readback. |
| GET    | `/api/startup-speakers`              | `api_wrapper.py:3382`       | Speaker config readback. |
| GET    | `/api/force-emotion`                 | `api_wrapper.py:3384`       | Emotion override presets. |
| GET    | `/api/semantic/status`               | `api_wrapper.py:3386`       | Semantic index build state. |
| GET    | `/api/test-harness/noise-patterns`   | `api_wrapper.py:3388`       | Test config readback. |
| GET    | `/api/emotion/state`                 | `api_wrapper.py:3390`       | Live PAD/mood readback. |

These are the principal Tier 1 / Tier 2 candidates on port 8015.

#### Compute paths (read-only-ish; produces output but does not persist)
| Method | Path                              | Where                  | Notes |
|--------|-----------------------------------|------------------------|-------|
| POST   | `/v1/audio/speech`                | `api_wrapper.py:3474`  | OpenAI-compat TTS. Returns audio/{mpeg,wav,ogg}. No memory write. **Smoke-safe** when called with a tiny string. |
| POST   | `/v1/audio/transcriptions`        | `api_wrapper.py:3476`  | OpenAI-compat STT (multipart/form-data). Returns `{"text": "..."}`. No memory write. **Smoke-safe** with a fixture WAV (Tier 3 only). |
| POST   | `/api/canon/retrieve`             | `api_wrapper.py:3464`  | Dry-run knowledge retrieval. **Smoke-safe.** |
| POST   | `/api/semantic/test`              | `api_wrapper.py:3466`  | Dry-run semantic match. **Smoke-safe.** |

#### MUTATING — DO NOT CALL FROM SMOKE
| Method | Path                                       | Where                  | Mutation |
|--------|--------------------------------------------|------------------------|----------|
| POST   | `/v1/chat/completions`                     | `api_wrapper.py:3446`  | Writes to `_engine._conversation_store.append_multiple` (`api_wrapper.py:1271`) — pollutes conversation history. |
| POST   | `/announce`                                | `api_wrapper.py:3448`  | Calls HA `media_player.play_media` — audible side effect in the operator's house. |
| POST   | `/doorbell/screen`                         | `api_wrapper.py:3450`  | Doorbell screening pipeline; speaks via HA. |
| POST   | `/api/announcement-settings`               | `api_wrapper.py:3452`  | Persists config. |
| POST   | `/api/startup-speakers`                    | `api_wrapper.py:3454`  | Persists config. |
| POST   | `/api/force-emotion`                       | `api_wrapper.py:3456`  | Mutates emotion state. |
| POST   | `/api/reload-engine`                       | `api_wrapper.py:3458`  | Hot-reloads engine. |
| POST   | `/api/reload-disambiguation-rules`         | `api_wrapper.py:3460`  | Hot-reloads Tier 2 rules. |
| POST   | `/api/reload-canon`                        | `api_wrapper.py:3462`  | Hot-reloads canon. |
| POST   | `/api/semantic/rebuild`                    | `api_wrapper.py:3468`  | Rebuilds index — expensive. |
| POST   | `/api/emotion/reset`                       | `api_wrapper.py:3470`  | Clears emotion state. |
| POST   | `/api/emotion/push-event`                  | `api_wrapper.py:3472`  | Injects synthetic emotion event. |

### 2.2 Port 18015 — same routes, loopback only
Same handler class as 8015; bound to `127.0.0.1` only. **Not reachable
from the smoke suite over the network.** Existence noted so we don't
accidentally try.

### 2.3 Port 8052 — WebUI (`glados/webui/tts_ui.py`)

#### Public — no auth required (`tts_ui.py:511`)
| Method | Path                       | Where                      | Description |
|--------|----------------------------|----------------------------|-------------|
| GET    | `/health`                  | `tts_ui.py:1936`           | WebUI's own readiness probe. |
| GET    | `/login`                   | `tts_ui.py:1915`           | Login form. |
| POST   | `/login`                   | `tts_ui.py:1997`           | **Mutates** (creates session). |
| GET    | `/logout`                  | `tts_ui.py:1933`           | **Mutates** (revokes session). |
| GET    | `/tts`                     | `tts_ui.py:1939`           | Public TTS-generator page. |
| GET    | `/setup`, `/setup/*`       | `tts_ui.py:1622-1667`      | First-run wizard. **Mutates** when bootstrap is allowed. |
| GET    | `/api/auth/status`         | `tts_ui.py:1719`           | Session role/permissions readback. |
| GET    | `/api/health/aggregate`    | `tts_ui.py:3258, 1819`     | Sidebar status feed. **Unauthenticated callers get `{"overall": "unauth"}` only.** Authenticated callers see per-service detail. |
| GET    | `/api/health/public`       | `tts_ui.py:3329, 1821`     | **Always unauth.** Returns `{"services": [{"name": ..., "status": "ok"\|"down"}, ...]}` for API / TTS / STT / HA. *This is the single best smoke probe on 8052.* Probes use 3 s timeouts internally. |

#### Public-prefix paths (`_PUBLIC_PREFIXES` at `tts_ui.py:513-520`)
Static and rate-limited compute paths. Smoke-relevant ones:

| Method | Path prefix                | Notes |
|--------|----------------------------|-------|
| GET    | `/static/*`                | Inline assets (CSS, ui.js). |
| GET    | `/api/voices`              | Voices list (delegates to 8015 internally). |
| GET    | `/api/speakers`            | Speakers list. |
| GET    | `/api/attitudes`           | Attitudes list. |
| GET    | `/api/files/`              | Audio file index. |
| GET    | `/api/stt`                 | STT proxy. |
| POST   | `/api/generate`            | TTS proxy. |

`_RATE_LIMITED_PUBLIC_PATHS` (`tts_ui.py:532-535`) covers
`/api/stt`, `/api/generate`, `/api/voices`, `/api/speakers`,
`/api/attitudes`, `/api/files`, `/files/`. Each is per-IP rate-limited; a
smoke run hitting them once per execution will not trip limits.

#### Authenticated (require valid session cookie `glados_session`)
Useful read-only ones for smoke (only if we adopt option 2a from §0):

| Method | Path                            | Where                  |
|--------|---------------------------------|------------------------|
| GET    | `/`                             | `tts_ui.py:1949`       |
| GET    | `/api/sessions`                 | `tts_ui.py:1971`       |
| GET    | `/api/plugins`                  | `tts_ui.py:1976`       |
| GET    | `/api/log_groups`               | `tts_ui.py:1984`       |
| GET    | `/api/users/`                   | `tts_ui.py:2254`       |
| GET    | `/api/quips`, `/api/chimes`, `/api/canon` | `tts_ui.py:2256-2260` |
| GET    | `/api/logs/sources`             | `tts_ui.py:1840, 4074` |
| GET    | `/api/logs/tail?source=...`     | `tts_ui.py:1843, 4103-4116` |

The `/api/logs/tail` endpoint is the single best authenticated probe for
recent container errors — it shells through `/var/run/docker.sock` to
read GLaDOS's own stdout. Sources include `container`, `audit`, and
`chromadb`. Returns 500 if the docker socket isn't mounted.

#### MUTATING — DO NOT CALL FROM SMOKE
All `POST`/`DELETE` routes under `/api/*` except those explicitly listed
above. Concretely:
`/api/chat`, `/api/auth/change-password`, `/api/config/*`,
`/api/quips`, `/api/chimes`, `/api/canon` (POST/PUT/DELETE),
`/api/users/*` (POST/PUT/DELETE),
`/api/sessions/*` (DELETE), `/api/plugins/*` (POST/DELETE),
`/api/files/*` (DELETE), `/api/memory/*` (DELETE),
`/api/log_groups/*` (POST), `/api/plugins/install`.

### 2.4 Port 5051 — HA audio file server

`glados/ha/homeassistant_io.py:110-128` — uses `http.server.SimpleHTTPRequestHandler`
to serve `${GLADOS_AUDIO}/glados_ha/` (default `/app/audio_files/glados_ha/`)
as static WAV files. Purpose: HA's `media_player.play_media` fetches WAVs
from this URL.

- `GET /` returns a directory listing (or 200/404 depending on index
  presence) — **smoke-safe probe**.
- `GET /<filename>.wav` returns audio/wav.
- No status, health, or version endpoint. Probe = bare `GET /` for a
  TCP-and-HTTP-200.

---

## 3. WebSocket surface

### 3.1 Inbound (the container exposes none)
**The container hosts ZERO WebSocket endpoints.** Confirmed by the search
done in the integration sweep — every WS reference in source is an
**outbound** client connecting to Home Assistant.

### 3.2 Outbound (the container connects out)
| Target              | URL pattern                                          | Where                                |
|---------------------|------------------------------------------------------|--------------------------------------|
| Home Assistant WS   | `ws://<ha-host>:8123/api/websocket`                  | `glados/ha/ws_client.py:HAClient` (auth: bearer token in handshake + per-message bearer field) |

Routes used over the HA WS connection (read-only registry pulls):
- `config/area_registry/list`, `config/device_registry/list`,
  `config/floor_registry/list`, `config/entity_registry/list`
  (`server.py:128-141`)
- `conversation/process` (writeable — fires HA conversation pipeline) —
  used by `glados/ha/conversation.py:ConversationBridge`. **Smoke must
  never POST to HA conversation.**

---

## 4. Voice pipeline

### 4.1 Wake word
**NOT IN THIS CONTAINER.** Zero references in source. The container is
middleware downstream of HA's voice pipeline; HA / Wyoming Protocol
handles wake-word externally.

### 4.2 STT — Parakeet CTC (bundled, on-CPU, ONNX)
- **Module:** `glados/ASR/ctc_asr.py` (default), `glados/ASR/tdt_asr.py`
  (alternate), `glados/ASR/null_asr.py` (testing stub)
- **Model:** `/app/models/ASR/parakeet-tdt_ctc-110m.onnx` (~440 MB inc.
  Silero VAD; `Dockerfile:56-58`)
- **Endpoint:** `POST /v1/audio/transcriptions` on port 8015
  (`api_wrapper.py:3476`); OpenAI-compat multipart/form-data
- **Input:** `file` form field — WAV/MP3/OGG, format auto-detected
- **Output:** `{"text": "..."}` JSON
- **Readiness probe (no audio sent):** None directly. STT is a lazy
  singleton — first transcription triggers model load (~2 s). Indirect
  proof of life: `GET /health` on 8015 returning 200 means the engine is
  up; `POST /v1/audio/transcriptions` with a 1 s silence WAV confirms
  the ONNX session loads (returns `{"text": ""}` on empty audio).
  **Tier 2 cannot test STT without a fixture WAV** — mark as
  `requires_audio_fixtures`.

### 4.3 TTS — local VITS (bundled, on-CPU, ONNX)
- **Module:** `glados/TTS/tts_glados.py` (`SpeechSynthesizer`),
  `glados/TTS/phonemizer.py`
- **Model:** `/app/models/TTS/glados.onnx` (63.5 MB) +
  `phomenizer_en.onnx` (61 MB) — `Dockerfile:51-58`
- **Endpoint (Litestar declaration):** `glados/api/app.py:115` defines
  `@get("/v1/voices")`. The same path is also routed by api_wrapper
  (`api_wrapper.py:3392`). **Need to confirm in Phase 2 which actually
  serves the request — most likely api_wrapper, with `app.py` providing
  the import surface. Will verify with a targeted read before any
  endpoint test.**
- **Speech endpoint:** `POST /v1/audio/speech` on 8015
  (`api_wrapper.py:3474`)
- **Input:** JSON body
  `{"input": "...", "voice": "glados", "response_format": "mp3"|"wav"|"ogg", "speed": float, "length_scale": float, "noise_scale": float, "noise_w": float}`
  (`api/app.py:65-74`)
- **Output:** audio bytes; `Content-Type: audio/mpeg|wav|ogg` per
  `response_format`
- **Voice listing:** `glados/TTS/__init__.py:list_available_voices()`
  scans `/app/models/TTS/*.onnx`. Default container has only
  `["glados"]`. **Smoke probe:** `GET /v1/voices` on 8015, assert
  `{"voices": [..., "glados", ...]}`.
- **Readiness probe:** `GET /v1/voices` is the cheapest. To prove the
  ONNX session loads, `POST /v1/audio/speech` with a tiny string and
  assert `Content-Type: audio/*` + non-zero body.

### 4.4 LLM routing — slot-based, all upstream
The container hosts no LLM. It routes outbound to whichever endpoint
each slot points at.

- **Slots** (declared in `glados/core/config_store.py:413-442`):
  - `llm_interactive` — main chat (env `OLLAMA_URL`)
  - `llm_autonomy` — autonomy agents (env `OLLAMA_AUTONOMY_URL`)
  - `llm_triage` — Tier 2 disambiguator + persona rewriter
    (env `OLLAMA_TRIAGE_URL`)
  - `llm_vision` — vision (env `OLLAMA_VISION_URL`)
- Each slot is a `ServiceEndpoint` (`config_store.py:370`) with `url` +
  `model` fields. Resolved via
  `glados.autonomy.llm_client.LLMConfig.for_slot(name)`
  (`autonomy/llm_client.py:63-79`).
- **Container's chat endpoint:** `POST /v1/chat/completions` on 8015 —
  forwards to upstream Ollama per routing logic
  (`api_wrapper.py:2115-2125`). **MUTATES** (writes to conversation
  store at `:1271`).
- `/v1/models` returns a hardcoded `{"id": "glados", …}` and does NOT
  enumerate slots. There is no public endpoint exposing the four slot
  URLs — slot config is visible only via authenticated WebUI
  `/api/config/llm` or by reading `/app/configs/global.yaml` directly.
- **Readiness probe (no LLM call):** the simplest signal is
  `/api/health/public` on 8052 — its probes section returns API + TTS +
  STT + HA. It does NOT probe upstream LLMs. Direct upstream LLM probing
  would require knowing the Ollama URL, which is operator-specific and
  not in the smoke suite's config. **Recommend: smoke does not probe
  upstream LLMs in Tier 1/2; covered indirectly by Tier 3 e2e.**

### 4.5 Persona rewriter — internal only
- **Module:** `glados/persona/rewriter.py` — `PersonaRewriter`
- **Slot:** `llm_triage` (`server.py:203, 329`)
- **HTTP exposure:** none. Called internally by `CommandResolver`
  (`glados/core/command_resolver.py:679-689`) on Tier 1 / Tier 2 / time
  / weather paths.
- **Readiness probe:** indirect. Ensure `llm_triage` slot is set
  (operator config), then verify by examining recent audit log entries
  for any `RewriteResult` rows. **Skip directly testing the rewriter
  in smoke; covered by Tier 3 e2e if/when fixtures are recorded.**

---

## 5. Integrations

### 5.1 Home Assistant — primary integration
- **Module:** `glados/ha/` (`ws_client.py`, `conversation.py`,
  `entity_cache.py`, `semantic_index.py`, `homeassistant_io.py`)
- **Connection:** WS to `<HA_WS_URL>` (env / `global.yaml`); bearer
  token auth
- **Smoke probe (read-only, no automation trigger):**
  - On port 8052: `GET /api/health/public` includes HA in its services
    list, returning `{"name": "HA", "status": "ok"|"down"}`. Probe
    method internally is `GET <HA_URL>/api/` with the bearer token
    (`tts_ui.py:3362-3368`). **This is the single best smoke probe
    for HA.** No utterance, no automation fires.
- **Side effects to avoid:** never POST to HA `conversation/process`
  (the WS message variant); never call `/announce` or
  `/doorbell/screen` on the GLaDOS API.

### 5.2 MQTT — NOT YET WIRED
- Documented in `docs/Stage 3.md` as Phase 2.
- No `paho`/`gmqtt`/`aiomqtt` import in code; no `glados/mqtt/` module.
- `MQTTConfig` exists in `config_store.py` (~lines 185-208) with
  `enabled: bool` defaulting to `False`.
- **Smoke action: skip MQTT entirely.** If/when wired, add probe.

### 5.3 HUB75 LED display
- **Module:** `glados/hub75/` (7 files; `display.py:Hub75Display` is the
  daemon)
- **Transport:** UDP DDP (WLED protocol) to a WLED controller on the
  LAN
- **Config:** `Hub75DisplayConfig` (`config_store.py`) — `wled_ip`,
  `wled_ddp_port=4048`, panel dimensions, fps, `enabled` (default
  `False`)
- **Smoke action:** check whether `cfg.hub75.enabled` is `True`. If
  `False`, skip with a clear "feature dormant" message. If `True`, the
  cheapest probe is a `GET http://<wled_ip>/json` (WLED's own status
  API) — but that's reaching into operator-controlled hardware not
  belonging to this repo. **Recommend: skip in smoke; add a "config
  presence only" check that reports the current `enabled` state without
  touching the WLED.**

### 5.4 Discord — scaffolding only, NOT WIRED
- `glados/discord/__init__.py` exists with one line; no client logic
- `DiscordConfig` (`config_store.py`) provides `bot_token`,
  `active_channels`, `alert_channel`, `allowed_user_ids`
- **Smoke action: skip.**

### 5.5 Doorbell — wired and active
- **Module:** `glados/doorbell/screener.py`
- **Trigger:** `POST /doorbell/screen` on 8015 (synthesizes greeting,
  listens, transcribes, evaluates, replies, announces via HA)
- **Smoke action: NEVER call `/doorbell/screen`.** The most we can do
  is verify the route returns 405 / 401 / 400 on a `GET` (i.e. the
  handler is wired) — but that's circular and not worth the risk.
  **Skip.**

### 5.6 MCP plugins
- **Modules:** `glados/mcp/manager.py:MCPManager` (asyncio loop in
  background thread; `manager.py:71`),
  `glados/mcp/config.py:MCPServerConfig`, `glados/plugins/`
- **Transports:** stdio, http, sse (per `MCPServerConfig`)
- **Status endpoint:** the integrations sweep noted no
  `/api/mcp/status` route. Plugin enumeration is via authenticated
  `/api/plugins` on 8052 (`tts_ui.py:1976`).
- **Smoke action:** if smoke has an authenticated session, list plugins
  via `/api/plugins` and assert `200`. Otherwise skip with
  `requires_auth`.

### 5.7 Vision — external service
- **Module:** `glados/vision/` (5 files; middleware only)
- **External URL:** `VISION_URL` env / `cfg.services.vision.url`
- **Consumer:** `glados/autonomy/agents/camera_watcher.py`
- **Smoke action:** if `cfg.services.vision.url` is set,
  `/api/health/public` does NOT cover it (probes section is fixed at
  API/TTS/STT/HA). For Tier 2: read the configured vision URL from
  `/api/config/services` (auth) and ping its `/health` if exposed. If
  no auth, skip with reason.

### 5.8 ChromaDB — bundled, in-process
- **Module:** `glados/memory/chromadb_store.py:MemoryStore`
  (`MemoryStore.__init__` ~line 77+)
- **Path:** `/app/data/chromadb` (env `CHROMADB_PATH`)
- **Collections:** `episodic` (TTL), `semantic` (persistent)
- **Smoke probe:** `/api/health/public` includes ChromaDB indirectly
  via the aggregate version (`tts_ui.py:3320-3325`) — but the public
  variant *omits* ChromaDB. From the network, the closest probe is the
  authenticated `/api/health/aggregate` which checks the path's
  writability (`tts_ui.py:3320-3325`). Without auth, **skip ChromaDB
  in Tier 1; cover indirectly via Tier 3 e2e (memory-touching).**

### 5.9 Bitfocus Companion / Stream Deck — NOT PRESENT
Zero source references. Drop from scope.

### 5.10 Hue / BiFrost — NOT PRESENT
Zero source references (the only `hue` hits are CSS color tokens and
`companion cube` game references). Drop from scope.

### 5.11 Sonorium — referenced as future MQTT peer only
Mentioned in `docs/Stage 3.md` as a planned MQTT peer. No current
runtime integration. Drop from Phase 2 scope.

---

## 6. Logging & health

### 6.1 Loguru sink configuration — the SUCCESS gate
- **Where:** `glados/observability/log_groups.py:1151-1189`
  (`install_loguru_sink()`)
- **Default level:** `LogGroupRegistry` with `default_level=LogLevel.SUCCESS`
  (level 25). See `log_groups.py:249, 688, 845`.
- **Effect:** `logger.info()` (level 20) is **invisible** in
  `docker logs` and the container stdout sink. Only SUCCESS+ and
  per-group-enabled levels pass through.
- **Implication for smoke:** when reading container logs as the
  baseline-vs-now diff source, looking for `INFO` lines is a
  no-op. Filter to `WARNING`, `ERROR`, `CRITICAL`, `Traceback` only.

### 6.2 Log destinations
- **Stdout** → loguru stderr sink → captured by `docker logs glados`
- **Files** under `/app/logs/` (mounted as `glados_logs` volume).
  Per-service `.log` files; legacy list at `tts_ui.py:3613-3616`.
- **Audit log:** `cfg.audit.path` defaults to `/app/logs/audit.jsonl`.
  JSONL, one record per utterance / tool call. Always-on by default.
- **Per-plugin stderr:** `/app/logs/plugins/*.log`
  (`glados/mcp/manager.py:107`)

### 6.3 Reading logs from outside the container
Three options for the smoke suite, in order of cleanness:

1. **`/api/logs/tail?source=container&lines=N` on 8052**
   (`tts_ui.py:1843, 4103-4116`) — uses the read-only Docker socket
   mounted into the container. **Requires authentication.** Returns 500
   if the socket isn't mounted; 502 on Docker API failure. Sources:
   `container`, `audit`, `chromadb`.
2. **SSH to docker-host.local + `docker logs --since <ts> glados`** —
   reuses the path `scripts/deploy_ghcr.py` already uses. Cleanest if
   the smoke suite can carry the SSH credentials (ask operator).
3. **`/api/logs/tail` without auth → can't be done.** Will return 401.

**Recommendation for TEST_PLAN.md:** Tier 1 captures a baseline
timestamp at suite start; if `/api/logs/tail` is auth-available,
diffs between then and end. Otherwise log diffing is deferred to a
later iteration that adds an SSH-backed log fetcher fixture.

### 6.4 Health endpoints summary

| Endpoint                                  | Port | Auth     | Returns |
|-------------------------------------------|-----:|----------|---------|
| `GET /health`                             | 8015 | none     | `{"status":"ok","engine":"running"}` (200) or `"starting"`/`"stopping"` (503). `api_wrapper.py:4504-4521` |
| `GET /health`                             | 8052 | none     | WebUI's own readiness. `tts_ui.py:1936` |
| `GET /api/health/public`                  | 8052 | none     | `{"services": [{"name": "API"\|"TTS"\|"STT"\|"HA", "status": "ok"\|"down"}, ...]}` — best aggregate probe. `tts_ui.py:3329-3377` |
| `GET /api/health/aggregate`               | 8052 | optional | Without auth: `{"overall":"unauth"}`. With auth: full per-service detail incl. ChromaDB. `tts_ui.py:3258-3327` |
| `GET /health` (loopback)                  | 18015| none     | Same as 8015. Not network-reachable from smoke. |

There is no `/status`, `/healthz`, `/ready`, `/metrics`, `/version`
endpoint in this container. The closest "version" surface is the
`X-GLaDOS-Version` header (if it exists — needs Phase 2 verification)
and the git SHA baked into the image label.

---

## 7. Recommended Tier 1 / Tier 2 surface (preview, will move to TEST_PLAN.md)

For TEST_PLAN.md to draw on, the cleanest cheap probes are:

**Tier 1 (no auth, no external dependencies):**
- TCP connect to 8015, 8052, 5051
- `GET <host>:8015/health` → 200, JSON `{"status":"ok"}`
- `GET <host>:8052/health` → 200
- `GET <host>:8052/api/health/public` → 200, all 4 services `ok`
- `GET <host>:8015/v1/voices` → 200, `voices` includes `glados`
- `GET <host>:5051/` → 200 or 404 (TCP+HTTP-200/404 either fine; both
  prove the listener is up)

**Tier 2 (still unauth):**
- `GET <host>:8015/v1/models` → 200, contains `glados`
- `GET <host>:8015/api/attitudes` → 200, list shape
- `GET <host>:8015/api/emotion/state` → 200, PAD shape
- `GET <host>:8015/api/semantic/status` → 200
- `GET <host>:8015/entities` → 200, list shape (HA wired)
- `POST <host>:8015/v1/audio/speech` with `{"input":"smoke test","voice":"glados"}`
  → 200, `Content-Type` starts with `audio/`, body length > 0

**Tier 2 (auth-requiring, conditional on §0 question 2):**
- `GET <host>:8052/api/plugins` → 200, list of MCP servers
- `GET <host>:8052/api/logs/tail?source=container&lines=20` → 200,
  body contains no `Traceback`/`CRITICAL`/`ERROR` lines

**Tier 3 (skipped pending fixtures):**
- Inject WAV → `POST /v1/audio/transcriptions` → text out → feed to
  `POST /v1/chat/completions` (DESTRUCTIVE — see §0 q2 — may need a
  dry-run flag) → text response → `POST /v1/audio/speech` → audio out.
  Total under N seconds.

---

## 8. STOP — human review needed

Phase 1 is done. **Do not proceed to Phase 2 until the operator has
reviewed this map, answered the Open Questions in §0, and confirmed
or corrected the integration findings.** Specifically I need answers
on:

1. TLS state on glados.example.com (§0 q1)
2. WebUI auth strategy for smoke (§0 q2)
3. Docker daemon access for log reading (§0 q3)
4. Confirmation that wake word, MQTT, Bitfocus, Hue/BiFrost are out
   of scope (§0 q6, q7, q8)
5. Anything missing from this map — components I didn't find, or
   surfaces the operator knows about that aren't reachable from source
   alone.
