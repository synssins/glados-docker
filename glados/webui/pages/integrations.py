"""HTML for the integrations tab (id="tab-config").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.

Phase 6 / Approach 2 follow-up (2026-05-09): page chrome normalised
to match the System page pattern. The outer ``<div class="card">``
that wrapped the entire content (causing the "background block"
operator-flagged inconsistency vs System) is removed; the Configuration
title moves into a ``page-header`` block, and the JS-rendered form +
sub-tabs flow naturally without an enclosing card. Sub-content
``.card`` panels (rendered by ui.js's cfgRender* functions) carry
their own visual containment, same as System's tab panels.
"""

HTML = r"""
<!-- TAB 4: Configuration -->
<div id="tab-config" class="tab-content">
<div class="page-shell">
<div class="container" style="position:relative;">
  <div id="configAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Configuration</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="page-header">
    <div>
      <h2 class="page-title" id="cfg-section-label">Configuration</h2>
      <div class="page-title-desc" id="cfg-section-desc">Service endpoints and integration settings.</div>
    </div>
  </div>

  <div class="advanced-toggle-row">
    <label>
      <input type="checkbox" id="advancedToggle" onchange="toggleAdvanced()">
      Show Advanced Settings
    </label>
  </div>

  <!-- Form sections (generated dynamically by cfgRender* in ui.js) -->
  <div id="cfg-form-area" style="min-height:200px;">
    <div class="txt-dim" style="padding:20px;text-align:center;">Select a section or loading...</div>
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
</div>
"""
