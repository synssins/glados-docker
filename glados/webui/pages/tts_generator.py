"""HTML for the tts_generator tab (id="tab-tts").

Variant A redesign (2026-04-25): segmented mode pill, telemetry strip,
persona/format locked to GLaDOS/MP3 (dropdowns removed), pronunciation
overrides applied on the synthesis path, icon action buttons in file list.
"""

HTML = r"""<div id="tab-tts" class="tab-content">
<div class="page-shell">

  <div class="telemetry-strip" id="ttsTele">
    <span>MODE <b id="ttsModeLabel">SCRIPT</b></span>
    <span class="t-sep">&#x2502;</span>
    <span>PERSONA <b>GLaDOS</b></span>
    <span class="t-sep">&#x2502;</span>
    <span>FORMAT <b>MP3</b></span>
    <span class="t-sep">&#x2502;</span>
    <span>PIPER <span class="t-dot t-dot-ok" id="piperDot" title="TTS service status"></span></span>
  </div>

  <h1 class="page-h1">TTS Generator</h1>
  <p class="page-sub">Synthesize a clip. Pronunciation overrides from Personality apply automatically.</p>

  <div class="card">
    <div class="section-title">Recording mode</div>
    <div class="tts-seg" id="ttsModeSeg">
      <div class="tts-seg-cell on" data-mode="script" onclick="_ttsSwitchMode('script')">SCRIPT &mdash; verbatim</div>
      <div class="tts-seg-cell" data-mode="improv" onclick="_ttsSwitchMode('improv')">IMPROV &mdash; paraphrase</div>
    </div>
  </div>

  <div class="card tts-mode-card" id="tts-script-card">
    <div class="section-title">Script &mdash; read verbatim</div>
    <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
    <div class="char-count"><span id="charCount">0</span> CHARACTERS</div>
    <div class="controls">
      <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate audio</button>
      <div class="status" id="ttsStatus"></div>
    </div>
  </div>

  <div class="card tts-mode-card" id="tts-improv-card" style="display:none;">
    <div class="section-title">Brief her &mdash; she drafts, you approve</div>
    <textarea id="improvInstruction" placeholder="e.g. 'call everyone to dinner, snidely' or 'announce a thunderstorm warning in her bored voice'"></textarea>
    <div class="controls">
      <button class="btn btn-primary" id="improvDraftBtn" onclick="_ttsImprovDraft()">Draft text</button>
      <div class="status" id="improvStatus"></div>
    </div>
    <div id="improvDraftSection" style="display:none; margin-top:var(--sp-4);">
      <div class="section-title" style="font-size:0.82rem; margin-bottom:var(--sp-2);">She wrote:</div>
      <textarea id="improvDraftedText" placeholder="[draft appears here; edit if needed]"></textarea>
      <div class="controls">
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
          <option value="">&#x2014; pick category &#x2014;</option>
        </select>
        <input id="ttsSaveFilename" type="text" placeholder="filename (optional)" autocomplete="off">
        <button class="btn btn-primary" id="ttsSaveBtn" onclick="_ttsSaveToCategory()">Save to library</button>
        <div class="status" id="ttsSaveStatus"></div>
      </div>
      <div class="trait-desc" style="margin-top:var(--sp-2);">
        Creates <code>configs/sounds/&lt;category&gt;/&lt;filename&gt;</code> and registers the file
        in <code>sound_categories.yaml</code>. Pick <em>&#x2014; new category &#x2014;</em> to add a fresh category.
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
