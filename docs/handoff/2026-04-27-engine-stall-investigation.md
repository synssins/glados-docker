# Engine stall investigation (2026-04-27)

## Symptom

Chat through `https://glados.example.com:8052/api/chat` AND
through `:8015/v1/chat/completions` hangs for the full
`api_wrapper._response_timeout` (180 s after the timeout fix)
and never produces an assistant message. The api_wrapper logs
`Timeout after 180s` and returns 504.

The WebUI chat tab shows `LLM 0.0s · Total 0.2s` with empty
GLaDOS reply, because the WebUI stops streaming at its own
response timeout (separate from the api_wrapper timeout) and
emits a fallback "no reply" placeholder.

## What's known

### LM Studio backend is healthy

Direct calls to LM Studio (`http://aibox.local:11434/v1/chat/completions`)
from the Docker host succeed in:
- 3.1 s for non-streaming chat with content fitting in default max_tokens
- 5.4 s with max_tokens=600
- ~5 s for tool-call requests (returns structured `tool_calls`)
- Streaming returns 133 chunks (131 reasoning + 1 content + DONE) — verified

These are the baseline tests in `2026-04-27-lmstudio-baseline-tests.md`.

### `lms ps` shows `STATUS: IDLE` during chat-hang

While the api_wrapper is waiting 180 s for the engine to produce
a response, LM Studio reports both models as IDLE — **meaning no
inference is running there**. Either:

- The engine isn't dispatching the chat to the upstream LLM at all; OR
- The upstream call completed in milliseconds (perhaps with empty/
  null response that the parser dropped silently), and the engine
  is now stuck in a non-LLM phase.

### Autonomy is hammering LM Studio with broken parsing

Container logs show continuous:
```
WARNING | glados.autonomy.llm_client:llm_call:98 - LLM call: unexpected response format
```

This is `glados/autonomy/llm_client.py:98` returning None because
the response shape didn't match either OpenAI (`choices[0].message.content`)
or Ollama (`message.content`) expected lookups. With GLM-4.7-Flash
returning content="" (when reasoning consumes the budget) and the
real text in `reasoning_content`, the autonomy client sees the
non-empty `reasoning_content` field as "unexpected" and discards.

Autonomy retries on a tick — likely 5-10 background calls per
minute. Each call burns one of LM Studio's parallel slots
(parallel=4 per model, four shared between chat + autonomy).

### The earlier 45 s timeout was masking the bigger issue

Before commit `31763e9`, api_wrapper timed out at 45 s. Now at
180 s. The engine takes the full 180 s either way — confirming
the engine never produces a response, not that responses are
just slow.

### `submit_text_input` returns True

The api_wrapper logs `[<request_id>] Submitted (muted): <text>`
when it pushes the user message into the engine queue. We see
this log, so the message IS being accepted. The problem is
downstream of submission.

### `_get_engine_response` polls for an assistant message via `store.version`

```python
# api_wrapper.py:923-944
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    if store.version > version_before:
        messages = store.snapshot()
        for i in range(len(messages) - 1, search_start - 1, -1):
            msg = messages[i]
            if msg.get("role") == "user" and msg.get("content","").strip() == text.strip():
                for j in range(i + 1, len(messages)):
                    mj = messages[j]
                    if mj.get("role") != "assistant" or not mj.get("content"):
                        continue
                    if mj.get("_source") == "autonomy":
                        continue
                    return response_text, request_id
```

So a response is detected when the conversation store version
bumps AND there's an assistant message with `content` that's NOT
tagged as autonomy. Two ways this fails:

1. **Engine never writes any assistant message for this user
   turn.** Store version doesn't bump or only bumps for autonomy
   messages.
2. **Engine writes a message but with empty `content`.** The
   `if not mj.get("content")` guard skips it.

Hypothesis (2) is consistent with the streaming parser dropping
all reasoning chunks and the single content chunk possibly being
mis-routed somewhere in the engine's emit path.

## Hypotheses (ranked)

### H1 — Streaming parser drops reasoning, emits no content to TTS pipeline, engine never gets a "complete sentence" to commit to the conversation store

**Why plausible:** the engine writes assistant messages from the
TTS pipeline (`_process_sentence_for_tts`). That function
batches received content into sentence-bounded flushes. If the
content channel emits exactly one short token like "alive" via
one chunk, AND the post-stream cleanup at line 1024-1025
(`_process_sentence_for_tts(sentence_buffer)`) doesn't fire
because something earlier broke the loop, the assistant message
never gets written.

**How to confirm:** add DEBUG logging at:
- `_process_chunk` line 358 — log `delta.keys()` for every
  non-tool chunk
- `_extract_thinking_standard` — log when entering/leaving
  thinking mode
- The post-stream cleanup at line 1022-1025 — log
  `len(sentence_buffer)` and `tool_calls_buffer`

Then run a chat and inspect logs for what actually happened.

### H2 — Autonomy starvation: LM Studio's 4 parallel slots are perma-occupied by autonomy retries

**Why plausible:** with 5 autonomy agents × retry-on-failure
loop × every-tick frequency, parallel slots could fill up faster
than they drain. Chat call queues at the LM Studio side and the
TCP request hangs waiting for a slot.

**How to confirm:**
- Snapshot LM Studio's running-request count at a few points
  during a chat hang (`lms ps` will show GENERATING vs IDLE)
- Temporarily disable autonomy agents (set
  `glados_config.autonomy.enabled=false` if such a knob exists,
  or comment out the autonomy loop) and try a chat
- If chat works without autonomy, this is H2

**Counterpoint:** during the latest chat hang, `lms ps` showed
both models IDLE. So at least at the snapshot moment, no
inference was running. Either autonomy intermittently saturates
and chat slipped through but got dropped, OR H2 is wrong and
the engine never even attempted the call.

### H3 — `_api_lock` deadlock or contention

**File:** `glados/core/api_wrapper.py:3573` —
`with _api_lock: response_text = _get_engine_response_with_retry(...)`

The api_wrapper serializes API requests with a process-level
lock. If a previous request is still holding the lock (e.g. an
autonomy-or-WebUI call that errored mid-flight without releasing),
new chats wait forever.

**How to confirm:** add a DEBUG log when acquiring/releasing
`_api_lock`. If "acquired" appears but "released" doesn't, this
is H3.

### H4 — `submit_text_input` returns True but the message gets routed to the wrong engine path

**Why plausible:** there's evidence of an autonomy lane and a
chat lane in the engine. If the engine's submit accepts the
message but enqueues it into a queue that's not being drained
(e.g. autonomy busy-waiting blocks chat queue), this fits.

**How to confirm:** trace `glados.submit_text_input` — what
queue does it enqueue to? Where's the consumer?

## Recommended dig order for next session

1. Add DEBUG logging at the four points listed under H1.
2. Run one chat. Capture logs.
3. If logs show the LLM call IS happening but no content
   buffered: H1 confirmed → fix streaming parser to handle
   `reasoning_content`.
4. If logs show no LLM call at all: pivot to H3 / H4. Add
   `_api_lock` acquire/release logging and submit_text_input
   trace.
5. If logs show the LLM call returns content but it's
   `_source="autonomy"`-tagged: that's the cross-talk fix at
   line 946 — the message IS being written but skipped by the
   filter. Investigate why chat-lane messages get autonomy
   tag.

## Don't-do list

- Don't bump LM Studio's parallel slots above 4 to "fix"
  autonomy starvation — the GLaDOS-side fix (autonomy parsing
  reasoning_content correctly so it stops retrying) is the
  right answer.
- Don't disable autonomy permanently — that's a workaround,
  not a fix. Temporarily off as a diagnostic is fine.
- Don't add GLM-specific code anywhere. If the parser needs to
  know about reasoning_content, it does so for ALL OpenAI
  reasoning models, not flagged on model name.

## Files most likely to need touching

| File | What |
|---|---|
| `glados/core/llm_processor.py:344-368` | `_process_chunk` — add `reasoning_content` handling |
| `glados/core/llm_processor.py:560-648` | `_extract_thinking_standard` — alternative entry point for separate-channel reasoning |
| `glados/core/llm_processor.py:920-1025` | streaming loop — log around chunk processing, sentence flushing, post-stream cleanup |
| `glados/autonomy/llm_client.py:73-99` | non-streaming response parser — accept `reasoning_content` |
| `glados/core/api_wrapper.py:3573` | `_api_lock` — add acquire/release logging if H1 doesn't pan out |
| `glados/core/engine.py` | `submit_text_input` plumbing if H4 needs investigation |
