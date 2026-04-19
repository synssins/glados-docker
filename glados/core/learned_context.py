"""LearnedContextStore — durable SQLite store for learned guesses.

When a user's utterance is ambiguous and short-term memory doesn't
cover it ("turn the lights up" with no recent turn and no area on the
request), we still want a sensible default. If the user has spoken this
exact phrase from this exact channel/source before, and it was
resolved successfully, we can guess the same resolution again — as
long as HA's current state plausibly supports that action.

This store records those learned (utterance → resolution) patterns
with a reinforcement counter. The resolver consults it *before*
falling back to asking for clarification, and always validates the
guess against HA state before executing. See CURRENT_STATE.md §Q5 for
the full design rationale.

Safety properties encoded here:

  - Rows start at reinforcement = 1 (one successful use).
  - `bump_success` increments on each validated execution.
  - `bump_failure` decrements on validation fail or user correction.
  - Rows with reinforcement ≤ 0 are considered dead and get removed
    on the next sweep — the store never accumulates bad guesses.
  - Rows untouched for `idle_ttl_days` (default 14) get swept. A
    habit the user no longer has naturally decays.

The learned-context store is intentionally separate from both
`conversation_db` (the durable chat log) and `session_memory` (the
short-lived context buffer). Different retention, different query
patterns, different concurrency needs.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


_SCHEMA_VERSION = 1

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
)
"""

_CREATE_LEARNED = """
CREATE TABLE IF NOT EXISTS learned_context (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_key     TEXT NOT NULL,
    source_channel    TEXT NOT NULL,
    source_area_id    TEXT,                     -- NULL when request had none
    resolved_area_id  TEXT NOT NULL,
    resolved_verb     TEXT NOT NULL,
    resolved_tier     TEXT,                     -- lamp / overhead / ... / NULL
    reinforcement     INTEGER NOT NULL DEFAULT 1,
    first_seen_at     REAL NOT NULL,
    last_used_at      REAL NOT NULL,
    UNIQUE(utterance_key, source_channel, source_area_id,
           resolved_area_id, resolved_verb, resolved_tier)
)
"""

_CREATE_IDX_LOOKUP = (
    "CREATE INDEX IF NOT EXISTS idx_learned_lookup "
    "ON learned_context(utterance_key, source_channel, source_area_id)"
)

_CREATE_IDX_TOUCH = (
    "CREATE INDEX IF NOT EXISTS idx_learned_touch "
    "ON learned_context(last_used_at)"
)


DEFAULT_IDLE_TTL_DAYS = 14
_SECONDS_PER_DAY = 86_400.0


# ---------------------------------------------------------------------------
# Utterance normalization
# ---------------------------------------------------------------------------

# Kept minimal on purpose. Aggressive stopword stripping would collapse
# too many distinct utterances into the same key ("brighter" and
# "brighter please" should match; "brighter" and "dimmer" MUST NOT).
_PUNCT_RE = re.compile(r"[^\w\s']")
_WS_RE = re.compile(r"\s+")


def normalize_utterance(text: str) -> str:
    """Normalize an utterance to its learned-context key.

    Lowercase, strip punctuation except apostrophes (we preserve
    "it's"), collapse whitespace. That's it. Stopword stripping and
    stemming are intentionally absent — they would merge semantically
    distinct commands.
    """
    if not text:
        return ""
    t = text.lower().strip()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearnedRow:
    """A single learned-context record. Frozen so the resolver can hold
    it while validating against HA without fear of mutation."""

    id: int
    utterance_key: str
    source_channel: str
    source_area_id: str | None
    resolved_area_id: str
    resolved_verb: str
    resolved_tier: str | None
    reinforcement: int
    first_seen_at: float
    last_used_at: float


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class LearnedContextStore:
    """SQLite-backed learned-context store.

    Shape mirrors `ConversationDB` (single long-lived connection, WAL
    mode, RLock for thread safety, schema migrations on open) so
    operators reason about both stores the same way.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        idle_ttl_days: float = DEFAULT_IDLE_TTL_DAYS,
        now_fn: "_NowFn | None" = None,
    ) -> None:
        if idle_ttl_days <= 0:
            raise ValueError("idle_ttl_days must be > 0")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_s = idle_ttl_days * _SECONDS_PER_DAY
        self._now = now_fn or time.time
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        logger.info("LearnedContextStore opened at {}", self._path)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover — cleanup
                pass

    # ---- Migrations --------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(_CREATE_SCHEMA_VERSION)
            cur.execute(_CREATE_LEARNED)
            cur.execute(_CREATE_IDX_LOOKUP)
            cur.execute(_CREATE_IDX_TOUCH)
            cur.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )

    # ---- Writes ------------------------------------------------------

    def record_success(
        self,
        *,
        utterance: str,
        source_channel: str,
        source_area_id: str | None,
        resolved_area_id: str,
        resolved_verb: str,
        resolved_tier: str | None = None,
    ) -> LearnedRow:
        """Record that this (utterance, source) resolved to these
        entities and the result was validated in HA. Upserts on the
        uniqueness tuple — a repeat of the same resolution bumps the
        reinforcement counter rather than duplicating the row.
        """
        key = normalize_utterance(utterance)
        if not key:
            raise ValueError("utterance must be non-empty after normalization")
        if not resolved_area_id or not resolved_verb:
            raise ValueError("resolved_area_id and resolved_verb are required")
        now = self._now()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT id, reinforcement FROM learned_context
                 WHERE utterance_key = ?
                   AND source_channel = ?
                   AND (source_area_id IS ?
                        OR source_area_id = ?)
                   AND resolved_area_id = ?
                   AND resolved_verb = ?
                   AND (resolved_tier IS ?
                        OR resolved_tier = ?)
                """,
                (
                    key, source_channel,
                    source_area_id, source_area_id,
                    resolved_area_id, resolved_verb,
                    resolved_tier, resolved_tier,
                ),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO learned_context
                      (utterance_key, source_channel, source_area_id,
                       resolved_area_id, resolved_verb, resolved_tier,
                       reinforcement, first_seen_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        key, source_channel, source_area_id,
                        resolved_area_id, resolved_verb, resolved_tier,
                        now, now,
                    ),
                )
                row_id = cur.lastrowid or 0
            else:
                row_id = int(existing["id"])
                cur.execute(
                    """
                    UPDATE learned_context
                       SET reinforcement = reinforcement + 1,
                           last_used_at = ?
                     WHERE id = ?
                    """,
                    (now, row_id),
                )
            return self._load_by_id_locked(row_id)

    def bump_failure(self, row_id: int) -> LearnedRow | None:
        """Decrement reinforcement. If it drops to ≤ 0 the row is
        deleted immediately — a learned guess that fails validation or
        gets corrected by the user doesn't deserve to linger.

        Returns the updated row, or None if the row was deleted.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE learned_context "
                "   SET reinforcement = reinforcement - 1 "
                " WHERE id = ?",
                (row_id,),
            )
            cur.execute(
                "SELECT reinforcement FROM learned_context WHERE id = ?",
                (row_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if int(row["reinforcement"]) <= 0:
                cur.execute("DELETE FROM learned_context WHERE id = ?", (row_id,))
                return None
            return self._load_by_id_locked(row_id)

    def forget(self, row_id: int) -> None:
        """Delete a specific row. Used by 'never mind' style corrections
        and by admin cleanup."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM learned_context WHERE id = ?", (row_id,),
            )

    # ---- Reads -------------------------------------------------------

    def lookup(
        self,
        *,
        utterance: str,
        source_channel: str,
        source_area_id: str | None,
        limit: int = 5,
    ) -> list[LearnedRow]:
        """Find candidate learned resolutions for this (utterance,
        source). Results are filtered to reinforcement > 0, sorted by
        reinforcement DESC then last_used_at DESC — the strongest and
        most-recent guess comes first.

        Does NOT touch `last_used_at` — a lookup is not a success yet;
        the caller bumps via `record_success` only after HA validation
        passes. That way a stale guess that keeps missing doesn't
        artificially refresh its own TTL.
        """
        key = normalize_utterance(utterance)
        if not key:
            return []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT * FROM learned_context
                 WHERE utterance_key = ?
                   AND source_channel = ?
                   AND (source_area_id IS ?
                        OR source_area_id = ?)
                   AND reinforcement > 0
                 ORDER BY reinforcement DESC, last_used_at DESC
                 LIMIT ?
                """,
                (
                    key, source_channel,
                    source_area_id, source_area_id,
                    int(limit),
                ),
            )
            return [_row_to_dataclass(r) for r in cur.fetchall()]

    def count(self) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM learned_context")
            return int(cur.fetchone()[0])

    # ---- Admin -------------------------------------------------------

    def sweep(self) -> int:
        """Remove rows that have decayed past their TTL or dropped to
        reinforcement ≤ 0. Returns number of rows deleted.

        Called periodically by the retention agent. Safe to call
        concurrently with reads/writes.
        """
        cutoff = self._now() - self._ttl_s
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM learned_context "
                " WHERE reinforcement <= 0 OR last_used_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    # ---- Internals ---------------------------------------------------

    def _load_by_id_locked(self, row_id: int) -> LearnedRow:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM learned_context WHERE id = ?", (row_id,))
        row = cur.fetchone()
        if row is None:  # pragma: no cover — shouldn't happen after insert
            raise RuntimeError(f"LearnedContextStore: row {row_id} vanished")
        return _row_to_dataclass(row)


def _row_to_dataclass(row: sqlite3.Row) -> LearnedRow:
    return LearnedRow(
        id=int(row["id"]),
        utterance_key=row["utterance_key"],
        source_channel=row["source_channel"],
        source_area_id=row["source_area_id"],
        resolved_area_id=row["resolved_area_id"],
        resolved_verb=row["resolved_verb"],
        resolved_tier=row["resolved_tier"],
        reinforcement=int(row["reinforcement"]),
        first_seen_at=float(row["first_seen_at"]),
        last_used_at=float(row["last_used_at"]),
    )


# Protocol-free alias — tests pass a callable for deterministic time.
_NowFn = "type(time.time)"  # noqa: ERA001 — documentation-only
