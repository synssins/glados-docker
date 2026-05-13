# GLaDOS Infrastructure — Live Topology Reference

**Read at every session start.** Source of truth for what's running, where,
and how the pieces connect. If reality drifts from this doc, the doc is
wrong — fix it.

Last verified live: **2026-05-13** (chat lane swap from OpenArc → llama.cpp 35B on Arc B60).

---

## Hosts

```
┌──────────────────────────────┐      ┌────────────────────────────────┐
│  AIBox (Windows Server 2022) │      │  Docker host (OMV / Debian)    │
│  192.168.1.75                │      │  192.168.1.150                 │
│                              │      │  SSH: root / R0ck0nFiero!!!!!  │
│  - Intel Arc Pro B60 (24 GB) │      │                                │
│  - Tesla T4 #0 + #1 (PCIe)   │      │  Compose:                      │
│  - Models on local SSD       │      │   /srv/dev-disk-by-uuid-.../   │
│  - NSSM services run here    │      │   data/docker/compose/         │
│  - Claude Code session runs  │      │     docker-compose.yml         │
│    on this host              │      │                                │
└──────────────┬───────────────┘      └─────────────┬──────────────────┘
               │                                    │
               │   HTTP (LAN)                       │   docker exec / SSH
               ▼                                    ▼
               ┌────────────────────────────────────────────┐
               │  glados container (Debian 12, py 3.12)     │
               │  Image: ghcr.io/synssins/glados-docker:    │
               │         latest  (image SHA 475fd953...     │
               │         as of 2026-05-09)                  │
               │                                            │
               │  Ports:                                    │
               │   8015  → /v1/* OpenAI-compatible API      │
               │   8052  → WebUI (Cloudflare-Access fronted)│
               │   5051  → SERVE_PORT (audio)               │
               │   18015 → loopback-only plain HTTP         │
               │            (TLS-conditional 8015 mirrors)  │
               │                                            │
               │  External:                                 │
               │   - LLM   → 192.168.1.75:{11434,11436,...} │
               │   - HA    → 192.168.1.104:8123             │
               │   - MQTT  → (Phase 2, not yet wired)       │
               │                                            │
               │  Internal (no external deps):              │
               │   - TTS   → local Piper VITS (CPU,         │
               │              glados.onnx bundled)          │
               │   - STT   → local Parakeet CTC (CPU,       │
               │              bundled in image)             │
               │   - Memory→ ChromaDB in-process            │
               │   - Embed → BGE-small-en-v1.5 (entity      │
               │              semantic match)               │
               └────────────────────────────────────────────┘
                                  │
                                  │   HTTPS via Cloudflare Access
                                  ▼
                          glados.denofsyn.com
                          (operator login: synssins@gmail.com
                           + 6-digit code; session cookie
                           `glados_session`)
```

---

## AIBox NSSM services — current

All managed via `nssm`. **Safe-form rule**: never invoke `nssm` (no args),
`nssm set <name>` (no param), `nssm install <name>` (no app), `nssm edit
<name>`, or `nssm remove <name>` (without trailing `confirm`) — those open
a blocking GUI dialog. Always supply the full args.

Reference patterns:

```powershell
sc.exe query <service>                              # status
nssm status <service>                               # NSSM-flavoured status
nssm get <service> <param>                          # read one param
nssm start <service>                                # safe
nssm stop  <service>                                # safe
nssm restart <service>                              # safe
nssm set <service> <param> <value>                  # safe (3 args)
nssm install <service> <path-to-exe-or-bat>         # safe (2 args)
nssm remove <service> confirm                       # safe (with `confirm`)
```

### Service inventory

| Service             | Port  | Backend                                      | Model / Purpose                                                            | GPU            | Start  |
|---------------------|-------|----------------------------------------------|----------------------------------------------------------------------------|----------------|--------|
| **llamacpp-chat**   | 11434 | `C:\AI\llama.cpp\llama-server.exe` (SYCL)   | `Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf` — chat lane (free-form, persona)         | Arc Pro B60    | DEMAND (manual until validated, then flip to AUTO) |
| **llama-rewriter**  | 11436 | `C:\llamacpp\llama-server.exe` (CUDA)        | `Qwen3-4B-Instruct-2507-Q5_K_M.gguf` — triage + autonomy probe + rewriter  | Tesla T4 #0    | AUTO   |
| **llamacpp-vision** | 11437 | `C:\llamacpp\llama-server.exe` (CUDA)        | `Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` + mmproj — camera vision / `look_at_camera` tool | Tesla T4 #1 | AUTO   |

### Service file layouts

```
C:\llamacpp\                                    ← CUDA build of llama.cpp (T4 cards)
├── llama-server.exe
├── ggml-cuda.dll, cublas64_13.dll, ...
├── logs\
│   ├── llama-rewriter.stdout.log
│   ├── llama-rewriter.stderr.log
│   ├── llamacpp-vision.stdout.log
│   ├── llamacpp-vision.stderr.log
│   ├── llamacpp-chat.stdout.log
│   └── llamacpp-chat.stderr.log
├── models\
│   ├── Qwen3-4B-Instruct-2507-Q5_K_M.gguf
│   ├── Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf
│   └── mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf
└── nssm\
    ├── llamacpp-vision\
    │   ├── start_vision.bat
    │   └── start_vision_shadow.bat
    └── llamacpp-chat\
        └── start_chat.bat       ← wraps env vars + llama-server invocation
                                    for the SYCL build at C:\AI\llama.cpp

C:\AI\llama.cpp\                                ← SYCL build of llama.cpp (Arc B60)
├── llama-server.exe
├── ggml-sycl.dll, libsycl-fallback-*.spv, ...
├── sycl-ls.exe
├── launch-server.ps1            ← manual launcher (operator-authored)
└── ...

C:\AI\models\
└── Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf
```

### Battlemage / Xe2 / SYCL workarounds (Arc B60 only)

Both the NSSM `start_chat.bat` and the manual `launch-server.ps1` set
these env vars before invoking `llama-server.exe`:

| Env var                      | Value                  | Why                                                                                                                          |
|------------------------------|------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `GGML_SYCL_DISABLE_OPT`      | `1`                    | Workaround for `ggml-org/llama.cpp#21893`: weight corruption on Xe2 causes the model to emit looping `11111` or hallucinated training fragments without it. |
| `ONEAPI_DEVICE_SELECTOR`     | `level_zero:gpu`       | Pins to Intel Level Zero GPU. Filters out the two T4s and the iGPU stub — llama.cpp would otherwise try to split layers across all of them and immediately crash. |

And the llama-server invocation includes:

| Flag                       | Value                                           | Why                                                                                                                                                |
|----------------------------|-------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `--chat-template`          | `chatml`                                        | `--jinja` rejects the operator's system-then-history call order because Qwen3.5's embedded jinja template insists on system-must-be-first. Using `chatml` bypasses that. |
| `--reasoning-budget`       | `0`                                             | Forces immediate end-of-thinking. Without this, the 35B model spends all 1024 token budget producing `<think>...` reasoning content that the container's think-filter strips, leaving empty chat bubbles. |
| `--ctx-size`               | `65536`                                         | Comfortable headroom for persona + memory context + RAG.                                                                                          |
| `--n-gpu-layers`           | `99`                                            | All-layer GPU offload.                                                                                                                            |
| `--cache-type-k` / `-v`    | `q8_0`                                          | 8-bit KV cache — fits more context on the 24 GB B60 without quality loss.                                                                          |
| `--flash-attn`             | `on`                                            | Faster attention on SYCL.                                                                                                                          |

If you ever need to debug "looping `11111`" output, the first two env vars
are the cure. If you ever need to debug "empty chat bubbles" after a 50 s
LLM round-trip, `--reasoning-budget 0` is the cure.

### Decommissioned (don't reinstall, don't mention as fallback)

- **`openarc`** (OpenVINO inference server) — was the chat lane on
  `:11434` until 2026-05-13. Replaced by `llamacpp-chat` for two reasons:
  (1) upstream gaps (no `chat_template_kwargs`, `response_format` silently
  dropped, temperature=0.0 crashes), (2) operator preferred llama.cpp on
  the B60. The `C:\OpenVino\OpenArc\` install on disk is untouched; only
  the NSSM service registration is gone.
- **`OVMS`** (OpenVINO Model Server) — never made it into the chat lane;
  removed 2026-05-03 after multi-model routing failed (single HTTP layer
  registers first, drops subsequent graphs).

---

## Docker host — glados container

| Aspect              | Value                                                                                                                 |
|---------------------|-----------------------------------------------------------------------------------------------------------------------|
| Image               | `ghcr.io/synssins/glados-docker:latest`                                                                              |
| Current image SHA   | `sha256:475fd9532ee62...` (as of 2026-05-09)                                                                          |
| Branch              | `main`                                                                                                                |
| Compose             | `/srv/dev-disk-by-uuid-8db26308-e3bf-41bc-8a5f-a3eb2c527f41/data/docker/compose/docker-compose.yml` (on the docker host) |
| Deploy script       | `scripts/_local_deploy.py` (build on the docker host directly — GHCR LFS budget exhausted)                            |
| Hot-copy iteration  | `scripts/_hot_copy.py` (for fast iteration — bypasses image rebuild)                                                  |

External services the container expects to reach:

| Service                   | URL                          | Backed by                                |
|---------------------------|------------------------------|------------------------------------------|
| **Chat LLM**              | `http://192.168.1.75:11434`  | NSSM `llamacpp-chat` (Arc B60)           |
| **Triage / autonomy LLM** | `http://192.168.1.75:11436`  | NSSM `llama-rewriter` (T4 #0)            |
| **Vision LLM**            | `http://192.168.1.75:11437`  | NSSM `llamacpp-vision` (T4 #1)           |
| **Home Assistant**        | `http://192.168.1.104:8123`  | Operator's HA instance                   |

**WebUI is the source of truth for service URLs.** Configuration →
Services tab. Don't quote a URL/model from memory or stale comments —
read the WebUI. (Operator-corrected rule, 2026-05-05.)

---

## Audio path

Local in-container, never upstream:

- **TTS**: Piper VITS, `glados.onnx` voice baked into the image at
  `/app/models/TTS/`. Served at `/v1/audio/speech`.
- **STT**: Parakeet CTC + Silero VAD, ONNX-runtime, bundled in the image
  at `/app/models/ASR/`. Served at `/v1/audio/transcriptions`.

**Hard rule (operator-flagged 5+ times):** Speaches is **NOT** the audio
path. Never mention Speaches in diagnoses or designs. Local Piper for TTS,
local ASR for STT.

---

## Auth / access

| Surface                       | Detail                                                                                |
|-------------------------------|---------------------------------------------------------------------------------------|
| Public URL                    | `https://glados.denofsyn.com`                                                         |
| Edge auth                     | Cloudflare Access (operator email + 6-digit code)                                     |
| Container session cookie      | `glados_session`                                                                      |
| WebUI accounts                | Multi-user Argon2id (auth rebuild shipped 2026-04-25, CHANGES.md Change 23–24)        |
| Recovery                      | `GLADOS_AUTH_BYPASS=1` env var (red banner shown when set; remove + restart to revert)|
| First-run wizard              | `/setup` — operator-flagged 2026-05-09: cut to single admin-password step ONLY; cannot be skipped. Lands in Approach 3 of design-system v3. |

---

## Scope discipline (CLAUDE.md restated)

This repo is a **consumer** of external services (Ollama-compatible LLM,
Home Assistant, ChromaDB-in-process, MQTT-eventually). Each is an opaque
HTTP endpoint from the container's perspective.

- GPU hardware, model-file tuning, llama.cpp build flags, HA YAML — all
  **out of scope** for changes via this repo. (NSSM services and
  `start_*.bat` files are AIBox config, but they're documented here so
  future sessions know what's running.)
- Speaches phoneme lexicon — out of scope (Speaches is not the audio
  path; see "Audio path" above).
- T4 / NVIDIA / CUDA recommendations for the **chat lane** are out by
  default (Intel B60 is the chat lane). T4s ARE in-play for triage and
  vision per the service inventory above. Operator-clarified 2026-05-04.

---

## Pointers (where else to look)

- `CLAUDE.md` (repo root) — behavioural guidelines, "READ FIRST" list,
  surgical-changes / simplicity-first / TDD norms.
- `docs/CHANGES.md` — chronological change log. Most recent entry at the
  bottom (Change 45 as of 2026-05-09, SIP Slice 1 foundation).
- `docs/roadmap.md` — prioritised pending work + Technical Debt section.
- `C:\src\SESSION_STATE.md` — historical "what was live when" log; large
  (~162 KB) so don't read end-to-end. Search specific terms.
- `.interface-design/system.md` — authoritative WebUI design system spec.
- `~/.claude/projects/C--src/memory/MEMORY.md` — auto-loaded memory index;
  points at smaller per-topic files under
  `~/.claude/projects/C--src/memory/`.

---

## Reflexes worth keeping

1. **WebUI = source of truth for service URLs and models.** Don't quote
   from memory. Configuration → Services tab.
2. **Verify before recommending.** Tool / engine / library names: hit
   the actual project page, check Windows compat, check active
   maintenance, check requirements — in that order.
3. **Production writes need an artifact.** Tested on parallel port, or
   upstream doc citing exact use case, or operator-acknowledged with the
   literal change spelled out. The phrases "the flag exists", "the
   syntax parsed", "the log says AVAILABLE", "the tool didn't error" are
   NOT evidence — they confirm a thing happened, not the right thing.
4. **Operator runs nothing. Claude executes everything.** SSH to docker
   host, NSSM service control on AIBox, deploy script, log reads — all
   via tools. No "run this command for me" hand-offs.
5. **NSSM safe-form** — see top of this doc, repeated for emphasis.
6. **Trust operator-stated facts** over derived/inferred ones. If the
   operator says "I switched to X," X is now the truth even if every
   memory file still says Y.
