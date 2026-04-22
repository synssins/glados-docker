"""HTML for the tts_generator tab (id="tab-tts").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""<div id="tab-tts" class="tab-content">
<div class="container">
  <div class="card">
    <div class="section-title">Enter text to synthesize</div>
    <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
    <div class="char-count"><span id="charCount">0</span> characters</div>
    <div class="controls">
      <select id="voiceSelect" title="Voice model">
        <option value="glados">Voice: GLaDOS</option>
      </select>
      <select id="formatSelect">
        <option value="wav">WAV</option>
        <option value="mp3">MP3</option>
        <option value="ogg">OGG</option>
      </select>
      <select id="attitudeSelect" title="Attitude â€” controls vocal delivery style (GLaDOS only)">
        <option value="random">Attitude: Random</option>
        <option value="default">Attitude: Default</option>
      </select>
      <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate</button>
      <div class="status" id="ttsStatus"></div>
    </div>
  </div>
  <div class="card player-section" id="playerCard">
    <div class="player-label" id="playerLabel"></div>
    <audio id="audioPlayer" controls></audio>
  </div>
  <div class="card">
    <div class="section-title">Generated Files</div>
    <div id="fileList"><div class="empty-msg">No files yet.</div></div>
  </div>
</div>
</div>
"""
