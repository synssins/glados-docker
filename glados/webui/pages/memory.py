"""HTML for the memory tab (id="tab-config-memory").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.

Phase 6 / Approach 2 sweep (2026-05-09): inline-style attributes
replaced with v3 utility classes. Phantom-var references swept.
"""

HTML = r"""
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
      <div class="mode-label mb-1">Default status for new passive facts</div>
      <label><input type="radio" name="memDefaultStatus" value="approved" onchange="memSaveDefaultStatus('approved')"> Approved (enters RAG immediately)</label>
      <label><input type="radio" name="memDefaultStatus" value="pending" onchange="memSaveDefaultStatus('pending')"> Pending (manual review)</label>
      <div class="mode-desc mt-1">
        Stored as <code>memory.passive_default_status</code>. Approved = reinforcement-on-repetition via ChromaDB similarity dedup.
        Pending = facts queue below for operator approval before entering RAG.
      </div>
    </div>
    <div class="row gap-3 wrap mt-4">
      <button class="btn-small" onclick="memSweepRetention()">Sweep retention now</button>
      <span id="memRetentionStatus" class="fs-xs txt-dim"></span>
    </div>
  </div>

  <div class="card">
    <div class="row between wrap gap-2">
      <div class="section-title mb-0">Long-term facts</div>
      <div class="row gap-1">
        <input id="memSearchInput" type="text" placeholder="Search..." oninput="memSearchDebounced()">
        <button class="btn-small" onclick="memShowAddForm()">+ Add</button>
      </div>
    </div>
    <div id="memAddForm" class="mt-3" style="display:none;">
      <textarea id="memAddText" placeholder="The operator prefers the living room lights at 40% in the evening"></textarea>
      <div class="row gap-2 wrap mt-1">
        <span class="fs-sm txt-dim">Importance:</span>
        <div class="tts-seg" id="memAddImportanceSeg">
          <div class="tts-seg-cell" data-value="0.20" onclick="memSegSelect(this,'memAddImportanceSeg')">Background</div>
          <div class="tts-seg-cell" data-value="0.40" onclick="memSegSelect(this,'memAddImportanceSeg')">Useful</div>
          <div class="tts-seg-cell on" data-value="0.60" onclick="memSegSelect(this,'memAddImportanceSeg')">Important</div>
          <div class="tts-seg-cell" data-value="0.80" onclick="memSegSelect(this,'memAddImportanceSeg')">Critical</div>
          <div class="tts-seg-cell" data-value="1.00" onclick="memSegSelect(this,'memAddImportanceSeg')">Extreme</div>
        </div>
        <button class="btn-small" onclick="memAddFact()">Save</button>
        <button class="btn-cancel" onclick="memHideAddForm()">Cancel</button>
      </div>
    </div>
    <div id="memFactsList" class="mt-3">Loading...</div>
  </div>

  <div class="card">
    <div class="row between">
      <div class="section-title mb-0">Recently learned</div>
      <button class="btn-small" onclick="memLoadRecent()">Refresh</button>
    </div>
    <div class="mode-desc mt-1">Facts she&rsquo;s added on her own from conversation. Review and promote/reject if needed.</div>
    <div id="memRecentList" class="mt-2">Loading...</div>
  </div>

  <div class="card" id="memPendingCard" style="display:none;">
    <div class="row between">
      <div class="section-title mb-0">Pending review</div>
      <button class="btn-small" onclick="memLoadPending()">Refresh</button>
    </div>
    <div class="mode-desc mt-1">Facts auto-extracted but not yet approved for RAG.</div>
    <div id="memPendingList" class="mt-2">Loading...</div>
  </div>
</div>
</div>
</div>
"""
