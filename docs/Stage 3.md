# Stage 3 — HA Conversation Bridge + MQTT Peer Bus

**Status:** Approved direction (2026-04-17)
**Supersedes:** The prior "MQTT Intent Classifier" plan (deleted)
**Prerequisite:** Stage 2 (tool execution loop) complete, SSL complete

---

## What changed from the prior plan

Four adversarial reviews on 2026-04-17 (security/OWASP, architecture, 2025-2026
best-practice research, codebase reality check) surfaced the following pivots:

| Prior plan | Revised plan | Reason |
|---|---|---|
| Local `hassil` + `home-assistant-intents` classifier | Delegate to HA `/api/conversation/process` | One fewer pinned dependency; HA maintains entity sync + intent templates; negligible latency delta over LAN; user does not want to maintain a custom sentence grammar. |
| MQTT `statestream` for state mirroring | HA WebSocket API (`subscribe_entities`) | `mqtt_statestream` is a community integration, one-way, no metadata updates, subject to 2026.x churn. WS is first-party, bidirectional, supports resync on reconnect. |
| `paho-mqtt` | `aiomqtt` v3 | 2026 consensus async MQTT client; `paho-mqtt` sync is still fine but would bolt awkwardly onto a design that needs event-loop integration with the WS client. |
| MQTT as the HA transport | MQTT as a **peer bus** for NodeRed / Sonorium / other LAN services | User has existing MQTT infra. HA state/command goes via WS; MQTT is where sibling services exchange events with GLaDOS. |
| Global fuzzy threshold 50 | Per-domain safety gates; HA-side fuzzy; LLM disambiguation for ambiguous cases | Existing code at `api_wrapper.py:1300` already uses threshold 50, which is dangerous for `lock`/`alarm`/`cover`/`camera`. Offload safe fuzzy to HA; disambiguate the rest with the LLM. |
| Single-tier: fast path OR LLM | Three-tier: HA conversation → LLM disambiguation → full LLM | The Alexa UX problem ("no device by that name, please check") is exactly what Tier 2 prevents. |

Deployment context that shapes the threat model:

- **Container is LAN-only.** The operator's hostname is a split-horizon
  DNS record resolving internally; there are no port forwards or DMZ
  rules.
  Several "Critical" findings from the security review drop to Medium
  because of this — but the microphone-channel attacks (TV ads, ultrasonic
  injection, prompt injection via chat) are unchanged by network topology.

---

## Problem

1. **Latency.** Current path (LLM → tool call → HA → LLM response) is
   10-20s for a simple light switch. Target: p95 < 1s visible response
   for common device commands.
2. **Name resolution UX.** Today, if the user says "bedroom lights" and
   there are three fixtures in the bedroom, the LLM either picks wrong,
   refuses, or (worst case) emits an Alexa-style "no device by that name"
   error. The user should never need to memorize exact friendly_names.
3. **Peer integration.** NodeRed and Sonorium already speak MQTT.
   Bidirectional event exchange with GLaDOS lets those services trigger
   utterances / intents / autonomy cycles, and lets their flows react to
   what GLaDOS is doing.
4. **Safety.** The existing single-threshold fuzzy matcher is a
   door-unlocking-by-typo risk for `lock`/`alarm`/`cover`/`camera`.
   Source of the utterance (chat UI, voice mic, MQTT bus, autonomy loop)
   currently has no trace through the pipeline and cannot gate sensitive
   operations.

---

## Architecture: Three-Tier Matching

```
User utterance (+ source tag)
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│ Tier 1 — HA Conversation API (fast, <1s)                 │
│   POST /api/conversation/process                         │
│   Accept only response_type == action_done && success    │
│   On match: execute → persona rewriter → stream response │
└────────────────────────┬─────────────────────────────────┘
                         │ miss / low confidence / ambiguous
                         ▼
┌──────────────────────────────────────────────────────────┐
│ Tier 2 — LLM Disambiguation (medium, 2-5s)               │
│   Feed LLM: utterance, candidate entity list (fuzzy      │
│     pre-filtered from local cache), HA's error reason    │
│   Single tool: resolve_and_execute(entity_ids, service)  │
│   LLM either picks or asks a clarifying question in      │
│     persona voice                                        │
└────────────────────────┬─────────────────────────────────┘
                         │ out of scope / complex query
                         ▼
┌──────────────────────────────────────────────────────────┐
│ Tier 3 — Full LLM w/ MCP tools (slow, 5-15s)             │
│   Unchanged from current Stage 2 agentic loop            │
│   Reserved for weather, general conversation, multi-step │
└──────────────────────────────────────────────────────────┘
```

Gate on every tier: **source-tag + intent-allowlist check** before any
service call reaches HA.

---

## Components

### 1. HA WebSocket client — `glados/ha/ws_client.py`

- Persistent websocket to `/api/websocket` using the existing HA token.
- Sequence: `auth` → `subscribe_entities` (state deltas) → `get_states`
  (initial snapshot with metadata).
- Maintains `EntityCache` (in-memory) keyed by `entity_id` with:
  `friendly_name`, `aliases[]`, `area_id`, `area_name`, `domain`,
  `device_class`, `state`, `attributes`.
- Exposes:
  - `async get_candidates(query, domain_filter=None) -> list[EntityMatch]`
    — rapidfuzz-based local candidate list (not final resolution; for
    Tier 2 context and audit only).
  - `async call_service(domain, service, data) -> ServiceCallResult` —
    issues WS `call_service` message, awaits result.
  - `async conversation_process(text, conversation_id=None) -> ConvResult`
    — issues WS `conversation/process`.
- Reconnect with exponential backoff + `get_states` re-sync on
  reconnect. Staleness SLO: <1s for state, <5s for metadata.
- **Per-entity freshness tracking.** Every cache entry carries a
  `state_as_of: datetime` updated on each WS state delta. On
  `get_states` resync after a reconnect, all entries are refreshed
  together. Consumers (notably the state-based inference rule in the
  disambiguator) query `cache.age(entity_id) -> timedelta` and decide
  how much to trust it. Freshness budget is config-driven in
  `glados_config.yaml`:

  ```yaml
  ha_cache:
    max_state_age_seconds: 5      # hard ceiling for state-based inference
    warn_state_age_seconds: 2     # log warning above this; still usable
    resync_on_stale: true         # if a consumer sees an entry older than
                                  # max, trigger a get_states resync
  ```

  If `resync_on_stale` is true and a consumer hits a stale entry, the
  cache issues a `get_states` refresh in the background (rate-limited
  to once per 10s) so subsequent lookups have fresh data.
- Async-native; bridged to the threaded main request path using the
  same `asyncio.run_coroutine_threadsafe` pattern already used by
  `glados/mcp/manager.py:171`.

### 2. HA conversation bridge — `glados/ha/conversation.py`

- Thin wrapper over the WS `conversation/process` call.
- Classifies HA's response:
  - `action_done` + `success` → Tier 1 win.
  - `action_done` + `error.code in {no_intent_match, no_valid_targets}`
    → fall through to Tier 2.
  - `query_answer` → Tier 1 win (state query, e.g. "is the garage open").
  - anything else → Tier 3 (full LLM).
- Persona rewriter (below) transforms HA's plain-English response into
  GLaDOS voice. HA's own response text is **never** surfaced raw to the
  user.

### 3. Disambiguation layer — `glados/intent/disambiguator.py`

Invoked only when Tier 1 misses. Not a new classifier; it's a constrained
LLM prompt with:

- The original utterance.
- Source tag and whether the source is allowed to act on sensitive domains.
- Up to N fuzzy-matched candidate entities from `EntityCache`
  (pre-filtered to reasonable domains for the intent).
- **Current state** of each candidate entity (on/off, brightness, etc.) —
  state drives several of the disambiguation rules below.
- HA's error reason as context.
- The operator's **disambiguation rules** (below), loaded from config.
- A single tool: `resolve_and_execute(entity_ids: list[str], service: str, params: dict)`.

LLM is instructed to either:
- Call the tool with the unique right answer if context disambiguates.
- Call the tool with multiple entity_ids if the user clearly meant a group.
- Ask a clarifying question in persona voice if truly ambiguous.
- Refuse (persona voice) if the requested action is blocked by the
  allowlist for this source.

Runs on the fast autonomy Ollama model (T4 CUDA) for speed; revisit if
quality insufficient.

#### Disambiguation Rules (config-driven)

Lives in `configs/disambiguation.yaml`, loaded at startup and passed to
the disambiguator as part of its system prompt. Initial ruleset:

**Naming convention (operator's terminology):**

| Term | Meaning |
|---|---|
| `lamp` / `lamps` | plug-in fixtures or smart bulbs |
| `light` / `lights` | overhead fixtures (plural = all lights in scope) |
| `switch` / `switches` | physical wall switches |

**Scope-broadening rule (generic terms in an area).**
When the user names a **group** in an area ("bedroom lights", "kitchen
lamps"), resolve to **every entity of that type in that area**, not just
one. "Turn off the bedroom lights" with three ceiling fixtures = turn off
all three. Specific terms ("overhead", "ceiling", "reading lamp") resolve
to only those fixtures. A specific term always beats the generic group
when both could match.

**Specific-term override.**
`overhead` and `ceiling` always mean the ceiling fixture, regardless of
how friendly_names are labeled in HA. The disambiguator matches these
terms against `device_class`, entity_id fragments, and friendly_name in
that priority order.

**State-based inference.**
If an action clearly implies a state transition (e.g. "turn off the
lights" when nothing is on is meaningless), filter candidates by current
state first. Concretely:

1. For `turn_off`-type intents, only consider candidates currently `on`.
2. For `turn_on`-type intents, only consider candidates currently `off`.
3. **If after state filtering exactly one candidate (or one coherent
   group) remains, that is the answer** — even if the utterance did not
   specify an area. "Turn off the lights" when only the kitchen overhead
   is on → turn off the kitchen overhead, no clarification asked.
4. If state filtering leaves multiple disjoint groups, fall back to
   clarification. ("Two rooms currently have lights on — kitchen and
   office. Which would you like darkened?")

**State-freshness guard (correctness-critical).**
State-based inference is only applied when every candidate entity's
`cache.age(entity_id) <= ha_cache.max_state_age_seconds` (default 5s).
If any candidate's state is stale, the disambiguator:

1. Triggers a background `get_states` resync (if `resync_on_stale`).
2. For this turn, **does not use state to narrow candidates** — falls
   back to area + naming-convention rules only.
3. If still ambiguous, asks for clarification rather than guessing on
   stale data.

Rationale: acting on stale state silently produces wrong-device
outcomes (turning off a light the user already turned off, leaving on
the one they thought was off). A brief clarification question is
strictly better than a confident wrong action, especially for lights
where the user's next move is often to try again and accumulate
frustration.

Rules are data, not code, so the operator can edit them without a
redeploy — disambiguator re-reads the file on change.

### 4. Persona rewriter — `glados/persona/rewriter.py`

Short prompt: *"Rewrite this plain confirmation in GLaDOS's voice,
preserving every fact: '{ha_response}'"*. Runs on the autonomy model;
streams through the existing TTS + chat pipeline. Target: <1s additional
latency on top of Tier 1.

### 5. Source tagging — engine-wide

Every utterance entering the engine carries a `source` field:

| source | Origin |
|---|---|
| `webui_chat` | WebUI chat pane |
| `api_chat` | External caller hitting `/v1/chat/completions` |
| `voice_mic` | STT pipeline (future Stage 4) |
| `mqtt_cmd` | NodeRed / Sonorium publishing to `glados/cmd/*` |
| `autonomy` | Self-triggered autonomy loop |
| `discord` | Discord bridge (existing module) |

Plumbed through:
- `engine.llm_queue_priority` item dict gains a `source` key.
- `ToolExecutor` and the new disambiguator both check source before
  executing sensitive-domain actions.
- Audit log records it.

### 6. Intent allowlist — `glados/intent/allowlist.py`

Matrix of `source × domain` with default-deny. Initial policy:

| Domain | webui_chat | api_chat | voice_mic | mqtt_cmd | autonomy |
|---|---|---|---|---|---|
| `light`, `switch`, `fan`, `scene`, `media_player`, `script` | allow | allow | allow | allow | allow |
| `climate`, `input_boolean`, `input_number`, `input_select` | allow | allow | allow | allow | allow |
| `cover` (non-garage), `vacuum` | allow | allow | allow | allow | deny |
| `cover` (garage), `camera` | allow | deny | deny | deny | deny |
| `lock`, `alarm_control_panel` | allow (+ future PIN) | deny | deny | deny | deny |

`garage` detection via `device_class=garage` attribute on the cover
entity.

Tier 1 consults the allowlist after HA resolves the target entity;
if denied, short-circuits to a persona-voice refusal via rewriter.
Tier 2 / Tier 3 consult the allowlist before `resolve_and_execute` /
any MCP tool call.

### 7. MQTT peer bus — `glados/mqtt/peer_bus.py`

Client: `aiomqtt` v3. Persistent connection, auto-reconnect, TLS preferred.

**Subscribed topics (NodeRed/Sonorium → GLaDOS):**

| Topic | Payload | Effect |
|---|---|---|
| `glados/cmd/say` | `{text: str, voice?: str}` | Speak arbitrary text through TTS. Source tag `mqtt_cmd`. |
| `glados/cmd/intent` | `{utterance: str, conversation_id?: str}` | Feed utterance into Tier 1→2→3 pipeline. Subject to intent allowlist for `mqtt_cmd`. |
| `glados/cmd/think` | `{seed: str}` | Trigger an autonomy cycle with the given seed. |
| `glados/cmd/action/<name>` | `{params?: dict}` | Execute a named action from `configs/mqtt_actions.yaml`. |

**Published topics (GLaDOS → NodeRed/Sonorium):**

| Topic | Payload | When |
|---|---|---|
| `glados/events/heard` | `{source, text, ts}` | User input transcript lands in engine. |
| `glados/events/spoke` | `{text, voice, ts}` | TTS begins streaming an utterance. |
| `glados/events/intent` | `{utterance, tier, entity_ids, service, params, result, latency_ms, ts}` | Intent executed (any tier). |
| `glados/events/state` | `{status: idle \| listening \| thinking \| speaking \| error, ts}` | Status transition. |

Broker credentials in `.env` only; **never** rendered in the WebUI config
pane, **never** logged. Prefer mTLS client certs over username/password.

Mosquitto ACL template shipped in `docs/mosquitto-acl.example`:
```
user glados-bridge
topic read  glados/cmd/#
topic write glados/events/#
```
(Bridge user reads only its own cmd namespace, writes only its own
events namespace, has no access to `homeassistant/#` or anything else.)

### 8. Audit log — `glados/observability/audit.py`

JSON-lines at `/app/logs/audit.jsonl`, rotated 30 days. Fields:

```
ts, source, principal, utterance, tier, ha_conv_response,
candidates[], chosen_entity_ids, service, params, result,
latency_ms, allowlist_decision
```

WebUI "Activity" tab shows last N entries with filter by source/tier/domain.

### 9. Session auth on `/api/chat` — `glados/core/api_wrapper.py`

Current state: `_handle_chat_completions` at `api_wrapper.py:2163` has
no auth check. WebUI is protected via session cookie
(`tts_ui.py:292-304`), chat API is not.

Change: apply the same session verification used by WebUI paths. When
the session cookie is absent AND the request originates from a
configured LAN CIDR allowlist, log a warning but allow (backward compat
for existing local scripts). When neither: 401.

New config keys in `glados_config.yaml`:

```yaml
auth:
  require_api_session: true
  lan_allowlist: ["192.168.1.0/24"]
```

---

## Data Flow Examples

### Example A — clean Tier 1 hit

```
User (webui_chat): "turn off the kitchen lights"
  → source_tag = webui_chat
  → WS conversation/process → action_done, turned off light.kitchen_ceiling
  → allowlist(webui_chat, light) = allow
  → persona rewriter: "Illumination in the kitchen, eliminated."
  → stream to user (~700ms total)
  → audit: tier=1, result=ok
  → publish glados/events/intent
```

### Example B1 — generic group, scope-broadening rule

```
User (webui_chat): "turn off the bedroom lights"
  → WS conversation/process → error: multiple targets match
  → Tier 2 invoked
  → candidates in area=bedroom, domain=light:
    light.room_a_ceiling, light.room_a_reading, light.room_a_closet
  → naming convention: "lights" (plural, generic) = all light-type
    fixtures in the area → turn off all three
  → resolve_and_execute(
      entity_ids=[light.room_a_ceiling, light.room_a_reading,
                  light.room_a_closet],
      service=turn_off)
  → persona: "Bedroom illumination, all three sources, terminated."
  → audit: tier=2, candidates=3, chosen=3
```

### Example B2 — specific term overrides the group

```
User (webui_chat): "turn off the bedroom overhead"
  → Tier 2 invoked
  → "overhead" = ceiling fixture only
  → resolve_and_execute(
      entity_ids=[light.room_a_ceiling], service=turn_off)
  → persona: "Bedroom ceiling fixture extinguished. The remaining lamps
    continue their vigil."
  → audit: tier=2, candidates=1, chosen=1
```

### Example B3 — state-based inference (no area specified)

```
User (webui_chat): "turn off the lights"
  → Tier 2 invoked
  → cache state filter: currently on → only light.kitchen_ceiling
  → exactly one coherent candidate after state filter → no clarification
  → resolve_and_execute(
      entity_ids=[light.kitchen_ceiling], service=turn_off)
  → persona: "Kitchen overhead deactivated. It was the only illumination
    still pretending to be useful."
  → audit: tier=2, candidates=1, chosen=1, rule=state_inference
```

### Example C — NodeRed trigger

```
NodeRed publishes to glados/cmd/intent:
  {"utterance": "announce dinner is ready"}
  → source_tag = mqtt_cmd
  → Tier 1: no intent match (not a device command)
  → Tier 3: full LLM path with tools
  → LLM uses TTS tool; persona says "Dinner, presumably edible, is
    ready. Attendance is advised."
  → publish glados/events/spoke
```

### Example D — sensitive domain blocked

```
User (api_chat from unknown session): "unlock the front door"
  → source_tag = api_chat, session = none
  → If LAN allowlist matches: continue; else 401
  → Tier 1: WS conversation/process → resolves lock.entry_door
  → allowlist(api_chat, lock) = deny
  → persona refusal: "I have an astonishing array of methods to decline
    that request. This is one of them."
  → audit: tier=1, allowlist_decision=deny, result=refused
```

---

## Phases

### Phase 0 — Auth, source-tagging, audit (small, prerequisite)

1. Add source-tag field to engine input items; plumb through
   `llm_queue_priority`, `ToolExecutor`, `LLMProcessor`.
2. Session-cookie check on `_handle_chat_completions` at
   `api_wrapper.py:2163`; LAN CIDR allowlist fallback.
3. `glados/observability/audit.py` — JSON-lines writer + WebUI viewer tab.
4. Plumb `source` and `tier` (initially just `tier=3` for everything)
   through to audit log end-to-end.

**Success criteria:**
- Every action written to `audit.jsonl` has a non-null `source` tag.
- Anonymous `/api/chat` request from outside LAN allowlist gets 401.
- WebUI Activity tab renders at least the last 100 audit entries.

### Phase 1 — HA WebSocket + Conversation Bridge + Disambiguator

1. `ha/ws_client.py` + `EntityCache` + reconnect/resync.
2. `ha/conversation.py` wrapper.
3. `persona/rewriter.py`.
4. Fast-path intercept: in `_handle_chat_completions` at
   `api_wrapper.py:2163`, before calling `_stream_chat_sse`, try Tier 1
   via `conversation.process_utterance(text, source_tag)`. On win: stream
   rewriter output as if it were the LLM response. On miss: current path.
5. `intent/disambiguator.py` — invoked on Tier 1 miss instead of jumping
   straight to Tier 3. Constrains tool set to `resolve_and_execute`.
6. `intent/allowlist.py` + enforcement at `ToolExecutor` and
   `resolve_and_execute`.
7. Extract the fuzzy logic currently at `api_wrapper.py:1265-1308` into
   `EntityCache.get_candidates` so there is one place for per-domain
   thresholds and scorer choice (`fuzz.WRatio`, cutoff 75 for safe
   domains, exact-match-only for sensitive).

**Success criteria:**
- "Turn off the kitchen lights" → p95 < 1000ms end-to-end (measured).
- "Turn off the bedroom lights" with 3 candidates → conversational
  clarification or sensible default + narration; never a raw
  "no device found" error.
- Lock/alarm domains: reachable only from `webui_chat` source, and
  persona-refused via disambiguator for other sources.
- HA offline: container logs degraded mode; Tier 3 still works through
  Ollama alone for non-device conversation.

### Phase 2 — MQTT peer bus

1. `mqtt/peer_bus.py` with `aiomqtt` v3, TLS + optional mTLS.
2. Subscriber side: `glados/cmd/*` topics feed engine input queues with
   `source=mqtt_cmd`.
3. Publisher side: hook into utterance start, utterance end,
   intent execution, state transitions.
4. `configs/mqtt_actions.yaml` schema for named actions.
5. `docs/mosquitto-acl.example` + NodeRed integration guide.
6. WebUI MQTT config pane (broker URL, topic prefix, TLS toggle). No
   password field in UI — `.env` only.

**Success criteria:**
- NodeRed publishes `glados/cmd/say` → GLaDOS speaks it within 2s.
- NodeRed subscribes `glados/events/intent` → receives event within
  500ms of execution.
- Mosquitto ACL prevents the bridge user from reading
  `homeassistant/#` or publishing to topics outside `glados/events/#`.

### Phase 3 — Tests + hardening

1. `tests/intent/` — corpus of ~50 labeled real utterances with expected
   entity IDs / tiers. Tier-1 hit rate + Tier-2 clarification quality
   measured.
2. WS reconnect integration test (mocked HA; kill + restart).
3. MQTT peer bus round-trip test (mosquitto-in-docker for CI).
4. Audit log schema test.
5. Second-factor design sketch for sensitive domains (spoken PIN or
   mobile confirm). **Design only — implementation is Stage 4+.**

---

## Open Questions (explicit — resolve during Phase 1)

1. Does WS `conversation/process` respond fast enough that the network
   round-trip is negligible vs local hassil? If not (>100ms), reconsider
   the local classifier — but only after measuring.
2. Should disambiguator run on autonomy model (T4, fast) or interactive
   (B60, higher quality)? Benchmark both with the Phase 3 test corpus.
3. HA long-lived token scope: worth generating a separate
   reduced-domain token for the conversation bridge, so a container
   compromise can't reach `lock`/`alarm` even if the allowlist is
   bypassed? (Defense in depth.)
4. Does HA's conversation API support streaming responses or only
   whole-utterance? If only whole-utterance, persona rewrite + TTS
   sequencing needs careful buffering.

---

## What this plan explicitly does NOT do

- **No voice-print speaker ID for sensitive commands.** Research shows
  voiceprint is a convenience signal, not an auth factor. Locks/alarms
  need a real second factor, designed in Phase 3, implemented Stage 4+.
- **No custom hassil sentences in the repo.** User chose delegation to
  HA's conversation API; custom phrasings for "initiate test chamber"
  style Portal vocabulary live in HA config (or become a future item).
- **No bypass of HA's state machine.** All device commands go through
  `call_service`, never directly to a device MQTT topic, so HA's
  automations stay consistent with what GLaDOS did.
- **No sharing of MQTT credentials between HA and GLaDOS.** GLaDOS has
  its own broker user with ACL restricted to `glados/*`; HA keeps its
  existing broker access; the two coexist without seeing each other's
  topics.

---

## References

- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)
- [Home Assistant Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/)
- [aiomqtt v3](https://github.com/empicano/aiomqtt)
- [Mosquitto ACL configuration](https://mosquitto.org/man/mosquitto-conf-5.html)
- [OWASP API Security Top 10 (2023)](https://owasp.org/API-Security/editions/2023/en/0x11-t10/)
- [OWASP IoT Top 10](https://owasp.org/www-project-internet-of-things/)
- Adversarial review notes from 2026-04-17 session (security, architecture,
  research, codebase) — on file in session history.
