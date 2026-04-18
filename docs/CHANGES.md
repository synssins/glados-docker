# GLaDOS Container — Change Log

Every structural change to the container repo, in chronological order. Each
entry records what changed, why, and what side-effects the operator should
expect. This is the running journal for the containerization work — it
supplements git history with *why* rather than *what*.

---

## Change 1-5 — (earlier history, see git log)

Summary of prior work (pre-April 17, 2026):
- Change 1: Pure-middleware refactor (removed bundled TTS/ONNX, replaced with
  speaches HTTP client)
- Change 2: CI workflow setup (Snyk Python + Container scans)
- Change 3: Snyk policy file committed
- Change 4: Snyk CLI migration (UUID token format)
- Change 5: Container security hardening (non-root user)

All merged into PR #1 which landed on `main` on April 16, 2026.

---

## Change 6 — Step 1.10: local smoke test, porting fixes

**Date:** 2026-04-15
**Status:** Complete
**Commits:** `9630f1a`

Fixes surfaced by the first local `docker build` + `docker compose up` per
Stage 1 Step 1.10. The port commit (`2c10651`) touched ~60 modules in a
single pass without runtime testing; this change captured every failure and
fixed them.

### Build + infrastructure fixes

- **`docker/compose.yml`** — chromadb healthcheck switched from `curl` (not
  in chroma image) to `bash /dev/tcp` probe
- **Dockerfile** — glados user now has home dir (`useradd -m`) for subagent
  memory writes

### Missing modules — not ported in `2c10651`

- **`glados.observability`** (4 files: bus, events, minds, __init__)
- **`glados.vision`** — lightweight only (VisionState, VisionConfig,
  VisionRequest, constants). FastVLM + VisionProcessor stay external.
- **`glados.mcp`** (11 files, 1377 lines) — HA MCP client

### Code fixes

- **`glados/webui/tts_ui.py:1542`** — syntax error (stray quote in
  os.environ.get rewrite)
- **`glados/tools/__init__.py`** — removed `slow_clap` (requires sounddevice)
- **`glados/ASR/__init__.py`** + `null_asr.py` — added `"none"` engine type
  for container mode

### Smoke test results

6/8 endpoints passed on first try (health, models, attitudes, speakers,
chat, chromadb via override). Expected failures: TTS 404 (voice not
registered in speaches — Stage 4), announce (no announcements.yaml — fixed
in Change 7).

---

## Change 7 — Docker host deployment + feature completion

**Date:** 2026-04-17
**Status:** Complete
**Commits:** `a7de7c1` through `2f44046`

Deployment to user's Docker host (OMV, Btrfs, separate from AIBox) plus
significant feature work: agentic tool calling, audio playback UI, HA MCP
integration, fuzzy entity resolution, SSL certificate management, and
Stage 3 planning.

### Deployment

- **GHCR build permissions fix** (commit `a7de7c1`) — added
  `permissions: packages: write` to `.github/workflows/build.yml` so
  `GITHUB_TOKEN` can push to `ghcr.io/synssins/glados-docker:latest`
- **Compose fragment delivered** to operator — fits inside their existing
  docker-compose.yml alongside Plex/Arr/Ollama stack
- **ACL workaround documented** — OMV uses POSIX ACLs; operator must run
  `setfacl -R -m u:1000:rwx,m::rwx $DOCKERCONFDIR/glados` so uid 1000 (the
  container user) can write to bind-mounted config/audio dirs

### Configuration path fixes

- **`announcements.yaml`, `vision_announcements.yaml`, `commands.yaml`**
  moved from `audio_files/` tree to `/app/configs/` (commit `28bfa68`).
  These are config, not media — separating them cleaned up the mount
  layout. Code paths in `api_wrapper.py` and `ha_sensor_watcher.py` now
  read from `cfg._configs_dir` or `GLADOS_CONFIG_DIR` env var.

### TTS fixes

- **`glados/TTS/tts_speaches.py`** (commit `11e6c31`) — fixed two bugs:
  1. Attitude params (`length_scale`, `noise_scale`, `noise_w`) moved from
     `extra_body` wrapper (speaches-only convention) to top-level fields so
     the AIBox Piper TTS at port 5050 accepts them
  2. Default `model` no longer hardcoded to `hexgrad/Kokoro-82M` — sent
     only if explicitly configured

### WebUI fixes

- **Removed Training tab** (commit `683050a`) — piper_train is a host-native
  tool not available in the container. Both sidebar and mobile topbar.
- **Token display encoding** (commit `683050a`) — replaced UTF-8 mojibake
  arrow `â†→` with ASCII `->`
- **Curly quote JS syntax error** (commit `ecf1142`) — bulk replacement of
  Unicode smart quotes (U+2018/U+2019) with straight ASCII quotes (0x27)
  in the JS inline script
- **Audio playback controls** (commits `6d3c148`, `78603f9`, `759d53f`):
  1. Added `do_HEAD` handler so `<audio>` element's probe request
     succeeds (was returning 501)
  2. Added HTTP Range (206 Partial Content) support for audio seeking
  3. Added `Accept-Ranges: bytes` header on all binary responses
  4. Sync background `Audio()` playback with visible `<audio controls>` —
     handoff on `canplay` event preserves current playback position

### HA MCP agentic tool loop

- **`glados/core/api_wrapper.py` `_stream_chat_sse`** — major additions:
  1. `commit 7332a2e`: agentic tool loop — streaming chat now includes
     tool definitions from the MCP manager, detects `tool_calls` in
     streaming responses (both Ollama and OpenAI formats), executes via
     `mcp_manager.call_tool()`, feeds results back, up to 5 rounds
  2. `commit 361fcdd`: MCP tools only (dropped static tools like
     `do_nothing`/`robot_*` that clutter context), `num_ctx: 16384`
     override to fit personality + tools
  3. `commit 7ecba22`: tool-use reinforcement system message (persona
     few-shots biased toward text responses; explicit instruction to use
     tools overrides that bias)
  4. `commit 2236ec2`: strip few-shot user/assistant examples from
     preprompt when tools are present — prevents model from following
     text-only example pattern
  5. `commit f0ca98c`: removed function-syntax examples from tool hint
     (model was copying `HassTurnOff(...)` as literal text output)
  6. `commit 82d2b9e`: use real HA names (not "testing illumination" from
     Aperture Science persona), use `domain` not `device_class` for lights
  7. `commit baf83bd`: tool argument auto-fixup — if model puts `light` in
     `device_class`, move to `domain`; ensure `domain` is always an array
  8. `commit b25878b`: **fuzzy entity name resolution** — before any
     `HassTurnOn`/`HassTurnOff`/`HassLightSet` call, query HA REST API for
     entities and fuzzy-match the name parameter using `rapidfuzz`.
     "cabinet lights" → "Kitchen cabinet light switch" (score 88)

### Compaction agent bug (conversation store growth)

- **`glados/core/conversation_store.py`** + **`glados/autonomy/agents/compaction_agent.py`**
  (commit `1100df7`) — root cause of B60 IPEX Ollama freezes on AIBox. The
  compaction agent excluded ALL system messages from compaction (role-based
  check), but each compaction cycle CREATES a new system-role `[summary]`
  message. Over hours/days these summaries accumulated unbounded — host
  reached 132K tokens with compaction running continuously but unable to
  shrink. Fix: replaced role-based exclusion with index-based preprompt
  protection. `ConversationStore` now tracks `preprompt_count`. Compactable
  set = everything except the initial preprompt messages and the last N
  recent messages. Summaries ARE now compactable. Host-native glados-api
  also received this fix.

### SSL certificate management

**Commits:** `9ad2b52`, `4ad589c`, `2f44046`

Complete Let's Encrypt integration with Cloudflare DNS-01 challenge, plus
manual PEM upload path and live certificate status display.

- **`Dockerfile`** — installs `certbot` + `certbot-dns-cloudflare` via pip
- **`glados/core/config_store.py`** — `SSLGlobal` expanded with
  `use_letsencrypt`, `acme_email`, `acme_provider`, `acme_api_token`
- **`glados/webui/tts_ui.py`** — new endpoints:
  - `GET /api/ssl/status` — parses cert with `cryptography` library,
    returns subject, issuer, SANs, not_before, not_after, days_remaining,
    source (letsencrypt/self-signed/manual)
  - `POST /api/ssl/upload` — accepts PEM content via JSON, validates
    headers, writes to configured paths with 0600 on key
  - `POST /api/ssl/request` — runs certbot with DNS-01 Cloudflare
    challenge, captures output, copies issued cert/key to configured
    paths. Handles "not due for renewal" gracefully.
- **WebUI** — new SSL section in Configuration tab with live status box,
  Let's Encrypt form (domain, email, provider, API token, Request button),
  manual upload pickers, advanced path overrides, restart reminder

### Verified working on operator's Docker host (192.168.1.150)

- Let's Encrypt cert issued for `glados.denofsyn.com` (E7 issuer)
- HTTPS returns 200, valid chain, expires Jul 16, 2026
- HA MCP tool calls execute end-to-end (e.g. `HassTurnOff` on kitchen
  cabinet light switch confirmed)
- Fuzzy entity resolver matches "cabinet lights" to real entity name
- Audio playback controls show live progress, pause works, download works
- Streaming chat produces tool calls instead of hallucinated text

### Side effects

1. **New pip dependencies in the image** — `certbot` + `certbot-dns-cloudflare`
   (+15 MB). Runs as subprocess when operator clicks "Request Certificate".
2. **`num_ctx: 16384` override** in streaming chat requests — overrides the
   Modelfile's 8192 to fit personality + 21 MCP tools. Increases prompt
   processing time slightly but prevents tool definition truncation.
3. **Few-shot examples dropped** from streaming chat messages when MCP is
   available — personality system prompt still sent, but the user/assistant
   example pairs are omitted to prevent text-response bias.
4. **Fuzzy resolver makes an HA REST API call per tool invocation** — adds
   ~50-200ms. Mitigated in Stage 3 by local entity cache.
5. **SSL is container-internal** — `/app/certs` is a named Docker volume.
   Certs persist across restarts but don't survive `docker compose down -v`.
   Recommended: change the volume to a host bind mount for backup resilience.
6. **HA MCP tool schema rigidity** — even with reinforcement and argument
   fixup, the model occasionally refuses to call tools ("do it or don't, I
   don't care") due to persona bias. Logged as a roadmap item.

---

## Change 8 — Stage 3 Phase 0 + Phase 1: HA Conversation Bridge

**Date:** 2026-04-17 (continued through evening into 04-18 UTC)
**Status:** Phase 0 + Phase 1 complete and deployed; Phase 2 (MQTT peer
bus) and Phase 3 (safety hardening / tests) pending.
**Commits:** `a0b5d69` through `a42434b` (17 commits)

The big architectural lift. Replaces the previous "every utterance →
LLM agentic loop" path (10–20s per command) with a three-tier matcher
that gets common device commands under one second and ambiguous ones
under ten — without lying about success when state didn't actually
change. Plan in `docs/Stage 3.md`; this entry summarizes what landed.

### Phase 0 — Audit logging (`712bffe`, `ebde74a`)

Foundation for everything that follows: durable, queryable record of
every utterance entering the system and every tool/intent decision
that resulted.

- **`glados/observability/audit.py`** — new module. `AuditEvent`
  dataclass + `AuditLogger` (bounded-queue background JSONL writer
  that never blocks hot paths on disk I/O) + `Origin` constants
  (`webui_chat`, `api_chat`, `voice_mic`, `mqtt_cmd`, `autonomy`,
  `discord`). 11 unit tests.
- **`glados/core/config_store.py`** — new `AuditGlobal` model;
  `cfg.audit.{enabled,path}` accessor. Defaults: enabled,
  `/app/logs/audit.jsonl`.
- **`glados/server.py`** — initializes the singleton at startup;
  failure is non-fatal (no audit ≠ no engine).
- **`glados/core/tool_executor.py`** — `_audit_tool()` called at
  every tool-call terminus (ok/error/timeout/no-mcp/unknown-tool).
  Reads `_origin` and `_principal` from the queue item; defaults to
  `UNKNOWN` so missing plumbing is audit-visible.
- **`glados/core/api_wrapper.py`** — `_handle_chat_completions` reads
  the `X-GLaDOS-Origin` header (set by the WebUI when it proxies),
  defaults unknowns to `api_chat`, emits an `utterance` event for
  both streaming and non-streaming paths.
- **`glados/webui/tts_ui.py`** — `_chat` and `_chat_stream` emit
  utterance events with `origin=webui_chat` and the session principal,
  forward `X-GLaDOS-Origin` to api_wrapper. New protected
  `GET /api/audit/recent` endpoint with `limit/origin/kind` filters.

### Phase 1 — HA Conversation Bridge (`5da0288`, `0a62ade`, `1ca9a93`,
`8ab6773`)

Tier 1: HA's `/api/conversation/process` over WebSocket.

- **`glados/ha/`** — new package. `entity_cache.py` (in-memory
  EntityCache with per-domain fuzzy thresholds, `state_as_of`
  freshness timestamps, sensitive-domain hard guard for
  lock/alarm/camera/garage-cover); `ws_client.py` (persistent
  asyncio HAClient in its own thread, follows the MCPManager
  pattern, reconnect with backoff + `get_states` resync, inject-
  at-constructor `connect_fn` for testability); `conversation.py`
  (thin wrapper + classifier — handled / disambiguate / fall_through
  / garbage_speech).
- **`glados/server.py` `_init_ha_client()`** — opens the persistent
  WS at startup, loads ~3,500 entities into the cache.
- **`glados/core/api_wrapper.py`** — `_try_tier1_fast_path` (SSE)
  and `_try_tier1_nonstreaming` (JSON) before `_stream_chat_sse`.
  On hit: emit response, audit `tier=1, result=ok`; latency typically
  300–600 ms.
- **`pyproject.toml`** — added `websockets>=13.0` and
  `rapidfuzz>=3.0.0` (was transitively present, now declared).

Bug fixes during Phase 1:
- **HA env overrides YAML for credentials** (`0a62ade`) — operator's
  `configs/global.yaml` had a placeholder `eyJh...PFNFG8` token
  (truncated middle); env var had the real 183-char JWT. Pydantic
  was preferring YAML, so the WS auth failed with `auth_invalid`.
  Added a `model_validator` so `HA_TOKEN`, `HA_URL`, `HA_WS_URL`
  always win when set in env. Quietly fixed MCP tool calls too
  (they read the same config).
- **Non-streaming Tier 1 intercept** (`1ca9a93`) — WebUI's `/api/chat`
  posts `stream:false`, so the streaming-only intercept missed it.
  Added the matching path; both branches now go through Tier 1.
- **Garbage-speech filter** (`8ab6773`) — HA's intent matcher
  occasionally returns `query_answer` with literal speech `"None None"`
  (templated from a Person entity with empty first/last name attrs).
  `_is_garbage_speech()` rejects empty/`"None"`/`"None None"`/`"null"`
  speech and falls through to the LLM with `error_code=garbage_speech`
  in the audit so HA's bad intents stay visible.

### Phase 1 — Tier 2 LLM Disambiguator (`36f0d94`, `b92b160`)

When HA returns `no_intent_match` or `no_valid_targets`, the
disambiguator pulls candidates from the local cache and asks a
constrained LLM to pick / clarify / refuse with structured JSON.

- **`glados/intent/`** — new package. `rules.py` (keyword→domain
  mapping for candidate filtering, `IntentAllowlist` with
  per-source × per-domain matrix — sensitive domains
  `lock`/`alarm_control_panel`/`camera`/`garage cover`
  permit `webui_chat` only — and YAML loader for operator-tunable
  `DisambiguationRules`); `disambiguator.py` (Disambiguator class
  that builds the prompt, calls Ollama with `format=json`, parses
  the structured decision, validates entity IDs and allowlist
  before executing via `HAClient.call_service`).
- **`glados/server.py`** — initializes the disambiguator after the
  HA WS client; loads optional `configs/disambiguation.yaml`.
- **`glados/core/api_wrapper.py`** — both Tier 1 paths now consult
  Tier 2 when `should_disambiguate=True`. Hits emit through the
  same SSE/JSON shape; misses fall through to Tier 3.

Bug fixes during Tier 2:
- **Wrong Ollama URL + wrong model** (`b92b160`) — first deploy
  used `cfg.service_url("ollama_autonomy")` which returned the
  interactive URL (services.yaml hardcoded). Switched to env-first
  resolution. Default model also changed from `glados` (which
  fights JSON output — *"I am GLaDOS, not an API endpoint for JSON
  responses."*) to a clean instruction-follower. Bumped LLM timeout
  from 8 s to 25 s after observing 12 s cold-starts on the autonomy
  GPU.

### Phase 1 polish (`5ce0111`, `f01e4a2`, `d2e7999`, `5fac500`,
`7a616a5`, `e803da8`, `cf15e1a`, `a42434b`)

Iterative tightening from live testing against the operator's house.

- **Persona rewriter** (`5ce0111`) — `glados/persona/rewriter.py`.
  Tier 1 hits now flow HA's plain text ("Turned off the kitchen
  light") through a short Ollama call that restyles in GLaDOS voice
  ("Kitchen illumination, terminated. Predictable."). Best-effort:
  any LLM failure returns HA's original speech so the user always
  gets a real reply. 12 unit tests. Audit gains `speech_plain` /
  `rewrote` fields.
- **Model split** (`f01e4a2`) — settled on **`qwen2.5:14b-instruct`
  for the disambiguator** (instruction-following matters for the
  long structured prompt; ~5 s warm) and **`qwen2.5:3b-instruct`
  for the rewriter** (style task with short input/output, ~500 ms).
  `qwen2.5:3b` was pulled onto the autonomy box specifically.
- **Service-name mapping in prompt** (`d2e7999`) — Tier 2 was
  refusing scene activations because the schema only listed
  `turn_on/off/toggle`; LLM didn't know `scene.turn_on` is the
  HA service for "activate scene". Added an explicit domain →
  service mapping table to the prompt with examples for scenes,
  scripts, covers, locks, climate, vacuum.
- **Fuzzy match overhaul** (`5fac500`) — `'activate the evening
  scene'` was resolving to `scene.scene_go_away` because the
  entity_id-derived form `'scene go away'` matched the word `scene`
  at score 85, beating the real `Living Room Scene: Evening` at 47.
  Three changes: `searchable_names()` no longer adds the entity_id-
  derived form when `friendly_name` or aliases exist; `process.extractOne`
  now uses `processor=utils.default_process` so case + punctuation
  no longer penalize legitimate matches; scene/script cutoffs lowered
  from 75 → 60 (loose semantic categories); new `_preprocess_query`
  strips command verbs (`activate the evening scene` → `evening scene`).
- **Universal-quantifier handling** (`7a616a5`) — `'all lights'` and
  `'turn off the whole house'` were getting clarified instead of
  executed. New `_has_universal_quantifier()` detects `all`/`every`/
  `whole`/`entire`/etc.; bumps candidate limit from 12 → 30; prompt
  instructs the LLM to PREFER group entities (`light.whole_house_lights`)
  over enumerating individuals, and to IGNORE non-actuatable domains
  (zone, sensor, automation) for action verbs.
- **Prompt-example speech leakage** (`e803da8`) — LLM was copying the
  prompt example speech verbatim ("Evening scene engaged…" for a
  reading-scene activation). Removed the example speech text from the
  prompt; added explicit "do not echo example phrasing" instruction.
- **Activity inference** (`cf15e1a`) — `'I would like to read in the
  living room'` wasn't mapping to `scene.living_room_scene_reading`.
  New ACTIVITY INFERENCE section in the prompt explicitly maps
  activities (`read`, `movie`, `sleep`/`goodnight`, `wake up`,
  `dinner`/`cooking`) to scenes/scripts. Same commit also captures
  `call_service_failed` exception class names so empty `str(exc)`
  values from low-level failures are debuggable, and surfaces HA's
  `success: false` error payloads when the WS returns an error
  response instead of raising.
- **Vocative elimination + no-ack handling** (`a42434b`) — operator
  asked to eliminate "test subject" / "human" trailing addresses.
  Both prompts (disambiguator + rewriter) now explicitly forbid
  vocative labels; deterministic `_strip_trailing_vocative()`
  pass in the rewriter removes them if the LLM ignores. Also: HA's
  WS `call_service` ack timeout bumped from 5 s to 15 s; on
  `concurrent.futures.TimeoutError` specifically, the disambiguator
  returns `decision=execute_no_ack` (action almost certainly
  succeeded — HA acks acceptance, not completion, and group
  cascades sometimes don't ack in time) instead of falsely
  reporting failure.

### Tests

110 tests pass at end of Change 8 (was 11 before Change 8):
- 11 audit module
- 20 entity_cache (incl. fuzzy regression for `evening scene`)
- 5 WS client (with fake WS)
- 9 conversation classifier
- 17 disambiguator (incl. allowlist enforcement, defensive paths)
- 18 intent rules + allowlist + YAML loader
- 21 persona rewriter (incl. vocative-strip cases)

### Live performance (measured against operator's HA, ~3,500 entities)

| Path | p50 | p95 | Notes |
|------|-----|-----|-------|
| Tier 1 hit + persona rewrite | ~600 ms | ~1 s | "what time is it" → 0.93s |
| Tier 1 miss → Tier 2 disambiguate | 5 s | 11 s | 14B disambig + WS call |
| Tier 1 garbage-speech reject → Tier 3 LLM | — | 10–30 s | unchanged from Stage 2 |
| Tier 2 falls through → Tier 3 LLM | — | 15–30 s | bad JSON / mixed domains / etc. |

### Side effects

1. **New pip dependencies**: `websockets>=13.0`, `rapidfuzz>=3.0.0`.
2. **New container env vars** (all optional, defaults work):
   - `DISAMBIGUATOR_OLLAMA_URL` (overrides cfg's autonomy URL)
   - `DISAMBIGUATOR_MODEL` (default `qwen2.5:14b-instruct-q4_K_M`)
   - `DISAMBIGUATOR_TIMEOUT_S` (default 25)
   - `REWRITER_MODEL` (default `qwen2.5:3b-instruct-q4_K_M`)
   - `REWRITER_TIMEOUT_S` (default 8)
3. **Two Ollama models loaded persistently on the autonomy box**:
   `qwen2.5:14b-instruct-q4_K_M` (8.6 GB, disambiguator) and
   `qwen2.5:3b-instruct-q4_K_M` (1.8 GB, rewriter). Operator should
   pull these before deploy: `ollama pull qwen2.5:3b-instruct-q4_K_M`.
4. **Audit log on disk**: `/app/logs/audit.jsonl` — JSONL, line-buffered,
   no rotation yet (30-day field reserved). Operator should logrotate
   externally.
5. **HA WebSocket connection is persistent**. Container reconnects on
   drop with exponential backoff capped at 30 s; `get_states` resync
   runs on every reconnect to bridge the gap.
6. **Tier 1 path bypasses the MCP tool loop entirely** for handled
   utterances. The MCP path still runs for fall-through (Tier 3) and
   anything that isn't a HA-recognized intent.
7. **Operator-tunable** `configs/disambiguation.yaml` (example shipped
   at `configs/disambiguation.example.yaml`) — naming convention,
   overhead synonyms, state-inference toggle, freshness budget,
   candidate limit.

### Known issues introduced or unsolved

1. **HA misclassifies state queries as `action_done`**. "Is the
   kitchen cabinet light on" comes back as `action_done` with speech
   "Turned on the lights" — HA's intent matcher gets it wrong on
   their side. Tier 1 honors HA's verdict; the rewriter restyles
   the wrong text. Workaround would be local query-vs-action
   detection before the HA call.
2. **`switch` entities pollute "lights" candidate filter**. Operator's
   Sonos exposes switch entities like `Sonos_Master Bedroom Crossfade`
   that fuzzy-match "bedroom lights" because of the room name. They
   appear in clarify lists. Possible fix: when user explicitly says
   "lights", restrict to `domain=light` only (drop `switch`).
3. **Some entities report success but state doesn't change** — seen
   on the lights test (master closet light, office wall wash
   reported `action_done` but no state change). 139/198 lights
   are in `unavailable` state; HA's conversation API silently
   accepts service calls against them. Needs post-execute state
   verification with retry/error.
4. **Conversation history not yet propagated**. Every utterance is
   processed in isolation; "All lights" after "turn off the whole
   house" doesn't inherit the verb context. Would need
   conversation_id pass-through from WebUI → api_wrapper → bridge.
5. **Phase 2 (MQTT peer bus) and Phase 3 (tests + safety hardening)
   not started.** Phase 2 brings NodeRed/Sonorium integration;
   Phase 3 brings the labeled-utterance test corpus and HA WS
   reconnect integration tests.

### Verified behaviors against the operator's house

- "what time is it" → Tier 1 hit, ~0.9 s, GLaDOS-voiced response
- "is the kitchen cabinet light on" → Tier 1 hit, ~0.8 s
- "turn off the bedroom lights" → Tier 1 miss → Tier 2 clarify,
  names specific candidates ("the master bedroom color bars, …")
- "turn off the kitchen cabinet light" → Tier 1 hit, ~0.8 s,
  state actually changes
- "turn on the kitchen cabinet light" → restores state correctly
- "Activate the evening scene" → Tier 2 execute,
  `scene.living_room_scene_evening` activated
- "Turn off the whole house" → Tier 2 execute, `light.turn_off`
  on both whole-house light groups (operator confirms house went
  dark even when ack timed out)
- "Activate the living room reading scene" → Tier 2 execute,
  `scene.living_room_scene_reading` activated, fresh GLaDOS speech
- No vocative labels ("test subject" etc.) appear in any post-fix
  response

---

## Change 9 — Neutral model + conversation persistence + memory review

**Date:** 2026-04-17 → 2026-04-18 (continuous session)
**Status:** Phases A–E backend complete; WebUI Memory tab UI deferred.
**Commits:** `cf4aed4` (A), `1bf4cbf` (B), `0a48386` (C), `2d96720` (D+E)

Goal: retire the custom `glados:latest` Modelfile so the container is
the sole source of GLaDOS persona, AND give the half-built memory
pipeline real teeth — durable conversation history, multi-turn
context, operator-tunable retention, and a review queue for auto-
extracted facts. All five phases of the approved plan landed; UI
panel for memory review is the only piece still pending.

### Phase A — Neutral-model foundation

- New `ModelOptionsConfig` on `PersonalityConfig` exposing
  temperature / top_p / num_ctx / repeat_penalty so persona strength
  can be tuned without code changes when running a base model
  (qwen2.5:14b-instruct-q4_K_M) instead of a Modelfile-tuned one.
- env-overrides-YAML pattern: `OLLAMA_TEMPERATURE`,
  `OLLAMA_TOP_P`, `OLLAMA_NUM_CTX`, `OLLAMA_REPEAT_PENALTY` win
  when set.
- `_stream_chat_sse` reads `cfg.personality.model_options` instead
  of the hardcoded `{"num_ctx": 16384}`.
- 8 new tests lock the contract.
- **Operator action**: change `Glados.llm_model` in
  `/app/configs/glados_config.yaml` from `"glados:latest"` to
  `"qwen2.5:14b-instruct-q4_K_M"`, restart, then
  `ollama rm glados:latest`. Phase A code already supports either
  model — the swap is operator-triggered.

### Phase B — SQLite-backed conversation persistence

- New `glados/core/conversation_db.py`: WAL-mode SQLite at
  `/app/data/conversation.db`. Schema versioning, indexed columns
  (`conversation_id`, `idx`, `ts`), per-message metadata (source,
  principal, tier, ha_conversation_id). Methods: append /
  append_many / replace_conversation / snapshot / messages_since /
  latest_ha_conversation_id / prune_before / disk_size_bytes.
  18 unit tests including concurrent writers + persistence round-trip.
- `ConversationStore` wrapped over the SQLite layer. Backward-
  compatible — existing callers passing no `db` get unchanged
  in-memory behavior. New optional kwargs on append* /
  replace_all: `source`, `principal`, `tier`, `ha_conversation_id`.
  New `load_from_db()` for startup hydration.
- `engine.py` opens the DB at init, hydrates last 200 messages,
  preserves Change 7 invariant (preprompt set in __init__ +
  load_from_db appends DB rows AFTER it; never duplicates the
  preprompt across restarts).
- `api_wrapper.py`: both Tier 1 paths (streaming + non-streaming)
  and the Tier 2 hit branches now call `_append_tier_exchange`
  after emitting. Without this, the engine's ConversationStore had
  no record of any device-control exchange — every chat-API call
  started from preprompt only. Fixed.
- HA `conversation_id` forward-propagation: bridge calls now use
  `_last_ha_conversation_id()` from the store so HA's own
  multi-turn context is preserved across utterances.
- 7 new integration tests including the failure case fix:
  "turn off the whole house" → "all lights" must not be processed
  in isolation.

### Phase C — Conversation retention sweeper

- `glados/autonomy/agents/retention_agent.py`: simple background
  thread (no LLM), runs hourly. Two policies stacked:
    1. age-based prune of messages older than
       `conversation_max_days` (default 30, hard-cap 180).
    2. size-based prune when DB exceeds `conversation_max_disk_mb`
       (default 500), oldest tier=3 chat first.
  Tier 1 / Tier 2 device-control rows are PROTECTED from age-based
  pruning by default — they're the operationally valuable audit
  trail and persist for the full hard-cap window. If the size cap
  forces a choice between deleting tier=1 audit and warning, the
  agent warns instead of silently nuking history.
- `MemoryConfig` gains `conversation_max_days`,
  `conversation_hard_cap_days`, `conversation_max_disk_mb`,
  `chromadb_max_disk_mb`, `retention_sweep_interval_s`.
  All operator-tunable; the future Memory tab will surface them.
- 6 new tests cover hard-cap clamping, tier protection, audit-
  preservation refusal under tight cap, status dict shape.

### Phase D — Passive memory review queue

- `MemoryStore` extended: `list_by_status()`, `get_by_id()`,
  `update()` for promote/demote/edit operations.
- `memory_writer.write_fact()` accepts new `review_status`
  parameter. Auto-derived from `source`:
    `explicit`/`compaction` → `approved` (RAG-eligible immediately)
    `passive` → `pending` (held for operator review)
- `MemoryContext.as_prompt()` over-fetches and client-side filters
  to `{approved, no-status}`. Pending and rejected facts are
  excluded from RAG. Legacy facts (pre-Phase D, no status field)
  are still returned so months of operator-curated memory don't
  silently disappear after upgrade.
- New WebUI endpoints (auth-protected):
    GET    /api/memory/list?limit=N&q=...
    GET    /api/memory/pending?limit=N
    POST   /api/memory/<id>/promote
    POST   /api/memory/<id>/demote
    POST   /api/memory/<id>/edit       (JSON body: document, importance, review_status)
    DELETE /api/memory/<id>
- 8 new tests cover write_fact status assignment, override,
  pending-excluded-from-RAG, legacy-still-included.
- WebUI Memory **tab** (HTML/JS) is deferred to a follow-up commit.
  Endpoints are usable via curl in the meantime.

### Phase E — Episodic TTL enforcement

- `episodic_ttl_hours` was a placeholder MemoryConfig field for a
  long time. Now actually enforced: every retention sweep deletes
  ChromaDB episodic entries older than the TTL. Semantic facts
  intentionally untouched (operator-curated, persist forever).
- engine.py wires the live MemoryStore into the already-running
  RetentionAgent after MemoryStore init.
- Scheduled cron-driven daily summarization is **not** in this
  change — the existing CompactionAgent already handles
  token-threshold trigger, which covers the primary need. Daily
  summary on a clock is a polish follow-up.

### Test coverage

Phases A–E added 47 new tests:

| File | Tests |
|---|---|
| tests/test_message_construction.py | 8 |
| tests/test_conversation_db.py | 18 |
| tests/test_multi_turn.py | 7 |
| tests/test_retention.py | 6 |
| tests/test_memory_review.py | 8 |

**157 tests pass total** (was 110 at end of Change 8).

### New env vars

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_TEMPERATURE` | `0.7` | Persona-strength tuning for neutral models |
| `OLLAMA_TOP_P` | `0.9` | "" |
| `OLLAMA_NUM_CTX` | `16384` | Override context window |
| `OLLAMA_REPEAT_PENALTY` | `1.1` | "" |
| `GLADOS_DATA` | `/app/data` | Conversation DB lives at `<dir>/conversation.db` |
| `GLADOS_CONVERSATION_ID` | `default` | Partition key for the conversation DB |

### Verified behaviors

- Container restart → prior turns visible (loaded from
  conversation.db on hydrate).
- Tier 1 exchange: persisted with `tier=1`, `source=webui_chat`,
  `ha_conversation_id=<HA's conv id>`. Subsequent turn passes that
  conv id back to HA, restoring HA's multi-turn context.
- `GET /api/memory/list` and `/pending` return JSON with empty
  rows on fresh deploy (no facts stored yet).
- New conversation.db file at `/app/data/conversation.db` plus
  WAL sidecars; permissions correct (uid 1000).

### Known follow-ups

1. **WebUI Memory tab** — endpoints exist; the operator-friendly
   panel for reviewing/promoting/rejecting pending facts still
   needs the HTML/JS work. Tracked.
2. **Scheduled daily summarization** — cron-driven background
   summarizer parked; the token-threshold compaction agent
   currently covers the primary need.
3. **Per-principal conversation_id** — the SQLite schema supports
   it; everything still uses the single `"default"` partition.
   When multi-user (or MQTT peer-bus) integration lands, switching
   is just a constructor argument away.

---

## Change 9.1 — Operator-side neutral-model swap (live)

**Date:** 2026-04-18 morning
**Status:** Complete and verified live.
**Commits:** none (operational change only — Ollama state + container
config edit + restart on the docker host).

Phase A code shipped in Change 9 supports running with a neutral base
model. This change actually executed that swap on the operator's
hardware:

1. Unloaded `glados:latest` from the AIBox interactive Ollama
   (`192.168.1.75:11434`) via `POST /api/generate {keep_alive: 0}`.
2. Loaded `qwen2.5:14b-instruct-q4_K_M` on the same Ollama with
   `keep_alive: -1` (persistent, year 2318 expiry). VRAM dropped
   from 11.55 GB (glados:latest) to 10.38 GB (base) — Modelfile
   SYSTEM/TEMPLATE overhead removed.
3. Edited `/app/configs/glados_config.yaml` on the docker host:
   - `Glados.llm_model: "glados:latest"` → `"qwen2.5:14b-instruct-q4_K_M"`
   - `Glados.autonomy.llm_model: "glados:latest"` → `"qwen2.5:14b-instruct-q4_K_M"`
   - Backup saved as `glados_config.yaml.bak.20260418_124635`
4. `docker compose restart glados`; healthy in ~10s.

Verification (live against operator's house):

| Path | Latency | Response |
|---|---|---|
| Tier 1 (rewriter on qwen2.5:3b, unchanged) | 0.85 s | *"The chronometer reports seven forty-six AM. A most mundane hour."* |
| Tier 1/3 (qwen2.5:14b in chat path, NEW) | 0.82 s | *"Thermostat laughter, at 7:46 AM. Quite predictable."* |

Persona intact in both. Container is now sole source of GLaDOS
personality for all paths. The `glados:latest` Modelfile image
remains in `/api/tags` for fallback — operator's call when to
`ollama rm` it.

---

## Change 10 — WebUI Phase 5: restructure + Memory tab + auto-discovery

**Date:** 2026-04-18
**Status:** Complete and live in production.

**Commits (in order, post-history-rewrite hashes):**

- `9f644cc` — Commit 1: backend endpoints + SSL FIELD_META cleanup + tests
- `e4fe05f` — Commit 2: sidebar restructure + default page → Chat
- `4947acb` — Commit 3: Memory page UI + dedup-with-reinforcement backend
- `b7f0e69` — Commit 4: service auto-discovery UI (Discover button + URL-blur)
- `c5f4ae0` — Commit 5: UX polish (toasts, engine-status, display font)
- `0758174` — Commit 6: docs

Note: an author-rewrite pass on 2026-04-18 (mailmap swap of
`Chris Kliewer <chris@denofsyn.com>` → `synssins <synssins@gmail.com>`
across all 71 commits) changed every hash on `main`. The hashes above
are post-rewrite; the earlier SESSION_STATE prompt mentioned pre-rewrite
hashes (`6984ac2` / `670e94f` / `eeca0ab` / `88a19a6` / `3c60aa4` /
`2630a34`) that no longer exist on origin.

**Plan file:** `C:\Users\Administrator\.claude\plans\mellow-purring-kitten.md`

### What landed

**Sidebar / routing.** Configuration is a hierarchical parent with
System, Global, Services, Speakers, Audio, Personality, SSL, Memory,
Raw YAML nested under it. System moved from a flat top-level tab into
Configuration > System (no content change; just relocation).
`navigateTo(key)` takes dotted keys (`chat`, `config.global`,
`config.memory`, etc.); legacy localStorage keys (`tts`, `chat`,
`control`, `config`) migrate on read. Default page is now Chat.

**Memory page (Configuration > Memory).** Four cards:
1. Memory configuration — radio toggle for `passive_default_status`
   (Approved = enters RAG immediately, Pending = manual review).
   Edits via `PUT /api/config/memory` preserving other fields.
2. Long-term facts — search + Add form + scrollable list (uses
   `GET /api/memory/list` with optional `q=...`).
3. Recent activity — sorts facts by `max(last_mentioned_at,
   written_at)`; top 10; reinforcement rows show "reinforced
   importance X → Y, mentions=N" and, when `last_mention_text`
   differs from the canonical document, offer "Update wording from
   latest mention" (operator opt-in; never silent).
4. Pending review — only rendered when
   `passive_default_status="pending"`; Approve / Edit / Reject on
   each row.

**Dedup-with-reinforcement.** New `MemoryConfig` fields:
`passive_default_status` (default `"approved"`),
`passive_dedup_threshold` (`0.30` cosine distance),
`passive_base_importance` (`0.5`),
`passive_reinforce_step` (`0.05`),
`passive_importance_cap` (`0.95`). `write_fact(source="passive",
review_status="approved")` queries existing approved rows first and,
on a match within the threshold, updates in place — bumps importance
(capped), increments `mention_count`, refreshes `last_mentioned_at`,
stores incoming text in `last_mention_text`. New metadata on every
write: `mention_count`, `last_mentioned_at`, `last_mention_text`,
`original_importance` (audit). `explicit` and `compaction` sources
never dedup; `pending` landings never dedup either — Phase D review
flow is preserved for operators who opt into it.

**Service auto-discovery.** New GET endpoints:
`/api/discover/ollama?url=` (proxies `/api/tags`),
`/api/discover/voices?url=` (proxies `/v1/voices`, accepts both
top-level list and OpenAI `{"data":[...]}` shapes), and
`/api/discover/health?url=` (reachability check; always returns HTTP
200 with `ok: true/false` + `latency_ms`). Wired into Services page:
each URL has a Discover button + a status pill; URL blur auto-fetches
(debounced 300 ms); results populate neighbouring model / voice
dropdowns. Current saved value is always retained as an `<option>`
so a stale or offline upstream never blanks the config.

**SSL deduplication.** Removed `ssl.domain` and `ssl.certbot_dir`
from `FIELD_META` — they were being rendered both on Global (via the
auto-form) and on the dedicated SSL page. SSL page is now the single
source of truth.

**UX polish.** Stackable toast notifications (auto-dismiss 4 s);
Major Mono Display heading font via a `--font-display` CSS variable
(body text stays system-ui); live engine status dot in the sidebar
brand header (polls `/api/status` every 30 s while the tab is
visible); confirm dialogs on destructive memory actions; service
health dots now route through `/api/discover/health` so CORS and
mixed-scheme issues are gone and latency is exposed as a tooltip.

**New/modified endpoints:**

| Verb   | Path                           | Purpose                              |
|--------|--------------------------------|--------------------------------------|
| GET    | `/api/discover/ollama?url=`    | List models from upstream Ollama     |
| GET    | `/api/discover/voices?url=`    | List voices from upstream Speaches   |
| GET    | `/api/discover/health?url=`    | Reachability + latency               |
| POST   | `/api/memory/add`              | Operator-curated long-term fact      |
| POST   | `/api/retention/sweep`         | Manually trigger RetentionAgent      |

**Tests:** 178 pass (was 157). +21 new:
`test_discover_endpoints.py` (9),
`test_memory_endpoints.py` (5),
`test_memory_dedup.py` (6),
plus a review-queue override test in `test_memory_review.py`.

### Caveats (by design)

- **Canonical wording:** first write establishes the canonical text.
  Reinforcement never silently rewrites. Operator can opt in per
  fact via the Recent Activity button.
- **No time decay:** frequently-mentioned facts grow to the 0.95 cap
  and stay there. Acceptable for v1; can add decay if RAG quality
  drifts.
- **Similarity threshold sensitivity:** 0.30 is conservative;
  too-loose values risk merging related-but-distinct facts.
  Operator-tunable via `MemoryConfig.passive_dedup_threshold`.

---

---
