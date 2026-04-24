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
