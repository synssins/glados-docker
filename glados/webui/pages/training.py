"""HTML for the training tab (id="tab-training").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- TAB: TRAINING MONITOR                                          -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-training" class="tab-content">
<div class="page-shell">
<div class="container" style="position:relative;">
  <div id="trainingAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Training</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <!-- Status Cards -->
  <div class="train-status-row">
    <div class="train-card">
      <div class="train-card-label">Status</div>
      <div class="train-card-value"><span id="trainRunning" class="train-dot train-dot-off"></span> <span id="trainRunningText">Unknown</span></div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Fine-Tune Epoch</div>
      <div class="train-card-value" id="trainEpoch">--</div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Generator Loss</div>
      <div class="train-card-value" id="trainGenLoss">--</div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Discriminator Loss</div>
      <div class="train-card-value" id="trainDiscLoss">--</div>
    </div>
  </div>

  <!-- Action Buttons -->
  <div class="card" style="margin-top:12px;">
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="trainingSnapshot()" id="btnSnapshot">Snapshot &amp; Deploy</button>
      <button class="btn btn-danger" onclick="trainingStop()" id="btnTrainStop">Stop Training</button>
      <span id="snapshotStatus" style="font-size:0.85rem;color:var(--text-dim);"></span>
    </div>
  </div>

  <!-- Loss Chart -->
  <div class="card" style="margin-top:12px;">
    <div class="section-title">Loss Curves</div>
    <div class="train-chart-wrap">
      <canvas id="trainingChart"></canvas>
    </div>
  </div>

  <!-- Training Log -->
  <div class="card" style="margin-top:12px;">
    <div class="section-title">Training Log</div>
    <pre id="trainingLog" class="train-log">Loading...</pre>
  </div>

</div>
</div>
</div>
"""
