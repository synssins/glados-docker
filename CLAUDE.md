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
   entry at the bottom (currently Change 22, 2026-04-23).
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

## 1. Project state snapshot (2026-04-23)

- **Deployed on `10.0.0.50`**: `ghcr.io/synssins/glados-docker:latest`
  at commit `dbf40c7`. Healthy.
- **Engine models**: chat, Tier 2 disambiguator, and persona rewriter
  all on `qwen3:14b` via a single Ollama endpoint.
- **Tests**: 1157 passed / 5 skipped. `pytest -q` runs in ~42 s on AIBox.
- **Phase 8.x remediation plan complete.** 8.0 → 8.14 all shipped.
- **Phase Emotion A–I complete** (2026-04-22 / 2026-04-23) —
  deterministic repetition math, semantic clustering, command
  flood detector, hard-rule response directive, PAD→Piper audio
  override, rewriter band overlay, and operator-tunable config
  via Personality → Voice production. See CHANGES.md Change 22.
- **Self-hosted GitHub Actions runner** installed on AIBox
  (`10.0.0.10`) — service
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

### Known open items (not urgent)

- Piper-side phoneme lexicon for context-dependent homographs
  (`live` / `read` / `lead`) — outside this container, Speaches
  scope.
- Quip library content at 156 lines; plan target was ~450.
- Harness scratch dir at `C:\src\glados-test-battery` should
  eventually land in its own git repo.
- TTS pronunciation defaults expansion as operators surface more
  Piper-slurred cases.

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
- **Operator owns deploys.** Claude commits, pushes, and deploys
  via `scripts/deploy_ghcr.py` using the credentials in
  `C:\src\SESSION_STATE.md`. Do not hand the operator shell
  commands when automation exists.
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
4. `scripts/deploy_ghcr.py` SSHes to `10.0.0.50`, pulls the
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
