"""Tests for glados.intent.area_inference — Phase 8.5."""

from __future__ import annotations

from glados.intent.area_inference import AreaFloorHint, infer_area_floor


# Typical house registry shape: a couple of floors, several areas.
FLOORS = {
    "floor_main":  "Main Floor",
    "floor_upper": "Upper Floor",
    "floor_base":  "Basement",
}
AREAS = {
    "area_kitchen":  "Kitchen",
    "area_bedroom":  "Master Bedroom",
    "area_livingrm": "Living Room",
    "area_yard":     "Backyard",
    "area_patio":    "Patio",
    "area_frontyd":  "Front Yard",
}


class TestShippedFloorKeywords:
    def test_downstairs_resolves_to_main_floor(self) -> None:
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "floor_main"
        assert hint.source == "floor_keyword"
        assert hint.matched_keyword == "downstairs"

    def test_upstairs_resolves_to_upper(self) -> None:
        hint = infer_area_floor(
            "dim the upstairs bedroom",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "floor_upper"
        assert hint.matched_keyword == "upstairs"

    def test_basement_resolves_to_basement(self) -> None:
        hint = infer_area_floor(
            "is the basement furnace running?",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "floor_base"

    def test_first_floor_beats_substring_match(self) -> None:
        """Longest-keyword-wins: 'first floor' must beat 'first' if
        both are in the lookup table."""
        hint = infer_area_floor(
            "turn off the first floor lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "floor_main"
        assert hint.matched_keyword == "first floor"


class TestShippedAreaKeywords:
    def test_outside_resolves_to_an_outdoor_area(self) -> None:
        hint = infer_area_floor(
            "is anything on outside?",
            floor_names=FLOORS, area_names=AREAS,
        )
        # Any outdoor-ish area is acceptable — first match wins.
        assert hint.area_id in {"area_yard", "area_patio", "area_frontyd"}
        assert hint.source == "area_keyword"

    def test_backyard_is_more_specific(self) -> None:
        hint = infer_area_floor(
            "turn on the backyard lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.area_id == "area_yard"


class TestNoMatch:
    def test_empty_utterance_returns_none(self) -> None:
        hint = infer_area_floor("", floor_names=FLOORS, area_names=AREAS)
        assert hint.area_id is None and hint.floor_id is None

    def test_no_keyword_returns_none(self) -> None:
        hint = infer_area_floor(
            "turn on the kitchen lights",  # no area/floor qualifier
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.area_id is None and hint.floor_id is None

    def test_keyword_without_matching_registry_is_none(self) -> None:
        """'upstairs' mentioned but no Upper/Second/Top floor in
        the registry — result is None (we don't invent an id)."""
        bare_registry = {"floor_main": "Main Floor"}
        hint = infer_area_floor(
            "upstairs bedroom", floor_names=bare_registry, area_names={},
        )
        assert hint.floor_id is None

    def test_word_boundary_respected(self) -> None:
        """'insider info' must not fire the 'outside' keyword because
        'outside' is not actually in the utterance."""
        hint = infer_area_floor(
            "any insider info on the kitchen?",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.area_id is None and hint.floor_id is None


class TestOperatorAliases:
    def test_floor_alias_overrides_shipped(self) -> None:
        """Operator says 'main level' in their house — they add a
        floor alias pointing that to 'Main Floor'. Utterance then
        resolves the same as 'downstairs'."""
        hint = infer_area_floor(
            "lights on the main level please",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"main level": "Main Floor"},
        )
        assert hint.floor_id == "floor_main"
        assert hint.source == "floor_alias"

    def test_area_alias_routes_to_registry_match(self) -> None:
        hint = infer_area_floor(
            "what's on in mom's room?",
            floor_names=FLOORS, area_names=AREAS,
            area_aliases={"mom's room": "Master Bedroom"},
        )
        assert hint.area_id == "area_bedroom"
        assert hint.source == "area_alias"

    def test_alias_takes_precedence_over_shipped_when_both_match(self) -> None:
        """Operator aliased 'downstairs' to the basement instead of
        main floor. Their call wins over the shipped default."""
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"downstairs": "Basement"},
        )
        assert hint.floor_id == "floor_base"
        assert hint.source == "floor_alias"

    def test_alias_to_missing_name_is_ignored(self) -> None:
        """Alias points at a floor the registry doesn't know — the
        inference gracefully falls through to the shipped keywords."""
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"downstairs": "Mezzanine"},
        )
        # Shipped keyword still matches against "Main Floor".
        assert hint.floor_id == "floor_main"
        assert hint.source == "floor_keyword"
