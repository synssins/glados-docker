"""HTML for the integrations tab (id="tab-config").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 4: Configuration                                           -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-config" class="tab-content">
<div class="container" style="position:relative;">
  <div id="configAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Configuration</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title" id="cfg-section-label">Configuration</div>
    <!-- In-page tab strip removed in Phase 5; the sidebar Configuration
         submenu drives which section is rendered into cfg-form-area. -->

    <div class="advanced-toggle-row">
      <label>
        <input type="checkbox" id="advancedToggle" onchange="toggleAdvanced()">
        Show Advanced Settings
      </label>
    </div>

    <!-- Form sections (generated dynamically) -->
    <div id="cfg-form-area" style="min-height:200px;">
      <div style="color:var(--text-dim);padding:20px;text-align:center;">Select a section or loading...</div>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div style="display:flex;gap:12px;align-items:center;">
      <button class="btn" onclick="cfgReload()" style="background:#555;">Reload from Disk</button>
      <span id="cfg-status" style="color:var(--text-dim);font-size:0.85em;"></span>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="section-title">Audio Storage</div>
    <div id="audioStatsPanel" style="font-size:0.85rem;color:var(--text-dim);">Loading...</div>
  </div>

</div>
</div>
"""
