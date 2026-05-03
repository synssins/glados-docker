# 2026-05-03 — Chat-path truncation investigation (CLOSED)

**Status:** ROOT CAUSE IDENTIFIED AND FIXED. Commit `d2e638d` on `main`
ships the fix; `c3435fb` reverts the debug instrumentation. Deployed
to image SHA `e4ea26eb6b15…` on the docker host. Verified with 3
tool-using chat trials — all complete with terminal punctuation.

**Root cause:** Round-2's stream loop in `_stream_chat_sse_impl`
(`glados/core/api_wrapper.py`) was missing the `think_state["tail"]`
flush at end-of-stream that round-1 already had (lines 2611-2629).
The think-tag filter `_filter_think_chunk` holds back the last
`_MAX_TAIL_LEN` (12) chars of the buffer between chunks so it can
detect a `</think>` / `</thinking>` / `</reasoning>` tag that splits
across chunk boundaries. Without an end-of-stream flush, those last
12 chars stranded in the buffer never reached the user — producing
the mid-sentence truncation on tool-using chat turns (which go
through round-2).

Symptom was specific to tool-using turns because plain chitchat
goes through round-1 only, which already had the flush.

This document is preserved for the audit trail. The next-step probes
section was the actual investigation script — P1 + P2 were combined
in commit `dace14b` (later reverted in `c3435fb`).

---

## Original document (pre-resolution) — preserved for the audit trail

**Status when handoff written:** truncation cause NOT yet identified. Multiple
fixes shipped that addressed adjacent issues (real bugs found and corrected),
but the operator-visible mid-word truncation on tool-using chat turns persists.
This document records what is established by evidence, what I retract as
unsupported, and concrete next-step probes.

---

## Operator-visible symptom

User sends a tool-using chat query (e.g. "Suggest a movie from my library
that I should watch"). Chat completes round-1 with a tool_call to
`mcp.*arr Stack.radarr_get_movies`, dispatches the tool, receives the
result, then round-2 emits the final response — but **the visible content
truncates mid-word or mid-sentence** at unfortunate positions:

* "...with a file size of 7.65 GB. It's a popular choice for its
  suspenseful storyline and critical acclaim. Would you like more"
  (no terminal punctuation; sentence cut)
* "...The studio is Paramou" (mid-word "Paramou[nt]")
* "...immersive storytelling and cinematography. Would you like to add it
  to your queue or explore o" (mid-word "o[ther]")
* "...gripping historical " (trailing space, sentence incomplete)

Reproducible across many trials (~67-100% truncation rate on tool-using
turns). Chitchat turns (no tools) complete cleanly with terminal
punctuation under the same `personality.model_options`.

---

## Hard facts established (do not re-prove)

### F1. Container chat-path has two HTTP request sites
Same function `glados/core/api_wrapper.py::_stream_chat_sse_impl`:
* Round-1 connect at line ~2173 (`LLM upstream connecting:` log)
* Round-2 connect at line ~2840 (`_c2 = _http.HTTPConnection(...)`)

### F2. Both rounds use `_apply_streaming_options` helper (single source of truth)
File: `glados/core/api_wrapper.py`. Helper added in commit `f0dd3f0`. Both
round-1 and round-2 call the same helper, mutating their respective payload
dicts. Translation rules:
* On `/v1/` paths: emit OpenAI-spec top-level fields
  (`temperature`, `top_p`, `top_k`, `max_tokens`, `repetition_penalty`)
* On non-`/v1/` paths: keep the Ollama-style `options` dict shape

### F3. Both rounds received identical params, verified live
Captured via temporary `logger.success(...)` debug log (commit `79b9152`,
reverted in `cad23a8`). Live capture from a real chat turn:
```
17:57:32 _apply_streaming_options: path=/v1/chat/completions  max_tokens=1024 temperature=0.85 top_p=0.9 top_k=None repetition_penalty=1.0 options_kept=False  ← round-1
17:57:51 _apply_streaming_options: path=/v1/chat/completions  max_tokens=1024 temperature=0.85 top_p=0.9 top_k=None repetition_penalty=1.0 options_kept=False  ← round-2
```
Both calls are functionally identical at the param-translation layer.

### F4. Container's actual round-2 truncates at fewer tokens than direct replay
Captured request `917918cf` (2026-05-03 19:35 UTC):
* Container's round-2 diag: `prompt_tokens=3034 completion_tokens=219`
  finish_reason='stop' visible_chars=266
  Last visible: `'...Would you like to add it to your queue or explore o'`
* Round-2 payload dumped to `/app/logs/round2_payload_917918cf_*.json`
  (12070 bytes) via temporary debug code in
  `_stream_chat_sse_impl` (since reverted).

### F5. Direct replays of the captured 12070-byte payload always COMPLETE
Multiple test runs against `http://192.168.1.75:11434/v1/chat/completions`:

| Library | Runs | tokens | finish | last_token |
|---|---|---|---|---|
| `urllib.request` | 3 | 256 each | stop | `?` |
| `http.client` (container's library) | 3 | 256 each | stop | `?` |
| Sequential round-1 + round-2 from same proc | 1 | 241 (r2) | stop | `?` |
| Cold round-2 alone after the seq | 1 | 309 | stop | `?` |

All replay outputs end with **terminal punctuation** ("?"). All are
"complete" sentences. None reproduce the container's 219-token
"...explore o" mid-word truncation. Variance in length (241–309) suggests
OpenArc has some state-dependent sampling, but never produces mid-word
EOS in these external probes.

### F6. OpenArc traps documented in `feedback_openarc_upstream_gaps.md`
* `OpenAIChatCompletionRequest` model has no `response_format`,
  `chat_template_kwargs`, or `do_sample` — Pydantic silently drops them.
  See `src/server/models/requests_openai.py` lines 23-39.
* `temperature=0.0` crashes the OV-GenAI worker (divide-by-zero in
  sampler when `do_sample=true` is the model default + temp=0).
  Worker auto-unloads with no auto-reload — recovery requires
  `POST /openarc/load`.
* `finish_reason="stop"` is HARDCODED at `src/server/routes/openai.py:273`
  (`finish_reason = "tool_calls" if tool_call_sent else "stop"`). It does
  NOT distinguish max_tokens-cap from EOS-token-emission from any other
  stop cause. **Treating finish_reason='stop' as "model emitted EOS" is
  unsupported and was a critical error in earlier reasoning.**

### F7. Real bugs found and shipped during this investigation
* `a341bad` — round-1's payload was sending Ollama-style `options` dict
  (silently dropped by all OpenAI-compat backends including OpenArc).
  Fixed: translate to OpenAI-spec fields on `/v1/` paths.
* `4825f5f` — round-2's payload was missing all sampling params entirely.
  Fixed.
* `f0dd3f0` — refactored both translation sites into `_apply_streaming_options`
  helper (single source of truth).
* `35a3b47` — bumped command-lane `num_predict` 512 → 1024 (the 512
  was a holdover assuming a small qwen2.5-coder model on `llm_commands`,
  but that slot is currently the same Qwen3-30B as interactive).
* `f1c5a5f` — added `max_tokens` kwarg to `glados/autonomy/llm_client.py`
  + memory note about the temperature=0 trap.
* Personality.yaml model_options now: `temperature=0.85, top_p=0.9,
  num_ctx=16384, repeat_penalty=1.0` (operator-tuned, settled after
  Options 1+2 testing).
* Personality.yaml preprompt has a "completion rule" entry (helps
  chitchat; doesn't reach command-lane tool turns).

---

## What I claimed without evidence and retract

> "The truncation is a model quality issue."

The evidence I cited at the time was `finish_reason='stop'` and
`completion_tokens < cap`. Per **F6**, OpenArc hardcodes `finish_reason="stop"`
whenever `tool_call_sent=False`, so it does not distinguish EOS from
max_tokens from anything else. And `completion_tokens < cap` is consistent
with many causes other than "the model emitted EOS naturally."

The direct-replay evidence (**F5**) actively contradicts the claim — when
the captured payload is replayed verbatim, OpenArc always emits a complete
response ending in proper punctuation. So the model can and does generate
complete sentences for this prompt; the mid-word truncation only occurs
in the container's actual chat-flow context.

**Retract this claim. Do not propagate it.** The operator (correctly)
called this out as an unsupported assertion.

---

## Hypotheses for the actual root cause (NOT YET PROVEN)

State each as a hypothesis. Do not act on any without per-hypothesis evidence.

### H1. OpenArc has request-history-dependent state
The container's container's round-1 immediately precedes round-2 (~0.3s
gap). My external probes don't match that state. Sampling outcome may
depend on OpenArc internals (KV cache, RNG, batch slot) that vary based
on request history.

**Counter-evidence:** The sequential round-1+round-2 replay from the
same external Python process produced a complete 241-token response.
Mimics the container's flow externally and still completes. Doesn't
fully rule out the hypothesis (process boundary may matter), but
weakens it.

### H2. Subtle byte-level difference between captured payload and what was actually sent
The capture happens after `_b2 = json.dumps(_p2).encode("utf-8")` but
before the actual `conn.request("POST", ..., body=_b2, ...)`. If `_b2`
or `_p2` is mutated in between, the capture diverges from the wire.

**No evidence either way yet.** Need byte-hash comparison.

### H3. HTTP-level differences
Chunked-encoding, keep-alive, TCP socket state, header set differences
between `http.client` from container vs external. If OpenArc/uvicorn
treats some of these differently, sampling could vary.

**Indirect counter-evidence:** Same library (http.client) from external
process always completes. So pure library code path isn't the difference.

### H4. The container's chat-stream RELAY is dropping the tail
The `round-2 diag` log reports `chunks=218, completion_tokens=219` —
they match. So if chunks were being dropped, the diag would also reflect
the drop. UNLESS the diag is computed from the same dropped stream.

**Need:** byte-tap on what OpenArc sends INTO the container vs what
appears in the diag. If they differ → relay dropping. If identical →
issue is upstream of the relay.

### H5. Concurrent autonomy-agent traffic affects state
Saw in the log: `mcp.*arr Stack.radarr_search (autonomy)` calls firing
in the background during operator chats. Autonomy agents make concurrent
LLM calls. OpenArc may serve these via the same worker pool; concurrent
requests may interfere.

**Need:** test with autonomy disabled vs enabled, compare truncation rate.

---

## Things to NOT assume (lessons from this session)

1. **`finish_reason='stop'` proves nothing** when the upstream is OpenArc.
   It's hardcoded. Don't infer model behavior from it.
2. **`completion_tokens < cap` doesn't mean "model chose EOS naturally."**
   Could be cap, could be stream cut, could be stop sequence.
3. **Identical byte counts don't prove identical bytes.** Hash before
   asserting equivalence.
4. **External probes succeeding doesn't mean "system is fine."**
   The operator-visible failure is reproducible; the probe just
   establishes one boundary of where it isn't.
5. **My conclusions reached too early were wrong.** Verify before
   asserting cause. The operator's pushback was correct and well-deserved.

---

## Next-step probes (in order, cheapest first)

### P1. Byte-hash verification
**Question:** Are the bytes captured in `/app/logs/round2_payload_*.json`
byte-identical to what was sent on the wire?

**Probe:** Add a log line after `_b2 = json.dumps(_p2).encode("utf-8")` that
emits `hashlib.sha256(_b2).hexdigest()` AND `len(_b2)`. Capture from a
real turn. Compute the same hash on the dumped file. Compare.

**If hashes match:** H2 is dead. Move to P2.
**If hashes differ:** Find the divergence. Likely candidate: file dump uses
`open(..., "w", encoding="utf-8").write(_b2.decode("utf-8"))`, which
re-encodes through Python's str layer. Switch to `open(..., "wb").write(_b2)`
for true byte capture.

### P2. Capture the container's incoming response stream
**Question:** Are OpenArc's response chunks the same when the container
sends round-2 vs when an external script sends the same bytes?

**Probe:** In `_stream_chat_sse_impl` round-2 stream loop (~line 2849),
add: append every line of `_r2.readline()` to a debug file at
`/app/logs/round2_response_<request_id>.ndjson` BEFORE any parsing or
content-extraction. Also dump the request bytes. Ship both. Run a chat.

Then externally: replay the captured request bytes with `http.client`,
saving the response stream to a file the same way. Diff the two response
streams byte-for-byte.

**If identical:** Issue is between the response-stream and the user. Find
where in the relay tokens are being dropped (visible_chars accumulator,
SSE forwarding logic, chunking buffer).
**If different:** OpenArc is responding differently to functionally-same
input. Continue with P3.

### P3. Run the round-2 send from INSIDE the container
**Question:** Is the difference attributable to the container's process
environment (Python interpreter version, libs, network namespace,
threading state, OS-level TCP behavior)?

**Probe:** `docker exec glados python3 /tmp/replay_httpclient.py` (push
the same script we used externally into the container, run it from
inside). Capture the response. Compare with the container's actual
chat-path output AND with the external replay.

**If inside-container replay matches external replay (complete output):**
The issue is specific to the chat-path code at the time of the actual
turn — concurrent threading state, ordering, or some subtle race.
**If inside-container replay matches container's chat-path output
(truncated):** The issue is at the OpenArc connection state from the
container's network namespace. Continue with P4.

### P4. Disable autonomy traffic, retest
**Question:** Does concurrent autonomy-agent LLM traffic affect the
chat-path's outcomes via OpenArc shared state?

**Probe:** Set `GLADOS_AUTONOMY_ENABLED=false` (or whatever the
appropriate env var is — verify via grep) in compose, restart, run 3
trials. Compare truncation rate with autonomy on (current state).

### P5. Inspect OpenArc's worker queue behavior
Last resort. Read `src/server/worker_registry.py` and trace how requests
are queued. Look for any per-request-RNG-state, per-batch state, or
similar that could explain sampling-outcome divergence between concurrent
contexts.

---

## What's deployed right now (clean state)

* Image SHA: latest after `cad23a8` deploy (no debug code)
* `glados/core/api_wrapper.py` has the `_apply_streaming_options` helper
  and round-1 and round-2 both call it. No temp debug.
* Personality.yaml: `temperature=0.85, top_p=0.9, num_ctx=16384,
  repeat_penalty=1.0`. Preprompt has the operator-acknowledged completion rule.
* Truncation behavior: OPERATOR-VISIBLE, ~67-100% on tool-using turns.

---

## Resolution (added 2026-05-03 evening)

### How the probes played out

**P1 (byte-hash verification) — H2 dead.**
- Commit `dace14b` added P1+P2 instrumentation: hash + binary-mode dump of
  `_b2` at the wire boundary, plus binary-mode dump of OpenArc's full
  response stream.
- Triggered chat: "Suggest a movie from my library that I should watch
  tonight." Request id `b116182c`.
- Wire bytes: 16253 bytes, sha256 `4de9f8eb…`. Re-hashing the dumped
  binary file matched exactly. Bytes preserved by the binary dump.
- → H2 (subtle byte difference) is dead.

**P2 (response-stream byte capture) — H4 alive in a specific form.**
- OpenArc's actual streamed response: 55909 bytes, 247 SSE lines,
  ending with three content tokens `" request"`, `" now"`, `"?"` then
  `finish_reason: "stop"` + `[DONE]`. **Complete sentence, terminal
  punctuation.**
- Container's diag: `visible_chars=224 raw_chars=1201 done_seen=True
  finish_reason='stop'`. Operator-visible output ended at `"…retry the "`.
- → OpenArc sent the full reply; the relay dropped exactly 12 trailing
  characters (`request now?` = 12 chars = `_MAX_TAIL_LEN`).

**P3 (in-container replay) — confirmed not network/lib.**
- Replayed `round2_b2_b116182c.bin` from inside the container against
  OpenArc 3 times: all produced 1696 visible chars, complete sentences.
- Same network namespace as the live chat-flow round-2, same library,
  same bytes — and the replay completes cleanly.
- → H3 (HTTP-level differences) is dead.

### Root cause

`_filter_think_chunk` (api_wrapper.py:2349) holds back the last
`_MAX_TAIL_LEN` (12) chars of the buffer between chunks so it can detect
a `</think>` / `</thinking>` / `</reasoning>` tag that splits across chunk
boundaries.

Round-1's stream loop flushes that tail at end-of-stream (lines 2611-2629
inside `if done:`). Round-2's loop did not — `if _done2: break` jumped
straight to teardown without flushing. So up to 12 trailing visible chars
were stranded in the filter and never reached the user.

This is precisely why the symptom was tool-using-chat-specific: only
those go through round-2.

### Fix (commit `d2e638d`)

Mirrored round-1's flush block into round-2's `if _done2:` branch.
25 lines added; no other changes.

### Verification (post-deploy, image SHA `e4ea26eb6b15…`)

Three tool-using chat trials, each via `127.0.0.1:18015` from inside the
container with `X-GLaDOS-Origin: webui_chat`:

| Trial | Prompt | Last 80 chars | Terminal punct |
|---|---|---|---|
| 1 | "Suggest a movie from my library that I should watch tonight." | "…strong choice for a cinematic experience. Would you like details about other titles?" | ✅ `?` |
| 2 | "What TV shows are on my Sonarr server?" | "…seriesId\`. Would you like to search for a particular show?" | ✅ `?` |
| 3 | "Recommend something fun to watch from my movie library." | "…critique films. Let me know if you'd like details about either!" | ✅ `!` |

Pre-fix: 224 chars cut mid-word at `"…retry the "`. Post-fix: 213/271/376
chars all ending in proper terminal punctuation.

Round-2 diag for trial 1: `visible_chars=211 raw_chars=840 done_seen=True
finish_reason='stop'`. Round-2 diag for trial 3: `visible_chars=374
raw_chars=1608 done_seen=True finish_reason='stop'`. Visible matches
captured output exactly.

### Hypotheses status

- **H1** (request-history-dependent state) — moot. The "longer external
  replay" was sampling variance (Qwen3 at temp=0.85 produces variable-
  length output); not state-dependence.
- **H2** (byte difference) — dead. P1 confirmed bytes preserved exactly.
- **H3** (HTTP-level differences) — dead. P3 in-container replay completes.
- **H4** (relay drop) — confirmed in a specific form: not full-chunk drop,
  but a 12-char tail strand in the think-filter at end-of-stream.
- **H5** (concurrent autonomy traffic) — moot. Not the cause.

### Captured probe data

Local copies preserved at `scripts/probe_data/`:
- `round2_b2_b116182c.bin` — wire request bytes (16253 bytes)
- `round2_resp_b116182c.bin` — OpenArc's full response stream (55909 bytes)
- `external_replay_trial{1,2,3}.bin` — three external replays of the
  request bytes against OpenArc directly

These are .gitignore territory — not committed. Useful if a future
investigation needs to re-test against captured payloads.

---

## What is NOT a path forward

* Tweaking sampling params further. Already validated that none of
  `temperature` 0.7→0.85, `repeat_penalty` 1.0→1.1, `top_p` 0.9→1.0,
  `max_tokens` 256→1024 reliably fix it. The cause isn't sampling-param
  related; it's something at the system level.
* Adding more system-prompt rules. Already tried (command-lane completion
  rule, persona preprompt completion rule) — at best partial, at worst
  introduced refusal-mode behavior (Trial-1 of the command-rule test).
* Switching to a different model. May or may not fix it; doesn't address
  the underlying mystery of why direct probes complete and container
  calls don't.
