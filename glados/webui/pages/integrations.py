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

  <!-- Phase 6.1 (2026-04-22): 'Reload from Disk' and 'Audio Storage'
       cards moved to the System page's Maintenance zone. They used
       to live in this shared Configuration shell so they appeared
       below every single sub-page (Integrations / LLM / Audio /
       Personality / etc) — pure chrome clutter on 90% of views.
       Now they show up only where an operator would go to actually
       do maintenance. -->

</div>
</div>
"""
