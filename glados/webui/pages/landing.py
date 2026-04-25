"""Landing page rendered at / for unauthenticated visitors.

Replaces the former 302 → /login redirect. Gives unauthenticated users
a branded entry point with a Sign in button and a direct link to the
publicly-accessible Speech (TTS) tools page.
"""
from __future__ import annotations

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0a;
    color: #e0e0e0;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .card {
    background: #1a1a2e;
    border: 1px solid #333;
    border-radius: 12px;
    padding: 48px 40px 40px;
    width: 340px;
    text-align: center;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5);
  }
  .brand {
    font-size: 2rem;
    font-weight: 700;
    color: #f4a623;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .tagline {
    font-size: 0.85em;
    color: #666;
    margin-bottom: 36px;
  }
  .btn-signin {
    display: block;
    background: #f4a623;
    color: #000;
    text-decoration: none;
    border-radius: 6px;
    padding: 12px 24px;
    font-size: 1em;
    font-weight: 600;
    margin-bottom: 16px;
    transition: background 0.15s;
  }
  .btn-signin:hover { background: #f5b84d; }
  .btn-tts {
    display: block;
    color: #888;
    text-decoration: none;
    font-size: 0.88em;
    padding: 8px;
    border-radius: 5px;
    transition: color 0.15s;
  }
  .btn-tts:hover { color: #f4a623; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">GLaDOS</div>
  <div class="tagline">Control Panel</div>
  <a href="/login" class="btn-signin">Sign in</a>
  <a href="/tts" class="btn-tts">Speech tools &rarr;</a>
</div>
</body>
</html>
"""
