"""HTML for the system tab (id="tab-config-system").

Phase 5.1 (2026-04-21): consolidated information architecture.
Removed the Maintenance Entities card (data-model moved to Raw YAML
only — operator sets HA entity IDs once at deploy and never edits)
and the Service Logs card (duplicate of the dedicated Logs tab).
Remaining cards grouped into three semantic zones with headings:

  Zone 1 — At a glance        (Service Health, Weather, GPU)
  Zone 2 — Mode and access    (Mode Controls, Auth and Audit)
  Zone 3 — Hardware and ops   (Eye Demo, Robot Nodes, Test Harness)

Announcement Verbosity, Startup Speakers, and Default TTS params
move to their proper homes in separate Phase 5 commits (5.2 / 5.3).
"""

HTML = r"""
<!-- ════════════════════════════════════════════════════════════════ -->
<!-- TAB 3: System Control — Phase 5.1 consolidation                  -->
<!-- ════════════════════════════════════════════════════════════════ -->
<div id="tab-config-system" class="tab-content">
<div class="container" style="position:relative;">
  <div id="controlAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access System Controls</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <!-- Phase 6.1 (2026-04-22): removed the placeholder telemetry strip
       that never got hydrated — it just said 'STATE loading…' forever.
       When live system telemetry is wired it goes back here; until
       then the page is quieter without the stub. -->

  <!-- ────────────────────────────────────────────────────────────────
       ZONE 1 — At a glance
       ──────────────────────────────────────────────────────────────── -->
  <h3 class="zone-heading">At a glance</h3>

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

  <!-- Phase 6.1 (2026-04-22): removed Weather card (moving to
       Integrations as its own tab — belongs with the external
       data sources, not on the System-health page) and GPU Status
       card (redundant — operators check GPU via nvidia-smi or
       their chosen ops tool, not this panel). -->

  <!-- ────────────────────────────────────────────────────────────────
       ZONE 2 — Mode and access
       ──────────────────────────────────────────────────────────────── -->
  <h3 class="zone-heading">Mode and access</h3>

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

  <!-- Authentication & Audit — WebUI sign-in enforcement + audit trail. -->
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

  <!-- ────────────────────────────────────────────────────────────────
       ZONE 2.5 — Services (Phase 6.2, 2026-04-22)
       ────────────────────────────────────────────────────────────────
       TTS / STT / Vision / api_wrapper endpoints, formerly on the
       LLM & Services page. LLM (Ollama) moved to Integrations in
       the same commit. Hydrated by loadSystemServices() from the
       config.system activation hook. -->
  <h3 class="zone-heading">Services</h3>

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

  <!-- ────────────────────────────────────────────────────────────────
       ZONE 3 — Hardware and ops
       ──────────────────────────────────────────────────────────────── -->
  <h3 class="zone-heading">Hardware and ops</h3>

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

  <!-- ────────────────────────────────────────────────────────────────
       ZONE 4 — Maintenance
       ────────────────────────────────────────────────────────────────
       Phase 6.1 (2026-04-22): Reload from Disk + Audio Storage moved
       here from the shared Configuration shell, where they used to
       clutter every sub-page. They belong with System ops because
       both are 'cleanup / administration' actions — re-reading
       YAMLs from disk and pruning cached audio files. -->
  <h3 class="zone-heading">Maintenance</h3>

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

  <div class="card">
    <div class="section-title">Audio storage</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      Files generated by TTS, chat, and autonomous announcements.
      Each directory can be cleared independently — empty rarely-used
      ones to keep the bind mount manageable.
    </div>
    <div id="audioStatsPanel" style="font-size:0.85rem;color:var(--text-dim);">Loading...</div>
  </div>

  <!-- Test harness — Advanced. Battery-scoring knobs for the
       external harness at C:\src\glados-test-battery\harness.py. -->
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
