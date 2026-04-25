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
        return ok, ok  # rehash iff successful verification

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
