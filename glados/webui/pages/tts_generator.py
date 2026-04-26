"""HTML for the tts_generator tab (id="tab-tts").

Phase 2 Chunk 8 (2026-04-25): chat-thread refit.
- Legacy player-section card and file-list card replaced with a scrollable
  bubble thread (user + GLaDOS bubbles).
- Input docks at the bottom.
- Telemetry strip + segmented mode pill retained from Phase 1 Task 7.
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
  <p class="page-sub">Type a line, hit generate, hear it back. Pronunciation overrides from Personality apply automatically.</p>

  <div class="card">
    <div class="section-title">Recording mode</div>
    <div class="tts-seg" id="ttsModeSeg">
      <div class="tts-seg-cell on" data-mode="script" onclick="_ttsSwitchMode('script')">SCRIPT &mdash; verbatim</div>
      <div class="tts-seg-cell" data-mode="improv" onclick="_ttsSwitchMode('improv')">IMPROV &mdash; paraphrase</div>
    </div>
  </div>

  <div class="tts-thread" id="ttsThread">
    <!-- bubbles painted by _ttsRenderThread() -->
  </div>

  <div class="tts-input-dock" id="ttsInputDock">
    <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
    <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate &#x2192;</button>
  </div>

  <!-- Improv mode dock — swapped in/out by _ttsSwitchMode -->
  <div id="improvInputDock" style="display:none;">
    <div class="tts-input-dock">
      <textarea id="improvInstruction" placeholder="e.g. &#x2018;call everyone to dinner, snidely&#x2019; or &#x2018;announce a thunderstorm, bored voice&#x2019;"></textarea>
      <button class="btn btn-primary" id="improvDraftBtn" onclick="_ttsImprovDraft()">Draft &#x2192;</button>
    </div>
    <div id="improvDraftSection" style="display:none; margin-top:var(--sp-2);">
      <div class="tts-input-dock">
        <textarea id="improvDraftedText" placeholder="[draft appears here; edit if needed]"></textarea>
        <div style="display:flex;flex-direction:column;gap:var(--sp-2);">
          <button class="btn" onclick="_ttsImprovDraft()">Redraft</button>
          <button class="btn btn-primary" id="improvGenerateBtn" onclick="_ttsImprovGenerate()">Speak &#x2192;</button>
        </div>
      </div>
    </div>
  </div>

</div>
</div>
"""
