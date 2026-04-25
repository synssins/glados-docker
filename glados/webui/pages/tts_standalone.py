"""Standalone /tts page — minimal text-to-audio form. Unauthenticated.

The full TTS Generator inside the SPA shell remains available to admins.
This page exists so external integrations and casual users can hit the
TTS service without logging in. See docs/AUTH_DESIGN.md §3.4.
"""
from __future__ import annotations


TTS_STANDALONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS Speech</title>
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif;
         background: #0a0a0a; color: #e0e0e0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; padding: 20px; }
  .box { background: #1a1a2e; border: 1px solid #333;
         border-radius: 12px; padding: 32px; width: 520px; max-width: 100%; }
  h1 { color: #ff6600; margin: 0 0 16px; font-size: 1.4em; }
  textarea { width: 100%; min-height: 120px; background: #111;
             border: 1px solid #444; border-radius: 6px; color: #e0e0e0;
             padding: 10px; font-family: inherit; font-size: 1em;
             box-sizing: border-box; resize: vertical; }
  button { margin-top: 12px; padding: 10px 20px; background: #ff6600;
           color: #fff; border: none; border-radius: 6px; font-size: 1em;
           cursor: pointer; }
  button:hover { background: #e55a00; }
  button:disabled { background: #666; cursor: not-allowed; }
  audio { width: 100%; margin-top: 16px; display: none; }
  audio.visible { display: block; }
  .status { margin-top: 10px; font-size: 0.85em; color: #aaa; }
</style>
</head>
<body>
<div class="box">
  <h1>GLaDOS Speech</h1>
  <form id="ttsForm" onsubmit="return false;">
    <textarea id="text" placeholder="Type text for GLaDOS to speak..."></textarea>
    <button id="go" type="submit">Generate</button>
  </form>
  <div class="status" id="status"></div>
  <audio id="audio" controls></audio>
</div>
<script>
document.getElementById('ttsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = document.getElementById('text').value.trim();
  if (!text) return;
  const btn = document.getElementById('go');
  const status = document.getElementById('status');
  const audio = document.getElementById('audio');
  btn.disabled = true;
  status.textContent = 'Synthesising...';
  audio.classList.remove('visible');
  try {
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    audio.src = data.url;
    audio.load();
    audio.classList.add('visible');
    audio.play();
    status.textContent = 'Ready.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>"""
