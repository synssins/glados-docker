# WebUI Design Language — Reconciliation & Extension

**Date:** 2026-05-06
**Status:** Audit deliverable. Companion to `.interface-design/system.md`
(authoritative spec) and `docs/ui-polish-audit.md` (2026-04-25 P0/P1/P2 fix list).
This doc identifies what `system.md` is **missing**, what the prior audit
**didn't catch**, and proposes the full vocabulary needed to apply one
unified design language across every WebUI surface.

---

## Why this doc exists

The operator reported that the WebUI "isn't identical between pages — some
have slightly different font sizes" and asked for a "unified design schema"
where changing one thing in the schema potentially updates everything.

A design system already exists (`system.md`, Phase 4, 2026-04-21) and a fix
audit was produced 4 days later (`ui-polish-audit.md`). Both are good. But:

1. `system.md` defines color, border, spacing, radius, and typography tokens —
   it does **not** define a font-size scale, a transition-timing scale, or
   any class-vocabulary policy for replacing inline styles.
2. `ui-polish-audit.md` catalogues 19 specific findings (V1-V7, S1-S7, F1-F6)
   — it does not quantify font-size proliferation, inline-style burden, or
   the parallel design system used by the setup wizard.
3. Five undeclared CSS variables are referenced 50+ times across the codebase
   and produce silent rendering fallthrough — neither prior doc names this.

This doc fills those gaps and proposes the additional tokens and policy
needed for "change one thing, update everything" to actually work.

---

## Part 1 — Reconciliation: what's broken in the existing system

### R1. Phantom CSS variables (NEW finding — not in prior audit)

**Severity:** Correctness bug + consistency violation.
**system.md says:** `--text` is "Alias for `--fg-primary` (legacy)" and
`--border: #3a3a42` is "kept for legacy call sites."
**Reality:** Neither alias is declared in `style.css`'s `:root` (lines 8-85).

Variables referenced in code but never defined anywhere:

| Variable | References | Files | Should map to |
|---|---|---|---|
| `--text` | 5+ | `system.py`, `ui.js` | `--fg-primary` |
| `--text-dim` | 20+ | `system.py`, `users_page.py`, `training.py`, `integrations.py`, `memory.py`, `ui.js` | `--fg-secondary` or `--fg-tertiary` |
| `--text-muted` | (per prior audit) | various | `--fg-muted` |
| `--border` | 7+ | `system.py`, `users_page.py` | `--border-default` |
| `--accent` | 2 | `style.css:1321` (`.snap-running`), `ui.js:6643` (announcement slider) | Never had a definition; intent unclear |
| `--error` | 2 | `ui.js:6652`, `ui.js:6817` | `--red` |

**Effect:** CSS variables that resolve to nothing fall back to inherited or
initial values per the cascade. `color: var(--text-dim)` on a `<span>` inside
a `<td>` falls back to the `<td>`'s color. `border: 1px solid var(--border)`
falls back to `currentColor`. The result: silent inconsistency where the
operator sees the wrong color, never an error.

**Fix:** two paths — **(a)** declare aliases in `:root` so all the legacy
call sites resolve correctly, then deprecate gradually, or **(b)** sweep
all 50+ call sites to use the v2 names. (a) is the surgical 5-line fix
that clears all symptoms; (b) is the ideal end state. Both should happen,
in that order.

### R2. Duplicate `.btn-danger` rule with conflicting hex (NEW finding)

**Severity:** Specificity bug.
**Location:** `style.css:356-361` and `style.css:1310-1316`.

```css
/* line 356 — original */
.btn-danger {
  background: transparent; color: var(--red);
  border: 1px solid var(--red);
  padding: 0.3rem 0.6rem; font-size: 0.8rem;
}

/* line 1310 — Training-page override, declared globally */
.btn-danger {
  background: #dc2626 !important;
  border-color: #dc2626 !important;
}
```

The second rule is in the Training Monitor section but uses no parent
selector, so it overrides every `.btn-danger` on every page. `#dc2626`
is not in the palette (`--red` is `#e05555`). The `!important` makes
it impossible to override without another `!important` cascade.

**Fix:** scope the second rule to `.train-` ancestor or remove it.

### R3. system.md vs. style.css drift (NEW finding)

**Severity:** Documentation correctness.
**Location:** `system.md:53` says `--bg-input` is `#2e2e35`. `style.css:16`
says `--bg-input: #16161a` (operator-directed 2026-04-25 to match
`--bg-sidebar`).

Anyone reading `system.md` to learn the design system gets the wrong hex
for input wells. Same applies to the legacy-alias claims (R1).

**Fix:** `system.md` needs an update pass to match the current `:root`,
including any tokens added during Phases 5 and 6.

### R4. Parallel design system on setup wizard + standalone TTS (NEW finding — partial mention in prior audit)

**Severity:** Design language fork.
**Location:** `glados/webui/setup/shell.py:11-42` (setup wizard frame),
`glados/webui/pages/tts_standalone.py:16-36` (TTS standalone page).

These two surfaces use **completely different** visual vocabulary from
the rest of the WebUI:

| Property | Main app (style.css) | Setup wizard + standalone TTS |
|---|---|---|
| Page background | `--bg-dark` `#1a1a1e` | `#0a0a0a` (off-palette) |
| Card background | `--bg-card` `#242429` | `#1a1a2e` (off-palette) |
| Input background | `--bg-input` `#16161a` | `#111` (off-palette) |
| Brand orange | `--orange` `#f4a623` | `#ff6600` (off-palette — different orange) |
| Hover orange | `--orange-hover` `#f5b84d` | `#e55a00` (off-palette) |
| Error red | `--red` `#e05555` | `#ff4444` / `#ff6666` (off-palette) |
| Border | `--border-default` rgba | `#333`, `#444`, `#666` raw hex |
| Text | `--fg-secondary/tertiary/muted` | `#aaa`, `#888`, `#999`, `#ccc` raw hex |
| Font | `--font-mono` (JetBrains Mono) | `Segoe UI`, `system-ui`, sans-serif |
| Sizing unit | `rem` (with `--sp-*` scale) | `em` and raw `px` |
| Token usage | CSS variables throughout | Zero CSS variables used |
| Border-radius | `--r-input/card/modal` | Raw `12px`, `6px` |

The login page (`tts_ui.py:LOGIN_PAGE`, called out in prior audit S6) has
the same problem; this finding extends it to setup wizard + standalone TTS.

**Effect:** an operator who sets up GLaDOS sees a visibly different product
during the wizard than after first login — different orange, different bg,
different font. Then if they hit `/tts` (the public TTS endpoint), they see
the wizard's design language again. The main SPA is the only surface using
the canonical system.

**Fix:** the setup wizard, login page, and standalone TTS must consume
`/static/style.css` and use the design tokens. Their inline `<style>` blocks
can shrink to ~10 lines (page-specific layout overrides only).

### R5. `--font-display` declared but actively suppressed by `!important` (prior audit V5, restated for context)

The Phase 4 spec says `--font-display` (Major Mono Display) is "page H1
only, brand usage only." Three CSS rules set `font-family: var(--font-display)`
on titles, then a Phase 5.0 block at `style.css:1256-1267` overrides them
with `var(--font-mono) !important`. This means:

- `--font-display` is **not actually used anywhere in the app** despite being
  loaded from Google Fonts on every page.
- The `<link>` to Major Mono Display in `_shell.py:17` is dead weight.

**Fix:** decide. Either restore `--font-display` to a real role (the sidebar
brand mark, per S4 of the prior audit) or remove the font load. Currently
it's costing one HTTP request per page load for nothing.

---

## Part 2 — What `system.md` is missing

### M1. Font-size scale (PRIMARY operator complaint — quantified)

`system.md` defines tokens for color, border, spacing, radius, and typography
faces. **It does not define a font-size scale.** The operator's complaint
about "slightly different font sizes between pages" is therefore not a
violation of any rule — there are no rules.

Distinct font-sizes counted in `style.css` and inline styles:

```
0.55rem, 0.6rem, 0.65rem, 0.68rem, 0.7rem, 0.72rem, 0.73rem, 0.74rem,
0.75rem, 0.76rem, 0.78rem, 0.8rem, 0.82rem, 0.84rem, 0.85rem, 0.86rem,
0.88rem, 0.9rem, 0.92rem, 0.95rem, 1.0rem, 1.05rem, 1.1rem, 1.3rem,
1.4em, 1.6em, 2.5rem, plus raw px: 11px, 12px, 18px, 22px
```

That's **27 distinct font-size values** in active use. There is no scale,
no semantic naming, no token. Adding/changing a size means hunting through
2,680 lines of CSS plus 503 inline-style attributes.

**Proposed scale (ratio ~1.125, rounded to readable values):**

| Token | Value | Role |
|---|---|---|
| `--fs-2xs` | `0.65rem` (10.4px) | Telemetry strip cells, tag pills, tiny labels |
| `--fs-xs`  | `0.72rem` (11.5px) | Field descriptions, metadata, footnotes |
| `--fs-sm`  | `0.78rem` (12.5px) | Default for forms, table cells, secondary labels |
| `--fs-base` | `0.85rem` (13.6px) | Body text, primary labels, default UI |
| `--fs-md`  | `0.92rem` (14.7px) | Section titles, card headers |
| `--fs-lg`  | `1.05rem` (16.8px) | Page titles |
| `--fs-xl`  | `1.3rem` (20.8px)  | Page H1 (rare) |
| `--fs-display` | `2.0rem` (32px) | Setup wizard / login brand mark only |

This collapses 27 values into 8 semantic slots. Every font-size in CSS or
inline style becomes one of these tokens or stays an exception that needs
justification.

### M2. Transition-timing scale

Distinct transition durations counted: `0.08s`, `0.12s`, `0.15s`, `0.2s`,
`0.25s`, `0.3s`, `0.5s`, `0.7s`, `1s`, `1.5s`. Ten distinct durations,
no scale.

**Proposed scale:**

| Token | Value | Role |
|---|---|---|
| `--t-instant` | `0.08s` | Drag/zoom feedback, near-imperceptible |
| `--t-fast`    | `0.15s` | Hover, focus, button press |
| `--t-medium`  | `0.25s` | Toast, accordion, panel switch |
| `--t-slow`    | `0.5s`  | Loading state changes |

Three durations cover ~90% of cases. Animation loops (spinner, pulse) are
intentionally not on the scale — those are loop durations, not transitions.

### M3. Letter-spacing scale

Distinct letter-spacing values: `0.01em`, `0.02em`, `0.03em`, `0.04em`,
`0.05em`, `0.06em`, `0.08em`, `0.12em`, `0.14em`. Nine values for what
should be ~3 categories.

**Proposed scale:**

| Token | Value | Role |
|---|---|---|
| `--ls-tight` | `0.02em` | Default mono for labels |
| `--ls-mid`   | `0.06em` | Brand text, page-tab labels |
| `--ls-wide`  | `0.12em` | UPPERCASE eyebrow labels (zone-heading, mqtt-subgroup) |

### M4. Z-index scale

Z-index values found: `10`, `50`, `100`, `1000`, `2000`, `9999`. Six values
chosen ad hoc.

**Proposed scale:**

| Token | Value | Role |
|---|---|---|
| `--z-base`    | `1`    | Default flow |
| `--z-dropdown` | `10`   | Account menu, popovers |
| `--z-overlay` | `50`   | Auth overlay |
| `--z-sidebar` | `100`  | Sidebar, topbar |
| `--z-modal`   | `1000` | Modal backdrops |
| `--z-lightbox` | `2000` | Image lightbox |
| `--z-toast`   | `9999` | Toast notifications |

### M5. Inline-style elimination policy

**Quantified burden:** 503 occurrences of `style="..."` attributes across
12 files, plus 351 runtime `.style.*` assignments in `ui.js`. The vast
majority fall into a small number of repeated patterns:

| Pattern | Approx count | Could be replaced by |
|---|---|---|
| `style="display:none;"` (initial-hidden show/hide) | ~80 | `hidden` attribute or `.is-hidden` utility |
| `style="display:flex;..."` (one-off layout) | ~120 | `.row`, `.col`, `.row-between`, `.row-center` utilities |
| `style="margin-top:Xpx;"` / `margin-bottom` | ~60 | `.mt-N`, `.mb-N` utilities mapped to `--sp-*` |
| `style="color:var(--text-dim);"` / `--red` / `--orange` | ~80 | `.txt-dim`, `.txt-danger`, `.txt-accent` utilities |
| `style="font-size:0.85rem;..."` (table cell padding + size) | ~50 | Class on the table itself |
| Reinvented input/button styles via inline | ~40 | Use existing `.cfg-field input`, `.btn-small`, etc. |
| Hardcoded modal dimensions | ~15 | `.modal-box` (already exists in CSS) |

**Policy proposal:**

> Inline `style="..."` is permitted only for: (1) values computed at
> render time (e.g., `width: ${pct}%` for progress bars), (2) initial
> hide-state when `hidden` attribute can't be used, (3) a single ad-hoc
> override that doesn't merit a class. Any inline style with three or
> more declarations, or that duplicates an existing class's styling,
> must be a class.

This is enforceable via a CI grep check (count regression) once the
initial sweep is done.

### M6. The legacy aliases dilemma — declare or purge

`system.md` claims `--text` and `--border` are "kept for legacy call
sites" — but they're not actually declared in CSS. Two ways to honor
the spec:

**Path A — declare the aliases in `:root` (5 lines, 0 risk):**
```css
:root {
  /* Legacy aliases — every site referencing these resolves to the
     v2 token. Targeted for removal in a future sweep. */
  --text: var(--fg-primary);
  --text-dim: var(--fg-secondary);
  --text-muted: var(--fg-muted);
  --border: var(--border-default);
  --error: var(--red);
}
```
This restores rendering correctness immediately.

**Path B — purge legacy references and remove `--accent`:**
Sweep 50+ sites to use v2 tokens, delete legacy aliases entirely.

**Recommendation:** do A first (today, as part of the migration's earliest
chunk), then B as gradual cleanup.

---

## Part 3 — Migration approach options

Three paths. Each builds on the prior. Pick where to stop based on appetite
for blast radius.

### Approach 1 — Minimal: declare missing tokens, no class refactor

**Scope:**
- Add the new tokens (font-size scale M1, transition M2, letter-spacing M3,
  z-index M4) to `:root`
- Add legacy aliases (Path A from R1/M6) to fix phantom variables
- Update `system.md` to match current `:root` (R3) and add new sections
  for the new scales
- Fix duplicate `.btn-danger` (R2) and decide on `--font-display` (R5)

**Touched files:** `style.css` (`:root` block only), `system.md`.

**Effort:** ~1 hour.

**What changes visually:** nothing. All existing values still resolve to
the same hex; no class behavior changes. Phantom-variable rendering bugs
are silently fixed.

**What this enables:** future code can reach for `var(--fs-base)`,
`var(--t-fast)`, etc., and the operator's "change one thing, update
everything" intent works for new code. Existing code is still scattered
but **the schema exists** — engineers (Claude) have a reference.

**Trade-off:** existing inconsistencies remain visible. The operator sees
the same WebUI tomorrow as today.

### Approach 2 — Tokens + utility classes (replace ~70% of inline styles)

**All of Approach 1, plus:**
- Add a utility-class layer to `style.css`: `.row`, `.row-between`,
  `.row-center`, `.col`, `.gap-N`, `.mt-N`, `.mb-N`, `.txt-dim`,
  `.txt-danger`, `.txt-accent`, `.is-hidden`, `.fs-sm`, `.fs-base`,
  `.fs-md` (mapping straight to the new tokens)
- Sweep the page renderers (12 files) replacing the highest-frequency
  inline-style patterns with utility classes
- Per-page CSS for whatever doesn't compress to utilities (table padding
  on `users_page.py`, `system.py` table)

**Touched files:** `style.css` (added utility section), `pages/*.py`
(12 files), `ui.js` (351 `.style.*` calls — most are show/hide that map
to a class toggle).

**Effort:** ~6-10 hours for the sweep. Reviewable in chunks: utilities
first, then one page renderer at a time.

**What changes visually:** most pages stay the same; pages with the most
inline-style hardcoding (`system.py`, `users_page.py`) get tighter,
consistent spacing.

**What this enables:** "change one thing in `:root`, see it ripple" works
for ~70% of the UI. Inline styles are reserved for genuine one-offs.

**Trade-off:** the setup wizard, login page, and standalone TTS still use
their parallel design system (R4).

### Approach 3 — Full sweep: one design language, every page

**All of Approach 2, plus:**
- Setup wizard (`setup/shell.py`), login page (`tts_ui.py:LOGIN_PAGE`),
  and standalone TTS (`pages/tts_standalone.py`) consume `/static/style.css`
  and use design tokens
- Their inline `<style>` blocks shrink to per-page layout overrides only
- Login page rebuild per prior audit S6

**Effort:** Approach 2 plus ~3-5 hours for the three isolated surfaces.

**What changes visually:** the setup wizard and standalone TTS now look
like they belong to the same product as the SPA. Different orange goes
away. Segoe UI → JetBrains Mono.

**What this enables:** truly one design language. The operator's intent
("changing one thing in the design schema would allow for potentially
updating everything") is fully realized.

**Trade-off:** this is the most invasive option. Setup wizard rebranding
risks breaking the first-run experience if not tested in a fresh
container; would need a `/setup` live-probe before declaring done.

---

## Part 4 — Recommendation

**Do Approach 1 immediately, then plan Approach 2 as Slice B.**

Reasoning:

- Approach 1 is one hour of `:root` edits and a doc update. It carries no
  visual risk because no class behavior changes — but it silently fixes
  the phantom-variable rendering bugs (R1) and gives the codebase a
  vocabulary for the next round.
- Approach 2 is the substance of "fix the inconsistencies." It's the right
  scope for a Slice B plan: ~6-10 hours of mostly mechanical sweep work
  reviewable in surgical chunks (one page renderer per commit).
- Approach 3's setup-wizard and login work is genuinely valuable but
  carries the most user-visible risk and warrants its own dedicated round.
  If Approach 2 lands clean and the operator is happy with the SPA's
  uniformity, fold the three isolated surfaces in as a follow-up Slice B.5.

The operator's stated goal — "build the schema and document it, you don't
necessarily have to modify every file yet, but knowing that you have a
reference to go back to would be helpful" — is exactly Approach 1's
deliverable. So that's where to land first.

Approach 2's per-page sweep is the natural follow-on, and is the work
that will actually make the operator's perceived inconsistency go away.

---

## Part 5 — Deliverables this proposal commits to

If the operator approves and asks for a plan, the plan will produce:

**Slice B (this branch — `design-system-v3`):**

- `glados/webui/static/style.css` — `:root` extended with `--fs-*`, `--t-*`,
  `--ls-*`, `--z-*` tokens + legacy aliases (`--text`, `--text-dim`,
  `--text-muted`, `--border`, `--error`)
- `style.css` cleanup: dedup `.btn-danger`, decide on `--font-display`
- `.interface-design/system.md` — updated to match current `:root`, new
  sections for font-size / timing / letter-spacing / z-index scales,
  inline-style policy
- `style.css` utility-class layer (additive only, no existing class touched)
- Sweep of 12 page renderers + `ui.js` to remove the highest-frequency
  inline-style patterns
- Verify visual parity via screenshot diff before / after

**Out of scope this round (deferred to Slice B.5 or later):**

- Setup wizard / login / standalone TTS rebrand (R4) — keeps blast radius
  manageable, gets operator sign-off on SPA result before touching the
  unauthenticated surfaces

**Out of scope entirely (already-tracked items, don't re-bundle):**

- The 19 specific findings in `ui-polish-audit.md` (V1-V7, S1-S7, F1-F6)
  remain on their own track. Some will be naturally resolved by the
  Approach 1/2 work (phantom variables, dedup `.btn-danger`); others are
  page-specific bug fixes that should land in Phase 2 as planned.

---

## Appendix — quick-reference tables

### Existing tokens (from `style.css` `:root` lines 8-85)

```
Color: --bg-dark, --bg-card, --bg-input, --bg-sidebar
       --orange, --orange-hover, --orange-dim
       --red, --red-hover
       --green, --blue
Text:  --fg-primary, --fg-secondary, --fg-tertiary, --fg-muted
Border: --border-subtle, --border-default, --border-strong,
        --border-focus, --border-danger
Spacing: --sp-1 through --sp-8 (4px base unit)
Radius:  --r-sharp (2), --r-input (4), --r-card (6), --r-modal (8)
Fonts:   --font-display, --font-mono, --font-body
Layout:  --sidebar-w, --sidebar-w-collapsed, --topbar-h, --content-max
```

### Proposed additions

```
Font-size: --fs-2xs, --fs-xs, --fs-sm, --fs-base, --fs-md,
           --fs-lg, --fs-xl, --fs-display
Timing:    --t-instant, --t-fast, --t-medium, --t-slow
Spacing:   --ls-tight, --ls-mid, --ls-wide
Z-index:   --z-base, --z-dropdown, --z-overlay, --z-sidebar,
           --z-modal, --z-lightbox, --z-toast
Aliases:   --text → --fg-primary
           --text-dim → --fg-secondary
           --text-muted → --fg-muted
           --border → --border-default
           --error → --red
```

### Files touched per approach

| Approach | `style.css` | `system.md` | `pages/*.py` | `ui.js` | Setup/login surfaces |
|---|---|---|---|---|---|
| 1 (Minimal) | `:root` only | Yes | No | No | No |
| 2 (Tokens + utilities) | `:root` + utility layer | Yes | All 12 | Yes | No |
| 3 (Full sweep) | Same as 2 | Yes | All 12 + setup/standalone | Yes | Yes |
