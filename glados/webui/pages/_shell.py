"""HTML chrome for the GLaDOS WebUI: head, sidebar, main open/close.

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). Composed with per-page HTML constants
(pages/chat.py, pages/system.py, etc.) in glados.webui.tts_ui to
form the full HTML_PAGE constant.
"""

SHELL_TOP = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GLaDOS Control Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Major+Mono+Display&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<!-- â”€â”€ Sidebar (desktop) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ -->
<nav class="sidebar">
  <div class="sidebar-brand">
    <span class="engine-status-dot" id="engineStatusDot" title="Engine status"></span>
    <span>GLaDOS</span>
    <span>Control</span>
  </div>
  <div class="nav-items">
    <a class="nav-item" data-nav-key="chat" onclick="navigateTo('chat')" data-requires-auth="true" style="display:none;">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      Chat
    </a>
    <a class="nav-item" data-nav-key="tts" onclick="navigateTo('tts')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
      TTS Generator
    </a>
    <a class="nav-item nav-parent" data-nav-key="config" onclick="navToggleConfig()" data-requires-admin="true" style="display:none;">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
      Configuration
      <span class="nav-caret">&#9656;</span>
    </a>
    <div class="nav-children" data-requires-admin="true" style="display:none;">
      <a class="nav-item" data-nav-key="config.system" onclick="navigateTo('config.system')">System</a>
      <a class="nav-item" data-nav-key="config.integrations" onclick="navigateTo('config.integrations')">Integrations</a>
      <a class="nav-item" data-nav-key="config.audio-speakers" onclick="navigateTo('config.audio-speakers')">Audio &amp; Speakers</a>
      <a class="nav-item" data-nav-key="config.personality" onclick="navigateTo('config.personality')">Personality</a>
      <a class="nav-item" data-nav-key="config.memory" onclick="navigateTo('config.memory')">Memory</a>
      <a class="nav-item" data-nav-key="config.logs" onclick="navigateTo('config.logs')">Logs</a>
      <a class="nav-item" data-nav-key="config.ssl" onclick="navigateTo('config.ssl')">SSL</a>
      <a class="nav-item" data-nav-key="config.raw" onclick="navigateTo('config.raw')">Raw YAML</a>
      <a class="nav-item" data-nav-key="config.users" onclick="navigateTo('config.users')">Users</a>
    </div>
    <!-- Training removed: piper_train is a host-native tool, not available in container -->
  </div>
  <div class="sidebar-footer">
    <!-- Account block — populated by updateAuthUI() in ui.js -->
    <div id="sidebarAccount" class="sidebar-account" style="display:none;">
      <div class="sidebar-account-user">
        <span class="sidebar-account-icon">&#128100;</span>
        <span id="sidebarUsername" class="sidebar-account-name"></span>
        <span id="sidebarRole" class="sidebar-account-role"></span>
      </div>
      <a href="#" onclick="navigateTo('config.system');showPageTab('system','account');return false;" class="sidebar-account-link">Change Password</a>
    </div>
    <div id="sidebarSignIn" class="sidebar-signin">
      <a href="/login" class="sidebar-signin-btn">Sign in</a>
    </div>
    <a id="sidebarLogout" href="/logout" class="sidebar-logout" style="display:none;">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      Sign out
    </a>
  </div>
</nav>

<!-- â”€â”€ Top bar (mobile) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ -->
<div class="topbar">
  <div class="topbar-inner">
    <span class="topbar-brand">GLaDOS</span>
    <a class="nav-item" data-nav-key="chat" onclick="navigateTo('chat')">Chat</a>
    <a class="nav-item" data-nav-key="tts" onclick="navigateTo('tts')">TTS</a>
    <a class="nav-item" data-nav-key="config.system" onclick="navigateTo('config.system')" data-requires-auth="true">System</a>
    <a class="nav-item" data-nav-key="config.integrations" onclick="navigateTo('config.integrations')" data-requires-auth="true">Config</a>
    <!-- Training removed: not available in container -->
  </div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- MAIN CONTENT                                                   -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<main class="main-content">

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 1: TTS Generator                                           -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
"""

SHELL_BOTTOM = r"""
</main>

<!-- Toast -->
<div id="toastStack" class="toast-stack"></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="/static/ui.js"></script>
</body>
</html>
"""
