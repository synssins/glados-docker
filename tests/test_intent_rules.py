"""Tests for glados.intent.rules — keyword-domain mapping, allowlist,
and YAML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.intent.rules import (
    DisambiguationRules,
    IntentAllowlist,
    apply_precheck_overrides,
    domain_filter_for_utterance,
    explain_home_command_match,
    load_rules_from_yaml,
    looks_like_home_command,
    min_expected_action_count,
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


class TestPhase81RulesFields:
    def test_defaults_empty_opposing_pairs_dedup_on(self) -> None:
        rules = DisambiguationRules()
        assert rules.opposing_token_pairs == []
        assert rules.twin_dedup is True

    def test_load_opposing_token_pairs(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text("""
opposing_token_pairs:
  - [upstairs, downstairs]
  - [kids, master]
twin_dedup: false
""", encoding="utf-8")
        rules = load_rules_from_yaml(p)
        assert rules.opposing_token_pairs == [
            ["upstairs", "downstairs"],
            ["kids", "master"],
        ]
        assert rules.twin_dedup is False

    def test_malformed_pairs_are_dropped(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text("""
opposing_token_pairs:
  - [valid, pair]
  - [onlyone]
  - [a, a]
  - not_a_list
""", encoding="utf-8")
        rules = load_rules_from_yaml(p)
        assert rules.opposing_token_pairs == [["valid", "pair"]]

    def test_roundtrip_save_load(self, tmp_path: Path) -> None:
        from glados.intent.rules import save_rules_to_yaml
        rules = DisambiguationRules()
        rules.opposing_token_pairs = [["left", "right"]]
        rules.twin_dedup = False
        p = tmp_path / "out.yaml"
        save_rules_to_yaml(p, rules)
        loaded = load_rules_from_yaml(p)
        assert loaded.opposing_token_pairs == [["left", "right"]]
        assert loaded.twin_dedup is False

    def test_phase_85_alias_roundtrip(self, tmp_path: Path) -> None:
        """Phase 8.5 — floor/area alias maps survive a YAML round-trip."""
        from glados.intent.rules import save_rules_to_yaml
        rules = DisambiguationRules()
        rules.floor_aliases = {"main level": "Main Floor"}
        rules.area_aliases = {"mom's room": "Master Bedroom"}
        p = tmp_path / "out.yaml"
        save_rules_to_yaml(p, rules)
        loaded = load_rules_from_yaml(p)
        assert loaded.floor_aliases == {"main level": "Main Floor"}
        assert loaded.area_aliases == {"mom's room": "Master Bedroom"}

    def test_phase_85_alias_keys_lowercased_and_stripped(self, tmp_path: Path) -> None:
        """Operator typed mixed-case keys; loader normalises them so
        the inference module's lowercase lookup matches."""
        p = tmp_path / "aliases.yaml"
        p.write_text(
            "floor_aliases:\n"
            "  '  Main Level  ': 'Main Floor'\n"
            "area_aliases:\n"
            "  PATIO: 'Patio'\n",
            encoding="utf-8",
        )
        loaded = load_rules_from_yaml(p)
        assert loaded.floor_aliases == {"main level": "Main Floor"}
        assert loaded.area_aliases == {"patio": "Patio"}


# ──────────────────────────────────────────────────────────────
# Phase 8.2 — Command verbs, ambient patterns, precheck extras
# ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_precheck_overrides_between_tests():
    """Precheck overrides live at module level. Reset them before and
    after each test so one test's configuration doesn't leak into
    another's. Cleaner than making every test call it manually."""
    apply_precheck_overrides(DisambiguationRules())
    yield
    apply_precheck_overrides(DisambiguationRules())


class TestCommandVerbPrecheck:
    def test_shipped_verbs_trigger_precheck(self) -> None:
        # "darken" alone should pass — no noun keyword needed.
        assert looks_like_home_command("darken the bedroom")
        assert looks_like_home_command("bump it up a bit")
        assert looks_like_home_command("crank everything")
        assert looks_like_home_command("kill the power")

    def test_no_verb_no_noun_falls_through(self) -> None:
        # Pure chitchat with no verb, no noun, no activity, no pattern
        # must remain a chitchat candidate.
        assert not looks_like_home_command("tell me about rome")
        assert not looks_like_home_command("what's your favourite colour")

    def test_extra_verbs_additive_to_defaults(self) -> None:
        # Defaults still work after overrides are applied with extras.
        rules = DisambiguationRules()
        rules.extra_command_verbs = ["nudge", "tickle"]
        apply_precheck_overrides(rules)
        assert looks_like_home_command("nudge the thermostat")
        assert looks_like_home_command("tickle the lights")
        # Default verb still active.
        assert looks_like_home_command("darken the room")

    def test_extra_verbs_are_case_insensitive(self) -> None:
        rules = DisambiguationRules()
        rules.extra_command_verbs = ["NUDGE"]
        apply_precheck_overrides(rules)
        assert looks_like_home_command("nudge it")
        assert looks_like_home_command("Nudge it")


# ──────────────────────────────────────────────────────────────
# Phase 8.6 — compound-command expected-action-count helper
# ──────────────────────────────────────────────────────────────

class TestMinExpectedActionCount:
    def test_no_conjunction_returns_one(self) -> None:
        assert min_expected_action_count("dim the bedroom") == 1
        assert min_expected_action_count("turn off the kitchen lights") == 1
        assert min_expected_action_count("") == 1

    def test_single_verb_across_multiple_entities_returns_one(self) -> None:
        # "turn off the kitchen AND the living room" is still ONE
        # action with two entity_ids. Only one direction particle
        # ('off') + no additional verb = not compound.
        assert min_expected_action_count(
            "turn off the kitchen and the living room"
        ) == 1

    def test_two_different_verbs_returns_two(self) -> None:
        assert min_expected_action_count(
            "brighten the office and dim the basement"
        ) == 2

    def test_two_direction_particles_returns_two(self) -> None:
        # "turn X up AND Y down" — two distinct direction particles
        # signal a compound command.
        assert min_expected_action_count(
            "turn the kitchen cabinet up and the lower hallway down"
        ) == 2

    def test_kill_and_light_up_returns_two(self) -> None:
        assert min_expected_action_count(
            "kill the office wall wash lights and light up the living room cabinets"
        ) == 2

    def test_semicolon_also_counts_as_conjunction(self) -> None:
        assert min_expected_action_count(
            "dim the office; brighten the hallway"
        ) == 2

    def test_then_counts_as_conjunction(self) -> None:
        assert min_expected_action_count(
            "close the shades then turn on the reading lamp"
        ) == 2

    def test_conjunction_without_enough_signals_stays_one(self) -> None:
        # "and" joining two non-command phrases doesn't mean compound.
        assert min_expected_action_count(
            "the office is dark and cold"
        ) == 1


class TestAmbientPatternPrecheck:
    def test_default_its_too_dark(self) -> None:
        assert looks_like_home_command("it's too dark in here")
        assert looks_like_home_command("the living room is cold")

    def test_default_i_want_more_light(self) -> None:
        assert looks_like_home_command("I want more light")
        assert looks_like_home_command("I need it brighter")
        assert looks_like_home_command("I can't see")

    def test_time_to_read_triggers(self) -> None:
        assert looks_like_home_command("time to read")
        assert looks_like_home_command("time for bed")

    def test_bare_i_want_x_stays_chitchat(self) -> None:
        # Phase 8.2 intentionally keeps "I want X" patterns narrow so
        # random desires don't punch through the precheck. Note the
        # chosen phrase avoids tripping the separate _ACTIVITY_PHRASES
        # set (which includes words like "dinner", "movie" on their own).
        assert not looks_like_home_command("I want coffee")
        assert not looks_like_home_command("I need a new car")

    def test_custom_pattern_extends_defaults(self) -> None:
        rules = DisambiguationRules()
        rules.extra_ambient_patterns = [r"\bthe cats? (?:need|want)\b"]
        apply_precheck_overrides(rules)
        assert looks_like_home_command("the cats need feeding")
        # Default still fires.
        assert looks_like_home_command("it's too dark")

    def test_invalid_regex_is_skipped_not_fatal(self) -> None:
        rules = DisambiguationRules()
        rules.extra_ambient_patterns = ["(unterminated"]
        # Loader compile errors are logged but must not raise.
        apply_precheck_overrides(rules)
        # Defaults still work.
        assert looks_like_home_command("it's too dark")


class TestExplainHomeCommandMatch:
    def test_keyword_path_reports_reason_and_domains(self) -> None:
        res = explain_home_command_match("turn off the bedroom lights")
        assert res["matches"] is True
        assert "keyword" in res["via"]
        assert res["domains"] and "light" in res["domains"]

    def test_activity_phrase_reason(self) -> None:
        res = explain_home_command_match("movie time")
        assert res["matches"] is True
        assert "activity_phrase" in res["via"]

    def test_command_verb_reason(self) -> None:
        res = explain_home_command_match("darken")
        assert res["matches"] is True
        assert "command_verb" in res["via"]

    def test_ambient_pattern_reason(self) -> None:
        res = explain_home_command_match("it's too dark")
        assert res["matches"] is True
        assert "ambient_pattern" in res["via"]

    def test_no_match_empty_via(self) -> None:
        res = explain_home_command_match("tell me a joke")
        assert res["matches"] is False
        assert res["via"] == []

    def test_multiple_reasons_stack(self) -> None:
        # "time to read" is both an activity phrase AND an ambient pattern.
        res = explain_home_command_match("time to read")
        assert res["matches"] is True
        assert len(res["via"]) >= 1


class TestPhase82YAMLRoundTrip:
    def test_load_extra_verbs_and_patterns(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text("""
extra_command_verbs:
  - nudge
  - tickle
extra_ambient_patterns:
  - "\\\\bfeed the cats\\\\b"
""", encoding="utf-8")
        rules = load_rules_from_yaml(p)
        assert rules.extra_command_verbs == ["nudge", "tickle"]
        assert rules.extra_ambient_patterns == [r"\bfeed the cats\b"]

    def test_roundtrip_preserves_extras(self, tmp_path: Path) -> None:
        from glados.intent.rules import save_rules_to_yaml
        rules = DisambiguationRules()
        rules.extra_command_verbs = ["zap"]
        rules.extra_ambient_patterns = [r"\bwake me up\b"]
        p = tmp_path / "rules.yaml"
        save_rules_to_yaml(p, rules)
        loaded = load_rules_from_yaml(p)
        assert loaded.extra_command_verbs == ["zap"]
        assert loaded.extra_ambient_patterns == [r"\bwake me up\b"]
