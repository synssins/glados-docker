# SIP Slice 1 — Inbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers-extended-cc:subagent-driven-development` (recommended)
> or `superpowers-extended-cc:executing-plans` to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up GLaDOS as a SIP user agent that registers with the
operator's PBX at 192.168.1.1, accepts inbound calls on a dedicated
extension, gates entry on a 4-digit PIN (verbal STT or DTMF), exposes a
DTMF-driven IVR menu post-PIN with four built-in handlers + a
drop-to-freeform key, runs the existing engine path on caller utterances
with `phone_call_mode=True` persona injection, records each call as
MP3 + JSON metadata + transcript with FIFO 5 retention, and ends cleanly
on caller BYE.

**Architecture:** baresip (BSD-3, v4.7.0+) subprocess inside the GLaDOS
container handles SIP signalling and RTP. Python controls baresip via
`ctrl_tcp` JSON over loopback and bridges PCM audio via named pipes
(FIFOs) using baresip's `aufile` module. STT and TTS reuse the existing
in-container pipeline. Speculative TTS pre-rendering during wait states
provides perceived latency hiding.

**Tech Stack:** Python 3.14, baresip 1.0.0+ (Debian Bookworm apt),
scipy (resample), pydub or lameenc (MP3 encoding), existing GLaDOS STT/TTS
pipeline. No new SIP library in requirements.txt — baresip is the SIP
stack and lives in the image, not Python deps.

**Companion docs:**
- `docs/superpowers/specs/2026-05-08-sip-client-design.md` — full architecture spec (rev: baresip subprocess)
- `.interface-design/system.md` — design system v3 vocabulary (Slice 2 dependency, not Slice 1)

**Out of scope (this slice):**
- Outbound calls — Slice 2.
- WebUI Configuration → SIP page — Slice 2 (depends on Approach 2 sweep).
- Autonomous alerts — Slice 3.
- TLS/SRTP, voicemail, transfer, hold, conference — out of scope entirely.

---

## Task 1: Bootstrap — Dockerfile, requirements, docker-compose, env

**Goal:** Get baresip into the image, get the SIP ports forwarded, get
the env-var gate `GLADOS_SIP_ENABLED` declared. No code yet — pure
infrastructure.

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `requirements.txt`
- Modify: `.env.example` (or whatever the env-template file is)

**Acceptance Criteria:**
- [ ] `docker exec glados which baresip` returns a path inside the new image
- [ ] `docker exec glados baresip -h` returns help text (binary works)
- [ ] `docker port glados` shows `5060/udp` and `16384-16484/udp` published
- [ ] `docker inspect glados --format '{{ .Config.Env }}'` includes `GLADOS_SIP_ENABLED=false`
- [ ] No regression: existing pytest suite passes (1887/5)
- [ ] Container still healthy after rebuild

**Verify:** Build new image via `_local_deploy.py` (GHCR LFS still
broken; build on host). Run the four shell-out checks above.

**Steps:**

- [ ] **Step 1: Update Dockerfile.** Add baresip core install after the
  existing apt block. Avoid `baresip-x11`, `baresip-gtk`,
  `baresip-ffmpeg`, `baresip-gstreamer` — those pull in GPL/LGPL deps
  we don't want.

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends baresip && \
    rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Update requirements.txt.** Verify `scipy` presence
  (likely already there). Add `pydub` (depends on system `ffmpeg`
  which is also already in the image) for MP3 encoding. Note: NO
  Python SIP library — baresip is the stack.

```
# Add to requirements.txt:
pydub>=0.25.1   # MP3 encoding for SIP recordings
```

- [ ] **Step 3: Update docker-compose.yml.** Add SIP ports + env var.

```yaml
services:
  glados:
    # ... existing config ...
    ports:
      # ... existing ports ...
      - "5060:5060/udp"
      - "16384-16484:16384-16484/udp"
    environment:
      # ... existing env ...
      GLADOS_SIP_ENABLED: "${GLADOS_SIP_ENABLED:-false}"
```

- [ ] **Step 4: Update `.env.example`** (or equivalent). Document the
  new toggle.

```bash
# SIP client (Slice 1, inbound only). Default off — flip true to register.
GLADOS_SIP_ENABLED=false
```

- [ ] **Step 5: Run pytest.** Confirm no regression (1887/5).

- [ ] **Step 6: Local deploy via `scripts/_local_deploy.py`** (GHCR
  LFS still busted per tech-debt entry). Verify the container starts
  cleanly with `GLADOS_SIP_ENABLED=false` and the SIP ports show up
  in `docker port glados`.

- [ ] **Step 7: Commit.**

```
chore(sip): bootstrap — baresip in image, SIP ports, GLADOS_SIP_ENABLED gate
```

---

## Task 2: configs/sip.yaml schema + loader

**Goal:** Operator-facing YAML config. Schema validates on load. Module
short-circuits when `enabled: false`.

**Files:**
- Create: `configs/sip.example.yaml` (operator-copy template)
- Create: `glados/sip/__init__.py`
- Create: `glados/sip/config.py` (SipConfig dataclass + loader)
- Create: `tests/sip/__init__.py`
- Create: `tests/sip/test_config.py`

**Acceptance Criteria:**
- [ ] Valid YAML loads into a typed `SipConfig` dataclass
- [ ] Missing required fields raise a clear error
- [ ] `SipConfig.enabled=False` makes downstream callers short-circuit
- [ ] Plaintext PIN is loaded but never logged at INFO level (only
      DEBUG) — verified via test
- [ ] Module-level guard: if `not enabled`, `client.start()` is a no-op
- [ ] Test: round-trip load + validate + verify all fields populate

**Verify:** `pytest tests/sip/test_config.py -v` passes.

**Steps:**

- [ ] **Step 1: Author `configs/sip.example.yaml`** matching the spec's
  schema verbatim. Include comments. Operator copies this to
  `configs/sip.yaml` and edits.

- [ ] **Step 2: Author `glados/sip/config.py`** with a Pydantic model
  (existing config_store uses Pydantic; match style):

```python
from pydantic import BaseModel, Field

class SipServer(BaseModel):
    host: str = "192.168.1.1"
    port: int = 5060
    username: str
    password: str = Field("", repr=False)  # Excluded from repr
    transport: str = "UDP"
    realm: str = ""
    register_expires: int = 600

class SipIvrItem(BaseModel):
    key: str
    label: str
    handler: str

class SipIvrMenu(BaseModel):
    enabled: bool = True
    drop_to_freeform_dtmf: str = "0"
    items: list[SipIvrItem] = []

class SipInbound(BaseModel):
    pin: str = Field("", repr=False)
    pin_failures_max: int = 3
    greeting_template: str = "default"
    recording_enabled: bool = True
    allow_caller_ids: list[str] = []
    ivr_menu: SipIvrMenu = SipIvrMenu()

# ... outbound, autonomous, recordings, audio, latency models ...

class SipConfig(BaseModel):
    enabled: bool = False
    server: SipServer
    inbound: SipInbound
    # ... etc
```

- [ ] **Step 3: Loader function** in `config.py`:

```python
def load_sip_config(path: pathlib.Path) -> SipConfig | None:
    """Returns None if file absent OR enabled=false. Raises ValueError on schema violations."""
    if not path.exists():
        return None
    with path.open("r") as f:
        data = yaml.safe_load(f)
    cfg = SipConfig(**data)
    return cfg if cfg.enabled else None
```

- [ ] **Step 4: Tests.** Round-trip load, validate that PIN/password
  are excluded from `repr(cfg)`, validate disabled-shortcut returns
  None.

- [ ] **Step 5: Commit.**

```
feat(sip): config schema + loader (configs/sip.yaml)
```

---

## Task 3: baresip subprocess supervisor

**Goal:** Spawn baresip with a generated config + accounts file. Manage
its lifecycle (start, monitor, restart on crash, stop on shutdown).

**Files:**
- Create: `glados/sip/baresip_supervisor.py`
- Create: `tests/sip/test_baresip_supervisor.py`
- Create: `glados/sip/_baresip_config.py` (config-file generator helpers)

**Acceptance Criteria:**
- [ ] On `start()`, generates `/tmp/baresip/config`, `/tmp/baresip/accounts`
      from `SipConfig`
- [ ] Spawns `baresip -f /tmp/baresip` as a subprocess
- [ ] Captures stdout/stderr to a log
- [ ] Detects subprocess exit and emits a `BaresipExited` event the
      caller can subscribe to
- [ ] On `stop()`, sends SIGTERM, waits up to 5s, then SIGKILL
- [ ] No zombies: subprocess wait() called on exit
- [ ] Test: spawn a fake baresip (a Python script that prints "Ready"
      then sleeps), supervisor detects ready, kills cleanly

**Verify:** `pytest tests/sip/test_baresip_supervisor.py -v` passes.

**Steps:**

- [ ] **Step 1: Generate baresip config files.** baresip reads:
  - `<config_dir>/config` — main config (modules, ports, audio)
  - `<config_dir>/accounts` — one line: `<sip:user@host>;auth_pass=...`

  In `_baresip_config.py`:

```python
def write_baresip_files(cfg: SipConfig, outdir: pathlib.Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "config").write_text(_render_config(cfg))
    (outdir / "accounts").write_text(_render_accounts(cfg))
```

  Modules to enable in the generated `config`:
  - `module ctrl_tcp.so` (control interface, listen on 127.0.0.1:4444)
  - `module aufile.so` (FIFO audio I/O — see Task 5)
  - `module account.so` (account loader)
  - `module dtmf.so` and `module telev.so` (RFC 2833 telephone-event)
  - Codec modules: `module g711.so`, `module g722.so`
  - Disable any GUI/X11 modules that might be default

- [ ] **Step 2: Subprocess spawn.** Use `asyncio.create_subprocess_exec`
  for non-blocking lifecycle. Capture stdout+stderr to a logger sink
  routed to the existing `logger.bind(group="sip")` logger.

- [ ] **Step 3: Health watch.** A background task `await
  proc.wait()` resolves when baresip exits. On exit, emit
  `BaresipExited` event. Caller (call_session) can decide:
  - If no active call → restart after 2s backoff (up to 5 attempts)
  - If active call → fire BYE event upstream first

- [ ] **Step 4: Graceful shutdown.** `stop()`: send SIGTERM, await up
  to 5 s, escalate to SIGKILL if still alive.

- [ ] **Step 5: Tests.** Mock baresip with a tiny Python script.
  Verify start, ready-detection (parse stderr for "ua: account
  registered" or similar), stop, restart.

- [ ] **Step 6: Commit.**

```
feat(sip): baresip subprocess supervisor + config generator
```

---

## Task 4: ctrl_tcp client — commands + events

**Goal:** TCP client that connects to baresip's `ctrl_tcp` module on
loopback:4444. Sends JSON commands (`/dial`, `/hangup`, `/dnd`, etc.)
and parses event JSON (incoming-call, DTMF, BYE, registration state).

**Files:**
- Create: `glados/sip/ctrl_client.py`
- Create: `tests/sip/test_ctrl_client.py`

**Acceptance Criteria:**
- [ ] Connects to `127.0.0.1:4444` after baresip starts
- [ ] Auto-reconnects with exponential backoff (1s, 2s, 4s, 8s, max 10s) on disconnect
- [ ] `send(command, **params)` sends JSON, returns response future
- [ ] `subscribe(event_type, callback)` fires callback on matching events
- [ ] Handles partial JSON across TCP packet boundaries
- [ ] Test: against an in-process mock baresip ctrl server, verify
      commands sent and events received

**Verify:** `pytest tests/sip/test_ctrl_client.py -v` passes.

**Steps:**

- [ ] **Step 1: Read baresip ctrl_tcp wire format.** baresip uses
  netstring-framed JSON: `<length>:<json>,`. Implement a parser.

- [ ] **Step 2: Command dispatch.** `send_command()` returns a
  `Future` resolved when the response arrives. Match request/response
  by token field.

- [ ] **Step 3: Event subscription.** Maintain a callback registry:
  `{event_type: [callback, ...]}`. On event arrival, dispatch.

- [ ] **Step 4: Reconnection.** If TCP disconnects, mark all
  in-flight futures as failed, then reconnect with backoff.

- [ ] **Step 5: Tests.** Mock baresip ctrl server (a tiny asyncio
  TCP server that scripts a sequence). Test: command roundtrip,
  event dispatch, reconnect after kill, partial-frame handling.

- [ ] **Step 6: Commit.**

```
feat(sip): ctrl_tcp JSON client with command + event dispatch
```

---

## Task 5: audio_bridge — FIFO PCM I/O

**Goal:** Bridge baresip's PCM audio (via FIFOs) to/from the existing
STT and TTS pipeline. Resample 8 kHz μ-law ↔ 16 kHz PCM. Implement
self-listen mute during TTS playback.

**Files:**
- Create: `glados/sip/audio_bridge.py`
- Create: `tests/sip/test_audio_bridge.py`

**Acceptance Criteria:**
- [ ] On call start, creates two FIFOs: `/tmp/sip-rx.fifo` (caller →
      us), `/tmp/sip-tx.fifo` (us → caller); baresip's aufile module
      points at these
- [ ] PCM samples from rx FIFO are resampled 8k → 16k and pushed to STT
- [ ] PCM samples from TTS are resampled 16k → 8k and written to tx FIFO
- [ ] When TTS is playing, STT input is muted (no self-listen feedback)
- [ ] Resume STT 100 ms after TTS final packet
- [ ] On call end, FIFOs are closed and unlinked
- [ ] No deadlock: non-blocking I/O with timeouts; if STT consumer
      stalls, audio bridge logs and recovers (drop frames, continue)
- [ ] Test: write synthetic PCM to rx, verify STT receives 16k frames

**Verify:** `pytest tests/sip/test_audio_bridge.py -v` passes.

**Steps:**

- [ ] **Step 1: FIFO lifecycle.** `os.mkfifo()` on call start,
  `os.unlink()` on cleanup. Ownership: `audio_bridge` owns both.

- [ ] **Step 2: Resampler.** scipy.signal.resample_poly with up=2,
  down=1 for 8→16k upsampling, and up=1, down=2 for 16→8k
  downsampling. Per-frame processing (160 samples at 8k = 320 at
  16k, both representing 20 ms).

- [ ] **Step 3: μ-law codec.** baresip's aufile module reads/writes
  raw PCM (16-bit signed at the configured rate). If baresip is
  configured with PCMU codec, the file format is 8 kHz signed 16-bit
  PCM (post-decode). No μ-law decoding on our side — baresip handles
  it.

- [ ] **Step 4: Reader loop.** asyncio task reads chunks from the rx
  FIFO, resamples, queues for STT. Drop frames if STT consumer is
  slow (log a warning).

- [ ] **Step 5: Writer loop.** asyncio task drains a TTS-output queue,
  resamples, writes to the tx FIFO.

- [ ] **Step 6: Self-listen mute.** A `tts_active` flag set/cleared
  by call_session around TTS playback. When set, the reader loop
  drops incoming frames (don't pass to STT).

- [ ] **Step 7: Tests.** Synthetic 8k PCM injection → assert STT
  receives 16k frames; TTS output → assert tx FIFO contains
  resampled 8k PCM. Self-listen mute test.

- [ ] **Step 8: Commit.**

```
feat(sip): audio bridge with FIFO PCM I/O + resample + self-listen mute
```

---

## Task 6: pin_gate — STT digits + DTMF events

**Goal:** Concurrent listeners for spoken-digit STT and DTMF
ctrl_tcp events. First valid 4-digit PIN match wins. Three failures →
return `failed=True` so call_session can hang up.

**Files:**
- Create: `glados/sip/pin_gate.py`
- Create: `tests/sip/test_pin_gate.py`

**Acceptance Criteria:**
- [ ] Accepts both `"8316"` (read-aloud) and `"eight three one six"`
      (spoken numerals) from STT
- [ ] Tolerant of leading/trailing words ("uh, eight three one six please")
- [ ] DTMF events from ctrl_client buffered into a 4-digit string,
      evaluated on the 4th digit
- [ ] Failure counter shared between paths
- [ ] Three failures → `await pin_gate.run() == GateResult.FAIL`
- [ ] Match → `GateResult.PASS`
- [ ] Test: spoken digits, DTMF digits, mixed, 3-fail path

**Verify:** `pytest tests/sip/test_pin_gate.py -v` passes.

**Steps:**

- [ ] **Step 1: Spoken-digit parser.** Map English digit words ("zero"
  through "nine") + numeric strings (`"8316"`) to extracted digits.
  Be tolerant: ignore filler ("uh", "um"), accept alternative spellings
  ("oh" for zero).

- [ ] **Step 2: DTMF buffer.** Subscribe to ctrl_client's "dtmf"
  event. Buffer 4 digits. Reset on timeout (15 s with no digit).

- [ ] **Step 3: Convergence.** First path to produce a 4-digit string
  evaluates against `cfg.inbound.pin`. Match → return PASS.
  Mismatch → increment failure counter, audibly notify caller via
  call_session callback (which uses speculative TTS for the
  rejection lines).

- [ ] **Step 4: Tests.**

- [ ] **Step 5: Commit.**

```
feat(sip): PIN gate (STT digits + DTMF, 3-failure cutoff)
```

---

## Task 7: speculative_tts — background TTS futures

**Goal:** Background workers pre-render likely-next TTS responses so
they're ready before they're needed. Cancel and discard non-matching
branches when state resolves.

**Files:**
- Create: `glados/sip/speculative_tts.py`
- Create: `tests/sip/test_speculative_tts.py`

**Acceptance Criteria:**
- [ ] `register_branch(branch_name, labels)` starts background TTS
      tasks for each label
- [ ] `consume(label) → bytes` returns ready audio if cached, awaits
      if in-flight, falls back to fresh sync TTS if not started
- [ ] `cancel_other(branch_name, kept_label)` cancels all jobs in the
      branch except the matched label
- [ ] Hard cap on concurrent in-flight jobs per call (config:
      `latency.speculative.max_concurrent`)
- [ ] Test: simulated slow TTS (artificial 2 s delay), verify
      consume after 200 ms returns within 50 ms (cache hit)

**Verify:** `pytest tests/sip/test_speculative_tts.py -v` passes.

**Steps:**

- [ ] **Step 1: Renderer registry.** A label maps to a
  `(text, voice_params)` pair OR a callable that produces text from
  current state (for menu handlers).

- [ ] **Step 2: Branch dispatch.** On `register_branch`, launch
  asyncio tasks (one per label) up to the concurrency cap. Track
  futures in a dict.

- [ ] **Step 3: Consume.** Look up label's future. If `done()` →
  return result. If still running → await. If not registered → run
  synchronously.

- [ ] **Step 4: Cancel logic.** `cancel_other` iterates the branch's
  futures, cancels all but the matched label. Ensures any partial
  work is not stranded.

- [ ] **Step 5: Tests.** Use a `MockTtsService` with configurable
  delay; verify cache hit, in-flight wait, and cancellation
  semantics.

- [ ] **Step 6: Commit.**

```
feat(sip): speculative TTS pre-rendering with cancellable branches
```

---

## Task 8: ivr — menu state machine + 4 handlers

**Goal:** DTMF-driven menu post-PIN. Plays pre-rendered prompt; on key
press, dispatches a deterministic handler (NOT free-form LLM); plays
its response; loops back to menu. `0` drops to free-form conversation.

**Files:**
- Create: `glados/sip/ivr.py`
- Create: `glados/sip/handlers/` (subdirectory for handler modules)
- Create: `glados/sip/handlers/__init__.py`
- Create: `glados/sip/handlers/house_status.py`
- Create: `glados/sip/handlers/security_state.py`
- Create: `glados/sip/handlers/door_locks.py`
- Create: `glados/sip/handlers/doorbell_recent.py`
- Create: `tests/sip/test_ivr.py`

**Acceptance Criteria:**
- [ ] On entry, plays the pre-rendered menu prompt (cached in memory)
- [ ] DTMF key matches an item → dispatches the handler, plays its
      response, returns to menu
- [ ] `0` → exits IVR, returns IvrExit.FREEFORM signal to call_session
- [ ] Silence/timeout 10 s → re-prompts. After 3 re-prompts, returns
      IvrExit.HANGUP
- [ ] Each handler is purely deterministic (no LLM call)
- [ ] Handler runs while menu prompt is playing (speculative); audio
      is ready when the digit is pressed
- [ ] Test: each handler, drop-to-freeform, silence-3-prompts hangup

**Verify:** `pytest tests/sip/test_ivr.py -v` passes.

**Steps:**

- [ ] **Step 1: Menu prompt synthesis.** At container startup,
  generate the menu prompt audio from the config's IVR items. Cache
  in memory.

```python
def build_menu_prompt(items: list[SipIvrItem]) -> str:
    parts = ["Press " + _digit_to_word(item.key) + " for " + item.label
             for item in items]
    return ", ".join(parts) + ", or zero to talk to me directly."
```

- [ ] **Step 2: Handlers** in `handlers/`. Each implements:

```python
async def render(ctx: HandlerContext) -> str:
    """Returns text to be spoken. Pure formatter, no LLM."""
```

  - `house_status`: queries HA state via the existing `HAClient`,
    summarises lights/climate/last-event in 2-3 sentences.
  - `security_state`: reads alarm panel + doors + motion sensors,
    summarises armed-state.
  - `door_locks`: per-door lock state, ordered by name.
  - `doorbell_recent`: last 3 doorbell events from the audit log,
    with screener verdicts inlined.

- [ ] **Step 3: State machine.** asyncio loop:
  - Start: register speculative branch `menu_idle` with handlers
    pre-rendering audio.
  - Play prompt.
  - Wait for DTMF event OR timeout.
  - On match: consume the speculative future for the matched
    handler, cancel siblings, play audio, loop.
  - On `0`: exit FREEFORM.
  - On timeout: increment re-prompt counter, replay prompt.
  - At 3 re-prompts: exit HANGUP.

- [ ] **Step 4: Tests.** Mock ctrl_client to inject DTMF events,
  mock TTS, verify handler dispatch + audio playback.

- [ ] **Step 5: Commit.**

```
feat(sip): IVR menu + 4 deterministic handlers
```

---

## Task 9: recording — MP3 + JSON + transcript, FIFO 5

**Goal:** Capture mixed audio + per-call metadata + speaker-labelled
transcript. Prune to 5 most recent on each save.

**Files:**
- Create: `glados/sip/recording.py`
- Create: `tests/sip/test_recording.py`

**Acceptance Criteria:**
- [ ] On call start, opens an MP3 encoder + transcript writer + JSON
      metadata buffer
- [ ] Mixed audio (caller + GLaDOS) appended throughout the call
- [ ] Transcript records each utterance with timestamp + speaker label
- [ ] On call end, finalizes MP3, writes JSON, writes .txt
- [ ] FIFO prune: globs `media/sip-recordings/*.mp3`, sorts by mtime,
      deletes triplets (mp3+json+txt) past index 5
- [ ] Storage path is `cfg.recordings.store_path` (default
      `media/sip-recordings`)
- [ ] Test: synthetic call → save → verify all 3 files exist with
      correct content; FIFO prune test with 7 files

**Verify:** `pytest tests/sip/test_recording.py -v` passes.

**Steps:**

- [ ] **Step 1: MP3 encoder.** Use `pydub.AudioSegment` to accumulate
  PCM chunks, export as MP3 on close. Sample rate matches call audio
  (8 kHz from baresip).

- [ ] **Step 2: Transcript writer.** On each utterance, append a line:
  `[HH:MM:SS] Speaker: text`.

- [ ] **Step 3: Metadata buffer.** Built up during the call, written
  on close. Schema in spec.

- [ ] **Step 4: FIFO prune.** On `close()`, run after writing.

- [ ] **Step 5: Tests.**

- [ ] **Step 6: Commit.**

```
feat(sip): per-call recording (MP3 + JSON + transcript, FIFO 5)
```

---

## Task 10: persona injection + canned responses

**Goal:** System-prompt fragment for `phone_call_mode=True` plus
pre-rendered MP3s for the screening responses (greeting, PIN failure,
hangup goodbye). Wired into the spec's "potato form" persona language.

**Files:**
- Create: `glados/sip/persona.py`
- Create: `glados/sip/canned_audio/` (directory for pre-rendered MP3s)
- Create: `glados/sip/canned_audio/.gitkeep`
- Create: `tests/sip/test_persona.py`

**Acceptance Criteria:**
- [ ] `PHONE_CALL_PROMPT_FRAGMENT` constant matches the spec verbatim
- [ ] `bake_canned_responses()` runs at SIP-module init, generates
      MP3s from the persona-flavored greeting, PIN failure variants
      (1, 2, final), hangup goodbye, "fine, what did you actually
      want to know" drop-to-freeform line
- [ ] Generated MP3s cached in memory (also written to
      `canned_audio/` for inspection by operator)
- [ ] Test: bake runs, every expected file exists, audio length is
      non-zero

**Verify:** `pytest tests/sip/test_persona.py -v` passes.

**Steps:**

- [ ] **Step 1: Author the fragment** matching the spec.

- [ ] **Step 2: Canned response texts** as a constants module.

- [ ] **Step 3: `bake_canned_responses(tts_service)`** synthesises
  each text once, stores bytes in a dict, also writes to disk for
  inspection.

- [ ] **Step 4: Tests.**

- [ ] **Step 5: Commit.**

```
feat(sip): potato-form persona injection + canned screening responses
```

---

## Task 11: call_session — state machine glue

**Goal:** The orchestrator. Subscribes to ctrl_client events; manages
the IDLE → RINGING → ESTABLISHED → GREETING → PIN_ENTRY →
MENU/CONVERSATION → BYE state machine; coordinates audio_bridge,
pin_gate, ivr, recording.

**Files:**
- Create: `glados/sip/call_session.py`
- Create: `tests/sip/test_call_session.py`

**Acceptance Criteria:**
- [ ] Receives `incoming_call` from ctrl_client, advances to RINGING
- [ ] Auto-answers, advances to ESTABLISHED, plays greeting
- [ ] PIN_ENTRY runs pin_gate; on PASS dispatches to MENU or
      CONVERSATION based on `ivr_menu.enabled`
- [ ] CONVERSATION runs the existing engine.process loop with
      `phone_call_mode=True`
- [ ] On caller BYE, finalises recording + transcript, advances to
      IDLE
- [ ] Single-call rule: incoming-call event while in non-IDLE state
      gets a busy-decline (486)
- [ ] Test: full happy-path through state machine with mocked
      collaborators

**Verify:** `pytest tests/sip/test_call_session.py -v` passes.

**Steps:**

- [ ] **Step 1: State enum + transitions table.**

- [ ] **Step 2: Event handlers** for ctrl_client events: incoming_call,
  call_established, dtmf, bye.

- [ ] **Step 3: Greeting playback** via canned MP3 → tx FIFO.

- [ ] **Step 4: PIN_ENTRY** spawns pin_gate, registers the
  `pin_entry` speculative branch.

- [ ] **Step 5: MENU dispatch** to ivr.run().

- [ ] **Step 6: CONVERSATION loop:** STT utterance → engine.process →
  TTS → tx FIFO. Loop until BYE.

- [ ] **Step 7: BYE handling.** Save recording, prune FIFO, reset
  to IDLE.

- [ ] **Step 8: Tests** with mocked collaborators.

- [ ] **Step 9: Commit.**

```
feat(sip): call_session state machine + lifecycle orchestration
```

---

## Task 12: engine integration + audit logging

**Goal:** Wire `phone_call_mode=True` flag through `engine.process`.
Add audit-log entries for SIP calls.

**Files:**
- Modify: `glados/core/engine.py` (or wherever engine.process lives)
- Modify: relevant context/persona files
- Modify: `glados/audit/` integration

**Acceptance Criteria:**
- [ ] `engine.process(text, phone_call_mode=True)` injects the SIP
      persona fragment into the system prompt
- [ ] Existing chat-path callers unaffected (default `phone_call_mode=False`)
- [ ] Audit log records each SIP call with: origin=sip, direction,
      remote_caller_id, pin_outcome, duration, recording_path
- [ ] Test: engine.process with phone_call_mode invokes correct prompt; audit log entry created

**Verify:** Existing 1887/5 tests still pass plus new tests.

**Steps:**

- [ ] **Step 1: Find the engine.process signature** in
  `glados/core/engine.py`. Add a `phone_call_mode: bool = False`
  kwarg.

- [ ] **Step 2: Persona fragment injection.** When True, prepend the
  `PHONE_CALL_PROMPT_FRAGMENT` from `glados/sip/persona.py` to the
  active system prompt.

- [ ] **Step 3: Audit logging.** Use the existing audit logger.
  Format matches `webui_chat` rows but with `origin=sip` and
  SIP-specific fields.

- [ ] **Step 4: Tests** for both the persona injection and audit
  logging.

- [ ] **Step 5: Commit.**

```
feat(engine): phone_call_mode + SIP audit logging integration
```

---

## Task 13: integration test — mock SIP exchange

**Goal:** End-to-end test exercises the full inbound flow with a mock
SIP UAS standing in for the PBX. Validates wiring of all prior tasks.

**Files:**
- Create: `tests/sip/test_integration_mock_pbx.py`
- Create: `tests/sip/_mock_uas.py` (test fixture: minimal SIP server)

**Acceptance Criteria:**
- [ ] Test boots a real baresip subprocess against a Python-asyncio
      mock UAS that sends INVITE
- [ ] Asserts: greeting plays, PIN entry succeeds (DTMF path), menu
      prompt plays, handler 1 dispatches, drop-to-freeform works,
      conversation turn completes (mocked engine response), BYE is
      received and recording is saved
- [ ] Test runs in <60 seconds
- [ ] Marked `@pytest.mark.slow` (skipped by default) since it
      requires baresip in PATH

**Verify:** `pytest tests/sip/test_integration_mock_pbx.py -v --run-slow` passes.

**Steps:**

- [ ] **Step 1: Author `_mock_uas.py`** — minimal SIP UAS that handles
  REGISTER, sends INVITE, can send RFC 2833 DTMF events, accepts BYE.

- [ ] **Step 2: Author `test_integration_mock_pbx.py`** — fixture
  spawns mock UAS + baresip + GLaDOS SIP module, scripts the full
  flow, asserts every milestone.

- [ ] **Step 3: Pytest marker** for slow integration tests.

- [ ] **Step 4: Commit.**

```
test(sip): end-to-end integration test with mock PBX
```

---

## Task 14: CHANGES.md + final review + durable deploy

**Goal:** Document Slice 1 in CHANGES.md, run the full test suite,
build + deploy a durable image, prepare operator validation steps.

**Files:**
- Modify: `docs/CHANGES.md`
- Modify: `docs/roadmap.md` (move SIP Slice 1 to "shipped" section)

**Acceptance Criteria:**
- [ ] `docs/CHANGES.md` Change 44 entry written: architecture summary,
      what shipped, deploy artifacts (image SHA), operator validation
      steps, side effects
- [ ] `pytest -q --ignore=tests/smoke` passes
- [ ] `_local_deploy.py` builds and ships the durable image
- [ ] Live-probe: container healthy with `GLADOS_SIP_ENABLED=false`
      (default — operator opts in via `.env` after configuring
      `configs/sip.yaml`)
- [ ] Spec doc cross-referenced from CHANGES.md

**Verify:** Operator-runnable steps captured for in-call testing
once they configure the PBX side.

**Steps:**

- [ ] **Step 1: Write CHANGES.md Change 44.** Summary of slice,
      architecture pointer, list of new modules, test counts,
      deploy artifact (image SHA), known limitations.

- [ ] **Step 2: Update docs/roadmap.md.** Move SIP Slice 1 from
      "planned" to "shipped" if it was tracked there; otherwise add
      to recent-shipments list.

- [ ] **Step 3: Run pytest** end-to-end.

- [ ] **Step 4: Local-deploy** durable image.

- [ ] **Step 5: Live-probe** /health both ports, confirm container
      healthy with SIP disabled by default (operator-flips when ready).

- [ ] **Step 6: Final commit.**

```
docs(sip): Slice 1 shipped — CHANGES Change 44 + roadmap update
```

- [ ] **Step 7: Push branch + PR-ready state.** Operator merges to
      main when ready (durable deploy already happened on the docker
      host via `_local_deploy.py`; merging to main is for the
      source-of-truth update).

---

## Acceptance criteria — overall slice

- [ ] `GLADOS_SIP_ENABLED=true` + valid `configs/sip.yaml` + container
      restart → GLaDOS registers with the PBX, accepts an inbound
      call, runs the full PIN → menu → handler → drop → conversation
      → BYE flow
- [ ] All 12 modules in `glados/sip/` exist with their tests
- [ ] `tests/sip/` has at least 9 unit test files + 1 integration test
- [ ] `pytest -q --ignore=tests/smoke` passes (1887 + new SIP tests)
- [ ] No regression on existing functionality (chat, TTS, HA, doorbell)
- [ ] Documentation updated: spec rev, plan checked off, CHANGES Change 44
- [ ] Live image deployed durably to docker host

## Risk + mitigation

**Risk: baresip ctrl_tcp event vocabulary differs from documentation.**
Mitigation: Task 4 includes an exploration step — start baresip with
verbose logging, observe actual event JSON, record findings in a code
comment for future maintainers.

**Risk: FIFO audio bridge has unforeseen blocking behavior.**
Mitigation: non-blocking I/O with timeouts (Task 5 step 6); integration
test simulates slow STT consumer to verify no deadlock.

**Risk: PIN spoken-digit STT WER is high enough to make voice-PIN
unreliable.**
Mitigation: spec explicitly recommends DTMF as primary; voice is the
fallback. Greeting can be tuned to encourage DTMF if testing shows STT
errors.

**Risk: time budget overrun.**
Mitigation: tasks 1–6 and 9 are independently shippable infrastructure;
tasks 7–11 are the integration story. If the schedule slips, Slice 1
can ship with IVR disabled (free-form conversation only) by setting
`ivr_menu.enabled: false` in default config — the implementation still
gets done but isn't on the critical path for live testing.

---

## Estimated effort

| Task | Estimate | Risk |
|---|---|---|
| 1. Bootstrap | 45 min | Low |
| 2. configs + loader | 1 h | Low |
| 3. baresip supervisor | 2 h | Med (subprocess subtleties) |
| 4. ctrl_tcp client | 2 h | Med (wire format unknowns) |
| 5. audio_bridge | 2.5 h | High (real-time audio) |
| 6. pin_gate | 1.5 h | Low |
| 7. speculative_tts | 1.5 h | Med (asyncio edge cases) |
| 8. ivr + 4 handlers | 2 h | Low |
| 9. recording | 1 h | Low |
| 10. persona + canned | 1 h | Low |
| 11. call_session glue | 2 h | Med (state machine integration) |
| 12. engine integration | 1 h | Low |
| 13. integration test | 1.5 h | Med (mock PBX is non-trivial) |
| 14. CHANGES + deploy | 30 min | Low |
| **Total** | **~20 h** | |

Within the spec's 15–20 h estimate.
