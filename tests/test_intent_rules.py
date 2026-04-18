"""Tests for glados.intent.rules — keyword-domain mapping, allowlist,
and YAML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.intent.rules import (
    DisambiguationRules,
    IntentAllowlist,
    domain_filter_for_utterance,
    load_rules_from_yaml,
)


class TestDomainFilter:
    def test_lights_returns_light_and_switch(self) -> None:
        d = domain_filter_for_utterance("turn off the bedroom lights")
        assert d is not None
        assert "light" in d and "switch" in d

    def test_lock_unlock_returns_lock(self) -> None:
        d = domain_filter_for_utterance("unlock the front door")
        assert d is not None
        assert "lock" in d

    def test_no_keyword_returns_none(self) -> None:
        d = domain_filter_for_utterance("how are you doing today")
        assert d is None

    def test_empty_utterance_returns_none(self) -> None:
        assert domain_filter_for_utterance("") is None
        assert domain_filter_for_utterance("   ") is None

    def test_punctuation_stripped(self) -> None:
        d = domain_filter_for_utterance("Lights, please!")
        assert d is not None
        assert "light" in d

    def test_case_insensitive(self) -> None:
        d = domain_filter_for_utterance("TURN ON THE LAMP")
        assert d is not None
        assert "light" in d


class TestAllowlist:
    def setup_method(self) -> None:
        self.al = IntentAllowlist()

    def test_light_allowed_from_all_sources(self) -> None:
        for src in ["webui_chat", "api_chat", "voice_mic", "mqtt_cmd", "autonomy"]:
            assert self.al.is_allowed(src, "light")

    def test_lock_only_from_webui_chat(self) -> None:
        assert self.al.is_allowed("webui_chat", "lock")
        for src in ["api_chat", "voice_mic", "mqtt_cmd", "autonomy"]:
            assert not self.al.is_allowed(src, "lock"), src

    def test_alarm_only_from_webui_chat(self) -> None:
        assert self.al.is_allowed("webui_chat", "alarm_control_panel")
        for src in ["api_chat", "voice_mic", "mqtt_cmd", "autonomy"]:
            assert not self.al.is_allowed(src, "alarm_control_panel")

    def test_garage_cover_treated_as_sensitive(self) -> None:
        # Non-garage cover: most sources allowed.
        assert self.al.is_allowed("api_chat", "cover", device_class="window")
        assert self.al.is_allowed("voice_mic", "cover", device_class=None)
        # Garage cover: webui_chat only.
        assert self.al.is_allowed("webui_chat", "cover", device_class="garage")
        for src in ["api_chat", "voice_mic", "mqtt_cmd", "autonomy"]:
            assert not self.al.is_allowed(src, "cover", device_class="garage")

    def test_camera_only_webui(self) -> None:
        assert self.al.is_allowed("webui_chat", "camera")
        assert not self.al.is_allowed("voice_mic", "camera")

    def test_unknown_domain_default_deny(self) -> None:
        # Safety: unknown domains require explicit allow.
        assert not self.al.is_allowed("webui_chat", "weird_unknown_domain")

    def test_autonomy_blocked_from_covers(self) -> None:
        # Even non-garage covers — autonomy can't open windows on its own.
        assert not self.al.is_allowed("autonomy", "cover")

    def test_explain_denial_mentions_garage(self) -> None:
        msg = self.al.explain_denial("voice_mic", "cover", device_class="garage")
        assert "garage" in msg


class TestRulesYAML:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        rules = load_rules_from_yaml(tmp_path / "absent.yaml")
        assert isinstance(rules, DisambiguationRules)
        assert rules.state_inference is True

    def test_partial_yaml_overrides_only_specified(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text(
            "max_state_age_seconds: 30\n"
            "candidate_limit: 5\n",
            encoding="utf-8",
        )
        rules = load_rules_from_yaml(p)
        assert rules.max_state_age_seconds == 30
        assert rules.candidate_limit == 5
        # Defaults preserved for other fields.
        assert rules.state_inference is True
        assert "lamp / lamps" in rules.naming_convention

    def test_full_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text("""
naming_convention:
  "lamp": "smart bulbs only"
  "light": "ceiling"
overhead_synonyms: ["ceiling", "roof"]
state_inference: false
max_state_age_seconds: 10
candidate_limit: 8
extra_guidance: "be terse"
""", encoding="utf-8")
        rules = load_rules_from_yaml(p)
        assert rules.naming_convention["lamp"] == "smart bulbs only"
        assert rules.overhead_synonyms == ["ceiling", "roof"]
        assert rules.state_inference is False
        assert rules.max_state_age_seconds == 10
        assert rules.candidate_limit == 8
        assert rules.extra_guidance == "be terse"

    def test_malformed_yaml_returns_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text(":::not:valid:::", encoding="utf-8")
        rules = load_rules_from_yaml(p)
        assert rules.state_inference is True  # defaulted
