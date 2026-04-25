# WebUI Polish Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the audit, foundation chunks, and TTS Generator redesign agreed in [the design spec](../specs/2026-04-25-webui-polish-design.md). Per-page sweep for the rest of the pages will be a Phase 2 plan written from audit findings.

**Architecture:** Single-file SPA shell rendered by `tts_ui.py`, sidebar/topbar in `pages/_shell.py`, per-page modules in `pages/*.py` (Python-rendered) plus JS-rendered pages in `static/ui.js`. Design tokens live in `static/style.css`. Login lives in `tts_ui.py` (~line 752). Foundation work is broken into self-contained PRs that deploy + live-verify between commits. Tests for backend (status-dot endpoint, account-cluster wiring) gate via existing pytest suite; visual changes verified via Claude Preview MCP at `:28052` against screenshot fixtures.

**Tech Stack:** Python 3.12 (stdlib `http.server`-based), vanilla JS, CSS custom properties, JetBrains Mono via Google Fonts, Major Mono Display via Google Fonts, pytest 8.x, Claude Preview MCP.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `docs/ui-polish-audit.md` | Audit finding catalog (Task 0 deliverable) | New |
| `glados/webui/static/style.css` | Design tokens, page styles, `.page-shell`, `.telemetry-strip`, `.account-menu` | Modify |
| `glados/webui/pages/_shell.py` | Sidebar brand, status dot, Account cluster | Modify |
| `glados/webui/static/ui.js` | `updateAuthUI()`, status-dot poll, account dropdown JS | Modify |
| `glados/webui/tts_ui.py` | `/login` HTML template (~line 752), `/api/health/aggregate` endpoint | Modify |
| `glados/webui/pages/tts_generator.py` | TTS Generator HTML | Modify |
| `glados/api/server.py` | Pronunciation override application on `/api/generate` | Modify |
| `tests/test_health_aggregate.py` | Status-dot aggregate endpoint tests | New |
| `tests/test_tts_generator_pronunciation.py` | Pronunciation overrides applied on TTS Generator path | New |

---

## Task 0: Run UI polish audit

**Goal:** Produce `docs/ui-polish-audit.md` — a single ranked finding catalog covering visual, structural, and functional issues across the WebUI. Findings drive every subsequent task and the Phase 2 per-page sweep plan.

**Files:**
- Create: `docs/ui-polish-audit.md`

**Acceptance Criteria:**
- [ ] File exists with sections: Visual / Structural / Functional / Per-page summary.
- [ ] Each finding tagged P0 / P1 / P2 with one-line rationale.
- [ ] Each finding tagged with target chunk: `4a` / `4b` / `4c` / `4d` / `4e` / `4f` (Phase 1 foundation) or `Page-<name>` (Phase 2).
- [ ] Visual findings reference `style.css` line numbers; structural findings reference `pages/*.py` and `ui.js` line numbers; functional findings reference the offending route/handler.
- [ ] Layout-shift root cause section explicitly identified (which wrappers are missing, which page widths drift).
- [ ] Sections-with-no-settings inventory enumerates every offender.
- [ ] Committed as `docs: ui polish audit findings`.

**Verify:** `git log -1 docs/ui-polish-audit.md` shows the commit; `wc -l docs/ui-polish-audit.md` returns >100 lines.

**Steps:**

- [ ] **Step 1: Run the visual layer via the interface-design audit skill.** Invoke `interface-design:audit` with `glados/webui/static/style.css` and `glados/webui/pages/` as inputs. Capture raw output.

- [ ] **Step 2: Run the structural layer manually.** For each `pages/*.py`, note:
  - Outer wrapper class(es) and any width/padding declarations.
  - Whether it uses `.tab-content` / `.container` / both / neither.
  - Whether it has a telemetry strip.
  - JS-rendered or Python-rendered (does the body live in `_shell.py` or in `ui.js` `render*Page()`?).
  - Sections that contain only descriptive text, no inputs, no telemetry.

  For `static/ui.js` JS-rendered pages (LLM & Services, Audio & Speakers, Personality, SSL, Raw YAML), note:
  - Function name (`renderLLMServicesPage` etc.) and line range.
  - Wrapper element it produces.
  - Whether it accepts a shared shell or recreates one.

- [ ] **Step 3: Run the functional layer.** Verify:
  - **Persona dropdown wiring on TTS Generator** (`pages/tts_generator.py:43-45`): does it populate from a config endpoint or is it hard-coded? File the gap.
  - **Status-dot data source** (`static/ui.js`, search for `engineStatusDot` and `updateEngineStatus`): which endpoint feeds it, what does "running" actually mean? Identify the gap vs the design spec.
  - **Pronunciation overrides on TTS Generator path** (`glados/api/server.py`, `/api/generate` handler): does it apply the same pronunciation table the engine uses, or a stripped/different path? Trace the call.
  - **Attitude dropdown** (`pages/tts_generator.py:51-54` and `:81-84`): is it wired to anything in `/api/generate`? If unused, it's dead code.
  - **Format default** (`pages/tts_generator.py:46-50`): WAV listed first; switch to MP3 default per spec.

- [ ] **Step 4: Write the audit document.** Use this skeleton:

  ```markdown
  # WebUI Polish Audit — 2026-04-25

  Companion to [docs/superpowers/specs/2026-04-25-webui-polish-design.md].

  ## Summary

  - P0 findings: <count>
  - P1 findings: <count>
  - P2 findings: <count>
  - Layout-shift root cause: <one sentence>

  ## Visual findings

  ### V1 — <title>
  - **Severity:** P0 / P1 / P2
  - **Location:** style.css:NNN-MMM
  - **Issue:** <one paragraph>
  - **Routed to:** chunk 4f / Page-<name>

  ## Structural findings

  ### S1 — Layout-shift root cause
  ...

  ## Functional findings

  ### F1 — TTS Generator persona dropdown not populated
  ...

  ## Per-page summary

  | Page | Telemetry strip | page-shell | Tokens compliant | Sections-no-settings | Other |
  |---|---|---|---|---|---|
  | Chat | ✗ | ✗ | partial | ... | ... |
  | TTS Generator | ✗ | ✗ | partial | none | nailed in Task 7 |
  | System / Status | ✗ | ✗ | yes | ... | ... |
  | ...
  ```

- [ ] **Step 5: Commit.**

  ```bash
  git add docs/ui-polish-audit.md
  git commit -m "docs: ui polish audit findings"
  ```

---

## Task 1: `.page-shell` wrapper foundation

**Goal:** Single CSS container — fixed max-width, padding, grid behavior — that every page renders inside. Migrate all Python-rendered and JS-rendered pages to use it. Layout-shift bug closed at the source.

**Files:**
- Modify: `glados/webui/static/style.css` (add `.page-shell` rules near existing `.container` block)
- Modify: `glados/webui/pages/_shell.py` (line 91 — `<main class="main-content">`)
- Modify: `glados/webui/pages/*.py` (every per-page module — wrap top-level `<div class="tab-content">` in `.page-shell` or fold the wrapper into `_shell.py`'s `<main>`)
- Modify: `glados/webui/static/ui.js` (every JS render function that produces a top-level page wrapper)

**Acceptance Criteria:**
- [ ] `.page-shell` defined in style.css with `max-width: var(--content-max)`, `padding: var(--sp-5) var(--sp-5)`, `margin: 0 auto`, `display: grid`, `gap: var(--sp-4)`.
- [ ] Every page renders inside exactly one `.page-shell`. Verify by `grep -c 'class="page-shell"' glados/webui/pages/*.py` and reading the JS render functions.
- [ ] Switching among Configuration → System / Integrations / Audio & Speakers / Personality / Memory / Logs / SSL / Raw YAML / Users in the live preview produces zero layout shift. Verified manually via Claude Preview MCP and confirmed in DevTools (sidebar `<nav class="sidebar">` `getBoundingClientRect()` width identical between page transitions).
- [ ] No regression in the `tab-content` selector (it still exists; `.page-shell` is its parent).

**Verify:**
```bash
pytest tests/ -k webui -q     # baseline regression
# Visual: load each Configuration sub-page in the preview and inspect for layout shift
```

**Steps:**

- [ ] **Step 1: Add the CSS rule.** In `style.css`, after the existing `.container` declaration, add:

  ```css
  /* ── Page shell — every page renders inside exactly one ───────── */
  .page-shell {
    max-width: var(--content-max, 1440px);
    margin: 0 auto;
    padding: var(--sp-5) var(--sp-5);
    display: grid;
    gap: var(--sp-4);
    box-sizing: border-box;
  }
  @media (max-width: 1024px) {
    .page-shell { padding: var(--sp-4); }
  }
  @media (max-width: 640px) {
    .page-shell { padding: var(--sp-3); }
  }
  ```

- [ ] **Step 2: Wrap pages.** In each `pages/*.py`, change the outer `<div id="tab-..." class="tab-content">` so that `.tab-content` lives inside `.page-shell`:

  Before:
  ```html
  <div id="tab-tts" class="tab-content">
  <div class="container">
    ...
  </div>
  </div>
  ```

  After:
  ```html
  <div id="tab-tts" class="tab-content">
  <div class="page-shell">
    ...
  </div>
  </div>
  ```

  Apply to: `pages/chat.py`, `pages/tts_generator.py`, `pages/system.py`, `pages/integrations.py`, `pages/memory.py`, `pages/logs.py`, `pages/training.py` (if still present), `pages/users.py`, `pages/users_page.py`, `pages/tts_standalone.py`.

- [ ] **Step 3: Update JS-rendered pages.** In `static/ui.js`, find each `render*Page()` function (search for `renderLLMServicesPage`, `renderAudioSpeakersPage`, `renderPersonalityPage`, `renderSSLPage`, `renderRawYamlPage`). Each produces a top-level wrapper. Replace any `<div class="container">` it emits with `<div class="page-shell">`. Where it emits no wrapper, prepend one.

- [ ] **Step 4: Verify visually.** In the dev harness (`tests/dev_webui.py` on `:28052`), open the preview, navigate through every Configuration child, watch the sidebar divider position. It must not shift.

- [ ] **Step 5: Run tests.**

  ```bash
  pytest tests/ -k webui -q
  ```
  Expected: pass (no regressions; we only added a wrapper).

- [ ] **Step 6: Commit.**

  ```bash
  git add glados/webui/static/style.css glados/webui/static/ui.js glados/webui/pages/
  git commit -m "feat(webui): introduce .page-shell wrapper, close layout-shift bug"
  ```

- [ ] **Step 7: Deploy + live-verify.**

  ```bash
  python scripts/deploy_ghcr.py
  ```
  Then on the live host, navigate Configuration sub-pages and verify zero shift.

---

## Task 2: Brand mark font fix (Major Mono Display → JetBrains Mono)

**Goal:** Sidebar "GLaDOS · CONTROL" mark switches to JetBrains Mono. Major Mono Display reserved for page H1 only, per `.interface-design/system.md`.

**Files:**
- Modify: `glados/webui/static/style.css` lines 111–123 (`.sidebar-brand`, `.sidebar-brand span`)
- Modify: `glados/webui/pages/_shell.py` lines 24–28 (sidebar brand markup — restructure as `GLaDOS · CONTROL` single mono mark with the dot still in front)

**Acceptance Criteria:**
- [ ] `.sidebar-brand` uses `font-family: var(--font-mono)`.
- [ ] Letter-spacing tightened (Major Mono Display has wide intrinsic spacing; JetBrains Mono needs less). Use `letter-spacing: 0.06em`.
- [ ] Font-weight 700 to keep brand presence; size 0.85rem.
- [ ] Markup reads `GLaDOS · CONTROL` (the dim "CONTROL" remains, separated by a center-dot rather than line break).
- [ ] Major Mono Display referenced exactly once outside this file: in page H1 styling (verify with `grep -n "var(--font-display)" glados/webui/static/style.css` — should remain on the page-H1 rules around lines 387/646/660; should NOT remain on `.sidebar-brand`).

**Verify:**
```bash
grep -n "var(--font-display)" glados/webui/static/style.css
# Expected: only the page-H1 rules remain; .sidebar-brand is gone from the list.
```

**Steps:**

- [ ] **Step 1: Update CSS.** Replace lines 111–123 in `style.css` with:

  ```css
  .sidebar-brand {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 16px 16px;
    font-family: var(--font-mono);
    font-weight: 700;
    font-size: 0.85rem;
    letter-spacing: 0.06em;
    color: var(--orange);
    border-bottom: 1px solid var(--border-default, rgba(224,224,224,0.10));
  }
  .sidebar-brand .brand-sep { color: var(--fg-muted); padding: 0 2px; }
  .sidebar-brand .brand-dim { color: var(--fg-tertiary); font-weight: 500; }
  ```

- [ ] **Step 2: Update markup.** In `pages/_shell.py` lines 24–28, replace:

  ```html
  <div class="sidebar-brand">
    <span class="engine-status-dot" id="engineStatusDot" title="Engine status"></span>
    <span>GLaDOS</span>
    <span>Control</span>
  </div>
  ```

  with:

  ```html
  <div class="sidebar-brand">
    <span class="engine-status-dot" id="engineStatusDot" title="Engine status"></span>
    <span>GLaDOS</span>
    <span class="brand-sep">·</span>
    <span class="brand-dim">CONTROL</span>
  </div>
  ```

- [ ] **Step 3: Visual check.** Load the dev preview; brand should read `● GLaDOS · CONTROL` in JetBrains Mono with the orange "GLaDOS" and dimmer "CONTROL".

- [ ] **Step 4: Commit.**

  ```bash
  git add glados/webui/static/style.css glados/webui/pages/_shell.py
  git commit -m "feat(webui): sidebar brand mark uses JetBrains Mono not Major Mono Display"
  ```

- [ ] **Step 5: Deploy + live-verify.**

---

## Task 3: Status dot live aggregate

**Goal:** Top-left status dot reflects `/health` aggregate when authenticated, grey when unauthenticated, hidden on `/setup`. Tooltip enumerates per-service health.

**Files:**
- Create: `tests/test_health_aggregate.py`
- Modify: `glados/webui/tts_ui.py` (add `/api/health/aggregate` endpoint near other `/api/auth/*` endpoints)
- Modify: `glados/webui/static/ui.js` (replace `updateEngineStatus` polling logic with aggregate-driven update)
- Modify: `glados/webui/static/style.css` lines 125–133 (`.engine-status-dot` — add `.degraded` and `.unauth` states)

**Acceptance Criteria:**
- [ ] `GET /api/health/aggregate` returns JSON `{"overall": "ok|degraded|down|unauth", "services": [{"name": "API", "status": "ok|degraded|down"}, ...]}`. Auth required for the per-service detail; an unauthenticated request returns `{"overall": "unauth"}` only.
- [ ] Dot CSS classes: `running` (green, all OK), `degraded` (orange, any degraded), `stopping` (red, any down), `unauth` (grey).
- [ ] On `/setup`, the dot is hidden (`display: none`).
- [ ] On `/login`, the dot reflects only what `/health` (public) reports — i.e. WebUI is up; cannot probe upstream services without auth.
- [ ] `updateEngineStatus()` in `ui.js` polls `/api/health/aggregate` every 30 s when authenticated, every 5 min when unauthenticated.
- [ ] Hovering the dot shows a tooltip listing each service and its state.

**Verify:**
```bash
pytest tests/test_health_aggregate.py -v
# Expected: 4+ passing tests covering: unauth response, all-ok, mixed-degraded, all-down, /setup hides dot.
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** Create `tests/test_health_aggregate.py`:

  ```python
  """Tests for /api/health/aggregate — feeds the sidebar status dot."""
  import json
  import pytest
  from glados.webui.tts_ui import _build_health_aggregate

  def test_aggregate_unauth_returns_unauth_only():
      result = _build_health_aggregate(authenticated=False, probes=None)
      assert result == {"overall": "unauth"}

  def test_aggregate_all_ok():
      probes = [
          ("API", True), ("TTS", True), ("STT", True),
          ("HA", True), ("ChromaDB", True),
      ]
      result = _build_health_aggregate(authenticated=True, probes=probes)
      assert result["overall"] == "ok"
      assert len(result["services"]) == 5
      assert all(s["status"] == "ok" for s in result["services"])

  def test_aggregate_one_degraded_overall_degraded():
      probes = [("API", True), ("TTS", "degraded"), ("STT", True)]
      result = _build_health_aggregate(authenticated=True, probes=probes)
      assert result["overall"] == "degraded"

  def test_aggregate_one_down_overall_down():
      probes = [("API", True), ("Vision", False), ("STT", True)]
      result = _build_health_aggregate(authenticated=True, probes=probes)
      assert result["overall"] == "down"

  def test_aggregate_down_dominates_degraded():
      """If any service is down, overall is 'down', not 'degraded'."""
      probes = [("API", "degraded"), ("Vision", False)]
      result = _build_health_aggregate(authenticated=True, probes=probes)
      assert result["overall"] == "down"
  ```

- [ ] **Step 2: Run tests.** `pytest tests/test_health_aggregate.py -v` → expected: ImportError on `_build_health_aggregate`.

- [ ] **Step 3: Implement `_build_health_aggregate`.** In `tts_ui.py`, near other auth helpers:

  ```python
  def _build_health_aggregate(authenticated: bool, probes: list[tuple[str, bool | str]] | None) -> dict:
      """Aggregate per-service probe results into the status-dot payload.

      probes: list of (service_name, status) where status is True (ok),
              False (down), or the literal "degraded".
      """
      if not authenticated or probes is None:
          return {"overall": "unauth"}
      services = []
      any_down = False
      any_degraded = False
      for name, status in probes:
          if status is True:
              s = "ok"
          elif status == "degraded":
              s = "degraded"
              any_degraded = True
          else:
              s = "down"
              any_down = True
          services.append({"name": name, "status": s})
      if any_down:
          overall = "down"
      elif any_degraded:
          overall = "degraded"
      else:
          overall = "ok"
      return {"overall": overall, "services": services}
  ```

- [ ] **Step 4: Run tests.** Expected: PASS.

- [ ] **Step 5: Wire the endpoint.** Find the `/api/auth/status` handler in `tts_ui.py` and add an analogous handler for `/api/health/aggregate` that:
  - Calls existing per-service health checks (the same ones the System → Status page already uses).
  - Passes `authenticated = (current user session is valid)` and the probe list to `_build_health_aggregate`.
  - Returns the dict as JSON.

- [ ] **Step 6: Update `ui.js`.** Replace the existing `updateEngineStatus()` poll with:

  ```javascript
  async function updateEngineStatus() {
    try {
      const r = await fetch('/api/health/aggregate', { credentials: 'same-origin' });
      const data = await r.json();
      const dot = document.getElementById('engineStatusDot');
      if (!dot) return;
      dot.classList.remove('running', 'degraded', 'stopping', 'unauth');
      const cls = ({
        ok: 'running', degraded: 'degraded', down: 'stopping', unauth: 'unauth',
      })[data.overall] || 'unauth';
      dot.classList.add(cls);
      if (data.services) {
        dot.title = data.services.map(s => `${s.name}: ${s.status}`).join('\n');
      } else {
        dot.title = 'Sign in to see service health';
      }
    } catch (e) {
      const dot = document.getElementById('engineStatusDot');
      if (dot) {
        dot.classList.remove('running', 'degraded', 'stopping');
        dot.classList.add('unauth');
        dot.title = 'Service status unknown';
      }
    }
  }
  setInterval(updateEngineStatus, 30000);
  updateEngineStatus();
  ```

- [ ] **Step 7: Add CSS for the new states.** In `style.css`, replace lines 125–133 with:

  ```css
  .engine-status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--fg-muted);
    flex: 0 0 8px;
  }
  .engine-status-dot.running { background: var(--green); }
  .engine-status-dot.degraded { background: var(--orange); }
  .engine-status-dot.stopping { background: var(--red); }
  .engine-status-dot.unauth { background: var(--fg-muted); }
  ```

- [ ] **Step 8: Hide on /setup.** In `pages/_shell.py` (or in `setup/` if it has its own shell), wrap the dot in a Jinja-style or Python condition that omits it when the request path is `/setup`. Easier: in `ui.js`, check `window.location.pathname` — if it starts with `/setup`, set `dot.style.display = 'none'`.

- [ ] **Step 9: Commit.**

  ```bash
  git add tests/test_health_aggregate.py glados/webui/tts_ui.py glados/webui/static/ui.js glados/webui/static/style.css
  git commit -m "feat(webui): live status-dot aggregate, grey unauth, tooltip"
  ```

- [ ] **Step 10: Deploy + live-verify** that the dot reflects real state.

---

## Task 4: Account cluster rebuild

**Goal:** Replace the boxed Account block + separate Sign-in/Sign-out rows with a single sidebar-row-styled username + click-to-open dropdown (Change Password / Sessions / Sign out).

**Files:**
- Modify: `glados/webui/pages/_shell.py` lines 56–73 (`<div class="sidebar-footer">`)
- Modify: `glados/webui/static/style.css` lines 180–227 (sidebar footer / account / signin / logout rules — replace block)
- Modify: `glados/webui/static/ui.js` (`updateAuthUI()`, add `toggleAccountMenu()` and outside-click close handler)

**Acceptance Criteria:**
- [ ] No `<div id="sidebarAccount">` outer "box" wrapper. The username is a plain sidebar-row matching `.nav-item` styling.
- [ ] Purple emoji icon (`&#128100;`) removed.
- [ ] Click on the username row toggles `.account-menu` upward (above the row), with three items: Change Password, Sessions, Sign out.
- [ ] Sign out moves into the dropdown (no separate `id="sidebarLogout"` row).
- [ ] Sign in (unauth state) remains a single sidebar-row, not a button.
- [ ] Outside-click closes the menu.
- [ ] `updateAuthUI()` toggles between "username row" (auth) and "Sign in row" (unauth) without leaving stale wrappers.
- [ ] Visual: user row matches the affordance of the nav rows above it (same padding rhythm, same hover treatment).

**Verify:**
```bash
# No automated tests — UI behavior verified via the preview MCP:
# 1. Auth as admin, click username → dropdown opens upward with 3 items.
# 2. Click outside → menu closes.
# 3. Click "Sign out" inside dropdown → logged out, Sign in row visible.
```

**Steps:**

- [ ] **Step 1: Replace markup.** In `_shell.py` lines 56–73, replace the entire `<div class="sidebar-footer">` block with:

  ```html
  <div class="sidebar-footer">
    <!-- Auth state: account row (renders when signed in) -->
    <div id="sidebarAccount" class="sidebar-account-row" style="display:none;">
      <a class="nav-item account-trigger" onclick="toggleAccountMenu(event)">
        <span class="account-name">
          <span id="sidebarUsername"></span>
          <span class="account-sep">·</span>
          <span id="sidebarRole" class="account-role"></span>
        </span>
        <span class="account-chev">▴</span>
      </a>
      <div class="account-menu" id="accountMenu" hidden>
        <a class="account-menu-item" onclick="navigateTo('config.system');showPageTab('system','account');closeAccountMenu();return false;">Change Password</a>
        <a class="account-menu-item" onclick="navigateTo('config.system');showPageTab('system','account');closeAccountMenu();return false;">Sessions</a>
        <a class="account-menu-item account-menu-danger" href="/logout">Sign out</a>
      </div>
    </div>
    <!-- Auth state: sign-in row (renders when signed out) -->
    <a id="sidebarSignIn" class="nav-item" href="/login">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
      Sign in
    </a>
  </div>
  ```

- [ ] **Step 2: Replace CSS.** In `style.css` lines 180–227, replace the entire block with:

  ```css
  .sidebar-footer {
    margin-top: auto;
    padding: var(--sp-2) var(--sp-2);
    border-top: 1px solid var(--border-default, rgba(224,224,224,0.10));
    position: relative;
  }
  .sidebar-account-row { position: relative; }
  .account-trigger {
    display: flex !important;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-2);
    cursor: pointer;
  }
  .account-name {
    color: var(--fg-primary);
    font-family: var(--font-mono);
    font-size: 0.78rem;
    letter-spacing: 0.04em;
  }
  .account-sep { color: var(--fg-muted); padding: 0 var(--sp-1); }
  .account-role { color: var(--fg-tertiary); }
  .account-chev { color: var(--fg-muted); font-size: 0.7rem; }
  .account-menu {
    position: absolute;
    bottom: calc(100% + 4px);
    left: var(--sp-2);
    right: var(--sp-2);
    background: var(--bg-dark);
    border: 1px solid var(--border-default, rgba(224,224,224,0.10));
    border-radius: var(--r-input, 4px);
    overflow: hidden;
    z-index: 10;
  }
  .account-menu[hidden] { display: none; }
  .account-menu-item {
    display: block;
    padding: var(--sp-2) var(--sp-3);
    color: var(--fg-secondary);
    font-family: var(--font-mono);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-decoration: none;
    border-bottom: 1px solid var(--border-subtle, rgba(224,224,224,0.06));
    cursor: pointer;
  }
  .account-menu-item:last-child { border-bottom: none; }
  .account-menu-item:hover {
    background: rgba(255,255,255,0.03);
    color: var(--fg-primary);
  }
  .account-menu-danger { color: var(--red); }
  .account-menu-danger:hover { color: var(--red); background: rgba(224,85,85,0.06); }
  ```

- [ ] **Step 3: Add JS handlers.** In `ui.js`, add near other UI helpers:

  ```javascript
  function toggleAccountMenu(ev) {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    const m = document.getElementById('accountMenu');
    if (!m) return;
    if (m.hasAttribute('hidden')) {
      m.removeAttribute('hidden');
      setTimeout(() => document.addEventListener('click', closeAccountMenuOnOutside), 0);
    } else {
      closeAccountMenu();
    }
  }
  function closeAccountMenu() {
    const m = document.getElementById('accountMenu');
    if (m) m.setAttribute('hidden', '');
    document.removeEventListener('click', closeAccountMenuOnOutside);
  }
  function closeAccountMenuOnOutside(ev) {
    const m = document.getElementById('accountMenu');
    const t = document.querySelector('.account-trigger');
    if (m && !m.contains(ev.target) && t && !t.contains(ev.target)) closeAccountMenu();
  }
  ```

- [ ] **Step 4: Update `updateAuthUI()`** to toggle the new IDs only (`sidebarAccount` and `sidebarSignIn`); remove any code referencing `sidebarLogout`. Keep the existing `sidebarUsername` / `sidebarRole` text writes — they still exist inside the new row.

- [ ] **Step 5: Visual verify** in the preview: auth state shows username row + dropdown; unauth state shows Sign in row.

- [ ] **Step 6: Commit.**

  ```bash
  git add glados/webui/pages/_shell.py glados/webui/static/style.css glados/webui/static/ui.js
  git commit -m "feat(webui): rebuild account cluster as plain row + dropdown"
  ```

- [ ] **Step 7: Deploy + live-verify.**

---

## Task 5: Login page rebuild

**Goal:** Replace the bootstrap-styled login (white inputs, blue button) with the design-system treatment: bare canvas, telemetry strip with public `/health` per-service rollup, JetBrains Mono labels, `--bg-input` fields, single warning-light-styled orange button.

**Files:**
- Modify: `glados/webui/tts_ui.py` (the `LOGIN_HTML` string template, ~line 752)
- Modify: `glados/webui/tts_ui.py` (the `/health` route — ensure it returns per-service status accessible without auth, OR add a new public `/api/health/public` returning `{"services": [{"name":"API","status":"ok"}, ...]}` for the login telemetry strip)

**Acceptance Criteria:**
- [ ] Login renders on `--bg-dark` canvas with no card wrapper.
- [ ] Brand mark "GLaDOS · CONTROL" at top, JetBrains Mono.
- [ ] Telemetry strip beneath the brand: `API ● │ TTS ● │ STT ● │ HA ●` showing live status (probed by an unauthenticated route).
- [ ] Inputs use `--bg-input` and `--border-default`.
- [ ] Stay-signed-in checkbox is a custom monospace label, not a system checkbox.
- [ ] Submit button uses outline orange (warning-light treatment from `.interface-design/system.md`), not solid.
- [ ] No `font-family` declarations outside the design tokens; no white-on-blue.
- [ ] On invalid credentials, error message uses `--red`, monospace caps `INVALID CREDENTIALS`.

**Verify:**
```bash
pytest tests/ -k login -q       # baseline
# Visual: navigate to /login on the live preview, compare to mockup option B.
```

**Steps:**

- [ ] **Step 1: Add public health rollup endpoint.** In `tts_ui.py`, near `/health`, add a new route `/api/health/public` returning `{"services": [{"name": "API", "status": "ok"}, ...]}` based on the same probes used by the System → Status page, but without the per-service detail an attacker could harvest — only OK / DOWN booleans. Include API, TTS, STT, HA, ChromaDB.

- [ ] **Step 2: Rewrite `LOGIN_HTML`.** Find the current template (search for `class="login-box"` in `tts_ui.py`). Replace the body with:

  ```html
  <!doctype html>
  <html lang="en">
  <head>
    <meta charset="utf-8">
    <title>GLaDOS — Sign in</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/static/style.css">
  </head>
  <body class="login-body">
    <div class="login-shell">
      <div class="login-brand">GLaDOS <span class="brand-sep">·</span> <span class="brand-dim">CONTROL</span></div>
      <div class="login-sub">Sign-in required</div>
      <div class="login-tele" id="loginTele">…probing</div>
      <form class="login-form" method="post" action="/login">
        <label class="login-label" for="lu">User</label>
        <input class="login-input" id="lu" name="username" autofocus required>
        <label class="login-label" for="lp">Password</label>
        <input class="login-input" id="lp" name="password" type="password" required>
        <label class="login-stay">
          <input type="checkbox" name="stay" id="ls">
          <span class="login-stay-box"></span>
          STAY SIGNED IN
        </label>
        {{LOGIN_ERROR_BLOCK}}
        <button class="login-submit" type="submit">Sign in →</button>
      </form>
    </div>
    <script>
      fetch('/api/health/public').then(r => r.json()).then(d => {
        const el = document.getElementById('loginTele');
        if (!el || !d.services) return;
        el.innerHTML = d.services.map(s =>
          `<span class="lt-svc"><span class="lt-dot lt-${s.status}"></span> ${s.name}</span>`
        ).join('<span class="lt-sep">│</span>');
      }).catch(() => {});
    </script>
  </body>
  </html>
  ```

  Replace `{{LOGIN_ERROR_BLOCK}}` with whatever the existing template uses for error injection — keep the existing flow.

- [ ] **Step 3: Add login CSS to style.css.** Append:

  ```css
  /* ── Login page ──────────────────────────────────────────────── */
  .login-body {
    background: var(--bg-dark);
    color: var(--fg-primary);
    font-family: var(--font-mono);
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .login-shell {
    width: 320px;
    padding: var(--sp-5);
  }
  .login-brand {
    color: var(--orange);
    font-weight: 700;
    font-size: 0.9rem;
    letter-spacing: 0.06em;
  }
  .login-brand .brand-dim { color: var(--fg-tertiary); font-weight: 500; }
  .login-brand .brand-sep { color: var(--fg-muted); padding: 0 4px; }
  .login-sub { color: var(--fg-secondary); font-size: 0.7rem; margin: 2px 0 16px; }
  .login-tele {
    background: rgba(0,0,0,0.25);
    border: 1px solid var(--border-subtle, rgba(224,224,224,0.06));
    border-radius: 2px;
    padding: 6px 10px;
    font-size: 0.6rem;
    color: var(--fg-tertiary);
    letter-spacing: 0.06em;
    margin-bottom: 16px;
  }
  .login-tele .lt-dot {
    display: inline-block;
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--fg-muted);
    margin-right: 4px;
  }
  .login-tele .lt-ok { background: var(--green); }
  .login-tele .lt-down { background: var(--red); }
  .login-tele .lt-sep { color: var(--fg-muted); padding: 0 6px; }
  .login-form { display: flex; flex-direction: column; }
  .login-label {
    color: var(--fg-secondary);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
  }
  .login-input {
    background: var(--bg-input);
    border: 1px solid var(--border-default, rgba(224,224,224,0.10));
    border-radius: var(--r-input, 4px);
    padding: 8px 10px;
    color: var(--fg-primary);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    margin-bottom: 12px;
  }
  .login-input:focus {
    outline: none;
    border-color: var(--border-focus, rgba(244,166,35,0.45));
  }
  .login-stay {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--fg-tertiary);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin: 4px 0 14px;
    cursor: pointer;
  }
  .login-stay input { display: none; }
  .login-stay-box {
    width: 10px; height: 10px;
    border: 1px solid var(--border-default, rgba(224,224,224,0.10));
    background: var(--bg-input);
    border-radius: 1px;
    flex: 0 0 10px;
  }
  .login-stay input:checked + .login-stay-box {
    background: var(--orange);
    border-color: var(--orange);
  }
  .login-submit {
    background: transparent;
    border: 1px solid var(--border-focus, rgba(244,166,35,0.45));
    color: var(--orange);
    padding: 8px 12px;
    font-family: var(--font-mono);
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    border-radius: var(--r-input, 4px);
  }
  .login-submit:hover {
    background: var(--orange);
    color: var(--bg-dark);
  }
  .login-error {
    color: var(--red);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin: -4px 0 12px;
  }
  ```

- [ ] **Step 4: Remove old login CSS.** Find the existing inline `<style>` block in the old `LOGIN_HTML` template (the one defining `.login-box` etc.) and remove it — replaced by the style.css rules above.

- [ ] **Step 5: Visual verify** in the preview at `/login`: matches the operator-approved Variant B mockup.

- [ ] **Step 6: Commit.**

  ```bash
  git add glados/webui/tts_ui.py glados/webui/static/style.css
  git commit -m "feat(webui): login page rebuild — bare canvas + telemetry strip"
  ```

- [ ] **Step 7: Deploy + live-verify.**

---

## Task 6: Token sweep (mechanical, post-audit)

**Goal:** Replace legacy `--text` / `--text-dim` tokens with the four-tier `--fg-*` system; replace solid `#3a3a42` borders with rgba `--border-*` tokens; normalize off-grid spacing values to `--sp-*`. Drop-shadow purges. This is mechanical follow-through on the audit's visual layer.

**Files:**
- Modify: `glados/webui/static/style.css` (broad changes)
- Modify: `glados/webui/static/ui.js` (any inline-styled JS-rendered HTML using legacy tokens)
- Possibly modify: `glados/webui/pages/*.py` (any inline styles)

**Acceptance Criteria:**
- [ ] `grep -nE "var\(--text\b" glados/webui/static/` returns 0 results except in the explicit alias declaration (line 59 of system.md note: `--text` is a legacy alias for `--fg-primary`; keep the alias declaration but stop using `var(--text)` in rules).
- [ ] `grep -nE "#3a3a42" glados/webui/static/style.css` returns 0 results in rule bodies (kept only as the `--border` legacy alias declaration if any).
- [ ] `grep -nE "box-shadow:[^n]" glados/webui/static/style.css` returns 0 results except `box-shadow: none` declarations.
- [ ] Spacing values: every numeric rule in style.css uses `var(--sp-*)`, `0`, `100%`, `auto`, `100vh`, percentages, or em-based values. No `13px`, `21px`, `7px`, etc.
- [ ] Tests still pass.

**Verify:**
```bash
grep -nE "var\(--text\b" glados/webui/static/style.css glados/webui/static/ui.js
grep -nE "#3a3a42" glados/webui/static/style.css
grep -nE "box-shadow:[^n]" glados/webui/static/style.css
pytest tests/ -k webui -q
```

**Steps:**

- [ ] **Step 1: Sweep `--text` / `--text-dim` references.** Run `grep -n "var(--text\b\|--text-dim" glados/webui/static/style.css glados/webui/static/ui.js` to enumerate each occurrence. Decide per-context which `--fg-*` tier replaces it:
  - Default body / values / H1 → `--fg-primary`
  - Labels / section titles → `--fg-secondary`
  - Field descriptions / metadata → `--fg-tertiary`
  - Placeholders / disabled / "off" → `--fg-muted`
  Apply replacements with sed-style edits, one logical group per file.

- [ ] **Step 2: Sweep solid borders.** Replace `border: ... #3a3a42` with `border: ... var(--border-default, rgba(224,224,224,0.10))`. For inner field separators inside a card, use `var(--border-subtle, rgba(224,224,224,0.06))`. For emphasis edges, `var(--border-strong, rgba(224,224,224,0.16))`.

- [ ] **Step 3: Purge drop-shadows.** Find every `box-shadow:` rule that isn't `none`. Replace with the surface-color shift recipe: a 2-3 point lightness shift on the inner surface plus a `--border-default` edge.

- [ ] **Step 4: Normalize spacing.** Find numeric pixel values in style.css that aren't multiples of 4 or aren't already tokenized. Replace with the closest `--sp-*` token.

- [ ] **Step 5: Run tests.** `pytest tests/ -k webui -q` → expected: pass.

- [ ] **Step 6: Visual smoke test.** Cycle through every page in the preview; nothing should look broken. If anything does, the sweep wasn't conservative enough — back off the offending change.

- [ ] **Step 7: Commit.**

  ```bash
  git add glados/webui/static/style.css glados/webui/static/ui.js
  git commit -m "refactor(webui): mechanical token sweep — fg-*/border-*/sp-* only"
  ```

- [ ] **Step 8: Deploy + live-verify.**

---

## Task 7: TTS Generator redesign (operator-approved Variant A)

**Goal:** Apply the operator-approved redesign from the brainstorm: stroke icons (play / download / trash) at 12px, segmented mode pill, persona+format dropdowns removed (locked to GLaDOS / MP3), attitude dropdown removed, pronunciation overrides applied on the synthesis path, telemetry strip with persona/format/Piper status.

**Files:**
- Modify: `glados/webui/pages/tts_generator.py` (full rewrite of HTML)
- Modify: `glados/webui/static/style.css` (add `.tts-seg`, `.ico-btn`, `.tts-list-r`, `.telemetry-strip`)
- Modify: `glados/webui/static/ui.js` (`renderTTSFileList()` — switch action buttons to icon SVGs; `ttsGenerate()` — drop dropdown reads, hardcode `voice="glados"`, `format="mp3"`)
- Modify: `glados/api/server.py` (the `/api/generate` handler — apply pronunciation overrides from `cfg.tts.pronunciation` to the input text before synthesis call)
- Create: `tests/test_tts_generator_pronunciation.py`

**Acceptance Criteria:**
- [ ] No `<select id="voiceSelect">`, no `<select id="formatSelect">`, no `<select id="attitudeSelect">` in `tts_generator.py` (verify with grep).
- [ ] Mode toggle uses `.tts-seg` (segmented pill), not the current `.tts-mode-toggle` two-card layout.
- [ ] Telemetry strip on the page reading `MODE SCRIPT │ PERSONA GLaDOS │ FORMAT MP3 │ PIPER ●`.
- [ ] Generated-file rows are 28-32px tall, action buttons are 22px icon buttons (no text labels), Delete uses `:hover` red treatment (no permanent red fill).
- [ ] `/api/generate` applies pronunciation overrides from config before synthesis. New test confirms the substitution table is honored.
- [ ] Visual verification in preview matches the approved Variant A mockup.

**Verify:**
```bash
pytest tests/test_tts_generator_pronunciation.py -v
grep -nE 'id="(voiceSelect|formatSelect|attitudeSelect)"' glados/webui/pages/tts_generator.py
# Expected: pytest passes; grep returns 0 results.
```

**Steps:**

- [ ] **Step 1: Write failing pronunciation test.** Create `tests/test_tts_generator_pronunciation.py`:

  ```python
  """Pronunciation overrides apply on the TTS Generator synthesis path."""
  from unittest.mock import MagicMock, patch
  from glados.api.server import _apply_pronunciation_overrides

  def test_apply_overrides_substitutes_basic():
      table = {"AI": "Aye Eye", "GLaDOS": "Glah Doss"}
      assert _apply_pronunciation_overrides("Hello AI", table) == "Hello Aye Eye"
      assert _apply_pronunciation_overrides("This is GLaDOS", table) == "This is Glah Doss"

  def test_apply_overrides_word_boundary():
      """Overrides must not match inside other words."""
      table = {"AI": "Aye Eye"}
      assert _apply_pronunciation_overrides("Saint", table) == "Saint"  # 'ai' inside 'Saint' must not match
      assert _apply_pronunciation_overrides("Their AIM is true", table) == "Their AIM is true"  # 'AI' starts AIM but is part of it

  def test_apply_overrides_case_sensitive_match_returns_replacement_as_given():
      table = {"AI": "Aye Eye"}
      assert _apply_pronunciation_overrides("ai is fun", table) == "ai is fun"  # lowercase doesn't match

  def test_empty_table_passthrough():
      assert _apply_pronunciation_overrides("Hello AI", {}) == "Hello AI"
      assert _apply_pronunciation_overrides("Hello AI", None) == "Hello AI"
  ```

- [ ] **Step 2: Run test.** `pytest tests/test_tts_generator_pronunciation.py -v` → expected: ImportError on `_apply_pronunciation_overrides`.

- [ ] **Step 3: Implement `_apply_pronunciation_overrides`.** In `glados/api/server.py`:

  ```python
  import re

  def _apply_pronunciation_overrides(text: str, table: dict[str, str] | None) -> str:
      """Replace exact-match keys with their values, preserving word boundaries."""
      if not table:
          return text
      result = text
      # Sort longest-first so 'GLaDOS' wins over 'GL' if both are keys.
      for src in sorted(table.keys(), key=len, reverse=True):
          pattern = r'\b' + re.escape(src) + r'\b'
          result = re.sub(pattern, table[src], result)
      return result
  ```

- [ ] **Step 4: Wire into `/api/generate`.** Find the existing `/api/generate` handler in `server.py`. Before calling the Speaches synthesis client, replace the input text:

  ```python
  text = _apply_pronunciation_overrides(text, cfg.tts.pronunciation)
  ```

  Where `cfg.tts.pronunciation` is the existing pronunciation table on the config. If the config field doesn't exist yet, add it as a `dict[str, str]` field on `TTSConfig` with default `{}`.

- [ ] **Step 5: Run test.** Expected: PASS.

- [ ] **Step 6: Rewrite `tts_generator.py`.** Replace the entire `HTML` constant with:

  ```python
  HTML = r"""<div id="tab-tts" class="tab-content">
  <div class="page-shell">

    <div class="telemetry-strip" id="ttsTele">
      <span>MODE <b id="ttsModeLabel">SCRIPT</b></span>
      <span class="t-sep">│</span>
      <span>PERSONA <b>GLaDOS</b></span>
      <span class="t-sep">│</span>
      <span>FORMAT <b>MP3</b></span>
      <span class="t-sep">│</span>
      <span>PIPER <span class="t-dot t-dot-ok" id="piperDot"></span></span>
    </div>

    <h1 class="page-h1">TTS Generator</h1>
    <p class="page-sub">Synthesize a clip. Pronunciation overrides from Personality apply automatically.</p>

    <div class="card">
      <div class="section-title">Recording mode</div>
      <div class="tts-seg" id="ttsModeSeg">
        <div class="tts-seg-cell on" data-mode="script" onclick="_ttsSwitchMode('script')">SCRIPT — verbatim</div>
        <div class="tts-seg-cell" data-mode="improv" onclick="_ttsSwitchMode('improv')">IMPROV — paraphrase</div>
      </div>
    </div>

    <div class="card tts-mode-card" id="tts-script-card">
      <div class="section-title">Script — read verbatim</div>
      <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
      <div class="char-count"><span id="charCount">0</span> CHARACTERS</div>
      <div class="tts-actions-row">
        <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate audio</button>
        <div class="status" id="ttsStatus"></div>
      </div>
    </div>

    <div class="card tts-mode-card" id="tts-improv-card" style="display:none;">
      <div class="section-title">Brief her — she drafts, you approve</div>
      <textarea id="improvInstruction" placeholder="e.g. 'call everyone to dinner, snidely' or 'announce a thunderstorm warning in her bored voice'"></textarea>
      <div class="tts-actions-row">
        <button class="btn btn-primary" id="improvDraftBtn" onclick="_ttsImprovDraft()">Draft text</button>
        <div class="status" id="improvStatus"></div>
      </div>
      <div id="improvDraftSection" style="display:none; margin-top:var(--sp-4);">
        <div class="section-title" style="font-size:0.82rem; margin-bottom:var(--sp-2);">She wrote:</div>
        <textarea id="improvDraftedText" placeholder="[draft appears here; edit if needed]"></textarea>
        <div class="tts-actions-row">
          <button class="btn" onclick="_ttsImprovDraft()">Redraft</button>
          <button class="btn btn-primary" id="improvGenerateBtn" onclick="_ttsImprovGenerate()">Generate audio</button>
          <div class="status" id="improvGenStatus"></div>
        </div>
      </div>
    </div>

    <div class="card player-section" id="playerCard">
      <div class="player-label" id="playerLabel"></div>
      <audio id="audioPlayer" controls></audio>
      <div class="tts-save-row" id="ttsSaveRow" style="display:none;">
        <label class="mqtt-label" style="margin-bottom:0;">Save this recording</label>
        <div class="controls" style="flex-wrap:wrap;">
          <select id="ttsSaveCategory" title="Pick a category or create a new one">
            <option value="">— pick category —</option>
          </select>
          <input id="ttsSaveFilename" type="text" placeholder="filename (optional)" autocomplete="off">
          <button class="btn btn-primary" id="ttsSaveBtn" onclick="_ttsSaveToCategory()">Save to library</button>
          <div class="status" id="ttsSaveStatus"></div>
        </div>
        <div class="trait-desc" style="margin-top:var(--sp-2);">
          Creates <code>configs/sounds/&lt;category&gt;/&lt;filename&gt;</code> and registers the file
          in <code>sound_categories.yaml</code>. Pick <em>— new category —</em> to add a fresh category.
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">Generated files</div>
      <div id="fileList"><div class="empty-msg">No files yet.</div></div>
    </div>

  </div>
  </div>
  """
  ```

- [ ] **Step 7: Add CSS for `.tts-seg`, `.telemetry-strip`, `.ico-btn`, `.tts-list-r`.** Append to `style.css`:

  ```css
  /* ── Telemetry strip — signature element ─────────────────────── */
  .telemetry-strip {
    background: rgba(0,0,0,0.25);
    border: 1px solid var(--border-subtle, rgba(224,224,224,0.06));
    border-radius: 2px;
    padding: 6px 10px;
    font-family: var(--font-mono);
    font-size: 0.6rem;
    color: var(--fg-tertiary);
    letter-spacing: 0.06em;
    margin-bottom: var(--sp-3);
    display: flex;
    flex-wrap: wrap;
    gap: 0;
  }
  .telemetry-strip b { color: var(--fg-primary); font-weight: 700; }
  .telemetry-strip .t-sep { color: var(--fg-muted); padding: 0 6px; }
  .telemetry-strip .t-dot {
    display: inline-block;
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--fg-muted);
  }
  .telemetry-strip .t-dot-ok { background: var(--green); }
  .telemetry-strip .t-dot-degraded { background: var(--orange); }
  .telemetry-strip .t-dot-down { background: var(--red); }

  /* ── Segmented mode pill ─────────────────────────────────────── */
  .tts-seg {
    display: inline-flex;
    border: 1px solid var(--border-default, rgba(224,224,224,0.10));
    border-radius: 2px;
    overflow: hidden;
    font-family: var(--font-mono);
    font-size: 0.7rem;
  }
  .tts-seg-cell {
    padding: 5px 12px;
    color: var(--fg-tertiary);
    border-right: 1px solid var(--border-subtle, rgba(224,224,224,0.06));
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .tts-seg-cell:last-child { border-right: none; }
  .tts-seg-cell.on { color: var(--orange); background: rgba(244,166,35,0.06); }

  /* ── Icon action buttons (file list rows) ────────────────────── */
  .ico-btn {
    width: 22px; height: 22px;
    border: 1px solid var(--border-default, rgba(224,224,224,0.10));
    border-radius: 2px;
    background: transparent;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-secondary);
  }
  .ico-btn:hover {
    color: var(--fg-primary);
    border-color: var(--border-strong, rgba(224,224,224,0.16));
  }
  .ico-btn.danger:hover {
    color: var(--red);
    border-color: var(--border-danger, rgba(224,85,85,0.40));
  }
  .ico-btn svg { width: 12px; height: 12px; }
  ```

- [ ] **Step 8: Update `renderTTSFileList()` in `ui.js`.** Replace each `Play / Download / Delete` text-button row with an icon-button row:

  ```javascript
  function _ttsFileRowActions(filename) {
    return `
      <button class="ico-btn" title="Play" onclick="ttsPlay('${filename}')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 3 L13 8 L5 13 Z"/></svg>
      </button>
      <button class="ico-btn" title="Download" onclick="ttsDownload('${filename}')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2 L8 11 M4 7 L8 11 L12 7 M3 14 L13 14"/></svg>
      </button>
      <button class="ico-btn danger" title="Delete" onclick="ttsDelete('${filename}')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5 L13 5 M5 5 L5 13 L11 13 L11 5 M6 3 L10 3 L10 5"/></svg>
      </button>`;
  }
  ```

- [ ] **Step 9: Update `ttsGenerate()` in `ui.js`.** Remove the dropdown reads. Hardcode `voice = "glados"` and `format = "mp3"`. The body of the request changes from reading `voiceSelect.value` / `formatSelect.value` to fixed values.

- [ ] **Step 10: Update `_ttsSwitchMode()` in `ui.js`** to switch the `.tts-seg-cell.on` class, not the legacy `.tts-mode-option.active` class. Also update the `#ttsModeLabel` in the telemetry strip.

- [ ] **Step 11: Run tests.** `pytest tests/test_tts_generator_pronunciation.py -v` → PASS.

- [ ] **Step 12: Visual verify.** Compare to the approved Variant A mockup.

- [ ] **Step 13: Commit.**

  ```bash
  git add tests/test_tts_generator_pronunciation.py glados/api/server.py glados/webui/pages/tts_generator.py glados/webui/static/style.css glados/webui/static/ui.js
  git commit -m "feat(tts): TTS Generator redesign — icons, segmented mode, persona/format locked"
  ```

- [ ] **Step 14: Deploy + live-verify.**

---

## Phase 1 wrap-up

After Task 7:

1. Re-run `interface-design:audit` against the post-foundation `style.css` and confirm the audit findings tagged for Phase 1 are closed. Move any P2 leftovers to `docs/roadmap.md` Technical Debt.
2. Update `docs/CHANGES.md` with a new "Change 25 — WebUI polish phase 1" entry.
3. Update `C:\src\SESSION_STATE.md` 2026-04-25 handoff with the live image SHA and the Phase 2 starting point.
4. Open the Phase 2 plan: `docs/superpowers/plans/2026-04-XX-webui-polish-phase-2.md` covering the per-page sweep, informed by audit findings tagged `Page-<name>`.
