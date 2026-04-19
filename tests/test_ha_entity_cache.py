"""Tests for glados.ha.entity_cache."""

from __future__ import annotations

import time

import pytest

from glados.ha.entity_cache import (
    CandidateMatch,
    EntityCache,
    EntityState,
    _cutoff_for,
)


def _state(entity_id: str, friendly_name: str = "", state: str = "off",
           device_class: str | None = None, aliases: list[str] | None = None,
           area_id: str | None = None) -> dict:
    """Construct a HA state dict in the shape `get_states` returns."""
    attrs: dict = {}
    if friendly_name:
        attrs["friendly_name"] = friendly_name
    if device_class:
        attrs["device_class"] = device_class
    if aliases:
        attrs["aliases"] = aliases
    if area_id:
        attrs["area_id"] = area_id
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


class TestApply:
    def test_apply_get_states_loads_entities(self) -> None:
        cache = EntityCache()
        loaded = cache.apply_get_states([
            _state("light.kitchen_ceiling", "Kitchen Overhead"),
            _state("lock.front_door", "Front Door Lock"),
        ])
        assert loaded == 2
        assert cache.size() == 2

    def test_apply_get_states_replaces_previous(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.a", "A")])
        cache.apply_get_states([_state("light.b", "B")])
        assert cache.get("light.a") is None
        assert cache.get("light.b") is not None

    def test_apply_state_changed_updates_in_place(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.x", "X", state="off")])
        cache.apply_state_changed({
            "entity_id": "light.x",
            "old_state": _state("light.x", "X", state="off"),
            "new_state": _state("light.x", "X", state="on"),
        })
        assert cache.get("light.x").state == "on"

    def test_apply_state_changed_removes_when_new_state_null(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.x", "X")])
        cache.apply_state_changed({"entity_id": "light.x", "new_state": None})
        assert cache.get("light.x") is None

    def test_malformed_entries_are_ignored(self) -> None:
        cache = EntityCache()
        loaded = cache.apply_get_states([
            {},
            {"entity_id": "no_dot"},
            {"entity_id": "light.ok", "state": "on", "attributes": {}},
        ])
        assert loaded == 1


class TestFreshness:
    def test_age_grows_over_time(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.x", "X")])
        # Age should be tiny immediately after load.
        assert cache.age("light.x") < 1.0
        # Force an older timestamp to simulate drift.
        entity = cache.get("light.x")
        entity.state_as_of = time.time() - 10
        assert cache.age("light.x") >= 9.9

    def test_age_infinite_for_unknown_entity(self) -> None:
        cache = EntityCache()
        assert cache.age("light.nope") == float("inf")


class TestFuzzyMatching:
    def test_exact_friendly_name_matches(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen_ceiling", "Kitchen Overhead"),
            _state("light.bedroom_lamp", "Bedroom Lamp"),
        ])
        matches = cache.get_candidates("Kitchen Overhead")
        assert len(matches) >= 1
        assert matches[0].entity.entity_id == "light.kitchen_ceiling"
        assert matches[0].score >= 75

    def test_partial_match_within_cutoff(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen_cabinet", "Kitchen cabinet light"),
        ])
        matches = cache.get_candidates("cabinet lights")
        assert len(matches) == 1
        assert matches[0].score >= 75

    def test_sensitive_domains_reject_fuzzy_match(self) -> None:
        """A lock with friendly_name 'Front Door' must not match an
        approximate query like 'fron dor'."""
        cache = EntityCache()
        cache.apply_get_states([
            _state("lock.front_door", "Front Door"),
        ])
        # Exact match works.
        exact = cache.get_candidates("Front Door")
        assert len(exact) == 1
        assert exact[0].sensitive is True
        # Typo-ish query is rejected.
        typo = cache.get_candidates("fron dor")
        assert typo == []

    def test_garage_cover_treated_as_sensitive(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("cover.garage_door", "Garage Door",
                   device_class="garage"),
            _state("cover.living_room_blinds", "Living Room Blinds"),
        ])
        # Garage rejects loose match.
        loose = cache.get_candidates("garag")
        assert loose == []
        # Non-garage cover allows loose match.
        loose = cache.get_candidates("living blinds")
        assert len(loose) == 1

    def test_domain_filter(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen", "Kitchen"),
            _state("switch.kitchen", "Kitchen"),
        ])
        lights = cache.get_candidates("kitchen", domain_filter=["light"])
        assert len(lights) == 1
        assert lights[0].entity.domain == "light"

    def test_aliases_participate_in_matching(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen", "Kitchen Overhead",
                   aliases=["main kitchen light"]),
        ])
        matches = cache.get_candidates("main kitchen")
        assert len(matches) == 1
        assert "main" in matches[0].matched_name.lower()

    def test_empty_query_returns_empty(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.a", "A")])
        assert cache.get_candidates("") == []
        assert cache.get_candidates("   ") == []

    def test_limit_respected(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state(f"light.x{i}", f"Kitchen {i}") for i in range(20)
        ])
        matches = cache.get_candidates("Kitchen", limit=5)
        assert len(matches) == 5

    def test_results_sorted_by_score_descending(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen", "Kitchen"),
            _state("light.kitchenette", "Kitchenette"),
        ])
        matches = cache.get_candidates("Kitchen")
        assert len(matches) >= 2
        # Exact match ("Kitchen") should outrank the near-miss.
        assert matches[0].entity.entity_id == "light.kitchen"
        assert matches[0].score >= matches[1].score


class TestSearchableNames:
    def test_friendly_name_used_when_set(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("scene.scene_go_away", "Scene: GO AWAY")])
        ent = cache.get("scene.scene_go_away")
        names = ent.searchable_names()
        # entity_id-derived name MUST be omitted when friendly_name exists,
        # otherwise it produces false matches like "scene go away" hitting
        # "evening scene" queries on shared word "scene".
        assert names == ["Scene: GO AWAY"]
        assert "scene go away" not in [n.lower() for n in names]

    def test_aliases_only_when_friendly_name_empty(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.x", "", aliases=["alias one"]),
        ])
        names = cache.get("light.x").searchable_names()
        assert "alias one" in names
        assert "x" not in names  # entity_id fallback skipped because alias exists

    def test_entity_id_fallback_when_no_label(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([_state("light.kitchen_ceiling", "")])
        names = cache.get("light.kitchen_ceiling").searchable_names()
        assert names == ["kitchen ceiling"]


class TestSceneRegression:
    """Regression for: 'activate the evening scene' resolving to
    scene.scene_go_away because the entity_id-derived 'scene go away'
    matched the word 'scene' in the query at score 85, beating the real
    'Living Room Scene: Evening' candidate (score 43-47)."""

    def test_evening_scene_picks_evening_friendly_name(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("scene.scene_go_away",            "Scene: GO AWAY"),
            _state("scene.living_room_scene_evening","Living Room Scene: Evening"),
            _state("scene.scene_morning_wake_up",    "Scene: Morning wake up"),
            _state("scene.evening_restore_snapshot", "evening_restore_snapshot"),
        ])
        results = cache.get_candidates(
            "activate the evening scene", domain_filter=["scene"], limit=10,
        )
        assert results, "no candidates returned for evening scene query"
        ids = [r.entity.entity_id for r in results]
        # The evening-named scenes must appear; the unrelated "Go Away"
        # must NOT be the top result.
        assert "scene.scene_go_away" not in ids[:1], (
            f"go_away wrongly outranked: {ids}"
        )
        evening_match = next(
            (r for r in results if "evening" in r.entity.entity_id), None
        )
        assert evening_match is not None, (
            f"no evening-related scene in candidates: {ids}"
        )

    def test_query_preprocessor_strips_command_verbs(self) -> None:
        from glados.ha.entity_cache import _preprocess_query
        assert _preprocess_query("activate the evening scene") == "evening scene"
        assert _preprocess_query("turn off the kitchen lights") == "kitchen lights"
        assert _preprocess_query("please run the bedtime script") == "bedtime script"

    def test_query_preprocessor_does_not_eat_meaningful_short_query(self) -> None:
        from glados.ha.entity_cache import _preprocess_query
        # If stripping leaves nothing, fall back to lowered original.
        assert _preprocess_query("on") == "on"

    def test_query_preprocessor_strips_direction_and_quantity_modifiers(self) -> None:
        """P0 2026-04-19: 'Turn the desk lamp down by half' produced
        zero candidates because 'down by half' polluted the fuzzy
        score against 'Office Desk Monitor Lamp'. Direction / quantity
        words belong in service_data, not in the entity name match."""
        from glados.ha.entity_cache import _preprocess_query
        assert _preprocess_query("turn the desk lamp down by half") == "desk lamp"
        assert _preprocess_query("make the lamp brighter") == "lamp"
        assert _preprocess_query("reduce the fan to minimum") == "fan"
        assert _preprocess_query("can you raise the volume a bit") == "volume"

    def test_query_preprocessor_leaves_multi_word_modifiers_alone(self) -> None:
        """Whole-word matching: 'downstairs' must NOT be stripped just
        because 'down' is a stopword."""
        from glados.ha.entity_cache import _preprocess_query
        assert "downstairs" in _preprocess_query(
            "turn on the downstairs hallway light")
        assert "upstairs" in _preprocess_query("dim the upstairs bedroom")


class TestSoftCandidateRanking:
    """P0 2026-04-19: 'Turn the desk lamp down by half' produced three
    Tier 2 candidates — Office Desk Monitor Lamp (full match) plus
    two Living Room Arc Lamp entities (partial). The disambiguator
    then asked the user to pick between three unrelated fixtures.

    Design: qualifier coverage and area match are SOFT ranking
    bonuses layered on top of raw WRatio. Full-coverage /
    same-area candidates rank first so the LLM sees them first,
    but partial matches still appear so synonym / scope / activity
    overrides can win when appropriate."""

    def _cache_with(self, *states: dict) -> EntityCache:
        cache = EntityCache()
        cache.apply_get_states(list(states))
        return cache

    def _state(self, eid: str, name: str, state: str = "on",
               area: str | None = None):
        attrs: dict = {"friendly_name": name}
        if area:
            attrs["area_id"] = area
        return {
            "entity_id": eid, "state": state,
            "attributes": attrs,
        }

    def test_full_coverage_ranks_above_partial(self) -> None:
        cache = self._cache_with(
            self._state("light.office_desk_monitor_lamp",
                        "Office Desk Monitor Lamp"),
            self._state("light.living_arc_1", "Living Room Arc Lamp 1"),
            self._state("light.living_arc_2", "Living Room Arc Lamp 2"),
        )
        results = cache.get_candidates("desk lamp", domain_filter=["light"])
        assert results, "no candidates returned"
        # Full-coverage must rank first...
        assert results[0].entity.entity_id == "light.office_desk_monitor_lamp"
        # ...and its coverage is reported as 1.0 for the prompt / audit.
        assert results[0].coverage == 1.0
        # ...but partial-coverage arc lamps still appear (soft boost,
        # not hard filter) so scope / synonym rules can still fire.
        ids = {c.entity.entity_id for c in results}
        assert "light.living_arc_1" in ids
        assert "light.living_arc_2" in ids

    def test_no_full_coverage_means_no_ranking_change(self) -> None:
        """When no candidate covers every token the bonuses are
        uniform-low (or zero) and the raw WRatio ordering dominates."""
        cache = self._cache_with(
            self._state("light.a", "Bedroom Alpha Light"),
            self._state("light.b", "Bedroom Beta Light"),
        )
        # Query "bedroom lights" — plural doesn't match "Light" singular
        # whole-word, so neither candidate gets full coverage.
        results = cache.get_candidates("bedroom lights", domain_filter=["light"])
        assert len(results) == 2
        for c in results:
            assert c.coverage < 1.0

    def test_alias_coverage_beats_non_alias_partial(self) -> None:
        """Aliases participate in coverage so operators can hint the
        right match without renaming the friendly name."""
        from glados.ha.entity_cache import EntityState
        # Direct-construct entities so we can set aliases (the state
        # blob doesn't carry them through apply_get_states by default).
        cache = EntityCache()
        cache._entities["light.utility"] = EntityState(
            entity_id="light.utility", friendly_name="Utility Fixture",
            domain="light", state="on", state_as_of=time.time(),
            aliases=["garage work lamp"],
        )
        cache._entities["light.arc"] = EntityState(
            entity_id="light.arc", friendly_name="Living Room Arc Lamp",
            domain="light", state="on", state_as_of=time.time(),
        )
        results = cache.get_candidates("garage lamp", domain_filter=["light"])
        assert results[0].entity.entity_id == "light.utility"

    def test_source_area_boosts_same_area_candidate(self) -> None:
        """Voice satellite in a given area: same-area candidate wins
        over an identically-named fixture elsewhere."""
        cache = self._cache_with(
            self._state("light.living_reading", "Reading Lamp",
                        area="living_room"),
            self._state("light.office_reading", "Reading Lamp",
                        area="office"),
        )
        # No area hint -> tie on all signals, raw WRatio decides; both
        # appear.
        baseline = cache.get_candidates("reading lamp",
                                         domain_filter=["light"])
        assert {c.entity.entity_id for c in baseline} == {
            "light.living_reading", "light.office_reading",
        }
        for c in baseline:
            assert c.area_match is None, \
                "area_match should be None when no source_area given"

        # With source_area=living_room, the living-room entity ranks
        # first and carries area_match=True.
        ranked = cache.get_candidates(
            "reading lamp", domain_filter=["light"],
            source_area="living_room",
        )
        assert ranked[0].entity.entity_id == "light.living_reading"
        assert ranked[0].area_match is True
        # And the other one now shows area_match=False (for the prompt).
        other = next(
            c for c in ranked if c.entity.entity_id == "light.office_reading"
        )
        assert other.area_match is False

    def test_single_token_query_has_uniform_coverage(self) -> None:
        """One-token queries give every above-cutoff candidate the
        same coverage bonus, so the prior raw-WRatio ranking is
        preserved."""
        cache = self._cache_with(
            self._state("light.a", "Office Desk Monitor Lamp"),
            self._state("light.b", "Living Room Arc Lamp"),
        )
        results = cache.get_candidates("lamp", domain_filter=["light"])
        assert len(results) == 2
        coverages = {round(c.coverage, 2) for c in results}
        # Both contain "lamp" -> 1/1 = 1.0 coverage for both.
        assert coverages == {1.0}


class TestCutoffs:
    def test_sensitive_domain_gets_100_cutoff(self) -> None:
        e = EntityState(
            entity_id="lock.door", friendly_name="Door", domain="lock",
            state="locked", state_as_of=time.time(),
        )
        assert _cutoff_for(e) == 100

    def test_camera_is_sensitive(self) -> None:
        e = EntityState(
            entity_id="camera.driveway", friendly_name="Driveway",
            domain="camera", state="idle", state_as_of=time.time(),
        )
        assert _cutoff_for(e) == 100

    def test_garage_cover_is_sensitive(self) -> None:
        e = EntityState(
            entity_id="cover.garage", friendly_name="Garage",
            domain="cover", device_class="garage",
            state="closed", state_as_of=time.time(),
        )
        assert _cutoff_for(e) == 100

    def test_light_is_permissive(self) -> None:
        e = EntityState(
            entity_id="light.a", friendly_name="A", domain="light",
            state="off", state_as_of=time.time(),
        )
        assert _cutoff_for(e) == 75
