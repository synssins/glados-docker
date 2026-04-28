"""Tests for legacy-YAML → users-list synthesizer."""
import pytest
from glados.core.config_store import AuthGlobal
from glados.core.config_store import _synthesize_legacy_admin


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
        "users": [{"username": "residenta", "password_hash": "$argon2id$x",
                   "role": "admin"}],
    }
    out = _synthesize_legacy_admin(raw)
    assert out["users"] == raw["users"]
    assert out["bootstrap_allowed"] is False


def test_auth_global_loads_legacy_via_synthesizer():
    raw = {
        "enabled": True,
        "password_hash": "$2b$12$xyz",
        "session_secret": "s" * 64,
    }
    raw = _synthesize_legacy_admin(raw)
    a = AuthGlobal.model_validate(raw)
    assert len(a.users) == 1
    assert a.users[0].hash_algorithm == "bcrypt-legacy"


def test_synthesizer_seeds_admin_when_users_present_but_no_admin():
    """Operator-hit case: ResidentA (chat) was added but admin migration
    never persisted. Synthesizer must re-seed admin from the dormant
    legacy password_hash."""
    raw = {
        "enabled": True,
        "password_hash": "$2b$12$dormantlegacy",
        "session_secret": "s" * 64,
        "users": [{
            "username": "residenta", "role": "chat",
            "password_hash": "$argon2id$x", "hash_algorithm": "argon2id",
        }],
    }
    out = _synthesize_legacy_admin(raw)
    usernames = [u["username"] for u in out["users"]]
    assert "admin" in usernames
    assert "residenta" in usernames
    admin = next(u for u in out["users"] if u["username"] == "admin")
    assert admin["role"] == "admin"
    assert admin["hash_algorithm"] == "bcrypt-legacy"


def test_synthesizer_skips_admin_seed_when_admin_already_present():
    raw = {
        "enabled": True,
        "password_hash": "$2b$12$legacy",
        "users": [{"username": "ops", "role": "admin",
                   "password_hash": "$argon2id$x"}],
    }
    out = _synthesize_legacy_admin(raw)
    usernames = [u["username"] for u in out["users"]]
    assert "admin" not in usernames  # didn't double-seed
    assert "ops" in usernames
