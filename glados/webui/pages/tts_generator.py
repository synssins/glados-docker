"""HTML for the tts_generator tab (id="tab-tts").

2026-04-26: revert of Chunk 8's bubble/dock layout. Standard
page-header pattern; input on top, file list below; single shared
<audio> element driven by per-row play/stop buttons.
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

  <div class="page-header">
    <div>
      <h2 class="page-title">TTS Generator</h2>
      <div class="page-title-desc">Type a line, hit generate, hear it back.</div>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Recording mode</div>
    <div class="tts-seg" id="ttsModeSeg">
      <div class="tts-seg-cell on" data-mode="script" onclick="_ttsSwitchMode('script')">SCRIPT &mdash; verbatim</div>
      <div class="tts-seg-cell" data-mode="improv" onclick="_ttsSwitchMode('improv')">IMPROV &mdash; paraphrase</div>
    </div>
  </div>

  <div class="card" id="ttsScriptCard">
    <div class="section-title">Generate</div>
    <div class="tts-generate-row">
      <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
      <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate &#x2192;</button>
    </div>
  </div>

  <div class="card" id="ttsImprovCard" style="display:none;">
    <div class="section-title">Improv brief</div>
    <div class="tts-generate-row">
      <textarea id="improvInstruction" placeholder="e.g. &#x2018;call everyone to dinner, snidely&#x2019; or &#x2018;announce a thunderstorm, bored voice&#x2019;"></textarea>
      <button class="btn btn-primary" id="improvDraftBtn" onclick="_ttsImprovDraft()">Draft &#x2192;</button>
    </div>
    <div id="improvDraftSection" style="display:none; margin-top:var(--sp-3);">
      <div class="section-title">Draft</div>
      <div class="tts-generate-row">
        <textarea id="improvDraftedText" placeholder="[draft appears here; edit if needed]"></textarea>
        <div style="display:flex;flex-direction:column;gap:var(--sp-2);">
          <button class="btn" onclick="_ttsImprovDraft()">Redraft</button>
          <button class="btn btn-primary" id="improvGenerateBtn" onclick="_ttsImprovGenerate()">Speak &#x2192;</button>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Generated Files</div>
    <div id="ttsFileList"></div>
  </div>

  <audio id="ttsAudio" preload="none"></audio>

</div>
</div>
"""
