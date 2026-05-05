# GLaDOS Smoke Suite

A small, modular pytest suite that answers one question fast:
**"Is GLaDOS working correctly right now?"**

Run it after every Docker rebuild, model swap, config change, or merge.

---

## Quick start

From the repo root:

```powershell
.\smoke.ps1
```

This:
1. Runs Tier 1 + Tier 2 (~30-50 s).
2. Writes a JSON record to `tests/smoke/reports/`.
3. Renders an HTML report and copies it to
   `tests/smoke/reports/latest.html`.
4. Opens `latest.html` in your default browser.
5. Prints a one-line console summary and exits 0 on green / 1 on red.

Direct pytest works too:

```powershell
python -m pytest tests/smoke -m "tier1 or tier2" -v
```

`make smoke` does the same on Windows (proxies to `smoke.ps1`).

### Sample reports

See `reports/_examples/` for what the rendered output looks like in
each state:

- `example-all-pass.html` — clean run, all green
- `example-with-failure.html` — one Tier 2 failure with logs/traceback
- `example-with-regression.html` — diff banner showing a regression vs
  the previous run

Open them by double-clicking from Explorer; they're standalone files.

---

## Tiers

| Tier | What it covers                               | Budget  | Default? |
|------|----------------------------------------------|--------:|----------|
| 1    | Health probes (auth, /health, public health) | <10 s   | yes      |
| 2    | Component reachability (TTS/STT/LLM/HA/MCP)  | <60 s   | yes      |
| 3    | End-to-end voice pipeline                    | <90 s   | -Full    |
| 4    | Regression diff (baseline capture/compare)   | varies  | -Tier4   |

`smoke.ps1` flags:

- `-Full` — include Tier 3
- `-Host <name>` — override target host for this run only
- `-IncludeMutating` — include tests that write GLaDOS state
- `-Tier4 -CaptureBaseline` — capture a regression baseline
- `-Tier4 -Baseline <dir>` — compare against a captured baseline
- `-NoOpen` — don't auto-open the browser
- `-Quiet` — suppress per-test pytest chatter
- `-NoDiff` — suppress diff banner in the rendered report

---

## HTML report layout

Top-to-bottom:

1. **Status banner.** Green / red / amber depending on overall result.
   Shows timestamp, target host, scheme (http/https), git commit, and
   total duration.
2. **Diff banner** (when a previous run exists). Compares the current
   run against the most-recent prior JSON. See "Diff feature" below.
3. **Summary cards.** Passed / Failed / Skipped / Duration. Click the
   Failed card to jump to the first failed test.
4. **Trend sparkline.** Pass-rate over the last 10 runs (hidden when
   there's only one run).
5. **Tier sections.** Each tier with its tests collapsed by default;
   failed/regressed rows auto-expand.
6. **Footer** with suite version and links to SURFACE_MAP / TEST_PLAN.

Click a test row to expand. Expanded view shows: what was checked,
expected vs actual, the assertion error and traceback (on failure),
and the last 20 container log lines (on failure, if the auth-gated
log endpoint was reachable).

### Diff feature

The renderer looks up the most recent `smoke-*.json` (other than the
current one) and computes a diff. Tests are matched by their stable
`id` field — never by display name.

**Per-test badges:**
- `regressed` (red) — passed previously, fails now. Auto-expands.
- `recovered` (green) — failed previously, passes now.
- `new` (blue) — first appearance.
- `duration changed` (grey) — same status, runtime > 3× baseline AND
  > 1 second absolute difference.

**Banner states:**
- Regressions present → red banner with clickable list.
- Recoveries only → green banner.
- New / removed only → blue informational banner.
- Nothing changed → muted grey banner referencing previous run time.

**Edge cases the banner notes:**
- Different `target_host` than the previous run.
- Different `git_commit` than the previous run.
- Previous run >30 days old (marked "stale baseline").

`-NoDiff` suppresses the diff entirely.

---

## Configuration

`tests/smoke/config.yaml` is the single source of truth for runtime
settings. The defaults target `https://glados.example.com` with the operator's
admin login.

Common overrides:

```yaml
host: "glados.alt.example.com"           # different deployment
scheme: "http"                  # warns at suite start, doesn't fail
verify_tls: false               # for self-signed certs
disabled_tests:
  - "tier2::stt_route_alive"    # skip a single test by ID
  - "tier2::vision_url_configured"
```

Or via env (per run):

```powershell
$env:GLADOS_SMOKE_HOST = "glados.alt.example.com"
$env:GLADOS_SMOKE_INSECURE = "1"   # equivalent to verify_tls: false
.\smoke.ps1
```

**Credentials never live in config.yaml.** Override:

```powershell
$env:GLADOS_SMOKE_USER = "alt-admin"
$env:GLADOS_SMOKE_PASS = "..."
```

The defaults (`admin` / `glados`) are only there because they're
already known to anyone with shell access on this network. They are
NOT secrets.

### Disabling individual tests

Two ways:

1. **Permanent** — add the test ID to `disabled_tests` in
   `config.yaml`.
2. **One-off** — `pytest -m "tier1 or tier2 and not test_tier2_no_recent_errors"` etc.

The ID format is `tierN::short_name` (e.g. `tier2::tts_synth_smoke`).
The reporter, the disable list, and the diff matcher all key on the
same ID — once a test is approved, the ID never changes even if the
display name evolves.

---

## Audio fixtures (Tier 3 e2e)

Tier 3 has two tests:

- `tier3::stt_synth_roundtrip` — TTS + STT round-trip.
  **No fixture needed.** The suite synthesises its own test audio.
- `tier3::e2e_voice_pipeline` — full WAV → STT → LLM → TTS chain.
  **Mutates the conversation store**; requires a recorded fixture
  AND `-IncludeMutating`.

To enable the full e2e test, record `query_what_time.wav` in
`tests/smoke/fixtures/` per the spec in `fixtures/README.md`. Without
the fixture, the test SKIPS cleanly.

---

## Regression diff (Tier 4)

Capture a baseline:

```powershell
.\smoke.ps1 -Tier4 -CaptureBaseline -IncludeMutating
```

Writes `tests/smoke/baselines/<UTC-timestamp>/<prompt-id>.json` for
each prompt in `config.yaml: regression.prompts`. **Capture writes to
GLaDOS's conversation store** — once per prompt.

Compare against a captured baseline:

```powershell
.\smoke.ps1 -Tier4 -Baseline tests/smoke/baselines/20260505T140000Z -IncludeMutating
```

Asserts each prompt's response is non-empty, the model in use matches,
latency is within `regression.latency_factor` × baseline, and length
is within `regression.length_band`.

Exact text equality is NOT required — LLMs are non-deterministic.

---

## Troubleshooting

**`tier1::login_succeeds` fails.**
Check `GLADOS_SMOKE_USER` / `GLADOS_SMOKE_PASS` if you've changed
credentials. The defaults assume the original auth-rebuild values.
Without a session, every Tier 2 authed test SKIPS.

**`tier1::api_health_ok` fails with connection refused.**
The container isn't running or the host is wrong. Check
`docker ps` on the docker host. Check `config.yaml: host` matches the
deployment.

**`tier2::no_recent_errors` SKIPs with "log endpoint unavailable".**
The container's `/var/run/docker.sock` mount isn't there or is
read-locked. The compose file mounts it `:ro`; if you removed that,
the WebUI Logs page can't tail container stdout. SKIP is correct
behaviour — the test isn't ground-truth without log access.

**`tier2::tts_synth_smoke` times out.**
First request after a container restart pays the ONNX cold-load cost
(~2-4 s). Bump `timeouts.tts_synth_s` in `config.yaml` if your host
is slow.

**`/api/health/public` returns a non-ok service.**
That's the smoke suite doing its job — investigate the named service.
Common causes: external Ollama / HA URL changed, ChromaDB volume
remount needed, certificate expired.

**Reports directory growing without bound.**
The runner prunes to `reports_keep` (default 30) on each run.
`baselines/` and `_examples/` are never pruned.

---

## Adding a new test

1. Pick a tier file: `test_tier1_health.py` / `test_tier2_components.py`
   / `test_tier3_e2e.py` / `test_tier4_regression.py`.
2. Name the function `test_tierN_<short_name>` so the smoke ID
   resolves to `tierN::<short_name>` automatically.
3. Apply markers as needed:
   - `@pytest.mark.requires_auth` if the test needs the auth session
   - `@pytest.mark.requires_audio_fixtures` if it needs WAV files
   - `@pytest.mark.requires_log_endpoint` if it needs `/api/logs/tail`
   - `@pytest.mark.mutates` if it writes GLaDOS state (opt-in only)
   - `@pytest.mark.slow` if it runs >10 s individually
4. Take `smoke_record` as a fixture and populate it:
   ```python
   def test_tier2_my_thing(http_session, smoke_config, smoke_record):
       url = smoke_config.url("api", "/api/my/route")
       smoke_record.checked = f"GET {url}"
       smoke_record.expected = "200 + the right shape"

       r = http_session.get(url, timeout=smoke_config.timeouts["default"])
       smoke_record.actual = f"{r.status_code} {r.text[:200]}"

       assert r.status_code == 200
       smoke_record.summary = "My route works"
   ```
5. The first time the test runs, its ID becomes part of the public
   contract — don't rename it later or the diff feature will treat
   it as `removed` + `new` instead of matching across runs.

---

## What this suite does NOT do

- **Mock anything.** Every test hits a real container. If GLaDOS isn't
  running, the suite fails — that's correct.
- **Retry on flake.** Flaky tests get marked, not silently retried.
- **Fix anything.** The suite reports state; it doesn't repair.
- **Touch HA automations / Discord / doorbell / hub75.** Tier 1+2 is
  fully read-only against GLaDOS state.
- **Modify GLaDOS source code.** This whole tree is additive.

See `SURFACE_MAP.md` for the full enumeration of GLaDOS surfaces and
which are intentionally NOT probed.

See `TEST_PLAN.md` for the per-test rationale, including the open
items the operator weighed in on.
