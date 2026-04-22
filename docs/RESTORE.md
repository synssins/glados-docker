# RESTORE — WebUI Refactor Safety Net

**Baseline commit:** `253ae1f` (docs: refresh all documentation for CLI handoff)
**Baseline tag:** `pre-webui-refactor-20260421`
**Refactor branch:** `webui-refactor`
**Protected branch:** `main` (untouched by the refactor)
**Date captured:** 2026-04-21

## What this file is for

The WebUI refactor splits `glados/webui/tts_ui.py` (11,393 lines, monolithic HTML+CSS+JS+Python) into per-page modules and extracts static assets. If the refactor leaves the working tree broken mid-flight — stale f-string concatenation, missing imports, 500s from the WebUI — use the commands below to get back to a known-good state.

The deployed container on `10.0.0.50` runs `ghcr.io/synssins/glados-docker:latest` which is pinned to the pre-refactor image. **Production is not at risk from local refactor work.** This file is strictly about the working tree.

## Uncommitted files at checkpoint time

These untracked files existed on `main` at the baseline and are NOT tracked by the refactor. They survive any restore:

- `configs/attitudes.json`
- `scripts/deploy_ghcr.py`
- `scripts/inspect_live_tier3.py`
- `scripts/probe_semantic.py`
- `scripts/probe_verification.py`

## Restore recipes

### 1. Working tree is broken — throw away refactor branch entirely

```bash
cd /c/src/glados-container
git checkout main
git branch -D webui-refactor
```

The branch is gone. Repo is back on `main` at `253ae1f`.

### 2. Refactor branch has commits worth keeping but tree is currently broken

Check out the branch at a known-good commit:

```bash
cd /c/src/glados-container
git log --oneline webui-refactor  # find the last good commit
git checkout webui-refactor
git reset --hard <good-commit-sha>
```

Destructive: discards any uncommitted work on the branch.

### 3. Verify the baseline is still intact

```bash
cd /c/src/glados-container
git show pre-webui-refactor-20260421 --stat | head -5
git diff pre-webui-refactor-20260421 main  # should be empty
```

If the diff is non-empty, `main` has moved — stop and investigate before restoring.

### 4. Full nuclear restore from GitHub (last resort)

If the local repo is corrupted and the tag is gone:

```bash
cd /c/src
mv glados-container glados-container.broken
git clone https://github.com/synssins/glados-docker.git glados-container
cd glados-container
git checkout main
```

The untracked files listed above will be lost by this path — copy them out of `glados-container.broken/` before deleting it.

## Reconstructing mid-deployment state

If the refactor landed Phase 1-3 (CSS/JS/page-module extraction) but Phase 4 (design tokens) broke, you can keep the structural work and rebuild just the visual layer:

```bash
git checkout webui-refactor
git log --oneline --grep "Phase"  # find commit boundaries
git reset --hard <last-good-phase-commit>
```

Phase commits will be tagged in their message (e.g. "Phase 1: extract CSS" / "Phase 2: extract JS"). Any phase can be rolled back independently because each is a self-contained commit on the branch.

## What the refactor will change (for diff review)

| Area | Before | After |
|---|---|---|
| `glados/webui/tts_ui.py` | 11,393 lines | Thin router (~200-400 lines) |
| `glados/webui/static/` | (nonexistent) | `style.css`, `ui.js` |
| `glados/webui/pages/` | (nonexistent) | Per-page renderers (`system.py`, `integrations.py`, `ssl.py`, etc.) |
| `glados/webui/components/` | (nonexistent) | Shared primitives (`cfg_form.py`, `telemetry_strip.py`) |
| `.interface-design/system.md` | (nonexistent) | Design-system decisions — depth, tokens, patterns |

No config schema changes. No API contract changes. No test changes expected beyond path updates.

## Test invariant

Baseline: **1069 tests pass / 3 skipped** at `253ae1f`. Any phase that fails to maintain this is a stop-and-restore trigger.

```bash
cd /c/src/glados-container
pytest --tb=short 2>&1 | tail -10
```

Must report `1069 passed, 3 skipped` (or better).
