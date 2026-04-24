"""Tests for the Argon2id + legacy-bcrypt-verify wrapper."""
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


def test_verify_password_bcrypt_wrong_password_no_rehash():
    """A failed bcrypt verify must NOT signal needs_rehash."""
    import bcrypt
    legacy = bcrypt.hashpw(b"correct", bcrypt.gensalt()).decode("ascii")
    ok, rehash = verify_password("wrong", legacy)
    assert not ok
    assert not rehash, "failed bcrypt verify must not trigger rehash"
