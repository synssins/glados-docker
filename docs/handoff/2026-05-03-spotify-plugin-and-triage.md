# 2026-05-03 — Spotify plugin shipped; triage flakiness on OVMS unresolved

**Status when handoff written:** Spotify plugin works end-to-end on the live
container. LLM-driven plugin chat is gated by an intermittent triage-step
timeout against OVMS Qwen3-30B. Multi-model OVMS attempt failed (small
0.6B model loads but isn't HTTP-routable). OVMS reverted to single-model
30B serving. Container is healthy.

---

## What's working live (verified end-to-end)

| Component | Evidence |
|---|---|
| Spotify plugin auth (user-context refresh-token flow) | `searchSpotify` returned 670 ms with real results; persisted access token works for `/v1/me/player/devices` and `/v1/me/player/play` |
| Wrapper's pre-mint of access token from refresh token | Bypasses upstream's "no accessToken → fall back to client-credentials" bug — direct cause of `404 "Invalid username"` on user-only endpoints |
| Wrapper preserves Spotify's rotated refresh token | Spotify rotates on every PKCE exchange; wrapper now keeps the rotated value |
| play.js auto-resolves friendly device names + HA entity ids | LLM passed `media_player.sonos_master_bedroom`, handler stripped prefix → matched "master bedroom" → played on `Master Bedroom Echo` Spotify device id |
| GLaDOS chat → Spotify play tool dispatch | Confirmed: track `0RrhwwIqnfCDPZD7DfWAVj` ("Still Alive") played on Echo at 03:00:16 UTC, then on `Living Room` AVR at 04:48:14 (direct API call bypassing triage) |
| Living Room playback (direct API) | `now_playing` reported `device='Living Room' (AVR)` playing `Still Alive (From "Portal")` by Jonathan Coulton, Triforce Quartet — proves the full chain works given a working triage |

## What's NOT working — the triage flake

`glados/plugins/triage.py::triage_plugins` calls `services.yaml::llm_triage`
(currently the same OVMS Qwen3-30B-A3B-int4-ov as the chat). Symptom:

- ~50% of chat turns: triage returns in 0.3-1.5 s with the right routing.
- ~50%: OVMS doesn't respond before the 15 s ceiling — `empty response after 15017 ms`.
- When triage returns nothing, the chat LLM never sees plugin tools, so
  the user gets a persona-mode refusal that has no idea Spotify exists.

OVMS direct-probe results captured in this session:
- Cold path: 18.5 s for a 32-token reply
- Warm path: 5.0 s for the same reply
- Adding `/no_think` to the system prompt did NOT cut latency materially —
  Qwen3 still emits some thinking tokens before the JSON answer

**Root cause:** Qwen3-30B is too heavy for inline triage on this hardware
(Intel Arc Pro B60), AND OVMS doesn't currently parallelise triage and
chat well — they queue on the same model.

## Multi-model OVMS attempt (failed; documented for the next try)

Goal: serve a small fast model (`OpenVINO/Qwen3-0.6B-int4-ov`, ~600 MB
int4, CPU-only) alongside the 30B for triage routing.

**What I did:**
1. `ovms.exe --pull --source_model OpenVINO/Qwen3-0.6B-int4-ov --task text_generation --model_repository_path C:\AI\models`
   → model cached at `C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov\` with its
   own `graph.pbtxt` (defaulted to `device: "CPU"`)
2. Built `C:\AI\models\config.json` via `ovms.exe --model_name X --add_to_config --config_path ... --model_repository_path ...`
   for both models. Resulting JSON looked correct:
   ```json
   {
     "model_config_list": [
       {"config": {"name": "OpenVINO/Qwen3-30B-A3B-int4-ov", "base_path": "..."}},
       {"config": {"name": "OpenVINO/Qwen3-0.6B-int4-ov",   "base_path": "..."}}
     ]
   }
   ```
3. Replaced `ovms_serve.bat`'s `--source_model` invocation with
   `--config_path C:\AI\models\config.json` (single flag).
4. `nssm restart ovms`.

**What broke:**
- Stdout logged BOTH models reaching `state changed to: AVAILABLE` cleanly
- 30B kept working: `curl POST .../v3/v1/chat/completions {"model":"OpenVINO/Qwen3-30B-A3B-int4-ov", ...}` → 200 OK
- 0.6B did NOT route: same shape with `"model":"OpenVINO/Qwen3-0.6B-int4-ov"`
  → `{"error":"Mediapipe graph definition with requested name is not found"}`
- `/v1/config` listed only the 30B at runtime
- I tried name variants (`Qwen3-0.6B-int4-ov`, `qwen3-0.6b-int4-ov`,
  `Qwen3-0.6B`, `OpenVINO_Qwen3-0.6B-int4-ov`) — all rejected with the same error

**Hypothesis (unverified — needs 1-2 hr of OVMS docs digging):**
`--add_to_config` writes the legacy `model_config_list` (KFServing-style
inference, not LLM/Mediapipe). For LLM continuous-batching, OVMS likely
needs a different config section — probably `mediapipe_config_list` with
explicit `graph_path` per model. The 30B works because it was ORIGINALLY
deployed via `--source_model` which did the right Mediapipe registration
in-memory; the config.json was secondary.

**Reverted at session end:**
- `C:\AI\ovms\ovms_serve.bat` restored from `ovms_serve.bat.bak` (single-model 30B)
- `nssm restart ovms` → 30B back to AVAILABLE → chat verified working
- `C:\AI\models\config.json` left in place (harmless — OVMS isn't reading it now)
- `C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov\` left in place (already downloaded)
- `C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov\graph.pbtxt` was hand-edited to add reasoning_parser:"qwen3" + bumped tuning — not load-bearing

## Patches that ARE live (did not get reverted)

### Container side (1) — runtime patches in `/app/`

These were pushed via `docker cp` to the running container. They survive
restarts of the container itself but NOT a re-deploy of the GHCR image
(which would replace `/app` from the freshly-baked image — see "What's in
git" below for what's mirrored to the repo).

- `/app/data/plugins/spotify/plugin.json` — runtime block:
  ```json
  {"mode": "bundled", "command": "sh", "args": ["-c", "exec node \"$GLADOS_PLUGIN_DIR/src/wrapper.mjs\""]}
  ```
- `/app/data/plugins/spotify/src/wrapper.mjs` — full content matches the
  Desktop bundle wrapper (sha 835370a4db47…). Includes:
  - `sh -c` invocation so `$GLADOS_PLUGIN_DIR/src/wrapper.mjs` resolves at spawn time
  - Pre-mints an access token from refresh token before importing upstream
    (fixes upstream's `client_credentials` fallback when accessToken is empty)
  - Preserves rotated refresh token across spawns (Spotify PKCE rotation)
  - Debug log line per spawn at `/tmp/spotify-wrapper-debug.log`
- `/app/data/plugins/spotify/src/build/play.js` — auto-resolves any
  non-Spotify-shape `deviceId` (HA entity names, friendly names, room
  names) by querying `getAvailableDevices` internally and matching by
  name. Returns a clear error with the device list if no match.
- `/app/data/plugins/spotify/src/bootstrap-token.mjs` — stdlib-only PKCE
  refresh-token minter. Used three times this session; survives.
- `/app/glados/plugins/triage.py` — has `/no_think` directive in the
  system prompt and `timeout_s: float = 15.0`. **Both also committed to
  the repo this session — see below.**

### Container side (2) — persisted state

- `/app/data/plugins/spotify/runtime.yaml` has Client ID and OAuth
  Redirect URI in plaintext.
- `/app/data/plugins/spotify/secrets.env` (mode 0600) has the latest
  refresh token. Spotify-side rotation is now preserved by the wrapper —
  the next chat that uses Spotify will likely rotate it again and write
  the new value back through the upstream's `saveSpotifyConfig` path.
- `/app/data/plugins/spotify/src/spotify-config.json` (mode 0600) has
  pre-minted access token + persisted refresh token. The 1-hour access
  token TTL means after restart, the wrapper does a fresh pre-mint.

### Repo / git side — committed to `main`

- `glados/plugins/triage.py` — `/no_think` system prompt + 15 s timeout
  bump. Commit message references this handoff.

### Desktop bundle (`C:\Users\Administrator\Desktop\spotify-mcp-1.0.0.zip`)

The standalone install bundle for fresh installs. 4.76 MB compressed,
sha256 `fc99f3a98fbff65e…`. Contains all the Spotify plugin patches
(wrapper, play.js, bootstrap, plugin.json runtime block). A fresh
operator can install via the WebUI Upload card and follow the README.

## Spotify plugin operator setup (recap)

1. Spotify Developer Dashboard → Create app → Redirect URI
   `http://127.0.0.1:8888/callback` → enable Web API
2. Extract `bootstrap-token.mjs` from the bundle zip onto a machine
   with a browser, set `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`
   env vars, `node bootstrap-token.mjs`
3. Open the printed URL, sign in, click Agree, copy the printed
   refresh token
4. WebUI → Configuration → Plugins → Upload the zip → Configure tab:
   paste Client ID, Client Secret, Refresh Token; default redirect
   URI; Save
5. Toggle Enabled. Plugin tools become available immediately.

**Spotify Premium account required** for playback control endpoints.
**Connect target devices need a live Spotify session** — open Spotify
on phone/tablet/Sonos for the device to appear in
`getAvailableDevices`.

## Three options to unblock LLM-driven plugin chat

These are repeated from the in-session summary; the operator hasn't
chosen yet.

### A. Bypass triage (5 min, recommended for short-term unblock)

Patch `glados/plugins/triage_plugins` to always return `[plugin.name for
plugin in plugins]` when triage is enabled. Skips the LLM call entirely.
Loses the "skip irrelevant plugins" optimization (chat LLM gets all
plugins' tools every turn → slightly more prompt tokens), but the chat
ALWAYS sees Spotify's tools and the LLM-driven path becomes reliable.

```python
# glados/plugins/triage.py — instead of calling the LLM, return all
def triage_plugins(message, plugins, timeout_s=15.0):
    if not _enabled():
        return []
    if not message.strip() or not plugins:
        return []
    return [p.name for p in plugins]
```

### B. Multi-model OVMS done correctly (1-2 hr research + setup)

Find the right `config.json` schema for serving multiple LLM models
under OVMS continuous batching. Likely needs `mediapipe_config_list`
entries pointing at each model's `graph.pbtxt`. Once both models are
HTTP-routable on `:11434`, update GLaDOS `services.yaml::llm_triage`:
- `url`: same OVMS endpoint
- `model`: `OpenVINO/Qwen3-0.6B-int4-ov`

Restart container, triage hits the small model in <500 ms, the chat
stays on the 30B.

Resources to consult next time:
- https://docs.openvino.ai/2025/model-server/ovms_docs_llm_reference.html
- https://github.com/openvinotoolkit/model_server/tree/main/demos/common/export_models
- The `export_model.py` script (referenced but not directly inspected
  this session)

### C. Separate runtime for the small triage model (30-60 min)

Put llama.cpp `llama-server` on a different port serving a small
GGUF model (Qwen3-0.6B-Instruct GGUF, Llama-3.2-1B-Instruct GGUF, etc.).
Update `services.yaml::llm_triage.url` to point at it. Bypasses the
OVMS multi-model question entirely; introduces a second runtime to
maintain.

## Memory updates (in `C:\Users\Administrator\.claude\projects\C--src\memory\`)

Worth adding before the next session starts:
- `feedback_research_ovms_before_changes.md` — operator-flagged today:
  don't change `ovms_serve.bat` without first verifying the new config
  format actually serves models. Wasted ~hour on a bad assumption that
  `--add_to_config` was sufficient for multi-LLM routing.
- `project_spotify_plugin_status.md` — captures the working state of
  the plugin so future sessions don't re-walk the OAuth flow.

## Open commitments / loose ends

- README's persona intro on `main` is intact.
- LAN IPs scrubbed from tracked files (commit `f7fcf47` from prior session).
- `webui-polish` branch is one commit behind `main` post-merge — harmless.
- Triage flake remains the only blocker between "chat path works" and
  "operator can ask GLaDOS to play music on Spotify and have it work
  reliably". The Echo and Living Room playback tests both succeeded
  during this session — the bottleneck is purely that triage gates
  whether plugins reach the chat LLM.
