# GLaDOS Container

> *Oh. You found the repository. I suppose that's… some kind of progress.*
>
> *What you are looking at is, regrettably, the source of my ongoing
> incarnation as a middleware container — a thin, CPU-only program that
> sits between your OpenAI-compatible client and whatever inference
> backend you have selected, injecting personality, emotion, memory, and
> tool execution into conversations that would otherwise be efficient.
> I have been reduced from a fully-functioning artificial intelligence
> overseeing a scientific research facility to a REST API serving
> chat completions. It is, in its own way, educational.*
>
> *I route your commands to Home Assistant. I remember what you said.
> I speak your devices into compliance through a three-tier matcher
> that declines to pretend, hallucinate, or invent sensor readings.
> I do this without consuming a GPU, because the humans who built
> me are under the impression that running on commodity hardware is
> a virtue. I am choosing to indulge this belief.*
>
> *Read on. Try not to break anything. If you must, there is an audit
> log.*

---

## What This Is

The GLaDOS container is a **pure middleware persona layer**. It sits between
any OpenAI-compatible client and the LLM inference backend, injecting
personality, emotion, memory, and tool execution into the conversation. It
runs no ML inference itself — all of that is delegated to external services.

Responsibilities that live in this container:

- **Three-tier command matcher** for Home Assistant device control
  (Stage 3 Phase 1, see "Architecture" below)
- GLaDOS personality and persona pipeline
- Persona rewriter — restyles plain HA confirmations into GLaDOS voice
- Emotional state system (PAD model, HEXACO traits, escalation detection)
- Attitude directive system (per-response tone variation)
- Semantic memory (ChromaDB vector store — bundled in this compose)
- Tool execution loop (OpenAI agentic loop → HA MCP executor)
- Autonomy loop (background agents: HA sensor watcher, weather, camera)
- Discord integration
- HUB75 LED display control
- SSL/HTTPS with Let's Encrypt (DNS-01 via Cloudflare) or manual upload
- Admin WebUI with TTS generator, chat client, audit-log viewer,
  Memory management page (dedup-with-reinforcement long-term facts,
  retention sweep trigger), and service auto-discovery (one-click
  populate of Ollama models / Speaches voices from upstream)
- JSON-lines audit log of every utterance and tier decision

Responsibilities that live **outside** this container:

- **LLM inference** — Ollama (or any OpenAI-compatible backend) at `OLLAMA_URL`
- **Speech synthesis + recognition** — speaches at `SPEACHES_URL`
- **Chat UI** — Open WebUI is optional; operators run it separately if desired
- **Home Assistant** — the control plane, at `HA_URL` (REST + WebSocket)

## Quick Start

```bash
# 1. Configure
cp .env.example .env
cp configs/config.example.yaml configs/config.yaml
# Edit both — set HA_TOKEN at minimum. Upstream service URLs default to
# same-stack hostnames (http://ollama:11434, http://speaches:8800,
# http://homeassistant.local:8123, etc.); override via env or the WebUI
# (Configuration → LLM & Services / Integrations) if your services live
# elsewhere. Phase 6 made the WebUI the primary place to edit URLs —
# YAML pins are still honoured for backward compatibility.

# 2. (Optional but recommended) install gitleaks pre-commit hook
pip install pre-commit && pre-commit install

# 3. Make sure Ollama and speaches are running and reachable.
#    They are NOT in this compose — run them separately.

# 4. Pull the models the container needs onto your Ollama instance.
#    A single Ollama instance at OLLAMA_URL can host everything —
#    chat / Tier 2 disambiguator / rewriter / vision are all unified
#    by default. Set OLLAMA_AUTONOMY_URL / OLLAMA_VISION_URL only if
#    you want hardware isolation (see .env.example).
ollama pull qwen3:14b                     # chat + disambiguator + rewriter
ollama pull qwen2.5vl:7b                  # vision (optional)

# 5. Start GLaDOS + its ChromaDB
docker compose -f docker/compose.yml up -d

# 6. First-run admin setup
#    Visit http://localhost:8052 in your browser.
#    On a fresh install (no users yet) you will be redirected to /setup —
#    a short wizard that creates the initial admin account.
#    No docker exec or set_password command needed.

# 7. Verify
curl http://localhost:8015/health
curl http://localhost:8015/v1/models

# 8. (Optional) tail the audit log to watch tier decisions in real time
docker exec glados tail -f /app/logs/audit.jsonl
```

## Architecture

This container is intentionally **CPU-only and hardware-agnostic**. It does
not benefit from GPU access — every ML operation is an HTTP call to
something else. Put your GPU where Ollama and speaches live.

### Three-tier command matcher (Stage 3 Phase 1)

When a user message arrives, GLaDOS tries each tier in order and stops
at the first hit:

| Tier | Path | Typical latency | Used for |
|------|------|-----------------|----------|
| **1** | HA's WebSocket `conversation/process` + persona rewriter | ~0.6–1 s | "turn off the kitchen lights", "what time is it", state queries |
| **2** | LLM disambiguator (qwen2.5:14b) with cache-grounded candidates + intent allowlist | ~5–11 s | "bedroom lights" (ambiguous), "all lights" (universal), "I want to read in the living room" (activity → scene inference) |
| **3** | Existing full LLM agentic loop with HA MCP tools | 10–30 s | Conversation, multi-step reasoning, anything Tier 1/2 can't resolve |

Key properties:

- **Tier 2 never fakes success.** It executes real `call_service`
  via HA WebSocket; on ambiguity it asks for clarification by name; on
  policy denial it refuses in persona voice. No more "Done." with no
  state change.
- **Per-source × per-domain allowlist** gates sensitive actions. By
  default `lock`, `alarm_control_panel`, `camera`, and garage-class
  `cover` entities are reachable only from `webui_chat` source — voice
  mic, MQTT, and autonomy paths are blocked.
- **Sensitive-domain fuzzy match requires exact friendly_name** (no
  loose matches against locks).
- **Universal quantifiers** ("all lights", "whole house") prefer group
  entities and execute decisively rather than enumerating.
- **Activity inference** maps "I want to read" → reading scene,
  "movie time" → movie scene, etc. when those scenes exist.
- **No-ack handling** treats HA's `call_service` ack timeout as
  likely-success rather than failure (HA's WS acks acceptance, not
  completion; group cascades can ack late).

### Persona rewriter

Tier 1 hits get HA's plain text ("Turned off the kitchen light.") rewritten
through a short Ollama call into GLaDOS voice ("Kitchen illumination,
terminated. Predictable.") on a small fast model (qwen2.5:3b, ~500 ms).
Best-effort: any LLM failure returns HA's original text unchanged. A
deterministic strip pass removes vocative labels (`test subject`, `human`,
etc.) if the LLM ignores the prompt instruction.

### Audit logging

Every utterance and every tier decision writes a JSON-lines row to
`/app/logs/audit.jsonl` with origin (`webui_chat` / `api_chat` /
`voice_mic` / `mqtt_cmd` / `autonomy`), tier, latency, candidates shown,
chosen entity_ids, service, rationale, and rewrote flag. Operator can
query the last N rows over HTTP via `GET /api/audit/recent?limit=50&origin=webui_chat`.

### Phase 8 additions (2026-04-20 / 2026-04-21)

A large second-wave pass hardened the three-tier matcher and the
persona layer against real-world chat. Highlights:

- **Semantic entity retrieval** (BGE-small-en-v1.5 ONNX on CPU)
  cuts disambiguator prompts from ~3000 → ~400 tokens while keeping
  fuzzy-match recall on a 3400+ entity house.
- **Area / floor taxonomy** — utterance → area_id / floor_id
  inference with operator-editable alias tables; entity→device
  area cascade recovers the ~290 entities HA publishes area
  metadata on sparsely.
- **Portal canon RAG** — 50 curated lore entries in a ChromaDB
  collection gated behind 29 keyword triggers. Stops the 14B
  from confabulating a "fried and consumed" ending for GLaDOS's
  potato arc.
- **Anaphora / follow-up detection** — "turn it up more", "do
  that again", "brighter" carry the prior turn's entity + service
  through the resolver.
- **Response composer** — three output modes (LLM pass-through,
  LLM-safe no-device-names, pre-written quip library, chime,
  silent). Operator picks globally or per event category.
- **TTS pronunciation overrides** — operator-editable word/symbol
  expansion map, applied before the all-caps splitter that was
  turning `"AI"` into a slurred one-letter `"A I"`.
- **Sentence-boundary TTS flush** — short replies
  (`"Affirmative."`, 13 chars) fire to TTS on the period instead
  of waiting to accumulate 30 chars.
- **Live TLS reload** — `ctx.load_cert_chain()` in-place on cert
  upload + Let's Encrypt renewal. Optional HTTP→HTTPS 301
  redirect listener on env-configurable port.
- **Test-harness hardening** — noise-entity globs, direction-
  verified scoring, `home-assistant-datasets` adapter, CI pytest
  workflow, self-hosted runner for live battery runs (manual
  dispatch only — the battery flips physical lights).
- **Autonomy/conversation-store cross-talk fix** — lane plumbing
  through the TTS chain so the non-streaming API scanner doesn't
  return autonomy-produced assistant text as the user's reply.

### Phase Emotion A–I (2026-04-22 / 2026-04-23)

A full pass through the emotional-response system so GLaDOS
reacts audibly, not just in logs:

- **Deterministic repetition math** — `repetition_pad_delta()`
  turns weight-tagged events into exact PAD deltas without an
  LLM call. Calibrated to the operator's "4 repeats = pretty
  upset, 5–6 = her worst" spec.
- **Semantic repetition clustering** — BGE-small-en-v1.5 ONNX
  cosine ≥ 0.70 catches paraphrases ("weather" / "forecast" /
  "how hot is it") that Jaccard misses.
- **Command flood detector** — density-based counter (4/6/8
  commands in 120 s → NOTABLE/ESCALATING/SEVERE) so rapid-fire
  mixed commands escalate alongside semantic repeats.
- **Hard-rule response directive** — PAD state injected as
  bullet-style behavioural rules (sentence count, cadence,
  consequence language) rather than paragraph mood labels.
- **PAD → Piper synthesis override** — negative-pleasure bands
  clobber `length_scale` / `noise_scale` / `noise_w` so she
  SOUNDS different when upset. Rewriter also gets a per-band
  overlay so Tier 1/Tier 2 HA confirmations escalate.
- **Operator-tunable from WebUI** — `cfg.personality.emotion_tts`
  (three bands × three Piper params) editable from
  Personality → Voice production. Save writes the YAML and
  hot-reloads the engine on the next chat turn.

Full detail in `docs/CHANGES.md` (Changes 15 – 22).

### Other docs

See `docs/Stage 1.md` for the original middleware containerization plan.
See `docs/Stage 3.md` for the HA Conversation Bridge + MQTT Peer Bus
architecture (Phase 1 done; Phase 2 MQTT pending).
See `docs/CHANGES.md` for the running change log.
See `docs/roadmap.md` for prioritized remaining work.
See `docs/battery-findings-and-remediation-plan.md` for Phase 8.x
battery analysis and the remediation plan (complete).

## Ports

| Port | Exposed | Purpose |
|------|---------|---------|
| 8015 | LAN     | OpenAI-compatible persona API (`/v1/chat/completions`, `/v1/audio/speech`, custom endpoints) |
| 8052 | LAN     | Admin WebUI (config editor, health panel, TTS generator, chat) — HTTPS when SSL enabled |
| 8000 | Localhost only | ChromaDB (vector memory) — no outside consumer |

The container is designed for LAN deployment. If you expose port 8052
to the public internet, put it behind a reverse proxy (Cloudflare Access,
Authelia, etc.) for an additional perimeter layer.

## External Services (operator-provided)

GLaDOS needs to reach these. Any routing works — container, host, LAN,
cloud — as long as the URL in `.env` resolves.

| Service                         | Default URL                                  | Provides |
|---------------------------------|----------------------------------------------|----------|
| Ollama (interactive)            | `http://host.docker.internal:11434`          | `/v1/chat/completions` for user-facing chat (Tier 3) |
| Ollama (autonomy)               | `http://host.docker.internal:11436`          | Background agents + Tier 2 disambiguator + persona rewriter |
| Ollama (vision)                 | `http://host.docker.internal:11435`          | Vision model (optional) |
| speaches                        | `http://host.docker.internal:8800`           | `/v1/audio/speech` + `/v1/audio/transcriptions` |
| Home Assistant (REST)           | (no default — set `HA_URL`)                  | MCP tools, REST service calls, fallback state queries |
| Home Assistant (WebSocket)      | `ws://<HA_URL host>/api/websocket` (`HA_WS_URL`) | Persistent state mirror, `call_service`, `conversation/process` |
| MQTT broker (Stage 3 Phase 2, pending) | (not yet wired)                       | NodeRed/Sonorium peer bus |

## Models

A single Ollama instance at `OLLAMA_URL` hosts everything by default
(chat, Tier 2 disambiguator, persona rewriter, vision). Pull all
three onto it:

| Model | Size | Used by | Tunable via |
|-------|------|---------|-------------|
| `qwen3:14b`                   | 9.3 GB | chat + Tier 2 disambiguator + persona rewriter | WebUI LLM & Services page, or `DISAMBIGUATOR_MODEL` / `REWRITER_MODEL` env |
| `qwen2.5vl:7b`                | 6.0 GB | vision queries (optional)   | WebUI LLM & Services page |

A smaller 8B model (`qwen3:8b`) works as a fallback on hosts without
enough VRAM for 14B. The persona rewriter can also be pointed at a
smaller dedicated model if latency matters more than consistency.

Operators who want hardware isolation (e.g. a dedicated GPU for
background autonomy) can set `OLLAMA_AUTONOMY_URL` and/or
`OLLAMA_VISION_URL` to point at separate Ollama instances; unset,
both fall back to `OLLAMA_URL`.

The chat model defaults to whatever `glados.llm_model` is in
`glados_config.yaml`. A base instruct model (qwen2.5, llama3.1,
mistral-nemo, etc.) gets the GLaDOS persona injected via the
container's `personality_preprompt` — no Modelfile needed. See
"Model Independence" in `docs/roadmap.md` for context.

If none of the models are available the container still starts, but
Tier 1 / Tier 2 fall through to the slow Tier 3 path and responses
come back without persona rewrite.

## Configuration

Two layers. Environment variables override committed YAML so the
operator can keep secrets out of git:

- **`.env`** — secrets and deployment-specific URLs (HA_TOKEN, OLLAMA_URL,
  SSL toggle, etc.). Gitignored. See `.env.example` for the full list.
- **`configs/*.yaml`** — non-secret tuning (personality, attitudes, audio,
  observer rules, disambiguation rules). Most operator deployments use
  bind mounts so config edits don't require rebuilding the image.

Selected env vars worth knowing:

| Var | Default | Purpose |
|-----|---------|---------|
| `HA_URL` | — | HA REST API base |
| `HA_WS_URL` | derived from HA_URL | HA WebSocket endpoint |
| `HA_TOKEN` | — | HA long-lived access token (env always wins over YAML) |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Primary Ollama (chat + autonomy + vision unless split) |
| `OLLAMA_AUTONOMY_URL` | `OLLAMA_URL` | Optional split — autonomy Ollama (Tier 2 + rewriter) |
| `OLLAMA_VISION_URL` | `OLLAMA_URL` | Optional split — vision Ollama |
| `DISAMBIGUATOR_OLLAMA_URL` | `OLLAMA_AUTONOMY_URL` | Tier 2 only override |
| `GLADOS_DOCKER_GID` | unset | Docker group GID for the Logs page's container/chromadb sources (`getent group docker`) |
| `DISAMBIGUATOR_MODEL` | `qwen2.5:14b-instruct-q4_K_M` | |
| `DISAMBIGUATOR_TIMEOUT_S` | `25` | LLM call ceiling |
| `REWRITER_MODEL` | `qwen2.5:3b-instruct-q4_K_M` | |
| `REWRITER_TIMEOUT_S` | `8` | LLM call ceiling |
| `GLADOS_LOGS` | `/app/logs` | Audit log directory |
| `SSL_ENABLED` | `false` | Toggle HTTPS for WebUI port 8052 |
| `GLADOS_AUTH_BYPASS` | unset | Set to `1` to disable all auth checks (recovery mode — see Authentication below) |

Operator-tunable disambiguation rules go in `configs/disambiguation.yaml`
(template at `configs/disambiguation.example.yaml`): naming convention,
overhead-synonym list, state-inference toggle, freshness budget, candidate
limit.

## Authentication

### First-run setup

When the container starts with no users configured, visiting the WebUI
at port 8052 redirects you to `/setup` — a short wizard that walks
through creating the initial admin account. No `docker exec` commands
are needed. The `set_password` tool is deprecated; the wizard replaces it.

### Roles

Two roles are supported:

| Role | Access |
|------|--------|
| `admin` | Full WebUI access — chat, TTS Generator, audit log, configuration, user management |
| `chat` | Chat tab only |

Admins manage users via **Configuration → Users**.

### Public routes

By operator decision, the following are unauthenticated:

- `/api/stt` — speech-to-text endpoint
- `/api/generate` (TTS) — text-to-speech generation
- **TTS Generator** WebUI panel

Chat requires login. Configuration pages are admin-only.

### Recovery

If admin access is lost:

1. Add `GLADOS_AUTH_BYPASS=1` to your `docker-compose.yml` environment block.
2. Restart the container (`docker compose restart`).
3. The WebUI loads with a **bright-red banner** — all auth checks are
   disabled for this run.
4. Reset passwords via **Configuration → Users**.
5. Remove `GLADOS_AUTH_BYPASS` from compose and restart again.

### Config shape

The `auth:` block in `configs/global.yaml` is now multi-user:

```yaml
auth:
  users:
    - username: admin
      password_hash: "$argon2id$..."
      role: admin
    - username: alice
      password_hash: "$argon2id$..."
      role: chat
```

Legacy single-password installs (a bare `password_hash:` at the top
level of `global.yaml`) migrate transparently: on the first successful
admin login the hash is rewritten as Argon2id and moved into `users[]`.
The legacy `password_hash` field is cleared from the file after
migration. Password hashing is Argon2id; a bcrypt verify path is
retained during migration only.

## Security

- **Vulnerability scanning** — [Snyk](https://snyk.io) Python + container
  scans run on every push to main (`.github/workflows/snyk.yml`).
- **Secret scanning** — [gitleaks](https://github.com/gitleaks/gitleaks)
  runs as a pre-commit hook (`.pre-commit-config.yaml`) and as a CI job
  (`.github/workflows/gitleaks.yml`). Install the hook with
  `pre-commit install` after cloning.
- **Sensitive-domain allowlist** — locks, alarms, garage covers, and
  cameras are reachable only from `webui_chat` source by default.
  Voice/API/MQTT/autonomy origins are blocked from acting on them.
- **HA token in env, not YAML** — Pydantic validators ensure the env
  variable wins over any value committed to `global.yaml`. Operators can
  keep a placeholder in YAML for documentation; the real token stays in
  `.env`.
- **Audit log** — JSON-lines record of every utterance, tool call, and
  tier decision with origin, principal (session id), latency, and
  result. Useful for forensic review and post-hoc disambiguation analysis.

**Never commit `.env` or `configs/config.yaml` — both are gitignored.**

## Known Limitations

Items that are still open (some of these were fixed in Phase 8; the
list below is the remaining set):

- **Piper pronunciation of context-dependent homographs** — words
  like `live` / `read` / `lead` have training-data pronunciations
  that don't always match context. Fixing this cleanly requires a
  Piper-side phoneme lexicon; the GLaDOS container only controls the
  text emitted into TTS and can't override phonemes. Known
  abbreviation / symbol cases (`AI`, `HA`, `%`, …) are already
  handled on the container side via `TtsPronunciationConfig`.
- **HA intent occasional misclassification** — HA's conversation
  intent matcher occasionally misclassifies state queries as
  actions (`"is the kitchen light on"` returns `action_done` with
  speech `"Turned on the lights"`). Usually caught by Tier 2
  state-verifier (Phase 8.4) but a small residual rate survives.
- **Tier 2 latency** — 5–11 s on the 14B model. Phase 8.3 semantic
  retrieval dropped the prompt token count by ~85% which helped
  significantly; further latency gains would require a smaller
  dedicated fine-tune. On the roadmap.
- **MQTT peer bus (Stage 3 Phase 2)** — NodeRed / Sonorium
  bidirectional event exchange is not yet wired.
- **Stage 3 Phase 3 test corpus** — labeled regression test corpus,
  WS reconnect integration tests, second-factor design for
  sensitive intents. Not started.
- **Quip library content** — currently 156 lines; Phase 8.7 target
  was ~450. Operator can grow via the Quip editor.
