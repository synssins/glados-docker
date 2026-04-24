"""Tests for require_perm and the helper resolution path."""
import pytest
from unittest.mock import MagicMock

from glados.core.config_store import cfg, AuthGlobal, UserConfig


def _handler_with_session(session_resolved):
    """Mock handler that has _resolve_user_for_request short-circuited."""
    h = MagicMock()
    h.headers = {"Cookie": "glados_session=fake"}
    h.path = "/api/memory/recent"
    h._resolved_user = session_resolved
    h._json_response = None

    def _send_json(code, payload):
        h._json_response = (code, payload)
    h._send_json = _send_json

    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    h.wfile = MagicMock()
    return h


@pytest.fixture
def admin_users(monkeypatch):
    auth = AuthGlobal(
        enabled=True,
        session_secret="s" * 64,
        users=[UserConfig(username="admin", role="admin",
                          password_hash="$argon2id$x")],
    )
    monkeypatch.setattr(cfg._global, "auth", auth)


@pytest.fixture
def chat_user_present(monkeypatch):
    auth = AuthGlobal(
        enabled=True,
        session_secret="s" * 64,
        users=[
            UserConfig(username="alice", role="chat",
                       password_hash="$argon2id$x"),
        ],
    )
    monkeypatch.setattr(cfg._global, "auth", auth)


# ── require_perm ────────────────────────────────────────────────

def test_require_perm_admin_satisfies_admin_sentinel(admin_users):
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session({"username": "admin", "role": "admin",
                               "session_id": "abc"})
    assert require_perm(h, "admin") is True


def test_require_perm_chat_user_denied_admin_sentinel(chat_user_present):
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session({"username": "alice", "role": "chat",
                               "session_id": "abc"})
    h.path = "/api/config/reload"
    assert require_perm(h, "admin") is False
    assert h._json_response[0] == 403


def test_require_perm_chat_user_allowed_chat_send(chat_user_present):
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session({"username": "alice", "role": "chat",
                               "session_id": "abc"})
    h.path = "/api/chat"
    assert require_perm(h, "chat.send") is True


def test_require_perm_no_session_returns_401_for_api(admin_users):
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session(None)
    h.path = "/api/memory/recent"
    assert require_perm(h, "admin") is False
    assert h._json_response[0] == 401


def test_require_perm_no_session_redirects_for_html(admin_users):
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session(None)
    h.path = "/"
    assert require_perm(h, "webui.view") is False
    h.send_response.assert_called_once_with(302)
    h.send_header.assert_any_call("Location", "/login")
    h.end_headers.assert_called_once()


def test_require_perm_auth_disabled_always_allows(monkeypatch):
    monkeypatch.setattr(cfg._global, "auth",
                        AuthGlobal(enabled=False, session_secret="x" * 64))
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session(None)  # no session at all
    assert require_perm(h, "admin") is True


def test_resolve_user_disabled_in_yaml_returns_none(monkeypatch):
    """If session row points at alice, but YAML says alice.disabled=True,
    the resolver must return None (treated as unauthenticated)."""
    auth = AuthGlobal(
        enabled=True,
        session_secret="s" * 64,
        users=[UserConfig(username="alice", role="admin",
                          password_hash="$argon2id$x", disabled=True)],
    )
    monkeypatch.setattr(cfg._global, "auth", auth)

    # Mock sessions.verify to return a valid session for "alice"
    from glados.auth import sessions as auth_sessions
    monkeypatch.setattr(
        auth_sessions, "verify",
        lambda token: (True, {"username": "alice", "session_id": "sid",
                              "role_at_issue": "admin"}),
    )

    from glados.webui.tts_ui import _resolve_user_for_request
    handler = MagicMock()
    handler.headers = {"Cookie": "glados_session=anything"}
    # No _resolved_user attribute → cache miss → real lookup runs
    if hasattr(handler, "_resolved_user"):
        del handler._resolved_user

    result = _resolve_user_for_request(handler)
    assert result is None


def test_require_perm_chat_user_denied_html_admin_path(chat_user_present):
    """HTML routes return 403 HTML body, not JSON."""
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session({"username": "alice", "role": "chat",
                               "session_id": "abc"})
    h.path = "/admin-page-html"   # any non-/api/ path
    assert require_perm(h, "admin") is False
    h.send_response.assert_called_once_with(403)
    # Content-Type header set to text/html
    type_calls = [c for c in h.send_header.call_args_list
                  if c.args[0] == "Content-Type"]
    assert any("text/html" in c.args[1] for c in type_calls)
    h.end_headers.assert_called_once()
    h.wfile.write.assert_called_once()


# ── Public/private route map sanity ────────────────────────────

def test_public_paths_includes_login_and_health():
    from glados.webui.tts_ui import _PUBLIC_PATHS
    assert "/login" in _PUBLIC_PATHS
    assert "/health" in _PUBLIC_PATHS
    assert "/logout" in _PUBLIC_PATHS


def test_public_prefixes_includes_stt_and_static():
    from glados.webui.tts_ui import _PUBLIC_PREFIXES
    assert "/api/stt" in _PUBLIC_PREFIXES
    assert "/static/" in _PUBLIC_PREFIXES
    assert "/api/auth/" in _PUBLIC_PREFIXES


def test_public_prefixes_excludes_chat():
    """Chat endpoint is now gated, per operator decision 2026-04-24."""
    from glados.webui.tts_ui import _PUBLIC_PREFIXES
    assert "/api/chat" not in _PUBLIC_PREFIXES
    assert "/chat_audio/" not in _PUBLIC_PREFIXES


def test_require_auth_method_removed():
    """The legacy _require_auth method should be gone — require_perm replaces it."""
    from glados.webui.tts_ui import Handler
    assert not hasattr(Handler, "_require_auth"), \
        "Legacy _require_auth still present; sweep incomplete"


def test_no_remaining_require_auth_call_sites():
    """No call to self._require_auth() should remain."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "glados" / "webui" / "tts_ui.py"
    text = src.read_text(encoding="utf-8", errors="replace")
    assert "_require_auth" not in text, \
        "Some _require_auth references survived the sweep"
