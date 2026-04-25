"""Tests for _is_authenticated / _auth_password_configured (post-Task-3 logic)."""
import pytest
from unittest.mock import MagicMock

from glados.core.config_store import cfg, AuthGlobal, UserConfig
from glados.webui.tts_ui import _is_authenticated, _auth_password_configured


@pytest.fixture
def empty_users(monkeypatch):
    monkeypatch.setattr(cfg, "_global", cfg._global.model_copy(update={
        "auth": AuthGlobal(enabled=True, users=[]),
    }))


@pytest.fixture
def one_admin_user(monkeypatch):
    monkeypatch.setattr(cfg, "_global", cfg._global.model_copy(update={
        "auth": AuthGlobal(
            enabled=True,
            session_secret="s" * 64,
            users=[UserConfig(username="admin", role="admin",
                              password_hash="$argon2id$x")],
        ),
    }))


def _handler_with_no_cookie():
    h = MagicMock()
    h.headers = {"Cookie": ""}
    return h


def test_is_authenticated_false_when_no_users(empty_users):
    assert _is_authenticated(_handler_with_no_cookie()) is False


def test_auth_password_configured_false_when_no_users(empty_users):
    assert _auth_password_configured() is False


def test_is_authenticated_false_with_users_but_no_cookie(one_admin_user):
    assert _is_authenticated(_handler_with_no_cookie()) is False


def test_auth_password_configured_true_with_users(one_admin_user):
    assert _auth_password_configured() is True


def test_is_authenticated_true_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(cfg, "_global", cfg._global.model_copy(update={
        "auth": AuthGlobal(enabled=False),
    }))
    assert _is_authenticated(_handler_with_no_cookie()) is True
