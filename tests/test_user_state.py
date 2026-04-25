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
