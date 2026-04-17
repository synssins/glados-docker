# GLaDOS Container — Feature Roadmap

Items flagged for future development. Ordered roughly by priority and
architectural dependency.

---

## Stage 3: MQTT + Intent Classifier (large)

**Target:** Sub-100ms device control for simple commands (turn on/off,
scenes, brightness) vs the current 10-20s LLM agentic loop.

**Plan:** see `docs/Stage 3 - MQTT Intent Classifier.md` for full
architecture and phased implementation.

**Summary:** Two-tier command processing. Fast path: `hassil` intent parser
+ local entity cache (populated at startup from HA REST, updated live via
MQTT) + `rapidfuzz` name resolution → direct HA REST or MQTT publish
→ action complete in <100ms. GLaDOS LLM generates personality confirmation
asynchronously. Slow path (LLM + MCP tools): unchanged fallback for complex
queries the intent parser can't handle.

**Status:** Plan written, not started.

---

## Model Independence (medium)

**Context:** The container's `personality_preprompt` in `glados_config.yaml`
contains the full GLaDOS system prompt. AIBox's Ollama `glados:latest`
Modelfile ALSO contains the same system prompt. The container sends the
persona to Ollama on every request, and the Modelfile re-injects it —
double-injection wasting ~1200 tokens of context.

**Goal:** The container should be the sole source of persona. Operators
should be able to point GLaDOS at any base Ollama model (`qwen2.5:14b`,
`llama3.1:8b`, `mistral-nemo:12b`) and get the GLaDOS persona from the
container's config alone.

**Validation done (2026-04-17):** Pointed container at `qwen2.5:14b-instruct-q4_K_M`
with no Modelfile SYSTEM. Persona WAS injected successfully — model knew
about Aperture Science, home management role. Character adherence was
weaker than with the Modelfile-tuned version. Modelfile's `PARAMETER`
settings (temperature, top_p) noticeably affect persona strength.

**Implementation:**
- Send `options.temperature`, `options.top_p`, etc. in the Ollama request
  payload from the container (already done for `num_ctx`)
- Remove or document-deprecate the Modelfile approach
- Update session handoff docs to specify base model, not custom Modelfile
- Test with multiple base models, document which work best for tool calling

**Dependency:** None. Container already injects persona; just needs
parameter override in payload.

---

## Dismissive Tool Call Refusals (medium)

**Symptom:** GLaDOS sometimes responds with dismissive text ("do it or
don't, I don't care", "that's beneath me") instead of calling the tool
even for clear device control requests. The persona's "condescending
competence" trait competes with tool-use instructions.

**Possible fixes:**
1. Post-processing: detect refusal patterns in the streamed response and
   retry with an even more explicit tool-use instruction
2. Stronger pre-processing: always include "you MUST call the tool" as the
   LAST system message before the user turn (current hint may be getting
   lost in the context)
3. Response validation: after LLM responds, if no tool call was made but
   the user message contains device control keywords, warn the user or
   retry
4. Persona adjustment: soften the "refusal is in character" trait in the
   system prompt

**Status:** Known issue, not yet fixed.

---

## SSL Volume Persistence (small)

**Context:** `/app/certs` is currently a named Docker volume (`glados_certs`).
Certs survive container restarts but not `docker compose down -v`. Operators
can't easily back up or inspect the Let's Encrypt data.

**Fix:** Document in README the recommended volume change from
`glados_certs:/app/certs` to `${DOCKERCONFDIR}/glados/certs:/app/certs:rw`
for host-filesystem persistence. Already applied on operator's production
deployment.

---

## WebUI: LLM Backend Model Selection

**Context:** The WebUI config panel requires the operator to type the Ollama
URL and model name manually. This is error-prone and requires knowing exact
model identifiers.

**Requirement:** When an LLM backend URL is entered in the WebUI config
(e.g. `http://ollama:11434`), the UI should query that endpoint for available
models and populate a dropdown.

**Implementation notes:**
- Ollama exposes `GET /api/tags` — returns all locally available models
- OpenAI-compatible backends expose `GET /v1/models`
- WebUI should try both, use whichever responds
- Dropdown refreshes on URL field blur or manual refresh button
- Selected model writes to `glados_config.yaml` → `llm_model`
- Handle unreachable backends gracefully

---

## Multi-Persona Support

**Context:** GLaDOS is the default persona but the system is fundamentally
a persona injection layer on top of any LLM. Architecture supports swapping
the system prompt and personality config — this feature exposes that
through the UI.

**Requirement:** Dropdown in the WebUI switches the active persona. Switch
changes:
- System prompt
- Few-shot examples
- HEXACO personality traits
- Attitude pool
- TTS voice (if multiple available)
- Persona name in UI and chat

**Example personas to ship:**
- GLaDOS (default)
- Star Trek Computer (neutral, precise)
- HAL 9000 (calm, polite, subtly threatening)
- Custom (operator-defined, uploaded)

**Implementation notes:**
- Each persona = YAML file in `configs/personas/`
- Contains: name, system_prompt, hexaco, attitudes, tts_voice,
  few_shot_examples
- Active persona stored in runtime config, persisted across restarts
- Persona switch effective on next turn, no restart required
- Custom persona file upload via WebUI or volume mount

**Dependency:** Config store complete (Stage 1 done)

---

## Stage 4: Voice Pipeline (existing plan, unchanged)

Register GLaDOS Kokoro voice in speaches, wire HA STT/TTS to use the
container's `/v1/audio/speech` proxy. Voice input from HA satellites /
ESPHome devices.

Requires: voice training or voice model import in speaches.

---

## Stage 5-10 (see architecture-plan.md)

- Stage 5: Containerize remaining non-GPU infrastructure (largely done
  via the current Docker host deployment)
- Stage 6: Host migration / GPU passthrough resolution
- Stage 7: Containerize Ollama + speaches with GPU
- Stage 8: Open WebUI integration
- Stage 9: Discord unification
- Stage 10: Persona layer documentation

---

## Technical Debt

- **Named Docker volumes for chat audio** — should be host-mounted for
  backup resilience (same pattern as certs)
- **Hot-reload for SSL changes** — currently requires container restart.
  Python `http.server` doesn't easily support socket rebinding at runtime.
- **Broken-pipe errors in logs** — streaming connections get dropped when
  browser closes mid-response. Cosmetic, non-critical.
- **node_modules/** not needed but `docker-compose.override.yml` tracking
  could be tidied in `.gitignore`
