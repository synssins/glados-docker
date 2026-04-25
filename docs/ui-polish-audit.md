# WebUI Polish Audit — 2026-04-25

Companion to `.interface-design/system.md` (authoritative design system).
Drives the Phase 1 implementation plan at
`docs/superpowers/plans/2026-04-25-webui-polish-phase-1.md`
and informs the audit-driven Phase 2 per-page sweep plan to come.

---

## Summary

- **P0 findings: 7** — directly address operator-reported gripes
- **P1 findings: 6** — systemic violations that produce "feels inconsistent" perception
- **P2 findings: 6** — single-instance or minor compliance gaps
- **Layout-shift root cause:** `.main-content` has `max-width: 1200px` but no
  `margin-right: auto`, so at standard desktop widths the content area expands
  to fill the remaining viewport; JS-rendered config pages inject their own
  `page-header` + `page-tabs` chrome with fixed padding that differs from
  Python-rendered pages that use a bare `.container` + `.card` stack, causing
  the visible right edge of the content area to shift a few pixels between
  sub-pages.

---

## Visual findings

### V1 — Legacy two-level text tokens still dominant throughout stylesheet

- **Severity:** P1
- **Location:** `style.css:95, 123, 127, 140, 149, 199, 203, 206-207, 222-223,
  302-303, 313, 352, 370, 377, 385, 392-394, 428, 443, 457, 481, 508, 523,
  555, 559-560, 580-581, 605, 613-614, 619, 629, 650, 655, 683, 688, 699,
  724, 732, 747, 752, 759, 796, 803, 808, 813, 823, 885, 902, 934`
- **Issue:** The design system defines a four-tier text hierarchy
  (`--fg-primary`, `--fg-secondary`, `--fg-tertiary`, `--fg-muted`). The
  Phase 4 token architecture added these tokens and updated the telemetry
  strip, trait rows, MQTT, and speakers components to use them. However, the
  older, higher-surface-area CSS blocks — body default text, sidebar nav
  items, cards, inputs, table cells, memory page, weather/GPU components,
  config styles — still use the legacy `--text` / `--text-dim` / `--text-muted`
  variables. The result is a split system: new components whisper correctly,
  old components shout, and the interface reads as unevenly dense. The legacy
  aliases resolve to the same hex values as `--fg-primary` / `--fg-secondary`
  / `--fg-tertiary`, so this is a consistency violation, not a rendering break.
- **Routed to:** chunk `4f` (token sweep, Task 6)

### V2 — Legacy solid `--border` (`#3a3a42`) still used as default card/input border

- **Severity:** P1
- **Location:** `style.css:103, 118, 185, 188, 233, 298-299, 311, 371, 374,
  479, 504, 516-517, 559, 611-612, 620, 630, 641, 667, 695, 698, 731, 765,
  974, 977`
- **Issue:** The design system explicitly states: "The existing `--border:
  #3a3a42` is kept for legacy call sites but new code should reach for the
  rgba tokens." The sidebar border, nav separators, sidebar footer, card
  borders (in the old `.card` rule at line 290), inputs, table cell separators,
  memory page, config styles, and numerous other components still use the
  solid hex border. This makes older surfaces feel harder and more prominent
  than the newer components (telemetry strip, trait rows, MQTT fields) that
  use the rgba progression. The `.card` rule at line 1756-1758 partially
  overrides this with `--border-default`, but only at the end of the file via
  a second rule — the original `.card` at line 290 still declares `--border`.
- **Routed to:** chunk `4f` (token sweep, Task 6)

### V3 — Multiple `box-shadow` declarations violate the borders-only depth strategy

- **Severity:** P0
- **Location:** `style.css:496-497` (mic recording pulse animation),
  `style.css:1044` (toast), `style.css:1137` (training dot glow),
  `style.css:1428` (page-save-btn hover)
- **Issue:** The design system commits to "no drop shadows anywhere" and
  "elevation expressed by 2-3 point surface lightness shifts and rgba border
  progression." Four active `box-shadow` uses survive: the microphone button's
  recording pulse animation, the toast notification component, the training
  monitor dot glow, and the page-save-btn focus ring. The toast is the most
  visible — it pops up with a `0 4px 14px rgba(0,0,0,0.35)` shadow that looks
  papery against the otherwise flat surfaces. The page-save-btn uses a focus
  ring in `box-shadow` form rather than a border — this is directly named as
  a violation in the design system doc. The pulse and glow are animated, so
  they create visible movement that clashes with the "calm under 2am
  debugging" intent.
- **Routed to:** chunk `4f` (token sweep, Task 6)

### V4 — `font-family: 'Consolas', monospace` hardcoded in three cfg-field blocks

- **Severity:** P1
- **Location:** `style.css:825` (`.cfg-field input, .cfg-field select`),
  `style.css:869` (`.cfg-textarea`), `style.css:979` (`.att-table .tag-cell`),
  `style.css:982` (`.att-table .tts-cell`)
- **Issue:** The design system's mono face is `--font-mono` (`JetBrains Mono`
  with IBM Plex Mono / Consolas as fallbacks). Four CSS blocks hardcode
  `'Consolas', monospace` directly, bypassing the token. If the operator ever
  swaps the mono font token, these four blocks will not update. The `.cfg-field`
  input rule is particularly high-surface: it controls every input field rendered
  by the JS config form builder (`cfgBuildForm`), which is the majority of the
  Configuration sub-pages.
- **Routed to:** chunk `4f` (token sweep, Task 6)

### V5 — `.section-title` uses `--font-display` at line 387, overridden by !important at line 1265

- **Severity:** P2
- **Location:** `style.css:387-393` (original rule), `style.css:1256-1267`
  (Phase 5.0 override)
- **Issue:** The original `.section-title` rule at line 387 sets
  `font-family: var(--font-display)` — Major Mono Display. The Phase 5.0
  block at lines 1256-1267 overrides this with `var(--font-mono) !important`
  to fix the "weird font" regression the operator flagged. The source rule is
  never dead (it still sets color and margin-bottom properties that the
  override does not reset), but the `font-family` is always masked. This is
  technical debt: the original rule should be updated to use `--font-mono`
  directly, and the `!important` hack removed. The same issue exists for
  `.cfg-section-title` (line 646) and `.cfg-subsection-title` (line 660) which
  both declare `font-family: var(--font-display)` but are unconditionally
  overridden by the Phase 5.0 block.
- **Routed to:** chunk `4f` (token sweep, Task 6)

### V6 — Off-palette hex colors in weather/GPU component block

- **Severity:** P2
- **Location:** `style.css:579` (`#1a1a2e` — weather-item background),
  `style.css:584` (`#1a1a2e` — gpu-card background), `style.css:591`
  (`#333` — gpu-bar-bg), `style.css:603` (`#ff9800` — hot bar fill),
  `style.css:604` (`#f44336` — critical bar fill)
- **Issue:** `#1a1a2e` is not in the preserved palette (closest is `#1a1a1e`
  — `--bg-dark`). `#ff9800` and `#f44336` are not in the palette either;
  the system has `--red: #e05555` for critical states and `--orange: #f4a623`
  for warnings. The GPU bar colors are an isolated block and not directly
  visible on a default operator workflow, but they introduce two undeclared
  color values. The logs viewport uses `#0a0a0a` (line 764) and `#ccc` (line
  776) which are similarly off-palette (closest: `--bg-dark: #1a1a1e` and
  `--fg-primary: #e0e0e0`). The cfg components also use `#222`, `#333`, `#444`,
  `#111`, `#0d0d0d` (lines 787, 820, 829, 865) — not in the palette.
- **Routed to:** `Page-System` for GPU/weather; chunk `4f` for cfg-block
  cleanup

### V7 — `body` default `font-family` set to `'Segoe UI', system-ui` (not a design token)

- **Severity:** P2
- **Location:** `style.css:93`
- **Issue:** `body` has `font-family: 'Segoe UI', system-ui, -apple-system,
  sans-serif` — this is `--font-body` content but set as a raw value rather
  than `var(--font-body)`. For the body baseline this is defensively correct
  (prose fallback), but it means form inputs using `font-family: inherit`
  (e.g. the textarea at line 305) will inherit the proportional body face
  rather than `--font-mono`. Since all inputs should use mono for the
  instrument-panel aesthetic, the body default should be `var(--font-mono)`,
  with `--font-body` explicitly applied only to prose contexts (the preprompt
  editor). This also explains why the operator reports "hard-to-read font
  choices" — the system-ui fallback on Windows renders as Segoe UI, which
  is proportional and soft against the dark background.
- **Routed to:** chunk `4f` (token sweep, Task 6)

---

## Structural findings

### S1 — Layout-shift root cause: `.main-content` lacks `margin-right: auto` and pages use inconsistent wrapper structures

- **Severity:** P0
- **Location:** `style.css:267-273` (`.main-content`); `_shell.py:91`;
  `pages/system.py:23-24`, `pages/integrations.py:13-14`,
  `pages/chat.py:13-14`, `pages/memory.py:14-15`, `pages/logs.py:13-14`
- **Issue:** `.main-content` is defined with `margin-left: var(--sidebar-w);
  flex: 1; max-width: 1200px` but no `margin-right: auto`. On standard desktop
  viewports (1440px wide) the content area grows beyond 1200px because `flex: 1`
  overrides `max-width` when no centering is applied. The `@media (min-width:
  1601px)` block at line 1067 correctly adds `margin-right: auto`, but below
  1601px there is no centering — so the content hits the right edge of the
  viewport. More critically, JS-rendered config pages (integrations, audio,
  personality, SSL, LLM services, raw YAML) produce a `page-header` +
  `page-tabs` + `page-tab-panels` structure with additional padding from
  `.page-header` (8px top / 12px bottom, border-bottom). Python-rendered pages
  (system.py, memory.py, logs.py, users_page.py) that still use the raw
  `.container` wrapper produce different vertical rhythm. When the operator
  switches between a JS-rendered sub-page and a Python-rendered sub-page (e.g.
  System → Memory or Users → Integrations), the top boundary of the first card
  shifts by roughly the `page-header` height (~48px), which moves the sidebar
  divider relative to the content.
- **Routed to:** chunk `4a` (page-shell wrapper, Task 1)

### S2 — No `.page-shell` wrapper class exists; the design system calls for one

- **Severity:** P0
- **Location:** `_shell.py:91` (`<main class="main-content">`); design system
  `.interface-design/system.md` — there is no `page-shell` class defined in
  `style.css`
- **Issue:** The design system describes a signature structure where each config
  page begins with a telemetry strip inside a consistent page wrapper. Currently
  `<main class="main-content">` is the only outer wrapper, and individual tab
  panels jump directly to `.container` without a shared inner scaffold. A
  `.page-shell` class that imposes consistent padding, max-width centering, and
  the telemetry-strip slot would eliminate the layout drift when switching
  between sub-pages. Without it, every page can (and does) produce different
  effective widths via inline styles or their own container rules.
- **Routed to:** chunk `4a` (page-shell wrapper, Task 1)

### S3 — Telemetry strip absent from all Python-rendered pages and all JS-rendered pages

- **Severity:** P0
- **Location:** `pages/system.py` (no telemetry strip), `pages/memory.py` (no
  telemetry strip), `pages/logs.py` (no telemetry strip),
  `pages/users_page.py` (no telemetry strip), `pages/training.py` (no
  telemetry strip); JS-rendered pages in `ui.js` (all `cfgRender*` functions
  — none produce a telemetry strip)
- **Issue:** The telemetry strip is described as "the thread that ties the
  interface together" and "if a new page doesn't have a telemetry strip, ask
  why." Zero config pages currently have one. The CSS for `.telemetry-strip`
  exists (lines 1190-1245) and is well-specified. The system.py page has a
  `page-header` with a title and description, but that is not a telemetry
  strip — it lacks the live readout cells and mono-ribbon character. Absence
  of the signature element is the single largest contributor to the operator's
  "inconsistent design language" complaint.
- **Routed to:** chunk `4a` (page-shell wrapper, Task 1); Phase 2 per-page
  sweep will wire live data per page

### S4 — Brand mark in sidebar uses `--font-display` (Major Mono Display) — operator gripe #5

- **Severity:** P0
- **Location:** `style.css:113`, `_shell.py:24-28`
- **Issue:** The operator explicitly reported that the "GLADOS CONTROL" brand
  mark font is too "funky." `.sidebar-brand` sets `font-family:
  var(--font-display)` (Major Mono Display) at line 113. The design system
  says `--font-display` is for "page H1 only, brand usage only," but the
  sidebar brand is the most-always-visible element in the UI and at its current
  rendering weight it is ornate where the instrument-panel feel calls for
  crisp. The fix is to switch `.sidebar-brand` to `--font-mono` at that one
  declaration.
- **Routed to:** chunk `4b` (brand mark font, Task 2)

### S5 — Account/Sign-in/Logout cluster design doesn't match sidebar nav

- **Severity:** P0
- **Location:** `style.css:181-227`, `_shell.py:56-73`
- **Issue:** The operator reported gripe #3 verbatim: "bottom-left Account
  cluster doesn't match sidebar nav." The nav items use `font-size: 0.88rem`,
  left border active indicator, and mono text via inheritance. The account
  block (`.sidebar-account`, `.sidebar-account-link`, `.sidebar-signin-btn`,
  `.sidebar-logout`) uses mixed proportional and arbitrary font sizes (0.82rem,
  0.78rem, 0.72rem, 0.8rem), a box with `border-radius: 6px` and
  `rgba(255,255,255,0.03)` background that looks like a different UI widget,
  and the Sign In button is an orange-filled rectangle using `border-radius:
  5px` (off the `--r-*` token scale) and raw `color: #000` instead of the
  palette's `#1a1a1e`. The cluster should be refactored to match nav-item
  geometry and font sizing.
- **Routed to:** chunk `4d` (account cluster, Task 4)

### S6 — Login page has its own isolated stylesheet, entirely outside the design system

- **Severity:** P0
- **Location:** `tts_ui.py:735-825` (`LOGIN_PAGE`)
- **Issue:** Operator gripe #4: "Login page is bootstrap-styled, doesn't match
  design system." The login page is a fully self-contained HTML document with
  an inline `<style>` block. That block uses: `background: #0a0a0a` (not
  `--bg-dark: #1a1a1e`); `background: #1a1a2e` for the card (not in palette);
  `border-radius: 12px` (not on the `--r-*` scale); `box-shadow: 0 4px 24px
  rgba(0,0,0,0.5)` (prohibited by depth strategy); accent color `#ff6600`
  (not `--orange: #f4a623`); `font-family: 'Segoe UI', system-ui` (should be
  `--font-mono` for labels, `--font-body` for prose). The page shares zero
  tokens with the main SPA. It also uses a non-standard orange `#ff6600`
  rather than the palette's `#f4a623`, so the login accent color is
  visibly wrong to a trained eye.
- **Routed to:** chunk `4e` (login rebuild, Task 5)

### S7 — Sections-with-no-settings inventory (pure descriptive text, no inputs)

- **Severity:** P1
- **Location:** Multiple pages
- **Issue:** Operator gripe #6: "Sections that are pure descriptive text with
  no settings bloat pages." The following sections contain only read-only text
  or descriptive prose with no inputs, no toggles, and no actionable controls:

  | Page | Section | Location |
  |---|---|---|
  | Integrations (`integrations.py:21-37`) | `cfg-section-label` card — renders "Configuration" with the Advanced Settings toggle and a message "Select a section or loading..." | This card always appears; it is not a config section, it is chrome |
  | Memory (`memory.py:23-36`) | "Memory configuration" card — has radio buttons, so it does have inputs; not a pure prose section | OK |
  | Logs (`logs.py:22-25`) | `cfg-section-desc` paragraph — read-only description of the Logs controls; this is a three-line prose block above functional controls, not a bloat section | Borderline — desc is brief |
  | Training (`training.py`) | The Training tab itself — this is monitoring-only; all cards are read-only status displays (Status, Epoch, Loss). No settings exist. The entire tab is sections-without-settings | See `training.py:20-38` |
  | System → Status tab | Service health grid — health dots + restart buttons are actionable, but the tab has no form inputs; it is observation-only | Intentional; acceptable |
  | System → Mode tab | Mode Controls — has toggle inputs; not pure prose | OK |
  | System → Hardware tab | Eye demo, robot nodes — has action buttons but no persistent settings inputs | Borderline |

  The clearest offender is the Integrations wrapper card in `integrations.py`
  lines 21-37: it is a permanent chrome card that adds an "Advanced Settings"
  checkbox and a loading placeholder. On most sub-pages the user never sees
  the underlying form rendered because the config tabs framework takes over.
  The Training tab's entire content is status-only — the training feature was
  disabled in the container (see `_shell.py:54`) but the tab HTML still exists
  in `training.py` and is included in `tts_ui.py`'s HTML_PAGE. It is a
  sections-without-settings page because it has no writable inputs and the
  underlying functionality is unavailable.

- **Routed to:** `Page-Integrations`, `Page-Training` for Phase 2 sweep

---

## Functional findings

### F1 — Status dot polls `/api/status` for a single boolean, not a worst-of aggregate

- **Severity:** P0
- **Location:** `ui.js:104-131` (`pollEngineStatus`), `/api/status` endpoint
- **Issue:** Operator gripe implied by Phase 1 spec: the engine status dot
  should reflect a `/api/health/aggregate` endpoint with worst-of semantics
  across all services. Currently `pollEngineStatus()` calls `/api/status` and
  maps the single `data.running` boolean to green (running) or red (stopping).
  There is no `/api/health/aggregate` endpoint in `tts_ui.py`. The dot has
  only two states when it should have three (running/degraded/down) and the
  health data it displays (running=true/false) does not account for individual
  service failures (TTS down but API up, STT down, etc.). The system has
  individual health dots on the System page that DO reflect individual service
  states — but the sidebar dot, which is always visible, is not wired to them.
- **Routed to:** chunk `4c` (status dot, Task 3)

### F2 — Voice dropdown on TTS Generator populated from `/api/voices` — persona label reads "Voice: GLaDOS" but the endpoint drives it correctly

- **Severity:** P2
- **Location:** `pages/tts_generator.py:43-45` (static fallback option),
  `ui.js:4519-4533` (`loadVoices`)
- **Issue:** The HTML at `tts_generator.py:43-45` has a hard-coded `<option
  value="glados">Voice: GLaDOS</option>` as the initial/fallback option.
  `loadVoices()` in `ui.js` at line 4519 fetches `/api/voices` on page init
  and replaces all options with the live voice list. If the fetch fails (TTS
  service down), the fallback option remains. This means the dropdown IS
  populated from a live endpoint under normal operation, so the operator's
  complaint about "persona dropdown blank" is likely a symptom of the TTS
  service being unreachable rather than a code bug. The UI could improve by
  showing an error state rather than silently leaving the old option. No code
  gap, but a UX feedback gap.
- **Routed to:** `Page-TTS` (Phase 2)

### F3 — Attitude dropdown IS wired to `/api/generate` through TTS params, not dead code

- **Severity:** P2
- **Location:** `pages/tts_generator.py:51-54` and `:81-84`,
  `ui.js:4542-4567` (`loadAttitudes`, `getSelectedTtsParams`),
  `tts_ui.py:2248-2254` (`_generate`)
- **Issue:** The task asked to verify whether the attitude dropdown is dead
  code. It is not. `loadAttitudes()` fetches `/api/attitudes`, populates the
  dropdown with named attitudes from the personality config, and
  `getSelectedTtsParams()` translates the selected attitude into `length_scale`,
  `noise_scale`, `noise_w` parameters that are forwarded to the TTS service via
  `/api/generate` at `tts_ui.py:2250-2254`. The dropdown correctly disables
  for non-GLaDOS voices (`ui.js:4536-4539`). The attitude dropdown is
  functional, not dead. The operator's "attitude dropdown irrelevant on this
  page" gripe is a UX preference — the controls might be better hidden until
  a GLaDOS voice is selected, but they are not broken.
- **Routed to:** `Page-TTS` (Phase 2, UX simplification)

### F4 — WAV listed as first option (default) in format select; spec calls for MP3 default

- **Severity:** P0
- **Location:** `pages/tts_generator.py:46-50` (Script card format select),
  `pages/tts_generator.py:76-80` (Improv card format select)
- **Issue:** Both `<select id="formatSelect">` and `<select
  id="improvFormatSelect">` list WAV first with no `selected` attribute on the
  MP3 option. In HTML, the first option in a `<select>` without a `selected`
  attribute is the default. The operator's Phase 1 spec explicitly calls for
  MP3 as the default format because MP3 files are ~10x smaller and the TTS
  Generator is used to produce library files. The fix is to add `selected` to
  the MP3 `<option>` in both selects.
- **Routed to:** `Page-TTS` (Phase 2 Task 7)

### F5 — `/api/generate` handler bypasses the SpokenText pronunciation layer

- **Severity:** P1
- **Location:** `tts_ui.py:2231-2275` (`_generate`), `glados/api/tts.py:25-46`
  (`_get_converter`)
- **Issue:** The `/api/generate` handler in `tts_ui.py` passes the raw text
  directly to the Speaches TTS service at `tts_ui.py:2250`:
  `tts_payload = {"input": text, ...}`. The pronunciation conversion layer
  (`SpokenTextConverter`) that runs in the container's engine path
  (`glados/api/tts.py:_get_converter`) is NOT called by `_generate`. The
  `glados/api/tts.py:generate_speech` function DOES apply the converter
  (Phase 8.10), but `_generate` in `tts_ui.py` does not call `generate_speech`
  — it calls the Speaches HTTP endpoint directly. This means TTS Generator
  output does NOT apply pronunciation overrides (symbol expansions, word
  expansions from `tts_pronunciation.yaml`), while the engine-driven speech
  path does. Operator-configured pronunciation corrections silently don't apply
  in the TTS Generator, which may explain audio inconsistencies between
  generator output and engine audio for the same text.
- **Routed to:** `Page-TTS` / technical debt (pronunciation consistency is a
  functional correctness bug, not a UI polish issue, but surfaces here because
  the TTS Generator is the primary operator-facing audio tool)

### F6 — Users page modal classes (`modal-backdrop`, `modal-box`, `form-label`) have no CSS definitions

- **Severity:** P1
- **Location:** `pages/users_page.py:51-52, 59, 62, 65, 71` (class names used),
  `style.css` (no `.modal-backdrop`, `.modal-box`, `.modal-header`,
  `.modal-title`, `.modal-close`, `.form-label` rules anywhere in the file)
- **Issue:** The Users page modals (`usersAddModal`, `usersEditModal`,
  `usersResetModal`) use class names `modal-backdrop`, `modal-box`,
  `modal-header`, `modal-title`, `modal-close`, and `form-label`. None of
  these classes are defined in `style.css`. The modals likely render with
  browser defaults — no backdrop dimming, no centered box, no styled header.
  The modals are functional (JS wires them correctly) but are visually
  unstyled. This is a post-auth-rebuild gap: the users page was added in
  Change 23-24 (2026-04-25) and the modal CSS was not added to style.css.
- **Routed to:** `Page-Users` (Phase 2)

---

## Per-page summary

| Page | Telemetry strip | page-shell | Tokens compliant | Sections-no-settings | Other |
|---|---|---|---|---|---|
| Chat (`chat.py`) | No | No (raw `.container`) | Partial (uses `--text`, `--border` legacy) | None — single functional card | Pure JS, no page-header |
| TTS Generator (`tts_generator.py`) | No | No (raw `.container`) | Mostly (new components use v2 tokens) | None | F2 voice fallback; F3 attitude wired; F4 WAV default; F5 pronunciation bypass; P0 for gripes 8 items |
| System (`system.py`) | No | Has `page-header` + `page-tabs` | Partial (`--text`, `--border` legacy in sub-sections) | Status tab (observation-only) | No telemetry strip despite being highest-data page |
| Integrations (`integrations.py`) | No | JS-rendered `page-header` + `page-tabs` | Good (JS render uses v2 tokens in new components) | Chrome card at top (S7) | Config is JS-rendered; layout differs from Python pages |
| Memory (`memory.py`) | No | No (raw `.container`) | Partial (uses `--text`, `--border` legacy; inline style overrides throughout) | None — all cards have inputs | Many inline styles bypass class system |
| Logs (`logs.py`) | No | No (raw `.container`) | Mostly (`--font-mono` used for log body/controls) | None | Single-card page, no settings, observation only |
| Training (`training.py`) | No | No (raw `.container`) | Partial (`--text-dim` legacy, `#22c55e` off-palette) | Entire page (status-only, no inputs) | Feature disabled in container; tab exists but training unavailable |
| Users (`users_page.py`) | No | Has `page-header` (no page-tabs) | Partial (table uses inline styles, modal classes undefined) | None — table is CRUD-functional | F6 missing modal CSS is critical |
| Login (`tts_ui.py:LOGIN_PAGE`) | N/A | Isolated standalone page | None — entirely own stylesheet | N/A | S6: wrong palette, box-shadow, wrong orange `#ff6600` |
| Standalone TTS (`tts_standalone.py`) | N/A | Isolated standalone page | None — own inline stylesheet | N/A | Low operator visibility; same class of issues as login |

---

## Appendix: chunk routing reference

| Chunk | Task | Findings routed here |
|---|---|---|
| `4a` | Task 1 — `.page-shell` wrapper | S1, S2, S3 |
| `4b` | Task 2 — brand mark font | S4 |
| `4c` | Task 3 — status dot | F1 |
| `4d` | Task 4 — account cluster | S5 |
| `4e` | Task 5 — login rebuild | S6 |
| `4f` | Task 6 — token sweep | V1, V2, V3, V4, V5, V6 (partial), V7 |
| `Page-TTS` | Phase 2 | F2, F3, F4, F5; operator gripes 7-8 |
| `Page-System` | Phase 2 | V6 (partial — GPU/weather colors) |
| `Page-Users` | Phase 2 | F6 (missing modal CSS) |
| `Page-Integrations` | Phase 2 | S7 (chrome card) |
| `Page-Training` | Phase 2 | S7 (sections-without-settings entire page) |
