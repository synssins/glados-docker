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

The GLaDOS container is a **self-contained persona + smart-home middleware layer**.
It bundles TTS, STT, embeddings, and vector storage internally — a single Docker
image is all you need to run. The only things that live outside it are the LLM
backend and optionally Home Assistant.

**Bundled inside the container:**

- **TTS** — local Piper (VITS) inference on CPU; voice is `glados.onnx`, baked into the image
- **STT** — local Parakeet CTC + Silero VAD on CPU
- **Embeddings** — local BGE-small-en-v1.5 ONNX for semantic retrieval
- **Vector store** — ChromaDB via `PersistentClient` at `/app/data/chromadb/` (in-process; no sidecar)
- **Persona pipeline** — GLaDOS personality, emotional state system (PAD/HEXACO), attitude directives
- **Three-tier command matcher** for Home Assistant device control (see "Architecture" below)
- **Persona rewriter** — restyles plain HA confirmations into GLaDOS voice
- **Tool execution loop** — OpenAI agentic loop → HA MCP executor
- **Autonomy loop** — background agents: HA sensor watcher, weather, camera
- **Discord integration**
- **HUB75 LED display control**
- **SSL/HTTPS** with Let's Encrypt (DNS-01 via Cloudflare) or manual cert upload
- **Admin WebUI** — TTS generator, chat client, audit-log viewer, Memory management,
  Personality editor, Configuration pages
- **JSON-lines audit log** of every utterance and tier decision

**External dependencies (the only things the container talks to outside itself):**

| Dependency | Required? | Purpose |
|------------|-----------|---------|
| Any **OpenAI-compatible LLM endpoint** (Ollama, LM Studio, vLLM, llama.cpp `llama-server`, …) | Required | Chat, Tier 2 disambiguator, persona rewriter, autonomy |
| **Home Assistant** at `HA_URL` + `HA_TOKEN` | Recommended | Device control, state queries, autonomy |
| Vision-capable LLM endpoint | Optional | Camera/image analysis; nothing breaks if absent |

URLs are configured as bare `scheme://host:port` — the container appends
`/v1/chat/completions`, `/api/chat`, `/v1/audio/speech`, etc. at dispatch
time. See `docs/models.md` for recommended models and a sample VRAM budget.

## Hardware Requirements

- **CPU only.** The container runs all inference (TTS, STT, embeddings) on CPU — no GPU
  required inside the container itself.
- **~700 MB** image size; **2–4 GB** resident memory in normal use.
- The LLM (Ollama) is what benefits from GPU — that runs on a separate machine or
  host-native. Any OpenAI-compatible endpoint works, any topology.

## Quick Start

```bash
# 1. Configure
mkdir -p glados && cd glados
curl -O https://raw.githubusercontent.com/synssins/glados-docker/main/.env.example
cp .env.example .env
# Edit .env — set OLLAMA_URL (required) and HA_URL+HA_TOKEN (recommended)

# 2. Make a chat-capable model available on your inference endpoint.
#    Either of these is a tested-good baseline; see docs/models.md for
#    full recommendations including a triage and vision model.
ollama pull qwen3:14b                          # original recommendation (Ollama)
# or, with LM Studio:
# lms load qwen3-30b-a3b -c 12288 --parallel 4 --gpu max

# 3. Pull and start GLaDOS
curl -O https://raw.githubusercontent.com/synssins/glados-docker/main/docker/compose.yml
docker compose -f compose.yml up -d

# 4. First-run setup
# Open https://localhost:8052 (self-signed cert; accept the warning).
# You'll be redirected to /setup — a wizard that creates the first admin
# account. No docker exec, no shell commands.

# 5. Verify
curl http://localhost:8015/health
```

## Deploy

The compose file below is also kept at `docker/compose.yml` in this repo.

```yaml
# docker/compose.yml — GLaDOS, single container.
#
# The container is self-contained: TTS (local Piper), STT (local Parakeet),
# embeddings (BGE), and ChromaDB all run inside. The only external services
# you need are an LLM (Ollama or any OpenAI-compatible endpoint) and
# optionally Home Assistant + a vision service.
#
# Required env (.env or shell):
#   OLLAMA_URL    LLM inference endpoint                      e.g. http://host.docker.internal:11434
#   HA_URL        Home Assistant base URL  (optional)         e.g. http://host.docker.internal:8123
#   HA_TOKEN      Home Assistant long-lived access token (optional, paired with HA_URL)
#
# Optional env:
#   TZ                       Timezone, default UTC
#   OLLAMA_AUTONOMY_URL      Override autonomy / disambiguator / rewriter endpoint
#   OLLAMA_VISION_URL        Override vision-call endpoint
#   VISION_URL               External vision service (e.g. http://host.docker.internal:8016)
#   GLADOS_DOCKER_GID        Host's docker group GID (enables WebUI Logs page; see comment below)
#
# Usage:
#   cp ../.env.example ../.env       # edit values
#   docker compose -f docker/compose.yml up -d
#
# To build from source instead of pulling :latest, uncomment the build: block
# and comment out the image: line.

services:
  glados:
    image: ghcr.io/synssins/glados-docker:latest
    # build:
    #   context: ..
    #   dockerfile: Dockerfile
    container_name: glados
    ports:
      - "8015:8015"   # OpenAI-compatible API
      - "8052:8052"   # Admin WebUI (HTTPS-capable; HTTP redirect on 8053 if WEBUI_HTTP_REDIRECT_PORT set)
    volumes:
      - glados_configs:/app/configs    # YAML config — first run creates defaults
      - glados_data:/app/data          # ChromaDB + conversation history + semantic memory
      - glados_audio:/app/audio_files  # TTS-generated audio + chimes
      - glados_logs:/app/logs          # audit JSONL + service logs
      - glados_certs:/app/certs        # Let's Encrypt cert/key (auto-managed) or operator uploads
      # Optional: read-only docker socket for the WebUI Logs page to tail
      # the GLaDOS container's own stdout. Comment out if you don't need it.
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - TZ=${TZ:-UTC}
    env_file:
      - ../.env
    extra_hosts:
      - "host.docker.internal:host-gateway"   # required on Linux Docker; harmless on Desktop
    # Grant access to the host's docker group for the Logs page (optional).
    # On Linux:  getent group docker | cut -d: -f3
    group_add:
      - "${GLADOS_DOCKER_GID:-0}"
    restart: unless-stopped

volumes:
  glados_configs:
  glados_data:
  glados_audio:
  glados_logs:
  glados_certs:
```

## Architecture

This container is intentionally **CPU-only and hardware-agnostic** for everything
it hosts internally. Put your GPU where Ollama lives.

### Three-tier command matcher (Stage 3 Phase 1)

When a user message arrives, GLaDOS tries each tier in order and stops
at the first hit:

| Tier | Path | Typical latency | Used for |
|------|------|-----------------|----------|
| **1** | HA's WebSocket `conversation/process` + persona rewriter | ~0.6–1 s | "turn off the kitchen lights", "what time is it", state queries |
| **2** | LLM disambiguator on the `llm_triage` slot (recommended: `llama-3.2-1b-instruct`; falls back to `llm_interactive`) with cache-grounded candidates + intent allowlist | ~1–11 s depending on model | "bedroom lights" (ambiguous), "all lights" (universal), "I want to read in the living room" (activity → scene inference) |
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
| 8015 | LAN     | OpenAI-compatible persona API (`/v1/chat/completions`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/models`, `/v1/voices`). HTTPS when SSL cert mounted, plain HTTP otherwise. |
| 8052 | LAN     | Admin WebUI (config editor, health panel, TTS generator, chat). HTTPS when SSL cert mounted, plain HTTP otherwise. |
| 5051 | LAN     | HA audio file server — serves WAV / startup announcement files for `media_player.play_media`. HTTPS when SSL cert mounted, plain HTTP otherwise. |
| 18015 | loopback | Internal plain-HTTP API for in-container callers (env `GLADOS_INTERNAL_API_PORT`). Bound to `127.0.0.1` only — not reachable from the LAN. |

The container is designed for LAN deployment. If you expose port 8052
to the public internet, put it behind a reverse proxy (Cloudflare Access,
Authelia, etc.) for an additional perimeter layer.

## OpenAI API Compatibility

Port 8015 speaks the OpenAI HTTP protocol. Drop-in clients (the
official `openai` SDK, LangChain, LlamaIndex, Open WebUI, …) work
without modification — point them at `http://<host>:8015` and pass
any model name; the persona pipeline overrides the upstream model
identifier anyway.

| Endpoint | Behavior |
|----------|----------|
| `POST /v1/chat/completions` | Streaming + non-streaming. Persona-rewritten, emotion-tinted, three-tier matched. Returns OpenAI delta chunks; tool calls stream through `delta.tool_calls`. |
| `POST /v1/audio/speech`      | TTS via local Piper. Returns `audio/wav` or `audio/mpeg`. |
| `POST /v1/audio/transcriptions` | STT via local Parakeet + Silero VAD. Returns the OpenAI `{text}` shape. |
| `GET /v1/models`             | Lists the persona-overlaid `glados` virtual model. |
| `GET /v1/voices` and `/v1/audio/voices` | Lists available Piper voices. |

**Streaming features:**

- **`stream_options.include_usage=true`** — when set on the upstream
  request, the terminal usage chunk's `prompt_tokens` /
  `completion_tokens` populate the `tokens_per_second` field in the
  WebUI metrics bar. This means Ollama-native and OpenAI-compat backends
  (LM Studio, vLLM) both surface throughput, even when the upstream
  doesn't emit Ollama's `eval_count` / `eval_duration`.
- **Tool-call deltas** stream alongside content deltas; the three-tier
  matcher dispatches to HA MCP and the follow-up tokens stream back.
- **`/no_think` injection** at the system layer for chitchat on
  Qwen3-family models drops TTFT from ~30 s to ~3 s with no client
  change.

**Backend URLs are bare `scheme://host:port`.** The container appends
the right path (`/v1/chat/completions`, `/api/chat`, etc.) at dispatch
time, so operators don't need to know which protocol path their backend
expects.

The container itself uses stdlib `http.client` against the upstream —
no OpenAI SDK is imported. Anything emitting canonical OpenAI SSE
chunks works as a backend; production runs to date use Ollama and LM
Studio.

### TLS for OpenAI-compat clients

The container's external ports (`8015`, `8052`, `5051`) speak plain
HTTP by default and TLS when SSL cert + key files are mounted at
`/app/certs/{cert,key}.pem` (or wherever `SSL_CERT` / `SSL_KEY` env
point). Same cert is used on every port — single source of truth at
`glados/core/tls.py`. The decision is on file presence, so:

| Operator setup | Port 8015 speaks | What an OpenAI client points at |
|----------------|------------------|---------------------------------|
| No cert mounted | plain HTTP | `http://<host>:8015/...` |
| Let's Encrypt cert + matching DNS resolves on the LAN | HTTPS, validates cleanly | `https://<cert-domain>:8015/...` |
| Self-signed cert | HTTPS but the client must trust the CA or skip-verify | `https://<host>:8015/...` with verify off |
| Bare-IP, no domain, no cert | plain HTTP | `http://<ip>:8015/...` |

The "bare-IP, no cert" path is the universal floor — works with zero
configuration. The "LE cert + DNS" path is the production target.

In-container callers (autonomy announce, doorbell screening, the
WebUI's streaming-chat connection) hit a separate plain-HTTP listener
on `127.0.0.1:18015` (env `GLADOS_INTERNAL_API_PORT`) so they're never
asked to validate the public cert against `localhost`.

## Models

The container is **backend-agnostic** — anything that speaks
`/v1/chat/completions` works. Each LLM role has its own slot in
`services.yaml`, configured independently in **System → Services**:

| Slot | Used by | Recommended model |
|------|---------|-------------------|
| `llm_interactive` | Tier 3 chat, persona rewrites, tool-call planning | `qwen3:14b` (Ollama) or `qwen3-30b-a3b` (LM Studio) |
| `llm_autonomy`    | Background autonomy loops (sensor watcher, weather, camera, news) | Same as `llm_interactive`, or split onto a dedicated GPU |
| `llm_triage`      | Tier 2 disambiguator, autonomy compaction, memory classifier | `llama-3.2-1b-instruct` (small + fast) or `qwen3:8b` |
| `llm_vision`      | Camera / image analysis (optional) | `qwen2.5vl:7b` or `qwen2.5-vl-3b-instruct` |

A single endpoint can host all four; operators with a dedicated GPU
per role can split them onto separate URLs. Unset slots fall back to
`llm_interactive`.

See [docs/models.md](docs/models.md) for VRAM math, throughput
numbers, and the trade-offs between the 14B and 30B chat options
including an LM Studio JIT-loader gotcha that can silently revert
context size.

If no LLM is available the container still starts, but Tier 1 / Tier 2
fall through to the slow Tier 3 path and responses come back without
persona rewrite.

## Configuration

Two layers. Environment variables override committed YAML so the
operator can keep secrets out of git:

- **`.env`** — secrets and deployment-specific URLs (HA_TOKEN, OLLAMA_URL,
  SSL toggle, etc.). Gitignored. See `.env.example` for the full list.
- **`configs/*.yaml`** — non-secret tuning (personality, attitudes, audio,
  observer rules, disambiguation rules). Managed as a Docker volume so
  first-run creates defaults without any pre-staging.

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
| `GLADOS_DOCKER_GID` | unset | Docker group GID for the Logs page's container source (`getent group docker`) |
| `DISAMBIGUATOR_MODEL` | `qwen3:14b` | |
| `DISAMBIGUATOR_TIMEOUT_S` | `25` | LLM call ceiling |
| `REWRITER_MODEL` | `qwen3:14b` | |
| `REWRITER_TIMEOUT_S` | `8` | LLM call ceiling |
| `GLADOS_LOGS` | `/app/logs` | Audit log directory |
| `SSL_ENABLED` | `false` | Toggle HTTPS for WebUI port 8052 |
| `WEBUI_HTTP_REDIRECT_PORT` | unset | If set, listen on this port and redirect HTTP → HTTPS |
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
are needed.

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

**Never commit `.env` — it is gitignored.**

## Known Limitations

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
- **Quip library content** — currently 156 lines; operator can grow via the Quip editor.
