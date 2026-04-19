"""Tests for glados.intent.disambiguator.

Mocks Ollama via monkeypatching `_call_ollama`, and uses a fake HA
client that records call_service invocations without doing IO.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from glados.ha.entity_cache import EntityCache
from glados.intent.disambiguator import (
    Disambiguator,
    DisambiguationResult,
    _safe_parse_json,
)
from glados.intent.rules import DisambiguationRules, IntentAllowlist


def _state(eid, name, state="on", domain=None, dc=None, area=None,
           extra_attrs=None):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {
            "friendly_name": name,
            **({"device_class": dc} if dc else {}),
            **({"area_id": area} if area else {}),
            **(extra_attrs or {}),
        },
    }


class _FakeHAClient:
    """Captures call_service invocations. No network."""

    def __init__(self, fail=False):
        self.calls: list[dict] = []
        self.fail = fail

    def call_service(self, domain, service, target=None,
                     service_data=None, timeout_s=None):
        self.calls.append({
            "domain": domain, "service": service,
            "target": target, "service_data": service_data,
        })
        if self.fail:
            raise RuntimeError("simulated HA failure")
        return {"success": True, "result": {"context": {"id": "ctx-fake"}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make(cache_states, llm_response: str, fail_ha=False,
          force_candidates=True):
    """Build a Disambiguator with a primed cache + canned LLM response.

    When force_candidates=True (default), the cache's fuzzy matcher is
    bypassed: get_candidates() returns ALL entities. This isolates the
    disambiguator's decision logic from the fuzzy-matching layer
    (which has its own tests and depends on rapidfuzz scoring quirks)."""
    from glados.ha.entity_cache import CandidateMatch
    cache = EntityCache()
    cache.apply_get_states(cache_states)
    if force_candidates:
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name or e.entity_id,
                           score=100.0, sensitive=(e.domain in {"lock", "alarm_control_panel", "camera"}))
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
    ha = _FakeHAClient(fail=fail_ha)
    disambig = Disambiguator(
        ha_client=ha, cache=cache,
        ollama_url="http://fake", model="glados",
        rules=DisambiguationRules(),
        allowlist=IntentAllowlist(),
    )
    disambig._call_ollama = MagicMock(return_value=llm_response)  # type: ignore[method-assign]
    return disambig, ha, cache


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_clean_json(self) -> None:
        assert _safe_parse_json('{"a": 1}') == {"a": 1}

    def test_with_code_fence(self) -> None:
        assert _safe_parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_trailing_commentary(self) -> None:
        assert _safe_parse_json('{"a": 1} oops extra text') == {"a": 1}

    def test_garbage_returns_none(self) -> None:
        assert _safe_parse_json("not json at all") is None

    def test_array_at_top_returns_none(self) -> None:
        assert _safe_parse_json("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Execute path
# ---------------------------------------------------------------------------

class TestExecute:
    def test_executes_single_entity(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.kitchen_ceiling", "Kitchen Ceiling", state="on"),
            ],
            llm_response='{"decision":"execute","entity_ids":["light.kitchen_ceiling"],'
                         '"service":"turn_off","speech":"Kitchen darkened.","rationale":"only candidate on"}',
        )
        r = disambig.run("turn off the kitchen lights", source="webui_chat")
        assert r.handled is True and r.decision == "execute"
        assert r.entity_ids == ["light.kitchen_ceiling"]
        assert r.service == "light.turn_off"
        assert ha.calls == [{
            "domain": "light", "service": "turn_off",
            "target": {"entity_id": ["light.kitchen_ceiling"]},
            "service_data": None,
        }]

    def test_executes_group(self) -> None:
        states = [
            _state("light.bed_ceiling", "Bedroom Ceiling", state="on"),
            _state("light.bed_lamp",    "Bedroom Lamp",    state="on"),
            _state("light.bed_closet",  "Bedroom Closet",  state="on"),
        ]
        ids = ["light.bed_ceiling", "light.bed_lamp", "light.bed_closet"]
        disambig, ha, _ = _make(
            cache_states=states,
            llm_response='{"decision":"execute","entity_ids":' + str(ids).replace("'", '"') +
                         ',"service":"turn_off","speech":"All bedroom lights off.","rationale":"plural"}',
        )
        r = disambig.run("turn off the bedroom lights", source="webui_chat")
        assert r.entity_ids == ids
        assert ha.calls[0]["target"]["entity_id"] == ids


# ---------------------------------------------------------------------------
# Clarify / refuse paths
# ---------------------------------------------------------------------------

class TestClarifyRefuse:
    def test_clarify_does_not_call_ha(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.bed_a", "Bedroom A", state="on"),
                _state("light.kit_a", "Kitchen A", state="on"),
            ],
            llm_response='{"decision":"clarify","entity_ids":[],"service":"",'
                         '"speech":"Two rooms have lights on. Which?","rationale":"disjoint groups"}',
        )
        r = disambig.run("turn off the lights", source="webui_chat")
        assert r.handled is True and r.decision == "clarify"
        assert "Two rooms" in r.speech
        assert ha.calls == []

    def test_refuse_does_not_call_ha(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("lock.entry_door", "Front Door", domain=None),
            ],
            llm_response='{"decision":"refuse","entity_ids":[],"service":"",'
                         '"speech":"No.","rationale":"sensitive"}',
        )
        r = disambig.run("unlock the front door", source="voice_mic")
        assert r.decision == "refuse"
        assert ha.calls == []


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------

class TestAllowlistEnforcement:
    def test_lock_from_voice_blocked_even_if_llm_says_execute(self) -> None:
        """LLM may try to be helpful, but the allowlist always wins."""
        disambig, ha, _ = _make(
            cache_states=[_state("lock.front", "Front Door")],
            llm_response='{"decision":"execute","entity_ids":["lock.front"],'
                         '"service":"unlock","speech":"sure","rationale":"asked"}',
        )
        r = disambig.run("unlock the front door", source="voice_mic")
        assert r.decision == "refuse"
        assert ha.calls == []
        assert "allowlist_denied" in r.rationale

    def test_lock_from_webui_chat_allowed(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("lock.front", "Front Door", state="locked")],
            llm_response='{"decision":"execute","entity_ids":["lock.front"],'
                         '"service":"unlock","speech":"Unlocked.","rationale":"asked"}',
        )
        r = disambig.run("unlock the front door", source="webui_chat")
        assert r.decision == "execute"
        assert ha.calls and ha.calls[0]["domain"] == "lock"


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------

class TestDefensive:
    def test_no_candidates_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[],
            llm_response='{}',
        )
        r = disambig.run("turn off the lights", source="webui_chat")
        assert r.handled is False
        assert r.should_fall_through is True
        assert r.decision == "fall_through"

    def test_bad_json_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("light.x", "X", state="on")],
            llm_response="not json",
        )
        r = disambig.run("turn off the light", source="webui_chat")
        assert r.should_fall_through is True

    def test_unknown_decision_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("light.x", "X", state="on")],
            llm_response='{"decision":"explode","entity_ids":[],"service":""}',
        )
        r = disambig.run("turn off the light", source="webui_chat")
        assert r.should_fall_through is True

    def test_unknown_entity_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("light.x", "X", state="on")],
            llm_response='{"decision":"execute","entity_ids":["light.imaginary"],'
                         '"service":"turn_off","speech":"ok","rationale":"x"}',
        )
        r = disambig.run("turn off the light", source="webui_chat")
        assert r.should_fall_through is True

    def test_mixed_domains_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.a", "A", state="on"),
                _state("switch.b", "B", state="on"),
            ],
            llm_response='{"decision":"execute","entity_ids":["light.a","switch.b"],'
                         '"service":"turn_off","speech":"ok","rationale":"x"}',
        )
        # Utterance must pass the Phase 6 home-command precheck so Tier 2
        # actually runs and exercises the mixed-domains fall-through path.
        r = disambig.run("turn off the lights", source="webui_chat")
        assert r.should_fall_through is True
        assert "mixed_domains" in r.rationale

    def test_ha_failure_falls_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("light.a", "A", state="on")],
            llm_response='{"decision":"execute","entity_ids":["light.a"],'
                         '"service":"turn_off","speech":"ok","rationale":"x"}',
            fail_ha=True,
        )
        r = disambig.run("turn off the light", source="webui_chat")
        assert r.should_fall_through is True
        assert "call_service_failed" in r.rationale


class TestHomeCommandPrecheck:
    """Phase 6 follow-up — utterances with no device/activity signal must
    skip Tier 2 entirely so the LLM never sees candidates for proper
    nouns. Regression: "Say hello to my little friend, his name is
    Alan" fuzzy-matched 'alan' across 12 entities and the LLM returned
    a clarify response that read raw entity IDs to the operator."""

    @pytest.mark.parametrize("utterance", [
        "Say hello to my little friend.... His name is Alan.",
        "Hello GLaDOS",
        "How are you today?",
        "What's your favorite color?",
        "Tell me about cake",
        "Please say something nice",
    ])
    def test_chitchat_falls_through_before_llm(self, utterance: str) -> None:
        disambig, _, _ = _make(
            cache_states=[
                _state("binary_sensor.sm_alan_is_charging", "Alan charging", state="off"),
                _state("sensor.user_b_state", "Alan T state", state="idle"),
            ],
            llm_response='{"decision":"clarify","speech":"should not run"}',
        )
        r = disambig.run(utterance, source="webui_chat")
        assert r.should_fall_through is True
        assert r.rationale.startswith("no_home_command_intent")
        # LLM must NOT have been called on these utterances.
        disambig._call_ollama.assert_not_called()

    @pytest.mark.parametrize("utterance", [
        "turn off the bedroom lights",
        "dim the kitchen lamps",
        "activate the evening scene",
        "set the thermostat to 68",
        "is the garage door closed",
        "play some music in the living room",
        "goodnight",
        "movie time",
        "wake up",
    ])
    def test_home_commands_reach_tier2(self, utterance: str) -> None:
        """Utterances with a device keyword or activity phrase must
        still reach Tier 2. Mock LLM returns a benign clarify so we
        just need to confirm the LLM was called."""
        disambig, _, _ = _make(
            cache_states=[_state("light.bedroom", "Bedroom", state="off")],
            llm_response='{"decision":"clarify","speech":"need more detail"}',
        )
        r = disambig.run(utterance, source="webui_chat")
        # We don't care about the specific outcome — just that the
        # precheck didn't short-circuit.
        disambig._call_ollama.assert_called_once()
        assert not r.rationale.startswith("no_home_command_intent")


class TestSpeechEntityIdLeakGuard:
    """Phase 6 follow-up — if the LLM's speech contains any candidate
    entity_id (the 'Ambiguity detected: binary_sensor.sm_…' regression),
    Tier 2 must fall through rather than voice developer strings."""

    def test_leaked_entity_id_in_clarify_falls_through(self) -> None:
        disambig, _, _ = _make(
            cache_states=[
                _state("binary_sensor.alan_is_charging", "Alan phone charging", state="off"),
                _state("sensor.user_b_state", "Alan T state", state="idle"),
            ],
            # Utterance that DOES pass the precheck (has 'lights'),
            # so we exercise the speech-leak guard specifically.
            llm_response=(
                '{"decision":"clarify",'
                '"speech":"Ambiguity detected: binary_sensor.alan_is_charging, '
                'sensor.user_b_state. Specify which Alan you mean.",'
                '"rationale":"x"}'
            ),
        )
        r = disambig.run("turn off the lights for Alan", source="webui_chat")
        assert r.should_fall_through is True
        assert "speech_leaked_entity_ids" in r.rationale

    def test_clean_clarify_speech_is_preserved(self) -> None:
        disambig, _, _ = _make(
            cache_states=[
                _state("light.bedroom_main", "Master Bedroom Main", state="on"),
                _state("light.bedroom_reading", "Bedroom Reading Lamp", state="on"),
            ],
            llm_response=(
                '{"decision":"clarify",'
                '"speech":"Two candidates match: the master bedroom main '
                'and the reading lamp. Specify.","rationale":"x"}'
            ),
        )
        r = disambig.run("turn off the bedroom lights", source="webui_chat")
        assert r.handled is True
        assert r.should_fall_through is False
        assert r.decision == "clarify"
        assert "master bedroom" in r.speech.lower()


class TestHomeCommandHelper:
    """Unit coverage for the rules.looks_like_home_command helper itself."""

    @pytest.mark.parametrize("utterance", [
        "Say hello to my little friend.... His name is Alan.",
        "Good evening",
        "Tell me a joke",
        "What time is it",
        "",
    ])
    def test_non_home_commands_return_false(self, utterance: str) -> None:
        from glados.intent.rules import looks_like_home_command
        assert looks_like_home_command(utterance) is False

    @pytest.mark.parametrize("utterance", [
        "turn off the bedroom lights",
        "set thermostat to 70",
        "lock the front door",
        "open the blinds",
        "play music in the kitchen",
        "activate the evening scene",
        "goodnight",
        "movie time",
        "time for bed",
        "wake up",
    ])
    def test_home_commands_return_true(self, utterance: str) -> None:
        from glados.intent.rules import looks_like_home_command
        assert looks_like_home_command(utterance) is True


class TestServiceData:
    """P0 2026-04-19 — Tier 2 must forward service_data (brightness,
    colour, temperature, volume, fan speed) through to HA. Without this,
    'dim the lamp', 'set to 40 percent', 'warmer' all resolve to bare
    turn_on calls and the device silently ignores the parameter."""

    def test_absolute_brightness_passes_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state(
                    "light.desk_lamp", "Desk Lamp", state="on",
                    extra_attrs={"brightness": 128},
                ),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.desk_lamp"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":40},'
                '"speech":"Desk lamp to forty percent.","rationale":"absolute"}'
            ),
        )
        r = disambig.run("set the desk lamp to 40 percent", source="webui_chat")
        assert r.decision == "execute"
        assert r.service_data == {"brightness_pct": 40}
        assert ha.calls == [{
            "domain": "light", "service": "turn_on",
            "target": {"entity_id": ["light.desk_lamp"]},
            "service_data": {"brightness_pct": 40},
        }]

    def test_relative_adjustment_reads_current_state(self) -> None:
        """The prompt exposes current brightness via attrs=; the LLM
        is responsible for computing current+25. We verify that
        whatever service_data it returns is threaded through."""
        disambig, ha, _ = _make(
            cache_states=[
                _state(
                    "light.desk_lamp", "Desk Lamp", state="on",
                    extra_attrs={"brightness": 76},  # ~30%
                ),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.desk_lamp"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":55},'
                '"speech":"Brighter. Marginally.","rationale":"relative"}'
            ),
        )
        r = disambig.run("turn up the desk lamp", source="webui_chat")
        assert r.service_data == {"brightness_pct": 55}
        assert ha.calls[0]["service_data"] == {"brightness_pct": 55}

    def test_color_temperature_passes_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state(
                    "light.desk_lamp", "Desk Lamp", state="on",
                    extra_attrs={"color_temp_kelvin": 4500},
                ),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.desk_lamp"],'
                '"service":"turn_on",'
                '"service_data":{"color_temp_kelvin":2700},'
                '"speech":"Warmer light.","rationale":"warmer"}'
            ),
        )
        r = disambig.run("make the desk lamp warmer", source="webui_chat")
        assert r.service_data == {"color_temp_kelvin": 2700}
        assert ha.calls[0]["service_data"] == {"color_temp_kelvin": 2700}

    def test_color_name_passes_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.strip", "Strip Light", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.strip"],'
                '"service":"turn_on",'
                '"service_data":{"color_name":"blue"},'
                '"speech":"Blue.","rationale":"color by name"}'
            ),
        )
        r = disambig.run("change the strip light to blue", source="webui_chat")
        assert r.service_data == {"color_name": "blue"}
        assert ha.calls[0]["service_data"] == {"color_name": "blue"}

    def test_fan_percentage_passes_through(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state(
                    "fan.living", "Living Room Fan", state="on",
                    extra_attrs={"percentage": 50},
                ),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["fan.living"],'
                '"service":"turn_on",'
                '"service_data":{"percentage":30},'
                '"speech":"Thirty percent.","rationale":"lower"}'
            ),
        )
        r = disambig.run("slow down the fan", source="webui_chat")
        assert r.service_data == {"percentage": 30}
        assert ha.calls[0]["service_data"] == {"percentage": 30}

    def test_omitted_service_data_sends_none(self) -> None:
        """Bare turn_off must continue to pass service_data=None so HA
        doesn't get an empty-but-present payload."""
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.kitchen", "Kitchen", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen"],'
                '"service":"turn_off",'
                '"speech":"Off.","rationale":"bare"}'
            ),
        )
        r = disambig.run("turn off the kitchen lights", source="webui_chat")
        assert r.decision == "execute"
        assert r.service_data == {}
        assert ha.calls[0]["service_data"] is None

    def test_prompt_exposes_relevant_attributes(self) -> None:
        """State-fresh candidate lines must include brightness / color
        temp / fan percentage so the LLM can compute relative deltas."""
        from glados.ha.entity_cache import CandidateMatch, EntityCache

        cache = EntityCache()
        cache.apply_get_states([
            _state(
                "light.desk_lamp", "Desk Lamp", state="on",
                extra_attrs={"brightness": 76, "color_temp_kelvin": 3500},
            ),
        ])
        entity = cache.snapshot()[0]
        match = CandidateMatch(
            entity=entity, matched_name=entity.friendly_name,
            score=100.0, sensitive=False,
        )
        disambig = Disambiguator(
            ha_client=_FakeHAClient(), cache=cache,
            ollama_url="http://fake", model="glados",
            rules=DisambiguationRules(), allowlist=IntentAllowlist(),
        )
        msgs = disambig._build_prompt(
            utterance="turn it up",
            source="webui_chat",
            candidates=[match],
            state_fresh=True,
        )
        user_prompt = msgs[-1]["content"]
        assert "brightness_pct=30" in user_prompt  # derived from 76/255
        assert "color_temp_kelvin=3500" in user_prompt

    def test_prompt_omits_attributes_when_state_is_stale(self) -> None:
        from glados.ha.entity_cache import CandidateMatch, EntityCache

        cache = EntityCache()
        cache.apply_get_states([
            _state(
                "light.desk_lamp", "Desk Lamp", state="on",
                extra_attrs={"brightness": 76},
            ),
        ])
        entity = cache.snapshot()[0]
        match = CandidateMatch(
            entity=entity, matched_name=entity.friendly_name,
            score=100.0, sensitive=False,
        )
        disambig = Disambiguator(
            ha_client=_FakeHAClient(), cache=cache,
            ollama_url="http://fake", model="glados",
            rules=DisambiguationRules(), allowlist=IntentAllowlist(),
        )
        msgs = disambig._build_prompt(
            utterance="turn it up",
            source="webui_chat",
            candidates=[match],
            state_fresh=False,
        )
        user_prompt = msgs[-1]["content"]
        assert "attrs=" not in user_prompt

    def test_trailing_vocative_is_stripped_from_tier2_speech(self) -> None:
        """Tier 2 speech never goes through the rewriter's persona restyle
        (it's already persona-voiced from the disambiguator prompt). The
        trailing-vocative safety net has to run here too, otherwise 'test
        subject' leaks when the LLM ignores the explicit instruction."""
        disambig, ha, _ = _make(
            cache_states=[_state("light.desk_lamp", "Desk Lamp", state="on")],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.desk_lamp"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":70},'
                '"speech":"The desk lamp has been adjusted to your '
                'satisfaction, test subject.","rationale":"x"}'
            ),
        )
        r = disambig.run("turn the desk lamp up a bit", source="webui_chat")
        assert r.decision == "execute"
        assert "test subject" not in r.speech.lower()
        assert r.speech.endswith(".")

    def test_clarify_vocative_is_stripped(self) -> None:
        disambig, _, _ = _make(
            cache_states=[
                _state("light.a", "Master Bedroom", state="on"),
                _state("light.b", "Guest Bedroom", state="on"),
            ],
            llm_response=(
                '{"decision":"clarify",'
                '"speech":"Two bedrooms qualify. Specify, human.",'
                '"rationale":"x"}'
            ),
        )
        r = disambig.run("turn off the bedroom lights", source="webui_chat")
        assert r.decision == "clarify"
        assert ", human" not in r.speech.lower()

    def test_execute_no_ack_preserves_service_data(self) -> None:
        """A call_service timeout still reports the service_data that
        was requested so audit rows stay truthful."""
        import concurrent.futures

        disambig, ha, _ = _make(
            cache_states=[
                _state("light.desk_lamp", "Desk Lamp", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.desk_lamp"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":60},'
                '"speech":"Done.","rationale":"x"}'
            ),
        )

        def _timeout(**kwargs):
            raise concurrent.futures.TimeoutError("no ack")

        ha.call_service = _timeout  # type: ignore[method-assign]
        r = disambig.run("set the desk lamp to 60%", source="webui_chat")
        assert r.decision == "execute_no_ack"
        assert r.service_data == {"brightness_pct": 60}
