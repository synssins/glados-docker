"""HTML for the logs tab (id="tab-config-logs").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- ================================================================ -->
<!-- CONFIGURATION > LOGS (Phase 6 follow-up)                           -->
<!-- ================================================================ -->
<div id="tab-config-logs" class="tab-content">
<div class="container" style="position:relative;">
  <div id="logsAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to view Logs</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title">Logs</div>
    <div class="cfg-section-desc" style="margin-bottom:12px;">
      Read-only tail of recent log content. Choose a source and how many lines back. Toggle Auto to poll the view every 10 seconds while this tab is open.
    </div>
    <div class="logs-controls">
      <label class="logs-ctrl">
        <span>Source</span>
        <select id="logsSource" onchange="logsOnSourceChange()"></select>
      </label>
      <label class="logs-ctrl">
        <span>Lines</span>
        <select id="logsLines" onchange="logsRefresh()">
          <option value="100">100</option>
          <option value="500" selected>500</option>
          <option value="1000">1000</option>
          <option value="2000">2000</option>
          <option value="5000">5000</option>
        </select>
      </label>
      <label class="logs-ctrl">
        <span>Filter</span>
        <select id="logsFilter" onchange="logsRerender()">
          <option value="all" selected>All</option>
          <option value="warn">Warnings and errors</option>
          <option value="error">Errors only</option>
        </select>
      </label>
      <button class="btn-small" onclick="logsRefresh()">Refresh</button>
      <label class="logs-ctrl logs-auto">
        <input type="checkbox" id="logsAuto" onchange="logsToggleAuto()">
        <span>Auto-refresh (10 s)</span>
      </label>
      <span id="logsStatus" class="logs-status"></span>
    </div>
    <div id="logsSourceDesc" class="logs-source-desc"></div>
    <div class="logs-viewport">
      <pre id="logsBody" class="logs-body">Select a source and click Refresh.</pre>
    </div>
  </div>
</div>
</div>
"""
