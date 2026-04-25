"""Tests for the role + permission registry."""
import pytest
from glados.webui.permissions import (
    PERMISSION_REGISTRY,
    ROLES,
    PASSWORD_DENYLIST,
    user_has_perm,
    password_meets_policy,
)


def test_roles_contain_only_admin_and_chat():
    assert set(ROLES.keys()) == {"admin", "chat"}


def test_admin_has_wildcard():
    assert ROLES["admin"] == frozenset({"*"})


def test_chat_has_minimum_perms():
    assert ROLES["chat"] == frozenset({"webui.view", "chat.read", "chat.send"})


@pytest.mark.parametrize("perm", list(ROLES["chat"]))
def test_admin_satisfies_every_chat_perm(perm):
    assert user_has_perm("admin", perm)


@pytest.mark.parametrize("perm", ["admin", "config.write", "logs.read"])
def test_chat_does_not_satisfy_admin_perms(perm):
    assert not user_has_perm("chat", perm)


def test_user_has_perm_returns_false_for_unknown_role():
    assert not user_has_perm("nonexistent", "webui.view")


def test_password_denylist_rejects_common_weak():
    for pw in ["password", "12345678", "qwerty12"]:
        ok, msg = password_meets_policy(pw)
        assert not ok
        assert "too" in msg.lower() or "weak" in msg.lower() or "common" in msg.lower()


def test_password_denylist_case_insensitive():
    ok, _ = password_meets_policy("PASSWORD")
    assert not ok


def test_password_too_short():
    ok, msg = password_meets_policy("abc")
    assert not ok
    assert "8" in msg


def test_password_accepts_strong():
    ok, msg = password_meets_policy("hunter2goes")
    assert ok, msg
    assert msg == ""


def test_permission_registry_contains_core_perms():
    assert "webui.view" in PERMISSION_REGISTRY
    assert "chat.read" in PERMISSION_REGISTRY
    assert "chat.send" in PERMISSION_REGISTRY
