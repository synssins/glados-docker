# GLaDOS Container

Composable, standards-compliant AI assistant persona layer. CPU-only
middleware, OpenAI API compatible. Three-tier command matcher in front of
Home Assistant for sub-second device control with conversational
disambiguation.

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
ollama pull qwen2.5:14b-instruct-q4_K_M   # chat + disambiguator
ollama pull qwen2.5:3b-instruct-q4_K_M    # persona rewriter
ollama pull llama3.2-vision:latest        # vision (optional)

# 5. Start GLaDOS + its ChromaDB
docker compose -f docker/compose.yml up -d

# 6. Verify
curl http://localhost:8015/health
curl http://localhost:8015/v1/models

# 7. (Optional) tail the audit log to watch tier decisions in real time
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

### Other docs

See `docs/Stage 1.md` for the original middleware containerization plan.
See `docs/Stage 3.md` for the HA Conversation Bridge + MQTT Peer Bus
architecture (Phase 1 done; Phase 2 MQTT pending).
See `docs/CHANGES.md` for the running change log.
See `docs/roadmap.md` for prioritized remaining work.

## Ports

| Port | Exposed | Purpose |
|------|---------|---------|
| 8015 | LAN     | OpenAI-compatible persona API (`/v1/chat/completions`, `/v1/audio/speech`, custom endpoints) |
| 8052 | LAN     | Admin WebUI (config editor, health panel, TTS generator, chat) — HTTPS when SSL enabled |
| 8000 | Localhost only | ChromaDB (vector memory) — no outside consumer |

The container is designed for LAN deployment. If you expose either port
to the public internet, put it behind a reverse proxy with authentication
(Cloudflare Access, Authelia, etc.); the WebUI's bundled bcrypt password
is fine for LAN-trust but not for global exposure.

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
| `qwen2.5:14b-instruct-q4_K_M` | 8.6 GB | chat + Tier 2 disambiguator | `DISAMBIGUATOR_MODEL` env |
| `qwen2.5:3b-instruct-q4_K_M`  | 1.8 GB | persona rewriter            | `REWRITER_MODEL` env |
| `llama3.2-vision:latest`      | 7.8 GB | vision queries (optional)   | —                       |

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

Operator-tunable disambiguation rules go in `configs/disambiguation.yaml`
(template at `configs/disambiguation.example.yaml`): naming convention,
overhead-synonym list, state-inference toggle, freshness budget, candidate
limit.

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

Documented in `docs/roadmap.md` → Stage 3 follow-ups:

- HA's intent matcher occasionally misclassifies state queries as
  actions ("is the kitchen light on" returns `action_done` with speech
  "Turned on the lights").
- `switch.*` entities with room names in their friendly_name (e.g.
  Sonos audio settings) appear in clarify lists for "lights" queries.
- Some HA entities are in `unavailable` state but accept service calls
  silently — Tier 1 reports success without a real state change. Needs
  post-execute state verification.
- Conversation history is not yet propagated across turns; "All lights"
  after "turn off the whole house" doesn't inherit the verb context.
- Tier 2 latency (5–11 s) is above the 2–5 s plan target due to the
  14B model's response time. Switching to a smaller fine-tuned model
  is on the roadmap.
- Phase 2 (MQTT peer bus for NodeRed/Sonorium) and Phase 3 (labeled
  test corpus, reconnect tests, second-factor design for sensitive
  intents) are not yet implemented.
