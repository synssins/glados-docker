"""Tests for POST /api/auth/change-password."""
import io
import json
from unittest.mock import MagicMock

import pytest
import yaml

from glados.auth import db as auth_db, hashing
from glados.core.config_store import cfg


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    data = tmp_path / "data"
    configs.mkdir()
    data.mkdir()
    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(configs))
    monkeypatch.setenv("GLADOS_DATA", str(data))
    monkeypatch.setattr(auth_db, "_db_path", lambda: data / "auth.db")
    auth_db.ensure_schema()

    (configs / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "bootstrap_allowed": False,
            "users": [
                {"username": "admin", "role": "admin",
                 "password_hash": hashing.hash_password("hunter2goes"),
                 "hash_algorithm": "argon2id", "disabled": False,
                 "display_name": "admin", "created_at": 0},
                {"username": "alice", "role": "chat",
                 "password_hash": hashing.hash_password("hunter2goes"),
                 "hash_algorithm": "argon2id", "disabled": False,
                 "display_name": "Alice", "created_at": 0},
            ],
        },
    }))
    cfg.load(configs_dir=str(configs))
    yield {"configs": configs, "data": data}


def _handler_for(role, username, body):
    """Mock handler authed as the given user."""
    import types
    from glados.webui.tts_ui import Handler

    body_bytes = json.dumps(body).encode("utf-8")
    h = MagicMock()
    h.path = "/api/auth/change-password"
    h.command = "POST"
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Content-Length": str(len(body_bytes)),
                 "Cookie": "glados_session=fake"}
    h.rfile = io.BytesIO(body_bytes)
    h._json_response = None
    h._resolved_user = {"username": username, "role": role,
                        "session_id": "x"}
    h._send_json = lambda code, payload: setattr(
        h, "_json_response", (code, payload),
    )
    h.send_response = lambda c: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()
    # Bind real dispatch methods so MagicMock doesn't swallow them
    h._dispatch_post = types.MethodType(Handler._dispatch_post, h)
    h._change_password = types.MethodType(Handler._change_password, h)
    h._dispatch_setup = types.MethodType(Handler._dispatch_setup, h)
    return h


def test_change_password_self_succeeds(fresh_state):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin",
                     {"current": "hunter2goes", "new": "newpass1234"})
    Handler.do_POST(h)
    assert h._json_response[0] == 200
    raw = yaml.safe_load((fresh_state["configs"] / "global.yaml").read_text())
    admin = next(u for u in raw["auth"]["users"] if u["username"] == "admin")
    # New hash, password verifies
    valid, _ = hashing.verify_password("newpass1234", admin["password_hash"])
    assert valid


def test_change_password_wrong_current_returns_401(fresh_state):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin",
                     {"current": "wrong", "new": "newpass1234"})
    Handler.do_POST(h)
    assert h._json_response[0] == 401


def test_change_password_weak_new_returns_400(fresh_state):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin",
                     {"current": "hunter2goes", "new": "abc"})
    Handler.do_POST(h)
    assert h._json_response[0] == 400


def test_change_password_chat_user_can_change_own(fresh_state):
    """webui.view perm — both roles can change their own password."""
    from glados.webui.tts_ui import Handler
    h = _handler_for("chat", "alice",
                     {"current": "hunter2goes", "new": "newpass1234"})
    Handler.do_POST(h)
    assert h._json_response[0] == 200


def test_change_password_no_session_returns_401(fresh_state):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin",
                     {"current": "hunter2goes", "new": "newpass1234"})
    h._resolved_user = None  # no session
    Handler.do_POST(h)
    assert h._json_response[0] == 401


def test_change_password_does_not_revoke_other_sessions(fresh_state):
    """Operator decision 2026-04-24: pw change does NOT auto-revoke."""
    from glados.auth import sessions as _sess
    # Pre-create two sessions for admin (via cfg.auth.session_secret)
    _ = _sess.create(username="admin", role="admin")
    _ = _sess.create(username="admin", role="admin")
    before = len(_sess.list_active("admin"))

    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin",
                     {"current": "hunter2goes", "new": "newpass1234"})
    Handler.do_POST(h)
    assert h._json_response[0] == 200

    after = len(_sess.list_active("admin"))
    assert after == before
