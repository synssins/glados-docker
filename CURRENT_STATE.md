# CURRENT_STATE.md — Disambiguation Pipeline Inventory

**Date:** 2026-04-19
**Purpose:** Pre-rewrite map of every file, function, and config currently
involved in command disambiguation. Written per the rewrite prompt's
"Order of Work" step 1; all file deletions / architectural changes gated on
Chris acknowledging this document.
**Scope:** `C:\src\glados-container` (the Docker middleware container, the
working repo per `C:\src\SESSION_STATE.md`). Host-native GLaDOS at
`C:\AI\GLaDOS` is NOT covered — it has a parallel but separate codebase.

---

## 0. TL;DR — Two pipelines run in parallel today

The container has **two independent command paths** that do not share
disambiguation logic. The rewrite collapses both into a single resolver.

| Pipeline                | Entry point                     | Matcher        | Output style         |
|-------------------------|---------------------------------|----------------|----------------------|
| Voice (`/command`)      | `handle_command()`              | `commands.yaml` + `match_light_command()` (exact keyword + alias) | Pre-recorded WAV via HA `media_player`; chat response is `"."` |
| Chat (SSE + JSON)       | `_stream_chat_sse()` / `_try_tier1_nonstreaming()` | Three-tier: HA WS → LLM disambiguator → agentic loop | Text via SSE/JSON, persona-rewritten |

The voice path is the "old" keyword matcher the rewrite prompt targets for
deletion. The chat path is the "new" three-tier architecture from Stage 3
Phase 1, extended through Phase 6+ follow-ups. They do not share a
`SourceContext`, do not share memory, and reach HA through different code
paths (voice uses `_ha_call_service()` REST; chat uses `ConversationBridge`
WebSocket + `ToolExecutor` MCP).

---

## 1. Entry points

### Voice / `/command` endpoint
- `glados/core/api_wrapper.py:2471` — `/command` route definition
- `glados/core/api_wrapper.py:476` — `handle_command(request_data)` handler
  - Extracts text from payload (line 489)
  - Loads `commands.yaml` via `_load_cmd_config()` (line 494)
  - Runs `match_light_command()` (line 498)
  - Calls HA REST service via `_ha_call_service()` (line 513)
  - Picks base WAV + follow-up WAVs, concatenates with silence (lines 519–529)
  - Plays through HA `media_player` (lines 531–546)
  - Emits audit + HUB75 display event (lines 550–556)
  - Returns `"."` as the chat response when invoked through chat flow (line 485) — the spec behavior the rewrite prompt bans

### Chat SSE — streaming path
- `glados/core/api_wrapper.py:1832` — `_stream_chat_sse()` (priority-gate wrapper)
- `glados/core/api_wrapper.py:1851` — `_stream_chat_sse_impl()` (real work)
  - Zero-buffered http.client streaming (bypasses TTS until final)
  - Explicit memory-command detection (lines 1874–1910)
  - Flows through Tier 1 → Tier 2 → Tier 3

### Chat JSON — non-streaming path
- `glados/core/api_wrapper.py:1415` — `_try_tier1_nonstreaming()` wrapper
- `glados/core/api_wrapper.py:1431` — `_try_tier1_nonstreaming_impl()`
  - `/api/chat` with `stream: false`

### Chat SSE Tier 1 fast path
- `glados/core/api_wrapper.py:1566` — `_try_tier1_fast_path()` wrapper
- `glados/core/api_wrapper.py:1580` — `_try_tier1_fast_path_impl()`
  - Emits SSE early on Tier 1 hit

### Other ingress
- Discord, MQTT, HUB75 — not currently routed through the disambiguator;
  emit via their own adapters

---

## 2. Three-tier matcher (chat path only)

### Tier 1 — HA Conversation Bridge
- `glados/ha/conversation.py`
  - `ConversationBridge` class (line 216)
  - `process(text, conversation_id, language, timeout_s)` (line 223) — calls HA WS `/api/conversation/process`
  - `classify(raw)` (line 250) — returns `ConversationResult` with:
    - `handled` / `should_disambiguate` / `should_fall_through` / `speech`
    - `should_disambiguate=True` on HA error codes `no_intent_match`, `no_valid_targets` (`_FALL_THROUGH_CODES`)
  - `_is_garbage_speech()` (line 63) — rejects HA's `"None None"` templated responses
- `glados/ha/ws_client.py` — persistent WS connection, exponential backoff, `get_states` resync on reconnect
- `glados/ha/entity_cache.py` — in-memory mirror of ~3,500 entities + fuzzy matcher

### Tier 2 — LLM disambiguator
- `glados/intent/disambiguator.py`
  - `Disambiguator` class (line 108), stateless
  - `run(utterance, source, source_area, assume_home_command, prior_entity_ids, prior_service)` (line 129)
  - Flow:
    1. `looks_like_home_command()` precheck (`glados/intent/rules.py:102`)
    2. `cache.get_candidates(..., source_area=source_area)` (line 183)
    3. Carry-over merge of `prior_entity_ids` (lines 196–202)
    4. State freshness guard — skip filter if max age > `max_state_age_seconds` (default 5 s)
    5. `_build_prompt()` (line 472) — system: hard role override, naming convention, activity inference, state filtering, allowlist, GLaDOS persona, service mappings
    6. `_call_ollama(prompt_messages)` (line 740) — autonomy Ollama, timeout `DISAMBIGUATOR_TIMEOUT_S` (default 45 s)
    7. `_safe_parse_json(raw)` (line 236)
    8. Branch on decision: `execute` / `clarify` / `refuse` / fall-through
    9. Allowlist enforcement (line 322) via `IntentAllowlist.is_allowed(source, domain, device_class)` (`glados/intent/rules.py:199`)
    10. Trailing-vocative strip on speech (line 251)
  - `DisambiguationResult` (lines 48–66): includes `service_data` field (schema-supported, currently unused by prompt — **this is the P0 #1 bug in SESSION_STATE**)

### Tier 3 — agentic LLM loop
- `glados/core/llm_processor.py` — full agentic chat with HA MCP tools
- Reached when Tier 1 misses and Tier 2 returns `should_fall_through=True`
- Tool execution via `glados/core/tool_executor.py`

---

## 3. Context & memory

### Conversation store
- `glados/core/conversation_db.py`
  - `ConversationDB` class (line 109) — WAL SQLite at `/app/data/conversation.db`
  - Schema (lines 48–62): `messages(conversation_id, idx, role, content, tool_calls, extra, source, principal, ts, tier, ha_conversation_id)`
  - Thread-safe (`_lock: threading.RLock()`)
  - `append()` / `get_messages()` APIs
- `ConversationStore` (wrapper) hydrates last 200 messages on startup

### Origin / source context
- `glados/observability/audit.py:40–56` — `Origin` string constants:
  `WEBUI_CHAT`, `API_CHAT`, `VOICE_MIC`, `MQTT_CMD`, `AUTONOMY`, `DISCORD`,
  `TEXT_STDIN`, `UNKNOWN`
- Propagated via `X-GLaDOS-Origin` HTTP header through the WebUI proxy
- **No dataclass / no area_id field** — origin is a flat string. Satellite
  `area_id` is NOT currently carried anywhere through the chat pipeline.
  (The voice pipeline reads area from `commands.yaml`, not from the
  satellite device registry.)

### HA conversation_id
- Stored on each message row (`ha_conversation_id`)
- Retrieved via `_last_ha_conversation_id()` in api_wrapper
- Passed back to `ConversationBridge.process(conversation_id=prior_conv)` at line 1454
- Persisted through audit + stash at lines 1497, 1534, 1543

### Carry-over (pronoun / follow-up support)
- `_stash_recent_tier_action()` stores `(entity_ids, service, ha_conversation_id)` in `_recent_tier_action`
- `_get_recent_tier_action()` returns it on the next turn
- Used at api_wrapper.py:1377–1380 to populate `prior_entity_ids` / `prior_service` into Tier 2
- **Limitation** — this is the only memory available to the disambiguator;
  it covers "brighter" but not "and the office" (because "and the office"
  needs verb + params reuse with a new area, which the current carry-over
  doesn't model). This is the P0 #2 bug in SESSION_STATE.

---

## 4. Entity resolution

### Fuzzy matcher
- `glados/ha/entity_cache.py`
  - `rapidfuzz.fuzz.WRatio` (lines 27–32)
  - Per-domain cutoffs (lines 41–61):
    - 75 — light, switch, fan, media_player, sensor, binary_sensor
    - 60 — scene, script (looser semantics)
    - 80 — climate, input_*, vacuum, cover, binary_sensor (sensitive)
    - Sensitive domains (lock, alarm_control_panel, camera, garage covers) require exact friendly_name
  - `_preprocess_query()` (line 98) strips command verbs/stopwords before matching
  - Coverage% + same_area bonus in ranking (lines 111–119)

### Area awareness
- `source_area` parameter exists on `get_candidates()` and is honored for
  same-area bonus — **but nothing sets it from a real source today.**
  Chat has no satellite; voice reads area from `commands.yaml`, not
  HA's device registry.

### Entity state model
- `EntityState` dataclass (in entity_cache.py) — `entity_id`, `friendly_name`,
  `domain`, `device_class`, `state`, `area_id`, `timestamp`, `attributes`
- Populated by the WS client

### Candidate list sent to Tier 2 LLM
- Built in `_build_prompt()` (disambiguator.py:472)
- Fields per candidate: `entity_id, friendly_name, domain, state, coverage%, same_area=yes/no`
- Limit: default 12 (rules.py:150); bumped to 30+ on universal quantifiers ("all lights", "whole house") at disambiguator.py:179–182

---

## 5. Persona rewriter
- `glados/persona/rewriter.py`
  - `PersonaRewriter` class (line 45), `rewrite(plain_text, context_hint)` (line 56)
  - Called on Tier 1 hits only (Tier 2 output is already persona-voiced;
    Tier 3 injects persona via base prompt)
  - `/api/chat` on autonomy Ollama, model default `qwen2.5:3b-instruct-q4_K_M`
  - System prompt at lines 122–158: "ROLE: tone editor"
  - Output cleanup (lines 195–206) strips code fences, preambles, trailing
    quotes, and `BANNED_VOCATIVES` = `{test subject, subject, human, meatbag}`
  - Timeout: `REWRITER_TIMEOUT_S` default 8 s
  - Best-effort fallback: returns original text on failure (lines 95–98)
  - Call site: api_wrapper.py:1509 as `_persona_rewrite(plain_speech, utterance=user_message)`

---

## 6. commands.yaml and the static alias layer (TO DELETE in rewrite)

**Exists.** Path: `cfg._configs_dir / "commands.yaml"` (api_wrapper.py:66)

- Loader: `_load_cmd_config()` (api_wrapper.py:316), cached with mtime
- Structure (inferred from `match_light_command()` at line 337):
  - `commands.lights.device_keywords[]` — trigger words
  - `commands.lights.actions.<key>.keywords[]` — verb list
  - `commands.lights.areas.<key>.aliases[]` — spoken area aliases
  - `commands.lights.areas.<key>.area_id` — HA area_id
  - `commands.lights.areas.<key>.speakers[]` — media_player entity_ids
  - `commands.lights.areas.<key>.bases.<action_key>` — WAV filename
  - Top-level `settings.*` (ha_url, ha_token, serve_host, serve_port,
    sample_rate, silence_between_sentences_ms)

**Matcher:** `match_light_command(text, config)` (api_wrapper.py:337)
- Exact keyword + alias lookup — no regex (prompt's claim of a regex matcher
  was slightly off; it's simpler than regex)
- Returns `(action_key, area_key, area_config)` or `None` (lines 341–383)

**Rewrite-prompt disposition:** `commands.yaml`, `match_light_command()`,
`_load_cmd_config()`, `handle_command()`, and the `/command` route are
all marked for archival + deletion.

---

## 7. handle_command() — what it actually does

**Primary occurrence:** `glados/core/api_wrapper.py:476`

Signature: `handle_command(request_data: dict) -> tuple[dict, int]`

Contrary to the rewrite prompt's framing, this is **not a chat interceptor** —
it's a full handler for the `/command` voice endpoint. The `"."` return at
line 485 only triggers when a chat caller routes through this same function
(a code path where voice and chat got entangled). The WAV playback side is
the primary behavior.

**Rewrite-prompt disposition:** delete entirely. Voice path will be
re-implemented on the new single resolver; WAV confirmations (if any)
become a TTS-layer concern downstream of the resolver's action result.

**Second occurrence** — grep found `handle_command` in `glados/core/engine.py`
but the explore pass did not identify it as a distinct interceptor. Needs
a second read before deletion to confirm it is not a separate hook.

---

## 8. Tool execution (chat path → HA)
- `glados/core/tool_executor.py`
  - `ToolExecutor` class (line 17) — consumes from `tool_calls_queue`
  - `run()` (line 84) — worker loop, runs until `shutdown_event`
  - `_audit_tool()` (line 56) — writes durable audit row per call
  - Tool timeout default 30 s
- `glados/mcp/` — `MCPManager` for HA's MCP server
- MCP endpoint from `glados_config.yaml` `Glados.mcp_servers[]` (usually
  `http://<HA_URL>:8123/api/mcp`)

---

## 9. Configs involved

### `configs/disambiguation.yaml` (template at `.example.yaml`)
- `naming_convention` — user-phrase → entity-type hints
- `overhead_synonyms[]`
- `state_inference` (bool, default true)
- `max_state_age_seconds` (float, default 5.0)
- `candidate_limit` (int, default 12)
- `extra_guidance` (free text appended to system prompt)

### `configs/glados_config.yaml`
- `Glados.llm_model` — chat model (Phase 4: `qwen2.5:14b-instruct-q4_K_M`)
- `Glados.completion_url` — chat Ollama URL
- `Glados.autonomy.llm_model` — Tier 2 + rewriter model
- `Glados.autonomy.completion_url` — autonomy Ollama URL
- `Glados.personality_preprompt` — persona injection
- `Glados.mcp_servers[]` — MCP configs

### `configs/services.yaml` (Phase 6 — WebUI-managed)
- Ollama interactive / autonomy / vision URLs
- Speaches TTS / STT URLs
- Home Assistant REST + WS URLs
- Vision service URL
- Since Phase 6 commit `23a4d92`, saves here sync into
  `glados_config.yaml` so chat engine follows UI edits

### `configs/commands.yaml` (REWRITE TARGET — DELETE)
- See §6

---

## 10. Tests covering the disambiguation path

- `glados/tests/test_disambiguator.py`
  - `_FakeHAClient` records `call_service` calls
  - `_make()` builds `EntityCache` + `Disambiguator` with monkey-patched Ollama
  - Covers `_safe_parse_json` (clean JSON, code-fenced, trailing commentary)
  - Covers full `run()` flow through at least several canonical cases
- `glados/tests/test_intent_rules.py` — allowlist, home-command precheck, activity phrase detection
- `glados/tests/test_multi_turn.py` — conversation history propagation (Phase 4)
- `glados/tests/test_retention.py` — DB retention agent
- `glados/tests/test_memory_review.py` — memory approval flow
- 341 tests pass as of the 2026-04-19 overnight session (per SESSION_STATE)

---

## 11. Contradictions between the rewrite prompt and reality

| Prompt claim                                                      | Reality                                                                                                       |
|-------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| Target is `G:\Projects\GLaDOS\`                                   | Container is `C:\src\glados-container`; host-native (separate) is `C:\AI\GLaDOS`                              |
| Alias layer uses regex                                            | It's keyword + alias lookup, not regex (`match_light_command()` is structurally simpler than the prompt assumes) |
| `handle_command()` is a chat interceptor that returns `"."`       | It's the `/command` route handler; `"."` only fires on a specific cross-path call. Primary job is WAV playback |
| `services/api_wrapper.py`                                         | Container path is `glados/core/api_wrapper.py`                                                                |
| `area_id` flows through voice today                               | Only via `commands.yaml` static area mapping, **not** from HA device registry. Satellite → area lookup missing |
| Lights are already tier-tagged (lamp/accent/task/overhead/flood)  | Not in the container. HA entity labels may exist but nothing in the container prompt/disambiguator reads them |
| `SourceContext` dataclass exists                                  | Only `Origin` string constants; no `area_id`/`session_id`/`satellite_device_id` structure                     |
| Per-session memory keyed by `session_id` exists                   | Only `conversation_id` partition (default = `"default"`); per-session-id is "parked" per SESSION_STATE        |

---

## 12. What the rewrite would replace

If Chris approves, deletion candidates after archival to
`C:\src\glados-container\archive\pre-rewrite-20260419\`:

- `configs/commands.yaml`
- `match_light_command()` in `api_wrapper.py:337–383`
- `_load_cmd_config()` in `api_wrapper.py:316`
- `handle_command()` in `api_wrapper.py:476` (+ the `/command` route at 2471)
- Any `_ha_call_service()` / `_ha_play_media()` paths only reachable from
  `handle_command()` — to be confirmed by call-graph audit before deletion
- The second `handle_command` reference in `glados/core/engine.py` — to
  be re-read before deletion (did not appear in the explore pass as a
  separate interceptor; might be a method call, not a definition)

Things **kept**:

- `glados/ha/conversation.py` + `ws_client.py` + `entity_cache.py` — the
  HA WS client + entity mirror are the plumbing the new resolver also
  needs. Tier 1 behavior itself becomes optional (the new resolver lets
  the LLM decide whether to call the HA conversation API as a tool).
- `glados/intent/disambiguator.py` — the prompt builder and candidate
  filtering logic is the bones of the new resolver; rename + refactor
  rather than rewrite from zero.
- `glados/persona/rewriter.py` — persona voice application remains a
  post-processing stage on successful actions.
- `glados/core/conversation_db.py` — keep. The new session memory (10-min
  idle TTL, ring buffer of last 10 turns) is in-memory, but the DB is
  still the durable audit of actions.
- `glados/observability/audit.py` — extend `Origin` into a `SourceContext`
  dataclass; do not delete.
- `glados/core/tool_executor.py` + `glados/mcp/` — the resolver's action
  step reuses these.

---

## 13. What must be decided before the rewrite starts

1. **Which voice pipeline is the target?** The container's `/command`
   endpoint receives text that has already gone through STT — it does not
   own the satellite → area lookup. If the voice path is owned by Home
   Assistant's own assistant pipeline (HA handles wake word + STT + routes
   via `conversation/process`), then the "voice pipeline `/command` handler"
   the prompt describes may not be this container's responsibility at all.
   Worth confirming with Chris before deleting `/command`.

2. **Where do light tier labels live?** The rewrite requires every
   `light.*` entity to carry a label from `{lamp, accent, task, overhead, flood, strip}`.
   HA supports entity labels; today the container does not read them. The
   prompt's `tag_lights.ps1` helper is new work, not a rewrite of anything
   existing.

3. **Where does `user_preferences.yaml` live?** The prompt writes it to
   `G:\Projects\GLaDOS\configs\user_preferences.yaml`. In the container
   world this would be `configs/user_preferences.yaml` mounted from the
   Docker host's `appdata/glados/configs/`. Needs a config schema +
   Pydantic model + WebUI page (Phase 6 pattern).

4. **`glados_disambiguation_cases.csv` — where does it live?** The prompt
   ships it alongside but the file is not in `C:\src\` at the repo level.
   Before the test harness can be built, Chris needs to drop the CSV
   somewhere (suggested: `tests/fixtures/glados_disambiguation_cases.csv`).

5. **Does the "single resolver for chat + voice" collapse the three-tier
   optimization?** Tier 1's ~1 s latency on simple commands is a real UX
   win over the LLM round-trip. The rewrite description ("The LLM does the
   intent resolution. Code does the plumbing.") implies every command goes
   through the LLM. Worth an explicit yes/no from Chris: are we accepting
   5–11 s latency on *all* device control in exchange for architectural
   simplicity, or does the resolver still fast-path obvious commands?

---

**Next step (awaiting Chris's acknowledgment):** once these five open
questions are resolved, move on to Order-of-Work step 3 — build
`SourceContext`, `UserPreferences`, and the memory store in isolation
with unit tests. No deletions until then.

---

## ADDENDUM 2026-04-19 — Chris's answers + learned-context spec

### Q1 — Who owns the voice pipeline? **Deferred.**

`docs/Stage 1.md:44` documents `/command` as "ESPHome voice command →
pre-recorded WAV". Chris's direction (2026-04-19): **ignore the voice
assistant path for this rewrite.** The real voice-assistant flow goes
through Home Assistant first — by the time speech reaches the GLaDOS
container, HA will have already attached the satellite's area context.
So the container doesn't need to solve satellite → area lookup itself.

**Implications:**
- The current `/command` handler + `commands.yaml` + `match_light_command()`
  remain deletion candidates (they're a legacy ESPHome-direct path not
  relevant to the target architecture).
- The new resolver's voice entry point is whatever HA sends when it
  forwards a voice utterance — most likely through the chat-completions
  endpoint or a new small endpoint that accepts
  `{text, area_id, conversation_id}` from an HA conversation agent /
  automation. Wiring that is a **later phase**, not this rewrite.
- For now, the rewrite targets the **chat path only** (WebUI + API
  consumers). Voice gets a clean seat later on the same resolver once
  HA's forwarding shape is pinned.

### Q2 — HA as source of truth for devices and labels

Confirmed. Light-tier labels (`lamp, accent, task, overhead, flood, strip`)
live on HA entities — either as **HA labels** (the `label_registry` feature
added in HA 2024.x) or as entries in the entity's `categories` / custom
attributes. The container queries them on startup and refreshes on a
timer (refresh interval proposal: **2 hours**, with a shorter debounced
refresh on WS `registry_updated` events so changes propagate within
seconds).

No `tag_lights.ps1` script — tagging happens in HA's UI. The rewrite's
`UserPreferences` model holds only **user-level** knobs (tier priority,
task-area overrides, step sizes, aliases for rooms HA doesn't know
about like "Cindy's office"). It does NOT hold per-entity tier
assignments.

Storage layout updated:

```
HA (source of truth)          GLaDOS container
─────────────────────         ─────────────────────
area_registry         ────►   cached in entity_cache.py
device_registry       ────►   cached (new — needs adding).
                               Every HA device (satellites included)
                               carries its area_id here, so no
                               container-side override map is needed.
entity_registry       ────►   cached, with labels
label_registry        ────►   cached (new — needs adding)
state machine         ────►   cached via WS state stream

                               UserPreferences (local):
                                 - lighting_tier_priority
                                 - task_area_overrides
                                 - area_aliases (for names HA doesn't know,
                                   e.g. "Cindy's office" → area_id=cindy_office)
                                 - default kelvin / brightness / step sizes
```

Missing today: `device_registry` and `label_registry` are not cached
by the container. Adding them is part of the rewrite (and unblocks
the ESPHome satellite → area lookup from Q1).

### Q3 — `configs/user_preferences.yaml`

Confirmed. New Pydantic model + WebUI page (following the Phase 6
pattern of WebUI-first config with optional YAML persistence). The
model schema follows the rewrite prompt's `UserPreferences` dataclass;
the WebUI page lives under the `Personality` section (closest mental
model to "user taste").

### Q4 — CSV fixture

Created at `tests/fixtures/glados_disambiguation_cases.csv` verbatim
from Chris's paste. 110 rows including the header. Test harness in
Order-of-Work step 4 reads this file.

### Q5 — Fast path is fine, plus: **learned-context with HA validation**

Both changes approved:

**5a. Resolver keeps a fast path.** Not every command pays the full
LLM round-trip. When the resolver receives an utterance it will
short-circuit obvious matches through HA's conversation API (the
current Tier 1) before going to the LLM. Three-tier behavior is
preserved — the rewrite is about **unifying the entry points and the
context model**, not about collapsing latency tiers.

**5b. Learned context — durable, HA-validated, decaying.**

Motivation: ambiguous commands with no source area and no recent
turn context ("turn the lights up" spoken to the WebUI on Tuesday
after office work on Sunday) should guess from long-term habit rather
than always asking. But the container must never **act** on a stale
guess — it must **validate** against HA's current state first, and
only proceed if the guess is physically plausible right now.

Data model (new — lives alongside the existing conversation_db):

```sql
-- durable learned-context store
CREATE TABLE learned_context (
  id            INTEGER PRIMARY KEY,
  utterance_key TEXT    NOT NULL,   -- normalized form of utterance
                                    -- (lowercased, punctuation stripped,
                                    --  stopwords optional)
  source_channel TEXT   NOT NULL,   -- 'voice' | 'chat'
  source_area_id TEXT,              -- NULL if none was supplied
  resolved_area_id TEXT NOT NULL,   -- what was chosen last time
  resolved_verb  TEXT NOT NULL,     -- 'turn_on' | 'brightness_up' | …
  resolved_tier  TEXT NOT NULL,     -- 'lamp' | 'overhead' | … (may be NULL)
  reinforcement INTEGER DEFAULT 1,  -- +1 on validated success,
                                    --  −1 on validation fail / user correction
  last_used_at  TIMESTAMP NOT NULL,
  decay_at      TIMESTAMP NOT NULL  -- when this row's weight drops to 0
);

CREATE INDEX idx_learned_context_key
  ON learned_context(utterance_key, source_channel, source_area_id);
```

Resolution flow with learned-context:

1. Resolver receives utterance + `SourceContext`.
2. Short-term memory check (10-min session ring buffer) — existing
   carry-over. If hit, use it. Done.
3. **If short-term is empty AND source_area is NULL:**
   a. Compute `utterance_key` from the raw text.
   b. Query `learned_context` for rows matching
      `(utterance_key, source_channel, source_area=NULL)`
      with `reinforcement > 0`, ordered by `last_used_at DESC,
      reinforcement DESC`.
   c. If a candidate row exists, build a **guess**:
      `{area_id=row.resolved_area_id, verb=row.resolved_verb,
        tier=row.resolved_tier}`.
   d. **Validate against HA state** before acting. Example validations:
      - Verb is `brightness_up`: are there lights currently ON in
        `row.resolved_area_id` that can be brightened? (state=on AND
        brightness < 255). If none → guess invalid.
      - Verb is `turn_off`: is anything currently on in that area?
        If no → guess invalid.
      - Verb is `scene_activate` for a named scene: does the scene
        still exist in HA? If no → guess invalid.
   e. If validation passes: execute. Then on success, bump
      `reinforcement += 1`, update `last_used_at`, push `decay_at`
      forward.
   f. If validation fails or user corrects the action within N seconds
      ("no, the bedroom"): decrement `reinforcement`, let the resolver
      re-route to clarification.
4. If no learned row OR guess invalidated: ask for clarification
   (current behavior).

Decay:

- Each row has a TTL of **14 days from last use**. A row untouched for
  14 days drops off.
- A row with `reinforcement ≤ 0` is deleted immediately (self-correcting
  — learned-context never accumulates bad guesses).
- Background sweep in the retention agent (already exists —
  `glados/autonomy/agents/retention_agent.py`) does the cleanup.

Safety properties:

- **HA is still the source of truth.** Learned context only ranks
  candidate interpretations — it never overrides a current HA state
  that contradicts the guess.
- **Explicit user input always wins.** If the utterance contains an
  area name, that overrides any learned preference. If the source
  context has a satellite-derived area, that overrides too.
- **No dark patterns.** When the resolver acts on a learned guess
  (step 3e), the response should audibly acknowledge the scope:
  "Office lights up." — not just "Done." — so the user can correct
  if wrong on the same turn.
- **Audit.** Every learned-context hit writes an audit row with
  `tier=learned, confidence=reinforcement_count,
  validation_result=pass|fail_*` so it's inspectable.

### Revised §13 — all open questions closed

1. ✅ Voice path deferred. HA forwards voice utterances with area context
   already attached; the new resolver handles chat first, voice joins the
   same resolver later once HA's forwarding shape is pinned.
2. ✅ HA is source of truth for light tier labels (`label_registry`) AND
   for device→area mapping (`device_registry`). Container caches both,
   refreshes every 2h + on WS `registry_updated`. No manual override
   map — HA already has every device's area, including voice satellites.
3. ✅ `configs/user_preferences.yaml` with Pydantic + WebUI page.
4. ✅ CSV fixture created at `tests/fixtures/glados_disambiguation_cases.csv`.
5. ✅ Fast path preserved. Learned-context store added — durable,
   HA-validated at use time, decays in 14 days, self-corrects on
   reinforcement ≤ 0.

### Architectural principle (clarified 2026-04-19 by Chris): **one door**

GLaDOS is an OpenAI-API-compatible bridge between any consumer and the
Ollama backend. It acts as Ollama would, from the client's perspective,
plus the persona + tools + memory middleware. There is therefore
**one user-command entry point**: `POST /v1/chat/completions` (and its
streaming SSE variant). Every consumer hits it:

```
WebUI ─┐
HA (voice utterances forwarded)  ─┤
Discord bot  ─┤──►  POST /v1/chat/completions  ──►  CommandResolver
Any third-party OpenAI client ─┘                     (persona + tools
                                                      + disambiguation
                                                      + learned context)
```

The difference between a voice utterance and a WebUI chat is **metadata
on the same request**, not a different endpoint:

```
SourceContext built from:
  X-GLaDOS-Origin        →  "webui_chat" | "ha_voice" | "discord" | "api_chat" | …
  X-GLaDOS-Area-Id       →  HA area_id (voice sets this; chat usually NULL)
  X-GLaDOS-Session-Id    →  per-session conversation thread key
  X-GLaDOS-Principal     →  optional caller identity (username / device id)
```

(If HA's OpenAI conversation agent can't set custom headers, the same
fields ride as an OpenAI extension field in the request JSON body, e.g.
`extra_body={"glados_context": {...}}`.)

### Non-LLM endpoints that stay as-is

These are HA→GLaDOS machine-to-machine **event hooks**, not user
conversations. They don't touch the resolver and aren't part of the
rewrite:

- `POST /announce` — HA pushes pre-generated announcement state
  changes for GLaDOS to play on a speaker
- `POST /doorbell/screen` — HA pushes doorbell camera frame display
  triggers
- `GET /health`, `GET /entities`, `GET /v1/models` — service meta
- Various WebUI admin endpoints (`/api/audit/recent`, config pages,
  etc.) — WebUI control plane, not conversation

### Rewrite scope for this pass

- Single entry point: `POST /v1/chat/completions` (streaming + non-streaming).
- New `CommandResolver.resolve(request, source_context)` called from the
  handler whenever the LLM's first pass judges the utterance to be a
  home-control intent (the LLM decides — no regex pre-filter).
- `SourceContext` dataclass replaces the flat `Origin` string. Built
  from headers (or body extension) on every request.
- Resolver retains the three-tier strategy internally (HA conversation
  fast path → LLM disambiguator → agentic loop) for latency, behind
  one context model.
- Learned-context SQLite store, validated against HA state at use time.
- `device_registry` + `label_registry` caching added to the HA layer.
- **Delete entirely:** `configs/commands.yaml`, `match_light_command()`,
  `_load_cmd_config()`, `handle_command()`, the `POST /command` route,
  and any `_ha_call_service()` / `_ha_play_media()` paths only reachable
  from `handle_command()`. Archived first to
  `archive/pre-rewrite-20260419/`. The ESPHome-direct voice endpoint
  goes away; HA-mediated voice joins the chat-completions path later.

Ready to proceed to Order-of-Work step 3 (build `SourceContext`,
`UserPreferences`, and the memory/learned-context stores in isolation
with unit tests, no deletions yet) on Chris's go-ahead.
