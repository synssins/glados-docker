"""SQLite-backed durable conversation store.

Stage 3 Phase B: gives the in-memory ConversationStore a persistent
backing so:
  - container restarts don't wipe history
  - Tier 1/2 exchanges captured here become context for subsequent
    Tier 3 calls (fixes the "All lights" multi-turn failure)
  - retention sweepers (Phase C) have a real surface to prune
  - HA's conversation_id is preserved across utterances

Design notes:
  - sqlite3 stdlib only. No SQLAlchemy.
  - All writes use a per-instance lock; SQLite's own locking covers
    cross-process, but we want a single writer thread for clarity.
  - Schema versioning via a `schema_version` table. Migrations run on
    open(); idempotent.
  - One SQLite connection per process (kept open). Reopened on
    `close()`/`open()` cycles.
  - Messages are stored row-per-message with conversation_id +
    monotonically-increasing idx for ordering. NOT keyed by
    `(conversation_id, idx)` PK because reordering / inserts mid-
    conversation aren't supported (compaction replaces in bulk via
    `replace_conversation`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from loguru import logger


# Schema version constant — bump when adding a migration.
_SCHEMA_VERSION = 1

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
)
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL DEFAULT 'default',
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,             -- JSON-encoded list[dict] or NULL
    extra TEXT,                  -- JSON-encoded dict for forward-compat fields
    source TEXT,                 -- audit Origin (webui_chat, api_chat, ...)
    principal TEXT,              -- session sub or broker user, if any
    ts REAL NOT NULL,            -- Unix epoch seconds at write time
    tier INTEGER,                -- 1 / 2 / 3 / NULL
    ha_conversation_id TEXT      -- last HA conversation_id seen on this exchange
)
"""

_CREATE_IDX_CONV = "CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, idx)"
_CREATE_IDX_TS = "CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class StoredMessage:
    """One message row, normalized to OpenAI-style dict on read."""
    id: int
    conversation_id: str
    idx: int
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    extra: dict[str, Any]
    source: str | None
    principal: str | None
    ts: float
    tier: int | None
    ha_conversation_id: str | None

    def to_chat_message(self) -> dict[str, Any]:
        """Round-trip back to the dict shape ConversationStore exposes
        and llm_processor expects."""
        out: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        # extra holds arbitrary fields (e.g. tool_call_id, name) that
        # the message had in memory; preserve them.
        for k, v in self.extra.items():
            if k not in out:
                out[k] = v
        return out


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

class ConversationDB:
    """Thread-safe SQLite-backed message store.

    Use as a long-lived singleton owned by the engine. The connection
    stays open for the process lifetime; close() is for tests.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False because we serialize via the RLock and
        # the engine has multiple threads (tool_executor, audio loops,
        # api_wrapper request handlers) that all write.
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None,
        )
        # WAL mode lets readers and writers coexist without long blocks;
        # it's also durable across crashes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        logger.info("ConversationDB opened at {}", self._path)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── Migrations ───────────────────────────────────────────

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(_CREATE_SCHEMA_VERSION)
            cur.execute(_CREATE_MESSAGES)
            cur.execute(_CREATE_IDX_CONV)
            cur.execute(_CREATE_IDX_TS)
            row = cur.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                cur.execute("INSERT INTO schema_version(version) VALUES (?)",
                            (_SCHEMA_VERSION,))
            elif row[0] < _SCHEMA_VERSION:
                # Future migrations live here. For v1 there's nothing to do.
                cur.execute("UPDATE schema_version SET version=?", (_SCHEMA_VERSION,))
            cur.close()

    # ── Writes ───────────────────────────────────────────────

    def append(
        self,
        message: dict[str, Any],
        *,
        conversation_id: str = "default",
        source: str | None = None,
        principal: str | None = None,
        tier: int | None = None,
        ha_conversation_id: str | None = None,
        ts: float | None = None,
    ) -> int:
        """Append a single message. Returns the new row id.

        `idx` is auto-assigned as max(idx)+1 within the conversation_id.
        """
        return self.append_many(
            [message],
            conversation_id=conversation_id,
            source=source, principal=principal, tier=tier,
            ha_conversation_id=ha_conversation_id, ts=ts,
        )[0]

    def append_many(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        conversation_id: str = "default",
        source: str | None = None,
        principal: str | None = None,
        tier: int | None = None,
        ha_conversation_id: str | None = None,
        ts: float | None = None,
    ) -> list[int]:
        """Append a batch of messages atomically. Returns row ids in
        insertion order."""
        items = list(messages)
        if not items:
            return []
        write_ts = ts if ts is not None else time.time()
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT COALESCE(MAX(idx) + 1, 0) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            next_idx: int = cur.fetchone()[0]
            ids: list[int] = []
            for msg in items:
                role = str(msg.get("role") or "")
                content = msg.get("content")
                content_str = None if content is None else str(content)
                tool_calls = msg.get("tool_calls") or None
                tool_calls_json = (
                    json.dumps(tool_calls) if tool_calls else None
                )
                # Anything else (tool_call_id, name, function_call, etc.)
                # goes into `extra` so we can round-trip it.
                extra = {
                    k: v for k, v in msg.items()
                    if k not in {"role", "content", "tool_calls"}
                    and not k.startswith("_")  # drop internal flags
                }
                extra_json = json.dumps(extra) if extra else None
                # Per-row metadata: kwarg wins (for uniform one-shot
                # writes like `append_multiple(..., tier=2)`), else fall
                # back to underscore-stamped keys on the message dict.
                # This is how compaction preserves per-row tier / source /
                # ha_conversation_id when it rebuilds the history.
                row_tier = tier if tier is not None else msg.get("_tier")
                row_source = source if source is not None else msg.get("_source")
                row_ha_conv = (
                    ha_conversation_id if ha_conversation_id is not None
                    else msg.get("_ha_conversation_id")
                )
                row_principal = (
                    principal if principal is not None
                    else msg.get("_principal")
                )
                cur.execute(
                    "INSERT INTO messages "
                    "(conversation_id, idx, role, content, tool_calls, extra, "
                    " source, principal, ts, tier, ha_conversation_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (conversation_id, next_idx, role, content_str,
                     tool_calls_json, extra_json,
                     row_source, row_principal, write_ts, row_tier,
                     row_ha_conv),
                )
                ids.append(int(cur.lastrowid))
                next_idx += 1
            cur.close()
            return ids

    def replace_conversation(
        self,
        new_messages: list[dict[str, Any]],
        *,
        conversation_id: str = "default",
        source: str | None = None,
    ) -> None:
        """Atomically replace every message in `conversation_id` with a
        new sequence. Used by the compaction agent.

        The `source` kwarg is used as a DEFAULT for messages that don't
        carry `_source` stamping (e.g. a freshly-inserted compaction
        summary row). Messages that were snapshot()ed from the in-
        memory store retain their original `_source` / `_tier` /
        `_ha_conversation_id` — without this, compaction would stomp
        every row with `source="compaction", tier=None`, losing the
        Tier 1/2 device-control audit trail and breaking home-command
        carry-over."""
        write_ts = time.time()
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM messages WHERE conversation_id = ?",
                        (conversation_id,))
            cur.close()
        # Re-insert via append_many so idx restarts at 0. Pass source
        # kwarg as None so per-message `_source` stamps win; any row
        # without a stamp gets the caller's default stamped now.
        if new_messages:
            if source is not None:
                for msg in new_messages:
                    msg.setdefault("_source", source)
            self.append_many(
                new_messages,
                conversation_id=conversation_id,
                source=None, ts=write_ts,
            )

    # ── Reads ────────────────────────────────────────────────

    def snapshot(
        self,
        *,
        conversation_id: str = "default",
        limit: int | None = None,
    ) -> list[StoredMessage]:
        """Return messages in insertion order. `limit=N` returns the
        most-recent N (chronologically)."""
        with self._lock:
            cur = self._conn.cursor()
            if limit is None:
                cur.execute(
                    "SELECT id, conversation_id, idx, role, content, "
                    "tool_calls, extra, source, principal, ts, tier, "
                    "ha_conversation_id "
                    "FROM messages WHERE conversation_id = ? ORDER BY idx ASC",
                    (conversation_id,),
                )
            else:
                # Fetch most recent N then reverse to get chronological.
                cur.execute(
                    "SELECT * FROM ("
                    "  SELECT id, conversation_id, idx, role, content, "
                    "  tool_calls, extra, source, principal, ts, tier, "
                    "  ha_conversation_id "
                    "  FROM messages WHERE conversation_id = ? "
                    "  ORDER BY idx DESC LIMIT ?) "
                    "ORDER BY idx ASC",
                    (conversation_id, limit),
                )
            rows = cur.fetchall()
            cur.close()
        return [_row_to_message(r) for r in rows]

    def messages_since(
        self,
        since_ts: float,
        *,
        conversation_id: str = "default",
    ) -> list[StoredMessage]:
        """Return messages written at or after `since_ts`."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, conversation_id, idx, role, content, tool_calls, "
                "extra, source, principal, ts, tier, ha_conversation_id "
                "FROM messages WHERE conversation_id = ? AND ts >= ? "
                "ORDER BY idx ASC",
                (conversation_id, since_ts),
            )
            rows = cur.fetchall()
            cur.close()
        return [_row_to_message(r) for r in rows]

    def latest_ha_conversation_id(
        self, *, conversation_id: str = "default",
    ) -> str | None:
        """Return the most-recent non-null HA conversation_id seen, so
        the caller can pass it forward to maintain HA's conversation
        thread across utterances. Returns None if no HA exchange has
        been recorded yet."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT ha_conversation_id FROM messages "
                "WHERE conversation_id = ? AND ha_conversation_id IS NOT NULL "
                "ORDER BY idx DESC LIMIT 1",
                (conversation_id,),
            )
            row = cur.fetchone()
            cur.close()
        return row[0] if row else None

    def latest_assistant_tier_exchange(
        self, *, conversation_id: str = "default",
    ) -> tuple[int, float, str | None] | None:
        """Return (tier, ts, ha_conversation_id) for the MOST RECENT
        assistant row, but only when its tier is 1 or 2. If the most
        recent assistant turn was Tier 3 chitchat (or any other tier),
        return None — home-command carry-over should not jump over an
        unrelated intervening turn.

        Used by the api_wrapper to carry home-command intent forward
        one turn when the user follows up without a device keyword
        (P0 2026-04-19: 'turn it up more' after a successful Tier 1/2
        act on the desk lamp)."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT tier, ts, ha_conversation_id FROM messages "
                "WHERE conversation_id = ? AND role = 'assistant' "
                "ORDER BY idx DESC LIMIT 1",
                (conversation_id,),
            )
            row = cur.fetchone()
            cur.close()
        if row is None or row[0] is None:
            return None
        tier = int(row[0])
        if tier not in (1, 2):
            return None
        return tier, float(row[1]), (row[2] if row[2] else None)

    def count(self, *, conversation_id: str = "default") -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            n = int(cur.fetchone()[0])
            cur.close()
        return n

    # ── Retention (Phase C will use these) ───────────────────

    def prune_before(
        self,
        cutoff_ts: float,
        *,
        conversation_id: str | None = None,
        protect_tier: bool = True,
    ) -> int:
        """Delete messages older than `cutoff_ts`. Returns count deleted.

        If `protect_tier=True` (default), tier=1 and tier=2 exchanges
        are kept regardless of age — those are the device-control audit
        trail and shouldn't be lost just because the conversation
        retention window expired. Set False to prune unconditionally.

        If `conversation_id` is None, prunes across all conversations.
        """
        with self._lock, self._conn:
            cur = self._conn.cursor()
            params: list[Any] = [cutoff_ts]
            sql = "DELETE FROM messages WHERE ts < ?"
            if conversation_id is not None:
                sql += " AND conversation_id = ?"
                params.append(conversation_id)
            if protect_tier:
                sql += " AND (tier IS NULL OR tier NOT IN (1, 2))"
            cur.execute(sql, params)
            n = cur.rowcount
            cur.close()
        return int(n)

    def disk_size_bytes(self) -> int:
        """Approximate bytes consumed by the DB file (incl. WAL)."""
        size = 0
        for suffix in ("", "-wal", "-shm"):
            p = self._path.with_name(self._path.name + suffix)
            if p.exists():
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        return size


# ---------------------------------------------------------------------------
# Row → StoredMessage
# ---------------------------------------------------------------------------

def _row_to_message(row: tuple[Any, ...]) -> StoredMessage:
    (id_, conv_id, idx, role, content, tool_calls_json, extra_json,
     source, principal, ts, tier, ha_conv_id) = row
    tool_calls = None
    if tool_calls_json:
        try:
            tool_calls = json.loads(tool_calls_json)
        except (json.JSONDecodeError, TypeError):
            tool_calls = None
    extra: dict[str, Any] = {}
    if extra_json:
        try:
            obj = json.loads(extra_json)
            if isinstance(obj, dict):
                extra = obj
        except (json.JSONDecodeError, TypeError):
            extra = {}
    return StoredMessage(
        id=int(id_), conversation_id=str(conv_id), idx=int(idx),
        role=str(role), content=content, tool_calls=tool_calls,
        extra=extra, source=source, principal=principal,
        ts=float(ts), tier=tier, ha_conversation_id=ha_conv_id,
    )
