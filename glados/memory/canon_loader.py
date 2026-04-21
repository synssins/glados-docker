"""
Portal canon loader (Phase 8.14).

Curated canonical Portal 1/2 event summaries live under
``configs/canon/<topic>.txt``, one 2-3 sentence entry per blank-line-
separated block. ``load_canon_from_configs(memory_store)`` walks that
directory, hashes each block to a stable id, and writes entries into
the ChromaDB semantic collection with metadata that marks them as
canon so they are retrieved only by :class:`CanonContext`, not by
the user-fact :class:`MemoryContext`.

Idempotent: re-running the loader against the same content is a
no-op. Edits in the WebUI trigger a live reload via the API handler.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from loguru import logger


# Entries in a canon file are separated by blank lines. ``#`` comment
# lines are dropped inside a block (anywhere — leading or mid-block).
_COMMENT_RE = re.compile(r"^\s*#.*$", re.MULTILINE)


def parse_canon_file(path: Path) -> list[str]:
    """Parse a single ``.txt`` canon file into a list of entry strings.

    Comment lines (``# …``) are stripped. Blank lines separate entries.
    Whitespace is normalised but line breaks inside an entry are
    preserved as spaces so retrieval sees a compact single-line
    statement (ChromaDB's embedding works better on continuous text).
    """
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("canon_loader: failed to read {}: {}", path, exc)
        return []
    cleaned = _COMMENT_RE.sub("", raw)
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", cleaned):
        flat = " ".join(block.split())
        if flat:
            blocks.append(flat)
    return blocks


def _entry_id(topic: str, text: str) -> str:
    """Deterministic id: ``canon_<topic>_<sha256[:12]>``. Stable across
    runs so re-loads are idempotent unless the text itself changes."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"canon_{topic}_{digest}"


def load_canon_from_configs(
    memory_store: Any,
    canon_dir: Path | None = None,
) -> dict[str, int]:
    """Walk ``canon_dir`` and write each entry into the memory store.

    Idempotent: entries with an unchanged id are skipped. Returns a
    dict ``{topic: count}`` of entries processed per file (whether
    added or already-present). Returns an empty dict if ``memory_store``
    is None or the directory does not exist.
    """
    if memory_store is None:
        return {}
    base = canon_dir or Path("configs/canon")
    if not base.is_dir():
        logger.debug("canon_loader: no canon dir at {}", base)
        return {}

    # Fast-path existence check — ChromaDB's ``get(ids=[...])`` returns
    # only ids that already exist, so we can batch-query the collection
    # once per topic rather than one get-per-entry.
    def _existing_ids(ids: list[str]) -> set[str]:
        if not ids:
            return set()
        try:
            col = memory_store._get_collection("semantic")
            found = col.get(ids=ids)
            return set(found.get("ids") or [])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("canon_loader: existence check failed: {}", exc)
            return set()

    totals: dict[str, int] = {}
    for fp in sorted(base.glob("*.txt")):
        topic = fp.stem
        entries = parse_canon_file(fp)
        if not entries:
            continue
        proposed: list[tuple[str, str]] = [
            (_entry_id(topic, e), e) for e in entries
        ]
        already = _existing_ids([i for i, _ in proposed])
        added = 0
        for entry_id, text in proposed:
            if entry_id in already:
                continue
            memory_store.add_semantic(
                text=text,
                metadata={
                    "source": "canon",
                    "topic": topic,
                    # ``review_status="canon"`` keeps these entries out of
                    # :class:`MemoryContext`'s user-fact filter while
                    # still letting ChromaDB's where-clause target them
                    # for canon retrieval.
                    "review_status": "canon",
                    "canon_version": 1,
                },
                entry_id=entry_id,
            )
            added += 1
        totals[topic] = added
        if added:
            logger.info(
                "canon_loader: {} — added {} / {} entries",
                topic, added, len(proposed),
            )
    return totals


# ── Singleton-style helpers for the reload endpoint ─────────────

_reload_lock = threading.Lock()


def reload_canon(memory_store: Any, canon_dir: Path | None = None) -> dict[str, int]:
    """Thread-safe re-load entry point for the WebUI save path.

    Idempotent — same semantics as ``load_canon_from_configs``. The
    lock protects against concurrent saves interleaving their
    existence checks.
    """
    with _reload_lock:
        return load_canon_from_configs(memory_store, canon_dir)
