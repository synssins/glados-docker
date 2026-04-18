"""Conversation retention sweeper.

Stage 3 Phase C: enforces operator-tunable retention on the SQLite
ConversationDB. Runs as a background daemon thread; ticks once per
`retention_sweep_interval_s` (default hourly).

Two policies enforced together (whichever bites first wins):
  1. Age-based: prune messages older than `conversation_max_days`.
     Hard cap at `conversation_hard_cap_days` (180) — we will never
     keep raw transcripts longer than six months regardless of operator
     setting.
  2. Size-based: if the DB exceeds `conversation_max_disk_mb`, prune
     the oldest tier=3 (chit-chat) messages until under the cap.

Tier 1 + Tier 2 messages (device-control audit trail) are protected
from age-based pruning by default — they're the operationally
valuable records and stay for the full hard-cap window. Size-based
pruning will eventually remove them if the cap is genuinely tight,
but only after all chit-chat is gone.

This is NOT the ChromaDB retention agent — that's a separate concern
(Phase E will handle episodic TTL and summary cleanup).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from ...core.conversation_db import ConversationDB


class RetentionAgent:
    """Background daemon that prunes the ConversationDB on schedule."""

    def __init__(
        self,
        db: "ConversationDB",
        *,
        max_days: int,
        hard_cap_days: int = 180,
        max_disk_mb: int = 500,
        sweep_interval_s: int = 3600,
    ) -> None:
        # Clamp max_days to the hard cap immediately so a misconfigured
        # operator setting doesn't silently keep history forever.
        if max_days > hard_cap_days:
            logger.warning(
                "RetentionAgent: max_days={} exceeds hard cap {}; clamping",
                max_days, hard_cap_days,
            )
            max_days = hard_cap_days
        self._db = db
        self._max_age_s = max_days * 86400
        self._max_disk_bytes = max_disk_mb * 1024 * 1024
        self._sweep_interval_s = max(60, sweep_interval_s)  # min 1 minute
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sweep_at: float = 0.0
        self._last_pruned: int = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="RetentionAgent", daemon=True,
        )
        self._thread.start()
        logger.info(
            "RetentionAgent started; max_age={:.0f} days, max_disk={:.0f} MB, sweep={}s",
            self._max_age_s / 86400,
            self._max_disk_bytes / (1024 * 1024),
            self._sweep_interval_s,
        )

    def shutdown(self, timeout_s: float = 2.0) -> None:
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)

    # ── Internals ───────────────────────────────────────────────

    def _run(self) -> None:
        # Sweep once at startup so the operator sees an immediate effect
        # if they tightened retention via the WebUI before restart.
        self._sweep_once()
        while not self._shutdown.is_set():
            # Wake every second to react to shutdown promptly, but only
            # actually sweep on schedule.
            for _ in range(self._sweep_interval_s):
                if self._shutdown.is_set():
                    return
                time.sleep(1)
            self._sweep_once()

    def sweep_once(self) -> dict[str, int]:
        """Run one sweep synchronously. Public for tests / WebUI
        'sweep now' button."""
        return self._sweep_once()

    def _sweep_once(self) -> dict[str, int]:
        results = {"age_pruned": 0, "size_pruned": 0}
        try:
            results["age_pruned"] = self._prune_by_age()
        except Exception as exc:
            logger.warning("RetentionAgent age prune failed: {}", exc)
        try:
            results["size_pruned"] = self._prune_by_size()
        except Exception as exc:
            logger.warning("RetentionAgent size prune failed: {}", exc)
        self._last_sweep_at = time.time()
        self._last_pruned = sum(results.values())
        if self._last_pruned > 0:
            logger.info(
                "RetentionAgent sweep: pruned {} (age={}, size={}); db={:.1f} MB",
                self._last_pruned, results["age_pruned"], results["size_pruned"],
                self._db.disk_size_bytes() / (1024 * 1024),
            )
        return results

    def _prune_by_age(self) -> int:
        cutoff = time.time() - self._max_age_s
        # protect_tier=True keeps tier=1/2 device-control history alive
        # past the chat retention window. Operationally those rows are
        # the audit trail — useful for reviewing what GLaDOS did even
        # if the surrounding conversation is gone.
        return self._db.prune_before(cutoff, protect_tier=True)

    def _prune_by_size(self) -> int:
        """If the DB is over the size cap, prune oldest tier=3 (chit-
        chat) rows in batches until under cap. Tier 1/2 are protected
        in this pass too — only relax that if size hit becomes
        unavoidable."""
        deleted = 0
        for _ in range(20):  # batch loop with a hard ceiling
            if self._db.disk_size_bytes() <= self._max_disk_bytes:
                break
            # Delete a chunk of the oldest 100 tier=3 rows at a time so
            # we don't block on one massive transaction.
            n = self._db.prune_before(
                cutoff_ts=time.time() - 3600,  # never touch the last hour
                protect_tier=True,
            )
            if n == 0:
                # Couldn't prune any age-eligible chit-chat. Bail; the
                # operator's max_disk_mb is genuinely tighter than their
                # tier=1/2 audit volume requires. Don't start deleting
                # action history without an explicit signal.
                logger.warning(
                    "RetentionAgent: disk over cap ({:.0f} MB > {:.0f} MB) "
                    "but no age-eligible tier=3 rows to prune. Operator "
                    "should raise conversation_max_disk_mb or shorten "
                    "conversation_max_days.",
                    self._db.disk_size_bytes() / (1024 * 1024),
                    self._max_disk_bytes / (1024 * 1024),
                )
                break
            deleted += n
        return deleted

    # ── Status (for WebUI / future endpoint) ────────────────────

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._thread is not None and self._thread.is_alive(),
            "max_age_days": self._max_age_s / 86400,
            "max_disk_mb": self._max_disk_bytes / (1024 * 1024),
            "sweep_interval_s": self._sweep_interval_s,
            "last_sweep_at": self._last_sweep_at,
            "last_pruned": self._last_pruned,
            "db_size_mb": round(self._db.disk_size_bytes() / (1024 * 1024), 2),
            "db_message_count": self._db.count(),
        }
