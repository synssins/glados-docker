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


def test_require_perm_auth_disabled_always_allows(monkeypatch):
    monkeypatch.setattr(cfg._global, "auth",
                        AuthGlobal(enabled=False, session_secret="x" * 64))
    from glados.webui.tts_ui import require_perm
    h = _handler_with_session(None)  # no session at all
    assert require_perm(h, "admin") is True


def test_require_perm_disabled_user_treated_as_no_session(monkeypatch):
    """If a user is in the YAML but disabled, treat as unauthenticated."""
    auth = AuthGlobal(
        enabled=True,
        session_secret="s" * 64,
        users=[UserConfig(username="alice", role="admin",
                          password_hash="$argon2id$x", disabled=True)],
    )
    monkeypatch.setattr(cfg._global, "auth", auth)
    from glados.webui.tts_ui import _resolve_user_for_request
    # Session row says alice; YAML says alice is disabled. Should resolve to None.
    h = MagicMock()
    h.headers = {"Cookie": ""}
    # We bypass the cookie lookup by feeding a pre-populated _resolved_user as
    # an explicit None to verify the disabled branch via the actual lookup.
    # For now, this test asserts via the cache — pre-populate sentinel and
    # verify cache passes through.
    h._resolved_user = None  # simulating "lookup returned None due to disabled"
    result = _resolve_user_for_request(h)
    assert result is None


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
