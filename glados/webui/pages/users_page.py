"""HTML for the Users management tab (id="tab-config-users").

Admin-only page that exposes the /api/users CRUD endpoints as a
table UI with Add, Edit, Reset Password, Disable, and Delete actions.

Phase 6 / Approach 2 sweep (2026-05-09): inline-style attributes
replaced with v3 utility classes; the missing modal-chrome classes
(.modal-backdrop, .form-label, .users-table) added to style.css so
the modals render with proper backdrop dimming, centered box, and
form-label column layout instead of falling through to browser
defaults. This closes prior-audit finding F6.
"""

HTML = r"""
<!-- ================================================================ -->
<!-- CONFIGURATION > USERS (Task 7b)                                  -->
<!-- ================================================================ -->
<div id="tab-config-users" class="tab-content">
<div class="page-shell">
<div class="container" style="position:relative;">
  <div id="usersAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Administrator access required to manage users</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="page-header">
    <div>
      <h2 class="page-title">Users</h2>
      <div class="page-title-desc">Manage WebUI accounts, roles, and access.</div>
    </div>
    <button class="btn-small" onclick="usersShowAddModal()" style="align-self:center;">+ Add User</button>
  </div>

  <div class="card">
    <div id="usersErrorBanner" class="banner-error mb-3" style="display:none;"></div>
    <div id="usersTableWrap">
      <table class="users-table">
        <thead>
          <tr>
            <th>Username</th>
            <th>Display Name</th>
            <th>Role</th>
            <th>Status</th>
            <th>Last Login</th>
            <th class="actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="usersTableBody">
          <tr><td colspan="6" class="txt-dim">Loading&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>
</div>
</div>

<!-- ── Add User Modal ─────────────────────────────────────────────── -->
<div id="usersAddModal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)usersCloseAddModal()">
  <div class="modal-box" style="max-width:420px;">
    <div class="modal-header">
      <span class="modal-title">Add User</span>
      <button class="modal-close" onclick="usersCloseAddModal()">&#10005;</button>
    </div>
    <div id="usersAddError" class="banner-error mb-2" style="display:none;"></div>
    <div class="col gap-3">
      <label class="form-label">Username <span class="txt-danger">*</span>
        <input id="addUsername" type="text" autocomplete="off" placeholder="alice">
      </label>
      <label class="form-label">Display Name
        <input id="addDisplayName" type="text" autocomplete="off" placeholder="Alice (optional)">
      </label>
      <label class="form-label">Role
        <select id="addRole">
          <option value="chat" selected>chat</option>
          <option value="admin">admin</option>
        </select>
      </label>
      <label class="form-label">Password <span class="txt-danger">*</span>
        <input id="addPassword" type="password" autocomplete="new-password" placeholder="New password">
      </label>
    </div>
    <div class="row end gap-2 mt-4">
      <button class="btn-cancel" onclick="usersCloseAddModal()">Cancel</button>
      <button class="btn-small" onclick="usersSubmitAdd()">Create</button>
    </div>
  </div>
</div>

<!-- ── Edit User Modal ────────────────────────────────────────────── -->
<div id="usersEditModal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)usersCloseEditModal()">
  <div class="modal-box" style="max-width:420px;">
    <div class="modal-header">
      <span class="modal-title">Edit User: <span id="editModalUsername"></span></span>
      <button class="modal-close" onclick="usersCloseEditModal()">&#10005;</button>
    </div>
    <div id="usersEditError" class="banner-error mb-2" style="display:none;"></div>
    <div class="col gap-3">
      <label class="form-label">Display Name
        <input id="editDisplayName" type="text" autocomplete="off">
      </label>
      <label class="form-label">Role
        <select id="editRole">
          <option value="chat">chat</option>
          <option value="admin">admin</option>
        </select>
      </label>
      <label class="form-label row-inline">
        <input id="editDisabled" type="checkbox">
        Disabled (blocks login)
      </label>
    </div>
    <div class="row end gap-2 mt-4">
      <button class="btn-cancel" onclick="usersCloseEditModal()">Cancel</button>
      <button class="btn-small" onclick="usersSubmitEdit()">Save</button>
    </div>
  </div>
</div>

<!-- ── Reset Password Modal ───────────────────────────────────────── -->
<div id="usersResetModal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)usersCloseResetModal()">
  <div class="modal-box" style="max-width:380px;">
    <div class="modal-header">
      <span class="modal-title">Reset Password: <span id="resetModalUsername"></span></span>
      <button class="modal-close" onclick="usersCloseResetModal()">&#10005;</button>
    </div>
    <div id="usersResetError" class="banner-error mb-2" style="display:none;"></div>
    <div class="col gap-3">
      <label class="form-label">New Password <span class="txt-danger">*</span>
        <input id="resetPassword" type="password" autocomplete="new-password" placeholder="New password">
      </label>
    </div>
    <div class="row end gap-2 mt-4">
      <button class="btn-cancel" onclick="usersCloseResetModal()">Cancel</button>
      <button class="btn-small" onclick="usersSubmitReset()">Reset</button>
    </div>
  </div>
</div>

<script>
/* ── Users page state ─────────────────────────────────────────────── */
let _usersData = [];
let _usersEditTarget = null;
let _usersResetTarget = null;

/* ── Icon SVGs ─────────────────────────────────────────────────────
   _PENCIL_SVG and _TRASH_SVG are declared in /static/ui.js (shared
   across Memory + Users + future pages). Both inline-script blocks
   and the external script share the same global window scope, so
   re-declaring `const` here would fire a parse SyntaxError that
   aborts the entire SPA. _DISABLE_SVG is the one icon only Users
   uses; it lives in ui.js too — see _DISABLE_SVG declaration
   alongside the other shared icons. */

/* ── Load & render ────────────────────────────────────────────────── */
async function usersLoadAll() {
  const body = document.getElementById('usersTableBody');
  if (!body) return;
  try {
    const r = await fetch('/api/users');
    if (r.status === 401 || r.status === 403) {
      const ov = document.getElementById('usersAuthOverlay');
      if (ov) ov.style.display = 'flex';
      return;
    }
    if (!r.ok) {
      _usersShowError('Failed to load users: HTTP ' + r.status);
      return;
    }
    const data = await r.json();
    _usersData = data.users || [];
    // Hide auth overlay on success (may have been shown on a previous 403)
    const ov2 = document.getElementById('usersAuthOverlay');
    if (ov2) ov2.style.display = 'none';
    _usersRenderTable();
  } catch(e) {
    _usersShowError('Failed to load users: ' + e.message);
  }
}

function _usersRenderTable() {
  const body = document.getElementById('usersTableBody');
  if (!body) return;
  if (!_usersData.length) {
    body.innerHTML = '<tr><td colspan="6" class="txt-dim">No users found.</td></tr>';
    return;
  }
  body.innerHTML = _usersData.map(u => {
    const status = u.disabled ? '<span class="txt-danger">disabled</span>' : '<span class="txt-ok">active</span>';
    const lastLogin = u.last_login_at ? new Date(u.last_login_at * 1000).toLocaleString() : '—';
    const dn = escHtml(u.display_name || u.username);
    const un = escHtml(u.username);
    return `<tr>
      <td class="username-cell">${un}</td>
      <td class="display-name-cell">${dn}</td>
      <td>${escHtml(u.role)}</td>
      <td>${status}</td>
      <td class="last-login-cell">${escHtml(lastLogin)}</td>
      <td class="actions-col">
        <button class="ico-btn" title="Edit" onclick="usersShowEditModal(${escAttr(JSON.stringify(u.username))})">${_PENCIL_SVG}</button>
        <button class="ico-btn ${u.disabled?'danger':''}" title="${u.disabled?'Enable':'Disable'}" onclick="usersConfirmDisable(${escAttr(JSON.stringify(u.username))},${u.disabled?'false':'true'})">${_DISABLE_SVG}</button>
        <button class="ico-btn danger" title="Delete" onclick="usersConfirmDelete(${escAttr(JSON.stringify(u.username))})">${_TRASH_SVG}</button>
      </td>
    </tr>`;
  }).join('');
}

/* ── Error helpers ────────────────────────────────────────────────── */
function _usersShowError(msg) {
  const el = document.getElementById('usersErrorBanner');
  if (!el) return;
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
}
function _usersModalError(elId, msg) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
}

/* ── Add User modal ───────────────────────────────────────────────── */
function usersShowAddModal() {
  document.getElementById('addUsername').value = '';
  document.getElementById('addDisplayName').value = '';
  document.getElementById('addRole').value = 'chat';
  document.getElementById('addPassword').value = '';
  _usersModalError('usersAddError', '');
  document.getElementById('usersAddModal').style.display = 'flex';
  setTimeout(() => document.getElementById('addUsername').focus(), 50);
}
function usersCloseAddModal() {
  document.getElementById('usersAddModal').style.display = 'none';
}
async function usersSubmitAdd() {
  const username = document.getElementById('addUsername').value.trim();
  const display_name = document.getElementById('addDisplayName').value.trim();
  const role = document.getElementById('addRole').value;
  const password = document.getElementById('addPassword').value;
  _usersModalError('usersAddError', '');
  if (!username) { _usersModalError('usersAddError', 'Username is required.'); return; }
  if (!password) { _usersModalError('usersAddError', 'Password is required.'); return; }
  try {
    const r = await fetch('/api/users', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, display_name, role, password}),
    });
    const data = await r.json();
    if (!r.ok) { _usersModalError('usersAddError', data.error || 'Failed to create user.'); return; }
    usersCloseAddModal();
    showToast('User created: ' + username, 'success');
    usersLoadAll();
  } catch(e) {
    _usersModalError('usersAddError', 'Request failed: ' + e.message);
  }
}

/* ── Edit User modal ──────────────────────────────────────────────── */
function usersShowEditModal(username) {
  const u = _usersData.find(x => x.username === username);
  if (!u) return;
  _usersEditTarget = username;
  document.getElementById('editModalUsername').textContent = username;
  document.getElementById('editDisplayName').value = u.display_name || '';
  document.getElementById('editRole').value = u.role;
  document.getElementById('editDisabled').checked = !!u.disabled;
  _usersModalError('usersEditError', '');
  document.getElementById('usersEditModal').style.display = 'flex';
}
function usersCloseEditModal() {
  document.getElementById('usersEditModal').style.display = 'none';
  _usersEditTarget = null;
}
async function usersSubmitEdit() {
  if (!_usersEditTarget) return;
  const display_name = document.getElementById('editDisplayName').value.trim();
  const role = document.getElementById('editRole').value;
  const disabled = document.getElementById('editDisabled').checked;
  _usersModalError('usersEditError', '');
  if (disabled) {
    const orig = _usersData.find(x => x.username === _usersEditTarget);
    if (orig && !orig.disabled) {
      if (!confirm('Disabling ' + _usersEditTarget + ' will immediately revoke their access. Continue?')) return;
    }
  }
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(_usersEditTarget), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({display_name, role, disabled}),
    });
    const data = await r.json();
    if (!r.ok) { _usersModalError('usersEditError', data.error || 'Failed to update user.'); return; }
    usersCloseEditModal();
    showToast('User updated: ' + _usersEditTarget, 'success');
    usersLoadAll();
  } catch(e) {
    _usersModalError('usersEditError', 'Request failed: ' + e.message);
  }
}

/* ── Reset Password modal ─────────────────────────────────────────── */
function usersShowResetModal(username) {
  _usersResetTarget = username;
  document.getElementById('resetModalUsername').textContent = username;
  document.getElementById('resetPassword').value = '';
  _usersModalError('usersResetError', '');
  document.getElementById('usersResetModal').style.display = 'flex';
  setTimeout(() => document.getElementById('resetPassword').focus(), 50);
}
function usersCloseResetModal() {
  document.getElementById('usersResetModal').style.display = 'none';
  _usersResetTarget = null;
}
async function usersSubmitReset() {
  if (!_usersResetTarget) return;
  const password = document.getElementById('resetPassword').value;
  _usersModalError('usersResetError', '');
  if (!password) { _usersModalError('usersResetError', 'Password is required.'); return; }
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(_usersResetTarget) + '/password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
    });
    const data = await r.json();
    if (!r.ok) { _usersModalError('usersResetError', data.error || 'Failed to reset password.'); return; }
    usersCloseResetModal();
    showToast('Password reset for: ' + _usersResetTarget, 'success');
  } catch(e) {
    _usersModalError('usersResetError', 'Request failed: ' + e.message);
  }
}

/* ── Disable / Enable ─────────────────────────────────────────────── */
async function usersConfirmDisable(username, newDisabled) {
  const action = newDisabled ? 'disable' : 'enable';
  if (newDisabled && !confirm('Disable ' + username + '? This will immediately revoke their access.')) return;
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(username), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({disabled: newDisabled}),
    });
    const data = await r.json();
    if (!r.ok) { _usersShowError(data.error || 'Failed to ' + action + ' user.'); return; }
    showToast('User ' + action + 'd: ' + username, 'success');
    usersLoadAll();
  } catch(e) {
    _usersShowError('Request failed: ' + e.message);
  }
}

/* ── Delete ───────────────────────────────────────────────────────── */
async function usersConfirmDelete(username) {
  if (!confirm('Delete user "' + username + '"? This cannot be undone.')) return;
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(username), {method: 'DELETE'});
    const data = await r.json();
    if (!r.ok) { _usersShowError(data.error || 'Failed to delete user.'); return; }
    showToast('User deleted: ' + username, 'success');
    usersLoadAll();
  } catch(e) {
    _usersShowError('Request failed: ' + e.message);
  }
}

/* ── Admin-gated items are revealed by updateAuthUI() in ui.js ─────── */
/* (The IIFE that used to call /api/auth/status here was removed in     */
/*  favour of the global checkAuth() → updateAuthUI() flow in ui.js.)   */
</script>
"""
