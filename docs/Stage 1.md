# Stage 1 — Core Container: OpenAI API Endpoint + All Custom Endpoints
**Status:** Planning
**Dependency:** Stage 0 audit complete
**Goal:** Build and run the GLaDOS container with a fully functional OpenAI-compatible
API on port 8015, all existing custom endpoints intact, and the existing
host-native services (Ollama, speaches, ChromaDB) as its backends. At the end
of this stage, every current client (HA, ESPHome, WebUI, Discord) can point at
the container and receive identical behavior to the current NSSM services.

This is not a feature addition stage. It is a port of what already exists into
a container, with the minimum necessary changes to make it container-native.

---

## What This Stage Covers

The GLaDOS container must expose every endpoint that `glados-api` (port 8015)
currently provides, plus host the WebUI admin panel (port 8052). The container
talks outbound to Ollama, speaches, ChromaDB, and Home Assistant — all of which
remain on the host during this stage, reachable via `host.docker.internal`.

Nothing is retired in this stage. The host NSSM services keep running in
parallel. The container is additive — clients can be switched to it one by one
and switched back if anything breaks.

---

## Endpoints That Must Be Functional on Port 8015

These are carried directly from `api_wrapper.py` into the container. None are
new — all exist today. Verified against api_reference.md and api_wrapper.py.

### OpenAI-Compatible (Standards)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/v1/models` | Model list |
| POST | `/v1/chat/completions` | Streaming chat — persona pipeline entry point |

### GLaDOS Custom (Keep — no standard equivalent)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/announce` | Pre-generated announcement playback via HA |
| POST | `/command` | ESPHome voice command → pre-recorded WAV |
| POST | `/doorbell/screen` | Doorbell camera frame display trigger |
| GET | `/entities` | Cached HA entity list |
| GET | `/api/attitudes` | Attitude directive pool |
| GET/POST | `/api/announcement-settings` | Per-scenario verbosity config |
| GET/POST | `/api/startup-speakers` | Startup speaker selection |
| GET/POST | `/api/force-emotion` | Force emotion preset (A/B testing) |

### WebUI Admin Panel on Port 8052
The WebUI (`tts_ui.py`) proxies most of its API routes through to port 8015.
The container must serve both ports. The WebUI itself is also part of the
container — it is not a separate service.

---

## Steps in Order

### Step 1.1 — Identify and isolate all Windows-specific code paths
**What:** Grep the source for every path, API call, or behavior that is
Windows-specific. These must be conditionally replaced or made configurable
before the container build can succeed. Known issues from prior review:

- `apply_gpu_config.py` — uses `winreg` to write NSSM registry entries.
  This tool is not part of the container runtime; it is a host admin tool.
  **Resolution:** Exclude from container entirely. GPU config is handled by
  Compose overrides, not this script.
- `CREATE_NEW_PROCESS_GROUP` in subprocess calls — Windows-only process flag.
  **Resolution:** Replace with `start_new_session=True` which is
  cross-platform.
- Hardcoded `C:\AI\` paths in source and configs.
  **Resolution:** Replace with environment variables (`GLADOS_ROOT`,
  `GLADOS_DATA`, `GLADOS_AUDIO`, `GLADOS_LOGS`) with sensible container
  defaults (`/app/data`, `/app/audio`, `/app/logs`).
- `os.startfile()` if present — Windows only.
  **Resolution:** Remove or make conditional.
- SSL cert paths in `global.yaml` — currently absolute Windows paths.
  **Resolution:** Volume mount at a fixed container path (`/app/certs/`).

**Dependency:** None. Do this first — nothing else can proceed until the code
is portable.
**Output:** A list of every change made, committed to `glados-container` repo.

---

### Step 1.2 — Define the container's environment variable interface
**What:** Every hardcoded value that varies between deployments becomes an
environment variable with a sane default. This is the container's configuration
contract — what the operator sets in `config.yaml` or `.env`.

Required environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Interactive LLM |
| `OLLAMA_AUTONOMY_URL` | `http://host.docker.internal:11436` | Autonomy LLM |
| `OLLAMA_VISION_URL` | `http://host.docker.internal:11435` | Vision LLM |
| `SPEACHES_URL` | `http://host.docker.internal:8800` | TTS + STT |
| `CHROMADB_URL` | `http://host.docker.internal:8000` | Vector memory |
| `HA_URL` | — | Home Assistant base URL (required) |
| `HA_TOKEN` | — | HA long-lived access token (required) |
| `GLADOS_PORT` | `8015` | API listen port |
| `WEBUI_PORT` | `8052` | WebUI listen port |
| `SERVE_HOST` | — | Host IP for audio file URLs served to HA |
| `SERVE_PORT` | `5051` | Audio file server port |
| `GLADOS_ROOT` | `/app` | Container root path |
| `GLADOS_DATA` | `/app/data` | Runtime data directory |
| `GLADOS_AUDIO` | `/app/audio_files` | Generated audio directory |
| `GLADOS_LOGS` | `/app/logs` | Log directory |
| `SSL_ENABLED` | `false` | Enable HTTPS on WebUI |
| `SSL_CERT` | `/app/certs/cert.pem` | TLS certificate path |
| `SSL_KEY` | `/app/certs/key.pem` | TLS key path |

**Dependency:** Step 1.1 (paths identified before vars can be defined).
**Output:** Updated `config.example.yaml` and `.env.example` in the repo.

---

### Step 1.3 — Port the config loading system
**What:** The current `config_store.py` (Pydantic-based singleton) loads from
multiple YAML files under `C:\AI\GLaDOS\configs\`. In the container, these
files are volume-mounted at `/app/configs/`. The config store must read paths
from environment variables, not hardcoded locations.

The config loading chain:
- `global.yaml` → HA credentials, network settings, SSL, auth, tuning
- `services.yaml` → service endpoint URLs (all become env vars with YAML
  as optional override)
- `speakers.yaml` → HA speaker entities
- `audio.yaml` → audio directory paths
- `personality.yaml` → HEXACO, emotions, attitudes, preprompt
- `glados_config.yaml` → LLM model, autonomy config, vision config
- `memory.yaml` → ChromaDB settings
- `emotion_config.yaml` → PAD model, escalation, drift rates
- `context_gates.yaml` → weather/HA context injection rules

All of these are operator-provided via volume mount. The config store must:
1. Accept a configurable base path (env var `GLADOS_CONFIG_DIR`, default `/app/configs`)
2. Gracefully handle missing optional files with documented defaults
3. Never reference a Windows path internally

**Dependency:** Step 1.2 (env var names must be finalized first).
**Output:** Updated `config_store.py` portable to any OS.

---

### Step 1.4 — Port the conversation pipeline (`api_wrapper.py`)
**What:** `api_wrapper.py` is the core of the container — it handles all
inbound requests on port 8015. It must be portable with no Windows assumptions.

Specific changes required:
- Remove the `handle_command` interceptor from the chat path (already done
  in prior session — confirm it's clean)
- Replace all hardcoded paths with `GLADOS_*` env vars
- Replace `CREATE_NEW_PROCESS_GROUP` with `start_new_session=True`
- The `ThreadingHTTPServer` implementation is cross-platform — no change needed
- The SSE streaming path is cross-platform — no change needed
- All `urllib`/`httpx` outbound calls use URLs from config/env — no change
  needed once Step 1.3 is complete

**Note on tool execution loop:** The tool execution loop (Stage 2 in the
architecture plan) is NOT in scope for this stage. The container will initially
behave identically to the current `glados-api` service — the LLM can describe
tool calls but they won't be executed in the chat path. This is a known
limitation carried forward, to be fixed in Stage 2.

**Dependency:** Steps 1.1, 1.2, 1.3.
**Output:** Portable `api_wrapper.py` committed to repo.

---

### Step 1.5 — Port the engine and autonomy subsystem
**What:** The GLaDOS engine (`engine.py`) and all autonomy agents must run
inside the container. These are the background processes — emotion agent, HA
sensor watcher, camera watcher, weather agent, compaction agent, memory writer.

Portability requirements:
- All file I/O uses `GLADOS_DATA` env var as base path
- All outbound HTTP calls use URLs from config (already env-var driven in
  Step 1.3)
- HA WebSocket connection uses `HA_URL` and `HA_TOKEN` from env
- Emotion state persistence (`data/subagent_memory/emotion.json`) writes
  to `GLADOS_DATA`
- No `winreg`, no Windows process APIs

The autonomy loop is a long-running background thread inside the container
process — no separate service needed. Docker's restart policy handles recovery.

**Dependency:** Steps 1.1, 1.2, 1.3.
**Output:** Portable engine and autonomy code committed to repo.

---

### Step 1.6 — Port the WebUI admin panel (`tts_ui.py`)
**What:** The WebUI (port 8052) must run inside the same container as the API.
It currently runs as a separate NSSM service (`glados-tts-ui`) but in the
container it runs as a thread or subprocess alongside the API server.

Scope for this stage: admin panel only (config editor, system controls,
health panel, service restart buttons). The chat tab is not ported — it will
move to Open WebUI in Stage 8.

Changes required:
- Replace all NSSM service restart calls with Docker-appropriate equivalents.
  `restartService()` currently calls `nssm.exe restart <service>` — in the
  container this becomes a signal or a restart of the container itself via the
  Docker API, or is removed and replaced with a "restart container" button.
- Replace hardcoded Windows log paths with `GLADOS_LOGS` env var
- The ChromaDB restart button currently calls `docker restart glados-chromadb`
  — this works from inside a container only if the Docker socket is mounted.
  Decision: mount `/var/run/docker.sock` read-only, or expose a dedicated
  restart endpoint. Document this decision.
- SSL termination: if `SSL_ENABLED=true`, WebUI reads cert from `/app/certs/`

**Dependency:** Steps 1.1, 1.2, 1.3.
**Output:** Portable `tts_ui.py` committed to repo.

---

### Step 1.7 — Port the memory system (ChromaDB client)
**What:** `chromadb_store.py` and `memory_writer.py` connect to ChromaDB at a
configurable URL. The URL is already read from `memory.yaml`. Verify:
- ChromaDB URL comes from `CHROMADB_URL` env var (via config store)
- No hardcoded `localhost:8000`
- `memory.yaml` default host/port overridable by env

During this stage, ChromaDB continues to run as it does today (existing Docker
container at port 8000). The GLaDOS container connects to it via
`host.docker.internal:8000` or the configured `CHROMADB_URL`.

**Dependency:** Step 1.3.
**Output:** Verified portable memory client.

---

### Step 1.8 — Write the Dockerfile
**What:** Build the container image. The Dockerfile already exists as a
scaffold in the repo — this step fills it in with the real entrypoint, real
dependencies, and verified build.

Key decisions:
- Base image: `python:3.12-slim` — no CUDA, no GPU. Container is CPU-only.
- Entrypoint: a single launcher that starts both the API server (port 8015)
  and the WebUI (port 8052) as threads, and initializes the engine/autonomy
  loop. This replaces the two NSSM services (`glados-api` and `glados-tts-ui`).
- FFmpeg: required for audio processing. Install via apt in Dockerfile.
- ONNX Runtime: required for vision and TTS if not delegated to speaches.
  CPU-only `onnxruntime` (not `onnxruntime-gpu`) — container has no GPU.
- The `glados-vision` ONNX service: decision point — include in the GLaDOS
  container or keep as a separate container. Given it requires ONNX inference,
  recommendation is to keep it separate for now and connect via `VISION_URL`
  env var, consistent with the rest of the architecture.
- Non-root user: run as `glados` user (uid 1000), not root.
- Health check: `GET /health` on port 8015.

**Dependency:** Steps 1.1–1.7 (code must be portable before building image).
**Output:** Working `Dockerfile` that builds successfully and passes a local
`docker build` without errors.

---

### Step 1.9 — Update docker/compose.yml for this stage
**What:** The `docker/compose.yml` scaffold already exists. Update it to
reflect the real volume mounts, environment variables, and service
dependencies for Stage 1.

During Stage 1, Ollama, speaches, and ChromaDB are NOT in the Compose stack
— they run host-native. The GLaDOS container reaches them via
`host.docker.internal`. This keeps Stage 1 focused and avoids needing GPU
passthrough to make anything work.

Stage 1 Compose includes:
- `glados` — the new container
- `chromadb` — move from host Docker run to Compose-managed (low risk,
  already Docker, just adds it to the stack)

Stage 1 Compose excludes (added in later stages):
- `ollama` — host-native for now (GPU passthrough not yet resolved)
- `speaches` — host-native for now
- `open-webui` — Stage 8

**Dependency:** Step 1.8.
**Output:** Working `docker/compose.yml` that starts the GLaDOS container
and ChromaDB, with the container successfully connecting to host-native
Ollama and speaches.

---

### Step 1.10 — Local build and smoke test
**What:** Build the image locally and verify all endpoints respond correctly.
Run the container alongside existing host services. Test each endpoint.

Smoke test checklist:
```
GET  http://localhost:8015/health                → {"status": "ok"}
GET  http://localhost:8015/v1/models             → model list with "glados"
POST http://localhost:8015/v1/chat/completions   → GLaDOS streaming response
GET  http://localhost:8015/api/attitudes         → attitude list
GET  http://localhost:8015/api/startup-speakers  → speaker list
GET  http://localhost:8015/entities              → HA entity cache
POST http://localhost:8015/announce              → announcement playback
GET  http://localhost:8052/                      → WebUI login page
GET  http://localhost:8052/health                → {"status": "ok"}
```

Compare behavior against existing `glados-api` on port 8015 (still running
on host) to confirm parity.

**Dependency:** Step 1.9.
**Output:** Passing smoke test. Container confirmed functionally equivalent
to current host services.

---

### Step 1.11 — Snyk scan of built image
**What:** Run Snyk against the built container image to identify any
vulnerabilities in base image or installed packages before the image is
used in production.

```bash
snyk container test glados:latest --org=b905c516-c213-433b-973a-9d26adf03871
```

Review results. Address any HIGH or CRITICAL findings before proceeding.
The GitHub Actions workflow will also run this automatically on push.

**Dependency:** Step 1.8.
**Output:** Clean or accepted Snyk report. Any suppressions documented
in `.snyk` file committed to repo.

---

### Step 1.12 — Push to GitHub and validate CI
**What:** Push the complete Stage 1 implementation to `main`. Verify both
GitHub Actions workflows run successfully:
- `snyk.yml` — Snyk security scan passes
- `build.yml` — Docker image builds and pushes to GHCR

**Dependency:** Steps 1.10, 1.11.
**Output:** Green CI. Image available at `ghcr.io/synssins/glados-docker:latest`.

---

## What Is Explicitly NOT in Stage 1

These are out of scope and will be addressed in their respective stages:

| Item | Stage |
|------|-------|
| Tool execution loop (device control in chat) | Stage 2 |
| HA voice pipeline integration | Stage 3 |
| speaches TTS/STT container | Stage 4 / Stage 7 |
| Open WebUI chat interface | Stage 8 |
| Discord integration | Stage 9 |
| Ollama in Docker (GPU passthrough) | Stage 7 |
| GLaDOS custom voice in speaches | Stage 4 |
| GPU passthrough resolution | Stage 6 |

---

## What You May Be Missing

**1. The audio file server (port 5051)**
The current stack serves generated WAV files on port 5051 so HA can call
`media_player.play_media` with an HTTP URL. This is a static file server
built into `tts_ui.py`. Inside the container, this still works — but the
`SERVE_HOST` env var must be set to the host's IP that HA can reach, not
`localhost` or the container's internal IP. If unset, HA announcements
will fail silently. This needs to be explicitly documented and validated
in the smoke test.

**2. The pre-generated WAV files**
Announcement WAVs and command WAVs (`glados_announcements/`, `glados_commands/`)
are generated by `generate_announcements.py` and `generate_commands.py` which
currently call the host-native TTS service. These files must be volume-mounted
into the container (`/app/audio_files/`). The generation scripts themselves
don't need to run inside the container — they can remain host-native tools
that write into the mounted volume.

**3. SSL certificate handling**
The WebUI currently uses Let's Encrypt certs from `C:\AI\certs\`. These
need to be volume-mounted at `/app/certs/` in the container. If `SSL_ENABLED`
is false, the WebUI runs plain HTTP — acceptable for local network use.

The container should handle its own certificate lifecycle without requiring
external port exposure (no port 80/443 open to the internet). This means
DNS-01 challenge rather than HTTP-01. DNS-01 validates domain ownership by
writing a TXT record to the domain's DNS zone — the ACME server never
makes an inbound connection to the host, so no firewall rules or port
forwarding are required.

DNS-01 requires a DNS provider with an API that the ACME client can call to
create and remove TXT records automatically. The specific provider and API
token are operator-supplied — this is environment-specific and not part of
the container's core configuration.

Recommended approach: add a cert management sidecar container to the Compose
stack (options: `adferrand/lego-auto`, `certbot/certbot` with a DNS plugin,
or `Caddy` as a reverse proxy with built-in ACME). The sidecar obtains and
renews certs automatically and writes them to a shared Docker volume
(`glados_certs`) that the GLaDOS container mounts at `/app/certs/`. The GLaDOS
container itself never speaks ACME — it just reads the cert files.

Implementation notes for operators:
- Choose a DNS provider that has an ACME DNS-01 plugin (most major providers
  do — Route53, Cloudflare, Azure DNS, GoDaddy, Namecheap, etc.)
- Scope the DNS API token to the minimum required: DNS TXT record
  create/delete on the specific zone only — nothing else
- The API token is a secret: `.env` only, never committed
- If the domain is internal-only (does not resolve on the public internet),
  DNS-01 still works — Let's Encrypt only checks the DNS TXT record, not
  whether the domain routes to your server
- Self-signed certs are always an option for fully local/air-gapped installs
  where Let's Encrypt is not suitable

This is a Stage 1 design decision. The chosen DNS provider and sidecar
configuration should be documented in the operator's local `config.yaml`
and `.env`. The repo ships with `config.example.yaml` and `.env.example`
containing placeholder fields and comments explaining what is required.

**4. The `force_emotion.py` script**
This script currently calls `http://localhost:8015/api/force-emotion` — it
will work unchanged against the container since the port doesn't change.
No migration needed.

**5. Discord integration**
The Discord services (`gladys-api`, `gladys-discord`, `gladys-observer`) are
NOT being ported in Stage 1. They continue to run as NSSM services talking
to the host-native `glados-api`. This is acceptable for Stage 1 — they will
be unified in Stage 9. The container does not break them.

**6. The `glados-vision` service**
Vision (port 8016) is not part of the GLaDOS container. It remains host-native
for now. The container connects to it via `VISION_URL` env var. Vision's ONNX
inference cannot run in the CPU-only GLaDOS container without significant
performance degradation.

**7. Session state / auth across restarts**
The WebUI uses bcrypt session cookies. Session state is currently in-memory.
If the container restarts, all sessions are invalidated. This is acceptable
behavior — document it. For persistent sessions, a session store (Redis or
file-based) would be needed, but that's out of scope for Stage 1.

---

## Dependencies Summary

```
1.1 (path audit)
  └─► 1.2 (env vars)
        └─► 1.3 (config loading)
              ├─► 1.4 (api_wrapper)
              ├─► 1.5 (engine/autonomy)
              ├─► 1.6 (webui)
              └─► 1.7 (memory client)
                    └─► 1.8 (Dockerfile)
                          └─► 1.9 (compose.yml)
                                └─► 1.10 (smoke test)
                                      └─► 1.11 (Snyk)
                                            └─► 1.12 (push/CI)
```

Steps 1.4, 1.5, 1.6, and 1.7 can be worked in parallel once 1.3 is complete.
