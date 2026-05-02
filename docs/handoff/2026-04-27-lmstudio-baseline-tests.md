# LM Studio baseline tests (2026-04-27)

Repeatable tests confirming LM Studio + GLM-4.7-Flash + Qwen2.5-VL-3B
are operational. **Run these before debugging the GLaDOS middleware
side** — they bound where the bug can live. If any test below
regresses, the issue is on the LM Studio side, not GLaDOS.

All tests are network-callable — no AIBox shell needed. Run from
the Docker host (`docker-host.local`) or any LAN box with Python +
`requests`. Outputs include the wall-clock time observed
2026-04-27 17:04 UTC for reference.

## Pre-flight

```bash
# Both models loaded, parallel=4, ctx=4096:
ssh root@docker-host.local 'curl -s http://aibox.local:11434/v1/models | python3 -m json.tool'
```

**Expected:** `data` array contains `glm-4.7-flash` and
`qwen2.5-vl-3b-instruct`. `object: "list"`.

## Test 1 — `/v1/models` shape

```bash
curl -s http://aibox.local:11434/v1/models
```

**Expected JSON:**
```json
{
  "data": [
    {"id": "qwen2.5-vl-3b-instruct", "object": "model", "owned_by": "organization_owner"},
    {"id": "glm-4.7-flash",          "object": "model", "owned_by": "organization_owner"},
    {"id": "llama-3.2-1b-instruct",  "object": "model", "owned_by": "organization_owner"},
    {"id": "text-embedding-nomic-embed-text-v1.5", "object": "model", "owned_by": "organization_owner"}
  ],
  "object": "list"
}
```

**Observed wall:** <50 ms.

## Test 2 — `/api/tags` returns 404 (Ollama-native is intentionally not served)

```bash
curl -s -w "%{http_code}\n" http://aibox.local:11434/api/tags
```

**Expected:** body `{"error":"Unexpected endpoint or method. (GET /api/tags)"}`,
HTTP code `200`. (LM Studio returns 200 + JSON error body for
unknown endpoints rather than HTTP 404 — this is why the GLaDOS
discover function had to handle the shape mismatch.)

## Test 3 — Direct chat completion (non-streaming, GLM-4.7-Flash)

```bash
curl -s -X POST http://aibox.local:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"glm-4.7-flash",
    "messages":[{"role":"user","content":"Reply with one short sentence: ALIVE."}],
    "max_tokens":400,
    "temperature":0
  }'
```

**Expected response shape:**
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "glm-4.7-flash",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "<short sentence containing ALIVE>",
      "reasoning_content": "<chain of thought, several hundred chars>",
      "tool_calls": []
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": ~12,
    "completion_tokens": ~250-350,
    "completion_tokens_details": {"reasoning_tokens": ~250-330}
  }
}
```

**Observed wall:** **3.1 s** (no max_tokens cap reached) /
**5.4 s** at max_tokens=600. Reasoning chain is ~95% of completion
tokens.

**Critical observation for GLaDOS-side work:** GLM-4.7-Flash
**always emits reasoning before content** unless suppressed via
`chat_template_kwargs.enable_thinking=false`. We tested that flag —
LM Studio currently ignores it (chat template still emits `<think>`
prefix). For the GLaDOS contract this means: **the OpenAI response
shape includes the reasoning channel as a first-class field, not as
inline tags.** Any GLaDOS code that only consumes `message.content`
will see empty content if max_tokens cuts off during reasoning.

## Test 4 — Direct chat completion (streaming, GLM-4.7-Flash)

```bash
curl -sN -X POST http://aibox.local:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"glm-4.7-flash",
    "messages":[{"role":"user","content":"Reply with just the word ALIVE"}],
    "max_tokens":400,
    "stream":true
  }'
```

**Expected:** SSE stream where each event has the standard OpenAI
chunk shape, but the `delta` field carries one of:
- `delta.reasoning_content: "<token-or-fragment>"` — during reasoning
- `delta.content: "<token-or-fragment>"` — during the final answer
- `[DONE]` sentinel after `finish_reason: "stop"`

**Observed (2026-04-27):** 133 chunks total — **131 reasoning
chunks + 1 content chunk** (`"alive"`) + DONE. No chunk had both
fields populated. The two channels are mutually exclusive per chunk.

**This is the OpenAI extended streaming convention** (DeepSeek's
shape, adopted by LM Studio, vLLM reasoning models, mainline Ollama
0.14+). It is NOT a GLM-specific quirk. Any OpenAI-compliant
middleware should buffer `delta.reasoning_content` separately from
`delta.content`. GLaDOS today only consumes `delta.content` →
content arrives in 1 chunk per stream → loop should pick it up
unless something earlier short-circuits.

## Test 5 — Tool calling (GLM-4.7-Flash, OpenAI structured output)

```bash
curl -s -X POST http://aibox.local:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"glm-4.7-flash",
    "messages":[{"role":"user","content":"What is the weather in Paris right now? Use the tool."}],
    "tools":[{
      "type":"function",
      "function":{
        "name":"get_weather",
        "description":"Get the current weather for a city.",
        "parameters":{
          "type":"object",
          "properties":{"city":{"type":"string","description":"City name"}},
          "required":["city"]
        }
      }
    }],
    "tool_choice":"auto",
    "temperature":0,
    "max_tokens":800
  }'
```

**Expected (2026-04-27 actual):**
- `finish_reason: "tool_calls"`
- `message.tool_calls[0].function.name == "get_weather"`
- `message.tool_calls[0].function.arguments == "{\"city\":\"Paris\"}"` (JSON string)
- `message.content == ""` (cleared)
- `message.reasoning_content` populated (chain of thought about which tool to call)

**Wall:** ~5 s. Tool-call output is OpenAI-shaped. **Llama.cpp issue
#18808 ("broken for agentic use on Intel dGPUs") does not affect
this stack** — Vulkan + GLM + tool calling on B60 works as of
2026-04-27.

## Test 6 — Vision query against Qwen2.5-VL-3B (deferred — operator paused this)

Plan when re-enabled:

```bash
# Sample image from a known public URL
curl -s -X POST http://aibox.local:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"qwen2.5-vl-3b-instruct",
    "messages":[{
      "role":"user",
      "content":[
        {"type":"text","text":"Describe what you see in this image."},
        {"type":"image_url","image_url":{"url":"<public-image-url>"}}
      ]
    }],
    "max_tokens":300
  }'
```

**Expected:** OpenAI-shaped response with `message.content`
containing a description. Qwen2.5-VL doesn't have a reasoning
channel — content emits directly.

Not yet run as of 2026-04-27.

## Test 7 — Concurrent requests (parallelism check)

LM Studio reports `PARALLEL: 4` for both models (per `lms ps`). Two
concurrent chat calls + one vision call should all succeed without
the second/third request queuing past the first's completion time.

```bash
# Run 3 in parallel via background processes:
( curl -s -X POST http://aibox.local:11434/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"glm-4.7-flash","messages":[{"role":"user","content":"Say HELLO"}],"max_tokens":50,"temperature":0}' ) &

( curl -s -X POST http://aibox.local:11434/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"glm-4.7-flash","messages":[{"role":"user","content":"Say WORLD"}],"max_tokens":50,"temperature":0}' ) &

wait
```

**Expected:** both finish within ~the time of one — not 2× the
single-request time. If they serialize, parallel slots aren't
working as advertised, and that's a candidate root cause for
GLaDOS's "chat hangs while autonomy is busy" symptom.

Not yet run as of 2026-04-27.

## How `lms ps` should look mid-test

```
IDENTIFIER                MODEL                     STATUS         SIZE        CONTEXT    PARALLEL    DEVICE    TTL
glm-4.7-flash             glm-4.7-flash             GENERATING     18.13 GB    4096       4           Local        
qwen2.5-vl-3b-instruct    qwen2.5-vl-3b-instruct    IDLE           3.27 GB     4096       4           Local        
```

`STATUS: GENERATING` while a request is in flight; `STATUS: IDLE`
within ~5 s of completion. If `STATUS` stays IDLE while a chat is
allegedly being served — the request never reached LM Studio
(routing problem on the client side).

## Notes for the next session

- These tests are the **truth source for "is LM Studio healthy"**.
  Run Tests 1, 3, 4, 5 before any GLaDOS-side debugging.
- Test 4's chunk count distribution (131 reasoning : 1 content) is
  the empirical fact behind the GLaDOS streaming-parser fix:
  `delta.reasoning_content` chunks must be handled (or explicitly
  ignored) by the middleware, not silently dropped without
  acknowledging the channel exists.
- Vision (Test 6) and concurrency (Test 7) are owed but not
  blocking the current debug.
