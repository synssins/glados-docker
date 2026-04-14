# GLaDOS Container — Change Log

Every structural change to the container repo, in chronological order. Each
entry records what changed, why, and what side-effects the operator should
expect. This is the running journal for the containerization work — it
supplements git history with *why* rather than *what*.

---

## Change 1 — Pure-middleware refactor: remove bundled TTS, trim compose

**Date:** 2026-04-14
**Status:** In progress
**Rationale:** Align the container with the architecture plan's target state
("GLaDOS is CPU-only middleware"). The initial port brought the full host-native
stack into the container — including ONNX TTS engines (`glados/TTS/tts_glados.py`,
`tts_piper.py`, `tts_kokoro.py`) and compose services for Ollama, speaches, and
Open WebUI. Per the plan, only `glados` + `chromadb` belong in this compose
stack; TTS/STT is speaches's job; Ollama is a separately-deployed dependency
reached via `OLLAMA_URL`; Open WebUI is just one possible client, not part of
GLaDOS.

### Changes

**TTS: ONNX engines replaced with speaches HTTP client**
- New: `glados/TTS/tts_speaches.py` — `SpeachesSynthesizer` class implementing
  `SpeechSynthesizerProtocol`. POSTs to `${SPEACHES_URL}/v1/audio/speech`,
  decodes returned WAV bytes to `NDArray[float32]`. Passes `length_scale`,
  `noise_scale`, `noise_w` through as OpenAI `extra_body` params for attitude
  support.
- Rewritten: `glados/TTS/__init__.py` factory — `get_speech_synthesizer(voice)`
  now returns a `SpeachesSynthesizer` configured for that voice name. The
  voice is a speaches-side identifier (`glados`, `af_heart`, `kokoro-*`, etc.)
  not a local ONNX file.
- Deleted: `glados/TTS/tts_glados.py`, `glados/TTS/tts_piper.py`,
  `glados/TTS/tts_kokoro.py`, `glados/TTS/phonemizer.py` — all local ONNX
  inference gone.
- Deleted: `glados/tts/` (the lowercase directory, duplicate factory — only
  the uppercase `glados/TTS/` is canonical).
- Rewritten: `glados/api/tts.py` — the `/v1/audio/speech` endpoint now proxies
  to speaches instead of synthesizing locally. Kept so the WebUI's TTS
  generator page (one-off utterances) and existing clients continue to work
  unchanged; the container remains a valid OpenAI-compatible TTS endpoint.

**Dockerfile: GPU build path removed**
- Removed: `USE_GPU` build arg and the `onnxruntime-gpu` swap block. Container
  is pure middleware — no ONNX inference runs in it anymore (ASR notwithstanding,
  see below).
- Kept: `onnxruntime` in dependencies — `glados/ASR/` still uses it. STT will
  follow TTS out to speaches in a later change, at which point ASR can go too
  and onnxruntime comes out.

**Compose: trimmed to glados + chromadb**
- Removed `ollama` service — operator runs Ollama separately; container
  connects via `OLLAMA_URL`.
- Removed `speaches` service — same pattern.
- Removed `open-webui` service — Open WebUI is a client, not a GLaDOS
  subsystem. Operators who want it run it in their own compose.
- Deleted hardware overlay files: `docker/compose.cuda.yml`,
  `compose.ipex.yml`, `compose.rocm.yml`. They overlaid GPU runtime onto
  services that no longer exist in this compose.
- Kept `chromadb` — it has exactly one consumer (GLaDOS), is meaningless
  without it, and belongs to this unit of deployment.

**Configuration surface**
- `.env.example` — documented `OLLAMA_URL`, `SPEACHES_URL`, `CHROMADB_URL`
  as external-dependency URLs with `host.docker.internal` defaults.
- `configs/config.example.yaml` — same, plus removed any `tts.model_path` or
  `tts.gpu` knobs.
- `README.md` — rewrote the port map; "downstream services" table is gone,
  replaced with "external services this container expects to reach."

### Side effects the operator must know

1. **Speaches must be running and reachable** at `${SPEACHES_URL}` before the
   container starts, or TTS requests return HTTP errors. Health check on
   `/health` still passes — the container itself is fine, it just can't
   synthesize speech.
2. **GLaDOS voice must be registered in speaches.** This is Stage 4 work per
   the architecture plan. Until that's done, use any speaches-available voice
   (`af_heart`, `am_michael`, etc.) by setting `voice` in `config.yaml`.
   Character identity is preserved by the Ollama model + system prompt; the
   voice is the surface-level audio.
3. **TTS latency changes.** Bundled Piper VITS at ~0.3–1.6s on GPU is
   replaced by whatever speaches delivers. Stage 0 Task 0.1 in the
   architecture plan calls for measuring this — verify before relying on
   announcements for time-sensitive events (doorbell, locks).
4. **GPU-bearing hosts gain no benefit** from the container. Put the GPU
   where Ollama and speaches live.
5. **Open WebUI users**: point Open WebUI at `http://<glados-host>:8015/v1`
   (OpenAI base URL) and at `http://<speaches-host>:8800/v1` for audio.
   GLaDOS does not bundle Open WebUI.

---

## Change 2 — Fix CI: Snyk + Docker build

**Date:** 2026-04-14
**Status:** Complete
**Rationale:** Both GitHub Actions workflows have been red since 2026-04-12.
Neither had ever produced a successful run. Root causes identified and fixed.

### Changes

**`pyproject.toml` — build backend**
- Was: `build-backend = "setuptools.backends.legacy:build"` (does not exist as
  a real Python module — inherited from the initial scaffold, untested).
- Now: `build-backend = "setuptools.build_meta"` — the standard setuptools
  PEP 517 backend.
- Effect: `pip install -e ".[api]"` works, which unblocks the Snyk Python
  scan.

**`scripts/.gitkeep` — placeholder**
- The Dockerfile does `COPY scripts/ ./scripts/`, but `scripts/` was empty
  and git does not track empty directories. `docker buildx` failed with
  `"/scripts": not found`.
- Added `scripts/.gitkeep` documenting the directory's purpose and ensuring
  git tracks it. Operator tooling lands here in later stages.

**`.github/workflows/snyk.yml` — restructured for correctness**
- Split into two jobs: `snyk-python` (SCA of Python deps) and `snyk-docker`
  (container image scan). Previously a single job.
- `snyk-docker` now builds the image inline via `docker/build-push-action@v5`
  with `load: true`, so the Snyk docker action has a real image to scan.
  Previously it referenced `glados:latest` which was never built in that
  workflow — the scan always soft-failed on "image not found" and
  `continue-on-error: true` hid the fact.
- Removed `continue-on-error` — both scans now fail the job on HIGH/CRITICAL
  findings.
- Added `--file=Dockerfile` to the docker scan for Dockerfile-level checks.
- Explicit comment that `SNYK_TOKEN` is a GitHub Actions repository secret,
  rotatable from Snyk UI.

**GitHub Actions secret — rotated**
- `SNYK_TOKEN` secret updated to the new Snyk PAT provided by the operator.
- Snyk org ID confirmed: `b905c516-c213-433b-973a-9d26adf03871`.
- Snyk GitHub integration ID (for linking projects in Snyk UI, not used by
  CI): `e8f971c9-87a9-42c6-956c-8b56a8697418`. Informational only.

### Side effects

1. **Snyk Docker scan now runs on every push/PR.** Build time goes up by
   ~1–2 minutes on the Snyk workflow because it now builds the image.
   Acceptable tradeoff — the scan was previously not running at all.
2. **Snyk HIGH/CRITICAL findings will now block CI.** Previously they were
   silently soft-failed. Expect the first post-fix run to surface any
   existing findings; address via fixes or `.snyk` suppression file.
3. **No branch protection rule** is set on `main`, so failing Snyk runs do
   not yet block merges — they are informational. Enabling required status
   checks on `main` is a separate operator decision.

---
