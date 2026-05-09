"""HTML for the system tab (id="tab-config-system").

Phase 6.5.3 (2026-04-22): System page converted to the shared
page-tabs chrome. Zones became tabs:

  Status       — service health
  Mode         — mode controls + auth & audit
  Services     — TTS/STT/Vision/api_wrapper endpoints
  Hardware     — display (eye demo) + robot nodes
  Maintenance  — reload from disk + audio storage

Page-save button at top-right dispatches to the relevant per-tab
save handler (Mode → _cfgSaveSystemAuthAudit, Services →
_cfgSaveSystemServices). Status, Hardware, and Maintenance tabs
have no direct save — their actions (restart, reload, clear) fire inline.

Phase 6 / Approach 2 sweep (2026-05-09): inline-style attributes
replaced with v3 utility classes. `style="display:none;"` on
JS-toggled elements kept (initial-hide state per inline-style policy).
Phantom-var references (--text-dim, --text, --border) eliminated by
swapping to utility classes that resolve via the canonical v2
tokens.
"""

HTML = r"""
<!-- ════════════════════════════════════════════════════════════════ -->
<!-- TAB 3: System Control — Phase 6.5.3 page-tabs conversion          -->
<!-- ════════════════════════════════════════════════════════════════ -->
<div id="tab-config-system" class="tab-content">
<div class="page-shell">
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
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="time"
            onclick="showPageTab('system','time');_loadTimeStatus()">Time</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="account"
            onclick="showPageTab('system','account')">Account</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="ssl"
            onclick="showPageTab('system','ssl');_loadSslIntoSystemTab()">SSL</button>
    <button class="page-tab" role="tab" data-page-tab-group="system" data-tab="users"
            onclick="showPageTab('system','users');_loadUsersIntoSystemTab()">Users</button>
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
          <label class="fs-base txt-dim">Speaker:</label>
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

      <div class="card mt-3">
        <div class="section-title">Authentication &amp; Audit</div>
        <div class="mode-desc mb-2">
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
      <div id="system-services-body">Loading services&hellip;</div>
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

      <div class="card mt-3" id="robotNodesCard" style="display:none;">
        <div class="row between wrap gap-2">
          <div class="section-title mb-0">Robot Nodes</div>
          <button class="btn btn-danger" onclick="robotEmergencyStop()" style="font-size:0.8rem;padding:5px 14px;font-weight:600;letter-spacing:0.5px;" title="Emergency stop all nodes">&#9724; E-STOP</button>
        </div>
        <div id="robotNodesList" class="mt-2 fs-base txt-dim">Loading...</div>
        <div class="row gap-1 mt-3">
          <input type="text" id="robotNodeUrl" placeholder="http://robot.local" class="cfg-inline-input fs-sm" style="flex:1;">
          <input type="text" id="robotNodeName" placeholder="Name (optional)" class="cfg-inline-input fs-sm" style="width:140px;">
          <button class="btn-small fs-sm" onclick="robotAddNode()" style="padding:5px 12px;">Add Node</button>
        </div>
        <div id="robotBotsSection" class="mt-3" style="display:none;">
          <div class="fs-sm mb-1 txt-primary" style="font-weight:500;">Bots</div>
          <div id="robotBotsList" class="fs-sm txt-dim"></div>
        </div>
      </div>

    </div>

    <!-- ════════════════ Maintenance tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="maintenance">
      <div class="card">
        <div class="section-title">Reload configuration from disk</div>
        <div class="mode-desc mb-2">
          Re-reads every YAML under <code>configs/</code> without restarting the container.
          Use this after editing a YAML file directly on the host, or after changes
          from another session that haven&rsquo;t been picked up yet.
        </div>
        <div class="row gap-3">
          <button class="btn btn-primary" onclick="cfgReload()">Reload from Disk</button>
          <span id="cfg-status" class="txt-dim fs-base"></span>
        </div>
      </div>

      <div class="card mt-3">
        <div class="section-title">Audio storage</div>
        <div class="mode-desc mb-2">
          Files generated by TTS, chat, and autonomous announcements.
          Each directory can be cleared independently &mdash; empty rarely-used
          ones to keep the bind mount manageable.
        </div>
        <div id="audioStatsPanel" class="fs-base txt-dim">Loading...</div>
      </div>
    </div>

    <!-- ════════════════ Time tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="time">
      <div class="card">
        <div class="section-title">Sync Status</div>
        <div class="mode-desc mb-2">
          GLaDOS syncs an offset against an NTP server at startup and on the
          configured refresh interval, so the wall-clock time injected into
          chat is independent of the container's system clock. Timezone is
          read from the IANA zone resolved by the operator-configured
          weather coordinates (or the manual override below).
        </div>
        <div id="timeStatusGrid" class="fs-base txt-dim">Loading&hellip;</div>
        <div class="mt-2">
          <button class="btn-small fs-sm" onclick="_loadTimeStatus()" style="padding:5px 12px;">Refresh</button>
        </div>
      </div>

      <div class="card mt-3">
        <div class="section-title">Configuration</div>
        <div class="mode-desc mb-2">
          Changes save to <code>configs/global.yaml</code> and apply on the
          next container restart.
        </div>
        <div id="sysTimeForm"></div>
        <div class="cfg-save-row">
          <button class="cfg-save-btn" onclick="cfgSaveSystemTime()">Save Time Configuration</button>
          <span id="cfg-save-result-sys-time" class="cfg-result"></span>
        </div>
      </div>
    </div>

    <!-- ════════════════ Account tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="account">

      <div class="card">
        <div class="section-title">Change Password</div>
        <div class="mode-desc mb-2">
          Update your own password. You must supply your current password to confirm.
        </div>
        <div class="col gap-2" style="max-width:360px;">
          <input type="password" id="cpwCurrent" placeholder="Current password" class="cfg-inline-input">
          <input type="password" id="cpwNew" placeholder="New password (8+ characters)" class="cfg-inline-input">
          <input type="password" id="cpwConfirm" placeholder="Confirm new password" class="cfg-inline-input">
          <div class="row gap-3">
            <button class="btn btn-primary" onclick="_submitChangePassword()" style="padding:6px 16px;font-size:0.85rem;">Change Password</button>
            <span id="cpwResult" class="fs-sm"></span>
          </div>
        </div>
      </div>

    </div>

    <!-- ════════════════ SSL tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="ssl">
      <div id="systemSslMount"></div>
    </div>

    <!-- ════════════════ Users tab ════════════════ -->
    <div class="page-tab-panel" data-page-tab-panel-group="system" data-tab="users">
      <div id="systemUsersMount"></div>
    </div>

  </div>
</div>
</div>
</div>

<script>
(function () {
  function _escSession(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function _cpwResult(msg, ok) {
    var el = document.getElementById('cpwResult');
    if (!el) return;
    el.textContent = msg;
    // Phase 6 sweep: --success/--danger don't exist; --green/--red do.
    el.style.color = ok ? 'var(--green)' : 'var(--red)';
  }

  window._submitChangePassword = function () {
    var cur = (document.getElementById('cpwCurrent') || {}).value || '';
    var nw  = (document.getElementById('cpwNew')     || {}).value || '';
    var cfm = (document.getElementById('cpwConfirm') || {}).value || '';
    if (!cur || !nw || !cfm) { _cpwResult('All fields are required.', false); return; }
    if (nw !== cfm)          { _cpwResult('New passwords do not match.', false); return; }
    fetch('/api/auth/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({'current': cur, 'new': nw}),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) {
        _cpwResult('Password updated.', true);
        document.getElementById('cpwCurrent').value = '';
        document.getElementById('cpwNew').value = '';
        document.getElementById('cpwConfirm').value = '';
      } else {
        _cpwResult(d.error || 'Failed.', false);
      }
    }).catch(function () { _cpwResult('Request failed.', false); });
  };

  window._loadSessions = function () {
    var el = document.getElementById('sessionsTable');
    if (!el) return;
    el.textContent = 'Loading…';
    fetch('/api/sessions').then(function (r) { return r.json(); }).then(function (d) {
      var rows = d.sessions || [];
      if (!rows.length) { el.textContent = 'No active sessions.'; return; }
      var html = '<table class="system-session-table">'
        + '<thead><tr><th>User</th><th>Created</th><th>Last used</th><th>IP</th><th></th></tr></thead><tbody>';
      rows.forEach(function (s) {
        var created  = s.created_at  ? new Date(s.created_at  * 1000).toLocaleString() : '—';
        var lastUsed = s.last_used_at ? new Date(s.last_used_at * 1000).toLocaleString() : '—';
        html += '<tr>'
          + '<td>' + _escSession(s.username) + '</td>'
          + '<td>' + created + '</td>'
          + '<td>' + lastUsed + '</td>'
          + '<td>' + _escSession(s.remote_addr || '—') + '</td>'
          + '<td>'
          + '<button class="btn btn-danger" style="font-size:0.75rem;padding:3px 10px;" '
          + 'data-revoke-sid="' + _escSession(s.session_id) + '">Revoke</button>'
          + '</td></tr>';
      });
      html += '</tbody></table>';
      el.innerHTML = html;
      el.querySelectorAll('[data-revoke-sid]').forEach(function (b) {
        b.addEventListener('click', function () {
          _revokeSession(b.getAttribute('data-revoke-sid'));
        });
      });
    }).catch(function () { el.textContent = 'Failed to load sessions.'; });
  };

  window._revokeSession = async function (sid) {
    if (!confirm('Revoke this session?')) return;
    const r = await fetch('/api/sessions/' + encodeURIComponent(sid), {method: 'DELETE'});
    if (r.status === 401) {
      window.location = '/login';
      return;
    }
    if (!r.ok) {
      alert('Revoke failed: ' + r.status);
      return;
    }
    _loadSessions();
  };
})();
</script>
"""
