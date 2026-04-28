"""Stateful sessions backed by auth.db, cookies signed by itsdangerous.

Every request resolves the cookie to a row in auth_sessions; revoking a
row invalidates the cookie even if its signature is still valid.
"""
from __future__ import annotations

import threading
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
    (valid, row_dict). Defers last_used_at bookkeeping to a background
    flusher (see _record_last_used + _flush_loop) so the auth path is
    read-only and fast — operator UIs poll /api/auth/status repeatedly
    and a fsync-per-call commit on the host's bind-mount filesystem
    cost ~300 ms per request."""
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
    finally:
        con.close()
    _record_last_used(sid, now)
    return True, dict(row)


# ── last_used_at deferred-write coalescer ─────────────────────────
# Auth checks fire on every UI page load and many AJAX calls. Writing
# last_used_at synchronously per check costs ~300 ms each on the
# operator's btrfs bind-mount even with WAL+synchronous=NORMAL. We
# coalesce updates: keep the latest seen timestamp per session in
# memory, and flush every 30 s on a daemon thread. Worst-case loss
# on hard crash: 30 s of staleness in the Account → Sessions panel —
# acceptable for bookkeeping data.

_last_used: dict[str, int] = {}
_last_used_lock = threading.Lock()
_flusher_started = False
_flusher_lock = threading.Lock()


def _record_last_used(sid: str, ts: int) -> None:
    global _flusher_started
    with _last_used_lock:
        prev = _last_used.get(sid, 0)
        if ts > prev:
            _last_used[sid] = ts
    if not _flusher_started:
        with _flusher_lock:
            if not _flusher_started:
                t = threading.Thread(
                    target=_flush_loop, name="AuthLastUsedFlusher", daemon=True,
                )
                t.start()
                _flusher_started = True


def _flush_loop() -> None:
    while True:
        time.sleep(30)
        try:
            _flush_last_used()
        except Exception:
            pass


def _flush_last_used() -> None:
    with _last_used_lock:
        if not _last_used:
            return
        snapshot = list(_last_used.items())
        _last_used.clear()
    con = auth_db.connect()
    try:
        con.executemany(
            "UPDATE auth_sessions SET last_used_at=? WHERE session_id=?",
            [(ts, sid) for sid, ts in snapshot],
        )
        con.commit()
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
