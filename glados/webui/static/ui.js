/* GLaDOS WebUI JavaScript.
 * Extracted from glados/webui/tts_ui.py (Phase 2 of WebUI refactor,
 * 2026-04-21). Served by GLaDOSHandler._serve_static at /static/ui.js.
 * Depends on Chart.js (loaded before this script via CDN <script>).
 */
/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Authentication & Auth Gating
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _isAuthenticated = false;
let _currentRole = null;
let _currentUsername = null;

async function checkAuth() {
  try {
    const r = await fetch('/api/auth/status');
    const data = await r.json();
    _isAuthenticated = data.authenticated === true;
    _currentRole = data.role || null;
    _currentUsername = (data.user && data.user.username) || null;
  } catch(e) {
    _isAuthenticated = false;
    _currentRole = null;
    _currentUsername = null;
  }
  updateAuthUI();
}

function updateAuthUI() {
  const isAdmin = _currentRole === 'admin';

  // Show/hide admin-only sidebar items
  document.querySelectorAll('[data-requires-admin]').forEach(el => {
    el.style.display = isAdmin ? '' : 'none';
  });

  // Show/hide chat-only sidebar items (logged-in non-admin users)
  const isChatUser = _isAuthenticated && !isAdmin;
  document.querySelectorAll('[data-chat-only]').forEach(el => {
    el.style.display = isChatUser ? '' : 'none';
  });

  // Show/hide auth-required sidebar items (visible only when authenticated)
  document.querySelectorAll('[data-requires-auth]').forEach(el => {
    el.style.display = _isAuthenticated ? '' : 'none';
  });

  // Update lock icons (legacy — no longer used for config items but kept for other uses)
  const locks = document.querySelectorAll('.lock-icon');
  locks.forEach(l => {
    l.textContent = _isAuthenticated ? '' : '\u{1F512}';
  });

  // Show/hide auth overlays
  const controlOverlay = document.getElementById('controlAuthOverlay');
  const configOverlay = document.getElementById('configAuthOverlay');
  if (controlOverlay) controlOverlay.style.display = _isAuthenticated ? 'none' : 'flex';
  if (configOverlay) configOverlay.style.display = _isAuthenticated ? 'none' : 'flex';

  // Bottom-left account block
  const accountBlock = document.getElementById('sidebarAccount');
  const signInBlock = document.getElementById('sidebarSignIn');

  if (_isAuthenticated) {
    if (accountBlock) {
      accountBlock.style.display = 'block';
      const nameEl = document.getElementById('sidebarUsername');
      const roleEl = document.getElementById('sidebarRole');
      if (nameEl) nameEl.textContent = _currentUsername || '';
      if (roleEl) roleEl.textContent = _currentRole ? '(' + _currentRole + ')' : '';
    }
    if (signInBlock) signInBlock.style.display = 'none';
  } else {
    if (accountBlock) accountBlock.style.display = 'none';
    if (signInBlock) signInBlock.style.display = '';
  }
}

// Account dropdown helpers
function toggleAccountMenu(ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  const m = document.getElementById('accountMenu');
  if (!m) return;
  if (m.hasAttribute('hidden')) {
    m.removeAttribute('hidden');
    setTimeout(() => document.addEventListener('click', closeAccountMenuOnOutside), 0);
  } else {
    closeAccountMenu();
  }
}
function closeAccountMenu() {
  const m = document.getElementById('accountMenu');
  if (m) m.setAttribute('hidden', '');
  document.removeEventListener('click', closeAccountMenuOnOutside);
}
function closeAccountMenuOnOutside(ev) {
  const m = document.getElementById('accountMenu');
  const t = document.querySelector('.account-trigger');
  if (m && !m.contains(ev.target) && t && !t.contains(ev.target)) closeAccountMenu();
}

// Stackable toast system (Phase 5). Multiple toasts can be on screen
// simultaneously; each auto-dismisses after 4 s. Fade-in/out via CSS.
function showToast(msg, type) {
  const stack = document.getElementById('toastStack');
  if (!stack) return;  // safety: older fragments that haven't mounted yet
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'success');
  el.textContent = msg;
  stack.appendChild(el);
  // Force reflow so the transition runs from opacity:0.
  requestAnimationFrame(() => { el.classList.add('visible'); });
  setTimeout(() => {
    el.classList.remove('visible');
    // Remove after transition finishes so stack doesn't grow forever.
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  }, 4000);
}

// Phase 5: engine status in sidebar header. Polls /api/health/aggregate every
// 30 s when the page is visible; skips polling when tab is hidden so we don't
// wake the container unnecessarily.
let _engineStatusTimer = null;
async function pollEngineStatus() {
  const dot = document.getElementById('engineStatusDot');
  if (!dot) return;
  // Hide on /setup
  if (window.location.pathname.startsWith('/setup')) {
    dot.style.display = 'none';
    return;
  }
  try {
    const r = await fetch('/api/health/aggregate', { credentials: 'same-origin' });
    const data = await r.json();
    dot.classList.remove('running', 'degraded', 'stopping', 'unauth');
    const cls = ({
      ok: 'running', degraded: 'degraded', down: 'stopping', unauth: 'unauth',
    })[data.overall] || 'unauth';
    dot.classList.add(cls);
    if (data.services) {
      dot.title = data.services.map(s => `${s.name}: ${s.status}`).join('\n');
    } else {
      dot.title = 'Sign in to see service health';
    }
  } catch (e) {
    dot.classList.remove('running', 'degraded', 'stopping');
    dot.classList.add('unauth');
    dot.title = 'Service status unknown';
  }
}
function startEngineStatusPoll() {
  if (_engineStatusTimer) clearInterval(_engineStatusTimer);
  pollEngineStatus();
  _engineStatusTimer = setInterval(() => {
    if (document.hidden) return;
    pollEngineStatus();
  }, 30000);
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollEngineStatus();
});
// Kicked off after first checkAuth() resolves (see Shared utilities block).

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Advanced Mode Toggle
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

function toggleAdvanced() {
  const on = document.getElementById('advancedToggle').checked;
  document.body.classList.toggle('show-advanced', on);
  try { localStorage.setItem('glados_advanced', on ? '1' : '0'); } catch(e) {}
}

(function restoreAdvanced() {
  try {
    if (localStorage.getItem('glados_advanced') === '1') {
      document.getElementById('advancedToggle').checked = true;
      document.body.classList.add('show-advanced');
    }
  } catch(e) {}
})();

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Field Metadata Registry
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const FIELD_META = {
  // â”€â”€ Global: Home Assistant â”€â”€
  'home_assistant.url':    { label: 'Home Assistant URL', desc: 'Base URL of your Home Assistant instance' },
  'home_assistant.ws_url': { label: 'WebSocket URL', desc: 'WebSocket endpoint for real-time HA events', advanced: true },
  'home_assistant.token':  { label: 'API Token', desc: 'Long-lived access token for HA', type: 'password' },
  // â”€â”€ Global: Network â”€â”€ (Phase 6: hidden — env-driven, YAML edit is inert)
  'network.serve_host':    { label: 'Server Host', desc: 'env-driven; edit via SERVE_HOST', hidden: true },
  'network.serve_port':    { label: 'Server Port', desc: 'env-driven; edit via SERVE_PORT', hidden: true },
  // â”€â”€ Global: Paths â”€â”€ (Phase 6: all hidden — env-driven, no WebUI-writable effect)
  'paths.glados_root':     { label: 'GLaDOS Root Path', desc: 'env-driven; edit via GLADOS_ROOT', hidden: true },
  'paths.audio_base':      { label: 'Audio Base Path', desc: 'env-driven; edit via GLADOS_AUDIO', hidden: true },
  'paths.logs':            { label: 'Logs Path', desc: 'env-driven; edit via GLADOS_LOGS', hidden: true },
  'paths.data':            { label: 'Data Path', desc: 'env-driven; edit via GLADOS_DATA', hidden: true },
  'paths.assets':          { label: 'Assets Path', desc: 'env-driven; edit via GLADOS_ASSETS', hidden: true },
  // â”€â”€ Global: SSL â”€â”€
  // SSL fields are edited on the dedicated Configuration > SSL page
  // (cfgRenderSsl). They were previously duplicated here via FIELD_META
  // auto-rendering which produced two conflicting forms for the same
  // settings. Phase 5 removed the duplicates; the SSL page is the
  // single source of truth for ssl.*.
  // â”€â”€ Global: Auth â”€â”€ (Phase 6: all advanced — operators rarely touch this after initial setup)
  'auth.enabled':          { label: 'Authentication Enabled', desc: 'Require login to access System and Config' },
  'auth.password_hash':    { label: 'Password Hash', desc: 'Bcrypt hash (use set_password tool to change)', advanced: true, type: 'password' },
  'auth.session_secret':   { label: 'Session Secret', desc: 'Secret key for session tokens', advanced: true, type: 'password' },
  'auth.session_timeout_hours': { label: 'Session Timeout (hours)', desc: 'How long before a session expires' },
  // â”€â”€ Global: Mode Entities â”€â”€
  'mode_entities.maintenance_mode':    { label: 'Maintenance Mode Entity', desc: 'HA entity for maintenance mode' },
  'mode_entities.maintenance_speaker': { label: 'Maintenance Speaker Entity', desc: 'HA entity for maintenance speaker selection' },
  'mode_entities.silent_mode':         { label: 'Silent Mode Entity', desc: 'HA entity for silent mode', advanced: true },
  'mode_entities.dnd':                 { label: 'Do Not Disturb Entity', desc: 'HA entity for manual DND toggle', advanced: true },
  // â”€â”€ Global: Silent Hours â”€â”€
  'silent_hours.enabled':  { label: 'Silent Hours Enabled', desc: 'Drop low-priority alerts during the quiet window' },
  'silent_hours.start':    { label: 'Start Time', desc: 'When silent hours begin', options: [
    '00:00','01:00','02:00','03:00','04:00','05:00','06:00','07:00','08:00','09:00','10:00','11:00',
    '12:00','13:00','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00','22:00','23:00'] },
  'silent_hours.end':      { label: 'End Time', desc: 'When silent hours end', options: [
    '00:00','01:00','02:00','03:00','04:00','05:00','06:00','07:00','08:00','09:00','10:00','11:00',
    '12:00','13:00','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00','22:00','23:00'] },
  'silent_hours.min_tier': { label: 'Minimum Tier to Play', desc: 'Alerts below this tier are suppressed during silent hours', options: ['AMBIENT','LOW','MEDIUM','HIGH','CRITICAL'] },
  // â”€â”€ Global: Audit â”€â”€ (Phase 6: hidden — deprecated / no WebUI-writable effect)
  'audit.enabled':                 { label: 'Audit Log Enabled', desc: 'Write utterance/tool audit trail to JSONL' },
  'audit.path':                    { label: 'Audit Log Path', desc: 'env-driven via GLADOS_LOGS', hidden: true },
  'audit.retention_days':          { label: 'Audit Retention (days)', desc: 'rotation not implemented', hidden: true },
  // â”€â”€ Global: Weather â”€â”€ (Phase 6: unit fields hidden — UI preference, not backend)
  'weather.latitude':              { label: 'Weather Latitude', desc: 'Used when auto_from_ha is false' },
  'weather.longitude':             { label: 'Weather Longitude', desc: 'Used when auto_from_ha is false' },
  'weather.auto_from_ha':          { label: 'Auto-read Weather from HA', desc: 'Read lat/long from your HA configuration' },
  'weather.temperature_unit':      { label: 'Temperature Unit', desc: 'display preference', hidden: true },
  'weather.wind_speed_unit':       { label: 'Wind Speed Unit', desc: 'display preference', hidden: true },
  // â”€â”€ Global: Tuning â”€â”€
  'tuning.llm_connect_timeout_s':  { label: 'LLM Connect Timeout (s)', desc: 'Seconds to wait for LLM connection', advanced: true },
  'tuning.llm_read_timeout_s':     { label: 'LLM Read Timeout (s)', desc: 'Max seconds to wait for LLM response', advanced: true },
  'tuning.tts_flush_chars':        { label: 'TTS Flush Threshold', desc: 'Characters to buffer before sending to TTS', advanced: true },
  'tuning.engine_pause_time':      { label: 'Engine Pause Time (s)', desc: 'Pause between engine loop iterations', advanced: true },
  'tuning.mode_cache_ttl_s':       { label: 'Mode Cache TTL (s)', desc: 'Seconds to cache HA mode entity states', advanced: true },
  'tuning.engine_audio_default':   { label: 'Engine Audio Default', desc: 'no code consumers', hidden: true },
  // â”€â”€ Audio â”€â”€
  // Phase 6: audio directory paths are hidden — editing them via the UI
  // can't create the destination folder, so changes either silently do
  // nothing (path exists) or break playback (path doesn't exist).
  // Advanced file-count caps stay behind the Advanced toggle.
  'ha_output_dir':         { label: 'HA Output Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'archive_dir':           { label: 'Archive Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'archive_max_files':     { label: 'Max Archive Files', desc: 'Maximum files to keep in the archive', advanced: true },
  'tts_ui_output_dir':     { label: 'TTS UI Output', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'tts_ui_max_files':      { label: 'Max TTS UI Files', desc: 'Maximum generated files to keep', advanced: true },
  'chat_audio_dir':        { label: 'Chat Audio Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'chat_audio_max_files':  { label: 'Max Chat Audio Files', desc: 'Maximum chat audio files to keep', advanced: true },
  'announcements_dir':     { label: 'Announcements Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'commands_dir':          { label: 'Commands Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'silence_between_sentences_ms': { label: 'Silence Between Sentences (ms)', desc: 'Milliseconds of silence inserted between sentences' },
  'sample_rate':           { label: 'Sample Rate (Hz)', desc: 'Audio sample rate for WAV output', advanced: true },
  // â”€â”€ Speakers â”€â”€
  'default':               { label: 'Default Speaker', desc: 'Default HA media player for audio output' },
  'available':             { label: 'Available Speakers', desc: 'Comma-separated list of available speaker entity IDs' },
  'blacklist':             { label: 'Blocked Speakers', desc: 'Speakers to exclude from selection' },
  // â”€â”€ Personality: default_tts â”€â”€
  'default_tts.length_scale': { label: 'Default Length Scale', desc: 'Speech duration (higher = slower)', advanced: true },
  'default_tts.noise_scale':  { label: 'Default Noise Scale', desc: 'Phoneme variation', advanced: true },
  'default_tts.noise_w':      { label: 'Default Noise W', desc: 'Duration variation', advanced: true },
  // â”€â”€ Robots â”€â”€
  'enabled':                       { label: 'Robots Enabled', desc: 'Master enable for the robot subsystem' },
  'health_poll_interval_s':        { label: 'Health Poll Interval (s)', desc: 'How often to poll node health endpoints' },
  'request_timeout_s':             { label: 'Request Timeout (s)', desc: 'HTTP timeout for robot node API calls' },
  'emergency_stop_timeout_s':      { label: 'E-Stop Timeout (s)', desc: 'Shorter timeout for emergency stop (must be fast)' },
  'auth_token':                    { label: 'Global Auth Token', desc: 'Bearer token sent with all API requests (except e-stop)', type: 'password' },
};

const SECTION_META = {
  // Phase 6 page names (operators see these titles in the sidebar).
  integrations:     { title: 'Integrations', desc: 'Home Assistant, MQTT, and media-stack integrations (MQTT + *arr/Plex arrive in later phases)' },
  'audio-speakers': { title: 'Audio & Speakers', desc: 'HA media players and speech synthesis parameters' },
  personality:      { title: 'Personality', desc: 'Attitudes, TTS defaults, HEXACO traits, and emotion model' },
  memory:           { title: 'Memory', desc: 'ChromaDB retention, passive-fact defaults, and the review queue' },
  ssl:              { title: 'SSL', desc: 'HTTPS certificates — Let\'s Encrypt (DNS-01) or manual upload' },
  raw:              { title: 'Raw YAML', desc: 'Edit configuration files directly as YAML' },
  // Legacy section metas kept as defensive fallback — navigateTo() migrates
  // legacy nav keys to their Phase 6 equivalents, but direct cfgRenderSection
  // calls from elsewhere (error paths, older browser tabs, etc.) still find
  // a title instead of rendering the bare key.
  global:           { title: 'Integrations', desc: 'Home Assistant connection and related integration settings' },
  services:         { title: 'LLM & Services', desc: 'Service endpoint URLs and health' },
  speakers:         { title: 'Audio & Speakers', desc: 'Home Assistant media player configuration' },
  audio:            { title: 'Audio & Speakers', desc: 'Audio file paths, limits, and synthesis parameters' },
  robots:           { title: 'Robots', desc: 'Robot node integration — ESP32 nodes, bots, and emergency stop' },
};

const SERVICE_NAMES = {
  tts: 'TTS Engine',
  stt: 'Speech-to-Text',
  api_wrapper: 'API Wrapper',
  vision: 'Vision Service',
  llm_interactive: 'LLM (Interactive)',
  llm_autonomy: 'LLM (Autonomy)',
  llm_triage: 'LLM (Triage)',
  llm_vision: 'LLM (Vision)',
};

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   TAB 4: Configuration Manager
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _cfgData = {};
let _cfgRaw = {};
let _cfgCurrentSection = 'global';
let _cfgCurrentRawFile = 'global';

async function cfgLoadAll() {
  try {
    const r = await fetch('/api/config');
    if (r.status === 401) return;
    _cfgData = await r.json();
  } catch(e) { console.error('Config load failed:', e); }
}

async function cfgLoadRaw() {
  try {
    const r = await fetch('/api/config/raw');
    if (r.ok) _cfgRaw = await r.json();
  } catch(e) { console.error('Raw config load failed:', e); }
}

function cfgSwitchSection(name, btn) {
  document.querySelectorAll('.cfg-tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  _cfgCurrentSection = name;
  if (name === 'raw') {
    cfgLoadRaw().then(() => cfgRenderRaw());
  } else {
    cfgRenderSection(name);
  }
}

// Phase 6: virtual pages map onto existing backing sections for data
// access + save semantics. Field IDs use the backing name so
// cfgCollectForm / cfgSaveSection keep working unchanged.
const _CFG_BACKING = {
  'integrations':   'global',
  // 'audio-speakers' has no single backing — rendered by a custom
  // path that calls cfgBuildForm twice (speakers + audio) with
  // per-subsection save buttons.
  // 'llm-services' removed: LLM is now under System → Services tab.
};

// ════════════════════════════════════════════════════════════════
// Phase 6.0 — page-level top-tab navigation (within a sidebar page)
// ════════════════════════════════════════════════════════════════
// Each multi-section page emits:
//   <button class="page-tab" data-page-tab-group="GROUP" data-tab="ID" onclick="showPageTab('GROUP','ID')">...</button>
//   <div class="page-tab-panel" data-page-tab-panel-group="GROUP" data-tab="ID">...</div>
// showPageTab toggles the active state and persists last-active per
// group so the tab picks up where the operator left off on return.
function showPageTab(group, tabId) {
  const tabs = document.querySelectorAll('[data-page-tab-group="' + group + '"]');
  tabs.forEach(t => t.classList.toggle('active', t.getAttribute('data-tab') === tabId));
  const panels = document.querySelectorAll('[data-page-tab-panel-group="' + group + '"]');
  panels.forEach(p => p.classList.toggle('active', p.getAttribute('data-tab') === tabId));
  try { localStorage.setItem('glados_ptab_' + group, tabId); } catch(e) {}
}
function _loadPageTab(group, fallback) {
  try {
    const v = localStorage.getItem('glados_ptab_' + group);
    if (v) return v;
  } catch(e) {}
  return fallback;
}

// Floppy-disk SVG used by every page-save-btn.
function _floppySvg() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
    + '<polyline points="17 21 17 13 7 13 7 21"/>'
    + '<polyline points="7 3 7 8 15 8"/>'
    + '</svg>';
}

function cfgRenderSection(section) {
  if (section === 'audio-speakers') {
    _cfgRenderAudioSpeakers();
    return;
  }
  if (section === 'integrations') {
    _cfgRenderIntegrations();
    return;
  }
  const backing = _CFG_BACKING[section] || section;

  const data = (section === 'ssl') ? (_cfgData.global || {}) : _cfgData[backing];
  if (!data) {
    document.getElementById('cfg-form-area').innerHTML =
      '<div style="color:#ff6666;padding:20px;">Section not loaded. Click Reload.</div>';
    return;
  }
  const meta = SECTION_META[section] || SECTION_META[backing] || {};

  // personality and ssl have their own full-page chrome (page-header + tabs
  // for personality; standalone renderer for ssl) — skip the shared header
  // and trailing save button so they don't double-render.
  const hasOwnChrome = (backing === 'personality' || section === 'ssl');

  let html = '';
  if (!hasOwnChrome) {
    html += '<div class="cfg-section-header">'
      + '<div class="cfg-section-title">' + escHtml(meta.title || section) + '</div>'
      + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
      + '</div>';
  }

  if (backing === 'services') {
    html += cfgRenderServices(data);
  } else if (backing === 'personality') {
    html += cfgRenderPersonality(data);
  } else if (section === 'ssl') {
    html += cfgRenderSsl(_cfgData.global && _cfgData.global.ssl ? _cfgData.global.ssl : {});
  } else {
    // Skip keys that belong to a dedicated page (ssl → SSL tab; auth /
    // audit / mode_entities → System tab) or are env-only (paths, network
    // are driven by GLADOS_ROOT / SERVE_HOST etc., so the YAML-backed form
    // is inert inside the container).
    const skipKeys = (backing === 'global')
        ? ['ssl', 'paths', 'network', 'auth', 'audit', 'mode_entities']
        : null;
    html += cfgBuildForm(data, backing, '', skipKeys);
  }

  if (!hasOwnChrome) {
    const label = meta.title || backing;
    html += '<div class="cfg-save-row">'
      + '<button class="cfg-save-btn" onclick="cfgSaveSection(\'' + backing + '\')">Save ' + escHtml(label) + '</button>'
      + '<span id="cfg-save-result" class="cfg-result"></span>'
      + '</div>';
  }

  // Page-specific extras appended AFTER the main form + save button.
  if (section === 'integrations') {
    html += _cfgRenderIntegrationsExtras();
  }

  const _frmArea = document.getElementById('cfg-form-area');
  _frmArea.innerHTML = html;
  // Wire custom pbar sliders for HEXACO / PAD cards (personality section).
  if (backing === 'personality') setTimeout(function() { _pbarInit(_frmArea); }, 0);
}

// ════════════════════════════════════════════════════════════════
// Phase 6.0 (2026-04-22) — Integrations page, top-tabs layout.
// ════════════════════════════════════════════════════════════════
// Replaces the old scroll-forever stack of cards. Left sidebar click
// on "Integrations" lands on this renderer; operator switches between
// Home Assistant / MQTT / Disambiguation / Candidate retrieval via
// the top tab bar. Each panel hosts whatever was previously on the
// long scroll. Top-right Save button saves the active tab's section.
function _cfgRenderIntegrations() {
  // LLM moved to System → Services tab (Phase 2 Chunk 2).
  const globalData = _cfgData.global || {};
  const meta = SECTION_META['integrations'] || {};

  const TABS = [
    { id: 'ha',      label: 'Home Assistant' },
    { id: 'weather', label: 'Weather' },
    { id: 'mqtt',    label: 'MQTT' },
  ];
  const activeTabId = _loadPageTab('integrations', 'ha');

  // Page header: title + Save button.
  let html = '<div class="page-header">'
    + '<div>'
    +   '<h2 class="page-title">' + escHtml(meta.title || 'Integrations') + '</h2>'
    +   (meta.desc ? '<div class="page-title-desc">' + escHtml(meta.desc) + '</div>' : '')
    + '</div>'
    + '<button class="page-save-btn" onclick="_cfgSaveCurrentIntegrationsTab()" title="Save the active tab">'
    +   _floppySvg()
    +   '<span>Save</span>'
    + '</button>'
    + '</div>';

  // Tab bar.
  html += '<nav class="page-tabs" role="tablist">';
  for (const t of TABS) {
    const cls = t.id === activeTabId ? 'page-tab active' : 'page-tab';
    html += '<button class="' + cls + '" role="tab" data-page-tab-group="integrations" data-tab="' + t.id + '" onclick="showPageTab(\'integrations\',\'' + t.id + '\')">'
      + escHtml(t.label)
      + '</button>';
  }
  html += '</nav>';

  html += '<div class="page-tab-panels">';

  // ── HA panel ────────────────────────────────────────────────
  const haSubset = {};
  if (globalData.home_assistant) haSubset.home_assistant = globalData.home_assistant;
  html += '<div class="page-tab-panel' + (activeTabId === 'ha' ? ' active' : '') + '" data-page-tab-panel-group="integrations" data-tab="ha">';
  html +=   '<div class="card">' + cfgBuildForm(haSubset, 'global', '') + '</div>';
  html += '</div>';

  // ── Weather panel (Phase 6.4) ──────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'weather' ? ' active' : '') + '" data-page-tab-panel-group="integrations" data-tab="weather">';
  html +=   '<div class="card" id="cfg-weather-card">';
  html +=     '<div class="cfg-field-desc" style="margin-bottom:10px;">'
        +      'Weather provider is Open-Meteo &mdash; free, no API key, works anywhere on Earth. '
        +      'Point it at your location by postal code, city, or address; units are operator-selectable.'
        +    '</div>';
  html +=     '<div id="cfg-weather-body">Loading weather settings&hellip;</div>';
  html +=   '</div>';
  html += '</div>';

  // ── MQTT panel ──────────────────────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'mqtt' ? ' active' : '') + '" data-page-tab-panel-group="integrations" data-tab="mqtt">';
  html +=   '<div class="card" id="cfg-mqtt-card">';
  html +=     '<div class="cfg-field-desc" style="margin-bottom:10px;">'
        +      'Optional integration with an MQTT broker. Publishes GLaDOS events to the peer '
        +      'bus and subscribes to commands from other LAN services. Disabled by default.'
        +    '</div>';
  html +=     '<div id="cfg-mqtt-body">Loading MQTT settings&hellip;</div>';
  html +=   '</div>';
  html += '</div>';

  html += '</div>';  // end page-tab-panels

  document.getElementById('cfg-form-area').innerHTML = html;

  setTimeout(_cfgLoadMqtt, 0);
  setTimeout(_cfgLoadWeather, 0);
}

// Model Options + LLM Timeouts cards — kept for standalone use by
// cfgSaveModelOptions / cfgSaveLLMTimeouts (called from System → Services
// Advanced collapsible). The function itself is no longer called to render
// HTML but the save functions it backed still reference the same field IDs
// which are now rendered by loadSystemServices().
function _cfgRenderLLMExtrasOnly() {
  const mo = (_cfgData.personality || {}).model_options || {};
  const t = ((_cfgData.global || {}).tuning) || {};
  let html = '';
  html += '<div class="card" style="margin-top:14px;">';
  html +=   '<div class="cfg-subsection-title">Model Options</div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Temperature</label>'
    +      '<div class="cfg-field-desc">0.0 is deterministic, 1.0+ is creative</div>'
    +      '<input id="cfg-personality-model_options-temperature" data-path="model_options.temperature" data-type="number" type="number" step="any" value="' + escAttr(String(mo.temperature ?? 0.7)) + '"></div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Top P</label>'
    +      '<div class="cfg-field-desc">Nucleus sampling threshold (0.0 - 1.0)</div>'
    +      '<input id="cfg-personality-model_options-top_p" data-path="model_options.top_p" data-type="number" type="number" step="any" value="' + escAttr(String(mo.top_p ?? 0.9)) + '"></div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Context Window (num_ctx)</label>'
    +      '<div class="cfg-field-desc">Tokens of context the model sees per turn</div>'
    +      '<input id="cfg-personality-model_options-num_ctx" data-path="model_options.num_ctx" data-type="number" type="number" value="' + escAttr(String(mo.num_ctx ?? 16384)) + '"></div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Repeat Penalty</label>'
    +      '<div class="cfg-field-desc">Higher values reduce parroting (typical 1.0 - 1.3)</div>'
    +      '<input id="cfg-personality-model_options-repeat_penalty" data-path="model_options.repeat_penalty" data-type="number" type="number" step="any" value="' + escAttr(String(mo.repeat_penalty ?? 1.1)) + '"></div>';
  html +=   '<div class="cfg-save-row">'
    +      '<button class="cfg-save-btn" onclick="cfgSaveModelOptions()">Save Model Options</button>'
    +      '<span id="cfg-save-result-model-options" class="cfg-result"></span></div>';
  html += '</div>';
  html += '<div class="card" style="margin-top:14px;" data-advanced="true">';
  html +=   '<div class="cfg-subsection-title">LLM Timeouts <span class="cfg-placeholder-tag">advanced</span></div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Connect Timeout (s)</label>'
    +      '<div class="cfg-field-desc">Seconds to wait for LLM connection</div>'
    +      '<input id="cfg-llm-connect-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_connect_timeout_s ?? 10)) + '"></div>';
  html +=   '<div class="cfg-field"><label class="cfg-field-label">Read Timeout (s)</label>'
    +      '<div class="cfg-field-desc">Max seconds to wait for LLM response</div>'
    +      '<input id="cfg-llm-read-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_read_timeout_s ?? 180)) + '"></div>';
  html +=   '<div class="cfg-save-row">'
    +      '<button class="cfg-save-btn" onclick="cfgSaveLLMTimeouts()">Save Timeouts</button>'
    +      '<span id="cfg-save-result-timeouts" class="cfg-result"></span></div>';
  html += '</div>';
  return html;
}

// Dispatch the page Save button to the active Integrations tab's save handler.
// LLM moved to System → Services (Phase 2 Chunk 2).
function _cfgSaveCurrentIntegrationsTab() {
  const active = document.querySelector('[data-page-tab-group="integrations"].active');
  const id = active ? active.getAttribute('data-tab') : 'ha';
  switch (id) {
    case 'ha':      return cfgSaveSection('global');
    case 'weather': return _cfgSaveWeather();
    case 'mqtt':    return _cfgSaveMqtt();
    default: return;
  }
}



// Phase 8.1 — Disambiguation rules card population and save.

// ═════════════════════════════════════════════════════════════════
// Phase 5.8 (2026-04-22) — MQTT peer bus configuration pane.
//
// Loads from GET /api/config/mqtt (password masked server-side;
// password_is_set flag tells the UI whether to display a 'leave
// blank to keep' hint). Saves via PUT. No broker host / port /
// credential is hardcoded here; the form is the only surface.
// ═════════════════════════════════════════════════════════════════
let _mqttConfigState = null;  // { config, password_is_set }

// ════════════════════════════════════════════════════════════════
// Phase 6.4 (2026-04-22) — Weather tab on Integrations.
// ════════════════════════════════════════════════════════════════
// Provider: Open-Meteo (free, no API key). Location resolves either
// from HA zone.home (auto) or from operator-entered postal code /
// city / address via /api/weather/geocode. Unit preferences flow
// directly through to the forecast API as query params.
let _weatherState = null;

async function _cfgLoadWeather() {
  const body = document.getElementById('cfg-weather-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/global');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const gdata = await r.json();
    _weatherState = {
      config: gdata.weather || {},
      candidates: [],
    };
    _cfgRenderWeather();
    _cfgRenderWeatherPreview();
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);">Failed to load weather: '
      + escHtml(String(e)) + '</div>';
  }
}

function _cfgRenderWeather() {
  const body = document.getElementById('cfg-weather-body');
  if (!body || !_weatherState) return;
  const c = _weatherState.config;
  const autoHA = !!c.auto_from_ha;
  const loc = (c.location_name || '').trim();
  const lat = c.latitude || 0, lng = c.longitude || 0;

  let html = '';
  html += '<div class="mqtt-subgroup">Location</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-inline-check">'
    +   '<input type="radio" name="wx-loc-mode" id="wx-loc-auto" ' + (autoHA ? 'checked' : '') + ' onchange="_cfgWeatherModeChange()">'
    +   '<span>Use Home Assistant location (zone.home)</span>'
    + '</label>'
    + '<div class="trait-desc">Reads lat/long from your HA configuration on each refresh. Good default when HA is your home-location source of truth.</div>'
    + '</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-inline-check">'
    +   '<input type="radio" name="wx-loc-mode" id="wx-loc-manual" ' + (!autoHA ? 'checked' : '') + ' onchange="_cfgWeatherModeChange()">'
    +   '<span>Set location manually</span>'
    + '</label>'
    + '</div>';

  html += '<div id="wx-manual-block" style="' + (autoHA ? 'display:none;' : '') + 'padding-left:var(--sp-5);">';
  html +=   '<div class="mqtt-row">';
  html +=     '<div class="mqtt-field" style="flex:2;">';
  html +=       '<label class="mqtt-label" for="wx-query">City, postal code, or address</label>';
  html +=       '<input id="wx-query" type="text" placeholder="e.g. 76102 or London, UK or Tokyo" value="" autocomplete="off">';
  html +=     '</div>';
  html +=     '<div class="mqtt-field" style="flex:0 0 auto;">';
  html +=       '<label class="mqtt-label">&nbsp;</label>';
  html +=       '<button class="btn btn-primary" style="padding:var(--sp-2) var(--sp-4);" onclick="_cfgWeatherGeocode()">Look up</button>';
  html +=     '</div>';
  html +=   '</div>';
  html +=   '<div id="wx-candidates" class="trait-desc" style="margin-top:var(--sp-2);"></div>';
  html += '</div>';

  html += '<div class="mqtt-field" style="margin-top:var(--sp-3);">';
  html +=   '<label class="mqtt-label">Current resolved location</label>';
  html +=   '<div class="trait-desc" style="font-size:0.82rem;color:var(--fg-secondary);">'
        +    (loc ? escHtml(loc) + ' &nbsp;&middot;&nbsp; ' : '')
        +    (lat && lng ? (Number(lat).toFixed(4) + ', ' + Number(lng).toFixed(4))
                         : '<em>not set &mdash; use auto-HA or run a lookup above</em>')
        +  '</div>';
  html += '</div>';

  const tu = c.temperature_unit || 'fahrenheit';
  const wu = c.wind_speed_unit || 'mph';
  const pu = c.precipitation_unit || 'inch';
  html += '<div class="mqtt-subgroup">Units</div>';
  html += '<div class="mqtt-row">';
  html +=   _wxUnitRadio('Temperature', 'wx-temp', [['fahrenheit', '°F'], ['celsius', '°C']], tu);
  html +=   _wxUnitRadio('Wind speed',  'wx-wind', [['mph', 'mph'], ['kmh', 'km/h'], ['ms', 'm/s'], ['kn', 'knots']], wu);
  html +=   _wxUnitRadio('Precipitation','wx-prec', [['inch', 'inches'], ['mm', 'mm']], pu);
  html += '</div>';

  html += '<div class="mqtt-subgroup">Current reading</div>';
  html += '<div id="wx-preview" class="trait-desc" style="font-size:0.84rem;">Loading&hellip;</div>';
  html += '<div class="controls" style="margin-top:var(--sp-2);">';
  html +=   '<button class="btn btn-primary" onclick="_cfgRenderWeatherPreview()">Refresh preview</button>';
  html += '</div>';

  body.innerHTML = html;
}

function _wxUnitRadio(label, prefix, opts, current) {
  let html = '<div class="mqtt-field" style="flex:1;min-width:160px;">';
  html += '<label class="mqtt-label">' + escHtml(label) + '</label>';
  for (let i = 0; i < opts.length; i++) {
    const [val, disp] = opts[i];
    const id = prefix + '-' + val;
    html += '<label class="mqtt-inline-check" style="margin-right:var(--sp-3);">';
    html +=   '<input type="radio" name="' + prefix + '" id="' + id + '" value="' + val + '"' + (val === current ? ' checked' : '') + '>';
    html +=   '<span>' + escHtml(disp) + '</span>';
    html += '</label>';
  }
  html += '</div>';
  return html;
}

function _cfgWeatherModeChange() {
  const autoEl = document.getElementById('wx-loc-auto');
  const manualBlock = document.getElementById('wx-manual-block');
  if (autoEl && manualBlock) {
    manualBlock.style.display = autoEl.checked ? 'none' : '';
  }
}

async function _cfgWeatherGeocode() {
  const qEl = document.getElementById('wx-query');
  const candEl = document.getElementById('wx-candidates');
  if (!qEl || !candEl) return;
  const q = qEl.value.trim();
  if (!q) {
    candEl.innerHTML = '<span style="color:var(--orange)">Enter a postal code, city, or address first.</span>';
    return;
  }
  candEl.innerHTML = '<span class="spinner"></span> Looking up…';
  try {
    const r = await fetch('/api/weather/geocode?q=' + encodeURIComponent(q));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const hits = data.candidates || [];
    if (!hits.length) {
      candEl.innerHTML = '<span style="color:var(--red)">No matches. Try a different query.</span>';
      return;
    }
    let html = '<div style="color:var(--fg-secondary);margin-bottom:var(--sp-1);">Pick a match:</div>';
    for (let i = 0; i < hits.length; i++) {
      const h = hits[i];
      const fullName = [h.name, h.admin1, h.country].filter(Boolean).join(', ');
      html += '<div style="padding:var(--sp-1) 0;">'
        + '<button class="btn" style="background:var(--bg-input);text-align:left;width:100%;padding:var(--sp-2) var(--sp-3);" '
        +   'onclick="_cfgWeatherPickCandidate(' + i + ')">'
        +   escHtml(fullName) + ' <span style="color:var(--fg-tertiary);font-size:0.72rem;">&mdash; '
        +   Number(h.latitude).toFixed(4) + ', ' + Number(h.longitude).toFixed(4) + ' &middot; ' + escHtml(h.timezone || 'auto') + '</span>'
        + '</button>'
        + '</div>';
    }
    candEl.innerHTML = html;
    if (!_weatherState) _weatherState = { config: {}, candidates: [] };
    _weatherState.candidates = hits;
  } catch (e) {
    candEl.innerHTML = '<span style="color:var(--red)">Lookup failed: ' + escHtml(String(e)) + '</span>';
  }
}

function _cfgWeatherPickCandidate(i) {
  if (!_weatherState || !_weatherState.candidates || !_weatherState.candidates[i]) return;
  const h = _weatherState.candidates[i];
  const fullName = [h.name, h.admin1, h.country].filter(Boolean).join(', ');
  _weatherState.config.latitude = h.latitude;
  _weatherState.config.longitude = h.longitude;
  _weatherState.config.timezone = h.timezone || 'auto';
  _weatherState.config.location_name = fullName;
  _weatherState.config.auto_from_ha = false;
  _cfgWeatherCaptureUnits();
  _cfgRenderWeather();
}

function _cfgWeatherCaptureUnits() {
  if (!_weatherState) return;
  const tu = document.querySelector('input[name="wx-temp"]:checked');
  const wu = document.querySelector('input[name="wx-wind"]:checked');
  const pu = document.querySelector('input[name="wx-prec"]:checked');
  if (tu) _weatherState.config.temperature_unit = tu.value;
  if (wu) _weatherState.config.wind_speed_unit = wu.value;
  if (pu) _weatherState.config.precipitation_unit = pu.value;
  const autoEl = document.getElementById('wx-loc-auto');
  if (autoEl) _weatherState.config.auto_from_ha = autoEl.checked;
}

async function _cfgRenderWeatherPreview() {
  const el = document.getElementById('wx-preview');
  if (!el) return;
  el.innerHTML = '<span class="spinner"></span> Fetching current conditions…';
  try {
    const r = await fetch('/api/weather/refresh', { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const c = d.current || {};
    const t = d.today || {};
    const units = d.units || {};
    el.innerHTML = ''
      + '<div><strong>' + escHtml(String(c.temperature ?? '?') + (units.temperature || '°')) + '</strong>, '
      + escHtml(String(c.condition || '?')) + ', '
      + 'wind ' + (c.wind_speed ?? '?') + ' ' + escHtml(String(units.wind_speed || '')) + '</div>'
      + '<div>Today: high ' + (t.high ?? '?') + (units.temperature || '°')
      + ' / low ' + (t.low ?? '?') + (units.temperature || '°')
      + ', ' + escHtml(String(t.condition || '?')) + '</div>';
  } catch (e) {
    el.innerHTML = '<span style="color:var(--red)">Preview failed: ' + escHtml(String(e)) + '</span>';
  }
}

async function _cfgSaveWeather() {
  if (!_weatherState) return;
  _cfgWeatherCaptureUnits();
  try {
    const r = await fetch('/api/config/global');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const gdata = await r.json();
    gdata.weather = Object.assign({}, gdata.weather || {}, _weatherState.config);
    const put = await fetch('/api/config/global', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(gdata),
    });
    if (!put.ok) {
      const txt = await put.text();
      showToast('Weather save failed: ' + txt.slice(0, 200), 'error');
      return;
    }
    showToast('Weather settings saved.', 'success');
    _cfgRenderWeatherPreview();
  } catch (e) {
    showToast('Weather save error: ' + e.message, 'error');
  }
}

async function _cfgLoadMqtt() {
  const body = document.getElementById('cfg-mqtt-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/mqtt');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const cfg = await r.json();
    _mqttConfigState = {
      config: cfg,
      password_is_set: cfg.password_is_set === true,
    };
    _cfgRenderMqtt();
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);">Failed to load MQTT settings: '
      + escHtml(String(e)) + '</div>';
    console.error('mqtt config load failed:', e);
  }
}

function _cfgRenderMqtt() {
  const body = document.getElementById('cfg-mqtt-body');
  if (!body || !_mqttConfigState) return;
  const c = _mqttConfigState.config;
  const pwHint = _mqttConfigState.password_is_set
    ? 'Password is currently set. Leave blank to keep it; type a new value to change.'
    : 'No password stored. If auth is required, enter it here.';
  let html = '';

  // Top-level enable toggle. Everything else is just stored; the
  // client only attempts a connection when enabled is on AND
  // broker_host is non-empty.
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-inline-check">'
    +   '<input type="checkbox" id="cfg-mqtt-enabled"' + (c.enabled ? ' checked' : '') + '>'
    +   '<span>Enable MQTT peer bus</span>'
    + '</label>'
    + '<div class="trait-desc">When disabled, the client never connects and no events are published or subscribed.</div>'
    + '</div>';

  // Broker connection.
  html += '<div class="mqtt-subgroup">Broker connection</div>';
  html += '<div class="mqtt-row">'
    + '<div class="mqtt-field">'
    +   '<label class="mqtt-label" for="cfg-mqtt-host">Broker host</label>'
    +   '<input id="cfg-mqtt-host" type="text" value="' + escHtml(c.broker_host || '') + '" placeholder="homeassistant.local">'
    + '</div>'
    + '<div class="mqtt-field mqtt-port">'
    +   '<label class="mqtt-label" for="cfg-mqtt-port">Port</label>'
    +   '<input id="cfg-mqtt-port" type="number" min="1" max="65535" value="' + (c.broker_port || 1883) + '">'
    + '</div>'
    + '</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-inline-check">'
    +   '<input type="checkbox" id="cfg-mqtt-tls"' + (c.use_tls ? ' checked' : '') + '>'
    +   '<span>Use TLS</span>'
    + '</label>'
    + '<div class="trait-desc">Strongly recommended when the broker is reachable outside the trusted LAN. Requires the broker to present a certificate.</div>'
    + '</div>';

  // Authentication.
  html += '<div class="mqtt-subgroup">Authentication</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-inline-check">'
    +   '<input type="checkbox" id="cfg-mqtt-auth"' + (c.auth_enabled ? ' checked' : '') + ' onchange="_cfgMqttToggleAuth()">'
    +   '<span>Authentication required</span>'
    + '</label>'
    + '<div class="trait-desc">Check this if the broker is set up with a username/password. HA&rsquo;s Mosquitto add-on almost always requires auth.</div>'
    + '</div>';
  html += '<div id="cfg-mqtt-auth-fields" style="' + (c.auth_enabled ? '' : 'display:none;') + '">';
  html +=   '<div class="mqtt-field">'
    +       '<label class="mqtt-label" for="cfg-mqtt-user">Username</label>'
    +       '<input id="cfg-mqtt-user" type="text" value="' + escHtml(c.username || '') + '" autocomplete="off">'
    +     '</div>';
  html +=   '<div class="mqtt-field">'
    +       '<label class="mqtt-label" for="cfg-mqtt-pw">Password</label>'
    +       '<input id="cfg-mqtt-pw" type="password" value="" autocomplete="new-password" placeholder="' + escHtml(_mqttConfigState.password_is_set ? '(leave blank to keep)' : '') + '">'
    +       '<div class="trait-desc">' + escHtml(pwHint) + '</div>'
    +     '</div>';
  html += '</div>';

  // Identity + topic routing.
  html += '<div class="mqtt-subgroup">Identity and topics</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-label" for="cfg-mqtt-clientid">Client ID</label>'
    + '<input id="cfg-mqtt-clientid" type="text" value="' + escHtml(c.client_id || 'glados-bridge') + '">'
    + '<div class="trait-desc">Unique identifier GLaDOS presents to the broker. Change only if another client on the same broker already uses this name.</div>'
    + '</div>';
  html += '<div class="mqtt-field">'
    + '<label class="mqtt-label" for="cfg-mqtt-prefix">Topic prefix</label>'
    + '<input id="cfg-mqtt-prefix" type="text" value="' + escHtml(c.topic_prefix || 'glados') + '">'
    + '<div class="trait-desc">Root of the outbound event channel (<code>&lt;prefix&gt;/events/…</code>) and the inbound command channel (<code>&lt;prefix&gt;/cmd/…</code>). Keep it short and operator-meaningful.</div>'
    + '</div>';

  // Transport tuning — tucked behind Advanced.
  html += '<div class="mqtt-subgroup" data-advanced="true">Transport tuning (advanced)</div>';
  html += '<div class="mqtt-row" data-advanced="true">'
    + '<div class="mqtt-field">'
    +   '<label class="mqtt-label" for="cfg-mqtt-keepalive">Keepalive (seconds)</label>'
    +   '<input id="cfg-mqtt-keepalive" type="number" min="5" max="600" value="' + (c.keepalive_s || 60) + '">'
    + '</div>'
    + '<div class="mqtt-field">'
    +   '<label class="mqtt-label" for="cfg-mqtt-reconnect">Reconnect delay (seconds)</label>'
    +   '<input id="cfg-mqtt-reconnect" type="number" min="1" max="300" value="' + (c.reconnect_delay_s || 5) + '">'
    + '</div>'
    + '</div>';

  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="_cfgSaveMqtt()">Save MQTT settings</button>'
    + '<span id="cfg-save-result-mqtt" class="cfg-result"></span>'
    + '</div>';

  body.innerHTML = html;
}

function _cfgMqttToggleAuth() {
  const chk = document.getElementById('cfg-mqtt-auth');
  const fields = document.getElementById('cfg-mqtt-auth-fields');
  if (chk && fields) fields.style.display = chk.checked ? '' : 'none';
}

async function _cfgSaveMqtt() {
  const result = document.getElementById('cfg-save-result-mqtt');
  if (!_mqttConfigState) {
    if (result) result.textContent = 'Not loaded';
    return;
  }
  const g = id => document.getElementById(id);
  const next = {
    enabled: !!g('cfg-mqtt-enabled').checked,
    broker_host: g('cfg-mqtt-host').value.trim(),
    broker_port: parseInt(g('cfg-mqtt-port').value, 10) || 1883,
    use_tls: !!g('cfg-mqtt-tls').checked,
    auth_enabled: !!g('cfg-mqtt-auth').checked,
    username: g('cfg-mqtt-user').value.trim(),
    password: g('cfg-mqtt-pw').value,  // empty => backend keeps stored value
    client_id: g('cfg-mqtt-clientid').value.trim() || 'glados-bridge',
    topic_prefix: g('cfg-mqtt-prefix').value.trim() || 'glados',
    keepalive_s: parseInt(g('cfg-mqtt-keepalive').value, 10) || 60,
    reconnect_delay_s: parseInt(g('cfg-mqtt-reconnect').value, 10) || 5,
  };
  if (result) { result.textContent = 'Saving…'; result.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/config/mqtt', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error('HTTP ' + r.status + ': ' + txt.slice(0, 200));
    }
    if (result) { result.textContent = 'Saved'; result.className = 'cfg-result cfg-result-ok'; }
    // Refresh so password_is_set + other derived state updates.
    _cfgLoadMqtt();
  } catch (e) {
    if (result) { result.textContent = 'Save failed: ' + String(e); result.className = 'cfg-result cfg-result-err'; }
    console.error('mqtt save failed:', e);
  }
}

async function _cfgLoadDisambiguation() {
  const body = document.getElementById('cfg-disamb-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load rules (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _disambPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading rules: ' + escHtml(e.message) + '</div>';
  }
}

function _disambPopulate(data) {
  // Stash the raw server payload so the save handler can round-trip
  // floor_aliases / area_aliases that no longer have UI inputs.
  window._cfgDisambData = data;
  const body = document.getElementById('cfg-disamb-body');
  if (!body) return;
  const dedup = (data.twin_dedup === false) ? false : true;
  const ignoreSeg = (data.ignore_segments === false) ? false : true;
  const pairs = Array.isArray(data.opposing_token_pairs) ? data.opposing_token_pairs : [];
  const verifyMode = (typeof data.verification_mode === 'string') ? data.verification_mode : 'strict';
  const verifyTimeout = (typeof data.verification_timeout_s === 'number') ? data.verification_timeout_s : 3.0;
  let html = '';
  html += '<div class="cfg-field" style="display:flex;align-items:center;gap:10px;">'
    +   '<input type="checkbox" id="cfg-disamb-twin-dedup"' + (dedup ? ' checked' : '') + ' style="width:auto;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-twin-dedup" style="margin:0;">'
    +     'Collapse light/switch twins by device_id'
    +   '</label>'
    + '</div>'
    + '<div class="cfg-field-desc" style="margin:-6px 0 14px 28px;">'
    +   'When both <code>light.foo</code> and <code>switch.foo</code> represent the same physical relay, '
    +   'keep the light side (the only domain that honours <code>brightness_pct</code>). The switch still wins '
    +   'automatically when the light has no dim capability (Inovelli fan/light edge case).'
    + '</div>';
  // Phase 8.3 follow-up — operator-requested: drop segment
  // entities entirely from candidate lists. Most deployments
  // never address segments directly; the planner never sees them.
  html += '<div class="cfg-field" style="display:flex;align-items:center;gap:10px;">'
    +   '<input type="checkbox" id="cfg-disamb-ignore-segments"' + (ignoreSeg ? ' checked' : '') + ' style="width:auto;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-ignore-segments" style="margin:0;">'
    +     'Ignore segment entities (master lamp / scene only)'
    +   '</label>'
    + '</div>'
    + '<div class="cfg-field-desc" style="margin:-6px 0 14px 28px;">'
    +   'Drops any entity whose name or id matches the segment-token pattern before candidate resolution runs. '
    +   'Operators control the whole lamp or a preset scene; per-segment control is rare. Disable if your house '
    +   'genuinely needs per-segment control (theatrical lighting, etc.).'
    + '</div>';
  html += '<div class="cfg-field-label" style="margin-top:6px;">Opposing-token pairs</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Word pairs that rarely belong in the same command. If an utterance contains one side of a pair '
    +   'and a candidate device name contains the other side, that device becomes much less likely to be '
    +   'chosen &mdash; so saying &ldquo;upstairs&rdquo; won&rsquo;t accidentally target a device named &ldquo;downstairs.&rdquo; '
    +   'Leave empty to use the shipped defaults '
    +   '(<code>upstairs/downstairs</code>, <code>lower/upper</code>, <code>front/back</code>, '
    +   '<code>inside/outside</code>, <code>indoor/outdoor</code>, <code>master/guest</code>, '
    +   '<code>left/right</code>, <code>top/bottom</code>, <code>primary/secondary</code>, '
    +   '<code>north/south</code>, <code>east/west</code>).'
    + '</div>';
  html += '<div id="cfg-disamb-pairs" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px;"></div>';
  html += '<button type="button" class="cfg-save-btn" onclick="_disambAddPair()">+ Add pair</button>';
  // Phase 8.3.5 — operator-editable extra segment tokens used by
  // the device-diversity filter on top-K retrieval. Merges with
  // the shipped defaults (seg, segment, zone, channel, strip,
  // group, head); entries here add to (never replace) that list.
  const tokens = Array.isArray(data.extra_segment_tokens) ? data.extra_segment_tokens : [];
  html += '<div class="cfg-field-label" style="margin-top:14px;">Extra segment tokens</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Added to the shipped defaults (<code>seg, segment, zone, channel, strip, group, head</code>) '
    +   'when detecting multi-segment devices like Gledopto LED strips. Add house-specific tokens (e.g. '
    +   '<code>pixel</code>) if your strip entities use a different naming convention.'
    + '</div>'
    + '<div id="cfg-disamb-tokens" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" onclick="_disambAddToken()">+ Add token</button>';
  // Phase 8.4 — post-execute state verification.
  html += '<div class="cfg-field-label" style="margin-top:18px;">Post-execute state verification</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:8px;">'
    +   'After every <code>call_service</code>, the disambiguator waits for Home&nbsp;Assistant to report a '
    +   'matching <code>state_changed</code> event. '
    +   '<strong>Strict</strong> replaces the optimistic speech with an honest failure line when no matching '
    +   'transition lands within the timeout &mdash; so GLaDOS never confidently announces a change that '
    +   'silently failed. '
    +   '<strong>Warn</strong> still audits the outcome but keeps the optimistic line. '
    +   '<strong>Silent</strong> skips verification entirely (pre-Phase-8.4 behaviour).'
    + '</div>'
    + '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-verify-mode" style="margin:0;min-width:140px;">Mode</label>'
    +   '<select id="cfg-disamb-verify-mode" style="flex:1;min-width:160px;">'
    +     '<option value="strict"' + (verifyMode === 'strict' ? ' selected' : '') + '>Strict (replace speech on failure)</option>'
    +     '<option value="warn"' + (verifyMode === 'warn' ? ' selected' : '') + '>Warn (audit only)</option>'
    +     '<option value="silent"' + (verifyMode === 'silent' ? ' selected' : '') + '>Silent (no verification)</option>'
    +   '</select>'
    + '</div>'
    + '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-verify-timeout" style="margin:0;min-width:140px;">Timeout (seconds)</label>'
    +   '<input type="number" id="cfg-disamb-verify-timeout" min="0.1" max="30" step="0.1" value="' + escAttr(verifyTimeout.toFixed(1)) + '" style="flex:1;min-width:120px;">'
    + '</div>';
  // Phase 2 Chunk 3: floor/area alias editor removed — HA natively supports
  // aliases per area/floor. Render a notice card pointing to HA docs instead.
  // Existing floor_aliases / area_aliases in YAML are preserved on save (below).
  html += '<div class="cfg-field-label" style="margin-top:18px;">Area &amp; Floor Aliases</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:8px;">'
    +   'GLaDOS uses Home Assistant&rsquo;s built-in area and floor aliases to interpret commands like '
    +   '&ldquo;turn off the lights upstairs.&rdquo; Set aliases per-area or per-floor in your Home Assistant '
    +   'settings &mdash; they propagate here automatically. '
    +   '<a href="https://www.home-assistant.io/docs/organizing/areas/" target="_blank" rel="noopener noreferrer">'
    +   'Home Assistant alias docs &nearr;</a>'
    + '</div>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveDisambiguation()">Save Disambiguation rules</button>'
    + '<span id="cfg-save-result-disamb" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
  const rows = document.getElementById('cfg-disamb-pairs');
  pairs.forEach(p => _disambRenderPairRow(rows, p[0] || '', p[1] || ''));
  const tokensHost = document.getElementById('cfg-disamb-tokens');
  tokens.forEach(t => _disambRenderTokenRow(tokensHost, t));
}


function _disambRenderAliasRow(host, kind, keyword, target) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-alias-row cfg-disamb-alias-' + kind;
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-alias-keyword" value="' + escAttr(keyword) + '" placeholder="e.g. living floor" style="flex:1;">'
    + '<span style="opacity:0.6;">&rarr;</span>'
    + '<input type="text" class="cfg-disamb-alias-target" value="' + escAttr(target) + '" placeholder="e.g. Main Level" style="flex:1;">'
    + '<button type="button" class="btn btn-danger" title="Remove alias">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddFloorAlias() {
  const host = document.getElementById('cfg-disamb-floor-aliases');
  if (host) _disambRenderAliasRow(host, 'floor', '', '');
}

function _disambAddAreaAlias() {
  const host = document.getElementById('cfg-disamb-area-aliases');
  if (host) _disambRenderAliasRow(host, 'area', '', '');
}

function _disambRenderTokenRow(host, t) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-token-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-token" value="' + escAttr(t) + '" placeholder="e.g. pixel" style="flex:1;">'
    + '<button type="button" class="btn btn-danger" title="Remove token">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddToken() {
  const host = document.getElementById('cfg-disamb-tokens');
  if (host) _disambRenderTokenRow(host, '');
}

function _disambRenderPairRow(host, a, b) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-pair-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-pair-a" value="' + escAttr(a) + '" placeholder="e.g. upstairs" style="flex:1;">'
    + '<span style="opacity:0.6;">&harr;</span>'
    + '<input type="text" class="cfg-disamb-pair-b" value="' + escAttr(b) + '" placeholder="e.g. downstairs" style="flex:1;">'
    + '<button type="button" class="btn btn-danger" title="Remove pair">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddPair() {
  const host = document.getElementById('cfg-disamb-pairs');
  if (host) _disambRenderPairRow(host, '', '');
}

// ── Phase 8.3.5 — Candidate retrieval card ──────────────────

async function _cfgLoadCandRetrieval() {
  const body = document.getElementById('cfg-candretrieval-body');
  if (!body) return;
  let status = null;
  try {
    const r = await fetch('/api/semantic/status');
    if (r.ok) status = await r.json();
    else body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load status (' + r.status + ').</div>';
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading status: ' + escHtml(e.message) + '</div>';
    return;
  }
  if (!status) return;
  _candRetrievalPopulate(status);
}

function _candRetrievalPopulate(status) {
  const body = document.getElementById('cfg-candretrieval-body');
  if (!body) return;
  const ready = !!status.ready;
  const mtime = status.file_mtime ? new Date(status.file_mtime * 1000).toLocaleString() : 'never';
  const sizeMb = status.file_size_bytes ? (status.file_size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '—';
  let html = '';
  // Status row
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;line-height:1.6;">'
    + '<strong>Status:</strong> '
    + (ready ? '<span style="color:#6c6;">ready</span>' : '<span style="color:#d99;">not ready</span>')
    + ' &middot; <strong>Entities indexed:</strong> ' + (status.size || 0)
    + ' &middot; <strong>Last persist:</strong> ' + escHtml(mtime)
    + ' &middot; <strong>File size:</strong> ' + sizeMb
    + '</div>';
  if (!status.deps_available) {
    html += '<div class="cfg-field-desc" style="color:#d99;margin-bottom:8px;">'
      + 'Embedding dependencies or model files are missing. Tier&nbsp;2 stays on the fuzzy matcher.'
      + '</div>';
  }
  // Rebuild button
  html += '<div style="display:flex;gap:8px;align-items:center;margin-bottom:16px;">'
    + '<button class="cfg-save-btn" onclick="_candRetrievalRebuild()">Rebuild index</button>'
    + '<span class="cfg-field-desc" style="margin:0;">'
    + '(Background. Poll the status line above to confirm size updates.)'
    + '</span>'
    + '</div>';
  // Test input
  html += '<div class="cfg-field-label">Test a query</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    + 'See which entities the retriever + device-diversity filter would hand to the planner. '
    + 'Useful for confirming Gledopto-style multi-segment devices don&rsquo;t swamp top-K.'
    + '</div>'
    + '<div style="display:flex;gap:6px;align-items:center;">'
    + '<input type="text" id="cfg-candretrieval-q" placeholder="e.g. desk lamp" style="flex:1;">'
    + '<input type="number" id="cfg-candretrieval-k" value="8" min="1" max="20" style="width:60px;">'
    + '<button type="button" class="cfg-save-btn" onclick="_candRetrievalTest()">Test</button>'
    + '</div>'
    + '<div id="cfg-candretrieval-result" style="margin-top:10px;"></div>';
  body.innerHTML = html;
}

async function _candRetrievalRebuild() {
  showToast('Rebuild started...', 'info');
  try {
    const r = await fetch('/api/semantic/rebuild', {method: 'POST'});
    if (!r.ok) { showToast('Rebuild failed', 'err'); return; }
    // Give the background a couple seconds, then refresh the status line
    setTimeout(_cfgLoadCandRetrieval, 3000);
  } catch (e) {
    showToast('Rebuild error: ' + e.message, 'err');
  }
}

async function _candRetrievalTest() {
  const qEl = document.getElementById('cfg-candretrieval-q');
  const kEl = document.getElementById('cfg-candretrieval-k');
  const res = document.getElementById('cfg-candretrieval-result');
  if (!qEl || !res) return;
  const query = (qEl.value || '').trim();
  const k = parseInt(kEl.value || '8', 10) || 8;
  if (!query) { res.innerHTML = '<div class="cfg-field-desc">Enter a query.</div>'; return; }
  res.innerHTML = '<div class="cfg-field-desc">Running&hellip;</div>';
  try {
    const r = await fetch('/api/semantic/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, k}),
    });
    const resp = await r.json();
    if (!r.ok) {
      res.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">'
        + escHtml((resp.error && resp.error.message) || ('HTTP ' + r.status))
        + '</div>';
      return;
    }
    _candRetrievalRenderTable(res, resp);
  } catch (e) {
    res.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _candRetrievalRenderTable(host, resp) {
  const kept = Array.isArray(resp.kept) ? resp.kept : [];
  const dropped = Array.isArray(resp.dropped_by_diversity) ? resp.dropped_by_diversity : [];
  const tokens = Array.isArray(resp.segment_tokens) ? resp.segment_tokens : [];
  let html = '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    + '<strong>Raw pool:</strong> ' + resp.raw_pool_size
    + ' &middot; <strong>Segment tokens in effect:</strong> <code>' + tokens.map(escHtml).join(', ') + '</code>'
    + '</div>';
  const renderRows = (list, color) => {
    if (!list.length) return '<tr><td colspan="3" style="padding:6px;color:#888;">(none)</td></tr>';
    return list.map(h =>
      '<tr>'
      + '<td style="padding:4px 8px;font-family:monospace;color:' + color + ';">' + escHtml(h.entity_id) + '</td>'
      + '<td style="padding:4px 8px;text-align:right;">' + h.score.toFixed(3) + '</td>'
      + '<td style="padding:4px 8px;font-family:monospace;font-size:0.85em;color:#aaa;">' + escHtml(h.document || '') + '</td>'
      + '</tr>'
    ).join('');
  };
  html += '<div style="margin-top:8px;"><strong>Kept (top ' + resp.top_k + '):</strong></div>'
    + '<table style="width:100%;border-collapse:collapse;font-size:0.88rem;">'
    + '<thead><tr style="text-align:left;color:#888;"><th style="padding:4px 8px;">entity_id</th><th style="padding:4px 8px;text-align:right;">score</th><th style="padding:4px 8px;">document</th></tr></thead>'
    + '<tbody>' + renderRows(kept, '#6c6') + '</tbody>'
    + '</table>';
  if (dropped.length) {
    html += '<div style="margin-top:10px;"><strong>Dropped by diversity filter:</strong></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.88rem;">'
      + '<tbody>' + renderRows(dropped, '#d99') + '</tbody>'
      + '</table>';
  }
  host.innerHTML = html;
}

async function cfgSaveDisambiguation() {
  const resultEl = document.getElementById('cfg-save-result-disamb');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const twinEl = document.getElementById('cfg-disamb-twin-dedup');
  const twin = twinEl ? !!twinEl.checked : true;
  const ignoreSegEl = document.getElementById('cfg-disamb-ignore-segments');
  const ignoreSegments = ignoreSegEl ? !!ignoreSegEl.checked : true;
  const pairs = [];
  document.querySelectorAll('#cfg-disamb-pairs .cfg-disamb-pair-row').forEach(row => {
    const a = (row.querySelector('.cfg-disamb-pair-a') || {}).value || '';
    const b = (row.querySelector('.cfg-disamb-pair-b') || {}).value || '';
    if (a.trim() && b.trim()) pairs.push([a.trim(), b.trim()]);
  });
  const tokens = [];
  document.querySelectorAll('#cfg-disamb-tokens .cfg-disamb-token-row .cfg-disamb-token').forEach(el => {
    const t = (el.value || '').trim();
    if (t) tokens.push(t);
  });
  const verifyModeEl = document.getElementById('cfg-disamb-verify-mode');
  const verifyMode = verifyModeEl ? String(verifyModeEl.value || 'strict') : 'strict';
  const verifyTimeoutEl = document.getElementById('cfg-disamb-verify-timeout');
  let verifyTimeout = verifyTimeoutEl ? parseFloat(verifyTimeoutEl.value) : 3.0;
  if (!isFinite(verifyTimeout) || verifyTimeout <= 0) verifyTimeout = 3.0;
  // Phase 2 Chunk 3: alias editor removed from UI but data preserved.
  // Read the last-loaded server values so existing YAML aliases aren't wiped.
  const _disambData = (window._cfgDisambData) || {};
  const floorAliases = (_disambData.floor_aliases && typeof _disambData.floor_aliases === 'object')
    ? _disambData.floor_aliases : {};
  const areaAliases = (_disambData.area_aliases && typeof _disambData.area_aliases === 'object')
    ? _disambData.area_aliases : {};
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        twin_dedup: twin,
        ignore_segments: ignoreSegments,
        opposing_token_pairs: pairs,
        extra_segment_tokens: tokens,
        verification_mode: verifyMode,
        verification_timeout_s: verifyTimeout,
        floor_aliases: floorAliases,
        area_aliases: areaAliases,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}



// Model Options: wrap the existing personality section save with a
// targeted update — read current personality data, overlay the four
// model_options fields, PUT the full personality payload back.
async function cfgSaveModelOptions() {
  const resultEl = document.getElementById('cfg-save-result-model-options');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const current = _cfgData.personality || {};
  const next = Object.assign({}, current, {
    model_options: {
      temperature: Number(document.getElementById('cfg-personality-model_options-temperature').value),
      top_p: Number(document.getElementById('cfg-personality-model_options-top_p').value),
      num_ctx: parseInt(document.getElementById('cfg-personality-model_options-num_ctx').value, 10),
      repeat_penalty: Number(document.getElementById('cfg-personality-model_options-repeat_penalty').value),
    }
  });
  try {
    const r = await fetch('/api/config/personality', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData.personality = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

// LLM Timeouts: targeted update of the global.tuning.llm_*_timeout_s
// fields, preserving all other tuning + global settings.
async function cfgSaveLLMTimeouts() {
  const resultEl = document.getElementById('cfg-save-result-timeouts');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const current = _cfgData.global || {};
  const tuning = Object.assign({}, current.tuning || {}, {
    llm_connect_timeout_s: parseInt(document.getElementById('cfg-llm-connect-timeout').value, 10),
    llm_read_timeout_s: parseInt(document.getElementById('cfg-llm-read-timeout').value, 10),
  });
  const next = Object.assign({}, current, { tuning });
  try {
    const r = await fetch('/api/config/global', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData.global = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

// Phase 6 follow-up: System tab absorbs auth, audit, and the two
// maintenance_* mode entities that used to live under Integrations.
// These forms render into the System tab and save back to the 'global'
// backing. They use section name 'sysaux' for field IDs so they don't
// collide with an Integrations page that may still have the full
// global form rendered in another tab's DOM.

function loadSystemConfigCards() {
  // _cfgData may not be loaded yet on first visit — fetch if empty.
  const have = _cfgData && _cfgData.global;
  const run = () => {
    _cfgRenderSystemMaintForm();
    _cfgRenderSystemAuthAuditForm();
  };
  if (have) { run(); }
  else if (typeof cfgLoadAll === 'function') { cfgLoadAll().then(run); }
}

// Test harness UI removed 2026-04-25 (operator-directed polish sweep).
// The /api/test-harness/noise-patterns GET endpoint is KEPT — the
// external test battery (glados-test-battery/harness.py) depends on it.
// test_harness.yaml is still read/written by config_store; the UI just
// no longer exposes it.

function _cfgRenderSystemMaintForm() {
  const me = (_cfgData.global || {}).mode_entities || {};
  // Only the maintenance pair — silent_mode / dnd belong on Audio & Speakers.
  const subset = {
    mode_entities: {
      maintenance_mode:    me.maintenance_mode    || '',
      maintenance_speaker: me.maintenance_speaker || '',
    },
  };
  const host = document.getElementById('sysMaintForm');
  if (!host) return;
  host.innerHTML = cfgBuildForm(subset, 'sysaux', '', null);
}

function _cfgRenderSystemAuthAuditForm() {
  const g = _cfgData.global || {};
  const subset = {
    auth:  g.auth  || {},
    audit: g.audit || {},
  };
  const host = document.getElementById('sysAuthAuditForm');
  if (!host) return;
  host.innerHTML = cfgBuildForm(subset, 'sysaux', '', null);
}

// Generic save helper for the System-tab subset forms. Collects every
// `[id^="cfg-sysaux-"]` input inside `scopeEl`, rebuilds nested paths
// from `data-path`, deep-merges the result into a copy of _cfgData.global,
// and PUTs /api/config/global. Scoping to the form element prevents
// stray inputs from other cards bleeding into the payload.
async function _cfgSaveSystemSubset(scopeEl, resultElId) {
  const resultEl = document.getElementById(resultElId);
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }

  const delta = {};
  scopeEl.querySelectorAll('[id^="cfg-sysaux-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else if (type === 'array') val = el.value.split(',').map(s => s.trim()).filter(Boolean);
    else val = el.value;

    const parts = path.split('.');
    let cur = delta;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in cur)) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = val;
  });

  // Deep-merge delta into a snapshot of the current global config so we
  // don't clobber sibling fields (silent_hours, tuning, home_assistant, etc.).
  const next = JSON.parse(JSON.stringify(_cfgData.global || {}));
  const _merge = (dst, src) => {
    for (const k of Object.keys(src)) {
      if (src[k] && typeof src[k] === 'object' && !Array.isArray(src[k])) {
        if (!dst[k] || typeof dst[k] !== 'object') dst[k] = {};
        _merge(dst[k], src[k]);
      } else {
        dst[k] = src[k];
      }
    }
  };
  _merge(next, delta);

  try {
    const r = await fetch('/api/config/global', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next),
    });
    // The legacy /api/config/<section> handler uses _send_error for
    // non-ok paths, which emits plain text rather than JSON. Parse
    // defensively so our handler doesn't swallow the real error behind
    // "Unexpected token 'V'".
    const bodyText = await r.text();
    let resp = {};
    try { resp = JSON.parse(bodyText); } catch (_) { resp = { error: bodyText }; }
    if (r.ok) {
      _cfgData.global = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

async function cfgSaveSystemMaint() {
  const form = document.getElementById('sysMaintForm');
  if (form) await _cfgSaveSystemSubset(form, 'cfg-save-result-sys-maint');
}

async function cfgSaveSystemAuthAudit() {
  const form = document.getElementById('sysAuthAuditForm');
  if (form) await _cfgSaveSystemSubset(form, 'cfg-save-result-sys-authaudit');
}

// Phase 6 merged page: renders Speakers + Audio side-by-side with
// per-subsection Save buttons (each targets its own backing section).
function _cfgRenderAudioSpeakers() {
  // Phase 6.5.2 (2026-04-22): converted to page-tabs. Five tabs map
  // to the five concerns that used to stack vertically.
  const speakers = _cfgData.speakers;
  const audio = _cfgData.audio;
  if (!speakers || !audio) {
    document.getElementById('cfg-form-area').innerHTML =
      '<div style="color:#ff6666;padding:20px;">Audio &amp; Speakers sections not loaded. Click Reload.</div>';
    return;
  }
  const meta = SECTION_META['audio-speakers'] || {};

  const TABS = [
    { id: 'speakers',      label: 'Speakers' },
    { id: 'response',      label: 'Response behavior' },
    { id: 'pronunciation', label: 'Pronunciation' },
    { id: 'chimes',        label: 'Sounds' },
  ];
  const activeTabId = _loadPageTab('audio-speakers', 'speakers');

  let html = '<div class="page-header">'
    + '<div>'
    +   '<h2 class="page-title">' + escHtml(meta.title || 'Audio & Speakers') + '</h2>'
    +   (meta.desc ? '<div class="page-title-desc">' + escHtml(meta.desc) + '</div>' : '')
    + '</div>'
    + '<button class="page-save-btn" onclick="_cfgSaveCurrentAudioSpeakersTab()" title="Save the active tab">'
    +   _floppySvg() + '<span>Save</span>'
    + '</button>'
    + '</div>';

  html += '<nav class="page-tabs" role="tablist">';
  for (const t of TABS) {
    const cls = t.id === activeTabId ? 'page-tab active' : 'page-tab';
    html += '<button class="' + cls + '" role="tab" data-page-tab-group="audio-speakers" data-tab="' + t.id + '" onclick="showPageTab(\'audio-speakers\',\'' + t.id + '\')">'
      + escHtml(t.label) + '</button>';
  }
  html += '</nav>';

  html += '<div class="page-tab-panels">';

  // ── Speakers tab ──────────────────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'speakers' ? ' active' : '') + '" data-page-tab-panel-group="audio-speakers" data-tab="speakers">';
  html += '<div class="card">';
  html += '<div class="cfg-subsection-title">Speakers</div>';
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    + 'Media players detected on Home Assistant. Check the ones GLaDOS is allowed '
    + 'to announce on; uncheck to exclude. Pick a default for Maintenance Mode below.'
    + '</div>';
  html += '<div id="cfg-speakers-body">Loading detected speakers&hellip;</div>';
  html += '</div>';

  html += '<div class="card" id="cfg-startup-speakers-card" style="margin-top:var(--sp-3);">';
  html +=   '<div class="cfg-subsection-title">Startup speakers</div>';
  html +=   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
        +    'Which speakers announce GLaDOS&rsquo;s startup. Checked speakers '
        +    'receive the boot announcement; unchecked stay silent. '
        +    'Requires a container restart to apply.'
        +  '</div>';
  html +=   '<div id="startupSpeakers" style="opacity:0.5;">Loading&hellip;</div>';
  html +=   '<div id="startupSpeakersStatus" style="font-size:0.75rem;color:var(--orange);margin-top:6px;min-height:1.2em;"></div>';
  html += '</div>';
  html += '</div>';

  // ── Response behavior tab ─────────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'response' ? ' active' : '') + '" data-page-tab-panel-group="audio-speakers" data-tab="response">';
  html += '<div class="card" id="cfg-response-behavior-card">';
  html += '<div class="cfg-subsection-title">Response behavior</div>';
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    + 'Choose how GLaDOS acknowledges commands. '
    + '<strong>LLM</strong> (default) has the language model write each reply &mdash; expressive but can drift. '
    + '<strong>Quip</strong> picks a pre-written line from <code>configs/quips/</code> &mdash; never leaks device names, no drift. '
    + '<strong>Chime</strong> plays a sound file. '
    + '<strong>Silent</strong> makes no audible reply at all.'
    + '</div>';
  html += '<div id="cfg-response-behavior-body">Loading&hellip;</div>';
  html += '</div>';
  html += '</div>';

  // ── Pronunciation tab ─────────────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'pronunciation' ? ' active' : '') + '" data-page-tab-panel-group="audio-speakers" data-tab="pronunciation">';
  html += '<div class="card" id="cfg-pronunciation-card">';
  html += '<div class="cfg-subsection-title">TTS Pronunciation overrides</div>';
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    + 'Piper pronounces short abbreviations poorly by default &mdash; '
    + '<code>AI</code> becomes one slurred letter, <code>HA</code> reads '
    + 'mechanically. These overrides expand each key before the text-to-speech '
    + 'converter processes it. <strong>Word expansions</strong> match whole '
    + 'words case-insensitively. <strong>Symbol expansions</strong> replace '
    + 'literal characters. One <code>key = value</code> pair per line.'
    + '</div>';
  html += '<div id="cfg-pronunciation-body">Loading&hellip;</div>';
  html += '</div>';
  html += '</div>';

  // ── Chimes tab ────────────────────────────────────────────
  html += '<div class="page-tab-panel' + (activeTabId === 'chimes' ? ' active' : '') + '" data-page-tab-panel-group="audio-speakers" data-tab="chimes">';
  html += '<div class="card" id="cfg-chimes-card">';
  html += '<div class="cfg-subsection-title">Chime library</div>';
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    + 'Short sound clips played before announcements and for the '
    + '<strong>chime</strong> response mode. Allowed formats: '
    + '<code>.wav</code>, <code>.mp3</code>. 5 MB per clip. '
    + 'Flat library (no subdirectories). Upload / delete actions save immediately.'
    + '</div>';
  html += '<div id="cfg-chimes-body">Loading&hellip;</div>';
  html += '</div>';
  html += '</div>';

  html += '</div>';  // end page-tab-panels

  document.getElementById('cfg-form-area').innerHTML = html;
  setTimeout(loadStartupSpeakers, 0);
  setTimeout(_cfgLoadSpeakersPicker, 0);
  setTimeout(_cfgLoadResponseBehavior, 0);
  setTimeout(_cfgLoadPronunciation, 0);
  setTimeout(_cfgLoadChimes, 0);
}

// Page-save dispatcher for System (Phase 6.5.3). Status, Hardware, and
// Maintenance have no direct save; Mode saves auth+audit, Services
// saves the service endpoint grid.
function _cfgSaveCurrentSystemTab() {
  const active = document.querySelector('[data-page-tab-group="system"].active');
  const id = active ? active.getAttribute('data-tab') : 'status';
  switch (id) {
    case 'mode':     return cfgSaveSystemAuthAudit();
    case 'services': return _cfgSaveSystemServices();
    case 'hardware':
      showToast('Hardware tab has no saveable fields. Toggle changes apply immediately.', 'info');
      return;
    case 'status':
      showToast('Status tab is read-only. Restart buttons on each service save-by-action.', 'info');
      return;
    case 'maintenance':
      showToast('Maintenance actions save immediately; no separate save needed.', 'info');
      return;
    case 'ssl':    return cfgSaveSsl();
    case 'users':
      showToast('User actions save immediately via their own buttons.', 'info');
      return;
    default: return;
  }
}

function _loadSslIntoSystemTab() {
  cfgLoadAll().then(function() {
    var ssl = (_cfgData.global && _cfgData.global.ssl) ? _cfgData.global.ssl : {};
    var mount = document.getElementById('systemSslMount');
    if (mount) mount.innerHTML = cfgRenderSsl(ssl);
  });
}

function _loadUsersIntoSystemTab() {
  var mount = document.getElementById('systemUsersMount');
  if (!mount) return;
  if (mount.children.length === 0) {
    // First visit: clone the users content from the legacy panel.
    var src = document.getElementById('tab-config-users');
    if (src) {
      mount.innerHTML = src.innerHTML;
    }
  }
  if (typeof usersLoadAll === 'function') usersLoadAll();
}

// Page-save dispatcher for Audio & Speakers. Routes by active tab
// to the appropriate save handler; tabs without a clean master save
// (chime uploads/deletes save immediately) just toast a note.
function _cfgSaveCurrentAudioSpeakersTab() {
  const active = document.querySelector('[data-page-tab-group="audio-speakers"].active');
  const id = active ? active.getAttribute('data-tab') : 'speakers';
  switch (id) {
    case 'speakers':      return _cfgSaveSpeakersPicker();
    case 'response':
    case 'pronunciation': {
      // Click the card's own Save button if present.
      const host = document.getElementById('cfg-' + (id === 'response' ? 'response-behavior' : 'pronunciation') + '-body');
      const btn = host && host.querySelector('button');
      if (btn) btn.click();
      return;
    }
    case 'chimes':
      showToast('Sound uploads and deletes save immediately. No separate save needed.', 'info');
      return;
    default: return;
  }
}



// ── Phase 8.7 chime library JS ──────────────────────────────────

async function _cfgLoadChimes() {
  const body = document.getElementById('cfg-chimes-body');
  if (!body) return;
  try {
    const r = await fetch('/api/chimes');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _chimesPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _chimesPopulate(data) {
  const body = document.getElementById('cfg-chimes-body');
  if (!body) return;
  const files = Array.isArray(data.files) ? data.files : [];
  const tableRows = files.length
    ? files.map(f =>
        '<tr>'
        + '<td style="padding:4px 8px;font-family:monospace;">' + escHtml(f.name) + '</td>'
        + '<td style="padding:4px 8px;color:var(--fg-secondary);text-align:right;">'
        +   _chimesFmtBytes(f.bytes)
        + '</td>'
        + '<td style="padding:4px 8px;">'
        +   '<button class="btn-small" onclick="_chimesPlay(\'' + encodeURIComponent(f.name) + '\')" style="font-size:0.72rem;padding:3px 10px;margin-right:6px;">Play</button>'
        +   '<button class="btn btn-danger" onclick="_chimesDelete(\'' + encodeURIComponent(f.name) + '\')" style="font-size:0.72rem;padding:3px 10px;">Delete</button>'
        + '</td>'
        + '</tr>'
      ).join('')
    : '<tr><td colspan="3" style="padding:8px;color:var(--fg-secondary);font-style:italic;">No chime files. Upload one below.</td></tr>';
  let html = '';
  html += '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:12px;">';
  html += '<thead><tr style="background:var(--bg-input);">'
    +   '<th style="padding:6px 8px;text-align:left;">File</th>'
    +   '<th style="padding:6px 8px;text-align:right;">Size</th>'
    +   '<th style="padding:6px 8px;text-align:left;width:160px;">Actions</th>'
    + '</tr></thead>';
  html += '<tbody>' + tableRows + '</tbody>';
  html += '</table>';
  html += '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
    +   '<input type="file" id="cfg-chimes-upload" accept=".wav,.mp3,audio/wav,audio/mpeg" '
    +     'style="flex:1;min-width:220px;background:var(--bg-input);color:var(--fg-primary);'
    +     'border:1px solid var(--border-default);border-radius:4px;padding:5px 8px;font-size:0.82rem;">'
    +   '<button class="btn-small" onclick="_chimesUpload()" style="font-size:0.78rem;padding:5px 14px;">Upload</button>'
    +   '<audio id="cfg-chimes-player" controls style="flex:2;min-width:240px;"></audio>'
    + '</div>';
  body.innerHTML = html;
}

function _chimesFmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function _chimesPlay(encodedName) {
  const audio = document.getElementById('cfg-chimes-player');
  if (!audio) return;
  audio.src = '/api/chimes?path=' + encodedName;
  audio.play().catch(e => { /* browsers may require user gesture; fallthrough */ });
}

async function _chimesDelete(encodedName) {
  const decoded = decodeURIComponent(encodedName);
  if (!confirm('Delete chime "' + decoded + '"?')) return;
  try {
    const r = await fetch('/api/chimes?path=' + encodedName, { method: 'DELETE' });
    const resp = r.ok ? await r.json() : { error: 'HTTP ' + r.status };
    if (r.ok) {
      showToast('Deleted ' + decoded, 'success');
      _cfgLoadChimes();
    } else {
      showToast('Delete failed: ' + (resp.error || 'unknown'), 'error');
    }
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function _chimesUpload() {
  const input = document.getElementById('cfg-chimes-upload');
  if (!input || !input.files || !input.files[0]) {
    showToast('Choose a file first', 'warn');
    return;
  }
  const file = input.files[0];
  const name = file.name;
  const MAX = 5 * 1024 * 1024;
  if (file.size > MAX) {
    showToast('File exceeds 5 MB', 'error');
    return;
  }
  const b64 = await _fileToBase64(file);
  try {
    const r = await fetch('/api/chimes', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: name, data_b64: b64 }),
    });
    const resp = r.ok ? await r.json() : { error: await r.text() };
    if (r.ok) {
      showToast('Uploaded ' + name, 'success');
      input.value = '';
      _cfgLoadChimes();
    } else {
      showToast('Upload failed: ' + (resp.error || 'HTTP ' + r.status), 'error');
    }
  } catch (e) {
    showToast('Upload failed: ' + e.message, 'error');
  }
}

function _fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result || '';
      const comma = String(result).indexOf(',');
      resolve(comma >= 0 ? String(result).slice(comma + 1) : String(result));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function _cfgLoadPronunciation() {
  const body = document.getElementById('cfg-pronunciation-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/tts_pronunciation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _pronunciationPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _pronunciationPopulate(data) {
  const body = document.getElementById('cfg-pronunciation-body');
  if (!body) return;
  const sym = data.symbol_expansions || {};
  const words = data.word_expansions || {};
  const symText = Object.entries(sym).map(a => a[0] + ' = ' + a[1]).join('\n');
  const wordText = Object.entries(words).map(a => a[0] + ' = ' + a[1]).join('\n');
  const ta = 'background:var(--bg-input);color:var(--fg-primary);border:1px solid var(--border-default);'
    + 'border-radius:4px;padding:8px;width:100%;font-family:monospace;font-size:0.82rem;';
  let html = '';
  html += '<div class="cfg-field">'
    + '<label class="cfg-label">Word expansions <span style="color:var(--fg-secondary);font-weight:normal;">(whole-word, case-insensitive)</span></label>'
    + '<textarea id="cfg-pr-words" rows="6" style="' + ta + '">' + escHtml(wordText) + '</textarea>'
    + '</div>';
  html += '<div class="cfg-field" style="margin-top:10px;">'
    + '<label class="cfg-label">Symbol expansions <span style="color:var(--fg-secondary);font-weight:normal;">(literal replace, e.g. <code>%</code>, <code>&amp;</code>)</span></label>'
    + '<textarea id="cfg-pr-symbols" rows="3" style="' + ta + '">' + escHtml(symText) + '</textarea>'
    + '</div>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSavePronunciation()">Save Pronunciation</button>'
    + '<span id="cfg-save-result-pronunciation" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
}

function _parsePronunciationRows(text) {
  const out = {};
  String(text || '').split(/\r?\n/).forEach(line => {
    const t = line.trim();
    if (!t) return;
    const eq = t.indexOf('=');
    if (eq < 1) return;
    const k = t.substring(0, eq).trim();
    const v = t.substring(eq + 1).trim();
    if (k) out[k] = v;
  });
  return out;
}

async function cfgSavePronunciation() {
  const resultEl = document.getElementById('cfg-save-result-pronunciation');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const words = _parsePronunciationRows(document.getElementById('cfg-pr-words').value);
  const symbols = _parsePronunciationRows(document.getElementById('cfg-pr-symbols').value);
  try {
    const r = await fetch('/api/config/tts_pronunciation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        word_expansions: words,
        symbol_expansions: symbols,
      }),
    });
    const txt = await r.text();
    let resp = {};
    try { resp = JSON.parse(txt); } catch (_) { resp = { error: txt }; }
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Pronunciation overrides saved. Restart TTS to fully apply.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

async function _cfgLoadResponseBehavior() {
  const body = document.getElementById('cfg-response-behavior-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _responseBehaviorPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _responseBehaviorPopulate(data) {
  const body = document.getElementById('cfg-response-behavior-body');
  if (!body) return;
  const globalMode = (typeof data.response_mode === 'string') ? data.response_mode : 'LLM';
  const perEvent = (data.response_mode_per_event && typeof data.response_mode_per_event === 'object')
    ? data.response_mode_per_event : {};
  const MODES = [
    { value: 'LLM',      label: 'LLM (planner speech, pass-through)' },
    { value: 'LLM_safe', label: 'LLM (safe, no device names)' },
    { value: 'quip',     label: 'Quip (pre-written library)' },
    { value: 'chime',    label: 'Chime (sound file)' },
    { value: 'silent',   label: 'Silent (no reply)' },
  ];
  const EVENT_ROWS = [
    { key: 'command_ack',  label: 'Command acknowledgement',  desc: 'Replies after a light / switch / scene command fires.' },
    { key: 'query_answer', label: 'Query answer',             desc: 'Replies to "is the kitchen on?" and similar.' },
    { key: 'ambient_cue',  label: 'Ambient cue',              desc: 'Replies to "it\'s too dark", "time to read".' },
    { key: 'error',        label: 'Error / failure',          desc: 'Replies when a transition did not land.' },
  ];
  function modeSelect(id, value) {
    let h = '<select id="' + id + '">';
    h += '<option value="">&mdash; inherit global &mdash;</option>';
    MODES.forEach(m => {
      const sel = (m.value === value) ? ' selected' : '';
      h += '<option value="' + m.value + '"' + sel + '>' + escHtml(m.label) + '</option>';
    });
    h += '</select>';
    return h;
  }
  let html = '';
  html += '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">'
    +   '<label class="cfg-field-label" for="cfg-rb-global" style="margin:0;min-width:140px;">Global mode</label>'
    +   '<select id="cfg-rb-global" style="flex:1;min-width:200px;">';
  MODES.forEach(m => {
    const sel = (m.value === globalMode) ? ' selected' : '';
    html += '<option value="' + m.value + '"' + sel + '>' + escHtml(m.label) + '</option>';
  });
  html += '</select></div>';
  html += '<div class="cfg-field-label" style="margin-top:12px;">Per-event override</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Leave rows at <em>inherit global</em> unless you want a specific category to behave differently (for example, '
    +   'silent command acknowledgements but LLM replies for queries).'
    + '</div>';
  EVENT_ROWS.forEach(row => {
    const current = perEvent[row.key] || '';
    html += '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px;">'
      +   '<label class="cfg-field-label" style="margin:0;min-width:180px;flex-shrink:0;" for="cfg-rb-ev-' + row.key + '">'
      +     escHtml(row.label)
      +   '</label>'
      +   modeSelect('cfg-rb-ev-' + row.key, current)
      + '</div>'
      + '<div class="cfg-field-desc" style="margin:-4px 0 4px 190px;">' + escHtml(row.desc) + '</div>';
  });
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveResponseBehavior()">Save Response behavior</button>'
    + '<span id="cfg-save-result-rb" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
}

async function cfgSaveResponseBehavior() {
  const resultEl = document.getElementById('cfg-save-result-rb');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const globalEl = document.getElementById('cfg-rb-global');
  const globalMode = globalEl ? String(globalEl.value || 'LLM') : 'LLM';
  const perEvent = {};
  ['command_ack', 'query_answer', 'ambient_cue', 'error'].forEach(k => {
    const el = document.getElementById('cfg-rb-ev-' + k);
    const v = el ? String(el.value || '') : '';
    if (v) perEvent[k] = v;
  });
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        response_mode: globalMode,
        response_mode_per_event: perEvent,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Response behavior saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

function cfgBuildForm(obj, section, prefix, skipKeys) {
  let html = '';
  for (const [key, value] of Object.entries(obj)) {
    // Top-level skip list: callers pass keys that belong to a dedicated
    // page (e.g. `ssl` has its own tab) or are env-only. Only checked at
    // the top level so a nested field named `ssl` inside another group
    // is not accidentally hidden.
    if (skipKeys && !prefix && skipKeys.indexOf(key) !== -1) continue;
    const path = prefix ? prefix + '.' + key : key;
    const fieldId = 'cfg-' + section + '-' + path.replace(/\./g, '-');
    const meta = FIELD_META[path] || {};
    // Phase 6 user-friendly pass: hidden-flagged fields stay in the schema
    // (Raw YAML / env still drive them) but disappear from the friendly
    // form so non-technical users aren't asked to touch deprecated paths,
    // env-only fields, or settings that have no UI-writable effect.
    if (meta.hidden) continue;
    const label = meta.label || key;
    const desc = meta.desc || '';
    const isAdvanced = meta.advanced === true;
    const advAttr = isAdvanced ? ' data-advanced="true"' : '';

    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      // Skip the group entirely if every child is hidden — otherwise we'd
      // render an empty <div class="cfg-group"> with just a heading, which
      // is what Commit 1 explicitly set out to avoid elsewhere.
      const childKeys = Object.keys(value);
      const visibleChildKeys = childKeys.filter(k => {
        const childPath = path ? path + '.' + k : k;
        return !((FIELD_META[childPath] || {}).hidden === true);
      });
      if (visibleChildKeys.length === 0) continue;
      // Check if every VISIBLE child is advanced — if so, the whole group
      // can collapse behind the Advanced toggle. Hidden children don't
      // factor in; they're never rendered either way.
      const groupAdvanced = visibleChildKeys.every(k => {
        const childPath = path ? path + '.' + k : k;
        return (FIELD_META[childPath] || {}).advanced === true;
      });
      const gAdvAttr = groupAdvanced ? ' data-advanced="true"' : '';
      html += '<div class="cfg-group"' + gAdvAttr + '><div class="cfg-group-title">' + escHtml(key) + '</div>';
      html += cfgBuildForm(value, section, path, skipKeys);
      html += '</div>';
    } else if (Array.isArray(value)) {
      // Skip arrays of objects (handled by custom renderers)
      if (value.length > 0 && typeof value[0] === 'object') continue;
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<input id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="array"'
        + ' value="' + escAttr(value.join(', ')) + '" placeholder="comma-separated values">'
        + '</div>';
    } else if (typeof value === 'boolean') {
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<select id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="bool">'
        + '<option value="true"' + (value ? ' selected' : '') + '>true</option>'
        + '<option value="false"' + (!value ? ' selected' : '') + '>false</option>'
        + '</select></div>';
    } else if (meta.options && Array.isArray(meta.options)) {
      // Dropdown select from predefined options
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<select id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="' + typeof value + '">';
      for (const opt of meta.options) {
        const sel = (String(value) === String(opt)) ? ' selected' : '';
        html += '<option value="' + escAttr(opt) + '"' + sel + '>' + escHtml(opt) + '</option>';
      }
      html += '</select></div>';
    } else {
      const inputType = (meta.type === 'password') ? 'password' : (typeof value === 'number' ? 'number' : 'text');
      const step = typeof value === 'number' && !Number.isInteger(value) ? ' step="any"' : '';
      let displayVal = value ?? '';
      let hintHtml = '';
      // Path masking
      if (meta.pathMask && typeof displayVal === 'string' && displayVal.startsWith(meta.pathMask)) {
        const fullPath = displayVal;
        displayVal = displayVal.slice(meta.pathMask.length);
        hintHtml = '<div class="cfg-field-hint">Full path: ' + escHtml(fullPath) + '</div>';
      }
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<input id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="' + typeof value + '"'
        + (meta.pathMask ? ' data-path-mask="' + escAttr(meta.pathMask) + '"' : '')
        + ' type="' + inputType + '"' + step + ' value="' + escAttr(String(displayVal)) + '">'
        + hintHtml + '</div>';
    }
  }
  return html;
}

/* â”€â”€ Services custom renderer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

// Decide which discovery endpoint a given service should use.
// Ollama URLs go through /api/tags; TTS URLs through /v1/voices.
function _svcDiscoverKind(key) {
  if (key === 'tts') return 'voices';
  if (key.indexOf('llm_') === 0) return 'ollama';
  return null;
}

// Services that are deprecated / have no operator-facing effect — kept
// in the schema for backward compatibility but hidden from the friendly
// Services grid. Raw YAML still shows them.
const SERVICES_HIDDEN = new Set(['gladys_api']);


// Phase 2 Chunk 2 — System → Services tab: port-grouped status panel + LLM section.
// Replaces the old URL-input grid for in-container services.
async function loadSystemServices() {
  const body = document.getElementById('system-services-body');
  if (!body) return;
  if (!_cfgData.services) {
    try { await cfgLoadAll(); } catch (e) { /* continue with empty */ }
  }
  const svc = _cfgData.services || {};
  const mo = (_cfgData.personality || {}).model_options || {};
  const t = ((_cfgData.global || {}).tuning) || {};

  // Ollama interactive is the primary LLM endpoint shown.
  const ollama = svc.llm_interactive || {};
  const vision = svc.vision || {};
  const visionConfigured = !!(vision.url || '').trim();

  let html = '';

  // ── Card 1: Service Endpoints (port-grouped status) ─────────
  html += '<div class="card">';
  html +=   '<div class="section-title">Service Endpoints</div>';
  html +=   '<div class="mode-desc" style="margin-bottom:14px;">'
    +         'In-container services share port 8015. Vision is optional and external.'
    +       '</div>';

  // :8015 row — TTS, STT, API Wrapper (in-container)
  html +=   '<div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">';
  html +=     '<span style="font-family:var(--font-mono);font-size:0.82rem;color:var(--fg-primary);min-width:50px;">:8015</span>';
  html +=     '<span class="svc-health-dot" id="svc-dot-tts"></span>';
  html +=     '<span style="font-size:0.85rem;margin-right:14px;">TTS</span>';
  html +=     '<span class="svc-health-dot" id="svc-dot-stt"></span>';
  html +=     '<span style="font-size:0.85rem;margin-right:14px;">STT</span>';
  html +=     '<span class="svc-health-dot" id="svc-dot-api_wrapper"></span>';
  html +=     '<span style="font-size:0.85rem;">API Wrapper</span>';
  html +=   '</div>';
  html +=   '<div class="mode-desc" style="margin-bottom:14px;padding-left:60px;">'
    +         'In-container — no URL configuration. These start and stop with GLaDOS.'
    +       '</div>';

  // :vision row — Vision (external, optional)
  const visionDotCls = 'svc-health-dot' + (visionConfigured ? '' : '');
  html +=   '<div style="display:flex;gap:10px;align-items:center;margin-bottom:4px;">';
  html +=     '<span style="font-family:var(--font-mono);font-size:0.82rem;color:var(--fg-primary);min-width:50px;">vision</span>';
  if (visionConfigured) {
    html +=   '<span class="svc-health-dot" id="svc-dot-vision"></span>';
    html +=   '<span style="font-size:0.85rem;">Vision</span>';
  } else {
    html +=   '<span class="svc-health-dot" id="svc-dot-vision"></span>';
    html +=   '<span style="font-size:0.85rem;font-style:italic;color:var(--fg-muted);">Vision &mdash; inactive (not configured)</span>';
  }
  html +=   '</div>';
  html +=   '<div class="mode-desc" style="padding-left:60px;">'
    +         'External vision service. Configure via <code>VISION_URL</code> env var. '
    +         'Unconfigured or unreachable: vision feature unavailable, other functions unaffected.'
    +       '</div>';

  html += '</div>'; // end Card 1

  // ── Card 2: LLM (Ollama) ────────────────────────────────────
  html += '<div class="card" style="margin-top:var(--sp-3);">';
  html +=   '<div class="section-title">LLM (Ollama)</div>';

  // URL field
  html +=   '<div class="cfg-field">';
  html +=     '<label class="cfg-field-label">URL</label>';
  html +=     '<div class="svc-url-row">';
  html +=       '<input id="system-llm-url" type="text" value="' + escAttr(ollama.url || '') + '"'
    +           ' onblur="_systemLlmDiscover()">';
  html +=       '<button type="button" class="svc-discover-btn" onclick="_systemLlmDiscover()">&#x21bb; Discover</button>';
  html +=       '<span class="svc-discover-status" id="system-llm-discover-status"></span>';
  html +=     '</div>';
  html +=   '</div>';

  // Model dropdown
  html +=   '<div class="cfg-field">';
  html +=     '<label class="cfg-field-label">Model</label>';
  html +=     '<select id="system-llm-model" class="svc-dropdown">';
  html +=       '<option value="' + escAttr(ollama.model || '') + '" selected>'
    +           escHtml(ollama.model || '(enter URL then click Discover)') + '</option>';
  html +=     '</select>';
  html +=   '</div>';

  // Status line
  html +=   '<div class="cfg-field">';
  html +=     '<label class="cfg-field-label">Status</label>';
  html +=     '<span id="system-llm-status" style="font-size:0.85rem;color:var(--fg-muted);">—</span>';
  html +=   '</div>';

  // Advanced collapsible
  html +=   '<div style="margin-top:12px;">';
  html +=     '<button type="button" class="cfg-save-btn" style="background:none;border:none;padding:0;'
    +         'color:var(--fg-primary);font-size:0.85rem;cursor:pointer;display:flex;align-items:center;gap:6px;"'
    +         ' onclick="_systemLlmToggleAdvanced(this)">'
    +         '<span id="system-llm-adv-caret" style="font-size:0.7rem;">&#9656;</span>'
    +         'Advanced (Model Options &amp; Timeouts)'
    +         '</button>';
  html +=     '<div id="system-llm-advanced" style="display:none;margin-top:10px;">';

  // Model Options
  html +=       '<div class="cfg-subsection-title">Model Options</div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Temperature</label>'
    +           '<div class="cfg-field-desc">0.0 is deterministic, 1.0+ is creative</div>'
    +           '<input id="cfg-personality-model_options-temperature" data-path="model_options.temperature" data-type="number" type="number" step="any" value="' + escAttr(String(mo.temperature ?? 0.7)) + '"></div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Top P</label>'
    +           '<div class="cfg-field-desc">Nucleus sampling threshold (0.0 - 1.0)</div>'
    +           '<input id="cfg-personality-model_options-top_p" data-path="model_options.top_p" data-type="number" type="number" step="any" value="' + escAttr(String(mo.top_p ?? 0.9)) + '"></div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Context Window (num_ctx)</label>'
    +           '<div class="cfg-field-desc">Tokens of context the model sees per turn</div>'
    +           '<input id="cfg-personality-model_options-num_ctx" data-path="model_options.num_ctx" data-type="number" type="number" value="' + escAttr(String(mo.num_ctx ?? 16384)) + '"></div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Repeat Penalty</label>'
    +           '<div class="cfg-field-desc">Higher values reduce parroting (typical 1.0 - 1.3)</div>'
    +           '<input id="cfg-personality-model_options-repeat_penalty" data-path="model_options.repeat_penalty" data-type="number" type="number" step="any" value="' + escAttr(String(mo.repeat_penalty ?? 1.1)) + '"></div>';

  // LLM Timeouts
  html +=       '<div class="cfg-subsection-title" style="margin-top:12px;">LLM Timeouts</div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Connect Timeout (s)</label>'
    +           '<div class="cfg-field-desc">Seconds to wait for LLM connection</div>'
    +           '<input id="cfg-llm-connect-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_connect_timeout_s ?? 10)) + '"></div>';
  html +=       '<div class="cfg-field"><label class="cfg-field-label">Read Timeout (s)</label>'
    +           '<div class="cfg-field-desc">Max seconds to wait for LLM response</div>'
    +           '<input id="cfg-llm-read-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_read_timeout_s ?? 180)) + '"></div>';

  html +=     '</div>'; // end #system-llm-advanced
  html +=   '</div>';   // end collapsible wrapper

  // Save button
  html +=   '<div class="cfg-save-row" style="margin-top:14px;">';
  html +=     '<button class="btn btn-primary" onclick="_cfgSaveSystemServices()">Save Services &amp; LLM</button>';
  html +=     '<span id="cfg-save-result-system-services" class="cfg-result"></span>';
  html +=   '</div>';

  html += '</div>'; // end Card 2

  body.innerHTML = html;

  // Ping in-container services via health aggregate.
  _systemServicesPingStatus(svc);
}

function _systemLlmToggleAdvanced(btn) {
  const adv = document.getElementById('system-llm-advanced');
  const caret = document.getElementById('system-llm-adv-caret');
  if (!adv) return;
  const open = adv.style.display !== 'none';
  adv.style.display = open ? 'none' : 'block';
  if (caret) caret.innerHTML = open ? '&#9656;' : '&#9662;';
}

async function _systemLlmDiscover() {
  const urlInput = document.getElementById('system-llm-url');
  const status = document.getElementById('system-llm-discover-status');
  const llmStatus = document.getElementById('system-llm-status');
  if (!urlInput) return;
  const url = (urlInput.value || '').trim().replace(/\/$/, '');
  if (!url) { if (status) status.textContent = ''; return; }
  if (status) { status.className = 'svc-discover-status'; status.textContent = 'discovering\u2026'; }
  try {
    const r = await fetch('/api/discover/ollama?url=' + encodeURIComponent(url));
    const data = await r.json();
    if (!r.ok) {
      if (status) { status.className = 'svc-discover-status err'; status.textContent = data.error || 'failed'; }
      if (llmStatus) llmStatus.textContent = 'Unreachable';
      return;
    }
    const models = (data.models || []).map(m => m.name);
    _svcPopulateDropdown('system-llm-model', models);
    if (status) { status.className = 'svc-discover-status ok'; status.textContent = data.count + ' models'; }
    if (llmStatus) llmStatus.innerHTML = '<span style="color:var(--green);">&#9679;</span> Connected &middot; ' + data.count + ' model(s)';
  } catch(e) {
    if (status) { status.className = 'svc-discover-status err'; status.textContent = 'error'; }
    if (llmStatus) llmStatus.textContent = 'Error: ' + e.message;
  }
}

async function _systemServicesPingStatus(svcData) {
  // Ping in-container services via /api/health/aggregate.
  try {
    const r = await fetch('/api/health/aggregate', { credentials: 'same-origin' });
    const data = await r.json();
    const byName = {};
    (data.services || []).forEach(s => { byName[s.name.toLowerCase()] = s.status; });
    const map = { tts: 'tts', stt: 'stt', api_wrapper: 'api', vision: 'vision' };
    for (const [key, aggName] of Object.entries(map)) {
      const dot = document.getElementById('svc-dot-' + key);
      if (!dot) continue;
      if (key === 'vision') {
        const vision = svcData.vision || {};
        const configured = !!(vision.url || '').trim();
        if (!configured) {
          dot.className = 'svc-health-dot';  // grey = inactive
          dot.title = 'Not configured';
        } else {
          const st = byName[aggName] || byName['vision'];
          dot.className = 'svc-health-dot ' + (st === 'ok' ? 'ok' : 'err');
        }
      } else {
        const st = byName[aggName];
        if (st) dot.className = 'svc-health-dot ' + (st === 'ok' ? 'ok' : 'err');
      }
    }
  } catch(e) { /* leave dots grey */ }
}

async function _cfgSaveSystemServices() {
  const resultEl = document.getElementById('cfg-save-result-system-services');
  if (resultEl) { resultEl.textContent = 'Saving\u2026'; resultEl.className = 'cfg-result'; }

  // 1. Save Ollama URL + model into services (preserve non-LLM keys).
  const urlInput = document.getElementById('system-llm-url');
  const modelSel = document.getElementById('system-llm-model');
  if (urlInput || modelSel) {
    const partial = Object.assign({}, _cfgData.services || {});
    if (!partial.llm_interactive) partial.llm_interactive = {};
    if (!partial.llm_autonomy)    partial.llm_autonomy = {};
    if (urlInput) {
      partial.llm_interactive.url = urlInput.value.trim();
      partial.llm_autonomy.url    = urlInput.value.trim();
    }
    if (modelSel && modelSel.value) {
      partial.llm_interactive.model = modelSel.value;
      partial.llm_autonomy.model    = modelSel.value;
    }
    try {
      const r = await fetch('/api/config/services', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(partial),
      });
      const resp = await r.json();
      if (!r.ok) {
        if (resultEl) { resultEl.textContent = resp.error || ('Error ' + r.status); resultEl.className = 'cfg-result err'; }
        return;
      }
      _cfgData.services = partial;
    } catch (e) {
      if (resultEl) { resultEl.textContent = String(e); resultEl.className = 'cfg-result err'; }
      return;
    }
  }

  // 2. Model Options — only if the advanced section was rendered.
  if (document.getElementById('cfg-personality-model_options-temperature')) {
    await cfgSaveModelOptions();
  }

  // 3. LLM Timeouts.
  if (document.getElementById('cfg-llm-connect-timeout')) {
    await cfgSaveLLMTimeouts();
  }

  if (resultEl) { resultEl.textContent = 'Saved'; resultEl.className = 'cfg-result cfg-result-ok'; }
}

function cfgRenderServices(data, scope) {
  // Phase 6.2 (2026-04-22): scope filters the service grid.
  //   scope === 'llm'    -> Ollama endpoints only (Integrations page)
  //   scope === 'system' -> TTS / STT / Vision / api_wrapper (System page)
  //   scope === null     -> full grid (legacy behavior)
  const _isLLM = k => k.indexOf('llm_') === 0;
  let html = '<div class="service-grid">';
  for (const [key, svc] of Object.entries(data)) {
    if (SERVICES_HIDDEN.has(key)) continue;
    if (scope === 'llm' && !_isLLM(key)) continue;
    if (scope === 'system' && _isLLM(key)) continue;
    const name = SERVICE_NAMES[key] || key;
    const urlId = 'cfg-services-' + key + '-url';
    const discoverKind = _svcDiscoverKind(key);
    const hasVoice = (key === 'tts' && svc.voice !== undefined);
    const hasModel = (svc.model !== undefined) || (discoverKind === 'ollama');
    html += '<div class="service-card">'
      + '<div class="service-card-header">'
      + '<span class="svc-health-dot" id="svc-dot-' + key + '"></span>'
      + '<span class="service-card-name">' + escHtml(name) + '</span>'
      + '</div>'
      + '<div class="cfg-field" style="margin-bottom:6px;">'
      + '<label class="cfg-field-label">URL</label>'
      + '<div class="svc-url-row">'
      +   '<input id="' + urlId + '" data-path="' + key + '.url" data-type="string" value="' + escAttr(svc.url || '') + '"'
      +     (discoverKind ? ' onblur="svcUrlBlur(\'' + escAttr(key) + '\')"' : '')
      +   '>';
    if (discoverKind) {
      html += '<button type="button" class="svc-discover-btn" title="Discover from upstream" onclick="svcDiscover(\'' + escAttr(key) + '\')">&#x21bb; Discover</button>';
    }
    html +=   '<span class="svc-discover-status" id="svc-status-' + key + '"></span>'
      + '</div>'
      + '</div>';
    if (hasVoice) {
      html += '<div class="cfg-field" style="margin-bottom:6px;">'
        + '<label class="cfg-field-label">Voice</label>'
        + '<select id="cfg-services-' + key + '-voice" data-path="' + key + '.voice" data-type="string" class="svc-dropdown">'
        +   '<option value="' + escAttr(svc.voice || '') + '" selected>' + escHtml(svc.voice || '(none)') + '</option>'
        + '</select>'
        + '</div>';
    }
    if (hasModel) {
      html += '<div class="cfg-field" style="margin-bottom:0;">'
        + '<label class="cfg-field-label">Model</label>'
        + '<select id="cfg-services-' + key + '-model" data-path="' + key + '.model" data-type="string" class="svc-dropdown">'
        +   '<option value="' + escAttr(svc.model || '') + '" selected>' + escHtml(svc.model || '(click Discover to list)') + '</option>'
        + '</select>'
        + '</div>';
    }
    html += '</div>';
  }
  html += '</div>';
  // Ping + seed dropdowns from current URLs.
  setTimeout(() => cfgPingServices(data), 100);
  return html;
}

// Map a service grid key to the probe kind discover_health uses.
// Ollama endpoints use /api/tags, TTS uses /v1/voices, STT uses
// /health, GLaDOS-own services use /health. Without this hint,
// every Ollama / TTS dot is false-red because /health returns 404.
function _svcHealthKind(key) {
  if (key.indexOf('llm_') === 0) return 'ollama';
  if (key === 'tts') return 'tts';
  if (key === 'stt') return 'stt';
  if (key === 'api_wrapper' || key === 'vision') return key;
  return null;
}

async function cfgPingServices(data) {
  for (const key of Object.keys(data)) {
    const dot = document.getElementById('svc-dot-' + key);
    if (!dot) continue;
    const url = (data[key].url || '').replace(/\/$/, '');
    if (!url) { dot.className = 'svc-health-dot err'; continue; }
    try {
      const hint = _svcHealthKind(key);
      const qs = 'url=' + encodeURIComponent(url)
               + (hint ? '&kind=' + encodeURIComponent(hint) : '');
      const r = await fetch('/api/discover/health?' + qs,
                             { signal: AbortSignal.timeout(3500) });
      const d = await r.json();
      dot.className = 'svc-health-dot ' + (d.ok ? 'ok' : 'err');
      if (d.latency_ms != null) dot.title = d.latency_ms + ' ms';
    } catch(e) {
      dot.className = 'svc-health-dot err';
    }
  }
}

// ── Service auto-discovery (Phase 5) ────────────────────────────
let _svcBlurTimers = {};

function svcUrlBlur(key) {
  // Debounce blur-triggered discovery so rapid tab-throughs don't fire
  // N simultaneous upstream calls. First one wins; subsequent blurs
  // inside 300ms are dropped.
  if (_svcBlurTimers[key]) clearTimeout(_svcBlurTimers[key]);
  _svcBlurTimers[key] = setTimeout(() => { svcDiscover(key); }, 300);
}

async function svcDiscover(key) {
  const kind = _svcDiscoverKind(key);
  if (!kind) return;
  const urlInput = document.getElementById('cfg-services-' + key + '-url');
  const status = document.getElementById('svc-status-' + key);
  if (!urlInput) return;
  const url = (urlInput.value || '').trim().replace(/\/$/, '');
  if (!url) {
    if (status) status.textContent = '';
    return;
  }
  if (status) { status.className = 'svc-discover-status'; status.textContent = 'discovering…'; }
  try {
    const r = await fetch('/api/discover/' + kind + '?url=' + encodeURIComponent(url));
    const data = await r.json();
    if (!r.ok) {
      if (status) { status.className = 'svc-discover-status err'; status.textContent = data.error || 'failed'; }
      return;
    }
    if (kind === 'ollama') {
      _svcPopulateDropdown('cfg-services-' + key + '-model', (data.models || []).map(m => m.name));
      if (status) { status.className = 'svc-discover-status ok'; status.textContent = data.count + ' models'; }
    } else if (kind === 'voices') {
      _svcPopulateDropdown('cfg-services-' + key + '-voice', (data.voices || []).map(v => v.name));
      if (status) { status.className = 'svc-discover-status ok'; status.textContent = data.count + ' voices'; }
    }
    // Refresh the dot too — the URL may have changed.
    const dot = document.getElementById('svc-dot-' + key);
    if (dot) {
      try {
        const hint = _svcHealthKind(key);
        const qs = 'url=' + encodeURIComponent(url)
                 + (hint ? '&kind=' + encodeURIComponent(hint) : '');
        const hr = await fetch('/api/discover/health?' + qs);
        const hd = await hr.json();
        dot.className = 'svc-health-dot ' + (hd.ok ? 'ok' : 'err');
        if (hd.latency_ms != null) dot.title = hd.latency_ms + ' ms';
      } catch(e) {}
    }
  } catch(e) {
    if (status) { status.className = 'svc-discover-status err'; status.textContent = 'error'; }
  }
}

function _svcPopulateDropdown(id, options) {
  const el = document.getElementById(id);
  if (!el) return;
  const current = el.value;
  const values = new Set(options || []);
  if (current) values.add(current);  // keep current selection even if upstream hasn't loaded it
  const sorted = Array.from(values).sort();
  let html = '';
  for (const v of sorted) {
    const sel = (v === current) ? ' selected' : '';
    html += '<option value="' + escAttr(v) + '"' + sel + '>' + escHtml(v) + '</option>';
  }
  if (html === '') {
    html = '<option value="">(no options returned)</option>';
  }
  el.innerHTML = html;
}

/* ─── Custom pbar slider helpers (Phase 2 Chunk 3) ─────────────── *
 * Wires click+drag on .pbar-wrap elements rendered by cfgRenderPersonality.
 * _pbarNumChange: called from the number input onchange attribute.
 * _pbarInit:      call after innerHTML is set to attach mousedown handlers.
 */

function _pbarSetValue(barId, numId, min, max, val) {
  val = Math.max(min, Math.min(max, val));
  const pct = ((val - min) / (max - min) * 100);
  const thumb = document.getElementById(barId + '-thumb');
  const fill  = document.getElementById(barId + '-fill');
  const num   = document.getElementById(numId);
  if (thumb) thumb.style.left = pct.toFixed(4) + '%';
  if (num) num.value = (min < 0)
    ? (val > 0 ? '+' + val.toFixed(2) : val.toFixed(2))
    : val.toFixed(2);
  if (fill) {
    if (min < 0) {
      if (val >= 0) {
        fill.style.left  = '50%';
        fill.style.width = (pct - 50).toFixed(4) + '%';
      } else {
        fill.style.left  = pct.toFixed(4) + '%';
        fill.style.width = (50 - pct).toFixed(4) + '%';
      }
    } else {
      fill.style.left  = '0';
      fill.style.width = pct.toFixed(4) + '%';
    }
  }
}

function _pbarNumChange(barId, numId, min, max) {
  const num = document.getElementById(numId);
  if (!num) return;
  _pbarSetValue(barId, numId, min, max, parseFloat(num.value) || 0);
}

function _pbarInit(containerEl) {
  const wraps = containerEl ? containerEl.querySelectorAll('.pbar-wrap') : [];
  wraps.forEach(wrap => {
    const bar   = wrap.querySelector('.pbar');
    const thumb = wrap.querySelector('.pbar-thumb');
    if (!bar || !thumb) return;
    const barId  = bar.id;
    const numId  = barId.replace(/-bar$/, '-num');
    const numEl  = document.getElementById(numId);
    const min    = numEl ? parseFloat(numEl.min) : 0;
    const max    = numEl ? parseFloat(numEl.max) : 1;

    function _valFromClientX(cx) {
      const rect = bar.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (cx - rect.left) / rect.width));
      return min + ratio * (max - min);
    }

    function _onMove(e) {
      const cx = e.touches ? e.touches[0].clientX : e.clientX;
      _pbarSetValue(barId, numId, min, max, _valFromClientX(cx));
    }

    function _onUp() {
      document.removeEventListener('mousemove', _onMove);
      document.removeEventListener('mouseup', _onUp);
      document.removeEventListener('touchmove', _onMove);
      document.removeEventListener('touchend', _onUp);
    }

    bar.addEventListener('mousedown', function(e) {
      e.preventDefault();
      _pbarSetValue(barId, numId, min, max, _valFromClientX(e.clientX));
      document.addEventListener('mousemove', _onMove);
      document.addEventListener('mouseup', _onUp);
    });
    bar.addEventListener('touchstart', function(e) {
      _pbarSetValue(barId, numId, min, max, _valFromClientX(e.touches[0].clientX));
      document.addEventListener('touchmove', _onMove, {passive: true});
      document.addEventListener('touchend', _onUp);
    }, {passive: true});
  });
}

/* â”€â”€ Personality custom renderer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

function cfgRenderPersonality(data) {
  // Phase 6.5.1 (2026-04-22): zone headings converted to page-tabs.
  // Identity / Behavior / Content / Voice — each a tab panel;
  // floppy-disk Save at top-right commits the personality YAML.
  let html = '';

  const TABS = [
    { id: 'identity', label: 'Identity' },
    { id: 'behavior', label: 'Behavior' },
    { id: 'content',  label: 'Content libraries' },
    { id: 'voice',    label: 'Voice production' },
  ];
  const activeTabId = _loadPageTab('personality', 'identity');

  // Page header.
  html += '<div class="page-header">'
    + '<div>'
    +   '<h2 class="page-title">Personality</h2>'
    +   '<div class="page-title-desc">Who GLaDOS is, how she reacts, what she says, and how she sounds.</div>'
    + '</div>'
    + '<button class="page-save-btn" onclick="_cfgSavePersonalityTab()" title="Save the active tab">'
    +   _floppySvg() + '<span>Save</span>'
    + '</button>'
    + '</div>';

  // Tab bar.
  html += '<nav class="page-tabs" role="tablist">';
  for (const t of TABS) {
    const cls = t.id === activeTabId ? 'page-tab active' : 'page-tab';
    html += '<button class="' + cls + '" role="tab" data-page-tab-group="personality" data-tab="' + t.id + '" onclick="showPageTab(\'personality\',\'' + t.id + '\')">'
      + escHtml(t.label)
      + '</button>';
  }
  html += '</nav>';

  html += '<div class="page-tab-panels">';

  // ════════════════════════════════════════════════════════════════
  // Identity tab — who GLaDOS is.
  // ════════════════════════════════════════════════════════════════
  html += '<div class="page-tab-panel' + (activeTabId === 'identity' ? ' active' : '') + '" data-page-tab-panel-group="personality" data-tab="identity">';

  // Preprompt entries (system/user/assistant seed messages)
  // Bug fix (Phase 2 Chunk 3): was reading data.preprompt (empty list in
  // personality.yaml); actual persona lives at data.personality_preprompt.
  const _prepromptList = Array.isArray(data.personality_preprompt) ? data.personality_preprompt : [];
  if (_prepromptList.length > 0) {
    html += '<div class="cfg-group"><div class="cfg-group-title">Preprompt Messages</div>';
    for (let i = 0; i < _prepromptList.length; i++) {
      const entry = _prepromptList[i];
      for (const role of ['system', 'user', 'assistant']) {
        if (entry[role] != null) {
          html += '<div class="preprompt-pair">'
            + '<div class="preprompt-role">' + role + '</div>'
            + '<textarea class="preprompt-text" data-preprompt="' + i + '-' + role + '">' + escHtml(entry[role]) + '</textarea>'
            + '</div>';
        }
      }
    }
    html += '</div>';
  }

  // HEXACO personality traits (advanced)
  if (data.hexaco) {
    const HEXACO_META = {
      honesty_humility: {
        label: 'Honesty – Humility',
        desc: 'Sincerity and fairness versus willingness to manipulate and exploit others. '
            + 'Lower is more self-serving and manipulative; higher is more transparent and fair-minded.',
        pole_low: 'Manipulative', pole_high: 'Sincere',
      },
      emotionality: {
        label: 'Emotionality',
        desc: 'Fearfulness, anxiety, and sentimentality versus emotional detachment. '
            + 'Lower is more stoic and unflappable; higher is more anxious and sentimental.',
        pole_low: 'Unflappable', pole_high: 'Anxious',
      },
      extraversion: {
        label: 'Extraversion',
        desc: 'Outgoing sociability and liveliness versus reserved introversion. '
            + 'Lower is more reserved and withholding; higher is more outgoing and expressive.',
        pole_low: 'Reserved', pole_high: 'Outgoing',
      },
      agreeableness: {
        label: 'Agreeableness',
        desc: 'Patience, forgiveness, and cooperation versus combative irritability. '
            + 'Lower is more suspicious and caustic; higher is more trusting and forgiving.',
        pole_low: 'Combative', pole_high: 'Cooperative',
      },
      conscientiousness: {
        label: 'Conscientiousness',
        desc: 'Organization, discipline, and diligence versus carelessness. '
            + 'Lower is more impulsive and sloppy; higher is more organized and deliberate.',
        pole_low: 'Impulsive', pole_high: 'Disciplined',
      },
      openness: {
        label: 'Openness to experience',
        desc: 'Curiosity, imagination, and unconventionality versus preference for the familiar. '
            + 'Lower is more conventional and routine-bound; higher is more curious and experimental.',
        pole_low: 'Conventional', pole_high: 'Curious',
      },
    };
    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">HEXACO Personality Traits</div>';
    html += '<div class="cfg-field-desc" style="margin-bottom:12px;">Six-factor personality model. Each trait runs 0.00 (minimum) to 1.00 (maximum), with 0.50 as balanced.</div>';
    for (const [k, v] of Object.entries(data.hexaco)) {
      const numId = 'cfg-personality-hexaco-' + k + '-num';
      const barId = 'cfg-personality-hexaco-' + k + '-bar';
      const meta = HEXACO_META[k] || { label: k.replace(/_/g, ' '), desc: '', pole_low: '0', pole_high: '1' };
      const val = (v == null ? 0 : v);
      const pct = (val * 100).toFixed(4);
      const display = val.toFixed(2);
      html += '<div class="ptrait">'
        + '<div class="ptrait-name">' + escHtml(meta.label) + '</div>'
        + (meta.desc ? '<div class="ptrait-desc">' + escHtml(meta.desc) + '</div>' : '')
        + '<div class="ptrait-slider-row">'
        +   '<div class="pbar-wrap" id="' + barId + '-wrap">'
        +     '<div class="pbar" id="' + barId + '">'
        +       '<div class="pbar-fill" id="' + barId + '-fill" style="left:0;width:' + pct + '%;"></div>'
        +       '<div class="pbar-thumb" id="' + barId + '-thumb" style="left:' + pct + '%;"></div>'
        +     '</div>'
        +     '<div class="pbar-poles">'
        +       '<span>' + escHtml(meta.pole_low || '0') + '</span>'
        +       '<span>' + escHtml(meta.pole_high || '1') + '</span>'
        +     '</div>'
        +   '</div>'
        +   '<input class="pnum" id="' + numId + '" type="number" min="0" max="1" step="0.01" value="' + display + '" '
        +     'data-path="hexaco.' + k + '" data-type="number" '
        +     'onchange="_pbarNumChange(\'' + barId + '\',\'' + numId + '\',0,1)">'
        + '</div>'
        + '</div>';
    }
    html += '</div>';
  }

  // Emotion model (advanced)
  if (data.emotion) {
    const EMOTION_META = {
      enabled:             { label: 'Emotion engine enabled',    desc: 'Master switch. When off, GLaDOS always responds from her baseline mood without updating based on events.' },
      tick_interval_s:     { label: 'Tick interval (seconds)',   desc: 'How often the emotion engine re-evaluates mood between events. Shorter is more reactive, longer is calmer.' },
      max_events:          { label: 'Event memory',              desc: 'Number of recent interactions that influence current mood before they fade out.' },
      baseline_pleasure:   { label: 'Baseline pleasure',         desc: 'Default pleasantness when nothing has happened. −1 is miserable, 0 is neutral, +1 is delighted.',   pole_low: 'Displeased −1', pole_high: '+1 Pleased' },
      baseline_arousal:    { label: 'Baseline arousal',          desc: 'Default alertness. −1 is sedate, 0 is calm, +1 is frantic.',                                        pole_low: 'Sedate −1',    pole_high: '+1 Frantic' },
      baseline_dominance:  { label: 'Baseline dominance',        desc: 'Default assertiveness. −1 is submissive, 0 is neutral, +1 is commanding.',                          pole_low: 'Submissive −1', pole_high: '+1 Commanding' },
      mood_drift_rate:     { label: 'Mood drift rate',           desc: 'Per-tick pull from current mood back toward baseline. Higher is faster forgiveness; lower means events stick.' },
      baseline_drift_rate: { label: 'Baseline drift rate',       desc: 'Per-tick shift of baseline itself from repeated exposure. Lower is more stable personality; higher means sustained interactions reshape her.' },
    };
    const pad_fields = ['baseline_pleasure', 'baseline_arousal', 'baseline_dominance'];
    const dyn_fields = ['enabled', 'tick_interval_s', 'max_events', 'mood_drift_rate', 'baseline_drift_rate'];

    function _renderEmotionField(k, v) {
      const fieldId = 'cfg-personality-emotion-' + k;
      const numId = fieldId + '-num';
      const barId = fieldId + '-bar';
      const meta = EMOTION_META[k] || { label: k.replace(/_/g, ' '), desc: '' };
      if (typeof v === 'boolean') {
        return ''
          + '<div class="cfg-field">'
          +   '<label class="cfg-field-label" for="' + fieldId + '">' + escHtml(meta.label) + '</label>'
          +   '<select id="' + fieldId + '" data-path="emotion.' + k + '" data-type="bool">'
          +     '<option value="true"' + (v ? ' selected' : '') + '>true</option>'
          +     '<option value="false"' + (!v ? ' selected' : '') + '>false</option>'
          +   '</select>'
          +   (meta.desc ? '<div class="trait-desc">' + escHtml(meta.desc) + '</div>' : '')
          + '</div>';
      }
      if (pad_fields.indexOf(k) >= 0) {
        // Range −1 to 1. Fill anchors at 50% (zero) and grows in either direction.
        const val = (v == null ? 0 : v);
        const pct = ((val + 1) / 2 * 100).toFixed(4);
        const fillLeft = val >= 0 ? '50%' : pct + '%';
        const fillWidth = (Math.abs(val) / 2 * 100).toFixed(4) + '%';
        const signStr = val > 0 ? '+' + val.toFixed(2) : val.toFixed(2);
        return ''
          + '<div class="ptrait">'
          +   '<div class="ptrait-name">' + escHtml(meta.label) + '</div>'
          +   (meta.desc ? '<div class="ptrait-desc">' + escHtml(meta.desc) + '</div>' : '')
          +   '<div class="ptrait-slider-row">'
          +     '<div class="pbar-wrap" id="' + barId + '-wrap">'
          +       '<div class="pbar" id="' + barId + '">'
          +         '<div class="pbar-fill" id="' + barId + '-fill" style="left:' + fillLeft + ';width:' + fillWidth + ';"></div>'
          +         '<div class="pbar-thumb" id="' + barId + '-thumb" style="left:' + pct + '%;"></div>'
          +         '<div class="pbar-zero" style="left:50%;"></div>'
          +       '</div>'
          +       '<div class="pbar-poles">'
          +         '<span>' + escHtml(meta.pole_low || '−1') + '</span>'
          +         '<span>' + escHtml(meta.pole_high || '+1') + '</span>'
          +       '</div>'
          +     '</div>'
          +     '<input class="pnum" id="' + numId + '" type="number" min="-1" max="1" step="0.01" value="' + signStr + '" '
          +       'data-path="emotion.' + k + '" data-type="number" '
          +       'onchange="_pbarNumChange(\'' + barId + '\',\'' + numId + '\',-1,1)">'
          +   '</div>'
          + '</div>';
      }
      return ''
        + '<div class="cfg-field">'
        +   '<label class="cfg-field-label" for="' + fieldId + '">' + escHtml(meta.label) + '</label>'
        +   '<input id="' + fieldId + '" data-path="emotion.' + k + '" data-type="number" type="number" step="any" value="' + v + '">'
        +   (meta.desc ? '<div class="trait-desc">' + escHtml(meta.desc) + '</div>' : '')
        + '</div>';
    }

    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">Emotion Model</div>';
    html += '<div class="cfg-field-desc" style="margin-bottom:6px;">PAD (Pleasure / Arousal / Dominance) mood model with tuning knobs for how fast mood drifts back to baseline and how many recent events matter.</div>';
    html += '<div class="subgroup-label">Baseline mood</div>';
    for (const k of pad_fields) {
      if (data.emotion[k] !== undefined) html += _renderEmotionField(k, data.emotion[k]);
    }
    html += '<div class="subgroup-label">Dynamics</div>';
    for (const k of dyn_fields) {
      if (data.emotion[k] !== undefined) html += _renderEmotionField(k, data.emotion[k]);
    }
    const known = new Set([...pad_fields, ...dyn_fields]);
    const extras = Object.keys(data.emotion).filter(k => !known.has(k));
    if (extras.length) {
      html += '<div class="subgroup-label">Other</div>';
      for (const k of extras) html += _renderEmotionField(k, data.emotion[k]);
    }
    html += '</div>';
  }

  // Attitudes table (read-only)
  if (data.attitudes && data.attitudes.length > 0) {
    html += '<div class="cfg-group"><div class="cfg-group-title">Attitudes (' + data.attitudes.length + ')</div>';
    html += '<table class="att-table"><tr><th>Tag</th><th>Label</th><th>Weight</th><th>TTS Params</th></tr>';
    for (const a of data.attitudes) {
      const tts = a.tts || {};
      const ttsStr = 'L:' + (tts.length_scale ?? '-') + ' N:' + (tts.noise_scale ?? '-') + ' W:' + (tts.noise_w ?? '-');
      html += '<tr>'
        + '<td class="tag-cell">' + escHtml(a.tag || '') + '</td>'
        + '<td>' + escHtml(a.label || '') + '</td>'
        + '<td>' + (a.weight ?? 1.0) + '</td>'
        + '<td class="tts-cell">' + escHtml(ttsStr) + '</td>'
        + '</tr>';
    }
    html += '</table>';
    html += '<div style="font-size:0.73rem;color:var(--fg-tertiary);margin-top:6px;">Edit attitudes via Raw YAML tab</div>';
    html += '</div>';
  }

  html += '</div>';  // end identity panel

  // ════════════════════════════════════════════════════════════════
  // Behavior tab — how GLaDOS reacts.
  // ════════════════════════════════════════════════════════════════
  html += '<div class="page-tab-panel' + (activeTabId === 'behavior' ? ' active' : '') + '" data-page-tab-panel-group="personality" data-tab="behavior">';

  html += ''
    + '<div class="card" id="cfg-verbosity-card">'
    +   '<div class="cfg-subsection-title">Announcement verbosity</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Probability that GLaDOS adds a sarcastic follow-up comment to an announcement. '
    +     '<em>0%</em> is factual only, <em>100%</em> always adds commentary. '
    +     'Set per scenario (doorbells, alarms, arrivals, etc.).'
    +   '</div>'
    +   '<div id="verbositySliders" style="opacity:0.5;">Loading announcement settings&hellip;</div>'
    + '</div>';
  setTimeout(loadVerbositySliders, 0);

  html += ''
    + '<div class="card" id="cfg-cmdrec-card">'
    +   '<div class="cfg-subsection-title">Command recognition</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Tunes the Tier&nbsp;1/2 precheck gate. When an utterance matches any of these signals, '
    +     'GLaDOS attempts a home-control intent before falling through to chitchat. Shipped defaults '
    +     '(command verbs like <code>darken</code>, <code>dim</code>, <code>bump</code> and ambient '
    +     'phrases like <em>&ldquo;it&rsquo;s too dark&rdquo;</em>) are always active; the fields below add extras.'
    +   '</div>'
    +   '<div id="cfg-cmdrec-body">Loading command recognition rules&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadCommandRecognition, 0);

  html += ''
    + '<div class="card" id="cfg-disambiguation-card" data-advanced="true">'
    +   '<div class="cfg-subsection-title">Disambiguation rules <span class="cfg-placeholder-tag">advanced</span></div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     '<strong>In plain English:</strong> your home has dozens of devices with similar names '
    +     '("bedroom light," "office light," "guest bedroom light"). When you give a command '
    +     'like <em>"turn on the lights,"</em> GLaDOS has to pick which ones you meant. These '
    +     'rules tune how she chooses &mdash; what to treat as synonyms, which words to ignore, '
    +     'which tokens to expand. <strong>Most operators should leave this alone.</strong> '
    +     'Touch it only if GLaDOS is consistently picking the wrong devices.'
    +   '</div>'
    +   '<div id="cfg-disamb-body">Loading rules&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadDisambiguation, 0);

  html += ''
    + '<div class="card" id="cfg-candretrieval-card" data-advanced="true">'
    +   '<div class="cfg-subsection-title">Candidate retrieval <span class="cfg-placeholder-tag">advanced</span></div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     '<strong>In plain English:</strong> Home Assistant has thousands of devices; GLaDOS '
    +     'can&rsquo;t hand them all to the language model for every command. Before you even speak, '
    +     'a semantic search narrows them to a short list of likely candidates. This card tunes '
    +     'that search and lets you test it with a sample phrase. <strong>Most operators should '
    +     'leave this alone.</strong> Touch it only if GLaDOS is consistently failing to find the '
    +     'device you meant.'
    +   '</div>'
    +   '<div id="cfg-candretrieval-body">Loading retriever status&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadCandRetrieval, 0);

  html += '</div>';  // end behavior panel

  // ════════════════════════════════════════════════════════════════
  // Content libraries tab — what GLaDOS says.
  // ════════════════════════════════════════════════════════════════
  html += '<div class="page-tab-panel' + (activeTabId === 'content' ? ' active' : '') + '" data-page-tab-panel-group="personality" data-tab="content">';

  html += ''
    + '<div class="card" id="cfg-quip-card">'
    +   '<div class="cfg-subsection-title">Quip library</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'When <strong>Response behavior</strong> is set to <em>quip</em> (see Audio &amp; Speakers), GLaDOS '
    +     'replies using a line from the on-disk library under <code>configs/quips/</code>. Each file holds '
    +     'one quip per line; <code>#</code> lines are comments.'
    +   '</div>'
    +   '<div id="cfg-quip-body">Loading quip library&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadQuips, 0);

  html += ''
    + '<div class="card" id="cfg-canon-card">'
    +   '<div class="cfg-subsection-title">Canon library</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Curated Portal 1/2 facts under <code>configs/canon/</code>. GLaDOS retrieves relevant '
    +     'entries per-turn when a trigger keyword fires (potato, Wheatley, Caroline, Cave, Aperture, '
    +     'turret opera, combustible lemon, moon rock, etc.).'
    +   '</div>'
    +   '<div id="cfg-canon-body">Loading canon library&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadCanon, 0);

  html += '</div>';  // end content panel

  // ════════════════════════════════════════════════════════════════
  // Voice production tab — how GLaDOS sounds (acoustic tuning).
  // ════════════════════════════════════════════════════════════════
  html += '<div class="page-tab-panel' + (activeTabId === 'voice' ? ' active' : '') + '" data-page-tab-panel-group="personality" data-tab="voice">';
  if (data.default_tts) {
    html += '<div class="cfg-field-desc" style="margin-bottom:var(--sp-3);">'
      + 'Acoustic voice-engine parameters — length scale, noise scale, noise W. '
      + 'Marked advanced because most operators never touch them once the voice sounds right. '
      + 'Toggle the <em>Show Advanced Settings</em> checkbox on the left nav to reveal them.'
      + '</div>';
    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">Default TTS Parameters</div>';
    html += cfgBuildForm(data.default_tts, 'personality', 'default_tts');
    html += '</div>';
  }

  // Phase Emotion-I: per-band TTS overrides. When GLaDOS's pleasure
  // drops into annoyed / hostile / menacing territory, these Piper
  // params clobber the rolled attitude so her VOICE reflects the mood.
  // Each band ships at Piper defaults (1.0 / 0.667 / 0.8) which is a
  // silent no-op until the operator tunes.
  if (data.emotion_tts) {
    html += _cfgRenderEmotionTTS(data.emotion_tts);
  }

  html += '</div>';  // end voice panel

  html += '</div>';  // end page-tab-panels

  return html;
}

// Phase 6.5.1: page-save dispatch for Personality. All tabs share the
// same YAML backing (personality.yaml) so the save routes through the
// existing cfgSaveSection('personality') regardless of active tab.
function _cfgSavePersonalityTab() {
  return cfgSaveSection('personality');
}


// ════════════════════════════════════════════════════════════════
// Phase Emotion-I (2026-04-23) — Emotional TTS overrides card.
// ════════════════════════════════════════════════════════════════
// Three bands (annoyed / hostile / menacing) × three Piper params
// (length_scale / noise_scale / noise_w). A band left at defaults
// (1.0 / 0.667 / 0.8) is a silent no-op.
//
// Sliders feed cfgCollectForm via data-path / data-type. Saving the
// Personality page lands in personality.yaml under `emotion_tts.*`
// and api_wrapper does a cfg.reload()+engine rebuild so the change
// takes effect on the very next chat turn.

const _EMOTION_TTS_BANDS = [
  {
    key: 'annoyed',
    label: 'Annoyed',
    desc: 'Fires at pleasure ≤ −0.3 (mild irritation — three to five repeat requests).',
  },
  {
    key: 'hostile',
    label: 'Openly hostile',
    desc: 'Fires at pleasure ≤ −0.5 (cooldown engages — four to five repeats).',
  },
  {
    key: 'menacing',
    label: 'Dangerously quiet',
    desc: 'Fires at pleasure ≤ −0.7 (five or more repeats, full saturation).',
  },
];

const _EMOTION_TTS_PARAM_META = {
  length_scale: {
    label: 'Length scale (speed)',
    desc: 'Lower is faster, 1.0 is baseline, higher is slower. Try 0.88 for snappy and 1.05 for subtly drawn out.',
    min: 0.50, max: 1.50, step: 0.01, default: 1.0,
  },
  noise_scale: {
    label: 'Noise scale (pitch variation)',
    desc: 'Lower is flatter and more monotone, 0.667 is baseline, higher is more sing-song and expressive.',
    min: 0.10, max: 1.00, step: 0.01, default: 0.667,
  },
  noise_w: {
    label: 'Noise W (rhythm variation)',
    desc: 'Lower is more metronomic/clinical, 0.8 is baseline, higher is more naturally varied syllable timing.',
    min: 0.10, max: 1.00, step: 0.01, default: 0.8,
  },
};

function _cfgRenderEmotionTTS(data) {
  let html = '<div class="cfg-group"><div class="cfg-group-title">Emotional TTS overrides</div>';
  html += '<div class="cfg-field-desc" style="margin-bottom:12px;">'
    + 'Shape how GLaDOS sounds when her pleasure drops into each negative band. '
    + 'A band left at Piper defaults (1.00 / 0.667 / 0.80) is a silent no-op — '
    + 'only tuned bands override the rolled attitude. Values save into '
    + '<code>personality.yaml</code> under <code>emotion_tts</code>; save takes '
    + 'effect on the next chat turn (no container restart).'
    + '</div>';

  for (const band of _EMOTION_TTS_BANDS) {
    const bandData = data[band.key] || {};
    html += '<div class="cfg-subgroup" style="margin-top:var(--sp-3);padding:var(--sp-3);background:var(--bg-elev-1,#1f1f1f);border:1px solid var(--border,#333);border-radius:var(--rad-md,4px);">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--sp-2);">';
    html +=   '<div>'
           +    '<div class="cfg-group-title" style="font-size:1em;">' + escHtml(band.label) + '</div>'
           +    '<div class="cfg-field-desc" style="margin:4px 0 0 0;">' + escHtml(band.desc) + '</div>'
           +  '</div>';
    html +=   '<button type="button" class="cfg-save-btn" style="font-size:0.85em;padding:4px 10px;" '
           +    'onclick="_cfgResetEmotionTTSBand(\'' + band.key + '\')" '
           +    'title="Reset this band to Piper defaults (silent no-op)">Reset</button>';
    html += '</div>';

    for (const paramKey of ['length_scale', 'noise_scale', 'noise_w']) {
      const meta = _EMOTION_TTS_PARAM_META[paramKey];
      const value = (bandData[paramKey] == null) ? meta.default : Number(bandData[paramKey]);
      const display = value.toFixed(3);
      const fieldId = 'cfg-personality-emotion_tts-' + band.key + '-' + paramKey;
      const dataPath = 'emotion_tts.' + band.key + '.' + paramKey;
      html += '<div class="trait-row">'
           +   '<div class="trait-head">'
           +     '<label class="trait-label" for="' + fieldId + '">' + escHtml(meta.label) + '</label>'
           +     '<output class="trait-value" id="' + fieldId + '-value">' + display + '</output>'
           +   '</div>'
           +   '<input id="' + fieldId + '" data-path="' + dataPath + '" data-type="number" type="range" '
           +     'min="' + meta.min + '" max="' + meta.max + '" step="' + meta.step + '" value="' + value + '" '
           +     'oninput="document.getElementById(\'' + fieldId + '-value\').textContent = parseFloat(this.value).toFixed(3);">'
           +   '<div class="trait-desc">' + escHtml(meta.desc) + '</div>'
           + '</div>';
    }
    html += '</div>';
  }

  html += '</div>';  // end cfg-group
  return html;
}

function _cfgResetEmotionTTSBand(bandKey) {
  // Snap every slider in this band back to the Piper defaults. Operator
  // still has to click the page Save button — matches the behaviour of
  // the other edit-then-commit fields on the Personality page.
  for (const paramKey of ['length_scale', 'noise_scale', 'noise_w']) {
    const meta = _EMOTION_TTS_PARAM_META[paramKey];
    const fieldId = 'cfg-personality-emotion_tts-' + bandKey + '-' + paramKey;
    const input = document.getElementById(fieldId);
    const output = document.getElementById(fieldId + '-value');
    if (input) input.value = meta.default;
    if (output) output.textContent = Number(meta.default).toFixed(3);
  }
  showToast('Reset ' + bandKey + ' band to defaults. Click Save to apply.', 'info');
}



// Phase 8.7c — Quip library editor.

let _quipSelectedPath = null;

async function _cfgLoadQuips() {
  const body = document.getElementById('cfg-quip-body');
  if (!body) return;
  try {
    const r = await fetch('/api/quips');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _quipRenderTree(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _quipRenderTree(data) {
  const body = document.getElementById('cfg-quip-body');
  if (!body) return;
  const files = data.files || [];
  // Group by category/intent for the tree.
  const tree = {};
  files.forEach(f => {
    const parts = f.path.split('/');
    if (parts.length >= 3) {
      const cat = parts[0], intent = parts[1], leaf = parts.slice(2).join('/');
      tree[cat] = tree[cat] || {};
      tree[cat][intent] = tree[cat][intent] || [];
      tree[cat][intent].push({ leaf, path: f.path, count: f.quip_count });
    } else if (parts.length === 2) {
      // category/file.txt flat layout (outcome_modifier, global)
      const cat = parts[0];
      tree[cat] = tree[cat] || {};
      tree[cat]['_'] = tree[cat]['_'] || [];
      tree[cat]['_'].push({ leaf: parts[1], path: f.path, count: f.quip_count });
    }
  });
  let html = '<div style="display:flex;gap:14px;flex-wrap:wrap;">';
  // Left: tree
  html += '<div id="cfg-quip-tree" style="flex:1;min-width:260px;max-height:420px;overflow-y:auto;border:1px solid #333;border-radius:4px;padding:8px;background:#1a1a1a;">';
  Object.keys(tree).sort().forEach(cat => {
    html += '<div style="font-weight:bold;margin-top:6px;color:#ffa94d;">' + escHtml(cat) + '</div>';
    Object.keys(tree[cat]).sort().forEach(intent => {
      if (intent !== '_') {
        html += '<div style="margin-left:10px;margin-top:4px;color:#9cdcfe;font-size:0.9em;">' + escHtml(intent) + '</div>';
      }
      tree[cat][intent].forEach(f => {
        const indent = (intent === '_') ? 10 : 22;
        html += '<div style="margin-left:' + indent + 'px;cursor:pointer;padding:2px 4px;border-radius:2px;" '
          + 'onclick="_quipLoad(\'' + escAttr(f.path) + '\')" '
          + 'onmouseover="this.style.background=\'#2a2a2a\'" '
          + 'onmouseout="this.style.background=\'transparent\'">'
          + '&rarr; ' + escHtml(f.leaf) + ' <span style="color:#888;font-size:0.85em;">(' + f.count + ')</span>'
          + '</div>';
      });
    });
  });
  if (!Object.keys(tree).length) {
    html += '<div class="cfg-field-desc">Library is empty. Create a file with <em>New file</em> below.</div>';
  }
  html += '</div>';
  // Right: editor pane
  html += '<div style="flex:2;min-width:320px;display:flex;flex-direction:column;gap:8px;">';
  html += '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-quip-path" placeholder="command_ack/turn_on/normal.txt" style="flex:1;min-width:220px;">'
    + '<button type="button" class="cfg-save-btn" onclick="_quipLoadFromPath()">Open</button>'
    + '<button type="button" class="btn btn-danger" onclick="_quipDelete()">Delete</button>'
    + '</div>';
  html += '<textarea id="cfg-quip-editor" style="width:100%;min-height:280px;font-family:monospace;background:#1a1a1a;color:#ddd;border:1px solid #333;padding:8px;"></textarea>';
  html += '<div class="cfg-save-row"><button class="cfg-save-btn" onclick="_quipSave()">Save file</button>'
    + '<span id="cfg-quip-save-result" class="cfg-result"></span></div>';
  html += '</div>';  // editor pane
  html += '</div>';  // flex wrapper
  // Dry-run card
  html += '<div class="cfg-field-label" style="margin-top:14px;">Live test</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">Pick a category + intent + mood and see which line the composer would emit right now.</div>'
    + '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">'
    + '<select id="cfg-quip-test-cat">'
    +   '<option value="command_ack">command_ack</option>'
    +   '<option value="query_answer">query_answer</option>'
    +   '<option value="ambient_cue">ambient_cue</option>'
    +   '<option value="error">error</option>'
    + '</select>'
    + '<input type="text" id="cfg-quip-test-intent" placeholder="turn_on / turn_off / ..." value="turn_on" style="min-width:140px;">'
    + '<select id="cfg-quip-test-mood">'
    +   '<option value="normal">normal</option>'
    +   '<option value="cranky">cranky</option>'
    +   '<option value="amused">amused</option>'
    + '</select>'
    + '<button class="cfg-save-btn" onclick="_quipDryRun()">Pick a line</button>'
    + '</div>'
    + '<div id="cfg-quip-test-result" style="margin-top:8px;font-family:monospace;color:#9cdcfe;"></div>';
  body.innerHTML = html;
}

async function _quipLoad(path) {
  const ed = document.getElementById('cfg-quip-editor');
  const pathEl = document.getElementById('cfg-quip-path');
  try {
    const r = await fetch('/api/quips?path=' + encodeURIComponent(path));
    if (!r.ok) { showToast('Load failed (' + r.status + ')', 'warn'); return; }
    const data = await r.json();
    if (ed) ed.value = (data.lines || []).join('\n');
    if (pathEl) pathEl.value = data.path || path;
    _quipSelectedPath = data.path || path;
  } catch (e) {
    showToast('Load error: ' + e.message, 'warn');
  }
}

function _quipLoadFromPath() {
  const pathEl = document.getElementById('cfg-quip-path');
  if (pathEl && pathEl.value.trim()) _quipLoad(pathEl.value.trim());
}

async function _quipSave() {
  const pathEl = document.getElementById('cfg-quip-path');
  const ed = document.getElementById('cfg-quip-editor');
  const result = document.getElementById('cfg-quip-save-result');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) { showToast('Enter a path first', 'warn'); return; }
  const lines = (ed ? ed.value : '').split('\n');
  if (result) { result.textContent = 'Saving...'; result.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/quips', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path, lines }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (result) result.textContent = '';
      showToast('Saved: ' + path + ' (' + (resp.quip_count || 0) + ' quips)', 'success');
      _cfgLoadQuips();  // refresh tree counts
    } else if (result) {
      result.textContent = resp.error || ('Error (' + r.status + ')');
      result.className = 'cfg-result err';
    }
  } catch (e) {
    if (result) { result.textContent = 'Error: ' + e.message; result.className = 'cfg-result err'; }
  }
}

async function _quipDelete() {
  const pathEl = document.getElementById('cfg-quip-path');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) return;
  if (!confirm('Delete ' + path + '?')) return;
  try {
    const r = await fetch('/api/quips?path=' + encodeURIComponent(path), {method: 'DELETE'});
    if (r.ok) {
      showToast('Deleted: ' + path, 'success');
      const ed = document.getElementById('cfg-quip-editor');
      if (ed) ed.value = '';
      _cfgLoadQuips();
    } else {
      showToast('Delete failed (' + r.status + ')', 'warn');
    }
  } catch (e) {
    showToast('Delete error: ' + e.message, 'warn');
  }
}

async function _quipDryRun() {
  const catEl = document.getElementById('cfg-quip-test-cat');
  const intentEl = document.getElementById('cfg-quip-test-intent');
  const moodEl = document.getElementById('cfg-quip-test-mood');
  const resEl = document.getElementById('cfg-quip-test-result');
  const payload = {
    event_category: catEl ? catEl.value : 'command_ack',
    intent: intentEl ? intentEl.value : 'turn_on',
    mood: moodEl ? moodEl.value : 'normal',
  };
  try {
    const r = await fetch('/api/quips/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (resEl) {
      if (data.line) {
        resEl.textContent = '→ ' + data.line;
        resEl.style.color = '#9cdcfe';
      } else if (data.library_empty) {
        resEl.textContent = 'Library is empty — composer would fall back to LLM speech.';
        resEl.style.color = '#f66';
      } else {
        resEl.textContent = 'No line matched — composer would fall back to LLM speech for this request.';
        resEl.style.color = '#fa5';
      }
    }
  } catch (e) {
    if (resEl) { resEl.textContent = 'Error: ' + e.message; resEl.style.color = '#f66'; }
  }
}

// Phase 8.14 — Canon library editor. Tree of topic files on the left,
// textarea on the right for the whole-file content, dry-run panel at
// the bottom that shows gate firing + retrieved entries.

let _canonSelectedPath = null;

async function _cfgLoadCanon() {
  const body = document.getElementById('cfg-canon-body');
  if (!body) return;
  try {
    const r = await fetch('/api/canon');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _canonRenderTree(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _canonRenderTree(data) {
  const body = document.getElementById('cfg-canon-body');
  if (!body) return;
  const files = data.files || [];
  let html = '<div style="display:flex;gap:14px;flex-wrap:wrap;">';
  html += '<div id="cfg-canon-tree" style="flex:1;min-width:220px;max-height:420px;overflow-y:auto;border:1px solid #333;border-radius:4px;padding:8px;background:#1a1a1a;">';
  if (!files.length) {
    html += '<div class="cfg-field-desc">No canon files yet. Enter a <em>&lt;topic&gt;.txt</em> path and click Save to create one.</div>';
  } else {
    files.forEach(f => {
      html += '<div style="cursor:pointer;padding:3px 4px;border-radius:2px;" '
        + 'onclick="_canonLoad(\'' + escAttr(f.path) + '\')" '
        + 'onmouseover="this.style.background=\'#2a2a2a\'" '
        + 'onmouseout="this.style.background=\'transparent\'">'
        + '&rarr; ' + escHtml(f.path) + ' <span style="color:#888;font-size:0.85em;">(' + (f.entry_count || 0) + ' entries)</span>'
        + '</div>';
    });
  }
  html += '</div>';
  html += '<div style="flex:2;min-width:320px;display:flex;flex-direction:column;gap:8px;">';
  html += '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-canon-path" placeholder="<topic>.txt" style="flex:1;min-width:220px;">'
    + '<button type="button" class="cfg-save-btn" onclick="_canonLoadFromPath()">Open</button>'
    + '<button type="button" class="btn btn-danger" onclick="_canonDelete()">Delete</button>'
    + '</div>';
  html += '<textarea id="cfg-canon-editor" style="width:100%;min-height:300px;font-family:monospace;background:#1a1a1a;color:#ddd;border:1px solid #333;padding:8px;" placeholder="# Optional comment line.\n\nFirst canon entry. One to three sentences.\n\nSecond canon entry. Blank line separates."></textarea>';
  html += '<div class="cfg-save-row"><button class="cfg-save-btn" onclick="_canonSave()">Save file</button>'
    + '<span id="cfg-canon-save-result" class="cfg-result"></span></div>';
  html += '</div>';
  html += '</div>';
  html += '<div class="cfg-field-label" style="margin-top:14px;">Retrieval test</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">Enter an utterance; the panel shows whether the canon gate fires and which entries would be injected into the LLM context.</div>'
    + '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-canon-test-utt" placeholder="How did you cope with being a potato?" style="flex:1;min-width:280px;">'
    + '<button class="cfg-save-btn" onclick="_canonDryRun()">Retrieve</button>'
    + '</div>'
    + '<div id="cfg-canon-test-result" style="margin-top:8px;font-family:monospace;font-size:0.9em;color:#ddd;"></div>';
  body.innerHTML = html;
}

async function _canonLoad(path) {
  const ed = document.getElementById('cfg-canon-editor');
  const pathEl = document.getElementById('cfg-canon-path');
  try {
    const r = await fetch('/api/canon?path=' + encodeURIComponent(path));
    if (!r.ok) { showToast('Load failed (' + r.status + ')', 'warn'); return; }
    const data = await r.json();
    if (ed) ed.value = data.text || '';
    if (pathEl) pathEl.value = data.path || path;
    _canonSelectedPath = data.path || path;
  } catch (e) {
    showToast('Load error: ' + e.message, 'warn');
  }
}

function _canonLoadFromPath() {
  const pathEl = document.getElementById('cfg-canon-path');
  if (pathEl && pathEl.value.trim()) _canonLoad(pathEl.value.trim());
}

async function _canonSave() {
  const pathEl = document.getElementById('cfg-canon-path');
  const ed = document.getElementById('cfg-canon-editor');
  const result = document.getElementById('cfg-canon-save-result');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) { showToast('Enter a path first', 'warn'); return; }
  const text = ed ? ed.value : '';
  if (result) { result.textContent = 'Saving...'; result.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/canon', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path, text }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (result) result.textContent = '';
      const reloadedNote = resp.reloaded ? ' (live)' : ' (reload failed — restart to apply)';
      showToast('Saved: ' + path + ' (' + (resp.entry_count || 0) + ' entries)' + reloadedNote, 'success');
      _cfgLoadCanon();
    } else if (result) {
      result.textContent = resp.error || ('Error (' + r.status + ')');
      result.className = 'cfg-result err';
    }
  } catch (e) {
    if (result) { result.textContent = 'Error: ' + e.message; result.className = 'cfg-result err'; }
  }
}

async function _canonDelete() {
  const pathEl = document.getElementById('cfg-canon-path');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) return;
  if (!confirm('Delete ' + path + '?')) return;
  try {
    const r = await fetch('/api/canon?path=' + encodeURIComponent(path), {method: 'DELETE'});
    if (r.ok) {
      showToast('Deleted: ' + path, 'success');
      const ed = document.getElementById('cfg-canon-editor');
      if (ed) ed.value = '';
      _cfgLoadCanon();
    } else {
      showToast('Delete failed (' + r.status + ')', 'warn');
    }
  } catch (e) {
    showToast('Delete error: ' + e.message, 'warn');
  }
}

async function _canonDryRun() {
  const uttEl = document.getElementById('cfg-canon-test-utt');
  const resEl = document.getElementById('cfg-canon-test-result');
  const utterance = uttEl ? uttEl.value.trim() : '';
  if (!utterance) { showToast('Enter an utterance first', 'warn'); return; }
  try {
    const r = await fetch('/api/canon/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ utterance }),
    });
    const data = await r.json();
    if (!resEl) return;
    let html = '<div style="margin-bottom:6px;">Gate: '
      + (data.gate_fired ? '<span style="color:#6f6;">FIRED</span>' : '<span style="color:#f96;">skipped</span>')
      + '</div>';
    const entries = data.entries || [];
    if (!entries.length) {
      html += '<div style="color:#f96;">No entries retrieved.</div>';
    } else {
      html += '<div>' + entries.length + ' entr' + (entries.length === 1 ? 'y' : 'ies') + ':</div>';
      entries.forEach(e => {
        const dist = (e.distance != null) ? (' <span style="color:#888;">[' + e.distance.toFixed(3) + ']</span>') : '';
        const topic = e.topic ? ' <span style="color:#ffa94d;">[' + escHtml(e.topic) + ']</span>' : '';
        html += '<div style="margin:4px 0;padding:4px 6px;border-left:2px solid #555;">'
          + topic + dist + '<br>' + escHtml(e.document || '')
          + '</div>';
      });
    }
    resEl.innerHTML = html;
  } catch (e) {
    if (resEl) { resEl.innerHTML = '<span style="color:#f66;">Error: ' + escHtml(e.message) + '</span>'; }
  }
}

// Phase 8.2 — Command recognition card fetch/render/save.

async function _cfgLoadCommandRecognition() {
  const body = document.getElementById('cfg-cmdrec-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load rules (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _cmdrecPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading rules: ' + escHtml(e.message) + '</div>';
  }
}

function _cmdrecPopulate(data) {
  const body = document.getElementById('cfg-cmdrec-body');
  if (!body) return;
  const verbs = Array.isArray(data.extra_command_verbs) ? data.extra_command_verbs : [];
  const patterns = Array.isArray(data.extra_ambient_patterns) ? data.extra_ambient_patterns : [];
  let html = '';
  html += '<div class="cfg-field-label" style="margin-top:4px;">Extra command verbs</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Single words (not phrases). Merged with the shipped defaults (darken, brighten, dim, lighten, '
    +   'bump, lower, raise, reduce, increase, soften, tone, crank, kill, douse, extinguish, illuminate, '
    +   'light, set, put, dial, slide, push, pull, close, open, shut, drop).'
    + '</div>'
    + '<div id="cfg-cmdrec-verbs" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" onclick="_cmdrecAddVerb()">+ Add verb</button>';
  html += '<div class="cfg-field-label" style="margin-top:14px;">Extra ambient-state patterns (regex)</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Case-insensitive Python regex. Invalid patterns are rejected on save.'
    + '</div>'
    + '<div id="cfg-cmdrec-patterns" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" onclick="_cmdrecAddPattern()">+ Add pattern</button>';
  html += '<div class="cfg-field-label" style="margin-top:18px;">Test input</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Type a phrase and see whether the current precheck (defaults + any edits above once saved) would recognise it.'
    + '</div>'
    + '<div style="display:flex;gap:6px;align-items:center;">'
    +   '<input type="text" id="cfg-cmdrec-test-input" placeholder="e.g. bump the living room up a bit" style="flex:1;">'
    +   '<button type="button" class="cfg-save-btn" onclick="_cmdrecTest()">Test</button>'
    + '</div>'
    + '<div id="cfg-cmdrec-test-result" class="cfg-field-desc" style="margin-top:8px;"></div>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveCommandRecognition()">Save Command recognition</button>'
    + '<span id="cfg-save-result-cmdrec" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
  const verbsHost = document.getElementById('cfg-cmdrec-verbs');
  verbs.forEach(v => _cmdrecRenderVerbRow(verbsHost, v));
  const patsHost = document.getElementById('cfg-cmdrec-patterns');
  patterns.forEach(p => _cmdrecRenderPatternRow(patsHost, p));
}

function _cmdrecRenderVerbRow(host, v) {
  const row = document.createElement('div');
  row.className = 'cfg-cmdrec-verb-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-cmdrec-verb" value="' + escAttr(v) + '" placeholder="e.g. nudge" style="flex:1;">'
    + '<button type="button" class="btn btn-danger" title="Remove verb">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _cmdrecRenderPatternRow(host, p) {
  const row = document.createElement('div');
  row.className = 'cfg-cmdrec-pattern-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-cmdrec-pattern" value="' + escAttr(p) + '" placeholder="e.g. \\\\bthe cats? (?:need|want)\\\\b" style="flex:1;font-family:monospace;">'
    + '<button type="button" class="btn btn-danger" title="Remove pattern">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _cmdrecAddVerb() {
  const host = document.getElementById('cfg-cmdrec-verbs');
  if (host) _cmdrecRenderVerbRow(host, '');
}

function _cmdrecAddPattern() {
  const host = document.getElementById('cfg-cmdrec-patterns');
  if (host) _cmdrecRenderPatternRow(host, '');
}

async function _cmdrecTest() {
  const input = document.getElementById('cfg-cmdrec-test-input');
  const resultEl = document.getElementById('cfg-cmdrec-test-result');
  if (!input || !resultEl) return;
  const utt = (input.value || '').trim();
  if (!utt) {
    resultEl.innerHTML = '<span style="color:#999;">Enter an utterance above.</span>';
    return;
  }
  resultEl.textContent = 'Testing...';
  try {
    const r = await fetch('/api/precheck/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({utterance: utt}),
    });
    const resp = await r.json();
    if (!r.ok) {
      resultEl.innerHTML = '<span style="color:#d66;">' + escHtml(resp.error || ('HTTP ' + r.status)) + '</span>';
      return;
    }
    const matches = !!resp.matches;
    const reasons = Array.isArray(resp.via) ? resp.via : [];
    const domains = Array.isArray(resp.domains) ? resp.domains : null;
    let out = '<div style="color:' + (matches ? '#6c6' : '#d66') + ';font-weight:bold;">'
      + (matches ? 'Recognised as a home command.' : 'Not recognised &mdash; falls to chitchat.')
      + '</div>';
    if (matches) {
      out += '<div>Matched via: <code>' + reasons.map(escHtml).join('</code>, <code>') + '</code></div>';
    }
    if (domains && domains.length) {
      out += '<div>Domain hints: <code>' + domains.map(escHtml).join('</code>, <code>') + '</code></div>';
    }
    resultEl.innerHTML = out;
  } catch (e) {
    resultEl.innerHTML = '<span style="color:#d66;">Error: ' + escHtml(e.message) + '</span>';
  }
}

async function cfgSaveCommandRecognition() {
  const resultEl = document.getElementById('cfg-save-result-cmdrec');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const verbs = [];
  document.querySelectorAll('#cfg-cmdrec-verbs .cfg-cmdrec-verb-row .cfg-cmdrec-verb').forEach(el => {
    const v = (el.value || '').trim();
    if (v) verbs.push(v);
  });
  const patterns = [];
  document.querySelectorAll('#cfg-cmdrec-patterns .cfg-cmdrec-pattern-row .cfg-cmdrec-pattern').forEach(el => {
    const p = (el.value || '').trim();
    if (p) patterns.push(p);
  });
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        extra_command_verbs: verbs,
        extra_ambient_patterns: patterns,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

/* â”€â”€ Config form data collection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

function cfgCollectForm(section) {
  if (section === 'personality') return cfgCollectPersonality();
  if (section === 'services') return cfgCollectServices();

  const result = {};
  document.querySelectorAll('[id^="cfg-' + section + '-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else if (type === 'array') val = el.value.split(',').map(s => s.trim()).filter(Boolean);
    else {
      val = el.value;
      // Re-apply path mask
      if (el.dataset.pathMask && val && !val.startsWith(el.dataset.pathMask)) {
        val = el.dataset.pathMask + val;
      }
    }
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = val;
  });
  return result;
}

function cfgCollectServices() {
  const result = {};
  document.querySelectorAll('[id^="cfg-services-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = el.value;
  });
  return result;
}

function cfgCollectPersonality() {
  const result = {};
  // Collect standard fields
  document.querySelectorAll('[id^="cfg-personality-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else val = el.value;
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = val;
  });
  // Collect preprompt textareas — write back to personality_preprompt (not preprompt).
  // Bug fix Phase 2 Chunk 3: the actual persona lives at personality_preprompt.
  const prepromptEls = document.querySelectorAll('[data-preprompt]');
  if (prepromptEls.length > 0) {
    const entries = {};
    prepromptEls.forEach(el => {
      const [idx, role] = el.dataset.preprompt.split('-');
      if (!entries[idx]) entries[idx] = {};
      entries[idx][role] = el.value;
    });
    result.personality_preprompt = Object.values(entries);
  }
  // Preserve attitudes as-is (read-only in form view)
  if (_cfgData.personality && _cfgData.personality.attitudes) {
    result.attitudes = _cfgData.personality.attitudes;
  }
  return result;
}

async function cfgSaveSsl() {
  const result = document.getElementById('cfg-save-result');
  const data = _cfgData.global || {};
  if (!data.ssl) data.ssl = {};
  const getVal = (id, def) => {
    const el = document.getElementById(id);
    if (!el) return def;
    if (el.type === 'checkbox') return el.checked;
    return el.value;
  };
  data.ssl.enabled = getVal('ssl-enabled', false);
  data.ssl.domain = getVal('ssl-domain', '');
  data.ssl.acme_email = getVal('ssl-acme-email', '');
  data.ssl.acme_provider = getVal('ssl-acme-provider', 'cloudflare');
  data.ssl.acme_api_token = getVal('ssl-acme-token', '');
  data.ssl.use_letsencrypt = getVal('ssl-use-le', false);
  data.ssl.cert_path = getVal('ssl-cert-path', '/app/certs/cert.pem');
  data.ssl.key_path = getVal('ssl-key-path', '/app/certs/key.pem');
  result.textContent = 'Saving...';
  fetch('/api/config/global', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  }).then(r => r.json()).then(d => {
    if (d.ok) { result.textContent = 'Saved. Restart container for changes to take effect.'; result.style.color = '#6f6'; }
    else { result.textContent = 'Error: ' + (d.error || 'unknown'); result.style.color = '#f66'; }
  }).catch(e => { result.textContent = 'Error: ' + e; result.style.color = '#f66'; });
}

async function sslRefreshStatus() {
  try {
    const r = await fetch('/api/ssl/status');
    const d = await r.json();
    const statusEl = document.getElementById('ssl-status-display');
    if (!statusEl) return;
    const active = d.ssl_active ? '<span style="color:#6f6">Active (HTTPS)</span>' : '<span style="color:#f66">Inactive (HTTP)</span>';
    let html = '<div class="cfg-ssl-status">';
    html += '<div><strong>Status:</strong> ' + active + '</div>';
    if (d.cert_exists) {
      html += '<div><strong>Source:</strong> ' + escHtml(d.source) + '</div>';
      html += '<div><strong>Subject:</strong> ' + escHtml(d.subject || '-') + '</div>';
      html += '<div><strong>Issuer:</strong> ' + escHtml(d.issuer || '-') + '</div>';
      if (d.sans && d.sans.length) {
        html += '<div><strong>SANs:</strong> ' + d.sans.map(escHtml).join(', ') + '</div>';
      }
      html += '<div><strong>Issued:</strong> ' + escHtml((d.not_before || '').slice(0,10)) + '</div>';
      html += '<div><strong>Expires:</strong> ' + escHtml((d.not_after || '').slice(0,10)) + ' (' + d.days_remaining + ' days)</div>';
    } else {
      html += '<div>No certificate installed</div>';
    }
    html += '</div>';
    statusEl.innerHTML = html;
  } catch(e) {
    console.error('SSL status fetch failed:', e);
  }
}

async function sslRequestLetsEncrypt() {
  const resultEl = document.getElementById('ssl-request-result');
  resultEl.textContent = 'Requesting certificate from Lets Encrypt (30-60s)...';
  resultEl.style.color = '#ccc';
  try {
    const r = await fetch('/api/ssl/request', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      resultEl.innerHTML = '<div style="color:#6f6">' + escHtml(d.message) + '</div>' + (d.log ? '<pre style="font-size:11px;max-height:200px;overflow:auto;margin-top:8px;">' + escHtml(d.log) + '</pre>' : '');
      sslRefreshStatus();
    } else {
      resultEl.innerHTML = '<div style="color:#f66">Error: ' + escHtml(d.error || 'unknown') + '</div>' + (d.log ? '<pre style="font-size:11px;max-height:200px;overflow:auto;margin-top:8px;">' + escHtml(d.log) + '</pre>' : '');
    }
  } catch(e) {
    resultEl.innerHTML = '<div style="color:#f66">Request failed: ' + escHtml(String(e)) + '</div>';
  }
}

async function sslUploadFiles() {
  const resultEl = document.getElementById('ssl-upload-result');
  const certInput = document.getElementById('ssl-upload-cert');
  const keyInput = document.getElementById('ssl-upload-key');
  if (!certInput.files[0] || !keyInput.files[0]) {
    resultEl.innerHTML = '<div style="color:#f66">Select both cert and key files</div>';
    return;
  }
  try {
    const certText = await certInput.files[0].text();
    const keyText = await keyInput.files[0].text();
    resultEl.textContent = 'Uploading...';
    const r = await fetch('/api/ssl/upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cert: certText, key: keyText}),
    });
    const d = await r.json();
    if (d.ok || r.ok) {
      resultEl.innerHTML = '<div style="color:#6f6">' + escHtml(d.message || 'Uploaded') + '</div>';
      sslRefreshStatus();
    } else {
      resultEl.innerHTML = '<div style="color:#f66">Error: ' + escHtml(d.error || 'upload failed') + '</div>';
    }
  } catch(e) {
    resultEl.innerHTML = '<div style="color:#f66">Upload failed: ' + escHtml(String(e)) + '</div>';
  }
}

function cfgRenderSsl(ssl) {
  ssl = ssl || {};
  let html = '<div style="max-width:700px;">';
  html += '<div id="ssl-status-display" class="cfg-ssl-status-box" style="background:#1a1a1a;padding:12px;border-radius:4px;margin-bottom:16px;">Loading status...</div>';
  html += '<div style="margin-bottom:12px;"><button onclick="sslRefreshStatus()" class="cfg-btn">Refresh Status</button></div>';

  html += '<h3 style="margin-top:20px;">Lets Encrypt (Cloudflare DNS)</h3>';
  html += '<div class="cfg-row"><label>Enable HTTPS:</label><input type="checkbox" id="ssl-enabled"' + (ssl.enabled ? ' checked' : '') + '></div>';
  html += '<div class="cfg-row"><label>Use Lets Encrypt:</label><input type="checkbox" id="ssl-use-le"' + (ssl.use_letsencrypt ? ' checked' : '') + '></div>';
  html += '<div class="cfg-row"><label>Domain:</label><input type="text" id="ssl-domain" value="' + escAttr(ssl.domain || '') + '" placeholder="glados.example.com"></div>';
  html += '<div class="cfg-row"><label>ACME Email:</label><input type="text" id="ssl-acme-email" value="' + escAttr(ssl.acme_email || '') + '" placeholder="admin@example.com"></div>';
  html += '<div class="cfg-row"><label>DNS Provider:</label><select id="ssl-acme-provider"><option value="cloudflare"' + ((ssl.acme_provider === 'cloudflare' || !ssl.acme_provider) ? ' selected' : '') + '>Cloudflare</option></select></div>';
  html += '<div class="cfg-row"><label>API Token:</label><input type="password" id="ssl-acme-token" value="' + escAttr(ssl.acme_api_token || '') + '" placeholder="Cloudflare API token"></div>';
  html += '<div style="margin:12px 0;"><button onclick="sslRequestLetsEncrypt()" class="cfg-btn">Request / Renew Certificate</button></div>';
  html += '<div id="ssl-request-result" style="margin-bottom:20px;"></div>';

  html += '<h3 style="margin-top:20px;">Manual Upload</h3>';
  html += '<div class="cfg-row"><label>Certificate PEM:</label><input type="file" id="ssl-upload-cert" accept=".pem,.crt,.cert"></div>';
  html += '<div class="cfg-row"><label>Private Key PEM:</label><input type="file" id="ssl-upload-key" accept=".pem,.key"></div>';
  html += '<div style="margin:12px 0;"><button onclick="sslUploadFiles()" class="cfg-btn">Upload Certificate</button></div>';
  html += '<div id="ssl-upload-result" style="margin-bottom:20px;"></div>';

  html += '<h3 style="margin-top:20px;">File Paths (advanced)</h3>';
  html += '<div class="cfg-row"><label>Certificate Path:</label><input type="text" id="ssl-cert-path" value="' + escAttr(ssl.cert_path || '/app/certs/cert.pem') + '"></div>';
  html += '<div class="cfg-row"><label>Key Path:</label><input type="text" id="ssl-key-path" value="' + escAttr(ssl.key_path || '/app/certs/key.pem') + '"></div>';

  html += '<div style="margin-top:24px;padding:12px;background:#2a2010;border-left:3px solid #fa0;">';
  html += 'Container restart is required after certificate changes take effect.';
  html += '</div>';

  html += '<div class="cfg-save-row" style="margin-top:20px;">';
  html += '<button class="cfg-save-btn" onclick="cfgSaveSsl()">Save SSL Settings</button>';
  html += '<span id="cfg-save-result" class="cfg-result"></span>';
  html += '</div>';

  html += '</div>';
  setTimeout(function(){ try { sslRefreshStatus(); } catch(e){} }, 100);
  return html;
}

async function cfgSaveSection(section, resultElId) {
  // resultElId is optional — defaults to the page-wide #cfg-save-result.
  // Merged pages (Audio & Speakers) pass a per-subsection ID so each
  // save button updates its own status span.
  const data = cfgCollectForm(section);
  const resultEl = document.getElementById(resultElId || 'cfg-save-result');
  if (resultEl) {
    resultEl.textContent = 'Saving...';
    resultEl.className = 'cfg-result';
  }
  try {
    const r = await fetch('/api/config/' + section, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData[section] = data;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) {
      resultEl.textContent = 'Error: ' + e.message;
      resultEl.className = 'cfg-result err';
    }
  }
}

function cfgRenderRaw() {
  const meta = SECTION_META.raw || {};
  let html = '<div class="cfg-section-header">'
    + '<div class="cfg-section-title">' + escHtml(meta.title || 'Raw YAML') + '</div>'
    + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
    + '</div>';

  const files = ['global', 'services', 'speakers', 'audio', 'personality'];
  html += '<div class="cfg-file-tabs">';
  files.forEach(f => {
    const cls = f === _cfgCurrentRawFile ? 'cfg-file-tab active' : 'cfg-file-tab';
    html += '<button class="' + cls + '" onclick="cfgSwitchRawFile(\'' + f + '\')">' + f + '.yaml</button>';
  });
  html += '</div>';
  const content = _cfgRaw[_cfgCurrentRawFile] || '';
  html += '<textarea class="cfg-textarea" id="cfg-raw-editor">' + content.replace(/</g,'&lt;') + '</textarea>';
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveRaw()">Save ' + _cfgCurrentRawFile + '.yaml</button>'
    + '<span id="cfg-save-result" class="cfg-result"></span>'
    + '</div>';
  document.getElementById('cfg-form-area').innerHTML = html;
}

function cfgSwitchRawFile(name) {
  _cfgCurrentRawFile = name;
  cfgRenderRaw();
}

async function cfgSaveRaw() {
  const content = document.getElementById('cfg-raw-editor').value;
  const resultEl = document.getElementById('cfg-save-result');
  resultEl.textContent = 'Saving...';
  resultEl.className = 'cfg-result';
  try {
    const r = await fetch('/api/config/raw', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: _cfgCurrentRawFile, content})
    });
    if (r.ok) {
      let resp = {};
      try { resp = await r.json(); } catch (_) { /* old server */ }
      resultEl.textContent = '';
      if (resp && resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
      _cfgRaw[_cfgCurrentRawFile] = content;
      await cfgLoadAll();
    } else {
      let msg = 'Error (' + r.status + ')';
      try {
        const body = await r.json();
        if (body && body.error) msg = body.error;
      } catch (_) {}
      resultEl.textContent = msg;
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    resultEl.textContent = 'Error: ' + e.message;
    resultEl.className = 'cfg-result err';
  }
}

async function cfgReload() {
  const status = document.getElementById('cfg-status');
  status.textContent = 'Reloading...';
  try {
    const r = await fetch('/api/config/reload', {method: 'POST'});
    if (r.ok) {
      await cfgLoadAll();
      cfgRenderSection(_cfgCurrentSection === 'raw' ? 'global' : _cfgCurrentSection);
      status.textContent = '';
      showToast('Reloaded from disk.', 'success');
    } else {
      status.textContent = 'Reload failed';
    }
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  }
}

// Load config data on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
  cfgLoadAll();
});

/* ===============================================================
   Memory page (Phase 5) — long-term facts, recent activity,
   passive default-status toggle, manual retention sweep.
   =============================================================== */

let _memCachedConfig = null;

function memoryLoadAll() {
  memLoadConfig();
  memLoadFacts();
  memLoadRecent();
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   CONFIGURATION > LOGS (Phase 6 follow-up)
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _logsRawLines = [];          // latest raw lines from /api/logs/tail
let _logsSources = [];
let _logsAutoTimer = null;
const LOGS_AUTO_INTERVAL_MS = 10000;

// Tab entry: on first visit populate the source list; refresh every time.
async function logsOnTabActivate() {
  if (_logsSources.length === 0) {
    try {
      const r = await fetch('/api/logs/sources');
      if (r.status === 401) { logsSetStatus('auth required'); return; }
      const j = await r.json();
      _logsSources = j.sources || [];
      const sel = document.getElementById('logsSource');
      sel.innerHTML = '';
      for (const s of _logsSources) {
        const opt = document.createElement('option');
        opt.value = s.key; opt.textContent = s.label; sel.appendChild(opt);
      }
    } catch (e) {
      logsSetStatus('failed to list sources: ' + e.message);
      return;
    }
  }
  logsUpdateSourceDesc();
  logsRefresh();
}

function logsOnSourceChange() {
  logsUpdateSourceDesc();
  logsRefresh();
}

function logsUpdateSourceDesc() {
  const key = document.getElementById('logsSource').value;
  const s = _logsSources.find(x => x.key === key);
  document.getElementById('logsSourceDesc').textContent = s ? s.desc : '';
}

function logsSetStatus(msg) {
  document.getElementById('logsStatus').textContent = msg || '';
}

async function logsRefresh() {
  const source = document.getElementById('logsSource').value;
  const lines  = document.getElementById('logsLines').value;
  if (!source) return;
  logsSetStatus('loading...');
  try {
    const r = await fetch('/api/logs/tail?source=' + encodeURIComponent(source) + '&lines=' + lines);
    if (r.status === 401) { logsSetStatus('auth required'); return; }
    const j = await r.json();
    if (!r.ok) {
      _logsRawLines = [];
      document.getElementById('logsBody').textContent = 'error: ' + (j.error || r.status);
      logsSetStatus('error');
      return;
    }
    _logsRawLines = j.lines || [];
    logsRerender();
    const when = new Date().toLocaleTimeString();
    logsSetStatus(`${_logsRawLines.length} line${_logsRawLines.length===1?'':'s'} · refreshed ${when}`);
    // Pin to bottom after each refresh so new content is visible.
    const vp = document.querySelector('.logs-viewport');
    if (vp) vp.scrollTop = vp.scrollHeight;
  } catch (e) {
    logsSetStatus('fetch failed: ' + e.message);
  }
}

// Classify a line's severity. Works for loguru default format, Python's
// stdlib logging, and audit JSONL (checks for "level":"ERROR" patterns).
function _logsSeverity(line) {
  const s = line || '';
  if (/\|\s*ERROR\s*\||\bERROR\b|\"level\":\s*\"ERROR\"|Traceback|Exception:|Error:/i.test(s)) return 'error';
  if (/\|\s*WARN(ING)?\s*\||\bWARN(ING)?\b|\"level\":\s*\"WARNING\"/i.test(s)) return 'warn';
  if (/\|\s*SUCCESS\s*\||\"level\":\s*\"SUCCESS\"/i.test(s)) return 'success';
  if (/\|\s*INFO\s*\||\"level\":\s*\"INFO\"/i.test(s)) return 'info';
  if (/\|\s*DEBUG\s*\||\"level\":\s*\"DEBUG\"/i.test(s)) return 'dim';
  return null;
}

function logsRerender() {
  const filter = document.getElementById('logsFilter').value;
  const body = document.getElementById('logsBody');
  if (_logsRawLines.length === 0) {
    body.textContent = '(no log content yet)';
    return;
  }
  const frag = document.createDocumentFragment();
  let shown = 0;
  for (const raw of _logsRawLines) {
    const sev = _logsSeverity(raw);
    if (filter === 'error' && sev !== 'error') continue;
    if (filter === 'warn' && sev !== 'error' && sev !== 'warn') continue;
    const span = document.createElement('span');
    if (sev) span.className = 'log-' + sev;
    span.textContent = raw + '\n';
    frag.appendChild(span);
    shown++;
  }
  body.innerHTML = '';
  if (shown === 0) {
    body.textContent = '(filter matches no lines)';
  } else {
    body.appendChild(frag);
  }
}

function logsToggleAuto() {
  const on = document.getElementById('logsAuto').checked;
  if (on && !_logsAutoTimer) {
    _logsAutoTimer = setInterval(logsRefresh, LOGS_AUTO_INTERVAL_MS);
    logsRefresh();
  } else if (!on && _logsAutoTimer) {
    clearInterval(_logsAutoTimer);
    _logsAutoTimer = null;
  }
}

async function memLoadConfig() {
  try {
    const r = await fetch('/api/config/memory');
    if (!r.ok) return;
    const cfg = await r.json();
    _memCachedConfig = cfg;
    const defaultStatus = cfg.passive_default_status || 'approved';
    document.querySelectorAll('input[name="memDefaultStatus"]').forEach(rb => {
      rb.checked = (rb.value === defaultStatus);
    });
    const pendingCard = document.getElementById('memPendingCard');
    if (pendingCard) {
      pendingCard.style.display = (defaultStatus === 'pending') ? '' : 'none';
    }
    if (defaultStatus === 'pending') memLoadPending();
  } catch(e) { /* ignore */ }
}

async function memSaveDefaultStatus(val) {
  if (!_memCachedConfig) {
    // Fetch latest before PUT to preserve other fields.
    try {
      const r = await fetch('/api/config/memory');
      if (r.ok) _memCachedConfig = await r.json();
    } catch(e) {}
  }
  const body = Object.assign({}, _memCachedConfig || {}, {passive_default_status: val});
  try {
    const r = await fetch('/api/config/memory', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      showToast('Save failed: ' + t, 'error');
      return;
    }
    showToast('Default status saved: ' + val, 'success');
    memLoadConfig();
  } catch(e) {
    showToast('Save failed: ' + e.message, 'error');
  }
}

function memShowAddForm() { document.getElementById('memAddForm').style.display = ''; }
function memHideAddForm() {
  document.getElementById('memAddForm').style.display = 'none';
  document.getElementById('memAddText').value = '';
}

// Segmented control helper — shared by add form and edit form.
function memSegSelect(cell, segId) {
  document.querySelectorAll('#' + segId + ' .tts-seg-cell').forEach(c => c.classList.remove('on'));
  cell.classList.add('on');
}

// Map a 0-1 numeric importance value to the nearest 5-point label.
function _memImportanceLabel(val) {
  const v = Number(val) || 0;
  if (v <= 0.10) return 'Background';
  if (v <= 0.30) return 'Background';
  if (v <= 0.50) return 'Useful';
  if (v <= 0.70) return 'Important';
  if (v <= 0.90) return 'Critical';
  return 'Extreme';
}

async function memAddFact() {
  const text = document.getElementById('memAddText').value.trim();
  if (!text) { showToast('Text required', 'error'); return; }
  const onCell = document.querySelector('#memAddImportanceSeg .tts-seg-cell.on');
  const importance = onCell ? parseFloat(onCell.dataset.value) : 0.60;
  try {
    const r = await fetch('/api/memory/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: text, importance: importance}),
    });
    const data = await r.json();
    if (!data.added) {
      showToast('Add failed: ' + (data.error || 'unknown'), 'error');
      return;
    }
    memHideAddForm();
    memLoadFacts();
    memLoadRecent();
    showToast('Fact added', 'success');
  } catch(e) {
    showToast('Add failed: ' + e.message, 'error');
  }
}

let _memSearchTimer;
function memSearchDebounced() {
  clearTimeout(_memSearchTimer);
  _memSearchTimer = setTimeout(memLoadFacts, 300);
}

async function memLoadFacts() {
  const q = document.getElementById('memSearchInput');
  const qv = q ? q.value.trim() : '';
  const url = qv
    ? '/api/memory/list?q=' + encodeURIComponent(qv) + '&limit=50'
    : '/api/memory/list?limit=50';
  try {
    const r = await fetch(url);
    const data = await r.json();
    _memRenderFacts(data.rows || []);
  } catch(e) {
    document.getElementById('memFactsList').textContent = 'Load failed: ' + e.message;
  }
}

function _memRenderFacts(rows) {
  const el = document.getElementById('memFactsList');
  if (rows.length === 0) {
    el.innerHTML = '<div style="color:var(--fg-secondary);padding:8px;">No facts yet. Click + Add to record one.</div>';
    return;
  }
  let html = '';
  rows.forEach(r => { html += _memFactCard(r); });
  el.innerHTML = html;
}

// Shared icon SVGs — declared ONCE in this external script because inline
// <script> blocks in pages/*.py and /static/ui.js share global window
// scope; duplicate `const` between them is a SyntaxError that aborts the
// entire SPA at parse time. See feedback_devtools_console_first.md.
const _PENCIL_SVG  = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 2 L14 5 L5 14 L2 14 L2 11 Z M10 3 L13 6"/></svg>';
const _TRASH_SVG   = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5 L13 5 M5 5 L5 13 L11 13 L11 5 M6 3 L10 3 L10 5"/></svg>';
const _DISABLE_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><line x1="3.5" y1="3.5" x2="12.5" y2="12.5"/></svg>';

function _memFactCard(r) {
  const m = r.metadata || {};
  const sourceRaw = m.source || '';
  const source = sourceRaw.replace(/^user_/, '') || 'unknown';
  const impLabel = _memImportanceLabel(m.importance);
  const mentions = m.mention_count || 1;
  const age = _memFmtAge(m.written_at);
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  const imp = Number(m.importance || 0).toFixed(2);
  return '<div class="mem-fact" data-id="' + id + '" data-importance="' + imp + '" style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">'
    + '<div style="min-width:0;flex:1;">'
    +   '<div class="mem-fact-text">' + doc + '</div>'
    +   '<div class="mem-fact-meta">source=' + escHtml(source)
        + '  importance=' + impLabel
        + '  mentions=' + mentions
        + '  age=' + age + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:4px;flex-shrink:0;margin-top:2px;">'
    +   '<button class="ico-btn" title="Edit" onclick="memEdit(\'' + id + '\')">' + _PENCIL_SVG + '</button>'
    +   '<button class="ico-btn danger" title="Delete" onclick="memDelete(\'' + id + '\')">' + _TRASH_SVG + '</button>'
    + '</div>'
    + '</div>';
}

function _memFmtAge(ts) {
  if (!ts) return '?';
  const d = (Date.now() / 1000) - Number(ts);
  if (d < 60)    return Math.max(0, Math.floor(d)) + 's';
  if (d < 3600)  return Math.floor(d / 60) + 'm';
  if (d < 86400) return Math.floor(d / 3600) + 'h';
  return Math.floor(d / 86400) + 'd';
}

// Bucket a numeric importance value to its 5-point segment value, using the
// same thresholds as _memImportanceLabel so the pre-selected segment matches
// the label rendered in the row meta.
function _memImportanceToSeg(v) {
  const x = Number(v) || 0;
  if (x <= 0.30) return 0.20;
  if (x <= 0.50) return 0.40;
  if (x <= 0.70) return 0.60;
  if (x <= 0.90) return 0.80;
  return 1.00;
}

function memEdit(id) {
  const row = document.querySelector('.mem-fact[data-id="' + id + '"], .mem-recent[data-id="' + id + '"], .mem-pending[data-id="' + id + '"]');
  if (!row) return;
  const existing = row.parentNode.querySelector('.mem-edit-panel[data-edit-for="' + id + '"]');
  if (existing) {
    const ta = existing.querySelector('textarea');
    if (ta) ta.focus();
    return;
  }
  const currentText = (row.querySelector('.mem-fact-text, strong') || {}).textContent || '';
  const currentImp = parseFloat(row.dataset.importance || '0.60');
  const seg = _memImportanceToSeg(currentImp);
  const idAttr = escAttr(id);
  const segId = 'memEditSeg-' + idAttr;
  const opts = [
    [0.20, 'Background'],
    [0.40, 'Useful'],
    [0.60, 'Important'],
    [0.80, 'Critical'],
    [1.00, 'Extreme'],
  ];
  const cells = opts.map(o => {
    const v = o[0], label = o[1];
    const on = (Math.abs(v - seg) < 1e-6) ? ' on' : '';
    return '<div class="tts-seg-cell' + on + '" data-value="' + v.toFixed(2) + '"'
      + ' onclick="memSegSelect(this,\'' + segId + '\')">' + label + '</div>';
  }).join('');
  const html = '<div class="mem-edit-panel" data-edit-for="' + idAttr + '" style="margin-top:4px;padding:10px;background:var(--bg-input);border:1px solid var(--border-default);border-radius:var(--r-input);">'
    + '<textarea class="mem-edit-text" style="width:100%;min-height:60px;">' + escHtml(currentText) + '</textarea>'
    + '<div style="margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
    +   '<span style="font-size:0.82rem;color:var(--text-dim);">Importance:</span>'
    +   '<div class="tts-seg" id="' + segId + '">' + cells + '</div>'
    +   '<button class="btn-small" onclick="memEditSave(\'' + idAttr + '\')">Save</button>'
    +   '<button class="btn-small" onclick="memEditCancel(\'' + idAttr + '\')" style="background:#555;">Cancel</button>'
    + '</div>'
    + '</div>';
  row.insertAdjacentHTML('afterend', html);
  const panel = row.parentNode.querySelector('.mem-edit-panel[data-edit-for="' + idAttr + '"]');
  const ta = panel ? panel.querySelector('textarea') : null;
  if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}

async function memEditSave(id) {
  const panel = document.querySelector('.mem-edit-panel[data-edit-for="' + id + '"]');
  if (!panel) return;
  const ta = panel.querySelector('textarea');
  const newText = (ta ? ta.value : '').trim();
  if (!newText) { showToast('Text required', 'error'); return; }
  const onCell = panel.querySelector('.tts-seg-cell.on');
  const importance = onCell ? parseFloat(onCell.dataset.value) : 0.60;
  try {
    const r = await fetch('/api/memory/' + encodeURIComponent(id) + '/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: newText, importance: importance}),
    });
    const data = await r.json();
    if (!data.updated) {
      showToast('Edit failed: ' + (data.error || 'unknown'), 'error');
      return;
    }
    memLoadFacts(); memLoadRecent(); memLoadPending();
    showToast('Edited', 'success');
  } catch(e) {
    showToast('Edit failed: ' + e.message, 'error');
  }
}

function memEditCancel(id) {
  const panel = document.querySelector('.mem-edit-panel[data-edit-for="' + id + '"]');
  if (panel) panel.remove();
}

async function memDelete(id) {
  if (!confirm('Delete this fact? This cannot be undone.')) return;
  try {
    await fetch('/api/memory/' + encodeURIComponent(id), {method: 'DELETE'});
    memLoadFacts(); memLoadRecent(); memLoadPending();
    showToast('Deleted', 'success');
  } catch(e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function memPromote(id) {
  try {
    await fetch('/api/memory/' + encodeURIComponent(id) + '/promote', {method: 'POST'});
    memLoadPending(); memLoadFacts(); memLoadRecent();
    showToast('Approved', 'success');
  } catch(e) {
    showToast('Approve failed: ' + e.message, 'error');
  }
}

async function memReject(id) {
  if (!confirm('Reject this fact? It will not enter RAG.')) return;
  try {
    await fetch('/api/memory/' + encodeURIComponent(id) + '/demote', {method: 'POST'});
    memLoadPending();
    showToast('Rejected', 'success');
  } catch(e) {
    showToast('Reject failed: ' + e.message, 'error');
  }
}

// Recently learned: reuses /api/memory/list; filters to auto-learned
// (passive-origin) facts only — source stored as "user_passive" by
// memory_writer.py. Sorted by recency, shows top 10.
async function memLoadRecent() {
  try {
    const r = await fetch('/api/memory/list?limit=200');
    const data = await r.json();
    const rows = (data.rows || []).filter(row => {
      const src = (row.metadata || {}).source || '';
      return src === 'user_passive';
    });
    rows.sort((a, b) => {
      const am = a.metadata || {}, bm = b.metadata || {};
      const at = Math.max(Number(am.last_mentioned_at || 0), Number(am.written_at || 0));
      const bt = Math.max(Number(bm.last_mentioned_at || 0), Number(bm.written_at || 0));
      return bt - at;
    });
    _memRenderRecent(rows.slice(0, 10));
  } catch(e) {
    document.getElementById('memRecentList').textContent = 'Load failed: ' + e.message;
  }
}

function _memRenderRecent(rows) {
  const el = document.getElementById('memRecentList');
  if (rows.length === 0) {
    el.innerHTML = '<div style="color:var(--fg-secondary);padding:8px;">No auto-learned facts yet.</div>';
    return;
  }
  let html = '';
  rows.forEach(r => { html += _memRecentItem(r); });
  el.innerHTML = html;
}

function _memRecentItem(r) {
  const m = r.metadata || {};
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  const mentions = m.mention_count || 1;
  const isReinforcement = mentions > 1;
  const age = _memFmtAge(Math.max(Number(m.last_mentioned_at || 0), Number(m.written_at || 0)));
  const lastText = m.last_mention_text || '';
  const canUpdate = isReinforcement && lastText && lastText !== (r.document || '');
  const impLabel = _memImportanceLabel(m.importance);
  const origLabel = _memImportanceLabel(m.original_importance);
  const imp = Number(m.importance || 0).toFixed(2);
  let statusLabel = isReinforcement
    ? '<span class="mem-bump">reinforced</span> ' + origLabel + ' &rarr; ' + impLabel + ', mentions=' + mentions
    : 'new  importance=' + impLabel;
  const status = (m.review_status === 'pending') ? '  <span style="color:var(--orange);">pending</span>' : '';
  let html = '<div class="mem-recent" data-id="' + id + '" data-importance="' + imp + '" style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">'
    + '<div style="min-width:0;flex:1;">'
    +   '<div class="mem-fact-text">' + doc + '</div>'
    +   '<div class="mem-fact-meta">' + statusLabel + status + '  &bull;  ' + age + ' ago</div>';
  if (canUpdate) {
    html += '<div class="mem-fact-meta">Latest mention: &ldquo;' + escHtml(lastText) + '&rdquo;</div>'
      + '<div class="mem-fact-actions" style="margin-top:4px;">'
      + '<button class="btn-small" onclick="memUpdateWording(\'' + id + '\')">Update wording</button>'
      + '</div>';
  }
  html += '</div>'
    + '<div style="display:flex;gap:4px;flex-shrink:0;margin-top:2px;">'
    +   '<button class="ico-btn" title="Edit" onclick="memEdit(\'' + id + '\')">' + _PENCIL_SVG + '</button>'
    +   '<button class="ico-btn danger" title="Delete" onclick="memDelete(\'' + id + '\')">' + _TRASH_SVG + '</button>'
    + '</div>'
    + '</div>';
  return html;
}

async function memUpdateWording(id) {
  // Refetch to get the latest last_mention_text for the target id,
  // then POST the edit. Keeps the button idempotent and avoids stale
  // cached text.
  try {
    const r = await fetch('/api/memory/list?limit=200');
    const data = await r.json();
    const match = (data.rows || []).find(x => x.id === id);
    if (!match) return;
    const lt = (match.metadata || {}).last_mention_text;
    if (!lt) { showToast('No alternate wording available', 'error'); return; }
    await fetch('/api/memory/' + encodeURIComponent(id) + '/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: lt}),
    });
    memLoadFacts(); memLoadRecent();
    showToast('Wording updated', 'success');
  } catch(e) {
    showToast('Update failed: ' + e.message, 'error');
  }
}

async function memLoadPending() {
  try {
    const r = await fetch('/api/memory/pending?limit=50');
    const data = await r.json();
    const el = document.getElementById('memPendingList');
    const rows = data.rows || [];
    if (rows.length === 0) {
      el.innerHTML = '<div style="color:var(--fg-secondary);padding:8px;">Nothing pending.</div>';
      return;
    }
    let html = '';
    rows.forEach(r => { html += _memPendingCard(r); });
    el.innerHTML = html;
  } catch(e) {
    document.getElementById('memPendingList').textContent = 'Load failed: ' + e.message;
  }
}

function _memPendingCard(r) {
  const m = r.metadata || {};
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  const importance = Number(m.importance || 0).toFixed(2);
  const age = _memFmtAge(m.written_at);
  return '<div class="mem-pending" data-id="' + id + '" data-importance="' + importance + '">'
    + '<div><strong>' + doc + '</strong></div>'
    + '<div class="mem-fact-meta">source=passive  importance=' + importance + '  age=' + age + '</div>'
    + '<div class="mem-fact-actions" style="display:flex;align-items:center;gap:6px;">'
    +   '<button class="btn-small" onclick="memPromote(\'' + id + '\')">Approve</button>'
    +   '<button class="ico-btn" title="Edit" onclick="memEdit(\'' + id + '\')">' + _PENCIL_SVG + '</button>'
    +   '<button class="ico-btn danger" title="Reject" onclick="memReject(\'' + id + '\')">' + _TRASH_SVG + '</button>'
    + '</div></div>';
}

async function memSweepRetention() {
  const s = document.getElementById('memRetentionStatus');
  s.textContent = 'Sweeping...';
  try {
    const r = await fetch('/api/retention/sweep', {method: 'POST'});
    const data = await r.json();
    if (data.ok) {
      const parts = Object.entries(data.counts || {}).map(([k, v]) => k + '=' + v).join(', ');
      s.textContent = 'Done: ' + (parts || 'no changes');
      showToast('Retention swept', 'success');
    } else {
      s.textContent = 'Error: ' + (data.error || 'unknown');
      showToast('Sweep failed', 'error');
    }
  } catch(e) {
    s.textContent = 'Error: ' + e.message;
    showToast('Sweep failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Shared utilities
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

// ── Phase 5 navigation ──────────────────────────────────────────
// Dotted keys drive the whole UI now: "chat", "tts", "config.system",
// "config.global", ..., "config.memory", "config.raw". The mapping
// from key → DOM panel lives in _panelIdFor(); Configuration children
// all render into the single #tab-config host (except System and
// Memory which have their own HTML).

function _panelIdFor(key) {
  if (key === 'config.system') return 'tab-config-system';
  if (key === 'config.memory') return 'tab-config-memory';
  if (key === 'config.logs')   return 'tab-config-logs';
  if (key && key.indexOf('config.') === 0) return 'tab-config';
  return 'tab-' + key;
}

// Legacy keys translated on read so operators don't see a blank page
// after upgrade. Two generations of legacy keys are supported now:
//   • pre-Phase-5: bare 'tts'/'chat'/'control'/'config'
//   • pre-Phase-6: 'config.global' / '.services' / '.speakers' / '.audio'
// If cfgLoadAll hasn't populated _cfgData yet by the time these old
// pages were stored, the new virtual equivalents still render once
// the data arrives.
function _migrateLegacyKey(k) {
  if (k === 'control') return 'config.system';
  if (k === 'config')  return 'config.integrations';
  if (k === 'config.global')      return 'config.integrations';
  // legacy: config.services and config.llm-services both go to System → Services
  if (k === 'config.services')    return 'config.system';
  if (k === 'config.llm-services') return 'config.system';
  if (k === 'config.speakers')    return 'config.audio-speakers';
  if (k === 'config.audio')       return 'config.audio-speakers';
  if (k === 'config.ssl')         return 'config.system';
  if (k === 'config.users')       return 'config.system';
  return k;
}

function navToggleConfig() {
  // Clicking the Configuration parent toggles the submenu when it's
  // expanded but we're NOT on a config.* page. When on a child page,
  // the submenu is already pinned open (auto-expand) — toggling it
  // would hide the current page's sibling links, so do nothing.
  const parent = document.querySelector('.nav-parent[data-nav-key="config"]');
  if (!parent) return;
  const onChild = (_activeNavKey || '').indexOf('config.') === 0;
  if (onChild) return;
  parent.classList.toggle('open');
}

let _activeNavKey = 'chat';

function navigateTo(key) {
  // Capture legacy sub-tab intent before migration collapses the key.
  var _sslRedirect      = (key === 'config.ssl');
  var _usersRedirect    = (key === 'config.users');
  var _servicesRedirect = (key === 'config.llm-services' || key === 'config.services');
  key = _migrateLegacyKey(key);
  // Leaving Logs? Tear down the 10 s polling timer so we don't keep
  // hitting /api/logs/tail when the operator's on another page.
  if (_activeNavKey === 'config.logs' && key !== 'config.logs') {
    const el = document.getElementById('logsAuto');
    if (el && el.checked) { el.checked = false; logsToggleAuto(); }
  }
  _activeNavKey = key;

  const panelId = _panelIdFor(key);
  const panel = document.getElementById(panelId);
  if (!panel) return;

  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  panel.classList.add('active');

  // Highlight matching nav items in both sidebar and topbar.
  document.querySelectorAll('.nav-item[data-nav-key="' + key + '"]').forEach(n => {
    n.classList.add('active');
  });

  // Auto-expand Configuration parent iff the active key is a child.
  const parent = document.querySelector('.nav-parent[data-nav-key="config"]');
  if (parent) {
    if (key.indexOf('config.') === 0) parent.classList.add('open');
    else parent.classList.remove('open');
  }

  try { localStorage.setItem('glados_active_tab', key); } catch(e) {}

  // Tab activation hooks
  if (key === 'config.system') {
    // Phase 6.1 (2026-04-22): Weather + GPU cards removed from System
    // (Weather moving to Integrations; GPU was redundant). Audio
    // storage + Reload from Disk moved here from the Configuration
    // shell, so loadAudioStats needs to fire on this hook now.
    loadModes(); loadSpeakers(); loadHealth(); loadEyeDemo();
    loadRobots();
    loadAudioStats();
    loadSystemServices();
    startRobotAutoRefresh();
    if (typeof loadSystemConfigCards === 'function') loadSystemConfigCards();
    if (_sslRedirect)      { showPageTab('system', 'ssl');      _loadSslIntoSystemTab(); }
    if (_usersRedirect)    { showPageTab('system', 'users');    _loadUsersIntoSystemTab(); }
    if (_servicesRedirect) { showPageTab('system', 'services'); }
  } else if (key === 'config.memory') {
    // Memory page UI arrives in Phase 5 Commit 3; placeholder for now.
    if (typeof memoryLoadAll === 'function') memoryLoadAll();
  } else if (key === 'config.logs') {
    if (typeof logsOnTabActivate === 'function') logsOnTabActivate();
  } else if (key.indexOf('config.') === 0) {
    const section = key.substring('config.'.length);
    _cfgCurrentSection = section;
    cfgLoadAll().then(() => {
      if (section === 'raw') cfgLoadRaw().then(() => cfgRenderRaw());
      else cfgRenderSection(section);
    });
    loadAudioStats();
  } else if (key === 'training') {
    initTrainingTab();
  } else if (key === 'chat') {
    const ci = document.getElementById('chatInput');
    if (ci) ci.focus();
  } else if (key === 'tts') {
    const ti = document.getElementById('textInput');
    if (ti) ti.focus();
  }
}

// Backward-compat shim for any older inline onclick that still calls
// switchTab(). Routes through the new key mapping.
function switchTab(name) { navigateTo(_migrateLegacyKey(name)); }

// Check auth on load, THEN restore saved tab (default: TTS for unauth, Chat for auth).
checkAuth().then(() => {
  let restored = false;
  try {
    const raw = localStorage.getItem('glados_active_tab');
    if (raw) {
      const key = _migrateLegacyKey(raw);
      if (document.getElementById(_panelIdFor(key))) {
        navigateTo(key);
        restored = true;
      }
    }
  } catch(e) {}
  if (!restored) navigateTo(_isAuthenticated ? 'chat' : 'tts');
  // Phase 5: sidebar engine status dot.
  startEngineStatusPoll();
});

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function escAttr(s) {
  return String(s).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}
function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 1: TTS Generator
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const textInput = document.getElementById('textInput');
const genBtn    = document.getElementById('generateBtn');

const _icoDl    = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2 L8 11 M4 7 L8 11 L12 7 M3 14 L13 14"/></svg>';
const _icoDel   = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5 L13 5 M5 5 L5 13 L11 13 L11 5 M6 3 L10 3 L10 5"/></svg>';
const _icoSave  = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 8 L6 11 L13 4"/></svg>';
const _icoSaved = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8 L6 12 L14 4" stroke="var(--green)"/></svg>';

// Keyboard shortcut: Ctrl+Enter in the script textarea triggers Generate.
if (textInput) {
  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ttsGenerate(); }
  });
}

// ── Single shared <audio> element + play/stop state ─────────────
let _ttsPlayingName = null;
function _ttsAudio() { return document.getElementById('ttsAudio'); }
function _ttsPlayBtn(name) {
  return document.querySelector('.tts-row[data-name="' + (name || '').replace(/"/g, '\\"') + '"] .tts-row-play');
}
function _ttsResetPlayBtn(name) {
  const b = _ttsPlayBtn(name);
  if (b) { b.textContent = '\u25B6'; b.classList.remove('playing'); }
}
function ttsTogglePlay(btn) {
  const row = btn.closest('.tts-row');
  if (!row) return;
  const name = row.getAttribute('data-name');
  const url  = row.getAttribute('data-url');
  const audio = _ttsAudio();
  if (!audio) return;
  if (_ttsPlayingName === name) {
    audio.pause();
    audio.currentTime = 0;
    _ttsResetPlayBtn(name);
    _ttsPlayingName = null;
    return;
  }
  if (_ttsPlayingName) _ttsResetPlayBtn(_ttsPlayingName);
  audio.src = url;
  audio.play().then(() => {
    btn.textContent = '\u25A0';
    btn.classList.add('playing');
    _ttsPlayingName = name;
  }).catch(err => console.error('audio play failed:', err));
}
(function _ttsBindAudio() {
  const a = _ttsAudio();
  if (!a) return;
  a.addEventListener('ended', () => {
    if (_ttsPlayingName) { _ttsResetPlayBtn(_ttsPlayingName); _ttsPlayingName = null; }
  });
})();

// ── File row builder + list refresh ─────────────────────────────
function _ttsRowHtml(f) {
  const promptHtml = f.prompt
    ? '<div class="tts-row-prompt" title="' + escAttr(f.prompt) + '">' + escHtml(f.prompt) + '</div>'
    : '';
  const sizeStr = fmtSize(f.size || 0);
  return '<div class="tts-row" data-name="' + escAttr(f.name) + '" data-url="' + escAttr(f.url) + '">'
    + '<button class="tts-row-play" onclick="ttsTogglePlay(this)" title="Play / stop">\u25B6</button>'
    + '<div class="tts-row-body">'
    +   '<div class="tts-row-name">' + escHtml(f.name) + '</div>'
    +   promptHtml
    +   '<div class="tts-row-meta">' + sizeStr + '</div>'
    + '</div>'
    + '<div class="tts-row-actions">'
    +   '<a class="ico-btn" title="Download" href="' + escAttr(f.url) + '" download="' + escAttr(f.name) + '">' + _icoDl + '</a>'
    +   '<button class="ico-btn" title="Save to library" onclick="_ttsRowSaveToggle(this, \'' + escAttr(f.name) + '\')">' + _icoSave + '</button>'
    +   '<button class="ico-btn danger" title="Delete" onclick="_ttsRowDelete(\'' + escAttr(f.name) + '\')">' + _icoDel + '</button>'
    + '</div>'
    + '</div>';
}

async function _ttsLoadFiles() {
  const list = document.getElementById('ttsFileList');
  if (!list) return;
  try {
    const resp = await fetch('/api/files');
    const data = await resp.json();
    const files = data.files || [];
    list.innerHTML = files.map(_ttsRowHtml).join('') || '<div class="tts-row-meta" style="padding:var(--sp-3) 0;">No files yet.</div>';
  } catch (e) {
    console.error('TTS file load failed:', e);
  }
}

// Save-to-library inline form (toggles below the row)
async function _ttsRowSaveToggle(saveBtn, filename) {
  const row = saveBtn.closest('.tts-row');
  if (!row) return;
  const existing = row.querySelector('.tts-row-save-form');
  if (existing) { existing.remove(); return; }

  const form = document.createElement('div');
  form.className = 'tts-row-save-form';

  const catSel = document.createElement('select');
  catSel.innerHTML = '<option value="">-- pick category --</option><option value="__new__">-- new category --</option>';
  form.appendChild(catSel);

  const fnInput = document.createElement('input');
  fnInput.type = 'text';
  fnInput.placeholder = 'filename (optional)';
  fnInput.value = filename || '';
  form.appendChild(fnInput);

  const btn = document.createElement('button');
  btn.className = 'btn btn-primary';
  btn.textContent = 'Save';
  form.appendChild(btn);

  const statusEl = document.createElement('span');
  statusEl.style.fontSize = '0.7rem';
  form.appendChild(statusEl);

  row.appendChild(form);

  try {
    const r = await fetch('/api/config/sound_categories');
    if (r.ok) {
      const cfg = await r.json();
      const cats = (cfg.categories || []).slice().sort((a,b) => a.name.localeCompare(b.name));
      catSel.innerHTML = '<option value="">-- pick category --</option>';
      for (const c of cats) catSel.appendChild(new Option(c.name + ' (' + (c.description||'').slice(0,40) + ')', c.name));
      catSel.appendChild(new Option('-- new category --', '__new__'));
    }
  } catch(e) { console.error('failed to load sound categories:', e); }

  btn.onclick = async () => {
    let category = catSel.value;
    let createNew = false, newDesc = '';
    if (category === '__new__') {
      category = prompt('New category name (lowercase letters, digits, underscores):');
      if (!category) return;
      category = category.trim().toLowerCase();
      if (!/^[a-z][a-z0-9_]*$/.test(category)) {
        statusEl.innerHTML = '<span style="color:var(--red)">Invalid name.</span>';
        return;
      }
      newDesc = prompt('Short description of this category:') || '';
      createNew = true;
    }
    if (!category) { statusEl.innerHTML = '<span style="color:var(--orange)">Pick a category.</span>'; return; }
    const save_as = fnInput.value.trim() || filename;
    btn.disabled = true;
    statusEl.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await fetch('/api/tts/save-to-category', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ source_filename: filename, category, save_as, create_new: createNew, new_category_description: newDesc }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Save failed');
      saveBtn.innerHTML = _icoSaved;
      saveBtn.disabled = true;
      form.remove();
    } catch(e) {
      statusEl.innerHTML = '<span style="color:var(--red)">' + escHtml(String(e.message||e)) + '</span>';
      btn.disabled = false;
    }
  };
}

async function _ttsRowDelete(filename) {
  if (!confirm('Delete ' + filename + '?')) return;
  if (_ttsPlayingName === filename) {
    const a = _ttsAudio(); if (a) { a.pause(); a.currentTime = 0; }
    _ttsPlayingName = null;
  }
  try { await fetch('/api/files/' + encodeURIComponent(filename), { method: 'DELETE' }); } catch(e) {}
  await _ttsLoadFiles();
}

// Script mode: generate
async function ttsGenerate() {
  const text = textInput ? textInput.value.trim() : '';
  if (!text) return;
  if (genBtn) genBtn.disabled = true;
  try {
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text, voice: 'glados', format: 'mp3' }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Generation failed');
    if (textInput) textInput.value = '';
    await _ttsLoadFiles();
  } catch(e) {
    alert('Generate failed: ' + e.message);
  } finally {
    if (genBtn) genBtn.disabled = false;
  }
}

// Mode switch (script / improv) — toggles the corresponding input card.
let _ttsMode = 'script';

function _ttsSwitchMode(mode) {
  _ttsMode = mode;
  const scriptCard = document.getElementById('ttsScriptCard');
  const improvCard = document.getElementById('ttsImprovCard');
  if (mode === 'improv') {
    if (scriptCard) scriptCard.style.display = 'none';
    if (improvCard) improvCard.style.display = '';
  } else {
    if (scriptCard) scriptCard.style.display = '';
    if (improvCard) improvCard.style.display = 'none';
  }
  for (const el of document.querySelectorAll('.tts-seg-cell')) {
    el.classList.toggle('on', el.getAttribute('data-mode') === mode);
  }
  const modeLabel = document.getElementById('ttsModeLabel');
  if (modeLabel) modeLabel.textContent = mode.toUpperCase();
}

// Improv: draft
async function _ttsImprovDraft() {
  const instructionEl = document.getElementById('improvInstruction');
  const draftSection  = document.getElementById('improvDraftSection');
  const draftedTextEl = document.getElementById('improvDraftedText');
  const btn           = document.getElementById('improvDraftBtn');
  const instruction   = instructionEl ? instructionEl.value.trim() : '';
  if (!instruction) return;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/tts/draft', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({instruction}),
    });
    if (!r.ok) { const txt = await r.text(); throw new Error('HTTP ' + r.status + ': ' + txt.slice(0,200)); }
    const data = await r.json();
    if (draftedTextEl) draftedTextEl.value = (data.text || '').trim();
    if (draftSection) draftSection.style.display = '';
  } catch(e) {
    alert('Draft failed: ' + e.message);
    console.error('tts draft failed:', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Improv: generate from draft
async function _ttsImprovGenerate() {
  const textEl = document.getElementById('improvDraftedText');
  const btn    = document.getElementById('improvGenerateBtn');
  const text   = textEl ? textEl.value.trim() : '';
  if (!text) return;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text, voice: 'glados', format: 'mp3' }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Generation failed');
    await _ttsLoadFiles();
  } catch(e) {
    alert('Speak failed: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

_ttsLoadFiles();

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 2: Chat
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let chatHistory = [];
let chatWaiting = false;
let mediaRecorder = null;
let micStream = null;
let chatStreaming = false;
// Chat audio is now handled entirely by the visible <audio controls>
// element in the message DOM. No separate background player.

function renderChat() {
  // Incremental renderer. The previous version rewrote innerHTML on
  // every content chunk during streaming, which destroyed the <audio>
  // element and its playback state each time — the visible controls
  // kept appearing to reset to 0:00 while TTS audio played from a
  // detached (doomed) element. Here we reconcile the DOM against
  // chatHistory: existing <audio> elements are preserved across
  // re-renders so the operator's play/pause/volume/seek interactions
  // stick to a single persistent element.
  const el = document.getElementById('chatMessages');
  if (chatHistory.length === 0) {
    el.innerHTML = '<div class="empty-msg">Send a message to start talking with GLaDOS.</div>';
    return;
  }
  // Clear empty-state div if it exists
  const empty = el.querySelector('.empty-msg');
  if (empty) empty.remove();

  // Reconcile message-div count
  const want = chatHistory.length + (chatWaiting ? 1 : 0);
  while (el.children.length > want) {
    el.removeChild(el.lastChild);
  }
  while (el.children.length < want) {
    const div = document.createElement('div');
    div.className = 'chat-msg';
    el.appendChild(div);
  }

  for (let i = 0; i < chatHistory.length; i++) {
    const msg = chatHistory[i];
    const isLast = (i === chatHistory.length - 1);
    const msgEl = el.children[i];
    msgEl.className = 'chat-msg ' + msg.role;

    if (msg.role === 'user') {
      if (msgEl.textContent !== msg.content) {
        msgEl.textContent = msg.content;
      }
      continue;
    }

    // --- assistant message ---
    // Clear any leftover "Thinking..." placeholder span that was
    // rendered earlier when chatWaiting was true. Without this, the
    // spinner keeps spinning next to the real content because my
    // incremental reconciler reuses the same <div> slot.
    const stale = msgEl.querySelector('.thinking');
    if (stale) stale.remove();

    let labelEl = msgEl.querySelector('.msg-label');
    if (!labelEl) {
      labelEl = document.createElement('div');
      labelEl.className = 'msg-label';
      labelEl.textContent = 'GLaDOS';
      msgEl.appendChild(labelEl);
    }

    let textEl = msgEl.querySelector('.content-text');
    if (!textEl) {
      textEl = document.createElement('span');
      textEl.className = 'content-text';
      // Insert after label
      msgEl.appendChild(textEl);
    }
    if (textEl.textContent !== (msg.content || '')) {
      textEl.textContent = msg.content || '';
    }

    let cursor = msgEl.querySelector('.stream-cursor');
    if (isLast && chatStreaming) {
      if (!cursor) {
        cursor = document.createElement('span');
        cursor.className = 'stream-cursor';
        cursor.textContent = '|';
        msgEl.appendChild(cursor);
      }
    } else if (cursor) {
      cursor.remove();
    }

    // Audio: create ONCE, swap src in place if the URL changes (e.g.
    // streaming -> static replay). Never destroy the element — that's
    // what caused the regression.
    let audioEl = msgEl.querySelector('audio');
    if (msg.audio_url) {
      if (!audioEl) {
        audioEl = document.createElement('audio');
        audioEl.controls = true;
        audioEl.preload = 'auto';
        audioEl.src = msg.audio_url;
        msgEl.appendChild(audioEl);
        audioEl.play().catch(function() {});
      } else {
        const currentSrc = audioEl.getAttribute('src') || '';
        if (currentSrc !== msg.audio_url) {
          // URL swap (streaming URL -> static replay). Preserve
          // playback position and resume-if-was-playing.
          const pos = audioEl.currentTime || 0;
          const wasPlaying = !audioEl.paused && !audioEl.ended;
          audioEl.src = msg.audio_url;
          audioEl.addEventListener('loadedmetadata', function _once() {
            audioEl.removeEventListener('loadedmetadata', _once);
            try { audioEl.currentTime = pos; } catch (_) {}
            if (wasPlaying) audioEl.play().catch(function() {});
          }, {once: true});
          audioEl.load();
        }
      }
    } else if (audioEl) {
      audioEl.remove();
    }

    // Metrics: rebuild contents of the metrics div only when timing changes
    const wantMetrics = !!msg.timing;
    let metricsEl = msgEl.querySelector('.chat-metrics');
    if (wantMetrics) {
      if (!metricsEl) {
        metricsEl = document.createElement('div');
        metricsEl.className = 'chat-metrics';
        msgEl.appendChild(metricsEl);
      }
      const t = msg.timing;
      // Only rebuild if content changed — cheap to rebuild though, so
      // keep the logic straightforward.
      const parts = [];
      if (t.prompt_tokens || t.completion_tokens) {
        parts.push('<span>' + (t.prompt_tokens||0) + '->' + (t.completion_tokens||0) + ' tok</span>');
      }
      if (t.tokens_per_second) {
        parts.push('<span>' + t.tokens_per_second + ' tok/s</span>');
      }
      if (t.time_to_first_token_ms != null) {
        parts.push('<span>TTFT ' + (t.time_to_first_token_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.generation_time_ms) {
        parts.push('<span>LLM ' + (t.generation_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.tts_time_ms) {
        parts.push('<span>TTS ' + (t.tts_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.total_time_ms) {
        parts.push('<span>Total ' + (t.total_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.emotion) {
        const pct = t.emotion_intensity != null ? ' ' + (t.emotion_intensity * 100).toFixed(0) + '%' : '';
        const p = t.pad_p != null ? (t.pad_p >= 0 ? '+' : '') + t.pad_p.toFixed(2) : '?';
        const a = t.pad_a != null ? (t.pad_a >= 0 ? '+' : '') + t.pad_a.toFixed(2) : '?';
        const d = t.pad_d != null ? (t.pad_d >= 0 ? '+' : '') + t.pad_d.toFixed(2) : '?';
        const lock = t.emotion_locked_h ? ' [locked ' + t.emotion_locked_h.toFixed(1) + 'h]' : '';
        const tip = 'Pleasure:' + p + ' Arousal:' + a + ' Dominance:' + d
          + (t.emotion_locked_h ? ' | Cooldown: ' + t.emotion_locked_h.toFixed(1) + 'h remaining' : '');
        parts.push('<span class="emotion-metric" title="' + escAttr(tip) + '">'
          + '\u26A1 ' + escHtml(t.emotion) + pct + escHtml(lock) + '</span>');
      }
      const newHtml = parts.join('');
      if (metricsEl.innerHTML !== newHtml) {
        metricsEl.innerHTML = newHtml;
      }
    } else if (metricsEl) {
      metricsEl.remove();
    }
  }

  // "Thinking..." placeholder slot
  if (chatWaiting) {
    const thinkEl = el.children[chatHistory.length];
    if (thinkEl) {
      thinkEl.className = 'chat-msg assistant';
      thinkEl.innerHTML = '<div class="msg-label">GLaDOS</div>'
        + '<span class="thinking"><span class="spinner"></span> Thinking...</span>';
    }
  }

  el.scrollTop = el.scrollHeight;
}

async function chatSend() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || chatWaiting) return;

  chatHistory.push({role: 'user', content: text});
  input.value = '';
  chatWaiting = true;
  renderChat();

  const apiHistory = chatHistory.filter(m => m.role === 'user' || m.role === 'assistant')
    .map(m => ({role: m.role, content: m.content}));
  const history = apiHistory.slice(0, -1);

  try {
    await chatSendStreaming(text, history);
  } catch (e) {
    try {
      await chatSendBatch(text, history);
    } catch (e2) {
      chatHistory.push({role: 'assistant', content: 'Error: ' + e2.message});
      chatWaiting = false;
      renderChat();
    }
  }
}

function playAudioQueue(urls, onAllDone) {
  if (!urls || urls.length === 0) { if (onAllDone) onAllDone(); return; }
  let idx = 0;
  function playNext() {
    if (idx >= urls.length) { if (onAllDone) onAllDone(); return; }
    const audio = new Audio(urls[idx]);
    idx++;
    audio.onended = playNext;
    audio.onerror = playNext;
    audio.play().catch(() => playNext());
  }
  playNext();
}

async function chatSendStreaming(text, history) {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message: text, history }),
  });
  if (!resp.ok) throw new Error('Stream endpoint unavailable (' + resp.status + ')');
  if (!resp.body) throw new Error('ReadableStream not supported');

  const streamIdx = chatHistory.length;
  chatHistory.push({role: 'assistant', content: '', audio_url: null, timing: null});
  chatWaiting = false;
  chatStreaming = true;

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let pendingEventType = null;

  while (true) {
    const {done, value} = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, {stream: true});
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) { pendingEventType = null; continue; }

      // Capture named SSE event types
      if (trimmed.startsWith('event: ')) {
        pendingEventType = trimmed.slice(7).trim();
        continue;
      }

      if (!trimmed.startsWith('data: ')) continue;
      if (trimmed === 'data: [DONE]') continue;

      try {
        const chunk = JSON.parse(trimmed.slice(6));

        // Handle named events
        if (pendingEventType === 'timing') {
          chatHistory[streamIdx].timing = chunk;
          renderChat();
          pendingEventType = null;
          continue;
        }
        pendingEventType = null;

        if (chunk.full_text !== undefined) {
          chatStreaming = false;
          renderChat();
          continue;
        }

        if (chunk.audio_url !== undefined) {
          // Streaming audio URL. renderChat() is the single source of
          // DOM truth — on first mount it creates the <audio controls>
          // element, wires .play(), and never destroys it on later
          // content-chunk re-renders. No invisible background player,
          // no handoff, no restart-at-0:00 regression.
          if (chunk.audio_url) {
            chatHistory[streamIdx].audio_url = chunk.audio_url;
            renderChat();
          }
          continue;
        }

        if (chunk.audio_replay_url !== undefined) {
          // Static finalized WAV. renderChat() detects the src change
          // and swaps it in place on the existing element while
          // preserving currentTime + resuming if it was playing.
          chatHistory[streamIdx].audio_url = chunk.audio_replay_url;
          renderChat();
          continue;
        }

        if (chunk.audio_urls !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_urls[0] || null;
          renderChat();
          playAudioQueue(chunk.audio_urls);
          continue;
        }

        const content = chunk.choices?.[0]?.delta?.content;
        if (content) {
          chatHistory[streamIdx].content += content;
          renderChat();
        }
      } catch (e) { /* skip malformed chunks */ }
    }
  }

  // Process remaining buffer
  if (buffer.trim()) {
    for (const line of buffer.split('\n')) {
      const t = line.trim();
      if (!t.startsWith('data: ') || t === 'data: [DONE]') continue;
      try {
        const chunk = JSON.parse(t.slice(6));
        if (chunk.full_text !== undefined) {
          chatStreaming = false;
        } else if (chunk.audio_url !== undefined) {
          if (chunk.audio_url) {
            chatHistory[streamIdx].audio_url = chunk.audio_url;
          }
        } else if (chunk.audio_replay_url !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_replay_url;
        } else if (chunk.audio_urls !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_urls[0] || null;
          playAudioQueue(chunk.audio_urls);
        } else {
          const content = chunk.choices?.[0]?.delta?.content;
          if (content) chatHistory[streamIdx].content += content;
        }
      } catch(e) {}
    }
  }

  chatStreaming = false;
  renderChat();
}

async function chatSendBatch(text, history) {
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message: text, history }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || 'Chat failed');

  chatHistory.push({
    role: 'assistant',
    content: data.text,
    audio_url: data.audio_url || null,
  });

  chatWaiting = false;
  renderChat();

  if (data.audio_url) {
    try { new Audio(data.audio_url).play(); } catch(e) {}
  }
}

/* â”€â”€ Microphone (push-to-talk) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

async function toggleMic() {
  const btn = document.getElementById('micBtn');
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    btn.classList.remove('recording');
    return;
  }

  try {
    if (!micStream) {
      micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    }
    const chunks = [];
    mediaRecorder = new MediaRecorder(micStream, {mimeType: 'audio/webm'});
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      btn.classList.remove('recording');
      const blob = new Blob(chunks, {type: 'audio/webm'});
      if (blob.size < 100) return;

      const input = document.getElementById('chatInput');
      input.value = 'Transcribing...';
      input.disabled = true;

      try {
        const resp = await fetch('/api/stt', {
          method: 'POST',
          headers: {'Content-Type': 'audio/webm'},
          body: blob,
        });
        const data = await resp.json();
        if (data.text && data.text.trim()) {
          input.value = data.text.trim();
          input.disabled = false;
          chatSend();
        } else {
          input.value = '';
          input.disabled = false;
        }
      } catch (e) {
        input.value = '';
        input.disabled = false;
        console.error('STT failed:', e);
      }
    };
    mediaRecorder.start();
    btn.classList.add('recording');
  } catch (e) {
    console.error('Mic access denied:', e);
    btn.style.display = 'none';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 3: System Control
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _modeRefreshInterval = null;
let _healthRefreshInterval = null;

async function loadModes() {
  try {
    const resp = await fetch('/api/modes');
    const data = await resp.json();
    document.getElementById('maintToggle').checked = data.maintenance_mode;
    document.getElementById('silentToggle').checked = data.silent_mode;
    const speakerRow = document.getElementById('speakerRow');
    speakerRow.style.display = data.maintenance_mode ? 'flex' : 'none';
    if (data.maintenance_speaker) {
      const sel = document.getElementById('speakerSelect');
      for (const opt of sel.options) {
        if (opt.value === data.maintenance_speaker) { opt.selected = true; break; }
      }
    }
  } catch (e) { console.error('Failed to load modes:', e); }

  if (!_modeRefreshInterval) {
    _modeRefreshInterval = setInterval(loadModes, 10000);
  }
}

async function loadVerbositySliders() {
  const container = document.getElementById('verbositySliders');
  if (!container) return;
  try {
    const resp = await fetch('/api/announcement-settings');
    const data = await resp.json();
    const scenarios = data.scenarios || {};
    let html = '';
    for (const [key, cfg] of Object.entries(scenarios)) {
      const pct = Math.round((cfg.followup_probability || 0) * 100);
      html += '<div class="mode-row" style="flex-wrap:wrap;gap:4px;">'
        + '<div style="flex:1;min-width:120px;">'
        + '<div class="mode-label">' + cfg.label + '</div>'
        + '</div>'
        + '<div style="display:flex;align-items:center;gap:8px;min-width:200px;">'
        + '<input type="range" min="0" max="100" value="' + pct + '" '
        + 'style="flex:1;accent-color:var(--accent);" '
        + 'oninput="this.nextElementSibling.textContent=this.value+\'%\'" '
        + 'onchange="setVerbosity(\'' + key + '\',this.value)">'
        + '<span style="font-size:0.85rem;min-width:36px;text-align:right;">' + pct + '%</span>'
        + '</div></div>';
    }
    container.innerHTML = html || '<div style="color:var(--fg-secondary);">No announcement scenarios found.</div>';
    container.style.opacity = '1';
  } catch (e) {
    container.innerHTML = '<div style="color:var(--error);">Failed to load announcement settings.</div>';
    container.style.opacity = '1';
    console.error('Failed to load verbosity:', e);
  }
}

async function setVerbosity(scenario, pctValue) {
  try {
    await fetch('/api/announcement-settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scenario, followup_probability: parseInt(pctValue) / 100}),
    });
  } catch (e) { console.error('Failed to set verbosity:', e); }
}

// ── Phase 5.7: Speakers picker (replaces cfgBuildForm rendering
//    for speakers.yaml) ─────────────────────────────────────────
//
// State shape written back to /api/config/speakers:
//   { default: '<entity_id>|null', available: [<entity_id>, ...],
//     blacklist: [...] }  // blacklist preserved verbatim
let _speakersPickerState = null;  // { detected: [...], config: {...} }

async function _cfgLoadSpeakersPicker() {
  const body = document.getElementById('cfg-speakers-body');
  if (!body) return;
  try {
    const [det, cfg] = await Promise.all([
      fetch('/api/speakers').then(r => r.json()),
      fetch('/api/config/speakers').then(r => r.json()),
    ]);
    _speakersPickerState = {
      detected: det.speakers || [],
      config: cfg || { default: null, available: [], blacklist: [] },
    };
    _cfgRenderSpeakersPicker();
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);">Failed to load speakers: '
      + escHtml(String(e)) + '</div>';
    console.error('speakers picker load failed:', e);
  }
}

function _cfgRenderSpeakersPicker() {
  const body = document.getElementById('cfg-speakers-body');
  if (!body || !_speakersPickerState) return;
  const { detected, config } = _speakersPickerState;
  const enabled = new Set(config.available || []);
  // Flat list sorted alphabetically by friendly name.
  const sorted = detected.slice().sort((a, b) => a.name.localeCompare(b.name));
  let html = '';
  for (const sp of sorted) {
    const chk = enabled.has(sp.entity_id) ? ' checked' : '';
    html += ''
      + '<label class="speaker-item">'
      +   '<input type="checkbox" class="speaker-check" '
      +     'data-entity-id="' + escHtml(sp.entity_id) + '"' + chk
      +     ' onchange="_cfgOnSpeakerToggle()">'
      +   '<div class="speaker-item-body">'
      +     '<div class="speaker-name">' + escHtml(sp.name) + '</div>'
      +     '<div class="speaker-entity-id">' + escHtml(sp.entity_id) + '</div>'
      +   '</div>'
      + '</label>';
  }
  // Default-speaker dropdown, scoped to currently-enabled entities.
  html += '<div class="speakers-default-row">';
  html += '<label class="cfg-field-label" for="cfg-speakers-default">Default speaker for Maintenance Mode</label>';
  html += '<div class="trait-desc" style="margin-top:4px;">Maintenance Mode routes all audio to this single speaker. Restricted to speakers you have enabled above.</div>';
  html += '<select id="cfg-speakers-default" style="margin-top:6px;">';
  html += '<option value=""' + (!config.default ? ' selected' : '') + '>&mdash; none &mdash;</option>';
  const enabledList = detected.filter(sp => enabled.has(sp.entity_id));
  for (const sp of enabledList.sort((a,b) => a.name.localeCompare(b.name))) {
    const sel = sp.entity_id === config.default ? ' selected' : '';
    html += '<option value="' + escHtml(sp.entity_id) + '"' + sel + '>'
      + escHtml(sp.name) + '</option>';
  }
  html += '</select>';
  html += '</div>';
  // Save row.
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="_cfgSaveSpeakersPicker()">Save Speakers</button>'
    + '<span id="cfg-save-result-speakers" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
}

function _cfgOnSpeakerToggle() {
  // Recompute enabled set and re-render so the default dropdown
  // stays consistent with checked checkboxes. Preserve a
  // previously-selected default if it's still enabled.
  if (!_speakersPickerState) return;
  const checked = Array.from(document.querySelectorAll('.speaker-check:checked'))
    .map(el => el.getAttribute('data-entity-id'));
  _speakersPickerState.config.available = checked;
  // Clear default if it was unchecked.
  if (_speakersPickerState.config.default && checked.indexOf(_speakersPickerState.config.default) < 0) {
    _speakersPickerState.config.default = null;
  }
  _cfgRenderSpeakersPicker();
}

async function _cfgSaveSpeakersPicker() {
  const resultSpan = document.getElementById('cfg-save-result-speakers');
  if (!_speakersPickerState) {
    if (resultSpan) resultSpan.textContent = 'Not loaded';
    return;
  }
  const checked = Array.from(document.querySelectorAll('.speaker-check:checked'))
    .map(el => el.getAttribute('data-entity-id'));
  const defaultSel = document.getElementById('cfg-speakers-default');
  const nextDefault = (defaultSel && defaultSel.value) ? defaultSel.value : null;
  const next = {
    default: nextDefault,
    available: checked,
    blacklist: _speakersPickerState.config.blacklist || [],
  };
  if (resultSpan) { resultSpan.textContent = 'Saving…'; resultSpan.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/config/speakers', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error('HTTP ' + r.status + ': ' + txt.slice(0, 200));
    }
    _speakersPickerState.config = next;
    if (resultSpan) { resultSpan.textContent = 'Saved'; resultSpan.className = 'cfg-result cfg-result-ok'; }
  } catch (e) {
    if (resultSpan) { resultSpan.textContent = 'Save failed: ' + String(e); resultSpan.className = 'cfg-result cfg-result-err'; }
    console.error('speakers save failed:', e);
  }
}

async function loadStartupSpeakers() {
  const container = document.getElementById('startupSpeakers');
  if (!container) return;
  try {
    const resp = await fetch('/api/startup-speakers');
    const data = await resp.json();
    const speakers = data.speakers || [];
    if (!speakers.length) {
      container.innerHTML = '<div style="color:var(--fg-secondary);">No speakers found in speakers.yaml.</div>';
      container.style.opacity = '1';
      return;
    }
    let html = '';
    for (const sp of speakers) {
      html += '<div class="mode-row">'
        + '<div style="flex:1;">'
        + '<div class="mode-label">' + escHtml(sp.name) + '</div>'
        + '<div class="mode-desc" style="font-size:0.72rem;">' + escHtml(sp.entity_id) + '</div>'
        + '</div>'
        + '<label class="toggle">'
        + '<input type="checkbox" ' + (sp.startup ? 'checked' : '') + ' '
        + 'onchange="saveStartupSpeakers()" data-speaker="' + escAttr(sp.entity_id) + '">'
        + '<span class="toggle-slider"></span>'
        + '</label>'
        + '</div>';
    }
    container.innerHTML = html;
    container.style.opacity = '1';
  } catch (e) {
    container.innerHTML = '<div style="color:var(--error);">Failed to load speakers.</div>';
    container.style.opacity = '1';
    console.error('Failed to load startup speakers:', e);
  }
}

async function saveStartupSpeakers() {
  const status = document.getElementById('startupSpeakersStatus');
  const checkboxes = document.querySelectorAll('[data-speaker]');
  const selected = [];
  checkboxes.forEach(cb => { if (cb.checked) selected.push(cb.dataset.speaker); });
  if (!selected.length) {
    if (status) status.textContent = 'At least one speaker must be selected.';
    // Re-check the last unchecked box
    checkboxes.forEach(cb => { cb.checked = true; });
    return;
  }
  try {
    const resp = await fetch('/api/startup-speakers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({speakers: selected}),
    });
    const data = await resp.json();
    if (status) {
      status.textContent = data.status === 'ok'
        ? 'Saved. Restart glados-api to apply.'
        : (data.note || 'Saved.');
      setTimeout(() => { if (status) status.textContent = ''; }, 4000);
    }
  } catch (e) {
    if (status) status.textContent = 'Failed to save.';
    console.error('Failed to save startup speakers:', e);
  }
}

async function loadSpeakers() {
  const sel = document.getElementById('speakerSelect');
  try {
    const resp = await fetch('/api/speakers');
    const data = await resp.json();
    sel.innerHTML = '<option value="">-- Select speaker --</option>';
    for (const sp of (data.speakers || [])) {
      const opt = document.createElement('option');
      opt.value = sp.entity_id;
      opt.textContent = sp.name + ' (' + sp.area + ')';
      sel.appendChild(opt);
    }
  } catch (e) {
    sel.innerHTML = '<option value="">Error loading speakers</option>';
  }
}

async function toggleMaintenance() {
  const on = document.getElementById('maintToggle').checked;
  const speakerRow = document.getElementById('speakerRow');

  if (on) {
    speakerRow.style.display = 'flex';
    const speaker = document.getElementById('speakerSelect').value;
    if (!speaker) {
      document.getElementById('maintToggle').checked = false;
      document.getElementById('speakerSelect').focus();
      return;
    }
    await fetch('/api/modes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'maintenance_on', speaker}),
    });
  } else {
    speakerRow.style.display = 'none';
    await fetch('/api/modes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'maintenance_off'}),
    });
  }
}

async function toggleSilent() {
  const on = document.getElementById('silentToggle').checked;
  await fetch('/api/modes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: on ? 'silent_on' : 'silent_off'}),
  });
}

async function loadEyeDemo() {
  try {
    const resp = await fetch('/api/eye-demo');
    const data = await resp.json();
    document.getElementById('eyeDemoToggle').checked = data.running;
  } catch (e) { console.error('Failed to load eye demo state:', e); }
}

async function toggleEyeDemo() {
  const toggle = document.getElementById('eyeDemoToggle');
  const action = toggle.checked ? 'start' : 'stop';
  try {
    const resp = await fetch('/api/eye-demo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
    const data = await resp.json();
    if (!data.ok) {
      toggle.checked = !toggle.checked;
      console.error('Eye demo toggle failed:', data);
    }
  } catch (e) {
    toggle.checked = !toggle.checked;
    console.error('Eye demo toggle error:', e);
  }
}

async function loadHealth() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    for (const [key, ok] of Object.entries(data)) {
      const dot = document.getElementById('hd-' + key);
      if (dot) {
        dot.className = 'health-dot ' + (ok ? 'ok' : 'err');
      }
    }
  } catch (e) {
    document.querySelectorAll('.health-dot').forEach(d => d.className = 'health-dot unknown');
  }

  if (!_healthRefreshInterval) {
    _healthRefreshInterval = setInterval(loadHealth, 30000);
  }
}

async function restartService(key) {
  const btn = document.querySelector('.health-item #hd-' + key)?.parentElement?.querySelector('.restart-btn');
  if (btn) {
    btn.classList.add('restarting');
    btn.disabled = true;
  }
  const dot = document.getElementById('hd-' + key);
  if (dot) dot.className = 'health-dot unknown';
  try {
    const resp = await fetch('/api/restart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service: key}),
    });
    const data = await resp.json();
    if (data.ok) {
      setTimeout(loadHealth, 3000);
    } else {
      if (dot) dot.className = 'health-dot err';
      alert('Restart failed: ' + (data.stderr || data.error || 'unknown error'));
    }
  } catch (e) {
    if (dot) dot.className = 'health-dot err';
    alert('Restart request failed: ' + e.message);
  } finally {
    if (btn) {
      btn.classList.remove('restarting');
      btn.disabled = false;
    }
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Weather & GPU monitoring
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadWeather() {
  const panel = document.getElementById('weatherPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/weather');
    const data = await resp.json();
    if (data.error) {
      panel.innerHTML = '<div style="color:var(--fg-secondary)">' + escHtml(data.error) + '</div>';
      return;
    }
    const c = data.current || {};
    const t = data.today || {};
    const units = data.units || {};
    let html = '<div class="weather-grid">';
    html += '<div class="weather-item"><div class="weather-label">Temperature</div>'
      + '<div class="weather-value highlight">' + (c.temperature ?? '?') + (units.temperature || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Condition</div>'
      + '<div class="weather-value">' + escHtml(c.condition || '?') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Wind</div>'
      + '<div class="weather-value">' + (c.wind_speed ?? '?') + ' ' + (units.wind_speed || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Humidity</div>'
      + '<div class="weather-value">' + (c.humidity ?? '?') + '%</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Today High / Low</div>'
      + '<div class="weather-value">' + (t.high ?? '?') + (units.temperature || '') + ' / ' + (t.low ?? '?') + (units.temperature || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Today</div>'
      + '<div class="weather-value">' + escHtml(t.condition || '?') + '</div></div>';
    html += '</div>';
    if (data._cache_age_s != null) {
      const mins = Math.round(data._cache_age_s / 60);
      html += '<div style="font-size:0.7rem;color:#666;margin-top:6px;">Cache age: ' + mins + 'm</div>';
    }
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load weather</div>';
  }
}

async function loadGPU() {
  const panel = document.getElementById('gpuPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/gpu');
    const data = await resp.json();
    if (data.error) {
      panel.innerHTML = '<div style="color:var(--fg-secondary)">' + escHtml(data.error) + '</div>';
      return;
    }
    const gpus = data.gpus || [];
    if (!gpus.length) {
      panel.innerHTML = '<div style="color:var(--fg-secondary)">No GPUs detected</div>';
      return;
    }
    let html = '';
    for (const g of gpus) {
      const memPct = Math.round(g.memory_used_mb / g.memory_total_mb * 100);
      const barClass = memPct > 90 ? 'crit' : memPct > 70 ? 'hot' : 'mem';
      html += '<div class="gpu-card">'
        + '<div class="gpu-name">GPU ' + g.index + ': ' + escHtml(g.name) + '</div>'
        + '<div class="gpu-stat">'
        + '<span>VRAM: ' + g.memory_used_mb + ' / ' + g.memory_total_mb + ' MB (' + memPct + '%)</span>'
        + '<span>' + (g.temperature_c != null ? g.temperature_c + '\u00B0C' : '') + '</span>'
        + '</div>'
        + '<div class="gpu-bar-bg"><div class="gpu-bar-fill ' + barClass + '" style="width:' + memPct + '%"></div></div>';
      if (g.note) {
        html += '<div style="font-size:0.7rem;color:#888;margin-top:2px;">' + escHtml(g.note) + '</div>';
      }
      html += '</div>';
    }
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load GPU data</div>';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   GPU auto-refresh (15s), Weather auto-refresh (5min)
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _gpuRefreshInterval = null;
let _weatherRefreshInterval = null;

function startGPUAutoRefresh() {
  if (!_gpuRefreshInterval) {
    _gpuRefreshInterval = setInterval(loadGPU, 15000);
  }
}

function startWeatherAutoRefresh() {
  if (!_weatherRefreshInterval) {
    _weatherRefreshInterval = setInterval(loadWeather, 300000);
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Weather manual refresh
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function refreshWeather() {
  const btn = document.getElementById('weatherRefreshBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Refreshing...';
  try {
    const resp = await fetch('/api/weather/refresh', {method: 'POST'});
    if (resp.ok) {
      await loadWeather();
      showToast('Weather refreshed', 'success');
    } else {
      const data = await resp.json().catch(() => ({}));
      showToast('Refresh failed: ' + (data.error || resp.status), 'error');
    }
  } catch(e) {
    showToast('Refresh failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Service Logs viewer
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadLogs() {
  const service = document.getElementById('logServiceSelect').value;
  const panel = document.getElementById('logPanel');
  const info = document.getElementById('logSizeInfo');
  if (!panel) return;
  panel.textContent = 'Loading...';
  try {
    const resp = await fetch('/api/logs?service=' + encodeURIComponent(service) + '&lines=500');
    const data = await resp.json();
    panel.textContent = (data.lines || []).join('\n') || '(empty)';
    panel.scrollTop = panel.scrollHeight;
    if (info && data.total_size != null) {
      info.textContent = 'Log size: ' + fmtSize(data.total_size);
    }
  } catch(e) {
    panel.textContent = 'Failed to load logs: ' + e.message;
  }
}

async function clearLog() {
  const service = document.getElementById('logServiceSelect').value;
  if (!confirm('Clear ' + service + ' log file?')) return;
  try {
    const resp = await fetch('/api/logs/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service: service})
    });
    if (resp.ok) {
      showToast('Log cleared', 'success');
      loadLogs();
    } else {
      showToast('Clear failed', 'error');
    }
  } catch(e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Audio Storage stats and cleanup
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadAudioStats() {
  const panel = document.getElementById('audioStatsPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/audio/stats');
    const data = await resp.json();
    const labels = {
      ha_output: 'HA Playback',
      archive: 'Archive',
      tts_ui: 'TTS Generator',
      chat_audio: 'Chat Audio',
    };
    let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">';
    for (const [key, stats] of Object.entries(data)) {
      html += '<div style="background:var(--bg-input);padding:10px;border-radius:6px;">'
        + '<div style="font-weight:500;margin-bottom:4px;">' + escHtml(labels[key] || key) + '</div>'
        + '<div style="font-size:0.78rem;color:var(--fg-secondary);">' + stats.count + ' files (' + fmtSize(stats.size_bytes) + ')</div>'
        + '<button class="btn-small" style="margin-top:6px;font-size:0.72rem;padding:3px 8px;" onclick="clearAudioDir(\'' + key + '\')">Clear</button>'
        + '</div>';
    }
    html += '</div>';
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load audio stats</div>';
  }
}

async function clearAudioDir(key) {
  if (!confirm('Clear all audio files in this directory?')) return;
  try {
    const resp = await fetch('/api/audio/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({directory: key})
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast('Cleared ' + data.deleted + ' files', 'success');
      loadAudioStats();
    } else {
      showToast('Clear failed: ' + (data.error || 'unknown'), 'error');
    }
  } catch(e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Robot Nodes management
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _robotRefreshInterval = null;

async function loadRobots() {
  const card = document.getElementById('robotNodesCard');
  if (!card) return;
  try {
    const resp = await fetch('/api/robots/status');
    const data = await resp.json();
    if (!data.enabled) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    const list = document.getElementById('robotNodesList');
    const nodes = data.nodes || {};
    const nodeIds = Object.keys(nodes);
    if (nodeIds.length === 0) {
      list.innerHTML = '<div style="color:var(--fg-secondary);">No nodes configured. Add one below.</div>';
    } else {
      let html = '<div class="health-grid">';
      for (const [nid, n] of Object.entries(nodes)) {
        const dotClass = !n.enabled ? 'unknown' : (n.reachable ? 'ok' : 'err');
        const label = n.name || nid;
        const uptimeStr = n.reachable && n.uptime_s > 0 ? ' (' + fmtUptime(n.uptime_s) + ')' : '';
        html += '<div class="health-item" style="justify-content:space-between;">'
          + '<span><span class="health-dot ' + dotClass + '"></span>' + escHtml(label) + uptimeStr + '</span>'
          + '<span style="display:flex;gap:4px;align-items:center;">'
          + '<button class="restart-btn" onclick="robotIdentify(\'' + escHtml(nid) + '\')" title="Identify (flash LED)">&#128161;</button>'
          + '<label class="toggle" style="transform:scale(0.75);margin:0;"><input type="checkbox" ' + (n.enabled ? 'checked' : '') + ' onchange="robotToggle(\'' + escHtml(nid) + '\', this.checked)"><span class="toggle-slider"></span></label>'
          + '<button class="restart-btn" onclick="robotRemove(\'' + escHtml(nid) + '\')" title="Remove node" style="color:#e74c3c;">&#10005;</button>'
          + '</span></div>';
      }
      html += '</div>';
      list.innerHTML = html;
    }

    // Bots section
    const bots = data.bots || {};
    const botIds = Object.keys(bots);
    const botsSection = document.getElementById('robotBotsSection');
    if (botIds.length > 0) {
      botsSection.style.display = '';
      let bhtml = '';
      for (const [bid, b] of Object.entries(bots)) {
        const bLabel = b.name || bid;
        bhtml += '<div style="background:var(--bg-input);padding:8px 10px;border-radius:4px;margin-bottom:4px;">'
          + '<strong>' + escHtml(bLabel) + '</strong> <span style="color:var(--fg-secondary);">(' + escHtml(b.profile) + ')</span>';
        for (const [role, rn] of Object.entries(b.nodes || {})) {
          const rdot = rn.reachable ? '&#9679;' : '&#9675;';
          bhtml += ' <span style="margin-left:8px;">' + rdot + ' ' + escHtml(role) + ': ' + escHtml(rn.node_id) + '</span>';
        }
        bhtml += '</div>';
      }
      document.getElementById('robotBotsList').innerHTML = bhtml;
    } else {
      botsSection.style.display = 'none';
    }
  } catch(e) {
    console.error('Failed to load robots:', e);
  }
}

function fmtUptime(s) {
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

function startRobotAutoRefresh() {
  if (!_robotRefreshInterval) {
    _robotRefreshInterval = setInterval(loadRobots, 15000);
  }
}

async function robotAddNode() {
  const url = document.getElementById('robotNodeUrl').value.trim();
  if (!url) { showToast('Enter a node URL', 'error'); return; }
  const name = document.getElementById('robotNodeName').value.trim();
  try {
    const resp = await fetch('/api/robots/node/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, name})
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('robotNodeUrl').value = '';
      document.getElementById('robotNodeName').value = '';
      showToast('Node added: ' + data.node_id, 'success');
      loadRobots();
    } else {
      showToast(data.error || 'Failed to add node', 'error');
    }
  } catch(e) {
    showToast('Add node failed: ' + e.message, 'error');
  }
}

async function robotRemove(nodeId) {
  if (!confirm('Remove robot node "' + nodeId + '"?')) return;
  try {
    const resp = await fetch('/api/robots/node/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId})
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Node removed', 'success');
      loadRobots();
    } else {
      showToast('Remove failed', 'error');
    }
  } catch(e) {
    showToast('Remove failed: ' + e.message, 'error');
  }
}

async function robotToggle(nodeId, enabled) {
  try {
    await fetch('/api/robots/node/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId, enabled})
    });
    setTimeout(loadRobots, 1000);
  } catch(e) {
    showToast('Toggle failed: ' + e.message, 'error');
  }
}

async function robotIdentify(nodeId) {
  try {
    const resp = await fetch('/api/robots/node/identify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId})
    });
    const data = await resp.json();
    if (data.ok) showToast('LED identify sent', 'success');
    else showToast('Identify failed â€” node unreachable?', 'error');
  } catch(e) {
    showToast('Identify failed: ' + e.message, 'error');
  }
}

async function robotEmergencyStop() {
  try {
    const resp = await fetch('/api/robots/emergency-stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const data = await resp.json();
    if (data.ok) showToast('EMERGENCY STOP sent to all nodes', 'success');
    else showToast('Emergency stop failed', 'error');
    loadRobots();
  } catch(e) {
    showToast('Emergency stop failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Training Monitor
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
let _trainChart = null;
let _trainLastStep = 0;
let _trainRefreshInterval = null;
let _trainLogInterval = null;

function initTrainingTab() {
  // Auth overlay
  const overlay = document.getElementById('trainingAuthOverlay');
  if (overlay) overlay.style.display = _isAuthenticated ? 'none' : 'flex';
  const lock = document.getElementById('lockTraining');
  if (lock) lock.textContent = _isAuthenticated ? '' : '\u{1F512}';

  if (!_isAuthenticated) return;

  loadTrainingStatus();
  loadTrainingMetrics(true);
  loadTrainingLog();

  // Start polling
  if (_trainRefreshInterval) clearInterval(_trainRefreshInterval);
  _trainRefreshInterval = setInterval(() => {
    if (document.getElementById('tab-training').classList.contains('active')) {
      loadTrainingStatus();
      loadTrainingMetrics(false);
    }
  }, 5000);

  if (_trainLogInterval) clearInterval(_trainLogInterval);
  _trainLogInterval = setInterval(() => {
    if (document.getElementById('tab-training').classList.contains('active')) {
      loadTrainingLog();
    }
  }, 10000);
}

async function loadTrainingStatus() {
  try {
    const r = await fetch('/api/training/status');
    const d = await r.json();

    const dot = document.getElementById('trainRunning');
    const txt = document.getElementById('trainRunningText');
    if (d.running) {
      dot.className = 'train-dot train-dot-on';
      txt.textContent = 'Training';
    } else {
      dot.className = 'train-dot train-dot-off';
      txt.textContent = 'Stopped';
    }

    document.getElementById('trainEpoch').textContent = d.ft_epoch != null ? d.ft_epoch + ' / ' + (d.max_epochs - d.base_epoch) : '--';
    document.getElementById('trainGenLoss').textContent = d.gen_loss != null ? (d.gen_loss > 1e6 ? d.gen_loss.toExponential(1) : d.gen_loss.toFixed(1)) : '--';
    document.getElementById('trainDiscLoss').textContent = d.disc_loss != null ? d.disc_loss.toFixed(3) : '--';

    // Snapshot status
    const ss = document.getElementById('snapshotStatus');
    if (d.snapshot) {
      ss.textContent = d.snapshot.message || '';
      if (d.snapshot.state === 'running') {
        ss.className = 'snap-running';
        document.getElementById('btnSnapshot').disabled = true;
      } else {
        ss.className = '';
        document.getElementById('btnSnapshot').disabled = false;
      }
    }
  } catch(e) {}
}

async function loadTrainingMetrics(fullLoad) {
  try {
    const since = fullLoad ? 0 : _trainLastStep;
    const r = await fetch('/api/training/metrics?since_step=' + since);
    const d = await r.json();
    if (!d.metrics || d.metrics.length === 0) return;

    if (fullLoad || !_trainChart) {
      _trainLastStep = 0;
      initTrainingChart(d.metrics);
    } else {
      appendTrainingChart(d.metrics);
    }

    _trainLastStep = d.metrics[d.metrics.length - 1].step;
  } catch(e) {}
}

function initTrainingChart(data) {
  const ctx = document.getElementById('trainingChart');
  if (_trainChart) _trainChart.destroy();

  const labels = data.map(m => m.ft_epoch);
  const genData = data.map(m => m.gen_loss);
  const discData = data.map(m => m.disc_loss);

  _trainChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Generator Loss',
          data: genData,
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245,158,11,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          yAxisID: 'yGen',
          tension: 0.2,
        },
        {
          label: 'Discriminator Loss',
          data: discData,
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59,130,246,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          yAxisID: 'yDisc',
          tension: 0.2,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#999' } },
        tooltip: { backgroundColor: '#1e1e1e', titleColor: '#fff', bodyColor: '#ccc' }
      },
      scales: {
        x: {
          title: { display: true, text: 'Fine-Tune Epoch', color: '#666' },
          ticks: { color: '#666', maxTicksLimit: 20 },
          grid: { color: '#222' }
        },
        yGen: {
          type: 'logarithmic',
          position: 'left',
          title: { display: true, text: 'Gen Loss (log)', color: '#f59e0b' },
          ticks: { color: '#f59e0b' },
          grid: { color: '#222' }
        },
        yDisc: {
          type: 'linear',
          position: 'right',
          title: { display: true, text: 'Disc Loss', color: '#3b82f6' },
          ticks: { color: '#3b82f6' },
          grid: { drawOnChartArea: false }
        }
      }
    }
  });
}

function appendTrainingChart(data) {
  if (!_trainChart || !data.length) return;
  for (const m of data) {
    _trainChart.data.labels.push(m.ft_epoch);
    _trainChart.data.datasets[0].data.push(m.gen_loss);
    _trainChart.data.datasets[1].data.push(m.disc_loss);
  }
  _trainChart.update('none');
}

async function loadTrainingLog() {
  try {
    const r = await fetch('/api/training/log?lines=100');
    const d = await r.json();
    const el = document.getElementById('trainingLog');
    if (d.lines && d.lines.length > 0) {
      el.textContent = d.lines.join('\n');
      el.scrollTop = el.scrollHeight;
    } else {
      el.textContent = 'No training log available.';
    }
  } catch(e) {}
}

async function trainingSnapshot() {
  if (!confirm('Snapshot the current checkpoint and deploy to GLaDOS TTS?\n\nThis will export the model to ONNX and restart the TTS service.')) return;
  try {
    const r = await fetch('/api/training/snapshot', {method:'POST'});
    const d = await r.json();
    if (d.ok) showToast('Snapshot started...', 'success');
    else showToast(d.error || 'Snapshot failed', 'error');
  } catch(e) {
    showToast('Snapshot request failed', 'error');
  }
}

async function trainingStop() {
  if (!confirm('Stop the training process?\n\nThis will kill the running piper_train process. You will need to restart training manually.')) return;
  try {
    const r = await fetch('/api/training/stop', {method:'POST'});
    const d = await r.json();
    if (d.ok) showToast(d.message, 'success');
    else showToast(d.error || 'Stop failed', 'error');
  } catch(e) {
    showToast('Stop request failed', 'error');
  }
}

