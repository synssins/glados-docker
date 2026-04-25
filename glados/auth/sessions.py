"""Stateful sessions backed by auth.db, cookies signed by itsdangerous.

Every request resolves the cookie to a row in auth_sessions; revoking a
row invalidates the cookie even if its signature is still valid.
"""
from __future__ import annotations

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
    if not isinstance(payload, dict):
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
