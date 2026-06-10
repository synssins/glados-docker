# Event→Action Engine Design ("she acts")

**Date:** 2026-06-09
**Status:** Approved by operator (brainstorm 2026-06-09); awaiting implementation plan
**Builds on:** `2026-05-05-camera-vision-design.md` Feature B substrate (HAWebSocketHub + EventRouter, planned in camera-vision slice 3, unbuilt as of this writing)

---

## 1. Overview and scope

Today the autonomy subsystem can only `speak` or `do_nothing`. This design
gives GLaDOS an **action channel**: a Home Assistant event fires (e.g. a
camera detects a person in a dark hallway), GLaDOS decides whether to act,
and if so she triggers a Home Assistant automation (e.g. the hallway light
turns on).

### The boundary (operator-set, load-bearing)

**HA executes. GLaDOS decides.**

- What an action *does* lives in Home Assistant — automations, scripts,
  scenes. HA is built for execution; we do not rebuild that machinery here.
- GLaDOS owns *whether and when*. Each rule whitelists exactly one HA
  target. The decision layer can fire it or decline it — it can never pick
  a different target, entity, or service. The blast radius of the whole
  system is the operator-authored rule list, nothing more.

### In scope (this slice)

1. **Event substrate** — `EventRouter` (rule matching, gating,
   dispatch) consuming the EXISTING `HAClient` singleton
   (`glados/ha/ws_client.py`, stood up by `server.py:_init_ha_client`)
   via its public `on_state_changed()` fan-out API. No new WebSocket
   connection; no hub extraction needed — the hub already exists.
2. **`ha_action` action kind** — trigger an HA automation / script /
   scene / plain service call, gated by a per-rule decision mode
   (`always` or `llm`).
3. **Full WebUI rule editor from day one** — admin-only
   Integrations → Events page: rule CRUD with live entity / target
   pickers, enable toggles, last-decision status, test-fire.
4. **Visibility** — every fire / decline / failure audited and surfaced.

### Out of scope (deferred, same router later)

- **Announce-side actions** (`audio_random`, `vision_cascade` — stall
  clips, snapshot+VLM, persona continuation). Camera-vision slice 3
  retargets onto this router as a follow-up slice.
- **MQTT peer bus** (Stage 3 Phase 2) — still future.
- **Autonomy-tick integration** — the autonomy LLM loop is untouched;
  this engine is event-driven, not tick-driven.
- **GLaDOS proposing schedule/timing refinements to HA automations**
  (operator-flagged interest) — Phase 2 hook, see §9.

### Hard constraints (house rules this design must honor)

- No unattended physical actions outside the operator-authored whitelist.
  New rules are created **disabled**; the operator flips them on.
- No silent fallback: every decline, timeout, parse failure, and HA call
  failure is logged loudly and visible in the WebUI.
- Single source of truth: rules live in `configs/events.yaml` and nowhere
  else; the WebUI is an editor of that file.
- No secrets / real entity names in committed code or docs — example
  rules in the repo use placeholder entity ids; the operator's real
  rules live only in the runtime config volume.
- LLM wire format is OpenAI chat-completions end-to-end (lesson from the
  2026-06-09 doorbell fix — no Ollama shapes against OpenAI endpoints).

---

## 2. Architecture and data flow

```
HA state_changed (existing HAClient WebSocket, glados.ha.get_client())
        │ on_state_changed() fan-out
        └─→ EventRouter   (ha_sensor_watcher keeps its own connection,
                           untouched this slice — consolidation is a
                           Phase 2 hook)
                │ match: entity_id + to_state (+ optional from_state)
                │ gate:  master switch → maintenance mode → rule.enabled
                │        → cooldown_s → min_clear_s
                ▼
        mode: always ──────────────────────────────┐
        mode: llm                                  │
                │ gather context:                  │
                │   rule.context_entities states   │
                │   + local time + trigger detail  │
                │ llm_triage slot, JSON verdict    │
                │   {act, reason, quip?}           │
                │ timeout/garble → act=false (safe)│
                ▼                                  ▼
        act=false → audit "declined: <reason>"   fire ha_action
                                                   │ HA REST service call
                                                   │ (automation.trigger /
                                                   │  script.turn_on /
                                                   │  scene.turn_on /
                                                   │  <domain>.<service>)
                                                   ▼
                                         audit Origin.EVENT_RULE
                                         + optional announce (per-rule,
                                           default silent): quip → TTS →
                                           rule's announce_speaker
```

Failure at any stage → `logger.error` + rule status surfaced in WebUI.
A failed HA call never retries silently; one retry with backoff, then
loud failure.

### Decision latency budget

Trigger → action ≤ 2 s for `mode: llm` (context reads ~0.3 s parallel,
triage-lane LLM ≤ 1 s warm, HA call ~0.2 s). `mode: always` ≤ 0.5 s.

---

## 3. Components

### New modules

| Path | Purpose |
|---|---|
| `glados/events/config.py` | Pydantic schema for `events.yaml` (see §4) + load/save helpers following the existing config-store atomic-write pattern. |
| `glados/events/router.py` | Rule matching, gate chain, dispatch, per-rule runtime state (last_fired, last_decision, last_error). |
| `glados/events/decision.py` | `mode: llm` verdict call — context gathering + OpenAI-format request on the `llm_triage` slot + strict JSON parse + fail-safe defaults. |
| `glados/events/actions/ha_action.py` | Maps rule.action to the HA REST service call; one retry; loud failure. |

### Modified existing code

| Path | Change |
|---|---|
| `glados/server.py` | `_init_event_router()` after `_init_ha_client()`: stand up the router on the existing HAClient singleton; if HA is unconfigured (`get_client()` is None) the engine logs loudly and stays off. |
| `glados/observability/audit.py` | Add `Origin.EVENT_RULE`. |
| `glados/webui/*` | New Integrations → Events page + REST endpoints (§6). |
| `configs/events.example.yaml` | Committed example with placeholder entity ids; runtime `events.yaml` lives on the config volume. |

### Decision LLM slot

`llm_triage` (the fast lane — WebUI services tab governs URL/model, same
inheritance pattern as the persona rewriter). Request: OpenAI
chat-completions, `temperature 0.1`, `max_tokens 128`, 5 s timeout,
`/no_think` via `apply_model_family_directives`. Response must be a JSON
object `{"act": bool, "reason": str, "quip": str|""}`. Anything else —
timeout, non-JSON, missing fields — is a **no-act** with a WARNING and a
`last_decision: error` in the rule status. Acting is never the failure
default.

---

## 4. Rule schema (`events.yaml`)

```yaml
# configs/events.yaml — operator-authored; edited via WebUI or by hand.
enabled: true            # master switch for the whole engine
rules:
  - id: hallway_dark_person          # slug, unique
    enabled: false                   # ALWAYS created disabled
    trigger:
      entity_id: binary_sensor.hallway_person_detected
      to_state: "on"
      from_state: null               # optional narrowing
    mode: llm                        # always | llm
    context_entities:                # mode: llm only — states injected
      - sensor.hallway_lux           #   into the decision prompt
      - sun.sun
    decision_prompt: >               # mode: llm only — the question
      Turn on the hallway light only if the hallway is dark.
    action:
      kind: ha_automation            # ha_automation | ha_script |
      target: automation.hallway_light_on   # ha_scene | ha_service
      # ha_service form:
      #   kind: ha_service
      #   target: light.turn_on
      #   entity_id: light.hallway
      #   data: {brightness_pct: 40}
    cooldown_s: 120                  # min seconds between fires
    min_clear_s: 30                  # trigger must have been clear this long
    announce: false                  # default silent
    announce_speaker: null           # media_player entity when announce: true
    announce_text: null              # spoken line for mode: always rules
                                     # (mode: llm rules speak the verdict quip)
```

Validation rules: unique ids; `mode: llm` requires `decision_prompt`;
`announce: true` requires `announce_speaker`, and additionally
`announce_text` when `mode: always`; unknown keys rejected;
malformed file → engine disabled + loud boot ERROR, container otherwise
healthy (the events engine failing must never take down chat/TTS).

### Gate semantics

Evaluated in order: master `enabled` → maintenance/silent mode (existing
`mode_entities` machinery) → `rule.enabled` → `cooldown_s` since last
fire **or decline** (declines consume cooldown too — prevents LLM-call
flapping) → `min_clear_s` (trigger entity continuously in a non-trigger
state for this long before the firing transition).

---

## 5. Announce path

When `announce: true` and the action fired: speak the decision `quip`
(mode `llm`) or a per-rule static `announce_text` fallback (mode
`always`) through the existing TTS path to `announce_speaker`, reusing
the media-player play pattern already proven in `ha_sensor_watcher`.
Announce failures are WARNINGs — they never roll back or block the
action itself.

---

## 6. WebUI — Integrations → Events (admin-only)

Follows the established page conventions (`.page-shell > .container >
.page-header`, cards, `.page-tabs`) and the design-system v3 utility
classes.

**Rule list card:** one row per rule — enable toggle, id, trigger
summary, action target, mode badge, last activity ("fired 2h ago — lux
was 4", "declined 10m ago — already bright", "error: HA call 500"),
edit / delete / test buttons.

**Editor (create + edit):**
- Trigger entity picker fed by the live HA entity cache (the same cache
  `ha_sensor_watcher` maintains), with `to_state` input.
- Context entity multi-picker (same source).
- Action target dropdown populated from HA's actual
  `automation.* / script.* / scene.*` entities; `ha_service` mode exposes
  domain/service/entity/data fields for advanced use.
- Mode select, decision prompt textarea (`llm` only), cooldown,
  min-clear, announce toggle + speaker picker (media_player entities).
- Save writes `events.yaml` atomically and hot-reloads the router.

**Test controls per rule (admin):**
- **Dry-run** — runs context gathering + decision, shows the verdict and
  reason, does NOT call HA.
- **Fire** — runs the full pipeline for real, bypassing cooldown
  (operator-initiated, so the unattended-action rule is satisfied).

**REST (all admin-gated, 401/403 per existing RBAC pattern):**
`GET/POST /api/integrations/events`, `PUT/DELETE
/api/integrations/events/<id>`, `GET /api/integrations/events/targets`
(automations/scripts/scenes/media_players for the pickers),
`POST /api/integrations/events/<id>/dry_run`,
`POST /api/integrations/events/<id>/fire`,
plus master-switch toggle.

---

## 7. Error handling

| Failure | Behavior |
|---|---|
| HA WS drops | The existing HAClient reconnect supervisor (exponential backoff, `ws_client.py`) recovers; the router just sees an event gap. Reconnect logged. |
| Context entity unreadable | Noted as `unavailable` in the decision prompt; not fatal — the LLM decides with what it has. |
| Decision LLM timeout / bad JSON | No act. WARNING log, `last_decision: error` in UI. |
| HA service call fails | One retry with short backoff, then ERROR log + rule status `error`. Never silent. |
| `events.yaml` malformed | Engine disabled, boot ERROR, rest of container unaffected. |
| Announce fails | WARNING only; action result stands. |

Log levels chosen for docker-log visibility (SUCCESS-and-above sink
convention): normal fires log at SUCCESS, declines at SUCCESS (they are
decisions, not noise), failures at ERROR.

---

## 8. Testing

TDD throughout (failing test first, per repo standard):

- Schema: valid/invalid rule fixtures, unique-id, mode-conditional
  required fields, unknown-key rejection, malformed-file behavior.
- Router: match/no-match, gate chain order, cooldown consumed by
  declines, min_clear_s edge cases, master switch, maintenance mode.
- Decision: OpenAI request shape (regression guard from the doorbell
  bug), verdict parse, timeout → no-act, garbage → no-act, context
  injection including unavailable entities.
- ha_action: each kind maps to the right HA service call (mocked HA),
  retry-then-error path.
- WebUI REST: CRUD round-trip onto a temp events.yaml, RBAC (401/403),
  targets endpoint filtering, dry-run vs fire semantics.

Success criteria: full suite green; live verification = operator creates
the hallway rule via the WebUI, dry-run shows a sane verdict both in
dark and bright conditions, then a real walk-through fires the light.

---

## 9. Phase 2 hooks

- **Announce-side actions** — `audio_random` + `vision_cascade` from
  camera-vision slice 3 register as additional action kinds on this
  router; the stall-clip + VLM cascade design carries over unchanged.
- **Watcher consolidation** — migrate `ha_sensor_watcher` off its
  private WebSocket onto the shared `HAClient`, then fold its rule
  logic into the router (camera-vision spec Note A Phase 2). Until
  then the container runs two HA WS connections, as it already does
  today (HAClient + watcher).
- **MQTT triggers** — `source: mqtt` rule type when Stage 3 Phase 2 lands.
- **Schedule refinement** — GLaDOS observing patterns and *proposing*
  timing changes to HA automations (operator approves in WebUI before
  anything changes). Operator-flagged interest 2026-06-09; needs its own
  brainstorm.
- **Two-way audio satellites** — unaffected by this slice; the modular
  router keeps the audio path swappable for the conversational future.
