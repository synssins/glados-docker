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
| `--bg-input` | `#2e2e35` | Input well (darker than surroundings — content receives here) |
| `--bg-sidebar` | `#16161a` | Sidebar (same hue family, one tick deeper) |
| `--orange` | `#f4a623` | Primary accent — warning-light semantics only, never decoration |
| `--green` | `#4caf50` | Health OK |
| `--red` | `#e05555` | Danger / critical |
| `--blue` | `#4a9eff` | Portal blue, used for informational links |
| `--text` | `#e0e0e0` | Alias for `--fg-primary` (legacy) |

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

The existing `--border: #3a3a42` is kept for legacy call sites but
new code should reach for the rgba tokens.

### Spacing — base unit 4px, multiples only

`--sp-1` 4, `--sp-2` 8, `--sp-3` 12, `--sp-4` 16, `--sp-5` 24,
`--sp-6` 32, `--sp-7` 48, `--sp-8` 64.

Random values (13px, 21px, 7px) are the clearest sign of no system.

### Radius — sharper is more technical

`--r-sharp` 2, `--r-input` 4, `--r-card` 6, `--r-modal` 8.

This product is an instrument panel. Bias toward the sharper end.
Never round buttons, never round data tables.

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
