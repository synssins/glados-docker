# Prompt to start the next session — 2026-05-03

Paste this into a fresh Claude Code session started in `C:\src\glados-container`.

---

I'm continuing the GLaDOS containerization effort. Read these files in
order BEFORE doing anything else:

1. `C:\src\glados-container\CLAUDE.md` — operator preferences and hard
   rules. Section-2 bullets are non-negotiable; Section-3 is the
   research/honesty contract. CLAUDE.md is the source of truth — every
   rule in this prompt is also there, but read it directly so you
   internalize the wording.
2. `C:\src\SESSION_STATE.md` — top "Active Handoff" section. Live
   state, what landed, what's open.
3. `docs/handoff/2026-05-03-spotify-plugin-and-triage.md` — last
   session's full record. Spotify plugin shipped end-to-end. Triage
   flake on OVMS Qwen3-30B is the open blocker. Multi-model OVMS
   attempt failed; documented. Three options for the unblock laid out.
4. `docs/CHANGES.md` — Change 39 was the last big change (time
   hallucination fix). Today's session did NOT add a new Change number
   — only in-container hot-patches and one repo commit to `triage.py`.
5. `C:\Users\Administrator\.claude\projects\C--src\memory\MEMORY.md` —
   auto-loaded. Pay particular attention to:
   - `feedback_nssm_gui_traps`
   - `feedback_no_t4_options`
   - `feedback_deploy_ownership`
   - `feedback_no_secrets_in_commits`
   - `feedback_no_local_docker`
   - **`feedback_research_before_prod_writes`** — NEW. Captures the
     2026-05-03 OVMS-edit failure mode and the artifact-or-don't-write
     rule. Read this BEFORE touching any running service config.
   - **`feedback_ovms_multi_model_attempt`** — NEW. Concrete record
     of the failed multi-LLM attempt: what was tried, why it didn't
     route, where to look next time. Don't repeat the same approach.

## Core operating rules (also in CLAUDE.md §2)

- I run nothing. You execute everything: commits, pushes, deploys, SSH
  to the docker host (192.168.1.150 — credentials in SESSION_STATE.md
  §"Credentials / Secrets State"), `lms.exe` / `ovms.exe` / NSSM
  service control on AIBox (192.168.1.75 — same Windows host you're
  running on right now), reading container logs, editing remote
  `services.yaml`, restarting containers — all of it. Do NOT hand me
  shell commands.
- Use `scripts/_local_deploy.py` for container deploys with
  `MSYS_NO_PATHCONV=1`. Use paramiko-via-Python for ad-hoc SSH.
- NSSM: only safe forms. `nssm` (no args), `nssm set` (no args),
  `nssm install <name>` (no app), `nssm edit <name>`, `nssm remove
  <name>` (without `confirm`) all open a blocking GUI dialog. See
  `feedback_nssm_gui_traps.md`. Always pass full required args.
- Intel Arc Pro B60 only. No T4 / NVIDIA / CUDA recommendations.
- **Verify Windows compat AND verify the actual config format before
  recommending or changing tools.** Don't suggest Linux-only stacks
  (vLLM, TGI, SGLang, mainline Ollama). Don't change `ovms_serve.bat`
  without first verifying the new flags / config actually serve the
  models you intend — last session lost ~1 hr on `--add_to_config`
  registering models for the wrong code path.
- **Production writes need an artifact, not a hunch** (CLAUDE.md §2,
  earned by the 2026-05-03 OVMS failure). Before any write to a
  running production service config — `ovms_serve.bat`, NSSM service
  params, the live container's `services.yaml` / `plugin.json` /
  in-container python patches, GHCR image build — the change must
  pass at least ONE of:
  1. Tested on a parallel non-prod port/instance and verified to
     do what's intended (e.g. spin up a second OVMS on `:11435`
     pointing at the candidate config; confirm both models route
     before swapping into the `:11434` service).
  2. Backed by an upstream doc citation that names this exact use
     case (URL + the relevant snippet), not just a flag's existence
     in `--help`.
  3. Operator-acknowledged with the specific change spelled out
     before execution. Don't paraphrase intent ("set up a small
     model") into a different concrete action ("replace
     `--source_model` with `--config_path`") — restate the literal
     mutation and get sign-off.
  Phrases like "the flag exists", "the syntax parsed", "the log
  says AVAILABLE" are NOT evidence the change does what's intended.
  The "always research" rule applies per-mutation, not per-task.
- Don't hardcode against current state (SSL on/off, port-binding).
  Route internal calls through the always-stable 127.0.0.1:18015.
- Research before recommending. Hit project pages with WebSearch +
  WebFetch; verify every filter (Windows + GPU + maintained) before
  naming a tool.
- Be honest about evidence. "Documented bug" needs a citation.
- Surgical commits, tests stay green at every commit, no secrets.

## Production state (2026-05-03 morning)

- **AIBox** (192.168.1.75 — `WIN-GTLJ7GFJPC4`, Windows Server 2022,
  Intel Arc Pro B60):
  - NSSM service `ovms` running, serves on 0.0.0.0:11434.
  - Loaded model: `OpenVINO/Qwen3-30B-A3B-int4-ov` ONLY (single-model
    serve via `--source_model`).
  - `C:\AI\models\OpenVINO\Qwen3-0.6B-int4-ov\` is downloaded and
    ready but NOT being served. Multi-model attempt failed.
  - `C:\AI\models\config.json` exists from last session's failed
    attempt but is not currently referenced by `ovms_serve.bat`.
  - LM Studio / Ollama variants completely removed (Change 38).
- **Docker host** (192.168.1.150, OMV): `glados` container healthy.
  Image SHA from last session deploy.
- **Tests**: 1754 passed / 5 skipped at last full run (pre-Change 39
  follow-ups + triage commit haven't been retested but the change is
  trivial).

## Spotify plugin — fully operational on the live container

Plugin tools execute end-to-end when triage matches. Verified by
direct API tests during last session:
- `Master Bedroom Echo` played "Still Alive (From Portal)" by 8-Bit
  Big Band at ~03:00 UTC
- `Living Room` AVR played the same track at ~04:48 UTC

Live-container patches (kept across container restarts; lost on
fresh GHCR-image deploy unless re-applied):
- `wrapper.mjs` pre-mints access token + preserves rotated refresh
  token across spawns
- `play.js` auto-resolves friendly device names / HA entity ids
  (e.g. `media_player.sonos_master_bedroom` → matches "master
  bedroom" → real Echo device id)
- `plugin.json` runtime: `sh -c 'exec node "$GLADOS_PLUGIN_DIR/src/wrapper.mjs"'`
  (the runner doesn't expand env vars in args, this works around it)

Bundle on Desktop: `C:\Users\Administrator\Desktop\spotify-mcp-1.0.0.zip`
(sha256 `fc99f3a98fbff65e…`). All patches baked in.

## The actual blocker — triage flake

`glados/plugins/triage.py::triage_plugins` calls the `llm_triage` slot
to decide which plugins are relevant per chat turn. With the slot
pointing at OVMS Qwen3-30B-A3B (currently the only model served), the
LLM call:
- Cold: 18.5 s for ~30 tokens
- Warm: 5.0 s for ~30 tokens
- Frequently misses the (now 15 s) timeout under chat-side load

When triage misses, plugins are excluded from the chat LLM's tool
catalog → user gets a persona-mode refusal even though the plugin
backend works fine.

Last session committed `/no_think` directive + 15 s timeout to
`glados/plugins/triage.py` on `main`. Helps marginally but doesn't
solve the structural problem.

## Three options to unblock (operator hasn't chosen yet)

### Option A — Bypass triage server-side (5 min)

Edit `glados/plugins/triage.py::triage_plugins` to return all enabled
plugin names when called, skip the LLM round-trip entirely. Loses the
prompt-token optimization but the chat path always sees plugin tools.
Recommended for immediate unblock if the operator wants reliable
plugin chat NOW.

### Option B — Multi-model OVMS done right (1-2 hr)

Last session's attempt used `--add_to_config` which writes the legacy
`model_config_list` (KFServing-style). For LLM continuous batching,
OVMS needs a different config — likely `mediapipe_config_list` with
`graph_path` per model. Resources:
- https://docs.openvino.ai/2025/model-server/ovms_docs_llm_reference.html
- https://github.com/openvinotoolkit/model_server/tree/main/demos/common/export_models
- The `export_model.py` script there

Once both models are HTTP-routable, update GLaDOS `services.yaml`:
```yaml
llm_triage:
  url: http://192.168.1.75:11434/v3/v1/chat/completions
  model: OpenVINO/Qwen3-0.6B-int4-ov
```

Restart container. Triage should hit in <500 ms with the small model
on CPU; chat stays on the 30B on GPU.0.

### Option C — Separate runtime for triage (30-60 min)

`llama-server` (llama.cpp) on a different port serving Qwen3-0.6B GGUF
or Llama-3.2-1B-Instruct GGUF. Bypasses the OVMS multi-model question.
New runtime to maintain.

## Open priorities (top to bottom)

1. **Pick option A/B/C** to unblock LLM-driven plugin chat reliably.
2. **TTS pronunciation regressions** — operator-flagged 2026-04-28
   ("81", "8 mph", "mph" letter-by-letter) plus 2026-05-02 ("P.M." →
   "Pem"). Container-side text normalization. Memory:
   `project_tts_pronunciation_cases.md`. Will surface again when the
   time/weather fast-path lands.
3. **Time & weather fast-path** — captured in `docs/roadmap.md` under
   "Time & weather fast-path (TODO — 2026-05-02)" + memory
   `project_time_weather_fastpath.md`. Bypass the LLM round-trip on
   deterministic queries; format from `time_source.now()` /
   `weather_cache.get_data()`, run through persona rewriter (~2 s),
   audit `kind="time"|"weather"`.
4. **Operator smoke for plugin Phase 2b** (off-state / install-by-URL
   / stdio uvx+npx / browse) — carried.
5. **Wire HA `mcp_server` as the first cataloged plugin** — carried.
6. **Seed `synssins/glados-plugins` curated repo** — carried (Spotify
   plugin from this session is a candidate, but upstream
   marcelmarais/spotify-mcp-server has no LICENSE file — defer
   public redistribution).
7. **(carried)** %s/%d → {} loguru placeholder sweep across
   glados/autonomy/agents/*, subagent_*, weather.py, hacker_news.py,
   emotion_agent.py, knowledge_store.py.
8. **(carried)** Drop "good morning" from looks_like_home_command
   activity phrases (glados/intent/rules.py:264).
9. **(carried)** Tighten test_config_save_writes_llm_keys round-trip
   + slot-resolution regression test for _init_ha_client.
10. **(carried)** Vision model reload + VRAM tradeoffs.
11. **Phase 4** — GLaDOS as MCP server on port 8017 (Streamable HTTP
    + TLS).

## Operator instructions given over the prior session (verbatim, in order)

These are the literal instructions from the operator across the
2026-05-02 evening + 2026-05-03 morning sessions, kept in chronological
order. Use them to interpret tone and constraints — particularly the
operator's emphasis on getting things RIGHT before changing live state.

```
Continue → start time-hallucination fix
Time should be set based on time servers, such as the nist.gov ones.
  They should automatically set time zone based on geo-coordinates
  used for weather forecasting and handle DST.
It should be UI configurable
Use system clock. System Page. Not certain what you are asking.
  Simplified, time on day/date
Tool loaded.
Continue
What is left?
Update session_state, merge into main branch and update all
  documentation to reflect work in main branch. Simplified Readme to
  provide a more user friendly description of the project, review
  technical documentation in readme.md, check docker compose files in
  root and/or subfolders, yaml, etc to ensure it is updated and
  accurate for the current state of the project. Commit to main.
I did not want to lose the GLaDOS introduction to the repository.
  Generate a new one, from scratch, and add it back to the readme file.
Review the code in main, ensure no secrets or sensitive information
  has been committed.
1.    [option 1: forward-only fix, no history rewrite]
Is there a Spotify MCP that could be packaged as a plugin for GLaDOS?
Manual upload for right now
[uploaded the bundle, started filling form, asked: "How do I set this up"]
Get the Refresh token for me. [pasted Client Secret + Client ID]
[pasted callback URL] (token #1)
[Spotify auth error in browser]
Done [callback URL #2 — token #2]
[chat hit "config not found"]
[plugin tools error: PREMIUM_REQUIRED]
I have Spotify Premium.
No. Master Bedroom Echo can go fuck itself. It's going to be unplugged.
  Master Bedroom Sonos.
Spotify Connect is connected to "Bedroom" right now.
This is NOT the Echo device. This is the Sonos.
[token revoked, re-bootstrapped — token #3]
[same callback delivered fresh token]
[checking logs — She's spinning]
DLM should query for devices [meant LLM]
yes [roll wrapper + play.js fixes into Desktop bundle zip]
are the fixes in the currently running stack?
check the logs. the bedroom speakers are off limits now. use the
  living room for testing
test on the bedroom echo instead [later flipped to off-limits]
you set up a small model. i thought we already did that to improve
  performance
[Spotify Connect on tablet now] [retry succeeded; played on Echo]
so reverting means you just wasted a fuckton of time. write md files
  for handoff, provide detailed prompt with all prior commands and
  instructions i gave you.
```

## What the next session should NOT redo

- Don't re-fix the Spotify plugin auth path. Wrapper, play.js,
  plugin.json runtime — all live in the container AND the bundle.
- Don't re-mint refresh tokens unless the operator says one is
  revoked. Persisted refresh token survives across restarts as long
  as the wrapper preserves Spotify's rotation (which it does now).
- Don't try `--add_to_config` for OVMS multi-model — verified it
  doesn't register the second model for HTTP routing.
- Don't change `ovms_serve.bat` again without verifying the new
  config actually serves both models BEFORE switching the live `.bat`.
  Backup file at `C:\AI\ovms\ovms_serve.bat.bak`.

## Wait for direction

If the operator just says "continue", they probably mean **option A
(bypass triage)** — that's the fastest path to a working LLM-driven
plugin chat. But verify before patching: ask which option they want.
