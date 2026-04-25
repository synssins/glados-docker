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
