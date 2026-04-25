# WebUI Polish Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** Operator-flagged Phase 2 work — IA reshuffles, layout redesigns, mechanical sweeps, and bug investigations after Phase 1 deploy review.

**Source feedback:** Operator screenshots + commentary from 2026-04-25 sessions, captured in conversation. Phase 1 is live at `e73938b` on the host.

**Branch:** `webui-polish` (continuing).

---

## Hard rule from operator

Audio files NEVER live under `configs/`. Path is `<audio_root>/<Category>/<emotion>/<file>.mp3`. Existing `configs/sounds/<category>/` references in the TTS Generator save-to-library flow and `sound_categories.yaml` are bugs to fix during Phase 2C.

---

## File Structure

| File | Responsibility |
|---|---|
| `glados/webui/static/style.css` | Token-consistency sweep, `.btn` rule consolidation, slider redesign rules, speakers list rule rewrite |
| `glados/webui/static/ui.js` | Render functions for new layouts (HEXACO, Emotion, Speakers flat, Memory icons, Users icons), state-dot panels, cfg-form button sweep |
| `glados/webui/pages/_shell.py` | Sidebar nav: remove Users + SSL entries; demote LLM (it lives in System now) |
| `glados/webui/pages/system.py` | New tabs: SSL, Users, LLM (folded from Integrations); current Services tab redesigned to port-grouped status-only |
| `glados/webui/pages/integrations.py` | Remove LLM card; HA-aliases section gets a notice + link |
| `glados/webui/pages/users_page.py` | Row actions become icons (disable/edit/delete); Reset PW removed |
| `glados/webui/pages/memory.py` | Importance slider; auto-learned facts feed; pencil/trash icons; RAG explainer |
| `glados/webui/tts_ui.py` | Personality page rebuild (HEXACO + Emotion sliders, Behavior tokenized, Floor/Area aliases removed); routing for new tabs |

---

## Chunks

### Chunk 1 — System tab consolidation + button + input sweep (mechanical-heavy)

**Goal:** Sidebar IA tightens to **Chat / TTS Generator / Configuration / Integrations**. SSL, Users, LLM all become tabs under System. All primary action buttons unified to `.btn-primary`. All inputs/selects/textareas use `--bg-sidebar` (`#16161a`) instead of `--bg-input` (`#2e2e35`). Test Harness section removed. Audio tab removed. "Chimes" tab → "Sounds". Reload-from-Disk → primary. "Personality" duplicate header investigated and reduced.

**Files:** `_shell.py`, `system.py`, `integrations.py`, `users_page.py`, `style.css`, `ui.js`.

**Acceptance:**
- Sidebar shows: Chat, TTS Generator, Configuration → (System, Integrations, Audio & Speakers, Personality, Memory, Logs, Raw YAML).
- System page tabs: Status, Mode, Services, Hardware, Maintenance, Account, **SSL** (new), **Users** (new), **LLM** (new).
- Integrations no longer has an LLM sub-section.
- `grep -nE "btn-secondary|btn-grey|background:[^;]*#[ef]" style.css | grep -v audio` finds nothing in sweep targets (audio Clear retained).
- Inputs use `--bg-sidebar`. All `<input>`, `<select>`, `<textarea>` rules in style.css that previously used `--bg-input` now use `--bg-sidebar`.
- Hardware tab has no Test Harness section (HTML + JS handler removed).
- Audio & Speakers has no "Audio" tab.
- `Chimes` tab renamed to `Sounds`.
- `Reload from Disk` button uses `.btn-primary`.
- Tests pass.

### Chunk 2 — Service Endpoints rebuild (port-grouped, status-only)

**Goal:** Replace the URL-input-per-service layout with a port-grouped status panel.

In-container services (TTS, STT, API Wrapper) all live on `:8015`; show as one row: `:8015 — TTS │ STT │ API ●` with green/red status dots per service. No URL inputs.

External services keep their URL fields ONLY when the service is genuinely external from the container's perspective:
- **Vision** (currently `<external_vision_host>:8016`): status-only on `:8016`. If not configured (no URL or unreachable), show as "inactive" rather than "down" — Vision absence does not block functionality.
- **Ollama** (currently in Integrations → LLM): folded into Services tab here. URL field retained, model selector, status dot.

**Files:** `system.py`, `ui.js` (existing services-tab JS, plus new LLM-on-services JS).

**Acceptance:**
- TTS/STT/API Wrapper rendered as port-grouped status row, no URLs.
- Vision: status-only with "inactive" state when unconfigured.
- Ollama: full URL + model + status, folded in.
- Save Services → `.btn-primary`.

### Chunk 3 — Personality page rebuild

**Goal:** Address the "weird" feedback: duplicate Personality header, full-width sliders that don't scale, missing min/max labels, non-editable numerics, wrong color on Behavior sliders, "Loses 50 rank points" gibberish, Floor/Area alias removal.

**Acceptance:**
- One "Personality" header, not two.
- HEXACO traits: each trait card has the description **above** the slider (under the title), min/max polarity labels under the bar ends (e.g., "Unflappable" / "Anxious"), an editable numeric input on the right (0–1, max 2 decimals), and the slider bar capped at a sensible width (`max-width: 320px` or similar) so it doesn't span the viewport.
- Emotion model: same layout, range −1 to 1.
- Behavior tab: red sliders re-styled to use `--orange` for the active fill (warning-light, consistent with the rest of the UI).
- Disambiguation token descriptions rewritten in plain English (e.g. "Loses 50 rank points" → "Reduces likelihood that this entity will be selected").
- Floor/Area aliases section: removed. Replaced with a notice card: "GLaDOS uses Home Assistant's built-in area and floor aliases to interpret commands like 'turn off the lights upstairs.' Add aliases in HA's area/floor configuration." + link to HA aliases docs.
- Visual demos pushed to the browser companion before code lands.

### Chunk 4 — Memory page rebuild

**Goal:** Icon row actions, importance slider, auto-learned-facts feed, RAG defined inline.

**Acceptance:**
- Each fact row has pencil + trash icons on the right (consistent with the new TTS Generator icon style from Phase 1 Task 7).
- Each fact has an importance slider (5- or 6-point scale UI, label values "background" → "extremely important"). UI-only — backing storage may be a single number 0–1 or an enum.
- Recent activity panel replaced with auto-learned-facts feed (facts written via the passive memory pipeline, not by the operator).
- An info card at the top defines RAG in plain English: "Retrieval-Augmented Generation. When she's about to respond, related facts from this library are pulled in and quoted to her so she can reference them accurately."

### Chunk 5 — Users page row icons + Speakers flat list

**Goal:** Users row actions become icons; Reset PW removed. Speakers becomes a single flat list.

**Acceptance:**
- Users page row actions: Disable (⊘), Edit (✎), Delete (🗑). Same icon set as TTS / Memory.
- Reset PW button removed (operator can edit user → set new password if needed).
- Speakers tab: single flat list, no room grouping. Each row: speaker friendly name + entity ID below + checkbox.
- Maintenance default speaker selector: still present, single dropdown picker from the same flat list.

### Chunk 6 — Functional bug investigations

Two investigations, run in parallel with the UI work or after, your call.

**6a — Maintenance speaker not honored.** Operator: "Living Room 2 selected, but Master Bedroom is the one that is playing whenever she speaks up right now."

Hypothesis: `silent_mode_speaker` config saves but at speech-time the engine resolves something else (perhaps the legacy `default_speaker` field, or HA's `media_player.master_bedroom` is hardcoded somewhere as a fallback). Read `glados/audio_io/homeassistant_io.py:silent_mode` and the engine's speaker-resolver path. Add logging if needed; one fix-commit.

**6b — Hyphen / dash rendering.** Operator: "TTS treats hyphens as no pause. The hyphens generated by her are weird, really long. Multiple types of hyphens, we try not to mix and match."

Hypothesis: GLaDOS LLM-rewriter generates em-dashes (`—`) where she should generate periods or commas; Piper does not pause on em-dashes. Read the `SpokenTextConverter` path (`glados/api/tts.py`) and the persona rewriter (`glados/persona/rewriter.py`). Either pre-process to replace em-dashes with comma+space (or period+space) before TTS, or train the rewriter to stop emitting them.

### Chunk 7 (deferred — own brainstorm) — Pre-recorded audio clip system

**Out of Phase 2 polish scope. Own brainstorm + spec.**

The operator wants:
- Quips → pre-recorded audio (upload .txt → batch-synthesize → store).
- Action confirmations (lights on/off, scenes, voice commands) play pre-recorded clips when complete.
- All audio under `<audio_root>/<Category>/<emotion>/<file>.mp3`.
- Existing `configs/sounds/` paths migrated.

This is a feature, not polish. Brainstorm session needed: what's the audio root path inside the container? How does the engine resolve `<Category>/<emotion>/`? How does the TTS Generator's "Save to library" path move? How do confirmations get triggered (HA event hook? engine-level)?

---

## Execution

Subagent-driven, Sonnet implementers, two-stage review per chunk. Push + GHA build + deploy after each chunk so operator can live-verify per chunk.
