"""Unit tests for the rewritten _handle_login (username + password + sessions module).

These tests exercise the login logic via an in-memory mock handler rather
than spinning up a full HTTP server. Full HTTP coverage will land in Task 4
once require_perm is in place.
"""
import io
import json
import pytest
from unittest.mock import MagicMock
from urllib.parse import urlencode

import bcrypt
import yaml

from glados.auth import db as auth_db, hashing
from glados.auth import sessions as auth_sessions
from glados.core.config_store import cfg


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Set up a tmp configs dir + tmp auth.db + monkeypatched cfg."""
    configs = tmp_path / "configs"
    data = tmp_path / "data"
    configs.mkdir()
    data.mkdir()

    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(configs))
    monkeypatch.setenv("GLADOS_DATA", str(data))
    monkeypatch.setattr(auth_db, "_db_path", lambda: data / "auth.db")
    auth_db.ensure_schema()

    # Point the singleton at the tmp dir (env var alone doesn't move _configs_dir
    # because it was resolved at singleton construction time).
    cfg.load(configs_dir=configs)

    yield {"configs": configs, "data": data}


def _write_global_yaml(configs, users):
    (configs / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "bootstrap_allowed": False,
            "users": users,
        },
    }))


def _make_login_handler(form_body: dict, remote_addr="127.0.0.1"):
    """Build a mock handler-like object with the attributes _handle_login uses."""
    body_bytes = urlencode(form_body).encode("utf-8")
    h = MagicMock()
    h.client_address = (remote_addr, 0)
    h.headers = {
        "Content-Length": str(len(body_bytes)),
        "User-Agent": "pytest",
        "Cookie": "",
    }
    h.rfile = io.BytesIO(body_bytes)
    h.wfile = io.BytesIO()
    h._sent_responses = []
    h.send_response = lambda c: h._sent_responses.append(("status", c))
    h.send_header = lambda k, v: h._sent_responses.append(("header", k, v))
    h.end_headers = lambda: h._sent_responses.append(("end_headers",))

    def _send_json(code, payload):
        h._json_response = (code, payload)

    h._send_json = _send_json
    return h


def test_login_with_correct_credentials_creates_session(fresh_state):
    """Argon2id user logs in successfully — session row created, cookie set."""
    pw_hash = hashing.hash_password("hunter2goes")
    _write_global_yaml(fresh_state["configs"], [{
        "username": "admin", "display_name": "admin", "role": "admin",
        "password_hash": pw_hash, "hash_algorithm": "argon2id",
        "disabled": False, "created_at": 0,
    }])
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_login_handler({"username": "admin", "password": "hunter2goes"})
    Handler._handle_login(h)

    # Success path uses raw HTTP, not _send_json — check status code and body.
    assert ("status", 200) in h._sent_responses
    h.wfile.seek(0)
    assert json.loads(h.wfile.read()) == {"ok": True}
    # A session row exists for this user.
    assert len(auth_sessions.list_active(username="admin")) == 1


def test_login_with_wrong_password_returns_401(fresh_state):
    pw_hash = hashing.hash_password("hunter2goes")
    _write_global_yaml(fresh_state["configs"], [{
        "username": "admin", "display_name": "admin", "role": "admin",
        "password_hash": pw_hash, "hash_algorithm": "argon2id",
        "disabled": False, "created_at": 0,
    }])
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_login_handler({"username": "admin", "password": "wrong-guess"})
    Handler._handle_login(h)

    code, body = h._json_response
    assert code == 401
    assert "invalid" in body["error"].lower()


def test_login_with_unknown_user_returns_401_same_message(fresh_state):
    """No username enumeration — same message as bad password."""
    _write_global_yaml(fresh_state["configs"], [])
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_login_handler({"username": "ghost", "password": "anything"})
    Handler._handle_login(h)

    code, body = h._json_response
    assert code == 401
    assert "invalid" in body["error"].lower()


def test_login_with_bcrypt_legacy_user_rehashes_to_argon2id(fresh_state):
    """After first successful login, the YAML must hold an argon2id hash."""
    legacy = bcrypt.hashpw(b"hunter2goes", bcrypt.gensalt()).decode("ascii")
    _write_global_yaml(fresh_state["configs"], [{
        "username": "admin", "display_name": "admin", "role": "admin",
        "password_hash": legacy, "hash_algorithm": "bcrypt-legacy",
        "disabled": False, "created_at": 0,
    }])
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_login_handler({"username": "admin", "password": "hunter2goes"})
    Handler._handle_login(h)

    assert ("status", 200) in h._sent_responses
    raw = yaml.safe_load((fresh_state["configs"] / "global.yaml").read_text())
    assert raw["auth"]["users"][0]["hash_algorithm"] == "argon2id"
    assert raw["auth"]["users"][0]["password_hash"].startswith("$argon2id$")


def test_login_with_disabled_user_denied(fresh_state):
    pw_hash = hashing.hash_password("hunter2goes")
    _write_global_yaml(fresh_state["configs"], [{
        "username": "admin", "display_name": "admin", "role": "admin",
        "password_hash": pw_hash, "hash_algorithm": "argon2id",
        "disabled": True, "created_at": 0,
    }])
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_login_handler({"username": "admin", "password": "hunter2goes"})
    Handler._handle_login(h)

    assert h._json_response[0] == 401
