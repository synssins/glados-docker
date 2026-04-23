"""HTML for the system tab (id="tab-config-system").

Phase 6.5.3 (2026-04-22): System page converted to the shared
page-tabs chrome. Zones became tabs:

  Status       — service health
  Mode         — mode controls + auth & audit
  Services     — TTS/STT/Vision/api_wrapper endpoints
  Hardware     — display (eye demo) + robot nodes + test harness (adv)
  Maintenance  — reload from disk + audio storage

Page-save button at top-right dispatches to the relevant per-tab
save handler (Mode → _cfgSaveSystemAuthAudit, Services →
_cfgSaveSystemServices, Hardware → cfgSaveTestHarness). Status and
Maintenance tabs have no direct save — their actions (restart,
reload, clear) fire inline.
"""

HTML = r"""
<!-- ════════════════════════════════════════════════════════════════ -->
<!-- TAB 3: System Control — Phase 6.5.3 page-tabs conversion          -->
<!-- ════════════════════════════════════════════════════════════════ -->
<div id="tab-config-system" class="tab-content">
<div class="container" style="position:relative;">
  <div id="controlAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access System Controls</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="page-header">
    <div>
      <h2 class="page-title">System</h2>
      <div class="page-title-desc">Operational state, services, hardware, and maintenance.</div>
    </div>
    <button class="page-save-btn" onclick="_cfgSaveCurrentSystemTab()" title="Save the active tab">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="width:16px;height:16px;stroke-width:2;">
        <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
        <polyline points="17 21 17 13 7 13 7 21"/>
        <polyline points="7 3 7 8 15 8"/>
      </svg>
      <span>Save</span>
    </button>
  </div>

  <nav class="page-tabs" role="tablist">
    <button class="page-tab active" role="tab" data-page-tab-group="system" data-tab="status"
            onclick="showPageTab('system','status')">Status</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="mode"
            onclick="showPageTab('system','mode')">Mode</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="services"
            onclick="showPageTab('system','services')">Services</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="hardware"
            onclick="showPageTab('system','hardware')">Hardware</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="maintenance"
            onclick="showPageTab('system','maintenance')">Maintenance</button>
  </nav>

  <div class="page-tab-panels">

    <!-- ════════════════ Status tab ════════════════ -->
    <div class="page-tab-panel active" data-page-tab-panel-group="system" data-tab="status">
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
    </div>

    <!-- ════════════════ Mode tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="mode">
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

      <div class="card" style="margin-top:var(--sp-3);">
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
    </div>

    <!-- ════════════════ Services tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="services">
      <div class="card">
        <div class="section-title">Service endpoints</div>
        <div class="mode-desc" style="margin-bottom:10px;">
          Non-LLM backends GLaDOS calls: TTS (Speaches), STT (Faster-Whisper),
          Vision, and the local api-wrapper. URL + health + discovery per service.
          LLM / Ollama config lives under <em>Integrations &rsquo;LLM&rsquo;</em>.
        </div>
        <div id="system-services-body">Loading services&hellip;</div>
        <div class="cfg-save-row">
          <button class="cfg-save-btn" onclick="_cfgSaveSystemServices()">Save Services</button>
          <span id="cfg-save-result-system-services" class="cfg-result"></span>
        </div>
      </div>
    </div>

    <!-- ════════════════ Hardware tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="hardware">
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

      <div class="card" id="robotNodesCard" style="display:none;margin-top:var(--sp-3);">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          <div class="section-title" style="margin-bottom:0;">Robot Nodes</div>
          <button class="btn-small" onclick="robotEmergencyStop()" style="font-size:0.8rem;padding:5px 14px;background:#e74c3c;font-weight:600;letter-spacing:0.5px;" title="Emergency stop all nodes">&#9724; E-STOP</button>
        </div>
        <div id="robotNodesList" style="margin-top:10px;font-size:0.85rem;color:var(--text-dim);">Loading...</div>
        <div style="margin-top:12px;display:flex;gap:6px;align-items:center;">
          <input type="text" id="robotNodeUrl" placeholder="http://robot.local" style="flex:1;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
          <input type="text" id="robotNodeName" placeholder="Name (optional)" style="width:140px;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
          <button class="btn-small" onclick="robotAddNode()" style="font-size:0.78rem;padding:5px 12px;">Add Node</button>
        </div>
        <div id="robotBotsSection" style="margin-top:12px;display:none;">
          <div style="font-weight:500;font-size:0.82rem;margin-bottom:6px;color:var(--text);">Bots</div>
          <div id="robotBotsList" style="font-size:0.82rem;color:var(--text-dim);"></div>
        </div>
      </div>

      <div class="card" data-advanced="true" style="margin-top:var(--sp-3);">
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

    <!-- ════════════════ Maintenance tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="maintenance">
      <div class="card">
        <div class="section-title">Reload configuration from disk</div>
        <div class="mode-desc" style="margin-bottom:10px;">
          Re-reads every YAML under <code>configs/</code> without restarting the container.
          Use this after editing a YAML file directly on the host, or after changes
          from another session that haven&rsquo;t been picked up yet.
        </div>
        <div style="display:flex;gap:12px;align-items:center;">
          <button class="btn" onclick="cfgReload()" style="background:#555;">Reload from Disk</button>
          <span id="cfg-status" style="color:var(--text-dim);font-size:0.85em;"></span>
        </div>
      </div>

      <div class="card" style="margin-top:var(--sp-3);">
        <div class="section-title">Audio storage</div>
        <div class="mode-desc" style="margin-bottom:10px;">
          Files generated by TTS, chat, and autonomous announcements.
          Each directory can be cleared independently &mdash; empty rarely-used
          ones to keep the bind mount manageable.
        </div>
        <div id="audioStatsPanel" style="font-size:0.85rem;color:var(--text-dim);">Loading...</div>
      </div>
    </div>

  </div>
</div>
</div>
"""
