# GLaDOS Container

Composable, standards-compliant AI assistant persona layer. Docker-first,
hardware-agnostic, OpenAI API compatible.

## What This Is

The GLaDOS container is a **middleware persona layer** — it sits between
any OpenAI-compatible client and the LLM inference backend. It provides:

- GLaDOS personality and persona pipeline
- Emotional state system (PAD model, HEXACO traits, escalation detection)
- Semantic memory (ChromaDB vector store)
- Tool execution loop (receives `tool_calls` from LLM, executes via HA MCP)
- Autonomy loop (background agents: HA sensor watcher, weather, camera)
- Discord integration
- HUB75 LED display control
- Admin WebUI (config editor, system controls, health panel)

It does **not** run any models. It makes HTTP calls to:
- Ollama (`/v1/chat/completions`) — LLM inference
- speaches (`/v1/audio/speech`, `/v1/audio/transcriptions`) — TTS/STT
- ChromaDB — vector memory
- Home Assistant — device control and sensor data

## Quick Start

```bash
# CPU only (any hardware)
cp configs/config.example.yaml configs/config.yaml
# edit configs/config.yaml with your HA URL, token, and service URLs
docker compose up -d

# NVIDIA GPU (for speaches/ollama containers)
docker compose -f docker/compose.yml -f docker/compose.cuda.yml up -d

# Intel Arc
docker compose -f docker/compose.yml -f docker/compose.ipex.yml up -d
```

## Port Map

| Port | Endpoint |
|------|----------|
| 8015 | `/v1/chat/completions` — OpenAI-compatible persona API |
| 8052 | Admin WebUI |

Downstream services (not exposed by this container):

| Port | Service |
|------|---------|
| 11434 | Ollama (interactive LLM) |
| 11436 | Ollama (autonomy LLM) |
| 8800 | speaches (STT + TTS) |
| 8000 | ChromaDB |

## Security

This project uses [Snyk](https://snyk.io) for vulnerability scanning.
All secrets are managed via environment variables. See `configs/config.example.yaml`
for required configuration. **Never commit `config.yaml` or any `.env` file.**

## Architecture

See `docs/architecture-plan.md` for the full composable stack design.
See `docs/Future State/Stage 0.md` for the current audit and migration plan.
