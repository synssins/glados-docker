"""Tests for glados.core.user_preferences."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from glados.core.user_preferences import (
    DEFAULT_TASK_AREAS,
    DEFAULT_TIER_PRIORITY,
    UserPreferences,
    load_user_preferences,
    save_user_preferences,
)


class TestDefaults:
    def test_empty_model_has_documented_defaults(self) -> None:
        prefs = UserPreferences()
        assert prefs.lighting_tier_priority == list(DEFAULT_TIER_PRIORITY)
        assert prefs.task_areas == list(DEFAULT_TASK_AREAS)
        assert prefs.area_aliases == {}
        assert prefs.default_warm_kelvin == 2700
        assert prefs.default_cool_kelvin == 5000
        assert prefs.default_normal_brightness_pct == 60
        assert prefs.brightness_step_pct == 25
        assert prefs.brightness_step_small_pct == 10
        assert prefs.brightness_step_large_pct == 50


class TestTierValidation:
    def test_rejects_unknown_tier(self) -> None:
        # Typo — "lamps" vs "lamp". Must fail loud at load time, not
        # silently fall through at first use.
        with pytest.raises(ValidationError) as exc_info:
            UserPreferences(lighting_tier_priority=["lamp", "lamps"])
        assert "Unknown lighting tier" in str(exc_info.value)

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UserPreferences(lighting_tier_priority=["lamp", "accent", "lamp"])
        assert "Duplicate" in str(exc_info.value)

    def test_accepts_subset_in_custom_order(self) -> None:
        # Valid — user prefers overheads everywhere, lamps are a
        # fallback. No rule says they must include every tier.
        prefs = UserPreferences(
            lighting_tier_priority=["overhead", "task", "lamp"]
        )
        assert prefs.lighting_tier_priority == ["overhead", "task", "lamp"]


class TestAreaAliases:
    def test_aliases_are_lowercased_and_trimmed(self) -> None:
        prefs = UserPreferences(area_aliases={
            "ResidentB's Office": "residentb_office",
            "  THE  Workshop  ": "garage",
        })
        assert prefs.area_aliases == {
            "residentb's office": "residentb_office",
            "the workshop": "garage",
        }

    def test_empty_alias_key_dropped(self) -> None:
        prefs = UserPreferences(area_aliases={"  ": "whatever"})
        assert prefs.area_aliases == {}

    def test_empty_area_id_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UserPreferences(area_aliases={"home": ""})
        assert "empty area_id" in str(exc_info.value)

    def test_resolve_alias_roundtrip(self) -> None:
        prefs = UserPreferences(area_aliases={
            "ResidentB's Office": "residentb_office",
        })
        assert prefs.resolve_area_alias("residentb's office") == "residentb_office"
        assert prefs.resolve_area_alias("ResidentB's office") == "residentb_office"
        assert prefs.resolve_area_alias("  residentb's   office  ") == "residentb_office"
        assert prefs.resolve_area_alias("unknown room") is None
        assert prefs.resolve_area_alias(None) is None
        assert prefs.resolve_area_alias("") is None


class TestTaskAreaQuery:
    def test_is_task_area_true_for_configured(self) -> None:
        prefs = UserPreferences(task_areas=["kitchen", "office_desk"])
        assert prefs.is_task_area("kitchen") is True
        assert prefs.is_task_area("office_desk") is True

    def test_is_task_area_false_for_others(self) -> None:
        prefs = UserPreferences(task_areas=["kitchen"])
        assert prefs.is_task_area("living_room") is False
        assert prefs.is_task_area(None) is False
        assert prefs.is_task_area("") is False


class TestBrightnessBounds:
    def test_brightness_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            UserPreferences(default_normal_brightness_pct=150)

    def test_brightness_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            UserPreferences(brightness_step_pct=0)

    def test_kelvin_bounds(self) -> None:
        with pytest.raises(ValidationError):
            UserPreferences(default_warm_kelvin=1000)  # below 1500


class TestExtraFieldsForbidden:
    def test_unknown_field_rejected(self) -> None:
        # Catches typos at load time (e.g., "brightness_steps_pct").
        with pytest.raises(ValidationError):
            UserPreferences(brightness_steps_pct=25)  # type: ignore[call-arg]


class TestYamlLoadSave:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        prefs = load_user_preferences(tmp_path / "does_not_exist.yaml")
        assert prefs.lighting_tier_priority == list(DEFAULT_TIER_PRIORITY)

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "prefs.yaml"
        p.write_text("", encoding="utf-8")
        prefs = load_user_preferences(p)
        assert prefs.default_warm_kelvin == 2700

    def test_partial_yaml_merges_with_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "prefs.yaml"
        p.write_text(
            "default_warm_kelvin: 2500\n"
            "area_aliases:\n"
            "  my office: office\n",
            encoding="utf-8",
        )
        prefs = load_user_preferences(p)
        assert prefs.default_warm_kelvin == 2500
        assert prefs.area_aliases == {"my office": "office"}
        # Untouched fields keep their defaults
        assert prefs.default_cool_kelvin == 5000

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        original = UserPreferences(
            default_warm_kelvin=2400,
            area_aliases={"ResidentB's Office": "residentb_office"},
            task_areas=["kitchen", "bathroom_vanity"],
        )
        p = tmp_path / "prefs.yaml"
        save_user_preferences(original, p)
        assert p.exists()
        # Reload and compare
        reloaded = load_user_preferences(p)
        assert reloaded.default_warm_kelvin == 2400
        assert reloaded.area_aliases == {"residentb's office": "residentb_office"}
        assert reloaded.task_areas == ["kitchen", "bathroom_vanity"]

    def test_save_atomic_via_tempfile(self, tmp_path: Path) -> None:
        # The save path should not leave a half-written temp file
        # lying around after a successful write.
        p = tmp_path / "prefs.yaml"
        save_user_preferences(UserPreferences(), p)
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == []

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "prefs.yaml"
        p.write_text("this: is: not: valid:\n  - yaml", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_user_preferences(p)

    def test_non_mapping_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "prefs.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc_info:
            load_user_preferences(p)
        assert "mapping" in str(exc_info.value)
