"""Tests for /api/sessions endpoints."""
import io
import json
import types
from unittest.mock import MagicMock

import pytest
import yaml

from glados.auth import db as auth_db, hashing, sessions
from glados.core.config_store import cfg


@pytest.fixture
def fresh_state_with_sessions(tmp_path, monkeypatch):
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
                 "password_hash": "$argon2id$x", "hash_algorithm": "argon2id",
                 "disabled": False, "display_name": "admin", "created_at": 0},
                {"username": "alice", "role": "chat",
                 "password_hash": "$argon2id$x", "hash_algorithm": "argon2id",
                 "disabled": False, "display_name": "Alice", "created_at": 0},
            ],
        },
    }))
    cfg.load(configs_dir=str(configs))

    sessions.create(username="admin", role="admin")
    sessions.create(username="alice", role="chat")
    sessions.create(username="alice", role="chat")
    yield {"configs": configs, "data": data}


def _handler_for(role, username, method, path):
    from glados.webui.tts_ui import Handler

    h = MagicMock()
    h.path = path
    h.command = method
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Cookie": "glados_session=fake"}
    h._json_response = None
    h._resolved_user = {"username": username, "role": role, "session_id": "x"}
    h._send_json = lambda c, p: setattr(h, "_json_response", (c, p))
    h.send_response = lambda c: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()
    h.rfile = io.BytesIO()
    # Bind real dispatch methods
    h._dispatch_get = types.MethodType(Handler._dispatch_get, h)
    h._dispatch_post = types.MethodType(Handler._dispatch_post, h)
    h._dispatch_setup = types.MethodType(Handler._dispatch_setup, h)
    return h


def test_list_sessions_admin_sees_all(fresh_state_with_sessions):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin", "GET", "/api/sessions")
    Handler.do_GET(h)
    code, body = h._json_response
    assert code == 200
    assert len(body["sessions"]) == 3


def test_list_sessions_chat_user_sees_own(fresh_state_with_sessions):
    from glados.webui.tts_ui import Handler
    h = _handler_for("chat", "alice", "GET", "/api/sessions")
    Handler.do_GET(h)
    code, body = h._json_response
    assert code == 200
    assert len(body["sessions"]) == 2  # alice's two
    assert all(s["username"] == "alice" for s in body["sessions"])


def test_list_sessions_no_password_hash_in_response(fresh_state_with_sessions):
    """Session rows don't carry password_hash anyway, but verify nothing leaks."""
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin", "GET", "/api/sessions")
    Handler.do_GET(h)
    _, body = h._json_response
    for s in body["sessions"]:
        assert "password_hash" not in s


def test_revoke_session_admin_can_revoke_any(fresh_state_with_sessions):
    """Admin revokes alice's session by id."""
    from glados.webui.tts_ui import Handler
    target = sessions.list_active("alice")[0]
    h = _handler_for("admin", "admin", "DELETE",
                     f"/api/sessions/{target['session_id']}")
    Handler.do_DELETE(h)
    code, _ = h._json_response
    assert code == 200
    assert len(sessions.list_active("alice")) == 1


def test_revoke_session_chat_user_can_revoke_own(fresh_state_with_sessions):
    target = sessions.list_active("alice")[0]
    from glados.webui.tts_ui import Handler
    h = _handler_for("chat", "alice", "DELETE",
                     f"/api/sessions/{target['session_id']}")
    Handler.do_DELETE(h)
    assert h._json_response[0] == 200


def test_revoke_session_chat_user_cannot_revoke_other(fresh_state_with_sessions):
    """Alice cannot revoke admin's session."""
    target = sessions.list_active("admin")[0]
    from glados.webui.tts_ui import Handler
    h = _handler_for("chat", "alice", "DELETE",
                     f"/api/sessions/{target['session_id']}")
    Handler.do_DELETE(h)
    code, _ = h._json_response
    assert code == 403


def test_revoke_nonexistent_session_returns_404(fresh_state_with_sessions):
    from glados.webui.tts_ui import Handler
    h = _handler_for("admin", "admin", "DELETE",
                     "/api/sessions/00000000-0000-0000-0000-000000000000")
    Handler.do_DELETE(h)
    code, _ = h._json_response
    assert code == 404
