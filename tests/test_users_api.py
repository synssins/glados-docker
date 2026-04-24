"""Tests for the admin-only Users CRUD API."""
import io
import json
from pathlib import Path
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

    yield {"configs": configs, "data": data}


def _write_admin_only_yaml(configs):
    (configs / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "bootstrap_allowed": False,
            "users": [{
                "username": "admin", "display_name": "admin", "role": "admin",
                "password_hash": hashing.hash_password("hunter2goes"),
                "hash_algorithm": "argon2id",
                "disabled": False, "created_at": 0,
            }],
        },
    }))
    cfg.load(configs_dir=str(configs))


def _write_two_users(configs):
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


def _admin_handler(method, path, body=None):
    from glados.webui.tts_ui import Handler
    body_bytes = json.dumps(body).encode("utf-8") if body else b""
    h = MagicMock()
    h.path = path
    h.command = method
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Content-Length": str(len(body_bytes)),
                 "Cookie": "glados_session=fake"}
    h.rfile = io.BytesIO(body_bytes)
    h._json_response = None
    h._sent = []
    h._resolved_user = {"username": "admin", "role": "admin", "session_id": "x"}

    def _send_json(code, payload):
        h._json_response = (code, payload)
    h._send_json = _send_json
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()
    # Bind real dispatch methods so mock doesn't swallow them
    import types
    h._dispatch_get = types.MethodType(Handler._dispatch_get, h)
    h._dispatch_post = types.MethodType(Handler._dispatch_post, h)
    h._dispatch_setup = types.MethodType(Handler._dispatch_setup, h)
    return h


def _chat_handler(method, path, body=None):
    h = _admin_handler(method, path, body)
    h._resolved_user = {"username": "alice", "role": "chat", "session_id": "x"}
    return h


# ── Helpers (pure functions in users.py) ────────────────────────

def test_list_users_returns_sanitized(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.pages.users import list_users
    users = list_users()
    assert len(users) == 2
    assert all("password_hash" not in u for u in users)


def test_create_user_succeeds(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import create_user
    ok, err = create_user("alice", "Alice", "chat", "hunter2goes")
    assert ok, err

    raw = yaml.safe_load((fresh_state["configs"] / "global.yaml").read_text())
    assert any(u["username"] == "alice" for u in raw["auth"]["users"])


def test_create_user_rejects_weak_password(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import create_user
    ok, err = create_user("bob", "", "chat", "abc")
    assert not ok
    assert "8" in err


def test_create_user_rejects_unknown_role(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import create_user
    ok, err = create_user("bob", "", "superuser", "hunter2goes")
    assert not ok


def test_create_user_rejects_duplicate(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import create_user
    ok, err = create_user("admin", "", "chat", "hunter2goes")
    assert not ok
    assert "already exists" in err


def test_update_user_role(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.pages.users import update_user
    ok, err = update_user("alice", role="admin")
    assert ok, err


def test_update_user_demote_last_admin_refused(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import update_user
    ok, err = update_user("admin", role="chat")
    assert not ok
    assert "last admin" in err


def test_disable_last_admin_refused(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import update_user
    ok, err = update_user("admin", disabled=True)
    assert not ok


def test_reset_password_succeeds(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.pages.users import reset_password
    ok, err = reset_password("alice", "newpassword")
    assert ok, err
    raw = yaml.safe_load((fresh_state["configs"] / "global.yaml").read_text())
    alice = next(u for u in raw["auth"]["users"] if u["username"] == "alice")
    # verify new hash is argon2id
    assert alice["password_hash"].startswith("$argon2id$")


def test_reset_password_rejects_weak(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.pages.users import reset_password
    ok, err = reset_password("alice", "abc")
    assert not ok


def test_delete_user_succeeds(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.pages.users import delete_user
    ok, err = delete_user("alice")
    assert ok, err
    raw = yaml.safe_load((fresh_state["configs"] / "global.yaml").read_text())
    assert not any(u["username"] == "alice" for u in raw["auth"]["users"])


def test_delete_last_admin_refused(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.pages.users import delete_user
    ok, err = delete_user("admin")
    assert not ok
    assert "last admin" in err


# ── HTTP route handlers (mock handler smoke tests) ──────────────

def test_get_users_admin_returns_list(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("GET", "/api/users")
    Handler.do_GET(h)
    code, body = h._json_response
    assert code == 200
    assert len(body["users"]) == 2


def test_get_users_chat_user_403(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _chat_handler("GET", "/api/users")
    Handler.do_GET(h)
    code, _ = h._json_response
    assert code == 403


def test_post_users_creates(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("POST", "/api/users", body={
        "username": "bob", "display_name": "Bob", "role": "chat",
        "password": "hunter2goes",
    })
    Handler.do_POST(h)
    code, body = h._json_response
    assert code == 201
    assert body["ok"] is True


def test_post_users_duplicate_409(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("POST", "/api/users", body={
        "username": "admin", "role": "chat", "password": "hunter2goes",
    })
    Handler.do_POST(h)
    code, _ = h._json_response
    assert code == 409


def test_put_users_updates_role(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("PUT", "/api/users/alice", body={"role": "admin"})
    Handler.do_PUT(h)
    code, body = h._json_response
    assert code == 200


def test_post_password_reset_other_user(fresh_state):
    _write_two_users(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("POST", "/api/users/alice/password",
                       body={"new_password": "newpass1"})
    Handler.do_POST(h)
    code, body = h._json_response
    assert code == 200


def test_delete_last_admin_400(fresh_state):
    _write_admin_only_yaml(fresh_state["configs"])
    from glados.webui.tts_ui import Handler
    h = _admin_handler("DELETE", "/api/users/admin")
    Handler.do_DELETE(h)
    code, body = h._json_response
    assert code == 400
    assert "last admin" in body["error"]
