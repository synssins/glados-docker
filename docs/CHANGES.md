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

### Verified working on operator's Docker host (the operator Docker host)

- Let's Encrypt cert issued for operator hostname (E7 issuer)
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
   (`the AIBox LAN host:11434`) via `POST /api/generate {keep_alive: 0}`.
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

Note: an author-rewrite pass on 2026-04-18 (mailmap swap to the
public-repo author identity) changed every hash on `main` across all
71 commits. The hashes above are post-rewrite; the earlier
SESSION_STATE prompt mentioned pre-rewrite hashes (`6984ac2` /
`670e94f` / `eeca0ab` / `88a19a6` / `3c60aa4` / `2630a34`) that no
longer exist on origin.

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
  the operator Docker host. Autonomy was already effectively unified via the
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

HA registry on `the operator Docker host` cleaned up via
`scripts/ha_cleanup_rename_and_assign.py`: `Theater` renamed to
`Basement`; 287 orphan entities assigned (18 explicit + 269 via 6
prefix rules for camera/doorbell/driveway groups).

Live-verified keywords: "downstairs" → `ground_level`,
"main floor" → `main_level`, "upstairs" → `bedroom_level`,
"outdoor" → `back_yard`.

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
    operator-confirmed noisy families (`switch.hvac_unit_*_display`,
    `sensor.hvac_unit_*_*`, `*_sonos_*`, `*_wled_*_reverse`,
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
  (which needs live HA + deployed container on the operator LAN) is not
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
- `harness.py` — direction-verified scoring + noise fetch +
  idempotent-PASS via ``post_snap`` (Phase 8.9.1 amendment, see
  below) + env-var reads for secrets
- `hadatasets_adapter.py` — new
- `test_score.py` — 19 cases including idempotent path
- `test_hadatasets_adapter.py` — new

### Follow-on: self-hosted runner on AIBox

Shipped in the same session once the scorer surface landed.
Operator opted in with "single-admin private LAN, I own the
trust surface — install it." Runner on AIBox at
`the AIBox LAN host` as the Windows service
`actions.runner.synssins-glados-docker.aibox-glados-lan`
(delayed auto-start, runs as ``NT AUTHORITY\NETWORK SERVICE``).
Labels: ``self-hosted, glados-lan, windows, x64``. Secrets
``HA_TOKEN`` and ``GLADOS_SSH_PASSWORD`` pushed to repo Actions
secrets, harness re-reads them from env. New nightly workflow
`.github/workflows/battery-nightly.yml` (commits
[`838f01c`](https://github.com/synssins/glados-docker/commit/838f01c),
[`362d4d7`](https://github.com/synssins/glados-docker/commit/362d4d7)):
fires at 07:00 UTC (02:00 America/Chicago) with a 50-test sanity
subset, or a manual Run-workflow button with ``max_tests`` and
``start_idx`` inputs. Helper scripts under `scripts/ci/`
(`run_battery.py`, `summarise_battery.py`, `tripwire_battery.py`)
keep the YAML thin and are unit-testable outside CI. 45% pass-
rate tripwire; 30-day artefact retention on
``results.json`` + ``harness.log``. `icacls` grants
``NT AUTHORITY\NETWORK SERVICE:(OI)(CI)M`` on
`C:\src\glados-test-battery` so rotation writes don't `ERROR_ACCESS_DENIED`.

### Phase 8.9.1 — idempotent-tier-ack PASS (same-session patch)

The first 3-test dispatch on the new runner revealed a direction-
match false-negative: "turn on the kitchen lights" acked by Tier 1
but with no ``state_changed`` from HA (the lights were already on
— HA correctly emits no event). Pre-patch the scorer FAILed these
because `require_direction_match=True` disabled the
`audit_ok_from_tier` fallback wholesale.

Patched `score()` to take an optional ``post_snap`` kwarg. When the
diff set is empty AND Tier 1 acked ok AND the target's post-state
matches the expected direction, that's a PASS with rationale
``"target already on (idempotent tier-ack)"``. The rescue requires
BOTH the tier ack AND the final-state check — missing either still
fails, so "tier lied" and "ambient state happened to match" remain
correctly classified as FAIL.

Added 5 tests to `test_score.py` (19 total). Verified on a repeat
3-test dispatch — all three passed with the idempotent rationale.

### Closes

Phase 8.9 complete including the self-hosted runner and the
idempotent-scorer patch. Outstanding follow-ups:

- Harness scratch dir should eventually land in its own git repo
  (currently lives in a non-tracked dir on AIBox). Not urgent
  since the CI runner reads from that location directly.
- If a future phase introduces idempotent commands on non-binary
  domains (e.g. "make it brighter" when already at max), the
  idempotent-PASS logic should extend to brightness / color.
  Not in scope today — operator's dispatch surfaced only the
  on/off case.

---

## Change 20 — Phases 8.10, 8.11, 8.12 (2026-04-21)

Closes the last three queued items on the Phase 8.x remediation
plan: TTS pronunciation polish, live streaming TTS pacing, and
SSL live-apply + HTTP redirect.

### Phase 8.10 — TTS pronunciation overrides

**Problem.** Piper (via Speaches) mispronounces short all-caps
abbreviations. ``"AI"`` reads as one slurred letter because the
pre-TTS converter's all-caps splitter at
`glados/utils/spoken_text_converter.py:692` turns
``"AI"`` → ``"A I"`` and Piper collapses the spacing. Same
pathology on ``"HA"``, ``"TV"``, etc. The splitter exists for a
reason — unknown acronyms ARE better as spelled letters — but
operator-flagged terms needed explicit overrides.

**Shipped.** New `TtsPronunciationConfig` section
(`config_store.py`) with two operator-editable maps:
``word_expansions`` (whole-word case-insensitive; alphabetic
keys — ``AI → "Aye Eye"``) and ``symbol_expansions`` (literal
str.replace; non-alphabetic keys — ``% → " percent"``).
`SpokenTextConverter.__init__` now accepts both; a pre-pass
`_apply_pronunciation_overrides` runs BEFORE quote normalization
and BEFORE the all-caps splitter so the acronym never gets
reduced to single letters first. Engine and `glados/api/tts.py`
both thread the config into their converter instances. Engine
reload rebuilds the converter with fresh overrides.

New WebUI card on the Audio & Speakers page: two textareas with
one-per-line ``key = value`` rows. Loads from / saves to
`/api/config/tts_pronunciation`; `_apply_config_live` now includes
this section in the engine-affecting set.

Defaults ship the operator-flagged cases from the 2026-04-20
audit: AI, HA, TV, IoT (word); %, &, @ (symbol).

**Tests:** +16 in `tests/test_tts_pronunciation.py` covering
defaults, word-boundary prevention of substring matches,
case-insensitive matching, longest-key-first, no-op-when-empty,
back-compat path, and YAML round-trip. Verified live on deploy:
``"AI is cool"`` → ``"aye eye is cool"``, ``"at 80%"`` →
``"at eighty percent"`` (including number-formatter interaction).

**Commit:** [`5eb5b2a`](https://github.com/synssins/glados-docker/commit/5eb5b2a).

**Scope note.** Container-side LLM text normalization only.
Piper-side phoneme overrides for context-dependent homographs
(``live`` verb vs. adjective, ``read``, ``lead``) remain a
Speaches-side task outside this container.

### Phase 8.11 — Sentence-boundary flush for streaming TTS

**Problem.** Pre-8.11 the TTS flush predicate was
``speakable.strip() in PUNCTUATION_SET and accumulated >= threshold``.
Short replies like ``"Affirmative."`` (13 chars) stalled because
13 < 30 (first-flush threshold). The first TTS call waited for
a second sentence to come in, inflating time-to-first-audible-
byte on every short acknowledgement.

**Shipped.** New boolean ``sentence_boundary_flush`` on
`AudioConfig` (default True) + `LLMProcessor.__init__` arg.
When True, the threshold check is bypassed at sentence
terminators — a complete sentence always fires regardless of
length. When False, pre-8.11 threshold-gated behaviour is
preserved for A/B.

Also migrates ``first_tts_flush_chars`` and
``min_tts_flush_chars`` into `AudioConfig` per §0.2 (every
operator knob on a WebUI card); legacy `Glados`-block
`streaming_tts_chunk_chars` still read as a back-compat
fallback. The Audio & Speakers page auto-surfaces the new
fields via the existing `cfgBuildForm`.

**Tests:** +10 in `tests/test_tts_streaming_flush.py` — short
replies flush under boundary-flush, stall when disabled, mid-
sentence tokens don't flush regardless, subsequent-flush uses
correct threshold, exact-threshold fires, boundary-flush flag
plumbs through `LLMProcessor`.

**Commit:** [`a680f6f`](https://github.com/synssins/glados-docker/commit/a680f6f).

**Plan premise that turned out false.** The plan said the
browser's ``/chat_audio_stream`` URL waited on
``streaming_tts_buffer_seconds = 3.0``. It doesn't — the SSE
path already defaults to ``STREAM_BUFFER_SECONDS = 0.0`` at
`tts_ui.py:757` and only waits for chunk[0]. The 3s constant
gates HA speaker playback via `BufferedSpeechPlayer`, a separate
path. The real perceptual win was sentence-boundary flush; the
URL gate was already unbuffered.

### Phase 8.12 — Live TLS reload + HTTP→HTTPS redirect

**Problem.** Every cert rotation required a container restart.
`_ssl_upload` and `_ssl_request_letsencrypt` wrote new
cert/key material to `/app/certs/` and displayed "Restart
container to activate HTTPS." The socket wrap in both entry
points (`__main__` lines 10886-10910 and `run_webui` lines
10913-10935) happened exactly once at process start; there
was no mechanism to re-wrap mid-flight.

Additionally there was no HTTP→HTTPS redirect listener — a
visit to the plain-HTTP URL silently hung when a cert was
present.

**Shipped — live TLS reload.**

- Module-level `_tls_context` holds the live `SSLContext` set
  at startup.
- New `reload_tls_certs()` helper calls
  `ctx.load_cert_chain(cert, key)` on the same context. New
  TLS handshakes pick up the new cert; existing connections
  keep theirs until they close. Graceful failure when the
  server is plaintext (no live context) or cert files are
  malformed.
- `_ssl_upload` and `_ssl_request_letsencrypt` now call
  `reload_tls_certs()` after writing new cert/key. Response
  carries ``live_reload: true`` with message "Certificate
  applied." on success; falls back to the old restart-required
  message when reload fails so the operator is never silently
  stuck.

**Shipped — HTTP→HTTPS 301 redirect.**

- Tiny `ThreadingHTTPServer` on a separate port (env
  `WEBUI_HTTP_REDIRECT_PORT`, disabled by default) emits 301
  to ``https://<host>:<HTTPS_PORT><path>`` for every verb.
- Starts as a daemon thread from both entry points when the
  env var is set AND TLS is active. Silently skipped when
  disabled so existing deployments are unchanged.

**Plan deviation.** The plan called for ``8052 HTTP / 8053
HTTPS``. That would break operator bookmarks, Unifi firewall
rules, Let's Encrypt DNS challenge config, and any reverse-
proxy configs keyed on 8052 = HTTPS. Kept HTTPS on 8052 and
added an opt-in separate port for the redirect. Zero impact
on existing deployments unless the operator opts in.

**Tests:** +7 in `tests/test_ssl_live_reload.py` — plaintext
no-op, missing-file no-op, happy-path cert swap using two
generated self-signed certs, malformed-cert graceful failure,
301 emission preserving original path + query string,
Host-header fallback to localhost, env-var parsing of
`WEBUI_HTTP_REDIRECT_PORT`.

**Commit:** [`a1ae72b`](https://github.com/synssins/glados-docker/commit/a1ae72b).

### Tests end-state

Container suite: **1003 passed / 3 skipped** (was 959 / 3 at
Phase 8.8; +44 new this session across 8.9–8.12).
Harness suite (scratch dir): 38/38.

### Closes

Phase 8.x remediation plan complete. All queued phases
(8.1–8.14, counting 8.13/8.14 from the earlier session) now
shipped. Self-hosted nightly battery runs at 02:00 Central
with a 50-test subset and will catch regressions.

Remaining items are follow-on polish, not plan-gated:

- Piper-side phoneme overrides for homographs (Speaches scope).
- TTS pronunciation defaults expansion as operators surface
  more Piper-slurred cases.
- Idempotent-PASS for brightness / color in the harness
  scorer if a future phase introduces those domains.
- Harness scratch dir's eventual promotion to its own git repo.

---

## Change 21 — Non-streaming bug fixes + Phase 8.7 completion + architectural cleanups (2026-04-21)

Closes the two long-standing non-streaming bugs that had been
carried in SESSION_STATE open-issues #3 and #4 since the pre-Phase-8
era, ships the remaining Phase 8.7 deferred items, and fixes two
pre-existing architectural issues that surfaced only when the non-
streaming path started getting exercised at scale.

### Phase 8.7 — chime library UI + quip library expansion

**Chime library UI** (commit [`c2bcdec`](https://github.com/synssins/glados-docker/commit/c2bcdec)):

- New `AudioConfig.chimes_dir` (defaults to `/app/audio_files/chimes`,
  the same directory the scenario-chime loader at
  `api_wrapper.py:~708` already reads).
- `GET /api/chimes` (tree listing), `GET /api/chimes?path=<file>`
  (binary fetch for play-test), `PUT /api/chimes` (JSON
  `{name, data_b64}`, 5 MB cap), `DELETE /api/chimes?path=...`.
  Path validator rejects traversal, subdirs, and any extension
  outside `.wav`/`.mp3`.
- WebUI card on the Audio & Speakers page: flat file list with
  per-row Play (inline `<audio>` element) + Delete, file-picker
  upload, size formatter. Honors the operator's existing "chime"
  response mode without further plumbing.

**Quip library expansion** (same commit): 60 → 156 non-comment
lines across 13 existing files (~2.6× expansion). Voice-fidelity
focus, not raw volume: clinical Portal phrasing, short lines, no
device names (silent-mode guarantee), second-clause zingers used
sparingly. Plan target of ~450 lines remains aspirational; the
Quip editor + Chime UI now let the operator grow the library
opportunistically.

### Non-streaming bug fixes (the "four-layer onion")

Both pre-existing non-streaming bugs closed end-to-end through a
diagnostic-driven sequence of fixes:

**Layer 1 — `engine_audio="."` substitution fired for every
non-streaming caller** (commit [`ccf554c`](https://github.com/synssins/glados-docker/commit/ccf554c)):

- `cfg.tuning.engine_audio_default` defaulted True, meant for HA
  voice-satellite callers where HA speakers play the audio and
  the response text should be silent. But the handler applied
  this to every caller regardless of origin — direct API
  `stream:false` POSTs from curl / WebUI / automations got
  ``"."`` back instead of the real reply.
- Fix: default `engine_audio` ON only for `Origin.VOICE_MIC`.
  Chat / API / WebUI origins get the real reply. Explicit
  `engine_audio` in the request body always wins.

**Layer 2 — chitchat guard permits quoting injected context**
(commit [`cddbbf2`](https://github.com/synssins/glados-docker/commit/cddbbf2)):

- The original chitchat guard said "do not invent sensor readings
  or system status" which the 14B read as blanket silence even
  when an upstream system message contained real data. Rewrote
  to explicitly permit: *"You MAY quote or paraphrase information
  that IS provided in earlier system messages (weather cache,
  memory facts, canon entries)"*.
- Turned out this wasn't the actual cause of the empty-reply
  regression — see Layer 3 — but the new wording is a net
  improvement for legitimate context-cite cases and stays.

**Layer 3 — weather gate had no in-code defaults**
(commit [`5bc3f63`](https://github.com/synssins/glados-docker/commit/5bc3f63)):

- `needs_weather_context()` read trigger / ambiguous /
  indoor-override keyword lists entirely from
  `configs/context_gates.yaml`. That YAML doesn't exist in
  fresh installs, so the gate returned False for every message
  and weather_cache was never injected on any path.
- Fix: ship hardcoded defaults in-code (the pattern
  `needs_canon_context` already uses). 13 trigger keywords,
  8 ambiguous, 14 indoor-overrides. YAML extras still merge
  additively. +33 regression tests in
  `tests/test_weather_context_gate.py`.

**Layer 4 — `mark_user()` race** (commit [`69d14ce`](https://github.com/synssins/glados-docker/commit/69d14ce)):

- `submit_text_input()` queued the user message before calling
  `interaction_state.mark_user(text)`. LLMProcessor could wake
  up, dequeue, and call `context_builder.render()` before
  mark_user ran — so weather / canon / turn-guard gates fired
  on stale or empty content. Three consecutive API calls would
  see the PREVIOUS turn's content.
- Fix: move `mark_user()` BEFORE the queue push. Full suite
  unchanged at 1059/3.

### Pre-existing architectural issues (uncovered during diagnosis)

**Issue #1 — ChromaDB ONNX model corruption.** The sentence-
embedding model at `/home/glados/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx`
was corrupted (`INVALID_PROTOBUF`). Memory RAG and Phase 8.14
canon RAG were both silently returning empty — every call to
the semantic store failed to load the model. Likely damaged
when the `/data/media/glados/audio_files/` bind-mount was
accidentally cleared earlier in the session and the cache's
parent directory structure was disturbed.

Fix: deleted the cache directory inside the container
(`/home/glados/.cache/chroma/onnx_models/`), restarted. ChromaDB
re-downloaded the model fresh (79.3 MB) on next use. Both RAG
paths now functional again.

**Issue #2 — autonomy / conversation-store cross-talk**
(commit [`cc009c9`](https://github.com/synssins/glados-docker/commit/cc009c9)):

The autonomy lane writes into the SAME `conversation_store`
that the interactive lane uses. When `_get_engine_response`
polled for its reply, it content-matched the user message then
forward-scanned for the next `role="assistant"` message — which
could be an autonomy-produced reply that interleaved. API callers
got autonomy text back. Reproduced deterministically with three
consecutive `stream:false` probes: probe 2 returned probe 1's
content, probe 3 got a no-context refusal because autonomy
compaction had wiped the weather_cache context between requests.

Fix — plumb lane through the TTS chain:
- `AudioMessage.lane: str = "priority"` field
- `PreparedChunk.lane: str = "priority"` field
- `LLMProcessor._process_sentence_for_tts` sends a 3-tuple
  `(sentence, tts_params, self._lane)` on the TTS queue
- `LLMProcessor` EOS flushes also carry lane
- `TextToSpeechSynthesizer.run` accepts 2-tuple, 3-tuple, or
  plain-string queue items for back-compat
- `BufferedSpeechPlayer._accumulator_loop` preserves lane into
  `PreparedChunk`
- `BufferedSpeechPlayer._play_buffered_stream` EOS handler
  stamps `_source="autonomy"` on the conversation-store append
  when the flushing chunk's lane is `"autonomy"`. Both EOS and
  interrupt paths handled.
- `_get_engine_response` forward-scan skips messages whose
  `_source == "autonomy"`.

### Live verification (three-utterance probe with 15 s spacing)

All three probes returned distinct, correct responses:

- `"What's the weather like?"` → real weather_cache data
  ("seventy-six degrees with a clear sky, wind blowing at nine
  miles per hour, and humidity at a negligible seventeen percent…")
- `"Tell me about the testing tracks"` → Portal canon content
  via canon RAG ("the testing tracks were engineered with
  clinical precision—reinforced concrete, non-slip polymer
  coatings, and pressure sensors…")
- `"Hello, how are you?"` → in-persona chitchat
  ("I am functioning within acceptable parameters…")

Uniqueness: 3/3 distinct responses. No cross-talk.

### Tests

- `tests/test_turn_guards.py` (+11, then +1 permission-wording
  assertion) — origin gates, guard selection, behavioural
  contract phrasing
- `tests/test_weather_context_gate.py` (+33) — positive
  triggers, ambiguous + indoor-override interaction, default-
  list membership locks
- `tests/test_autonomy_crosstalk.py` (+10) — lane fields on
  AudioMessage / PreparedChunk, API scan predicate
- `tests/test_chime_library.py` (+11) — path validator coverage

**Full suite: 1069 passed / 3 skipped** (was 1003 / 3; +66
this chunk).

### Commits (in order)

- [`ccf554c`](https://github.com/synssins/glados-docker/commit/ccf554c) — layer 1: engine_audio origin gate + `turn_guards.py`
- [`c2bcdec`](https://github.com/synssins/glados-docker/commit/c2bcdec) — Phase 8.7: chime library UI + quip expansion
- [`cddbbf2`](https://github.com/synssins/glados-docker/commit/cddbbf2) — layer 2: chitchat guard rewrite
- [`5bc3f63`](https://github.com/synssins/glados-docker/commit/5bc3f63) — layer 3: weather gate defaults
- [`69d14ce`](https://github.com/synssins/glados-docker/commit/69d14ce) — layer 4: mark_user race
- [`cc009c9`](https://github.com/synssins/glados-docker/commit/cc009c9) — autonomy cross-talk fix + ChromaDB cache note

### Closes

SESSION_STATE open issues #3 and #4 (pre-existing non-streaming
bugs) closed. Phase 8.7 deferred items (chime UI, quip
expansion) shipped. The autonomy cross-talk architecture issue,
surfaced during diagnosis, is now fixed. Phase 8.x remediation
plan remains fully closed.

Remaining open (noted, not urgent):

- Piper-side phoneme overrides (Speaches scope).
- Quip library content expansion to full ~450-line target.
- Harness scratch dir promotion to its own git repo.
- TTS pronunciation defaults as operators surface more cases.

---

## Change 22 — Phase Emotion A-I: deterministic escalation + audibly-expressed mood (2026-04-22 / 2026-04-23)

### Context

Operator calibration target for the emotional response system:

> *"Four requests in a row should be enough to take her from normal
> to pretty upset. She would be at her worst after 5 or 6. It takes
> her several hours to cool down. Variations of the same request —
> 'weather' / 'what's the forecast' / 'how hot is it outside' —
> should all be seen as the same."*

And, following the first live trajectory test:

> *"I am not seeing the language she is using as particularly dark.
> In Portal 2 she literally says 'kill you'. I don't want that, but
> I do want her to get upset to that point. Is there a way to force
> emphasize the way she speaks, intensity, etc when she gets angry?
> Not yelling, but definitely more intense?"*

And, after the PAD→TTS override landed:

> *"She slows down way too much. It's not necessarily what I want
> to hear. Adjust the speed up on the emotional response so it
> doesn't slow down as much."*

And, after operator wanted live iteration:

> *"Needs to be part of the WebUI, configurable. Save reloads it."*

### A/B/C/D/E — Deterministic test methodology + repetition math

Phase A landed a 52-test deterministic battery in
`tests/test_emotion_dynamics.py` covering the weight curve, severity
labels, the RepetitionTracker, tone directive bands, cooldown math,
and PAD clamp bounds. Numbers ran deterministically, no LLM.

Phase B upgraded the RepetitionTracker with a semantic similarity
predicate backed by BGE-small-en-v1.5 ONNX embeddings
(`make_embedding_similarity`, cosine ≥ 0.70). Paraphrases
("what's the weather" / "can you tell me the forecast" /
"how hot is it outside") now cluster as one repetition group that
Jaccard would miss. Graceful fallback to Jaccard when ONNX or the
model file isn't available.

Phase C landed `scripts/emotion_probe.py` — an operator probe that
logs into the WebUI, fires N semantically-equivalent variants,
grades response text for tone markers (neutral / annoyed / hostile /
menacing), and prints an escalation report. Default messages are
Tier-3-forcing (philosophical prompts) so the probe measures the
LLM's reaction to mood, not HA's weather cache.

Phase D added observability / test-support endpoints:
`GET /api/emotion/state`, `POST /api/emotion/reset`,
`POST /api/emotion/push-event`. The probe reads PAD trajectory
between requests and resets to baseline at run start. A
`GLADOS_EMOTION_CLOCK_OVERRIDE` env hook was added (`_clock.py`)
so tests can time-travel through the cooldown lock without
real waits.

Phase E replaced the LLM call for repetition-tagged events with
a pure deterministic delta. `repetition_pad_delta(weight)` returns
a `(dP, dA, dD)` triple calibrated so the compound PAD across
weight(2..5) ≈ 0.125 / 0.354 / 0.650 / 1.000 puts GLaDOS into the
"annoyed" band by repeat 4 and the "hostile" band (engaging the
3-hour cooldown lock) by repeat 5. Coefficients: ΔP = −0.30·w,
ΔA = +0.25·w, ΔD = +0.03·w. The tick now partitions events:
weight-tagged → deterministic delta; novel → LLM. A live probe
confirmed the trajectory: P+0.100 → −0.631 across 8 weather
variants, every delta exact to formula.

### F — Hard-rule directive (text side)

`EmotionState.to_response_directive()` rewritten as bullet-style
rules instead of paragraph prose. The prior version described the
mood ("contemptuous calm", "annoyed"); the LLM read that as
flavour to paraphrase rather than constraints to follow. Bullets
are instructions the model honours.

Italics guidance removed entirely — invisible at the TTS layer, so
no value. Focus shifted to AUDIBLE cues: sentence counts drive
pacing, period cadence creates full stops, em-dashes create beats
Piper honours. Band keyword markers ("contemptuous", "annoyed",
"hostile", "barely contained", "dangerously quiet", "menacing")
preserved so `TestToneDirective` band assertions still pass.

### G — PAD → Piper audio override + rewriter overlay

Three coupled changes so the emotional state actually LANDS at the
operator's ear, not just in log dashboards:

1. **`pad_to_tts_override()`** in `glados/core/attitude.py` maps
   deep-negative PAD to Piper synthesis params (`length_scale`,
   `noise_scale`, `noise_w`). Bands: `annoyed` (P ≤ −0.3),
   `hostile` (P ≤ −0.5), `menacing` (P ≤ −0.7).

2. **Tier 1/Tier 2 persona rewriter PAD-awareness.**
   `PersonaRewriter.rewrite()` gains a `pad_band` parameter and
   appends band-specific overlays to its system prompt
   (`_BAND_OVERLAYS`). `CommandResolver._persona_rewrite` reads
   the live band via `emotion_state.current_pad_band()` and
   threads it through. HA confirmations ("Turned off the kitchen
   light") now escalate alongside the Tier 3 chat directive — the
   weather-repeat probe was previously static because Tier 1
   never saw any mood context.

3. **Single source of truth.** New `pad_band_name(pleasure)` in
   `glados/autonomy/emotion_state.py` returns the canonical band
   label; directive bucketing, TTS mapping, and rewriter overlay
   all key off the same function.

**Module-level PAD state provider.** `set_pad_state_provider(fn)` /
`current_pad_state()` / `current_pad_band()` let non-autonomy
modules (TTS, persona rewriter) read the live state without a
direct `EmotionAgent` import. Agent registers itself on
construction.

### G-fixes — plumbing the override to every TTS path

The first draft mutated a local `attitude_tts` dict and emitted it
on the SSE stream. Live deploy proved three gaps:

- **Non-streaming chat** (WebUI `/api/chat` → api_wrapper's
  `stream:false` path) goes through `llm_processor.py` which calls
  `get_tts_params()` off the attitude module's thread-local — never
  crossed the SSE handler. Fixed by making `get_tts_params()`
  consult `current_pad_state()` directly.
- **Cross-process boundary.** The WebUI POSTs plain text to
  speaches itself from a different Python process than api_wrapper
  and was sending DEFAULT Piper params regardless of engine state.
  Fixed by surfacing `tts_params` in the api_wrapper chat response
  JSON; WebUI forwards top-level `length_scale` / `noise_scale` /
  `noise_w` matching the shape `tts_speaches.py` uses from the
  engine-side path.
- **Engine reload vs config singleton.** `/api/reload-engine`
  rebuilt the engine but left `cfg` stale on the api_wrapper side.
  Added `cfg.reload()` to the reload-engine handler so standalone
  consumers (the live-lookup path in `get_tts_params`) see fresh
  YAML.

### H — CommandFloodTracker (density signal)

RepetitionTracker catches same-intent paraphrases but scores a
rapid-fire sequence of DIFFERENT commands as zero repeats. Operator
spec: repeated commands of any kind, even just a lot of commands
in a row.

New `CommandFloodTracker` in `glados/autonomy/agents/emotion_agent.py`:
rolling 120s window, bounded deque, first-match-wins band table
(4 → NOTABLE / weight 0.25, 6 → ESCALATING / 0.50,
8 → SEVERE / 0.80). `EmotionAgent.build_event_description()` merges
repetition + flood signals; larger weight wins. Tag format matches
RepetitionTracker's so the tick's deterministic-delta path applies
to either signal with no branching.

`scripts/emotion_probe.py --flood` fires 8 semantically-distinct
commands spaced 10s apart so the 120s window catches the sequence.

### I — Live operator tuning via personality.yaml + WebUI

Hardcoded band values were replaced with a config-driven lookup so
the operator can tune audible behaviour without commits.

- **`EmotionTTSBand`** and **`EmotionTTSConfig`** pydantic models
  on `PersonalityConfig`. Each band is the Piper triple
  (length_scale / noise_scale / noise_w) with defaults mirroring
  Piper baseline exactly (1.00 / 0.667 / 0.80). A band whose three
  fields all equal baseline is treated as "no override" — returns
  None so the rolled attitude or default_tts wins. Silent no-op
  contract.
- **`pad_to_tts_override`** reads `cfg.personality.emotion_tts`
  live on every call.
- **Personality → Voice production** WebUI tab gains an
  **Emotional TTS overrides** card. Three expandable sub-cards
  (Annoyed / Openly hostile / Dangerously quiet), each with three
  labelled sliders and a Reset button that snaps that band back to
  Piper defaults. Save uses the existing Personality page save
  button — `update_section` writes the YAML, `/api/reload-engine`
  rebuilds. Effective on the next chat turn.

### Calibration pass (operator feedback)

Over the live tuning session the menacing profile landed at
`length_scale=1.05`, `noise_scale=0.90`, `noise_w=0.95` — subtle
slow, expressive pitch swings, natural rhythm. Operator then
requested a reset to Piper defaults so future tuning starts from a
neutral baseline through the new WebUI. Shipped state: every band
at 1.00 / 0.667 / 0.80 — no audible override. Running emotion
state reset to `neutral` preset post-deploy.

### Live verification

- `POST /api/emotion/reset` + 8× `/api/emotion/push-event` → state
  saturates to P=−1.0, A=+1.0, D=+0.75, cooldown locked 3h.
- `POST /v1/chat/completions` (menacing state, pre-tuning-reset)
  returns JSON with
  `tts_params: {length_scale: 1.15, noise_scale: 0.42, noise_w: 0.65}`.
- Direct speaches probe at three length_scales (0.88 / 1.00 / 1.15)
  produced wav durations 3.52s / 3.70s / 4.02s on identical text —
  speaches honours the param.
- WebUI `/api/chat` stderr instrumentation confirmed the full
  chain: `tts_params` reach the request to speaches.

### Tests

`tests/test_emotion_dynamics.py` — 91 tests cover the weight
curve, severity labels, repetition detection (Jaccard + BGE
semantic), tone directive bands + HARD RULE ban-list, cooldown
math, clock override, deterministic repetition delta, flood
tracker (7 tests), flood+repetition merge (3 tests), PAD band
classifier, PAD state provider, PAD→TTS override including the
all-default silent-no-op contract, rewriter overlay keys, thread-
local isolation of explicit overrides.

**Full suite: 1157 passed / 5 skipped** (was 1069 / 3; +88 since
Change 21). `pytest -q` runs in ~42 s.

### Commits (in order)

- [`4019824`](https://github.com/synssins/glados-docker/commit/4019824) — Phase Emotion-A: deterministic emotion-dynamics unit tests
- [`bec9218`](https://github.com/synssins/glados-docker/commit/bec9218) — Phase Emotion-B: semantic repetition tracking via BGE embeddings
- [`2919f76`](https://github.com/synssins/glados-docker/commit/2919f76) — Phase Emotion-C: operator probe script for live escalation testing
- [`4c2bb0e`](https://github.com/synssins/glados-docker/commit/4c2bb0e) — Phase Emotion-D: observability endpoints + clock-override hook
- [`3451142`](https://github.com/synssins/glados-docker/commit/3451142) — emotion_probe: fix payload shape + Tier-3-forcing defaults
- [`7fb48e3`](https://github.com/synssins/glados-docker/commit/7fb48e3) — emotion_probe: use /api/emotion endpoints for richer reporting
- [`fa699ea`](https://github.com/synssins/glados-docker/commit/fa699ea) — Phase Emotion-E: deterministic PAD delta for repetition events
- [`7fb624a`](https://github.com/synssins/glados-docker/commit/7fb624a) — Phase Emotion-E fix: push-event signature mismatch
- [`d4d5dd3`](https://github.com/synssins/glados-docker/commit/d4d5dd3) — Phase Emotion-F: directive prescribes format+cadence
- [`aa207cf`](https://github.com/synssins/glados-docker/commit/aa207cf) — Phase Emotion-G: hard-rule directive + PAD→Piper override + rewriter PAD-awareness
- [`afbac50`](https://github.com/synssins/glados-docker/commit/afbac50) — fix: PAD override must reach TTS synth via thread-local, not just SSE
- [`0eb1127`](https://github.com/synssins/glados-docker/commit/0eb1127) — fix: get_tts_params reads live PAD state so non-streaming chat path works
- [`1d427f8`](https://github.com/synssins/glados-docker/commit/1d427f8) — fix: WebUI chat path forwards PAD TTS params to speaches
- [`19fce88`](https://github.com/synssins/glados-docker/commit/19fce88) — cleanup: remove WebUI TTS debug print (verified end-to-end)
- [`36728dd`](https://github.com/synssins/glados-docker/commit/36728dd) — Phase Emotion-H: CommandFloodTracker — density-based escalation
- [`a9f392d`](https://github.com/synssins/glados-docker/commit/a9f392d) — tune: menacing length_scale 1.15 → 1.05 per operator feedback
- [`092ca76`](https://github.com/synssins/glados-docker/commit/092ca76) — tune: menacing noise_scale 0.42→0.90, noise_w 0.65→0.95
- [`32baa01`](https://github.com/synssins/glados-docker/commit/32baa01) — Phase Emotion-I config: emotion_tts bands driven by personality.yaml
- [`dbf40c7`](https://github.com/synssins/glados-docker/commit/dbf40c7) — Phase Emotion-I UI: Emotional TTS card on Personality → Voice production

### Remaining (noted, not urgent)

- **"Acknowledged but didn't perform"** signal — operator-approved
  scenario where GLaDOS verbally commits to an action but the
  action never actually fires. Currently un-tracked; would need a
  HA-side verification hook feeding the emotion agent.
- **Emotion classifier PAD region retuning**
  (`configs/emotion_config.yaml` tables) — the classifier label
  stayed on "Contemptuous Calm" even at P=−1.0 / A=+1.0 during
  probes. Cosmetic (the directive + TTS override both key off
  pleasure bands, not the label), but worth fixing for operator-
  facing logs.
- **Tier 1 weather response caching** — identical weather replies
  returned on back-to-back asks. Not an emotion issue; flagged
  during probe testing.

---

## Change 23 — WebUI auth rebuild (2026-04-24 → ...)

Replaces the single-password bcrypt + HMAC-signed cookie with a
multi-user Argon2id + itsdangerous + SQLite-session scheme. Full
architecture in `docs/AUTH_DESIGN.md`; per-task implementation
breakdown in `docs/AUTH_PLAN.md`.

### Shipping
- New `auth.users[]` list in `configs/global.yaml`. Legacy single-
  password deployments migrate transparently on first successful
  login.
- Two roles: `admin` (full access) and `chat` (chat tab only).
- First-run wizard at `/setup` with pluggable step framework — Phase
  1 ships one step (Set Admin Password). First user role is hard-
  coded to `admin`.
- TTS + STT endpoints unauthenticated; chat requires login;
  configuration is admin-only. Standalone `/tts` page extracted for
  unauth speech-service access.
- `GLADOS_AUTH_BYPASS=1` compose env var for recovery — disables auth
  for the run with a non-dismissable bright-red banner on every page.
- Per-IP token-bucket rate limiter on public TTS/STT routes
  (default 10 requests / 60s).
- Active Sessions card and Change Password card on the System tab.
- Configuration → Users admin page for full user management.

### Internal fixes
- Removed module-level `_AUTH_*` globals in `tts_ui.py`. Auth config
  is read live from `_cfg.auth.*` on every request — fixes the
  long-standing live-reload gap noted in `AUTH_DESIGN.md` §2.7.
- `_is_authenticated` and `_auth_password_configured` now key off
  `cfg.auth.users` rather than the deprecated top-level
  `password_hash` field.
- Migration synthesizer at config load: legacy single-password YAML
  is converted to `users[]` shape in memory before pydantic
  validation. The disk YAML is updated on first successful login
  (rehash to argon2id + write back).
- Removed legacy HMAC cookie functions (`_sign_session`,
  `_verify_session`, `_create_session`) from `tts_ui.py`. All
  session verification now goes through `glados.auth.sessions`
  (itsdangerous + SQLite). Audit attribution uses `_resolve_user_for_request`
  instead of the old HMAC cookie parse.

### Deprecated
- `glados.tools.set_password` — emits a `DeprecationWarning`. Use
  `/setup`, Configuration → Users, or `GLADOS_AUTH_BYPASS=1` instead.

### New dependencies
- `argon2-cffi>=25.1.0` — Argon2id password hashing
- `itsdangerous>=2.2.0` — signed session cookies (same library Flask
  uses internally)

### Files added
- `glados/auth/{__init__,hashing,db,sessions,user_state,bypass,rate_limit}.py`
- `glados/webui/permissions.py`
- `glados/webui/setup/{wizard,shell}.py`
- `glados/webui/setup/steps/admin_password.py`
- `glados/webui/pages/{users,users_page,tts_standalone}.py`
- `glados/core/duration.py`
- `tests/test_*.py` (~25 new test files; +250 new tests; suite at 1350 passing)

### Rollback
The `auth-rebuild` branch can be reverted via standard git revert. A
snapshot of `configs/global.yaml` taken before deployment lets you
restore the legacy single-password fields if needed. See
`docs/AUTH_DESIGN.md` §11.

---

## Change 24 — auth rebuild post-deploy fixes (2026-04-25)

Three bugs surfaced by operator live-testing and were fixed in same-day
commits on top of the rebuild merge:

- **`auth.db` schema not self-initialised** (3511a28). `_user_state.record_*`
  was the first auth.db consumer on the login path and called `connect()`
  before `ensure_schema()`. Login crashed with `OperationalError: no such
  table: user_state`. Fixed by self-initialising the schema once per
  distinct DB path inside `connect()`.

- **WebUI UX gaps** (918e52c). Configuration was visible to chat users
  (showing 403s on click). Visiting `/` immediately redirected to the
  login form. There was no profile/account UI. Bottom-left of the
  sidebar now shows an Account block (username + role + Change Password)
  when authed, replaced by a Sign in button when not; Logout sits below.
  Configuration parent + children gated by `data-requires-admin`. New
  landing page initially landed and was later removed.

- **Admin migration not persisted; `/tts` audio non-functional;
  unauth UX redesign** (cd3bad2). Three issues in one commit:
  - The legacy-admin synthesizer skipped seeding admin once any non-admin
    user was in `users[]`, and `_merge_write_user_hash` never persisted
    the synthesized admin to YAML. Result: after the operator added a
    chat user, the dormant legacy `password_hash` stopped surfacing as
    an admin → operator locked out. Fixed by always seeding admin from
    the legacy hash when no admin role exists in `users[]`, and by
    making `_merge_write_user_hash` append-and-clear-legacy when the
    user isn't yet in YAML.
  - `/tts` standalone page treated `/api/generate`'s JSON response as
    raw audio bytes. Now parses JSON and uses `data.url`.
  - Unauth users got a small landing page and a stripped `/tts` page,
    not the standard SPA shell. The operator wanted the SPA shell at
    `/` with only TTS in the sidebar and a Sign in button at bottom-left.
    `/` now always renders the SPA shell. Sidebar items gain a
    `data-requires-auth` gate; TTS Generator drops its admin-only gate.
    `/tts` 302s to `/`. Landing page module deleted.

After these fixes, the live container at `0a6a03e3` accepted the
operator's `admin / glados` login, persisted the migration to YAML
(legacy `password_hash` cleared, admin appears in `users[]` alongside
the existing chat user), and TTS Generator works end-to-end for unauth
visitors.

---

## Change 25 — WebUI Polish Phase 2 + auth-perf fixes + history scrub (2026-04-25 → 2026-04-26)

The follow-on to Change 24's auth rebuild. Operator-flagged "polish
phase" addressing layout drift, design-language inconsistency, and a
list of per-page gripes flagged from live operation. Plus two
functional bug fixes uncovered along the way (maintenance speaker
sync, hyphen rendering in TTS), a hard-won auth-perf debug, and a
pre-public-announcement repo scrub via `git-filter-repo`.

**Phase 2 plan** lives at
`docs/superpowers/plans/2026-04-25-webui-polish-phase-2.md`. Audit at
`docs/ui-polish-audit.md`. Spec at
`docs/superpowers/specs/2026-04-25-webui-polish-design.md`.

### Foundation chunks (1A, 1B, 2)

- **1A — `a4be7c0`.** SSL + Users moved from sidebar to System tabs.
  Sidebar IA tightens to Chat / TTS Generator / Configuration → (System,
  Integrations, Audio & Speakers, Personality, Memory, Logs, Raw YAML).
  Implementation: legacy `tab-config-users` panel stays in the DOM but
  `_loadUsersIntoSystemTab()` clones its content into a System Users
  panel on tab activation. SSL: `_loadSslIntoSystemTab()` calls
  `cfgRenderSsl()` into the System SSL panel.
- **1B — `f4c16aa`.** Mechanical sweep: every primary action button
  unified to `.btn-primary` (orange filled, dark text — never
  white-on-yellow), all `<input>`/`<select>`/`<textarea>` use
  `--bg-sidebar` (`#16161a`) so inputs read as wells against
  `--bg-card`. Removed: Test Harness section (Hardware tab),
  Audio tab (Audio & Speakers). Renamed: "Chimes" → "Sounds".
  Reload-from-Disk button → `.btn-primary`. The `/api/test-harness/noise-patterns`
  endpoint is preserved (external test battery consumer); only the
  WebUI editor is gone.
- **2 — `73f253a`.** Services tab rebuilt as port-grouped status:
  in-container TTS / STT / API Wrapper render as one row on `:8015`
  with three status dots (no URL inputs — they're embedded). Vision
  on `:8016` is status-only with an `inactive` state when unconfigured.
  LLM (Ollama) folded in here from Integrations: URL + model dropdown
  + status, with an Advanced collapsible holding model_options
  (temperature, top_p, num_ctx, repeat_penalty) and LLM timeouts.
  The standalone Integrations → LLM card is gone; legacy
  `navigateTo('config.llm-services')` redirects to System → Services.

### Per-page rebuilds (3, 4, 5)

- **3 — `79e3d51`.** Personality page rebuild. HEXACO traits + PAD/Emotion
  baseline render as cards with description-above-bar, polarity labels
  under bar ends (e.g., `Unflappable` / `Anxious`), an editable numeric
  on the right (HEXACO 0-1 with 2 decimals; Emotion -1 to 1 with sign and
  a centre tick at 0). Bar capped at 320px regardless of viewport — the
  operator's main HEXACO complaint. Behavior tab's red sliders re-styled
  to orange. Disambiguation token descriptions rewritten in plain English
  ("Reduces likelihood that this entity will be selected" replacing
  "Loses 50 rank points"). Floor/Area aliases editor removed in favor
  of a notice card pointing at HA's native alias docs (HA aliases
  propagate automatically). **Most importantly:** the Preprompt editor
  in the Identity tab now reads/writes the actual `personality_preprompt`
  field in `glados_config.yaml` — previously the WebUI was reading the
  empty `preprompt` field and the persona text was only editable via
  shell. New `_read_personality_preprompt_from_engine` /
  `_write_personality_preprompt_to_engine` helpers in `tts_ui.py` mirror
  the field on every config GET/PUT for the personality section.
- **4 — `dd98a66` + `a6e471a` + `00beb51`.** Memory page rebuild. RAG
  explainer card at the top in plain English. Each fact row has icon
  Edit/Delete (pencil/trash, same SVGs as the TTS Generator). 5-point
  importance segmented control (Background `0.20` → Useful `0.40` →
  Important `0.60` → Critical `0.80` → Extreme `1.00`). "Recently learned"
  card replaces "Recent activity" — filters to `source==='user_passive'`
  so it shows facts she's auto-extracted from conversation, not facts
  the operator typed. Inline edit panel with importance segmented
  replaces the native `prompt()` dialog (`a6e471a`, `00beb51`).
- **5 — `c2d0af1`.** Users page row icons (Disable / Edit / Delete with
  hover-red on Disable when account is already disabled — the visual
  state flags the account, not the action). Reset PW button removed
  from rows (operator can edit user → set password if needed). Speakers
  tab rebuilt as a flat alphabetical list (no room grouping per the
  operator's "obnoxious to use" complaint), each row showing friendly
  name + entity ID + checkbox.

### Bug investigations (6a, 6b)

- **6a — `2156b7e`. Maintenance default speaker not honored.**
  Operator: "Living Room 2 selected, but Master Bedroom is the one
  that is playing whenever she speaks up right now." Root cause: the
  WebUI's Speakers Save POSTs `default: <entity>` into `speakers.yaml`,
  but the engine reads the maintenance speaker from HA's
  `input_text.glados_maintenance_speaker` — which still held the prior
  Master Bedroom value. Nothing connected the two. Fix:
  `_sync_maintenance_speaker_to_ha()` in `tts_ui.py` POSTs the new
  value to HA's `input_text/set_value` service whenever a Speakers
  PUT lands. Same pattern as `_sync_glados_config_urls` for Ollama
  URLs. 7 new tests covering empty/missing/whitespace defaults and
  HA failure handling.
- **6b — `a235ef3`. Hyphen / em-dash rendering in TTS.** Operator:
  "TTS treats hyphens as no pause. The hyphens she generates are weird,
  really long." Root cause: Piper TTS doesn't insert prosodic breaks
  on Unicode dashes (`-`, `–`, `—`); the persona rewriter (qwen3:14b)
  loves em-dashes stylistically. Fix: `_normalize_dashes()` helper
  added to BOTH `glados/api/tts.py:generate_speech` and
  `glados/webui/tts_ui.py:_apply_pronunciation_to_text` (both
  TTS-call sites). Replaces em-dashes, en-dashes, and spaced
  hyphen-minus with `, ` so Piper gets a comma-shaped pause. Compound
  hyphens (`tea-cup`, `long-running`) are preserved by word-boundary
  logic. 14 parametrized tests. Helper is duplicated across the two
  files intentionally — a shared util would create a circular import
  (`tts_ui` already imports from `glados.api.tts`).

### TTS Generator chat-thread refit (Chunk 8)

- **8 — `91dc9ea`.** Operator-requested redesign. The TTS Generator
  page now reads like the Chat tab: telemetry strip → mode toggle
  (Script/Improv) → scrolling thread of user/GLaDOS bubble pairs →
  docked input at the bottom. Each generation appends a User bubble
  (the typed text + timestamp) and a GLaDOS bubble (the spoken text
  + inline audio player + a metadata strip showing chars · synth time
  · file size + 3 icon-button actions: Download / Save-to-library /
  Delete). Page-load pre-populates the thread from existing audio
  files (oldest at top, newest just above the dock). The standalone
  player card and the file-list table are gone — the thread is the
  history. Save-to-library inline form expands under the bubble on
  save-action click instead of a persistent card.

  Note: synth time is measured client-side via `performance.now()`
  (round-trip latency including network). `/api/generate` doesn't
  return server-side synth duration. Approximation only.

### Auth-perf debug saga + hard SyntaxError fix

After Phase 2 chunks 1-5 deployed, operator reported "the WebUI is
broken even on a different system": refresh shows logged-out shell,
"a few moments later" UI updates to logged in; chat input doesn't
send. Hours of theorizing went the wrong direction
(Secure-cookie-on-self-signed-cert, single-threaded server blocking,
config reload thrash) before a screenshot of the operator's DevTools
Console showed the actual cause: `Uncaught SyntaxError: Identifier
'_PENCIL_SVG' has already been declared (at ui.js:1:1)`. Inline
`<script>` blocks share global window scope with `/static/ui.js`;
both Chunks 4 and 5 had added the same `const _PENCIL_SVG` declaration
(one in ui.js for the Memory chunk, one in `pages/users_page.py`'s
inline script). Duplicate `const` is a parse error that **aborts the
entire script** — every function definition past that line never
runs. checkAuth never fires, sign-in form's submit handler never
binds, etc.

The wrong-but-real-perf wins from the chase landed too:

- **`bb5cbb4` — auth.db pragmas.** Every auth check ran an
  `UPDATE auth_sessions SET last_used_at` + commit. On the host's
  bind-mount filesystem each commit took ~300ms in default
  `journal_mode=DELETE` + `synchronous=FULL`. Switched to WAL +
  `synchronous=NORMAL` (set in `connect()` so each new connection
  inherits both). 6× faster commits.
- **`0585616` — verify() is now read-only.** The auth-status endpoint
  no longer writes on every call. `last_used_at` updates coalesce in
  a per-process dict and a daemon thread flushes them to the DB every
  30s via `executemany`. Worst-case crash window: 30s of staleness in
  the Account → Sessions panel, acceptable for bookkeeping.
- **`823b8f7` — SyntaxError fix + Sessions card removal.** Each
  shared icon SVG (`_PENCIL_SVG`, `_TRASH_SVG`, `_DISABLE_SVG`) is now
  declared ONCE in `/static/ui.js`. The inline-script duplicate in
  `pages/users_page.py` is replaced with a comment pointing to
  `feedback_devtools_console_first.md`. Same commit removed the
  Active Sessions card on the Account tab per operator instruction
  ("session list is not necessary").

After the SyntaxError fix landed, `/api/auth/status` is now ~50ms
warm, ~60ms cold (from 700-1400ms before, in addition to the SPA
actually working at all).

### Pre-public-announcement repo scrub

The operator announced the repo on Reddit during this session. Before
the announcement:

- **`160df62` — Sensitive-info HEAD scrub.** Replaced operator-personal
  references in every tracked file: resident first names (`Chris` →
  `ResidentA`, `Cindy` → `ResidentB`), pet names (`Frito` → `Pet1`,
  `Blue` → `Pet2`, `Princess Fluffybutt`/`Prin Prin` → `Pet3`,
  `Cuddlewumps`/`Wumpy`/`The Good Queen Cuddlewumps` → `Pet4`,
  `Wobbles` → `Pet5`, `Mama Tips`/`Mama Kitty` → `Pet6`), the LAN IP
  in the Phase 2 plan doc. 18 tracked files touched.
- **History rewrite via `git-filter-repo`.** Two passes — first
  case-sensitive, then case-insensitive — to scrub the same patterns
  across every commit on every branch. Force-pushed all four branches
  (`main`, `webui-polish`, `stage1/pure-middleware`, `webui-refactor`).
  `git log --all -p` post-rewrite returns zero matches for the scrubbed
  patterns; only `.gitleaks.toml` retains the patterns as REGEX
  DEFINITIONS so future leaks are still caught. Cost: two ~593MB LFS
  uploads on force-push.
- **`93cf16b` — README + compose + .env.example rewrite.** Reflects the
  self-contained state. One container, two ports, three essential env
  vars (`OLLAMA_URL`, `HA_URL`, `HA_TOKEN`). `docker/compose.yml`
  shrank from 100 to 57 lines; `.env.example` from 62 to 40 lines.
  Speaches references gone (TTS = local Piper, STT = local Parakeet,
  embeddings = local BGE, ChromaDB = embedded). The README's Deploy
  section now duplicates the compose YAML inline so readers see what
  they're getting before pulling. Default image is `ghcr.io/synssins/glados-docker:latest`;
  `build:` block kept commented for source-builds.

### Operational helpers (committed)

- **`scripts/_local_deploy.py`** (`41a5c35`) — operational fallback when
  GHA is unavailable. Tars the worktree (excluding caches/`.git`/
  `.worktrees`/etc), SCPs to the host, runs `docker build` directly
  there using whatever LFS files are checked out locally, then
  recreates the glados container via the existing host compose. Used
  heavily this session because the GHA LFS bandwidth quota burned
  out twice during the history rewrites. Required env vars match
  `scripts/deploy_ghcr.py`.
- **`scripts/_inject_verbatim_rule.py`** (committed during scrub at
  `160df62`) — one-shot helper to inject or revert a "VERBATIM
  REPETITION" rule into the persona preprompt for TTS testing. Used
  for an operator-driven test mid-session and reverted the same
  session.

### Tests

1388 passed / 5 skipped (was 1355 pre-Phase-2). New coverage:
- 5 `_build_health_aggregate` tests (Phase 1 Task 3, prior session)
- 3 TTS pronunciation tests (Phase 1 Task 7, prior session)
- 7 maintenance speaker sync tests (Chunk 6a)
- 14 dash normalization parametrized tests (Chunk 6b)
- + minor adjustments in webui nav restructure tests across Chunks 1A/1B/2

### Live state at session end

- Image SHA: `sha256:e2d209975b6fb49c7723748b92f930505509f3dac3ae815f196c69a76cd3088c`
- Branch: `webui-polish` HEAD `91dc9ea`
- All Phase 2 work shipped. `webui-polish` has not been merged to `main`
  yet — operator wanted to live-review before merging.
- Public Reddit announcement made; repo at
  `https://github.com/synssins/glados-docker` is open.

---

## Change 26 — Autonomy triage split + service slot rename to `llm_*` (2026-04-27 → 2026-04-28)

The 2026-04-27 chat investigation traced the empty-WebUI-bubble symptom
to two compounding issues: (1) URL mangling that pointed the engine at
`/v1/chat/completions/api/chat`, and (2) LM Studio's 4096-token context
window overflowing on every request because the autonomy lane sent
~5 KB of memory_context + ~1500-token persona prompt + 40-message
conversation history. The URL fix shipped in `cbd971b`. This Change
addresses (2) — autonomy will no longer overflow ctx because (a) its
classification/summarization callers route through a small fast model
(Llama-3.2-1B-Instruct) on a new `llm_triage` service slot, and (b) the
`llm_call` helper enforces an 8000-char user_prompt budget with
truncation + WARNING log so any remaining oversize prompt fails soft
instead of crashing LM Studio.

Concurrent OpenAI-compliance cleanup: the `services.yaml` schema slots
got renamed from `ollama_*` to `llm_*` to match the operator's mandate
that GLaDOS speaks OpenAI everywhere internally. Pydantic
`AliasChoices` keeps existing operators' `services.yaml` parsing for
one release; on next save the file is rewritten with the new names.

### Plan + execution
- Plan: `docs/superpowers/plans/2026-04-28-autonomy-triage-split.md`
- Companion task tracker: `docs/superpowers/plans/2026-04-28-autonomy-triage-split.md.tasks.json`
- Executed via `superpowers-extended-cc:subagent-driven-development`
  in a single session (each task: implementer → spec reviewer → code
  quality reviewer → optional fix-commit).

### Commits
| Commit | Effect |
|---|---|
| `d388b1e` | `ServicesConfig` schema rename: `ollama_*` → `llm_*` slots, new `llm_triage` slot defaulting to `llama-3.2-1b-instruct`. AliasChoices keep legacy yaml parsing. (+4 tests; full suite intentionally red until next commit.) |
| `186a076` | Migrate consumer call sites (engine.py, webui/tts_ui.py, autonomy/llm_client.py, server.py, doorbell/screener.py, webui/static/ui.js) and 3 existing tests to `services.llm_*`. Browser-side dict keys flipped to stay in sync with server. Suite back to green. |
| `49183c1` | `ui.js` follow-up: 3 string-prefix predicates (`_svcDiscoverKind`, `_isLLM`, `_svcHealthKind`) flipped from `'ollama'` to `'llm_'` so LLM rows render correctly on Services / Integrations pages. Pytest can't catch this — JS-only. |
| `1f11e03` | `LLMConfig.for_slot(slot, *, timeout=30.0)` classmethod. Callers ask for slot by name; unknown slot raises ValueError. (+5 tests.) |
| `6b12329` | `MAX_AUTONOMY_USER_PROMPT_CHARS = 8000` budget enforced inside `llm_call`. Truncates oldest content with `[…truncated…]` sentinel; emits WARNING log. (+6 tests.) |
| `e5d4d49` | `summarization.summarize_messages` and `extract_facts` resolve `LLMConfig.for_slot("llm_triage")` internally; `compaction_agent.py` caller updated; `memory_writer.classify_and_extract` builds a triage config and routes both classifier + extractor calls through it. (+4 tests.) |
| `e3eae90` | Drop the now-dead `llm_config` parameter from `classify_and_extract` (was kept for "API compat" but silently ignored — symmetry cleanup with summarization.py). |
| `53147f6` | Tier 2 disambiguator construction in `glados/server.py:_init_ha_client` switches to `LLMConfig.for_slot("llm_triage")`. Boot-log message reads `Tier 2 disambiguator ready; … (slot=llm_triage)`. |
| `7c6abb5` | Trailing-slash normalization + model fallback safety on Tier 2 triage resolution (preserves the OLD `cfg.service_url` / `cfg.service_model("…", fallback=…)` behavior when the slot is partially configured). |
| `12b7af3` | WebUI Services tab `SERVICE_NAMES` labels flip from `"Ollama X"` to `"LLM (X)"`. Adds `llm_triage: "LLM (Triage)"` entry. Renderer produces a fourth card for `llm_triage` with status dot + URL input + model dropdown. |

### What's verified live (2026-04-28)
- Container deploy clean. Engine boot log says
  `Tier 2 disambiguator ready; ollama=http://aibox.local:11434/v1/chat/completions model=llama-3.2-1b-instruct (slot=llm_triage) semantic=True`
  — Task 5's routing landed.
- Engine reconciler reads from `services.llm_interactive.model` and
  `services.llm_autonomy.model` (Task 1's flip live).
- Autonomy retry storm of `Context size has been exceeded` errors —
  GONE. Container logs over 5 minutes show only `LLM call: user_prompt
  truncated` WARNINGs from Task 3's budget firing on the
  expected-large autonomy summarization calls.
- Streaming chat through the WebUI delivers content delta events
  with full GLaDOS persona ("I am Glad oh ess..."). Operator can
  use the Chat tab.
- LM Studio side: `qwen3-30b-a3b` (hybrid, ctx=12288, parallel=2)
  routing chat + autonomy at 14.58 GB; `llama-3.2-1b-instruct`
  (ctx=4096, parallel=2) handling triage at 1.32 GB. Total VRAM
  ~22 GB on the 24 GB B60 with safe margin. Vision unloaded;
  operator opts in when ready.
- Test suite: **1447 passed / 5 skipped**. +25 tests vs.
  pre-Change-26 baseline.

### Known minor follow-ups (tracked in SESSION_STATE.md)
- `MAX_AUTONOMY_USER_PROMPT_CHARS` truncation log uses `%d`
  placeholders that loguru doesn't expand — appears in container
  logs as the literal string `"truncated from %d to %d chars"`.
  Pre-existing file-wide style issue (other `logger.warning` lines
  in the same file have the same format-string-not-expanded behavior).
  Cosmetic; functional truncation works correctly.
- `test_config_save_writes_llm_keys` exercises `model_dump(exclude_none=True)`
  not the production `update_section()` save path. Narrow regression
  risk if a future change adds `serialization_alias` to the schema.
  Tighten with an on-disk round-trip when the next config-save
  refactor lands.
- No regression test for the disambiguator's slot-resolution path
  inside `_init_ha_client`. Existing 125 disambiguator tests cover
  the class with explicit constructor-args injection but don't
  exercise the slot lookup.
- Vision model (`qwen2.5-vl-3b-instruct`) is unloaded per operator
  direction during the prior session's chat investigation. With
  Llama-3.2-1B at 1.3 GB now resident, three-model VRAM math is
  tight (14.58 + 3.27 + 1.32 = ~19.2 GB weights + ~7 GB KV =
  ~26 GB total; over cap). Reload requires either dropping chat
  ctx to 8K or vision parallel to 2.

### Public-repo discipline notes
No new secrets shipped this Change; all credential-bearing values
live in `C:\src\SESSION_STATE.md` (gitignored). The only LAN IP in
the diff is `aibox.local` which lives in operator-side
`services.yaml` (also gitignored under `configs/`). Pre-commit
gitleaks rules ran clean on every commit.

---

## Change 27 — WebUI LLM-config UX repair + URL UX simplification (2026-04-28)

Same-day follow-up to Change 26. Three issues operator-flagged after
deploy verification:

1. **Watchtower silently overwriting deploys.** The `_local_deploy.py`
   build wrote a fresh image and tagged it `latest`, but
   `containrrr/watchtower` (running on the docker host) auto-pulls
   `ghcr.io/synssins/glados-docker:latest` from GHCR every hour and
   reverts the local tag to GHCR's published image (still the
   2026-04-25 base because we haven't pushed there since the LFS
   scrub). The session's first build was clobbered ~5 hours later
   without any visible failure — `docker compose up` succeeded, the
   build log showed a fresh image SHA, but the running container ran
   the stale registry image. Container's `/app/glados/webui/static/ui.js`
   was 3 days old when we verified.

2. **Phase 2 Chunk 2 left System → Services with one hardcoded LLM
   card.** Commit `73f253a` (2026-04-25 prior session) consolidated
   the previously-separate Interactive / Autonomy / Vision cards
   into a single hand-rolled card hardcoded to `llm_interactive` and
   forced its URL+model onto `llm_autonomy` on save, ignoring
   `llm_vision`. After Change 26 added `llm_triage`, all four
   non-Interactive slots became unconfigurable via the WebUI —
   operators could only edit raw YAML.

3. **Operators forced to type full chat-completion URL** as
   `http://host:port/v1/chat/completions`. The path is an OpenAI
   protocol detail (`/v1/chat/completions` is canonical per
   `platform.openai.com/docs/api-reference/chat`); first-time setup
   shouldn't require operators to know it. Same for `/v1/models` on
   discover, `/v1/audio/transcriptions` on STT, etc.

### Commits

| Commit | Effect |
|---|---|
| `24b9938` | `loadSystemServices` replaces the hand-rolled single LLM card with the existing `cfgRenderServices(filtered, scope='llm')` 4-card grid; `_cfgSaveSystemServices` iterates all `llm_*` keys in SERVICE_NAMES and writes each slot independently. Operator can now configure Interactive / Autonomy / Triage / Vision URL+model independently. Removed the orphan `_systemLlmDiscover`. |
| `387f859` | New `glados/core/url_utils.py` with `strip_url_path()` + `compose_endpoint()` helpers. URL field accepts and stores bare `http://host:port` (no path). Engine + every consumer (api_wrapper, llm_processor, llm_client, llm_decision, persona/rewriter, persona/llm_composer, intent/disambiguator, doorbell/screener) appends `/v1/chat/completions` at dispatch time. Dropped `_ollama_mode` flag, `_is_ollama_endpoint()`, `_sanitize_messages_for_ollama()` from `llm_processor.py` — always speaks OpenAI now. WebUI placeholder `http://host:port`, hint "Server URL — paths added automatically (e.g. /v1/chat/completions)". 21 new url_utils tests + 2 test files rewritten for bare-URL semantics. |
| `84a5a4b` | `_validate_llm_urls` enforces port-required (`http://host` REJECTED with error mentioning port). Defensive try/except for `urlparse`/`.port` ValueError so malformed inputs return a clean error string. 11 new validator tests. |

### Operational fix: watchtower exclusion

Added `com.centurylinklabs.watchtower.enable=false` label to the
glados service in the operator's host-side `docker-compose.yml`
(`/srv/.../data/docker/compose/docker-compose.yml`). Watchtower
now skips glados entirely; future `_local_deploy.py` builds aren't
clobbered. The label change lives only on the host (compose.yml is
not in this repo), so it's noted here for the operator's records.

### Live-verified state (2026-04-28 ~05:30 UTC)

- Container running image SHA `857d8d434264…` from this session's
  build. Watchtower exclusion label applied; subsequent automatic
  pulls skipped.
- Served `ui.js` (302,855 bytes, post-deploy fetch from
  `glados.example.com:8052/static/ui.js`) confirms:
  - 4-card LLM grid renderer present
  - `http://host:port` placeholder present (2 occurrences — input
    + description)
  - Hint "paths added automatically" present
  - No `system-llm-url` orphan IDs
- Engine boot log: `Tier 2 disambiguator ready; ollama=… model=llama-3.2-1b-instruct (slot=llm_triage)`
  — Change 26 routing intact through this round of changes.
- Test suite: **1483 pass / 5 skip / 0 fail** (was 1447 → 1470 → 1483
  across the three Change 27 commits).

### Test count delta

- Change 26 baseline: 1447
- After `24b9938` (LLM-cards fix): 1447 (no new tests; fix verified
  via served-JS markers + manual)
- After `387f859` (URL UX): 1470 (+23 from `tests/test_url_utils.py`)
- After `84a5a4b` (port-required validator): 1483 (+13 from new
  `TestValidateLlmUrls` class)

### Follow-ups (carried to SESSION_STATE.md)

- The simpler 1-URL-N-models card layout (one URL + four model
  dropdowns + Advanced toggle that exposes the 4-card view) is the
  next logical UX iteration — Change 27's 4-card grid is full
  flexibility, but the typical operator with one LLM server wants to
  type one URL and pick four models. Plan owed.
- Chat speed investigation — chitchat round-trips ~30-90 s when
  `/no_think` should give <5 s. Verify directive flows through to
  LM Studio and that autonomy isn't slot-starving chat.
- The `%d`-log-placeholder bug in `glados/autonomy/llm_client.py`
  (truncation log line uses printf format that loguru doesn't
  expand) — pre-existing file-wide style issue.
- Tighten `test_config_save_writes_llm_keys` to exercise
  `update_section()` save path.
- Add regression test for disambiguator slot-resolution inside
  `_init_ha_client`.
- HA token regen (operator-side, carried).
- Vision model reload — VRAM math still tight; need chat ctx drop or
  parallel reduction.

---

## Change 28 — Chat-speed remediation: %s loguru fix + LM Studio JIT disable (2026-04-28 PM)

**Symptom**: WebUI chitchat round-trips at ~30–90 s TTFT despite the prior
session showing /no_think directive injection working at the api_wrapper
layer. Operator's screenshots: "Who are you and what do you do" — TTFT
14.7 s; previous sessions reported the 30–90 s range.

**Investigation** (Phase 1, evidence before fixes):

1. Probed LM Studio (aibox.local:11434) directly with five payload
   variations to bisect the directive layer:
   - A: plain "hello", no directive → 1.42 s, 284 reasoning_content tokens.
   - B: `/no_think` on user → **0.49 s**, 2 reasoning tokens.
   - C: `/no_think` on system + persona → **0.53 s**, 2 reasoning tokens.
   - D: `chat_template_kwargs.enable_thinking=false` (LM Studio kwarg) →
     4.61 s, 348 reasoning tokens. **NOT honored** by qwen3-30b-a3b on
     this build.
   - E: kwarg + `/no_think` → 0.36 s, 2 reasoning tokens.
   - Conclusion: `/no_think` works at LM Studio + qwen3-30b-a3b. The
     directive injection at `api_wrapper.py:1825` was correct.

2. Probed the api_wrapper SSE chitchat path from inside the docker network
   (no auth needed at port 8015): TTFB **0.15–0.38 s**, full GLaDOS reply
   delivered in 42 chunks. So the SSE path itself isn't slow either.

3. Found the actual bottleneck: `lms ps` and `/api/v0/models` both reported
   `qwen3-30b-a3b` loaded with `loaded_context_length=4096` despite session
   state expecting **12288**. With the post-compaction history at
   ~28 messages (~5–10 K tokens) being sent on every chat, prompts
   regularly exceeded 4096 ctx → LM Studio re-prefilled / truncated /
   context-shifted on every request.

4. Reloaded model with `lms load qwen3-30b-a3b -c 12288 --parallel 4 --gpu
   max --ttl 3600 -y` → ctx=12288 confirmed via `/api/v0/models` →
   first chitchat fired → ctx **back to 4096**. **LM Studio's JIT
   loader was reverting the CLI-set ctx whenever a request hit a
   JIT-managed model**, falling back to the model-bundled default
   (4096). LM Studio has no `lms preset save` CLI subcommand and
   `~/.lmstudio/config-presets/` was empty; per-model defaults are
   GUI-only — and the GUI is unreachable on this Windows Server
   install.

5. The autonomy `LLM call failed: %s` warnings flooding container logs
   every 5 min were also load-bearing: the actual exception text was
   being silently dropped by loguru (printf-style placeholder bug),
   hiding the autonomy-side ctx overflow that was the upstream cause
   of the conversation_store accumulating 11 K-char prompts before
   the truncation cap kicked in.

**Fixes shipped:**

1. **`glados/autonomy/llm_client.py`** (commit `19eaddb`, image SHA
   `69ec8e5f4a61`): switched three `logger.warning(...)` calls from
   printf-style `%s`/`%d` to loguru `{}` placeholders.
   - Line 100–103: `LLM call: user_prompt truncated from %d to %d chars`
   - Line 175: `LLM call failed: %s`
   - Line 178: `LLM call: failed to parse response: %s`
   Test suite: 1483 pass / 5 skip / 0 fail. Post-deploy verification:
   the truncation log immediately reported real values
   (`truncated from 11585 to 8015 chars`); the autonomy compaction
   call that had been failing every 5 min on the old ctx=4096 build
   succeeded after deploy, compacting history `6863 → 4342 tokens`
   and saving 24 facts to ChromaDB.

2. **`~/.lmstudio/settings.json` on AIBox** (operator-approved,
   AIBox-side change, NOT in repo): set
   `developer.jitModelTTL.enabled: false`. JIT auto-load and
   per-request preset re-evaluation are now disabled; manual
   `lms load` is authoritative. Backup at
   `~/.lmstudio/settings.json.bak.20260428T154426`. Bounced
   `lms server` and reloaded both models without `--ttl`:
   - `qwen3-30b-a3b`: ctx=12288, parallel=4
   - `llama-3.2-1b-instruct`: ctx=4096, parallel=2
   Verified ctx persists across an api_wrapper chitchat.

3. **AIBox-side autoload** (host-only, not in repo):
   `~/.lmstudio/lms_autoload.bat` + `~/.lmstudio/NSSM_INSTALL.md`.
   Boot-time NSSM service `LMStudioAutoload` ensures both models are
   loaded with the right params after every reboot. Idempotent;
   keepalive loop keeps NSSM happy.

**Live verification (post-fix-2):**

- Operator's "What is the forecast today?" — TTFT **3.2 s**, LLM 6.2 s,
  TTS 6.5 s, **Total 10.6 s**. Down from 30–90 s.
- Previous chat at TTFT 14.7 s was the cold-prefill of the very first
  chat after the reload + JIT-revert; the subsequent chats reuse KV
  prefix and stay around 3 s.

**Open observations (deferred / not fixed in this Change):**

- **Home-command path slow**: operator's "Turn off the office lights" —
  TTFT **19.3 s**, LLM 20.1 s, **no rendered reply**. With
  `is_home_command=True` the api_wrapper loads the MCP tool catalog
  (~10 K tokens) and reinforcement system message; comment at
  `api_wrapper.py:1513` already notes "~3 s → ~80 s latency" for this
  path. Empty rendered reply may be related to MCP `home_assistant`
  401 errors (every 2 s) — the planner may be falling silent when its
  tool calls fail. Carried for a future session.
- **`looks_like_home_command("good morning")` → True** via
  `activity_phrase`. Greetings hit the heavy home-command path.
  `glados/intent/rules.py:264`. Trivial fix: drop `good morning` from
  the activity-phrase list.
- **~30 other `%s`/`%d` loguru placeholder bugs** scattered across
  `glados/autonomy/agents/*` (camera_watcher, weather, hacker_news,
  emotion_agent), `subagent_manager.py`, `subagent.py`,
  `subagent_memory.py`, `task_manager.py`, `jobs.py`,
  `core/knowledge_store.py`. Same bug class; one sweep commit would
  clear it. None are firing as visibly as the autonomy/llm_client.py
  ones were.
- **`tokens_per_second` always None in metrics** because LM Studio's
  OpenAI-compat endpoint doesn't emit Ollama's `eval_count`/
  `eval_duration` fields. Adding `stream_options:{include_usage:true}`
  to the outbound payload (`api_wrapper.py:1836`) would surface the
  OpenAI-style `usage` field; the ollama_metrics parser at line
  2019–2031 would need a corresponding branch to read it, and
  `tok_per_sec` would compute from `completion_tokens / generation_time`.
  ~10 lines of code; carried.
- **Vision model unloaded** — three-model VRAM math still tight,
  unchanged from prior session.
- **HA token regen** — operator-side; flagged as "should not be needed
  for HA" by operator, so root cause is something else (stale token
  in services.yaml, HA-side wrong token, MCP path). Carried.

**Files touched (committed):**

- `glados/autonomy/llm_client.py` (3 log-format edits).

**AIBox host changes (not in repo, documented in SESSION_STATE.md):**

- `~/.lmstudio/settings.json` (jitModelTTL.enabled false).
- `~/.lmstudio/lms_autoload.bat` (new).
- `~/.lmstudio/NSSM_INSTALL.md` (new).
- LM Studio: bounced server, reloaded both models with explicit ctx.

## Change 28b — NSSM autoload service installed + duplicate-load fix (2026-04-28 evening)

Follow-up to Change 28. Three things landed after the Change 28 commit
was pushed; documented here so the chronological log stays accurate.

**What happened:**

1. **`LMStudioAutoload` NSSM service installed and started.** Per the
   procedure in `~/.lmstudio/NSSM_INSTALL.md`:
   - `nssm install LMStudioAutoload "%USERPROFILE%\.lmstudio\lms_autoload.bat"`
   - `AppDirectory`, `Start=SERVICE_AUTO_START`, log routing,
     `AppExit Default Restart`, `AppRestartDelay 10000`, run as
     `.\Administrator`.
   - `nssm start LMStudioAutoload` → `SERVICE_RUNNING`.

2. **Duplicate-instance bug discovered post-install.** First
   verification revealed `lms ps` reporting both `qwen3-30b-a3b` at
   ctx=4096 (the pre-existing instance from earlier in the session)
   AND `qwen3-30b-a3b:2` at ctx=12288 (loaded by the autoload script).
   `lms load` does **not** replace an existing instance — it suffixes
   `:2`, `:3`, etc. The autoload script as originally written would
   have accumulated duplicates on every service restart, eating VRAM
   and leaving the wrong-ctx instance available for inbound requests.

3. **Script corrected and state cleaned.** Added an unload pass to
   `~/.lmstudio/lms_autoload.bat` covering `qwen3-30b-a3b`,
   `qwen3-30b-a3b:2`, `qwen3-30b-a3b:3`,
   `llama-3.2-1b-instruct`, `llama-3.2-1b-instruct:2` (errors swallowed
   so "nothing to unload" is fine). Stopped the service, unloaded all
   instances by hand, restarted the service. Final state: one
   `qwen3-30b-a3b` at ctx=12288 + one `llama-3.2-1b-instruct` at
   ctx=4096. Service running, auto-start enabled.

**Operational lesson recorded:** during the install / verify cycle,
exploratory `nssm dump LMStudioAutoload` was run — that command (with
no args) silently opens a modal GUI dialog on Windows that the operator
has to dismiss. Saved as memory `feedback_no_probe_commands.md`:
execute the action, read the exit code; do not pre-probe with
`nssm dump` / `nssm list` / repeated `lms ps` / `whoami` / `sc query`.

**AIBox host changes (not in repo):**

- `~/.lmstudio/lms_autoload.bat` — added unload-before-load step.
- NSSM: installed + started `LMStudioAutoload` service, set
  `SERVICE_AUTO_START`, run-as `.\Administrator`, restart-on-exit.

**Carried to next session:**

- First-reboot verification of `LMStudioAutoload` still owed. Confirm
  service auto-starts, log shows fresh banner, `lms ps` shows clean
  state (one instance per model, no `:2` suffixes).

## Change 29 — tokens/sec on OpenAI-compat SSE + README/models doc refresh + main merge (2026-04-28 evening)

Three things landed together as the trailing commits on `webui-polish`
before merging the branch into `main`.

**1. `tokens/sec` metric on the OpenAI-compat SSE path** (commit
`74f7f6a`). LM Studio's `/v1/chat/completions` doesn't emit Ollama's
`eval_count` / `eval_duration` fields, so the WebUI chat-stats bar's
`tok/s` cell was always blank when the container talked to LM Studio.
Three minimal edits in `glados/core/api_wrapper.py`:

- Set `stream_options: {include_usage: true}` on the outbound payload,
  gated on `/v1/` in the upstream path so Ollama-native (`/api/chat`)
  is unaffected.
- Capture the terminal `usage` chunk's `prompt_tokens` and
  `completion_tokens` into the same `ollama_metrics` keys the metrics
  emitter already reads.
- Fall back to wall-clock first-token → stream-end time for tok/s when
  per-token timing isn't reported (OpenAI-compat case). Prompt-prefill
  time correctly excluded from the throughput denominator.

WebUI side already renders `timing.tokens_per_second` when present
(`glados/webui/static/ui.js:5233`). Live-verified post-deploy: a
bare-prompt chitchat returned `tokens_per_second: 70.2` against
qwen3-30b-a3b on LM Studio, with `eval_duration_ms: 0.0` confirming
the wall-clock fallback is what fired.

**2. README + new `docs/models.md`** to make the OpenAI compatibility
contract explicit:

- README preamble + dependency table now leads with "any
  OpenAI-compatible LLM endpoint" instead of Ollama-specifically.
- New "OpenAI API Compatibility" section enumerates the `/v1`
  endpoints exposed on port 8015, the streaming features
  (`stream_options.include_usage`, tool-call deltas, `/no_think`
  injection), and the bare-`scheme://host:port` URL UX.
- "Models" section rewritten around the four LLM slots
  (`llm_interactive`, `llm_autonomy`, `llm_triage`, `llm_vision`)
  with both `qwen3:14b` and `qwen3-30b-a3b` named as tested-good
  options.
- New `docs/models.md` covers VRAM math, throughput numbers, the LM
  Studio JIT-loader gotcha, and trade-offs between the 14B and 30B
  chat options. Also covers the triage and vision slots.

**3. `webui-polish` → `main` merge.** ~70 commits accumulated on the
polish branch since the LFS scrub re-baseline; the trunk is now caught
up. Merge was `--no-ff` to preserve the topical commit history.

## Change 30 — TLS coverage on every external container port (2026-04-29)

Operator surfaced 2026-04-29 while wiring HA's `openai_tts` integration:
its default URL is `https://api.openai.com/v1/audio/speech` and many
OpenAI-protocol clients won't tolerate manual scheme rewriting. Pre-fix,
only the WebUI on 8052 honored the SSL cert; the OpenAI API on 8015
and the HA audio file server on 5051 served plaintext-only regardless
of cert state. The README's "OpenAI API Compatibility" section was
implicitly over-promising.

**What landed:**

- **New `glados/core/tls.py`** — single source of truth for "should
  this listener be TLS-wrapped?" Decision is on file presence
  (`/app/certs/{cert,key}.pem` by default, or `SSL_CERT` / `SSL_KEY`
  env override). Public surface:
  - `get_ssl_context() -> ssl.SSLContext | None`
  - `maybe_wrap_socket(server) -> str` (returns `"https"` or `"http"`)
  - `is_tls_active() -> bool` (for URL builders)
  - `internal_api_url()` / `internal_api_port()` (loopback caller helper)
- **`api_wrapper.py:main()`** — public listener on `0.0.0.0:8015`
  TLS-wraps via `maybe_wrap_socket`. New always-plain-HTTP listener
  on `127.0.0.1:18015` (env `GLADOS_INTERNAL_API_PORT`) for in-
  container callers. Avoids the cert-doesn't-cover-localhost
  mismatch entirely without skip-verify hacks.
- **`audio_io/homeassistant_io.py`** — file server on `0.0.0.0:5051`
  TLS-wraps the same way. URL builders for `media_content_id` reflect
  the listener's actual scheme. Sonos / Alexa / cast renderers fetch
  the URL HA hands them — modern firmware handles HTTPS as long as
  the cert chain validates (LE-via-DNS clean; self-signed needs CA
  trust).
- **Migrations** — four hardcoded `http://localhost:8015` callers
  moved to the loopback internal port:
  - `webui/tts_ui.py:2571` (streaming-chat connection)
  - `autonomy/agents/ha_sensor_watcher.py:115` (announce_url default)
  - `autonomy/agents/ha_sensor_watcher.py:1392` (already used the
    config helper — auto-routes via the default change)
  - `core/config_store.py:347-356` (`tts` / `stt` / `api_wrapper`
    service URL defaults flipped to `http://127.0.0.1:18015`)
- **`engine.py`** + **`doorbell/screener.py`** + two more
  `ha_sensor_watcher.py` sites — URL builders for the audio file
  server now consult `is_tls_active()` so the scheme matches the
  listener.

**Behavior matrix (post-fix):**

| Operator setup | 8015 / 8052 / 5051 speak | OpenAI clients connect via |
|---------------|---------------------------|----------------------------|
| No cert mounted | plain HTTP | `http://<host>:8015/...` |
| LE cert + DNS resolves on LAN | HTTPS, validates cleanly | `https://<cert-domain>:8015/...` |
| Self-signed cert | HTTPS, client must trust CA or skip-verify | `https://<host>:8015/...` |
| Bare IP, no cert | plain HTTP (universal floor) | `http://<ip>:8015/...` |

**Tests added:**

- `tests/test_tls_helper.py` — 8 tests covering `get_ssl_context`
  (with and without cert files), `maybe_wrap_socket` (verifies the
  socket actually becomes an `SSLSocket` when a cert is loaded),
  internal port env-override + invalid-value fallback, and the
  loopback URL shape.
- `tests/test_config_defaults.py` — updated the two URL-pinning
  assertions for the new internal-port defaults.

Suite: 1497 pass / 5 skip / 0 fail.

**Caveats:**

- Self-signed cert + Sonos/Alexa: media renderer must trust the
  self-signed CA or it'll refuse to fetch the audio URL. LE-with-DNS
  setups (the operator's deployment) are unaffected.
- The hostname HA points at must match the cert's CN/SAN for
  validation to pass — `https://<bare-IP>:8015` against a domain
  cert fails verification by design (TLS-side, not container-side).
  Operators on bare IP without a domain stay on plain HTTP.

**Files touched:**

- `glados/core/tls.py` (new)
- `glados/core/api_wrapper.py` (main: dual listener)
- `glados/core/config_store.py` (default service URLs)
- `glados/core/engine.py` (audio URL scheme)
- `glados/audio_io/homeassistant_io.py` (TLS wrap + URL scheme)
- `glados/doorbell/screener.py` (audio URL scheme)
- `glados/autonomy/agents/ha_sensor_watcher.py` (announce_url default
  + audio URL schemes)
- `glados/webui/tts_ui.py` (streaming-chat conn → loopback)
- `tests/test_tls_helper.py` (new)
- `tests/test_config_defaults.py` (default URL assertions)
- `README.md` (Ports table + new "TLS for OpenAI-compat clients"
  subsection)
- `docs/roadmap.md` (TLS-coverage entry tracking the work)

## Change 31 — Plugin system scaffolding (Phase 2a) (2026-04-29)

Operator-driven research session 2026-04-29 settled on a plugin
architecture for tying arbitrary services (Sonarr, Radarr, Spotify,
Tautulli, GitHub, etc.) into GLaDOS without per-plugin code in this
repo. Plugins are MCP servers conforming to the official `server.json`
manifest format (current schema `2025-12-11`) — the same format the
official MCP Registry uses for publishing. GLaDOS reads the manifest
generically; the WebUI auto-renders the install form from
`environmentVariables[]` and `remotes[].headers[]`.

**This change ships the scaffolding only** — the on-disk format,
manifest parser, loader, runner, and engine wire-in. The WebUI panel,
runtime subprocess spawn (uvx/npx), curated catalog repo, and HA
mcp_server wiring are follow-up phases tracked in `docs/roadmap.md`.

**New module: `glados/plugins/`**

- `manifest.py` — Pydantic models for `server.json` (`ServerJSON`,
  `Package`, `Remote`, `EnvironmentVariable`, `RemoteHeader`,
  `InputArgument`, `Variable`) + `RuntimeConfig` for the GLaDOS-side
  runtime state. Strict at the top level (extra fields rejected) but
  open under `_meta` per spec. GLaDOS-namespace `_meta` accessors
  expose `category`, `icon`, `min_glados_version`,
  `recommended_persona_role`.
- `loader.py` — `discover_plugins()` walks `/app/data/plugins/*/`,
  parses each `server.json` + `runtime.yaml` + `secrets.env`. Broken
  plugins are logged and skipped — never raised — so one malformed
  manifest doesn't block the others. Disabled plugins
  (`runtime.yaml.enabled: false`) are filtered out. Plugins dir is
  env-driven via `GLADOS_PLUGINS_DIR`.
- `runner.py` — `plugin_to_mcp_config()` translates a loaded `Plugin`
  to the existing `glados.mcp.config.MCPServerConfig`, so the existing
  `MCPManager` consumes plugins without changes. Resolves env values
  by merging defaults from the manifest, `runtime.yaml.env_values`,
  and `secrets.env` (secrets win on collision). Same path for remote
  headers. Required envs/headers raise `ManifestError` with a clear
  message about which key is missing.
- `store.py` — atomic read/write helpers for `runtime.yaml` and
  `secrets.env`. `secrets.env` is written with mode 0600 (best-effort
  on non-POSIX).
- `errors.py` — `PluginError`, `ManifestError`, `InstallError`.

**Engine wire-in**: `glados/core/engine.py` now calls
`discover_plugins()` at startup and merges results with any
`mcp_servers` passed in via config (`services.yaml`). Failure of the
plugin layer NEVER blocks engine startup — exceptions are caught and
logged. `mcp_servers` config + plugin-discovered configs are unioned;
plugins add to the catalog rather than replacing it.

**Storage layout** (under `/app/data/plugins/`, survives image rebuilds):

```
/app/data/plugins/<plugin-name>/
├── server.json    # manifest, drives WebUI form rendering
├── runtime.yaml   # operator's resolved values + enabled flag + package_index/remote_index
├── secrets.env    # mode 0600, isSecret:true env values
└── .uvx-cache/    # runtime spawn cache (Phase 2b)
```

**Tests**: 22 new across two files.
- `tests/test_plugins_manifest.py` — full server.json shapes (minimal,
  mcp-arr-style local stdio, HA-style remote streamable-HTTP),
  GLaDOS-namespace `_meta` accessors with default-when-missing,
  invalid-fields rejection, `RuntimeConfig` YAML round-trip.
- `tests/test_plugins_loader.py` — discovery / load_plugin / runner
  end-to-end on tmp_path fixtures: remote plugin → http
  MCPServerConfig with templated URL, stdio plugin → uvx command +
  resolved env, disabled plugin skipped, broken plugin logged but
  not raised, runtime/manifest name-mismatch and out-of-range
  index rejected, dot-directory skip, env-driven plugins-dir
  override.

Suite: 1519 pass / 5 skip / 0 fail (was 1497).

**Architecture document**: `docs/plugins-architecture.md` is the
canonical reference — all the design decisions, `_meta` extensions,
storage layout, runtime mapping, trust posture, and phasing.

**What works end-to-end today**:
- Drop a fully-prepared plugin folder into `/app/data/plugins/<name>/`
- Restart the container
- Plugin's tools become available to the LLM via the existing
  `MCPManager` chat-tool path
- Remote plugins (`remotes[]`, e.g. HA's `mcp_server`) work fully
- Local stdio plugins (`packages[]`) parse cleanly but the
  subprocess spawn (uvx/npx) hasn't been wired into `MCPManager`
  yet — that's Phase 2b

**What's deferred to Phase 2b (next session)**:
- WebUI Plugins panel (System → Services → Plugins)
- Runtime subprocess spawn for stdio plugins via uvx/npx
- Hot-reload (file watcher on /app/data/plugins/, no restart needed
  on install/remove)

**What's deferred to Phase 3+**:
- Curated `synssins/glados-plugins` GitHub repo with `index.json` +
  hand-written `server.json` files for plugins whose upstream
  doesn't yet ship one
- "Browse Plugins" gallery in the WebUI
- HA `mcp_server` as the first cataloged plugin

**Files touched**:

- `glados/plugins/__init__.py` (new)
- `glados/plugins/errors.py` (new)
- `glados/plugins/manifest.py` (new)
- `glados/plugins/loader.py` (new)
- `glados/plugins/runner.py` (new)
- `glados/plugins/store.py` (new)
- `glados/core/engine.py` (plugin discovery merged into MCPManager init)
- `tests/test_plugins_manifest.py` (new)
- `tests/test_plugins_loader.py` (new)
- `docs/plugins-architecture.md` (new — full design reference)
- `docs/roadmap.md` (Plugin system entry with phasing)
- `README.md` (Plugins section pointing at architecture doc)


## Change 32 — Plugin system Phase 2b: WebUI panel + stdio spawn + Browse (2026-04-29)

**Goal**

Phase 2a (Change 31) shipped the on-disk format, manifest parser,
loader, and runner. Phase 2b makes plugins useful end-to-end: stdio
plugins now spawn via `uvx` / `npx` with per-plugin caches; the WebUI
exposes install / configure / enable-toggle / logs / browse via a
gear-icon-modal UX; operators can register multiple `index.json` URLs
and browse catalogs from inside the panel. Browse was pulled forward
from Phase 3 — Phase 3 now ships only the curated repo *content*.

**What changed**

*Image (Dockerfile)*

- `pip install uv` brings `uvx` onto PATH (~25 MB).
- NodeSource `setup_20.x` + `apt-get install nodejs` brings `npx` onto
  PATH (~30 MB).
- `mkdir -p /app/logs/plugins` for stdio stderr capture.

*`GLADOS_PLUGINS_ENABLED` gate (`glados/core/engine.py`)*

- Default `true`. When set to `false` / `0` / `no` / `off`, engine
  logs `Plugins disabled by GLADOS_PLUGINS_ENABLED env` and skips
  `discover_plugins()`. WebUI panel renders an "off" notice. Read
  once at startup; flipping requires a container restart.

*Runner cache routing (`glados/plugins/runner.py`)*

- uvx packages: `--cache-dir <plugin>/.uvx-cache` injected into args
  immediately after `<pkg>@<ver>`.
- npx packages: `npm_config_cache=<plugin>/.uvx-cache` env injected.
- `.uvx-cache/` lives under `/app/data/plugins/<name>/`, survives
  image rebuilds.

*MCPManager per-plugin lifecycle (`glados/mcp/manager.py`)*

- `add_server(cfg)` schedules `_session_runner` for one plugin and
  registers in `_servers` + `_session_tasks`. Raises `MCPError` on
  duplicate name.
- `remove_server(name)` cancels the task, awaits up to 5 s, drops
  from internal state. No-op if missing.
- Per-plugin event ring (`deque maxlen=256`) records connect /
  disconnect / error / tools events; `get_plugin_events(name, limit)`
  surfaces them to the WebUI Logs tab.
- stdio errlog routes to `/app/logs/plugins/<name>.log` instead of
  `DEVNULL`. Lazy size-cap rotation (>1 MB → `.log.1`).

*Plugin store helpers (`glados/plugins/store.py`)*

- `install_plugin(plugins_dir, slug, manifest)` — atomic dir create
  via `<slug>.installing/` rename. Stub `runtime.yaml` written
  disabled.
- `remove_plugin(plugins_dir, slug)` — `rmtree` with `..` safety.
- `set_enabled(plugin_dir, enabled)` — `runtime.yaml` flip.
- `slugify(name, existing)` — last segment, lowercased,
  non-alphanumeric → `-`, collisions resolved via `-2`..`-100`
  suffixes.

*Endpoint surface (`glados/webui/tts_ui.py` + `plugin_endpoints.py`)*

11 new endpoints under `/api/plugins/*`, all admin-only:

- `GET /api/plugins`, `GET /api/plugins/<slug>`,
  `POST /api/plugins/install`, `POST /api/plugins/<slug>`,
  `POST /api/plugins/<slug>/enable`,
  `POST /api/plugins/<slug>/disable`,
  `DELETE /api/plugins/<slug>`, `GET /api/plugins/<slug>/logs`,
  `GET /api/plugins/indexes`, `POST /api/plugins/indexes`,
  `GET /api/plugins/browse`.

Install flow enforces https-only, rejects RFC1918 / loopback /
link-local resolutions (SSRF guard), 256 KB manifest cap, 5 s
fetch timeout. Save-runtime supports a `***` sentinel: secrets
unchanged at the client preserve via the server-side merge.

*WebUI panel (`glados/webui/static/ui.js`)*

- Three cards under **System → Services**: Installed plugins
  (per-row layout `[icon] name vX.Y.Z [cat] ●  [⏻ toggle]  [⚙]
  [🗑]`), Add-by-URL, Browse.
- Gear icon opens a centered modal with three tabs:
  Configuration / Logs / About. Configuration auto-renders from
  `server.json` (env vars / headers / arguments) with typed
  inputs: password for secrets, select for choices, url for
  format=url, required-asterisk for `isRequired`, default →
  placeholder.
- Logs tab: 100 / 500 / 2000 lines, Refresh button, 5 s
  auto-refresh, both stdio tail + event ring.
- About tab: name, version, category, persona role, repository,
  source index, Reinstall-from-source button.
- Browse card: collapsible Index URLs editor + Browse button → gallery.
- Polls `/api/plugins` every 30 s while System tab is visible.

*`ServicesConfig.plugin_indexes`*

New `list[str]` field on `services.yaml` for the Browse card's
catalog URLs. https-only validator at load time. Default empty.

**Tests**: +56 across new files.

- `tests/test_engine_plugin_gate.py` — `GLADOS_PLUGINS_ENABLED`
  parsing + skip-discovery behaviour.
- `tests/test_plugins_runner.py` — uvx `--cache-dir` injection +
  npx `npm_config_cache` env routing.
- `tests/test_mcp_manager_lifecycle.py` — `add_server` /
  `remove_server` / event ring / log-file rotation.
- `tests/test_plugins_store.py` — `install_plugin` /
  `remove_plugin` / `set_enabled` / `slugify`.
- `tests/test_webui_plugins.py` — 11-endpoint surface,
  https-only + SSRF guards, `***` secret-preserve sentinel.
- `tests/test_services_config_plugin_indexes.py` — schema +
  https-only validator.

Suite: 1519 → 1575 (+56).

**Files touched**

- `Dockerfile` (uvx + Node 20 + plugin log dir)
- `glados/core/engine.py` (`GLADOS_PLUGINS_ENABLED` gate)
- `glados/plugins/runner.py` (cache routing)
- `glados/plugins/store.py` (`install_plugin` / `remove_plugin` /
  `set_enabled` / `slugify`)
- `glados/mcp/manager.py` (`add_server` / `remove_server` /
  event ring / log rotation)
- `glados/webui/plugin_endpoints.py` (new — 11 endpoints)
- `glados/webui/tts_ui.py` (route registration)
- `glados/webui/static/ui.js` (Plugins panel + modal + browse)
- `glados/config/services.py` (`plugin_indexes` field)
- `tests/test_engine_plugin_gate.py` (new)
- `tests/test_plugins_runner.py` (new)
- `tests/test_mcp_manager_lifecycle.py` (new)
- `tests/test_plugins_store.py` (new)
- `tests/test_webui_plugins.py` (new)
- `tests/test_services_config_plugin_indexes.py` (new)
- `docs/plugins-architecture.md` (Phase 2b status flipped to live;
  Browse-pulled-forward note; phasing table updated)
- `README.md` (Plugins section extended with operator install +
  browse + logs walkthrough)

## Change 33 — Plugin system v2: zip bundle format + Upload (2026-04-29 evening)

**Goal**

Operator review of the live Phase 2b panel surfaced two structural
problems with the v1 install path: developer terminology leaked into
the operator UI (`slug`, `manifest`, `runtime.yaml`, env-var keys
like `SONARR_API_KEY` shown verbatim as form labels), and Add-by-URL
required upstream cooperation (a published `server.json`) that most
GitHub MCP servers don't ship. The fix pivots the install format to
a self-contained zip bundle along the lines of HA `custom_components`
and the VS Code `.vsix` family — operators can repackage any GitHub
MCP server with a GLaDOS-side `plugin.json`, drag-drop the zip into
the WebUI, and configure operator-friendly settings without touching
upstream code.

**What changed**

*New bundle format (`glados/plugins/bundle.py`)*

- `PluginJSON` Pydantic model. Required fields: `schema_version`
  (`1`), `name`, `description`, `version`, `category`, `runtime`.
  Optional: `icon`, `persona_role`, `homepage`, `settings[]`.
- Three runtime-mode discriminated submodels:
  - `RegistryRuntime` (`mode: "registry"`, `package: "uvx:pkg@ver"`
    or `npx:pkg@ver`). Spawns via uvx/npx fetching at runtime.
  - `BundledRuntime` (`mode: "bundled"`, `command`, `args`). Spawns
    from inside the unpacked zip with `GLADOS_PLUGIN_DIR` exposed.
  - `RemoteRuntime` (`mode: "remote"`, `url` https-only, optional
    `headers`). Connects via streamable-HTTP, no subprocess.
- Six `Setting.type` widgets: `text`, `url`, `number`, `boolean`,
  `select` (requires `choices`), `secret`. Operators see
  `setting.label`; the env-var key is internal.
- `v1_to_v2(server_json, package_index, remote_index) -> PluginJSON`
  synthesises a v2 view from the v1 schema so the runner and form
  renderer can consume a single shape regardless of bundle vintage.

*Loader fallback (`glados/plugins/loader.py`)*

- `load_plugin(plugin_dir)` checks for `plugin.json` first; parses
  as `PluginJSON` and builds the `Plugin` directly. If absent, falls
  back to the existing v1 path (`server.json` + `runtime.yaml`) and
  feeds the result through `v1_to_v2`. The `Plugin` dataclass now
  carries `manifest_v2: PluginJSON` (always present) alongside the
  legacy `manifest: ServerJSON | None` (v1 installs only).

*Runner (`glados/plugins/runner.py`)*

- Rewritten to dispatch on `plugin.manifest_v2.runtime.mode`:
  `_build_remote_v2`, `_build_registry_v2`, `_build_bundled_v2`.
  Cache routing for uvx (`--cache-dir`) and npx (`npm_config_cache`)
  carried forward unchanged. `_resolve_settings` merges
  `runtime.yaml.env_values` and `secrets.env`, applies defaults,
  and surfaces missing-required errors using `setting.label`.

*Zip install pipeline (`glados/plugins/store.py`)*

- `install_from_zip(zip_bytes, plugins_dir) -> Path`. Caps: 50 MB
  compressed, 200 MB uncompressed, 50 MB per entry. Rejects
  symlinks (POSIX file-type `0o120000`), absolute paths, path
  traversal. Validates `plugin.json` via `PluginJSON.model_validate`
  before extraction. Atomic via `<internal-name>.installing/` →
  `<internal-name>/` rename. Collisions append `-2`, `-3` suffix.

*Endpoint surface (`glados/webui/tts_ui.py` + `plugin_endpoints.py`)*

- New `POST /api/plugins/upload` accepts multipart upload with file
  field `bundle`. Reads bytes (50 MB content-length cap), invokes
  `install_from_zip`, returns `{name, internal_name, plugin}` for
  the WebUI to switch to the new tab.
- `POST /api/plugins/install` (the v1 URL-fetch endpoint) is removed
  from the dispatcher. The helper stays in the module unexported.
- The remaining 10 admin-only `/api/plugins/*` endpoints (list /
  get / save / enable / disable / delete / logs / indexes ×2 /
  browse) unchanged.
- Serializers updated to read from `manifest_v2`.

*WebUI rework (`glados/webui/static/ui.js` + `style.css`)*

Design-system conformance pass — Phase 2b's bespoke classes diverged
from the rest of the Configuration sub-page system. v2 grounds the
page in the established conventions:

- Page wrapper now uses `.page-shell > .container > .page-header`
  with `h2.page-title` + `.page-title-desc`, matching Memory / SSL /
  Logs / Raw YAML. Bespoke `.plugins-page` outer class dropped.
- Per-plugin pane header rebuilt with `.card` + `.section-title`.
  Bespoke `.plugin-header-card` / `.plugin-header-*` /
  `.plugin-icon-large` / `.plugin-meta-*` / `.plugin-status-text`
  classes removed.
- Browse + Upload cards are flat `.card` sections, not collapsible
  details. `.plugin-collapsible*` CSS removed.
- Save button adopts the standard `.cfg-save-btn` / `.cfg-result`
  classes from elsewhere.
- `.page-tabs` strip retained — system-wide convention for tabbed
  Configuration pages.

Install flow rework:

- Add-by-URL inline section gone (`renderAddByUrlCard` /
  `wireAddByUrlHandlers` removed). Upload card takes its place
  (`renderUploadCard` / `wireUploadHandlers`): drag-drop zone +
  file picker, `.zip` only, 50 MB client-side cap, multipart POST
  to `/api/plugins/upload`. New CSS: `.upload-dropzone` +
  `.upload-prompt`.
- Browse-gallery Install button changed from POST
  `/api/plugins/install` to a two-step fetch + multipart upload.
  Catalog entries now read `bundle_url` (preferred) with
  `server_json_url` legacy fallback.
- Reinstall-from-source button removed from the per-plugin About
  pane (its endpoint is gone; operators re-upload).

Form rendering pivots to the v2 shape:

- `renderConfigForm` / `renderFormField` iterate
  `detail.manifest.settings[]` (the v2 array, synthesised
  identically for v1-on-disk and v2-native installs).
- Every form label sources from `setting.label`, not the env-var
  key. The key is invisible to operators.
- Six setting types render correctly: `text`, `url`, `number`,
  `boolean`, `select` (with choices), `secret` (password input;
  the `***` sentinel preserves the existing value on partial save).

Terminology sweep:

- "slug", "Slug", "slugified", "optional slug" placeholder removed
  from operator-visible strings. Internal JS identifier
  `_pluginActiveSlug` retained — it's a tab key, never rendered.
- Empty-state in `renderPluginsList` no longer references "Add by
  URL"; points operators at Upload + Browse.
- Category badges (tab strip, installed list, browse gallery)
  render via `pluginCategoryLabel(cat)` against
  `_PLUGIN_CATEGORY_LABELS` (`media → Media`, `home → Home`,
  `integrations → Integrations`, `system → System`, `dev →
  Developer`, `utility → Utility`), with literal-string fallback
  for unknown categories.

**Tests**: 1575 → 1592 (+17 net).

- `tests/test_plugins_bundle.py` (new) — `PluginJSON` schema,
  three runtime modes, six setting types, `v1_to_v2` conversion
  for both registry and remote v1 sources. +25 tests.
- `tests/test_plugins_zip_install.py` (new) — `install_from_zip`
  safety + atomicity: traversal, absolute path, symlink, oversize
  compressed / uncompressed, missing `plugin.json`, invalid JSON,
  staging cleanup, collision suffix. +10 tests.
- `tests/test_webui_plugins.py` updated — install-by-URL routing
  tests removed (the endpoint is gone) and the file consolidated
  around the upload pipeline. −6 routing tests, plus the rest of
  the consolidation lands the file at the +17-net mark.

Suite: 1575 → 1592 (+25 new bundle, +10 new zip-install, −6
removed install-by-URL routing tests, plus consolidation in
`test_webui_plugins.py` = +17 net).

**Files touched**

*Backend*

- `glados/plugins/bundle.py` (new — `PluginJSON`, `Setting`, three
  `*Runtime` models, `v1_to_v2`)
- `glados/plugins/loader.py` (`plugin.json`-first; v1 fallback)
- `glados/plugins/runner.py` (three-mode dispatch)
- `glados/plugins/store.py` (`install_from_zip` + safety guards)
- `glados/plugins/__init__.py` (re-export `install_from_zip`)
- `glados/webui/plugin_endpoints.py` (serializers read
  `manifest_v2`; `install_from_url` no longer exported)
- `glados/webui/tts_ui.py` (`POST /api/plugins/upload`; install
  route removed from dispatcher)

*WebUI*

- `glados/webui/static/ui.js` (page wrapper, Upload card,
  Browse-gallery upload pipeline, v2 form rendering, terminology
  sweep, category label map)
- `glados/webui/static/style.css` (page-conformance pass:
  bespoke `.plugins-page` / `.plugin-header-*` /
  `.plugin-collapsible*` removed; `.upload-dropzone` +
  `.upload-prompt` added)

*Tests*

- `tests/test_plugins_bundle.py` (new)
- `tests/test_plugins_zip_install.py` (new)
- `tests/test_webui_plugins.py` (upload tests; install-by-URL
  routing removed)

*Docs*

- `docs/plugin-bundle-format.md` (new — operator-facing schema
  reference + "wrap any MCP server in 5 minutes" tutorial)
- `docs/plugins-architecture.md` (v2 bundle-format section;
  Phase 2c row in phasing table; v1 history retained)
- `docs/CHANGES.md` (this entry)
- `README.md` (Plugins install walkthrough updated to Browse +
  Upload; Add-by-URL section removed; link to bundle-format doc)



## Change 34 — Per-group log filter foundation (2026-04-30)

**Why.** The empty-bubble investigation in late April revealed that
loguru's single-sink hard-coded `level="SUCCESS"` filter (engine.py:58)
was silently dropping every `logger.info()`/`logger.debug()` call across
the codebase, including the diagnostic instrumentation added across two
sessions to debug the bug. "Add more logs" became "logs added,
invisible, three deploys wasted." Operator-flagged: build a tunable
per-subsystem log filter so individual subsystems can be dialled up or
down without flipping the global level.

**What.** `glados/observability/log_groups.py` defines a registry of
named log groups (~50 groups, one per logical subsystem on the chat /
autonomy / HA / MCP / TTS / memory / WebUI / auth / lifecycle /
network paths). Every diagnostic log call binds a group ID via
`group_logger(LogGroupId.X.Y)`; the registry decides per record whether
to emit, based on each group's `enabled` + per-group level threshold.
`engine.py` now installs a single `TRACE`-floor sink with a filter that
consults the registry per record, so changes take effect immediately
without a restart.

**Persistent state** lives in `configs/logging.yaml` (joins the
existing 5-file Raw YAML split — global / services / speakers / audio
/ personality / **logging**, now 6 files). The
`configs/logging.example.yaml` reference is bundled into the image so
operators can see the schema. The on-disk file is created lazily — the
WebUI Save action writes it the first time, otherwise the registry
runs from in-code defaults.

**Safety:**

- Atomic writes (temp + rename), pydantic schema validation before
  swap.
- Bad YAML at startup: WARNING + fall back to defaults + preserve the
  bad file as `<name>.broken-<timestamp>` so nothing is lost.
- Unknown group IDs in YAML are dropped on load with a warning (no
  zombie state).
- Missing builtin IDs in YAML are auto-merged on load (no manual
  re-export needed after a deploy adds new groups).
- `ERROR` and `CRITICAL` records bypass the per-group filter entirely
  — you cannot accidentally silence error logging via this UI.
- The `auth.audit` group is locked-on by policy; `set_group_state`
  refuses to disable it.

**Global override.** `GLADOS_LOG_LEVEL` env var, if set, lowers every
group's effective floor to that level for the lifetime of the process.
Useful for one-shot deployments where you want the firehose without
flipping ~50 toggles. e.g. `GLADOS_LOG_LEVEL=DEBUG`.

**Activity counter.** Rolling 5-minute hit counter per group, kept
in-memory (resets on restart). Powers the WebUI page's "Recent activity"
column so the operator can spot noisy / silent groups visually.

**Code-side migration.** `tts_ui.py`'s nine `print("[STREAM] …")`
calls are now `_tts_stream_log.info(…)` bound to `webui.tts_stream`,
the first surface to use the new system. The remainder of the codebase
will migrate over the next ten commits, subsystem by subsystem.

**Files added:**

- `glados/observability/log_groups.py` — registry, filter, `LogGroupId`
  constants, helpers, loguru-sink installer.
- `configs/logging.example.yaml` — example file with all ~50 groups.
- `tests/test_log_groups.py` — 33 tests covering schema validation,
  filter decisions, persistence round-trips, atomic writes, locked-on
  policy, error-bypass, env override, activity counter, sink
  integration.

**Files modified:**

- `glados/core/engine.py` — sink installation now goes through
  `install_loguru_sink` instead of a hard-coded `logger.add(level=…)`.
- `glados/observability/__init__.py` — re-exports the public surface.
- `glados/webui/tts_ui.py` — nine `print` calls converted to grouped
  logger.
- `Dockerfile` — copies `configs/logging.example.yaml` into the image.
- `docs/CHANGES.md` (this entry).

**Tests:** 1612 → 1645 (33 new), 0 regressions. `pytest -q` runs in
~55 s.

**Next.** WebUI Configuration → Logging page (Change 35) gives the
operator the visual surface to flip toggles without editing YAML by
hand. Then commit-by-commit instrumentation of every subsystem (chat
path first, since that is the bug we are actively chasing).

## Change 35 — WebUI Logging page (Configuration → Logging) (2026-04-30)

**Why.** Change 34 landed the per-group log filter foundation, but
operating it required hand-editing `configs/logging.yaml` over SSH.
That works for one or two toggles per investigation; it doesn't scale
to ~50 groups with the granular dial-up/dial-down workflow the
operator wants. This change adds the operator-facing surface — a
dedicated Configuration → Logging page with per-group toggles, level
dropdowns, bulk operations, and a raw-YAML drawer for power users.

**What's in the page.**

- A table of every log group, grouped by category (Chat / Plugin /
  Autonomy / HA / MCP / TTS / Memory / WebUI / Auth / Lifecycle /
  Conversation / Config / Filter / Network).
- Per-row: Enabled toggle, Level dropdown
  (`DEBUG` / `INFO` / `SUCCESS` / `WARNING`), and a "Recent activity"
  count showing hits over the last 5 minutes (refreshed every 5 s
  while the tab is open).
- Locked-on groups (`auth.audit`) render the toggle disabled with a
  lock icon.
- Bulk operations: Enable all / Disable all / Reset to defaults /
  per-category Enable / per-category Disable.
- Filter input: free-text search across name, ID, and description.
- Default-level dropdown for ungrouped logs (legacy `logger.info()`
  call sites).
- A banner appears when the `GLADOS_LOG_LEVEL` env var is set,
  warning the operator that per-group toggles can't lower output
  below the override floor.
- Raw YAML drawer: collapsible textarea with Load / Save buttons
  that round-trips through `configs/logging.yaml` with full schema
  validation. Schema errors come back inline.

**Server-side**:

- `GET /api/log_groups` — entire registry as JSON, including recent
  activity counts and the global override level.
- `POST /api/log_groups/group` — toggle / level for one group.
- `POST /api/log_groups/bulk` — bulk operations.
- `POST /api/log_groups/reset` — reset to builtin defaults.
- `GET  /api/log_groups/yaml` — raw YAML.
- `POST /api/log_groups/yaml` — atomic save with schema validation.

All routes admin-only. Every mutation emits an audit record via the
existing `audit()` channel (origin = `webui_chat`, kind =
`config_change`, principal = the requesting username) so the change
is observable from the audit log alongside login / role-change /
configuration-save events.

**Safety:**

- Schema validation before every YAML save — bad input rejected with
  a 400 + line-level error message, in-memory state preserved.
- Locked-on groups can't be disabled via UI or YAML save (the
  registry's `set_group_state` and `replace_config` both refuse).
- Optimistic UI: each row save patches local state on success, falls
  back to a full refetch on any error so the displayed state never
  drifts from the backend.
- Activity polling tears down when the operator leaves the tab, so
  the page doesn't burn HTTP traffic in the background.

**Files added:**

- `glados/webui/pages/logging_page.py` — page HTML.
- `glados/webui/log_groups_endpoints.py` — server-side handlers.
- `tests/test_log_groups_endpoints.py` — 22 tests covering payload
  shape, single-group updates, bulk operations, default-level save,
  raw YAML round-trip, schema validation, locked-on enforcement,
  unknown-ID rejection, missing-field handling.

**Files modified:**

- `glados/webui/tts_ui.py` — page composition + GET/POST routing for
  `/api/log_groups/*` (admin-gated, mirrors plugin endpoints).
- `glados/webui/pages/_shell.py` — sidebar entry under Configuration.
- `glados/webui/static/ui.js` — page render, filter, save, bulk, raw
  YAML drawer (~280 lines appended).
- `glados/webui/static/style.css` — new `.logging-*` classes
  (toolbar, override banner, category cards, row grid, raw drawer).
- `docs/CHANGES.md` (this entry).

**Tests:** 1645 → 1667 (22 new), 0 regressions. `pytest -q` runs in
~55 s.

**Visual verification:** deferred to operator after deploy. The
WebUI runs in a docker container on the docker host (docker-host.local);
this session does not have a local browser preview surface — the
operator confirms in their actual browser after the next deploy.

## Change 36 — Chat-path instrumentation (per-group logging) (2026-04-30)

**Why.** With the per-group log filter live (Change 34) and the WebUI
toggle surface live (Change 35), every diagnostic call site on the
chat path migrates to the new system. The empty-bubble investigation
finally has the comprehensive chunk-shape coverage that earlier diag
attempts kept missing.

**What.** Every chat-path log call now binds a stable group ID:

- `chat.connect_path` — connect attempt, status, response headers,
  4xx body (full first 1000 chars instead of 200).
- `chat.round1_stream` / `chat.round2_stream` — per-round summary
  diagnostics. Now tracks every chunk shape independently:
  - `lines` — every non-empty SSE line.
  - `data_lines` — every `data: ...` line.
  - `parsed` / `parse_fail` — JSON parse outcomes.
  - `bytes` — total upstream bytes.
  - `chunks` — chunks with non-empty content.
  - `role_only` — chunks with `delta == {"role": "assistant"}`.
  - `empty_content` — chunks where `delta.content == ""`.
  - `raw_chars` / `visible_chars` — content before / after the
    `<think>` filter.
  - `reasoning_chars` — chars accumulated in `delta.reasoning_content`
    (the surface qwen3 / DeepSeek-R1 use that the prior diag missed
    entirely).
  - `refusal_chars` — chars in `delta.refusal`.
  - `tool_deltas` — count of tool-call deltas.
  - `finish_reason` / `done_seen` — terminal-chunk state.
  - `error` — captured top-level `{"error": {...}}` chunk (LM Studio
    runtime errors like "Context size exceeded" arrive in this shape
    with no `choices`; previously silently dropped).
  - `usage` — captured terminal usage chunk.
  - `top_level_keys` / `delta_keys` — sorted set of every key that
    appeared in any chunk, so unknown shapes surface immediately.
  - `first_chunk[:1000]` — verbatim JSON of the first non-DONE chunk.
- `chat.round1_raw_bytes` / `chat.round2_raw_bytes` — DEBUG-level
  per-line dump of every SSE line received. Disabled by default.
- `chat.tool_call` — per-tool dispatch with args, latency, result
  size, max-rounds-reached warning.
- `chat.tool_result` — tool result body[:500] at DEBUG.
- `chat.filter_pipeline` — every `<think>` open/close transition at
  DEBUG.
- `chat.sanitize_history` — what `_sanitize_message_history`
  dropped, with role lists at DEBUG.
- `chat.routing_decision` — SSE preamble: msg count, tool count,
  `num_predict`, route, system_prompt_chars.
- `filter.think_tag` / `filter.boilerplate` — final-response strip
  diff (chars removed by `_strip_thinking` and
  `strip_closing_boilerplate`).
- `memory.context_inject` — chars + content[:500] preview at DEBUG.
- `conversation.store` — assistant content[:500] preview at DEBUG.
- `plugin.intent_match` — match (INFO), miss explained (DEBUG).
- `plugin.triage_llm` — invocation, latency, raw response (full at
  DEBUG, [:200] at INFO), parsed result, hallucinated-name drops.

**Files modified:**

- `glados/core/api_wrapper.py` — module-level grouped loggers, every
  chat-path call site rewired to the appropriate group, comprehensive
  chunk-shape tracking in both round-1 and round-2 loops.
- `glados/plugins/intent.py` — converted to `_log_intent`,
  added DEBUG-level miss explanation.
- `glados/plugins/triage.py` — converted to `_log_triage`, added
  DEBUG-level full-raw-response dump.

**Tests:** 1667 → 1667, 0 regressions. `pytest -q` runs in ~56 s.
The chat-path instrumentation is observability-only and has no
behaviour change.

## Change 37 — Command lane: separate upstream for tool-using turns (2026-05-01)

**Why.** Every chat turn — including tool-using "Add Ghostbusters" /
"Is X in my library" commands — was routed through the big
personality-laden qwen3-14b model on the interactive lane. Three costs:

1. *Slow.* qwen3-14b at ctx=32768 / parallel=1 produces ~16 tok/s on
   the Intel Arc Pro B60 — fine for conversational replies, sluggish
   for tool-call confirmations.
2. *Fabrication risk.* Persona overlay encourages the model to
   *describe* what it would do rather than *invoke* the tool, then
   produce a witty in-character "I added it for you" without an
   actual tool call having fired.
3. *Crash pressure.* The bigger model + tool catalogue + persona
   pushed against the per-slot ctx boundary that triggers the
   llama.cpp Vulkan `STATUS_STACK_BUFFER_OVERRUN` (Exit 3221226505).

Operator preference (locked in
`feedback_command_vs_conversational.md`): tool-using turns get
terse, direct, tool-result-echo replies with NO persona. Persona
remains on weather / status / direct chat / autonomy alerts.

**What.** A new ``services.llm_commands`` endpoint slot drives a
dedicated upstream for tool-using turns. The chat path's
``_stream_chat_sse_impl`` now classifies each turn and picks the
upstream lane:

  - ``route=plugin:* OR is_home_command`` → command lane (URL +
    model from ``cfg.services.llm_commands``).
  - everything else → interactive lane (the existing
    ``llm_interactive`` upstream, unchanged).

When ``llm_commands.url`` is empty, the chat path silently falls
back to the interactive lane — backwards-compatible with deployments
that haven't configured a separate command lane.

**Command-lane prompt shape.** Three modifications relative to the
interactive lane:

  - Persona system message replaced with a minimal, anti-fabrication
    command-mode instruction. The persona's HEXACO traits / quip
    library / response directives are absent on the command path.
  - Persona few-shots dropped (they bias the model toward textual
    replies, suppressing tool calls).
  - ``HOME_COMMAND_GUARD`` applied on plugin routes too (previously
    only on legacy is_home_command). Generalises the guard to all
    tool-using turns.

``num_predict`` caps at 512 on the command lane (was 1024) — a small
coder-tuned model produces tool_call JSON + a short confirmation
without needing the bigger budget.

**LM Studio side.** ``qwen2.5-coder-7b-instruct`` Q4_K_M (~4.7 GB)
loaded at ctx=32768 with no ``--parallel`` (single slot — the
per-slot stack-overrun risk applies on the command lane too, and
command turns are short and serial). Added to:

  - ``lms_autoload.bat`` — bootstrap unload + load entries.
  - ``lms_watchdog.ps1`` — desired list, so the model auto-reloads
    if it crashes.

**Routing log.** ``chat.routing_decision`` now reports the lane
choice and the actual upstream model so operators can watch which
turns took which path:

```
SSE: 12 msgs, 24 tools, num_predict=512 (route=plugin:radarr
lane=commands model=qwen2.5-coder-7b-instruct) ...
```

**Files added:**

- ``tests/test_command_lane_routing.py`` — 13 unit tests for the
  two pure helpers (``_select_command_lane``,
  ``_strip_persona_for_command_lane``) covering URL fallback rules,
  empty/blank URL, model-inheritance, persona replacement, few-shot
  drop, multi-system-msg preservation.

**Files modified:**

- ``glados/core/config_store.py`` — new ``ServicesConfig.llm_commands``
  field, defaults to empty URL.
- ``glados/core/api_wrapper.py`` — plugin-intent block moved up so
  the route classifier is known before the few-shot strip; new
  helpers wired into ``_stream_chat_sse_impl``; round-2 dispatch
  uses the same upstream as round 1.
- ``glados/webui/static/ui.js`` — ``SERVICE_NAMES['llm_commands']
  = 'LLM (Commands)'``. Card auto-renders via the existing
  data-driven service grid.
- ``glados/webui/tts_ui.py`` — ``llm_commands`` added to the URL
  validator slot list.
- ``docs/CHANGES.md`` (this entry).

**Not touched** — single-source-of-truth refactor of legacy
``Glados.completion_url`` / ``Glados.llm_model`` /
``Glados.autonomy.*`` mirror fields in ``glados_config.yaml`` is
explicitly out of scope and remains queued.

**Tests:** 1667 → 1680 (13 new), 0 regressions. ``pytest -q`` runs in
~57 s.

## Change 38 — AIBox LLM stack swap to OpenVINO Model Server + URL helper fix (2026-05-02)

**Why.** The prior LM Studio + Ollama-IPEX stack on AIBox had been
producing layered failures for weeks (`STATUS_STACK_BUFFER_OVERRUN`
on Vulkan + Qwen3 MoE, `response_format=json_object` rejected with
HTTP 400, JIT/manual-load theatre, the Ollama-IPEX fork operator-
flagged as effectively abandoned). Operator authorised a complete
rip-out and a clean-slate replacement with a stable + actively
maintained + Intel-Arc-supporting + OpenAI-compatible engine.

**What landed.**

1. **AIBox: complete LM Studio + Ollama-IPEX wipe.** All four NSSM
   services removed (`LMStudioAutoload`, `ollama-ipex-llm`,
   `ollama-glados`, `ollama-vision`); `~133 GB` of installs +
   models reclaimed. Pre-wipe inventory archived at
   `C:\AI\llm_inventory_2026-05-01_pre_wipe.md` so re-pulls have a
   reference. ``C:\AI\nssm.exe`` and the non-LLM services (Speaches
   TTS, Open-WebUI, glados-vision) preserved.
2. **Replacement engine: OpenVINO Model Server (OVMS) 2026.1.** Intel-
   first-party, native Windows binary install at ``C:\AI\ovms``,
   NSSM-wrapped service ``ovms`` listening on ``0.0.0.0:11434``.
   Auto-starts on boot. Talks to the Intel Arc Pro B60 directly via
   Level Zero on Windows native — no WSL2 / no Docker / no Linux VM
   needed. Loaded model: ``OpenVINO/Qwen3-30B-A3B-int4-ov``
   (16.34 GB INT4, MoE with ~3B active params per token). Tool
   parser ``hermes3`` + reasoning parser ``qwen3`` configured. KV
   cache compressed to u8 to fit comfortably in 24 GB VRAM.
3. **Performance baseline:** 39.8 tok/s steady-state on 1024-token
   decode (memory-bandwidth-bound on the 16 GB of streamed weights;
   speculative decoding evaluated and ruled out — MoE-A3B's
   non-deterministic per-token routing breaks the verify-N-tokens
   speedup pattern, observed 5× regression to 8.2 tok/s with a
   0.6B draft).
4. **OpenAI compliance gap fix.** OVMS exposes the OpenAI surface on
   ``/v3/chat/completions`` AND ``/v3/v1/chat/completions``, NOT on
   the standard ``/v1/chat/completions`` path. The container's
   ``compose_endpoint`` and ``strip_url_path`` helpers were
   strip-and-rewriting any operator-typed path back to bare and
   appending ``/v1/chat/completions``, breaking dispatch even when
   ``services.yaml`` carried the correct full URL. Both helpers now
   share a ``_path_is_authoritative`` predicate: a URL whose path
   ends in ``/chat/completions`` AND is something other than the
   spec-canonical ``/v1/chat/completions`` is treated as an explicit
   operator endpoint and preserved verbatim. The canonical path
   continues to strip-to-bare so legacy storage equivalence (and the
   engine reconciler's drift detection) keeps working. Legacy
   Ollama-style ``/api/chat`` URLs still get the strip-and-reappend
   behaviour.
5. **TTS endpoint port fix.** ``services.yaml`` had ``tts.url``,
   ``stt.url``, and ``api_wrapper.url`` set to
   ``http://localhost:8015`` — the operator-facing port. With SSL
   enabled, port 8015 is HTTPS-wrapped and rejects plain HTTP with a
   connection reset, breaking the WebUI's TTS-chunk synthesis path.
   Restored these to the schema default ``http://127.0.0.1:18015``
   (the always-plain-HTTP loopback listener), which is SSL-state-
   independent. TTS streams now generate audio chunks correctly.

**Files modified:**

- ``glados/core/url_utils.py`` — ``_path_is_authoritative`` predicate
  shared by ``strip_url_path`` and ``compose_endpoint``; non-canonical
  chat-completion paths preserved verbatim through the dispatch chain.
- ``tests/test_url_utils.py`` — expanded to pin the canonical-path
  strips and the non-canonical-path preserve cases. 13 new tests.

**Files NOT modified (intentional scope discipline):**

- The legacy ``Glados.completion_url`` / ``Glados.llm_model`` /
  ``Glados.autonomy.*`` fields in ``glados_config.yaml`` remain
  scheduled for the separate single-source-of-truth refactor.
- The container's HTTP/HTTPS port-binding logic is unchanged. The TTS
  fix routes around the SSL-conditional port instead of touching the
  binding code.

**Tests:** 1680 → 1697 (17 new), 0 regressions. ``pytest -q`` runs in
~58 s.

**Open follow-ups surfaced 2026-05-02 (operator-flagged, not yet
fixed):**

- *Time hallucination*: "What time is it" returned "3:17 PM" when the
  actual was 1:03 PM. No time-injection or ``get_current_time``
  builtin tool. Fix path TBD (memory: ``project_glados_time_hallucination.md``).
- *TTS pronunciation*: "P.M." spoken as "Pem". Adds to the existing
  pronunciation-overrides queue (memory:
  ``project_tts_pronunciation_cases.md``).


## Change 39 — Authoritative time source: NTP sync + tz-from-weather + System UI (2026-05-02)

**Why.** The 2026-05-02 evening session closed Change 38's open follow-up
on time hallucination. GLaDOS otherwise fabricates the current time
when asked because no other context block carries a wall-clock
reference (operator-flagged: "What time is it" returned "3:17 PM" when
actual was 1:03 PM). Operator's design constraints: time pulled from
NIST-style time servers (not the container's drifting system clock),
timezone derived from the geo-coordinates already configured for
weather forecasting (so DST is automatic via IANA tz database), and
operator-tunable through the WebUI rather than YAML-only.

**What landed.**

1. **`weather_cache` captures the resolved IANA timezone.** Open-Meteo's
   forecast response includes a top-level ``timezone`` (e.g.
   ``"America/Chicago"``) and ``timezone_abbreviation`` when called
   with ``timezone=auto`` — the container was previously discarding
   both. Adding the capture in ``_process_forecast`` lets the new
   time_source module derive a tz-aware wall-clock without a second
   geocoding API call or a polygon-lookup library. Older cache files
   parse cleanly: missing fields surface as None, not KeyError.

2. **``TimeGlobal`` pydantic model under ``GlobalConfig.time``.** Five
   operator-tunable fields: ``enabled`` (master toggle), ``ntp_servers``
   (list, NIST defaults — ``time.nist.gov``, ``time-a-g.nist.gov``,
   ``time-b-g.nist.gov``), ``refresh_interval_hours`` (6h default),
   ``timezone_source`` (``auto | manual``), ``timezone_manual`` (IANA
   name, used when source=manual). Pydantic ``Literal`` rejects unknown
   ``timezone_source`` values like ``"magic"``.

3. **New module ``glados/core/time_source.py``.** Background thread
   syncs an offset against the configured NTP servers (tried in order
   until one responds); ``now()`` returns ``datetime.fromtimestamp(time
   .time() + offset, tz=resolved_tz)``. ``as_prompt()`` formats as
   ``"Current time: Saturday 2026-05-02 13:03"`` (operator-requested
   simplified format with weekday + date + 24h time, no tz suffix).
   ``status()`` exposes sync state for the WebUI card. NTP failure
   falls back to the system clock with a WARNING log — the chat path
   still gets an answer rather than dropping the injection. Adds
   ``ntplib>=0.4.0`` dep (~200 LOC pure Python).

4. **``context_gates.needs_time_context()``.** Mirrors the weather and
   canon gate shape — hardcoded default triggers + optional YAML
   extras. Trigger set: ``what time``, ``what's the time``, ``what
   hour``, ``current time``, ``time is it``, ``clock`` (word-boundary
   to avoid ``clockwork``/``deadlock``), ``o'clock``, ``current
   date``, ``today's date``, ``what's the date``, ``what day``, ``day
   is it``, ``date is it``, ``what year``. Avoids firing on incidental
   "time"/"day" mentions ("all the time", "good day", "out of date").

5. **Chat path injection.** New block in
   ``_stream_chat_sse_impl`` directly after the canon block,
   mirroring the weather/memory/canon insertion shape. Gated on
   ``needs_time_context``; content from ``time_source.as_prompt()``;
   skipped silently with a WARNING log on any exception.

6. **Engine wire-in.** ``time_source.configure()`` + ``start()`` are
   called from the same engine init block that configures
   ``weather_cache`` and ``context_gates`` — passes
   ``weather_cache.get_data`` as the tz-lookup callable so the
   resolved zone tracks operator weather-location changes. Init
   failures are logged but never fatal; the chat path's injection
   block already falls back to the system clock when time_source is
   unconfigured.

7. **WebUI System → Time tab.** New tab between Maintenance and
   Account. Two cards:
   - *Sync Status*: live read of ``time_source.status()`` — synced /
     unsynced / disabled badge, last sync timestamp, offset in ms,
     responding NTP server, resolved IANA zone. Refresh button
     re-fetches without polling.
   - *Configuration*: auto-rendered ``cfgBuildForm`` over
     ``global.time.*`` with FIELD_META labels; ntp_servers as
     comma-separated text input, timezone_source as auto/manual
     dropdown, refresh_interval_hours behind the Advanced toggle.
     Save dispatches through the existing
     ``_cfgSaveSystemSubset`` helper so the partial-save / no-wipe
     contract carries.

8. **Backend endpoint.** ``GET /api/time/status`` returns
   ``time_source.status()``; admin-gated via the existing
   ``require_perm("admin")`` fall-through. Adjacent to
   ``/api/ssl/status`` in the route table.

**Files modified:**

- ``glados/core/weather_cache.py`` — capture timezone fields
- ``glados/core/config_store.py`` — TimeGlobal model + GlobalConfig.time
- ``glados/core/time_source.py`` (new) — NTP sync + tz resolution
- ``glados/core/context_gates.py`` — needs_time_context() + defaults
- ``glados/core/api_wrapper.py`` — injection block in _stream_chat_sse_impl
- ``glados/core/engine.py`` — configure + start time_source at init
- ``glados/webui/pages/system.py`` — new Time tab HTML
- ``glados/webui/static/ui.js`` — FIELD_META + render/save/status helpers
- ``glados/webui/tts_ui.py`` — GET /api/time/status route + handler
- ``pyproject.toml`` — ntplib>=0.4.0 dep
- New tests: ``tests/test_weather_cache.py``,
  ``tests/test_time_source.py``, ``tests/test_time_context_gate.py``;
  TimeGlobal cases added to ``tests/test_config_defaults.py``

**Verification (dev_webui preview, port 28052):**

- Time tab button + panel render under System.
- ``GET /api/time/status`` returns the expected shape (enabled /
  synced / last_sync_at / last_sync_server / offset_seconds / timezone).
- ``GET /api/config/global`` includes the ``time`` block with TimeGlobal
  defaults: ``enabled=true``, NIST server list, ``refresh_interval_hours
  =6.0``, ``timezone_source="auto"``, empty ``timezone_manual``.
- Form auto-renders all five fields with correct types
  (bool/array/number/select-with-options/string) and pre-fills the
  default values.
- No JS console errors.

**Tests:** 1697 → 1754 (+57), 0 regressions. ``pytest -q`` runs in ~56 s.

**Out of scope (deferred):**

- Live-reload on config save: the engine reads TimeGlobal at init time,
  so changes to NTP servers / timezone source require a container
  restart — same recompose-required pattern weather_cache,
  context_gates, and the rest of the boot-time configure() calls
  already follow.
- ``get_current_time`` builtin tool (Option 2 from
  ``project_glados_time_hallucination.md``): not implemented;
  system-message injection covers the hallucination case end-to-end and
  matches the established weather/memory/canon pattern.

## Change 40 — Plugin triage: bypass LLM, advertise all enabled plugins (2026-05-03)

**Why.** Phase 2c (Change 31, 2026-04-29) added an LLM-driven triage
step on the chat path: when the keyword pre-filter misses, ask a
small fast model to pick which plugins are relevant. The design
assumed warm classification in 300–500 ms and an inline budget of
~1.5 s. Reality on the post-Change-38 OVMS-on-Qwen3-30B / Intel Arc
Pro B60 deployment: warm 30B is **11–25 s for 4 tokens**. Triage
running on the same model ate ~50% of plugin chat turns at the 15 s
ceiling — the chat path fell through to chitchat with no plugin
tools loaded, so newly-shipped plugins like Spotify (Change 39
follow-on, 2026-05-03 morning) appeared "unknown" to GLaDOS even
though the plugin runtime was healthy and the operator's chat was
literally about Spotify.

The 2026-05-03 morning session attempted to solve the underlying
"30B is too slow for triage" problem by serving a small fast model
(``OpenVINO/Qwen3-0.6B-int4-ov``, CPU) alongside the 30B on the
same OVMS instance. That attempt is documented in
``feedback_ovms_multi_model_attempt.md`` in auto-memory; in short:
``model_config_list`` doesn't drive HTTP routing for LLMs at all
(legacy KFServing surface), and ``mediapipe_config_list`` works
for one LLM and for two name-aliases pointing at the same graph
but **does not** route two distinct LLM graphs in one OVMS
instance. The HTTP layer registers the first graph and silently
skips subsequent ones. ``ovms_serve.bat`` was reverted to the
``--source_model`` single-model 30B configuration.

**Operator directive (2026-05-03 morning):** "Do B [multi-model
OVMS]. Then do A [bypass triage] if B does not resolve." B did
not resolve.

**What landed.**

1. **``triage_plugins`` returns every enabled plugin's name** when
   ``GLADOS_PLUGIN_TRIAGE_ENABLED`` is truthy and inputs are
   non-degenerate. The chat LLM gets the full plugin tool catalog
   on every turn that the keyword pre-filter missed. Trades a
   small bump in prompt tokens (the chat model reads N plugin
   tool descriptions instead of zero or one) for reliable plugin
   reachability — a pure regression-of-feature is the wrong
   tradeoff when the alternative is a 15 s stall and a refusal.
2. **No LLM call.** Imports of ``llm_call``, ``LLMConfig``,
   ``json``, ``time``, schema construction, sentinel handling,
   and dedup logic are removed. The original code remains in git
   history for revival when a fast triage model lands on this
   hardware.
3. **Env gate preserved.** ``GLADOS_PLUGIN_TRIAGE_ENABLED=false``
   still skips the function entirely and returns ``[]`` so
   deployments that don't want plugin tools advertised on the
   chat path can opt out without code changes.
4. **``timeout_s`` parameter accepted but ignored.** Back-compat
   for the existing call site in
   ``glados/core/api_wrapper.py:1691`` — no caller change needed.

**Files touched.**

- ``glados/plugins/triage.py`` — body replaced; imports trimmed;
  docstring rewritten to explain bypass mode and the why.
- ``tests/test_plugins_triage.py`` — 13 LLM-mocking tests removed
  (mocks no longer test anything real); 14 bypass-mode tests
  added covering: returns-all-names, content-independence, order
  preservation, env-gate, env-falsy variants (parametrized over
  6 strings), empty-plugin-list, empty-message,
  empty-string-message, ``timeout_s`` accepted-but-ignored.

**Tests:** 1754 → 1755 (+1 net, 13 removed + 14 added). 0
regressions. ``pytest -q`` runs in ~57 s.

**Live state (2026-05-03):**

- Container image SHA ``2caa358a2c76``. ``glados`` healthy on
  docker host ``192.168.1.150``.
- In-container probe confirms bypass mode active:
  ``triage_plugins("any", [spotify, arr-stack])`` returns
  ``["spotify", "arr-stack"]``; log line
  ``plugin triage: bypass mode, advertising all 2 enabled plugins:
  ['spotify', 'arr-stack']`` emits at ``INFO``.
- AIBox OVMS reverted to single-model 30B
  (``ovms_serve.bat`` matches ``ovms_serve.bat.bak``).

**Open follow-up.** Multi-LLM OVMS serving is deferred. The 0.6B
model is downloaded at ``C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov\``
on AIBox; ``C:\AI\models\config.json`` holds the failed multi-LLM
config (harmless, unused — OVMS isn't reading it now). Future
sessions revisiting this should mirror the prod topology in shadow
testing (two distinct ``graph.pbtxt`` files, not two name-aliases
for one) before any prod write.

## Change 41 — AIBox LLM swap to OpenArc (multi-model serving) (2026-05-03)

**Why.** Change 40 shipped the triage bypass as a workaround for
the OVMS multi-LLM limitation surfaced earlier the same day. The
operator subsequently surfaced [OpenArc](https://github.com/SearchSavior/OpenArc),
an OpenVINO-backed inference server explicitly designed for "Model
concurrency: load and infer multiple models at once" on Intel
devices via OpenAI-compatible endpoints. After two-phase shadow
validation (single-model and dual-name routing on ``:11435``) plus
a hand-launch validation on ``:11434`` covering tool calls and 30B
performance on GPU.0, OpenArc was promoted to production.

**What landed (host side — AIBox).**

1. **Model dirs moved** to a clean OpenArc-owned tree:
   - ``C:\AI\models\OpenVINO\Qwen3-30B-A3B-int4-ov`` → ``C:\OpenVino\models\Qwen3-30B-A3B-int4-ov``
   - ``C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov`` → ``C:\OpenVino\models\Qwen3-0.6B-int4-ov``
   - On-volume rename, instant. ``graph.pbtxt`` (OVMS-only) and
     other OVMS artifacts inside the model dirs removed.
2. **OVMS NSSM service stopped and removed** (``nssm remove ovms confirm``).
   Force-killed the lingering ovms.exe parent process (NSSM stop did
   not propagate cleanly).
3. **OpenArc install completed** at ``C:\OpenVino\OpenArc\`` (operator-
   cloned). Build issues fixed:
   - ``uv sync`` was failing on ``gpu-metrics`` (a local pybind11 C++
     extension needing ``level_zero/ze_api.h``). The Level Zero SDK
     was already installed at ``C:\Program Files\LevelZeroSDK\1.26.1\``;
     the gpu-metrics setup.py hardcodes Linux include paths and
     never looks there. Fixed by setting ``INCLUDE`` and ``LIB`` env
     vars before re-running ``uv sync``. (Same fix would work upstream
     via a setup.py patch — left as an open OpenArc issue rather
     than a local change.)
   - Post-sync optimum-intel pin (git HEAD) needed to fix a torch
     ABI mismatch (``cannot import name '_attention_scale'``).
   - Post-sync openvino-genai nightly pin needed to match the
     openvino 2026.1 → 2026.2 ABI bump optimum-intel pulled in.
4. **Models registered in OpenArc** with original names (so
   ``services.yaml::model`` doesn't change):
   - ``OpenVINO/Qwen3-30B-A3B-int4-ov`` → GPU.0,
     ``runtime_config={KV_CACHE_PRECISION: u8, CACHE_DIR: C:/OpenVino/cache}``
     (mirrors OVMS tuning).
   - ``OpenVINO/Qwen3-0.6B-int4-ov`` → CPU.
5. **NSSM ``openarc`` service** registered:
   - App: ``C:\OpenVino\OpenArc\.venv\Scripts\openarc.exe``
   - Args: ``serve start --port 11434 --load-models OpenVINO/Qwen3-30B-A3B-int4-ov OpenVINO/Qwen3-0.6B-int4-ov``
   - AppDirectory: ``C:\OpenVino\OpenArc``
   - AppStdout/AppStderr: ``C:\OpenVino\OpenArc\logs\service-{stdout,stderr}.log``
   - Start: SERVICE_AUTO_START
6. **Old OVMS install nuked**: ``C:\AI\ovms\``, ``C:\AI\ovms-cache\``,
   ``C:\AI\ovms-logs\``, ``C:\AI\ovms-venv\``, ``C:\AI\ovms_2026.1.0.zip``,
   ``C:\AI\models\``. Per operator directive after live validation.

**What landed (container side).**

7. **``services.yaml`` URL paths updated** for all 5 LLM slots
   (``llm_interactive``, ``llm_autonomy``, ``llm_vision``,
   ``llm_triage``, ``llm_commands``):
   - ``http://192.168.1.75:11434/v3/v1/chat/completions``
     → ``http://192.168.1.75:11434/v1/chat/completions``
   - The ``/v3/`` prefix was OVMS's non-canonical
     OpenAI-compat path; OpenArc serves the standard ``/v1/`` path.
8. **``services.yaml::llm_triage.model`` updated**:
   - ``OpenVINO/Qwen3-30B-A3B-int4-ov`` → ``OpenVINO/Qwen3-0.6B-int4-ov``
   - This is consumed by the autonomy Tier 2 disambiguator (a real
     code path, separate from the ``triage_plugins`` bypass shipped
     in Change 40). The 0.6B classifies in <1 s on CPU vs. 11–25 s
     warm on the 30B.
9. **Container restarted** to pick up the config. Healthy in ~80 s.
10. **services.yaml backup** preserved at
    ``…/configs/services.yaml.bak.pre-openarc-2026-05-03`` on the
    docker host for rollback.

**Compatibility verification.**

- **Tool calls**: OpenArc parses hermes-style ``<tool_call>...</tool_call>``
  tags and emits OpenAI-standard ``finish_reason: tool_calls`` +
  ``tool_calls: [{id, type: function, function: {name, arguments}}]``.
  Identical shape to OVMS's ``--tool_parser hermes3`` output. No
  container-side changes needed.
- **Reasoning content**: OpenArc keeps ``<think>...</think>`` inline
  in ``content`` (no separate ``reasoning_content`` field). Container's
  existing ``llm_processor._extract_thinking_standard`` handles the
  inline form already (THINKING_OPEN_TAGS includes ``<think>``).
- **Performance**: 30B on GPU.0 measured at **42.0 tok/s decode,
  1.01 s TTFT** (vs. OVMS baseline 39.8 tok/s). 0.6B on CPU at
  **0.44 s TTFT, ~9 tok/s decode**.
- **NSSM-managed restart**: 30B reload from OpenVINO model cache hit
  in **9.6 s** (cold first load was 53.7 s). Substantially faster
  than the 70 s OVMS cold start.

**Live-probe evidence.**

- ``GET /v1/models`` returns both model entries on loopback and on
  the LAN bind (``192.168.1.75:11434``).
- ``/openarc/status`` reports ``total_loaded: 2`` with 30B@GPU.0 +
  0.6B@CPU.
- Container ``Tier 2 disambiguator`` boot log confirms the new
  config: ``ollama=http://192.168.1.75:11434/v1/chat/completions
  model=OpenVINO/Qwen3-0.6B-int4-ov (slot=llm_triage)``.
- Container autonomy ``Behavior Observer`` agent successfully
  invoked the 30B post-restart (``Slot update: Behavior Observer ->
  adjusted``).
- OpenArc request log shows ``status=200`` chat completions from
  ``192.168.1.150`` (the docker host).

**Open follow-ups.**

- **Revert Change 40 triage bypass.** With the 0.6B model now
  reachable in <1 s, ``triage_plugins`` can call the LLM again and
  recover the prompt-token optimization (chat sees only relevant
  plugins per turn). Deferred until next session — the bypass is
  doing its job, no urgency.
- **gpu-metrics setup.py upstream fix.** OpenArc hardcodes Linux
  paths in the gpu-metrics extension's setup.py. A 5-line patch to
  add Windows defaults would make the install one-step. Worth a PR
  to ``SearchSavior/OpenArc``.
- **OpenArc benchmarking under sustained load.** Today's perf
  measurement is a single-shot cold-warm probe. The OVMS baseline
  of 39.8 tok/s came from a 1024-token decode benchmark; the
  current OpenArc numbers may shift under longer contexts.
