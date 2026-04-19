# State-query handling — prompt-side research

**Date:** 2026-04-19
**Context:** Companion document to the resolver-side state-query fix
(commit where `_is_state_query` / `_STATE_QUERY_OPENERS` landed in
`glados/core/command_resolver.py`). That fix short-circuits utterances
starting with "what / is / are / who / why / when / where" to
fall-through **before** Tier 1/2 runs. This document records what a
deeper prompt-side improvement would look like, when/if that's
warranted.

## Current behavior after the resolver short-circuit

| Utterance | Path |
|---|---|
| "Are the lights on?" | Resolver → fall-through → Tier 3 agentic LLM → MCP tools → state answer |
| "What lights are on?" | Same |
| "Is the office light on?" | Same |
| "Why is it so bright" | Same — routes to Tier 3 |

The regex is deliberately broad. It *will* over-trigger on some
declarative phrasings ("Is that light supposed to be on? Turn it
off please"), sending them to Tier 3 when Tier 2 could have handled
them faster. Acceptable trade-off for now — Tier 3 can still execute
via MCP, just slower.

## When prompt-side work would be worth it

Three signals:

1. **Tier 3 latency becomes a UX problem for queries.** Tier 3 on
   the 14B model is 10–30 s. If state queries feel sluggish,
   teaching Tier 2 to *answer* queries (instead of deferring) would
   recover the 1–3 s budget.
2. **The over-trigger gets annoying.** If operators report that
   declarative turns starting with a question word are getting
   wrongly deferred, tightening the regex alone isn't enough —
   the prompt needs to make the decision instead.
3. **Tier 2 starts mis-handling queries despite the short-circuit.**
   If follow-ups ("Is it still on?") slip past the short-circuit
   (because the utterance has a prior-context dependency the opener
   regex can't see), Tier 2 would re-enter query-as-ambiguity mode.

None of those are in play right now. File this for when they are.

## What the prompt-side fix would look like

### Option A — add a `query` decision value to Tier 2's schema

`glados/intent/disambiguator.py:_build_prompt()` currently tells the
LLM its output schema is:

```
{
  "decision": "execute" | "clarify" | "refuse",
  "speech": "...",
  "entity_ids": [...],
  "service": "...",
  "service_data": {...},
  "rationale": "..."
}
```

Add `"query"` as a fourth decision value. On `query`, Tier 2 returns
`should_fall_through=True` without claiming the turn — effectively
the same outcome as the resolver-side short-circuit, but decided
after the LLM has seen the full context (including candidates and
state).

Changes needed:

- Schema line in the system prompt: document `"query"` as a valid
  decision with criteria like *"user is asking for a state readout,
  not requesting a change"*.
- One or two few-shot examples of query inputs + `query` outputs.
- `Disambiguator._run_decision_branch()` — add a branch that maps
  `decision=query` to a `DisambiguationResult(handled=False,
  should_fall_through=True, rationale="llm_classified_as_query")`.
- Audit: `tier=2, result="query"` so we can measure how often the
  LLM takes this path vs the regex short-circuit.

Estimated work: ~30 lines + prompt text + 2–3 unit tests with canned
LLM responses.

### Option B — Tier 2 answers the query directly using the candidate list

Riskier but possibly faster. On `decision=query`, Tier 2 returns
`handled=True` with `speech` synthesized from the candidate list
(entity states are already in the prompt under `state=on`/`off`).

Example:
```
User: "Are the lights on?"
Tier 2 candidates: Office Lamp (on), Living Room Lamp (on), ...
Tier 2 output:
  {"decision": "query",
   "speech": "Living room lamp, office lamp, and hallway are on.
              Everything else is off."}
```

Pros: sub-second query answers, no Tier 3 round-trip.

Cons:
- Candidate list is already filtered/truncated; the answer might be
  incomplete.
- State freshness is bounded by the `max_state_age_seconds` budget
  (default 5 s). Older cache could produce a stale answer.
- Persona: Tier 2 output goes through *without* the rewriter (only
  Tier 1 gets rewritten). Query answers may come out flatter than
  command confirmations.

Would want operator testing to confirm the speed win outweighs the
accuracy loss before shipping this.

### Option C — hybrid

Short-circuit (today) handles the common cases.
Option A handles the edge cases the regex misses.
Option B is the latency optimization, gated behind a config flag.

Recommended path: **ship the short-circuit, measure Tier 3 query
latency in the audit log, revisit Option A when there's evidence
it's needed.**

## Instrumentation we already have

Every `resolve()` emits an audit row. For state queries the row will
be:

```json
{"kind":"intent","tier":null,"result":"fall_through",
 "extra":{"rationale":"state_query", ...}}
```

and the subsequent Tier 3 audit row gives end-to-end latency. A
simple aggregator (`jq` over `audit.jsonl`) can report query
frequency + P50/P95 latency over any window — enough data to decide
whether to pursue Option A.

## Related CSV rows

The CSV cases that the short-circuit now routes to Tier 3:

- [60] `"What lights are on?"` — grouped state report
- [61] `"Are the lights on?"` — boolean-ish summary
- [62] `"Is the office light on?"` — specific-entity state
- [64] `"What lights are on?"` (duplicate phrasing)
- [84] `"Why is the kitchen so dark"` — implicit complaint; the
  rewrite prompt says "Favor action: turn on kitchen lights" — this
  is a case where the short-circuit defers to Tier 3, but the CSV
  expects action. Tier 3 can still execute via MCP, so the outcome
  is right even if the path is longer.
- [97] `"Is anyone home"` — presence query
- [115] `"Why is it so bright"` — implicit complaint variant

All currently route to Tier 3. Measure latency, iterate if needed.
