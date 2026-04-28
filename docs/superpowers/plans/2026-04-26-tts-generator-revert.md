# TTS Generator — Revert Chat-Thread, Rebuild Plain List — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Chunk 8's bubble-thread TTS Generator UI with a flat
list that matches the rest of the WebUI: standard page-header pattern,
input on top, file rows below with single play/stop button per row.
Persist generation prompts via `.txt` sidecar files.

**Architecture:** Backend writes a `<wav>.txt` sidecar at generate time
and reads it during file listing; deletion removes both. Frontend
swaps the bubble-thread builder for a flat-row builder, replaces native
`<audio controls>` with a single shared hidden `<audio>` element driven
by per-row play/stop buttons, and conforms the page header to the
shared `page-header / page-title / page-title-desc` pattern.

**Tech Stack:** Python 3.14 stdlib HTTP, vanilla JS, vanilla CSS.

**Spec:** `docs/superpowers/specs/2026-04-26-tts-generator-revert-design.md`

---

## Task 0: Sidecar `.txt` files — backend (write, list, delete)

**Goal:** Persist the prompt text used for each generated `.wav` so it
survives page refresh.

**Files:**
- Modify: `glados/webui/tts_ui.py:2256-2304` (`_generate`), `:3993-4004` (`_list_files`), `:4321-4334` (`_delete_file`)
- Test: `tests/test_tts_sidecar.py` (new)

**Acceptance Criteria:**
- [ ] `_generate` writes `<wav_path>.txt` containing the raw text passed in (post-pronunciation-overrides — same text the synth saw).
- [ ] `_list_files` returns rows with a `prompt` key. If the sidecar exists, `prompt` is its UTF-8 contents; otherwise `prompt` is `None`. `.txt` files are not listed as their own entries.
- [ ] `_delete_file` removes the matching `.txt` sidecar if present, ignoring missing-file errors. Wav-not-found still returns 404.

**Verify:** `python -m pytest tests/test_tts_sidecar.py -v` → all tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests for sidecar behavior.**

```python
# tests/test_tts_sidecar.py
import io, json, tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from glados.webui import tts_ui
from glados.webui.tts_ui import Handler


def _make_handler(path, body=b""):
    h = Handler.__new__(Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h._status_code = None
    def _sr(code, *a, **k): h._status_code = code
    h.send_response = _sr
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    return h


def _resp(h):
    return h._status_code, json.loads(h.wfile.getvalue().decode())


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_ui, "OUTPUT_DIR", tmp_path)
    return tmp_path


def test_list_files_includes_prompt_from_sidecar(output_dir):
    wav = output_dir / "Hey-There.wav"
    wav.write_bytes(b"RIFF....fakewav")
    (output_dir / "Hey-There.wav.txt").write_text("Hey there friend", encoding="utf-8")
    h = _make_handler("/api/files")
    h._list_files()
    status, payload = _resp(h)
    assert status == 200
    rows = {f["name"]: f for f in payload["files"]}
    assert rows["Hey-There.wav"]["prompt"] == "Hey there friend"


def test_list_files_prompt_none_when_sidecar_missing(output_dir):
    (output_dir / "Legacy.wav").write_bytes(b"RIFF")
    h = _make_handler("/api/files")
    h._list_files()
    _, payload = _resp(h)
    rows = {f["name"]: f for f in payload["files"]}
    assert rows["Legacy.wav"]["prompt"] is None


def test_list_files_skips_txt_sidecars(output_dir):
    (output_dir / "x.wav").write_bytes(b"R")
    (output_dir / "x.wav.txt").write_text("hi", encoding="utf-8")
    h = _make_handler("/api/files")
    h._list_files()
    _, payload = _resp(h)
    names = [f["name"] for f in payload["files"]]
    assert names == ["x.wav"]


def test_delete_removes_sidecar(output_dir):
    wav = output_dir / "Doomed.wav"
    wav.write_bytes(b"R")
    sidecar = output_dir / "Doomed.wav.txt"
    sidecar.write_text("doomed", encoding="utf-8")
    h = _make_handler("/api/files/Doomed.wav")
    h._delete_file()
    status, _ = _resp(h)
    assert status == 200
    assert not wav.exists()
    assert not sidecar.exists()


def test_delete_tolerates_missing_sidecar(output_dir):
    wav = output_dir / "NoSidecar.wav"
    wav.write_bytes(b"R")
    h = _make_handler("/api/files/NoSidecar.wav")
    h._delete_file()
    status, _ = _resp(h)
    assert status == 200
    assert not wav.exists()


def test_generate_writes_sidecar(output_dir):
    body = json.dumps({"text": "Hello there", "format": "wav"}).encode()
    h = _make_handler("/api/generate", body)
    fake_audio = b"RIFFfakewav"
    class _FakeResp:
        def read(self): return fake_audio
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with patch("glados.webui.tts_ui._apply_pronunciation_to_text", side_effect=lambda t: t), \
         patch("urllib.request.urlopen", return_value=_FakeResp()), \
         patch("glados.webui.tts_ui._cleanup_old_files"):
        h._generate()
    status, payload = _resp(h)
    assert status == 200
    fname = payload["filename"]
    assert (output_dir / fname).exists()
    assert (output_dir / f"{fname}.txt").read_text(encoding="utf-8") == "Hello there"
```

- [ ] **Step 2: Run tests to confirm they fail.**

```bash
python -m pytest tests/test_tts_sidecar.py -v
```

Expected: 6 failures (sidecar logic doesn't exist yet).

- [ ] **Step 3: Modify `_generate` to write the sidecar.**

In `glados/webui/tts_ui.py` `_generate` method, after `file_path.write_bytes(audio_data)`:

```python
file_path.write_bytes(audio_data)
try:
    file_path.with_suffix(file_path.suffix + ".txt").write_text(text, encoding="utf-8")
except OSError:
    pass  # best-effort — don't fail the synth if sidecar can't be written
```

(Note: `.with_suffix(".wav.txt")` would replace `.wav`. The pattern `<file>.<ext>.txt` requires concatenating, hence `file_path.with_suffix(file_path.suffix + ".txt")` which gives `Hey-There.wav.txt` from `Hey-There.wav`.)

- [ ] **Step 4: Modify `_list_files` to read sidecars and skip `.txt`.**

Replace the `_list_files` body with:

```python
def _list_files(self):
    files = []
    for f in sorted(OUTPUT_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        if f.suffix == ".txt":
            continue
        st = f.stat()
        prompt = None
        sidecar = f.with_suffix(f.suffix + ".txt")
        if sidecar.is_file():
            try:
                prompt = sidecar.read_text(encoding="utf-8")
            except OSError:
                prompt = None
        files.append({
            "name": f.name,
            "size": st.st_size,
            "date": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "url": f"/files/{f.name}",
            "prompt": prompt,
        })
    self._send_json(200, {"files": files})
```

- [ ] **Step 5: Modify `_delete_file` to unlink sidecar.**

Replace the `_delete_file` body with:

```python
def _delete_file(self):
    name = self.path[len("/api/files/"):]
    name = urllib.request.url2pathname(name)
    file_path = OUTPUT_DIR / name
    if not file_path.is_file() or not file_path.is_relative_to(OUTPUT_DIR):
        self._send_json(404, {"error": "File not found"})
        return
    try:
        file_path.unlink()
        sidecar = file_path.with_suffix(file_path.suffix + ".txt")
        if sidecar.is_file():
            try:
                sidecar.unlink()
            except OSError:
                pass
        self._send_json(200, {"deleted": name})
    except OSError as e:
        self._send_json(500, {"error": str(e)})
```

- [ ] **Step 6: Re-run tests to confirm they pass.**

```bash
python -m pytest tests/test_tts_sidecar.py -v
```

Expected: 6 passed.

- [ ] **Step 7: Commit.**

```bash
git add tests/test_tts_sidecar.py glados/webui/tts_ui.py
git commit -m "feat(tts): persist generation prompts via .txt sidecar files"
```

---

## Task 1: Page header + remove old h1/subtitle

**Goal:** Make the TTS Generator page header look like System and Users.

**Files:**
- Modify: `glados/webui/pages/tts_generator.py` (full rewrite of the HTML body — also covers Task 3's flat-row structure since they share the same file).

**Acceptance Criteria:**
- [ ] Page renders `<div class="page-header"><div><h2 class="page-title">TTS Generator</h2><div class="page-title-desc">…</div></div></div>` at the top of its content body, after the telemetry strip.
- [ ] No `<h1>` element on the page.
- [ ] No multi-line "Type a line, hit generate, hear it back. Pronunciation overrides…" subtitle paragraph (the description-line summary on `page-title-desc` is one short sentence).

**Verify:** Page renders and text matches; test by inspecting rendered HTML in Task 4 manual verify.

**Steps:**

- [ ] **Step 1: Locate the current header block in `tts_generator.py`.**

It's the first `<h1>TTS Generator</h1>` + paragraph subtitle inside the content body.

- [ ] **Step 2: Replace it with the standard pattern.**

```html
<div class="page-header">
  <div>
    <h2 class="page-title">TTS Generator</h2>
    <div class="page-title-desc">Type a line, hit generate, hear it back.</div>
  </div>
</div>
```

(Full file rewrite happens in Task 3 — header change is part of that single edit; this task documents the header-specific acceptance criteria so the rewrite is verifiable.)

- [ ] **Step 3: Defer commit to Task 3.**

Tasks 1, 2, and 3 all touch `tts_generator.py` / `style.css` / `ui.js` cohesively; one commit covers them.

---

## Task 2: Strip bubble/dock CSS

**Goal:** Remove all CSS rules tied to the chat-thread layout.

**Files:**
- Modify: `glados/webui/static/style.css`

**Acceptance Criteria:**
- [ ] No selector matching `.tts-bubble`, `.tts-bubble-*`, `.tts-thread`, `.tts-dock`, `.tts-input-dock`, or any class introduced in `91dc9ea`'s style.css hunk remains in the file.
- [ ] CSS rules for the new flat layout (`.tts-row`, `.tts-row-name`, `.tts-row-prompt`, `.tts-row-meta`, `.tts-row-play`, `.tts-row-actions`) are added.
- [ ] `style.css` parses without errors (no orphan braces).

**Verify:** `python -m pytest tests/ -k "css or style" -v` if any css tests exist; otherwise rely on Task 4 (full suite).

**Steps:**

- [ ] **Step 1: Identify removed-block boundaries.**

Find the block introduced by `91dc9ea` (search for `.tts-bubble` and the surrounding section comment "Phase 2 Chunk 8 — TTS chat thread"). Remove the full block.

- [ ] **Step 2: Add new flat-row rules.**

Append (or place adjacent to existing `.tts-row` neighbors if any):

```css
/* TTS Generator — flat file rows (Chunk 8 revert, 2026-04-26) */
.tts-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px 16px;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
}
.tts-row:last-child { border-bottom: none; }
.tts-row-play {
  width: 36px; height: 36px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent;
  border: 1px solid var(--border-input);
  border-radius: 4px;
  color: var(--text-primary);
  cursor: pointer;
  font-size: 14px;
}
.tts-row-play:hover { background: var(--bg-sidebar); }
.tts-row-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.tts-row-name { font-family: var(--font-mono); color: var(--text-primary); }
.tts-row-prompt {
  color: var(--text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tts-row-meta { color: var(--text-muted); font-size: 11px; }
.tts-row-actions { display: flex; gap: 6px; }
```

(Variables `--border-subtle`, `--border-input`, `--bg-sidebar`, `--text-primary/secondary/muted`, `--font-mono` already exist in the design tokens — confirm via `grep "--border-subtle\|--border-input\|--bg-sidebar\|--text-primary" glados/webui/static/style.css` before applying. If any are missing, fall back to the closest available token.)

- [ ] **Step 3: Defer commit to Task 3.**

---

## Task 3: Rebuild flat-row file list (`ui.js` + `tts_generator.py`)

**Goal:** Replace the bubble-thread JS with a flat-row builder driven
by a single shared `<audio>` element.

**Files:**
- Modify: `glados/webui/pages/tts_generator.py` (final layout — header from Task 1, generate area, file-list container)
- Modify: `glados/webui/static/ui.js` (replace the bubble functions with flat-row builder + play/stop logic)

**Acceptance Criteria:**
- [ ] Page DOM has, in order: telemetry strip → `.page-header` → recording-mode card → generate-area card (textarea + Generate button) → `<div id="ttsFileList"></div>` → hidden `<audio id="ttsAudio">` element (no `controls` attribute).
- [ ] `loadTtsFiles()` (or equivalent) fetches `/api/files`, builds flat `.tts-row` divs, each with: `.tts-row-play` (▶), `.tts-row-body` (filename + prompt + meta), `.tts-row-actions` (Download / Save / Delete icons).
- [ ] On generate success, prepend a new row at the top of `#ttsFileList` (no user-bubble preamble).
- [ ] Clicking ▶ on row A: sets `ttsAudio.src` to A's url, plays, marks the button as `■`. Clicking ▶ on row B while A plays: pauses+resets A, plays B. Clicking `■` on the playing row: pauses+resets it. `audio.onended` resets the marked-playing row.
- [ ] No `.tts-bubble*`, no `.tts-thread`, no dock-input rendering.

**Verify:** `python -m pytest tests/ -v` (full suite, sanity-check no regression). Manual UI verify deferred to Task 5.

**Steps:**

- [ ] **Step 1: Rewrite `tts_generator.py` body.**

```html
<div class="tts-telemetry-strip">
  <!-- existing strip preserved verbatim: MODE / PERSONA / FORMAT / PIPER pills -->
</div>

<div class="page-header">
  <div>
    <h2 class="page-title">TTS Generator</h2>
    <div class="page-title-desc">Type a line, hit generate, hear it back.</div>
  </div>
</div>

<div class="card">
  <div class="section-title">Recording mode</div>
  <!-- existing SCRIPT / IMPROV segmented toggle preserved -->
</div>

<div class="card">
  <div class="section-title">Generate</div>
  <div class="tts-generate-row">
    <textarea id="ttsInput" placeholder="Type something to synthesize..." rows="3"></textarea>
    <button id="ttsGenerateBtn" class="btn-primary" onclick="ttsGenerate()">Generate →</button>
  </div>
</div>

<div class="card">
  <div class="section-title">Generated Files</div>
  <div id="ttsFileList"></div>
</div>

<audio id="ttsAudio" preload="none"></audio>
```

The exact block ids and class names match the existing JS from pre-Chunk-8 where possible. Preserve the IMPROV brief textarea (when SCRIPT/IMPROV switches modes, it appears under the recording-mode card as before).

- [ ] **Step 2: Rewrite `ui.js` TTS Generator section.**

Find the Chunk 8 hunk that defined `_buildBubble`, `_loadTtsHistory`, `_dockInput`, etc. — remove those entirely. Add:

```javascript
// TTS Generator — flat file list (Chunk 8 revert, 2026-04-26)
let _ttsPlayingName = null;

function ttsRowHtml(f) {
  const promptHtml = f.prompt
    ? '<div class="tts-row-prompt" title="' + escAttr(f.prompt) + '">' + escHtml(f.prompt) + '</div>'
    : '';
  const sizeKb = (f.size / 1024).toFixed(1);
  return '<div class="tts-row" data-name="' + escAttr(f.name) + '" data-url="' + escAttr(f.url) + '">'
    + '<button class="tts-row-play" onclick="ttsTogglePlay(this)">▶</button>'
    + '<div class="tts-row-body">'
    +   '<div class="tts-row-name">' + escHtml(f.name) + '</div>'
    +   promptHtml
    +   '<div class="tts-row-meta">' + sizeKb + ' KB</div>'
    + '</div>'
    + '<div class="tts-row-actions">'
    +   '<button class="icon-btn" title="Download" onclick="ttsDownload(\'' + escAttr(f.name) + '\')">' + _DOWNLOAD_SVG + '</button>'
    +   '<button class="icon-btn" title="Save to library" onclick="ttsSaveToLibrary(\'' + escAttr(f.name) + '\')">' + _SAVE_SVG + '</button>'
    +   '<button class="icon-btn" title="Delete" onclick="ttsDelete(\'' + escAttr(f.name) + '\')">' + _TRASH_SVG + '</button>'
    + '</div>'
    + '</div>';
}

async function ttsLoadFiles() {
  try {
    const resp = await fetch('/api/files');
    const data = await resp.json();
    const html = (data.files || []).map(ttsRowHtml).join('');
    document.getElementById('ttsFileList').innerHTML = html;
  } catch (e) {
    console.error('TTS file load failed', e);
  }
}

function ttsTogglePlay(btn) {
  const row = btn.closest('.tts-row');
  const name = row.dataset.name;
  const audio = document.getElementById('ttsAudio');
  if (_ttsPlayingName === name) {
    audio.pause();
    audio.currentTime = 0;
    btn.textContent = '▶';
    _ttsPlayingName = null;
    return;
  }
  if (_ttsPlayingName) {
    const prev = document.querySelector('.tts-row[data-name="' + _ttsPlayingName.replace(/"/g, '\\"') + '"] .tts-row-play');
    if (prev) prev.textContent = '▶';
  }
  audio.src = row.dataset.url;
  audio.play().then(() => {
    btn.textContent = '■';
    _ttsPlayingName = name;
  }).catch(err => console.error('audio play failed', err));
}

document.getElementById('ttsAudio')?.addEventListener('ended', () => {
  if (_ttsPlayingName) {
    const btn = document.querySelector('.tts-row[data-name="' + _ttsPlayingName.replace(/"/g, '\\"') + '"] .tts-row-play');
    if (btn) btn.textContent = '▶';
    _ttsPlayingName = null;
  }
});

async function ttsGenerate() {
  const input = document.getElementById('ttsInput');
  const text = input.value.trim();
  if (!text) return;
  const btn = document.getElementById('ttsGenerateBtn');
  btn.disabled = true;
  try {
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, format: 'wav' }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert('Generate failed: ' + (err.error || resp.status));
      return;
    }
    input.value = '';
    await ttsLoadFiles();
  } finally {
    btn.disabled = false;
  }
}

async function ttsDelete(name) {
  if (!confirm('Delete ' + name + '?')) return;
  await fetch('/api/files/' + encodeURIComponent(name), { method: 'DELETE' });
  await ttsLoadFiles();
}

function ttsDownload(name) {
  const a = document.createElement('a');
  a.href = '/files/' + encodeURIComponent(name);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ttsSaveToLibrary keeps existing inline-form pattern (existing helper preserved).
```

If `escHtml`, `escAttr`, `_DOWNLOAD_SVG`, `_SAVE_SVG`, `_TRASH_SVG` are already defined in `ui.js`, reuse them — don't duplicate.

- [ ] **Step 3: Wire `ttsLoadFiles()` to page-init.**

Find the existing TTS-page-init handler (likely a `route` callback or a `DOMContentLoaded` block) and call `ttsLoadFiles()` from it. Remove the old bubble-history loader call.

- [ ] **Step 4: Run the full test suite.**

```bash
python -m pytest tests/ -q
```

Expected: 1394 passed / 5 skipped (1388 prior + 6 new sidecar tests).

- [ ] **Step 5: Commit.**

```bash
git add glados/webui/pages/tts_generator.py glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "fix(tts-ui): revert chat-thread, rebuild flat list with single play/stop"
```

---

## Task 4: Deploy + visual verify

**Goal:** Deploy the rebuilt UI to the host and confirm it renders.

**Files:** None edited; deploy only.

**Acceptance Criteria:**
- [ ] `scripts/_local_deploy.py` completes without error and prints the new image SHA.
- [ ] Curl `https://glados.denofsyn.com:8052/static/ui.js | grep -c tts-bubble` returns `0`.
- [ ] Operator hard-refreshes the WebUI and confirms the layout matches the spec.

**Verify:**

```bash
python scripts/_local_deploy.py
curl -ks https://glados.denofsyn.com:8052/health
curl -ks https://glados.denofsyn.com:8052/static/ui.js | grep -c tts-bubble  # → 0
```

**Steps:**

- [ ] **Step 1: Run deploy.**

```bash
MSYS_NO_PATHCONV=1 python scripts/_local_deploy.py
```

- [ ] **Step 2: Capture image SHA from deploy output.**

The script prints the resulting image SHA. Record it for the handoff update.

- [ ] **Step 3: Curl-verify the bundle is the new version.**

```bash
curl -ks https://glados.denofsyn.com:8052/static/ui.js | grep -c "tts-bubble" || true
```

Expected: `0`.

- [ ] **Step 4: Ask operator to hard-refresh and visually confirm.**

Operator opens `https://glados.denofsyn.com:8052/`, hard-refreshes, navigates to TTS Generator, generates a line, confirms:
- Header style matches Users / System pages.
- Input on top, file list below, no docking.
- Play/stop button works (one button, no transport bar).
- Prompt text shows under the new file's name.

---
