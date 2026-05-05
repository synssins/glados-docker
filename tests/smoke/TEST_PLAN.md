# TEST_PLAN.md — GLaDOS Smoke Suite

**Phase 2 deliverable.** Specific tests proposed for each tier. The
operator marks each as `APPROVED`, `MODIFY` (with notes), or `SKIP`
before any implementation code is written.

## Decisions folded in from operator answers (2026-05-05)

1. **TLS:** Default scheme is `https://`. The runner SHOULD detect an
   `http://` config and emit a **warning** (not a failure). Production
   target is HTTPS-green.
2. **Auth:** No API token mechanism exists. Smoke logs in via
   `POST /login` on port 8052 with `admin / glados` and caches the
   `glados_session` cookie for the duration of the run. Credentials
   come from env vars (`GLADOS_SMOKE_USER`, `GLADOS_SMOKE_PASS`),
   defaulting to `admin` / `glados`. Never committed.
3. **Log access:** API-first. The suite uses authenticated
   `GET /api/logs/tail?source=container` on 8052. SSH is available as a
   future fallback but not the default. Where the existing API does not
   cover a state we'd want to read, this plan calls it out as a "gap"
   for the operator to decide on (separate task, NOT part of this
   smoke build).
4. **Out of scope:** wake word (not in container), MQTT (not yet
   wired), Bitfocus / Companion / Stream Deck (absent), Hue / BiFrost
   (absent). No probes proposed.

## Conventions

- Each test has a stable ID `tierN::short_name` used by the diff
  reporter to match across runs. Once approved, IDs MUST NOT change
  even if the display name evolves.
- "Mutates" means the test changes container state (memory, config,
  emotion). Smoke avoids mutating tests by default.
- "Auth required" means the test consumes the `glados_session` cookie
  obtained at suite start. If login fails, dependent tests SKIP with
  reason `authentication unavailable`.
- Runtimes are estimates; budgets per tier are hard targets the
  selected test set should respect.
- All HTTP probes use a 5 s default per-request timeout unless noted.
- `requests.Session.verify` defaults to `True` for HTTPS. If the live
  cert is self-signed, a `GLADOS_SMOKE_INSECURE=1` env override
  disables verification with a one-time runner warning. Default behavior
  on prod is full verify.

---

# Tier 1 — Health (budget: under 10 seconds total)

Tier 1 is the "is the system alive at all" probe. Five tests, all
parallelizable, no auth needed except the login probe.

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier1::login_succeeds`             | PROPOSED |
| `tier1::api_health_ok`              | PROPOSED |
| `tier1::webui_health_ok`            | PROPOSED |
| `tier1::webui_health_public_ok`     | PROPOSED |
| `tier1::audio_server_responding`    | PROPOSED |
| `tier1::log_baseline_capture`       | PROPOSED |
| `tier1::scheme_is_https`            | PROPOSED |

### tier1::login_succeeds
- **Asserts:** `POST https://<host>:8052/login` with form
  `username=admin&password=glados` returns 200 (or 302) and a
  `Set-Cookie: glados_session=...` header.
- **Setup:** none (precedes all auth-dependent tests).
- **Runtime:** ~1 s.
- **Risk:** mutates only the session table (creates a session record).
  Acceptable — sessions are short-lived; the suite calls `/logout` in
  teardown to clean up.
- **Why first:** every Tier 2 authed probe depends on this. Without
  it, the suite still completes Tier 1 but Tier 2 authed tests SKIP.

### tier1::api_health_ok
- **Asserts:** `GET https://<host>:8015/health` returns 200 and JSON
  `{"status": "ok", "engine": "running"}`.
  Reference: `glados/core/api_wrapper.py:4504-4521`.
- **Setup:** none.
- **Runtime:** <500 ms.

### tier1::webui_health_ok
- **Asserts:** `GET https://<host>:8052/health` returns 200.
  Reference: `glados/webui/tts_ui.py:1936`.
- **Setup:** none.
- **Runtime:** <500 ms.

### tier1::webui_health_public_ok
- **Asserts:** `GET https://<host>:8052/api/health/public` returns
  200 and `{"services": [...]}` where every entry's `status == "ok"`.
  This single probe covers API / TTS-upstream / STT-upstream / HA in
  one call. Reference: `glados/webui/tts_ui.py:3329-3377`.
- **Setup:** none (public endpoint).
- **Runtime:** ~1 s (the handler internally probes 4 services with 3 s
  timeouts; total bounded by slowest).
- **Note:** "TTS" and "STT" rows in this aggregate refer to the
  externally configured speaches URL, not the in-container TTS/STT.
  The container's own TTS/STT are exercised in Tier 2.

### tier1::audio_server_responding
- **Asserts:** `GET https://<host>:5051/` returns ANY HTTP response
  (200, 403, 404 all acceptable). Proves the SimpleHTTPRequestHandler
  listener at `glados/ha/homeassistant_io.py:110-128` is up.
- **Setup:** none.
- **Runtime:** <500 ms.

### tier1::log_baseline_capture
- **Asserts:** none (this is a fixture-style "test" that records the
  capture timestamp into the report's `details`). Subsequent tests in
  Tier 2 use this timestamp as the lower bound when scanning for new
  errors.
- **Setup:** runs once, before any other Tier 2 log probe.
- **Runtime:** instant.
- **Status field:** always `PASS` if the timestamp is captured; if
  this fails the suite has a deeper problem.

### tier1::scheme_is_https
- **Asserts:** the configured `host` URL begins with `https://`. If
  it begins with `http://`, the test reports a **WARNING** (skip with
  `xfail`-style message) — does NOT fail the suite. Per operator: HTTP
  is acceptable but not the prod state.
- **Setup:** reads `config.yaml` value.
- **Runtime:** instant.

**Tier 1 total budget:** ~4-5 s if probes run in parallel.

---

# Tier 2 — Component reachability (budget: under 60 seconds total)

Tier 2 exercises each voice/integration component independently with
the minimum work needed to prove "wired up".

## Voice pipeline (in-container)

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier2::tts_voices_listed`          | PROPOSED |
| `tier2::tts_synth_smoke`            | PROPOSED |
| `tier2::stt_route_alive`            | PROPOSED |
| `tier2::llm_models_listed`          | PROPOSED |
| `tier2::llm_slots_configured`       | PROPOSED (auth) |

### tier2::tts_voices_listed
- **Asserts:** `GET https://<host>:8015/v1/voices` returns 200 and the
  body's `voices` list contains the literal string `"glados"`.
  Reference: `glados/api/app.py:115`, `glados/core/api_wrapper.py:3392`.
- **Setup:** none.
- **Runtime:** <300 ms.
- **Why useful:** confirms TTS module imported, `/app/models/TTS/`
  scanned successfully.

### tier2::tts_synth_smoke
- **Asserts:** `POST https://<host>:8015/v1/audio/speech` with body
  `{"input": "smoke test", "voice": "glados", "response_format": "wav"}`
  returns 200, `Content-Type` starts with `audio/`, and body length
  > 1024 bytes.
  Reference: `glados/core/api_wrapper.py:3474`.
- **Setup:** none.
- **Runtime:** ~2-4 s on first call (ONNX session cold load), <1 s on
  warm subsequent calls.
- **Risk:** none — TTS does not write to memory. Generated audio is
  discarded in Python (not written to disk by this test).
- **Note on flakiness:** if the test fails first-run on a freshly
  recreated container due to the cold-load delay, raise this test's
  per-request timeout to 8 s (configurable via `config.yaml`).

### tier2::stt_route_alive
- **Asserts:** `POST https://<host>:8015/v1/audio/transcriptions` with
  no `file` field returns 4xx (400 or 422), NOT 404 or 502. Proves the
  route is registered.
  Reference: `glados/core/api_wrapper.py:3476`.
- **Setup:** none.
- **Runtime:** <300 ms.
- **Note:** this is a weak signal — it confirms the route handler
  exists but does NOT prove ASR runs. Real STT verification requires
  a fixture WAV (Tier 3).
- **Marker:** `@pytest.mark.tier2`. **Open question:** is a 4xx-without-
  fixture probe worth keeping, or should we skip STT entirely in Tier 2
  with `requires_audio_fixtures`? Recommendation: keep it; cheap and
  it catches the case where the handler is wired but the route is
  somehow misregistered.

### tier2::llm_models_listed
- **Asserts:** `GET https://<host>:8015/v1/models` returns 200 with a
  `data` array containing an entry whose `id == "glados"`.
  Reference: `glados/core/api_wrapper.py:3372`.
- **Setup:** none.
- **Runtime:** <300 ms.
- **Note:** this only proves the API listener serves the
  OpenAI-compat endpoint. It does NOT prove any upstream LLM is
  reachable. The four slots are not enumerated by this endpoint.

### tier2::llm_slots_configured (auth required)
- **Asserts:** `GET https://<host>:8052/api/config/llm` returns 200
  with `interactive`, `autonomy`, `triage`, `vision` slot objects,
  each with non-empty `url` (slot URL configured by operator).
  Reference: `glados/webui/tts_ui.py:1854-1863, 4582`.
- **Setup:** auth session.
- **Runtime:** <500 ms.
- **Asserts NOT made:** does NOT probe upstream LLM endpoints
  themselves (that's outside container scope per operator memory and
  CLAUDE.md §1).
- **Gap noted:** there is no API path that exercises each slot with a
  cheap test prompt. See "API enhancement opportunities" below.

## Integrations

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier2::ha_entities_present`        | PROPOSED |
| `tier2::ha_aggregate_ok`            | PROPOSED (auth) |
| `tier2::chromadb_writable`          | PROPOSED (auth) |
| `tier2::mcp_plugins_listed`         | PROPOSED (auth) |
| `tier2::vision_url_configured`      | PROPOSED (auth) |

### tier2::ha_entities_present
- **Asserts:** `GET https://<host>:8015/entities` returns 200 with a
  list (or dict) whose entity count > 0. Proves HA WS authenticated
  AND `EntityCache` populated AND registry pulls succeeded.
  Reference: `glados/core/api_wrapper.py:3376`.
- **Setup:** none.
- **Runtime:** <500 ms.

### tier2::ha_aggregate_ok (auth required)
- **Asserts:** `GET https://<host>:8052/api/health/aggregate` returns
  200 and the `services.HA` entry shows ok. Reference:
  `glados/webui/tts_ui.py:3258-3327`.
- **Setup:** auth session.
- **Runtime:** ~1 s (the handler hits HA /api/ with bearer).
- **Note:** the public variant `/api/health/public` already covers HA
  (Tier 1). This auth variant adds ChromaDB to the same probe — see
  next test.

### tier2::chromadb_writable (auth required)
- **Asserts:** the `services.ChromaDB` entry in
  `/api/health/aggregate` shows ok (path exists + writable).
  Reference: `glados/webui/tts_ui.py:3320-3325`.
- **Setup:** auth session (same call as `ha_aggregate_ok`; both can be
  derived from one HTTP call to amortize cost).
- **Runtime:** included in the prior call.

### tier2::mcp_plugins_listed (auth required)
- **Asserts:** `GET https://<host>:8052/api/plugins` returns 200 and
  a list-shaped body. Asserts shape only, NOT contents (the operator
  may have zero plugins or many; either is fine).
  Reference: `glados/webui/tts_ui.py:1976`.
- **Setup:** auth session.
- **Runtime:** <500 ms.

### tier2::vision_url_configured (auth required)
- **Asserts:** `GET https://<host>:8052/api/config/services` returns
  200 with a `vision` block. If `vision.url` is non-empty, the test
  passes; if empty, the test SKIPS with reason "vision feature
  disabled by config" (not a failure — vision is optional).
- **Setup:** auth session.
- **Runtime:** <500 ms.
- **Gap noted:** there is no in-container API that probes the vision
  URL itself; doing so would mean reaching the operator's vision
  service from this test, which is "container scope" boundary
  territory. **Recommend: leave probing of the vision URL out of
  smoke. Adding a `/api/health/aggregate` row for vision (mirroring
  the existing API/TTS/STT/HA/ChromaDB rows) would be the cleanest
  fix — see "API enhancement opportunities".**

## API surface integrity

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier2::api_attitudes_loaded`       | PROPOSED |
| `tier2::api_emotion_state`          | PROPOSED |
| `tier2::api_semantic_status`        | PROPOSED |

### tier2::api_attitudes_loaded
- **Asserts:** `GET https://<host>:8015/api/attitudes` returns 200 and
  a non-empty list/dict shape. Reference: `api_wrapper.py:3378`.
- **Runtime:** <300 ms.

### tier2::api_emotion_state
- **Asserts:** `GET https://<host>:8015/api/emotion/state` returns 200
  with PAD-shaped fields (e.g. `pleasure`, `arousal`, `dominance`).
  Proves the emotion engine is alive. Reference: `api_wrapper.py:3390`.
- **Runtime:** <300 ms.
- **Note:** the test should accept the PAD field names that the API
  actually returns; will confirm exact shape at implementation time.

### tier2::api_semantic_status
- **Asserts:** `GET https://<host>:8015/api/semantic/status` returns
  200. Body shape is whatever the handler returns; assertion is just
  status code + valid JSON. Reference: `api_wrapper.py:3386`.
- **Runtime:** <300 ms.

## Auth + logs

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier2::auth_status_admin`          | PROPOSED (auth) |
| `tier2::log_groups_readable`        | PROPOSED (auth) |
| `tier2::no_recent_errors`           | PROPOSED (auth) |

### tier2::auth_status_admin (auth required)
- **Asserts:** `GET https://<host>:8052/api/auth/status` returns 200
  with `authenticated: true` and a role consistent with the configured
  smoke user (default `admin`). Reference: `tts_ui.py:1719`.
- **Runtime:** <300 ms.

### tier2::log_groups_readable (auth required)
- **Asserts:** `GET https://<host>:8052/api/log_groups` returns 200
  and a non-empty list. Proves the loguru-group registry initialised.
  Reference: `tts_ui.py:1984`.
- **Runtime:** <300 ms.

### tier2::no_recent_errors (auth required)
- **Asserts:** `GET https://<host>:8052/api/logs/tail?source=container&lines=200`
  returns 200, and the returned text contains zero lines matching the
  pattern `(CRITICAL|FATAL|Traceback|Unhandled exception)` since the
  baseline timestamp captured in `tier1::log_baseline_capture`.
  Reference: `tts_ui.py:1843, 4103-4116`.
- **Runtime:** ~1 s.
- **Failure mode:** if the docker socket isn't mounted, this returns
  500. Test SKIPS with reason "log endpoint unavailable" rather than
  fails — log readability is operationally optional.
- **Note on noise:** WARNING-level loguru output is allowed; only
  ERROR/CRITICAL/Traceback signals are treated as failures.
  Configurable via `config.yaml: log_severity_threshold`.

**Tier 2 total budget:** ~25-40 s with sequential execution; ~15-20 s
if the suite parallelizes the independent probes.

---

# Tier 3 — End-to-end (budget: under 90 seconds, runs less often)

Tier 3 proves the full pipeline. Skipped by default; enabled with
`-Full` or by setting `GLADOS_SMOKE_TIER3=1`.

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier3::stt_synth_roundtrip`        | PROPOSED |
| `tier3::e2e_voice_pipeline`         | PROPOSED (mutates) |

### tier3::stt_synth_roundtrip
- **Asserts:** TTS-then-STT round-trip without invoking the LLM:
  1. `POST /v1/audio/speech` with `{"input": "the operator is testing"}` →
     wav bytes.
  2. `POST /v1/audio/transcriptions` with the wav from step 1.
  3. Assert the transcript is non-empty AND has Levenshtein distance
     ≤ 5 from "the operator is testing" (case-insensitive,
     punctuation-stripped).
- **Setup:** no fixtures required — TTS produces the test audio.
- **Runtime:** ~3-6 s (TTS synth + STT decode).
- **Risk:** none. No memory write; no LLM call; no HA call.
- **Why this matters:** proves both audio surfaces work end-to-end
  WITHOUT requiring the operator to record fixture WAVs first.
- **Marker:** `@pytest.mark.tier3`. Always runs in Tier 3; no fixture
  dependency.

### tier3::e2e_voice_pipeline (MUTATES — opt-in only)
- **Asserts:** the canonical voice flow:
  1. Load `tests/smoke/fixtures/query_what_time.wav` (operator records
     this later).
  2. `POST /v1/audio/transcriptions` → transcript.
  3. `POST /v1/chat/completions` with the transcript as user message,
     a benign system prompt, `max_tokens=64`, `temperature=0.2`.
     **NOTE: temperature MUST NOT be 0.0 — operator memory says this
     crashes the worker on the current OpenArc upstream.**
  4. `POST /v1/audio/speech` with the LLM response.
  5. Assert: transcript non-empty, response non-empty, audio bytes
     > 1024, total elapsed < threshold (default 30 s, configurable).
- **Setup:** requires `query_what_time.wav` fixture. Without it,
  test SKIPS with reason `requires_audio_fixtures`.
- **Runtime:** ~10-25 s typical, can spike to 60 s on cold caches.
- **Risk:** **MUTATES.** Step 3 writes to `_conversation_store`. Per
  operator constraint "no tests that pollute conversation history",
  this test is **opt-in only**. Run with
  `pytest -m tier3 --include-mutating` or via
  `.\smoke.ps1 -Full -IncludeMutating`. Default Tier 3 runs
  `tier3::stt_synth_roundtrip` only.
- **Sentinel utterance:** the prompt asks for a query that does NOT
  fire HA automations. "What time is it" is safe — it routes through
  the time fast-path (per operator memory `project_glados_time_hallucination`)
  but does not command any device. **Operator: confirm "what time is it"
  is the right sentinel, OR specify a different one.**
- **Asserts NOT made:** exact LLM response text. LLM is non-deterministic.
- **Pollution mitigation:** the test could be made non-mutating if the
  GLaDOS API exposed a `dry_run: true` flag on `/v1/chat/completions`
  that suppresses memory writes. See "API enhancement opportunities".

**Tier 3 total budget:** ~6 s default (round-trip only), ~30 s with
`--include-mutating` and fixtures available.

---

# Tier 4 — Regression diff (runs only when explicitly requested)

Tier 4 compares pre/post behavior on a fixed prompt set. Triggered
explicitly: `pytest tests/smoke -m regression --baseline=<dir>`.
Operator's primary use case: "did the model swap or config change
make GLaDOS behave differently?"

| ID                                  | Decision |
|-------------------------------------|----------|
| `tier4::baseline_capture`           | PROPOSED |
| `tier4::baseline_compare`           | PROPOSED (mutates) |

### tier4::baseline_capture
- **Mode:** `pytest tests/smoke -m regression --capture-baseline`.
- **Action:** for each prompt in
  `tests/smoke/baselines/_prompts.yaml`, calls
  `POST /v1/chat/completions` with `temperature=0.2` (NEVER 0.0),
  `max_tokens=128`, captures response text + latency + which
  upstream model handled it (from response metadata, if exposed —
  otherwise from `/api/config/llm` snapshotted at the same time).
- **Output:** writes
  `tests/smoke/baselines/<UTC-timestamp>/{prompt_id}.json` per prompt
  PLUS a `_meta.json` recording git SHA, host, image SHA, model
  identifiers per slot, and capture timestamp.
- **Risk:** mutates conversation store (one row per prompt).
  Acceptable for operator-triggered baseline runs; not acceptable for
  unattended runs. Will print a confirmation prompt before capture
  unless `--no-confirm` is passed.
- **Open question:** the prompt set itself. Recommendation:
  start with three prompts that exercise different paths:
  1. `"what time is it"` — time fast-path
  2. `"what's the weather"` — weather fast-path
  3. `"tell me a fun fact about portals"` — long-form chat path
  Operator: confirm or supply your preferred prompt set.

### tier4::baseline_compare (MUTATES)
- **Mode:** `pytest tests/smoke -m regression --baseline=<dir>`.
- **Action:** runs the same prompt set, asserts each new response:
  - is non-empty,
  - has the same component path (same model name in slot used) as
    baseline,
  - latency is within 2× of baseline (configurable via
    `config.yaml: regression_latency_factor`),
  - response text is structurally similar (length within 50% of
    baseline; rejection if length swings 10× — a stub-vs-real-output
    detector). Exact text equality NOT required (LLM
    non-determinism).
- **Risk:** mutates conversation store. Same opt-in posture as Tier 3.
- **Reports diffs:** when slot model changed, latency exceeded
  threshold, or length swung outside the band — each reported as a
  per-test failure with details for the HTML report.

**Tier 4 budget:** depends entirely on prompt count and model latency.
Typical: 3 prompts × 5 s = 15 s. Hard cap at 90 s — anything beyond
that fails the suite with a "regression run timed out" assertion.

---

# Test fixtures, harness, and pytest mechanics

## Pytest markers (declared in pyproject.toml addition or smoke conftest)
- `tier1`, `tier2`, `tier3`, `tier4`
- `regression` (alias for tier4 when used with --baseline flags)
- `slow` — anything >10 s individually
- `requires_audio_fixtures` — SKIP if WAVs missing
- `requires_auth` — SKIP if `tier1::login_succeeds` failed
- `requires_log_endpoint` — SKIP if `/api/logs/tail` returned 500
- `mutates` — opt-in; NOT run unless `--include-mutating` passed

## Fixtures provided in conftest.py
- `host: str` — base URL from `config.yaml`, env-overridable
- `http: requests.Session` — unauthenticated session, scheme-correct
- `auth_http: requests.Session` — logged-in session (or `None` if login
  failed). Tests marked `requires_auth` are skipped by collection hook
  when `auth_http is None`.
- `log_baseline: float` — UNIX timestamp captured at suite start.
- `audio_fixture(name)` — loader for `tests/smoke/fixtures/<name>.wav`
  with skip-if-missing semantics.

## Configuration knobs (config.yaml — defaults below)
```yaml
host: "glados.example.com"
ports:
  api: 8015
  webui: 8052
  audio: 5051
scheme: "https"          # http triggers warning, not failure
verify_tls: true         # GLADOS_SMOKE_INSECURE=1 overrides
auth:
  username_env: GLADOS_SMOKE_USER  # default: admin
  password_env: GLADOS_SMOKE_PASS  # default: glados
timeouts:
  default_request_s: 5
  tts_synth_s: 8         # higher to absorb cold load
  log_tail_s: 10
fixtures:
  audio_dir: "tests/smoke/fixtures"
sentinel:
  utterance: "what time is it"   # operator-confirmable
log_severity_threshold: ERROR
regression_latency_factor: 2.0
```

NO secrets in this YAML. Credentials are env-only.

## Console output contract

After every run, one line:

```
GLaDOS Smoke: 14/16 PASS (1 skip, 1 fail) in 38.2s — see latest.html
```

Exit code 0 if all non-skipped tests passed; 1 otherwise.

---

# API enhancement opportunities (NOT in scope of this build)

These are gaps where the existing GLaDOS API forces smoke into
weaker probes than would otherwise be available. Listed for the
operator's awareness — adopting any of them is a separate task.

1. **`POST /v1/chat/completions` — `dry_run: true` flag.**
   Would suppress conversation-store writes, allowing Tier 3 / Tier 4
   to run unattended without polluting memory. Today the only safe
   workaround is opt-in mutating runs.
2. **`/api/health/aggregate` — vision row.** The handler probes API,
   TTS, STT, HA, ChromaDB but skips vision. A vision row mirroring
   the others would let smoke verify the operator's vision URL is
   reachable without leaving the container scope.
3. **`/api/health/aggregate` — upstream LLM rows.** Each of the four
   slots could ship a "trivially small" reachability probe (HEAD
   request to the slot URL, or a single-token completion with a
   1 s timeout). Would let smoke detect "Ollama died" without crossing
   the container boundary.
4. **`GET /api/version`.** Currently no path exposes the running git
   SHA / image SHA / build time. Smoke can't bind a regression
   baseline to a specific build today; it has to read the docker
   inspect output via SSH.
5. **`GET /api/uptime`.** Detecting "the container was just restarted
   between smoke runs" matters for log-baseline diffing.
6. **`GET /api/mcp/status`.** Today MCP server runtime state is
   visible only via authenticated `/api/plugins` and only as
   "configured" — not "currently connected and tools loaded". A
   richer status endpoint would let smoke detect a broken plugin
   without shelling through `/api/logs/tail` patterns.
7. **`GET /api/persona/rewriter/status`.** Verifies the rewriter has
   exercised its triage slot at least once since boot. Today the
   rewriter is observable only by inducing a Tier 1 HA call and
   inspecting the response shape.

These are all read-only additions that would tighten smoke without
adding state-changing surface. **Operator: which (if any) should
become follow-up tasks after this smoke build lands?**

---

# Summary table

Total tests proposed: **24** (counting the audio_server probe and
the log baseline as tests).

| Tier | Count | Auth | Mutating | Default-skipped | Budget   |
|------|------:|-----:|---------:|----------------:|---------:|
| 1    |     7 |    1 |        0 |               0 | <10 s    |
| 2    |    13 |    9 |        0 |               0 | <60 s    |
| 3    |     2 |    0 |        1 |               1 | <90 s    |
| 4    |     2 |    0 |        2 |               2 | on-demand|

After the operator marks each test APPROVED / MODIFY / SKIP, Phase 3
implements the approved set.

---

# STOP — operator review needed

The operator decides per test. A simple way:

```
APPROVED: tier1::*, tier2::tts_*, tier2::ha_*, tier2::api_*
MODIFY:   tier2::stt_route_alive — drop entirely; weak signal
SKIP:     tier3::e2e_voice_pipeline — too slow, will revisit
```

Or paste-back the table with decisions filled in. Anything the operator
SKIPs is removed from the implementation plan.

Pending answers I still need before any code lands:
- The two open questions in this plan:
  - Tier 2 STT 4xx-handler probe — keep or drop?
  - Tier 3 sentinel utterance — confirm "what time is it"?
- Tier 4 prompt set — confirm or supply.
- Which (if any) "API enhancement opportunities" should spawn as
  separate follow-ups? (Particularly #1, the `dry_run` flag —
  it's the cleanest way to make Tier 3 unattended-safe.)
