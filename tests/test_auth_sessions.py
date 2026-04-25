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


# ── Session CRUD ───────────────────────────────────────────────

import time
from glados.auth import sessions


def test_create_session_roundtrip(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    token = sessions.create(username="admin", role="admin",
                            remote_addr="127.0.0.1", user_agent="pytest")
    assert token and "." in token

    valid, row = sessions.verify(token)
    assert valid
    assert row["username"] == "admin"
    assert row["role_at_issue"] == "admin"


def test_verify_bad_signature_returns_none(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    valid, row = sessions.verify("not.a.valid.token")
    assert not valid
    assert row is None


def test_revoke_marks_session_revoked(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    token = sessions.create(username="admin", role="admin")
    _, row = sessions.verify(token)
    sessions.revoke(row["session_id"])
    valid, _ = sessions.verify(token)
    assert not valid


def test_expires_at_in_past_invalidates(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    token = sessions.create(username="admin", role="admin",
                            expires_at=int(time.time()) - 10)
    valid, _ = sessions.verify(token)
    assert not valid


def test_list_active_sessions_filters_revoked(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    t1 = sessions.create(username="admin", role="admin")
    t2 = sessions.create(username="admin", role="admin")
    _, row1 = sessions.verify(t1)
    sessions.revoke(row1["session_id"])

    active = sessions.list_active(username="admin")
    assert len(active) == 1
    _, row2 = sessions.verify(t2)
    assert active[0]["session_id"] == row2["session_id"]


def test_revoke_all_for_user(tmp_auth_db, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.auth, "session_secret", "a" * 64)
    t1 = sessions.create(username="alice", role="chat")
    t2 = sessions.create(username="alice", role="chat")
    t3 = sessions.create(username="bob", role="chat")
    count = sessions.revoke_all_for_user("alice")
    assert count == 2
    assert len(sessions.list_active(username="alice")) == 0
    assert len(sessions.list_active(username="bob")) == 1
