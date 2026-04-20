# GLaDOS Test-Battery — Findings & Remediation Plan

**Date:** 2026-04-19 overnight session; revised 2026-04-20 after operator feedback; session-delivery log last updated 2026-04-20 late afternoon.
**Trigger:** 435-utterance live battery against `a13e9bd` on 10.0.0.50. Pass rate 55.9% (243 PASS / 154 FAIL / 38 QUERY_OK), with systemic failures concentrated entirely on Tier 2.
**Status:** Phase 8.0 delivered + infrastructure cleanups beyond original scope; Phase 8.1–8.9 queued.

---

## -1. Session delivery log (2026-04-20)

Work actually shipped in this session, in chronological order. Reference for Phase 8 status tracking + the commit trail.

### Infrastructure

- **Ollama-ipex full rip-and-reinstall on AIBox.** Old NSSM service removed, 16 GB install dir deleted, logs + env vars cleared. Fresh IPEX-LLM 2.3.0b20250725 nightly installed. B60 now stable (no repeat of the 50–90 s pathology) — operator's "it was cruft" call was correct.
- **Models on B60 (ollama-ipex, port 11434):** `qwen3:8b` (5.2 GB Q4_K_M), `qwen3:14b` (9.3 GB Q4_K_M), `qwen2.5vl:7b` (6.0 GB Q4_K_M). Production operator-selected model is `qwen3:14b` as of session end.
- **OpenWebUI deployed** as Docker container `open-webui` on port 3000 for out-of-GLaDOS manual testing.

### Container fixes (all pushed, built, deployed to 10.0.0.50)

| Commit | Subject |
|---|---|
| `a70889d` | fix(webui): `_send_error` returns JSON envelope so browser can surface pydantic validation detail |
| `03f3d02` | docs: Phase 8 battery-findings + remediation plan |
| `4d641c1` | feat(config): hot-reload engine on save so changes apply live |
| `58a2876` | fix(config): all LLM consumers honor live settings (model-name sync + frozen-at-import module constants replaced with live helpers) |
| `292e029` | fix(webui): hoist loguru import to module scope (validation errors were shadowed by NameError) |
| `5170cc1` | fix(webui): consistent "Changes saved." toast on live-apply paths |
| `1c409fc` | fix(chat): strip Qwen3 `<think>` reasoning from user-visible responses + single-element audio playback |
| `666549b` | fix(config): cross-process engine reload via `/api/reload-engine` (WebUI 8052 → api_wrapper 8015) |
| `4fb26b4` | fix(config): hot-reload releases HA audio_io port before rebuild |
| `95b85f1` | fix(api_wrapper): `main()` loops across hot-reloads instead of exiting |
| `4babbc0` | feat(config): every LLM consumer honors LLM & Services page — no hard-coded model names anywhere |
| `9b88f03` | fix(chat): `renderChat()` is incremental — audio element persists across content chunks |

### Engine-side changes (operator-editable, on-disk)

- **Production preprompt rewritten** (`/app/configs/glados_config.yaml`).
  - Added PRIME DIRECTIVE at top: never invent facts / narrate scenes / state current-tense activities for residents or pets.
  - Removed all 6 few-shot examples that were being copy-pasted verbatim by the model ("We have both said a lot of things you're going to regret" etc.).
  - Demoted household narrative: `Pet1 obsessed with oranges — peeling one summons him from anywhere. Infiltrates the cat room …` → `Pet1: likes oranges.`
  - Added explicit forbidden-phrase rule + "Use Aperture terminology as flavor, not factual location."
  - Old preprompt preserved at `glados_config.yaml.bak.20260420_preprompt`.

### What this means for the original Phase 8 plan

- **Phase 8.0 COMPLETE** (in a stronger form than originally scoped — did the model swap AND solved the entire live-config-apply infrastructure chain end-to-end, fixed a cascade of four related bugs that surfaced, and fixed the audio playback regression discovered in passing).
- **Phase 8.1 COMPLETE** (2026-04-20 late evening). Change 14.1 in `docs/CHANGES.md`. Twin dedup by HA device_id, 11-pair opposing-token penalty, operator-editable WebUI card under Integrations → Home Assistant with hot-reload via new `/api/reload-disambiguation-rules`. 551 tests pass.
- **Phase 8.2 COMPLETE** (2026-04-20 late evening). Change 14.2. 28-verb command set + 5 shipped ambient-state regexes expand the precheck gate; operator-editable extras on a new "Command recognition" card on the Personality page with a live test input. `/api/precheck/test` endpoint. Same reload path as 8.1. 569 tests pass.
- **Phase 8.3–8.9 NOT STARTED** — queued and unchanged.

Three new tracks surfaced during the session that weren't in the original plan:

- **Phase 8.10 — TTS pronunciation polish** (new). Piper/Speaches mispronounces "AI" as "Aye" not "Aye Eye"; "live" vowel length wrong in some contexts; punctuation occasionally yields zero-gap word collision. Two attack surfaces: preprompt-side abbreviation expansion, Piper-side lexicon/SSML.
- **Phase 8.11 — Live audio streaming** (new). Server currently buffers ~3 s of TTS audio before emitting the streaming URL; first playback starts ~1.5–3 s after TTS begins. Operator wants closer-to-zero first-chunk latency — stream byte-by-byte instead of chunk-gated.
- **Phase 8.12 — SSL live-apply + HTTPS redirect + HTTP/HTTPS port split** (new). Certificate upload still says "restart container to activate HTTPS." Should hot-reload the TLS context. Non-HTTPS access should 301 to HTTPS when a valid cert is present. Port convention: 8052 HTTP (redirect), 8053 HTTPS.

---

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

### Phase 8.0 — Unified Qwen3 via external Ollama (COMPLETE, 2026-04-20)

**Problem fixed:** two-model plumbing (14B disambiguator + 3B rewriter); Qwen2.5-3B persona drift (German/Thai leakage, verb-polarity flips, cross-turn contamination); no live-apply on config saves.

**Shipped beyond the original scope:**
- Ollama-ipex rip-and-reinstall on B60. `qwen3:8b`, `qwen3:14b`, `qwen2.5vl:7b` pulled. Operator selects which via LLM & Services.
- Engine hot-reload via `/api/reload-engine`. Four stacked bugs unblocked ([`a70889d`], [`4d641c1`], [`666549b`], [`4fb26b4`], [`95b85f1`]). Config saves now apply live with no container restart.
- Model-name sync from `services.yaml` → `glados_config.yaml` ([`58a2876`]).
- `cfg.service_model()` helper ([`4babbc0`]) — every LLM consumer (chat, Tier 2 disambiguator, persona rewriter, autonomy subagents, observer judgment, doorbell screener) resolves through the operator's LLM & Services selection. No hard-coded model names anywhere. Dataclass defaults that previously said `gpt-4o-mini` are now required fields — fail loud instead of silently routing to OpenAI.
- Preprompt rewritten to kill confabulation source: PRIME DIRECTIVE + few-shots removed + household narrative demoted.
- Qwen3 `<think>` reasoning stripped from user-visible text ([`1c409fc`]).
- Audio playback regression fixed (incremental `renderChat`) ([`9b88f03`]).

**Outcome measured:** chat responses are in-character Qwen3-14B output with no scene confabulation, no German/Thai drift, no "regret" copy-paste; audio controls are a single persistent element; config saves take effect on the next request.

**Pending within 8.0 scope:** 3B rewriter code path still imported but unused. Deletion deferred to Phase 8.7 when the response composer replaces it entirely. Native Qwen3 Hermes tool-calling still uses the `a13e9bd` tolerant parser — fine to keep as a safety net.

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

---

### Phase 8.10 — TTS pronunciation polish (surfaced 2026-04-20, P3)

**Problem observed:** Piper (via Speaches TTS) mispronounces common short terms in GLaDOS replies. Specific operator-flagged cases: "AI" pronounced as "Aye" instead of "Aye Eye"; "live" (the verb) given the short-i vowel when the long-i was wanted. Punctuation sometimes yields zero-gap word collision (text-to-phoneme boundary issue).

**Two attack surfaces:**

1. **LLM-side expansion (container-scope).** Strengthen SPEECH RULES in the preprompt with explicit examples: `AI → "Aye Eye"`, `HA → "Home Assistant"`, percent signs spelled, and so on. Catch the common cases before they reach TTS. Low cost, maintainable via the Personality WebUI page.

2. **TTS-side lexicon (Piper / Speaches scope).** Custom pronunciation dictionary per Piper voice (supported via `.espeak-ng` lexicon or Piper's phoneme override config). Handles homographs ("live" / "lead" / "read") and abbreviations. Edits live on the Speaches/Piper side, outside this container. The GLaDOS container's job is to emit well-formed text; it does not own phoneme-level rendering.

**Success:** in a 20-utterance sample, zero mispronunciations of operator-flagged terms. Punctuation pacing sounds natural.

**Cost:** ~100 LOC preprompt edits + operator-side Piper lexicon work (not in container).

---

### Phase 8.11 — Live streaming TTS (surfaced 2026-04-20, P2)

**Problem observed:** current chat flow emits the streaming audio URL to the client only after `~3 s of TTS has buffered` on the server (see `streaming_tts_buffer_seconds: 3.0` in `glados_config.yaml`). First audible byte lands ~1.5–3 s after TTS begins. Operator wants closer to zero first-chunk latency — audio playback starts as soon as the first sentence's first chunk is rendered, not after a buffer is met.

**Work:**

- **Server side.** Replace the 3 s buffer gate with an immediate streaming-URL emission. `/chat_audio_stream/<request_id>` already supports serving chunks as they're generated. The buffer is there to absorb TTS jitter — alternatives: dynamic buffer (start shorter, grow only on underrun), or Range + chunked-transfer-encoding with partial-content semantics so the browser keeps the connection open.
- **Per-sentence TTS dispatch.** `llm_processor._process_sentence_for_tts` already batches sentences for TTS (`MIN_TTS_FLUSH_CHARS: int = 150` in llm_processor.py). Lower that threshold to ~40–60 characters for streaming-mode so the first sentence fires sooner. Accept the extra TTS call overhead (cost: ~50 ms prep per call, parallel-able).
- **Client side.** The `<audio controls>` already handles partial-content / streaming responses correctly now that [`9b88f03`] made it the single persistent element. No client changes needed unless the browser's default buffering is the bottleneck (then `preload="auto"` and small seek hints).
- **Make the buffer target operator-configurable** via the Audio & Speakers page. Default 0.5–1.0 s; operators with slow TTS or flaky networks can raise it.

**Success:** TTFB-audio (time from user send to first audible byte) drops from ~5–8 s current to ~2–3 s. Streaming playback handles underruns gracefully (no clicks / repeat chunks).

**Cost:** ~250 LOC server, ~50 LOC config knob + WebUI slider.

---

### Phase 8.12 — SSL live-apply + HTTPS redirect + port split (surfaced 2026-04-20, P2)

**Problem observed:** operator uploads / renews a TLS cert via the SSL page → toast says *"Certificate uploaded. Restart container to activate HTTPS."* Visiting the plain-HTTP URL doesn't auto-301 to HTTPS once a cert is present. The container uses port 8052 for HTTPS only — there's no plaintext listener that could redirect.

**Work:**

1. **Live TLS reload.** The server currently constructs an `SSLContext` once at startup and wraps the socket. Python's `ssl.SSLContext.load_cert_chain()` can be called on a running context to swap in new cert/key material; subsequent connections pick it up. After a cert save, call `ctx.load_cert_chain(new_cert, new_key)` in-place and update the WebUI toast to "Certificate applied."
2. **HTTP → HTTPS 301 redirect.** Second listener on a separate port (default 8052 HTTP, 8053 HTTPS per operator preference; docker-compose env). The HTTP listener serves one handler — `301 Location: https://host:8053/<path>` for every request. Cheap; fewer than 30 LOC.
3. **WebUI port config.** Ports are already env-driven via `SERVE_PORT` / a new `SERVE_HTTPS_PORT`. Docker-compose yaml controls them per the operator's note (no WebUI toggle needed for plumbing this low-level).
4. **Cert upload & renewal flows** (certbot DNS-01 via Cloudflare is already wired): both trigger the live-reload path.

**Success:** operator uploads cert → toast "Certificate applied." → visiting `http://host:8052/` returns 301 → browser lands on `https://host:8053/` with the new cert. No container restart needed.

**Cost:** ~200 LOC (reload + redirect listener + small WebUI copy updates).

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
