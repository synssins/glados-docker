# CLAUDE.md

Behavioral guidelines for Claude Code sessions on this repo. Operator
maintains this file; re-read at the start of every session.

---

## 0. Orientation — READ FIRST

Before making changes to this repo, read these files in order:

1. **`C:\src\SESSION_STATE.md`** — current project status, deployed
   commit, credentials, next steps. Authoritative source for "what
   is live right now." Re-read every session.
2. **`docs/CHANGES.md`** — chronological change log. Every structural
   change documented with rationale and side effects. Most recent
   entry at the bottom (currently Change 24, 2026-04-25).
3. **`docs/battery-findings-and-remediation-plan.md`** — Phase 8.x
   battery analysis and remediation plan (complete as of 2026-04-21).
   Useful for understanding the voice-agent quality bar the operator
   expects.
4. **`docs/roadmap.md`** — prioritized list of remaining work items.
5. **`docs/Stage 1.md`** — original Stage 1 plan (pure middleware
   refactor).
6. **`docs/Stage 3.md`** — HA Conversation Bridge + MQTT Peer Bus
   architecture (Phase 1 done; Phase 2 MQTT pending).

**Portability rule.** This repo is the single source of truth for
the GLaDOS container. Any change that affects runtime behaviour must
land inside this repo's code, configs, or docs. Host-specific
deployment details (AIBox paths, Docker host paths, credentials)
belong in `C:\src\SESSION_STATE.md`, never in the committed code.

---

## 1. Project state snapshot (2026-04-24)

- **Deployed on the operator's Docker host**: `ghcr.io/synssins/glados-docker:latest`
  at commit `cd3bad2` (image SHA `0a6a03e3`). Healthy. (Prior snapshots reference
  commit hashes that no longer exist — history was rewritten with
  `git-filter-repo` during the 2026-04-23 secrets scrub.)
- **Auth rebuild shipped** (2026-04-25). Multi-user Argon2id auth,
  `/setup` first-run wizard, role-based sidebar, `GLADOS_AUTH_BYPASS`
  recovery path. See CHANGES.md Change 23–24.
- **Scope discipline:** this repo is a consumer of external
  services (Ollama, Home Assistant, Speaches, ChromaDB, MQTT).
  Those services run on separate systems with stock firmware from
  this container's perspective — we treat each as an opaque HTTP
  endpoint. GPU hardware, model-file tuning on Ollama, HA config,
  Speaches phoneme lexicon, etc. are **out of scope**. All work
  stays inside this repo's code, configs, tests, Dockerfile, and
  CI.
- **Engine models**: chat, Tier 2 disambiguator, and persona
  rewriter all point at `qwen3:14b` on a single configured Ollama
  endpoint (URL set per operator deployment).
- **Tests**: 1157 passed / 5 skipped. `pytest -q` runs in ~42 s.
- **Phase 8.x remediation plan complete.** 8.0 → 8.14 all shipped.
- **Phase Emotion A–I complete** (2026-04-22 / 2026-04-23) —
  deterministic repetition math, semantic clustering, command
  flood detector, hard-rule response directive, PAD→Piper audio
  override, rewriter band overlay, and operator-tunable config
  via Personality → Voice production. See CHANGES.md Change 22.
- **Self-hosted GitHub Actions runner** installed on the AIBox LAN
  host — service
  `actions.runner.synssins-glados-docker.aibox-glados-lan`, label
  `glados-lan`. **Manual dispatch only.** Cron was removed because
  the battery flips physical lights in an occupied house. Operator
  triggers from the Actions tab when awake.
- **CI**: `.github/workflows/tests.yml` gates PRs on the 1069-test
  suite. `.github/workflows/build.yml` publishes the Docker image
  to GHCR. `.github/workflows/battery-nightly.yml` is
  manual-dispatch-only for the live battery.
- **Secrets in repo**: `HA_TOKEN`, `GLADOS_SSH_PASSWORD`,
  `SNYK_TOKEN` in Actions Secrets. Do not commit tokens to source.
  **Hard rule:** never commit tokens, passwords, LAN IPs, real HA
  entity names, personal domains, or operator identity — in code,
  tests, docs, OR commit messages/bodies. Prior sessions violated
  this and the entire history had to be rewritten via
  `git-filter-repo`. Custom gitleaks rules now catch the operator's
  specific patterns (see `.gitleaks.toml`).

### Known open items (not urgent)

- Quip library content at 156 lines; plan target was ~450.
- Harness scratch dir at `C:\src\glados-test-battery` should
  eventually land in its own git repo.
- TTS pronunciation defaults expansion as operators surface more
  Piper-slurred cases (container-side pronunciation overrides,
  not the upstream Piper voice).

---

## 2. Operator preferences (learned over this project)

These are tuned for the operator's working style. Deviations
require explicit confirmation.

- **Ship in surgical, reviewable chunks.** Each commit should be
  self-contained and reversible. One bug class per commit, one
  feature per commit.
- **Diagnostics before speculation.** When a bug presents, prove
  what's happening (logs, instrumentation, observation) before
  guessing at fixes. The Phase 8 non-streaming onion ate four
  commits because early layers were speculative — that was waste.
  Always gather evidence first.
- **No destructive actions without explicit approval.** Never
  `rm -rf`, force-push, drop tables, or touch shared filesystems
  outside the container without asking. This includes host paths,
  docker volumes, and the Home Assistant state.
- **Never schedule anything that affects physical reality
  unattended.** The nightly battery was removed after one
  operator pushback — it flipped lights in a household with people
  sleeping. Anything that commands real devices runs under manual
  dispatch only.
- **Operator runs nothing. Claude executes everything.** Commits,
  pushes, deploys, SSH to the docker host (`docker-host.local` —
  credentials in `C:\src\SESSION_STATE.md` §"Credentials / Secrets
  State"), `lms.exe` / `ovms.exe` / NSSM service control on AIBox,
  reading container logs, editing remote `services.yaml`, restarting
  containers — all of it. Do NOT hand the operator shell commands
  to run; that defeats the whole point of the agent. Use
  `scripts/_local_deploy.py` (with `MSYS_NO_PATHCONV=1` to avoid
  Git Bash path mangling on the Windows host) for container deploys.
  Use paramiko-via-Python for ad-hoc SSH operations.
- **NSSM safe-form rule.** Never invoke `nssm` or any subcommand with
  insufficient args — `nssm` (no args), `nssm set` (no args),
  `nssm install <name>` (no app), `nssm edit <name>`, and
  `nssm remove <name>` (without trailing `confirm`) **all open a
  blocking GUI dialog** that pins the host until physically
  dismissed. Repeated offence; see
  `feedback_nssm_gui_traps.md` in auto-memory for the full safe-vs-
  unsafe form list. Always pass `<servicename> <parameter>` minimum
  for set/get, `<servicename> <app>` for install, `<servicename>
  confirm` for remove.
- **Verify Windows compat BEFORE recommending tools.** If the host
  OS is Windows, do not recommend Linux-only tools (vLLM, TGI,
  SGLang, mainline Ollama). Check the project's supported-platform
  list before naming it. The "OpenAI compliance + actively
  maintained" filter doesn't matter if the tool can't run on this
  hardware.
- **Production writes need an artifact, not a hunch.** Before any
  write to a running production service config — `ovms_serve.bat`,
  NSSM service params, the live container's `services.yaml` /
  `plugin.json` / `wrapper.mjs` / patched python in `/app/`, GHCR
  image build, anything that shapes how a running daemon serves
  requests — the change must pass at least ONE of:
  1. **Tested on a parallel non-prod port/instance** and verified
     to do what's intended (e.g. spin up a second OVMS on `:11435`
     pointing at the candidate config; confirm both models route
     before swapping into the `:11434` service).
  2. **Backed by an upstream doc citation** that names this exact
     use case (URL + the relevant snippet in scratch notes), not
     just a flag's existence in `--help`.
  3. **Operator-acknowledged with the specific change spelled out**
     before execution. "Set up a small triage model" is not
     acknowledgement of "replace `--source_model` with
     `--config_path` in `ovms_serve.bat`" — restate the actual
     mutation and get sign-off.
  The phrases **"the flag exists", "the syntax parsed", "the log
  says AVAILABLE", "the tool didn't error"** are NOT evidence the
  change does what's intended. They confirm that a thing happened,
  not that the right thing happened. The "always research" rule in
  §3 fires per-mutation, not per-task — a research pass at the
  start of work doesn't cover later sub-steps. See
  `feedback_research_before_prod_writes.md` and
  `feedback_ovms_multi_model_attempt.md` in auto-memory for the
  failure cases that earned this rule.
- **Intel Arc Pro B60 is the only GPU path.** No T4 / NVIDIA / CUDA
  recommendations even though both T4s are physically present in
  the box — operator has explicitly scoped them out. See
  `feedback_no_t4_options.md` in auto-memory.
- **Don't hardcode against current state.** When fixing config
  (URLs, ports, SSL, etc.), the fix must work in BOTH "feature on"
  and "feature off" modes. Example: route internal calls through
  `127.0.0.1:18015` (always plain HTTP loopback) rather than the
  external `0.0.0.0:8015` (SSL-conditional) — the latter breaks
  the moment SSL toggles.
- **Cost discipline** (per `subagent-cost-control` skill): Opus
  orchestrates, Sonnet executes, Haiku scouts. Exploratory
  codebase queries and grep sweeps go to Haiku subagents;
  single-module implementations go to Sonnet; architecture and
  taste stay in main. `/compact` between logical chunks.

---

## 3. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick
  silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

For exploratory / "what should we do about X?" questions, respond
in 2–3 sentences with a recommendation and the main tradeoff.
Present it as something the operator can redirect. Do not
implement without agreement.

**Research before recommending.** When asked which tool / engine /
library to use, hit the actual project pages with `WebSearch` and
`WebFetch`, verify (a) it runs on the operator's actual platform,
(b) it's actively maintained, (c) it satisfies the explicit
requirements. Do not name a tool just because it has the right
buzzwords ("OpenAI compliant", "actively maintained") — the
filters compose; one disqualifying check kills the recommendation.
The vLLM-on-Windows misfire of 2026-05-01 cost an hour because the
Windows-incompatibility check came after the recommendation
instead of before it.

**Be honest about your evidence.** Don't say "documented bug" when
all you have is one local repro. Don't say "this works" when you
haven't actually run it. When you misspeak, retract clearly and
name what you actually have.

---

## 4. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask: "Would a senior engineer say this is overcomplicated?" If
yes, simplify.

---

## 5. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports / variables / functions that YOUR changes made
  unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the
operator's request.

---

## 6. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make
  them pass."
- "Fix the bug" → "Write a test that reproduces it, then make it
  pass."
- "Refactor X" → "Ensure tests pass before and after."

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria
("make it work") require constant clarification.

---

## 7. Deploy workflow

This repo ships via GHCR. The automation is:

1. Commit + push to `main`.
2. `.github/workflows/build.yml` builds and pushes
   `ghcr.io/synssins/glados-docker:latest`.
3. `.github/workflows/tests.yml` runs the pytest suite on the
   same push.
4. `scripts/deploy_ghcr.py` SSHes to the operator's Docker host (env
   GLADOS_SSH_HOST), pulls the
   new image, recreates the container. Credentials in
   `SESSION_STATE.md`.
5. Verify `/health` on port 8015 AND 8052, live-probe affected
   behaviour.

Run deploys as background tasks when you can (60–90 s for image
pull + container recreate) so the main session stays responsive.

---

## 8. Bug-fix pattern (observed heuristics)

From the Phase 8 non-streaming onion — worth repeating:

- **Fix one layer, verify, then look at what's next.** Don't try
  to one-shot a multi-layer bug with a single fix.
- **Empty or sentinel responses are informative.** `"."` vs
  `""` vs `"I don't have access to…"` all mean different things.
  Read the actual bytes, not the interpretation.
- **Logs before guesses.** Container logs show what reached the
  LLM, what the LLM returned, and where the engine hands off.
  Always pull logs for a specific request before forming a
  hypothesis.
- **Architecture can masquerade as a bug.** The autonomy /
  conversation-store cross-talk was a latent architectural issue
  that presented as a bug fix target. Recognising "this is deeper
  than the four-layer onion" saved hours of speculative commits.

These guidelines are working if: fewer unnecessary changes in
diffs, fewer rewrites due to overcomplication, and clarifying
questions come before implementation rather than after mistakes.
