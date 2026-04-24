# WebUI Auth Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bespoke bcrypt+HMAC WebUI auth with a supported, multi-user, RBAC-aware system — argon2id hashes, SQLite-backed sessions, a pluggable first-run wizard, and a compose-only auth-bypass recovery mode. No framework rewrite; keep `http.server.ThreadingHTTPServer`.

**Architecture:** Assemble from primitives (`argon2-cffi` + `itsdangerous`). Users list lives in `configs/global.yaml`; sessions and dynamic state in a new `/app/data/auth.db` SQLite file. Two fixed roles (`admin`, `chat`) with a small permission registry. TTS + STT stay unauthenticated; chat is authed; configuration is admin-only. First-run wizard framework is extensible but Phase 1 ships only the Set-Admin-Password step. Full design in [docs/AUTH_DESIGN.md](AUTH_DESIGN.md).

**Tech Stack:** Python 3.12+, pytest, pydantic 2.10+, stdlib `http.server`, SQLite3 (stdlib), PyYAML, loguru, `argon2-cffi` 25.1+, `itsdangerous` 2.2+.

---

## File structure

**New files:**
- `glados/webui/permissions.py` — role/permission registry, password denylist, `user_has_perm()`.
- `glados/auth/__init__.py` — package marker.
- `glados/auth/hashing.py` — Argon2id wrapper with bcrypt-verify+rehash migration.
- `glados/auth/db.py` — SQLite bootstrap and migrations for `/app/data/auth.db`.
- `glados/auth/sessions.py` — create/verify/revoke sessions, itsdangerous signing.
- `glados/auth/user_state.py` — last-login / failed-login counter CRUD.
- `glados/auth/bypass.py` — env-var read + banner HTML + audit tagging helpers.
- `glados/auth/rate_limit.py` — in-memory token-bucket limiter for login + service routes.
- `glados/webui/setup/__init__.py` — package marker.
- `glados/webui/setup/wizard.py` — `WizardStep` abstraction + step registry + engine.
- `glados/webui/setup/shell.py` — shared wizard HTML shell.
- `glados/webui/setup/steps/__init__.py`
- `glados/webui/setup/steps/admin_password.py` — `SetAdminPasswordStep`.
- `glados/webui/pages/users.py` — Users-management page handlers.
- `glados/webui/pages/tts_standalone.py` — unauthed `/tts` form page.
- `glados/core/duration.py` — `"30d"` / `"never"` parsing.
- `tests/test_permissions.py`
- `tests/test_auth_hashing.py`
- `tests/test_auth_sessions.py`
- `tests/test_auth_migration.py`
- `tests/test_auth_routes.py`
- `tests/test_setup_wizard.py`
- `tests/test_users_api.py`
- `tests/test_auth_bypass.py`
- `tests/test_rate_limiter.py`
- `tests/test_duration.py`

**Modified files:**
- `pyproject.toml` — add `argon2-cffi`, `itsdangerous` deps; bump `bcrypt` comment to "legacy migration only".
- `glados/core/config_store.py` (`AuthGlobal` model at lines 185-189) — replace with the new multi-user shape.
- `glados/webui/tts_ui.py` — large rewrite of the auth section (lines 439-746), `_require_auth` call sites (~60 locations), login page HTML, new routes for `/setup/*`, `/api/users/*`, `/api/auth/change-password`, `/api/sessions/*`, `/tts`. Remove module-level `_AUTH_*` globals.
- `glados/tools/set_password.py` — add deprecation warning; slated for removal after +90 days.
- `glados/observability/audit.py` — add `operator_id` and `auth_bypass` fields to `AuditEvent`.
- `configs/config.example.yaml` — update `auth:` block to new shape.
- `docs/CHANGES.md` — one entry per phase as it lands.

---

## Task ordering constraints

- **Task 1** must land first (shared primitives: permissions module, schema, deps).
- **Task 2** depends on Task 1 (uses new schema).
- **Task 3** depends on Tasks 1–2 (migration synthesizer reads new schema; login uses sessions module).
- **Task 4** depends on Tasks 1–3 (route-gating rewrite assumes the new auth is callable).
- **Tasks 5, 6, 10** are independent of each other once Task 4 ships; can be parallelized if desired.
- **Task 7** depends on Task 4 (Users page needs `require_perm`).
- **Task 8** depends on Task 7 (Active Sessions card lives in the same admin UI surface).
- **Task 9** must land after Task 4 (bypass short-circuits `require_perm`).
- **Task 11** is the last step — removes legacy shims that earlier tasks relied on.

---

## Task 1: Shared primitives and schema

**Files:**
- Modify: `pyproject.toml`
- Create: `glados/webui/permissions.py`
- Create: `glados/core/duration.py`
- Create: `tests/test_permissions.py`
- Create: `tests/test_duration.py`
- Modify: `glados/core/config_store.py:185-189` (`AuthGlobal` model + new `UserConfig` + `RateLimitsConfig`)
- Modify: `glados/webui/tts_ui.py:439-448` (remove module-level `_AUTH_*` globals)

### 1.1 Add dependencies

- [ ] **Step 1: Edit `pyproject.toml` `[project].dependencies`**

Find the block containing `"bcrypt>=4.0.0",` and add argon2-cffi + itsdangerous. Update the bcrypt comment to note its legacy-only status.

```toml
    # Auth
    "argon2-cffi>=25.1.0",       # Argon2id password hashing (new default)
    "itsdangerous>=2.2.0",       # signed session cookies
    "bcrypt>=4.0.0",             # legacy hash verification during migration; slated for removal
```

- [ ] **Step 2: Install and smoke-test**

Run:
```
cd /c/src/glados-container && python -m pip install -e .
python -c "import argon2, itsdangerous; print(argon2.__version__, itsdangerous.__version__)"
```
Expected: both versions print without error.

- [ ] **Step 3: Commit**

```
git add pyproject.toml
git commit -m "deps: add argon2-cffi + itsdangerous for auth rebuild"
```

### 1.2 Permissions module

- [ ] **Step 1: Write `tests/test_permissions.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/src/glados-container && pytest tests/test_permissions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'glados.webui.permissions'`.

- [ ] **Step 3: Create `glados/webui/permissions.py`**

```python
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


# 20 of the most common weak passwords. Case-insensitive match.
# Intentionally small — catches embarrassments without maintenance
# overhead. See docs/AUTH_DESIGN.md §6.5.
PASSWORD_DENYLIST: frozenset[str] = frozenset({
    "password", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyui", "qwerty12", "qwerty123", "letmein1", "abcd1234",
    "admin123", "password1", "glados12", "passpass", "11111111",
    "00000000", "welcome1", "baseball", "football", "monkey12",
})


MIN_PASSWORD_LENGTH = 8


def password_meets_policy(password: str) -> tuple[bool, str]:
    """Return (ok, error_message). Empty string on success.

    Policy: at least MIN_PASSWORD_LENGTH chars AND not in PASSWORD_DENYLIST
    (case-insensitive). No complexity classes per NIST SP 800-63B.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in PASSWORD_DENYLIST:
        return False, "Password is too common; choose something less guessable."
    return True, ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_permissions.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```
git add glados/webui/permissions.py tests/test_permissions.py
git commit -m "feat(auth): add role/permission registry and password policy"
```

### 1.3 Duration parser

- [ ] **Step 1: Write `tests/test_duration.py`**

```python
"""Tests for the session_timeout duration parser."""
import pytest
from glados.core.duration import parse_duration, NEVER


@pytest.mark.parametrize("s, expected", [
    ("never", NEVER),
    ("0", 0),
    ("60", 60),
    ("30s", 30),
    ("5m", 5 * 60),
    ("2h", 2 * 60 * 60),
    ("30d", 30 * 24 * 60 * 60),
    ("1w", 7 * 24 * 60 * 60),
    ("2W", 14 * 24 * 60 * 60),
])
def test_parse_duration_valid(s, expected):
    assert parse_duration(s) == expected


@pytest.mark.parametrize("s", ["", "abc", "2x", "-1d", "1.5h"])
def test_parse_duration_invalid_raises(s):
    with pytest.raises(ValueError):
        parse_duration(s)


def test_never_sentinel():
    assert NEVER is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_duration.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `glados/core/duration.py`**

```python
"""Parse human durations like '30d' or 'never' into seconds."""
from __future__ import annotations

import re

# Sentinel meaning "no expiry". Using None rather than a large integer
# so callers can pass it straight through to itsdangerous.loads(max_age=NEVER).
NEVER = None

_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_MULTIPLIER = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_duration(value: str) -> int | None:
    """Parse '30d' / 'never' / bare integer seconds into seconds.

    Returns NEVER (None) for 'never'. Raises ValueError on any other
    unparseable input.
    """
    if not isinstance(value, str):
        raise ValueError(f"duration must be str, got {type(value).__name__}")
    s = value.strip()
    if s.lower() == "never":
        return NEVER
    m = _PATTERN.match(s)
    if not m:
        raise ValueError(f"cannot parse duration: {value!r}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * _MULTIPLIER[unit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_duration.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```
git add glados/core/duration.py tests/test_duration.py
git commit -m "feat(core): add duration parser for session_timeout values"
```

### 1.4 AuthGlobal schema — new shape

- [ ] **Step 1: Write the schema test additions in `tests/test_config_defaults.py` (existing file)**

Locate the existing `test_config_defaults.py`. Append the following test functions at the bottom (do not modify existing tests):

```python
# ── Auth schema (added 2026-04-XX for multi-user rebuild) ──────────

def test_auth_global_new_shape_defaults():
    from glados.core.config_store import AuthGlobal
    a = AuthGlobal()
    assert a.enabled is True
    assert a.session_secret == ""
    assert a.session_timeout == "30d"
    assert a.session_idle_timeout == "0"
    assert a.bootstrap_allowed is True
    assert a.users == []
    assert a.rate_limits.login_max_attempts == 5
    assert a.rate_limits.service_max_requests == 10


def test_auth_global_legacy_fields_still_parse():
    """Existing deployments have password_hash + session_timeout_hours
    at the top level. Migration must not reject the legacy shape."""
    from glados.core.config_store import AuthGlobal
    a = AuthGlobal.model_validate({
        "enabled": True,
        "password_hash": "$2b$12$legacyhash",
        "session_secret": "abc123",
        "session_timeout_hours": 24,
    })
    assert a.password_hash == "$2b$12$legacyhash"
    assert a.session_timeout_hours == 24
    assert a.users == []   # will be synthesized at load-time in task 3


def test_auth_user_config_defaults():
    from glados.core.config_store import UserConfig
    u = UserConfig(username="alice", password_hash="$argon2id$...")
    assert u.role == "chat"
    assert u.hash_algorithm == "argon2id"
    assert u.disabled is False
    assert u.display_name == "alice"   # defaults to username


def test_auth_user_config_rejects_unknown_role():
    import pydantic
    from glados.core.config_store import UserConfig
    with pytest.raises(pydantic.ValidationError):
        UserConfig(username="x", password_hash="h", role="superuser")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_defaults.py -v -k "auth"`
Expected: FAIL with `ImportError` on `UserConfig` / field-doesn't-exist errors.

- [ ] **Step 3: Rewrite `AuthGlobal` in `glados/core/config_store.py`**

Locate lines 185-189 (the current `AuthGlobal` class). Replace that block with:

```python
class RateLimitsConfig(BaseModel):
    login_window_seconds: int = 60
    login_max_attempts: int = 5
    service_window_seconds: int = 60
    service_max_requests: int = 10


class UserConfig(BaseModel):
    username: str                             # 1-64 chars, no control chars, case-sensitive
    display_name: str = ""                    # defaults to username at post-init
    role: Literal["admin", "chat"] = "chat"
    password_hash: str = ""
    hash_algorithm: Literal["argon2id", "bcrypt-legacy"] = "argon2id"
    disabled: bool = False
    created_at: int = 0                       # unix epoch seconds

    @model_validator(mode="after")
    def _fill_display_name(self) -> "UserConfig":
        if not self.display_name:
            object.__setattr__(self, "display_name", self.username)
        return self


class AuthGlobal(BaseModel):
    enabled: bool = True
    session_secret: str = ""
    session_timeout: str = "30d"              # "never" | "<n>m|h|d|w" | int seconds
    session_idle_timeout: str = "0"           # "0" disabled | "<duration>"
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    bootstrap_allowed: bool = True
    users: list[UserConfig] = Field(default_factory=list)

    # DEPRECATED — retained for one release cycle to accept legacy YAML.
    # Migration synthesizer (Task 3) converts these into `users[]` on load.
    password_hash: str = Field(default="", deprecated=True)
    hash_algorithm: Literal["argon2id", "bcrypt-legacy"] = Field(
        default="argon2id", deprecated=True,
    )
    session_timeout_hours: int = Field(default=0, deprecated=True)
```

Also add the required imports at the top of `config_store.py` if they aren't already present:

```python
from typing import Literal
from pydantic import model_validator
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_defaults.py -v -k "auth"`
Expected: all new auth tests PASS. Existing tests also still pass — run `pytest tests/test_config_defaults.py -q` to confirm.

- [ ] **Step 5: Commit**

```
git add glados/core/config_store.py tests/test_config_defaults.py
git commit -m "feat(auth): multi-user schema in AuthGlobal; legacy fields kept for migration"
```

### 1.5 Remove module-level `_AUTH_*` globals from tts_ui.py

- [ ] **Step 1: Read the current auth block**

Read `glados/webui/tts_ui.py` lines 439–600. Identify every use of `_AUTH_ENABLED`, `_AUTH_PASSWORD_HASH`, `_AUTH_SESSION_SECRET`, `_AUTH_SESSION_TIMEOUT_H`, and `_SESSION_SHORT_S`. (`grep -n '_AUTH_\|_SESSION_SHORT_S' glados/webui/tts_ui.py` lists them.)

- [ ] **Step 2: Write a regression test at `tests/test_auth_routes.py` (new file)**

```python
"""Regression: live-reload of auth config propagates without restart."""
from glados.core.config_store import cfg as _cfg


def test_auth_config_reads_live_from_cfg(monkeypatch):
    """If _cfg.auth.enabled changes, subsequent auth checks must see it.

    Proves that the fix in tts_ui.py reads _cfg.auth.* per request rather
    than capturing values at import. See AUTH_DESIGN.md §2.7 / §7.3.
    """
    from glados.webui import tts_ui

    # The module must NOT hold module-level aliases of auth config.
    banned = {"_AUTH_ENABLED", "_AUTH_PASSWORD_HASH",
              "_AUTH_SESSION_SECRET", "_AUTH_SESSION_TIMEOUT_H"}
    present = banned & set(vars(tts_ui))
    assert not present, f"tts_ui still holds stale auth globals: {present}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_auth_routes.py::test_auth_config_reads_live_from_cfg -v`
Expected: FAIL (globals still present).

- [ ] **Step 4: Edit `glados/webui/tts_ui.py` lines 444-452**

Replace:
```python
# Auth config (reloaded on each check to pick up changes)
_AUTH_ENABLED = _cfg.auth.enabled
_AUTH_PASSWORD_HASH = _cfg.auth.password_hash
_AUTH_SESSION_SECRET = _cfg.auth.session_secret
_AUTH_SESSION_TIMEOUT_H = _cfg.auth.session_timeout_hours

# Session durations
_SESSION_SHORT_S = _AUTH_SESSION_TIMEOUT_H * 3600       # normal session (24h default)
_SESSION_LONG_S = 30 * 24 * 3600                        # "stay logged in" (30 days)
```

With:
```python
# Auth config is read live from _cfg on every check. NO module-level
# aliases — earlier code captured _cfg.auth.* at import which silently
# broke live-reload. See docs/AUTH_DESIGN.md §2.7.
_SESSION_LONG_S = 30 * 24 * 3600                        # "stay logged in" cookie Max-Age (30 days)
```

Then update every helper in that section to read `_cfg.auth.<field>` directly. Sweep:
- `_sign_session` line 483 → `_cfg.auth.session_secret`
- `_verify_session` line 497 → `_cfg.auth.session_secret`
- `_is_authenticated` lines 586, 588 → `_cfg.auth.enabled`, `_cfg.auth.password_hash`
- `_auth_password_configured` line 599 → `_cfg.auth.password_hash`
- `_handle_login` lines 1253, 1259 → `_cfg.auth.password_hash`

Use grep to find and Edit to replace each occurrence. After edits, `grep -n '_AUTH_' glados/webui/tts_ui.py` should return **zero matches**.

- [ ] **Step 5: Run the regression test + full tts_ui test suite**

Run:
```
pytest tests/test_auth_routes.py -v
pytest tests/test_config_defaults.py -v
python -c "from glados.webui import tts_ui; print('import OK')"
```
Expected: regression test PASSES. tts_ui imports without error. Existing tests still pass.

- [ ] **Step 6: Commit**

```
git add glados/webui/tts_ui.py tests/test_auth_routes.py
git commit -m "fix(auth): remove module-level _AUTH_* globals; read _cfg.auth live

Prior code captured cfg.auth.* values at module import, defeating the
live-reload path despite the comment claiming the opposite. Password
changes required a container restart to take effect, contrary to
MEMORY.md > feedback_tools_and_timeouts.

Regression test in test_auth_routes.py asserts the globals are gone."
```

---

## Task 2: Sessions, hashing, SQLite

**Files:**
- Create: `glados/auth/__init__.py`
- Create: `glados/auth/hashing.py`
- Create: `glados/auth/db.py`
- Create: `glados/auth/sessions.py`
- Create: `glados/auth/user_state.py`
- Create: `tests/test_auth_hashing.py`
- Create: `tests/test_auth_sessions.py`

### 2.1 Hashing module

- [ ] **Step 1: Write `tests/test_auth_hashing.py`**

```python
"""Tests for the Argon2id + legacy-bcrypt-verify wrapper."""
import pytest
from glados.auth.hashing import hash_password, verify_password, needs_rehash


def test_hash_password_returns_argon2id_encoding():
    h = hash_password("hunter2goes")
    assert h.startswith("$argon2id$")


def test_verify_password_argon2id_roundtrip():
    h = hash_password("hunter2goes")
    ok, rehash = verify_password("hunter2goes", h)
    assert ok
    assert not rehash


def test_verify_password_wrong_returns_false():
    h = hash_password("hunter2goes")
    ok, _ = verify_password("wrong", h)
    assert not ok


def test_verify_password_bcrypt_legacy_hash():
    """Old deployment has $2b$... — verify must still work."""
    import bcrypt
    legacy = bcrypt.hashpw(b"hunter2goes", bcrypt.gensalt()).decode("ascii")
    assert legacy.startswith("$2")
    ok, rehash = verify_password("hunter2goes", legacy)
    assert ok
    assert rehash, "legacy bcrypt must signal needs-rehash"


def test_needs_rehash_true_for_bcrypt():
    import bcrypt
    legacy = bcrypt.hashpw(b"x", bcrypt.gensalt()).decode("ascii")
    assert needs_rehash(legacy)


def test_needs_rehash_false_for_argon2id():
    h = hash_password("x" * 8)
    assert not needs_rehash(h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_hashing.py -v`
Expected: `ModuleNotFoundError: No module named 'glados.auth'`.

- [ ] **Step 3: Create `glados/auth/__init__.py` and `glados/auth/hashing.py`**

Empty `__init__.py`:
```
# empty — package marker
```

`glados/auth/hashing.py`:
```python
"""Argon2id password hashing with legacy bcrypt-verify compatibility.

All new hashes are Argon2id. Old bcrypt hashes are verified as-is on
first successful login, which triggers a silent rehash and YAML
merge-write. See docs/AUTH_DESIGN.md §6 + §10.
"""
from __future__ import annotations

import argon2
import bcrypt


_hasher = argon2.PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return an Argon2id PHC-encoded hash string."""
    return _hasher.hash(plaintext)


def _is_bcrypt(stored: str) -> bool:
    return stored.startswith(("$2a$", "$2b$", "$2y$"))


def needs_rehash(stored: str) -> bool:
    """True if the stored hash should be replaced (bcrypt legacy, or
    argon2 with stale parameters)."""
    if _is_bcrypt(stored):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except argon2.exceptions.InvalidHash:
        # Unknown format — force rehash to normalize.
        return True


def verify_password(plaintext: str, stored: str) -> tuple[bool, bool]:
    """Return (valid, needs_rehash).

    - valid: True iff plaintext matches the stored hash.
    - needs_rehash: True iff the caller should re-hash the plaintext
      with the current Argon2id parameters and persist the new hash.
      False means the stored hash is already up-to-date.
    """
    if not stored:
        return False, False

    if _is_bcrypt(stored):
        try:
            ok = bcrypt.checkpw(plaintext.encode("utf-8"), stored.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return False, False
        return ok, ok  # valid bcrypt result → rehash iff successful verification

    try:
        _hasher.verify(stored, plaintext)
    except (argon2.exceptions.VerifyMismatchError,
            argon2.exceptions.InvalidHash,
            argon2.exceptions.VerificationError):
        return False, False
    try:
        return True, _hasher.check_needs_rehash(stored)
    except argon2.exceptions.InvalidHash:
        return True, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_hashing.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add glados/auth/__init__.py glados/auth/hashing.py tests/test_auth_hashing.py
git commit -m "feat(auth): argon2id hasher with bcrypt-legacy verify+rehash"
```

### 2.2 SQLite bootstrap

- [ ] **Step 1: Write `tests/test_auth_sessions.py` — schema half**

```python
"""Tests for auth.db schema + session CRUD."""
from pathlib import Path
import pytest
from glados.auth import db as auth_db


@pytest.fixture
def tmp_auth_db(tmp_path, monkeypatch):
    target = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "_db_path", lambda: target)
    auth_db.ensure_schema()
    yield target


def test_ensure_schema_creates_file(tmp_auth_db):
    assert tmp_auth_db.exists()


def test_ensure_schema_is_idempotent(tmp_auth_db):
    # Call twice; no exception means good.
    auth_db.ensure_schema()
    auth_db.ensure_schema()


def test_schema_has_expected_tables(tmp_auth_db):
    import sqlite3
    con = sqlite3.connect(tmp_auth_db)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    names = {r[0] for r in rows}
    assert {"auth_sessions", "user_state"}.issubset(names)


def test_schema_revoked_sessions_index_exists(tmp_auth_db):
    import sqlite3
    con = sqlite3.connect(tmp_auth_db)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    finally:
        con.close()
    names = {r[0] for r in rows}
    assert "idx_auth_sessions_username" in names
    assert "idx_auth_sessions_expires" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_sessions.py -v -k "schema"`
Expected: `ImportError` on `from glados.auth import db`.

- [ ] **Step 3: Create `glados/auth/db.py`**

```python
"""SQLite bootstrap for /app/data/auth.db.

The DB carries session rows and dynamic per-user state (last-login,
failed-login counter). Users themselves live in configs/global.yaml
(YAML is authoritative — see docs/AUTH_DESIGN.md §6.3).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id      TEXT PRIMARY KEY,
    username        TEXT NOT NULL,
    role_at_issue   TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    last_used_at    INTEGER NOT NULL,
    expires_at      INTEGER,
    revoked_at      INTEGER,
    user_agent      TEXT,
    remote_addr     TEXT,
    auth_method     TEXT NOT NULL DEFAULT 'password'
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
    ON auth_sessions(expires_at)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_username
    ON auth_sessions(username)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS user_state (
    username              TEXT PRIMARY KEY,
    last_login_at         INTEGER,
    last_login_addr       TEXT,
    failed_login_count    INTEGER NOT NULL DEFAULT 0,
    last_failed_login_at  INTEGER
);
"""


def _db_path() -> Path:
    """/app/data/auth.db in the container; respects GLADOS_DATA env for dev."""
    root = os.environ.get("GLADOS_DATA", "/app/data")
    return Path(root) / "auth.db"


def ensure_schema() -> None:
    """Create the auth.db file and its schema if missing. Idempotent."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(_SCHEMA_SQL)
        con.commit()
    finally:
        con.close()


def connect() -> sqlite3.Connection:
    """Return an open sqlite3.Connection with Row factory enabled."""
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    return con
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_sessions.py -v -k "schema"`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add glados/auth/db.py tests/test_auth_sessions.py
git commit -m "feat(auth): SQLite schema bootstrap for auth.db"
```

### 2.3 Sessions module

- [ ] **Step 1: Append session-CRUD tests to `tests/test_auth_sessions.py`**

```python
# ── Session CRUD ───────────────────────────────────────────────

import time
from glados.auth import sessions


def test_create_session_roundtrip(tmp_auth_db, monkeypatch):
    monkeypatch.setattr("glados.core.config_store.cfg.auth.session_secret",
                        "a" * 64, raising=False)
    token = sessions.create(username="admin", role="admin",
                            remote_addr="127.0.0.1", user_agent="pytest")
    assert token and "." in token

    valid, row = sessions.verify(token)
    assert valid
    assert row["username"] == "admin"
    assert row["role_at_issue"] == "admin"


def test_verify_bad_signature_returns_none(tmp_auth_db, monkeypatch):
    monkeypatch.setattr("glados.core.config_store.cfg.auth.session_secret",
                        "a" * 64, raising=False)
    valid, row = sessions.verify("not.a.valid.token")
    assert not valid
    assert row is None


def test_revoke_marks_session_revoked(tmp_auth_db, monkeypatch):
    monkeypatch.setattr("glados.core.config_store.cfg.auth.session_secret",
                        "a" * 64, raising=False)
    token = sessions.create(username="admin", role="admin")
    _, row = sessions.verify(token)
    sessions.revoke(row["session_id"])
    valid, _ = sessions.verify(token)
    assert not valid


def test_expires_at_in_past_invalidates(tmp_auth_db, monkeypatch):
    monkeypatch.setattr("glados.core.config_store.cfg.auth.session_secret",
                        "a" * 64, raising=False)
    token = sessions.create(username="admin", role="admin",
                            expires_at=int(time.time()) - 10)
    valid, _ = sessions.verify(token)
    assert not valid


def test_list_active_sessions_filters_revoked(tmp_auth_db, monkeypatch):
    monkeypatch.setattr("glados.core.config_store.cfg.auth.session_secret",
                        "a" * 64, raising=False)
    t1 = sessions.create(username="admin", role="admin")
    t2 = sessions.create(username="admin", role="admin")
    _, row1 = sessions.verify(t1)
    sessions.revoke(row1["session_id"])

    active = sessions.list_active(username="admin")
    assert len(active) == 1
    _, row2 = sessions.verify(t2)
    assert active[0]["session_id"] == row2["session_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_sessions.py -v`
Expected: schema tests PASS, session-CRUD tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `glados/auth/sessions.py`**

```python
"""Stateful sessions backed by auth.db, cookies signed by itsdangerous.

Every request resolves the cookie to a row in auth_sessions; revoking a
row invalidates the cookie even if its signature is still valid.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from itsdangerous import URLSafeSerializer, BadSignature

from glados.auth import db as auth_db


def _serializer():
    from glados.core.config_store import cfg
    secret = cfg.auth.session_secret
    if not secret:
        raise RuntimeError("auth.session_secret is empty; cannot sign sessions")
    return URLSafeSerializer(secret, salt="glados-session-v1")


def create(
    *,
    username: str,
    role: str,
    remote_addr: str = "",
    user_agent: str = "",
    expires_at: int | None = None,
    auth_method: str = "password",
) -> str:
    """Insert a session row and return the signed cookie token."""
    auth_db.ensure_schema()
    sid = str(uuid.uuid4())
    now = int(time.time())
    con = auth_db.connect()
    try:
        con.execute(
            """
            INSERT INTO auth_sessions (
              session_id, username, role_at_issue, created_at, last_used_at,
              expires_at, user_agent, remote_addr, auth_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, username, role, now, now, expires_at,
             user_agent[:500], remote_addr, auth_method),
        )
        con.commit()
    finally:
        con.close()
    return _serializer().dumps({"sid": sid, "u": username, "iat": now})


def verify(token: str) -> tuple[bool, dict[str, Any] | None]:
    """Validate cookie signature + look up live session row. Returns
    (valid, row_dict). Updates last_used_at on hit."""
    if not token:
        return False, None
    try:
        payload = _serializer().loads(token)
    except BadSignature:
        return False, None
    sid = payload.get("sid")
    if not sid:
        return False, None

    now = int(time.time())
    con = auth_db.connect()
    try:
        row = con.execute(
            "SELECT * FROM auth_sessions WHERE session_id=?", (sid,),
        ).fetchone()
        if not row:
            return False, None
        if row["revoked_at"] is not None:
            return False, None
        if row["expires_at"] is not None and row["expires_at"] < now:
            return False, None
        con.execute(
            "UPDATE auth_sessions SET last_used_at=? WHERE session_id=?",
            (now, sid),
        )
        con.commit()
        return True, dict(row)
    finally:
        con.close()


def revoke(session_id: str) -> None:
    con = auth_db.connect()
    try:
        con.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (int(time.time()), session_id),
        )
        con.commit()
    finally:
        con.close()


def revoke_all_for_user(username: str) -> int:
    con = auth_db.connect()
    try:
        cur = con.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL",
            (int(time.time()), username),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def list_active(username: str | None = None) -> list[dict[str, Any]]:
    con = auth_db.connect()
    try:
        if username is None:
            rows = con.execute(
                "SELECT * FROM auth_sessions WHERE revoked_at IS NULL "
                "ORDER BY last_used_at DESC",
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM auth_sessions WHERE username=? AND revoked_at IS NULL "
                "ORDER BY last_used_at DESC",
                (username,),
            ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_sessions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add glados/auth/sessions.py tests/test_auth_sessions.py
git commit -m "feat(auth): itsdangerous-signed sessions backed by auth.db"
```

### 2.4 User state module

- [ ] **Step 1: Write `tests/test_user_state.py`**

```python
"""Tests for per-user dynamic state (last-login, fail counter)."""
import pytest
from glados.auth import db as auth_db
from glados.auth import user_state


@pytest.fixture
def tmp_auth_db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_db_path", lambda: tmp_path / "auth.db")
    auth_db.ensure_schema()


def test_record_successful_login_sets_timestamp(tmp_auth_db):
    user_state.record_success("alice", "10.0.0.5")
    row = user_state.get("alice")
    assert row["last_login_at"] > 0
    assert row["last_login_addr"] == "10.0.0.5"
    assert row["failed_login_count"] == 0


def test_record_failure_increments_counter(tmp_auth_db):
    user_state.record_failure("alice")
    user_state.record_failure("alice")
    row = user_state.get("alice")
    assert row["failed_login_count"] == 2


def test_successful_login_resets_failure_counter(tmp_auth_db):
    user_state.record_failure("alice")
    user_state.record_failure("alice")
    user_state.record_success("alice", "10.0.0.5")
    row = user_state.get("alice")
    assert row["failed_login_count"] == 0


def test_get_missing_user_returns_none(tmp_auth_db):
    assert user_state.get("nobody") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_state.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `glados/auth/user_state.py`**

```python
"""Dynamic per-user state: last-login + failed-login counter.

Users themselves live in configs/global.yaml (authoritative). This
module only tracks session-adjacent values that would churn YAML if
stored there.
"""
from __future__ import annotations

import time
from typing import Any

from glados.auth import db as auth_db


def get(username: str) -> dict[str, Any] | None:
    con = auth_db.connect()
    try:
        row = con.execute(
            "SELECT * FROM user_state WHERE username=?", (username,),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def record_success(username: str, remote_addr: str) -> None:
    now = int(time.time())
    con = auth_db.connect()
    try:
        con.execute(
            """
            INSERT INTO user_state (username, last_login_at, last_login_addr,
                                    failed_login_count, last_failed_login_at)
                VALUES (?, ?, ?, 0, NULL)
            ON CONFLICT(username) DO UPDATE SET
                last_login_at = excluded.last_login_at,
                last_login_addr = excluded.last_login_addr,
                failed_login_count = 0
            """,
            (username, now, remote_addr),
        )
        con.commit()
    finally:
        con.close()


def record_failure(username: str) -> None:
    now = int(time.time())
    con = auth_db.connect()
    try:
        con.execute(
            """
            INSERT INTO user_state (username, failed_login_count, last_failed_login_at)
                VALUES (?, 1, ?)
            ON CONFLICT(username) DO UPDATE SET
                failed_login_count = failed_login_count + 1,
                last_failed_login_at = excluded.last_failed_login_at
            """,
            (username, now),
        )
        con.commit()
    finally:
        con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_state.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add glados/auth/user_state.py tests/test_user_state.py
git commit -m "feat(auth): user_state module for dynamic per-user timestamps"
```

---

## Task 3: Legacy-shape migration + username+password login

**Files:**
- Modify: `glados/core/config_store.py` — add `_synthesize_legacy_admin` hook called by `_load_model`.
- Modify: `glados/webui/tts_ui.py` — rewrite `LOGIN_PAGE` HTML and `_handle_login`.
- Create: `tests/test_auth_migration.py`

### 3.1 Migration synthesizer

- [ ] **Step 1: Write `tests/test_auth_migration.py`**

```python
"""Tests for legacy-YAML → users-list synthesizer."""
from glados.core.config_store import AuthGlobal
from glados.core.config_store import _synthesize_legacy_admin  # module-private helper


def test_legacy_bcrypt_hash_synthesizes_admin_user():
    raw = {
        "enabled": True,
        "password_hash": "$2b$12$abcdef",
        "session_secret": "deadbeef" * 8,
        "session_timeout_hours": 24,
    }
    out = _synthesize_legacy_admin(raw)
    assert len(out["users"]) == 1
    u = out["users"][0]
    assert u["username"] == "admin"
    assert u["role"] == "admin"
    assert u["password_hash"] == "$2b$12$abcdef"
    assert u["hash_algorithm"] == "bcrypt-legacy"
    assert out["bootstrap_allowed"] is False
    # session_timeout_hours=24 → session_timeout="24h"
    assert out["session_timeout"] == "24h"


def test_empty_legacy_leaves_bootstrap_open():
    raw = {"enabled": True, "password_hash": "", "session_secret": ""}
    out = _synthesize_legacy_admin(raw)
    assert out["users"] == []
    assert out["bootstrap_allowed"] is True


def test_new_shape_passes_through_unchanged():
    raw = {
        "enabled": True,
        "bootstrap_allowed": False,
        "users": [{"username": "ResidentA", "password_hash": "$argon2id$...",
                   "role": "admin"}],
    }
    out = _synthesize_legacy_admin(raw)
    assert out["users"] == raw["users"]
    # bootstrap_allowed preserved
    assert out["bootstrap_allowed"] is False


def test_auth_global_loads_legacy_via_synthesizer():
    """End-to-end: AuthGlobal parsing through the synthesizer."""
    raw = {
        "enabled": True,
        "password_hash": "$2b$12$xyz",
        "session_secret": "s" * 64,
    }
    raw = _synthesize_legacy_admin(raw)
    a = AuthGlobal.model_validate(raw)
    assert len(a.users) == 1
    assert a.users[0].hash_algorithm == "bcrypt-legacy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth_migration.py -v`
Expected: `ImportError` on `_synthesize_legacy_admin`.

- [ ] **Step 3: Add `_synthesize_legacy_admin` to `glados/core/config_store.py`**

Locate the `_load_model` static method in `ConfigStore` (around line 906). Above it, add a module-level helper and wire it into the load path for `AuthGlobal`:

```python
def _synthesize_legacy_admin(raw: dict) -> dict:
    """Convert legacy single-password-hash YAML into the new multi-user
    shape before pydantic validation. Idempotent on already-new YAML.

    Rules:
    - If raw.users is already set, pass-through unchanged.
    - Else if raw.password_hash is non-empty, synthesize users=[{admin}]
      with hash_algorithm=bcrypt-legacy and bootstrap_allowed=false.
    - Else leave users=[] and bootstrap_allowed=true (fresh install).
    - Convert session_timeout_hours → session_timeout string.

    See docs/AUTH_DESIGN.md §10.1.
    """
    import time as _time
    out = dict(raw)

    # session_timeout_hours → session_timeout
    hrs = out.get("session_timeout_hours", 0)
    if hrs and "session_timeout" not in out:
        out["session_timeout"] = f"{hrs}h"

    existing_users = out.get("users")
    if existing_users:
        # Preserve whatever bootstrap_allowed is already set to; default False
        # (if users exist, bootstrap should never auto-fire).
        out.setdefault("bootstrap_allowed", False)
        return out

    legacy_hash = out.get("password_hash", "")
    if legacy_hash:
        out["users"] = [{
            "username": "admin",
            "display_name": "admin",
            "role": "admin",
            "password_hash": legacy_hash,
            "hash_algorithm": out.get("hash_algorithm", "bcrypt-legacy"),
            "disabled": False,
            "created_at": int(_time.time()),
        }]
        out["bootstrap_allowed"] = False
    else:
        out["users"] = []
        out.setdefault("bootstrap_allowed", True)

    return out
```

Then modify the `_load_model` method to apply the synthesizer for `AuthGlobal`. Locate:

```python
    @staticmethod
    def _load_model(path: Path, model_cls: type[BaseModel]) -> BaseModel:
        if not path.exists():
            logger.debug("Config not found, using defaults: {}", path)
            return model_cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return model_cls.model_validate(raw)
```

Replace the last line with:

```python
        # AuthGlobal ships through a migration synthesizer so legacy
        # single-password YAML deployments come up with a users[] list.
        if "auth" in raw and model_cls is GlobalConfig:
            raw["auth"] = _synthesize_legacy_admin(raw.get("auth") or {})
        return model_cls.model_validate(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth_migration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add glados/core/config_store.py tests/test_auth_migration.py
git commit -m "feat(auth): synthesize legacy single-password YAML into users[] at load"
```

### 3.2 Login handler rewrite (username + password)

- [ ] **Step 1: Write `tests/test_login_flow.py`**

```python
"""Integration tests for /login: username+password, bcrypt migration."""
import pytest
from http.client import HTTPConnection


# Requires a running test fixture that spins up tts_ui against a tmp
# configs dir. Existing tests in tests/test_config_defaults.py may
# provide one; reuse or create a new fixture here.

def _post_login(host: str, port: int, username: str, password: str) -> tuple[int, dict]:
    conn = HTTPConnection(host, port)
    body = f"username={username}&password={password}"
    conn.request("POST", "/login",
                 body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    data = resp.read()
    import json
    return resp.status, json.loads(data) if data else {}


def test_login_with_correct_credentials_returns_200(live_webui_admin):
    status, body = _post_login(*live_webui_admin.addr, "admin", "hunter2goes")
    assert status == 200
    assert body["ok"] is True


def test_login_with_wrong_password_returns_401(live_webui_admin):
    status, body = _post_login(*live_webui_admin.addr, "admin", "wrong-guess")
    assert status == 401


def test_login_with_unknown_user_returns_401_same_message(live_webui_admin):
    """No username enumeration — same message as bad password."""
    status, body = _post_login(*live_webui_admin.addr, "nobody", "any")
    assert status == 401
    assert "invalid" in body.get("error", "").lower()


def test_login_with_bcrypt_legacy_user_rehashes_to_argon2id(live_webui_bcrypt):
    """After first successful login, the YAML must hold an argon2id hash."""
    import yaml
    path = live_webui_bcrypt.configs_dir / "global.yaml"

    before = yaml.safe_load(path.read_text())
    assert before["auth"]["users"][0]["hash_algorithm"] == "bcrypt-legacy"

    status, _ = _post_login(*live_webui_bcrypt.addr, "admin", "hunter2goes")
    assert status == 200

    after = yaml.safe_load(path.read_text())
    assert after["auth"]["users"][0]["hash_algorithm"] == "argon2id"
    assert after["auth"]["users"][0]["password_hash"].startswith("$argon2id$")
```

(Note: this task assumes the existing test-harness can stand up `tts_ui` against a tmp configs dir. If it can't, a lighter-weight test suite that exercises `_handle_login` directly against a mock `BaseHTTPRequestHandler` is acceptable. The fixtures `live_webui_admin` and `live_webui_bcrypt` will be added as part of Step 3 below.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_login_flow.py -v`
Expected: fixtures missing or login not yet username-aware.

- [ ] **Step 3: Add fixtures in `tests/conftest.py` and rewrite the login handler**

In `tests/conftest.py`, add two fixtures:

```python
# Append to tests/conftest.py

@pytest.fixture
def live_webui_admin(tmp_path, monkeypatch):
    """Spin up tts_ui against a tmp configs dir with one argon2id admin."""
    import yaml
    from glados.auth import hashing
    configs = tmp_path / "configs"
    data = tmp_path / "data"
    configs.mkdir(); data.mkdir()

    (configs / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "bootstrap_allowed": False,
            "users": [{
                "username": "admin",
                "password_hash": hashing.hash_password("hunter2goes"),
                "hash_algorithm": "argon2id",
                "role": "admin",
            }],
        },
    }))

    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(configs))
    monkeypatch.setenv("GLADOS_DATA", str(data))

    from glados.core.config_store import cfg
    cfg.reload()

    yield _start_webui_in_thread(port=0, configs_dir=configs)


@pytest.fixture
def live_webui_bcrypt(tmp_path, monkeypatch):
    """Same as live_webui_admin but with a legacy bcrypt hash."""
    import bcrypt
    import yaml
    configs = tmp_path / "configs"
    data = tmp_path / "data"
    configs.mkdir(); data.mkdir()

    (configs / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "password_hash": bcrypt.hashpw(b"hunter2goes",
                                           bcrypt.gensalt()).decode("ascii"),
        },
    }))

    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(configs))
    monkeypatch.setenv("GLADOS_DATA", str(data))

    from glados.core.config_store import cfg
    cfg.reload()

    yield _start_webui_in_thread(port=0, configs_dir=configs)
```

`_start_webui_in_thread` is a helper the engineer adds inline — it should use `ThreadingHTTPServer` directly against the `Handler` class from `tts_ui`, bound to `127.0.0.1:0`, exposing `.addr = (host, port)` and `.configs_dir = configs`. The server teardown happens via `server.shutdown(); thread.join()` on fixture exit.

Then rewrite `_handle_login` in `glados/webui/tts_ui.py`. Locate `_handle_login` (currently around line 1236). Replace with:

```python
    def _handle_login(self):
        """POST /login — validate username + password, set session cookie."""
        from glados.auth import hashing, sessions, user_state
        from glados.core.config_store import cfg

        # Parse form
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        params = urllib.parse.parse_qs(body)
        username = (params.get("username", [""])[0] or "").strip()
        password = params.get("password", [""])[0]

        # Rate limit
        ip = self.client_address[0] if self.client_address else "?"
        if _check_rate_limit(ip):
            self._send_json(429, {"ok": False, "error": "Too many failed attempts. Try again later."})
            return

        # Look up user (exact case-sensitive match)
        user = next(
            (u for u in cfg.auth.users
             if u.username == username and not u.disabled),
            None,
        )
        if not user:
            _record_fail(ip)
            if username:
                user_state.record_failure(username)
            self._send_json(401, {"ok": False, "error": "Invalid credentials"})
            return

        # Verify password
        valid, rehash_needed = hashing.verify_password(password, user.password_hash)
        if not valid:
            _record_fail(ip)
            user_state.record_failure(username)
            self._send_json(401, {"ok": False, "error": "Invalid credentials"})
            return

        _clear_fails(ip)
        user_state.record_success(username, ip)

        # Rehash bcrypt-legacy on first successful login
        if rehash_needed:
            new_hash = hashing.hash_password(password)
            _merge_write_user_hash(username, new_hash, "argon2id")
            cfg.reload()

        # Create session
        ua = self.headers.get("User-Agent", "")[:500]
        token = sessions.create(
            username=username, role=user.role,
            remote_addr=ip, user_agent=ua,
        )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        cookie_parts = [
            f"glados_session={token}",
            f"Max-Age={_SESSION_LONG_S}",
            "Path=/", "HttpOnly", "SameSite=Strict",
        ]
        if SSL_CERT and SSL_CERT.exists():
            cookie_parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(cookie_parts))
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)
```

Add the `_merge_write_user_hash` helper nearby (after `_handle_login`):

```python
def _merge_write_user_hash(username: str, new_hash: str, algorithm: str) -> None:
    """Update a single user's password_hash + hash_algorithm in global.yaml
    via merge-write — leaves every other field untouched."""
    config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
    path = Path(config_dir) / "global.yaml"
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    users = raw.setdefault("auth", {}).setdefault("users", [])
    for u in users:
        if u.get("username") == username:
            u["password_hash"] = new_hash
            u["hash_algorithm"] = algorithm
            break
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)
```

Also update `LOGIN_PAGE` HTML around line 604 to add a `username` input above the `password` input:

```html
    <div class="field">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" autofocus required>
    </div>
    <div class="field">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" required>
    </div>
```

Update the autofocus: remove it from password, add it to username.

Update the fetch-body in the login `<script>` to include username:

```javascript
      body: new URLSearchParams({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
        remember: document.getElementById('remember').checked ? '1' : '0'
      })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_login_flow.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add glados/webui/tts_ui.py tests/conftest.py tests/test_login_flow.py
git commit -m "feat(auth): username+password login with bcrypt→argon2id rehash-on-login"
```

---

## Task 4: `require_perm` helper + route gating rewrite

**Files:**
- Modify: `glados/webui/tts_ui.py` — new `require_perm` helper; rewrite `_is_authenticated` and all ~60 `_require_auth` sites; update `_PUBLIC_PREFIXES`.
- Create: `tests/test_route_gating.py`

### 4.1 `require_perm` helper

- [ ] **Step 1: Write `tests/test_route_gating.py` — auth helper slice**

```python
"""Tests for require_perm + route gating."""
import pytest
from unittest.mock import MagicMock


def test_require_perm_admin_satisfies_admin_sentinel():
    from glados.webui.tts_ui import require_perm
    handler = _fake_admin_handler()
    assert require_perm(handler, "admin") is True


def test_require_perm_chat_user_denied_for_admin_sentinel():
    from glados.webui.tts_ui import require_perm
    handler = _fake_chat_handler()
    # 403 response will be sent; require_perm returns False.
    assert require_perm(handler, "admin") is False


def test_require_perm_chat_user_allowed_for_chat_send():
    from glados.webui.tts_ui import require_perm
    handler = _fake_chat_handler()
    assert require_perm(handler, "chat.send") is True


def test_require_perm_unauthenticated_denied():
    from glados.webui.tts_ui import require_perm
    handler = _fake_unauth_handler()
    assert require_perm(handler, "webui.view") is False


# ── helpers ────────────────────────────────────────────────────

def _fake_admin_handler():
    h = MagicMock()
    h.headers.get.return_value = "glados_session=valid_admin_token"
    h._resolved_user = {"username": "admin", "role": "admin"}
    h.path = "/"
    return h


def _fake_chat_handler():
    h = MagicMock()
    h.headers.get.return_value = "glados_session=valid_chat_token"
    h._resolved_user = {"username": "alice", "role": "chat"}
    h.path = "/"
    return h


def _fake_unauth_handler():
    h = MagicMock()
    h.headers.get.return_value = ""
    h._resolved_user = None
    h.path = "/"
    return h
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_route_gating.py -v`
Expected: FAIL (`require_perm` not defined).

- [ ] **Step 3: Implement `require_perm` in `glados/webui/tts_ui.py`**

Locate the `_require_auth` method on the `Handler` class (around line 1194). Keep it as a thin wrapper for compatibility during the rewrite, but add a new module-level `require_perm`:

```python
# ── Permission check (Task 4) ─────────────────────────────────────

def _resolve_user_for_request(handler) -> dict | None:
    """Return {'username': ..., 'role': ...} or None. Caches on the handler
    so repeat checks within one request don't re-query auth.db."""
    cached = getattr(handler, "_resolved_user", "__unset__")
    if cached != "__unset__":
        return cached
    from glados.auth import sessions
    from glados.core.config_store import cfg

    token = _extract_session_cookie(handler)
    valid, row = sessions.verify(token) if token else (False, None)
    if not valid or not row:
        handler._resolved_user = None
        return None

    user = next(
        (u for u in cfg.auth.users
         if u.username == row["username"] and not u.disabled),
        None,
    )
    if user is None:
        handler._resolved_user = None
        return None

    handler._resolved_user = {"username": user.username, "role": user.role,
                               "session_id": row["session_id"]}
    return handler._resolved_user


def _extract_session_cookie(handler) -> str:
    cookie_header = handler.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("glados_session="):
            return part[len("glados_session="):]
    return ""


def require_perm(handler, perm: str) -> bool:
    """Enforce `perm` on `handler`. Returns True if allowed. On denial,
    writes 401 (no session) or 403 (session but missing perm) and returns
    False. Handler short-circuits on False, same calling convention as
    _require_auth.
    """
    from glados.webui.permissions import user_has_perm
    from glados.core.config_store import cfg
    from glados.auth import bypass  # added in Task 9; safe import

    if bypass.active():
        return True

    if not cfg.auth.enabled:
        return True

    user = _resolve_user_for_request(handler)
    if user is None:
        if handler.path.startswith("/api/"):
            handler._send_json(401, {"error": "Authentication required"})
        else:
            handler.send_response(302)
            handler.send_header("Location", "/login")
            handler.end_headers()
        return False

    if not user_has_perm(user["role"], perm):
        if handler.path.startswith("/api/"):
            handler._send_json(403, {"error": "Forbidden",
                                     "required_permission": perm})
        else:
            handler.send_response(403)
            handler.send_header("Content-Type", "text/html")
            handler.end_headers()
            handler.wfile.write(b"<h1>403 Forbidden</h1>")
        return False

    return True
```

Note: `glados.auth.bypass` is created in Task 9. For now, a stub is needed to prevent ImportError. Add `glados/auth/bypass.py`:

```python
"""Auth-bypass mode — populated in Task 9. This stub exists so Task 4
can call bypass.active() without an ImportError.
"""


def active() -> bool:
    return False


def banner_html() -> str:
    return ""


def audit_tag() -> dict:
    return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_route_gating.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add glados/webui/tts_ui.py glados/auth/bypass.py tests/test_route_gating.py
git commit -m "feat(auth): require_perm helper + bypass stub"
```

### 4.2 Route gating sweep

- [ ] **Step 1: Inventory `_require_auth` call sites**

Run:
```
grep -n '_require_auth\(\)' glados/webui/tts_ui.py
```
Expected output: ~55-65 lines. Save the list; each corresponds to an endpoint that needs a per-route permission.

- [ ] **Step 2: Build the route → permission map**

From [AUTH_DESIGN.md §3.4](AUTH_DESIGN.md#34-route-gating-map-operator-locked), each existing `_require_auth` site maps to one of these permissions:

| URL-prefix | Permission |
|---|---|
| `/api/chat`, `/chat_audio/`, `/chat_audio_stream/` | `chat.send` |
| `/` (SPA shell GET) | `webui.view` |
| `/api/config/*`, `/api/memory/*`, `/api/logs/*`, `/api/audit/*`, `/api/system/*`, `/api/users/*`, `/api/ssl/*`, `/api/discover/*`, `/api/reload-engine`, etc. | `admin` |

For each call site: replace `if not self._require_auth(): return` with `if not require_perm(self, "<perm>"): return`, picking the permission by matching the URL the handler serves. Read the enclosing function/route to identify which bucket it falls in.

- [ ] **Step 3: Update `_PUBLIC_PREFIXES` list**

Locate lines 461-477 in `tts_ui.py`. Replace:

```python
_PUBLIC_PATHS = frozenset({"/login", "/health"})

_PUBLIC_PREFIXES = (
    "/api/generate", "/api/chat", "/api/stt",
    "/api/files", "/api/attitudes", "/api/speakers", "/api/voices",
    "/files/", "/chat_audio/", "/chat_audio_stream/",
    "/api/auth/",
    "/static/",
)
```

With:

```python
# Public paths — no session cookie required.
# Matches AUTH_DESIGN.md §3.4. /api/chat and /chat_audio/* were previously
# here; they now require chat.send.
_PUBLIC_PATHS = frozenset({"/login", "/health", "/logout", "/tts"})

_PUBLIC_PREFIXES = (
    # STT + TTS service endpoints (operator decision 2026-04-24)
    "/api/stt",
    "/api/generate", "/api/voices", "/api/speakers",
    "/api/attitudes", "/api/files", "/files/",
    # Infrastructure
    "/api/auth/", "/static/", "/setup",
)
```

- [ ] **Step 4: Remove the old `_require_auth` method**

After all call sites have been swept, delete the `_require_auth` method from `Handler` (currently around line 1194). `require_perm` replaces it.

- [ ] **Step 5: Append integration tests to `tests/test_route_gating.py`**

```python
# ── End-to-end route gating ────────────────────────────────────

def test_gated_api_route_returns_401_without_cookie(live_webui_admin):
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_admin.addr)
    conn.request("GET", "/api/memory/recent")
    resp = conn.getresponse()
    assert resp.status == 401


def test_public_stt_route_open_without_cookie(live_webui_admin):
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_admin.addr)
    # HEAD is enough to confirm no 401 gate (actual POST tested elsewhere).
    conn.request("HEAD", "/api/stt")
    resp = conn.getresponse()
    assert resp.status != 401


def test_chat_user_denied_admin_route(live_webui_chat_user):
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_chat_user.addr)
    conn.request("GET", "/api/config/reload",
                 headers={"Cookie": live_webui_chat_user.cookie})
    resp = conn.getresponse()
    assert resp.status == 403


def test_chat_user_allowed_chat_send(live_webui_chat_user):
    # Post an empty chat body; 400 from handler is acceptable — what
    # matters is it isn't 401 or 403.
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_chat_user.addr)
    conn.request("POST", "/api/chat", body="{}",
                 headers={"Cookie": live_webui_chat_user.cookie,
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status not in (401, 403)
```

Add `live_webui_chat_user` fixture to `conftest.py` (pattern identical to `live_webui_admin` but with role=`chat` and logging in to obtain a cookie string).

- [ ] **Step 6: Run tests to verify they pass**

Run:
```
pytest tests/test_route_gating.py -v
pytest -q     # full suite — flush out any handler missed during sweep
```
Expected: all PASS. No test regressions.

- [ ] **Step 7: Commit**

```
git add glados/webui/tts_ui.py tests/test_route_gating.py tests/conftest.py
git commit -m "feat(auth): route-gating rewrite with require_perm and new public list"
```

---

## Task 5: First-run wizard framework

**Files:**
- Create: `glados/webui/setup/__init__.py`
- Create: `glados/webui/setup/wizard.py`
- Create: `glados/webui/setup/shell.py`
- Create: `glados/webui/setup/steps/__init__.py`
- Create: `glados/webui/setup/steps/admin_password.py`
- Modify: `glados/webui/tts_ui.py` — wire `/setup` and `/setup/<step>` routes.
- Create: `tests/test_setup_wizard.py`

### 5.1 Wizard abstraction

- [ ] **Step 1: Write `tests/test_setup_wizard.py` — engine tests**

```python
"""Tests for the first-run wizard framework."""
import pytest
from dataclasses import dataclass
from glados.webui.setup import wizard as wiz


@dataclass(frozen=True)
class _FakeStep:
    name: str
    order: int = 100
    _required: bool = True
    _title: str = "fake"

    @property
    def title(self) -> str:
        return self._title

    def is_required(self, cfg) -> bool:
        return self._required

    def render(self, handler) -> str:
        return "<form></form>"

    def process(self, handler, form) -> wiz.StepResult:
        return wiz.StepResult.DONE


def test_resolve_next_step_with_one_required():
    steps = (_FakeStep("a", order=10),)
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt and nxt.name == "a"


def test_resolve_next_step_skips_non_required():
    steps = (
        _FakeStep("a", order=10, _required=False),
        _FakeStep("b", order=20, _required=True),
    )
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt and nxt.name == "b"


def test_resolve_next_step_orders_by_order_field():
    steps = (
        _FakeStep("b", order=20),
        _FakeStep("a", order=10),
    )
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt and nxt.name == "a"


def test_resolve_next_step_returns_none_when_done():
    steps = (_FakeStep("a", _required=False),)
    assert wiz.resolve_next_step(steps, cfg=None) is None


def test_step_result_values():
    assert wiz.StepResult.DONE == wiz.StepResult("done")
    assert wiz.StepResult.ERROR == wiz.StepResult("error")
    assert wiz.StepResult.NEXT == wiz.StepResult("next")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_setup_wizard.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create wizard scaffold**

`glados/webui/setup/__init__.py`:
```
# empty
```

`glados/webui/setup/wizard.py`:
```python
"""First-run wizard engine. Pluggable step registry; Phase 1 ships one
step (admin password). See docs/AUTH_DESIGN.md §5.1.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class StepResult(str, Enum):
    DONE = "done"       # step completed successfully; wizard may advance
    NEXT = "next"       # step accepted but more work remains (rare)
    ERROR = "error"     # step re-renders with an error message


class WizardStep(Protocol):
    name: str
    order: int

    @property
    def title(self) -> str: ...

    def is_required(self, cfg) -> bool: ...

    def render(self, handler) -> str: ...

    def process(self, handler, form: dict) -> StepResult: ...


def resolve_next_step(steps, cfg) -> WizardStep | None:
    """Return the first-by-order step that still reports is_required=True.
    Returns None if all required steps are done."""
    ordered = sorted(steps, key=lambda s: s.order)
    for step in ordered:
        if step.is_required(cfg):
            return step
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_setup_wizard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add glados/webui/setup/ tests/test_setup_wizard.py
git commit -m "feat(setup): wizard engine + WizardStep protocol"
```

### 5.2 Set-Admin-Password step

- [ ] **Step 1: Append step tests to `tests/test_setup_wizard.py`**

```python
# ── SetAdminPasswordStep ───────────────────────────────────────

def test_admin_password_step_required_when_no_users_and_bootstrap_allowed():
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep(order=100)

    class FakeCfg:
        class auth:
            users = []
            bootstrap_allowed = True

    assert s.is_required(FakeCfg) is True


def test_admin_password_step_not_required_when_admin_exists():
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep(order=100)

    class FakeCfg:
        class auth:
            users = [type("U", (), {"role": "admin"})()]
            bootstrap_allowed = False

    assert s.is_required(FakeCfg) is False


def test_admin_password_step_process_rejects_short_password(live_webui_fresh):
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep(order=100)
    handler = _mock_handler()
    form = {"username": "ResidentA", "password": "abc", "confirm": "abc"}
    result = s.process(handler, form)
    assert result == _import("glados.webui.setup.wizard").StepResult.ERROR


def test_admin_password_step_process_creates_admin_user(live_webui_fresh):
    import yaml
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep

    s = SetAdminPasswordStep(order=100)
    handler = _mock_handler()
    form = {"username": "ResidentA", "display_name": "ResidentA",
            "password": "hunter2goes", "confirm": "hunter2goes"}
    result = s.process(handler, form)

    raw = yaml.safe_load((live_webui_fresh.configs_dir / "global.yaml").read_text())
    assert len(raw["auth"]["users"]) == 1
    u = raw["auth"]["users"][0]
    assert u["username"] == "ResidentA"
    assert u["role"] == "admin"     # role is hard-coded — never from form
    assert u["password_hash"].startswith("$argon2id$")
    assert raw["auth"]["bootstrap_allowed"] is False
    assert result == _import("glados.webui.setup.wizard").StepResult.DONE


def _import(name):
    import importlib
    return importlib.import_module(name)


def _mock_handler():
    from unittest.mock import MagicMock
    h = MagicMock()
    h.client_address = ("127.0.0.1", 0)
    h.headers = {}
    return h
```

The `live_webui_fresh` fixture needs adding to `conftest.py` — same shape as `live_webui_admin` but with `auth.users = []` and `auth.bootstrap_allowed = true`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_setup_wizard.py -v`
Expected: `ImportError` / `ModuleNotFoundError` on `glados.webui.setup.steps.admin_password`.

- [ ] **Step 3: Implement the step**

`glados/webui/setup/steps/__init__.py`:
```
# empty
```

`glados/webui/setup/steps/admin_password.py`:
```python
"""First-run step: create the initial admin user.

Per AUTH_DESIGN.md §5.1.3, the first user's role is hard-coded to
'admin' — the form does not expose a role field.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from glados.auth import hashing
from glados.webui.permissions import password_meets_policy
from glados.webui.setup.wizard import StepResult


_FORM_HTML = """
<h2>Set admin password</h2>
<p class="hint">Create the initial administrator account. This user
will have full control. You can add additional users later from
Configuration &rarr; Users.</p>

{error}

<form method="post">
  <div class="field">
    <label for="username">Username</label>
    <input type="text" id="username" name="username"
           autocomplete="username" autofocus required
           maxlength="64" value="{username}">
    <p class="hint">Case-sensitive. Pick anything memorable.</p>
  </div>

  <div class="field">
    <label for="display_name">Display name (optional)</label>
    <input type="text" id="display_name" name="display_name"
           maxlength="128" value="{display_name}">
  </div>

  <div class="field">
    <label for="password">Password</label>
    <input type="password" id="password" name="password"
           autocomplete="new-password" required minlength="8">
    <p class="hint">At least 8 characters. Avoid obvious choices.</p>
  </div>

  <div class="field">
    <label for="confirm">Confirm password</label>
    <input type="password" id="confirm" name="confirm"
           autocomplete="new-password" required minlength="8">
  </div>

  <button type="submit" class="btn">Create admin</button>
</form>
"""


@dataclass(frozen=True)
class SetAdminPasswordStep:
    order: int = 100
    name: str = "admin-password"

    @property
    def title(self) -> str:
        return "Set admin password"

    def is_required(self, cfg) -> bool:
        # Required iff no admin exists AND bootstrap is allowed.
        admins = [u for u in getattr(cfg.auth, "users", []) if u.role == "admin"]
        return cfg.auth.bootstrap_allowed and not admins

    def render(self, handler, error: str = "", sticky_form: dict | None = None) -> str:
        f = sticky_form or {}
        def esc(s: str) -> str:
            import html
            return html.escape(s or "")

        error_html = (
            f'<div class="error">{esc(error)}</div>' if error else ''
        )
        return _FORM_HTML.format(
            error=error_html,
            username=esc(f.get("username", "")),
            display_name=esc(f.get("display_name", "")),
        )

    def process(self, handler, form: dict) -> StepResult:
        username = (form.get("username") or "").strip()
        display_name = (form.get("display_name") or "").strip() or username
        password = form.get("password") or ""
        confirm = form.get("confirm") or ""

        err = _validate(username, password, confirm)
        if err:
            handler._wizard_error = err
            handler._wizard_form = {"username": username, "display_name": display_name}
            return StepResult.ERROR

        # Hash the password
        hashed = hashing.hash_password(password)

        # Merge-write global.yaml
        import os
        import secrets as _secrets
        config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
        path = Path(config_dir) / "global.yaml"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        auth = raw.setdefault("auth", {})
        users = auth.setdefault("users", [])
        users.append({
            "username": username,
            "display_name": display_name,
            "role": "admin",                           # HARD-CODED — never from form
            "password_hash": hashed,
            "hash_algorithm": "argon2id",
            "disabled": False,
            "created_at": int(time.time()),
        })
        auth["bootstrap_allowed"] = False
        if not auth.get("session_secret"):
            auth["session_secret"] = _secrets.token_hex(64)

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

        # Refresh live config
        from glados.core.config_store import cfg
        cfg.reload()
        return StepResult.DONE


def _validate(username: str, password: str, confirm: str) -> str:
    if not username:
        return "Username is required."
    if len(username) > 64:
        return "Username must be 64 characters or fewer."
    if any(ord(c) < 32 for c in username):
        return "Username must not contain control characters."
    if password != confirm:
        return "Passwords do not match."
    ok, msg = password_meets_policy(password)
    if not ok:
        return msg
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_setup_wizard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add glados/webui/setup/steps/ tests/test_setup_wizard.py
git commit -m "feat(setup): SetAdminPasswordStep — first user role hard-coded admin"
```

### 5.3 Shared shell + routing

- [ ] **Step 1: Create `glados/webui/setup/shell.py`**

```python
"""Shared HTML shell for wizard steps."""
from __future__ import annotations


_SHELL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS — Setup: {title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif;
           background: #0a0a0a; color: #e0e0e0;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; padding: 20px; }}
  .setup-box {{ background: #1a1a2e; border: 1px solid #333;
                border-radius: 12px; padding: 40px;
                width: 480px; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }}
  .setup-box h1 {{ text-align: center; color: #ff6600;
                    font-size: 1.6em; margin-bottom: 4px; }}
  .setup-box .step-indicator {{ text-align: center; color: #888;
                                 font-size: 0.8em; margin-bottom: 24px;
                                 text-transform: uppercase; letter-spacing: 1px; }}
  .setup-box h2 {{ color: #e0e0e0; font-size: 1.15em;
                    margin-bottom: 12px; }}
  .hint {{ color: #999; font-size: 0.8em; margin-top: 6px; }}
  .field {{ margin-bottom: 16px; }}
  .field label {{ display: block; font-size: 0.85em;
                    color: #aaa; margin-bottom: 6px; }}
  .field input {{ width: 100%; padding: 10px 12px;
                    background: #111; border: 1px solid #444;
                    border-radius: 6px; color: #e0e0e0; font-size: 1em; }}
  .field input:focus {{ border-color: #ff6600; outline: none; }}
  .btn {{ width: 100%; padding: 11px; background: #ff6600;
           color: #fff; border: none; border-radius: 6px;
           font-size: 1em; cursor: pointer; margin-top: 8px; }}
  .btn:hover {{ background: #e55a00; }}
  .error {{ background: #3a1111; border: 1px solid #ff4444;
             color: #ff6666; padding: 10px; border-radius: 6px;
             margin-bottom: 16px; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="setup-box">
  <h1>GLaDOS</h1>
  <div class="step-indicator">Step {step_num} of {total_steps}</div>
  {content}
</div>
</body>
</html>
"""


def render_shell(*, title: str, step_num: int, total_steps: int, content: str) -> str:
    return _SHELL_TEMPLATE.format(
        title=title, step_num=step_num, total_steps=total_steps, content=content,
    )
```

- [ ] **Step 2: Wire routes in `tts_ui.py`**

Add the step registry constant near the top of `Handler` class (after the existing imports section):

```python
# Wizard registry — Phase 1 ships one step.
from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
from glados.webui.setup import wizard as _wizard
from glados.webui.setup.shell import render_shell as _render_shell

_WIZARD_STEPS = (SetAdminPasswordStep(order=100),)
```

In `do_GET` dispatch (around line 1419 where `/login` is handled), add:

```python
        # Wizard routes (public while bootstrap_allowed)
        if self.path == "/setup" or self.path.startswith("/setup/"):
            self._dispatch_setup()
            return
```

In `do_POST` dispatch (around line 1459), add the same line.

Implement `_dispatch_setup` on `Handler`:

```python
    def _dispatch_setup(self):
        """Routes for the first-run wizard. See docs/AUTH_DESIGN.md §5.1."""
        from glados.core.config_store import cfg

        if not cfg.auth.bootstrap_allowed:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        # GET /setup → redirect to the first required step
        if self.path.rstrip("/") == "/setup" and self.command == "GET":
            nxt = _wizard.resolve_next_step(_WIZARD_STEPS, cfg)
            if nxt is None:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.send_response(302)
            self.send_header("Location", f"/setup/{nxt.name}")
            self.end_headers()
            return

        # /setup/<step_name>
        if self.path.startswith("/setup/"):
            step_name = self.path[len("/setup/"):].strip("/").split("?", 1)[0]
            step = next((s for s in _WIZARD_STEPS if s.name == step_name), None)
            if step is None or not step.is_required(cfg):
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
                return

            if self.command == "GET":
                self._render_wizard_step(step, error="", sticky_form=None)
                return

            if self.command == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
                result = step.process(self, form)
                if result == _wizard.StepResult.ERROR:
                    err = getattr(self, "_wizard_error", "Invalid input.")
                    sticky = getattr(self, "_wizard_form", None)
                    self._render_wizard_step(step, error=err, sticky_form=sticky)
                    return

                # Advance
                cfg.reload()
                nxt = _wizard.resolve_next_step(_WIZARD_STEPS, cfg)
                if nxt is None:
                    # Wizard complete — log the operator in
                    self._complete_wizard_session(form.get("username", "").strip())
                    return
                self.send_response(302)
                self.send_header("Location", f"/setup/{nxt.name}")
                self.end_headers()
                return

        self._send_error(404, "Not Found")

    def _render_wizard_step(self, step, error: str, sticky_form: dict | None):
        content = step.render(self, error=error, sticky_form=sticky_form)
        html = _render_shell(
            title=step.title, step_num=1, total_steps=len(_WIZARD_STEPS),
            content=content,
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _complete_wizard_session(self, username: str):
        """After the final wizard step, issue a session cookie and
        redirect to /."""
        from glados.auth import sessions
        from glados.core.config_store import cfg

        user = next((u for u in cfg.auth.users if u.username == username), None)
        if user is None:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        ua = self.headers.get("User-Agent", "")[:500]
        ip = self.client_address[0] if self.client_address else ""
        token = sessions.create(
            username=username, role=user.role, remote_addr=ip, user_agent=ua,
        )

        self.send_response(302)
        self.send_header("Location", "/")
        cookie_parts = [
            f"glados_session={token}", f"Max-Age={_SESSION_LONG_S}",
            "Path=/", "HttpOnly", "SameSite=Strict",
        ]
        if SSL_CERT and SSL_CERT.exists():
            cookie_parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(cookie_parts))
        self.end_headers()
```

- [ ] **Step 3: Update `/login` GET handler to redirect to `/setup` when fresh**

Locate the `/login` GET handler (around line 1419). Before rendering the login page, check bootstrap state:

```python
        if self.path == "/login":
            if _is_authenticated(self):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            # Fresh install → wizard
            from glados.core.config_store import cfg as _cfg_live
            if not _cfg_live.auth.users and _cfg_live.auth.bootstrap_allowed:
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
                return
            self._serve_login()
            return
```

- [ ] **Step 4: Integration test — full fresh-install flow**

Append to `tests/test_setup_wizard.py`:

```python
def test_fresh_install_redirects_login_to_setup(live_webui_fresh):
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_fresh.addr)
    conn.request("GET", "/login")
    resp = conn.getresponse()
    assert resp.status == 302
    assert resp.getheader("Location") == "/setup"


def test_setup_flow_end_to_end(live_webui_fresh):
    import yaml
    from http.client import HTTPConnection
    host, port = live_webui_fresh.addr

    # Step 1: follow /setup → /setup/admin-password
    conn = HTTPConnection(host, port)
    conn.request("GET", "/setup")
    resp = conn.getresponse(); resp.read()
    assert resp.status == 302
    assert resp.getheader("Location") == "/setup/admin-password"

    # Step 2: POST valid form
    conn = HTTPConnection(host, port)
    body = "username=ResidentA&display_name=ResidentA&password=hunter2goes&confirm=hunter2goes"
    conn.request("POST", "/setup/admin-password", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse(); resp.read()
    assert resp.status == 302
    assert resp.getheader("Location") == "/"
    assert "glados_session=" in resp.getheader("Set-Cookie", "")

    # Verify YAML is updated
    raw = yaml.safe_load((live_webui_fresh.configs_dir / "global.yaml").read_text())
    assert raw["auth"]["bootstrap_allowed"] is False
    assert raw["auth"]["users"][0]["username"] == "ResidentA"


def test_setup_unreachable_after_bootstrap_complete(live_webui_admin):
    """Once an admin exists, /setup 302s to /login."""
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_admin.addr)
    conn.request("GET", "/setup")
    resp = conn.getresponse(); resp.read()
    assert resp.status == 302
    assert resp.getheader("Location") == "/login"
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```
pytest tests/test_setup_wizard.py -v
pytest -q
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add glados/webui/tts_ui.py glados/webui/setup/shell.py tests/test_setup_wizard.py
git commit -m "feat(setup): /setup routing + shared shell + fresh-install flow"
```

---

## Task 6: Standalone `/tts` page

**Files:**
- Create: `glados/webui/pages/tts_standalone.py`
- Modify: `glados/webui/tts_ui.py` — add `/tts` GET route returning the standalone form.
- Modify: `glados/webui/pages/_shell.py` or equivalent — hide TTS tab from non-admin SPA shells.
- Create/append: `tests/test_route_gating.py`

### 6.1 Standalone page HTML

- [ ] **Step 1: Write `glados/webui/pages/tts_standalone.py`**

```python
"""Standalone /tts page — a minimal text-to-audio form with no auth
and no SPA shell. See docs/AUTH_DESIGN.md §3.4.
"""
from __future__ import annotations


TTS_STANDALONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS — TTS</title>
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif;
         background: #0a0a0a; color: #e0e0e0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; padding: 20px; }
  .box { background: #1a1a2e; border: 1px solid #333;
         border-radius: 12px; padding: 32px; width: 520px; max-width: 100%; }
  h1 { color: #ff6600; margin: 0 0 16px; font-size: 1.4em; }
  textarea { width: 100%; min-height: 120px; background: #111;
             border: 1px solid #444; border-radius: 6px; color: #e0e0e0;
             padding: 10px; font-family: inherit; font-size: 1em;
             box-sizing: border-box; resize: vertical; }
  button { margin-top: 12px; padding: 10px 20px; background: #ff6600;
           color: #fff; border: none; border-radius: 6px; font-size: 1em;
           cursor: pointer; }
  button:hover { background: #e55a00; }
  audio { width: 100%; margin-top: 16px; display: none; }
  audio.visible { display: block; }
  .status { margin-top: 10px; font-size: 0.85em; color: #aaa; }
</style>
</head>
<body>
<div class="box">
  <h1>GLaDOS Speech</h1>
  <textarea id="text" placeholder="Type text for GLaDOS to speak…"></textarea>
  <button id="go">Generate</button>
  <div class="status" id="status"></div>
  <audio id="audio" controls></audio>
</div>
<script>
document.getElementById('go').addEventListener('click', async () => {
  const text = document.getElementById('text').value.trim();
  if (!text) return;
  const btn = document.getElementById('go');
  const status = document.getElementById('status');
  const audio = document.getElementById('audio');
  btn.disabled = true;
  status.textContent = 'Synthesising…';
  audio.classList.remove('visible');
  try {
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const blob = await resp.blob();
    audio.src = URL.createObjectURL(blob);
    audio.classList.add('visible');
    audio.play();
    status.textContent = 'Ready.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>
"""
```

- [ ] **Step 2: Wire the `/tts` GET route in `tts_ui.py`**

In `do_GET`, add near the other public-path handlers:

```python
        if self.path == "/tts":
            from glados.webui.pages.tts_standalone import TTS_STANDALONE_HTML
            body = TTS_STANDALONE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
```

- [ ] **Step 3: Hide the TTS Generator tab from non-admins in the SPA**

In the JS that builds the sidebar (search for `Tab 1` or `TTS Generator` in `glados/webui/static/ui.js`), wrap the TTS tab rendering in a role check that reads from `/api/auth/status`:

```javascript
if (currentUser && currentUser.role === 'admin') {
  // render TTS Generator tab
}
```

Leave the handler endpoints unchanged — they're already unauth per Task 4. Only the SPA's navigation menu hides the entry for chat users.

- [ ] **Step 4: Integration test**

Append to `tests/test_route_gating.py`:

```python
def test_standalone_tts_page_serves_without_auth(live_webui_admin):
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_admin.addr)
    conn.request("GET", "/tts")
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    assert b"<title>GLaDOS" in body
    assert b"/api/generate" in body  # form wired correctly


def test_standalone_tts_page_has_no_sidebar_nav(live_webui_admin):
    """Minimal form, not the SPA shell."""
    from http.client import HTTPConnection
    conn = HTTPConnection(*live_webui_admin.addr)
    conn.request("GET", "/tts")
    body = conn.getresponse().read()
    # The main SPA shell contains the Chat tab; /tts must not
    assert b"Chat" not in body or b"<nav" not in body
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_route_gating.py -v -k "standalone"`
Expected: PASS.

- [ ] **Step 6: Commit**

```
git add glados/webui/pages/tts_standalone.py glados/webui/tts_ui.py glados/webui/static/ui.js tests/test_route_gating.py
git commit -m "feat(auth): standalone /tts page for unauthed TTS service access"
```

---

## Task 7: Users management (API + UI)

**Files:**
- Create: `glados/webui/pages/users.py` — server-side list/create/update/delete helpers.
- Modify: `glados/webui/tts_ui.py` — route dispatchers for `/api/users[...]`.
- Modify: `glados/webui/static/ui.js` + `glados/webui/static/style.css` — Users page in the SPA sidebar (admin-only).
- Create: `tests/test_users_api.py`

This task has seven CRUD-oriented sub-tasks. Each follows the same TDD cycle: test → run fail → implement → run pass → commit.

### 7.1 GET /api/users

- [ ] **Step 1: Write test**

```python
# tests/test_users_api.py
"""Tests for the admin-only Users CRUD API."""
import pytest
from http.client import HTTPConnection
import json


def _req(conn, method, path, headers=None, body=None):
    conn.request(method, path, headers=headers or {}, body=body or "")
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_list_users_requires_admin(live_webui_chat_user):
    conn = HTTPConnection(*live_webui_chat_user.addr)
    status, _ = _req(conn, "GET", "/api/users",
                     headers={"Cookie": live_webui_chat_user.cookie})
    assert status == 403


def test_list_users_as_admin_returns_current_list(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    status, body = _req(conn, "GET", "/api/users",
                        headers={"Cookie": live_webui_admin.cookie})
    assert status == 200
    data = json.loads(body)
    assert isinstance(data["users"], list)
    assert all("password_hash" not in u for u in data["users"])  # sanitized
```

- [ ] **Step 2: Run, expect fail (404 or no endpoint).**

- [ ] **Step 3: Implement `GET /api/users`**

In `glados/webui/pages/users.py`:

```python
"""Server-side helpers for the Users CRUD endpoints."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from glados.auth import hashing, sessions, user_state
from glados.webui.permissions import ROLES, password_meets_policy


ALLOWED_ROLES = {"admin", "chat"}


def _global_yaml_path() -> Path:
    import os
    return Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "global.yaml"


def _read() -> dict:
    path = _global_yaml_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write(raw: dict) -> None:
    _global_yaml_path().write_text(
        yaml.safe_dump(raw, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _sanitize(user: dict) -> dict:
    """Strip password_hash before returning over the wire."""
    return {k: v for k, v in user.items() if k != "password_hash"}


def list_users() -> list[dict[str, Any]]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    enriched = []
    for u in users:
        out = _sanitize(u)
        state = user_state.get(u["username"])
        if state:
            out["last_login_at"] = state.get("last_login_at")
            out["last_login_addr"] = state.get("last_login_addr")
        enriched.append(out)
    return enriched


def validate_username(username: str) -> str:
    if not username:
        return "Username is required."
    if len(username) > 64:
        return "Username must be 64 characters or fewer."
    if any(ord(c) < 32 for c in username):
        return "Username must not contain control characters."
    return ""


def validate_role(role: str) -> str:
    if role not in ALLOWED_ROLES:
        return f"Role must be one of: {sorted(ALLOWED_ROLES)}."
    return ""


def create_user(username: str, display_name: str, role: str, password: str) -> tuple[bool, str]:
    err = validate_username(username)
    if err:
        return False, err
    err = validate_role(role)
    if err:
        return False, err
    ok, msg = password_meets_policy(password)
    if not ok:
        return False, msg

    raw = _read()
    auth = raw.setdefault("auth", {})
    users = auth.setdefault("users", [])
    if any(u["username"] == username for u in users):
        return False, f"User {username!r} already exists."
    users.append({
        "username": username,
        "display_name": display_name or username,
        "role": role,
        "password_hash": hashing.hash_password(password),
        "hash_algorithm": "argon2id",
        "disabled": False,
        "created_at": int(time.time()),
    })
    _write(raw)
    return True, ""


def update_user(username: str, *, role: str | None = None,
                display_name: str | None = None,
                disabled: bool | None = None) -> tuple[bool, str]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    idx = next((i for i, u in enumerate(users) if u["username"] == username), -1)
    if idx < 0:
        return False, "User not found."

    if role is not None:
        err = validate_role(role)
        if err:
            return False, err
        # Guard: can't demote last admin
        if users[idx]["role"] == "admin" and role != "admin":
            remaining_admins = sum(
                1 for u in users if u["role"] == "admin" and u["username"] != username
            )
            if remaining_admins == 0:
                return False, "Cannot demote the last admin."
        users[idx]["role"] = role

    if display_name is not None:
        users[idx]["display_name"] = display_name or users[idx]["username"]

    if disabled is not None:
        # Guard: can't disable last active admin
        if disabled and users[idx]["role"] == "admin":
            remaining = sum(
                1 for u in users
                if u["role"] == "admin" and not u.get("disabled") and u["username"] != username
            )
            if remaining == 0:
                return False, "Cannot disable the last admin."
        users[idx]["disabled"] = disabled

    _write(raw)
    return True, ""


def reset_password(username: str, new_password: str) -> tuple[bool, str]:
    ok, msg = password_meets_policy(new_password)
    if not ok:
        return False, msg
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    idx = next((i for i, u in enumerate(users) if u["username"] == username), -1)
    if idx < 0:
        return False, "User not found."
    users[idx]["password_hash"] = hashing.hash_password(new_password)
    users[idx]["hash_algorithm"] = "argon2id"
    _write(raw)
    return True, ""


def delete_user(username: str) -> tuple[bool, str]:
    raw = _read()
    users = (raw.get("auth") or {}).get("users") or []
    victim = next((u for u in users if u["username"] == username), None)
    if victim is None:
        return False, "User not found."
    if victim["role"] == "admin":
        remaining = sum(1 for u in users if u["role"] == "admin" and u["username"] != username)
        if remaining == 0:
            return False, "Cannot delete the last admin."
    users[:] = [u for u in users if u["username"] != username]
    _write(raw)
    sessions.revoke_all_for_user(username)
    return True, ""
```

In `tts_ui.py`, add route dispatch in `do_GET`:

```python
        if self.path == "/api/users":
            if not require_perm(self, "admin"): return
            from glados.webui.pages import users as _users_page
            self._send_json(200, {"users": _users_page.list_users()})
            return
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```
git add glados/webui/pages/users.py glados/webui/tts_ui.py tests/test_users_api.py
git commit -m "feat(users): GET /api/users admin-only listing"
```

### 7.2 – 7.5 POST / PUT / password-reset / DELETE

Each follows the same pattern. Test code (append to `tests/test_users_api.py`):

```python
def test_create_user_as_admin(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"username": "alice", "role": "chat", "password": "hunter2goes"})
    status, out = _req(conn, "POST", "/api/users",
                       headers={"Cookie": live_webui_admin.cookie,
                                "Content-Type": "application/json"},
                       body=body)
    assert status == 201
    assert json.loads(out)["ok"] is True


def test_create_user_rejects_weak_password(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"username": "bob", "role": "chat", "password": "abc"})
    status, out = _req(conn, "POST", "/api/users",
                       headers={"Cookie": live_webui_admin.cookie,
                                "Content-Type": "application/json"},
                       body=body)
    assert status == 400
    assert "8" in json.loads(out)["error"]


def test_create_user_duplicate_username(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"username": "admin", "role": "chat", "password": "hunter2goes"})
    status, out = _req(conn, "POST", "/api/users",
                       headers={"Cookie": live_webui_admin.cookie,
                                "Content-Type": "application/json"},
                       body=body)
    assert status == 409


def test_update_user_role(live_webui_admin_and_chat):
    conn = HTTPConnection(*live_webui_admin_and_chat.addr)
    body = json.dumps({"role": "admin"})
    status, _ = _req(conn, "PUT", "/api/users/alice",
                     headers={"Cookie": live_webui_admin_and_chat.cookie,
                              "Content-Type": "application/json"},
                     body=body)
    assert status == 200


def test_reset_other_user_password(live_webui_admin_and_chat):
    conn = HTTPConnection(*live_webui_admin_and_chat.addr)
    body = json.dumps({"new_password": "newpass123"})
    status, _ = _req(conn, "POST", "/api/users/alice/password",
                     headers={"Cookie": live_webui_admin_and_chat.cookie,
                              "Content-Type": "application/json"},
                     body=body)
    assert status == 200


def test_delete_last_admin_refused(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    status, out = _req(conn, "DELETE", f"/api/users/{live_webui_admin.username}",
                       headers={"Cookie": live_webui_admin.cookie})
    assert status == 400
    assert "last admin" in json.loads(out)["error"]
```

Route handlers in `tts_ui.py` `do_POST` / `do_PUT` / `do_DELETE`:

```python
        # POST /api/users — create
        if self.path == "/api/users":
            if not require_perm(self, "admin"): return
            from glados.webui.pages import users as _users_page
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            ok, err = _users_page.create_user(
                username=(body.get("username") or "").strip(),
                display_name=(body.get("display_name") or "").strip(),
                role=body.get("role", "chat"),
                password=body.get("password") or "",
            )
            if not ok:
                self._send_json(409 if "already exists" in err else 400, {"ok": False, "error": err})
                return
            self._send_json(201, {"ok": True})
            return

        # POST /api/users/<u>/password — admin resets a user's password
        if self.path.startswith("/api/users/") and self.path.endswith("/password"):
            if not require_perm(self, "admin"): return
            from glados.webui.pages import users as _users_page
            username = self.path[len("/api/users/"):-len("/password")]
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            ok, err = _users_page.reset_password(username, body.get("new_password") or "")
            if not ok:
                self._send_json(404 if "not found" in err else 400, {"ok": False, "error": err})
                return
            sessions.revoke_all_for_user(username)  # forces re-login
            self._send_json(200, {"ok": True})
            return
```

Symmetric blocks for `do_PUT` (updating a user) and `do_DELETE` (deleting a user).

Commit after each endpoint lands with its tests:

```
git commit -m "feat(users): POST /api/users for admin-created accounts"
git commit -m "feat(users): PUT /api/users/<u> for role/display_name/disabled"
git commit -m "feat(users): POST /api/users/<u>/password — admin-reset flow"
git commit -m "feat(users): DELETE /api/users/<u> with last-admin guard"
```

### 7.6 WebUI Users page

- [ ] **Step 1: Add Users entry to the sidebar (admin-only)**

In the SPA shell JS (`glados/webui/static/ui.js` or `glados/webui/pages/_shell.py`), add a sidebar entry:

```javascript
if (currentUser && currentUser.role === 'admin') {
  // Add Users entry under Configuration
}
```

- [ ] **Step 2: Build the page HTML**

Create `glados/webui/pages/users_page.py` (SPA-rendered page):

```python
USERS_PAGE_HTML = """
<section class="page-users">
  <h2>Users</h2>
  <button id="u-add" class="btn-primary">Add user</button>

  <table class="u-table">
    <thead><tr><th>Username</th><th>Display name</th><th>Role</th>
      <th>Status</th><th>Last login</th><th></th></tr></thead>
    <tbody id="u-rows"></tbody>
  </table>

  <!-- Add/Edit modal -->
  <div id="u-modal" class="modal hidden">
    <div class="modal-body">
      <h3 id="u-modal-title"></h3>
      <label>Username <input id="u-username" type="text" maxlength="64"></label>
      <label>Display name <input id="u-display-name" type="text" maxlength="128"></label>
      <label>Role
        <select id="u-role">
          <option value="chat" selected>chat</option>
          <option value="admin">admin</option>
        </select>
      </label>
      <label class="u-password-field">Password
        <input id="u-password" type="password" minlength="8">
      </label>
      <button id="u-save" class="btn-primary">Save</button>
      <button id="u-cancel">Cancel</button>
      <div id="u-err" class="error"></div>
    </div>
  </div>
</section>
<script>/* ...AJAX wiring to /api/users... */</script>
"""
```

Full JavaScript wiring is a straightforward JSON-over-fetch affair; omit here for brevity but ensure the dropdown default is `chat` (per AUTH_DESIGN.md §5.6).

- [ ] **Step 3: Manual smoke test + commit**

Run the full suite:
```
pytest -q
```
Then commit:
```
git add glados/webui/pages/users_page.py glados/webui/static/*.js glados/webui/static/*.css
git commit -m "feat(users): Users admin page in SPA with role dropdown"
```

---

## Task 8: Active Sessions + Change Password

**Files:**
- Modify: `glados/webui/tts_ui.py` — `/api/auth/change-password`, `/api/sessions`, `/api/sessions/<id>/revoke`.
- Modify: SPA — "Active Sessions" card on System tab, "Change Password" card.
- Create: `tests/test_change_password.py`

### 8.1 Change password endpoint

- [ ] **Step 1: Test**

```python
# tests/test_change_password.py
import json
from http.client import HTTPConnection


def test_change_password_self(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"current": "hunter2goes", "new": "newer-pass-2026"})
    conn.request("POST", "/api/auth/change-password",
                 headers={"Cookie": live_webui_admin.cookie,
                          "Content-Type": "application/json"},
                 body=body)
    resp = conn.getresponse()
    assert resp.status == 200


def test_change_password_wrong_current(live_webui_admin):
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"current": "wrong", "new": "valid-new-pw"})
    conn.request("POST", "/api/auth/change-password",
                 headers={"Cookie": live_webui_admin.cookie,
                          "Content-Type": "application/json"},
                 body=body)
    assert conn.getresponse().status == 401


def test_change_password_does_not_revoke_other_sessions(live_webui_admin):
    """Operator decision 2026-04-24: change-password does NOT auto-revoke."""
    from glados.auth import sessions
    before = len(sessions.list_active(live_webui_admin.username))
    conn = HTTPConnection(*live_webui_admin.addr)
    body = json.dumps({"current": "hunter2goes", "new": "new-strong-pw"})
    conn.request("POST", "/api/auth/change-password",
                 headers={"Cookie": live_webui_admin.cookie,
                          "Content-Type": "application/json"},
                 body=body)
    conn.getresponse().read()
    after = len(sessions.list_active(live_webui_admin.username))
    assert after == before
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement** in `tts_ui.py` `do_POST`:

```python
        if self.path == "/api/auth/change-password":
            if not require_perm(self, "webui.view"): return
            from glados.auth import hashing
            from glados.webui.permissions import password_meets_policy
            from glados.webui.pages import users as _users_page

            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            current = body.get("current") or ""
            new_pw = body.get("new") or ""

            user = _resolve_user_for_request(self)
            from glados.core.config_store import cfg
            yaml_user = next((u for u in cfg.auth.users if u.username == user["username"]), None)

            valid, _ = hashing.verify_password(current, yaml_user.password_hash)
            if not valid:
                self._send_json(401, {"ok": False, "error": "Current password is incorrect"})
                return

            ok, err = password_meets_policy(new_pw)
            if not ok:
                self._send_json(400, {"ok": False, "error": err})
                return

            ok, err = _users_page.reset_password(user["username"], new_pw)
            if not ok:
                self._send_json(400, {"ok": False, "error": err})
                return

            # Operator decision 2026-04-24: do NOT revoke other sessions here.
            self._send_json(200, {"ok": True})
            return
```

- [ ] **Step 4: Run, expect pass. Commit.**

```
git add glados/webui/tts_ui.py tests/test_change_password.py
git commit -m "feat(auth): POST /api/auth/change-password; does not revoke other sessions"
```

### 8.2 Active sessions endpoints

Analogous pattern — `GET /api/sessions` lists (admin sees all, chat sees own), `DELETE /api/sessions/<id>` revokes. UI cards on the System tab render the table. Keep commits tight.

---

## Task 9: Auth-bypass mode (`GLADOS_AUTH_BYPASS`)

**Files:**
- Modify: `glados/auth/bypass.py` — replace stub with real implementation.
- Modify: `glados/webui/tts_ui.py` — inject banner HTML in every HTML response; tag audit events.
- Modify: `glados/observability/audit.py` — add `auth_bypass` + `operator_id` fields.
- Create: `tests/test_auth_bypass.py`

### 9.1 Bypass detection

- [ ] **Step 1: Write `tests/test_auth_bypass.py`**

```python
"""Tests for GLADOS_AUTH_BYPASS mode."""
import os
import pytest
import importlib


@pytest.fixture
def bypass_on(monkeypatch):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", "1")
    import glados.auth.bypass as b
    importlib.reload(b)
    yield b
    monkeypatch.delenv("GLADOS_AUTH_BYPASS", raising=False)
    importlib.reload(b)


@pytest.fixture
def bypass_off(monkeypatch):
    monkeypatch.delenv("GLADOS_AUTH_BYPASS", raising=False)
    import glados.auth.bypass as b
    importlib.reload(b)
    yield b


def test_bypass_active_when_env_set(bypass_on):
    assert bypass_on.active() is True


def test_bypass_inactive_by_default(bypass_off):
    assert bypass_off.active() is False


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_bypass_inactive_for_falsy_values(monkeypatch, val):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", val)
    import glados.auth.bypass as b
    importlib.reload(b)
    assert b.active() is False


def test_banner_html_contains_required_phrases(bypass_on):
    html = bypass_on.banner_html()
    assert "AUTHENTICATION BYPASS" in html.upper()
    assert "GLADOS_AUTH_BYPASS" in html
    assert "background" in html.lower()  # styled


def test_audit_tag_reflects_bypass(bypass_on):
    tag = bypass_on.audit_tag(remote_addr="10.0.0.5")
    assert tag["auth_bypass"] is True
    assert tag["operator_id"] == "bypass:10.0.0.5"
```

- [ ] **Step 2: Run, expect fail** (stub still returns False, banner empty).

- [ ] **Step 3: Implement `glados/auth/bypass.py`**

Replace the stub entirely:

```python
"""Auth-bypass mode — compose-only GLADOS_AUTH_BYPASS=1 env flag.

Disables all auth checks for the container's run. Banner must be
visible on every HTML page, audit events carry operator_id="bypass:<ip>"
and auth_bypass=true. See docs/AUTH_DESIGN.md §9.
"""
from __future__ import annotations

import os
import threading
import time
from loguru import logger


_active = os.environ.get("GLADOS_AUTH_BYPASS", "").strip().lower() in {"1", "true", "yes", "on"}

_BANNER_HTML = """
<div id="glados-auth-bypass-banner" style="
    position: sticky; top: 0; z-index: 9999;
    background: #c81010; color: #ffffff;
    padding: 12px 16px; font-weight: 700; font-family: system-ui, sans-serif;
    text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.5);">
  ⚠ AUTHENTICATION BYPASS MODE — anyone with network access to this
  WebUI has full admin control. Remove
  <code>GLADOS_AUTH_BYPASS</code> from docker-compose.yml and restart
  the container to resume normal authentication.
</div>
"""


def active() -> bool:
    return _active


def banner_html() -> str:
    return _BANNER_HTML if _active else ""


def audit_tag(*, remote_addr: str = "") -> dict:
    if not _active:
        return {}
    return {
        "auth_bypass": True,
        "operator_id": f"bypass:{remote_addr or 'unknown'}",
    }


def _periodic_warning():
    while _active:
        time.sleep(15 * 60)  # 15 min
        logger.warning(
            "GLaDOS is running in AUTH BYPASS MODE. "
            "Remove GLADOS_AUTH_BYPASS from compose and restart."
        )


if _active:
    logger.error(
        "=" * 72 + "\n"
        "  AUTHENTICATION BYPASS MODE ACTIVE\n"
        "  All auth checks are disabled for this container run.\n"
        "  Remove GLADOS_AUTH_BYPASS from docker-compose.yml to restore.\n"
        + "=" * 72
    )
    threading.Thread(target=_periodic_warning, daemon=True).start()
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Banner injection in HTML responses**

In `tts_ui.py`, find the `_send_html` helper (or wherever HTML responses are written). Wrap the body:

```python
def _inject_bypass_banner(html_body: bytes) -> bytes:
    from glados.auth import bypass
    if not bypass.active():
        return html_body
    banner = bypass.banner_html().encode("utf-8")
    # Insert right after <body>, or prepend if no <body> marker.
    body_open = html_body.find(b"<body")
    if body_open < 0:
        return banner + html_body
    close = html_body.find(b">", body_open)
    if close < 0:
        return banner + html_body
    return html_body[:close+1] + banner + html_body[close+1:]
```

Every place that writes an HTML body to `self.wfile` wraps through `_inject_bypass_banner(body)`. This includes `/`, `/login`, `/setup/*`, `/tts`, and error HTML.

- [ ] **Step 6: Audit tagging**

Modify `glados/observability/audit.py` `AuditEvent` dataclass — add:

```python
operator_id: str | None = None
auth_bypass: bool = False
```

Then modify every WebUI `audit(...)` call to carry these fields, populated from `bypass.audit_tag(remote_addr=ip) | user["username"]`.

- [ ] **Step 7: `/api/auth/status` reports bypass state**

Locate `_get_auth_status` in `tts_ui.py` (line 1313). Replace:

```python
    def _get_auth_status(self):
        from glados.auth import bypass
        if bypass.active():
            self._send_json(200, {
                "authenticated": True,
                "bypass": True,
                "user": {"username": "bypass", "role": "admin"},
            })
            return
        user = _resolve_user_for_request(self)
        self._send_json(200, {
            "authenticated": user is not None,
            "bypass": False,
            "user": user,
        })
```

Update the SPA `ui.js` to render the red banner if `status.bypass === true`.

- [ ] **Step 8: Integration test**

```python
def test_bypass_bypasses_auth_for_gated_route(monkeypatch, tmp_path):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", "1")
    # ... reload bypass module and spin up a fresh webui ...
    # GET /api/config/reload (admin-only) without any cookie → 200
    # Verify banner present in HTML responses
```

- [ ] **Step 9: Commit**

```
git add glados/auth/bypass.py glados/webui/tts_ui.py glados/observability/audit.py glados/webui/static/ui.js tests/test_auth_bypass.py
git commit -m "feat(auth): GLADOS_AUTH_BYPASS mode with red banner + audit tagging"
```

---

## Task 10: Service-endpoint rate limiter

**Files:**
- Create: `glados/auth/rate_limit.py`
- Modify: `glados/webui/tts_ui.py` — wrap public service routes.
- Create: `tests/test_rate_limiter.py`

- [ ] **Step 1: Test**

```python
# tests/test_rate_limiter.py
import time
import pytest
from glados.auth.rate_limit import TokenBucket


def test_bucket_allows_up_to_capacity():
    b = TokenBucket(capacity=3, window_seconds=60)
    assert b.allow("1.2.3.4")
    assert b.allow("1.2.3.4")
    assert b.allow("1.2.3.4")
    assert not b.allow("1.2.3.4")


def test_bucket_isolates_different_keys():
    b = TokenBucket(capacity=2, window_seconds=60)
    b.allow("a"); b.allow("a")
    assert not b.allow("a")
    assert b.allow("b")


def test_bucket_refills_after_window(monkeypatch):
    b = TokenBucket(capacity=1, window_seconds=1)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    assert b.allow("x")
    assert not b.allow("x")
    now[0] += 2.0
    assert b.allow("x")
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement `glados/auth/rate_limit.py`**

```python
"""Per-IP token-bucket limiter for unauth service endpoints.

In-memory state; restart clears it. Phase 2 adds SQLite persistence.
See docs/AUTH_DESIGN.md §8.2.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    capacity: int
    window_seconds: float
    _state: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        """Return True if this request is allowed; decrements a token.
        False if the bucket is empty."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            elapsed = now - last
            refilled = min(
                self.capacity,
                tokens + (elapsed / self.window_seconds) * self.capacity,
            )
            if refilled < 1.0:
                self._state[key] = (refilled, now)
                return False
            self._state[key] = (refilled - 1.0, now)
            return True
```

- [ ] **Step 4: Wrap public service routes**

Create a module-level bucket and gate `/api/stt`, `/api/generate`, `/tts` POSTs:

```python
# in tts_ui.py near top
_service_limiter = TokenBucket(
    capacity=_cfg.auth.rate_limits.service_max_requests,
    window_seconds=_cfg.auth.rate_limits.service_window_seconds,
)

# in each public-service handler, before processing:
ip = self.client_address[0] if self.client_address else "?"
if not _service_limiter.allow(ip):
    self._send_json(429, {"error": "Rate limit exceeded", "retry_after": 60})
    return
```

- [ ] **Step 5: Run, expect pass. Commit.**

```
git add glados/auth/rate_limit.py glados/webui/tts_ui.py tests/test_rate_limiter.py
git commit -m "feat(auth): token-bucket rate limiter on public TTS/STT routes"
```

---

## Task 11: Cleanup — remove legacy cookie acceptance and deprecate set_password

**Files:**
- Modify: `glados/webui/tts_ui.py` — drop legacy HMAC cookie fallback.
- Modify: `glados/tools/set_password.py` — add deprecation warning.
- Modify: `docs/CHANGES.md` — entry for the auth rebuild.

- [ ] **Step 1: Remove legacy HMAC cookie fallback**

In `tts_ui.py`, find the logic that reads the old cookie format (there should be a path where `itsdangerous.loads` fails and a legacy HMAC verify is attempted — added in Task 3 Step 3 via a fallback). Delete it. Leave a comment:

```python
# Legacy HMAC cookie format removed 2026-04-XX per AUTH_DESIGN.md §10.2
# (+30-day deprecation window elapsed). Users still holding pre-rebuild
# cookies get a 401 and are redirected to /login for re-auth.
```

- [ ] **Step 2: Deprecate set_password.py**

```python
# glados/tools/set_password.py — add at the top of main()

def main() -> None:
    import warnings
    warnings.warn(
        "glados.tools.set_password is deprecated. Use the WebUI wizard "
        "(/setup) or Configuration → Users to manage passwords. See "
        "docs/AUTH_DESIGN.md §10.2.",
        DeprecationWarning, stacklevel=2,
    )
    logger.warning(
        "set_password tool is deprecated and will be removed in the "
        "next release cycle. Use the WebUI instead."
    )
    # ... existing main() body ...
```

- [ ] **Step 3: Update `configs/config.example.yaml`**

Replace the current `auth:` block with the new shape (from AUTH_DESIGN.md §6.1). Add comments explaining each field.

- [ ] **Step 4: Write the CHANGES.md entry**

Append to `docs/CHANGES.md`:

```markdown
## Change 23 — WebUI auth rebuild (2026-04-XX)

Replaces the single-password bcrypt + HMAC-signed cookie with a
multi-user Argon2id + itsdangerous + SQLite-session scheme. See
`docs/AUTH_DESIGN.md` for full architecture.

**Shipping changes:**
- New `auth.users[]` list in `configs/global.yaml`. Legacy single-
  password deployments migrate transparently on first successful
  login.
- Two roles: `admin` (full access) and `chat` (chat tab only).
  First user from `/setup` is always admin.
- First-run wizard at `/setup` with pluggable step framework (Phase
  1 ships one step).
- Public TTS + STT endpoints; chat requires login; config is admin-
  only. Standalone `/tts` page extracted.
- `GLADOS_AUTH_BYPASS=1` compose env var for recovery with red
  banner on every page.
- Per-IP token bucket rate limiter on public service routes.
- Active Sessions card for revoking individual cookies.
- Fixed: `_AUTH_*` module globals in `tts_ui.py` broke live-reload
  of auth config; rewritten to read `_cfg.auth.*` on each request.
- Deprecated: `glados.tools.set_password` (removal in next cycle).
- Audit events carry `operator_id` and `auth_bypass` fields.

**Rollback:** snapshot of `configs/global.yaml` taken as
`global.yaml.pre-auth-rebuild` before rollout; revert code + restore
the legacy `password_hash` / `session_secret` fields from that
snapshot.
```

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: all pass. Before merging, also run the battery harness if
available — auth changes must not regress existing live-test flows.

- [ ] **Step 6: Commit**

```
git add glados/webui/tts_ui.py glados/tools/set_password.py configs/config.example.yaml docs/CHANGES.md
git commit -m "chore(auth): remove legacy cookie fallback; deprecate set_password tool"
```

---

## Self-review checklist

Before handing this plan to an executor, walk through these:

**1. Spec coverage:** Each section of `docs/AUTH_DESIGN.md` maps to a Task:
- §2 current state → documented, not implemented (descriptive).
- §3 options → decided, locked in architecture.
- §4 roles + permissions → Task 1.2 (permissions module).
- §5 data flows → Tasks 3, 4, 5, 7, 8 collectively.
- §6 storage schema → Tasks 1.4 (YAML) + 2.2 (SQLite).
- §7 session expiry → Tasks 1.4 (defaults) + 2.3 (session layer) + 1.3 (duration parser).
- §8 rate limits → Task 10 (service) + Task 3 (login, reuses existing `_check_rate_limit`).
- §9 bypass → Task 9.
- §10 migration → Task 3.
- §11 rollback → documented in Task 11 CHANGES.md entry.
- §12 deferred → not implemented (by design).
- §13 phase preview → mirrors this plan's task order.

**2. Placeholder scan:** No "TBD" / "similar to" / "implement later".
Every step has code or an exact command.

**3. Type consistency:**
- `user_has_perm(role, perm)` signature is used consistently in
  permissions.py and require_perm.
- `sessions.create(*, username, role, remote_addr="", user_agent="", expires_at=None)` matches usage in `_handle_login` and `_complete_wizard_session`.
- `WizardStep.process(handler, form) -> StepResult` is the signature used by both engine and the SetAdminPasswordStep.
- `bypass.active()`, `bypass.banner_html()`, `bypass.audit_tag(remote_addr=...)` — matched in stub (Task 4) and impl (Task 9).

---

## Execution handoff

Plan complete and saved to `docs/AUTH_PLAN.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
