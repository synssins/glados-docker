"""HTML for the memory tab (id="tab-config-memory").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- ================================================================ -->
<!-- CONFIGURATION > MEMORY (Phase 5)                                   -->
<!-- ================================================================ -->
<div id="tab-config-memory" class="tab-content">
<div class="page-shell">
<div class="container" style="position:relative;">
  <div id="memoryAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Memory</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title">About this page</div>
    <div class="mode-desc" style="line-height:1.55;">
      <strong>Retrieval-Augmented Generation (RAG).</strong> When GLaDOS is about to respond,
      the most relevant facts from the library below are pulled in and quoted to
      her so she can reference them accurately &mdash; names, preferences, household
      details, things she&rsquo;s learned over time. The library combines facts you&rsquo;ve
      added manually with ones she&rsquo;s auto-extracted from conversation. Approved
      facts are RAG-eligible; pending facts queue for your review before becoming
      available.
    </div>
  </div>

  <div class="card">
    <div class="section-title">Memory configuration</div>
    <div class="mem-radio-row">
      <div class="mode-label" style="margin-bottom:4px;">Default status for new passive facts</div>
      <label><input type="radio" name="memDefaultStatus" value="approved" onchange="memSaveDefaultStatus('approved')"> Approved (enters RAG immediately)</label>
      <label><input type="radio" name="memDefaultStatus" value="pending" onchange="memSaveDefaultStatus('pending')"> Pending (manual review)</label>
      <div class="mode-desc" style="margin-top:4px;">
        Stored as <code>memory.passive_default_status</code>. Approved = reinforcement-on-repetition via ChromaDB similarity dedup.
        Pending = facts queue below for operator approval before entering RAG.
      </div>
    </div>
    <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <button class="btn-small" onclick="memSweepRetention()">Sweep retention now</button>
      <span id="memRetentionStatus" style="font-size:0.78rem;color:var(--text-dim);"></span>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
      <div class="section-title" style="margin-bottom:0;">Long-term facts</div>
      <div style="display:flex;gap:6px;align-items:center;">
        <input id="memSearchInput" type="text" placeholder="Search..." oninput="memSearchDebounced()">
        <button class="btn-small" onclick="memShowAddForm()">+ Add</button>
      </div>
    </div>
    <div id="memAddForm" style="display:none;margin-top:12px;">
      <textarea id="memAddText" placeholder="The operator prefers the living room lights at 40% in the evening"></textarea>
      <div style="margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <span style="font-size:0.82rem;color:var(--text-dim);">Importance:</span>
        <div class="tts-seg" id="memAddImportanceSeg">
          <div class="tts-seg-cell" data-value="0.20" onclick="memSegSelect(this,'memAddImportanceSeg')">Background</div>
          <div class="tts-seg-cell" data-value="0.40" onclick="memSegSelect(this,'memAddImportanceSeg')">Useful</div>
          <div class="tts-seg-cell on" data-value="0.60" onclick="memSegSelect(this,'memAddImportanceSeg')">Important</div>
          <div class="tts-seg-cell" data-value="0.80" onclick="memSegSelect(this,'memAddImportanceSeg')">Critical</div>
          <div class="tts-seg-cell" data-value="1.00" onclick="memSegSelect(this,'memAddImportanceSeg')">Extreme</div>
        </div>
        <button class="btn-small" onclick="memAddFact()">Save</button>
        <button class="btn-small" onclick="memHideAddForm()" style="background:#555;">Cancel</button>
      </div>
    </div>
    <div id="memFactsList" style="margin-top:12px;">Loading...</div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Recently learned</div>
      <button class="btn-small" onclick="memLoadRecent()">Refresh</button>
    </div>
    <div class="mode-desc" style="margin-top:4px;">Facts she&rsquo;s added on her own from conversation. Review and promote/reject if needed.</div>
    <div id="memRecentList" style="margin-top:10px;">Loading...</div>
  </div>

  <div class="card" id="memPendingCard" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Pending review</div>
      <button class="btn-small" onclick="memLoadPending()">Refresh</button>
    </div>
    <div class="mode-desc" style="margin-top:4px;">Facts auto-extracted but not yet approved for RAG.</div>
    <div id="memPendingList" style="margin-top:10px;">Loading...</div>
  </div>
</div>
</div>
</div>
"""
