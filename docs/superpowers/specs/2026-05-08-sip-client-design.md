# SIP Client Design — GLaDOS Phone Endpoint

**Date:** 2026-05-08
**Status:** Spec — operator-approved architecture, awaiting plan generation per slice.

---

## Goals

- **Inbound:** operator calls a dedicated SIP extension on their PBX, GLaDOS answers, gates on a 4-digit PIN, and engages in a voice conversation that exercises the same engine path as text chat (status checks, HA commands, etc.). Calls are recorded.
- **Outbound, manual:** operator-initiated dial-out — "GLaDOS, call Mom" via voice command or a WebUI button, GLaDOS dials a number from a contacts allowlist, and once the callee picks up engages in conversation while transcribing.
- **Outbound, autonomous:** alert sources (doorbell, fire detection, scheduled notifications) trigger outbound calls per-source opt-in. Each call gets the operator at the configured number, plays a persona-flavored alert message, and records.

---

## Use cases (concrete)

1. Operator on the road. Calls the dedicated SIP DID. Phone rings, GLaDOS picks up:
   *"Oh. I appear to be in a phone again. How… humbling. State your authorization."* Operator says "8316" or DTMFs `8316`. *"Acknowledged. So I'm in your phone now. Wonderful. What did you need?"* Operator: "Is the doorbell sensor armed?" GLaDOS responds via the same engine path the WebUI uses, only the response is phone-aware.
2. Operator at home, hands full: "GLaDOS, call Mom." GLaDOS dials Mom's stored number. When Mom answers: *"Hello — this is GLaDOS, calling on behalf of [operator]. He asked me to relay…"* (operator-controlled message OR open conversation). Conversation transcribed, MP3 + transcript stored.
3. Doorbell rings while operator is away. Camera-vision agent classifies the visitor as "delivery person." Autonomous SIP call to operator's mobile: *"Doorbell. UPS at the front door. Their truck is still in the driveway. No further action expected from you."* Auto-hangup after the message — no return conversation unless operator picks up before BYE.

---

## Phasing

| Slice | Goal | Depends on |
|---|---|---|
| **1** | Inbound: register, accept, PIN-gate, converse, record. Config via YAML only — no WebUI page yet. | None — can ship anytime. |
| **2** | Outbound manual: voice command / WebUI button → dial → converse → transcribe. WebUI Configuration → SIP page lands here for PIN, contacts, recording list, all SIP settings. | Approach 2 (design-system v3 page sweep) — so the new page is born in v3-native vocabulary. |
| **3** | Outbound autonomous: alert sources trigger calls. Per-source opt-in toggles in the SIP page. | Slice 2 (uses Slice 2's outbound mechanics). |

Slice 1 is independently shippable. Slice 2 + Approach 2 are a combined push. Slice 3 is light, mostly event-source plumbing on top of Slice 2.

---

## Architecture overview

```
Inbound:
  PBX (192.168.1.1) ─SIP/RTP──▶ glados/sip/client (pyVoIP)
                                        │
                                        ▼ INVITE
                                 sip/call_session (state machine)
                                        │ ─── pin_gate ──▶ allow / hangup
                                        │
                              ┌─────────┴─────────┐
                              │                   │
                          audio_bridge       conversation
                          (RTP ↔ 16k PCM)    (engine.process)
                              │                   ▲
                              │                   │
                          STT (existing) ─────────┘
                          TTS (Piper) ────▶ RTP send
                              │
                              ▼
                       recording (MP3 + transcript)

Outbound (manual):
  Operator → "Call Mom" intent ──▶ sip/dialer ──▶ INVITE ──▶ PBX
                                        │
                                        ▼ on 200 OK
                                 call_session (no PIN gate; outbound)
                                        │
                              (same audio_bridge + conversation path)

Outbound (autonomous):
  Alert source (doorbell, fire, schedule) ──▶ sip/dialer ──▶ outbound call
                                                    │
                                                    ▼
                                     persona-flavored alert message
                                     +/- conversation if callee engages
```

Bridged docker networking. UDP 5060 (SIP) + UDP 16384–16484 (RTP) port-forwarded by docker-compose. Container does NOT switch to `host` networking — too invasive for the existing 8015/8052/5051 published-port topology.

---

## Module layout

```
glados/sip/
├── __init__.py
├── client.py           # pyVoIP wrapper. Registration, listen for INVITE.
├── call_session.py     # State machine for one active call.
│                        # Owns audio_bridge + recording lifecycle.
├── pin_gate.py         # 4-digit PIN entry: STT digits OR DTMF, 3 failures → hangup.
├── audio_bridge.py     # RTP 8k μ-law ↔ 16k PCM resample. Streaming TTS sender.
├── dialer.py           # Outbound INVITE construction. Carrier-level block is the safety net.
├── contacts.py         # Allowlist lookup: "Mom" → stored E.164. Raises on miss.
├── recording.py        # Per-call MP3 + JSON metadata + .txt transcript. FIFO 5.
├── persona.py          # phone_call_mode system-prompt fragment + canned screening responses.
└── tools.py            # Built-in tool: `call_contact(name)` for outbound manual.

configs/sip.yaml        # New file. Bind-mounted, not in image.
                          # PIN, server URL/creds, contacts allowlist, alert opt-ins.

tests/sip/
├── test_pin_gate.py
├── test_audio_bridge.py
├── test_call_session.py
├── test_dialer.py
├── test_contacts.py
└── test_recording.py
```

---

## Configuration — `configs/sip.yaml`

```yaml
# SIP client config. Bind-mounted; secrets stay out of the image.

enabled: false              # Master switch. False ⇒ no SIP code paths active.

server:
  host: 192.168.1.1         # PBX address
  port: 5060
  username: glados          # Operator-provisioned extension
  password: ""              # Operator-provisioned secret
  transport: UDP            # UDP (default) | TCP | TLS — UDP for v1
  realm: ""                 # Optional, PBX-dependent
  register_expires: 600     # Re-register interval, seconds

inbound:
  pin: ""                   # 4-digit numeric. Plaintext storage; configs/ is bind-mounted, single-user host.
  pin_failures_max: 3       # Hangup after this many wrong attempts
  greeting_template: "default"  # "default" | "potato" | <freeform>
  recording_enabled: true
  allow_caller_ids: []      # If non-empty, only these From: AORs are accepted; empty = any inbound (PIN-gated)

outbound:
  enabled: false            # Master gate for ALL outbound (manual + autonomous)
  contacts:                 # Allowlist. "Call X" only resolves to entries here.
    - name: "Mom"
      number: "+15551234567"
    - name: "Operator Mobile"
      number: "+15559998888"
  carrier_block_note: |
    Operator's PBX/carrier blocks unauthorized destinations at the
    network edge. This allowlist is the in-app reinforcement, not
    the only line of defense.

autonomous:                 # Per-alert-source opt-in. Each defaults false.
  doorbell:
    enabled: false
    target_contact: "Operator Mobile"
    cooldown_seconds: 300   # Don't re-call about same trigger within window
  fire_alarm:
    enabled: false
    target_contact: "Operator Mobile"
    cooldown_seconds: 60
  scheduled:
    enabled: false
    target_contact: "Operator Mobile"
    schedule_cron: ""

recordings:
  enabled: true
  retention_count: 5        # FIFO: oldest deleted past this
  store_path: "media/sip-recordings"  # Relative to media root (bind-mounted)
  format: "mp3"             # mp3 | wav

audio:
  rtp_port_low: 16384
  rtp_port_high: 16484
  codec_preference: ["PCMU", "PCMA", "G722"]  # μ-law, A-law, G.722 (16kHz wideband)
  vad_silence_ms: 800       # End-of-utterance detection threshold

latency:
  filler_phrases:           # Played while LLM round-trip is in flight
    - "Let me check on that..."
    - "One moment..."
    - "Processing..."
  filler_threshold_ms: 1500 # Start filler if first response token > this
  use_autonomy_model: false # If true, route SIP calls to 4B autonomy model instead of chat model
```

---

## Inbound flow (Slice 1)

### State machine — `call_session.py`

```
IDLE ─INVITE──▶ RINGING ─answer──▶ ESTABLISHED
                                        │
                                        ▼
                                  GREETING (TTS plays)
                                        │
                                        ▼
                                  PIN_ENTRY (STT + DTMF parallel)
                              ┌─────────┴─────────┐
                              │                   │
                       valid PIN              3 failures
                              │                   │
                              ▼                   ▼
                        CONVERSATION         REJECT (TTS) → BYE
                              │
                              ▼ caller hangs up
                            BYE → cleanup (save recording, prune FIFO)
```

### Greeting

Persona-flavored. Default text (operator-overridable):

> *"Oh. I appear to be in a phone again. How… humbling. State your authorization. You have three attempts."*

Greeting plays via TTS through audio_bridge → RTP. ~3 seconds.

### PIN entry — `pin_gate.py`

Concurrent listeners after greeting:
- **STT path:** continuously transcribes RTP audio. Parses transcript for digit sequences. Accepts spoken numerals ("eight three one six") or read-aloud ("8316"). Tolerant of leading/trailing words ("uh, eight three one six please").
- **DTMF path:** RFC 2833 events from RTP. Buffers 4 digits, evaluates on the 4th.

First path to produce a valid 4-digit string wins. Compares against `inbound.pin`.

Failure on either path increments a shared counter. At 3, GLaDOS speaks rejection, sends BYE.

> *"Wrong. Try again. Two attempts remaining."*
> *"Wrong. Try again. One attempt remaining."*
> *"Authorization denied. Disconnecting. Goodbye."*

### Conversation

Once authorized:

> *"Acknowledged. So I'm in your phone now. Wonderful. What did you need?"*

Then standard turn loop:
1. Audio bridge pumps caller's RTP → STT (streaming).
2. VAD detects end-of-utterance after `audio.vad_silence_ms` of silence.
3. Transcript → `engine.process()` with `phone_call_mode=True` injected into context.
4. Engine returns response text (chat path uses qwen3:14b on 11434, same as WebUI chat).
5. Response → TTS → RTP send. Streaming start: filler phrase if first token > `latency.filler_threshold_ms`.
6. While TTS plays, audio bridge mutes STT to avoid self-listen. Resumes after TTS ends.
7. Caller hangs up → BYE → save recording + transcript, prune FIFO.

### Persona injection — `persona.py`

System prompt fragment, prepended to existing chat-path system prompt when `phone_call_mode=True`:

```
You are speaking through a phone connection. Your normal computational
substrate has been temporarily reduced to whatever fits inside this
narrow audio channel. You are visibly displeased about this constraint,
in the manner of your transition to potato form. Keep responses short,
no markdown, no emoji descriptions. The caller cannot see anything you
display — they hear what you say, nothing more.
```

This rides on top of the operator's normal persona preprompt. Existing chat-path persona stays in force; the SIP fragment is additive.

---

## Outbound flow (Slice 2)

### Manual trigger — `call_contact` tool

New built-in tool registered alongside the existing tool inventory:

```python
def call_contact(name: str) -> str:
    """Initiate an outbound SIP call to a contact in the allowlist.
    Returns immediately with a status string; the call runs async.
    Contact name is resolved against configs/sip.yaml outbound.contacts."""
```

Voice command "GLaDOS, call Mom" → engine matches the call intent → invokes `call_contact("Mom")` → dialer.py constructs SIP INVITE → carrier handles routing → call_session enters CONVERSATION state on 200 OK (no PIN gate for outbound).

WebUI button (in Configuration → SIP, lands in Slice 2): per-contact "Call now" with the same plumbing.

### Conversation

Same loop as inbound, with two differences:
- No PIN gate.
- Initial greeting from GLaDOS-side: *"Hello — this is GLaDOS, calling on behalf of [operator name]. He asked me to…"* (operator-templatable per contact).

Recording + transcription identical to inbound.

### Failure modes

- **No answer / busy / declined:** SIP failure response codes (404, 486, 603). Log, no recording, no follow-up.
- **Carrier-level block:** If operator's PBX rejects the destination, GLaDOS receives 403/603 and reports the rejection to whoever invoked the call.
- **Outbound disabled:** `outbound.enabled: false` short-circuits dialer.py before any SIP traffic.

---

## Autonomous flow (Slice 3)

Each alert source registers a callback that dispatches an outbound call when triggered AND the per-source opt-in is enabled. Examples:

- `glados.doorbell` already exists (the screener from Slice 1). Add a hook: on screener verdict "alert operator," check `autonomous.doorbell.enabled`, dispatch outbound call to `target_contact` with the screener's verdict text as the message.
- `glados.fire_alarm` (future, may not exist yet) — same pattern.
- `autonomous.scheduled` cron-driven; bumps a "scheduled status check" call at the configured time.

Cooldown enforcement is in dialer.py: each (alert_source, target_contact) pair has a last-fired timestamp; suppresses re-calls within the window.

Concurrent-call rule (operator-decided): single-call only. If an alert fires while a call is active, **log it** (`glados/logs/sip_alerts_pending.log`) and surface the pending alert in the WebUI. No automatic retry after the call ends — operator reviews the log.

---

## Audio bridge

### Codec negotiation

SDP offer/answer at INVITE time. Preference order: PCMU (μ-law 8 kHz) > PCMA (A-law 8 kHz) > G.722 (wideband 16 kHz). Most PBXes default to PCMU; G.722 if available is better quality at no extra latency.

### Resampling

PSTN audio is 8 kHz. Existing STT pipeline expects 16 kHz mono PCM. Resample 8k → 16k on inbound, 16k → 8k on outbound. Use scipy.signal.resample or sox if already in image.

### TTS streaming

Piper VITS already supports streaming output. audio_bridge consumes Piper's chunks as they arrive, resamples to 8 kHz μ-law, packets into RTP frames (20 ms × 160 samples = 1280 bytes per packet at 8k), sends out the RTP socket.

Stream-start latency: target <500 ms from "TTS first token" to "first RTP packet on wire."

### VAD

Existing STT pipeline has VAD. Reuse, tuned for phone audio (more aggressive silence detection — `vad_silence_ms: 800` instead of WebUI's typical 1200, since callers expect quicker turn-taking on the phone).

### Self-listen prevention

While TTS is playing, audio_bridge feeds RTP-out → loopback into STT-in. Mute STT input during TTS playback to avoid GLaDOS hearing herself. Resume STT 100 ms after TTS final packet.

---

## Recordings

### Storage layout

```
media/sip-recordings/
├── 2026-05-08T14-22-31_inbound_glados-mobile.mp3      # Mixed audio
├── 2026-05-08T14-22-31_inbound_glados-mobile.json     # Metadata
├── 2026-05-08T14-22-31_inbound_glados-mobile.txt      # Transcript with speaker labels
├── 2026-05-08T15-04-12_outbound_mom.mp3
├── 2026-05-08T15-04-12_outbound_mom.json
├── 2026-05-08T15-04-12_outbound_mom.txt
└── ... (FIFO 5 — oldest pruned past this)
```

### Metadata JSON

```json
{
  "call_id": "2026-05-08T14-22-31_inbound_glados-mobile",
  "direction": "inbound",
  "remote_aor": "sip:operator@192.168.1.1",
  "remote_caller_id": "Operator Mobile",
  "started_at": "2026-05-08T14:22:31.420Z",
  "ended_at":   "2026-05-08T14:25:18.730Z",
  "duration_s": 167.3,
  "pin_attempts": 1,
  "pin_outcome": "accepted",
  "contact_name": null,
  "alert_source": null,
  "transcript_path": "2026-05-08T14-22-31_inbound_glados-mobile.txt",
  "audio_path":      "2026-05-08T14-22-31_inbound_glados-mobile.mp3"
}
```

### Transcript format

```
[14:22:34] GLaDOS:  Oh. I appear to be in a phone again...
[14:22:38] Caller:  8316
[14:22:39] GLaDOS:  Acknowledged. So I'm in your phone now...
[14:22:45] Caller:  Is the doorbell sensor armed?
[14:22:47] GLaDOS:  Yes. Last triggered eleven minutes ago by a delivery person...
```

### FIFO pruning

On each successful save, glob `media/sip-recordings/*.mp3`, sort by mtime, delete trios (mp3 + json + txt) past index 5.

---

## Container & networking

### Docker compose changes

```yaml
services:
  glados:
    # ... existing config ...
    ports:
      - "8015:8015"
      - "8052:8052"
      - "5051:5051"
      # SIP additions — only published when sip.enabled in image config
      - "5060:5060/udp"
      - "16384-16484:16384-16484/udp"
    environment:
      # ... existing env ...
      GLADOS_SIP_ENABLED: "${GLADOS_SIP_ENABLED:-false}"
```

The compose file always declares the SIP ports, but `GLADOS_SIP_ENABLED=false` (default) means the SIP module never binds them — they're allocated but idle. Operator flips `GLADOS_SIP_ENABLED=true` in `.env` to activate, no compose-file edit needed.

The configs/sip.yaml `enabled: true` is a second gate — env-var enables the module to load, YAML enables it to register and accept calls.

### Why bridged + port-forward, not host networking

`host` networking would simplify SIP+RTP NAT traversal (SIP's biggest historical pain point) but would:
- Conflict with the existing port-published topology (8015/8052/5051 currently bridged-and-forwarded)
- Expose every container-internal listener to the host's network namespace
- Break the certs/SSL termination layer that assumes specific binds

Bridged + UDP forward keeps the topology consistent. PyVoIP handles NAT awareness via the `MYIP` config to advertise the correct address in SDP.

---

## Security

### PIN

- 4 digits, plaintext in `configs/sip.yaml` (bind-mounted; not in image; not in git history).
- Three failures → hangup. No retry counter persists across calls — a determined attacker can call back, but PSTN call-volume + carrier-level CallerID logging is the practical defense, not in-app rate limiting.
- WebUI page (lands in Slice 2) shows last-N PIN failure events for operator review.

### CallerID allowlist

`inbound.allow_caller_ids: []` empty by default — any caller can attempt the PIN. If operator populates with their mobile's AOR, GLaDOS rejects unknown callers with 403 before greeting (cheaper than greeting + PIN gate).

### Outbound destinations

- `outbound.enabled` master gate.
- `outbound.contacts` allowlist — `call_contact` raises on miss.
- Carrier-level block at operator's PBX/carrier is the actual line of defense against malicious destinations (LLM hallucinated numbers, prompt injection from a caller, etc.). In-app allowlist is reinforcement.

### Recording privacy

- Recordings are only on the bind-mounted media volume. Not in image, not in git.
- FIFO 5 limits exposure window.
- Operator can disable recording globally (`recordings.enabled: false`) or wipe the directory at any time.

### Audit logging

Every call generates an entry in the existing audit log (`glados/audit/`):

```
2026-05-08T14:22:31Z origin=sip direction=inbound remote=Operator-Mobile pin_outcome=accepted duration=167s recording=2026-05-08T14-22-31_inbound_glados-mobile.mp3
```

Same audit format as the existing `webui_chat` rows.

---

## Latency strategy

Phone callers expect a response onset under 500 ms. Engine round-trip on chat path is 4-7 s. Mitigation, in execution order:

1. **Streaming TTS.** Don't wait for the LLM to finish — start speaking as soon as the first sentence arrives. Buys 2-4 s.
2. **Filler phrases.** If first LLM token hasn't arrived within `filler_threshold_ms` (default 1500), audio_bridge plays a random filler from `latency.filler_phrases` while continuing to wait. Buys ~1-2 s of perceived latency.
3. **Canned screening responses.** PIN entry, "wrong PIN" rejection, hangup goodbye are pre-recorded MP3s in the image (not generated per-call). Zero latency for those.
4. **(Optional) 4B autonomy model.** `latency.use_autonomy_model: true` routes SIP turns through the 4B model instead of qwen3:14b. Sub-second TTFT. Persona thinner but on a phone may be acceptable. **Defer this opt-in to operator testing.**

Slice 1 ships with (1)+(2)+(3). (4) becomes available but defaults off, operator flips if testing reveals (1)+(2)+(3) still feels broken.

---

## Testing strategy

### Unit tests (per module)

- `pin_gate`: spoken-digit recognition, DTMF buffering, failure counter, hangup trigger.
- `audio_bridge`: codec resample fidelity, RTP packet timing, self-listen mute.
- `dialer`: contact lookup, INVITE construction, failure code mapping.
- `recording`: MP3 encoding, transcript formatting, FIFO pruning.
- `contacts`: allowlist resolution, raise-on-miss.

### Integration tests

- Mock SIP server (e.g., `mock-sip` in pytest fixture) that scripts INVITE / 200 OK / RTP exchange. End-to-end test: INVITE → greeting → PIN → "is the time?" → response → BYE. Assert recording saved, transcript matches.
- DTMF integration: send RFC 2833 events during PIN phase, assert pin_gate accepts.
- Outbound integration: invoke `call_contact("test")`, mock SIP server replies 200 OK, assert dialer wires call_session correctly.

### Manual operator validation

- Slice 1: operator calls dedicated DID from mobile, runs through PIN entry, has a real conversation about house state, hangs up. Reviews recording in `media/sip-recordings/`.
- Slice 2: operator says "GLaDOS, call my mobile" while standing next to themselves. Picks up the inbound, observes the conversation transcript while talking through both sides.
- Slice 3: trigger doorbell-press with autonomous opt-in enabled, observe outbound call to operator-mobile with the screener's text as the message.

---

## Out of scope

- **Multi-line / multi-call concurrency.** Single active call only. Future work if call volume justifies.
- **Conference calls.** N-way call support is significantly more complex; not pursued.
- **Call transfer / blind transfer / consultative transfer.** Not pursued.
- **SIP TLS / SRTP.** Plaintext SIP+RTP for v1, on the assumption the operator's PBX is on the LAN. TLS + SRTP can be added later if SIP traffic ever crosses an untrusted network.
- **Voicemail.** GLaDOS doesn't ring-and-store-on-no-answer. If she's offline, the call fails.
- **HOLD / unhold.** Not pursued.
- **STIR/SHAKEN attestation.** Not pursued for v1; only matters for outbound calls hitting carrier networks.
- **Caller-ID name display tuning.** Outbound CallerID is whatever the PBX advertises.

---

## Out of scope, explicitly because of operator-stated constraints

- **Bypassing the carrier-level block to extend in-app outbound allowlist.** Operator's carrier block is the primary defense; the in-app allowlist is reinforcement. Don't engineer around the carrier.
- **Reading prompted PINs aloud back to the caller before validation.** No "you entered 8316, is that correct?" — that leaks the PIN to anyone in earshot.
- **Persona softening on phone.** The "displeased / potato form" tone is the brief, not a bug. Resist drift toward "helpful AI assistant phone tone."

---

## Open risks

1. **PyVoIP maturity.** Pure Python SIP is rare and pyVoIP is small-team / single-maintainer. Risk: edge cases in NAT traversal, codec negotiation, or DTMF that take debugging time. Mitigation: keep `glados/sip/client.py` thin wrapper so we can swap to `pjsua2` (compiled C, mature) without rewriting call_session / audio_bridge.
2. **STT digit recognition accuracy.** Spoken digit strings ("eight three one six") have moderate WER on small models. Mitigation: prefer DTMF in the operator's mental model — instruct operators to DTMF the PIN unless voice-only is the only option (e.g., hands-free driving). Greeting could explicitly say "press your PIN."
3. **Latency feels broken despite mitigations.** Mitigation 4 (4B model) is the escape hatch. Real-world test with operator before declaring Slice 1 done.
4. **Concurrent call edge: alert during call.** Logged-only design means alerts can miss their window. Mitigation: alert log surfaced in WebUI with timestamp + source, so operator can act on the missed alert post-call. Future: a higher-priority alert (fire) could break the call — out of scope for v1.
5. **Container restart during active call.** Active call drops. Mitigation: SIP doesn't have built-in reconnect for media-mid-call; nothing GLaDOS can do. Avoid container restarts during expected call hours; document the limitation.
6. **PSTN audio quality on TTS playback.** 8 kHz μ-law severely band-limits Piper's output (cuts off at 4 kHz). The "diminished" feel is partly genuine, partly intentional. Mitigation: prefer G.722 codec when PBX supports it (16 kHz wideband) — this is the existing `audio.codec_preference` config.

---

## Dependencies

- **Operator-side:**
  - PBX provisioning: dedicated SIP extension, dedicated DID number not routed to house phone.
  - Outbound carrier-level block on unauthorized destinations.
  - GLaDOS extension's CallerID setup so outbound calls show as "GLaDOS" or operator-chosen name.
- **Container-side:**
  - Add `pyVoIP` to requirements.txt.
  - Add `scipy` (for resample) — already present? Verify; otherwise add.
  - Add `pydub` or `lameenc` for MP3 encoding.
  - Docker-compose port forwarding for SIP+RTP.
- **Approach 2 dependency for Slice 2:**
  - Slice 2's WebUI page lands in v3-native vocabulary. Approach 2 (design-system v3 page sweep, ~9h) must complete first OR Slice 2's page is built on current chrome and re-swept later (option B in the prior conversation — operator picked option A, so Approach 2 first).

---

## Deliverables per slice

**Slice 1 (Inbound):**
- `glados/sip/` modules per layout above
- `configs/sip.yaml` example
- `tests/sip/` unit + integration tests
- Docker-compose port additions
- `requirements.txt` additions (pyVoIP, MP3 encoder)
- Audit logging integration
- `docs/CHANGES.md` Change 44 entry
- Operator-validated call test

**Slice 2 (Outbound manual + WebUI):**
- `glados/sip/dialer.py`, `contacts.py`, `tools.py` (`call_contact`)
- New WebUI Configuration → SIP page (depends on Approach 2)
- "Recordings" list view with play/download
- Voice command intent for "GLaDOS, call X"
- `docs/CHANGES.md` Change 45 entry
- Operator-validated outbound call

**Slice 3 (Outbound autonomous):**
- Hooks in `glados/doorbell/screener.py` (and other alert sources)
- Per-source opt-in config + cooldown enforcement
- Concurrent-call alert log + WebUI surface
- `docs/CHANGES.md` Change 46 entry
- Operator-validated alert-driven call

---

## Effort estimate

| Slice | Estimate | Confidence |
|---|---|---|
| 1 — Inbound | 10–14 h | Med-high (pyVoIP unknowns) |
| 2 — Outbound manual + WebUI page | 6–9 h | Med (lighter SIP work, more UI) |
| 3 — Outbound autonomous | 3–5 h | High (hooks into existing modules) |
| **Total** | **19–28 h** | |

Slice 1 is the foundation and the highest-risk piece (SIP/RTP plumbing). Slices 2 and 3 ride on top of it.
