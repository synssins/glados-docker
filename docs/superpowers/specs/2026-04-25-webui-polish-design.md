# WebUI Polish Phase — Design

**Date:** 2026-04-25
**Status:** approved (operator: "Just get started")
**Followups from:** auth rebuild (Change 24), prior WebUI refactor Phase 5
**Memory pointer:** [project_ui_polish_followups.md](../../../../Users/Administrator/.claude/projects/C--src/memory/project_ui_polish_followups.md)
**Design system reference:** [.interface-design/system.md](../../../.interface-design/system.md)

## Scope

**In:**
- Login page rebuild (bare canvas + telemetry strip variant)
- Brand mark font fix (Major Mono Display → JetBrains Mono on the sidebar "GLaDOS · CONTROL" mark)
- Sidebar Account cluster rebuild (plain row + click-to-open dropdown)
- TTS Generator redesign (stroke icons, segmented mode pill, persona+format locked)
- Layout-shift root cause (shared `.page-shell` wrapper)
- Cross-page consistency: token sweep, font readability, sections-with-no-settings cleanup
- Telemetry strip on every Configuration child page
- Status dot accuracy (live `/health` aggregate, grey when unauth)
- JS module extraction **only where needed** to fix layout-shift on JS-rendered pages

**Out (deferred to a future IA phase):**
- System page IA split
- Disambiguation card restructure (Matching/Aliases/Verification grouping)
- Tuning param relabel
- Color palette changes (operator preserved verbatim per design system)
- Any new functional features beyond the bug-class fixes listed above

## Approach: 3 stages

1. **Audit** — single pass producing `docs/ui-polish-audit.md`. Three layers (visual / structural / functional). Each finding tagged P0/P1/P2 and routed to a Foundation chunk or a Sweep page.
2. **Foundation** — one PR per chunk, deploy + live-verify between. Six chunks (4a–4f below).
3. **Per-page sweep** — one PR per page, operator review per page before commit, deploy after each page or small batch.

## Audit deliverable

`docs/ui-polish-audit.md`. Single ranked finding catalog covering:

- **Visual:** invoke `interface-design:audit` skill against `style.css` + `pages/*.py`. Token violations, drop-shadow violations, wrong `--fg-*` tier, missing rgba border layer, off-grid spacing, font-family on numerics.
- **Structural:** missing shared wrappers, per-page max-width drift, sidebar-divider position drift root cause, font-size shifts, JS-vs-Python rendering parity gaps, sections-with-no-settings inventory.
- **Functional:** TTS persona-dropdown wiring, status-dot data source, pronunciation-override routing, attitude-dropdown dead code, format-selector defaults.

## Foundation chunks (one PR each)

| # | Chunk | Deliverable |
|---|---|---|
| 4a | `.page-shell` wrapper | Single CSS container with fixed max-width, padding, grid; every page migrated; layout-shift closed. |
| 4b | Brand mark font | Sidebar "GLaDOS · CONTROL" → JetBrains Mono. Major Mono Display reserved for page H1 only. |
| 4c | Status dot | Grey unauth; worst-of `/health` aggregate when auth (green/orange/red); tooltip enumerates services; hidden on `/setup`. |
| 4d | Account cluster | Plain sidebar row `admin · admin`; click opens upward dropdown (Change Password, Sessions, Sign out). Drop box wrapper, drop purple icon, move Sign-out into dropdown. |
| 4e | Login page | Bare canvas, telemetry strip with `/health` for API/TTS/STT/HA, JetBrains Mono labels, `--bg-input` fields, single orange button (warning-light treatment). |
| 4f | Token sweep | `--text`/`--text-dim` → four-tier `--fg-*`. Solid `#3a3a42` borders → rgba `--border-*`. Off-grid spacing → `--sp-*`. Mechanical PR after audit findings. |

## Per-page sweep

For each page (Chat, TTS Generator, System tabs, Integrations, Audio & Speakers, Personality, Memory, Logs, SSL, Raw YAML, Users):

1. Reconcile against `.interface-design/system.md`.
2. Add telemetry strip if absent.
3. Apply audit findings tagged to that page.
4. Reduce or earn-the-keep on sections-with-no-settings.
5. Operator review before commit. One commit/PR per page (small pages may batch).
6. Deploy after each page or small batch; live-verify.

**TTS Generator** is already nailed down (today's Variant A): stroke icons (play/download/trash) at 12px, segmented mode pill (Script | Improv), no persona dropdown, no format dropdown, no attitude dropdown. Persona locked to GLaDOS, format locked to MP3, both surfaced as state in the telemetry strip. Pronunciation overrides from Personality config apply on the synthesis path.

## Success criteria

- Switching Configuration sub-pages causes zero layout shift in DevTools.
- Sidebar divider, font size, and chrome metrics constant across pages.
- Every page renders through `.page-shell`; every page uses only `--fg-*`/`--border-*`/`--sp-*` tokens for new code.
- Telemetry strip visible on every Configuration child page and the login page.
- Login no longer reads as a stock bootstrap card.
- Account cluster matches sidebar nav rows; Change Password lives in the dropdown.
- TTS Generator matches the approved Variant A mockup; persona/format locked, attitude removed, pronunciation overrides apply.
- `docs/ui-polish-audit.md` committed with all P0/P1 findings closed; P2 logged in `docs/roadmap.md` Technical Debt.

## Confirmed selections (browser visual companion)

- Login layout: **B** — bare canvas + telemetry strip
- Account cluster: **A** — plain row + dropdown (no avatar, Sign-out inside dropdown)
- TTS Generator: **A** — stroke icons + segmented mode pill, with persona+format dropdowns removed (locked)

## Out-of-scope guardrails

- No color palette tuning. Hex values preserved.
- No new typefaces beyond the three documented (Major Mono Display, JetBrains Mono, Inter).
- No drop shadows. Borders-only depth.
- No System IA split, Disambiguation restructure, or Tuning relabel — those go to a future IA phase.
