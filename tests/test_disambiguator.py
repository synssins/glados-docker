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
    _candidate_search_text,
    _extract_qualifiers,
    _filter_by_qualifiers,
    _find_qualifier_matches,
    _parse_actions,
    _safe_parse_json,
)
from glados.ha.entity_cache import CandidateMatch, EntityCache, EntityState
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
    """Captures call_service invocations. No network.

    Extended for Phase 8.4: accepts state-changed callback
    registrations so StateVerifier can attach. Tests that need
    verification to SUCCEED can call `emit_state_changed()` to
    simulate the entity transition landing. Tests that want
    verification to FAIL simply don't emit — the watch times out.
    """

    def __init__(self, fail=False, autoemit: bool = True):
        self.calls: list[dict] = []
        self.fail = fail
        self._state_cbs: list = []
        # When True (default), call_service auto-emits a state_changed
        # event for each targeted entity inferred from the service,
        # emulating a responsive HA. Tests that want verification to
        # FAIL (timeout) should set autoemit=False.
        self.autoemit = autoemit
        # When set, overrides auto-derivation — fires these exact events.
        self.autoemit_on_call: list[dict] | None = None

    def call_service(self, domain, service, target=None,
                     service_data=None, timeout_s=None):
        self.calls.append({
            "domain": domain, "service": service,
            "target": target, "service_data": service_data,
        })
        if self.fail:
            raise RuntimeError("simulated HA failure")
        resp = {"success": True, "result": {"context": {"id": "ctx-fake"}}}
        if self.autoemit_on_call:
            for ev in self.autoemit_on_call:
                self.emit_state_changed(
                    ev["entity_id"], ev["state"],
                    ev.get("attributes") or {},
                )
        elif self.autoemit:
            # Derive expected state from service name.
            state_map = {"turn_on": "on", "turn_off": "off", "toggle": "on"}
            new_state = state_map.get(service)
            if new_state is not None and target:
                eids = target.get("entity_id") or []
                if isinstance(eids, str):
                    eids = [eids]
                attrs = dict(service_data or {})
                for eid in eids:
                    self.emit_state_changed(eid, new_state, attrs)
        return resp

    def on_state_changed(self, cb) -> None:
        self._state_cbs.append(cb)

    def off_state_changed(self, cb) -> None:
        try:
            self._state_cbs.remove(cb)
        except ValueError:
            pass

    def emit_state_changed(self, entity_id, state, attributes=None) -> None:
        payload = {
            "entity_id": entity_id,
            "new_state": {
                "entity_id": entity_id,
                "state": state,
                "attributes": attributes or {},
            },
        }
        for cb in list(self._state_cbs):
            cb(payload)


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
# Qualifier pre-filter
# ---------------------------------------------------------------------------

def _cand(entity_id: str, name: str, area: str | None = None) -> CandidateMatch:
    """Build a CandidateMatch around an EntityState for filter tests."""
    e = EntityState(
        entity_id=entity_id,
        friendly_name=name,
        domain=entity_id.split(".", 1)[0],
        state="on",
        state_as_of=time.time(),
        area_id=area,
    )
    return CandidateMatch(
        entity=e, matched_name=name, score=100.0, sensitive=False,
    )


class TestExtractQualifiers:
    def test_picks_distinctive_words(self) -> None:
        assert _extract_qualifiers("The desk lamp is too dim") == ["desk"]

    def test_strips_stopwords_and_verbs(self) -> None:
        # "turn", "on", "the" → stopwords. "lights" → generic head
        # noun. "office" is the distinctive qualifier.
        assert _extract_qualifiers("Turn on the office lights") == ["office"]

    def test_keeps_multiple_qualifiers_in_order(self) -> None:
        assert _extract_qualifiers(
            "Turn on the reading lamp in Cindy's office"
        ) == ["reading", "cindy's", "office"]

    def test_deduplicates_repeats(self) -> None:
        # Even if the user repeats a qualifier, it appears once in
        # the result.
        assert _extract_qualifiers("office office office lamp") == ["office"]

    def test_drops_length_one_tokens(self) -> None:
        assert _extract_qualifiers("a lamp") == []

    def test_empty_utterance_empty_list(self) -> None:
        assert _extract_qualifiers("") == []
        assert _extract_qualifiers("   ") == []

    def test_all_stopwords_empty_list(self) -> None:
        # "turn on the lights" is all stopwords + generic head noun.
        assert _extract_qualifiers("turn on the lights") == []

    def test_preserves_apostrophes(self) -> None:
        assert "cindy's" in _extract_qualifiers("Cindy's office lamp")


class TestFilterByQualifiers:
    def test_keeps_candidates_containing_all_qualifiers(self) -> None:
        candidates = [
            _cand("light.task_lamp_one", "Office Desk Monitor Lamp"),
            _cand("light.uplighter_floor_lamp", "Uplighter Floor Lamp"),
            _cand("light.floor_lamp_two", "Living Room Arc Lamp 1"),
        ]
        # "desk" qualifier → only the desk monitor lamp survives.
        filtered = _filter_by_qualifiers(candidates, ["desk"])
        assert len(filtered) == 1
        assert filtered[0].entity.entity_id == "light.task_lamp_one"

    def test_requires_all_qualifiers_not_any(self) -> None:
        candidates = [
            _cand("light.office_desk_lamp", "Office Desk Lamp"),
            _cand("light.bedroom_desk_lamp", "Bedroom Desk Lamp"),
            _cand("light.office_floor_lamp", "Office Floor Lamp"),
        ]
        # Both 'office' AND 'desk' must appear in the candidate's
        # text. Only the office desk lamp survives.
        filtered = _filter_by_qualifiers(candidates, ["office", "desk"])
        assert [c.entity.entity_id for c in filtered] == [
            "light.office_desk_lamp",
        ]

    def test_empty_qualifiers_returns_unfiltered(self) -> None:
        candidates = [
            _cand("light.one", "One"),
            _cand("light.two", "Two"),
        ]
        filtered = _filter_by_qualifiers(candidates, [])
        assert len(filtered) == 2

    def test_empty_candidates_stays_empty(self) -> None:
        assert _filter_by_qualifiers([], ["desk"]) == []

    def test_empty_filter_result_returned_verbatim(self) -> None:
        # Filter returning empty is the caller's signal to fall
        # back — it's not silently replaced with the input here.
        # The caller (Disambiguator.run) decides what to do.
        candidates = [
            _cand("light.office_lamp", "Office Lamp"),
        ]
        filtered = _filter_by_qualifiers(candidates, ["desk"])
        assert filtered == []

    def test_matches_against_entity_id_too(self) -> None:
        # Friendly_name doesn't contain 'desk' but entity_id does —
        # still counts. The user may speak of the device by a
        # qualifier that's in the entity_id or aliases.
        candidates = [
            _cand("light.office_desk_5678", "Plug 5678"),
            _cand("light.office_floor_1234", "Plug 1234"),
        ]
        filtered = _filter_by_qualifiers(candidates, ["desk"])
        assert len(filtered) == 1
        assert filtered[0].entity.entity_id == "light.office_desk_5678"

    def test_candidate_search_text_includes_aliases(self) -> None:
        e = EntityState(
            entity_id="light.fred_1",
            friendly_name="Plug 1",
            domain="light",
            state="on",
            state_as_of=time.time(),
            aliases=["Chris's reading lamp"],
        )
        c = CandidateMatch(entity=e, matched_name="Plug 1", score=90.0, sensitive=False)
        text = _candidate_search_text(c)
        assert "reading" in text
        assert "chris's" in text


class TestQualifierFilterIntegration:
    """End-to-end: the pre-filter in Disambiguator.run() restricts
    the candidate list the LLM sees, so state-based filtering can't
    cause the LLM to pick a different qualifier."""

    def test_desk_lamp_pins_to_desk_entity_when_others_are_on(self) -> None:
        # Setup: 'desk lamp' is OFF, other lamps in the same area
        # are ON. Without the pre-filter, the state-inference rule
        # would remove the desk lamp from the turn_on pool; the LLM
        # could then pick the floor lamp as "the brighter option".
        # With the pre-filter, the LLM only sees desk-matching
        # candidates.
        from unittest.mock import MagicMock
        from glados.ha.entity_cache import EntityCache
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.task_lamp_one",
                   "Office Desk Monitor Lamp", state="off", area="office"),
            _state("light.office_uplighter_floor_lamp",
                   "Office Uplighter Floor Lamp", state="on", area="office"),
        ])
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: list(all_matches)  # type: ignore[method-assign]

        from glados.intent.disambiguator import Disambiguator
        from glados.intent.rules import DisambiguationRules, IntentAllowlist
        ha = _FakeHAClient()
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=DisambiguationRules(), allowlist=IntentAllowlist(),
        )

        # Capture what the disambiguator passes to the LLM.
        captured_messages: list[list[dict]] = []

        def _fake_ollama(messages):
            captured_messages.append(list(messages))
            # LLM decides: execute on the (only) desk candidate
            return (
                '{"decision":"execute",'
                '"entity_ids":["light.task_lamp_one"],'
                '"service":"turn_on","service_data":{"brightness_pct":80},'
                '"speech":"Desk lamp, illuminated.",'
                '"rationale":"qualifier match"}'
            )
        d._call_ollama = _fake_ollama  # type: ignore[method-assign]

        result = d.run("The desk lamp is too dim", source="webui_chat")
        assert result.handled is True
        assert result.decision == "execute"
        assert result.entity_ids == ["light.task_lamp_one"]
        # Crucially, the user-message we sent to the LLM should NOT
        # include the uplighter as a candidate — it was filtered
        # out before the prompt was built.
        user_msg = next(
            m["content"] for m in captured_messages[0] if m["role"] == "user"
        )
        assert "office_desk_monitor_lamp" in user_msg
        assert "uplighter" not in user_msg.lower()

    def test_filter_falls_back_when_no_candidate_matches_qualifier(self) -> None:
        # If none of the user's entities contain the qualifier word,
        # don't drop all candidates — use the unfiltered list so the
        # LLM can still do its job.
        from glados.ha.entity_cache import EntityCache
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.room_a_ceiling", "Bedroom Ceiling", state="on", area="bedroom"),
            _state("light.ceiling_lights", "Kitchen Overhead", state="on", area="kitchen"),
        ])
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: list(all_matches)  # type: ignore[method-assign]

        from glados.intent.disambiguator import Disambiguator
        from glados.intent.rules import DisambiguationRules, IntentAllowlist
        ha = _FakeHAClient()
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=DisambiguationRules(), allowlist=IntentAllowlist(),
        )

        captured: list[list[dict]] = []

        def _fake_ollama(messages):
            captured.append(list(messages))
            # LLM clarifies because 'desk' doesn't match either
            return ('{"decision":"clarify","speech":"I see no desk '
                    'lamp.","rationale":"no desk lamp found"}')
        d._call_ollama = _fake_ollama  # type: ignore[method-assign]

        result = d.run("the desk lamp", source="webui_chat")
        assert result.handled is True
        # Both candidates should be in the prompt — the filter saw
        # zero matches for 'desk' and fell back to the full list.
        user_msg = next(m["content"] for m in captured[0] if m["role"] == "user")
        assert "bedroom_ceiling" in user_msg
        assert "kitchen_overhead" in user_msg


# ---------------------------------------------------------------------------
# Qualifier cache scan — the production fix for fuzzy saturation
# ---------------------------------------------------------------------------

class TestFindQualifierMatches:
    def _cache_with_desk_and_uplighter_segments(self) -> EntityCache:
        """Mimics the live failure case: one desk lamp, many same-
        scoring Uplighter Floor Lamp Segments. Fuzzy matcher would
        saturate its top-N with the segments and push the desk lamp
        out of view."""
        cache = EntityCache()
        states = [_state("light.task_lamp_one",
                         "Office Desk Monitor Lamp", state="on",
                         area="office")]
        # 15 segments, same structure as the live box
        for i in range(1, 16):
            states.append(_state(
                f"light.uplighter_floor_lamp_segment_{i:03d}",
                f"Uplighter Floor Lamp Segment {i:03d}",
                state="unknown", area="office",
            ))
        cache.apply_get_states(states)
        return cache

    def test_all_match_returns_only_qualifier_entities(self) -> None:
        cache = self._cache_with_desk_and_uplighter_segments()
        matches = _find_qualifier_matches(cache, ["desk"], ["light"])
        assert len(matches) == 1
        assert matches[0].entity.entity_id == "light.task_lamp_one"

    def test_any_match_fallback_covers_multi_area(self) -> None:
        # No single entity has both "office" and "entryway" — but
        # each area has entities matching one qualifier. ANY-match
        # fallback unions the two.
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.office_ceiling", "Office Ceiling",
                   state="off", area="office"),
            _state("light.office_lamp", "Office Lamp",
                   state="off", area="office"),
            _state("light.front_entryway_ceiling",
                   "Front Entryway Ceiling", state="off",
                   area="front_entryway"),
            _state("light.front_entryway_path",
                   "Front Entryway Path", state="off",
                   area="front_entryway"),
            _state("light.bedroom_lamp", "Bedroom Lamp",
                   state="off", area="bedroom"),
        ])
        matches = _find_qualifier_matches(
            cache, ["office", "front", "entryway"], ["light"],
        )
        kept = {m.entity.entity_id for m in matches}
        # Office entities matched "office"; entryway entities matched
        # "front" and "entryway". Bedroom matched none → excluded.
        assert "light.office_ceiling" in kept
        assert "light.office_lamp" in kept
        assert "light.front_entryway_ceiling" in kept
        assert "light.front_entryway_path" in kept
        assert "light.bedroom_lamp" not in kept

    def test_no_matches_returns_empty(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.kitchen", "Kitchen", state="on"),
        ])
        assert _find_qualifier_matches(cache, ["desk"], ["light"]) == []

    def test_respects_domain_filter(self) -> None:
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.office_lamp", "Office Lamp", state="off"),
            # Non-light domain with "office" in the name
            {"entity_id": "sensor.office_temperature",
             "state": "72",
             "attributes": {"friendly_name": "Office Temperature"}},
        ])
        matches = _find_qualifier_matches(cache, ["office"], ["light"])
        assert len(matches) == 1
        assert matches[0].entity.entity_id == "light.office_lamp"

    def test_uncapped(self) -> None:
        # 50 matching entities → all 50 should come back. No top-N
        # truncation.
        cache = EntityCache()
        cache.apply_get_states([
            _state(f"light.office_seg_{i:03d}", f"Office Seg {i}",
                   state="off")
            for i in range(50)
        ])
        matches = _find_qualifier_matches(cache, ["office"], ["light"])
        assert len(matches) == 50

    def test_all_match_preferred_over_any_match(self) -> None:
        # Cache has entities that match ALL qualifiers AND entities
        # matching only SOME. Return just the ALL-matches.
        cache = EntityCache()
        cache.apply_get_states([
            _state("light.office_desk", "Office Desk", state="off"),
            _state("light.office_floor", "Office Floor", state="off"),
            _state("light.bedroom_desk", "Bedroom Desk", state="off"),
        ])
        matches = _find_qualifier_matches(
            cache, ["office", "desk"], ["light"],
        )
        # ALL-match: office+desk present → only office_desk
        kept = {m.entity.entity_id for m in matches}
        assert kept == {"light.office_desk"}


# ---------------------------------------------------------------------------
# Action parsing — SHAPE 1 legacy vs SHAPE 2 compound
# ---------------------------------------------------------------------------

class TestParseActions:
    def test_legacy_shape_converts_to_single_action(self) -> None:
        actions = _parse_actions({
            "decision": "execute",
            "entity_ids": ["light.office_lamp"],
            "service": "light.turn_on",
            "service_data": {"brightness_pct": 60},
        })
        assert len(actions) == 1
        assert actions[0]["service"] == "light.turn_on"
        assert actions[0]["entity_ids"] == ["light.office_lamp"]
        assert actions[0]["service_data"] == {"brightness_pct": 60}

    def test_compound_shape_preserved(self) -> None:
        actions = _parse_actions({
            "decision": "execute",
            "actions": [
                {"service": "light.turn_on",
                 "entity_ids": ["light.office_lamp"]},
                {"service": "light.turn_off",
                 "entity_ids": ["light.living_room_lamp"]},
            ],
        })
        assert len(actions) == 2
        assert actions[0]["service"] == "light.turn_on"
        assert actions[1]["service"] == "light.turn_off"

    def test_compound_drops_invalid_entries(self) -> None:
        actions = _parse_actions({
            "decision": "execute",
            "actions": [
                {"service": "light.turn_on",
                 "entity_ids": ["light.office_lamp"]},
                {"service": "", "entity_ids": ["light.x"]},   # no service
                {"service": "fan.turn_on", "entity_ids": []}, # no entities
                "not a dict",                                 # wrong type
            ],
        })
        assert len(actions) == 1
        assert actions[0]["entity_ids"] == ["light.office_lamp"]

    def test_compound_preferred_over_legacy_when_both_present(self) -> None:
        # If the LLM emits both shapes (unlikely but defensive), the
        # `actions` list wins. Otherwise we'd double-fire.
        actions = _parse_actions({
            "decision": "execute",
            "entity_ids": ["light.legacy"],
            "service": "light.turn_on",
            "actions": [
                {"service": "fan.turn_on",
                 "entity_ids": ["fan.main"]},
            ],
        })
        assert len(actions) == 1
        assert actions[0]["service"] == "fan.turn_on"

    def test_returns_empty_for_unparseable(self) -> None:
        assert _parse_actions({"decision": "execute"}) == []
        assert _parse_actions({"decision": "execute",
                               "actions": []}) == []
        assert _parse_actions({"decision": "clarify"}) == []

    def test_unwraps_response_data_wrapper(self) -> None:
        # LLM drift observed 2026-04-19 on live 14B:
        # {"response": {"type": "json", "data": {"actions": [...]}}}
        # The parser should peel the wrapper and find the actions.
        actions = _parse_actions({
            "response": {
                "type": "json",
                "data": {
                    "actions": [
                        {"action": "turn_off",
                         "entity_id": "light.front_entryway_flood_01"},
                        {"action": "turn_on",
                         "entity_id": "light.ceiling_lights"},
                    ],
                    "message": "both done",
                },
            },
        })
        assert len(actions) == 2
        assert actions[0]["service"] == "turn_off"
        assert actions[0]["entity_ids"] == ["light.front_entryway_flood_01"]
        assert actions[1]["service"] == "turn_on"
        assert actions[1]["entity_ids"] == ["light.ceiling_lights"]

    def test_unwraps_result_wrapper(self) -> None:
        actions = _parse_actions({
            "result": {
                "decision": "execute",
                "entity_ids": ["light.office"],
                "service": "turn_on",
            },
        })
        assert len(actions) == 1
        assert actions[0]["entity_ids"] == ["light.office"]

    def test_per_action_field_aliases(self) -> None:
        # Each action may use `action` instead of `service` and
        # `entity_id` (singular) instead of `entity_ids` (list).
        actions = _parse_actions({
            "decision": "execute",
            "actions": [
                # `action` + `entity_id` (str)
                {"action": "light.turn_on",
                 "entity_id": "light.one"},
                # `service` + `entity_id` as list under singular name
                {"service": "light.turn_off",
                 "entity_id": ["light.two", "light.three"]},
                # `method` field alias
                {"method": "light.turn_on",
                 "entity_ids": ["light.four"]},
            ],
        })
        assert len(actions) == 3
        assert actions[0]["service"] == "light.turn_on"
        assert actions[0]["entity_ids"] == ["light.one"]
        assert actions[1]["entity_ids"] == ["light.two", "light.three"]
        assert actions[2]["service"] == "light.turn_on"

    def test_per_action_service_data_aliases(self) -> None:
        actions = _parse_actions({
            "actions": [
                {"service": "light.turn_on",
                 "entity_ids": ["light.a"],
                 "data": {"brightness_pct": 50}},
                {"action": "light.turn_on",
                 "entity_id": "light.b",
                 "params": {"brightness_pct": 80}},
                {"service": "light.turn_on",
                 "entity_ids": ["light.c"],
                 "parameters": {"brightness_pct": 30}},
            ],
        })
        assert actions[0]["service_data"] == {"brightness_pct": 50}
        assert actions[1]["service_data"] == {"brightness_pct": 80}
        assert actions[2]["service_data"] == {"brightness_pct": 30}


# ---------------------------------------------------------------------------
# Compound execute integration — two different verbs in one utterance
# ---------------------------------------------------------------------------

class TestCompoundExecute:
    def test_compound_fires_separate_call_services(self) -> None:
        # User utterance hits both turn_on (office) and turn_off
        # (living room). LLM returns SHAPE 2. Each action should
        # produce its own call_service on the fake HA.
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.office_lamp", "Office Lamp",
                       state="off", area="office"),
                _state("light.living_room_lamp", "Living Room Lamp",
                       state="on", area="living_room"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"actions":['
                '{"service":"light.turn_on",'
                '"entity_ids":["light.office_lamp"]},'
                '{"service":"light.turn_off",'
                '"entity_ids":["light.living_room_lamp"]}],'
                '"speech":"Office lit, living room darkened.",'
                '"rationale":"compound"}'
            ),
        )
        r = disambig.run(
            "turn on the office lights and turn off the living room lights",
            source="webui_chat", assume_home_command=True,
        )
        assert r.handled is True
        assert r.decision == "execute"
        assert set(r.entity_ids) == {"light.office_lamp", "light.living_room_lamp"}
        # Both HA calls fired, in order, with the right service.
        assert len(ha.calls) == 2
        assert ha.calls[0]["service"] == "turn_on"
        assert ha.calls[0]["target"] == {"entity_id": ["light.office_lamp"]}
        assert ha.calls[1]["service"] == "turn_off"
        assert ha.calls[1]["target"] == {"entity_id": ["light.living_room_lamp"]}

    def test_compound_many_actions(self) -> None:
        # User could mix three verbs across three areas. All should
        # fire.
        states = [
            _state("light.office", "Office", state="off", area="office"),
            _state("light.bedroom", "Bedroom", state="on", area="bedroom"),
            _state("light.kitchen", "Kitchen", state="off", area="kitchen"),
        ]
        disambig, ha, _ = _make(
            cache_states=states,
            llm_response=(
                '{"decision":"execute","actions":['
                '{"service":"light.turn_on","entity_ids":["light.office"]},'
                '{"service":"light.turn_off","entity_ids":["light.bedroom"]},'
                '{"service":"light.turn_on","entity_ids":["light.kitchen"],'
                ' "service_data":{"brightness_pct":50}}],'
                '"speech":"three things","rationale":"three"}'
            ),
        )
        r = disambig.run(
            "turn on the office lights, turn off the bedroom light, "
            "set the kitchen light to 50",
            source="webui_chat", assume_home_command=True,
        )
        assert r.handled is True
        assert len(ha.calls) == 3
        services = [c["service"] for c in ha.calls]
        assert services == ["turn_on", "turn_off", "turn_on"]
        # Brightness_pct plumbed through on the third action
        assert ha.calls[2]["service_data"] == {"brightness_pct": 50}

    def test_compound_unknown_entity_falls_through(self) -> None:
        # One action references an entity not in the cache → fall-
        # through for the whole turn (refuse to partially act on an
        # uncertain target).
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.office", "Office", state="off"),
            ],
            llm_response=(
                '{"decision":"execute","actions":['
                '{"service":"light.turn_on","entity_ids":["light.office"]},'
                '{"service":"light.turn_off",'
                ' "entity_ids":["light.does_not_exist"]}],'
                '"speech":"ok"}'
            ),
        )
        r = disambig.run(
            "turn on the office lights and turn off ghost lights",
            source="webui_chat", assume_home_command=True,
        )
        assert r.should_fall_through is True
        assert ha.calls == []  # nothing fired — invariant

    def test_actions_without_decision_field_inferred_as_execute(self) -> None:
        # Observed 2026-04-19 on live 14B: the LLM returned
        # {"actions": [...], "speech": "..."} without the
        # `decision` key at all. Defensive inference treats that
        # as execute rather than falling through to Tier 3 where
        # the chat LLM acknowledges but doesn't actually fire.
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.office", "Office", state="off", area="office"),
                _state("light.kitchen", "Kitchen", state="off", area="kitchen"),
            ],
            llm_response=(
                '{"actions":['
                '{"service":"light.turn_on","entity_ids":["light.office"]},'
                '{"service":"light.turn_off","entity_ids":["light.kitchen"]}'
                '],"speech":"both done"}'
            ),
        )
        r = disambig.run(
            "turn on the office lights and turn off the kitchen lights",
            source="webui_chat", assume_home_command=True,
        )
        assert r.handled is True
        assert r.decision == "execute"
        assert len(ha.calls) == 2

    def test_response_data_wrapper_with_action_aliases(self) -> None:
        # This is the exact shape captured from live 14B on 2026-04-19.
        # Before the parser normalization fix, this went unknown_decision
        # and fell through to Tier 3 which then misfired on a button
        # indicator entity. With normalization, both actions execute.
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.front_entryway_flood_01",
                       "Front Entryway Flood 01",
                       state="on", area="front_entryway"),
                _state("light.ceiling_lights",
                       "Kitchen Overhead",
                       state="off", area="kitchen"),
            ],
            llm_response=(
                '{"response":{"type":"json","data":{'
                '"actions":['
                '{"action":"turn_off",'
                '"entity_id":"light.front_entryway_flood_01"},'
                '{"action":"turn_on",'
                '"entity_id":"light.ceiling_lights"}'
                '],"message":"done"}}}'
            ),
        )
        r = disambig.run(
            "turn off the front entryway lights and turn on the kitchen lights",
            source="webui_chat", assume_home_command=True,
        )
        assert r.handled is True
        assert r.decision == "execute"
        assert len(ha.calls) == 2
        assert ha.calls[0]["service"] == "turn_off"
        assert ha.calls[0]["target"] == {
            "entity_id": ["light.front_entryway_flood_01"],
        }
        assert ha.calls[1]["service"] == "turn_on"
        assert ha.calls[1]["target"] == {
            "entity_id": ["light.ceiling_lights"],
        }
        # Speech comes from `message` field via aliasing
        assert r.speech == "done"

    def test_legacy_missing_decision_but_entity_ids_present_executes(self) -> None:
        # Same defense for the legacy SHAPE 1 variant.
        disambig, ha, _ = _make(
            cache_states=[_state("light.kitchen", "Kitchen", state="on")],
            llm_response=(
                '{"entity_ids":["light.kitchen"],"service":"turn_off",'
                '"speech":"off"}'
            ),
        )
        r = disambig.run(
            "turn off the kitchen lights", source="webui_chat",
            assume_home_command=True,
        )
        assert r.handled is True
        assert r.decision == "execute"
        assert len(ha.calls) == 1

    def test_legacy_single_action_still_works(self) -> None:
        # The old schema still produces one action.
        disambig, ha, _ = _make(
            cache_states=[_state("light.kitchen", "Kitchen", state="on")],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen"],'
                '"service":"turn_off","speech":"Off."}'
            ),
        )
        r = disambig.run(
            "turn off the kitchen lights", source="webui_chat",
            assume_home_command=True,
        )
        assert r.handled is True
        assert len(ha.calls) == 1


# ---------------------------------------------------------------------------
# Execute path
# ---------------------------------------------------------------------------

class TestStateVerification:
    """Phase 8.4 — post-execute state verification. Confirm the
    disambiguator:
      - marks state_verified=true on the audit and keeps optimistic
        speech when the expected transition lands.
      - marks state_verified=false AND replaces speech with an
        honest note when strict mode is active and the transition
        does NOT land.
      - respects verification_mode=warn (keep optimistic speech
        but still audit) and verification_mode=silent (no verifier
        at all)."""

    def _strict_rules(self) -> DisambiguationRules:
        r = DisambiguationRules()
        r.verification_mode = "strict"
        # Small timeout so the "no state change" path doesn't make
        # tests sluggish.
        r.verification_timeout_s = 0.2
        return r

    def test_verified_success_keeps_speech(self) -> None:
        """When the state_changed event lands, the optimistic
        speech from Tier 2 is preserved and state_verified=True."""
        states = [_state("light.kitchen", "Kitchen", state="off")]
        ha = _FakeHAClient()
        # Simulate a successful transition right after call_service.
        ha.autoemit_on_call = [
            {"entity_id": "light.kitchen", "state": "on"},
        ]
        cache = EntityCache()
        cache.apply_get_states(states)
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=self._strict_rules(),
        )
        d._call_ollama = MagicMock(return_value=(
            '{"decision":"execute","entity_ids":["light.kitchen"],'
            '"service":"turn_on","speech":"Kitchen, illuminated.",'
            '"rationale":"test"}'
        ))
        r = d.run("turn on the kitchen lights", source="webui_chat")
        assert r.handled and r.decision == "execute"
        # Optimistic speech preserved — the action actually landed.
        assert "Kitchen, illuminated." in r.speech
        assert ha.calls, "call_service should have fired"

    def test_strict_mode_replaces_speech_on_fail(self) -> None:
        """No state_changed observed within timeout → strict mode
        replaces speech with an honest note + flags rationale."""
        states = [_state("light.ghost", "Ghost Lamp", state="off")]
        ha = _FakeHAClient(autoemit=False)  # no transition emitted
        cache = EntityCache()
        cache.apply_get_states(states)
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=self._strict_rules(),
        )
        d._call_ollama = MagicMock(return_value=(
            '{"decision":"execute","entity_ids":["light.ghost"],'
            '"service":"turn_on","speech":"Ghost Lamp, illuminated.",'
            '"rationale":"should not land"}'
        ))
        r = d.run("turn on the ghost lamp", source="webui_chat")
        assert r.handled and r.decision == "execute"
        # Speech was REPLACED with the honest-failure line.
        assert "Ghost Lamp, illuminated." not in r.speech
        assert "did not register" in r.speech or "transition failed" in r.speech
        # Rationale mentions the unverified entity id.
        assert "light.ghost" in r.rationale or "unverified" in r.rationale

    def test_warn_mode_keeps_optimistic_speech_on_fail(self) -> None:
        """verification_mode=warn: the audit still records the
        failure but the user hears the optimistic speech anyway."""
        states = [_state("light.ghost", "Ghost Lamp", state="off")]
        ha = _FakeHAClient(autoemit=False)
        cache = EntityCache()
        cache.apply_get_states(states)
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
        rules = self._strict_rules()
        rules.verification_mode = "warn"
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=rules,
        )
        d._call_ollama = MagicMock(return_value=(
            '{"decision":"execute","entity_ids":["light.ghost"],'
            '"service":"turn_on","speech":"Ghost Lamp, illuminated.",'
            '"rationale":"should not land"}'
        ))
        r = d.run("turn on the ghost lamp", source="webui_chat")
        assert r.handled and r.decision == "execute"
        # Speech NOT replaced — warn mode keeps the optimistic line.
        assert "Ghost Lamp, illuminated." in r.speech

    def test_silent_mode_does_not_verify(self) -> None:
        """verification_mode=silent: no watch created, no wait, no
        honest-failure injection. Pre-Phase-8.4 behaviour."""
        states = [_state("light.ghost", "Ghost Lamp", state="off")]
        ha = _FakeHAClient()
        cache = EntityCache()
        cache.apply_get_states(states)
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
        rules = self._strict_rules()
        rules.verification_mode = "silent"
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=rules,
        )
        d._call_ollama = MagicMock(return_value=(
            '{"decision":"execute","entity_ids":["light.ghost"],'
            '"service":"turn_on","speech":"Ghost Lamp, illuminated.",'
            '"rationale":"no verify"}'
        ))
        r = d.run("turn on the ghost lamp", source="webui_chat")
        # No callback should have been registered.
        assert ha._state_cbs == []
        assert "Ghost Lamp, illuminated." in r.speech

    def test_scene_turn_on_is_skipped_not_failed(self) -> None:
        """Scenes can't be verified on their own entity (the child
        lights move, not the scene itself). StateVerifier returns
        skipped, so the run must NOT trip strict-mode failure."""
        states = [_state("scene.evening", "Evening Scene",
                         state="2026-04-20T02:39:30+00:00")]
        ha = _FakeHAClient()
        cache = EntityCache()
        cache.apply_get_states(states)
        all_matches = [
            CandidateMatch(entity=e, matched_name=e.friendly_name,
                           score=100.0, sensitive=False)
            for e in cache.snapshot()
        ]
        cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
        d = Disambiguator(
            ha_client=ha, cache=cache,
            ollama_url="http://fake", model="glados",
            rules=self._strict_rules(),
        )
        d._call_ollama = MagicMock(return_value=(
            '{"decision":"execute","entity_ids":["scene.evening"],'
            '"service":"turn_on","speech":"Evening scene, engaged.",'
            '"rationale":"scene activation"}'
        ))
        r = d.run("activate the evening scene", source="webui_chat")
        # Strict mode, but scene was skipped — speech preserved.
        assert "Evening scene, engaged." in r.speech


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

    def test_enum_synonym_acknowledge_treated_as_execute(self) -> None:
        """Phase 8.0.2 — qwen3:8b emits 'decision: acknowledge' instead
        of 'execute'. Tolerant mapping must convert it so the action
        still fires rather than falling through to Tier 3."""
        disambig, ha, _ = _make(
            cache_states=[_state("light.office", "Office Light", state="on")],
            llm_response=(
                '{"decision":"acknowledge",'
                '"entity_ids":["light.office"],'
                '"service":"turn_off",'
                '"speech":"Office, extinguished.",'
                '"rationale":"qwen3 enum drift"}'
            ),
        )
        r = disambig.run("turn off the office light", source="webui_chat")
        assert r.handled is True
        assert r.decision == "execute"
        assert r.entity_ids == ["light.office"]
        assert ha.calls and ha.calls[0]["service"] == "turn_off"

    def test_enum_synonym_proceed_maps_to_execute(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[_state("light.kitchen", "Kitchen Light", state="off")],
            llm_response=(
                '{"decision":"proceed",'
                '"entity_ids":["light.kitchen"],'
                '"service":"turn_on",'
                '"speech":"Kitchen engaged.",'
                '"rationale":"enum drift variant"}'
            ),
        )
        r = disambig.run("turn on the kitchen light", source="webui_chat")
        assert r.decision == "execute"
        assert ha.calls

    def test_enum_synonym_ask_maps_to_clarify(self) -> None:
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.bed_a", "Bedroom A"),
                _state("light.bed_b", "Bedroom B"),
            ],
            llm_response=(
                '{"decision":"ask",'
                '"entity_ids":[],'
                '"service":"",'
                '"speech":"Which bedroom light?",'
                '"rationale":"needs specification"}'
            ),
        )
        r = disambig.run("which bedroom light", source="webui_chat")
        assert r.decision == "clarify"
        assert not ha.calls  # clarify never fires HA

    def test_unknown_enum_with_actionable_payload_infers_execute(self) -> None:
        """qwen3:8b has been observed emitting self-inconsistent JSON:
        decision='no_action' while service='light.turn_on' and
        speech='Turning on the lights.' The payload is fully actionable;
        the enum is just off. Trust the structure and execute rather
        than waste Tier 3 on a redundant disambiguation."""
        disambig, ha, _ = _make(
            cache_states=[_state("light.office", "Office Light", state="off")],
            llm_response=(
                '{"decision":"no_action",'
                '"entity_ids":["light.office"],'
                '"service":"turn_on",'
                '"speech":"Turning on the overhead lights.",'
                '"rationale":"qwen3 inconsistent enum"}'
            ),
        )
        r = disambig.run("turn on the office light", source="webui_chat")
        assert r.decision == "execute"
        assert r.entity_ids == ["light.office"]
        assert ha.calls and ha.calls[0]["service"] == "turn_on"

    def test_unknown_enum_without_payload_still_falls_through(self) -> None:
        """When the decision is gibberish AND there's no actionable
        structure (empty entity_ids, empty service), fall through to
        Tier 3. No silent execution on garbage."""
        disambig, ha, _ = _make(
            cache_states=[_state("light.x", "X")],
            llm_response=(
                '{"decision":"zorfblarg",'
                '"entity_ids":[],'
                '"service":"",'
                '"speech":"?","rationale":"gibberish"}'
            ),
        )
        r = disambig.run("turn on x", source="webui_chat")
        assert r.handled is False
        assert r.should_fall_through is True
        assert not ha.calls


# ──────────────────────────────────────────────────────────────
# Phase 8.3.4 — SemanticIndex integration (fallback + preference)
# ──────────────────────────────────────────────────────────────

class _StubSemanticIndex:
    """Drop-in replacement for SemanticIndex that returns a canned
    hit list. Used to verify the disambiguator wiring without
    loading the real BGE-small model."""

    def __init__(self, hits_by_query=None, ready=True, raises=False,
                 area_names=None, floor_names=None):
        from glados.ha.semantic_index import SemanticHit
        self._hits = hits_by_query or {}
        self._ready = ready
        self._raises = raises
        self.SemanticHit = SemanticHit
        self.calls: list[str] = []
        self.call_kwargs: list[dict] = []  # Phase 8.5 — capture area/floor filters
        self._area_names = area_names or {}
        self._floor_names = floor_names or {}

    def is_ready(self) -> bool:
        return self._ready

    def retrieve_for_planner(self, query, *, k=8, domain_filter=None,
                             area_id=None, floor_id=None,
                             segment_tokens=None,
                             ignore_segments=True):  # noqa: ARG002
        self.calls.append(query)
        self.call_kwargs.append({"area_id": area_id, "floor_id": floor_id})
        if self._raises:
            raise RuntimeError("simulated retriever explosion")
        return self._hits.get(query, [])

    # Phase 8.5 — the disambiguator calls these to resolve
    # utterance keywords into registry ids before retrieval.
    def area_names(self) -> dict[str, str]:
        return dict(self._area_names)

    def floor_names(self) -> dict[str, str]:
        return dict(self._floor_names)


class TestSemanticIndexIntegration:
    def test_uses_semantic_index_when_ready(self) -> None:
        """When the SemanticIndex is present and returns hits, the
        disambiguator builds its candidate list from semantic hits
        rather than calling cache.get_candidates."""
        from glados.ha.semantic_index import SemanticHit

        # Cache still must contain the entity the semantic hit
        # refers to — the adapter looks it up for state/attrs.
        cache_states = [
            _state("light.task_lamp_one", "Office Desk Monitor Lamp"),
            _state("light.room_a_strip_seg_1", "Bedroom Strip Seg 1"),
        ]
        semantic = _StubSemanticIndex(hits_by_query={
            "turn on the desk lamp": [
                SemanticHit(
                    entity_id="light.task_lamp_one",
                    score=0.88,
                    document="Office Desk Monitor Lamp",
                    device_id="dev_desk",
                ),
            ],
        })

        cache = EntityCache()
        cache.apply_get_states(cache_states)
        disambig = Disambiguator(
            ha_client=_FakeHAClient(), cache=cache,
            ollama_url="http://fake", model="glados",
            rules=DisambiguationRules(),
            semantic_index=semantic,
        )
        disambig._call_ollama = MagicMock(return_value=(
            '{"decision":"execute",'
            '"entity_ids":["light.task_lamp_one"],'
            '"service":"turn_on","speech":"ok","rationale":"semantic"}'
        ))

        captured: list[list[dict]] = []
        orig = disambig._call_ollama
        def _capture(msgs):
            captured.append(list(msgs))
            return orig(msgs)
        disambig._call_ollama = _capture  # type: ignore[method-assign]

        r = disambig.run("turn on the desk lamp", source="webui_chat")
        assert r.decision == "execute"
        assert r.entity_ids == ["light.task_lamp_one"]
        assert semantic.calls == ["turn on the desk lamp"]
        # Candidate list sent to the LLM includes the semantic hit.
        user_msg = next(
            m["content"] for m in captured[0] if m["role"] == "user"
        )
        assert "office_desk_monitor_lamp" in user_msg

    def test_falls_back_to_fuzzy_when_index_not_ready(self) -> None:
        """Background index build hasn't finished → disambiguator
        must use the fuzzy cache path and still resolve the turn."""
        semantic = _StubSemanticIndex(ready=False)
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.kitchen_ceiling", "Kitchen Ceiling",
                       state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen_ceiling"],'
                '"service":"turn_off","speech":"ok","rationale":"fuzzy"}'
            ),
        )
        disambig._semantic_index = semantic
        r = disambig.run("turn off the kitchen lights", source="webui_chat")
        assert r.decision == "execute"
        # Not ready → semantic retrieve never invoked.
        assert semantic.calls == []
        assert ha.calls

    def test_falls_back_when_semantic_returns_no_hits(self) -> None:
        """Index is ready but BGE produced no usable hits — legit
        fallback, fuzzy must still produce candidates."""
        semantic = _StubSemanticIndex(hits_by_query={})
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.kitchen_ceiling", "Kitchen Ceiling",
                       state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen_ceiling"],'
                '"service":"turn_off","speech":"ok","rationale":"fallback"}'
            ),
        )
        disambig._semantic_index = semantic
        r = disambig.run("turn off the kitchen lights", source="webui_chat")
        # Semantic was tried and returned empty → fuzzy fallback.
        assert semantic.calls == ["turn off the kitchen lights"]
        assert r.decision == "execute"

    def test_semantic_exception_falls_back_gracefully(self) -> None:
        """If the retriever itself raises, the disambiguator logs
        and degrades to fuzzy — never crashes the request."""
        semantic = _StubSemanticIndex(raises=True)
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.x", "X", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.x"],'
                '"service":"turn_off","speech":"ok","rationale":"fallback"}'
            ),
        )
        disambig._semantic_index = semantic
        r = disambig.run("turn off the x lights", source="webui_chat")
        assert r.handled is True
        assert r.decision == "execute"

    def test_semantic_hits_drop_missing_cache_entries(self) -> None:
        """If the semantic index references an entity that's been
        removed from the live cache between build and retrieve, the
        adapter silently skips it (HA cache wins on freshness).
        When the only hit is missing, we fall back to fuzzy."""
        from glados.ha.semantic_index import SemanticHit
        semantic = _StubSemanticIndex(hits_by_query={
            "desk lamp": [
                SemanticHit(
                    entity_id="light.ghost_removed",
                    score=0.9, document="Ghost Removed",
                    device_id="dev_ghost",
                ),
            ],
        })
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.kitchen_ceiling", "Kitchen Ceiling",
                       state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen_ceiling"],'
                '"service":"turn_off","speech":"ok","rationale":"fb"}'
            ),
        )
        disambig._semantic_index = semantic
        r = disambig.run("desk lamp", source="webui_chat")
        # Semantic was tried, the single hit was dropped (not in
        # cache), then fuzzy ran.
        assert semantic.calls == ["desk lamp"]
        assert r.decision == "execute"

    def test_phase_85_floor_keyword_forwards_floor_id_to_retriever(self) -> None:
        """Phase 8.5 — when the utterance says "downstairs", the
        disambiguator resolves it via the area_inference module
        against the stub index's registry and passes floor_id to
        retrieve_for_planner."""
        from glados.ha.semantic_index import SemanticHit

        cache_states = [
            _state("light.kitchen_ceiling", "Kitchen Ceiling"),
        ]
        semantic = _StubSemanticIndex(
            hits_by_query={"turn off the downstairs lights": [
                SemanticHit(
                    entity_id="light.kitchen_ceiling",
                    score=0.9, document="doc",
                    area_id="kitchen", floor_id="floor_ground",
                ),
            ]},
            # "downstairs" resolves via hint 'ground' → 'Ground Level'.
            floor_names={"floor_ground": "Ground Level",
                         "floor_upper":  "Upper Floor"},
        )
        disambig, _ha, _cache = _make(
            cache_states=cache_states,
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.kitchen_ceiling"],'
                '"service":"turn_off","speech":"ok","rationale":"fb"}'
            ),
        )
        disambig._semantic_index = semantic
        disambig.run(
            "turn off the downstairs lights", source="webui_chat",
        )
        # Single retrieve_for_planner call with floor_id hint.
        assert len(semantic.call_kwargs) == 1
        assert semantic.call_kwargs[0]["floor_id"] == "floor_ground"

    def test_phase_85_no_keyword_sends_no_filter(self) -> None:
        """Utterances without an area/floor keyword must NOT pass
        filters — otherwise plain 'turn on the lights' would get
        silently scoped and regress candidate recall."""
        from glados.ha.semantic_index import SemanticHit

        cache_states = [
            _state("light.office_desk", "Office Desk"),
        ]
        semantic = _StubSemanticIndex(
            hits_by_query={"turn on the desk lamp": [
                SemanticHit(entity_id="light.office_desk",
                            score=0.9, document="d"),
            ]},
            floor_names={"floor_main": "Main Floor"},
        )
        disambig, _ha, _cache = _make(
            cache_states=cache_states,
            llm_response=(
                '{"decision":"execute","entity_ids":["light.office_desk"],'
                '"service":"turn_on","speech":"ok","rationale":"r"}'
            ),
        )
        disambig._semantic_index = semantic
        disambig.run("turn on the desk lamp", source="webui_chat")
        assert semantic.call_kwargs[0]["floor_id"] is None
        assert semantic.call_kwargs[0]["area_id"] is None


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

    def test_assume_home_command_bypasses_precheck(self) -> None:
        """P0 2026-04-19: 'Increase the brightness by ten percent' as a
        follow-up after a desk-lamp action has no device keyword; the
        disambiguator's internal precheck used to reject it before the
        LLM ever saw the candidates. Carry-over callers pass
        assume_home_command=True to bypass that gate."""
        disambig, ha, _ = _make(
            cache_states=[
                _state("light.task_lamp_one",
                       "Office Desk Monitor Lamp", state="on",
                       extra_attrs={"brightness": 76}),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.task_lamp_one"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":40},'
                '"speech":"Brighter.","rationale":"follow-up"}'
            ),
        )
        # Without the bypass, this utterance would fall through at
        # no_home_command_intent.
        r = disambig.run(
            "increase the brightness by ten percent",
            source="webui_chat",
            assume_home_command=True,
            prior_entity_ids=["light.task_lamp_one"],
            prior_service="light.turn_on",
        )
        assert r.decision == "execute"
        assert r.entity_ids == ["light.task_lamp_one"]

    def test_prior_entities_injected_when_fuzzy_misses(self) -> None:
        """A follow-up like 'a bit more' has no fuzzy match against
        any entity name; the prior entity carries through as a synthetic
        candidate so Tier 2 can still act."""
        disambig, ha, cache = _make(
            cache_states=[
                _state("light.task_lamp_one",
                       "Office Desk Monitor Lamp", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.task_lamp_one"],'
                '"service":"turn_on",'
                '"service_data":{"brightness_pct":85},'
                '"speech":"Brighter.","rationale":"follow-up"}'
            ),
            # Force the fuzzy lookup to return nothing so we exercise
            # the injection path specifically.
            force_candidates=False,
        )
        # Defensive: make sure our test setup actually has fuzzy
        # returning zero for this utterance.
        assert cache.get_candidates("a bit more", domain_filter=["light"]) == []
        r = disambig.run(
            "a bit more",
            source="webui_chat",
            assume_home_command=True,
            prior_entity_ids=["light.task_lamp_one"],
            prior_service="light.turn_on",
        )
        assert r.decision == "execute"
        assert r.entity_ids == ["light.task_lamp_one"]
        assert r.service_data == {"brightness_pct": 85}

    def test_prior_entities_deduped_with_fuzzy_hits(self) -> None:
        """If fuzzy also surfaces the prior entity, it should appear
        once (with the prior-entity synthetic rank) rather than twice."""
        from glados.ha.entity_cache import CandidateMatch

        disambig, ha, cache = _make(
            cache_states=[
                _state("light.task_lamp_one",
                       "Office Desk Monitor Lamp", state="on"),
                _state("light.arc", "Living Room Arc Lamp", state="on"),
            ],
            llm_response=(
                '{"decision":"execute",'
                '"entity_ids":["light.task_lamp_one"],'
                '"service":"turn_on",'
                '"speech":"Done.","rationale":"x"}'
            ),
        )
        msgs_captured: list[list[dict]] = []

        def _capture(messages):  # type: ignore[override]
            msgs_captured.append(messages)
            return (
                '{"decision":"execute",'
                '"entity_ids":["light.task_lamp_one"],'
                '"service":"turn_on",'
                '"speech":"Done.","rationale":"x"}'
            )

        disambig._call_ollama = _capture  # type: ignore[method-assign]
        disambig.run(
            "desk lamp brighter",
            source="webui_chat",
            assume_home_command=True,
            prior_entity_ids=["light.task_lamp_one"],
            prior_service="light.turn_on",
        )
        # The prompt should contain each candidate id at most once.
        user_prompt = msgs_captured[0][-1]["content"]
        assert user_prompt.count("id=light.task_lamp_one") == 1

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
