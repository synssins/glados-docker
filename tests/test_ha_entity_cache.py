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
            _state("lock.entry_door", "Front Door Lock"),
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
            _state("light.counter_lights", "Kitchen cabinet light"),
        ])
        matches = cache.get_candidates("cabinet lights")
        assert len(matches) == 1
        assert matches[0].score >= 75

    def test_sensitive_domains_reject_fuzzy_match(self) -> None:
        """A lock with friendly_name 'Front Door' must not match an
        approximate query like 'fron dor'."""
        cache = EntityCache()
        cache.apply_get_states([
            _state("lock.entry_door", "Front Door"),
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
            _state("cover.vehicle_door", "Garage Door",
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
