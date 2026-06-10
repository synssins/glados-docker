"""HTML for the Integrations -> Events page (id="tab-config-events").

Admin-only rule editor for the event-action engine. Page shell,
master toggle, rule list, and editor form are all populated by
evLoad() / evRenderEditor() in static/ui.js. No server-rendered
state lives in this HTML block.

Spec: docs/superpowers/specs/2026-06-09-event-action-engine-design.md
"""

HTML = r"""
<!-- ================================================================ -->
<!-- CONFIGURATION > EVENTS (event-action engine)                      -->
<!-- ================================================================ -->
<div id="tab-config-events" class="tab-content">
<div class="page-shell">
<div class="container">

  <div class="page-header">
    <h2 class="page-title">Events</h2>
    <div class="page-title-desc">When something happens, GLaDOS decides &mdash; and Home Assistant does.</div>
  </div>

  <div class="card" id="ev-master-card">
    <div class="section-title">Engine</div>
    <div class="row gap-2">
      <label class="toggle" title="Enable or disable the event-action engine globally">
        <input type="checkbox" id="ev-master-toggle">
        <span class="toggle-slider"></span>
      </label>
      <span class="fs-sm">Event engine enabled</span>
    </div>
    <div id="ev-load-error" class="txt-danger mt-2" hidden></div>
  </div>

  <div class="card">
    <div class="row between wrap gap-2 mb-2">
      <div class="section-title mb-0">Rules</div>
      <button class="btn-small" id="ev-add-rule">+ Add rule</button>
    </div>
    <div id="ev-rule-list"></div>
  </div>

  <div class="card" id="ev-editor-card" hidden>
    <div class="section-title" id="ev-editor-title">New rule</div>
    <div id="ev-editor-form"></div>
  </div>

</div>
</div>
</div>
"""
