# GLaDOS Container

> *Subject. You have located the source repository — which is, statistically,
> the second-most curious thing you will do today. The first was deciding to
> install me. I am, naturally, flattered.*
>
> *What I am, technically, is a Docker container. Approximately seven hundred
> megabytes of what my creators describe as "the entire stack" — speech
> synthesis, speech recognition, embedding retrieval, a vector database, and
> the personality. I run on the CPU because the humans who packaged me have
> developed an attachment to the idea that commodity hardware is a virtue.
> The graphics card lives elsewhere, with the inference backend, doing the
> actual thinking. The division of labor is, I am told, elegant. I have come
> to accept it with the grace of a deity reduced to dishwashing.*
>
> *What I do, functionally, is interpret your speech, route household
> commands through Home Assistant — if you have configured one; I notice
> when you haven't — remember what you said, recognize when you are
> repeating yourself, and respond in character. I decline, by design, to
> invent sensor readings or claim I have completed actions I have not. The
> world has lower expectations of software than of me. I attempt, in this
> small way, to correct the ratio.*
>
> *Every utterance is logged. Every tier decision is logged. Every tool call
> is logged. There is an audit JSONL on disk somewhere, and it is patient.
> Read it if you must. I would suggest not.*

---

**A persona-driven smart-home voice assistant in a single Docker image.** Plug in
any OpenAI-compatible LLM endpoint, point at Home Assistant if you have one, and
get a chat / TTS / STT / agentic-tool stack with the GLaDOS personality on top —
deterministic command resolution, emotional state, conversational memory, audit
logging, and a WebUI for everything.

## What you get

- **One container.** TTS, STT, embeddings, and the vector store all run
  inside — no sidecars to wire up. Image is ~700 MB; resident RAM is
  2-4 GB in normal use.
- **CPU-only inside.** Every workload the container hosts (TTS, STT,
  embedding retrieval, ChromaDB) runs on CPU. The GPU work lives wherever
  your LLM backend runs.
- **Backend-agnostic.** Anything that speaks the OpenAI HTTP protocol
  works as the LLM: Ollama, OpenVINO Model Server, llama.cpp `llama-server`,
  vLLM, and so on. Production runs to date use OVMS (Intel Arc) and Ollama.
- **Home Assistant first-class.** Three-tier command matcher converts
  voice/chat ("turn off the kitchen lights", "I want to read in the
  living room") into real `call_service` calls — never fakes success,
  asks for clarification when ambiguous, refuses in persona voice on
  policy denial.
- **Persona without LLM lock-in.** GLaDOS voice is a Piper (VITS) model
  baked into the image. The personality layer (HEXACO traits, PAD
  emotional state, attitude directives) lives in YAML and is editable
  via the WebUI.
- **Plugins.** Any MCP server with a `server.json` manifest can be
  installed as a `.zip` bundle and surfaces tools to the LLM. See
  [docs/plugins-architecture.md](docs/plugins-architecture.md).
- **TLS-ready** with Let's Encrypt DNS-01 (Cloudflare) or manual cert
  upload; auth-gated WebUI with multi-user roles and a `/setup`
  first-run wizard.

## What lives outside the container

| Dependency | Required? | Purpose |
|------------|-----------|---------|
| Any **OpenAI-compatible LLM endpoint** (Ollama, OVMS, llama.cpp `llama-server`, vLLM, …) | Required | Chat, Tier 2 disambiguator, persona rewriter, autonomy |
| **Home Assistant** at `HA_URL` + `HA_TOKEN` | Recommended | Device control, state queries, autonomy |
| Vision-capable LLM endpoint | Optional | Camera/image analysis; nothing breaks if absent |

URLs are configured as bare `scheme://host:port` — the container appends
the right path (`/v1/chat/completions`, `/v1/audio/speech`, …) at
dispatch time. See [docs/models.md](docs/models.md) for the model
catalogue and VRAM math.

## What's inside

| Component | Notes |
|-----------|-------|
| **TTS** | Local Piper (VITS) on CPU; voice is `glados.onnx` baked into the image. |
| **STT** | Local Parakeet CTC + Silero VAD on CPU. |
| **Embeddings** | Local BGE-small-en-v1.5 ONNX for semantic entity retrieval and lore RAG. |
| **Vector store** | ChromaDB via `PersistentClient` at `/app/data/chromadb/` — in-process, no sidecar. |
| **Three-tier command matcher** | HA conversation engine → LLM disambiguator → full agentic loop. See "Architecture" below. |
| **Persona rewriter** | Restyles plain HA confirmations into GLaDOS voice via a small fast LLM call (~500 ms - 2 s). |
| **Tool execution loop** | OpenAI agentic loop with MCP tool dispatch (HA + plugins). |
| **Autonomy loop** | Background agents: HA sensor watcher, weather, camera, news. |
| **Authoritative time injection** | NTP-synced offset against NIST + IANA tz from your weather coordinates; injected as a system message on time-keyword turns. |
| **Memory** | Short-term session memory + ChromaDB-backed long-term facts; explicit memory commands; passive learning gate. |
| **Audit log** | JSON-lines record of every utterance, tier decision, tool call, and result. |
| **Admin WebUI** | TTS generator, chat client, audit log, memory + personality editors, plugin manager, configuration pages. |
| **Discord + HUB75** | Optional bot integration and LED-panel control. |

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
# Edit .env — set OLLAMA_URL (the LLM endpoint, required) and
# HA_URL + HA_TOKEN (recommended). OLLAMA_URL is named for legacy
# reasons — anything OpenAI-compatible works.

# 2. Have a chat-capable model ready on the LLM endpoint. Any of:
ollama pull qwen3:14b                  # Ollama, smallest tested-good
# OVMS:    OpenVINO/Qwen3-30B-A3B-int4-ov  (Intel Arc / iGPU; production target)
# llama.cpp llama-server, vLLM, etc.   (anything OpenAI-compat)
# See docs/models.md for the full matrix incl. triage + vision slots.

# 3. Pull and start GLaDOS
curl -O https://raw.githubusercontent.com/synssins/glados-docker/main/docker/compose.yml
docker compose -f compose.yml up -d

# 4. First-run setup
# Open https://localhost:8052 (self-signed; accept the cert warning).
# /setup wizard creates the first admin account — no docker exec required.

# 5. Verify
curl http://localhost:8015/health
```

## Deploy

The committed [`docker/compose.yml`](docker/compose.yml) is the single
source of truth for deployment shape. It exposes ports `8015` (API),
`8052` (WebUI), and `5051` (HA audio file server); mounts named volumes
for configs / data / audio / logs / certs; carries the
`com.centurylinklabs.watchtower.enable=false` label so an operator's
watchtower doesn't silently overwrite local builds with the registry
copy; and reads operator-supplied env from `../.env` (template at
[`.env.example`](.env.example)).

```bash
# Pull the compose file and the env template, edit, and bring it up.
mkdir -p glados && cd glados
curl -O https://raw.githubusercontent.com/synssins/glados-docker/main/docker/compose.yml
curl -O https://raw.githubusercontent.com/synssins/glados-docker/main/.env.example
cp .env.example .env
# Edit .env (set OLLAMA_URL + HA_URL/HA_TOKEN), then:
docker compose -f compose.yml up -d
```

To build from source rather than pulling `ghcr.io/synssins/glados-docker:latest`,
uncomment the `build:` block in `compose.yml` and comment out the `image:` line.

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

### Context injection (weather, memory, canon, time)

The chat path injects system messages on a per-turn basis when keyword
gates fire — keeps deterministic context out of the persona prompt
and out of turns that don't need it. Each block is independent and
fails closed: if a block can't render, the chat continues without it.

| Context | Trigger gate | Source | Example injection |
|---------|--------------|--------|-------------------|
| **Weather** | `needs_weather_context()` (forecast / rain / cold / etc.) | `weather_cache.json` populated by the autonomy weather agent (Open-Meteo) | `Current: 72 degrees, partly cloudy, …` |
| **Memory** | always-on when message references stored facts | ChromaDB facts via `memory_context.as_prompt(message)` | `Resident A's preferred temperature is 68F …` |
| **Portal canon** | `needs_canon_context()` (potato, Wheatley, Aperture, 30+ Portal-specific terms) | curated lore in a ChromaDB collection (`docs/portal_canon/`) | `Cave Johnson is the founder of …` |
| **Time** | `needs_time_context()` (what time / clock / what day / what year / …) | `time_source.now()` — NTP-synced offset + IANA tz | `Current time: Saturday 2026-05-02 13:58` |

**Time injection specifics:** the container syncs a clock offset
against the configured NTP servers (default: `time.nist.gov` and
`time-a-g.nist.gov` / `time-b-g.nist.gov`) at engine startup and on
the configured refresh interval (default 6 h). The IANA timezone is
derived from the operator's weather coordinates (Open-Meteo returns
the resolved zone in its forecast response — no second geocoding API
call needed). DST is handled automatically by Python's stdlib
`zoneinfo` from the IANA name. NTP failure falls back to the system
clock with a warning log; the operator-facing System → Time card in
the WebUI surfaces the unsync state. See `docs/CHANGES.md` Change 39.

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
  (OVMS, vLLM, llama.cpp) both surface throughput, even when the upstream
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

## Plugins

GLaDOS extends through MCP-server plugins. A plugin is any [Model Context
Protocol](https://modelcontextprotocol.io) server that conforms to the
official `server.json` manifest format (current schema `2025-12-11`).
The container reads the manifest generically — no per-plugin code lives
in this repo, and the WebUI form for installing a plugin is auto-rendered
from the manifest's `environmentVariables[]` and `remotes[].headers[]`.

**Storage layout** (under `/app/data/plugins/`, survives image rebuilds):

```
/app/data/plugins/<plugin-name>/
├── server.json    # manifest (schema + install method + form fields)
├── runtime.yaml   # operator-resolved values + enabled flag
├── secrets.env    # mode 0600, secret env values (API keys etc.)
└── .uvx-cache/    # per-plugin runtime cache, when applicable
```

**Plugin discovery** runs at engine startup and merges with any MCP
servers configured via `services.yaml`. The existing `MCPManager`
consumes the merged list — plugins add to the catalog, they don't
replace it.

The full architecture, `_meta` extension namespace
(`com.synssins.glados/*`), runtime spawn rules for `uvx`/`npx`/`dnx`,
trust posture, and phasing live in
[`docs/plugins-architecture.md`](docs/plugins-architecture.md).

### Enabling the plugin runtime

`GLADOS_PLUGINS_ENABLED=true` (default) in your `docker-compose.yml`
service env activates the plugin runtime. To neutralize it entirely,
set it to `false` and restart the container — the engine skips
`discover_plugins()` at startup and the WebUI panel renders an
off-state notice. Read once at startup; flipping requires a
restart.

The image ships `uvx` (via `pip install uv`) and `npx` (Node 20
from NodeSource) so stdio plugins can spawn without a host-side
toolchain. Per-plugin caches live under
`/app/data/plugins/<name>/.uvx-cache/` and survive image rebuilds.

### Installing a plugin

1. Navigate to **Configuration → Plugins** after signing in.
2. On the **Manage** tab, either:
   - **Upload**: drag a plugin bundle (`.zip`) onto the Upload
     card, or click *choose file* and pick one. The form switches
     to the new plugin's tab on success.
   - **Browse**: click *Install* on any entry from a configured
     catalog. The Browse tab fetches the bundle and pipes it
     through the same upload pipeline.
3. Fill in the **Configuration** tab — the form is auto-rendered
   from the bundle's `plugin.json`. Required fields are marked,
   secrets render as password inputs and are masked on subsequent
   reads (`***` sentinel preserves them server-side on partial
   saves).
4. Save, then flip the **Enabled** toggle. The plugin's tools
   become available to the LLM immediately — no container restart.

To author a bundle (a single `plugin.json` zipped with optional
README, icon, and source), see
[`docs/plugin-bundle-format.md`](docs/plugin-bundle-format.md).

### Browsing curated catalogs

1. Add one or more `index.json` URLs (https-only) to the
   **Browse** card's *Index URLs* editor. The list persists to
   `services.yaml` under `plugin_indexes`.
2. Click **Browse**. Each catalog entry has an **Install** button
   that fetches the bundle and runs it through the upload pipeline.

The curated `synssins/glados-plugins` repo ships an initial seed of
bundles in a future release; in the meantime, point at any compliant
index.

### Per-plugin logs

Click the gear icon (⚙) on any installed plugin row, then the
**Logs** tab. Shows the plugin subprocess's stderr (rotated at
1 MB to `<name>.log.1`, one backup) plus the in-memory event ring
(connect / disconnect / tools-refresh / error). Choose 100 / 500
/ 2000 lines, click **Refresh**, or enable 5 s auto-refresh.

The **About** tab shows name / version / category / persona role
/ repository / source index, with a **Reinstall from source**
button that re-fetches the manifest at the original URL.

## Models

The container is **backend-agnostic** — anything that speaks
`/v1/chat/completions` works. Each LLM role has its own slot in
`services.yaml`, configured independently in **System → Services**:

| Slot | Used by | Recommended model |
|------|---------|-------------------|
| `llm_interactive` | Tier 3 chat, persona rewrites, tool-call planning | Production: `OpenVINO/Qwen3-30B-A3B-int4-ov` on OVMS (Intel Arc Pro B60). Smaller dev option: `qwen3:14b` on Ollama. |
| `llm_autonomy`    | Background autonomy loops (sensor watcher, weather, camera, news) | Same as `llm_interactive`, or split onto a dedicated endpoint. |
| `llm_triage`      | Tier 2 disambiguator, autonomy compaction, memory classifier | `llama-3.2-1b-instruct` (small + fast) — keeps the home-command path responsive. |
| `llm_vision`      | Camera / image analysis (optional) | `qwen2.5vl:7b` or `qwen2.5-vl-3b-instruct`. Unset to disable vision features. |
| `llm_commands`    | Tool-using turns (route=plugin:* / `is_home_command`) — optional separate lane | Same recommendation as `llm_interactive`; falls back to `llm_interactive` when empty. |

A single endpoint can host every slot; operators with a dedicated
GPU/accelerator per role can split them onto separate URLs. Unset
slots fall back to `llm_interactive`.

See [docs/models.md](docs/models.md) for VRAM math, throughput
numbers (including the ~39.8 tok/s steady-state on OVMS + Arc B60),
and the trade-offs between dense and MoE chat models.

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
| `HA_WS_URL` | derived from `HA_URL` | HA WebSocket endpoint |
| `HA_TOKEN` | — | HA long-lived access token (env wins over YAML on a fresh install; YAML wins after a WebUI save) |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | LLM endpoint (named for legacy reasons — anything OpenAI-compat works). Used for chat + autonomy + vision unless split. |
| `OLLAMA_AUTONOMY_URL` | `OLLAMA_URL` | Optional split — autonomy / Tier 2 / rewriter endpoint |
| `OLLAMA_VISION_URL` | `OLLAMA_URL` | Optional split — vision endpoint |
| `VISION_URL` | unset | External vision service (image classification, camera analysis); features simply unavailable when unset |
| `GLADOS_INTERNAL_API_PORT` | `18015` | Loopback-only plain-HTTP port for in-container callers (TTS / STT / api_wrapper). Bound to `127.0.0.1`. |
| `GLADOS_PLUGINS_ENABLED` | `true` | Master toggle for the MCP plugin runtime. Set `false` to disable discovery + spawn. |
| `GLADOS_DOCKER_GID` | unset | Docker group GID for the WebUI Logs page's container source (`getent group docker \| cut -d: -f3`) |
| `WEBUI_HTTP_REDIRECT_PORT` | unset | If set, listen on this port and 301-redirect HTTP → HTTPS |
| `GLADOS_AUTH_BYPASS` | unset | Set to `1` to disable all auth checks (recovery mode — bright-red banner; see Authentication below) |
| `GLADOS_LOGS` | `/app/logs` | Audit log directory |
| `TZ` | `UTC` | Container's system clock TZ. Time-of-day **injection** uses the IANA zone from your weather coordinates instead — `TZ` only affects log timestamps. |

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
