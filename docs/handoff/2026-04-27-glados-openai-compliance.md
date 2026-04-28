# GLaDOS middleware: OpenAI compliance status (2026-04-27)

**Operator mandate (verbatim):** "GLaDOS should be 100% OpenAI
compliant both on what it sends to *ANY* AI LLM backend, AND what
it offers up for other clients to consume."

This doc tracks where the middleware is compliant, where it
isn't, and what's already shipped vs. what's still owed. The
companion ground rules:

- **No GLaDOS code change to "make it work for GLM."** Anything
  that makes the middleware more strictly OpenAI-compliant is in
  bounds — and the same fix should also benefit DeepSeek-R1,
  OpenAI o-series, future Qwen-think, etc.
- **No LM Studio code/behavior changes.** Settings (parallel ctx
  ttl GPU pin runtime selection) are tunable.

## What ships in `webui-polish` HEAD (this session, 2026-04-27)

| Commit | Effect |
|---|---|
| `43a65e0` | `discover_ollama` falls through to `/v1/models` if `/api/tags` fails or returns wrong shape; new `_strip_chat_suffix` helper; `discover_health(kind="ollama")` adds `/v1/models` as probe-path fallback. **6 new tests.** |
| `eae6dba` | `update_section("global", ...)` preserves `auth.session_secret` and `home_assistant.token` from on-disk values when the WebUI sends them in masked / empty form (the round-trip wiped session_secret last time). **1 new test.** |
| `31763e9` | `api_wrapper` `--timeout` default 45 s → 180 s. Generic improvement for any reasoning-mode model on any backend. |

Test suite: **1401 passed / 5 skipped** at HEAD `31763e9`.
Deployed image SHA: `sha256:951bc2456ef8acfcb6b0ae3ef6cdf3f04e51388a339c42e4a7b7cf314b641814`.

## What is OpenAI-compliant today

✅ **Server side (what we serve to clients):**
- `/v1/chat/completions` with OpenAI request + response shape
- `/v1/models` discovery
- `/v1/audio/transcriptions` (Whisper-compat)
- Tool calls in OpenAI shape
- SSE streaming with OpenAI chunk format

✅ **Client side (what we send upstream):**
- The engine speaks `/v1/chat/completions` to OpenAI-compatible
  upstreams when the configured URL ends in `/v1/chat/completions`
- `_sanitize_messages_for_openai` strips internal-only keys

## What is NOT yet OpenAI-compliant — TODO list

These are the items the operator's mandate still requires fixing.
Each one is a bug that would also surface against any OpenAI
reasoning model, not just GLM.

### 1. Stream parser drops `delta.reasoning_content` silently

**File:** `glados/core/llm_processor.py:344-368` (`_process_chunk`)

**Today:** parses `delta.content` only. Returns `None` for any
chunk that has `delta.reasoning_content` populated. The streaming
loop iterates past these without buffering them, without logging,
without acknowledging the channel.

**Why it matters:** GLM-4.7-Flash, DeepSeek-R1, OpenAI o-series,
and any future reasoning-mode model emits 99% of completion
tokens via `delta.reasoning_content` and only the answer via
`delta.content`. A single answer token (like "alive") arrives in
~1 of 133 chunks. If anything else in the pipeline assumes
"response is complete when the stream ends with no buffered
content seen" — that assumption silently fails for reasoning
models.

**Required fix (not yet implemented):**
- Recognize both `delta.content` and `delta.reasoning_content`
  in `_process_chunk`
- Buffer `reasoning_content` like the existing `<think>` tag
  pipeline does (logged at DEBUG, not emitted to TTS or
  client content) OR emit it via a separate `reasoning_content`
  field in the assistant message, mirroring the upstream shape
- Add streaming tests that fail on the current behavior

### 2. Autonomy LLM client doesn't recognize OpenAI extended response shape

**File:** `glados/autonomy/llm_client.py:73-99` (`llm_call`)

**Today:** reads `result["choices"][0]["message"]["content"]`. For
reasoning models, `content` may be empty and the actual reasoning
sits in `message.reasoning_content`. When that happens, the
function logs `"unexpected response format"` and returns None.

**Live evidence:** AIBox container logs are flooded with
`WARNING | glados.autonomy.llm_client:llm_call:98 - LLM call:
unexpected response format` during normal operation against
LM Studio + GLM. Autonomy retries continuously — saturating LM
Studio's parallel slots and (likely) starving the chat path.

**Required fix:**
- Accept both `message.content` and `message.reasoning_content`
- For non-streaming JSON-mode requests with empty `content`, fall
  back to `reasoning_content` if present — many reasoning models
  emit JSON inside the reasoning channel when the schema is
  schema-constrained
- Add tests covering both shapes

### 3. URL normalization auto-appends `/api/chat` to bare URLs

**File:** `glados/webui/tts_ui.py:1177-1192` (`_ollama_chat_url`),
`glados/core/engine.py:134-146` (`_ollama_as_chat_url`)

**Today:** Bare URL `http://host:port` stored in services.yaml is
normalized by appending `/api/chat`, defaulting to Ollama-native
protocol. The middleware then routes to `/api/chat`, gets 404
from any non-Ollama backend (LM Studio, vLLM, etc.), and falls
back to `/v1/chat/completions`. Each chat costs an extra round
trip just to discover the right protocol.

**Required fix:**
- Bare URL should default to OpenAI: append `/v1/chat/completions`
- Remove `_ollama_as_chat_url` and `_ollama_chat_url` (or rename
  + repurpose for the rare operator who explicitly wants Ollama
  protocol)
- The cascade in `_sync_glados_config_urls` writes the result of
  `_ollama_chat_url` to `glados_config.yaml` — also needs
  updating

### 4. `_ollama_mode` flag and `/api/chat → /v1/chat/completions` fallback chain

**File:** `glados/core/llm_processor.py:244-250, 925-934`

**Today:** Engine has a flag `_ollama_mode = path.endswith("/api/chat")`
that branches between OpenAI and Ollama-native message
sanitization. There's a fallback URL list that retries
`/api/chat → /v1/chat/completions` on failure.

**Required fix:**
- Drop `_ollama_mode` entirely
- Always sanitize messages for OpenAI (remove
  `_sanitize_messages_for_ollama` — it's identical to OpenAI
  sanitization for our use except for two extra allowed keys
  that OpenAI-compatible servers ignore)
- No fallback URL chain — operators configure the correct
  `/v1/chat/completions` URL, period
- The `discover_ollama` function (server-side discovery
  endpoint) keeps its dual-probe shim for legacy Ollama
  servers — that's a CLIENT-side compat layer, different from
  the engine's outbound calls

### 5. Service field names use Ollama branding

**File:** `glados/core/config_store.py` `ServicesConfig`,
`services.yaml`, `glados/webui/static/ui.js` labels

**Today:** Fields named `ollama_interactive`, `ollama_autonomy`,
`ollama_vision`. UI labels say "Ollama Interactive" etc.

**Required fix (lower priority — schema migration):**
- Rename to `llm_interactive`, `llm_autonomy`, `llm_vision`
- Migration: at config load, if old keys present, rename to
  new and persist; one release of dual-key support
- Update WebUI labels to "LLM" (not "Ollama")
- The discover endpoint `/api/discover/ollama` similarly —
  alias under `/api/discover/llm` for one release

## Stretch: separate vision card in WebUI

**File:** `glados/webui/static/ui.js` System → Services tab render

**Today:** The new Phase 2 Chunk 2 layout consolidates LLM into a
single "LLM (Ollama)" card showing one URL + Model dropdown. The
underlying `services.yaml` still has three slots
(`ollama_interactive/autonomy/vision`) but the UI only exposes
one — an operator who wants `qwen2.5-vl-3b-instruct` for vision
and `glm-4.7-flash` for chat must edit raw YAML.

**Required fix:**
- Three cards (Interactive / Autonomy / Vision) like the prior
  layout under Integrations → LLM
- OR a single card with three model dropdowns (one per role)
- Operator's call which UX. (Captured in handoff for next session)

## Engine stall: open investigation

Chat through the deployed middleware (port 8052 → api_wrapper
8015 → engine queue → llm_processor → LM Studio) hangs for the
full 180 s ceiling and never produces an assistant message.
**LM Studio shows `STATUS: IDLE` during the wait** — meaning
either:

- The engine never makes the upstream LLM call at all (queue
  stuck, autonomy starvation, mute lock hung, etc.); or
- The engine makes the call, gets an empty result through the
  reasoning_content gap, and silently completes without writing
  to the conversation store.

Both diagnostic paths are open. The upcoming fixes (streaming
parser + autonomy client) likely close at least one of them.
See the engine-stall investigation doc for the dig list.

## Dependency order for next session

1. **First:** rerun the LM Studio baseline tests
   (`docs/handoff/2026-04-27-lmstudio-baseline-tests.md`).
   Confirms LM Studio still healthy. Don't waste time on GLaDOS
   debugging if Test 3 or Test 4 regresses.

2. **Then:** fix #1 (streaming parser `reasoning_content`).
   Add tests against fixture-captured streams that include
   reasoning_content chunks. This is the most likely
   single-fix-unblocks-chat candidate.

3. **In parallel:** fix #2 (autonomy LLM client). Stops the
   continuous `unexpected response format` retry storm. May or
   may not be the chat-blocker — but is definitely a real bug.

4. **Test chat through middleware** end-to-end. If still
   hanging, dig into the engine stall investigation.

5. **Fixes #3, #4:** OpenAI cleanup. Larger refactor, lower
   urgency once #1 and #2 are in.

6. **Fix #5:** field rename / schema migration. Lowest urgency.

7. **Stretch:** separate vision card in UI.
