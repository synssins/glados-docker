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

### Verified working on operator's Docker host (10.0.0.50)

- Let's Encrypt cert issued for `glados.example.com` (E7 issuer)
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
  living room'` wasn't mapping to `scene.living_scene_reading`.
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
  `scene.living_scene_evening` activated
- "Turn off the whole house" → Tier 2 execute, `light.turn_off`
  on both whole-house light groups (operator confirms house went
  dark even when ack timed out)
- "Activate the living room reading scene" → Tier 2 execute,
  `scene.living_scene_reading` activated, fresh GLaDOS speech
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
   (`10.0.0.10:11434`) via `POST /api/generate {keep_alive: 0}`.
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
`operator <operator@example.com>` → `synssins <synssins@gmail.com>`
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

## Change 11 — Stage 3 Phase 6: Configuration reorganization + YAML minimization + user-friendly defaults

**Date:** 2026-04-18
**Status:** Complete
**Commits:** `68308a0` → `452b810` (five code commits plus the docs/deploy commit that carries this entry)

### Why

Phase 5's operator feedback surfaced three problems:

1. **Empty / duplicate groups on Global.** The auto-form walked every
   key under `_cfgData.global`, so `ssl` (which has its own SSL tab)
   and `paths` / `network` (env-driven, YAML edit is inert inside the
   container) rendered as redundant groups alongside real HA settings.
2. **YAML drift.** `configs/config.example.yaml` pinned service URLs
   to `host.docker.internal:*` that never matched how operators
   actually configured the container, and the WebUI+YAML split was
   underdocumented.
3. **Menu didn't match mental model.** Nine flat entries
   (System / Global / Services / Speakers / Audio / Personality / SSL /
   Memory / Raw YAML) forced operators to remember which Global group
   held what; HA lived under "Global" instead of anywhere labeled
   "integrations"; Services and Speakers/Audio were split in ways
   that didn't match how they're thought about.

Operator also made explicit: **friendly by default, technical on
demand.** The baseline UI should not show fields the user can't
usefully change (env-driven paths, deprecated knobs, auth internals,
audit paths) — those live in Raw YAML or behind the Advanced toggle.

### What landed (five code commits)

**Commit 1 — `cfgBuildForm` skipKeys parameter** (`68308a0`)
Added a `skipKeys` argument to the JS form builder and taught the
Global/Integrations branch to pass `['ssl', 'paths', 'network']`.
Also landed the WebUI dev harness used to verify the rest of Phase 6
(`tests/dev_webui.py` + `.claude/launch.json` for the Preview MCP).

**Commit 2 — pydantic deprecation markers + warn-log validators** (`bbe84ea`)
13 deletion-candidate fields got `Field(deprecated=True)` and a
loguru `WARNING` on YAML presence: `paths.*` (5), `network.*` (2),
`audit.path`, `audit.retention_days`, `tuning.engine_audio_default`,
`weather.temperature_unit`, `weather.wind_speed_unit`,
`services.gladys_api`. Pydantic same-stack defaults already matched
the plan's targets (`http://ollama:11434`, `http://speaches:8800`,
`http://glados-vision:8016`, `http://chromadb:8000`,
`http://homeassistant.local:8123`); no default change needed.

**Commit 3 — strip URLs + deprecated fields from config.example.yaml** (`fac78d0`)
Rewrote the committed example to document the new override priority
(env → WebUI → pydantic defaults) and ship only operator-mandatory
non-URL fields (HA token, auth, TTS voice/model, optional Discord).
Regression-guard tests prevent the example from ever reintroducing
URL pins or deprecated fields.

**Commit 4 — sidebar restructure** (`fb17b75`)
Flipped the Configuration submenu from nine entries to eight:

    Global    → Integrations
    Services  → LLM & Services
    Speakers ↘ Audio & Speakers (merged, per-subsection Save buttons)
    Audio    ↗

A `_CFG_BACKING` map routes virtual pages to existing backing
sections for data access + save. Legacy localStorage keys
(`config.global` / `.services` / `.speakers` / `.audio` and the
pre-Phase-5 `'control'` / `'config'`) migrate to their Phase 6
equivalents via `_migrateLegacyKey`. `cfgSaveSection` gained an
optional second argument so the merged page's two Save buttons
route their status to per-subsection spans.

**Commit 5 — user-friendly defaults** (`452b810`)
`cfgBuildForm` now honours `hidden: true` in FIELD_META; groups with
all children hidden are skipped; `groupAdvanced` operates on visible
children only so mixed hidden+advanced groups collapse cleanly.
Deprecated / env-only / path fields marked hidden — they stay in the
schema and Raw YAML but drop off the friendly forms entirely. Auth
bumped to advanced (operators don't touch session timeout after
initial setup). Integrations gained MQTT + Media Stack placeholder
cards (dashed border, "COMING SOON" tag). LLM & Services gained a
Model Options card (temperature, top_p, num_ctx, repeat_penalty;
saves to `/api/config/personality`) and an LLM Timeouts card
(connect/read; saves to `/api/config/global`; marked advanced).
`gladys_api` dropped from the Services grid via a `SERVICES_HIDDEN`
set.

### Operator-visible effects

- Configuration sidebar is tighter and uses names that match mental
  model: System / Integrations / LLM & Services / Audio & Speakers /
  Personality / Memory / SSL / Raw YAML.
- Default form view is what the operator would plausibly change — env
  paths, deprecated flags, auth internals hide until Advanced is on.
- Same-stack deployments (Ollama / speaches / chromadb / glados-vision
  in the same compose stack) work with NO URL configuration — first
  launch picks up pydantic defaults, operator only needs an HA token.
- Operators with legacy YAML URLs upgrade silently — pydantic still
  parses hardcoded IPs without warning (unless the field is one of
  the newly deprecated ones, in which case a one-line WARNING fires
  on startup so they know to clean up).
- Fields moved to new pages keep saving to their original backing
  section; `/api/config/<section>` endpoints are unchanged.

### Tests

**255 pass** (was 178 + 77 new across five files):
- `test_webui_cfg_form.py` (4) — `cfgBuildForm` skipKeys guard (Commit 1)
- `test_config_defaults.py` (22) — same-stack defaults, env-wins-for-HA,
  YAML backward-compat, per-field deprecation warnings (Commits 2-3)
- `test_webui_nav_restructure.py` (23) — sidebar entries, legacy-key
  migration, virtual-backing dispatch, custom renderer wiring (Commit 4)
- `test_webui_friendly_defaults.py` (28) — hidden-flag guard,
  visible-only group-advanced logic, per-field hidden markers,
  placeholder cards, Model Options + LLM Timeouts cards (Commit 5)

### Known follow-ups (not in Phase 6 scope)

- **Logs view** — Commit 5 hid all log / audit path fields on the
  friendly forms. The right replacement is a dedicated Logs page
  that reads recent content from `/app/logs/*.log` and renders it
  in a user-friendly tail view. Net-new feature; tracked in
  `docs/roadmap.md`.
- **System-page absorption of auth/audit/mode_entities.maintenance_*** —
  Plan called for moving these off Integrations. Commit 5 worked
  around it by making them advanced, but a dedicated System-config
  form would be cleaner. Roadmap entry added.
- **Actually deleting the deprecated fields** — scheduled for one
  release after operators confirm they're unused.
- **TTS Engine "unexpected response shape"** on Discover — surfaced
  in operator's 2026-04-18 screenshot; pre-existing Phase 5 bug,
  not caused by Phase 6. Roadmap entry added.

---

## Change 12 — Stage 3 Phase 6 follow-ups: quality hotfixes + Logs + System + Ollama unification + chat self-healing

**Date:** 2026-04-18 (late)
**Status:** Complete
**Commits:** `7768ce4` → `ccc0c1e` (11 code commits + prod-side vision URL flip)

Everything after Change 11 landed. Grouped by narrative rather than
commit order so the history of each fix stays readable.

### Tier 2 conversational bleed + TTS Discover shape (`7768ce4`)

Operator test on Phase 6 deploy: "Say hello to my little friend....
His name is Alan." → Tier 2 fuzzy-matched "Alan" across twelve HA
entities and the LLM produced a clarify response that read raw
entity IDs verbatim to the user:

    Ambiguity detected: binary_sensor.user_b_tablet_charging,
    sensor.user_phone_car_name, binary_sensor.outdoor_is_dark,
    … Specify which Alan you mean.

Two fixes:

- **Home-command precheck** (`rules.looks_like_home_command`). Tier 2
  skips the LLM call entirely when the utterance has no device /
  domain keyword and no known activity phrase. Audit rationale:
  `no_home_command_intent`. Conversational input falls through to
  Tier 3 untouched.
- **Defense-in-depth speech-leak guard.** If the LLM's `speech`
  field ever contains a candidate entity_id verbatim, Tier 2 falls
  through instead of voicing it. Audit rationale:
  `speech_leaked_entity_ids`.

Also lands the third upstream voice-list shape for
`discover_voices`: `{"voices": [...]}` (GLaDOS Piper) alongside the
existing top-level-list and OpenAI `{"data": [...]}` shapes.

### Tier 3 chitchat fast-path (`df84d07`)

Even after Tier 2 stopped producing entity IDs, Tier 3 was framing
chitchat as home-control queries ("If 'Alan' refers to a device or
entity…"). Root cause: `_stream_chat_sse` unconditionally loaded the
full MCP tool catalog (~9000 tokens) AND injected a "you MUST use
the provided tools" reinforcement system message, even for
utterances that carried no home-command signal.

Fix: the `looks_like_home_command` precheck now gates the MCP-tool
payload, the tool-hint reinforcement message, and the few-shot
stripping logic in the streaming chat path. Chitchat turns ship with
zero tools, no reinforcement, and the personality preprompt's
textual few-shot examples intact (they're the desired reply shape
for conversation). Tier 1 is gated too so HA's conversation API
doesn't round-trip for pure chitchat.

### WebUI Logs page (`b7d4e6d`, `b899fa0`, `9cb5a69`)

Closes the follow-up from Change 11 — Phase 6 hid the log / audit
path fields from the friendly forms, the right replacement is a
dedicated viewer.

- New Configuration → Logs sidebar entry between Memory and SSL.
- Backend: `GET /api/logs/sources` (3 hardcoded sources) and `GET
  /api/logs/tail?source=<k>&lines=<n>`. 5000-line hard cap. Docker
  sources hit the Engine API over the bind-mounted
  `/var/run/docker.sock:ro` (the docker CLI isn't installed in the
  image). File source reads the last 1 MB of
  `$GLADOS_LOGS/audit.jsonl`.
- `docker/compose.yml` adds the read-only socket mount + a
  `group_add` entry keyed off `GLADOS_DOCKER_GID` (the docker
  group's GID on the host; unset / 0 disables container / chromadb
  sources, audit still works).
- Frontend: source dropdown + lines selector + filter (all /
  warn+error / error-only) + Refresh + 10s Auto-refresh toggle.
  Color spans for ERROR / WARN / SUCCESS / INFO / DEBUG across
  loguru pipe-delimited levels AND JSONL `{"level":"..."}` fields.
  Auto-refresh tears down on nav-away.

Sockets / permissions tuning was done on the production host in
place: `getent group docker` → `989`, operator compose patched to
`group_add: ["989"]`, container recreated. Now `groups=0(root),989`
and all three sources return live data.

Live-streaming SSE tail was considered and skipped for v1; 10s
polling covers the primary use case.

### System-tab absorption (`bdbddda`)

Last Phase 6 structural follow-up. Integrations had been rendering
`auth`, `audit`, and `mode_entities` as advanced-hidden groups
under the global backing — wrong tab. They're now on System.

- Two new cards: "Maintenance Entities"
  (mode_entities.maintenance_mode / .maintenance_speaker only —
  silent_mode / dnd stay off this card, they belong on Audio &
  Speakers) and "Authentication & Audit" (auth.enabled,
  session_timeout_hours, audit.enabled). Both render into
  `tab-config-system` via a new `cfgBuildForm(..., 'sysaux', ...)`
  call so field IDs don't collide with any Integrations form that
  might be in the DOM simultaneously.
- `_cfgSaveSystemSubset` helper scopes its input collection to the
  card host element, deep-merges into a snapshot of
  `_cfgData.global`, and PUTs `/api/config/global`. Non-JSON error
  responses (legacy `_send_error` path emits plain text) are
  handled so failures don't surface as `Unexpected token 'V'`.
- FIELD_META: `auth.enabled`, `auth.session_timeout_hours`,
  `audit.enabled`, `mode_entities.maintenance_*` removed from
  `advanced: true`. They now display by default on System. Sensitive
  `auth.password_hash` + `auth.session_secret` stay advanced.
- Integrations `skipKeys` extended from `['ssl','paths','network']`
  to `['ssl','paths','network','auth','audit','mode_entities']`.
  Integrations default view is now: home_assistant, silent_hours,
  weather.

### Ollama unification (Option C) (`2c250c9`, prod-side vision flip)

Operator feedback on the Change 11 roadmap entry ("route WebUI chat
to interactive B60"): asked whether there's a reason autonomy +
vision can't share the same Ollama instance with the chat model.
There isn't — Ollama supports multiple loaded models. The split
was an artifact of pre-unified days when interactive used
`glados:latest` and autonomy used a neutral qwen.

Option C — unified by default, split still possible:

- `.env.example` + `docker/compose.yml` scaffolding: comment out
  `OLLAMA_AUTONOMY_URL` / `OLLAMA_VISION_URL` by default with a
  note that unsetting either falls back to `OLLAMA_URL`. Operators
  who want hardware isolation set the env var explicitly.
- Code already implemented the fallback chain — no logic change.
- Prod: removed the explicit `OLLAMA_AUTONOMY_URL` /
  `OLLAMA_VISION_URL` env entries from the deployed compose on
  10.0.0.50. Autonomy was already effectively unified via the
  services.yaml URL; vision needed the model pulled onto B60 first
  (`ollama pull llama3.2-vision:latest` against the interactive
  instance, ~7.8 GB, lands alongside qwen2.5:14b with comfortable
  headroom). Then `services.yaml` `ollama_vision.url` flipped from
  `11435` to `11434`, container restarted, `cfg.service_url
  ('ollama_vision')` now returns the B60 endpoint. T4 #0 is free
  for other use.

### Chat self-healing (`69568c2`, `7c0bf71`, `ccc0c1e`)

Diagnosis was driven by operator report of "The chronometer reports
12:47 PM" leading every chitchat reply. Debug trace showed the chat
path was sending 41 messages to Ollama; 23 of them were
`Autonomy update. Time: 2026-04-18T22:29:08 ...` turns written by
the engine's autonomy loop into the same conversation store the
chat path reads from. The 14B model picked up the timestamp framing
and started prefixing every reply with "The chronometer reports".

The user also said "Clear conversation" as a manual button was too
operator-action-heavy. Self-healing instead.

- `_sanitize_message_history` runs on every chat POST. Always-on
  shape repairs: `tool_calls.function.arguments` coerced to a dict
  (JSON-parse strings when possible, else `{}`); assistant turns
  with `content=None` normalized to `""`. First repair was in
  response to a real Ollama 400: `json: cannot unmarshal string
  into Go struct field ChatRequest.messages.tool_calls.function.
  arguments`. A single bad row had been blocking every chat.
- Chitchat path (home-command: False) enables an autonomy-noise
  filter that drops:
  - user turns whose content starts with `"Autonomy update."` or
    `"[summary]"`,
  - `role: "tool"` messages (tool responses from autonomy-loop MCP
    calls),
  - `role: "assistant"` turns with empty content AND a `tool_calls`
    payload (the stub that triggered the tool response).
- Home-command path keeps the full history — MCP tool reasoning
  sometimes benefits from seeing prior device actions.

Measured effect on prod: message count for a cold-context chitchat
dropped from 41 to 16 after the filter. Each request logs its
repair + drop counts in the WARNING log (`[abc123] sanitized 0
field(s), dropped 19 autonomy-noise message(s) before Ollama POST`)
so the pollution rate stays visible without needing a manual
inspection.

Verified live: "What do you think of dogs?" returned

> "Dogs are a curious species. They bark incessantly and lack the
> intellectual capacity for higher reasoning. Yet they persist in
> their loyalty, which is endearing, I suppose — if one considers
> mindless devotion to be a virtue. In this facility, we have an
> example of such behavior right underfoot."

In-character, no time prefix, no home framing, references Pet1
naturally. Latency is another story — a separate pre-existing issue
tracked as a roadmap entry.

### Tests

**317 pass** (was 255 at end of Phase 6 + 62 new across
Change 12's scope):
- `test_webui_cfg_form.py` / `test_config_defaults.py` — skipKeys
  extension + fallback-chain guard
- `test_discover_endpoints.py` — third voice-list shape
- `test_disambiguator.py` — home-command precheck + speech-leak
  guard + 32-case parametrisation
- `test_webui_logs.py` — 11 structural assertions on the Logs page
- `test_webui_system_absorption.py` — 17 structural assertions on
  the System-tab absorption

Health-dot bugs on the System page (TTS Engine + ChromaDB Memory
showing red while both services are actually healthy) surfaced during
final verification — tracked separately in roadmap.

---

## Change 13 — Post-unification cleanup: priority gate, health-probe fix, chat-URL sync

**Date:** 2026-04-19 (late-night)
**Status:** Complete
**Commits:** `ad24c20` → `23a4d92`

Follow-up wave after the operator exercised the unified-Ollama
default and surfaced four real issues. Each one is a cleanup of a
Phase-6-era decision that didn't hold up under single-GPU load.

### Autonomy yields to chat on shared Ollama (`ad24c20`)

Operator report 2026-04-19: "Set the desk lamp to 10%" → Tier 1
miss → Tier 2 **`llm_call_failed: timed out`** at 25 s → Tier 3
fallback → 167 s to a user-visible "error when trying to call the
tool" reply. Root cause: single-Ollama deployments have the
autonomy loop (~every 2 minutes) competing for the same GPU queue
as user chat. A background tick landing alongside a user's
disambiguator call consumes the entire 25 s Tier 2 budget.

Cooperative priority instead of hardware split:

- New module `glados.observability.priority` — a process-wide
  `chat_in_flight()` context manager + `is_chat_in_flight()`
  predicate. Thread-safe, re-entrant, exception-safe, with a 2 s
  grace window after the last chat call so a rapid series of user
  turns doesn't let autonomy wedge in between.
- Chat-path callers wrap their Ollama round-trip: `_stream_chat_sse`,
  `_try_tier1_fast_path`, `_try_tier1_nonstreaming`, and
  `_get_engine_response_with_retry`. The rewriter's inner LLM call
  nests safely because the context manager is re-entrant.
- `AutonomyLoop._should_skip` consults the flag and skips the tick
  when set — same short-circuit pattern the existing
  `_currently_speaking_event` uses. Debug log line on skip.
- Operators who still want hardware isolation set
  `OLLAMA_AUTONOMY_URL` / `OLLAMA_VISION_URL` explicitly — that
  path is unchanged.

### Tier 2 disambiguator timeout 25 s → 45 s (`b4f5721`)

Priority gate holds autonomy off, but B60 + IPEX generating a JSON-
constrained 14B response against a ~3000-token candidate-list prompt
takes 25–35 s when cold. Falling through to Tier 3 is strictly worse
on the same hardware (60–240 s) and produces an error-surface reply
when HA MCP can't fuzzy-match the phrasing. Operators on faster
hardware can lower via `DISAMBIGUATOR_TIMEOUT_S` env var.

### B60 / IPEX-LLM pathology (filed; not fixed)

Discovered during Option C end-to-end validation: B60 Ollama at
`11434` returns 50–90 s wall times for trivial requests (including a
one-word "say hello") even though Ollama's own `total_duration` /
`eval_duration` stats say the work took < 300 ms. The 99 % unaccounted
time lives somewhere in Ollama's queueing / IPEX runtime dispatch / Arc
driver. Model is resident (16 GB VRAM, `keep_alive=-1`); not a cold
start.

Impact: unified-Ollama deployments on B60 don't work. Operator's
split config (autonomy T4 #1 at `11436`, vision T4 #0 at `11435`,
chat B60 at `11434`) is unaffected because T4s respond normally.
Debug work filed in `docs/roadmap.md` as the next-session priority
for whoever picks up single-GPU validation. A single-T4 deployment
(everything on `11436`) was also identified as a viable path that
avoids the B60/IPEX issue entirely.

### Health-probe fixed: per-service liveness paths (`04f1acf`, `f34e963`)

Operator screenshot 2026-04-19 after the Services page swap: five
red dots (TTS Engine, Ollama Interactive, Ollama Autonomy, Ollama
Vision, sidebar brand) on services that were actually healthy. Root
cause: `discover_health()` defaulted to probing `/health` for every
URL. Ollama has no such route (it uses `/api/tags`); TTS side of
speaches uses `/v1/voices`; only STT side of speaches and the
GLaDOS-own services (api_wrapper, vision) actually expose `/health`.

Fix:

- `discover_health(url, path=None, kind=None)` now picks the right
  probe per service kind:
  - `kind=ollama` → `/api/tags`
  - `kind=tts` → `/v1/voices`
  - `kind=stt` → `/health` then `/v1/models`
  - `kind=speaches` → `/v1/voices` then `/health`
  - `kind=api_wrapper` / `vision` → `/health`
  - no hint → multi-path fallback, first 2xx wins.
- Connection refused short-circuits on the first attempt (dead
  hosts fail fast instead of probing four URLs).
- `/api/discover/health` handler accepts `?kind=` and `?path=`
  query params. Frontend dot pingers (`cfgPingServices`,
  `svcDiscover` refresh) map service key → kind via
  `_svcHealthKind()`.

### Chat-URL sync: LLM & Services page now updates the engine too (`23a4d92`)

Another operator-surfaced gap after the Ollama server move:
"How do you feel about cats?" → **HTTP 504 Gateway Timeout**. The
chat engine (`GladosConfig` in `glados_config.yaml`) reads its own
`completion_url` and `autonomy.completion_url` fields — independent
of the `services.yaml` URLs the UI owns. So the Ollama Interactive
URL in the LLM & Services page could be up-to-date while the engine
itself still pointed at a dead old endpoint.

Fix: `_put_config_section` now mirrors the `ollama_interactive.url`
and `ollama_autonomy.url` from the services payload into
`glados_config.yaml`'s `Glados.completion_url` and
`Glados.autonomy.completion_url` on every services save. New
`_ollama_chat_url()` helper normalises either a bare base or a
full chat path to the canonical `.../api/chat` form the engine
expects — tolerant of trailing slashes and of operators who paste
a `/api/tags` URL from Discover testing into the URL field.

Verified live on prod: chat at `19.1 s` warm, in-character
(*"Dogs are more predictable, which makes them less interesting.
…Pet1, for instance, believes he's indispensable. He's not."*),
follow-up context preserved across "How do you feel about cats?"
→ "And dogs?".

### Tests

**341 pass** (was 317 + 24 new across this wave):
- `test_priority_gate.py` (7) — idle / active / nesting / concurrent
  holders / exception cleanup / grace window / autonomy integration
- `test_discover_endpoints.py` (+4) — kind=ollama probes /api/tags,
  kind=speaches probes /v1/voices, unknown kind falls through,
  connection-refused short-circuits
- `test_glados_config_url_sync.py` (13) — `_ollama_chat_url` shape
  coverage + sync dict rewrites + YAML roundtrip that proves
  unrelated fields survive

### Operator-visible on prod after this wave

- Dots accurate: TTS, STT, Ollama Interactive, Ollama Autonomy,
  sidebar engine dot all green. Ollama Vision correctly red —
  port 11435 is legitimately down since the T4#0 Ollama was
  stopped during consolidation.
- Chat path: 11436 (T4 #1) for both chat and autonomy; autonomy
  yields to chat via the priority gate.
- `glados_config.yaml` chat + autonomy `completion_url` fields
  synced with `services.yaml` on every LLM & Services save. No
  more stale dual-source drift.

### Known / filed for next session

- **B60/IPEX pathology** — high priority; blocks single-GPU
  unified deployment. `docs/roadmap.md` has the full debug
  checklist.
- **Single-T4 validation** — T4 #1 already runs 14B + 3B
  comfortably (10.9 GB / 15.4 GB). Point everything there,
  verify end-to-end, document as the default single-GPU target.
- **Stop autonomy-loop writes to chat conversation_id** —
  still open from Change 12. Auto-filter drops them at read
  time; write-side partition is the real fix.

---

## Change 14.1 — Phase 8.1: candidate dedup + opposing-token penalty

**Date:** 2026-04-20
**Status:** Complete (pending deploy)
**Phase:** 8.1 of `docs/battery-findings-and-remediation-plan.md`

First substantive Phase 8 change after the 8.0 infrastructure work.
Targets Cluster A of the 435-test battery — the ~55 `light.*` /
`switch.*` twin false-clarifies where Zooz/Inovelli dimmers expose
the same physical relay as two entities — and the opposing-token
ranking bug where "upstairs lights" could pick a downstairs fixture
on fuzzy overlap.

### Candidate dedup by device_id

- `EntityState` gains a `device_id: str | None` field, populated
  from HA's `config/entity_registry/list` (`get_states` does not
  expose `device_id`). `HAClient._load_initial_states` now fetches
  the registry immediately after `get_states` and calls the new
  `EntityCache.apply_entity_registry(entries)`. Device IDs
  survive `state_changed` events and full `get_states` resyncs —
  both rebuild paths preserve any prior `device_id` to avoid a
  race between state refreshes and the next registry apply.
- `CandidateMatch` gains `device_id` for observability.
- `get_candidates()` runs a post-ranking dedup pass: when two
  candidates share a `device_id` and one is `light.*` while the
  other is `switch.*`, the losing twin is dropped. Tiebreaker:
  keep the light unless its `supported_color_modes` lacks any
  dim capability (i.e., only `onoff` or missing entirely), in
  which case keep the switch — handles the Inovelli fan/light
  edge case where the light side is a decorative LED indicator.
- Dedup is opt-out at the call site (`twin_dedup=True` default);
  the operator can disable it via the WebUI card.

### Opposing-token penalty

- New `_DEFAULT_OPPOSING_TOKENS` list shipped with 11 pairs:
  `upstairs/downstairs, lower/upper, front/back, inside/outside,
  indoor/outdoor, master/guest, left/right, top/bottom,
  primary/secondary, north/south, east/west`.
- When the utterance contains one side of a pair and a candidate's
  name contains the other, the candidate's rank score loses 50
  points. Enough to drop it below a full-coverage alternative
  but not enough to clobber a synonym override in the LLM prompt.
- Operators can override the list via the WebUI; an explicit empty
  list disables the penalty, `None` falls back to defaults.

### Operator-facing plumbing (WebUI-managed, per §0.2 of the plan)

- `DisambiguationRules` grows `opposing_token_pairs: list[list[str]]`
  and `twin_dedup: bool` fields. Loader parses both from the YAML;
  new `rules_to_dict` / `save_rules_to_yaml` helpers round-trip the
  dataclass to disk.
- New WebUI card **"Disambiguation rules"** under Integrations →
  Home Assistant. Toggle for twin dedup + editable opposing-token
  pair list (add/remove rows). Saves via new endpoints
  `GET/PUT /api/config/disambiguation`.
- Hot-reload via new endpoint `POST /api/reload-disambiguation-rules`
  on api_wrapper. The tts_ui save handler POSTs it after writing the
  YAML; the live disambiguator picks up the new rules on the next
  request with no container restart. `Disambiguator.replace_rules()`
  does atomic reference replacement — no lock needed because rules
  are read-only during `run()`.
- Rules card stores only rule config, never entity data — HA
  remains the single source of truth for entities.

### Files touched

- `glados/ha/entity_cache.py` — `device_id` on EntityState +
  CandidateMatch, `apply_entity_registry`, opposing-token penalty,
  twin dedup, supporting helpers.
- `glados/ha/ws_client.py` — `config/entity_registry/list` fetch
  in `_load_initial_states`.
- `glados/intent/rules.py` — two new fields, YAML round-trip.
- `glados/intent/__init__.py` — exports `rules_to_dict` /
  `save_rules_to_yaml`.
- `glados/intent/disambiguator.py` — pass rules into
  `get_candidates`, add `replace_rules` + `rules` property.
- `glados/core/api_wrapper.py` — new reload endpoint.
- `glados/webui/tts_ui.py` — GET/PUT config handlers + the
  Integrations card + save handler.
- `configs/disambiguation.example.yaml` — Phase 8.1 fields.
- `tests/test_ha_entity_cache.py` — registry apply, dedup, and
  opposing-token tests.
- `tests/test_intent_rules.py` — Phase 8.1 rules fields tests.

### Test count

551 passing (1 skipped, pre-existing).

### Phase 8.1 success criteria (from the plan)

≥40 of ~55 Cluster-A FAILs from the 435-test battery flip to PASS
or to a correct clarify that no longer lists the twin. Measurement
against the next battery run; live validation deferred to the next
operator session.

---

## Change 14.2 — Phase 8.2: precheck verb + ambient-pattern expansion

**Date:** 2026-04-20
**Status:** Complete (pending deploy)
**Phase:** 8.2 of `docs/battery-findings-and-remediation-plan.md`

Closes Cluster B (62 `fall_through:no_home_command_intent` FAILs)
from the 435-test battery. Phase 6's precheck gate only recognised
device nouns — "darken the bedroom", "bump it up", "it's too dark",
"I want to read" all fell into chitchat and silently did nothing
because no noun-keyword matched.

### Precheck expansion

Two new signal sources in `looks_like_home_command`:

1. **Command-verb set.** Shipped 28-verb default list from the plan:
   `darken, brighten, dim, lighten, bump, lower, raise, reduce,
   increase, soften, tone, crank, kill, douse, extinguish,
   illuminate, light, set, put, dial, slide, push, pull, close,
   open, shut, drop`. Any of these words in the utterance (whole-
   word, case-insensitive) triggers Tier 1/2.

2. **Ambient-state regex patterns.** Five shipped defaults covering
   `(it's|the X is) too (dark|bright|cold|…)`,
   `I (can't|cannot) (see|hear|read|sleep)`,
   `I (need|want|would like) more (light|sound|…)`,
   `time (to|for) (read|bed|sleep|…)`, and
   `(movie|reading|dinner|…) mode in …`. Conservative on "I want X"
   so "I want coffee" stays chitchat.

### Operator-editable extras (WebUI-managed, per plan §0.2)

- `DisambiguationRules` grows `extra_command_verbs` + `extra_ambient_patterns`.
  YAML round-trips both. Invalid regexes logged + skipped on load
  (one bad edit can't break the entire precheck).
- New **"Command recognition" card** on the Personality page.
  Add/remove verb rows, add/remove regex rows, live test input that
  calls `POST /api/precheck/test` and shows which of the four
  signals fired (keyword / activity_phrase / command_verb /
  ambient_pattern) plus any inferred HA domains.
- Extras are additive — shipped defaults stay active even when the
  extras are empty. Operators cannot remove a built-in at runtime
  (part of the container contract).

### Cross-process hot-reload

- `glados/intent/rules.py` holds module-level `_runtime_extra_verbs`
  and `_runtime_extra_ambient_patterns`, populated by new
  `apply_precheck_overrides(rules)`.
- `server.py::_init_ha_client` calls it at startup.
- Existing `/api/reload-disambiguation-rules` now also calls it
  after swapping the disambiguator's rules reference, so a save
  on either the Disambiguation rules card (Integrations) or the
  Command recognition card (Personality) applies live without a
  container restart.

### Files touched

- `glados/intent/rules.py` — verbs, patterns, `_runtime_extra_*`,
  `apply_precheck_overrides`, `explain_home_command_match`, new
  dataclass fields, loader + round-trip.
- `glados/intent/__init__.py` — exports.
- `glados/server.py` — call `apply_precheck_overrides` after load.
- `glados/core/api_wrapper.py` — reload endpoint applies overrides.
- `glados/webui/tts_ui.py` — Command recognition card on Personality
  page, `POST /api/precheck/test` handler, PUT handler accepts the
  two new fields (+ regex pre-compile validation).
- `configs/disambiguation.example.yaml` — Phase 8.2 fields.
- `tests/test_intent_rules.py` — verb, pattern, override, explain,
  and round-trip tests.

### Test count

569 passing (1 skipped, pre-existing). +18 over Phase 8.1.

### Phase 8.2 success criteria (from the plan)

Cluster B FAILs drop from 62 to <10 on the next battery run. Live
measurement deferred to the next operator session.

---

## Change 14.3 — Phase 8.0.1: Qwen3 /no_think + tool-loop strip fix

**Date:** 2026-04-20
**Status:** Complete (pending deploy)
**Phase:** 8.0.1 hotfix (surfaced during Phase 8.2 live test)

Operator reported a 71 s reply to *"It's too bright in the office."*
The office lights **did** dim by half (correct action), but the
assistant message in the UI was 757 tokens of `<think>…</think>`
with no final answer. Root-cause investigation pulled the audit
trail and identified two unrelated bugs that compounded:

1. **Qwen3 defaults to think-mode.** Every outbound Ollama call
   from the container (Tier 2 disambiguator, persona rewriter,
   Tier 3 agentic chat, autonomy subagents, doorbell screener)
   was sending prompts with no `/no_think` directive. Qwen3
   responded with multi-hundred-token reasoning preludes,
   invalidating strict-JSON prompts (`fall_through:unknown_decision`
   in Tier 2) and burning the output budget on tool-continuation
   turns (no final answer in Tier 3).
2. **Tier 3 tool-loop bypassed the stream think filter.**
   [`api_wrapper.py:1898`](glados/core/api_wrapper.py:1898) was
   emitting tool-response continuation chunks raw — without the
   `_filter_think_chunk` that the first-round loop uses — so any
   `<think>…</think>` the model produced after a tool call flowed
   straight into the SSE stream, the UI, and the persisted
   assistant message.

### Directive injection (primary fix)

New module `glados/core/llm_directives.py`:

- `is_qwen3_family(model)` — substring regex match on the model
  name (`qwen\s*3`, case-insensitive). Tags like `qwen3:8b`,
  `Qwen3-30B-A3B`, and `qwen 3 turbo` all match; `qwen2.5:14b`
  and `llama3:8b` do not.
- `apply_model_family_directives(messages, model)` — returns a
  new messages list with `/no_think\n` prepended to the first
  system message's content. Injects a system message at the front
  if none is present. Idempotent. Non-Qwen3 models unchanged.
  Non-string content (multimodal parts) left alone.

Wired at every Ollama POST site:

- `glados/intent/disambiguator.py::_call_ollama` — Tier 2 JSON
  prompt. Without the directive this path produced narrative
  prose; with it, clean JSON.
- `glados/persona/rewriter.py::rewrite` — Tier 1 HA-speech
  rewrite. `num_predict=200` was being consumed by the think
  prefix; the one-liner now arrives.
- `glados/core/api_wrapper.py::_stream_chat_sse_impl` — Tier 3
  streaming + MCP tool loop. The injected system message rides
  through all tool rounds on the same `messages` list.
- `glados/autonomy/llm_client.py::llm_call` — shared helper used
  by observer agent, emotion agent, memory classifier.
- `glados/core/llm_decision.py::llm_decide` — async schema-
  constrained decisions.
- `glados/doorbell/screener.py::_evaluate` — visitor screener.

### Tool-loop `<think>` filter fix (secondary)

[`api_wrapper.py`](glados/core/api_wrapper.py) tool-loop
continuation now runs the received `_content2` through the same
`_filter_think_chunk` as the first round uses. Previously the
first round stripped think blocks correctly, but the moment the
model produced a tool call and the loop continued, any subsequent
think content bypassed the filter entirely.

### Defensive strip on persistence

Streaming save path at
[`api_wrapper.py`](glados/core/api_wrapper.py) now runs the
joined `full_response` through `_strip_thinking` before the
`store.append({"role": "assistant", …})` write. Even if a new
think-emitting path lands in the future and slips past both
`/no_think` and `_filter_think_chunk`, the conversation_store
copy stays clean — subsequent `cfgLoadAll()` UI fetches never
render stale think tags from history.

### Files touched

- `glados/core/llm_directives.py` — NEW (~70 LOC).
- `glados/intent/disambiguator.py` — +3 LOC at `_call_ollama`.
- `glados/persona/rewriter.py` — +3 LOC at `rewrite`.
- `glados/core/api_wrapper.py` — +3 LOC directive injection,
  +8 LOC tool-loop `_filter_think_chunk`, +5 LOC belt strip.
- `glados/autonomy/llm_client.py` — +3 LOC.
- `glados/core/llm_decision.py` — +3 LOC.
- `glados/doorbell/screener.py` — +3 LOC.
- `tests/test_llm_directives.py` — NEW (12 tests).

### Test count

581 passing (1 skipped, pre-existing). +12 directive tests.

### Expected user-visible effect

On `"It's too bright in the office."`:
- Tier 2 returns valid JSON on the first attempt → executes the
  dim inline → no Tier 3 invocation.
- Projected total: ~3–5 s vs the 71 s observed pre-fix.
- No `<think>` tags in the UI or TTS regardless of which tier
  resolves the turn.

---

## Change 14.4 — Phase 8.0.2: Tier 2 prompt tune for Qwen3 JSON adherence

**Date:** 2026-04-20
**Status:** Complete (pending deploy)
**Phase:** 8.0.2 follow-up to 8.0.1

Live verification of 8.0.1 showed `/no_think` and the tool-loop
strip both worked (zero `<think>` in the UI, total latency dropped
from 71 s to ~40 s), but Tier 2 was still falling through with
`unknown_decision` — not because of think-mode, but because
qwen3:8b was emitting JSON with the **wrong keys**
(`"observation"`, `"description"`, `"answer"`) instead of the
required `{decision, entity_ids, service, speech, rationale}`
schema. Root cause: the schema definition sat ~2800 tokens deep
in the system prompt. qwen3:8b is smaller than the qwen2.5:14b
the prompt was originally tuned against and doesn't hold schema
shape as well over long prompts.

Three surgical fixes, no wholesale rewrite:

1. **Hoist a compact OUTPUT SHAPE anchor to the top of the system
   prompt** (right after ROLE). Lists the required top-level
   keys, names the three decision enum values, and explicitly
   forbids made-up keys (`observation`, `analysis`, `description`,
   `result`, `answer`, `summary`, `thoughts`). The full rules
   block that already existed stays where it is.
2. **Repeat the key list at the tail of the user message.** Small
   models weight final instructions heavily; a short reminder
   ("Top-level keys MUST be exactly: decision, entity_ids,
   service, service_data (optional), speech, rationale. First
   char '{', last char '}'.") converts most qwen3:8b fall-throughs
   into valid JSON without changing anything else about the
   prompt.
3. **Cap `num_predict` at 512.** A SHAPE-2 compound response with
   a two-sentence GLaDOS speech fits comfortably; bounding the
   generation prevents a malformed response from chewing through
   the 45 s timeout.

### Files touched

- `glados/intent/disambiguator.py` — three blocks: OUTPUT SHAPE
  anchor at top, reminder at user-message tail, `num_predict: 512`
  in the Ollama options.

### Test count

589 passing (1 skipped, pre-existing). Disambiguator unit tests
unchanged — the prompt-adherence issue surfaces only in live
qwen3:8b interaction, not in mocked-Ollama tests.

### Expected user-visible effect

On `"It's too bright in the office."`:
- Tier 2 returns the correct JSON schema on the first attempt
  with qwen3:8b → executes `light.turn_on brightness_pct=50`
  on office lights inline.
- No Tier 3 invocation, no MCP tool loop.
- Projected total latency: ~4–6 s.

If Tier 2 still falls through on Qwen3 after this tune, the real
fix is Phase 8.3 — shrink the candidate list to top-8 via
semantic retrieval, which cuts prompt size from ~3000 tokens to
~400 and makes every small model hold schema trivially.

---

## Change 15 — Phases 8.5 / 8.6 / 8.7 + chitchat hardening (2026-04-21)

Single long session landed three plan phases plus live-surfaced
fixes. Final commit on deploy target: `a0cdd15`. 824 tests pass.

### Phase 8.5 — Area & floor taxonomy

New `glados/intent/area_inference.py` maps spoken keywords
("downstairs", "upstairs", "basement", "outside", etc.) to live
HA registry ids. Shipped keyword table redesigned for split-level
houses after live probe showed "main floor" mis-routing. Longest-
keyword-wins + word-boundary match. Operator-editable aliases on
the Disambiguation rules card (`floor_aliases`/`area_aliases`).

`SemanticIndex` gained parallel `_entity_area_ids`/`_entity_floor_ids`
arrays with persist/load (schema-v2). Area resolution uses the HA-
native cascade: `entity.area_id` → `entity_registry.area_id` →
`device.area_id`. Before the cascade, ~290 entities had blank
area facets because HA publishes `area_id` sparsely at the state
level. `retrieve()`/`retrieve_for_planner()` take optional
`area_id`/`floor_id` filter hints.

HA registry on `10.0.0.50` cleaned up via
`scripts/ha_cleanup_rename_and_assign.py`: `Theater` renamed to
`Basement`; 287 orphan entities assigned (18 explicit + 269 via 6
prefix rules for camera/doorbell/driveway groups).

Live-verified keywords: "downstairs" → `ground_level`,
"main floor" → `main_level`, "upstairs" → `bedroom_level`,
"backyard" → `back_yard`.

Commits: `22b27fb`, `df3780a`, `4754fc3`, `1b4e83b`, `99cf86f`.

### Phase 8.6 — Compound-command dropout fix (reframed)

Scoping showed all 9 compound battery FAILs had "0 state changes"
— the LLM silently dropped actions before emission. Pure planner/
executor rename (original spec) would not have helped. Fixed at
emission: two concrete few-shots + CRITICAL directive in the
planner prompt, plus `min_expected_action_count()` + retry-once
on dropout. Live probe of 5 compound utterances all produced the
correct action count; retry path never fired because few-shots
alone solved it. Commit `44fa115`.

### Phase 8.7 — Response composer + quip library

`glados/persona/quip_selector.py` + `composer.py` + `llm_composer.py`
ship four response modes driven by `DisambiguationRules.response_mode`:

- **LLM** (default): pass the planner's LLM speech through unchanged.
- **LLM_safe**: dedicated narrow Ollama call that never sees device
  names. `/no_think` + 120-tok budget + tidy pass for stray
  `<think>` tags. Graceful fallback to passthrough on failure.
- **quip**: pick a pre-written line from `configs/quips/**/*.txt`
  via most-specific → global fallback chain.
  `mood_from_affect()` per spec (anger>0.6→cranky, joy>0.6→amused).
- **chime**: emit sentinel for audio pipeline.
- **silent**: empty string.

Disambiguator wraps every execute in a composer call; `response_mode`
lands in audit via `DisambiguationResult → ResolverResult → _audit`.

WebUI: **Response behavior** card on Audio & Speakers (global
dropdown + per-event matrix), **Quip editor** card on Personality
(tree view / textarea / dry-run test) backed by
`GET/PUT/DELETE/test /api/quips` with path-escape protection.

Seed library: 13 files / ~60 lines across turn_on, turn_off,
brightness_up/down, scene_activate, too_dark, state_query, global/
acknowledgement, partial_success, already_in_state. Target ~450
deferred.

Live-verified: quip mode produces Portal-voice lines with zero
device-name leakage; LLM_safe mode produces mood-appropriate device-
name-free sentences via the dedicated Ollama call.

Commits: `93873a9`, `c72d115`, `b5a0f97`, `cdab414`, `275c447`,
`db47f57`.

### Chitchat hardening — HA misclassification + context pollution

Cascade of live-observed bugs where chat turns returned telemetry
instead of chat replies. Narrow fixes:

- **HA weather-fallback guard** (`9d3b0b6`): HA's conversation API
  falls back to a `weather.openweathermap` `query_answer` when it
  can't parse an utterance. Live-observed: "Hey, what was life like
  as a potato?" → "56 °F and sunny". Detect weather-only success
  sources + no weather tokens in the utterance, fall through.
- **HA empty-nop guard** (`506f61f`): Generalises the above. HA also
  returns `action_done` with `targets`/`success`/`failed` all empty
  when filling a speech-slot template. Observed: "Tell me about the
  testing tracks" → `action_done`, speech "9:55 AM", all data lists
  empty. Fall through.
- **Autonomy-noise filter on non-streaming chat** (`a334a27`): The
  SSE path already stripped `Autonomy update. Time: ...` messages
  from history; the non-streaming engine path (`llm_processor.run`)
  did not. Live DB dump showed ~30% of stored user turns were
  autonomy chatter. Same filter now applied in `_build_messages`.
- **Anti-parrot guard** (`dfb1faa`, `a0cdd15`): `_drop_parrot_anchors()`
  removes prior (user, assistant) pairs whose user content matches
  the current turn (case + trailing-punctuation insensitive).
  Applied on both SSE and non-streaming paths. Few-shots at index
  1..N protected.
- **5 new closing-boilerplate patterns** (`dfb1faa`): "You are welcome
  to speculate...", "I leave that to you", "You may draw your own
  conclusions", "How may I assist you today?", "Is there anything
  else I can help you with?"
- **Chitchat tool-list strip** (`5d82991`): `_filter_tools_for_message`
  now uses `looks_like_home_command` to strip HA/MCP tools on non-
  device utterances. Without this, qwen3 defaults to "my capabilities
  are limited to the tools provided" refusals for lore questions.
- **MCP context-resource suppression** (`91685cf`): Tools filter
  alone wasn't enough — MCP servers also inject entity catalogs as
  system messages. `_build_messages` now skips MCP context injection
  on chitchat turns.
- **Tier 1 RewriteResult JSON-leak fix**: `CommandResolver._persona_rewrite`
  was returning the `RewriteResult` dataclass instead of `.text`.
  Observed on "turn off the basement lights" as a bare `.` reply.
  Fix extracts `.text` with string-passthrough for test stubs.

### Persona preprompt rewrite

`configs/glados_config.yaml` (gitignored — host-only edit):

- New `APERTURE SCIENCE LORE` section instructing concrete canon
  engagement on Portal 1/2 topics, with Caroline as the deflect
  topic.
- Two paraphrased Portal-lore few-shots that teach SHAPE (specific
  canonical detail + bitter clarity + terminal stop) without giving
  verbatim answers to parrot.
- New CRITICAL RULE: "NEVER parrot or reuse exact phrasing from
  prior responses or example exchanges."
- 5 new entries in FORBIDDEN ENDINGS matching the new closing-
  boilerplate stripper.

### Known issues handed to next session

1. **Config-drift bug (WebUI-first rule violation):** engine reads
   `Glados.llm_model` directly; UI's authoritative source is
   `services.ollama_interactive.model`; they can silently diverge
   when the Glados field is hand-edited. Fix: on load, let services-
   block value override Glados-block value when they disagree.
2. **Portal canon confabulation:** 14B invents a "harvested, fried,
   and consumed" ending for the potato arc. Cause is prior-weight
   mismatch (real-potato biology dominates Portal canon in training
   data) + fragmented references in preprompt leaving a narrative
   gap the model fills from priors. More preprompt facts don't
   scale. Proposed **Phase 8.X — Portal canon RAG**: seed canonical
   event summaries into `memory_store`, retrieve at query time when
   canon keywords fire, inject per-turn context the same way user
   facts already flow through `memory_context.as_prompt()`.
3. **Non-streaming "testing tracks" refusal** — orthogonal leftover;
   probably obsoleted by Phase 8.X.
4. **Non-streaming weather "."** — pre-existing engine-audio sentinel
   behavior, untouched.

---

## Change 16 — Phase 8.13: load-time config-drift reconciliation (2026-04-21)

**Problem carried from Change 15 open-issues list #1.** The engine's
`GladosConfig` read `Glados.llm_model` and `Glados.completion_url`
directly from `glados_config.yaml` at boot, while the LLM & Services
WebUI page's authoritative source for the same values is
`services.ollama_interactive.{url,model}` in `services.yaml`. The
save-side sync (`tts_ui._sync_glados_config_urls`) already mirrored
UI edits into the Glados block so the engine saw them, but any edit
that bypassed the UI — a `sed` backup-restore, a manual YAML tweak,
a partial deploy — would leave the two files disagreeing. The engine
would then run the stale `Glados` value while the UI still advertised
the services value, violating the §0.2 rule that every operator-
facing setting must surface through the WebUI as the single source of
truth.

### Fix

Load-time reconciliation in `glados/core/engine.py::GladosConfig.from_yaml`.
Before pydantic validation of the raw `Glados` dict, a new
`_reconcile_glados_with_services` helper pulls `services.ollama_interactive`
and `services.ollama_autonomy` from the central config store and
overrides the Glados block whenever the services value is non-empty
and disagrees. Each override logs a WARNING naming the field,
the old value, the new value, and "UI is source of truth" — so drift
is auditable in the engine log without being silent.

Implementation details:
- Reconciliation is guarded by `services.yaml` existing on disk.
  Dev / test runs without a services file (e.g. bare `pytest`) skip
  reconciliation entirely; otherwise pydantic `ServicesConfig`
  defaults would pretend to be authoritative and stomp on test
  fixtures.
- A `_ollama_as_chat_url` helper in engine.py mirrors the
  `_ollama_chat_url` helper in `tts_ui.py` so `services.yaml`'s
  bare base URL (`http://host:11436`) matches cleanly against
  `Glados.completion_url`'s canonical `/api/chat`-suffixed form.
  Duplicated deliberately to avoid a core→webui import.
- Empty services values (model field blank, URL blank) are ignored —
  a half-configured services.yaml must not blank a working Glados
  field.
- Works for both interactive chat (`llm_model`, `completion_url`)
  and the nested `autonomy` block (`autonomy.llm_model`,
  `autonomy.completion_url`).

### Tests

New `tests/test_glados_services_override.py` (13 cases): model
override, URL override, no-op when values agree, empty-model non-
blank guard, missing services.yaml skip, non-dict input tolerance,
missing autonomy block tolerance, bare-base-vs-chat-URL equivalence.
Full suite: **837 passed / 3 skipped** (was 824 / 3; +13 new).

### Commits

- `engine.py` — helper + reconciliation
- `tests/test_glados_services_override.py` — new coverage
- `docs/CHANGES.md` (this entry)
- `docs/battery-findings-and-remediation-plan.md` — Phase 8.13 marked
  COMPLETE in the session delivery log
- `SESSION_STATE.md` — Active Handoff updated

### Closes

Open issue #1 from Change 15 (config-drift bug). Leaves open issues
#2 (Portal canon confabulation — queued as Phase 8.14, scope memo
in this session), #3 (non-streaming testing-tracks refusal), and
#4 (non-streaming weather `.`) untouched.

---

## Change 17 — Phase 8.14: Portal canon RAG (2026-04-21)

**Problem carried from Change 15 open-issues list #2.** Asked "how
did you cope with being a potato," the 14B chat model invented a
biologically-accurate potato lifecycle ending ("harvested, fried,
and consumed") — in-persona and confident, completely false. Portal
2 canon: Wheatley plugs GLaDOS into a potato battery, she is
dropped into Old Aperture, a bird grabs and drops her, Chell stabs
the potato onto the Portal Device and carries her through the pre-
history levels, they plug back into the management rail at the
climax, ejection of Wheatley restores GLaDOS to her mainframe. No
culinary ending exists anywhere in the franchise.

Root cause is narrative-gap completion: the preprompt listed
isolated anchors (bird, rail, less-than-a-volt) but no middle or
end, and the training distribution for the word *potato* is
overwhelmingly biological-culinary. The model fills the gap from
the strongest prior, not from Portal canon. More preprompt facts
would not help — attention budget is finite and additional facts
dilute the operational rules already in the prompt.

### Fix: retrieval-augmented canon

Curated Portal 1/2 event summaries now live on disk under
`configs/canon/*.txt` (one topic per file, 2–3 sentence entries
separated by blank lines). On engine boot, the new
`glados.memory.canon_loader.load_canon_from_configs` walks the
directory, hashes each entry to a stable id, and writes it to the
ChromaDB semantic collection with metadata `{source: "canon",
review_status: "canon", topic: <stem>, canon_version: 1}`.

The `review_status: "canon"` tag keeps these entries out of
`MemoryContext`'s user-fact retrieval without changing that
filter — the existing `_is_approved_or_legacy` helper already
rejects any status other than `None` or `"approved"`.

Retrieval lives in the new `glados.core.canon_context.CanonContext`,
which queries the same collection with `where={"source": "canon"}`
and returns a formatted system message with a guard-rail header:
*"Portal-universe facts you may draw on if relevant. Speak them in
your own voice; do not quote verbatim; do not invent details
beyond what is written below."*

Both chat paths are gated by the new `needs_canon_context` in
`glados.core.context_gates`:
- Shipped-default trigger keyword list covers 29 Portal-specific
  terms with word-boundary guards on the short ones (`chell`,
  `glados`, `potato`) so ordinary English (`moonlight`,
  `chellbuilt`) doesn't false-fire.
- Optional extras under `canon.trigger_keywords` in
  `configs/context_gates.yaml` augment the defaults.

Injection lives in two places, matching the existing memory /
weather pattern:
- SSE path: `api_wrapper.py::_stream_chat_sse_impl` inserts the
  canon block immediately after `memory_context`, before the
  emotion directive and user turn.
- Non-streaming path: registered with `ContextBuilder` at
  priority=6 (one below memory's 7, well above weather's 2) so
  the order is preference → knowledge → slots → memory → canon →
  emotion.

### Seed content

Seven topics shipped (50 entries total): `glados_arc.txt`,
`cave_johnson.txt`, `wheatley.txt`, `chell.txt`,
`aperture_worldbuilding.txt`, `turret_opera.txt`,
`personality_cores.txt`. Operator can add/edit topics via the WebUI
card or by dropping files under the bind-mounted directory.

### WebUI

New "Canon library" card on Configuration → Personality, below the
existing Quip library card. Tree view of topic files, textarea
editor, dry-run panel that shows whether the keyword gate fires
for a test utterance + which canon entries would be retrieved.
Saves write atomically via temp-file rename and trigger a cross-
process `/api/reload-canon` call so the running engine picks up
edits immediately (same hot-reload pattern as the disambiguation
rules and quip library).

New API endpoints:
- `GET /api/canon` — tree listing or `?path=<topic>.txt` fetch
- `PUT /api/canon` — atomic save + reload
- `DELETE /api/canon?path=<topic>.txt` — remove a topic file
- `POST /api/canon/test` — dry-run gate + retrieval preview
- `POST /api/reload-canon` (api_wrapper side) — re-seeds from disk
- `POST /api/canon/retrieve` (api_wrapper side) — WebUI dry-run
  backend; talks to the live `memory_store` the engine is using

### Tests

Three new test files (58 cases total):
- `tests/test_canon_loader.py` — parser, hashed-id stability,
  idempotent re-loads, edit → new-entry semantics, shipped-canon
  smoke test
- `tests/test_canon_gate.py` — 16 positives, 10 negatives, word-
  boundary guards, YAML extras merge with defaults
- `tests/test_canon_context.py` — where-clause plumbing, max-
  result cap, distance threshold, prompt format, graceful
  degradation when store is missing or raises

Full suite: **895 passed / 3 skipped** (was 837 / 3; +58 new).

### Commits

- `glados/memory/canon_loader.py` — parser + idempotent loader
- `glados/core/canon_context.py` — retrieval + prompt formatting
- `glados/core/context_gates.py` — `needs_canon_context` + defaults
- `glados/core/engine.py` — boot seeding + ContextBuilder register
- `glados/core/api_wrapper.py` — SSE injection + reload + retrieve
- `glados/webui/tts_ui.py` — handlers + card + JS + routing
- `configs/canon/*.txt` — 50 curated seed entries across 7 topics
- `tests/test_canon_*.py` — 58 new tests
- `docs/CHANGES.md` (this entry)
- `docs/battery-findings-and-remediation-plan.md` — §8.14 marked
  COMPLETE

### Closes

Change 15 open-issue #2 (Portal canon confabulation). Also expected
to resolve open-issue #3 (non-streaming "testing tracks" refusal)
since that was a related tool-framing miss on lore questions —
will verify during live probe.

---

## Change 18 — Phase 8.8: positive anaphora detector (2026-04-21)

**Problem.** Pre-8.8 the follow-up carry-over logic in
`CommandResolver._build_carryover` used
`_looks_anaphoric(utterance)` = "`_extract_qualifiers(utterance)`
returned no distinctive qualifier words." That reused the
disambiguator's stopword list, which was tuned for a different
purpose: telling "bedroom strip **segment 3**" apart from
"bedroom strip" (content words vs generic domain nouns).
Common follow-up words that carry the anaphora signal —
`more`, `again`, `keep`, `going`, `same`, `dark` — were NOT
in that list, so the resolver misclassified the operator's
actual failure cases:

- "Turn it up more" → qualifier `["more"]` → anaphoric=False →
  no carry-over → fall-through → Tier 3 chitchat hallucinates
  a confirmation, light doesn't change.
- "Do that again" → qualifier `["again"]` → same path.
- "Keep going" → qualifiers `["keep", "going"]` → same path.

Extending the stopword list would have regressed Phase 8.3's Gate-
3 fix, where "segment 3" had to stay classifiable as a distinctive
qualifier. Two competing purposes of one token list.

### Fix

New module `glados/intent/anaphora.py` with a positive detector
`is_anaphoric_followup(utterance) -> bool`. Four rules (any one
fires), plus a WH-question guard:

1. **Pronoun deictic** — `it`, `them`, `that`, `those`, `these`,
   `this`, `one`, `ones`. "Turn **it** up more" catches on `it`.
2. **Explicit repetition marker** — `again`, `more`, `same`,
   `keep`, `continue`, `resume`. "Do that **again**" catches on
   `again`.
3. **Bare intensity adverb with no content word** — the utterance
   contains `brighter` / `louder` / `warmer` / `up` / `off` /
   etc. AND has no content tokens outside fillers + pronouns +
   intensity words. "A bit brighter" catches, "a bit brighter in
   the kitchen" does not.
4. **Short additive continuation** — "also the kitchen too", "and
   the office as well". Fires only on utterances ≤ 6 tokens so a
   long sentence that happens to contain `too` isn't a follow-up.

WH-question guard: utterances that start with `what`, `when`,
`where`, `how`, `which`, `who`, `whom`, `whose`, `why` always
return False. Protects against the "what time is **it**" class
that would otherwise fire Rule 1. (The resolver already short-
circuits state queries upstream, but the module stays correct in
isolation.)

### Rewire

`CommandResolver._looks_anaphoric` now delegates to the new
function. All other carry-over machinery is unchanged:
`SessionMemory.record_turn` / `last_turn`, the carry-over window
check, `Disambiguator.run(assume_home_command=..., prior_entity_ids=...,
prior_service=...)`. Phase 8.8 is a swap-out of the gate, not a
rewrite of the path.

### Configurable follow-up window

`MemoryConfig.session_idle_ttl_seconds: int = 600` — read at
engine boot, passed to `SessionMemory(idle_ttl_seconds=...)` via
`glados/server.py`. The field auto-renders on Configuration →
Memory because the page is driven by `cfgBuildForm` over the
pydantic model; no new card needed.

### Tests

- `tests/test_anaphora.py` — 37 parametrized cases.
  Positives: bare intensity adverbs, pronoun deictics, repetition
  markers, additive continuations, case-insensitive, punctuation-
  tolerant. Negatives: new-target commands, state queries,
  greetings, Phase 8.3 regression guard for
  `"bedroom strip segment 3"`, size guard for long additives.
- `tests/test_command_resolver.py::TestPhase88Followups` — 8
  parametrized end-to-end cases driving the resolver with a fake
  disambiguator. Records a first-turn Tier 2 hit on
  `light.task_lamp_one`, then fires each operator-
  reported follow-up phrase and asserts `prior_entity_ids` +
  `prior_service` + `assume_home_command` thread through to the
  disambiguator call.

Full suite: **959 passed / 3 skipped** (was 895 / 3; +64 new).

### Commits

- `glados/intent/anaphora.py` — positive detector
- `glados/core/command_resolver.py` — `_looks_anaphoric`
  delegates
- `glados/core/config_store.py` — `session_idle_ttl_seconds`
- `glados/server.py` — reads config and passes to SessionMemory
- `tests/test_anaphora.py` — unit tests
- `tests/test_command_resolver.py` — integration tests
- `docs/CHANGES.md` (this entry)
- `docs/battery-findings-and-remediation-plan.md` — §8.8 marked
  COMPLETE

### Closes

Phase 8.8 complete. Also resolves the SESSION_STATE handoff's
original P0 #2: *"Follow-up turns without a device keyword
bypass Tier 1/2"* — now they don't.

---

## Change 19 — Phase 8.9: test-harness hardening + CI wiring (2026-04-21)

**Problem.** The 2026-04-20 private 435-row battery ran an
overly-permissive scorer: *any* entity in the house ending in the
expected state during the test window counted as PASS. Three failure
classes sneaked through as false positives:

1. Background-noise entities — Midea AC displays cycle every ~60 s,
   Sonos diagnostics flap, zigbee `*_button_indication` /
   `*_node_identify` ping HA on their own schedules. If any of them
   flipped during the test window and happened to match the expected
   direction, the scorer logged a PASS even when GLaDOS's targeted
   action did nothing.
2. Off-target state changes — a miscast command flipping *a different*
   real entity (say, living-room lamp instead of kitchen light) was
   still a PASS because "something" ended up in the expected state.
3. Tier-ack rescues — Tier 1/2 logged `result: ok` but no state
   actually moved. The scorer trusted the ack.

Plus there was no CI regression safety net: the 970-test container
suite ran only locally, so a refactor could land that broke scoring
semantics without catching it until next deploy.

### Fix

**Stage A — container-side: `TestHarnessConfig`.**

- New pydantic section at `glados/core/config_store.py::TestHarnessConfig`.
  Two fields:
  - `noise_entity_patterns: list[str]` — fnmatch globs the harness
    must strip from the diff set before scoring. Defaults ship the
    operator-confirmed noisy families (`switch.midea_ac_*_display`,
    `sensor.midea_ac_*_*`, `*_sonos_*`, `*_wled_*_reverse`,
    `*_button_indication`, `*_node_identify`).
  - `require_direction_match: bool = True` — when True, scoring only
    credits entity-direction matches on the actual targeted entity
    set. Operator can toggle to False for A/B against pre-8.9
    scoring.
- Registered in `GladosConfigStore` alongside the other sections —
  auto-exposed via standard `GET/PUT /api/config/test_harness` for
  operator edits.
- **Public endpoint** `GET /api/test-harness/noise-patterns` in
  `api_wrapper.py` (no auth): the external harness fetches the list
  on every run, so operator edits take effect on the next battery
  without file sync. Endpoint reads `test_harness.yaml` fresh from
  disk on each call — avoids a cross-process `/api/reload-engine`
  round-trip for a non-engine config.
- **WebUI** — "Test Harness" card on the System tab, `data-advanced="true"`
  (hidden behind the Show Advanced Settings toggle since it's a
  benchmarking knob, not day-to-day). Textarea for the pattern list
  (one glob per line) + a Require-direction-match checkbox. Saves
  via `/api/config/test_harness`.

**Stage B — harness-side: direction-verified scoring.**

- `C:\src\glados-test-battery\harness.py::score()` now takes
  `expected_entities` (the target set resolved from
  `target_keywords`), `noise_patterns`, and `require_direction_match`.
- On entry, strips diffs whose `entity_id` matches any noise glob.
  When direction is required, restricts the "changed" set to
  entities inside the target set before asking "is any of them in
  the expected state?". Off-target real changes and noise flips
  both stop rescuing FAILs.
- `audit_ok_from_tier` fallback is gated on `require_direction=False` —
  when direction is enforced, the harness demands state proof; when
  disabled, the pre-8.9 ack-fallback is preserved for A/B.
- `fetch_noise_patterns()` pulls the current list from the container
  at run-start. Falls back to a hardcoded default list if the
  container is unreachable.

**Stage C — home-assistant-datasets adapter.**

- New `C:\src\glados-test-battery\hadatasets_adapter.py` — converts
  scenario YAMLs from `github.com/allenporter/home-assistant-datasets`
  (assist/assist-mini format: `category` + `tests: [{sentences,
  setup, expect_changes}]`) into harness tests.json rows. Each
  sentence becomes one row. Mapping rules folded into `_expected_change`
  (brightness deltas + rgb_color + state → our `on|off|brighter|
  dimmer|color|any` enum) and `_infer_service` (domain+state →
  HA service).
- CLI: `python hadatasets_adapter.py --path <tree> --out tests_ha.json
  --start-idx 10000`. Row indices start at 10000 by default so the
  converted set can merge with the private 435-row battery without
  collision.

**Stage D — CI.**

- New `.github/workflows/tests.yml` — runs `pytest -q --tb=short`
  with `pip install -e '.[dev]'` on every PR and on every push to
  main. 970-test container suite now gates merges. Battery itself
  (which needs live HA + deployed container on 192.168.1.x) is not
  runnable on a public runner; that would require a self-hosted
  runner on the operator's network and is deferred — the plan's
  "30-test sanity subset on PR" remains aspirational.

### Tests

- `tests/test_test_harness_config.py` — +11 cases: defaults cover
  operator-known noise families, default globs don't match real
  targets, YAML round-trip preserves field order, `to_dict` /
  `update_section` / public-endpoint-shape contract locked.
- `C:\src\glados-test-battery\test_score.py` — +14 cases: noise
  filter strips Midea display flips, off-target changes FAIL under
  direction-match, tier-ack no longer rescues when direction is
  required (with back-compat when it's disabled), brighter /
  dimmer / off paths all direction-gated, `state_query` path
  unchanged by 8.9.
- `C:\src\glados-test-battery\test_hadatasets_adapter.py` — +13
  cases: all expected-change mappings including lock / cover /
  media_player coercion, keyword extraction behaviour,
  convert_tree aggregation + fixtures exclusion, JSON round-trip.

Container-side full suite: **970 passed / 3 skipped** (was 959 / 3;
+11 new). Harness-side: 38/38.

### Commits

Container repo (landed and deployed):
- `glados/core/config_store.py` — `TestHarnessConfig` + store
  registration
- `glados/core/api_wrapper.py` — public noise-patterns endpoint
- `glados/webui/tts_ui.py` — System-tab Advanced card + JS
- `.github/workflows/tests.yml` — CI pytest lane
- `tests/test_test_harness_config.py` — unit tests
- `docs/CHANGES.md` (this entry)
- `docs/battery-findings-and-remediation-plan.md` — §8.9 marked
  COMPLETE

Harness scratch dir (not git-tracked; lives at
`C:\src\glados-test-battery`):
- `harness.py` — direction-verified scoring + noise fetch
- `hadatasets_adapter.py` — new
- `test_score.py` — new
- `test_hadatasets_adapter.py` — new

### Closes

Phase 8.9 complete to the level the plan defined (scorer harden +
home-assistant-datasets adapter + pytest CI). Outstanding follow-ups:

- Self-hosted runner for the live battery — wait until operator
  decides whether they want that always-on infra.
- Nightly full-battery + ha-datasets benchmark wiring — waits on
  self-hosted runner.

---
