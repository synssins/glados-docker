"""HTML for the chat tab (id="tab-chat").

Extracted from glados/webui/tts_ui.py during Phase 3 of the WebUI
refactor (2026-04-21). This module exports only its tab-content
block; the page shell (head, sidebar, main open/close) lives in
pages/_shell.py and composition happens in glados.webui.tts_ui.
"""

HTML = r"""
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 2: Chat                                                    -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-chat" class="tab-content active">
<div class="container">
  <div class="card" style="padding:0.75rem;">
    <div class="chat-messages" id="chatMessages">
      <div class="empty-msg">Send a message to start talking with GLaDOS.</div>
    </div>
    <div class="chat-input-row">
      <input type="text" id="chatInput" placeholder="Type a message..."
             onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();chatSend();}">
      <button class="mic-btn" id="micBtn" onclick="toggleMic()" title="Push to talk">&#127908;</button>
      <button class="btn btn-primary" onclick="chatSend()">Send</button>
    </div>
  </div>
</div>
</div>
"""
