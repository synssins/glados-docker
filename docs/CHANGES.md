# GLaDOS Container — Change Log

Every structural change to the container repo, in chronological order. Each
entry records what changed, why, and what side-effects the operator should
expect. This is the running journal for the containerization work — it
supplements git history with *why* rather than *what*.

---

## Change 1-5 — (earlier history, see git log)

Summary of prior work (pre-April 17, 2026):
- Change 1: Pure-middleware refactor (removed bundled TTS/ONNX, replaced with
  speaches HTTP client)
- Change 2: CI workflow setup (Snyk Python + Container scans)
- Change 3: Snyk policy file committed
- Change 4: Snyk CLI migration (UUID token format)
- Change 5: Container security hardening (non-root user)

All merged into PR #1 which landed on `main` on April 16, 2026.

---

## Change 6 — Step 1.10: local smoke test, porting fixes

**Date:** 2026-04-15
**Status:** Complete
**Commits:** `9630f1a`

Fixes surfaced by the first local `docker build` + `docker compose up` per
Stage 1 Step 1.10. The port commit (`2c10651`) touched ~60 modules in a
single pass without runtime testing; this change captured every failure and
fixed them.

### Build + infrastructure fixes

- **`docker/compose.yml`** — chromadb healthcheck switched from `curl` (not
  in chroma image) to `bash /dev/tcp` probe
- **Dockerfile** — glados user now has home dir (`useradd -m`) for subagent
  memory writes

### Missing modules — not ported in `2c10651`

- **`glados.observability`** (4 files: bus, events, minds, __init__)
- **`glados.vision`** — lightweight only (VisionState, VisionConfig,
  VisionRequest, constants). FastVLM + VisionProcessor stay external.
- **`glados.mcp`** (11 files, 1377 lines) — HA MCP client

### Code fixes

- **`glados/webui/tts_ui.py:1542`** — syntax error (stray quote in
  os.environ.get rewrite)
- **`glados/tools/__init__.py`** — removed `slow_clap` (requires sounddevice)
- **`glados/ASR/__init__.py`** + `null_asr.py` — added `"none"` engine type
  for container mode

### Smoke test results

6/8 endpoints passed on first try (health, models, attitudes, speakers,
chat, chromadb via override). Expected failures: TTS 404 (voice not
registered in speaches — Stage 4), announce (no announcements.yaml — fixed
in Change 7).

---

## Change 7 — Docker host deployment + feature completion

**Date:** 2026-04-17
**Status:** Complete
**Commits:** `a7de7c1` through `2f44046`

Deployment to user's Docker host (OMV, Btrfs, separate from AIBox) plus
significant feature work: agentic tool calling, audio playback UI, HA MCP
integration, fuzzy entity resolution, SSL certificate management, and
Stage 3 planning.

### Deployment

- **GHCR build permissions fix** (commit `a7de7c1`) — added
  `permissions: packages: write` to `.github/workflows/build.yml` so
  `GITHUB_TOKEN` can push to `ghcr.io/synssins/glados-docker:latest`
- **Compose fragment delivered** to operator — fits inside their existing
  docker-compose.yml alongside Plex/Arr/Ollama stack
- **ACL workaround documented** — OMV uses POSIX ACLs; operator must run
  `setfacl -R -m u:1000:rwx,m::rwx $DOCKERCONFDIR/glados` so uid 1000 (the
  container user) can write to bind-mounted config/audio dirs

### Configuration path fixes

- **`announcements.yaml`, `vision_announcements.yaml`, `commands.yaml`**
  moved from `audio_files/` tree to `/app/configs/` (commit `28bfa68`).
  These are config, not media — separating them cleaned up the mount
  layout. Code paths in `api_wrapper.py` and `ha_sensor_watcher.py` now
  read from `cfg._configs_dir` or `GLADOS_CONFIG_DIR` env var.

### TTS fixes

- **`glados/TTS/tts_speaches.py`** (commit `11e6c31`) — fixed two bugs:
  1. Attitude params (`length_scale`, `noise_scale`, `noise_w`) moved from
     `extra_body` wrapper (speaches-only convention) to top-level fields so
     the AIBox Piper TTS at port 5050 accepts them
  2. Default `model` no longer hardcoded to `hexgrad/Kokoro-82M` — sent
     only if explicitly configured

### WebUI fixes

- **Removed Training tab** (commit `683050a`) — piper_train is a host-native
  tool not available in the container. Both sidebar and mobile topbar.
- **Token display encoding** (commit `683050a`) — replaced UTF-8 mojibake
  arrow `â†→` with ASCII `->`
- **Curly quote JS syntax error** (commit `ecf1142`) — bulk replacement of
  Unicode smart quotes (U+2018/U+2019) with straight ASCII quotes (0x27)
  in the JS inline script
- **Audio playback controls** (commits `6d3c148`, `78603f9`, `759d53f`):
  1. Added `do_HEAD` handler so `<audio>` element's probe request
     succeeds (was returning 501)
  2. Added HTTP Range (206 Partial Content) support for audio seeking
  3. Added `Accept-Ranges: bytes` header on all binary responses
  4. Sync background `Audio()` playback with visible `<audio controls>` —
     handoff on `canplay` event preserves current playback position

### HA MCP agentic tool loop

- **`glados/core/api_wrapper.py` `_stream_chat_sse`** — major additions:
  1. `commit 7332a2e`: agentic tool loop — streaming chat now includes
     tool definitions from the MCP manager, detects `tool_calls` in
     streaming responses (both Ollama and OpenAI formats), executes via
     `mcp_manager.call_tool()`, feeds results back, up to 5 rounds
  2. `commit 361fcdd`: MCP tools only (dropped static tools like
     `do_nothing`/`robot_*` that clutter context), `num_ctx: 16384`
     override to fit personality + tools
  3. `commit 7ecba22`: tool-use reinforcement system message (persona
     few-shots biased toward text responses; explicit instruction to use
     tools overrides that bias)
  4. `commit 2236ec2`: strip few-shot user/assistant examples from
     preprompt when tools are present — prevents model from following
     text-only example pattern
  5. `commit f0ca98c`: removed function-syntax examples from tool hint
     (model was copying `HassTurnOff(...)` as literal text output)
  6. `commit 82d2b9e`: use real HA names (not "testing illumination" from
     Aperture Science persona), use `domain` not `device_class` for lights
  7. `commit baf83bd`: tool argument auto-fixup — if model puts `light` in
     `device_class`, move to `domain`; ensure `domain` is always an array
  8. `commit b25878b`: **fuzzy entity name resolution** — before any
     `HassTurnOn`/`HassTurnOff`/`HassLightSet` call, query HA REST API for
     entities and fuzzy-match the name parameter using `rapidfuzz`.
     "cabinet lights" → "Kitchen cabinet light switch" (score 88)

### Compaction agent bug (conversation store growth)

- **`glados/core/conversation_store.py`** + **`glados/autonomy/agents/compaction_agent.py`**
  (commit `1100df7`) — root cause of B60 IPEX Ollama freezes on AIBox. The
  compaction agent excluded ALL system messages from compaction (role-based
  check), but each compaction cycle CREATES a new system-role `[summary]`
  message. Over hours/days these summaries accumulated unbounded — host
  reached 132K tokens with compaction running continuously but unable to
  shrink. Fix: replaced role-based exclusion with index-based preprompt
  protection. `ConversationStore` now tracks `preprompt_count`. Compactable
  set = everything except the initial preprompt messages and the last N
  recent messages. Summaries ARE now compactable. Host-native glados-api
  also received this fix.

### SSL certificate management

**Commits:** `9ad2b52`, `4ad589c`, `2f44046`

Complete Let's Encrypt integration with Cloudflare DNS-01 challenge, plus
manual PEM upload path and live certificate status display.

- **`Dockerfile`** — installs `certbot` + `certbot-dns-cloudflare` via pip
- **`glados/core/config_store.py`** — `SSLGlobal` expanded with
  `use_letsencrypt`, `acme_email`, `acme_provider`, `acme_api_token`
- **`glados/webui/tts_ui.py`** — new endpoints:
  - `GET /api/ssl/status` — parses cert with `cryptography` library,
    returns subject, issuer, SANs, not_before, not_after, days_remaining,
    source (letsencrypt/self-signed/manual)
  - `POST /api/ssl/upload` — accepts PEM content via JSON, validates
    headers, writes to configured paths with 0600 on key
  - `POST /api/ssl/request` — runs certbot with DNS-01 Cloudflare
    challenge, captures output, copies issued cert/key to configured
    paths. Handles "not due for renewal" gracefully.
- **WebUI** — new SSL section in Configuration tab with live status box,
  Let's Encrypt form (domain, email, provider, API token, Request button),
  manual upload pickers, advanced path overrides, restart reminder

### Verified working on operator's Docker host (10.0.0.50)

- Let's Encrypt cert issued for `glados.example.com` (E7 issuer)
- HTTPS returns 200, valid chain, expires Jul 16, 2026
- HA MCP tool calls execute end-to-end (e.g. `HassTurnOff` on kitchen
  cabinet light switch confirmed)
- Fuzzy entity resolver matches "cabinet lights" to real entity name
- Audio playback controls show live progress, pause works, download works
- Streaming chat produces tool calls instead of hallucinated text

### Side effects

1. **New pip dependencies in the image** — `certbot` + `certbot-dns-cloudflare`
   (+15 MB). Runs as subprocess when operator clicks "Request Certificate".
2. **`num_ctx: 16384` override** in streaming chat requests — overrides the
   Modelfile's 8192 to fit personality + 21 MCP tools. Increases prompt
   processing time slightly but prevents tool definition truncation.
3. **Few-shot examples dropped** from streaming chat messages when MCP is
   available — personality system prompt still sent, but the user/assistant
   example pairs are omitted to prevent text-response bias.
4. **Fuzzy resolver makes an HA REST API call per tool invocation** — adds
   ~50-200ms. Mitigated in Stage 3 by local entity cache.
5. **SSL is container-internal** — `/app/certs` is a named Docker volume.
   Certs persist across restarts but don't survive `docker compose down -v`.
   Recommended: change the volume to a host bind mount for backup resilience.
6. **HA MCP tool schema rigidity** — even with reinforcement and argument
   fixup, the model occasionally refuses to call tools ("do it or don't, I
   don't care") due to persona bias. Logged as a roadmap item.

---
