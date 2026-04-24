"""Server-side helpers for the Users CRUD endpoints.

Operates against configs/global.yaml — the YAML is authoritative.
SQLite (auth.db) only carries dynamic state (last-login timestamps,
session rows). See docs/AUTH_DESIGN.md §5.6, §6.1, §6.3.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from glados.auth import hashing, sessions, user_state
from glados.webui.permissions import password_meets_policy


ALLOWED_ROLES = {"admin", "chat"}


def _global_yaml_path() -> Path:
    return Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "global.yaml"


def _read() -> dict:
    path = _global_yaml_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write(raw: dict) -> None:
    _global_yaml_path().write_text(
        yaml.safe_dump(raw, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _sanitize(user: dict) -> dict:
    """Strip password_hash before returning over the wire."""
    return {k: v for k, v in user.items() if k != "password_hash"}


def list_users() -> list[dict[str, Any]]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    enriched = []
    for u in users:
        out = _sanitize(u)
        state = user_state.get(u["username"])
        if state:
            out["last_login_at"] = state.get("last_login_at")
            out["last_login_addr"] = state.get("last_login_addr")
        enriched.append(out)
    return enriched


def validate_username(username: str) -> str:
    if not username:
        return "Username is required."
    if len(username) > 64:
        return "Username must be 64 characters or fewer."
    if any(ord(c) < 32 for c in username):
        return "Username must not contain control characters."
    return ""


def validate_role(role: str) -> str:
    if role not in ALLOWED_ROLES:
        return f"Role must be one of: {sorted(ALLOWED_ROLES)}."
    return ""


def create_user(username: str, display_name: str, role: str, password: str) -> tuple[bool, str]:
    err = validate_username(username)
    if err:
        return False, err
    err = validate_role(role)
    if err:
        return False, err
    ok, msg = password_meets_policy(password)
    if not ok:
        return False, msg

    raw = _read()
    auth = raw.setdefault("auth", {})
    users = auth.setdefault("users", [])
    if any(u["username"] == username for u in users):
        return False, f"User {username!r} already exists."
    users.append({
        "username": username,
        "display_name": display_name or username,
        "role": role,
        "password_hash": hashing.hash_password(password),
        "hash_algorithm": "argon2id",
        "disabled": False,
        "created_at": int(time.time()),
    })
    _write(raw)
    return True, ""


def update_user(
    username: str, *,
    role: str | None = None,
    display_name: str | None = None,
    disabled: bool | None = None,
) -> tuple[bool, str]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    idx = next((i for i, u in enumerate(users) if u["username"] == username), -1)
    if idx < 0:
        return False, "User not found."

    if role is not None:
        err = validate_role(role)
        if err:
            return False, err
        if users[idx]["role"] == "admin" and role != "admin":
            remaining_admins = sum(
                1 for u in users
                if u["role"] == "admin" and u["username"] != username
            )
            if remaining_admins == 0:
                return False, "Cannot demote the last admin."
        users[idx]["role"] = role

    if display_name is not None:
        users[idx]["display_name"] = display_name or users[idx]["username"]

    if disabled is not None:
        if disabled and users[idx]["role"] == "admin":
            remaining = sum(
                1 for u in users
                if u["role"] == "admin" and not u.get("disabled")
                and u["username"] != username
            )
            if remaining == 0:
                return False, "Cannot disable the last admin."
        users[idx]["disabled"] = disabled

    _write(raw)
    return True, ""


def reset_password(username: str, new_password: str) -> tuple[bool, str]:
    ok, msg = password_meets_policy(new_password)
    if not ok:
        return False, msg
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    idx = next((i for i, u in enumerate(users) if u["username"] == username), -1)
    if idx < 0:
        return False, "User not found."
    users[idx]["password_hash"] = hashing.hash_password(new_password)
    users[idx]["hash_algorithm"] = "argon2id"
    _write(raw)
    return True, ""


def delete_user(username: str) -> tuple[bool, str]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    victim = next((u for u in users if u["username"] == username), None)
    if victim is None:
        return False, "User not found."
    if victim["role"] == "admin":
        remaining = sum(
            1 for u in users
            if u["role"] == "admin" and u["username"] != username
        )
        if remaining == 0:
            return False, "Cannot delete the last admin."
    users[:] = [u for u in users if u["username"] != username]
    _write(raw)
    sessions.revoke_all_for_user(username)
    return True, ""
