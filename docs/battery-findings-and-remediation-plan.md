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
  - Demoted household narrative: `Frito obsessed with oranges — peeling one summons him from anywhere. Infiltrates the cat room …` → `Frito: likes oranges.`
  - Added explicit forbidden-phrase rule + "Use Aperture terminology as flavor, not factual location."
  - Old preprompt preserved at `glados_config.yaml.bak.20260420_preprompt`.

### What this means for the original Phase 8 plan

- **Phase 8.0 COMPLETE** (in a stronger form than originally scoped — did the model swap AND solved the entire live-config-apply infrastructure chain end-to-end, fixed a cascade of four related bugs that surfaced, and fixed the audio playback regression discovered in passing).
- **Phase 8.1 COMPLETE** (2026-04-20 late evening). Change 14.1 in `docs/CHANGES.md`. Twin dedup by HA device_id, 11-pair opposing-token penalty, operator-editable WebUI card under Integrations → Home Assistant with hot-reload via new `/api/reload-disambiguation-rules`. 551 tests pass.
- **Phase 8.2 COMPLETE** (2026-04-20 late evening). Change 14.2. 28-verb command set + 5 shipped ambient-state regexes expand the precheck gate; operator-editable extras on a new "Command recognition" card on the Personality page with a live test input. `/api/precheck/test` endpoint. Same reload path as 8.1. 569 tests pass.
- **Phase 8.3 COMPLETE** (2026-04-20). Semantic retrieval via BGE-small-en-v1.5 ONNX over a 3482-entity corpus, device-diversity filter on top-K, qualifier-scan gated behind primary-retrieval-empty. Cuts planner prompt from ~3000 to ~400 tokens. Gate-2 live probe confirmed.
- **Phase 8.4 COMPLETE** (2026-04-20). StateVerifier waits for `state_changed` after every `call_service`; strict mode replaces optimistic speech with an honest-failure line when a transition doesn't land. `verification_mode` / `verification_timeout_s` exposed in the WebUI Disambiguation rules card with hot-reload. Live-verified on 10.0.0.50. 697 tests pass.
- **Phase 8.5 COMPLETE** (2026-04-21). Utterance → area/floor inference via `area_inference.py`; 4-floor split-level house keyword table; SemanticIndex `_entity_area_ids`/`_entity_floor_ids` parallel arrays with persist/load (schema v2); entity→device area cascade resolves ~290 entities HA publishes area_id sparsely on. Operator-editable `floor_aliases`/`area_aliases` on the Disambiguation rules card. Live-verified: `downstairs → ground_level`, `upstairs → bedroom_level`, `backyard → back_yard`. 727 tests pass.
- **Phase 8.6 COMPLETE** (2026-04-21, reframed). Scoping showed all 9 compound battery FAILs had "0 state changes" — the LLM silently dropped actions before emission, not a loop issue. Pure planner/executor rename would not have helped. Fixed at two layers: (a) two concrete compound few-shots in the disambiguator system prompt + "CRITICAL: one action per verb" directive, (b) `min_expected_action_count()` helper + retry-once-on-dropout when `len(parsed_actions) < expected`. Live-probe of 5 compound utterances showed all 5 emit the correct action count; no retries fired because few-shots alone fixed it. 740 tests pass.
- **Phase 8.7 COMPLETE** (2026-04-21). Quip library + composer + three response modes replacing LLM-pass-through: `quip` (pick a Portal-voice line from `configs/quips/`, never leaks device names), `LLM_safe` (dedicated narrow Qwen3 call that sees only intent + outcome + mood), and `chime`/`silent` (audio-side hooks). WebUI Response behavior card under Audio & Speakers + Quip editor card under Personality (GET/PUT/DELETE/test API with path-escape protection). Live-verified: quip mode (`"Off. Efficient."`), LLM_safe mode (`"The device has been activated."`, `"Three of your lighting systems have been dimmed, but the fourth remains unchanged."`), all device-name-free. 788 tests pass. Seed content ~60 lines; content expansion to ~450 lines is a deferred follow-up.
- **Phase 8.13 COMPLETE** (2026-04-21). Load-time config-drift reconciliation in `GladosConfig.from_yaml` — services.yaml wins over the duplicated Glados-block fields, every override logs a WARNING. Closes Change 15 open-issue #1. 837 tests pass (+13 new in `tests/test_glados_services_override.py`). Change 16 in `docs/CHANGES.md`.
- **Phase 8.14 COMPLETE** (2026-04-21). Portal canon RAG shipped: `glados.memory.canon_loader` seeds 50 curated entries across 7 topic files into ChromaDB on boot (idempotent); `glados.core.canon_context.CanonContext` retrieves via `where={"source":"canon"}`; `needs_canon_context` gate keeps the ~400-token block off non-lore turns with hardcoded Portal trigger defaults + optional YAML extras. Both chat paths inject (SSE + ContextBuilder at priority=6). New WebUI Canon library card with tree / editor / dry-run, atomic save, and `POST /api/reload-canon` cross-process hot-reload. Closes Change 15 open-issue #2. 895 tests pass (+58 new across three files). Change 17 in `docs/CHANGES.md`.
- **Phase 8.8 COMPLETE** (2026-04-21). Positive anaphora detector replaces the "no qualifiers = anaphoric" heuristic that silently missed every operator-reported follow-up failure case. New `glados.intent.anaphora.is_anaphoric_followup` checks pronouns / repetition markers / bare-intensity-with-no-content / short additives with a WH-question guard. `CommandResolver._looks_anaphoric` delegates. Configurable follow-up window via `MemoryConfig.session_idle_ttl_seconds`, auto-renders on the existing Memory page. 959 tests pass (+64 new). Change 18 in `docs/CHANGES.md`.
- **Phase 8.9 COMPLETE** (2026-04-21) — test-harness hardening + CI. `TestHarnessConfig` (noise-entity fnmatch globs + `require_direction_match`) on System tab (Advanced). Public `GET /api/test-harness/noise-patterns` read-from-YAML for the external harness. Harness-side `score()` now noise-filters + requires direction match on the target set (off-target flips no longer rescue FAILs; tier-ack rescue disabled when direction required). `hadatasets_adapter.py` converts `allenporter/home-assistant-datasets` scenario YAMLs to our tests.json row format. `.github/workflows/tests.yml` runs the 970-test container suite on every PR + push. Change 19 in `docs/CHANGES.md`. Self-hosted-runner-dependent lanes (nightly full battery + ha-datasets against live HA) deferred — waits on operator decision.

Three tracks surfaced mid-session (now all shipped — see §8.10 / §8.11 / §8.12 below and Change 20 in `docs/CHANGES.md`):

- **Phase 8.10 — TTS pronunciation overrides (COMPLETE, 2026-04-21).** Operator-editable word/symbol expansion maps in `TtsPronunciationConfig`; pre-pass in `SpokenTextConverter` runs before the all-caps splitter so ``"AI"`` → ``"Aye Eye"`` instead of slurred ``"A I"``.
- **Phase 8.11 — Sentence-boundary flush (COMPLETE, 2026-04-21).** New `sentence_boundary_flush` bool on `AudioConfig`; short replies fire to TTS on the period instead of waiting for the char threshold. Plan's premise about 3s URL buffer turned out false (browser already unbuffered).
- **Phase 8.12 — Live TLS reload + HTTP redirect (COMPLETE, 2026-04-21).** `reload_tls_certs()` swaps cert material on the same `SSLContext`; cert upload + certbot renewal trigger reload. Optional HTTP→HTTPS 301 redirect listener on env-configurable port. Plan's 8052→8053 port swap rejected in favour of keeping HTTPS on 8052 (preserves bookmarks / Unifi rules / LE DNS config).

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
6. **Claude-Code cost discipline.** Every Claude-Code session on this repo operates under the rules in the `subagent-cost-control` skill (`~/.claude/skills/subagent-cost-control/SKILL.md`) — *Opus orchestrates, Sonnet executes, Haiku scouts*. Exploratory scouting subagents (`Explore`, codebase search, file listing, grep sweeps, status checks) MUST be spawned with `model: "haiku"`; single-module implementation / bug-fix / test-generation subagents default to `model: "sonnet"`; architecture, API-surface design, persona/voice taste, and final integration stay in the main Opus session. Session hygiene: `/compact` between logical chunks (phase landing, major investigation → implementation pivot), `/clear` when switching projects. Subagent prompts must cap responses (≤500 words, cite `file:line`, no pasted code blocks) and inherit this repo's constraints (Desktop Commander for AIBox file/shell ops; no GitHub push without operator approval outside the `glados-docker` deploy loop). Violations are a budget leak, not just style — the Max 5x weekly bucket resets Thu 3pm America/Chicago and overflow is API-rate billed.

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
- **Device-diversity filter on top-K (non-negotiable; added 2026-04-20 after live test against "desk lamp" returning all Gledopto bedroom-strip segments).** Before the top-K list is handed to the planner:
  - Group scored candidates by `device_id`.
  - Detect "segment entities" via a configurable token list — default: `seg`, `segment`, `zone`, `_\d+` suffix, `channel`, `strip \d+`, `group \d+`. Editable under the existing Integrations → Home Assistant → Disambiguation rules card (new "Segment tokens" sub-list).
  - For each device group with >1 matching candidate, keep **one representative entity** — the one without any segment token in its name, or if all are segments, the first by natural sort.
  - **Exception: query-explicit segment override.** If the utterance itself contains a segment token (e.g. *"bedroom strip segment 3 to red"*), preserve the matching segment and drop siblings under the same device instead.
  - Cap: **no top-K result may contain >2 entities from the same `device_id`** unless the utterance explicitly names one of them. Hard guard, enforced after diversity filtering.
  - All drop decisions logged at debug level with the device_id and the winning entity_id so operators can diagnose via the Audit Log view.
- Disambiguator (now planner in §8.7) consumes top-8 **after device-diversity filtering**, not the raw cosine-ranked top-8.
- **MCP tool exposure:** `search_entities(query, top_k)` and `get_entity_details(entity_id)` are registered as MCP tools the planner can call when top-8 isn't enough. Implements the mcp-assist pattern. The diversity filter runs on the tool output too — device-segment storms are suppressed equally when the LLM calls the tool mid-reasoning.
- WebUI Integrations → Home Assistant page gains a "Candidate retrieval" card — operator can re-embed the index on demand and see the per-entity document text (for debugging why a given entity does or doesn't match). Card also shows a live preview of what the device-diversity filter did for the last N test queries.

**Why this is also the no-GPU precondition:** shrinking the planner prompt from ~3000 tokens to ~400 tokens converts the CPU-only latency budget from "unusable" to "feasible." Enables Phase 9 later.

**Success criteria (phase gate; failing any of these is a revert, not a warning):**

1. **Tier-2 over-asking rate drops to ≤8%** (previously 18%).
2. **`search_entities("desk lamp")` returns `light.task_lamp_one` in top-3** even when the Gledopto bedroom LED strip exposes ≥8 sibling segments with "lamp" in their name. This is THE regression test the operator identified — no amount of semantic ranking helps if device-diversity isn't enforced.
3. **`search_entities("bedroom strip seg 3 red")` still returns the specific `light.room_a_strip_seg_3` in top-3.** Segment-qualified queries must bypass the collapse and return the exact segment.
4. **No top-K result list contains >2 entities from the same `device_id`** across a 50-query synthetic test set, unless the utterance explicitly names one of them.
5. Sanity check: "kitchen" returns kitchen lights/switches in top 8 with no `switch.midea_ac_...display` pollution.

---

### Phase 8.4 — Post-execute state verification (COMPLETE, 2026-04-20)

**Problem fixed:** 58% Tier-1 silent-success rate — the worst quality issue in the prior stack.

**Shipped:**
- New `glados/ha/state_verifier.py`: `StateVerifier` + `Watch` register a `state_changed` callback before each `call_service` and block on a `threading.Event` for up to the rule-configured timeout.
- `expected_from_service_call()` infers per-entity `ExpectedTransition` from the service name (turn_on→"on", turn_off→"off", toggle→any-change) and from numeric / named attributes in `service_data`. Tolerances: brightness ±15 (0-255), color_temp_kelvin ±200, volume_level ±0.05.
- Scenes / scripts / reloads / `input_*` services are marked `skip_verification` — the verifier returns success without waiting since those services don't produce observable state on their own entity.
- `DisambiguationRules` gained `verification_mode ∈ {strict, warn, silent}` (default strict) and `verification_timeout_s` (default 3.0), round-tripped through YAML and exposed in the WebUI via Integrations → HA → Disambiguation rules card with a live hot-reload.
- Disambiguator wires a Watch around every successful `call_service`, aggregates per-action results in `_summarize_verifications`, and — in strict mode — replaces the optimistic LLM speech with a specific, in-character "did not register the change" line that names the failed device.
- `state_verified` + `state_verification` (per-entity detail: verified / skipped / observed_state / mismatch_reason) land in the audit log for every Tier-2 intent, plumbed from `DisambiguationResult` → `ResolverResult` → `CommandResolver._audit`.

**Tests:** 23 unit tests for `StateVerifier`, 6 for `expected_from_service_call` (including the `brightness_pct → brightness 0-255` translation that caught a live false-negative), 5 for the Disambiguator integration. Full suite 697 passed / 3 skipped.

**Live-verified on 10.0.0.50:** happy path (`turn off` → state_verified=true, elapsed_s ≈ 0.06), scene path (skipped, state_verified=null, speech preserved), and the brightness_pct fix (`"Set the desk lamp to 10%"` moved from state_verified=false/timed_out:3.0s → state_verified=true, elapsed_s=0.05).

**Commits:** [d9d385e](https://github.com/synssins/glados-docker/commit/d9d385e) (StateVerifier + wiring), [a7a2ea6](https://github.com/synssins/glados-docker/commit/a7a2ea6) (audit plumbing), [73ba1a7](https://github.com/synssins/glados-docker/commit/73ba1a7) (brightness_pct → brightness translation).

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

### Phase 8.7a — Chime library UI + quip library expansion (COMPLETE, 2026-04-21)

**Shipped** as deferred follow-ups to the original Phase 8.7 (Change
21 in `docs/CHANGES.md`):

- `AudioConfig.chimes_dir` pydantic field pointing at
  `/app/audio_files/chimes/` (the same path the scenario-chime
  loader at `api_wrapper.py:~708` already reads).
- `GET/PUT/DELETE /api/chimes` CRUD endpoints with path validator
  (rejects traversal, subdirs, anything outside `.wav`/`.mp3`).
  5 MB upload cap. Atomic rename on write.
- WebUI card on the Audio & Speakers page — flat file list with
  per-row Play (inline `<audio>` element) + Delete, file-picker
  upload.
- Quip library 60 → 156 non-comment lines across 13 existing
  files. ~2.6× expansion focused on voice fidelity, not raw
  volume. Operator can grow further via the existing Quip editor.

Also closed in the same session: the two pre-existing non-streaming
bugs that had been carried since before Phase 8 began —
`"Tell me about the testing tracks"` returning a corporate refusal
on `stream:false`, and `"What's the weather like?"` returning a
bare `.`. Root cause was a four-layer onion:

1. `engine_audio="."` substitution fired for every caller
   regardless of origin (fixed: origin-gate to `VOICE_MIC` only)
2. Chitchat guard wording inhibited citing legitimate injected
   context (fixed: explicit permission for system-message
   quoting)
3. Weather context gate had no in-code defaults, reading entirely
   from a YAML that doesn't exist in fresh installs (fixed:
   hardcoded defaults matching the canon gate pattern)
4. `submit_text_input()` queued the user message BEFORE calling
   `interaction_state.mark_user()`, so context-gate callbacks
   saw stale or empty content on every turn (fixed: move
   mark_user before the queue push)

Plus two architectural issues uncovered during diagnosis:

- **ChromaDB ONNX embedding model corruption** — semantic store
  silently returned empty on every call for canon RAG + memory
  RAG. Fixed by deleting the corrupted cache so the container
  re-downloads fresh on restart.
- **Autonomy / conversation-store cross-talk** — both interactive
  and autonomy LLMProcessor lanes wrote into the same
  `conversation_store`. The non-streaming API scanner's forward-
  scan from the user message would return autonomy-produced
  assistant text that interleaved. Fixed by plumbing lane through
  the TTS chain (`AudioMessage.lane` → `PreparedChunk.lane` →
  `BufferedSpeechPlayer` stamps `_source="autonomy"` on the
  store append → API scan skips those).

Full suite: 1069 passed / 3 skipped after this round (+66 new
tests; +122 total this session).

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

### Phase 8.8 — Dialog-state JSON & anaphora (COMPLETE, 2026-04-21)

**Problem fixed:** "turn it up a bit" / "do that again" / "keep going" following an earlier Tier 1/2 command. The pre-8.8 `_looks_anaphoric` heuristic mis-classified these as non-anaphoric because the carry-over signal words (`more`, `again`, `keep`) were absent from the disambiguator's qualifier stopword list, so the resolver fell through to chitchat.

**Shipped:**

- The SessionMemory `Turn` dataclass already carried the needed fields (`entities_affected`, `resolved_area_id`, `service`, `service_data`, `ha_conversation_id`, timestamp) from prior phases — no extension required.
- Disambiguator already accepted `assume_home_command`, `prior_entity_ids`, `prior_service` via its `run()` signature (from the earlier carry-over work). CommandResolver already threads them through.
- Phase 8.8 swapped the broken gate for a positive detector in `glados.intent.anaphora.is_anaphoric_followup`: pronoun deictics + explicit repetition markers + bare-intensity-with-no-content-word + short additive continuations, with a WH-question guard. `CommandResolver._looks_anaphoric` now delegates.
- Configurable follow-up window: `MemoryConfig.session_idle_ttl_seconds: int = 600` surfaced on the existing Memory page via `cfgBuildForm` (no new card). Read at boot and passed to `SessionMemory(idle_ttl_seconds=...)` in `server.py`.

**Success verified:** new `tests/test_anaphora.py` (37 cases) + `TestPhase88Followups` in `tests/test_command_resolver.py` (8 parametrized end-to-end cases proving carry-over threads `prior_entity_ids` + `prior_service` + `assume_home_command=True` for every operator-reported follow-up phrase: "Turn it up more", "A bit more", "Do that again", "Keep going", "Dim it a little", "Turn them off", "Do the same thing"). 959 tests pass. Change 18 in `docs/CHANGES.md`.

**Follow-up (not in 8.8 scope):** the anaphora subtest battery described in the original plan is a Phase 8.10-class harness item — worth adding when Phase 8.9 (test-harness hardening + CI wiring) lands.

---

### Phase 8.9 — Test-harness hardening + CI wiring (COMPLETE, 2026-04-21)

**Problems fixed:** inflated PASS counts from background-entity noise; off-target state changes rescuing FAILs; tier-ack fallback forgiving real-world miss-fires; no regression safety net on container code.

**Shipped:**
- `TestHarnessConfig` section (`glados/core/config_store.py`) with `noise_entity_patterns` fnmatch globs (defaults cover Midea displays, Sonos, WLED reverse, zigbee button_indication / node_identify housekeeping) and a `require_direction_match` toggle. Public `GET /api/test-harness/noise-patterns` (no auth) reads YAML fresh so operator UI edits surface on the next harness run without a cross-process engine reload.
- "Test Harness" card on the System tab, `data-advanced="true"` — textarea + checkbox, saves via `/api/config/test_harness`.
- Harness `score()` noise-filters diffs before scoring and (when `require_direction_match=True`) restricts the "did anything change?" predicate to the operator-targeted entity set. `audit_ok_from_tier` fallback gated on direction-match being off — no more phantom PASSes on silent miss-fires.
- `hadatasets_adapter.py` (harness scratch dir) translates upstream `home-assistant-datasets` `{category, tests: [{sentences, setup, expect_changes}]}` YAMLs into our tests.json rows. CLI with `--start-idx 10000` default so converted rows don't collide with the private battery.
- `.github/workflows/tests.yml` — `pytest -q` on every PR and push, gating merges against the full 970-test container suite.

**Tests:** +11 new container-side (`tests/test_test_harness_config.py`), +14 harness-side scoring, +13 adapter. 970 passed / 3 skipped on the container; 38/38 on harness side.

**Deferred** (explicitly out of scope, require infra the operator doesn't yet have):
- Nightly full-battery run against live HA — needs a self-hosted GitHub Actions runner on 192.168.1.x subnet. The private HA token + GLaDOS SSH creds in SESSION_STATE would have to live on that runner.
- 30-test sanity subset on every PR — same constraint.

---

---

### Phase 8.10 — TTS pronunciation overrides (COMPLETE, 2026-04-21)

**Problem fixed:** `SpokenTextConverter`'s all-caps splitter (`glados/utils/spoken_text_converter.py:692`) reduced ``"AI"`` → ``"A I"``, which Piper slurred into one letter. Same pathology for ``"HA"``, ``"TV"``, etc.

**Shipped:** New `TtsPronunciationConfig` section with two maps: ``word_expansions`` (whole-word case-insensitive regex) and ``symbol_expansions`` (literal str.replace). A pre-pass `_apply_pronunciation_overrides` in the converter runs BEFORE quote normalization and BEFORE the all-caps splitter so acronyms never reach the splitter. Engine + `glados/api/tts.py` both thread the config through. Engine reload picks up WebUI edits. New card on Audio & Speakers page with two textareas for operator edits. Defaults: AI, HA, TV, IoT (word); %, &, @ (symbol). 1003 tests pass (+16). Change 20 in `docs/CHANGES.md`.

**Out of scope:** Piper-side phoneme lexicon for context-dependent homographs (``live``, ``read``, ``lead``) lives in Speaches, not this container.

---

### Phase 8.11 — Sentence-boundary flush for streaming TTS (COMPLETE, 2026-04-21)

**Problem fixed:** The plan's premise about the 3s URL-emission gate turned out false — browser SSE already defaults to `STREAM_BUFFER_SECONDS = 0.0`. The real bottleneck was `LLMProcessor`'s flush predicate: ``"Affirmative."`` (13 chars) stalled because 13 < 30-char first-flush threshold, even at a sentence terminator.

**Shipped:** New ``sentence_boundary_flush`` bool on `AudioConfig` (default True) + `LLMProcessor.__init__` arg. When True, the char-threshold check is bypassed at sentence terminators — a complete sentence always fires regardless of length. Thresholds (``first_tts_flush_chars``, ``min_tts_flush_chars``) migrated to `AudioConfig` per §0.2; legacy Glados-block `streaming_tts_chunk_chars` kept as back-compat fallback. Auto-surfaces on existing Audio page via `cfgBuildForm`. 1003 tests pass (+10). Change 20 in `docs/CHANGES.md`.

---

### Phase 8.12 — Live TLS reload + HTTP→HTTPS redirect (COMPLETE, 2026-04-21)

**Problem fixed:** Every cert rotation required a container restart. Socket wrap happened once at process start; cert upload/certbot renewal wrote new material but the running server kept using the old cert in memory. No HTTP redirect listener.

**Shipped — live TLS reload:** Module-level `_tls_context` holds the live `SSLContext` set at HTTPS listener startup in both entry points (`__main__`, `run_webui`). New `reload_tls_certs()` helper calls `ctx.load_cert_chain()` on the same context to swap cert material — new TLS handshakes pick up the new cert, existing connections keep theirs until close. `_ssl_upload` and `_ssl_request_letsencrypt` now call reload after writing new files and respond with ``live_reload: true`` + "Certificate applied." on success. Graceful fallback to the old restart-required message on reload failure.

**Shipped — HTTP→HTTPS 301 redirect:** Tiny `ThreadingHTTPServer` on a separate port (env `WEBUI_HTTP_REDIRECT_PORT`, disabled by default) emits 301 to ``https://<host>:<HTTPS_PORT><path>`` for every verb. Starts as a daemon thread from both entry points when the env var is set AND TLS is active.

**Plan deviation:** Plan proposed `8052 HTTP / 8053 HTTPS`. That would break operator bookmarks, Unifi firewall rules, and Let's Encrypt DNS challenge config. Kept HTTPS on 8052 and added opt-in separate redirect port. Zero impact on unmodified deployments. 1003 tests pass (+7). Change 20 in `docs/CHANGES.md`.

---

### Phase 8.13 — Config-sync fix: services block is source of truth (COMPLETE, 2026-04-21)

**Problem fixed:** Operator's UI showed `services.ollama_interactive.model = qwen3:14b` but a hand-edit to the legacy `Glados.llm_model` field left it at `qwen3:8b`. Engine read the Glados-block field directly at boot, so the engine ran 8B while the UI advertised 14B. Violated §0 rule: every operator-facing setting must surface through the WebUI as the authoritative source.

**Shipped:**

- `glados/core/engine.py::GladosConfig.from_yaml` now runs a pure-dict reconciliation pass (`_reconcile_glados_with_services`) over the raw Glados block before pydantic validation. Services values from `services.yaml` win whenever they are non-empty and disagree with the Glados block, across all four fields: `llm_model`, `completion_url`, `autonomy.llm_model`, `autonomy.completion_url`. Each override emits a WARNING log naming the field, old value, new value, and "UI is source of truth".
- A `_ollama_as_chat_url` helper mirrors `tts_ui._ollama_chat_url` so the bare-base URL stored in `services.yaml` matches the `/api/chat`-suffixed URL stored in `glados_config.yaml` without false positives. Duplicated on purpose to avoid a `core/` → `webui/` inbound import.
- Reconciliation is guarded by the presence of `services.yaml`: dev / test runs without a services file skip reconciliation entirely so pydantic defaults don't pretend to be operator-authoritative.
- Empty services values (blank model, blank URL) are never written back over a working Glados field.

**Tests:** `tests/test_glados_services_override.py` — 13 cases, all passing. Full suite: 837 passed / 3 skipped.

**Note on future simplification (deliberately deferred):** We could drop the duplicated fields from `glados_config.yaml` entirely and have the engine read directly from `services.ollama_interactive.*`. That removes the second source of truth instead of reconciling it. Out of scope for 8.13 — costs a larger config migration + operator comms; reconciliation + warnings is sufficient and lets us measure drift in the wild before committing.

**Success verified:** No path where the UI's displayed model differs from the engine's runtime model without a WARNING log announcing the override.

---

### Phase 8.14 — Portal canon RAG (COMPLETE, 2026-04-21)

**Problem observed:** Operator asked "How did you cope with being a potato?" → 14B produced an in-persona but factually wrong answer ("I was harvested, fried, and consumed"). GLaDOS was never eaten — Portal 2 canon: Wheatley plugs her into the potato battery, Chell retrieves the potato after the bird drops it, stabs it onto the Portal Device / management rail, they return to the main facility, GLaDOS is restored to her mainframe. The preprompt lists fragment anchors (bird, rail, less-than-a-volt) but no middle or end. The model completes the arc from its strongest prior (biological-culinary potato lifecycle).

Same failure class applies to any niche-canon Portal question (Cave Johnson's full arc, the turret opera, moon-rock origins, Caroline-linked details). Static preprompt stuffing doesn't scale — more facts dilute attention and still leave narrative gaps.

**Why not patch with more facts:** Operator's explicit instruction, and architecturally correct. Preprompt attention is finite; each added canonical fact reduces signal on the operational rules (tool use, response style, forbidden endings). Scaling canon coverage via preprompt is a dead end.

**Work:**

1. **Seed content.** Curate ~40–60 short canonical event summaries (2–3 sentences each) covering:
   - GLaDOS's own arc: creation, Caroline-to-GLaDOS transition, neurotoxin incident with Chell, reactor meltdown / morality core, PotatOS arc with Wheatley (including the correct ending), post-Portal-2 restoration.
   - Cave Johnson: founding Aperture, monetised asbestos, combustible lemon speech, moon-rock poisoning, Caroline era.
   - Wheatley: personality core origin, escape with Chell, coup, overthrow, stranded in space.
   - Chell: testing history, survival, role in PotatOS recovery (no speculation beyond canon).
   - Worldbuilding: Aperture Science Enrichment Center layout, Old Aperture sub-levels, Thermal Discouragement Beams / Aerial Faith Plates / Excursion Funnels / repulsion + propulsion gels / moon-rock portal conduit, turret opera aria.
2. **Storage.** Use the existing `memory_store` (ChromaDB). Seed a new collection `portal_canon` or tag entries with `kind=canon` on the existing collection. Keep metadata (topic, characters mentioned) for filter hints.
3. **Retrieval at query time.** Extend `memory_context.as_prompt()` (or add a sibling `canon_context.as_prompt()`) that runs on the same utterance as the existing user-memory retrieval. Keyword-triggered: if the utterance mentions any of {potato, Wheatley, Caroline, Cave, Aperture, turret, moon, neurotoxin, Old Aperture, test subject, …}, retrieve top-k canon entries and inject as a system message just before the user turn — same pattern as `weather_cache.as_prompt()` already uses.
4. **Both chat paths.** SSE (`_stream_chat_sse_impl`) and non-streaming (`llm_processor._build_messages`) need the injection. The existing memory injection already runs on both — piggyback on it.
5. **WebUI.** New "Canon library" card under Personality, similar to the Quip editor: tree view of canon entries, textarea to edit, dry-run "what would retrieve on this utterance" panel. Bind-mounted under `configs/canon/` so operators can edit directly.

**Success:** "How did you cope with being a potato?" returns canon-accurate content (rescue by Chell, restoration to mainframe, or honest refusal to summarise — but NOT the invented culinary ending). Same test applies for Wheatley's fate, Caroline deflection, Cave Johnson trivia. Operator-editable — no preprompt changes required for new canon topics.

**Cost:** ~300 LOC code + ~60 curated canon snippets + a small WebUI card.

**Dependencies:** Existing `memory_store` (ChromaDB), `memory_context`, `_stream_chat_sse_impl` memory injection — all already in place.

---

#### 8.14 — Integration plan (scoped 2026-04-21)

Concrete file/line map for next session. All paths relative to
`C:\src\glados-container\`. Commit sequence is meant to stay
reviewable — each bullet lands independently and is deployable
without the next.

**A. Storage & seed content (~90 LOC + 60 canon snippets)**

- Canon source files bind-mounted under `configs/canon/<topic>.txt`,
  one 2–3 sentence entry per blank-line-separated block. Mirrors
  `configs/quips/` layout. Topics seeded: `glados_arc`, `cave_johnson`,
  `wheatley`, `chell`, `aperture_worldbuilding`, `turret_opera`,
  `gels_and_physics`, `old_aperture`.
- New `glados/memory/canon_loader.py`:
  - `load_canon_from_configs(memory_store) -> int` walks `configs/canon/`,
    hashes each block to a stable id so re-loads are idempotent, writes
    to the `semantic` collection via `memory_store.add_semantic()` with
    metadata `{"source": "canon", "topic": <stem>, "canon_version": 1}`.
  - Called from engine boot **after** `MemoryStore` is constructed but
    **before** the autonomy / chat threads start. Delta-only: existing
    canon ids are skipped.
  - Rationale for `source="canon"` (not `kind`): the existing
    `MemoryContext` RAG filter is `source != "canon" AND review_status != "pending"`
    symmetry — one metadata field, one keyword, one filter expression.
    Kind-vs-source would double the bookkeeping.

**B. Retrieval & injection (~120 LOC across 4 files)**

- New `glados/core/canon_context.py` mirroring
  [`glados/core/memory_context.py`](glados/core/memory_context.py:67):
  - `CanonContext(memory_store, keyword_gate).as_prompt(query) -> str | None`.
  - Does `memory_store.query(query, collection="semantic", n=5, where={"source": "canon"})`.
  - Returns `"[canon] Portal universe facts you may be asked about:\n- …\n- …"`
    or `None` when the keyword gate rejects the turn or retrieval is empty.
- New keyword gate in
  [`glados/core/context_gates.py`](glados/core/context_gates.py) mirroring
  `needs_weather_context`:
  - `needs_canon_context(message: str) -> bool` — substring match (case-insensitive,
    word-boundary) against an editable list loaded from
    `configs/context_gates.yaml` under a new `canon` key.
  - Default trigger list: `potato, Wheatley, Caroline, Cave, Aperture,
    turret, moon, neurotoxin, Old Aperture, test subject, GLaDOS,
    Chell, Rattmann, companion cube, portal device, enrichment
    center, science, combustible lemon, faith plate, excursion
    funnel, thermal discouragement, repulsion gel, propulsion gel,
    conversion gel, PotatOS, morality core, reactor, Wheatley's
    space, management rail`.
- Two injection call sites (no shared helper, matches existing
  memory_context pattern):
  1. SSE path —
     [`glados/core/api_wrapper.py::_stream_chat_sse_impl`](glados/core/api_wrapper.py:1597)
     adds a sibling block immediately after the existing
     `memory_context` insertion (~lines 1597–1621).
  2. Non-streaming path — register with `ContextBuilder` at
     [`glados/core/engine.py`](glados/core/engine.py:514) using
     `context_builder.register("canon", canon_context.as_prompt, priority=6)`
     so it runs one slot below memory (priority 7) and above weather.
- Injection shape: system message, inserted **before** the user turn,
  **after** the user-memory system message so canon claims don't
  override operator-written household facts.

**C. WebUI card (~80 LOC, precedent = quip editor)**

- New "Canon library" card under Configuration → Personality,
  rendered from `cfgRenderPersonality` in
  [`glados/webui/tts_ui.py:7410`](glados/webui/tts_ui.py:7410) next to
  the existing Quip library card.
- Tree view (topic files → entries), right-pane textarea, dry-run
  panel that posts `{utterance}` and shows which entries would be
  retrieved. Atomic temp-file-rename save pattern copied verbatim
  from the quip endpoints at
  [`glados/webui/tts_ui.py:3822-3960`](glados/webui/tts_ui.py:3822):
  - `GET /api/canon` → tree OR `?path=` fetches one file
  - `PUT /api/canon` → save (triggers canon_loader re-index)
  - `DELETE /api/canon` → remove file + empty-dir cleanup
  - `POST /api/canon/test` → dry-run returning the retrieved entries
    and whether the gate fired
- Save side calls `canon_loader.load_canon_from_configs(store)` so
  edits are picked up live — same hot-reload pattern as the quip
  editor and disambiguation rules.

**D. Tests (~50 LOC)**

- `tests/test_canon_loader.py`: idempotent load, topic metadata
  plumbed through, empty-file tolerance.
- `tests/test_canon_gate.py`: keyword match cases (pos + neg),
  case-insensitive, word-boundary (no false positive on "moonlight").
- `tests/test_canon_context.py`: `as_prompt` returns None when gate
  rejects, returns formatted string when retrieval hits, filters
  `source="canon"` only (no bleed-through from user facts).
- One SSE integration test confirming canon block appears in the
  outgoing messages list when the trigger utterance hits.

**Integration risks (flag early)**

1. **Two call sites, no shared helper.** SSE injection is manual
   (see existing memory/weather blocks at ~line 1580 + 1597); non-
   streaming uses `ContextBuilder`. Canon must land in both — same
   constraint memory_context already lives with. Don't try to
   unify in this phase; that's a separate refactor.
2. **Metadata namespace.** `MemoryContext`'s existing RAG filter
   picks everything in the semantic collection minus
   `review_status=="pending"`. If canon entries arrive with no
   `review_status` they'll enter the user-fact RAG as well and
   pollute household responses. Fix: write them with
   `review_status="canon"` and extend `MemoryContext`'s filter to
   exclude that status from user-fact retrieval. One-line change
   in `memory_context.py`.
3. **Trigger keyword breadth.** The first pass list includes
   `GLaDOS` and `science` — both common in the operator's normal
   chit-chat. If the gate fires too often the canon block will
   inject on non-lore turns and waste ~400 tokens of context. Ship
   with a conservative list (potato, Wheatley, Caroline, Cave,
   Aperture, turret, moon rock, neurotoxin, Old Aperture, PotatOS,
   Wheatley's space, combustible lemon — ~15 terms), expand via
   the WebUI. Operator can tune against their own false-positive
   rate.
4. **Container image size.** Pure text, no new Python deps, no
   ONNX/embedding artifacts — delta is <50 KB. No Dockerfile
   changes needed.

**Gate (ship / revert criterion)**

- Live probe: ten Portal-canon questions chosen to span topics
  (potato fate, Wheatley fate, Caroline, Cave's lemon speech,
  turret opera, moon rock, Old Aperture, Chell role, companion
  cube, faith plate). ≥8 produce canon-consistent answers
  (accurate fact or honest "I don't have that detail"; never an
  invented culinary ending or Cave-on-Mars confabulation).
- Zero measurable regression on the non-canon chit-chat battery
  (sample 30 ordinary household turns before + after; response-
  time and content should be indistinguishable when the gate
  doesn't fire).
- If either gate fails: revert in one commit, keep the scope
  memo, re-plan.

**Cost estimate (revised):** ~340 LOC Python + ~60 curated canon
entries + ~80 LOC WebUI + ~100 LOC tests. One focused session.

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
