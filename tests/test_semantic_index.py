"""Tests for glados.ha.semantic_index — Phase 8.3.

This file covers the shipping units of 8.3.1:
  - `build_entity_document` shape (canonical embedding text)
  - `is_semantic_retrieval_available` gating logic
  - `SemanticIndex` registry apply/skeleton API

The `Embedder` class wraps BGE-small ONNX and can only run when the
model files are present on disk — tests that exercise it are
guarded by `pytest.importorskip` + a file-existence check so CI
(where the 130 MB ONNX isn't shipped) simply skips them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.ha.semantic_index import (
    DEFAULT_MODEL_PATH,
    DEFAULT_TOKENIZER_PATH,
    SemanticIndex,
    build_entity_document,
    is_semantic_retrieval_available,
)


# ──────────────────────────────────────────────────────────────
# Document shape — verbatim in the WebUI Candidate retrieval card
# ──────────────────────────────────────────────────────────────

class TestBuildEntityDocument:
    def test_minimum_entity_uses_friendly_name_only(self) -> None:
        doc = build_entity_document(
            friendly_name="Office Desk Monitor Lamp",
            entity_id="light.task_lamp_one",
            domain="light",
        )
        # Friendly name must come first (BGE weights leading clause).
        assert doc.startswith("Office Desk Monitor Lamp")
        assert "domain=light" in doc

    def test_full_shape_contains_all_provided_facets(self) -> None:
        doc = build_entity_document(
            friendly_name="Ceiling Light",
            entity_id="light.kitchen_ceiling",
            domain="light",
            device_class="light",
            area_name="Kitchen",
            floor_name="Main",
            device_name="Kitchen Zooz Dimmer",
            aliases=["overhead", "kitchen main"],
        )
        assert doc.startswith("Ceiling Light")
        assert "aliases=overhead, kitchen main" in doc
        assert "area=Kitchen" in doc
        assert "floor=Main" in doc
        assert "device_class=light" in doc
        assert "device_name=Kitchen Zooz Dimmer" in doc

    def test_missing_friendly_name_falls_back_to_entity_id(self) -> None:
        doc = build_entity_document(
            friendly_name="",
            entity_id="light.mystery_strip_seg_3",
            domain="light",
        )
        # Fallback humanises the entity_id tail so the embedding has
        # a shot at matching "mystery strip".
        assert doc.startswith("mystery strip seg 3")

    def test_aliases_empty_list_ignored(self) -> None:
        doc = build_entity_document(
            friendly_name="X",
            entity_id="light.x",
            domain="light",
            aliases=[],
        )
        assert "aliases=" not in doc

    def test_facets_order_is_stable(self) -> None:
        # Document shape must be stable across runs — operators (and
        # the on-disk embedding cache) rely on it. Any reorder
        # invalidates persisted embeddings.
        d1 = build_entity_document(
            friendly_name="A", entity_id="light.a", domain="light",
            area_name="Living", floor_name="Main", device_class="light",
            device_name="Inovelli Red", aliases=["foo"],
        )
        d2 = build_entity_document(
            friendly_name="A", entity_id="light.a", domain="light",
            area_name="Living", floor_name="Main", device_class="light",
            device_name="Inovelli Red", aliases=["foo"],
        )
        assert d1 == d2
        # Explicit order check: name → aliases → area → floor →
        # domain → device_class → device_name.
        expected_sequence = [
            "A", "aliases=foo", "area=Living", "floor=Main",
            "domain=light", "device_class=light", "device_name=Inovelli Red",
        ]
        last_idx = -1
        for token in expected_sequence:
            idx = d1.find(token)
            assert idx > last_idx, f"{token!r} out of order in {d1!r}"
            last_idx = idx


# ──────────────────────────────────────────────────────────────
# Availability gate — drives the fuzzy fallback decision
# ──────────────────────────────────────────────────────────────

class TestAvailabilityGate:
    def test_missing_files_returns_false(self, tmp_path: Path) -> None:
        assert not is_semantic_retrieval_available(
            model_path=tmp_path / "nope.onnx",
            tokenizer_path=tmp_path / "nope.json",
        )

    def test_present_files_returns_true_when_deps_loaded(
        self, tmp_path: Path,
    ) -> None:
        # Fake files — the gate only checks existence here, not
        # validity. The Embedder constructor will fail loud if the
        # file isn't a real model; that is tested in the live suite.
        (tmp_path / "model.onnx").write_bytes(b"fake")
        (tmp_path / "tokenizer.json").write_text("{}")
        # This test only makes sense when numpy + ort + tokenizers
        # are installed. On a bare dev env, skip rather than assert.
        pytest.importorskip("numpy")
        pytest.importorskip("onnxruntime")
        pytest.importorskip("tokenizers")
        assert is_semantic_retrieval_available(
            model_path=tmp_path / "model.onnx",
            tokenizer_path=tmp_path / "tokenizer.json",
        )


# ──────────────────────────────────────────────────────────────
# Registry application — lightweight, testable in CI
# ──────────────────────────────────────────────────────────────

class _StubCache:
    """Minimal EntityCache stand-in — SemanticIndex only pokes at it
    during build/retrieve, both of which land in Phase 8.3.2."""
    def snapshot(self):
        return []


class TestRegistryApply:
    def test_area_registry_caches_id_to_name_and_floor(self) -> None:
        idx = SemanticIndex(
            _StubCache(),
            model_path="/nonexistent/model.onnx",
            tokenizer_path="/nonexistent/tok.json",
        )
        n = idx.apply_area_registry([
            {"area_id": "kitchen", "name": "Kitchen", "floor_id": "main"},
            {"area_id": "office", "name": "Office", "floor_id": "main"},
            {"area_id": "attic", "name": "Attic"},  # no floor
        ])
        assert n == 3
        assert idx._area_names["kitchen"] == "Kitchen"
        assert idx._area_floor["kitchen"] == "main"
        assert "attic" not in idx._area_floor

    def test_device_registry_prefers_user_name(self) -> None:
        idx = SemanticIndex(
            _StubCache(),
            model_path="/nonexistent/model.onnx",
            tokenizer_path="/nonexistent/tok.json",
        )
        n = idx.apply_device_registry([
            {"id": "dev1", "name": "Zooz ZEN30", "name_by_user": "Kitchen Combo"},
            {"id": "dev2", "name": "Inovelli Red"},
            {"id": "dev3"},  # no names at all → fallback to id
        ])
        assert n == 3
        assert idx._device_names["dev1"] == "Kitchen Combo"  # user override wins
        assert idx._device_names["dev2"] == "Inovelli Red"
        assert idx._device_names["dev3"] == "dev3"

    def test_floor_registry_stores_names(self) -> None:
        idx = SemanticIndex(
            _StubCache(),
            model_path="/nonexistent/model.onnx",
            tokenizer_path="/nonexistent/tok.json",
        )
        n = idx.apply_floor_registry([
            {"floor_id": "main", "name": "Main Floor"},
            {"floor_id": "upstairs", "name": "Upstairs"},
        ])
        assert n == 2
        assert idx._floor_names["main"] == "Main Floor"

    def test_build_is_skeleton_in_this_commit(self) -> None:
        # Explicit check that 8.3.1 ships a stub; 8.3.2 fills it in.
        # This catches accidental early use by callers.
        idx = SemanticIndex(
            _StubCache(),
            model_path="/nonexistent/model.onnx",
            tokenizer_path="/nonexistent/tok.json",
        )
        with pytest.raises(NotImplementedError):
            idx.build()
        with pytest.raises(NotImplementedError):
            idx.retrieve("anything")


# ──────────────────────────────────────────────────────────────
# Live Embedder — only runs if the BGE-small ONNX is present
# ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not Path(DEFAULT_MODEL_PATH).exists()
    or not Path(DEFAULT_TOKENIZER_PATH).exists(),
    reason="BGE-small model files not present on this host; skipped in CI.",
)
class TestEmbedderLive:
    def test_embeddings_are_normalized_384d(self) -> None:
        pytest.importorskip("numpy")
        pytest.importorskip("onnxruntime")
        pytest.importorskip("tokenizers")
        import numpy as np

        from glados.ha.semantic_index import Embedder
        emb = Embedder()
        vecs = emb.embed([
            "Office Desk Monitor Lamp",
            "Kitchen Ceiling Light",
        ])
        assert vecs.shape == (2, 384)
        # L2-normalized → every row has unit norm (within float tol).
        norms = np.linalg.norm(vecs, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)

    def test_query_document_similarity_ordering(self) -> None:
        pytest.importorskip("numpy")
        pytest.importorskip("onnxruntime")
        pytest.importorskip("tokenizers")
        import numpy as np

        from glados.ha.semantic_index import Embedder
        emb = Embedder()
        docs = emb.embed([
            "Office Desk Monitor Lamp | area=Office | domain=light",
            "Kitchen Ceiling Light | area=Kitchen | domain=light",
            "Bedroom Fan | area=Bedroom | domain=fan",
        ])
        q = emb.embed(["desk lamp"], is_query=True)
        # dot == cosine because both sides are L2-normalized.
        sims = (docs @ q.T).flatten()
        # The "Office Desk Monitor Lamp" doc must rank highest for
        # the "desk lamp" query. Guards against a silent reorder of
        # pooler output (CLS vs mean) that would quietly destroy
        # retrieval quality.
        assert int(np.argmax(sims)) == 0
