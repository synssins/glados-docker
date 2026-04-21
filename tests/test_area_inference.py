"""Tests for glados.intent.area_inference — Phase 8.5."""

from __future__ import annotations

from glados.intent.area_inference import AreaFloorHint, infer_area_floor


# Reference deployment: 4-floor split-level. Dict insertion order is
# the same as HA returns (highest level first) so the resolver's
# first-floor-wins behaviour is exercised realistically.
FLOORS = {
    "bedroom_level": "Bedroom Level",
    "main_level":    "Main Level",
    "ground_level":  "Ground Level",
    "basement":      "Basement",
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
    def test_downstairs_resolves_to_ground_level(self) -> None:
        """In a split-level house 'downstairs' refers to the lowest
        habitable floor, not the basement or the main level."""
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "ground_level"
        assert hint.source == "floor_keyword"
        assert hint.matched_keyword == "downstairs"

    def test_upstairs_resolves_to_bedroom_level(self) -> None:
        hint = infer_area_floor(
            "dim the upstairs bedroom",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "bedroom_level"
        assert hint.matched_keyword == "upstairs"

    def test_main_floor_does_not_resolve_to_ground_or_basement(self) -> None:
        """Regression: the original keyword table lumped main/ground
        together so 'main floor' routed to `ground_level`. Split-
        level houses have both and need them distinct."""
        hint = infer_area_floor(
            "dim the main floor lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "main_level"

    def test_main_level_resolves_to_main_level(self) -> None:
        hint = infer_area_floor(
            "turn on the main level lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "main_level"

    def test_bedroom_level_resolves_explicitly(self) -> None:
        hint = infer_area_floor(
            "lights on the bedroom level",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "bedroom_level"

    def test_basement_resolves_to_basement(self) -> None:
        hint = infer_area_floor(
            "is the basement furnace running?",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "basement"

    def test_ground_level_resolves_explicitly(self) -> None:
        hint = infer_area_floor(
            "turn off the ground level lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "ground_level"

    def test_first_floor_beats_substring_match(self) -> None:
        """Longest-keyword-wins: 'first floor' must beat 'first' if
        both are in the lookup table. In this house 'first floor'
        routes to ground_level (US convention)."""
        hint = infer_area_floor(
            "turn off the first floor lights",
            floor_names=FLOORS, area_names=AREAS,
        )
        assert hint.floor_id == "ground_level"
        assert hint.matched_keyword == "first floor"


class TestTwoFloorHouse:
    """A common 2-floor layout where the shipped keywords should
    still do the right thing WITHOUT operator aliases for the
    frequent cases. 'downstairs' is allowed to miss here — operators
    of 2-floor houses with no 'ground/lower/first' floor name add
    an alias."""
    FLOORS_2 = {"main": "Main Floor", "upper": "Upper Floor"}

    def test_upstairs_resolves_to_upper(self) -> None:
        hint = infer_area_floor(
            "dim the upstairs light",
            floor_names=self.FLOORS_2, area_names={},
        )
        assert hint.floor_id == "upper"

    def test_main_floor_resolves_to_main(self) -> None:
        hint = infer_area_floor(
            "turn off the main floor lights",
            floor_names=self.FLOORS_2, area_names={},
        )
        assert hint.floor_id == "main"


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
        bare_registry = {"main": "Main Floor"}
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
    def test_floor_alias_routes_obscure_keyword(self) -> None:
        """Operator adds a house-specific phrase and points it at an
        existing floor — the inference resolves it the same as the
        shipped keywords would a standard phrase."""
        hint = infer_area_floor(
            "turn off the living floor lights",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"living floor": "Main Level"},
        )
        assert hint.floor_id == "main_level"
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
        the ground level. Their call wins over the shipped default."""
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"downstairs": "Basement"},
        )
        assert hint.floor_id == "basement"
        assert hint.source == "floor_alias"

    def test_alias_to_missing_name_is_ignored(self) -> None:
        """Alias points at a floor the registry doesn't know — the
        inference gracefully falls through to the shipped keywords."""
        hint = infer_area_floor(
            "turn off the downstairs lights",
            floor_names=FLOORS, area_names=AREAS,
            floor_aliases={"downstairs": "Mezzanine"},
        )
        # Shipped keyword still matches against 'Ground Level'.
        assert hint.floor_id == "ground_level"
        assert hint.source == "floor_keyword"
