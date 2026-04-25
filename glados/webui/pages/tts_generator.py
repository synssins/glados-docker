"""HTML for the tts_generator tab (id="tab-tts").

Phase 5.9.2 (2026-04-22): rewritten from a single text-input card
into a two-mode workflow. Operator picks Script (verbatim — she
reads exactly what you type) or Improv (brief her in plain English
— she drafts a line herself, you approve or edit). Both modes
share the audio preview and Save-to-category flow at the bottom.
"""

HTML = r"""<div id="tab-tts" class="tab-content">
<div class="page-shell">
<div class="container">

  <!-- ═════ Mode toggle ═══════════════════════════════════════════ -->
  <div class="card">
    <div class="section-title">Recording mode</div>
    <div class="tts-mode-toggle" id="ttsModeToggle">
      <label class="tts-mode-option active" data-mode="script">
        <input type="radio" name="tts-mode" value="script" checked onchange="_ttsSwitchMode('script')">
        <div class="tts-mode-title">Script</div>
        <div class="tts-mode-desc">
          You write exactly what she should say, word for word.
          She reads it verbatim, no interpretation, no edits.
        </div>
      </label>
      <label class="tts-mode-option" data-mode="improv">
        <input type="radio" name="tts-mode" value="improv" onchange="_ttsSwitchMode('improv')">
        <div class="tts-mode-title">Improv</div>
        <div class="tts-mode-desc">
          Brief her in plain English &mdash; e.g. <em>&ldquo;announce that the laundry is
          done, in her persona&rdquo;</em>. She drafts a line herself, you review
          or edit before it&rsquo;s recorded.
        </div>
      </label>
    </div>
  </div>

  <!-- ═════ Script input card ════════════════════════════════════ -->
  <div class="card tts-mode-card" id="tts-script-card">
    <div class="section-title">Script &mdash; she reads this verbatim</div>
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
      <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate audio</button>
      <div class="status" id="ttsStatus"></div>
    </div>
  </div>

  <!-- ═════ Improv input card ════════════════════════════════════ -->
  <div class="card tts-mode-card" id="tts-improv-card" style="display:none;">
    <div class="section-title">Brief her &mdash; she drafts, you approve</div>
    <textarea id="improvInstruction" placeholder="e.g. &lsquo;call everyone to dinner, snidely&rsquo; or &lsquo;announce a thunderstorm warning in her bored voice&rsquo;"></textarea>
    <div class="controls">
      <button class="btn btn-primary" id="improvDraftBtn" onclick="_ttsImprovDraft()">Draft text</button>
      <div class="status" id="improvStatus"></div>
    </div>
    <!-- Drafted text appears here after Draft text is clicked -->
    <div id="improvDraftSection" style="display:none; margin-top:var(--sp-4);">
      <div class="section-title" style="font-size:0.82rem; margin-bottom:var(--sp-2);">She wrote:</div>
      <textarea id="improvDraftedText" placeholder="[draft appears here; edit if needed]"></textarea>
      <div class="controls">
        <select id="improvVoiceSelect" title="Voice model">
          <option value="glados">Voice: GLaDOS</option>
        </select>
        <select id="improvFormatSelect">
          <option value="wav">WAV</option>
          <option value="mp3">MP3</option>
          <option value="ogg">OGG</option>
        </select>
        <select id="improvAttitudeSelect">
          <option value="random">Attitude: Random</option>
          <option value="default">Attitude: Default</option>
        </select>
        <button class="btn" onclick="_ttsImprovDraft()">Redraft</button>
        <button class="btn btn-primary" id="improvGenerateBtn" onclick="_ttsImprovGenerate()">Generate audio</button>
        <div class="status" id="improvGenStatus"></div>
      </div>
    </div>
  </div>

  <!-- ═════ Audio preview + Save-to-category (shared) ════════════ -->
  <div class="card player-section" id="playerCard">
    <div class="player-label" id="playerLabel"></div>
    <audio id="audioPlayer" controls></audio>
    <!-- Phase 5.9.2: Save-to-category row. Appears whenever the
         player has an audio loaded; moves the file from the generic
         TTS-output directory into configs/sounds/<category>/ and
         updates sound_categories.yaml so the file joins the library. -->
    <div class="tts-save-row" id="ttsSaveRow" style="display:none;">
      <label class="mqtt-label" style="margin-bottom:0;">Save this recording</label>
      <div class="controls" style="flex-wrap:wrap;">
        <select id="ttsSaveCategory" title="Pick a category or create a new one">
          <option value="">-- pick category --</option>
        </select>
        <input id="ttsSaveFilename" type="text" placeholder="filename (optional)" autocomplete="off">
        <button class="btn btn-primary" id="ttsSaveBtn" onclick="_ttsSaveToCategory()">Save to library</button>
        <div class="status" id="ttsSaveStatus"></div>
      </div>
      <div class="trait-desc" style="margin-top:var(--sp-2);">
        Creates <code>configs/sounds/&lt;category&gt;/&lt;filename&gt;</code> and registers the file
        in <code>sound_categories.yaml</code>. Leave filename blank to use the auto-generated name.
        Pick <em>&mdash; new category &mdash;</em> to add a fresh category to the library.
      </div>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Generated Files</div>
    <div id="fileList"><div class="empty-msg">No files yet.</div></div>
  </div>
</div>
</div>
</div>
"""
