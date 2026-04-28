# TTS Generator — Revert Chat-Thread, Rebuild Plain List

**Date:** 2026-04-26
**Branch:** `webui-polish`
**Targets:** `glados/webui/pages/tts_generator.py`, `glados/webui/static/style.css`, `glados/webui/static/ui.js`, `glados/webui/tts_ui.py`
**Origin:** Operator review of Chunk 8 (`91dc9ea`) — verdict: "I hate the UI redesign. Bad UI design." Bubble metaphor, docked input, oversized header, native audio-controls bar all rejected.

## Goal

Replace the chat-thread bubble layout introduced in `91dc9ea` with a plain
file-list layout that matches the rest of the WebUI (System, Users, Memory).
Capture and surface the prompt text used for each generated file so it
persists across refresh.

## Page layout (top to bottom)

1. **Telemetry strip** — kept verbatim. Status pills: `MODE / PERSONA / FORMAT / PIPER`.
2. **Page header** — uses the standard pattern from System/Users:
   ```html
   <div class="page-header">
     <div>
       <h2 class="page-title">TTS Generator</h2>
       <div class="page-title-desc">Type a line, hit generate, hear it back.</div>
     </div>
   </div>
   ```
   No more `<h1>` + paragraph subtitle.
3. **Recording mode card** — kept verbatim. SCRIPT verbatim / IMPROV paraphrase segmented toggle, with the IMPROV brief textarea revealing under the toggle when IMPROV is selected.
4. **Generate area card** — `<textarea>` for the line + `Generate →` primary button on the right. Static; no docking. Ctrl+Enter still triggers generate.
5. **Generated Files list** — under the generate card. Flat rows. No bubble framing, no `GLADOS · timestamp` headers, no `.tts-thread` scroll container.

## Per-row layout (Generated Files)

Each row contains:

- **Filename** (top, primary) — e.g. `Hey-There.wav`.
- **Prompt text** (below filename, muted) — the exact text used to generate the file. If unavailable (legacy file with no sidecar), render nothing — no placeholder.
- **Single play/stop button** (left of the row's metrics) — toggles playback of that file. Replaces the native `<audio controls>` transport bar entirely.
- **Metrics line** — chars + size, dimmed, single line. Same data as before.
- **Action icons** (right) — Download / Save-to-library / Delete, same icon set as Chunk 8 but laid out next to the file metadata instead of inside a bubble footer.

No timeline scrubber, no volume slider, no playback-rate menu. One button: ▶ Play / ■ Stop.

## Single shared audio element

One `<audio>` element exists on the page (no `controls` attribute, hidden).
Rows do not own their own `<audio>` element.

- Click ▶ on row A → set `audio.src = A.url`, play, mark row A as playing.
- Click ▶ on row B while A is playing → pause+reset A's button to ▶, set `audio.src = B.url`, play, mark row B as playing.
- Click ■ on the playing row → pause+reset that row's button to ▶.
- `audio.onended` → reset whichever row is marked playing back to ▶.

## Prompt-text persistence (sidecar files)

`/api/files` currently exposes `{name, size, date, url}`. Prompt text is
not stored. Fix: write a sidecar `.txt` file at generate time.

- **On generate** (`/api/generate` write path): after the `.wav` is written to `OUTPUT_DIR`, write `<wav_filename>.txt` containing the raw prompt string. UTF-8, no trailing newline transformations.
- **On list** (`_list_files` in `tts_ui.py`): for each `.wav`, attempt to read the matching `.txt` sidecar. Skip listing the `.txt` files themselves. Add `prompt` to the row dict, `null` if absent.
- **On delete** (`/api/files/<name>` DELETE): also unlink the matching `.txt` sidecar if present, ignoring missing-file errors.
- **On save-to-library**: copy the `.txt` alongside the `.wav` if the destination supports it (no behavior change for the on-disk layout under audio root — sidecars allowed there too, ignored by the audio-event system).

## What gets removed

From `glados/webui/pages/tts_generator.py`:
- The current `<h1>` + paragraph subtitle.
- Any markup specific to the bubble thread / docked input.

From `glados/webui/static/style.css`:
- All `.tts-bubble*` rules.
- The dock-input fixed-bottom CSS for the TTS Generator.
- `.tts-thread` scroll-container rules.
- Any auto-scroll padding hacks introduced in `91dc9ea`.

From `glados/webui/static/ui.js`:
- The bubble-thread builder and history pre-loader (replaced with a flat-row builder fed by the same `/api/files` data).
- The user-bubble-on-Generate flow (no user bubbles in the new layout).
- The auto-scroll-to-latest behavior.
- The inline-Save-to-library form-under-bubble logic. Save-to-library opens the existing inline form pattern used elsewhere in the WebUI (a small panel below the row when the icon is clicked, dismissable).

## What stays

- Telemetry strip + per-pill values + Mode/Persona/Format/Piper status logic.
- Recording-mode segmented toggle + IMPROV brief textarea + `Speak` flow.
- `/api/generate` semantics (request shape, response shape).
- Pronunciation overrides applied automatically (no UI change).
- Per-row Download / Save-to-library / Delete icon glyphs.

## Risks and trade-offs

- **Sidecar files orphan on manual rename.** Operator does not rename files via UI; acceptable.
- **Legacy files have no prompt text.** Empty rendering, no placeholder. Operator's existing files (the four shown in the screenshot) get no prompt text until regenerated.
- **`.txt` sidecars land under audio root if copied during Save-to-library.** Audio-event system ignores them by file extension. Confirmed safe.
- **Single shared `<audio>` element** — switching tabs/pages while playing stops playback; matches user mental model.

## Out of scope

- Server-side synth-time reporting (still client-side approximation if surfaced; row metrics line stays as chars + size, no synth time).
- Improv brief retention across refresh.
- Configuration → Sounds page (separate, untouched).
- Any change to `/api/generate` request shape.

## Acceptance

1. TTS Generator page header matches System/Users page styling.
2. Generate textarea + button sit under the recording-mode card; no docking.
3. Generated file list renders below the generate card; rows are flat (no bubble framing); each row shows filename + prompt text (when available) + a single play/stop button + metrics + icon actions.
4. Clicking play on row A then row B stops A and plays B. Clicking stop on the playing row stops it.
5. Generating a new file writes the `.txt` sidecar; reloading the page shows the prompt text under the filename.
6. Deleting a file via the row's trash icon removes both the `.wav` and the `.txt` sidecar.
7. No `.tts-bubble*`, `.tts-thread`, or dock-input CSS remains in `style.css`.
8. Test suite still passes (1388 → 1388+ with new sidecar tests).
