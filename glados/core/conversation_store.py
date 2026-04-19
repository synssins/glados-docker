"""
Thread-safe conversation history store.

This module provides a ConversationStore class that encapsulates all synchronization
for the shared conversation history, eliminating race conditions from conditional
lock usage patterns.

Stage 3 Phase B: optionally backed by `ConversationDB` (SQLite) so
history survives container restarts and Tier 1/2 exchanges captured
here become context for subsequent Tier 3 calls. The in-memory list
is retained as the hot read path so `snapshot()` cost is unchanged;
writes additionally fan out to the DB.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from .conversation_db import ConversationDB


# Underscore-prefixed keys used to carry per-message persistence
# metadata through snapshot() -> compaction() -> replace_all(). They
# are filtered out of LLM prompts by the allowlist sanitizers in
# glados/core/llm_processor.py and dropped from the DB's `extra`
# column by ConversationDB.append_many.
_META_TIER = "_tier"
_META_SOURCE = "_source"
_META_HA_CONV = "_ha_conversation_id"
_META_PRINCIPAL = "_principal"


def _stamp_row_meta(
    message: dict[str, Any],
    *,
    source: str | None,
    principal: str | None,
    tier: int | None,
    ha_conversation_id: str | None,
) -> None:
    """Attach the persistence metadata to the message dict in place.
    Only writes keys that are not already set AND the incoming value
    is non-None. This lets a later append() overlay a kwarg without
    clobbering an already-stamped value on the same dict."""
    if tier is not None and _META_TIER not in message:
        message[_META_TIER] = tier
    if source is not None and _META_SOURCE not in message:
        message[_META_SOURCE] = source
    if ha_conversation_id is not None and _META_HA_CONV not in message:
        message[_META_HA_CONV] = ha_conversation_id
    if principal is not None and _META_PRINCIPAL not in message:
        message[_META_PRINCIPAL] = principal


class ConversationStore:
    """
    Thread-safe conversation history store.

    All operations are atomic and protected by internal locking.
    Consumers should NOT hold references to internal lists or mutate
    returned snapshots if they need isolation.

    This replaces the previous pattern of sharing a raw list with a
    threading.Lock that was conditionally acquired.

    Optional SQLite backing via `db` parameter. When set:
      * Every append/replace fans out to the DB.
      * On engine startup, call `load_from_db()` to hydrate the in-
        memory list from the most-recent persisted messages.
      * The first `preprompt_count` messages are always treated as
        protected (Change 7 invariant); they're loaded from
        `initial_messages` and never overwritten by DB rows.
    """

    def __init__(
        self,
        initial_messages: list[dict[str, Any]] | None = None,
        *,
        db: "ConversationDB | None" = None,
        conversation_id: str = "default",
    ) -> None:
        """
        Initialize the conversation store.

        Args:
            initial_messages: Optional initial messages (e.g., personality preprompt).
                            These are copied, not referenced.
            db: Optional ConversationDB. When set, all writes are
                persisted; reads remain in-memory.
            conversation_id: Partition key for the DB (default
                "default" for the global single-conversation case).
        """
        self._lock = threading.RLock()  # RLock allows nested acquisition if needed
        self._messages: list[dict[str, Any]] = list(initial_messages or [])
        self._version: int = 0  # For change detection / optimistic concurrency
        self._preprompt_count: int = len(self._messages)  # Protected initial messages
        self._db: ConversationDB | None = db
        self._conversation_id: str = conversation_id

    # ── Lifecycle ──────────────────────────────────────────────

    def load_from_db(
        self,
        *,
        limit: int | None = None,
    ) -> int:
        """Hydrate in-memory history from the SQLite backing store.

        Call once at engine startup AFTER the constructor (which set up
        protected preprompt). Loaded DB messages are appended after
        the preprompt; they retain their original chronological order.

        `limit` caps how many recent messages are loaded (e.g. 200 for
        a fresh container that only needs recent context).

        Returns the number of messages loaded.
        """
        if self._db is None:
            return 0
        with self._lock:
            stored = self._db.snapshot(
                conversation_id=self._conversation_id, limit=limit,
            )
            # Convert stored rows back to chat-message dicts. Preprompt
            # was set in __init__; DB rows go after it.
            for sm in stored:
                self._messages.append(sm.to_chat_message())
            self._version += 1
            logger.info(
                "ConversationStore loaded {} messages from {} (preprompt={})",
                len(stored), self._conversation_id, self._preprompt_count,
            )
            return len(stored)

    # ── Writes ─────────────────────────────────────────────────

    def append(
        self,
        message: dict[str, Any],
        *,
        source: str | None = None,
        principal: str | None = None,
        tier: int | None = None,
        ha_conversation_id: str | None = None,
    ) -> int:
        """
        Append a single message to the conversation history.

        Args:
            message: The message dict to append (role, content, etc.)
            source: Optional Origin tag (webui_chat, api_chat, etc.)
                for the audit trail in the DB. Has no effect on the
                in-memory list.
            principal: Optional session sub / broker user.
            tier: Optional 1/2/3 to record which matcher tier produced
                this exchange. Used by retention sweepers to keep
                action audit trail longer than chit-chat.
            ha_conversation_id: Optional HA conversation_id captured
                from a Tier 1/2 response, so multi-turn HA threads can
                be reconstructed.

        Returns:
            The new length of the conversation history.
        """
        # Stamp per-message metadata as underscore-prefixed keys so it
        # survives snapshot() -> compaction -> replace_all() round trip.
        # Without this, the compaction agent's rebuild loses tier / source
        # and subsequent carry-over checks see tier=None for every row.
        # Underscore-prefixed keys are filtered by both the LLM-facing
        # sanitizers and the DB's `extra` column packer.
        _stamp_row_meta(message, source=source, principal=principal,
                        tier=tier, ha_conversation_id=ha_conversation_id)
        with self._lock:
            self._messages.append(message)
            self._version += 1
            new_len = len(self._messages)
        # Persist outside the in-memory lock to avoid blocking readers
        # on disk I/O. ConversationDB has its own lock for thread safety.
        self._persist_one(message, source=source, principal=principal,
                          tier=tier, ha_conversation_id=ha_conversation_id)
        return new_len

    def append_multiple(
        self,
        messages: list[dict[str, Any]],
        *,
        source: str | None = None,
        principal: str | None = None,
        tier: int | None = None,
        ha_conversation_id: str | None = None,
    ) -> int:
        """
        Atomically append multiple messages to the conversation history.

        This is useful for operations that need to add several related messages
        (e.g., user message + interrupted assistant partial response).

        Args:
            messages: List of message dicts to append.
            source / principal / tier / ha_conversation_id: optional
                metadata applied to ALL messages in the batch.

        Returns:
            The new length of the conversation history.
        """
        for _m in messages:
            _stamp_row_meta(_m, source=source, principal=principal,
                            tier=tier, ha_conversation_id=ha_conversation_id)
        with self._lock:
            self._messages.extend(messages)
            self._version += 1
            new_len = len(self._messages)
        if self._db is not None and messages:
            try:
                self._db.append_many(
                    messages,
                    conversation_id=self._conversation_id,
                    source=source, principal=principal, tier=tier,
                    ha_conversation_id=ha_conversation_id,
                )
            except Exception as exc:
                logger.warning("ConversationDB persist failed (in-memory still ok): {}", exc)
        return new_len

    def snapshot(self) -> list[dict[str, Any]]:
        """
        Return a shallow copy of all messages.

        The returned list is a new list object, but the message dicts
        inside are the same objects. This is safe for reading but callers
        should not mutate the individual message dicts.

        Returns:
            A shallow copy of the conversation history.
        """
        with self._lock:
            return list(self._messages)

    def deep_snapshot(self) -> list[dict[str, Any]]:
        """
        Return a deep copy of all messages for safe mutation.

        Use this when you need to modify messages without affecting
        the original store.

        Returns:
            A deep copy of the conversation history.
        """
        with self._lock:
            return deepcopy(self._messages)

    def replace_all(
        self,
        new_messages: list[dict[str, Any]],
        *,
        source: str | None = "compaction",
    ) -> None:
        """
        Atomically replace the entire conversation history.

        This is used by the compaction agent to swap in a compacted
        history without race conditions.

        Args:
            new_messages: The new message list to replace with (copied).
            source: optional Origin tag applied to the persisted rows;
                defaults to "compaction" since this is the typical caller.
        """
        with self._lock:
            self._messages.clear()
            self._messages.extend(new_messages)
            self._version += 1
        if self._db is not None:
            try:
                self._db.replace_conversation(
                    new_messages,
                    conversation_id=self._conversation_id,
                    source=source,
                )
            except Exception as exc:
                logger.warning("ConversationDB replace failed (in-memory still ok): {}", exc)

    def modify_message(
        self,
        index: int,
        modifier: Any,
    ) -> bool:
        """
        Modify a message at a specific index atomically.

        Args:
            index: The index of the message to modify.
            modifier: Either a dict to update with, or a callable that
                     takes the message and returns the modified message.

        Returns:
            True if modification succeeded, False if index out of range.

        Note: this does NOT persist the modified message to the DB. Use
        `replace_all()` for compaction-style edits that need to round-trip
        through SQLite. modify_message is for in-memory streaming-tool
        accumulation only.
        """
        with self._lock:
            if index < 0 or index >= len(self._messages):
                return False
            if callable(modifier):
                self._messages[index] = modifier(self._messages[index])
            else:
                self._messages[index].update(modifier)
            self._version += 1
            return True

    # ── Helpers ────────────────────────────────────────────────

    def latest_assistant_tier_exchange(
        self,
    ) -> tuple[int, float, str | None] | None:
        """Return (tier, ts, ha_conversation_id) for the most-recent
        assistant row whose tier is 1 or 2. Returns None when no DB is
        wired or no such row exists. Callers use this to carry
        home-command intent forward across a follow-up turn that has
        no device keyword."""
        if self._db is None:
            return None
        try:
            return self._db.latest_assistant_tier_exchange(
                conversation_id=self._conversation_id,
            )
        except Exception as exc:
            logger.debug("latest_assistant_tier_exchange query failed: {}", exc)
            return None

    def latest_ha_conversation_id(self) -> str | None:
        """Return the most-recent HA conversation_id seen, so callers
        can pass it forward to maintain HA's conversation thread.
        Returns None if no DB is wired or no HA exchange has been
        recorded yet."""
        if self._db is None:
            return None
        try:
            return self._db.latest_ha_conversation_id(
                conversation_id=self._conversation_id,
            )
        except Exception as exc:
            logger.debug("latest_ha_conversation_id query failed: {}", exc)
            return None

    def _persist_one(
        self,
        message: dict[str, Any],
        *,
        source: str | None,
        principal: str | None,
        tier: int | None,
        ha_conversation_id: str | None,
    ) -> None:
        if self._db is None:
            return
        try:
            self._db.append(
                message,
                conversation_id=self._conversation_id,
                source=source, principal=principal, tier=tier,
                ha_conversation_id=ha_conversation_id,
            )
        except Exception as exc:
            logger.warning("ConversationDB persist failed (in-memory still ok): {}", exc)

    def __len__(self) -> int:
        """Return the number of messages in the store."""
        with self._lock:
            return len(self._messages)

    @property
    def preprompt_count(self) -> int:
        """Number of initial messages (personality preprompt) that are protected from compaction."""
        return self._preprompt_count

    @property
    def version(self) -> int:
        """
        Current version number for change detection.

        Incremented on every modification. Can be used for optimistic
        concurrency checks or cache invalidation.
        """
        with self._lock:
            return self._version

    def iter_messages(self) -> list[dict[str, Any]]:
        """
        Return a snapshot for iteration.

        This is equivalent to snapshot() but named explicitly for
        iteration use cases.

        Returns:
            A shallow copy suitable for iteration.
        """
        return self.snapshot()
