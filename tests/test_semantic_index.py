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

    def test_build_without_embedder_returns_zero(self) -> None:
        # When the model files don't exist, build() short-circuits
        # to 0 instead of raising — the retriever silently disables
        # and the disambiguator falls back to fuzzy matching. This
        # is the CI path (no BGE-small on disk).
        idx = SemanticIndex(
            _StubCache(),
            model_path="/nonexistent/model.onnx",
            tokenizer_path="/nonexistent/tok.json",
        )
        assert idx.build() == 0
        # Retrieve returns empty when no embedder loaded.
        assert idx.retrieve("desk lamp") == []


# ──────────────────────────────────────────────────────────────
# Phase 8.3.2 — build / retrieve / persist / load with stub embedder
# ──────────────────────────────────────────────────────────────
#
# The real BGE-small produces 384-d embeddings; for unit tests we
# inject a tiny 4-d feature-based stub so retrieval ordering is
# deterministic and the embedder file isn't needed. The stub has
# features for {desk, lamp, office, kitchen} — enough to verify
# the query-document similarity path orders correctly.

class _StubEmbedder:
    dim = 4

    def embed(self, texts, is_query=False):  # noqa: ARG002
        import numpy as np
        vocab = ("desk", "lamp", "office", "kitchen")
        out = []
        for t in texts:
            t = t.lower()
            v = np.array(
                [1.0 if w in t else 0.0 for w in vocab],
                dtype=np.float32,
            )
            n = np.linalg.norm(v)
            v = v / n if n > 0 else np.ones(4, dtype=np.float32) / 2
            out.append(v)
        return np.array(out)


class _Entity:
    """Minimal EntityState-compatible object for index tests."""
    def __init__(
        self, entity_id, friendly_name, *,
        domain=None, device_class=None, area_id=None,
        device_id=None, aliases=None,
    ) -> None:
        self.entity_id = entity_id
        self.friendly_name = friendly_name
        self.domain = domain or entity_id.split(".", 1)[0]
        self.device_class = device_class
        self.area_id = area_id
        self.device_id = device_id
        self.aliases = aliases or []


class _CacheWithEntities:
    def __init__(self, entities):
        self._entities = list(entities)

    def snapshot(self):
        return list(self._entities)


@pytest.fixture
def _stub_idx(tmp_path, monkeypatch):
    """SemanticIndex wired to a stub embedder + temp index file."""
    pytest.importorskip("numpy")
    cache = _CacheWithEntities([
        _Entity("light.task_lamp_one",
                "Office Desk Monitor Lamp",
                area_id="office", device_id="dev_desk"),
        _Entity("light.kitchen_ceiling",
                "Kitchen Ceiling Light",
                area_id="kitchen", device_id="dev_kitchen"),
        _Entity("light.living_arc_lamp",
                "Living Arc Lamp",
                area_id="living", device_id="dev_arc"),
    ])
    idx = SemanticIndex(
        cache,
        model_path="/nonexistent/model.onnx",
        tokenizer_path="/nonexistent/tok.json",
        index_path=tmp_path / "entity_embeddings.npz",
    )
    idx.apply_area_registry([
        {"area_id": "office", "name": "Office", "floor_id": "main"},
        {"area_id": "kitchen", "name": "Kitchen", "floor_id": "main"},
        {"area_id": "living", "name": "Living Room", "floor_id": "main"},
    ])
    # Inject the stub directly — bypass _ensure_embedder's file check.
    monkeypatch.setattr(idx, "_ensure_embedder", lambda: _StubEmbedder())
    return idx


class TestBuild:
    def test_build_embeds_every_entity_in_cache(self, _stub_idx) -> None:
        n = _stub_idx.build()
        assert n == 3
        assert _stub_idx.size() == 3

    def test_build_captures_documents_in_sync(self, _stub_idx) -> None:
        _stub_idx.build()
        # Internal invariant: parallel arrays stay aligned.
        assert len(_stub_idx._entity_ids) == len(_stub_idx._documents)
        assert len(_stub_idx._entity_ids) == len(_stub_idx._device_ids)
        # Documents include the area from the registry join.
        docs = _stub_idx._documents
        assert any("area=Office" in d for d in docs)
        assert any("area=Kitchen" in d for d in docs)

    def test_build_captures_device_ids(self, _stub_idx) -> None:
        _stub_idx.build()
        dev_map = dict(zip(_stub_idx._entity_ids, _stub_idx._device_ids))
        assert dev_map["light.task_lamp_one"] == "dev_desk"
        assert dev_map["light.kitchen_ceiling"] == "dev_kitchen"


class TestRetrieve:
    def test_desk_lamp_ranks_office_first(self, _stub_idx) -> None:
        _stub_idx.build()
        hits = _stub_idx.retrieve("desk lamp", k=3)
        assert len(hits) == 3
        assert hits[0].entity_id == "light.task_lamp_one"
        # Score should be high (exact feature match = dot ~1.0).
        assert hits[0].score > 0.7

    def test_kitchen_ranks_kitchen_first(self, _stub_idx) -> None:
        _stub_idx.build()
        hits = _stub_idx.retrieve("kitchen", k=3)
        assert hits[0].entity_id == "light.kitchen_ceiling"

    def test_domain_filter_excludes_other_domains(self, _stub_idx) -> None:
        _stub_idx.build()
        hits = _stub_idx.retrieve(
            "desk lamp", k=3, domain_filter=["switch"],
        )
        # All entities in the fixture are `light.*` so filtering to
        # `switch` returns nothing.
        assert hits == []

    def test_empty_query_returns_empty(self, _stub_idx) -> None:
        _stub_idx.build()
        assert _stub_idx.retrieve("") == []
        assert _stub_idx.retrieve("   ") == []

    def test_retrieve_before_build_returns_empty(self, _stub_idx) -> None:
        # No build() call → no embeddings → empty result.
        assert _stub_idx.retrieve("desk lamp") == []

    def test_hit_carries_document_and_device_id(self, _stub_idx) -> None:
        _stub_idx.build()
        hits = _stub_idx.retrieve("desk lamp", k=1)
        assert hits[0].document  # non-empty
        assert hits[0].device_id == "dev_desk"


# ──────────────────────────────────────────────────────────────
# Phase 8.5 — area_id / floor_id filter hints on retrieve()
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def _multi_floor_idx(tmp_path, monkeypatch):
    """Build an index whose entities span two floors + outdoor,
    so the area/floor filter actually discriminates."""
    pytest.importorskip("numpy")
    cache = _CacheWithEntities([
        _Entity("light.office_desk_lamp", "Office Desk Lamp",
                area_id="office"),
        _Entity("light.kitchen_ceiling", "Kitchen Ceiling",
                area_id="kitchen"),
        _Entity("light.upstairs_bedroom", "Upstairs Bedroom",
                area_id="bedroom"),
        _Entity("light.outdoor_floodlight", "Backyard Spotlight",
                area_id="yard"),
    ])
    idx = SemanticIndex(
        cache,
        model_path="/nonexistent/model.onnx",
        tokenizer_path="/nonexistent/tok.json",
        index_path=tmp_path / "entity_embeddings.npz",
    )
    idx.apply_area_registry([
        {"area_id": "office",  "name": "Office",         "floor_id": "floor_main"},
        {"area_id": "kitchen", "name": "Kitchen",        "floor_id": "floor_main"},
        {"area_id": "bedroom", "name": "Master Bedroom", "floor_id": "floor_upper"},
        {"area_id": "yard",    "name": "Backyard",       "floor_id": None},
    ])
    idx.apply_floor_registry([
        {"floor_id": "floor_main",  "name": "Main Floor"},
        {"floor_id": "floor_upper", "name": "Upper Floor"},
    ])
    monkeypatch.setattr(idx, "_ensure_embedder", lambda: _StubEmbedder())
    return idx


class TestAreaFloorFilter:
    def test_floor_filter_excludes_other_floors(self, _multi_floor_idx) -> None:
        _multi_floor_idx.build()
        # Query matches "kitchen" + "office" + "bedroom" + "yard"
        # all weakly; we don't care about ranking, only that
        # non-main-floor entities are excluded.
        hits = _multi_floor_idx.retrieve("lamp", k=10, floor_id="floor_main")
        returned = {h.entity_id for h in hits}
        assert "light.upstairs_bedroom" not in returned
        assert "light.outdoor_floodlight" not in returned
        assert any(e in returned for e in (
            "light.office_desk_lamp", "light.kitchen_ceiling",
        ))

    def test_floor_filter_upper_only_returns_upper_entities(
        self, _multi_floor_idx,
    ) -> None:
        _multi_floor_idx.build()
        hits = _multi_floor_idx.retrieve(
            "lamp", k=10, floor_id="floor_upper",
        )
        returned = {h.entity_id for h in hits}
        assert returned <= {"light.upstairs_bedroom"}

    def test_area_filter_pins_specific_area(self, _multi_floor_idx) -> None:
        _multi_floor_idx.build()
        hits = _multi_floor_idx.retrieve(
            "kitchen", k=10, area_id="kitchen",
        )
        assert {h.entity_id for h in hits} == {"light.kitchen_ceiling"}

    def test_hit_carries_area_and_floor_ids(self, _multi_floor_idx) -> None:
        _multi_floor_idx.build()
        hits = _multi_floor_idx.retrieve("kitchen", k=1)
        h = hits[0]
        assert h.area_id == "kitchen"
        assert h.floor_id == "floor_main"

    def test_floor_filter_excludes_entities_with_no_floor(
        self, _multi_floor_idx,
    ) -> None:
        """Backyard has area_id but no floor — it must not surface
        when the operator asks for a specific floor."""
        _multi_floor_idx.build()
        hits = _multi_floor_idx.retrieve(
            "spotlight", k=10, floor_id="floor_main",
        )
        assert "light.outdoor_floodlight" not in {h.entity_id for h in hits}

    def test_registry_accessors_expose_maps(self, _multi_floor_idx) -> None:
        """area_names() / floor_names() are how the utterance-side
        inference resolves keywords to ids."""
        _multi_floor_idx.build()
        assert _multi_floor_idx.area_names()["kitchen"] == "Kitchen"
        assert _multi_floor_idx.floor_names()["floor_upper"] == "Upper Floor"


class TestPersistLoad:
    def test_persist_then_load_round_trip(self, _stub_idx) -> None:
        _stub_idx.build()
        assert _stub_idx.persist() is True
        assert _stub_idx._index_path.exists()

        # Build a fresh SemanticIndex pointed at the same file and
        # stub embedder; load() must restore the index.
        import numpy as np  # noqa: F401  # used for shape assertion below
        from glados.ha.semantic_index import SemanticIndex
        other = SemanticIndex(
            _stub_idx._cache,
            model_path=str(_stub_idx._model_path),
            tokenizer_path=str(_stub_idx._tokenizer_path),
            index_path=_stub_idx._index_path,
        )
        assert other.load() is True
        assert other.size() == 3
        assert "light.task_lamp_one" in other._entity_ids

    def test_load_missing_file_returns_false(self, tmp_path) -> None:
        pytest.importorskip("numpy")
        idx = SemanticIndex(
            _StubCache(),
            index_path=tmp_path / "missing.npz",
        )
        assert idx.load() is False

    def test_load_wrong_header_returns_false(self, tmp_path, _stub_idx) -> None:
        import numpy as np
        _stub_idx.build()
        _stub_idx.persist()
        # Corrupt the header on disk.
        data = np.load(_stub_idx._index_path, allow_pickle=True)
        np.savez_compressed(
            _stub_idx._index_path,
            header=np.array("v99|future-model|doc-v99"),
            embeddings=data["embeddings"],
            entity_ids=data["entity_ids"],
            documents=data["documents"],
            device_ids=data["device_ids"],
        )
        # Fresh index should refuse to load the incompatible file.
        from glados.ha.semantic_index import SemanticIndex
        other = SemanticIndex(
            _stub_idx._cache,
            index_path=_stub_idx._index_path,
        )
        assert other.load() is False
        assert other.size() == 0

    def test_persist_empty_index_is_noop(self, tmp_path) -> None:
        pytest.importorskip("numpy")
        idx = SemanticIndex(
            _StubCache(),
            index_path=tmp_path / "empty.npz",
        )
        # No build() → nothing to persist.
        assert idx.persist() is False
        assert not idx._index_path.exists()


# ──────────────────────────────────────────────────────────────
# Live Embedder — only runs if the BGE-small ONNX is present
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# Phase 8.3.3 — device-diversity filter (operator's 4 gates)
# ──────────────────────────────────────────────────────────────
#
# These tests DO NOT require the real Embedder — the filter
# operates on SemanticHit lists. Each test constructs the hits
# directly so the scoring inputs are deterministic and the gate
# semantics are asserted in isolation.

from glados.ha.semantic_index import (  # noqa: E402
    DEFAULT_SEGMENT_TOKENS,
    PER_DEVICE_CAP,
    SemanticHit,
    apply_device_diversity,
)


def _hit(entity_id: str, score: float = 0.8,
         device_id: str | None = None, document: str = "") -> SemanticHit:
    return SemanticHit(
        entity_id=entity_id,
        score=score,
        document=document or entity_id.split(".", 1)[-1].replace("_", " "),
        device_id=device_id,
    )


class TestDeviceDiversityOperatorGates:
    """The four gates from docs/battery-findings-and-remediation-plan.md
    §Phase 8.3 Success criteria. Each test maps to one gate."""

    # ── Gate 2: desk-lamp query survives N bedroom-strip siblings ──

    def test_gate_2_desk_lamp_survives_gledopto_siblings(self) -> None:
        """`search_entities("desk lamp")` must return
        `light.task_lamp_one` in top-3 even when the
        Gledopto bedroom LED strip has ≥8 sibling segments with
        'lamp' in their name."""
        hits = [
            # 10 bedroom strip segments, high-ranking by cosine
            # because they all include "lamp" in their aliases.
            *[
                _hit(
                    f"light.room_a_strip_seg_{i}",
                    score=0.90 - 0.01 * i,
                    device_id="gledopto_bedroom",
                    document=f"Bedroom Strip Seg {i} | aliases=lamp",
                )
                for i in range(1, 11)
            ],
            _hit(
                "light.task_lamp_one",
                score=0.85,  # loses the raw ranking battle
                device_id="dev_office_desk",
                document="Office Desk Monitor Lamp | area=Office",
            ),
        ]
        out = apply_device_diversity(hits, "desk lamp", top_k=3)
        ids = [h.entity_id for h in out]
        assert "light.task_lamp_one" in ids[:3]
        # At most one bedroom_strip entity should survive to top-3.
        bedroom_count = sum(1 for i in ids if "bedroom_strip" in i)
        assert bedroom_count <= 1

    # ── Gate 3: segment-qualified queries still return the segment ──
    # NOTE (2026-04-20 operator decision): segments are dropped by
    # default via ignore_segments=True; these tests pass
    # ignore_segments=False explicitly to exercise the opt-in path
    # where an operator genuinely needs per-segment control.

    def test_gate_3_segment_qualifier_preserves_specific_segment(self) -> None:
        """With ignore_segments=False, segment-qualified queries
        pin the matching segment."""
        hits = [
            *[
                _hit(
                    f"light.room_a_strip_seg_{i}",
                    score=0.90 - 0.01 * i,
                    device_id="gledopto_bedroom",
                    document=f"Bedroom Strip Seg {i}",
                )
                for i in range(1, 11)
            ],
        ]
        out = apply_device_diversity(
            hits, "bedroom strip seg 3 red", top_k=3,
            ignore_segments=False,
        )
        ids = [h.entity_id for h in out]
        assert "light.room_a_strip_seg_3" in ids[:3]

    def test_gate_3_variant_qualifier_wording(self) -> None:
        hits = [
            _hit(
                f"light.room_a_strip_seg_{i}",
                score=0.9,
                device_id="gledopto_bedroom",
                document=f"Bedroom Strip Seg {i}",
            )
            for i in range(1, 6)
        ]
        out = apply_device_diversity(
            hits, "bedroom strip segment 3 red", top_k=3,
            ignore_segments=False,
        )
        ids = [h.entity_id for h in out]
        assert "light.room_a_strip_seg_3" in ids

    # ── Gate 4: no device_id appears >2 times unless named ──

    def test_gate_4_per_device_cap_enforced(self) -> None:
        """No device_id may appear more than PER_DEVICE_CAP (=2)
        times in the output. Uses non-segment names so the cap
        logic exercises even with ignore_segments default-on."""
        hits = [
            _hit(
                f"light.big_device_bulb_{i}",
                score=0.9 - 0.01 * i,
                device_id="big_device",
                document=f"Big Device Bulb {i}",
            )
            for i in range(20)
        ]
        out = apply_device_diversity(hits, "lights", top_k=8)
        counts: dict[str, int] = {}
        for h in out:
            if h.device_id:
                counts[h.device_id] = counts.get(h.device_id, 0) + 1
        assert all(c <= PER_DEVICE_CAP for c in counts.values())

    def test_gate_4_cap_relaxed_when_segment_named(self) -> None:
        """With ignore_segments=False, a named segment bypasses the
        cap while non-pinned siblings still get collapsed."""
        hits = [
            _hit(
                f"light.strip_seg_{i}",
                score=0.9,
                device_id="dev_strip",
                document=f"Strip Seg {i}",
            )
            for i in range(1, 8)
        ]
        out = apply_device_diversity(
            hits, "strip seg 3", top_k=8, ignore_segments=False,
        )
        ids = [h.entity_id for h in out]
        assert "light.strip_seg_3" in ids
        non_pinned = [
            i for i in ids
            if i.startswith("light.strip_seg_") and i != "light.strip_seg_3"
        ]
        assert len(non_pinned) == 0


class TestDeviceDiversityRepresentativePicker:
    """Tiebreakers inside a same-device group with no pin."""

    def test_non_segment_name_wins_over_segment_sibling(self) -> None:
        hits = [
            _hit("light.foo_seg_1", score=0.95,
                 device_id="dev", document="Foo Seg 1"),
            _hit("light.foo_main", score=0.90,
                 device_id="dev", document="Foo Main"),
        ]
        out = apply_device_diversity(hits, "foo", top_k=3)
        ids = [h.entity_id for h in out]
        assert ids == ["light.foo_main"]

    def test_light_beats_switch_within_same_device(self) -> None:
        """Phase 8.1's twin-dedup rule baked into the representative
        picker for the semantic path."""
        hits = [
            _hit("switch.ceiling_lights", score=0.92,
                 device_id="zooz1", document="Kitchen Overhead"),
            _hit("light.ceiling_lights", score=0.85,
                 device_id="zooz1", document="Kitchen Overhead"),
        ]
        out = apply_device_diversity(hits, "kitchen overhead", top_k=3)
        ids = [h.entity_id for h in out]
        assert ids == ["light.ceiling_lights"]

    def test_natural_sort_order_on_pure_segment_group(self) -> None:
        """With ignore_segments=False, when every sibling is a
        segment and no pin exists, the representative picker uses
        natural sort (so Seg 2 beats Seg 10)."""
        hits = [
            _hit("light.strip_seg_10", score=0.9,
                 device_id="dev", document="Strip Seg 10"),
            _hit("light.strip_seg_2", score=0.9,
                 device_id="dev", document="Strip Seg 2"),
            _hit("light.strip_seg_1", score=0.9,
                 device_id="dev", document="Strip Seg 1"),
        ]
        out = apply_device_diversity(
            hits, "strip", top_k=3, ignore_segments=False,
        )
        assert out[0].entity_id == "light.strip_seg_1"


class TestIgnoreSegmentsDefault:
    """Phase 8.3 follow-up: by operator request, segment entities
    are dropped entirely by default. These tests pin the contract."""

    def test_ignore_segments_drops_all_segment_entities(self) -> None:
        hits = [
            _hit(f"light.room_a_strip_seg_{i}",
                 score=0.9, device_id="dev_strip",
                 document=f"Bedroom Strip Seg {i}")
            for i in range(1, 6)
        ]
        hits.append(_hit(
            "light.task_lamp_one", score=0.85,
            device_id="dev_desk",
            document="Office Desk Monitor Lamp",
        ))
        out = apply_device_diversity(hits, "desk lamp", top_k=5)
        ids = [h.entity_id for h in out]
        assert ids == ["light.task_lamp_one"]
        assert not any("strip_seg" in i for i in ids)

    def test_ignore_segments_false_restores_pin_behavior(self) -> None:
        """When disabled, the old segment-pin path kicks back in so
        operators with per-segment use cases aren't stranded."""
        hits = [
            _hit(f"light.strip_seg_{i}",
                 score=0.9, device_id="dev_strip",
                 document=f"Strip Seg {i}")
            for i in range(1, 5)
        ]
        out = apply_device_diversity(
            hits, "strip seg 3", top_k=5, ignore_segments=False,
        )
        ids = [h.entity_id for h in out]
        assert "light.strip_seg_3" in ids

    def test_ignore_segments_leaves_master_entity(self) -> None:
        """When a device has both a master and segments, the master
        survives and the segments are dropped."""
        hits = [
            _hit("light.room_a_strip_seg_1", score=0.90,
                 device_id="gle", document="Bedroom Strip Seg 1"),
            _hit("light.room_a_strip_seg_2", score=0.89,
                 device_id="gle", document="Bedroom Strip Seg 2"),
            _hit("light.room_a_strip", score=0.85,
                 device_id="gle", document="Bedroom Strip"),
        ]
        out = apply_device_diversity(hits, "bedroom strip", top_k=5)
        ids = [h.entity_id for h in out]
        assert ids == ["light.room_a_strip"]

    def test_ignore_segments_empty_when_only_segments(self) -> None:
        """A device that exposes only segments becomes invisible.
        Operators access it via a scene instead."""
        hits = [
            _hit(f"light.back_patio_string_2_segment_{i:03d}",
                 score=0.8, device_id="patio",
                 document=f"Back Patio String 2 Segment {i:03d}")
            for i in range(1, 5)
        ]
        out = apply_device_diversity(hits, "back patio", top_k=5)
        assert out == []


class TestDeviceDiversityEdgeCases:
    def test_empty_input_returns_empty(self) -> None:
        assert apply_device_diversity([], "anything", top_k=5) == []

    def test_hits_without_device_id_pass_through(self) -> None:
        """Entities not tied to a registry device can't be proven
        to be siblings; they shouldn't be collapsed."""
        hits = [
            _hit("scene.evening", score=0.9, device_id=None,
                 document="Evening Scene"),
            _hit("script.bedtime", score=0.8, device_id=None,
                 document="Bedtime Script"),
        ]
        out = apply_device_diversity(hits, "evening", top_k=5)
        assert len(out) == 2

    def test_default_segment_tokens_include_plan_list(self) -> None:
        expected = {"seg", "segment", "zone", "channel", "strip", "group"}
        assert expected.issubset(set(DEFAULT_SEGMENT_TOKENS))

    def test_custom_token_list_can_restrict_detection(self) -> None:
        """With ignore_segments=False AND a restricted token list,
        the diversity filter falls back to the `_<digits>` suffix
        detector for same-device collapse. Representative picker
        natural-sorts the survivors."""
        hits = [
            _hit(f"light.strip_seg_{i}", score=0.9,
                 device_id="dev", document=f"Strip Seg {i}")
            for i in range(1, 4)
        ]
        out = apply_device_diversity(
            hits, "strip", top_k=3,
            segment_tokens=("zone",),
            ignore_segments=False,
        )
        assert len(out) == 1
        assert out[0].entity_id == "light.strip_seg_1"

    def test_top_k_truncation_after_filter(self) -> None:
        # Use non-numeric suffixes so neither the token regex nor
        # the `_<digits>` fallback flags these as segments.
        suffixes = ["alpha", "bravo", "charlie", "delta", "echo",
                    "foxtrot", "golf", "hotel", "india", "juliet"]
        hits = [
            _hit(f"light.room_{s}", score=0.9 - 0.01 * i,
                 device_id=f"dev_{s}", document=f"Room {s} Light")
            for i, s in enumerate(suffixes)
        ]
        out = apply_device_diversity(hits, "room", top_k=3)
        assert len(out) == 3
        # Sorted by original rank.
        assert out[0].entity_id == "light.room_alpha"

    def test_ranking_order_preserved_for_survivors(self) -> None:
        """Survivors retain the ranker's ordering."""
        hits = [
            _hit("light.a", score=0.9, device_id="dev_a", document="A"),
            _hit("light.b", score=0.8, device_id="dev_b", document="B"),
            _hit("light.c", score=0.7, device_id="dev_c", document="C"),
        ]
        out = apply_device_diversity(hits, "anything", top_k=5)
        assert [h.entity_id for h in out] == ["light.a", "light.b", "light.c"]


class TestRetrieveForPlanner:
    """Integration check — the convenience method chains retrieve()
    into apply_device_diversity with sensible defaults."""

    def test_chains_retrieve_and_diversity(self, _stub_idx) -> None:
        # Add Gledopto-style siblings to the fixture cache so we
        # exercise the operator's Gate 2 case end-to-end.
        sibling_entities = [
            _Entity(
                f"light.room_a_strip_seg_{i}",
                f"Bedroom Strip Seg {i}",
                area_id="bedroom",
                device_id="gledopto_bedroom",
            )
            for i in range(1, 9)
        ]
        _stub_idx._cache._entities.extend(sibling_entities)
        _stub_idx.build()
        out = _stub_idx.retrieve_for_planner("desk lamp", k=3)
        ids = [h.entity_id for h in out]
        assert "light.task_lamp_one" in ids[:3]
        bedroom_count = sum(1 for i in ids if "bedroom_strip" in i)
        assert bedroom_count <= 1


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
