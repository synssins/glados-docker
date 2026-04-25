"""SQLite bootstrap for /app/data/auth.db.

Carries session rows and dynamic per-user state (last-login,
failed-login counter). Users live in configs/global.yaml — YAML is
authoritative. See docs/AUTH_DESIGN.md §6.3.
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
