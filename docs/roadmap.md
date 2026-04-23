# GLaDOS Container — Feature Roadmap

Items flagged for future development. Ordered roughly by priority and
architectural dependency.

---

## Stage 3: HA Conversation Bridge + MQTT Peer Bus (large)

**Target:** p95 < 1s visible response for common device commands vs
the current 10-20s LLM agentic loop. Conversational disambiguation
when device names are ambiguous. MQTT peer-bus integration with
NodeRed / Sonorium for bidirectional event exchange.

**Plan:** see `docs/Stage 3.md` for full architecture and phased
implementation.

**Summary:** Three-tier matching. Tier 1: HA `/api/conversation/process`
via websocket (fast, device commands). Tier 2: LLM disambiguation with
entity-cache candidates when Tier 1 misses ("bedroom lights" → clarify
which of 3). Tier 3: existing full LLM + MCP tools path for complex
queries. HA state mirrored via WebSocket (`subscribe_entities`), not
MQTT statestream. MQTT (Phase 2, pending) is reframed as a peer bus —
NodeRed publishes to `glados/cmd/*`, subscribes to `glados/events/*`.
Source-tagging + per-domain intent allowlist gates sensitive operations
(locks, alarms, garage, cameras) per utterance source.

**Status (2026-04-18):**

- ✅ **Phase 0** — audit logging, source-tagging, JSON-lines store
  with WebUI viewer endpoint. **Live in production.**
- ✅ **Phase 1** — HA WS client + EntityCache + ConversationBridge
  (Tier 1) + LLM Disambiguator with intent allowlist (Tier 2) +
  Persona Rewriter (GLaDOS voice on Tier 1 hits). **Live in
  production.** Live latencies: Tier 1 ~0.6–1 s, Tier 2 ~5–11 s.
- ⏳ **Phase 2** — MQTT peer bus (NodeRed/Sonorium). Not started.
- ⏳ **Phase 3** — labeled test corpus, WS reconnect integration
  test, MQTT round-trip CI test, second-factor design for sensitive
  intents. Not started.
- ✅ **Phase 4** — Neutral model support + conversation persistence +
  memory review queue. **Live in production.** 157 tests pass.
  See CHANGES.md Change 9 for details.
- ✅ **Phase 5** — Full WebUI restructure + Memory tab +
  dedup-with-reinforcement + service auto-discovery. **Live in
  production.** 178 tests pass. See CHANGES.md Change 10 for details.
- ✅ **Phase 6** — Configuration menu reorganization + YAML
  minimization + user-friendly defaults + same-stack mDNS defaults.
  **Live in production.** 255 tests pass. See CHANGES.md Change 11
  for details.

See `docs/CHANGES.md` Change 8 + Change 9 for full landing details.

---

## Stage 3 follow-ups (post-Phase 1, pre-Phase 2)

Things surfaced by live testing of Tier 1 + Tier 2 that are worth
fixing but didn't block deploy.

### "Switch" entities pollute "lights" candidate filter (medium)

When the user says "bedroom lights", `domain_filter_for_utterance`
maps to `["light", "switch"]` (some operators control overhead lights
via switches). On the operator's house, Sonos exposes `switch.*`
entities like `Sonos_Master Bedroom Crossfade` and
`Sonos_Master Bedroom Loudness` that fuzzy-match "bedroom" and end up
in the Tier 2 clarify list. The LLM then names them as candidates
even though they have nothing to do with illumination.

Possible fixes:
- When the user explicitly says "lights" (the noun), restrict
  candidates to `domain=light` only; only include `switch` when the
  user says "switch" or no `light` candidates exist
- Or post-filter switches by friendly_name keyword (skip if name
  contains 'sonos', 'media', 'crossfade', etc.)

### HA misclassifies state queries as `action_done` (medium)

"Is the kitchen cabinet light on" comes back from HA's conversation
API as `response_type=action_done` with speech "Turned on the
lights" — HA's intent matcher treats it as an action. Tier 1 honors
HA's verdict; the rewriter restyles the wrong text. Fix would need
local query-vs-action detection (regex for `is the …`, `what is …`,
`how much …`, etc.) before the HA call so we can either short-circuit
to a state lookup or warn the LLM.

### Some entities report success without state change (medium)

Discovered during the lights test matrix: 139/198 lights are in
`unavailable` state but HA's conversation API still accepts service
calls against them and returns `action_done`. Tier 1 reports success;
no actual change happens. Fix: post-execute state verification on a
short delay; if state didn't transition as expected, retry or report.

### Conversation history not propagated ✅ Fixed in Change 9 (Phase 4)

Tier 1/2 exchanges now persist to `/app/data/conversation.db` with
HA `conversation_id` captured per turn. The next turn passes the
prior `conversation_id` back to HA so multi-turn context is
preserved. Verified via `tests/test_multi_turn.py`.

### Reduce Tier 2 latency (low)

5–11 s for Tier 2 is well above the original 2–5 s plan target. The
14B disambiguator is the bottleneck (instruction-following requires
the larger model). Possible improvements:
- Smaller fine-tune of the 3B specifically for the disambiguator's
  JSON schema, retaining the 14B's instruction-following on the
  important cases
- Streaming the JSON output and starting the WS call_service as
  soon as `entity_ids` and `service` are parsed
- Cache last-N decisions per (utterance, candidate-set) hash for
  rapid re-asks

---

## Stage 3 Phase 4 follow-ups (post-Change 9)

### WebUI Memory tab + full UI restructure ✅ Shipped in Change 10 (Phase 5)

Landed across six commits (6984ac2 → docs commit). Highlights:
- Sidebar restructured; Configuration is a parent with System,
  Global, Services, Speakers, Audio, Personality, SSL, Memory,
  Raw YAML nested under it; default page is Chat.
- Memory page: operator-curated long-term facts list + recent
  activity stream + optional pending-review panel. Passive facts
  now default to `review_status="approved"` and reinforce on
  repetition via ChromaDB cosine-similarity dedup (0.30 threshold,
  +0.05 importance bump, cap 0.95). `mention_count`,
  `last_mentioned_at`, `last_mention_text`, `original_importance`
  fields added.
- Service auto-discovery: Discover button + URL-blur auto-fetch
  populates model/voice dropdowns from upstream `/api/tags` and
  `/v1/voices`. Health dots now route through the new
  `/api/discover/health` endpoint (always 200, latency in tooltip).
- SSL field duplication resolved — Global no longer renders
  `ssl.domain` / `ssl.certbot_dir`; the dedicated SSL page is the
  single source of truth.
- UX: stackable toast notifications, confirm dialogs on destructive
  memory actions, Major Mono Display heading font, live engine
  status dot in the sidebar brand (polls `/api/status` every 30 s
  when the tab is visible).
- Tests: +21 new (9 discover endpoints, 5 memory endpoints,
  6 memory dedup, 1 review-queue override). 178 total pass.

### Scheduled daily summarization (low)

`MemoryConfig.summarization_cron` is still a placeholder. The
existing `CompactionAgent` triggers on token threshold which covers
the primary need; a cron-based daily summary is a polish follow-up
that would put end-of-day rollups into ChromaDB independent of how
chatty the day was.

### Operator-side model swap not yet executed

Phase A code supports running with a neutral base model
(`qwen2.5:14b-instruct-q4_K_M` instead of `glados:latest`), but the
operator's `/app/configs/glados_config.yaml` still references
`"glados:latest"`. Swap is a one-line edit + container restart +
`ollama rm glados:latest`. Phase A's tests guarantee the
ModelOptionsConfig + cfg.personality.model_options pass-through path
works correctly with whichever model is loaded.

### Per-principal conversation_id (parked)

The SQLite schema already supports it; everything still uses the
single `"default"` partition. When multi-user WebUI auth or MQTT
peer-bus integration arrives, switching is just a constructor
argument away.

---

## Stage 3 Phase 6 follow-ups (post-Change 11)

### WebUI Logs view ✅ Shipped

Landed 2026-04-18. Configuration > Logs page sits between Memory and
SSL in the sidebar. Three log sources: the container's own stdout
(via `docker logs glados --timestamps`), ChromaDB stdout (via
`docker logs glados-chromadb`), and `audit.jsonl` from disk. Controls:
source dropdown, lines-back (100 / 500 / 1000 / 2000 / 5000), level
filter (all / warn+error / error-only), Refresh button, and a 10-second
Auto-refresh toggle that tears down on nav-away. WARN / ERROR / SUCCESS
/ INFO / DEBUG spans are color-coded. Endpoints: `GET /api/logs/sources`
and `GET /api/logs/tail?source=<key>&lines=<n>`, both auth-protected
and read-only. 5000-line hard cap on tail responses.

Live SSE streaming was considered but skipped for v1 — the 10 s
polling timer is simpler and covers the main use case (watching errors
scroll in while reproducing an issue).

### System-page absorption of auth/audit/mode_entities.maintenance_* ✅ Shipped in `bdbddda`

Shipped 2026-04-18. Two new System-tab cards — Maintenance Entities
(mode_entities.maintenance_mode/.maintenance_speaker) and
Authentication & Audit (auth.enabled, .session_timeout_hours,
audit.enabled). Both render via `cfgBuildForm(..., 'sysaux', ...)`
so field IDs don't collide with Integrations. Integrations
`skipKeys` extended to `['ssl','paths','network','auth','audit',
'mode_entities']`. Sensitive auth fields (password_hash,
session_secret) stay advanced.

### TTS Engine "unexpected response shape" on Discover ✅ Fixed in `7768ce4`

Shipped with the 2026-04-18 hotfix alongside the Tier 2 conversational
bleed fix. The `discover_voices` handler now accepts the
`{"voices": [...]}` shape GLaDOS Piper returns, in addition to the
pre-existing top-level-list and OpenAI `{"data": [...]}` shapes.

### Chitchat time-stamp prefix ✅ Fixed in `7c0bf71` + `ccc0c1e`

Root cause: the engine's autonomy loop writes "Autonomy update.
Time: ..." turns to the same `conversation.db` the chat path reads
from. A user saying "Tell me a joke" was shipping 41 messages to
Ollama, 23 of which were timestamped status pings — the 14B
picked up the framing and started every reply with "The chronometer
reports 12:47 PM ...".

Fixed via `_sanitize_message_history` in `api_wrapper.py`:
- Always-on shape repair (tool_calls.arguments coerced to dict,
  None content normalized to "") — closes a pre-existing bug
  where a single bad row blocked every subsequent chat with
  `json: cannot unmarshal string into Go struct field`.
- Chitchat-path autonomy-noise filter: drops user turns whose
  content starts with "Autonomy update." / "[summary]",
  `role: "tool"` messages, and empty-content assistants with a
  tool_calls payload. Home-command path keeps the full history
  because MCP reasoning benefits from prior device actions.

Measured effect on prod: chitchat message count 41 → 16. Response
quality confirmed clean on "What do you think of dogs?" — in-
character, no time prefix, no home framing.

### Ollama instance unification (Option C) ✅ Shipped in `2c250c9` + prod

Operator asked whether there's a reason autonomy + vision can't
share the same Ollama instance as chat. Short answer: no — Ollama
supports multiple loaded models. The split was an artifact of
pre-unified days when interactive used `glados:latest` and
autonomy used neutral qwen.

- `.env.example` + `docker/compose.yml` comment out the split
  env vars by default; unset → fall back to `OLLAMA_URL`.
- Code already implemented the fallback chain — no logic change.
- Prod: removed `OLLAMA_AUTONOMY_URL` / `OLLAMA_VISION_URL` from
  the compose env list. Autonomy unified immediately via
  services.yaml. Vision required pulling `llama3.2-vision:latest`
  onto B60 first; then `services.yaml` `ollama_vision.url` flipped
  from 11435 → 11434. T4 #0 is now free for other use.

### Chat self-healing for polluted conversation history ✅ Shipped in `69568c2` → `ccc0c1e`

Covered above under "Chitchat time-stamp prefix" — the same
`_sanitize_message_history` helper does both repair duties. No
"Clear conversation" button was built; the self-healing approach
was preferred per operator feedback.

### Actually delete the deprecated config fields (later)

Commit 2 marked 13 fields `Field(deprecated=True)` with loguru WARN
on YAML. After operators confirm none are needed (give it a release
cycle in production), remove the fields from the pydantic models,
drop the corresponding `FIELD_META` entries, and simplify the
warn-validators. No code references them today, so deletion is
just schema cleanup.

### Service-health paths ✅ Fixed in `7c4f5d6`

System-page was showing TTS Engine and ChromaDB Memory dots red
while both services were actually healthy, and the sidebar's own
engine-status dot (the red dot above the Configuration menu) was
always red regardless of state. Three stacked bugs:

- TTS check used `socket.create_connection(("localhost", 5050))` —
  hardcoded localhost is wrong. Now probes
  `<speaches base>/v1/voices`, same endpoint the Services-page
  Discover button uses.
- ChromaDB check used `http://localhost:8000/api/v2/heartbeat` —
  ignored `cfg.memory.chromadb_host` / `chromadb_port`. Now reads
  from config (`glados-chromadb:8000` in compose).
- Sidebar `pollEngineStatus()` reads `data.running` but
  `_get_status` had never populated that key. Added
  `status["running"] = bool(status.get("glados_api"))` so the
  sidebar reflects GLaDOS API reachability.

### Chat-priority gate: autonomy yields on shared Ollama ✅ Shipped in `ad24c20`

Operator report 2026-04-19: single-GPU deployments had autonomy
ticks competing with user chat for the same Ollama queue, blowing
Tier 2's 25 s disambiguator budget and forcing slow Tier 3
fall-through. New `glados.observability.priority` module exposes
a `chat_in_flight()` context manager; chat-path callers wrap their
Ollama round-trip and `AutonomyLoop._should_skip` yields when the
flag is set. Supports the "single GPU is the default" model — and
operators who want hardware isolation can still set
`OLLAMA_AUTONOMY_URL` / `OLLAMA_VISION_URL`.

### Services-grid health dots lied on Ollama / speaches ✅ Fixed in `04f1acf` + `f34e963`

`discover_health()` defaulted to probing `/health` on every URL,
which 404s on Ollama (uses `/api/tags`) and the TTS side of speaches
(uses `/v1/voices`). Result: five false-red dots on the LLM &
Services page. Now picks per-service probe paths via an optional
`kind=` hint (ollama / tts / stt / speaches / api_wrapper /
vision), with a multi-path fallback when no hint is supplied.
Connection-refused short-circuits on the first probe.

### Chat-URL drift: LLM & Services save → `glados_config.yaml` ✅ Fixed in `23a4d92`

Phase 6 design gap: the LLM & Services page wrote through pydantic
ServicesConfig (backing `services.yaml`), but the chat engine reads
its own `completion_url` and `autonomy.completion_url` fields from
`glados_config.yaml`. Operators moving their Ollama via the UI
silently diverged from the engine's actual chat routing — surfacing
as HTTP 504 on the first chat after a server move. `_put_config_section`
now mirrors `ollama_interactive.url` and `ollama_autonomy.url` into
the engine's yaml on save. New `_ollama_chat_url()` helper
normalises bare bases or partial `/api/...` URLs to the canonical
`.../api/chat` form.

### Tier 2 timeout 25 s → 45 s ✅ Shipped in `b4f5721`

Priority gate reduced contention but the 14B JSON-constrained
disambiguator call on a busy GPU genuinely takes 25–35 s. Falling
through to Tier 3 is strictly worse on the same hardware. Operators
on faster hardware can override via `DISAMBIGUATOR_TIMEOUT_S` env.

---

## Stage 3 Phase 7+ targets (next session)

Listed in the order they should be tackled. The two **P0** bugs at
the top are new as of 2026-04-19; they surfaced while verifying
unified Ollama on T4 #1 and are the right starting point for the
next session. Everything below them is still open but lower
priority than device-control correctness.

### P0: Tier 2 never sends `service_data` — brightness / colour / temperature requests silently no-op (high)

Operator report 2026-04-19:

    User: "The office is too dark. Can you adjust the desk lamp up a little bit?"
    GLaDOS: "Adjusting the office desk monitor lamp to a brighter setting, as requested."

The light didn't change. Audit trail shows Tier 2 executed with:

    service: light.turn_on
    entity_ids: ["light.task_lamp_one"]
    service_data: (never populated)

The disambiguator's JSON schema only exposes `decision`,
`entity_ids`, `service`, `speech`, `rationale`. There is no
`service_data` field for brightness_pct, color_temp_kelvin,
color_name, transition, etc. Every "dim", "brighter", "set to
50%", "warmer", "change to blue" request goes through as a bare
`turn_on` and Home Assistant does whatever the device's default
is — which reads as "nothing happened" to the operator.

**Scope of fix:**

1. Add `service_data` to the disambiguator's output schema and
   prompt examples ("dim" → lower brightness_pct vs current;
   "set to 40%" → brightness_pct=40; "warmer" → color_temp_kelvin
   toward 2700; "turn it up" → brightness_pct=100 *or* current+25).
2. Relative adjustments need the current state. The cache already
   has `EntityState.attributes` available on the candidate list —
   thread those through to the prompt so the LLM can compute
   "current + 25".
3. Pass `service_data` through to `HAClient.call_service` — the
   method already accepts it as a kwarg, so plumbing is trivial.
4. Tests: a handful of phrasings (brightness absolute, brightness
   relative, color by name, color temperature, fan speed).
5. Audit row should include service_data for debuggability.

File: `glados/intent/disambiguator.py` (prompt + schema + call),
`glados/ha/ws_client.py` / `glados/ha/conversation.py` (call_service
passthrough check), `tests/test_disambiguator.py` (new cases).

### P0: Follow-up turns without a device keyword bypass Tier 1 / 2 entirely (high)

Operator report 2026-04-19 (same session):

    (after GLaDOS had just acted on the desk lamp)
    User: "It's still too dark. Turn it up more."
    GLaDOS: "Increasing the office desk monitor lamp to maximum illumination.
             That should suffice."

No `tier=1` or `tier=2` audit row for that utterance. Tier 3
(chitchat) handled it, hallucinated a status confirmation, and
never called a tool.

Root cause: `rules.looks_like_home_command()` is keyword-based.
"Turn it up more" has no device word and no activity phrase →
returns False → `api_wrapper` skips Tier 1 + Tier 2 per the
chitchat-fast-path work from Phase 6 Change 12 (commit `df84d07`).

**Options, in order of preference:**

- **Option A (recommended): context carryover.** When the prior
  assistant turn resolved a home command successfully (Tier 1
  `ok:*` or Tier 2 `ok:execute`), let the next user turn inherit
  home-command intent for one turn even if it has no keyword.
  Implement in `api_wrapper` at the precheck site, not in
  `looks_like_home_command()` itself — keep the helper pure.
- **Option B: trust the fresh HA conversation_id.** If HA's
  prior `conversation_id` is within the cache freshness window
  (5 s), Tier 1 should fire regardless of keyword matching; HA's
  own intent engine can handle "it" from context.
- **Option C (weakest): extend keyword list.** `brighter`,
  `dimmer`, `warmer`, `cooler`, `louder`, `quieter`, `more`,
  `less`, `up`, `down`, "turn it". Brittle but cheap.

A + B together is the right call. C should only backstop them.

Tests: add a multi-turn case — first turn "turn on the lights"
(keyword hit), second turn "a little dimmer" (no keyword) —
assert tier_2 was consulted on both.

### Validate single-T4 as the supported default single-GPU path (high)

Late 2026-04-19 consolidation work identified T4 #1 as a working
single-GPU target for the entire GLaDOS stack when vision is
disabled:

- `qwen2.5:14b-instruct-q4_K_M` (chat + Tier 2 disambiguator) — ~9.0 GB
- `qwen2.5:3b-instruct-q4_K_M` (persona rewriter) — ~1.9 GB
- `nomic-embed-text` (ChromaDB) — optional, ~0.3 GB
- Leaves ~4 GB headroom on a 16 GB T4 for KV cache.

CUDA Ollama is stable (unlike the B60/IPEX stall — see next entry).
Operator already consolidated services: B60 Ollama stopped, T4 #0
Ollama orphan killed, T4 #1 keeps 14B + 3B resident. Chat works
at ~19 s warm via commit `23a4d92`'s glados_config.yaml URL sync.

Next-session work:

1. Run chat + Tier 2 + Tier 1 + autonomy end-to-end against the
   unified `11436` endpoint for a full evening of live use; log
   any timeouts or queue-contention issues.
2. Document T4-single as the **recommended** single-GPU target in
   README (currently the README says "a single Ollama at
   OLLAMA_URL hosts everything" but doesn't speak to which GPU).
3. Decide whether to ship the `.env.example` / compose defaults
   unchanged (favours the generic `http://ollama:11434` same-stack
   pattern) or add a commented-out "split onto separate Ollama"
   hint block.
4. If single-T4 holds up, the B60/IPEX debug below drops to low
   priority — only matters for operators who specifically want
   Arc acceleration.

### B60 / IPEX-LLM Ollama is genuinely slow (medium — relegated if single-T4 works)

Discovered 2026-04-19 while verifying the chat-priority gate
(commit `ad24c20`). The gate correctly holds autonomy off during
chat, but the underlying B60 Ollama at `the AIBox LAN host:11434` is
returning 50–90s wall times for trivial requests that Ollama's own
stats say should be near-instant:

    SMALL JSON call ("return {color: red}"): 99s wall
      total_duration=99s  prompt_eval=0.28s  eval=0.28s
    NO JSON ("say hello"): 52s wall
      total_duration=52s  eval=0.0s
    NO JSON warm: 91s wall

The 99 % of wall time unaccounted for by prefill + generation lives
somewhere in Ollama's queueing, IPEX runtime dispatch, or Arc
driver stall. It is NOT the priority gate, not the disambiguator
prompt size, not the model — `qwen2.5:14b-instruct-q4_K_M` is
sitting at 16 GB VRAM with `expires=2318...` (effectively
permanent keep_alive).

Impact: unified-Ollama deployments that would otherwise work fine
on single-GPU hardware are blocked until this is understood. The
operator's split config (autonomy → T4 #1 at 11436, vision →
T4 #0 at 11435, chat → B60 at 11434) is unaffected because T4s
respond normally.

Debug next session:
1. Restart the B60 Ollama instance cold and retest. Maybe it's
   accumulated garbage state.
2. Check IPEX-LLM release notes / known issues around JSON grammar
   constraints + Qwen 2.5 14B Q4.
3. Compare `ollama ps` + resource stats while a request is
   mid-flight to see if the model is swapped to CPU under the
   hood.
4. Try `llama3.1:8b-instruct-q4_K_M` (smaller, no grammar) for
   a shape-comparable control.
5. If IPEX is the problem, the fallback is CUDA Ollama on a
   spare T4 — but that negates the point of Option C.

### Tier 3 latency is still painful (~45–240s per turn) (medium)

Even after the 2026-04-18 autonomy-noise filter + tool-catalog
skip, a cold `Tell me a joke` streams for 60–240s on
`qwen2.5:14b-instruct-q4_K_M`. Prefix pollution is no longer the
issue (message count is down to 16 for a fresh chitchat turn);
this is real LLM time. Profiling needed, not guessing. Candidates
worth investigating in this order:

1. **Model-loading check.** First-request cold starts are ~3–5
   minutes because keep_alive didn't persist the new model.
   Confirm `keep_alive=-1` is actually being honoured on the B60
   Ollama (check `/api/ps` `expires_at` field) and that chat
   requests aren't colliding with autonomy-loop prefill.
2. **Prompt size.** Personality preprompt + few-shots is still
   ~10 messages even for chitchat. A single compact system
   message + the user turn should be sufficient for "tell me a
   joke"-class prompts and would cut prefill by an order of
   magnitude. Measure token count before/after.
3. **KV cache reuse.** Each SSE roundtrip re-submits full
   history. If Ollama's num_ctx / ctx-cache reuse isn't set up,
   every turn pays the full prefill cost. Instrument
   `prompt_eval_count` / `prompt_eval_duration` from Ollama's
   response stats on a few successive turns to see if caching
   is kicking in.
4. **Hardware.** B60 via IPEX-LLM. Verify the model is running
   on the Arc GPU (not spilling to CPU). Per-token latency
   should sit around 30 ms at steady state; if it's 200 ms,
   the Arc backend isn't engaged.

Result of this work: a roadmap entry with a number. Right now
the roadmap says "45–240s" because nobody has measured.

### Stop the autonomy loop from writing to the chat conversation store (medium)

The self-healing filter from `7c0bf71`/`ccc0c1e` drops autonomy
chatter from chitchat context at read time. The write is still
happening — `conversation.db` grows and compaction has to sweep
it. Stopping the write at the source is strictly better than
filtering at read.

Two approaches worth considering:

- **Separate conversation stores per role.** The engine autonomy
  loop gets its own `autonomy` conversation_id (already supported
  by the schema); the chat path reads from `default` only. Zero
  mixing.
- **Write-side tagging + read-side partition.** Keep a single
  store but tag autonomy turns with `source="autonomy"` on write
  and filter them out in the chat `snapshot()`. Lower lift; loses
  the DB-disk-space benefit.

Rough code map:
- Autonomy loop writes happen somewhere under
  `glados.autonomy.loop` → check how it currently reaches the
  conversation store. That's the single place to change.
- Keep `_sanitize_message_history`'s autonomy-noise filter as
  defense in depth even after the write-side fix; cheap to leave
  in and catches anything that slips through.

### Dedicated Chat-model routing + "interactive vs autonomy" labels (small)

Related to the above. Once autonomy stops writing to the chat
store, the Tier 3 chat path still pulls `cfg.llm_model` which
might be the autonomy model. The WebUI now has an LLM & Services
page that lets operators pick URLs per service; adding a
`chat_model` field alongside would let them choose a cheaper /
faster model for chat without disturbing autonomy.

---

## Model Independence (medium)

**Context:** The container's `personality_preprompt` in `glados_config.yaml`
contains the full GLaDOS system prompt. AIBox's Ollama `glados:latest`
Modelfile ALSO contains the same system prompt. The container sends the
persona to Ollama on every request, and the Modelfile re-injects it —
double-injection wasting ~1200 tokens of context.

**Goal:** The container should be the sole source of persona. Operators
should be able to point GLaDOS at any base Ollama model (`qwen2.5:14b`,
`llama3.1:8b`, `mistral-nemo:12b`) and get the GLaDOS persona from the
container's config alone.

**Validation done (2026-04-17):** Pointed container at `qwen2.5:14b-instruct-q4_K_M`
with no Modelfile SYSTEM. Persona WAS injected successfully — model knew
about Aperture Science, home management role. Character adherence was
weaker than with the Modelfile-tuned version. Modelfile's `PARAMETER`
settings (temperature, top_p) noticeably affect persona strength.

**Implementation:**
- Send `options.temperature`, `options.top_p`, etc. in the Ollama request
  payload from the container (already done for `num_ctx`)
- Remove or document-deprecate the Modelfile approach
- Update session handoff docs to specify base model, not custom Modelfile
- Test with multiple base models, document which work best for tool calling

**Dependency:** None. Container already injects persona; just needs
parameter override in payload.

---

## Dismissive Tool Call Refusals (medium)

**Symptom:** GLaDOS sometimes responds with dismissive text ("do it or
don't, I don't care", "that's beneath me") instead of calling the tool
even for clear device control requests. The persona's "condescending
competence" trait competes with tool-use instructions.

**Possible fixes:**
1. Post-processing: detect refusal patterns in the streamed response and
   retry with an even more explicit tool-use instruction
2. Stronger pre-processing: always include "you MUST call the tool" as the
   LAST system message before the user turn (current hint may be getting
   lost in the context)
3. Response validation: after LLM responds, if no tool call was made but
   the user message contains device control keywords, warn the user or
   retry
4. Persona adjustment: soften the "refusal is in character" trait in the
   system prompt

**Status:** Known issue, not yet fixed.

---

## SSL Volume Persistence (small)

**Context:** `/app/certs` is currently a named Docker volume (`glados_certs`).
Certs survive container restarts but not `docker compose down -v`. Operators
can't easily back up or inspect the Let's Encrypt data.

**Fix:** Document in README the recommended volume change from
`glados_certs:/app/certs` to `${DOCKERCONFDIR}/glados/certs:/app/certs:rw`
for host-filesystem persistence. Already applied on operator's production
deployment.

---

## WebUI: LLM Backend Model Selection

**Context:** The WebUI config panel requires the operator to type the Ollama
URL and model name manually. This is error-prone and requires knowing exact
model identifiers.

**Requirement:** When an LLM backend URL is entered in the WebUI config
(e.g. `http://ollama:11434`), the UI should query that endpoint for available
models and populate a dropdown.

**Implementation notes:**
- Ollama exposes `GET /api/tags` — returns all locally available models
- OpenAI-compatible backends expose `GET /v1/models`
- WebUI should try both, use whichever responds
- Dropdown refreshes on URL field blur or manual refresh button
- Selected model writes to `glados_config.yaml` → `llm_model`
- Handle unreachable backends gracefully

---

## Authentication follow-ups (TODO — 2026-04-23)

Two items added after the 2026-04-23 incident where the WebUI
password was wiped by a partial-save bug and the operator had to
bootstrap through a shell command. The partial-save bug itself is
fixed in this session (`configs/global.yaml` merge-on-write); these
items address the larger UX gap.

### Supported auth system (medium)

**Context:** current auth is a single bcrypt password hash stored
in `configs/global.yaml` with a `session_secret` cookie. There is
no per-user identity, no MFA, no OIDC, no rate limiting on sign-in,
no account recovery. It's fine for LAN-trust deployment behind a
reverse proxy but does not scale to multi-operator or any
internet-facing use.

**Suggested scope:**

- Swap the bespoke bcrypt path for a battle-tested auth library
  (e.g. `authlib` / OIDC relying-party, or integrate with an
  existing SSO — Authelia, Keycloak, Cloudflare Access).
- Retain local-bcrypt as a fallback for offline / bootstrap use.
- Per-operator identity in the audit log (currently every WebUI
  action is "the operator" — can't tell two operators apart).
- Rate-limit sign-in attempts (currently unlimited).
- Account recovery flow that doesn't require shell access.

**Not scoped yet.** Larger architectural change — touches the
auth middleware, session cookie, audit log schema, and login
page. Blocker for exposing the WebUI beyond LAN.

### Startup wizard UI (medium)

**Context:** first-run experience today is "open the WebUI, hit
sign-in page, realize there's no password set, SSH into container,
`python -m glados.tools.set_password`, restart container, come
back, sign in." Operator reasonably expects to set a password
through the UI itself.

**Suggested scope:**

- Detect first-run state: `cfg.auth.password_hash == ""` AND no
  `configs/config.yaml` (or an explicit bootstrap marker file)
  means "fresh install."
- On first request to any page, redirect to a one-time
  `/setup/welcome` route that:
    1. Shows a welcome screen explaining what GLaDOS is, what
       services it expects upstream (Ollama + speaches + HA +
       ChromaDB), and whether they're reachable (reuse the
       Discover-endpoint health checks).
    2. Prompts for an initial password (write via the existing
       `glados.tools.set_password` logic).
    3. Prompts for HA URL + token (validates by hitting HA's
       `/api/` with the token before accepting).
    4. Shows "you're done" screen with links into Configuration.
- Wizard is one-shot: once the password hash is written, the
  setup route 302-redirects to `/login` and can't be replayed.
- Recovery path: if the operator needs to reset without UI
  access, a `GLADOS_BOOTSTRAP=1` env var re-enables the wizard
  for one container start.

**Not scoped yet.** Decent-sized feature — new route handler, new
page templates, integration with the auth middleware to bypass
sign-in during setup.

---

## Emotion system follow-ups (TODO — 2026-04-23)

Three items surfaced during Phase Emotion A–I (see CHANGES.md
Change 22) that are worth picking up later but were not blockers.

### "Acknowledged but didn't perform" signal (small)

**Context:** operator-approved frustration scenario — GLaDOS
verbally commits to an action ("Turning off the lights now") but
the action doesn't actually fire at HA. Today the emotion agent
only sees the text reply; it can't tell that the device never
changed state. Legitimate escalation trigger when it happens.

**Suggested scope:**

- HA state-verification hook compares the resolver's promised
  state delta against the mirror cache N seconds later.
- On mismatch, push an EmotionEvent with a `[SEVERITY:NOTABLE]`
  tag and short description ("promised off, light still on").
- Feeds the existing deterministic-delta path without any LLM
  involvement.

**Not scoped yet.** Would need a small verification job scheduler
and careful de-duplication against the regular command-ack race
(`call_service` timeouts sometimes indicate late success, not
failure — see Tier 2 no-ack handling).

### Emotion classifier PAD region retuning (cosmetic)

**Context:** during Phase Emotion probes, `classification.name`
stayed on "Contemptuous Calm" even at P=−1.0 / A=+1.0. The
directive and TTS override both key off pleasure bands directly
(not the classifier label) so behaviour is correct — but the
operator-facing dashboard label is misleading.

**Suggested scope:** `configs/emotion_config.yaml` defines PAD
regions → human-readable emotion names. Add regions for the
hostile / menacing ends of the PAD cube so the live state slot
shows "Hostile Impatience" / "Sinister Menace" when those bands
actually apply.

### Tier-1 weather response caching (small)

**Context:** identical weather replies came back on back-to-back
asks during probe testing ("the weather is currently…" word-for-
word). Not an emotion issue — the HA/weather-cache path returns
cached data and the persona rewriter happens to produce similar
output. Flagged here because it hurts the "variations feel
different" quality bar the emotion work was trying to hit.

**Suggested scope:** rewriter-side anti-parrot — track the last
N Tier-1 weather outputs and re-roll if the new candidate
matches word-for-word.

---

## Unit-conversion quick responses (TODO — 2026-04-22)

**Context:** operator often asks GLaDOS for quick unit conversions —
"how many feet in a meter," "how many teaspoons in a cup," "convert
20°C to Fahrenheit" — and the full LLM chain is overkill for
deterministic arithmetic. The voice assistant should feel *instant*
for this class of question.

**Requirement:** a short-circuit path that detects conversion intent,
does the math locally, and answers in 1-2 sentences in persona
without hitting the full tier-3 pipeline.

**Suggested scope:**

- Detector at the precheck stage (same site as the intent/command
  recognition gate) that spots patterns like:
  - *"how many X in Y"* / *"X to Y"* / *"convert X units to Y"*
  - numeric magnitude + unit keyword.
- Local conversion engine covering at least:
  - Length: mm / cm / m / km / in / ft / yd / mi
  - Mass: g / kg / oz / lb / st
  - Volume: ml / L / tsp / tbsp / fl oz / cup / pint / quart / gal (US+UK)
  - Temperature: °C / °F / K
  - Speed: mph / km/h / m/s / knots / ft/s
  - Time: s / min / hr / day
  - Pressure / energy / power as follow-ups
- Persona layer wraps the numeric answer in a GLaDOS-voiced
  one-liner through the persona rewriter (reusing the same path the
  announcement quips use). Keep it to 1-2 sentences.
- Audit-log entry with a `conversion` kind so operator can see how
  often the fast path fires vs falling through to tier 3.

**Why not just let the LLM handle it:** current warm latency on
qwen3:14b is ~15-20 s. A local conversion is sub-millisecond and the
voice result feels live. This is the same argument as Tier 1 HA
conversation: offload the determinism-amenable intents to code so
the LLM only sees the genuinely ambiguous ones.

**Not scoped yet.** Captured here so the design reference exists when
we pick it up.

---

## Multi-Persona Support

**Context:** GLaDOS is the default persona but the system is fundamentally
a persona injection layer on top of any LLM. Architecture supports swapping
the system prompt and personality config — this feature exposes that
through the UI.

**Requirement:** Dropdown in the WebUI switches the active persona. Switch
changes:
- System prompt
- Few-shot examples
- HEXACO personality traits
- Attitude pool
- TTS voice (if multiple available)
- Persona name in UI and chat

**Example personas to ship:**
- GLaDOS (default)
- Star Trek Computer (neutral, precise)
- HAL 9000 (calm, polite, subtly threatening)
- Custom (operator-defined, uploaded)

**Implementation notes:**
- Each persona = YAML file in `configs/personas/`
- Contains: name, system_prompt, hexaco, attitudes, tts_voice,
  few_shot_examples
- Active persona stored in runtime config, persisted across restarts
- Persona switch effective on next turn, no restart required
- Custom persona file upload via WebUI or volume mount

**Dependency:** Config store complete (Stage 1 done)

---

## Stage 4: Voice Pipeline (existing plan, unchanged)

Register GLaDOS Kokoro voice in speaches, wire HA STT/TTS to use the
container's `/v1/audio/speech` proxy. Voice input from HA satellites /
ESPHome devices.

Requires: voice training or voice model import in speaches.

---

## Stage 5-10 (see architecture-plan.md)

- Stage 5: Containerize remaining non-GPU infrastructure (largely done
  via the current Docker host deployment)
- Stage 6: Host migration / GPU passthrough resolution
- Stage 7: Containerize Ollama + speaches with GPU
- Stage 8: Open WebUI integration
- Stage 9: Discord unification
- Stage 10: Persona layer documentation

---

## Technical Debt

- **Named Docker volumes for chat audio** — should be host-mounted for
  backup resilience (same pattern as certs)
- **Hot-reload for SSL changes** — currently requires container restart.
  Python `http.server` doesn't easily support socket rebinding at runtime.
- **Broken-pipe errors in logs** — streaming connections get dropped when
  browser closes mid-response. Cosmetic, non-critical.
- **node_modules/** not needed but `docker-compose.override.yml` tracking
  could be tidied in `.gitignore`
- **`glados/webui/tts_ui.py` encoding issues** — file has a UTF-8 BOM
  at byte 0 (breaks strict parsers like `ast.parse`; Python's module
  loader accepts it) AND mojibake in comments (Windows-1252 characters
  like em-dash saved as UTF-8 of their cp1252 bytes, showing up as
  `â€"` sequences). Fix: save file as UTF-8 without BOM, normalize
  mojibake to proper unicode (`—`, `"`, `'`). Verify no runtime impact
  (there shouldn't be any) before/after.
- **`docker-compose.yml` has obsolete `version:` attribute** — docker
  compose v2 emits `the attribute 'version' is obsolete, it will be
  ignored` on every `docker compose` command. Remove the top-level
  `version:` line. Cosmetic but polluting deploy logs.
- **`.github/workflows/build.yml` pins Node 20 actions** —
  `actions/checkout@v4`, `docker/login-action@v3`,
  `docker/setup-buildx-action@v3`, `docker/build-push-action@v5` all
  use Node 20 which is deprecated; GitHub will force Node 24 on
  2026-06-02 and remove Node 20 on 2026-09-16. Bump to current
  major versions before the forced cutover.
- **WebUI audit double-logging** — `webui/tts_ui.py` logs
  `origin=webui_chat` in `_chat` / `_chat_stream`, then proxies to
  `api_wrapper` which logs a second row (correctly tagged webui_chat
  via `X-GLaDOS-Origin`). Each WebUI utterance produces two audit
  rows. Intentional for tracing the full path but noisy for
  operator view — decide whether to dedupe at the viewer layer or
  suppress one of the two call sites.

### Standards-compliance scanning

Any new non-standards-compliant issues found during work should be
added to this list. Current scan helpers:
- BOM scan: `python -c "from pathlib import Path; [print(p) for p in Path('glados').rglob('*.py') if Path(p).read_bytes().startswith(b'\xef\xbb\xbf')]"`
- Mojibake scan: same pattern looking for `b'\xc3\xa2\xe2\x82\xac'` (UTF-8 of `â€`).
