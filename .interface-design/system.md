# GLaDOS WebUI — design system

Captured 2026-04-21 during Phase 4 of the WebUI refactor. Future
sessions that modify `glados/webui/static/style.css` or any of the
`glados/webui/pages/*.py` modules should read this first and keep
their work inside the decisions below. Deviations are fine when the
context genuinely calls for it — but they must be named deliberately,
not slid in by default.

## Direction and feel

- **Product domain:** home-operated AI lab. Single operator running an
  Aperture-Science-flavored GLaDOS persona on top of Home Assistant,
  Ollama, Speaches, and a Docker host. The WebUI is the instrument
  panel that configures, observes, and debugs that stack.
- **Intent:** cold industrial precision with warm warning-light
  accents. Confident, quiet, instrument-panel. Calm under 2am
  debugging and under Saturday-afternoon persona tuning equally.
- **What this is NOT:** a friendly SaaS dashboard, a marketing page,
  or a social product. No pastel gradients, no soft shadows, no
  playful illustrations. No Inter-on-white bootstrap cards.
- **Signature element:** the **telemetry strip** — a condensed
  monospace ribbon across the top of each config page that turns
  the page heading into a live control-panel readout (`SSL │ CERT
  glados.example.com │ EXP 2026-07-16 (86d) │ MODE LE-DNS01 │
  RELOAD 3d ago`). Terminal green when healthy, Aperture orange
  when degraded, red when down. If a new page doesn't have a
  telemetry strip, ask why — it's the thread that ties the
  interface together.

## Depth strategy: **borders-only, whispered**

Committed. No drop shadows anywhere. Elevation expressed by 2-3
point surface lightness shifts and rgba border progression.

**Why:** the product's world is industrial — engraved, welded,
stamped. Papery drop-shadow cards break the metaphor. Borders
carved into dark metal, on the other hand, feel exactly right.

**Squint test:** blur your eyes at any page. Structure should still
be legible, but no line should jump out. Borders must whisper.

## Color palette — preserve verbatim

The operator has explicitly asked to keep the color scheme.
**Do not edit these hex values.** Any tuning happens via the rgba
border layer and spacing, never the palette itself.

| Token | Hex | Role |
|---|---|---|
| `--bg-dark` | `#1a1a1e` | Page canvas |
| `--bg-card` | `#242429` | Card / panel surface |
| `--bg-input` | `#16161a` | Input well — matches `--bg-sidebar` so inputs read as wells (operator-directed 2026-04-25) |
| `--bg-sidebar` | `#16161a` | Sidebar (same hue family, one tick deeper) |
| `--orange` | `#f4a623` | Primary accent — warning-light semantics only, never decoration |
| `--green` | `#4caf50` | Health OK |
| `--red` | `#e05555` | Danger / critical |
| `--blue` | `#4a9eff` | Portal blue, used for informational links |
| `--text` | (alias) | Alias for `--fg-primary`. Declared in `:root` as part of v3 (2026-05-07) so legacy call sites resolve correctly. End state: callers should reach for `--fg-primary` directly. |

**Rule on orange:** single accent. It means "warning light" or
"attention needed" in this product's world — never "button" or
"brand color for its own sake." If orange is everywhere, it stops
meaning anything. Reserve.

## Token architecture v2 (added Phase 4)

### Text hierarchy — four levels, used consistently

- `--fg-primary`   `#e0e0e0` — default text, values, h1
- `--fg-secondary` `#a8a8ad` — labels, section titles, supporting text
- `--fg-tertiary`  `#70707a` — field descriptions, metadata
- `--fg-muted`     `#4a4a52` — placeholder, disabled, "off" state

Using only `--text` + `--text-dim` (the old two-level system)
flattens hierarchy and makes the interface feel uniform-gray.

### Border progression — rgba, not hex

- `--border-subtle`   `rgba(224,224,224,0.06)` — between fields in a group
- `--border-default`  `rgba(224,224,224,0.10)` — card / group edge
- `--border-strong`   `rgba(224,224,224,0.16)` — emphasis
- `--border-focus`    `rgba(244,166,35,0.45)` — focus ring
- `--border-danger`   `rgba(224,85,85,0.40)` — destructive button edge

`--border` is aliased to `--border-default` for legacy call sites
(declared in `:root` 2026-05-07; previously referenced but undeclared,
producing silent fallthrough). New code should reach for the rgba
tokens directly.

### Spacing — base unit 4px, multiples only

`--sp-1` 4, `--sp-2` 8, `--sp-3` 12, `--sp-4` 16, `--sp-5` 24,
`--sp-6` 32, `--sp-7` 48, `--sp-8` 64.

Random values (13px, 21px, 7px) are the clearest sign of no system.

### Radius — sharper is more technical

`--r-sharp` 2, `--r-input` 4, `--r-card` 6, `--r-modal` 8.

This product is an instrument panel. Bias toward the sharper end.
Never round buttons, never round data tables.

## Token architecture v3 (added 2026-05-07)

v2 left several scales implicit. v3 declares them. See
`docs/design-system-reconciliation.md` for the full audit and rationale.

### Font-size scale — 8 semantic slots

- `--fs-2xs`     `0.65rem` — telemetry cells, tag pills
- `--fs-xs`      `0.72rem` — field descriptions, metadata
- `--fs-sm`      `0.78rem` — default form text, table cells
- `--fs-base`    `0.85rem` — body, labels, primary UI
- `--fs-md`      `0.92rem` — section titles, card headers
- `--fs-lg`      `1.05rem` — page titles
- `--fs-xl`      `1.3rem`  — page H1 (rare)
- `--fs-display` `2.0rem`  — setup/login brand mark only

Pre-v3 the codebase used 27 distinct font-sizes. New code picks a slot.
Random sizes (`0.84rem`, `0.86rem`, `0.88rem`) are the clearest sign
of no system.

### Transition timing — three durations cover ~90% of cases

- `--t-instant` `0.08s` — drag/zoom feedback, near-imperceptible
- `--t-fast`    `0.15s` — hover, focus, button press
- `--t-medium`  `0.25s` — toast, accordion, panel switch
- `--t-slow`    `0.5s`  — loading state changes

Animation loops (spinner, pulse) are intentionally not on the scale —
those are loop durations, not transitions.

### Letter-spacing — three semantic slots

- `--ls-tight` `0.02em` — default mono for labels
- `--ls-mid`   `0.06em` — brand text, page-tab labels
- `--ls-wide`  `0.12em` — UPPERCASE eyebrow labels (zone-heading,
                          mqtt-subgroup, etc.)

### Z-index — semantic stack

- `--z-base`     `1`     — default flow
- `--z-dropdown` `10`    — account menu, popovers
- `--z-overlay`  `50`    — auth overlay
- `--z-sidebar`  `100`   — sidebar, topbar
- `--z-modal`    `1000`  — modal backdrops
- `--z-lightbox` `2000`  — image lightbox
- `--z-toast`    `9999`  — toast notifications

### Legacy aliases — declared, scheduled for sweep

The following tokens are aliases declared in `:root` so legacy call
sites resolve correctly. They are **not** the end state — new code
should reach for the v2 names. A future sweep will replace call
sites and remove the aliases.

- `--text`        → `--fg-primary`
- `--text-dim`    → `--fg-secondary`
- `--text-muted`  → `--fg-muted`
- `--border`      → `--border-default`
- `--accent`      → `--orange`
- `--error`       → `--red`

## Inline-style policy

Inline `style="..."` attributes proliferated across the page renderers
and `ui.js` (503 + 351 occurrences as of the v3 audit). The policy:

> Inline `style="..."` is permitted only for: (1) values computed at
> render time (e.g. `width: ${pct}%` for progress bars); (2) initial
> hide-state when the `hidden` attribute can't be used; (3) a single
> ad-hoc override that doesn't merit a class. Any inline style with
> three or more declarations, or that duplicates an existing class's
> styling, must be a class.

Enforcement is by code review for now. A CI grep check (count
regression) belongs to a later sweep.

## Typography

- **Display** (`--font-display`): `Major Mono Display`. Page H1 only,
  brand usage only. Not for labels, not for section titles.
- **Mono** (`--font-mono`): `JetBrains Mono` (400, 500, 700). EVERY
  label, value, readout, number, audit cell, telemetry cell. This
  inverts the usual "monospace = code only" expectation: in a
  control panel, the instrument reads you, not the other way.
- **Body** (`--font-body`): `Inter` / system-ui. Long-form prose
  only. Current use: the Personality → Preprompt editor. Don't reach
  for this in form labels.

Don't introduce new typefaces without naming the reason. Three
typefaces is the ceiling for this interface.

## Responsive breakpoints

Four tiers, documented in the `@media` section of `style.css`:

- `>1600px` — wide desktop. Content capped at `--content-max` (1440px)
  and margin-right: auto so lines don't stretch past a comfortable
  reading measure.
- `1024-1600px` — standard desktop. Sidebar 220px, full nav labels.
- `640-1024px` — tablet. Sidebar collapses to a 64px icon rail.
  `--sidebar-w` is rebound to `--sidebar-w-collapsed`.
- `<640px` — mobile. Sidebar hidden, topbar appears with hamburger.
  Grids collapse to 1 column. Telemetry strip wraps.

Never introduce a fifth breakpoint without a concrete reason.
Before adding one, try to express the intent through min()/max()/
clamp() and the existing token system first.

## File layout (post-refactor)

```
glados/webui/
├── tts_ui.py                # thin router + Python handlers
├── static/
│   ├── style.css            # design tokens + all page styles
│   └── ui.js                # navigation, form serialization, fetch plumbing
└── pages/
    ├── __init__.py
    ├── _shell.py            # head, sidebar, main open/close, tail
    ├── chat.py              # live chat pane
    ├── tts_generator.py     # TTS file generator
    ├── system.py            # System tab (still oversized; Phase 5 target)
    ├── integrations.py      # HA / MQTT / Disambiguation
    ├── memory.py            # ChromaDB facts / review queue
    ├── training.py          # training monitor
    └── logs.py              # log tail
```

Each `pages/*.py` module exports an `HTML` string constant holding
exactly its `<div class="tab-content">` block. Composition into the
final `HTML_PAGE` happens in `tts_ui.py` with string concatenation
around `_shell.SHELL_TOP` + `_shell.SHELL_BOTTOM`. JS-rendered pages
(LLM & Services, Audio & Speakers, Personality, SSL, Raw YAML) still
live in `ui.js` as JS render functions — extracting those into
per-page JS modules is future work, not Phase 4 scope.

## What to avoid

- Harsh `#3a3a42` solid borders. Use `--border-default` rgba.
- Drop shadows on any surface. If a thing needs lift, use a surface
  color shift + a border.
- `font-family: system-ui` on numeric data. Reach for `--font-mono`.
- Orange used as decoration (brand hover, card header stripe, etc).
  Orange is a warning-light. Scalars don't warn.
- New hex values outside the preserved palette. Every new color is
  a token or it doesn't exist.
- Multiple competing accent colors. The palette already has green /
  red / blue for state. Don't invent purple or teal.
- Media queries outside the four tiers above.

## Future phases

Phase 5 (scheduled) will consolidate page information architecture —
System page split into sub-sections, Disambiguation grouped into
Matching / Aliases / Verification, Tuning labels rewritten with
context. When it lands, add a Phase 5 section to this document
capturing the sub-section patterns so the next round of work
reuses them instead of reinventing.
