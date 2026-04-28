# Chunk 7 — Pre-Recorded Audio Clip Library (WIP brainstorm)

**Status:** brainstorm in progress. Blocked on operator decision for
workflow shape (A/B/C below). Resume here to continue.
**Branch:** `webui-polish`
**Spec target:** `docs/superpowers/specs/2026-04-26-chunk7-audio-library-design.md` (final)

## Operator-stated description

From the 2026-04-26 handoff (`C:\src\SESSION_STATE.md`, "Next session
direction" item 3):

> Upload a `.txt` of one quip per line, GLaDOS batch-synthesizes each,
> stores at `<audio_root>/<Category>/<emotion>/<file>.mp3` (NEVER
> under `configs/`). Used for action confirmations (lights on/off,
> scenes, voice-command acks) and for instant playback of stock quips.

## Existing infrastructure mapped during brainstorm

- **Audio root**: `_GLADOS_AUDIO` env var, default `/app/audio_files`
  (config_store.py:380 `AudioConfig`).
- **Sound library path**: `sounds_dir = ${audio_root}/sounds`. Today's
  on-disk layout is `<sounds_dir>/<category>/<file>` — flat, no
  emotion dim.
- **Schema**: `configs/sound_categories.yaml` with per-category entries
  (`name`, `description`, `action_kind`, `llm_preset`, `selected_file`,
  `ha_exposed`, `speaker`, `files: {}`). The `files` map registers each
  file with `enabled / added / note`.
- **Existing save flow**: `/api/tts/save-to-category` (tts_ui.py:2156
  `_post_tts_save_to_category`) — copies a single TTS-generated file
  into `<sounds_dir>/<category>/`, upserts the YAML entry. One file at
  a time, no emotion dim.
- **Existing UI surface**: Configuration → Audio & Speakers → Sounds
  tab. Currently only renders the **Chime library** (separate
  `chimes_dir`, also flat). No category browser yet.
- **Related plan in memory** (`project_event_actions_plan.md`):
  Chunk 7 is the **content-creation half** of the planned event-action
  system. Triggers (HA state, MQTT) will fire random picks from a
  named category folder. Build the library first, wire triggers later.

## Memory-rule constraints

- `feedback_audio_paths.md` — audio NEVER under `configs/`. Path is
  `Category/emotion/file.mp3` under the audio root. Hard rule, operator
  has flagged it repeatedly.
- `feedback_no_local_docker.md` — deploy via `_local_deploy.py` to the
  Docker host; don't `docker build` locally.
- `feedback_devtools_console_first.md` — for any WebUI bug post-deploy,
  ask for the DevTools console first.

## Workflow-shape options presented to operator (awaiting decision)

### Option A — one category + one emotion per upload (recommended)

Operator picks category (existing list or new), picks emotion from a
fixed list (`neutral / snide / earnest / excited / deadpan` —
vocabulary still TBD), uploads a `.txt` of one line per quip, hits
**Synthesize All**. Files land at
`<sounds_dir>/<category>/<emotion>/<slug>.mp3`. One synth pass, one
bucket. Multiple emotions per category = multiple uploads.

**Pros:** simplest UI, simplest validation, operator fully in control
of which bucket they're filling. No parsing of per-line metadata.

**Cons:** loading a multi-emotion library means N uploads instead of 1.

### Option B — metadata-per-line tags

Each `.txt` line carries inline tags, e.g. `[snide] Oh, well done.` or
`snide | "Oh, well done."`. Operator picks category once at upload
time; emotion auto-routes per line. One upload populates multiple
emotion subfolders within one category.

**Pros:** one-shot bulk-load of a whole category's variation.

**Cons:** parsing edge cases, typo failure modes (`[snid]` silently
creates a `snid` folder?), no UX way to validate before synth.

### Option C — defer emotion dim to a later chunk

Drop emotion entirely for v1. Build batch synth for the existing flat
`<sounds_dir>/<category>/<file>` layout. Adds emotion in a later chunk
after batch flow is proven. Less ambitious; matches existing schema.

**Pros:** smallest delta, smallest surface area, low risk.

**Cons:** missing the dimension that makes the library feel like
GLaDOS varies across moods rather than picking from one flat list of
all snark. Operator has explicitly flagged emotion in the audio-paths
memory rule, so deferring contradicts stated intent.

**Claude's recommendation:** **A**.

## Open questions (queued for after A/B/C decision)

If A is chosen — these need answers before writing the design doc:

1. **Emotion vocabulary.** Fixed list, operator-editable list, or
   free-text? If fixed: what set? (Suggested first cut:
   `neutral / snide / earnest / excited / deadpan / weary` — drawn
   from the persona's existing voice range.)
2. **Filename derivation.** AI-derived slug (existing `_ai_filename`
   in `tts_ui.py`)? Hash of the line text? Counter-based
   (`001.mp3`, `002.mp3`)?
3. **Synthesis params.** Use the engine's current PAD/emotion-derived
   `length_scale / noise_scale / noise_w`? Or persona+emotion
   presets? Or per-line override?
4. **Synth speed and progress feedback.** Piper is CPU; a 50-line
   batch may take a minute. Stream progress (SSE? polling?), batch
   in background, all-or-nothing on failure?
5. **Replay / regenerate flow.** Can operator re-synth a single
   line later? Edit the text? Delete a single clip?
6. **WebUI surface.** Extend the Configuration → Audio & Speakers
   → Sounds tab (category browser + batch upload), or new top-level
   tab? Sub-page or modal?
7. **Schema migration.** `sound_categories.yaml`'s `files: {}` map
   keys are filenames today. With emotion subfolders, do keys
   become `<emotion>/<filename>` (path-style) or do we add a
   nested `files: {<emotion>: {<filename>: {...}}}` shape?
8. **Playback wiring.** When the event-action system later picks
   from a category, does it pick at random across ALL emotions, or
   does the trigger specify which emotion bucket? (Probably the
   latter — but the schema needs to support it.)
9. **Per-clip metadata persistence.** Sidecar `.txt` (matches the
   2026-04-26 TTS Generator change), embedded in YAML, or both?
   (Operator value: re-listening to a clip and seeing its line
   text without DB lookup.)
10. **Validation / quality gate.** Hard-rule: don't synth if line
    > N chars, contains an emoji, or fails a duplicate-check
    against existing files in the same bucket?

## Resume instructions

When picked up:

1. Operator picks A / B / C from the workflow shapes above.
2. Walk the open-questions list 1–10 above (one at a time per
   brainstorming-skill discipline). Some answers will collapse: if A
   is chosen, the emotion vocabulary question (1) may be closed by a
   short suggested fixed list and a "make it editable later"
   deferral.
3. Write final design doc at
   `docs/superpowers/specs/2026-04-26-chunk7-audio-library-design.md`,
   committed; this WIP file gets superseded.
4. Then writing-plans skill → execution.

## Related

- Spec for the just-shipped TTS Generator revert:
  `docs/superpowers/specs/2026-04-26-tts-generator-revert-design.md`
  (the `.txt` sidecar pattern from that spec is a candidate building
  block here too — open question #9).
- Event-actions plan memory:
  `C:\Users\Administrator\.claude\projects\C--src\memory\project_event_actions_plan.md`.
