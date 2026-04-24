"""Shared HTML shell for wizard steps."""
from __future__ import annotations


_SHELL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS — Setup: {title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif;
          background: #0a0a0a; color: #e0e0e0;
          display: flex; align-items: center; justify-content: center;
          min-height: 100vh; padding: 20px; }}
  .setup-box {{ background: #1a1a2e; border: 1px solid #333;
                border-radius: 12px; padding: 40px;
                width: 480px; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }}
  .setup-box h1 {{ text-align: center; color: #ff6600;
                   font-size: 1.6em; margin-bottom: 4px; }}
  .setup-box .step-indicator {{ text-align: center; color: #888;
                                font-size: 0.8em; margin-bottom: 24px;
                                text-transform: uppercase; letter-spacing: 1px; }}
  .setup-box h2 {{ color: #e0e0e0; font-size: 1.15em;
                   margin-bottom: 12px; }}
  .hint {{ color: #999; font-size: 0.8em; margin-top: 6px; }}
  .field {{ margin-bottom: 16px; }}
  .field label {{ display: block; font-size: 0.85em;
                  color: #aaa; margin-bottom: 6px; }}
  .field input {{ width: 100%; padding: 10px 12px;
                  background: #111; border: 1px solid #444;
                  border-radius: 6px; color: #e0e0e0; font-size: 1em; }}
  .field input:focus {{ border-color: #ff6600; outline: none; }}
  .btn {{ width: 100%; padding: 11px; background: #ff6600;
          color: #fff; border: none; border-radius: 6px;
          font-size: 1em; cursor: pointer; margin-top: 8px; }}
  .btn:hover {{ background: #e55a00; }}
  .error {{ background: #3a1111; border: 1px solid #ff4444;
            color: #ff6666; padding: 10px; border-radius: 6px;
            margin-bottom: 16px; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="setup-box">
  <h1>GLaDOS</h1>
  <div class="step-indicator">Step {step_num} of {total_steps}</div>
  {content}
</div>
</body>
</html>
"""


def render_shell(*, title: str, step_num: int, total_steps: int, content: str) -> str:
    return _SHELL_TEMPLATE.format(
        title=title, step_num=step_num, total_steps=total_steps, content=content,
    )
