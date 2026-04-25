"""Role + permission registry for the WebUI.

Two fixed roles: admin (wildcard) and chat (minimum for home-control
chat). No custom roles. See docs/AUTH_DESIGN.md §4 for the rationale.
"""
from __future__ import annotations


PERMISSION_REGISTRY: frozenset[str] = frozenset({
    "webui.view",       # load SPA shell
    "chat.read",        # view chat history
    "chat.send",        # send a new chat turn
})
"""Every permission string known to the system. Admin-only routes use
the sentinel 'admin' and do not appear here."""


ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset({"*"}),
    "chat":  frozenset({"webui.view", "chat.read", "chat.send"}),
}


def user_has_perm(role: str, perm: str) -> bool:
    """Return True iff a user with `role` is granted `perm`.

    The sentinel permission 'admin' is satisfied only by the admin role
    (which has wildcard). Wildcard '*' grants everything including
    'admin'.
    """
    perms = ROLES.get(role)
    if perms is None:
        return False
    if "*" in perms:
        return True
    return perm in perms


PASSWORD_DENYLIST: frozenset[str] = frozenset({
    "password", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyui", "qwerty12", "qwerty123", "letmein1", "abcd1234",
    "admin123", "password1", "glados12", "passpass", "11111111",
    "00000000", "welcome1", "baseball", "football", "monkey12",
})


MIN_PASSWORD_LENGTH = 8


def password_meets_policy(password: str) -> tuple[bool, str]:
    """Return (ok, error_message). Empty string on success."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in PASSWORD_DENYLIST:
        return False, "Password is too common; choose something less guessable."
    return True, ""
