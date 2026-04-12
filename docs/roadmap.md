# GLaDOS Container — Feature Roadmap

Items flagged for future development. Not in scope for current stages.
Ordered roughly by logical dependency, not priority.

---

## WebUI: LLM Backend Model Selection

**Context:** The WebUI config panel requires the operator to type the Ollama
URL and model name manually. This is error-prone and requires knowing exact
model identifiers.

**Requirement:** When an LLM backend URL is entered in the WebUI config
(e.g. `http://ollama:11434`), the UI should query that endpoint for available
models and populate a dropdown. The operator selects the model from the list
rather than typing it.

**Implementation notes:**
- Ollama exposes `GET /api/tags` which returns all locally available models
- OpenAI-compatible backends expose `GET /v1/models`
- The WebUI should try both endpoints and use whichever responds
- Dropdown should refresh when the URL field loses focus or on a manual
  refresh button click
- Selected model name writes back to `glados_config.yaml` → `llm_model`
- Must handle backends being unreachable gracefully (show error, don't crash)

**Dependency:** WebUI port complete (Stage 1, Step 1.6)

---

## Multi-Persona Support

**Context:** GLaDOS is the default persona but the system is fundamentally
a persona injection layer on top of any LLM. The architecture already supports
swapping the system prompt and personality config — this feature exposes that
capability through the UI.

**Requirement:** A single dropdown in the WebUI switches the active persona.
Changing the persona changes:
- System prompt (the full personality instruction block)
- Few-shot examples (voice/tone examples specific to that persona)
- HEXACO personality traits (emotional model tuning)
- Attitude pool (pre-response tone directives)
- TTS voice (if multiple voices are available — e.g. GLaDOS ONNX vs Kokoro)
- Persona name displayed in the WebUI and chat

**Example personas to ship with:**
- GLaDOS (Aperture Science AI — default)
- Star Trek Computer (LCARS-style, precise, neutral, no personality)
- Dalek (hostile, extermination-focused, rhythmic cadence)
- HAL 9000 (calm, polite, subtly threatening)
- Custom (operator-defined, loaded from a file)

**Implementation notes:**
- Each persona is a YAML file in `configs/personas/`
- Persona files contain: name, system_prompt, hexaco, attitudes, tts_voice,
  few_shot_examples
- Active persona is stored in runtime config, persisted across restarts
- Persona switch takes effect on next conversation turn — no restart required
- Custom persona file can be uploaded via the WebUI or volume-mounted
- The persona system replaces the current monolithic `personality.yaml` —
  that file becomes the GLaDOS persona file

**Dependency:** WebUI port complete (Stage 1, Step 1.6), config store
refactor complete (Stage 1, Step 1.3)

---

## Notes

- Both features above are WebUI-level changes with no impact on the API
  layer — the `/v1/chat/completions` endpoint behavior is unchanged
- Multi-persona support is the larger of the two — persona files need a
  schema definition and validation before implementation begins
- The TTS voice component of persona switching depends on how many voices
  are available in the deployed TTS backend (bundled ONNX vs speaches)
