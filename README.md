# GLaDOS Container

Composable, standards-compliant AI assistant persona layer. CPU-only
middleware, OpenAI API compatible.

## What This Is

The GLaDOS container is a **pure middleware persona layer**. It sits between
any OpenAI-compatible client and the LLM inference backend, injecting
personality, emotion, memory, and tool execution into the conversation. It
runs no ML inference itself — all of that is delegated to external services.

Responsibilities that live in this container:

- GLaDOS personality and persona pipeline
- Emotional state system (PAD model, HEXACO traits, escalation detection)
- Attitude directive system (per-response tone variation)
- Semantic memory (ChromaDB vector store — bundled in this compose)
- Tool execution loop (OpenAI agentic loop → HA MCP executor)
- Autonomy loop (background agents: HA sensor watcher, weather, camera)
- Discord integration
- HUB75 LED display control
- Admin WebUI with integrated TTS generator and chat client

Responsibilities that live **outside** this container:

- **LLM inference** — Ollama (or any OpenAI-compatible backend) at `OLLAMA_URL`
- **Speech synthesis + recognition** — speaches at `SPEACHES_URL`
- **Chat UI** — Open WebUI is optional; operators run it separately if desired
- **Home Assistant** — the control plane, at `HA_URL`

## Quick Start

```bash
# 1. Configure
cp .env.example .env
cp configs/config.example.yaml configs/config.yaml
# Edit both — point at your Ollama, speaches, and HA instances.

# 2. Make sure Ollama and speaches are running and reachable.
#    They are NOT in this compose — run them separately (host-native,
#    another compose stack, or another machine).

# 3. Start GLaDOS + its ChromaDB
docker compose -f docker/compose.yml up -d

# 4. Verify
curl http://localhost:8015/health
curl http://localhost:8015/v1/models
```

## Ports

| Port | Exposed | Purpose |
|------|---------|---------|
| 8015 | Public  | OpenAI-compatible persona API (`/v1/chat/completions`, `/v1/audio/speech`, custom endpoints) |
| 8052 | Public  | Admin WebUI (config editor, health panel, TTS generator, chat) |
| 8000 | Localhost only | ChromaDB (vector memory) — no outside consumer |

## External Services (operator-provided)

GLaDOS needs to reach these. Any routing works — container, host, LAN,
cloud — as long as the URL in `.env` resolves.

| Service   | Default URL                              | Provides                              |
|-----------|------------------------------------------|---------------------------------------|
| Ollama (interactive) | `http://host.docker.internal:11434` | `/v1/chat/completions` for user-facing chat |
| Ollama (autonomy)    | `http://host.docker.internal:11436` | Background LLM calls (optional separate instance) |
| Ollama (vision)      | `http://host.docker.internal:11435` | Vision model (optional)               |
| speaches             | `http://host.docker.internal:8800`  | `/v1/audio/speech` + `/v1/audio/transcriptions` |
| Home Assistant       | (no default)                         | Device control, WebSocket state stream |

## Architecture

This container is intentionally **CPU-only and hardware-agnostic**. It does
not benefit from GPU access — every ML operation is an HTTP call to
something else. Put your GPU where Ollama and speaches live.

See `docs/Stage 1.md` for the current containerization plan.
See `docs/CHANGES.md` for the running change log.
See `C:\AI\GLaDOS\docs\Future State\architecture-plan.md` for the full
composable-stack design across all stages.

## Security

This project uses [Snyk](https://snyk.io) for vulnerability scanning of
the built image. All secrets are managed via environment variables. See
`.env.example` and `configs/config.example.yaml` for required configuration.

**Never commit `.env` or `configs/config.yaml` — both are gitignored.**
