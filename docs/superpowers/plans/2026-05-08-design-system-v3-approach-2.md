# Design System v3 — Approach 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers-extended-cc:subagent-driven-development` (recommended)
> or `superpowers-extended-cc:executing-plans` to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the highest-frequency inline-style patterns across the
WebUI with a utility-class layer that consumes the v3 token vocabulary,
eliminating ~70% of the 503 inline-style attributes (across 12 page
renderers) and a meaningful chunk of the 351 `.style.*` runtime
assignments in `ui.js`.

**Architecture:** Add a utility-class section to `glados/webui/static/style.css`
mapping the most-repeated inline patterns (flex layouts, spacing, text
colors, hide/show, font-size) to v3 tokens. Sweep page renderers
one-at-a-time replacing matching inline styles. Per-page CSS for
patterns that don't compress to utilities (e.g. table cell padding
specific to `system.py`'s session table). Verify visual parity via
docker hot-copy + live-probe at the end of each task.

**Tech Stack:** CSS variables (v3 tokens already in `:root`), utility
classes, vanilla JS (no framework).

**Companion docs:**
- `docs/design-system-reconciliation.md` — full audit + the three-approach decision
- `.interface-design/system.md` — authoritative token vocabulary (now v3)

**Out of scope:**
- Setup wizard / login page / standalone TTS rebrand (Approach 3 work)
- Any class behavior change (color shifts, layout changes) — visual
  parity is the bar
- New design surfaces, new components, new pages
- The 19 specific findings in `docs/ui-polish-audit.md` (V1-V7, S1-S7,
  F1-F6) — those land on their own track

---

## Task 1: Utility-class layer

**Goal:** Add a single utility-class section to `style.css` mapping
the most-repeated inline patterns to v3 tokens. No existing class is
modified; this is purely additive.

**Files:**
- Modify: `glados/webui/static/style.css` (append utility section
  before the final closing of the file, after the existing
  Plugins/Logging/Modal sections)

**Acceptance Criteria:**
- [ ] All utilities below added inside a single `=== Utility classes ===` section header
- [ ] Each declaration uses a v3 token, never a raw value
- [ ] No existing selector touched
- [ ] CSS still parses with no errors when served (`docker exec glados curl -s -I http://127.0.0.1:8015/static/style.css | head -1` → HTTP 200)
- [ ] `pytest --ignore=tests/smoke` still passes (1887/5)

**Verify:** Hot-copy `style.css` to live container, browser hard-refresh
the WebUI, confirm no visual regression on Chat / TTS / System pages.

**Steps:**

- [ ] **Step 1: Append the utility section** to `style.css`. Use this exact block:

```css
/* ════════════════════════════════════════════════════════════════
   === Utility classes (Phase 6, 2026-05-08, Approach 2) ===
   ════════════════════════════════════════════════════════════════
   Single-purpose utilities mapped to v3 tokens. Replaces the most
   repeated inline-style patterns observed in the v3 audit. Pick a
   utility before reaching for inline `style="..."`.

   Naming convention:
     .row, .col           — flex row / column shells
     .row-X, .col-X       — gap variants (X = 1..6 mapping to --sp-N)
     .between, .center,   — alignment modifiers (combine with .row)
     .end, .baseline
     .gap-N               — explicit gap (N = 1..6)
     .mt-N, .mb-N         — margin-top / margin-bottom
     .pt-N, .pb-N         — padding-top / padding-bottom
     .px-N, .py-N         — padding-x / padding-y
     .fs-XX               — font-size (XX matches token name)
     .txt-dim, etc        — text color (matches semantic alias)
     .is-hidden           — display:none (works on any element)
   ============================================================ */

/* Layout shells */
.row { display: flex; align-items: center; }
.col { display: flex; flex-direction: column; }
.row.between { justify-content: space-between; }
.row.center { justify-content: center; }
.row.end { justify-content: flex-end; }
.row.baseline { align-items: baseline; }
.row.start { align-items: flex-start; }
.row.wrap, .col.wrap { flex-wrap: wrap; }

/* Gap utilities — mapped to spacing scale */
.gap-1 { gap: var(--sp-1); }
.gap-2 { gap: var(--sp-2); }
.gap-3 { gap: var(--sp-3); }
.gap-4 { gap: var(--sp-4); }
.gap-5 { gap: var(--sp-5); }
.gap-6 { gap: var(--sp-6); }

/* Margin utilities */
.mt-0 { margin-top: 0; }
.mt-1 { margin-top: var(--sp-1); }
.mt-2 { margin-top: var(--sp-2); }
.mt-3 { margin-top: var(--sp-3); }
.mt-4 { margin-top: var(--sp-4); }
.mt-5 { margin-top: var(--sp-5); }
.mb-0 { margin-bottom: 0; }
.mb-1 { margin-bottom: var(--sp-1); }
.mb-2 { margin-bottom: var(--sp-2); }
.mb-3 { margin-bottom: var(--sp-3); }
.mb-4 { margin-bottom: var(--sp-4); }
.mb-5 { margin-bottom: var(--sp-5); }

/* Padding utilities */
.pt-1 { padding-top: var(--sp-1); }
.pt-2 { padding-top: var(--sp-2); }
.pt-3 { padding-top: var(--sp-3); }
.pb-1 { padding-bottom: var(--sp-1); }
.pb-2 { padding-bottom: var(--sp-2); }
.pb-3 { padding-bottom: var(--sp-3); }
.px-1 { padding-left: var(--sp-1); padding-right: var(--sp-1); }
.px-2 { padding-left: var(--sp-2); padding-right: var(--sp-2); }
.px-3 { padding-left: var(--sp-3); padding-right: var(--sp-3); }
.py-1 { padding-top: var(--sp-1); padding-bottom: var(--sp-1); }
.py-2 { padding-top: var(--sp-2); padding-bottom: var(--sp-2); }
.py-3 { padding-top: var(--sp-3); padding-bottom: var(--sp-3); }

/* Font-size utilities — match the v3 scale token names */
.fs-2xs    { font-size: var(--fs-2xs); }
.fs-xs     { font-size: var(--fs-xs); }
.fs-sm     { font-size: var(--fs-sm); }
.fs-base   { font-size: var(--fs-base); }
.fs-md     { font-size: var(--fs-md); }
.fs-lg     { font-size: var(--fs-lg); }

/* Text-color utilities — semantic */
.txt-primary  { color: var(--fg-primary); }
.txt-dim      { color: var(--fg-secondary); }
.txt-muted    { color: var(--fg-muted); }
.txt-tertiary { color: var(--fg-tertiary); }
.txt-accent   { color: var(--orange); }
.txt-danger   { color: var(--red); }
.txt-ok       { color: var(--green); }
.txt-info     { color: var(--blue); }

/* Visibility */
.is-hidden { display: none !important; }

/* Width helpers (rare cases — most layout should use grid/flex) */
.w-full { width: 100%; }
.w-auto { width: auto; }

/* Common combinations seen 3+ times in the audit */
.row-between { display: flex; justify-content: space-between; align-items: center; }
.row-center  { display: flex; justify-content: center; align-items: center; }
.col-tight   { display: flex; flex-direction: column; gap: var(--sp-1); }
```

- [ ] **Step 2: Verify CSS parses + no regression.**

```bash
# Hot-copy + check 200
GLADOS_SSH_HOST=192.168.1.150 GLADOS_SSH_USER=root \
  GLADOS_SSH_PASSWORD=<from SESSION_STATE.md> \
  python scripts/_hot_copy.py glados/webui/static/style.css

# Confirm tokens are still resolved
python -c "
import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.1.150', username='root', password='<from SESSION_STATE.md>', timeout=10)
_, out, _ = c.exec_command('docker exec glados grep -c \"\\.row-between\" /app/glados/webui/static/style.css')
print('row-between count:', out.read().decode().strip())
"
# Expected: 1
```

- [ ] **Step 3: Commit.**

```bash
git add glados/webui/static/style.css
git commit -m "feat(webui): utility-class layer for v3 design system

Single-purpose utilities mapped to the v3 token vocabulary
(--sp-*, --fs-*, --fg-*). Replaces the most-repeated inline-style
patterns observed in the audit. No existing class is modified;
purely additive. Per-page sweeps land in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Sweep `pages/system.py`

**Goal:** Replace the 53 inline-style attributes in `system.py` with
utility classes from Task 1, plus per-page CSS for the session table
that's specific to the page.

**Files:**
- Modify: `glados/webui/pages/system.py`
- Modify: `glados/webui/static/style.css` (append a small `system.py`
  section if any non-utility CSS is needed — e.g. session-table cell
  padding)

**Acceptance Criteria:**
- [ ] No `style="..."` attributes remain in `system.py` except
      computed-at-render-time values (none expected) or single ad-hoc
      overrides documented inline
- [ ] All phantom variables (`var(--text)`, `var(--text-dim)`,
      `var(--border)`) replaced with v2 names or removed (now that
      aliases work, pick the canonical name)
- [ ] No visual regression on the System page across all four tabs
      (Mode, Hardware, Account, Status)
- [ ] `pytest --ignore=tests/smoke` still passes

**Verify:** Hot-copy `system.py` (no — wait, `system.py` is Python, not
hot-copyable as static. Restart container.) Actually `pages/*.py` IS
hot-copyable per `_hot_copy.py`'s SAFE_PREFIXES (`glados/`). Hot-copy
`pages/system.py` + `static/style.css`, restart container, navigate
through all four System tabs in a browser, screenshot-compare with
the pre-sweep baseline.

**Steps:**

- [ ] **Step 1: Read `pages/system.py` fully** (~330 lines). Catalog
      every inline style by pattern category:
      - hide/show (`display:none`)
      - flex layouts (`display:flex;...`)
      - color overrides (`color:var(--text-dim)`)
      - font-size + padding combos (table cells)
      - reinvented input chrome (the `style="background:var(--bg-input);
        color:var(--text);border:1px solid var(--border);..."` pattern,
        which appears 6+ times)

- [ ] **Step 2: Replace pattern-by-pattern.** Conservative order:
      hide/show first (lowest risk), then flex layouts, then color
      utilities, then table-cell patterns. After each pattern category,
      run pytest + browser-check the page to catch regressions early.

- [ ] **Step 3: For the reinvented input chrome,** add a `.cfg-inline-input`
      class to `style.css`:

```css
.cfg-inline-input {
  background: var(--bg-input);
  color: var(--fg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--r-input);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-base);
  font-family: var(--font-mono);
}
```
      Replace the 6+ inline blocks with `class="cfg-inline-input"`.

- [ ] **Step 4: Address the session-table cell padding.** Currently
      every `<th>` and `<td>` in the session table carries
      `style="padding:5px 8px"` (varies between 4-8px across rows).
      Add to `style.css`:

```css
.system-session-table th,
.system-session-table td { padding: var(--sp-1) var(--sp-2); }
.system-session-table tr { border-top: 1px solid var(--border-default); }
.system-session-table th { color: var(--fg-secondary); text-align: left; }
```
      Set `class="system-session-table"` on the table itself (built in JS
      in `system.py:330` — adjust the JS string).

- [ ] **Step 5: Verify** via hot-copy + browser tour of all four tabs.
      Run pytest. Commit.

```bash
git add glados/webui/pages/system.py glados/webui/static/style.css
git commit -m "refactor(webui): sweep system.py inline styles to utility classes

Replaces 53 inline-style attributes with v3 utility classes.
Adds .cfg-inline-input + .system-session-table per-page CSS
for the patterns that don't compress to utilities.

No visual change. Phantom-var references (--text, --border)
swapped to canonical v2 names.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Sweep `pages/users_page.py`

**Goal:** Replace the 45 inline-style attributes in `users_page.py`,
plus add the missing modal CSS classes (the prior audit's F6 finding —
`.modal-backdrop`, `.modal-box`, `.modal-header`, `.modal-title`,
`.modal-close`, `.form-label` are referenced but never defined).

**Files:**
- Modify: `glados/webui/pages/users_page.py`
- Modify: `glados/webui/static/style.css` (add modal classes —
  `.modal-box` already exists from a different feature; check before
  duplicating)

**Acceptance Criteria:**
- [ ] All 45 inline-style attributes replaced with utility classes or
      per-page classes
- [ ] Hardcoded error-banner colors (`background:#5c1a1a;color:#f8d7da`)
      moved to a `.banner-error` class using `--red` + appropriate alpha
- [ ] Cancel buttons currently styled with `style="background:#555"`
      switched to a `.btn-cancel` class
- [ ] Status spans (`color:#e74c3c` disabled, `color:#2ecc71` active)
      switched to `.txt-danger` / `.txt-ok` from utilities
- [ ] Add Users / Edit / Reset Password modals render with proper
      backdrop + centered box (currently render with browser defaults)
- [ ] No visual regression — screenshot the Users page modal flows
      before and after

**Verify:** Open the Users page, click + Add User, screenshot the modal.
Click Edit on a user, screenshot. Click Reset Password, screenshot.
Compare with pre-sweep screenshots.

**Steps:**

- [ ] **Step 1: Read `users_page.py` fully** (~200 lines).

- [ ] **Step 2: Add modal CSS to style.css** (under a new
      `=== Modal chrome ===` heading). Reuses `.modal-box` if it
      already exists (it was added 2026-04-29 for plugins per
      `style.css:2321`). Add only what's missing:

```css
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  z-index: var(--z-modal);
  display: flex; align-items: center; justify-content: center;
  padding: var(--sp-4);
}
.modal-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--border-default);
}
.modal-title { margin: 0; font-size: var(--fs-md); color: var(--fg-primary); font-weight: 600; }
.modal-close {
  background: none; border: none; cursor: pointer;
  color: var(--fg-secondary); font-size: var(--fs-lg);
  padding: var(--sp-1) var(--sp-2);
}
.modal-close:hover { color: var(--fg-primary); }
.form-label { display: flex; flex-direction: column; gap: var(--sp-1); font-size: var(--fs-sm); color: var(--fg-secondary); }
.banner-error {
  padding: var(--sp-2) var(--sp-3);
  background: rgba(224, 85, 85, 0.15);
  border: 1px solid var(--border-danger);
  border-radius: var(--r-input);
  color: var(--fg-primary);
  font-size: var(--fs-base);
  margin-bottom: var(--sp-2);
}
.btn-cancel {
  background: transparent;
  color: var(--fg-secondary);
  border: 1px solid var(--border-default);
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--r-input);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.btn-cancel:hover { color: var(--fg-primary); border-color: var(--border-strong); }
```

- [ ] **Step 3: Sweep `users_page.py`** following the same pattern-order
      as Task 2: hide/show → flex → color → modal classes.

- [ ] **Step 4: Verify all three modal flows** in browser. Run pytest.

- [ ] **Step 5: Commit.**

---

## Task 4: Sweep `pages/memory.py`

**Goal:** Replace the 24 inline-style attributes in `memory.py`.

**Files:**
- Modify: `glados/webui/pages/memory.py`

**Acceptance Criteria:**
- [ ] All 24 inline-style attributes replaced with utility classes
- [ ] No new per-page CSS needed (memory page already has dedicated
      `.mem-*` classes)
- [ ] Cancel button `style="background:#555"` swapped to `.btn-cancel`
      (added in Task 3)
- [ ] No visual regression on Memory page

**Verify:** Open the Memory page, navigate Long-term facts / Recently
learned / Pending review tabs, exercise + Add Fact / Edit / Reject
flows. Screenshot-compare.

**Steps:**

- [ ] Read `memory.py` (~100 lines).
- [ ] Sweep pattern-by-pattern.
- [ ] Verify in browser.
- [ ] Commit.

---

## Task 5: Sweep mid-volume page renderers (training, integrations, logs, logging_page)

**Goal:** Sweep `training.py` (7 inline styles), `integrations.py` (4),
`logs.py` (3), and `logging_page.py` (6) in a single commit since
each is small and the patterns are identical.

**Files:**
- Modify: `glados/webui/pages/training.py`
- Modify: `glados/webui/pages/integrations.py`
- Modify: `glados/webui/pages/logs.py`
- Modify: `glados/webui/pages/logging_page.py`

**Acceptance Criteria:**
- [ ] All inline-style attributes in these four files replaced with
      utility classes
- [ ] Phantom-var references swapped to canonical names
- [ ] No visual regression on any of the four pages

**Verify:** Hot-copy the four files + restart container, browser-tour
each page (Training tab, Integrations sub-tabs, Logs, Logging).

**Steps:**

- [ ] Read each file (each <100 lines).
- [ ] Sweep — patterns are simple here, mostly `display:none` and
      `color:var(--text-dim)`.
- [ ] Verify in browser.
- [ ] Commit single bundled commit titled
      `refactor(webui): sweep mid-volume pages to utility classes`.

---

## Task 6: Sweep low-volume page renderers (tts_generator, chat)

**Goal:** Sweep `tts_generator.py` (3) and `chat.py` (1). Tiny files,
single bundled commit.

**Files:**
- Modify: `glados/webui/pages/tts_generator.py`
- Modify: `glados/webui/pages/chat.py`

**Acceptance Criteria:**
- [ ] All inline-style attributes replaced
- [ ] No visual regression on TTS Generator or Chat pages

**Verify:** Browser-tour TTS Generator (both Script and Improv modes,
Save-to-library flow) and Chat (camera image render still works).

**Steps:**

- [ ] Sweep + verify + commit.

---

## Task 7: Sweep `ui.js` HTML-string inline styles

**Goal:** Replace inline-style HTML strings inside `innerHTML =` and
template-literal blocks in `ui.js`. There are ~80 of these.

**Files:**
- Modify: `glados/webui/static/ui.js`

**Acceptance Criteria:**
- [ ] All HTML strings using `style="color:var(--fg-secondary);..."` or
      `style="background:var(--bg-input);..."` use utility classes
      instead, OR retain inline style only when the value is computed
      (e.g. `style="width:${pct}%"`)
- [ ] No visual regression on any page that ui.js renders into
      (LLM Services, Audio & Speakers, Personality, SSL, Raw YAML —
      these are JS-rendered config sub-pages)

**Verify:** Browser-tour every JS-rendered page (the entire
`Configuration → *` family). Run pytest.

**Steps:**

- [ ] Grep for all `style="` occurrences in `ui.js`:
      `grep -n 'style="' glados/webui/static/ui.js`
- [ ] Triage each into: replace-with-class / keep-as-computed / remove
- [ ] Sweep in chunks of ~20 changes per commit if needed (browser
      verification between each chunk).

---

## Task 8: Sweep `ui.js` runtime `.style.*` assignments

**Goal:** The 351 `.style.*` runtime assignments. Most are show/hide
(`el.style.display = 'none'/''/'block'/'flex'`) — those should switch
to `el.classList.toggle('is-hidden', cond)`. The remainder are
genuinely runtime-computed (progress bar widths, slider thumb
positions) and should stay.

**Files:**
- Modify: `glados/webui/static/ui.js`

**Acceptance Criteria:**
- [ ] All `el.style.display = ...` swapped to `classList.toggle('is-hidden', ...)`
      where the value is binary
- [ ] All `el.style.color = 'var(--red)'/'var(--fg-secondary)'/...`
      swapped to `classList.toggle('txt-danger', ...)` etc.
- [ ] Genuinely computed values left as-is (progress bars, slider thumb
      positions — these are NOT style-pollution)
- [ ] No regression on any page that uses these runtime updates

**Verify:** Exercise the live status polling (engine dot color
changes), the slider components (HEXACO sliders move and update),
the auth overlay (shown/hidden across pages), the announcement
slider in the Personality tab.

**Steps:**

- [ ] Grep `el.style.display\|\.style\.color\|\.style\.cssText` and
      classify
- [ ] Replace in chunks of ~30 per commit, browser-verify between each
- [ ] Commit per chunk.

---

## Task 9: Final live-probe + screenshot diff

**Goal:** Tour every page in the WebUI, screenshot, compare with
pre-sweep baselines saved before Task 2 began.

**Files:** None (verification-only).

**Acceptance Criteria:**
- [ ] Every page renders identically (or improves toward design system)
- [ ] No element previously visible is now hidden (or vice versa)
- [ ] Pytest passes (1887/5)
- [ ] Final live-probe via the production URL `https://glados.denofsyn.com`
      after durable image build + deploy

**Verify:**
- [ ] Take screenshot of every page in the SPA
- [ ] Open each in a tab and visually compare with baseline
- [ ] Note any differences in CHANGES.md

**Steps:**

- [ ] **Step 1: Take baseline screenshots** before starting Task 2 (so
      this step is "before" — pre-sweep).
- [ ] **Step 2: Take post-sweep screenshots** after Task 8.
- [ ] **Step 3: Side-by-side comparison.** Any visual diff that's not
      "obviously better alignment with v3 tokens" is a bug — investigate.
- [ ] **Step 4: Write `docs/CHANGES.md` Change 44** documenting the
      sweep, the inline-style burden reduced (count before vs after),
      and any per-page CSS added.

```bash
# Count check
echo "Before:"
git log --format='' main~N -- glados/webui/pages | xargs -L1 grep -c 'style="' 2>/dev/null
echo "After:"
grep -c 'style="' glados/webui/pages/*.py
```

- [ ] **Step 5: Final commit.**

```bash
git add docs/CHANGES.md
git commit -m "docs(changes): record design-system v3 Approach 2 sweep

X% of inline styles eliminated. Y page renderers swept. Z new
utility classes added. Visual parity verified across N pages.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: Merge to main + durable deploy** via the same path as
      Approach 1 (push main → CI builds GHCR image → run
      `scripts/deploy_ghcr.py` → live-probe `glados.denofsyn.com`).

---

## Acceptance criteria — overall slice

- [ ] Inline-style count under `glados/webui/pages/` reduced from 503
      to under 150 (~70% reduction)
- [ ] Inline-style count in `ui.js` `innerHTML` strings reduced from
      ~80 to under 20
- [ ] `el.style.*` runtime assignments reduced from 351 to under 100
      (only computed values remain)
- [ ] All phantom-variable references (`--text`, `--text-dim`,
      `--border`, `--accent`, `--error`) swept to canonical v2 names
      — at which point the legacy aliases in `:root` can be removed
      (defer this removal to a follow-up commit; do NOT remove in this
      slice in case any third-party page renderer is missed)
- [ ] `pytest --ignore=tests/smoke` passes (1887/5)
- [ ] Live-probe confirms no visual regression on `glados.denofsyn.com`
- [ ] `.interface-design/system.md` updated with: usage examples for
      the utility classes; pointer to the per-file sweep evidence

## Acceptance criteria — quality gates

- [ ] Each task is its own commit (per CLAUDE.md: "surgical, reviewable
      chunks")
- [ ] Each commit message names the file(s) swept and the reduction
      delta (`refactor(webui): sweep system.py — 53 → 0 inline styles`)
- [ ] No `!important` introduced
- [ ] No new hex values introduced — every value is a token
- [ ] No new font-size value introduced — every size is a `--fs-*` slot
- [ ] No new transition-timing introduced — every duration is a `--t-*`
      slot

## Risk + mitigation

**Risk: visual regression somewhere subtle.**
Mitigation: hot-copy + browser-tour after each task, not just at the
end. Take baseline screenshots before Task 2 and compare per-page
during the sweep.

**Risk: third-party page renderer (a plugin or external page) breaks
because we removed legacy aliases.**
Mitigation: don't remove aliases in this slice. Approach 2 only sweeps
canonical sites; alias removal is a follow-up after a confidence
period.

**Risk: a `.style.display` JS site needs a value other than 'none' or
empty (e.g. 'block', 'flex', 'grid').**
Mitigation: when a JS site sets a non-binary display value, the right
fix is usually to set the right class on the *parent* and let CSS
handle children's display. Don't blindly toggle `is-hidden` in those
cases — read the surrounding code.

**Risk: time budget overrun.**
Mitigation: the slice is decomposable. After Task 1 (utilities) is
landed, every page sweep is independent. Operator can stop at any
task boundary and the result is still a net improvement.

---

## Estimated effort

| Task | Estimate | Risk |
|---|---|---|
| 1. Utility-class layer | 30 min | Low |
| 2. system.py sweep | 1.5 h | Medium (table + many forms) |
| 3. users_page.py sweep + modal CSS | 1.5 h | Medium (modal flows) |
| 4. memory.py sweep | 45 min | Low |
| 5. Mid-volume pages | 45 min | Low |
| 6. Low-volume pages | 20 min | Low |
| 7. ui.js HTML-string sweep | 1.5 h | Medium |
| 8. ui.js runtime style sweep | 1.5 h | Medium-High (binary vs computed triage) |
| 9. Verification + CHANGES.md | 1 h | Low |
| **Total** | **~9 h** | |

Within the audit's 6–10 h estimate.
