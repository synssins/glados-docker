"""WebUI /api/plugins/* endpoints — round-trip + auth + SSRF + secrets.

The tests build a MagicMock-based admin Handler the same way
test_users_api.py does, then bind real dispatch methods so the real
routing fires. Auth is short-circuited by injecting a resolved admin
user; require_perm() honors the injected user.
"""
from __future__ import annotations

import io
import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _good_manifest_json(name: str = "demo.python") -> str:
    return json.dumps({
        "name": name,
        "description": "demo plugin",
        "version": "0.1.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "DEMO_KEY", "isSecret": True, "isRequired": True},
                {"name": "DEMO_URL", "isRequired": False, "default": "https://x.test"},
            ],
        }],
    })


def _admin_handler(method: str, path: str, body: dict | None = None):
    """Build a MagicMock Handler with admin user resolved + dispatch
    methods bound so the real routing fires."""
    from glados.webui.tts_ui import Handler

    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    h = MagicMock()
    h.path = path
    h.command = method
    h.client_address = ("127.0.0.1", 0)
    h.headers = {
        "Content-Length": str(len(body_bytes)),
        "Cookie": "glados_session=fake",
    }
    h.rfile = io.BytesIO(body_bytes)
    h._json_response = None
    h._sent = []
    h._resolved_user = {"username": "admin", "role": "admin", "session_id": "x"}

    def _send_json(code, payload):
        h._json_response = (code, payload)
    h._send_json = _send_json
    h._send_error = lambda code, msg: _send_json(code, {"ok": False, "error": msg})
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()

    h.do_GET = types.MethodType(Handler.do_GET, h)
    h.do_POST = types.MethodType(Handler.do_POST, h)
    h.do_DELETE = types.MethodType(Handler.do_DELETE, h)
    h._dispatch_plugins_get = types.MethodType(Handler._dispatch_plugins_get, h)
    h._dispatch_plugins_post = types.MethodType(Handler._dispatch_plugins_post, h)
    h._read_plugin_body = types.MethodType(Handler._read_plugin_body, h)
    h._list_plugins = types.MethodType(Handler._list_plugins, h)
    h._plugin_detail = types.MethodType(Handler._plugin_detail, h)
    h._install_plugin = types.MethodType(Handler._install_plugin, h)
    h._save_plugin_runtime = types.MethodType(Handler._save_plugin_runtime, h)
    h._set_plugin_enabled = types.MethodType(Handler._set_plugin_enabled, h)
    h._delete_plugin = types.MethodType(Handler._delete_plugin, h)
    h._plugin_logs = types.MethodType(Handler._plugin_logs, h)
    return h


def _chat_handler(method: str, path: str, body: dict | None = None):
    h = _admin_handler(method, path, body)
    h._resolved_user = {"username": "alice", "role": "chat", "session_id": "x"}
    return h


@pytest.fixture
def auth_off(monkeypatch):
    """Disable auth in cfg so require_perm short-circuits to True
    without needing a fully-loaded auth backend."""
    from glados.core.config_store import cfg as _cfg_live
    monkeypatch.setattr(_cfg_live.auth, "enabled", False)


# ── List endpoint ─────────────────────────────────────────────────────


def test_list_when_disabled_globally(auth_off, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", "false")
    h = _admin_handler("GET", "/api/plugins")
    h.do_GET()
    code, body = h._json_response
    assert code == 200
    assert body["enabled_globally"] is False
    assert body["plugins"] == []


def test_list_when_enabled_returns_discovered(auth_off, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", "true")
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(_good_manifest_json())
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\npackage_index: 0\n"
    )

    h = _admin_handler("GET", "/api/plugins")
    h.do_GET()
    code, body = h._json_response
    assert code == 200
    assert body["enabled_globally"] is True
    assert len(body["plugins"]) == 1
    assert body["plugins"][0]["slug"] == "demo"
    assert body["plugins"][0]["enabled"] is True


# ── Install endpoint guards ──────────────────────────────────────────


def test_install_https_only(auth_off):
    h = _admin_handler(
        "POST", "/api/plugins/install",
        body={"url": "http://example.test/server.json"},
    )
    h.do_POST()
    code, body = h._json_response
    assert code == 400
    assert "https" in body["error"].lower()


def test_install_rejects_loopback(auth_off):
    h = _admin_handler(
        "POST", "/api/plugins/install",
        body={"url": "https://127.0.0.1/server.json"},
    )
    h.do_POST()
    code, body = h._json_response
    assert code == 400
    err = body["error"].lower()
    assert "loopback" in err or "private" in err or "ssrf" in err


def test_install_rejects_rfc1918(auth_off):
    h = _admin_handler(
        "POST", "/api/plugins/install",
        body={"url": "https://10.0.0.5/server.json"},
    )
    h.do_POST()
    code, _ = h._json_response
    assert code == 400


def test_install_oversize_response_rejected(auth_off, monkeypatch):
    big = "x" * (256 * 1024 + 1)
    fake_resp = MagicMock(status_code=200, text=big, headers={"content-length": str(len(big))})
    fake_resp.content = big.encode()
    with patch("glados.webui.plugin_endpoints.httpx.get", return_value=fake_resp), \
         patch(
             "glados.webui.plugin_endpoints._resolve_safe_host",
             return_value=True,
         ):
        h = _admin_handler(
            "POST", "/api/plugins/install",
            body={"url": "https://example.test/server.json"},
        )
        h.do_POST()
    code, body = h._json_response
    assert code == 400
    assert "too large" in body["error"].lower()


def test_install_happy_path_writes_disabled_stub(
    auth_off, tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    fake_resp = MagicMock(status_code=200, text=_good_manifest_json())
    fake_resp.content = _good_manifest_json().encode()
    with patch("glados.webui.plugin_endpoints.httpx.get", return_value=fake_resp), \
         patch(
             "glados.webui.plugin_endpoints._resolve_safe_host",
             return_value=True,
         ):
        h = _admin_handler(
            "POST", "/api/plugins/install",
            body={"url": "https://example.test/server.json"},
        )
        h.do_POST()
    code, body = h._json_response
    assert code == 200, body
    assert body["slug"] == "demo-python"
    plugin_dir = tmp_path / "demo-python"
    assert (plugin_dir / "server.json").exists()
    assert (plugin_dir / "runtime.yaml").exists()
    rt_yaml = (plugin_dir / "runtime.yaml").read_text()
    assert "enabled: false" in rt_yaml


# ── Save runtime config ──────────────────────────────────────────────


def test_save_runtime_preserves_unchanged_secrets(
    auth_off, tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(_good_manifest_json())
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\npackage_index: 0\n"
    )
    (plugin_dir / "secrets.env").write_text("DEMO_KEY=existing-secret\n")

    h = _admin_handler(
        "POST", "/api/plugins/demo",
        body={
            "env_values": {"DEMO_URL": "https://changed.test"},
            "secrets": {"DEMO_KEY": "***"},
        },
    )
    h.do_POST()
    code, _ = h._json_response
    assert code == 200
    secrets_after = (plugin_dir / "secrets.env").read_text()
    assert "DEMO_KEY=existing-secret" in secrets_after


def test_save_runtime_overwrites_changed_secret(
    auth_off, tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(_good_manifest_json())
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\npackage_index: 0\n"
    )
    (plugin_dir / "secrets.env").write_text("DEMO_KEY=existing-secret\n")

    h = _admin_handler(
        "POST", "/api/plugins/demo",
        body={"env_values": {}, "secrets": {"DEMO_KEY": "new-secret"}},
    )
    h.do_POST()
    code, _ = h._json_response
    assert code == 200
    secrets_after = (plugin_dir / "secrets.env").read_text()
    assert "DEMO_KEY=new-secret" in secrets_after
    assert "existing-secret" not in secrets_after


# ── Enable / disable / delete (hot-rotate) ──────────────────────────


def test_enable_calls_add_server(auth_off, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\nremote_index: 0\n"
    )

    fake_manager = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", return_value=fake_manager):
        h = _admin_handler("POST", "/api/plugins/demo/enable")
        h.do_POST()
    code, _ = h._json_response
    assert code == 200
    add_mock = fake_manager.add_server
    add_mock.assert_called_once()
    cfg = add_mock.call_args.args[0]
    # MCPServerConfig.name is the manifest name (Plugin.name), not the slug.
    assert cfg.name == "demo.python"
    assert cfg.transport == "http"  # remote plugin in this test


def test_disable_calls_remove_server(auth_off, tmp_path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\nremote_index: 0\n"
    )

    fake_manager = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", return_value=fake_manager):
        h = _admin_handler("POST", "/api/plugins/demo/disable")
        h.do_POST()
    code, _ = h._json_response
    assert code == 200
    fake_manager.remove_server.assert_called_once_with("demo")


def test_delete_removes_session_and_dir(auth_off, tmp_path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\nremote_index: 0\n"
    )

    fake_manager = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", return_value=fake_manager):
        h = _admin_handler("DELETE", "/api/plugins/demo")
        h.do_DELETE()
    code, _ = h._json_response
    assert code == 200
    fake_manager.remove_server.assert_called_once_with("demo")
    assert not plugin_dir.exists()


# ── Auth gate (chat role rejected) ──────────────────────────────────
#
# The `auth_off` fixture above short-circuits require_perm() so the
# admin-only gate is never exercised in the happy-path tests. These
# three tests run with auth ON and a chat-role user to confirm GET /
# POST / DELETE all reject. The `_admin_handler` mock overrides
# `_send_json` to capture into `_json_response`, so we check that
# rather than `send_response`. The `***` literal elsewhere in this
# file is the SECRET_PLACEHOLDER convention, not a real secret.


@pytest.fixture
def auth_on(monkeypatch):
    """Enable auth so require_perm() actually runs the role check."""
    from glados.core.config_store import cfg as _cfg_live
    monkeypatch.setattr(_cfg_live.auth, "enabled", True)


def test_chat_role_blocked_from_get(auth_on):
    handler = _chat_handler("GET", "/api/plugins")
    handler.do_GET()
    code, _ = handler._json_response
    assert code in (401, 403)


def test_chat_role_blocked_from_post(auth_on):
    handler = _chat_handler(
        "POST", "/api/plugins/install",
        body={"url": "https://example.test/server.json"},
    )
    handler.do_POST()
    code, _ = handler._json_response
    assert code in (401, 403)


def test_chat_role_blocked_from_delete(auth_on):
    handler = _chat_handler("DELETE", "/api/plugins/some-slug")
    handler.do_DELETE()
    code, _ = handler._json_response
    assert code in (401, 403)


def test_logs_returns_stdio_tail_and_events(auth_off, tmp_path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("GLADOS_PLUGIN_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "demo.log").write_text("startup line\nerror line\n")

    fake_manager = MagicMock()
    fake_manager.get_plugin_events.return_value = [
        {"ts": 1, "kind": "connect", "message": "hi"},
    ]
    with patch("glados.webui.tts_ui._mcp_manager", return_value=fake_manager):
        h = _admin_handler("GET", "/api/plugins/demo/logs?lines=200")
        h.do_GET()
    code, body = h._json_response
    assert code == 200
    assert "startup line" in body["stdio_log"][0]
    assert body["events"][0]["kind"] == "connect"
