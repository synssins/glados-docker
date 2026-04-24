"""First-run step: create the initial admin user.

Per AUTH_DESIGN.md §5.1.3, the first user's role is hard-coded to
'admin' — the form does not expose a role field.
"""
from __future__ import annotations

import html
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from glados.auth import hashing
from glados.webui.permissions import password_meets_policy
from glados.webui.setup.wizard import StepResult


_FORM_HTML = """
<h2>Set admin password</h2>
<p class="hint">Create the initial administrator account. This user
has full control. Add more users later from
Configuration &rarr; Users.</p>

{error}

<form method="post">
  <div class="field">
    <label for="username">Username</label>
    <input type="text" id="username" name="username"
           autocomplete="username" autofocus required
           maxlength="64" value="{username}">
    <p class="hint">Case-sensitive. Pick anything memorable.</p>
  </div>

  <div class="field">
    <label for="display_name">Display name (optional)</label>
    <input type="text" id="display_name" name="display_name"
           maxlength="128" value="{display_name}">
  </div>

  <div class="field">
    <label for="password">Password</label>
    <input type="password" id="password" name="password"
           autocomplete="new-password" required minlength="8">
    <p class="hint">At least 8 characters. Avoid obvious choices.</p>
  </div>

  <div class="field">
    <label for="confirm">Confirm password</label>
    <input type="password" id="confirm" name="confirm"
           autocomplete="new-password" required minlength="8">
  </div>

  <button type="submit" class="btn">Create admin</button>
</form>
"""


@dataclass(frozen=True)
class SetAdminPasswordStep:
    order: int = 100
    name: str = "admin-password"

    @property
    def title(self) -> str:
        return "Set admin password"

    def is_required(self, cfg) -> bool:
        admins = [u for u in getattr(cfg.auth, "users", []) if u.role == "admin"]
        return cfg.auth.bootstrap_allowed and not admins

    def render(self, handler, error: str = "", sticky_form: dict | None = None) -> str:
        f = sticky_form or {}
        error_html = (
            f'<div class="error">{html.escape(error)}</div>' if error else ''
        )
        return _FORM_HTML.format(
            error=error_html,
            username=html.escape(f.get("username", "")),
            display_name=html.escape(f.get("display_name", "")),
        )

    def process(self, handler, form: dict) -> StepResult:
        username = (form.get("username") or "").strip()
        display_name = (form.get("display_name") or "").strip() or username
        password = form.get("password") or ""
        confirm = form.get("confirm") or ""

        err = _validate(username, password, confirm)
        if err:
            handler._wizard_error = err
            handler._wizard_form = {"username": username,
                                    "display_name": display_name}
            return StepResult.ERROR

        # Hash + merge-write
        hashed = hashing.hash_password(password)

        config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
        path = Path(config_dir) / "global.yaml"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        auth = raw.setdefault("auth", {})
        users = auth.setdefault("users", [])
        users.append({
            "username": username,
            "display_name": display_name,
            "role": "admin",                           # HARD-CODED
            "password_hash": hashed,
            "hash_algorithm": "argon2id",
            "disabled": False,
            "created_at": int(time.time()),
        })
        auth["bootstrap_allowed"] = False
        if not auth.get("session_secret"):
            auth["session_secret"] = secrets.token_hex(64)

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

        from glados.core.config_store import cfg
        cfg.reload()
        return StepResult.DONE


def _validate(username: str, password: str, confirm: str) -> str:
    if not username:
        return "Username is required."
    if len(username) > 64:
        return "Username must be 64 characters or fewer."
    if any(ord(c) < 32 for c in username):
        return "Username must not contain control characters."
    if password != confirm:
        return "Passwords do not match."
    ok, msg = password_meets_policy(password)
    if not ok:
        return msg
    return ""
