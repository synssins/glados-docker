"""Tests for Stage 3 Phase 5 memory HTTP endpoints.

Covers the thin handler methods that wrap MemoryStore operations:
  POST /api/memory/add           → _post_memory_add
  POST /api/memory/<id>/promote  → _memory_action("promote")
  POST /api/memory/<id>/demote   → _memory_action("demote")
  POST /api/memory/<id>/edit     → _memory_action("edit")
  DELETE /api/memory/<id>        → _memory_action("delete")

The existing review-queue tests in test_memory_review.py cover the
write_fact() contract; this module exercises the HTTP layer that the
Phase 5 WebUI sits on top of.
"""

from __future__ import annotations

import io
import json
import time
from typing import Any
from unittest.mock import patch

import pytest

from glados.webui.tts_ui import Handler


class _FakeStore:
    """Records the Handler's MemoryStore calls so we can assert on the
    HTTP→store contract without involving ChromaDB."""

    def __init__(self) -> None:
        self.semantic: list[dict[str, Any]] = []
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.delete_calls: list[tuple[str, list[str]]] = []

    def add_semantic(self, text: str, metadata: dict[str, Any] | None = None,
                     entry_id: str | None = None) -> str:
        eid = entry_id or f"sem_{len(self.semantic)}"
        meta = dict(metadata or {})
        meta.setdefault("timestamp", time.time())
        self.semantic.append({"id": eid, "document": text, "metadata": meta})
        return eid

    def query(self, text: str, collection: str = "semantic",
              n: int = 5, where: dict[str, Any] | None = None
              ) -> list[dict[str, Any]]:
        return []  # endpoint-level tests don't exercise dedup

    def update(self, entry_id: str, collection: str = "semantic",
               *, document: str | None = None,
               metadata_updates: dict[str, Any] | None = None) -> bool:
        self.update_calls.append(
            (entry_id, dict(metadata_updates or {}), document),
        )
        return True

    def delete_ids(self, collection: str, ids: list[str]) -> int:
        self.delete_calls.append((collection, list(ids)))
        return len(ids)


def _make_handler(path: str, body: bytes = b"", store: _FakeStore | None = None) -> Handler:
    """Build a Handler WITHOUT calling BaseHTTPRequestHandler.__init__
    (which would require a socket). We set the attributes the dispatcher
    and response helpers read from."""
    h = Handler.__new__(Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    # send_response / send_header / end_headers on BaseHTTPRequestHandler
    # write to self.wfile too; stub them out so we can read the response
    # body cleanly as a single JSON blob.
    h._status_code = None

    def _send_response(code: int, *_a, **_k):
        h._status_code = code
    h.send_response = _send_response  # type: ignore[method-assign]
    h.send_header = lambda *a, **k: None  # type: ignore[method-assign]
    h.end_headers = lambda: None  # type: ignore[method-assign]

    # Patch the memory-store lookup so tests don't need a running engine.
    h._memory_store = lambda: store  # type: ignore[method-assign]
    return h


def _response(h: Handler) -> tuple[int, dict]:
    return h._status_code, json.loads(h.wfile.getvalue().decode("utf-8"))


class TestMemoryAdd:
    def test_add_happy_path(self) -> None:
        store = _FakeStore()
        body = json.dumps({"document": "The operator prefers 40% lights",
                           "importance": 0.9}).encode()
        h = _make_handler("/api/memory/add", body, store)
        h._post_memory_add()
        status, payload = _response(h)
        assert status == 200
        assert payload["added"] is True
        assert len(store.semantic) == 1
        meta = store.semantic[0]["metadata"]
        # Operator-added facts land approved + explicit-sourced.
        assert meta["review_status"] == "approved"
        assert meta["source"] == "user_explicit"
        assert meta["importance"] == 0.9

    def test_add_rejects_empty_document(self) -> None:
        store = _FakeStore()
        body = json.dumps({"document": "   "}).encode()
        h = _make_handler("/api/memory/add", body, store)
        h._post_memory_add()
        status, payload = _response(h)
        assert status == 400
        assert store.semantic == []

    def test_add_503_when_memory_store_unavailable(self) -> None:
        body = json.dumps({"document": "anything"}).encode()
        h = _make_handler("/api/memory/add", body, store=None)
        h._post_memory_add()
        status, payload = _response(h)
        assert status == 503


class TestMemoryAction:
    def test_promote_updates_review_status(self) -> None:
        store = _FakeStore()
        h = _make_handler("/api/memory/sem_abc/promote", store=store)
        h._memory_action("promote")
        status, _ = _response(h)
        assert status == 200
        assert store.update_calls == [
            ("sem_abc", {"review_status": "approved"}, None),
        ]

    def test_delete_calls_delete_ids(self) -> None:
        store = _FakeStore()
        h = _make_handler("/api/memory/sem_xyz", store=store)
        h._memory_action("delete")
        status, payload = _response(h)
        assert status == 200
        assert store.delete_calls == [("semantic", ["sem_xyz"])]
        assert payload["deleted"] == 1
