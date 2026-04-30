"""HTML for the Logging configuration tab (id="tab-config-logging").

The page lists every log group (built-in + tunable) with a per-row
Enabled toggle and Level dropdown. Bulk operations across categories
let the operator turn an entire subsystem on/off at once. A "Raw YAML"
drawer at the bottom exposes the underlying ``configs/logging.yaml``
contents for power-user direct editing.

Server-side endpoints under /api/log_groups/* (see
glados/webui/log_groups_endpoints.py) drive the data flow.
"""

HTML = r"""
<!-- ================================================================ -->
<!-- CONFIGURATION > LOGGING (Change 35 — per-group log filter)         -->
<!-- ================================================================ -->
<div id="tab-config-logging" class="tab-content">
<div class="page-shell">
<div class="container" style="position:relative;">
  <div id="loggingAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to view Logging</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title">Logging</div>
    <div class="cfg-section-desc" style="margin-bottom:12px;">
      Per-subsystem log filter. Each group below routes a specific kind of
      diagnostic log; toggle the group off or raise its level threshold to
      reduce noise, lower it (DEBUG / INFO) when investigating. Changes
      take effect immediately — no container restart. ERROR and CRITICAL
      records bypass this filter entirely; the <code>auth.audit</code>
      group is locked-on by policy.
      <br>
      <span style="color:var(--fg-tertiary);">
        Direct file path: <code>configs/logging.yaml</code> on the host.
        Global override: set <code>GLADOS_LOG_LEVEL=DEBUG</code> (or
        <code>INFO</code>, <code>SUCCESS</code>, <code>WARNING</code>) in
        the compose env to lower every group's effective floor for
        one-shot debugging.
      </span>
    </div>

    <!-- Global controls -->
    <div class="logging-toolbar">
      <label class="logging-ctrl">
        <span>Filter</span>
        <input type="text" id="loggingFilter" oninput="loggingApplyFilter()"
               placeholder="search by name or ID...">
      </label>
      <label class="logging-ctrl">
        <span>Default level (ungrouped logs)</span>
        <select id="loggingDefaultLevel" onchange="loggingSaveDefaultLevel()">
          <option value="WARNING">WARNING</option>
          <option value="SUCCESS">SUCCESS</option>
          <option value="INFO">INFO</option>
          <option value="DEBUG">DEBUG</option>
        </select>
      </label>
      <span class="logging-spacer"></span>
      <button class="btn-small" onclick="loggingBulk('enable_all')">Enable all</button>
      <button class="btn-small" onclick="loggingBulk('disable_all')">Disable all</button>
      <button class="btn-small" onclick="loggingResetDefaults()">Reset to defaults</button>
      <button class="btn-small" onclick="loggingRefresh()">Refresh</button>
    </div>

    <!-- Override banner — appears when GLADOS_LOG_LEVEL is set -->
    <div id="loggingOverrideBanner" class="logging-override-banner" style="display:none;">
      <strong>Global override active:</strong>
      every group's effective floor is currently
      <code id="loggingOverrideLevel">?</code>
      due to <code>GLADOS_LOG_LEVEL</code> in the container environment.
      Per-group toggles below are still saved but won't lower output below
      the override level until that env var is removed.
    </div>

    <!-- Status line -->
    <div id="loggingStatus" class="logging-status"></div>

    <!-- Categories + group rows -->
    <div id="loggingTable" class="logging-table">
      <div class="logging-empty">Loading…</div>
    </div>

    <!-- Raw YAML drawer -->
    <div class="logging-raw-drawer">
      <div class="logging-raw-header">
        <button class="btn-small" onclick="loggingToggleRaw()">
          <span id="loggingRawCaret">&#9656;</span>
          Raw YAML (advanced)
        </button>
        <span class="logging-raw-hint">
          Direct edit of <code>configs/logging.yaml</code>. Validated
          against the schema before being written.
        </span>
      </div>
      <div id="loggingRawBody" class="logging-raw-body" style="display:none;">
        <textarea id="loggingRawText"
                  class="logging-raw-textarea"
                  spellcheck="false"
                  placeholder="Click 'Load current YAML' to fetch the file."></textarea>
        <div class="logging-raw-actions">
          <button class="btn-small" onclick="loggingLoadRaw()">Load current YAML</button>
          <button class="btn-small" onclick="loggingSaveRaw()">Save YAML</button>
          <span id="loggingRawStatus" class="logging-raw-status"></span>
        </div>
      </div>
    </div>

  </div>
</div>
</div>
</div>
"""
