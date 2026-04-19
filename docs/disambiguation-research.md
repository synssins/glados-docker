# Tier 2 Disambiguation — Research & Open Problems

**Status as of 2026-04-19 end-of-session:** partial fix deployed; core architectural question still open.

**Live image:** `ghcr.io/synssins/glados-docker:latest` @ commit `734e1f7`, running on 192.168.1.150.

**Test count:** 374 passing.

---

## 1. The Goal

GLaDOS should handle a natural device-control conversation without making the operator re-specify context on every turn. In particular:

1. **Compound noun queries** — `"desk lamp"` must pick the entity whose name contains both qualifiers, not every entity whose name contains "lamp".
2. **Follow-up refinements** — after `"Turn the desk lamp down by half"`, a subsequent `"Increase the brightness by ten percent"` should operate on the same desk lamp with the new quantity, without the user having to say "desk lamp" again.
3. **Activity / intent inference** — from a living-room voice satellite, `"I would like to read"` should activate the living-room reading scene without asking "which reading scene?" or "in which room?".
4. **Area inference** — `"the reading lamp"` from the living room satellite should resolve to the living-room reading lamp, not the office one.

All four collapse to one technical problem: **the Tier 2 disambiguator needs more context than the current turn's string alone.**

---

## 2. Stack in Play

```
User utterance (webui_chat, voice_mic, api_chat)
      │
      ▼
┌──────────────────────────────────────────────────────────────────┐
│ api_wrapper precheck                                             │
│   looks_like_home_command(utterance)  OR  _should_carry_over_*   │
└──────────────────────────────────────────────────────────────────┘
      │
      ▼ (if either true)
┌──────────────────────────────────────────────────────────────────┐
│ Tier 1 — HA conversation API (bridge.process)                    │
│   HA intent parser; ~0.5s; handles exact-name matches            │
└──────────────────────────────────────────────────────────────────┘
      │
      ▼ (miss with should_disambiguate)
┌──────────────────────────────────────────────────────────────────┐
│ Tier 2 — Disambiguator                                           │
│   1. Internal looks_like_home_command precheck                   │
│   2. EntityCache.get_candidates(utterance, domain_filter, ...)   │
│      ↳ _preprocess_query: strip verbs, modifiers                 │
│      ↳ WRatio fuzzy scoring (admission gate: 60–75 per domain)   │
│      ↳ Coverage boost + area boost (soft ranking)                │
│   3. qwen2.5:14b JSON-format call with candidate list + prompt   │
│   4. Execute via HAClient.call_service (entity_ids + service_data)│
└──────────────────────────────────────────────────────────────────┘
      │
      ▼ (fall-through)
Tier 3 — Full chat LLM with MCP tool loop (slow, ~30s+)
```

Each layer has its own set of hard-coded assumptions; brittleness compounds.

---

## 3. This Session's Fixes (chronological)

### 3.1 `service_data` plumbing (commit `31748a7`)

**Bug:** Tier 2 never populated `service_data`. Every brightness / colour / temperature / volume / fan request went through as a bare `turn_on`; HA defaulted to the device's last state and nothing visibly changed.

**Fix:**
- Added `service_data` to `DisambiguationResult`, the disambiguator JSON schema, and the LLM prompt (with absolute + relative phrasing examples).
- Candidate prompt lines now carry `attrs=brightness_pct=X, color_temp_kelvin=Y, …` when state is fresh so relative adjustments have a current-state anchor.
- Parsed `service_data` passed through to `HAClient.call_service`.
- Audit `extra` carries `service_data` for both `ok:execute` and `execute_no_ack`.
- Also stripped trailing vocatives ("test subject") from Tier 2 speech; was previously only rewriter-protected.

**Outcome:** ✅ Confirmed live. `"Turn the desk lamp down by half"` produced `brightness_pct:25`, lamp physically dimmed from 50% to 25%.

### 3.2 First carry-over attempt — DB-backed (commit `31748a7`, reworked later)

**Bug:** `"It's still too dark. Turn it up more."` after a successful Tier 2 had no device keyword → `looks_like_home_command` returned False → api_wrapper skipped Tier 1/2 entirely → Tier 3 chitchat hallucinated a status confirmation.

**Fix (initial):** `ConversationDB.latest_assistant_tier_exchange()` returning the most-recent assistant row's `(tier, ts, ha_conversation_id)`. `_should_carry_over_home_command()` used this; 120s window.

**Outcome:** ❌ Broken by the compaction-ts interaction described next.

### 3.3 Compaction metadata preservation (commit `5da099b`)

**Bug:** The compaction agent's rebuild path (`ConversationDB.replace_conversation`) wrote every row with a single uniform `source='compaction'`, `tier=None`, `ts=now`. Every post-compaction DB read showed the whole history as tier-less chitchat stamped with the compaction time, and:
- The carry-over DB check couldn't find a `tier=2` row.
- Even if I found one, the `ts` was always recent → the window check lied.

**Fix:**
- `ConversationStore.append` / `append_multiple` stamp `_tier` / `_source` / `_ha_conversation_id` / `_principal` on each message dict before persisting.
- `ConversationDB.append_many` reads those `_`-prefixed keys as fallback per-row metadata when the uniform kwarg is None.
- `replace_conversation(..., source="compaction")` only stamps `_source="compaction"` on messages that don't already carry one — preserved rows retain their origin.
- Underscore keys are stripped by `llm_processor._sanitize_messages_for_{ollama,openai}` allowlists and by the DB `extra` packer, so no LLM leakage.

**Outcome:** ✅ Tier metadata survives compaction. But the DB-based carry-over *approach* itself still had issues (ts semantics after multiple rewrites, race between compaction and read), which motivated the eventual switch to an in-memory cache.

### 3.4 Dynamic chat box (commit `5da099b`)

**Request:** chat panel was fixed at `height: 400px`, forcing inner scrolling in small viewports.

**Fix:** `.chat-messages { height: calc(100vh - 260px); min-height: 320px }`.

**Outcome:** ✅ Visible in operator screenshots.

### 3.5 Stopword expansion (commit `e3df8de`)

**Bug:** `"Turn the desk lamp down by half"` preprocessed to `"desk lamp down by half"`, and `"down by half"` polluted the WRatio score against `"Office Desk Monitor Lamp"` below the 75 cutoff. Tier 2 returned zero candidates.

**Fix:** Extended `_QUERY_STOPWORDS` with direction/quantity modifiers (`up`, `down`, `brighter`, `dimmer`, `more`, `less`, `bit`, `half`, `halfway`, `max`, `min`, …) and additional verbs (`adjust`, `change`, `increase`, `decrease`, `raise`, `reduce`, `can`, `you`, `could`, `would`). Whole-word so `downstairs` / `upstairs` are unaffected.

**Rationale:** These words belong in the *action payload* (`service_data`), not the entity-name match. The LLM still sees the full original utterance so quantity inference is unaffected.

**Outcome:** ✅ `"Turn the desk lamp down by half"` now preprocesses to `"desk lamp"` — matches "Office Desk Monitor Lamp" cleanly.

### 3.6 Hard qualifier-tight filter (commit `4736c27`, reverted next commit)

**Bug:** Even after 3.5, fuzzy returned three candidates for `"desk lamp"`: the Desk Monitor Lamp **and two unrelated Arc Lamps**. The disambiguator then asked the user to pick between them.

**Fix (attempted):** After WRatio scoring, if any candidate's name contained ALL query tokens whole-word, keep only those full-coverage candidates.

**Outcome:** ✅ Worked for the desk lamp case. ❌ The operator correctly pushed back on the architectural implications (see §4).

### 3.7 Soft coverage boost + area-match hook + prompt signal (commit `c408a36`)

**Replaces 3.6.** Blends three signals as soft-ranking bonuses:

- **Coverage (up to +15)**: `coverage_ratio × 15`. Full-coverage candidates rise to the top but partial matches still survive.
- **Area match (+10 flat)**: When `source_area` is provided, entities with matching `area_id` get a flat bonus. Threaded via `Disambiguator.run(utterance, source, source_area=...)` and `EntityCache.get_candidates(..., source_area=...)`. Voice-mic satellites aren't wired yet; chat/api callers pass `None` → behaviour unchanged.
- **Prompt-level signal**: Each candidate prompt line now carries `coverage=XX%` and (when a source_area was provided) `same_area=yes/no`. New prompt section tells the LLM these are *ranking hints, not hard filters* — synonym / scope / activity rules still override.

**Outcome:** ✅ Desk lamp case still resolves correctly. ✅ Preserves synonym / scope / activity overrides. **B (area binding) is the next substantive item** — wire `source_area` from voice satellites.

### 3.8 In-memory carry-over + prior-target injection (commit `734e1f7`)

**Bug:** `"Increase the brightness by ten percent"` after a Tier 2 success still fell to Tier 3. Audit: `tier=2 result="fall_through:no_home_command_intent"`. Two causes:
1. `Disambiguator.run` has its own `looks_like_home_command` precheck that fires *inside* the disambiguator, even when api_wrapper's outer gate passed via carry-over.
2. Even if bypassed, the fuzzy lookup on `"Increase the brightness by ten percent"` returns zero light entities — there's no device noun to match against.

**Fix:**
- Replaced the DB-backed carry-over with a module-level `_RECENT_TIER_ACTION: dict[conv_id, _RecentTierAction]` cache holding `(ts, tier, entity_ids, service, ha_conversation_id)`. Populated on every Tier 1/2 ok:execute, cleared on intervening Tier 3 chitchat.
- `assume_home_command` + `prior_entity_ids` + `prior_service` kwargs plumbed through `_try_tier1_fast_path` / `_try_tier1_nonstreaming` → `_try_tier2_disambiguation` → `Disambiguator.run`.
- `Disambiguator.run` skips the internal precheck when `assume_home_command=True`.
- When `prior_entity_ids` are provided, they're injected as synthetic full-coverage candidates at the top of the list (score=100, coverage=1.0) so the LLM can act on them even when fuzzy matching misses.
- Prompt carries a `Follow-up context: the user's PREVIOUS turn acted on entities [...]` segment.
- Default follow-up window bumped 120s → 300s.

**Outcome:** 🟡 **Deployed, but not operator-verified this session.** Needs morning retest:
- `"Turn the desk lamp down by half"` → expect Tier 2 execute with `brightness_pct:25`.
- `"Increase the brightness by ten percent"` → expect Tier 2 execute with `brightness_pct:~35` on the *same* desk lamp entity.

---

## 4. The Architectural Question the Operator Raised

After the hard qualifier-tight filter (3.6) landed, the operator asked:

> Is this the right fix? How will this affect other devices with similar names? What are the potential issues this will cause with the end goal of being able to issue a command with a general device name or request (such as "I would like to read" coming from a voice assistant in the living room) and having GLaDOS infer correctly which device, room, area, is meant without needing additional input?

The honest answer was no. The hard filter solved the desk-lamp case but foreclosed three downstream capabilities:

1. **Synonym overrides** (`overhead` ↔ `ceiling`). The prompt has an operator-tunable `overhead_synonyms` rule; the hard filter fires before the LLM can apply it.
2. **Area inference.** The filter has no notion of the requesting satellite's area.
3. **Scope-broadening for plurals** (`"bedroom lights"` → every bedroom fixture). Works in the narrow case where no literal group entity exists, breaks when one does.

Replacing with a **soft score boost** (3.7) keeps all three capabilities available. That's the current design. Whether it's the *right* architecture is the open question (see §5).

---

## 5. Open Research Questions

### 5.1 Is ranking-boost composition sufficient?

Current scoring is `final_rank = WRatio + coverage*15 + area_bonus*10 + carry_over_synthetic`. Each signal is hand-tuned. Known pathologies:

- **Plural/singular.** `"lamps"` vs `Lamp` is a miss on whole-word matching. Coverage ratio drops. Fine if no literal plural group exists; wrong if the LLM should scope-broaden.
- **Alias asymmetry.** Operators tend to alias ambiguous entities and skip the well-named ones. The well-named entity can lose to an alias-heavy cousin.
- **Long queries.** A 5-token query can mean a candidate with one exact phrase beats one with two scattered tokens, even though the scattered candidate is clearly the target.
- **Area vs coverage trade-off.** Area bonus (+10) beats coverage (+15 × 0.5 = +7.5 for 1/2 coverage). Good for "the reading lamp" from a living-room satellite; potentially wrong for "the office reading lamp" said from the living room.

### 5.2 Should we invert the control flow?

Currently the cache ranks then the LLM decides. An alternative: the LLM sees *more* candidates (top 30 instead of top 12) with metadata, and it does all the reasoning. The cache becomes dumb fetch-everything-plausible; the LLM carries the synonym / scope / activity / area logic end-to-end.

Cost: more tokens in the prompt, possibly slower. Benefit: one place to change behaviour (the prompt), no score-tuning.

### 5.3 Semantic embeddings instead of (or alongside) WRatio?

ChromaDB is already in the stack for episodic / semantic memory. A small embedding model over entity friendly_names + aliases + area + common synonyms would let `"ceiling"` ≈ `"overhead"` ≈ `"top light"` without a hand-maintained synonym list. Would also handle `"lamps"` / `"lamp"` via subword similarity.

Open: latency budget (ChromaDB is fast for cached queries), and whether to replace WRatio or supplement it.

### 5.4 Context as a first-class input, not a carry-over hack

Today "follow-up context" is bolted on via the in-memory cache and prior-entity injection. A cleaner model would treat every turn as: *(user utterance, active device context, active area context, active activity context)*. The disambiguator input is the full tuple, not just the utterance string.

Active contexts would be populated by:
- The prior Tier 1/2 turn's target → **device context**.
- The source satellite's area → **area context**.
- A recent activity phrase → **activity context** (e.g., after "movie time", volume/lighting commands default to the media room).

Contexts decay (time-based or turn-count-based). Multiple contexts compose.

This is the approach that generalises to "I would like to read" from a living-room satellite: the area context provides the room, the activity context picks the scene, the device context (if any) refines.

### 5.5 The disambiguator's two prechecks

Having both api_wrapper AND `Disambiguator.run` call `looks_like_home_command` caused the P0 in 3.8. Today's fix adds an `assume_home_command` bypass kwarg. A cleaner design might collapse the check to one place (either api_wrapper OR the disambiguator, not both). The disambiguator-side one exists because it's also the entry point for direct (non-api_wrapper) callers, but in practice nothing else calls it.

### 5.6 Evaluation harness

There is no labelled corpus or regression suite that tests a real disambiguation scenario end-to-end. Each bug this session was caught by the operator in production. `tests/test_disambiguator.py` covers unit behaviours but not "does the system handle the 'I would like to read from the living room' scenario as intended".

**Next-session investment:** build a small labelled dataset of (utterance, source, source_area, prior_context, expected_entity_ids, expected_service, expected_service_data) tuples — maybe 30–50 rows drawn from the operator's actual house entities + plausible follow-up chains. Run it as a pytest parametrised test against a mocked Ollama that records prompts but returns canned JSON.

---

## 6. Current Production State (session end, 2026-04-19)

**Image:** `ghcr.io/synssins/glados-docker:latest` @ `734e1f7`.

**Known working (audit-verified this session):**
- `"The office is too dark. Can you adjust the desk lamp up a little bit?"` → Tier 2 → `light.turn_on brightness_pct:55` on `light.office_desk_monitor_lamp`. ✅
- `"Turn the desk lamp down by half"` → Tier 2 → `light.turn_on brightness_pct:25`. ✅
- `"The brightness of the office desk monitor lamp has been reduced to half of its previous level"` — visible speech, no trailing vocative leak. ✅

**Known not yet verified live (deployed but not retested):**
- `"Increase the brightness by ten percent"` as a follow-up → should hit Tier 2 via carry-over with synthetic prior-entity injection. Morning retest target.

**Known still broken / unaddressed:**
- Voice-satellite `source_area` wiring — hook exists, satellites don't populate it.
- Activity-as-context (`"movie time"` → sets media context for subsequent commands). Today's carry-over only threads entity/service, not activity.
- Synonym maps (`overhead` / `ceiling`) are single-map in `DisambiguationRules.overhead_synonyms`; no general synonym registry.
- Plural vs singular token matching in coverage scoring (`"lamps"` vs `"Lamp"`).

---

## 7. Commits This Session

| SHA | Summary |
|-----|---------|
| `31748a7` | Tier 2 service_data + vocative strip + initial DB-backed carry-over |
| `5da099b` | Compaction preserves per-row metadata + dynamic chat box |
| `e3df8de` | Extend stopwords with direction/quantity modifiers |
| `4736c27` | Hard qualifier-tight filter (superseded) |
| `c408a36` | Replace with soft boost + area-match hook + LLM-visible coverage signal |
| `734e1f7` | In-memory carry-over cache + assume_home_command + prior-entity injection |

Tests: 341 at session start → 374 at session end (+33). All passing.

---

## 8. Resume Prompt (morning)

```
I'm resuming Stage 3 Tier 2 disambiguation work from yesterday.
Read docs/disambiguation-research.md first — it documents the
full chain of fixes attempted this session and the open
architectural question.

Container image 734e1f7 is live on 192.168.1.150. Retest
sequence on the webui chat:

  1. "Turn the desk lamp down by half"
     → expect Tier 2 execute, brightness_pct ≈ 25, lamp dims
  2. "Increase the brightness by ten percent"
     → expect Tier 2 execute via carry-over + prior-entity
        injection, brightness_pct bumps upward on the SAME
        desk lamp

If step 2 still fails, check docker exec glados tail -f
/app/logs/audit.jsonl for the intent row — should show
tier=2 result="ok:execute" with service_data populated.

Main architectural question still open: should we move to a
context-as-first-class-input model (§5.4) and/or semantic
embeddings (§5.3) rather than layering more hand-tuned score
bonuses? The soft-boost design landed this session handles the
filed P0s but does not inherently scale to the
"I would like to read" + "from the living room" + area inference
+ activity context composition the operator described in the
original question.

Priority probably:
  1. Retest step 2 above; audit what the disambiguator actually
     saw in the prompt (it has a `Follow-up context:` section
     when carry-over fires).
  2. Build the evaluation harness (§5.6) — ~30 labelled tuples.
  3. Decide on §5.4 vs §5.3 as the next architectural move.
```

---

## 9. Files Touched This Session

| Path | Role |
|------|------|
| `glados/intent/disambiguator.py` | service_data, vocative strip, soft ranking, prior injection |
| `glados/intent/rules.py` | (unchanged, referenced) |
| `glados/ha/entity_cache.py` | stopwords, coverage/area scoring, CandidateMatch fields |
| `glados/ha/ws_client.py` | (unchanged, referenced) |
| `glados/core/api_wrapper.py` | carry-over cache, plumbing, clear-on-chat |
| `glados/core/conversation_store.py` | underscore metadata stamping |
| `glados/core/conversation_db.py` | per-row metadata preservation, latest_assistant_tier_exchange (kept for observability) |
| `glados/persona/rewriter.py` | `strip_trailing_vocative` made public |
| `glados/webui/tts_ui.py` | dynamic chat-messages CSS |
| `tests/test_disambiguator.py` | service_data cases, vocative tests, carry-over + injection tests |
| `tests/test_ha_entity_cache.py` | stopword coverage, soft ranking tests |
| `tests/test_home_command_carryover.py` | in-memory cache behaviour |
| `tests/test_multi_turn.py` | tier-metadata-through-compaction regression test |
