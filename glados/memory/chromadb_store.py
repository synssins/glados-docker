"""
Shared ChromaDB memory store for GLaDOS episodic and semantic memory.

Two collections:
  - ``episodic`` — timestamped events with configurable TTL
  - ``semantic`` — persistent facts and conversation memories

All services import this module for memory read/write/retrieval.

Security notes (VibeSec):
  - All text inputs are sanitized before storage (strip control chars)
  - Metadata values are type-checked before insertion
  - ChromaDB connection details come from config, never hardcoded
  - No secrets are stored in or retrieved from memory collections
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings
from loguru import logger


# -- Security: strip control characters from text before storage
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(text: str, max_length: int = 8192) -> str:
    """Remove control characters and enforce length limit.

    VibeSec: input validation — never store raw unsanitized user input.
    """
    cleaned = _CONTROL_CHAR_RE.sub("", text)
    return cleaned[:max_length]


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Ensure metadata values are safe primitive types for ChromaDB.

    VibeSec: type validation — prevent injection via metadata fields.
    """
    safe: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)):
            if isinstance(v, str):
                v = _sanitize_text(v, max_length=1024)
            safe[str(k)[:128]] = v
    return safe


class MemoryStore:
    """Thread-safe ChromaDB memory interface.

    Usage::

        from glados.memory import MemoryStore
        mem = MemoryStore(host="localhost", port=8000)

        # Write episodic event
        mem.add_episodic("Front door opened", {"entity": "binary_sensor.front_door"})

        # Write semantic fact
        mem.add_semantic("ResidentA prefers lights at 40% brightness in the evening")

        # Query relevant memories for context injection
        results = mem.query("What does ResidentA like?", collection="semantic", n=5)
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
    ) -> None:
        # VibeSec: connection details from caller (config), not hardcoded
        self._host = host
        self._port = port
        self._client: chromadb.HttpClient | None = None
        self._collections: dict[str, Any] = {}

    def _get_client(self) -> chromadb.HttpClient:
        """Lazy-connect to ChromaDB with connection validation."""
        if self._client is None:
            self._client = chromadb.HttpClient(
                host=self._host,
                port=self._port,
                settings=ChromaSettings(
                    anonymized_telemetry=False,
                ),
            )
            logger.info("ChromaDB connected at {}:{}", self._host, self._port)
        return self._client

    def _get_collection(self, name: str) -> Any:
        """Get or create a collection by name."""
        if name not in self._collections:
            client = self._get_client()
            self._collections[name] = client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.debug("ChromaDB collection ready: {}", name)
        return self._collections[name]

    # ── Write operations ──────────────────────────────────────────

    def add_episodic(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Store a timestamped episodic event.

        Returns the generated entry ID.
        """
        entry_id = entry_id or f"ep_{uuid.uuid4().hex[:16]}"
        safe_text = _sanitize_text(text)
        if not safe_text.strip():
            logger.warning("Skipping empty episodic entry")
            return entry_id

        meta = _sanitize_metadata(metadata or {})
        meta["timestamp"] = time.time()
        meta["collection_type"] = self.EPISODIC

        col = self._get_collection(self.EPISODIC)
        col.add(
            ids=[entry_id],
            documents=[safe_text],
            metadatas=[meta],
        )
        logger.debug("Episodic memory stored: {} ({} chars)", entry_id, len(safe_text))
        return entry_id

    def add_semantic(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Store a persistent semantic memory (fact, preference, summary).

        Returns the generated entry ID.
        """
        entry_id = entry_id or f"sem_{uuid.uuid4().hex[:16]}"
        safe_text = _sanitize_text(text)
        if not safe_text.strip():
            logger.warning("Skipping empty semantic entry")
            return entry_id

        meta = _sanitize_metadata(metadata or {})
        meta["timestamp"] = time.time()
        meta["collection_type"] = self.SEMANTIC

        col = self._get_collection(self.SEMANTIC)
        col.add(
            ids=[entry_id],
            documents=[safe_text],
            metadatas=[meta],
        )
        logger.debug("Semantic memory stored: {} ({} chars)", entry_id, len(safe_text))
        return entry_id

    # ── Read operations ───────────────────────────────────────────

    def query(
        self,
        text: str,
        collection: str = "semantic",
        n: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query a collection for entries similar to *text*.

        Returns a list of dicts with keys: id, document, metadata, distance.
        """
        safe_text = _sanitize_text(text)
        if not safe_text.strip():
            return []

        col = self._get_collection(collection)

        try:
            count = col.count()
            if count == 0:
                return []
            # Don't request more results than exist
            effective_n = min(n, count)
            kwargs: dict[str, Any] = {
                "query_texts": [safe_text],
                "n_results": effective_n,
            }
            if where:
                kwargs["where"] = _sanitize_metadata(where)

            results = col.query(**kwargs)
        except Exception as exc:
            # VibeSec: never leak internal error details to callers
            logger.error("ChromaDB query failed: {}", exc)
            return []

        entries = []
        if results and results.get("ids"):
            ids = results["ids"][0]
            docs = results["documents"][0] if results.get("documents") else [""] * len(ids)
            metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(ids)
            dists = results["distances"][0] if results.get("distances") else [0.0] * len(ids)
            for i, entry_id in enumerate(ids):
                entries.append({
                    "id": entry_id,
                    "document": docs[i],
                    "metadata": metas[i],
                    "distance": dists[i],
                })
        return entries

    def get_episodic_since(self, since_timestamp: float) -> list[dict[str, Any]]:
        """Get all episodic entries newer than *since_timestamp*."""
        col = self._get_collection(self.EPISODIC)
        try:
            count = col.count()
            if count == 0:
                return []
            results = col.get(
                where={"timestamp": {"$gt": since_timestamp}},
            )
        except Exception as exc:
            logger.error("Failed to get episodic entries: {}", exc)
            return []

        entries = []
        if results and results.get("ids"):
            for i, entry_id in enumerate(results["ids"]):
                entries.append({
                    "id": entry_id,
                    "document": results["documents"][i] if results.get("documents") else "",
                    "metadata": results["metadatas"][i] if results.get("metadatas") else {},
                })
        return entries

    def get_episodic_before(self, before_timestamp: float) -> list[dict[str, Any]]:
        """Get all episodic entries older than *before_timestamp*."""
        col = self._get_collection(self.EPISODIC)
        try:
            count = col.count()
            if count == 0:
                return []
            results = col.get(
                where={"timestamp": {"$lt": before_timestamp}},
            )
        except Exception as exc:
            logger.error("Failed to get old episodic entries: {}", exc)
            return []

        entries = []
        if results and results.get("ids"):
            for i, entry_id in enumerate(results["ids"]):
                entries.append({
                    "id": entry_id,
                    "document": results["documents"][i] if results.get("documents") else "",
                    "metadata": results["metadatas"][i] if results.get("metadatas") else {},
                })
        return entries

    def delete_ids(self, collection: str, ids: list[str]) -> int:
        """Delete entries by ID from a collection. Returns count deleted."""
        if not ids:
            return 0
        col = self._get_collection(collection)
        try:
            col.delete(ids=ids)
            logger.debug("Deleted {} entries from {}", len(ids), collection)
            return len(ids)
        except Exception as exc:
            logger.error("Failed to delete from {}: {}", collection, exc)
            return 0

    # ── Review queue (Stage 3 Phase D) ───────────────────────────

    def list_by_status(
        self,
        review_status: str,
        collection: str = "semantic",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return entries whose `review_status` metadata matches.

        Used by the review queue UI to show pending facts the operator
        needs to triage. `review_status` values: "approved", "pending",
        "rejected". Legacy entries (pre-Phase D) have no review_status
        and are NOT returned by this method — query them by passing
        review_status="" or by using the existing query() method
        without filter."""
        col = self._get_collection(collection)
        try:
            count = col.count()
            if count == 0:
                return []
            results = col.get(
                where={"review_status": review_status},
                limit=limit,
            )
        except Exception as exc:
            logger.error("Failed to list by status: {}", exc)
            return []

        entries: list[dict[str, Any]] = []
        if results and results.get("ids"):
            for i, entry_id in enumerate(results["ids"]):
                entries.append({
                    "id": entry_id,
                    "document": (results["documents"][i]
                                 if results.get("documents") else ""),
                    "metadata": (results["metadatas"][i]
                                 if results.get("metadatas") else {}),
                })
        return entries

    def get_by_id(
        self, entry_id: str, collection: str = "semantic",
    ) -> dict[str, Any] | None:
        """Fetch a single entry by id, or None if not found."""
        col = self._get_collection(collection)
        try:
            results = col.get(ids=[entry_id])
        except Exception as exc:
            logger.error("Failed to get_by_id: {}", exc)
            return None
        if not results or not results.get("ids") or not results["ids"]:
            return None
        return {
            "id": results["ids"][0],
            "document": (results["documents"][0]
                         if results.get("documents") else ""),
            "metadata": (results["metadatas"][0]
                         if results.get("metadatas") else {}),
        }

    def update(
        self,
        entry_id: str,
        collection: str = "semantic",
        *,
        document: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> bool:
        """Update document text and/or merge metadata for an existing
        entry. Used by the review queue's promote/demote/edit actions."""
        existing = self.get_by_id(entry_id, collection)
        if existing is None:
            return False
        col = self._get_collection(collection)
        new_doc = document if document is not None else existing.get("document", "")
        merged_meta = dict(existing.get("metadata") or {})
        if metadata_updates:
            merged_meta.update(_sanitize_metadata(metadata_updates))
        merged_meta["updated_at"] = time.time()
        try:
            col.update(
                ids=[entry_id],
                documents=[_sanitize_text(new_doc)] if document is not None else None,
                metadatas=[_sanitize_metadata(merged_meta)],
            )
            return True
        except Exception as exc:
            logger.error("Failed to update {}: {}", entry_id, exc)
            return False

    # ── Health ────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Return True if ChromaDB is reachable."""
        try:
            client = self._get_client()
            client.heartbeat()
            return True
        except Exception:
            return False
