"""Phase 8.14 — canon loader unit tests.

Covers the pure-function bits of ``glados.memory.canon_loader``:
blank-line splitting, comment stripping, stable hashed-id generation,
idempotent re-loads, and the batch existence check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from glados.memory.canon_loader import (
    _entry_id,
    load_canon_from_configs,
    parse_canon_file,
    reload_canon,
)


class TestParseCanonFile:
    def test_blank_line_splits_entries(self, tmp_path: Path) -> None:
        p = tmp_path / "topic.txt"
        p.write_text("First.\n\nSecond.\n\nThird.\n", encoding="utf-8")
        assert parse_canon_file(p) == ["First.", "Second.", "Third."]

    def test_comment_lines_stripped(self, tmp_path: Path) -> None:
        p = tmp_path / "topic.txt"
        p.write_text(
            "# header comment\n"
            "Entry one.\n"
            "\n"
            "  # indented comment mid-file\n"
            "Entry two.\n",
            encoding="utf-8",
        )
        assert parse_canon_file(p) == ["Entry one.", "Entry two."]

    def test_multiline_entry_flattened_to_single_line(self, tmp_path: Path) -> None:
        p = tmp_path / "topic.txt"
        p.write_text(
            "A sentence that\nwraps across\nthree lines.\n\nSecond entry.\n",
            encoding="utf-8",
        )
        got = parse_canon_file(p)
        assert got == [
            "A sentence that wraps across three lines.",
            "Second entry.",
        ]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert parse_canon_file(tmp_path / "nope.txt") == []

    def test_all_comments_and_blanks_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "topic.txt"
        p.write_text("# only\n# comments\n\n\n", encoding="utf-8")
        assert parse_canon_file(p) == []

    def test_shipped_canon_files_parse_non_empty(self) -> None:
        shipped = Path("configs/canon")
        if not shipped.is_dir():
            pytest.skip("shipped canon dir not in this workspace")
        any_non_empty = False
        for fp in shipped.glob("*.txt"):
            entries = parse_canon_file(fp)
            if entries:
                any_non_empty = True
                # Every shipped entry should have real content — no stray
                # empty strings slipping through the splitter.
                assert all(e.strip() for e in entries), fp.name
        assert any_non_empty, "expected at least one shipped canon file to parse entries"


class TestEntryId:
    def test_stable_across_calls(self) -> None:
        a = _entry_id("glados_arc", "Canonical fact.")
        b = _entry_id("glados_arc", "Canonical fact.")
        assert a == b
        assert a.startswith("canon_glados_arc_")
        assert len(a.rsplit("_", 1)[1]) == 12  # 12-hex-digit suffix

    def test_different_text_different_id(self) -> None:
        assert _entry_id("topic", "A.") != _entry_id("topic", "B.")

    def test_different_topic_different_id(self) -> None:
        assert _entry_id("a", "X.") != _entry_id("b", "X.")


class _FakeCollection:
    """Minimal stand-in for a ChromaDB Collection that supports the
    subset of the API our loader touches: ``get(ids=[...])`` and
    ``add(ids=..., documents=..., metadatas=...)``."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, ids: list[str] | None = None) -> dict[str, Any]:
        hits = [i for i in (ids or []) if i in self.rows]
        return {"ids": hits}

    def add(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = {"document": d, "metadata": dict(m)}


class _FakeMemoryStore:
    """Fake MemoryStore matching the subset of the real API that
    ``load_canon_from_configs`` uses: ``_get_collection()`` and
    ``add_semantic()``."""

    def __init__(self) -> None:
        self._collections = {"semantic": _FakeCollection()}
        self.adds: list[dict[str, Any]] = []

    def _get_collection(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())

    def add_semantic(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> str:
        assert entry_id, "canon_loader must pass a stable id"
        meta = dict(metadata or {})
        self._collections["semantic"].add(
            ids=[entry_id],
            documents=[text],
            metadatas=[meta],
        )
        self.adds.append({"id": entry_id, "text": text, "metadata": meta})
        return entry_id


@pytest.fixture
def canon_workspace(tmp_path: Path) -> Path:
    d = tmp_path / "canon"
    d.mkdir()
    (d / "alpha.txt").write_text(
        "# header\n"
        "Alpha entry one.\n"
        "\n"
        "Alpha entry two.\n",
        encoding="utf-8",
    )
    (d / "beta.txt").write_text(
        "Beta entry one.\n", encoding="utf-8",
    )
    return d


class TestLoadCanonFromConfigs:
    def test_loads_all_entries_first_run(self, canon_workspace: Path) -> None:
        store = _FakeMemoryStore()
        result = load_canon_from_configs(store, canon_dir=canon_workspace)
        assert result == {"alpha": 2, "beta": 1}
        assert len(store.adds) == 3
        # Metadata is tagged so canon retrieval can filter and
        # MemoryContext's user-fact retrieval excludes them.
        for row in store.adds:
            assert row["metadata"]["source"] == "canon"
            assert row["metadata"]["review_status"] == "canon"
            assert row["metadata"]["topic"] in {"alpha", "beta"}

    def test_idempotent_second_run_adds_nothing(self, canon_workspace: Path) -> None:
        store = _FakeMemoryStore()
        load_canon_from_configs(store, canon_dir=canon_workspace)
        second = load_canon_from_configs(store, canon_dir=canon_workspace)
        assert second == {"alpha": 0, "beta": 0}
        assert len(store.adds) == 3  # unchanged

    def test_edit_triggers_new_entry(self, canon_workspace: Path) -> None:
        store = _FakeMemoryStore()
        load_canon_from_configs(store, canon_dir=canon_workspace)
        # Edit an entry — the hash changes, so the new text is added
        # (old entry is effectively orphaned in the collection, which
        # is acceptable per the WebUI's "file-oriented, not entry-
        # oriented" design).
        (canon_workspace / "beta.txt").write_text(
            "Beta entry one — amended.\n", encoding="utf-8",
        )
        result = load_canon_from_configs(store, canon_dir=canon_workspace)
        assert result == {"alpha": 0, "beta": 1}
        assert any("amended" in row["text"] for row in store.adds)

    def test_missing_store_is_noop(self, canon_workspace: Path) -> None:
        assert load_canon_from_configs(None, canon_dir=canon_workspace) == {}

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        store = _FakeMemoryStore()
        assert load_canon_from_configs(store, canon_dir=tmp_path / "nope") == {}
        assert store.adds == []

    def test_empty_files_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / "canon"
        d.mkdir()
        (d / "empty.txt").write_text("# only a comment\n", encoding="utf-8")
        (d / "real.txt").write_text("One entry.\n", encoding="utf-8")
        store = _FakeMemoryStore()
        result = load_canon_from_configs(store, canon_dir=d)
        # The empty file is not listed in the result dict because no
        # entries were proposed at all — matches the quip editor's
        # "invisible empty files" UX.
        assert result == {"real": 1}


class TestReloadCanon:
    def test_reload_matches_load_semantics(self, canon_workspace: Path) -> None:
        store = _FakeMemoryStore()
        reload_canon(store, canon_dir=canon_workspace)
        # Re-entrance yields no duplicate adds.
        reload_canon(store, canon_dir=canon_workspace)
        unique_ids = {row["id"] for row in store.adds}
        assert len(unique_ids) == len(store.adds) == 3
