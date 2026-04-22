"""HTML for the system tab (id="tab-config-system").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 3: System Control                                          -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-config-system" class="tab-content">
<div class="container" style="position:relative;">
  <div id="controlAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access System Controls</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>
  <div class="card">
    <div class="section-title">Mode Controls</div>

    <div class="mode-row">
      <div>
        <div class="mode-label">Maintenance Mode</div>
        <div class="mode-desc">Route all audio to a single speaker</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="maintToggle" onchange="toggleMaintenance()">
        <span class="toggle-slider"></span>
      </label>
    </div>

    <div class="speaker-select-row" id="speakerRow" style="display:none;">
      <label style="font-size:0.85rem;color:var(--text-dim);">Speaker:</label>
      <select id="speakerSelect">
        <option value="">Loading speakers...</option>
      </select>
    </div>

    <div class="mode-row">
      <div>
        <div class="mode-label">Silent Mode</div>
        <div class="mode-desc">Mute all audio, send HA notifications only</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="silentToggle" onchange="toggleSilent()">
        <span class="toggle-slider"></span>
      </label>
    </div>
  </div>

  <!-- Maintenance Entities — which HA input_* entities back the toggles above -->
  <div class="card">
    <div class="section-title">Maintenance Entities</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      Home Assistant entity IDs that control Maintenance Mode and select the
      speaker used during maintenance. Must exist in your HA setup.
    </div>
    <div id="sysMaintForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveSystemMaint()">Save Maintenance Entities</button>
      <span id="cfg-save-result-sys-maint" class="cfg-result"></span>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Announcement Verbosity</div>
    <div class="mode-desc" style="margin-bottom:12px;">Controls how often GLaDOS adds a sarcastic follow-up comment to announcements. 0% = factual only, 100% = always adds commentary.</div>
    <div id="verbositySliders" style="opacity:0.5;">Loading...</div>
  </div>

  <div class="card">
    <div class="section-title">Startup Speakers</div>
    <div class="mode-desc" style="margin-bottom:12px;">Which speakers GLaDOS announces startup on. Checked = announces. Multiple allowed. Requires restart to apply.</div>
    <div id="startupSpeakers" style="opacity:0.5;">Loading...</div>
    <div id="startupSpeakersStatus" style="font-size:0.75rem;color:var(--orange);margin-top:6px;min-height:1.2em;"></div>
  </div>

  <div class="card">
    <div class="section-title">Display</div>
    <div class="mode-row">
      <div>
        <div class="mode-label">Eye Demo</div>
        <div class="mode-desc">Mood cycle animation on HUB75 panel</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="eyeDemoToggle" onchange="toggleEyeDemo()">
        <span class="toggle-slider"></span>
      </label>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Service Health</div>
    <div class="health-grid" id="healthGrid">
      <div class="health-item">
        <span class="health-dot unknown" id="hd-glados_api"></span>GLaDOS API
        <button class="restart-btn" onclick="restartService('glados_api')" title="Restart glados-api">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-tts"></span>TTS Engine
        <button class="restart-btn" onclick="restartService('tts')" title="Restart glados-tts">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-stt"></span>Speech-to-Text
        <button class="restart-btn" onclick="restartService('stt')" title="Restart glados-stt">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-vision"></span>Vision
        <button class="restart-btn" onclick="restartService('vision')" title="Restart glados-vision">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-ha"></span>Home Assistant
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-chromadb"></span>ChromaDB Memory
        <button class="restart-btn" onclick="restartService('chromadb')" title="Restart ChromaDB container">&#10227;</button>
      </div>
    </div>
  </div>

  <!-- Authentication & Audit — previously on Integrations; relocated 2026-04-18 -->
  <div class="card">
    <div class="section-title">Authentication &amp; Audit</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      WebUI sign-in enforcement, session timeout, and the utterance/tool
      audit trail. The password itself is set via
      <code>docker exec glados python -m glados.tools.set_password</code> —
      not shown here.
    </div>
    <div id="sysAuthAuditForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveSystemAuthAudit()">Save Authentication &amp; Audit</button>
      <span id="cfg-save-result-sys-authaudit" class="cfg-result"></span>
    </div>
  </div>

  <!-- Robot Nodes card (hidden when robots.enabled is false) -->
  <div class="card" id="robotNodesCard" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div class="section-title" style="margin-bottom:0;">Robot Nodes</div>
      <button class="btn-small" onclick="robotEmergencyStop()" style="font-size:0.8rem;padding:5px 14px;background:#e74c3c;font-weight:600;letter-spacing:0.5px;" title="Emergency stop all nodes">&#9724; E-STOP</button>
    </div>
    <div id="robotNodesList" style="margin-top:10px;font-size:0.85rem;color:var(--text-dim);">Loading...</div>
    <div style="margin-top:12px;display:flex;gap:6px;align-items:center;">
      <input type="text" id="robotNodeUrl" placeholder="http://192.168.100.x" style="flex:1;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
      <input type="text" id="robotNodeName" placeholder="Name (optional)" style="width:140px;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
      <button class="btn-small" onclick="robotAddNode()" style="font-size:0.78rem;padding:5px 12px;">Add Node</button>
    </div>
    <div id="robotBotsSection" style="margin-top:12px;display:none;">
      <div style="font-weight:500;font-size:0.82rem;margin-bottom:6px;color:var(--text);">Bots</div>
      <div id="robotBotsList" style="font-size:0.82rem;color:var(--text-dim);"></div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Weather</div>
      <button class="btn-small" id="weatherRefreshBtn" onclick="refreshWeather()" style="font-size:0.75rem;padding:4px 10px;">Refresh</button>
    </div>
    <div id="weatherPanel" style="color:var(--text-dim);font-size:0.85rem;margin-top:8px;">Loading...</div>
  </div>

  <div class="card">
    <div class="section-title">GPU Status</div>
    <div id="gpuPanel" style="font-size:0.85rem;">Loading...</div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div class="section-title" style="margin-bottom:0;">Service Logs</div>
      <div style="display:flex;gap:6px;align-items:center;">
        <select id="logServiceSelect" onchange="loadLogs()" style="background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:0.8rem;">
          <option value="glados-api">GLaDOS API</option>
          <option value="glados-tts">TTS Engine</option>
          <option value="glados-stt">Speech-to-Text</option>
          <option value="glados-vision">Vision</option>
          <option value="glados-tts-ui">WebUI</option>
          <option value="ollama-glados">Ollama GLaDOS</option>
          <option value="ollama-ipex">Ollama IPEX</option>
          <option value="ollama-vision">Ollama Vision</option>
        </select>
        <button class="btn-small" onclick="loadLogs()" style="font-size:0.75rem;padding:4px 10px;">Refresh</button>
        <button class="btn-small" onclick="clearLog()" style="font-size:0.75rem;padding:4px 10px;background:#c0392b;">Clear</button>
      </div>
    </div>
    <div id="logSizeInfo" style="font-size:0.7rem;color:var(--text-dim);margin:6px 0;"></div>
    <pre id="logPanel" style="background:#0d0d0d;border:1px solid #333;border-radius:4px;padding:10px;max-height:400px;overflow:auto;font-size:0.72rem;color:#ccc;white-space:pre-wrap;word-break:break-all;margin-top:6px;">Select a service to view logs</pre>
  </div>

  <!-- Phase 8.9 — Test harness (Advanced). External battery-scoring knobs:
       noise-entity globs the harness must ignore, and whether direction
       matching is required. Exposed publicly at
       /api/test-harness/noise-patterns for the external harness to pull. -->
  <div class="card" data-advanced="true">
    <div class="section-title">Test Harness</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      Battery-scoring knobs consumed by the external test harness
      (<code>C:\\src\\glados-test-battery\\harness.py</code>). Noise-entity globs
      list entities that flip in the background (AC displays, Sonos diagnostics,
      <code>*_button_indication</code>, <code>*_node_identify</code>) and must not
      count toward PASS. Direction-match requires the targeted entity to end in
      the expected state ('on' for "turn on", etc.), not merely "something changed."
      Harness fetches these on run-start from <code>/api/test-harness/noise-patterns</code>
      (public endpoint, no auth).
    </div>
    <div id="testHarnessForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveTestHarness()">Save Test Harness</button>
      <span id="cfg-save-result-test-harness" class="cfg-result"></span>
    </div>
  </div>
</div>
</div>
"""
