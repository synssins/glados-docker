# Self-Containment Project

Working document for the "minimum-setup, self-contained GLaDOS container"
initiative. Updated continuously while this is an active project.

**Last updated:** 2026-04-25 (auth rebuild shipped + post-deploy hotfixes)
**Active branch:** `main`
**Deployed commit at start of project:** `a134723`
**Deployed commit now:** `cd3bad2` (image SHA `0a6a03e3`)

---

## Goal

A single Docker container that ships with all ML inference bundled and
needs only a minimal `compose.yml`:

```yaml
services:
  glados:
    image: ghcr.io/synssins/glados-docker:latest
    container_name: glados
    restart: unless-stopped
    ports:
      - "8015:8015"   # Chat API + /v1/audio/speech + /v1/audio/transcriptions
      - "8052:8052"   # WebUI
      - "5051:5051"   # Audio file server (for HA media_player playback)
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ${DOCKERCONFDIR}/glados/configs:/app/configs
      - ${DOCKERCONFDIR}/glados/data:/app/data
      - ${DOCKERCONFDIR}/glados/logs:/app/logs
      - ${DOCKERCONFDIR}/glados/audio_files:/app/audio_files
      - ${DOCKERCONFDIR}/glados/certs:/app/certs
    environment:
      - TZ=${TZ:-UTC}
```

Everything else lives inside the container and is editable via the WebUI,
with YAML as the authoritative source of truth.

---

## Architecture decisions

### YAML is authoritative

Every runtime configuration value lives in `/app/configs/*.yaml`. The
`config_store` reads these live; the WebUI writes them on Save; changes
that affect engine state trigger a hot-reload. Env vars (when present)
act as *fallback defaults* only — YAML wins whenever a value is set.

This enables the live-reload token-rotation pattern shipped in commit
`52a57e5` (`ha_ws: refresh token from cfg on every auth handshake`).
Moving secrets to `.env` would break this.

### Everything self-contained

The container runs its own ML inference rather than calling external
services. Already done this session:

- **TTS** — local VITS ONNX via `glados/TTS/tts_glados.py` + bundled
  phonemizer. No Speaches HTTP round-trip. See
  [CHANGES.md → self-contained TTS](../CHANGES.md).
- **STT** — local Parakeet CTC ONNX via `glados/ASR/ctc_asr.py` +
  Silero VAD. No Speaches STT HTTP round-trip. POST
  `/v1/audio/transcriptions` handler in `api_wrapper.py`.
- **BGE embeddings** — already bundled in the image (added earlier
  for Tier 2 semantic retrieval).

Still pending:
- **ChromaDB** — running as separate container today. This doc's next
  step embeds it via `PersistentClient`.
- **Vision** — out of scope for now. Still points at AIBox (which is
  offline), so feature is dormant. Bundling a vision ONNX (~5-8 GB)
  would make the container very large; decision deferred.

### What stays external

- **Ollama** (operator's deployment — not in-container scope). The
  container points at `${OLLAMA_URL}` in YAML; any Ollama instance on
  the LAN works.
- **Home Assistant** — the container is *an HA integration*, not HA
  itself.
- **MQTT broker** (optional, for the peer-bus integration) — external.

---

## Session chronology (2026-04-23 → 2026-04-24)

### Day 1 — AIBox retirement + TTS self-containment

1. **AIBox services audit** — identified what was running on the host
   at `<aibox-host>`:
   - `glados-api`, `glados-tts`, `glados-stt`, `glados-vision`
   - `gladys-api`, `gladys-discord`, `gladys-observer`
   - `ollama-ipex-llm` (Arc B60, port 11434), `ollama-glados` (T4, 11436)
   - `com.docker.service` + Docker containers (`glados`, `open-webui`,
     two chromadbs)
   - Native `speaches` Python process on port 8800
   - GitHub Actions self-hosted runner
2. **Shutdown** — stopped everything except Ollama on 11434 and the
   GHA runner. Shut down Docker Desktop entirely. Migrated `open-webui`
   to native Windows via `nssm` service at `C:\AI\open-webui\.venv`.
3. **TTS voice extraction** — initial confusion: I assumed Speaches
   served a GLaDOS Piper voice and started packaging that path. Wrong.
   The actual working GLaDOS TTS lived inside `C:\AI\GLaDOS\` (the
   legacy dnhkng/GLaDOS Python project) as bundled ONNX files.
   Corrected course.
4. **TTS bundled into container** — copied `glados.onnx`,
   `phomenizer_en.onnx`, four pickle files from
   `C:\AI\GLaDOS\models\TTS\` into `models/TTS/` (Git LFS). Ported
   `tts_glados.py` + `phonemizer.py`. Rewrote
   `glados/TTS/__init__.py` factory to default to local synth.
   Added `/v1/audio/speech` + `/v1/voices` routes to `api_wrapper`.
   **RTF 0.08 verified** (1.32 s audio in 0.11 s CPU synth after
   2.9 s cold init).
5. **STT bundled into container** — copied `nemo-parakeet_tdt_ctc_110m.onnx`,
   Silero VAD, config into `models/ASR/` (Git LFS). The ASR
   Python code was already in the container from a prior port; only
   the models + endpoint were missing. Added
   `POST /v1/audio/transcriptions` to `api_wrapper` with auto-resample
   (input → 16 kHz for CTC). Skipped TDT encoder (1.2 GB) — CTC
   alone is sufficient.

### Day 2 — Doorbell debug + screener hardening

1. **Doorbell.yaml + SERVE_HOST** — neither existed in the live
   container after the moves. Created `doorbell.yaml` with real
   G4 doorbell speaker entity + RTSPS stream. Set `SERVE_HOST=<docker-host>`
   in global.yaml (env alone wasn't enough — YAML overrode).
   Published port 5051 in compose.
2. **HA token discovery** — env `HA_TOKEN` was an old/revoked token
   (401 on REST). Live token lives in `global.yaml`; WS path reads
   that per-handshake via commit `52a57e5`. Any code path that
   needs an HA token should use `cfg.ha_token`, not `os.environ["HA_TOKEN"]`.
3. **LLM eval empty-JSON bug** — doorbell screener sent
   `format: "json"` to Ollama, qwen3:14b consistently punted to `{}`.
   Fix: drop the grammar constraint, add `_extract_json` helper that
   strips `<think>` preambles and markdown fences, parse first
   balanced `{...}`. Retry once on empty. Bumped timeout 30 s → 60 s.
4. **First-press-after-restart bug** — pressing the physical doorbell
   fired a state_changed on `event.g4_doorbell_doorbell`, but
   `_on_state_change` filter at line 528 dropped events where
   `old_state == "unknown"`. First-ever press after container restart
   always has `old_state="unknown"` (no cached history). Fix: inserted
   doorbell fast path before the generic filter, triggers screener
   unconditionally on any state_changed of a `doorbell_ring` typed
   entity. [ha_sensor_watcher.py:520ff](../glados/autonomy/agents/ha_sensor_watcher.py).

### Day 2 evening — Phase 1 (ChromaDB embed) + Phase 2 (env purge)

6. **ChromaDB embedded** — replaced `chromadb.HttpClient` with
   `chromadb.PersistentClient` at `/app/data/chromadb/`. Bundled
   `sentence-transformers/all-MiniLM-L6-v2` into the image so first
   use has no network dependency. Deleted the separate
   `glados-chromadb` compose service (backup at
   `docker-compose.yml.bak.pre-chroma-embed`). Verified writes +
   queries round-trip correctly in-process. Commits `05f881b` +
   `db39d56` + `7d6cf68` (chown fix).
7. **Household facts migrated to ChromaDB** — Pet1's descriptive
   content moved out of `personality_preprompt` into atomic ChromaDB
   `semantic` records (6 facts). Validated via chat: GLaDOS now
   retrieves facts on demand and no longer conflates unrelated
   prose ("raids litter box for oranges" hallucination is gone).
   Then bulk-migrated remaining pets + ResidentA + ResidentB + location =
   15 more atomic facts. Preprompt's HOUSEHOLD KNOWLEDGE section
   collapsed from a 320-word prose block to a single-line roster +
   one-sentence EMOTIONAL TONE rule. ~400 tokens/turn saved on
   every LLM call. **Nothing about ResidentA's father appears anywhere
   in preprompt or ChromaDB** (operator-explicit exclusion).
8. **Env-var purge (Phase 2)** — compose env block shrank from 8
   lines to 1 (`TZ=${TZ}`). Dockerfile now bakes
   `GLADOS_ROOT`/`GLADOS_CONFIG`/`GLADOS_CONFIG_DIR`/`GLADOS_DATA`/
   `GLADOS_LOGS`/`GLADOS_AUDIO`/`GLADOS_ASSETS`/`GLADOS_TTS_MODELS_DIR`/
   `GLADOS_PORT`/`WEBUI_PORT`/`SERVE_PORT`/`TTS_BACKEND` as image
   defaults. `ServicesConfig.tts.url` and `stt.url` defaults flipped
   to `http://localhost:8015` (the container's own api_wrapper).
   Dead Speaches env reads removed from config defaults.
   Operator-facing compose stanza now matches the target "minimum
   setup" block documented at the top of this file. Commit `2f245eb`.

### End-of-day state (2026-04-24)

- **Deployed commit:** `2f245eb` on `<docker-host>`
- **AIBox state:** Ollama on `:11434` + GHA runner + native open-webui
  on `:3000`. Everything else stopped.
- **Container state:** TTS ✅ embedded, STT ✅ embedded, ChromaDB ✅
  embedded, doorbell pipeline ✅ wired end-to-end, household facts ✅
  atomic in ChromaDB, compose env block ✅ minimal.
- **Memory page:** 21 household facts across 9 subjects + 56 legacy
  canon records = 77 semantic records.
- **SELF_CONTAINMENT.md phases:**
  - Phase 1 (ChromaDB embed) ✅
  - Phase 2 (env purge) ✅
  - Phase 3 (first-run bootstrap) — deferred, not urgent (live
    deploy is fully configured; this phase matters for *new*
    deployments on empty volumes)
  - Phase 4 (real-press doorbell acceptance test) — pending
    operator availability
  - Phase 5 (README/CLAUDE/roadmap refresh) — pending

### Post-day-2 (2026-04-25) — auth rebuild shipped

- **Deployed commit:** `cd3bad2` (image SHA `0a6a03e3`) on `<docker-host>`
- **Auth rebuild:** merged `f673ae5`; three same-day hotfixes
  (`3511a28`, `918e52c`, `cd3bad2`). Admin login verified; migration
  to `users[]` YAML confirmed; TTS Generator working for unauth.
- **SELF_CONTAINMENT.md phases:**
  - Phase 3 (first-run bootstrap) ✅ — covered by `/setup` wizard
  - Phase 5 (README/CLAUDE/roadmap refresh) ✅ — this commit

---

## Remaining work (prioritized)

### COMPLETE — Auth rebuild (merged `f673ae5`, 2026-04-25)

Multi-user Argon2id auth with itsdangerous sessions and SQLite
session store. Roles: `admin` (full access) / `chat` (chat tab
only). First-run `/setup` wizard. `GLADOS_AUTH_BYPASS=1` recovery
path. TTS/STT public; chat requires login; configuration is
admin-only. Legacy single-password `global.yaml` migrates
transparently on first login.

Three post-merge hotfixes landed same-day on top of `f673ae5`:
- `3511a28` — `auth.db` schema self-init on `connect()`
- `918e52c` — role-aware sidebar, landing page, bottom-left auth area
- `cd3bad2` — admin migration persistence + `/tts` audio JSON parse
  + SPA shell for unauth visitors

Details in CHANGES.md Change 23 (rebuild) and Change 24 (hotfixes).

### Phase 3 — First-run bootstrap (complete, via auth rebuild)

The `/setup` wizard shipped with the auth rebuild (`f673ae5`) covers
first-run admin account creation. A fresh container with no users
redirects to `/setup` automatically. HA token + URL are still set
via the WebUI after login, which the live-reload path handles.
This phase is effectively done.

### Phase 4 — Doorbell acceptance test

Real button press → greeting at door → capture → CTC transcribe →
LLM classify → indoor announcement. All in-container. No external
services except Ollama and HA. Pending operator availability.

### Phase 5 — Documentation sweep

- `README.md` — minimal compose example, remove Speaches setup steps
- `CLAUDE.md` — update "scope discipline" bullet: TTS/STT/Chroma
  all in-container
- `docs/roadmap.md` — mark self-containment items done
- `SESSION_STATE.md` — current deploy commit

---

## Deploy workflow (unchanged)

1. Commit + push to `main`
2. GHA self-hosted runner (on AIBox `<aibox-host>`) builds via
   `.github/workflows/build.yml`, LFS-aware since commit `9afd5d8`
3. Image published to `ghcr.io/synssins/glados-docker:latest`
4. `scripts/deploy_ghcr.py` SSHes to `<docker-host>`, pulls, recreates
5. Verify `/health` on 8015, live-probe affected behaviour

Credentials: operator-specific values live in `C:\src\SESSION_STATE.md`
(not committed). Envvars: `GLADOS_SSH_HOST`, `GLADOS_SSH_PASSWORD`,
`GLADOS_COMPOSE_PATH`.

---

## Operator preferences — calibration for this project

- Surgical changes, reviewable chunks. One structural change per
  commit.
- Diagnose before acting. Live-probe, never assume.
- No secrets in git. `.gitleaks.toml` rules active.
- WebUI is the operator interface. YAML is the storage. Env is the
  deployment-time override and only for values that must exist before
  YAML is loaded (paths, container port bindings).
- Don't push work onto the operator when automation exists. Container
  commit/push/SSH-deploy runs via `scripts/deploy_ghcr.py` using
  documented credentials.
