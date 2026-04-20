# GLaDOS Test-Battery — Findings & Remediation Plan

**Date:** 2026-04-19 overnight session; revised 2026-04-20 after operator feedback.
**Trigger:** 435-utterance live battery against `a13e9bd` on 10.0.0.50. Pass rate 55.9% (243 PASS / 154 FAIL / 38 QUERY_OK), with systemic failures concentrated entirely on Tier 2.
**Status:** plan approved in principle; awaiting final sign-off on this revision.

> **Non-negotiable outcome:** natural-language home control must work reliably on the operator's actual house. "Works on the demo phrase" is not success.

---

## 0. Cross-cutting project requirements

These apply to every phase below. Any solution that violates one is rejected.

1. **Standards compliance.** The container speaks OpenAI-compatible Chat Completions to upstream Ollama. No modifications to Ollama, HA, Speaches, or Chroma to make them "GLaDOS-compliant." No bespoke transports. The container points at a URL; whether that URL serves from a B60, a T4, another host's Ollama, or any other OpenAI-compatible endpoint is not the container's concern.
2. **WebUI-managed configuration.** Every operator-facing setting surfaces as a friendly control in the WebUI. YAML files are internal plumbing. Any new config introduced by a phase ships with its UI card on the same PR — never "TODO later."
3. **Benchmark parity.** Every phase re-runs `home-assistant-datasets` (the public HA voice-agent benchmark) alongside the private 435-test battery. Shared-vocabulary scoring with the rest of the field.
4. **No GPU dependency inside the container.** The container itself runs on CPU. LLM inference is external — any GPU acceleration happens in whatever serves Ollama.
5. **Silent-by-default device commands.** The operator controls verbal response per install; default is "quip" (short scripted GLaDOS line). Verbal LLM response is opt-in.

---

## 1. What we ran

A 515-utterance battery (filtered to 435 after blocking bedroom / guest / closet / office ceiling flood / living room arc lamp / dining room / desk lamp) hit the production `/v1/chat/completions` endpoint. Each test captured HA pre-state, GLaDOS verbal response, HA post-state, and the matching `audit.jsonl` row. Scoring: PASS if expected entities reached the expected state; QUERY_OK if a state query returned a substantive non-empty response; FAIL otherwise.

**Artifacts**
- `C:\src\glados-test-battery\glados_test_battery_FINAL.xlsx` — 435 rows (Tests + Summary sheets).
- `C:\src\glados-test-battery\results.json` — raw per-row data (harness-replayable).
- `C:\src\glados-test-battery\inventory.json` — active-entity snapshot.
- `C:\src\glados-test-battery\harness.py`, `generate_tests.py`, `export_xlsx.py` — reproducible rig.

---

## 2. Top-line numbers

| Disposition | n | % |
|---|---:|---:|
| PASS | 243 | 55.9% |
| FAIL | 154 | 35.4% |
| QUERY_OK | 38 | 8.7% |

**FAILs by category × tier (every FAIL is a Tier-2 or chitchat FAIL — Tier 1 has zero scored FAILs, though 58% of its "OK"s produced no state change):**

| Category | tier2 | chitchat |
|---|---:|---:|
| light_dim | 70 | 0 |
| light_onoff | 32 | 0 |
| area_zone | 16 | 0 |
| ambient | 14 | 0 |
| light_color | 11 | 0 |
| compound | 9 | 0 |
| state_query | 0 | 2 |

---

## 3. Findings (condensed; full agent reports preserved in prior session transcript)

1. **Switch/light twin phantom ambiguity (~55 FAILs).** Zooz/Inovelli dimmers expose both `light.foo` and `switch.foo` for the same physical fixture. The entity cache presents them as rival candidates. Tier 2 dutifully asks "X or X?"
2. **Precheck vocabulary gap (62 FAILs).** `looks_like_home_command` only recognises "turn/switch" verbs. `Darken`, `Bump`, `Soften`, `Lower`, `Raise`, `Reduce`, and every ambient phrasing ("it's too dark," "I want to read") fall into chitchat and silently do nothing.
3. **No area/floor/zone taxonomy (16 FAILs).** "Downstairs," "main floor," "first floor," "basement," "kitchen area," "hallways" — none resolve.
4. **Tier-1 silent-success on 58% of accepted calls.** HA's conversation API returns `action_done` while doing nothing or touching the wrong entity. The harness currently scores these as PASS via the "tier acked ok" fallback rationale — our real success rate is lower than the table above.
5. **3B persona rewriter is a liability.** Language drift (German, Thai, Chinese), cross-turn contamination (identical response to different utterances), verb-polarity flips ("I'll turn ON" said while turning off), occasional empty output.
6. **Background-noise pollution in scoring.** `switch.hvac_one_display` flips on 61/435 rows from its own heartbeat. 33 of those were scored PASS only because something changed.
7. **Compound-command dropout.** One-shot JSON over a 3000-token candidate list loses an action often enough to matter.
8. **14B disambiguator is the wrong size for this job.** SOTA for HA-voice-intent is 4–8B with native tool calling; published benchmarks have Qwen3-8B at 82.8% on `home-assistant-datasets` "assist", beating local 14B variants.

---

## 4. Unified diagnosis (one sentence each)

- Candidate list is garbage before Qwen sees it.
- Precheck gate is too narrow.
- HA Tier-1 `ok` is not ground truth — needs state verification.
- 3B rewriter free-text path is unsalvageable.
- No area / floor / device grouping.
- Compound commands lose actions.
- No post-execute state verification.
- 14B disambiguator is the wrong tool; Qwen3-8B does both roles better.

---

## 5. Target architecture

```
 utterance ─▶ Precheck / Intent Router  ──── rule table + optional DistilBERT
              (home_cmd | state_q | chat)
                     │                 │                  │
                     ▼                 ▼                  ▼
           ┌──────────────────┐ ┌──────────┐   ┌──────────────┐
           │ Candidate        │ │ HA state │   │ Open chat    │
           │ Retriever        │ │ lookup   │   │ (Qwen3-8B)   │
           │ (BGE-small ONNX  │ └──────────┘   └──────────────┘
           │  + dedup + area/ │
           │  floor tags)     │
           │  also exposed as │
           │  MCP tools       │
           └────────┬─────────┘
                    │ top-k=8 entities (~400 tokens)
                    ▼
           ┌─────────────────────────────┐
           │ Planner (Qwen3-8B, native   │
           │  Hermes tool-calling)        │
           │  • dialog-state JSON input   │
           │  • emits actions[] list      │
           └────────┬────────────────────┘
                    │
                    ▼
           ┌─────────────────────────────┐
           │ Executor (deterministic)    │
           │  • action → call_service     │
           │  • post-execute state verify │
           └────────┬────────────────────┘
                    │ verified outcomes
                    ▼
           ┌─────────────────────────────┐
           │ Response Composer           │
           │  • mode = silent | chime |  │
           │           quip | LLM        │
           │    per-event or global      │
           │  • quip library keyed on    │
           │    (intent, outcome, mood,  │
           │     tod, entity_count)      │
           │  • LLM mode reuses Qwen3-8B │
           └─────────────────────────────┘
```

Container runs on CPU. Qwen3-8B runs on whatever host the operator points the `completion_url` at (currently an AIBox GPU; eventually the operator's choice).

---

## 6. Phased plan

Each phase re-runs the 435-test battery AND `home-assistant-datasets` before merging. Phase gate: PASS rate improves ≥10 percentage points over the prior run on whichever benchmark the phase targets, OR the phase is reverted and re-planned.

### Phase 8.0 — Unified Qwen3-8B via external Ollama (1 day)

**Problem fixed:** two-model plumbing; 3B rewriter drift; 14B disambiguator latency; Ollama-side grammar flakiness (bypassed by native tool-calling).

**Work (container-side only):**
- Delete the 3B rewriter code path (`glados/persona/rewriter.py` callers removed from Tier 1, Tier 2, and chat SSE paths; file retained for legacy test coverage until Phase 8.8 lands).
- Remove the "persona rewriter URL" env/config plumbing.
- Wire Qwen3-native Hermes tool-calling: disambiguator prompt reshaped to use the model's built-in `<tools>` block, expecting `<tool_call>{...}</tool_call>` output.
- Keep the `a13e9bd` tolerant-parse layer as a safety net (Qwen3's JSON is near-100% valid, but "near" is not "always").
- WebUI LLM & Services page (already live) is the sole operator-facing control for model + URL. No code change needed there.

**Operator-side (documented, not a container task):** in WebUI, change the Ollama model selection to `qwen3:8b-instruct-q4_K_M`. The container picks it up.

**Success:** end-to-end latency for simple device commands drops from 15–45 s to under 5 s. Language drift in verbal responses drops to near zero. Re-runs of the battery and `home-assistant-datasets` produce the new baseline.

---

### Phase 8.1 — Candidate dedup + anti-match (1 day, P0)

**Problem fixed:** Cluster A (~55 FAILs from light/switch twin phantom ambiguity); opposing-token confusion ("lower" vs "upstairs").

**Work:**
- Extend `CandidateMatch` in `glados/ha/entity_cache.py` with `device_id` pulled from HA's `device_registry`.
- Dedup pass: when two candidates share `device_id` and one is `light.*` + the other `switch.*`, keep the light domain (or whichever has non-trivial `supported_color_modes`). Mark the hidden twin with `alias_of=<kept_id>`.
- Opposing-token penalty: `{upstairs↔downstairs, lower↔upper, front↔back, inside↔outside, master↔guest}`. If the utterance contains one side and a candidate's name contains the other, apply −50 score.
- WebUI Entities page (new card) shows the dedup + anti-match registry, editable — operator can add house-specific opposing tokens without touching code.

**Success:** ≥40 of the Cluster-A FAILs flip to PASS or to a *correct* clarify without the twin listed.

---

### Phase 8.2 — Precheck verb/phrase expansion (half day, P0)

**Problem fixed:** Cluster B (62 `fall_through:no_home_command_intent` FAILs).

**Work:**
- Expand the verb set in `glados/core/api_wrapper.py::looks_like_home_command`: `darken, brighten, dim, lighten, bump, lower, raise, reduce, increase, soften, tone, crank, kill, douse, extinguish, illuminate, light, set, put, dial, slide, push, pull, close, open, shut, drop`.
- Add state-description patterns: `(it's|i'm|the X is) (too )?(dark|bright|dim|hot|cold)`, `i (can't|need|want) …`, `time to …`, `… mode in …`, `(movie|reading|dinner|party) (time|mode)`.
- WebUI Personality page (existing) gains a new "Command recognition" card — operator-editable list of verbs + phrase patterns. Test input box at the bottom to preview whether a given utterance would be recognised.

**Success:** Cluster B FAILs drop from 62 to <10.

---

### Phase 8.3 — Semantic retrieval + MCP-style entity tools (3 days, P0 — biggest single win + no-GPU precondition)

**Problem fixed:** the root cause of most remaining FAILs — the candidate list is wrong before the planner sees it.

**Work:**
- Add onnxruntime-cpu + BGE-small-en-v1.5 ONNX to the container (cold start: ~200 MB image delta, ~2 s one-time index build for ~3500 entities, ~15–30 ms per query).
- New `glados/ha/semantic_index.py`:
  - Entity document shape: `"{friendly_name} | area={area_name} | floor={floor_name} | domain={domain} | device_class={device_class} | device_name={device_name}"`.
  - Embed on startup + on HA cache resync; persist `/app/data/entity_embeddings.npy` for warm restarts.
  - `retrieve(utterance, k=8, filter=None)` returns top-k candidates with cosine scores.
- Disambiguator (now planner in §8.7) consumes top-8 instead of the coverage-ranked full list.
- **MCP tool exposure:** `search_entities(query, top_k)` and `get_entity_details(entity_id)` are registered as MCP tools the planner can call when top-8 isn't enough. Implements the mcp-assist pattern.
- WebUI Integrations → Home Assistant page gains a "Candidate retrieval" card — operator can re-embed the index on demand and see the per-entity document text (for debugging why a given entity does or doesn't match).

**Why this is also the no-GPU precondition:** shrinking the planner prompt from ~3000 tokens to ~400 tokens converts the CPU-only latency budget from "unusable" to "feasible." Enables Phase 9 later.

**Success:** Tier-2 over-asking rate (previously 18%) drops to ≤8%. Sanity check: "kitchen" returns kitchen lights/switches in top 8 with no `switch.midea_ac_...display` pollution.

---

### Phase 8.4 — Post-execute state verification (2 days, P1 — fixes the biggest lie)

**Problem fixed:** 58% Tier-1 silent-success rate. The worst quality issue in the current stack.

**Work:**
- In `glados/ha/ws_client.py`, assign a correlation id per `call_service` dispatch.
- After dispatch, subscribe to `state_changed` for targeted `entity_ids`; wait up to 3 s for the expected transition.
- On no-transition: audit row gets `state_verified=false`, composer path is told "no change detected," and the verbal response (if any) reflects that honestly ("I tried, but the kitchen overhead didn't actually change. Retry?").
- New audit field `state_verified: true | false | timeout`, visible in the WebUI Audit Log view.
- WebUI Personality → Response behavior card (added in §8.8) gains a "verification mode" setting: `strict` (fail on no-transition) | `warn` (log, but tell user success) | `silent` (current behavior). Default `strict`.

**Success:** on the next battery run, rows with `state_verified=false` appear. PASS rate drops transparently (honest), but the "HA lied" failure mode becomes visible and addressable.

---

### Phase 8.5 — Area & floor taxonomy (1 day, P2)

**Problem fixed:** Cluster C (16 area_zone FAILs).

**Work:**
- Pull HA `config/area_registry/list` and `config/floor_registry/list` via WS on boot.
- Tag each entity document in the semantic index with `area_id` and `floor_id`.
- `retrieve()` accepts optional `area_id` / `floor_id` filter hints.
- Utterance → area/floor inference: `{downstairs, main floor, first floor, ground floor} → floor_id=1`; `{upstairs, second floor, top floor} → floor_id=2`; `{outside, outdoor, yard} → area ∈ Outside set`; etc. Mapping is editable in the WebUI (HA Integration page, new "Area / floor aliases" card).

**Success:** Cluster C FAILs drop from 16 to <4.

---

### Phase 8.6 — Planner / Executor split (4 days, P2)

**Problem fixed:** Cluster D compound-command dropout.

**Work:**
- Rename `disambiguator.py` → `planner.py`. Planner emits Hermes-tool-call list with `len(actions) ≥ 1`.
- New `executor.py`: iterates actions, calls HA per-action, accumulates per-entity verification outcomes from §8.4.
- Composer consumes the aggregated outcome list (see §8.8).
- Response-side: one reply summarises all actions ("Turned off the kitchen, dimmed the living room — the dining room light was already off").

**Success:** ≥50% of the 9 compound FAILs convert to PASS. No regressions in single-action categories.

---

### Phase 8.7 — Response composer + audio controls WebUI + quip library (5 days, P2 — bundled)

**Problem fixed:** cross-turn contamination (Cluster G), language drift, verb-polarity flips, operator has no control over when GLaDOS talks.

#### 8.7a — Audio response controls (WebUI)

- New card on Configuration → Audio & Speakers: **"Response behavior"**.
- **Standard view:** one global dropdown `{ silent, chime, quip, LLM }`. Default = `quip`.
  - When `chime` selected: chime file picker appears below. Link to chime library.
- **Advanced toggle:** reveals a per-event-category matrix:
  - event categories: `command_ack`, `query_answer`, `ambient_cue`, `error`.
  - mode per row: `silent | chime | quip | LLM`.
- **Chime library card:** separate card below. Upload MP3/WAV, list uploaded files, ▶ play-test per row, delete button. Stored in `/app/data/chimes/`.
- "Verification mode" dropdown from §8.4 lives here too.

#### 8.7b — Quip library

Directory layout under `configs/quips/` (bind-mounted so the operator can edit directly OR via the WebUI editor in 8.7c):

```
configs/quips/
├── command_ack/
│   ├── turn_on/
│   │   ├── normal.txt
│   │   ├── cranky.txt
│   │   ├── amused.txt
│   │   └── evening.txt
│   ├── turn_off/
│   ├── brightness_up/
│   ├── brightness_down/
│   ├── color_change/
│   └── scene_activate/
├── query_answer/
│   ├── state_query/
│   ├── environmental/
│   ├── status/
│   └── time/
├── ambient_cue/
│   ├── too_dark/
│   ├── too_bright/
│   ├── reading/
│   ├── movie/
│   └── dinner/
├── outcome_modifier/
│   ├── partial_success.txt
│   ├── already_in_state.txt
│   ├── no_such_entity.txt
│   └── unavailable_entity.txt
└── global/
    ├── acknowledgement.txt
    └── void_references.txt
```

One line per quip. Blank lines ignored. Selector: `pick(event_category, intent, outcome, mood, time_of_day, entity_count)` walks from most-specific to most-general; chooses uniformly at random.

**Mood mapping** (reads the existing HEXACO + emotion vector):
- `anger > 0.6` → `cranky.txt`
- `joy > 0.6` → `amused.txt`
- else → `normal.txt`

Seed content: ~30 files × ~15 lines each = ~450 Portal-voice one-liners at launch. Grows organically. Marked **TODO (expand):** future phase to revisit automatic personality-response tuning against the affect vector.

**Never inject device names.** Strict rule. Selector's substitution allowed only for: count (`"all three"`), scene name (scene entities carry human labels), outcome modifier (`"already asleep"`). Device friendly-names are forbidden.

#### 8.7c — Quip editor (WebUI)

- New page: Configuration → Personality → **"Quip library"**.
- Tree view matching the folder layout. Click a file → in-browser textarea, one line per quip, save button persists to disk.
- "Add variant" button creates `cranky.txt` / `amused.txt` if missing.
- "Test" card: select an event category, intent, and outcome → composer runs a dry pick and shows the line it would emit (with current affect vector applied).

#### 8.7d — LLM-mode composer fallback

When response-mode = `LLM`, the composer prompts Qwen3-8B with:
- the event category and outcome
- the current affect vector
- a strict "never recite entity IDs or raw friendly names" system prompt
- max 40 tokens

Output grammar-constrained (Qwen3 native) to English only, no JSON wrapping.

**Success:** zero cross-turn contamination; zero non-English output; per-event controls take effect; operator-uploaded chime plays; quip editor round-trips edits to disk.

---

### Phase 8.8 — Dialog-state JSON & anaphora (2 days, P3)

**Problem fixed:** "turn it up a bit" following an earlier command; "do that again"; ambient cues that reference prior context.

**Work:**
- Extend `glados/core/session_memory.py` (already exists per SESSION_STATE) with a per-session state object: `{last_entities, last_area, last_service, last_delta, last_ts}`.
- Planner prompt prepends this state; the Hermes tool schema adds an optional `context_anchor` field.
- Anaphoric utterances ("it", "that", "those", "a bit more", "again") trigger carry-over if last turn was Tier-1 or planner `ok`.
- WebUI Personality page gains "Follow-up window" setting (default 10 min idle TTL).

**Success:** a new 30-pair anaphora subtest (Phase 8.10) passes ≥80%.

---

### Phase 8.9 — Test-harness hardening + CI wiring (ongoing, P2 in parallel)

**Problems fixed:** inflated PASS counts from background-entity noise; no regression safety net.

**Work:**
- Exclude known-noise entities (`midea_ac_*_display`, `sonos_*_*`, `wled_*_reverse`, any `_button_indication`, `_node_identify`) from the diff scorer by default. Editable list in a WebUI "Test harness" card (hidden under Advanced in the System page).
- Harness verifies state actually changed to *match* the expected direction, not just "changed."
- Adopt `home-assistant-datasets` as parallel benchmark (§0 requirement). Add the adapter layer that translates their YAML scenario format to our harness rows.
- Wire CI: a 30-test sanity subset runs on every PR; full battery + `home-assistant-datasets` runs nightly.

---

## 7. Phase ordering

```
Week 1  [8.0 Qwen3 swap]                                ─▶ baseline #1 (expected: 55% → 70%+)
Week 1  [8.1 dedup+anti-match]  [8.2 verb/phrase]       ─▶ baseline #2 (expected: → 78%)
Week 2  [8.3 retrieval + MCP tools]                     ─▶ baseline #3 (expected: → 85%)
Week 3  [8.4 state verify]     [8.5 area/floor]         ─▶ baseline #4 (expected: → 88%, honest)
Week 4  [8.6 planner/executor split]                    ─▶ baseline #5 (expected: → 90%)
Week 5  [8.7 composer + audio UI + quip library]        ─▶ baseline #6 (subjective quality target)
Week 5  [8.8 anaphora]                                  ─▶ anaphora subtest ≥80%
Parallel across all weeks: [8.9 harness + CI]
```

**Projected PASS-rate trajectory (after removing Midea false-positives in 8.9 — real numbers, not inflated):**

| After phase | Floor | Ceiling |
|---|---:|---:|
| Current (inflated baseline) | 55.9% | 55.9% |
| 8.0 alone (Qwen3-8B drop-in) | 65% | 75% |
| 8.1 + 8.2 | 75% | 82% |
| 8.3 | 82% | 88% |
| 8.4 + 8.5 | 85% | 91% |
| 8.6 + 8.7 | 88% | 93% |
| 8.8 + 8.9 | 90% | 95% |

Ceiling ~95% acknowledges that some physically-impossible requests (dimming a binary switch) can never score PASS. The 5% gap is correct behavior.

---

## 8. Phase 9 (optional, post-Phase 8) — CPU-only validation track

Not on the container's critical path. Runs against a separate Linux host with `ik_llama.cpp` + Qwen3-4B-Instruct Q4_K_M. Container URL repoints; no container-side code change (standards-compliance principle §0.1).

Goal: confirm the container delivers acceptable UX with the LLM backend on a modest x86 CPU. If it does, the "installable on any box" story is complete. If latency is too high, BitNet b1.58 or Qwen3 4B Q3 variants are the follow-up.

---

## 9. Success definition

A production-equivalent battery run must:

- ≥85% PASS rate with background-noise entities excluded and `state_verified=true` required for control PASS.
- Zero cross-turn contamination (50 random consecutive pairs, manual spot-check).
- Zero non-English output.
- Anaphora sub-battery ≥80%.
- `home-assistant-datasets` assist score within 5 points of Qwen3-8B's published 82.8%.
- Audio response controls behave as configured in the WebUI (chime plays, quip emits, silent stays silent, LLM stays on-persona).
- Operator subjective approval — the only non-numeric gate, held by the operator.

---

## 10. Related docs

- `docs/Stage 3.md` — original three-tier architecture plan.
- `docs/disambiguation-research.md` — prior-session theories; several are confirmed and several superseded by this plan.
- `docs/CHANGES.md` — each phase lands with a Change 14.N entry.
- `docs/roadmap.md` — Phase 8 supersedes the "Stage 3 Phase 7+ targets" section; updated on merge.
- `CLAUDE.md` — the five cross-cutting requirements in §0 become project-level guardrails.

---

*Author:* Claude Opus 4.7 (1M ctx) with operator (synssins); session of 2026-04-19/20.
*Status:* revised per operator feedback of 2026-04-20; awaiting final approval before implementation begins on Phase 8.0.
