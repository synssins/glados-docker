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


def _state(eid, name, state="on", domain=None, dc=None, area=None):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {
            "friendly_name": name,
            **({"device_class": dc} if dc else {}),
            **({"area_id": area} if area else {}),
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
                _state("lock.front_door", "Front Door", domain=None),
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
        r = disambig.run("turn off them", source="webui_chat")
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
