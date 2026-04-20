"""Tests for glados.core.command_resolver.

The resolver is pure orchestration — it has no HA or LLM of its own.
Every test wires in fakes for the bridge, disambiguator, rewriter, and
entity cache, then asserts the resolver's flow-control and bookkeeping
behavior. The real bridge/disambiguator/cache have their own tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from glados.core.command_resolver import (
    CommandResolver,
    HAStateValidator,
    ResolvedAction,
    ResolverResult,
)
from glados.core.learned_context import LearnedContextStore
from glados.core.session_memory import SessionMemory
from glados.core.source_context import SourceContext
from glados.core.user_preferences import UserPreferences
from glados.observability.audit import Origin


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeConversationResult:
    handled: bool
    speech: str = ""
    response_type: str = ""
    should_disambiguate: bool = False
    should_fall_through: bool = False


@dataclass
class _FakeDisambigResult:
    handled: bool
    should_fall_through: bool = False
    speech: str = ""
    decision: str = ""
    entity_ids: list[str] = field(default_factory=list)
    service: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    latency_ms: int = 0


class _FakeBridge:
    def __init__(self, result: _FakeConversationResult | Exception | None = None) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def process(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str | None = None,
        timeout_s: float = 5.0,
    ) -> _FakeConversationResult:
        self.calls.append({"text": text, "timeout_s": timeout_s})
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is None:
            return _FakeConversationResult(handled=False, should_fall_through=True)
        return self._result


class _FakeDisambiguator:
    def __init__(self, result: _FakeDisambigResult | Exception | None = None) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        utterance: str,
        source: str,
        source_area: str | None = None,
        assume_home_command: bool = False,
        prior_entity_ids: list[str] | None = None,
        prior_service: str | None = None,
    ) -> _FakeDisambigResult:
        self.calls.append({
            "utterance": utterance,
            "source": source,
            "source_area": source_area,
            "assume_home_command": assume_home_command,
            "prior_entity_ids": list(prior_entity_ids or []),
            "prior_service": prior_service,
        })
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is None:
            return _FakeDisambigResult(handled=False, should_fall_through=True)
        return self._result


class _FakeRewriter:
    def __init__(self, func=None) -> None:
        self._func = func or (lambda s, hint=None: f"[glados] {s}")
        self.calls: list[tuple[str, str | None]] = []

    def rewrite(self, plain_text: str, context_hint: str | None = None) -> str:
        self.calls.append((plain_text, context_hint))
        return self._func(plain_text, context_hint)


@dataclass
class _FakeEntity:
    entity_id: str
    state: str
    area_id: str | None
    domain: str


class _FakeCache:
    def __init__(self, entities: list[_FakeEntity] | None = None) -> None:
        self._entities = {e.entity_id: e for e in (entities or [])}

    def snapshot(self) -> list[_FakeEntity]:
        return list(self._entities.values())

    def get(self, entity_id: str) -> _FakeEntity | None:
        return self._entities.get(entity_id)


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ctx_chat_no_area() -> SourceContext:
    return SourceContext.from_headers({
        "X-GLaDOS-Origin": "webui_chat",
        "X-GLaDOS-Session-Id": "sess-A",
    })


@pytest.fixture
def ctx_voice_living_room() -> SourceContext:
    return SourceContext.from_headers({
        "X-GLaDOS-Origin": "voice_mic",
        "X-GLaDOS-Session-Id": "sat-liv",
        "X-GLaDOS-Area-Id": "living_room",
    })


@pytest.fixture
def learned(tmp_path: Path):
    store = LearnedContextStore(tmp_path / "learned.db")
    yield store
    store.close()


def _make_resolver(
    *,
    bridge: _FakeBridge | None = None,
    disambiguator: _FakeDisambiguator | None = None,
    rewriter: _FakeRewriter | None = None,
    session_memory: SessionMemory | None = None,
    learned_context: LearnedContextStore,
    entities: list[_FakeEntity] | None = None,
    clock: _Clock | None = None,
    carryover_window_s: float = 120.0,
) -> tuple[CommandResolver, _FakeBridge, _FakeDisambiguator, _FakeCache]:
    bridge = bridge or _FakeBridge()
    disambiguator = disambiguator or _FakeDisambiguator()
    rewriter = rewriter  # may be None
    session = session_memory or SessionMemory(
        idle_ttl_seconds=600.0,
        now_fn=clock,
    )
    cache = _FakeCache(entities or [])
    validator = HAStateValidator(cache)
    resolver = CommandResolver(
        bridge=bridge,
        disambiguator=disambiguator,
        rewriter=rewriter,
        session_memory=session,
        learned_context=learned_context,
        preferences=UserPreferences(),
        state_validator=validator,
        carryover_window_s=carryover_window_s,
        now_fn=clock,
    )
    return resolver, bridge, disambiguator, cache


# ---------------------------------------------------------------------------
# Tests — empty and degenerate inputs
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_empty_utterance_short_circuits(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        resolver, bridge, disambig, _ = _make_resolver(learned_context=learned)
        result = resolver.resolve("   ", ctx_chat_no_area)
        assert result.handled is True
        assert result.should_fall_through is False
        assert result.spoken_response is None
        assert bridge.calls == []
        assert disambig.calls == []


class TestStateQueryShortCircuit:
    """State-query openers ("what", "is", "are", "who", "why", etc.)
    should fall through to Tier 3 without burning a Tier 1 or Tier 2
    call. Tier 2's action-shaped prompt misclassifies these as
    ambiguity; Tier 3 has the MCP tools to actually answer."""

    @pytest.mark.parametrize("utterance", [
        "What lights are on?",
        "what's in the kitchen",
        "whats the state of the office",
        "Are the lights on?",
        "Is the office light on?",
        "Was the front door unlocked?",
        "Were the garage lights on last night?",
        "Who turned on the lights",
        "Why is the kitchen so dark",
        "Why is it so bright",
        "When did the bedroom lamp turn on",
        "Where are the kitchen lights",
    ])
    def test_state_query_falls_through(
        self, utterance: str, ctx_chat_no_area: SourceContext,
        learned: LearnedContextStore,
    ) -> None:
        resolver, bridge, disambig, _ = _make_resolver(learned_context=learned)
        result = resolver.resolve(utterance, ctx_chat_no_area)
        assert result.handled is False
        assert result.should_fall_through is True
        assert result.rationale == "state_query"
        # Neither Tier 1 nor Tier 2 called — that's the point.
        assert bridge.calls == []
        assert disambig.calls == []

    def test_non_query_utterance_not_short_circuited(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # Declarative utterances must still reach Tier 1/2 even if
        # they contain question-like words mid-string.
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on",
            entity_ids=["light.office_lamp"],
            speech="Office lamps on.",
        ))
        resolver, bridge, d_seen, _ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        result = resolver.resolve("turn on the office lights", ctx_chat_no_area)
        assert result.handled is True
        assert result.tier == "2"
        assert d_seen.calls  # disambiguator was consulted

    def test_imperative_command_starting_with_similar_word(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # "What" at the start is a query. But "Whatever" or "Whatcha"
        # would not be (word-boundary guard). Also verify that
        # commands containing query words in the middle still run.
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on",
            entity_ids=["light.office_lamp"],
            speech="Done.",
        ))
        resolver, bridge, d_seen, _ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        # Declarative with "is" mid-string
        result = resolver.resolve(
            "The office is too dark, turn on the lights", ctx_chat_no_area,
        )
        assert result.handled is True
        assert d_seen.calls


# ---------------------------------------------------------------------------
# Tests — Tier 1 fast path
# ---------------------------------------------------------------------------

class TestTier1Path:
    def test_tier1_hit_returns_persona_rewritten_reply(
        self, ctx_voice_living_room: SourceContext, learned: LearnedContextStore,
    ) -> None:
        bridge = _FakeBridge(_FakeConversationResult(
            handled=True, speech="Turned off the kitchen light.",
            response_type="action_done",
        ))
        resolver, _, disambig, _ = _make_resolver(
            bridge=bridge,
            rewriter=_FakeRewriter(lambda s, hint=None: "Kitchen light, terminated."),
            learned_context=learned,
        )
        result = resolver.resolve("turn off the kitchen light", ctx_voice_living_room)
        assert result.handled is True
        assert result.tier == "1"
        assert result.spoken_response == "Kitchen light, terminated."
        assert result.should_fall_through is False
        # Tier 1 wins => Tier 2 never called
        assert disambig.calls == []

    def test_tier1_hit_without_rewriter_returns_plain(
        self, ctx_voice_living_room: SourceContext, learned: LearnedContextStore,
    ) -> None:
        bridge = _FakeBridge(_FakeConversationResult(
            handled=True, speech="Turned off the light.",
        ))
        resolver, *_ = _make_resolver(bridge=bridge, learned_context=learned)
        result = resolver.resolve("turn off", ctx_voice_living_room)
        assert result.spoken_response == "Turned off the light."

    def test_tier1_rewriter_failure_preserves_reply(
        self, ctx_voice_living_room: SourceContext, learned: LearnedContextStore,
    ) -> None:
        def _boom(s: str, hint: str | None = None) -> str:
            raise RuntimeError("ollama down")
        bridge = _FakeBridge(_FakeConversationResult(
            handled=True, speech="Done.",
        ))
        resolver, *_ = _make_resolver(
            bridge=bridge, rewriter=_FakeRewriter(_boom), learned_context=learned,
        )
        result = resolver.resolve("turn off", ctx_voice_living_room)
        # Persona rewrite failure must never block the reply.
        assert result.handled is True
        assert result.spoken_response == "Done."

    def test_tier1_bridge_exception_falls_to_tier2(
        self, ctx_voice_living_room: SourceContext, learned: LearnedContextStore,
    ) -> None:
        bridge = _FakeBridge(RuntimeError("ws disconnected"))
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_off", entity_ids=["light.kitchen"],
            speech="Done.",
        ))
        resolver, *_ = _make_resolver(
            bridge=bridge, disambiguator=disambig, learned_context=learned,
        )
        result = resolver.resolve("turn off the kitchen light", ctx_voice_living_room)
        assert result.handled is True
        assert result.tier == "2"


# ---------------------------------------------------------------------------
# Tests — Tier 2 path
# ---------------------------------------------------------------------------

class TestTier2Path:
    def test_tier2_hit_records_action(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            service_data={"brightness_pct": 60},
            speech="Office lamps on.",
        ))
        session = SessionMemory()
        resolver, *_ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned,
        )
        result = resolver.resolve("turn on the office lights", ctx_chat_no_area)
        assert result.handled is True
        assert result.tier == "2"
        assert result.spoken_response == "Office lamps on."
        assert result.action is not None
        assert result.action.service == "light.turn_on"
        assert result.action.entity_ids == ("light.office_lamp",)
        assert result.action.service_data == {"brightness_pct": 60}
        # Session memory has the turn for follow-ups
        last = session.last_turn(ctx_chat_no_area.session_id)
        assert last is not None
        assert last.entities_affected == ("light.office_lamp",)

    def test_tier2_clarify_does_not_reinforce_learned(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="clarify",
            speech="Which room?",
        ))
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        result = resolver.resolve("turn on the lights", ctx_chat_no_area)
        assert result.handled is True
        assert result.needs_clarification is True
        assert result.spoken_response == "Which room?"
        # Clarify is not a success — nothing to reinforce.
        assert learned.count() == 0

    def test_tier2_fall_through_returns_fall_through(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=False, should_fall_through=True,
            decision="fall_through",
        ))
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        result = resolver.resolve("tell me a joke", ctx_chat_no_area)
        assert result.handled is False
        assert result.should_fall_through is True

    def test_tier2_exception_falls_through(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        disambig = _FakeDisambiguator(RuntimeError("ollama 504"))
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        result = resolver.resolve("turn on the lights", ctx_chat_no_area)
        assert result.should_fall_through is True


# ---------------------------------------------------------------------------
# Tests — Session carry-over
# ---------------------------------------------------------------------------

class TestSessionCarryover:
    def test_recent_turn_feeds_prior_entities_to_tier2(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        clock = _Clock()
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            service_data={"brightness_pct": 80},
            speech="Brightened.",
        ))
        session = SessionMemory(now_fn=clock)
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned, clock=clock,
        )
        # Turn 1
        resolver.resolve("turn on the office lights", ctx_chat_no_area)
        # Turn 2 — follow-up within the window
        clock.advance(30.0)
        resolver.resolve("brighter", ctx_chat_no_area)

        last_call = d_seen.calls[-1]
        assert last_call["prior_entity_ids"] == ["light.office_lamp"]
        assert last_call["prior_service"] == "light.turn_on"
        # With carry-over, precheck is bypassed
        assert last_call["assume_home_command"] is True

    def test_stale_turn_does_not_carry_over(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        clock = _Clock()
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            speech="Office lamps on.",
        ))
        session = SessionMemory(now_fn=clock)
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned, clock=clock,
            carryover_window_s=120.0,
        )
        resolver.resolve("turn on the office lights", ctx_chat_no_area)
        # Advance past the carry-over window (but well inside the
        # session TTL so session memory is still alive).
        clock.advance(200.0)
        resolver.resolve("brighter", ctx_chat_no_area)

        last_call = d_seen.calls[-1]
        assert last_call["prior_entity_ids"] == []
        assert last_call["prior_service"] is None
        assert last_call["assume_home_command"] is False

    def test_qualifier_in_current_utterance_skips_carryover(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        """Phase 8.3 fix: if the second turn names its own target
        (e.g. 'bedroom strip segment 3' after 'turn on the desk
        lamp'), carry-over must NOT attach the prior desk lamp as
        prior context. Otherwise the disambiguator reads 'segment 3'
        as a brightness qualifier on the wrong entity. Regression
        test for the Gate 3 live failure."""
        clock = _Clock()
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.task_lamp_one"],
            speech="Office lamp on.",
        ))
        session = SessionMemory(now_fn=clock)
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned, clock=clock,
        )
        # Turn 1 — commits a desk-lamp target to session memory.
        resolver.resolve("turn on the desk lamp", ctx_chat_no_area)
        # Turn 2 — fresh target, has its own qualifiers.
        clock.advance(5.0)
        resolver.resolve("bedroom strip segment 3", ctx_chat_no_area)

        last_call = d_seen.calls[-1]
        # The second turn must be handled on its own merits:
        # no prior entity injection, no assume_home_command bypass.
        assert last_call["prior_entity_ids"] == []
        assert last_call["prior_service"] is None
        assert last_call["assume_home_command"] is False

    def test_true_anaphora_still_uses_carryover(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        """Guard against over-correcting the Gate 3 fix: single-word
        refinements with no qualifier tokens must still attach the
        prior entities. This is the 'increase the brightness by ten
        percent' / 'brighter' case that originally motivated the
        carry-over code path."""
        clock = _Clock()
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.task_lamp_one"],
            speech="Brighter.",
        ))
        session = SessionMemory(now_fn=clock)
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned, clock=clock,
        )
        resolver.resolve("turn on the desk lamp", ctx_chat_no_area)
        clock.advance(5.0)
        # Pure modifier — no distinctive qualifier, no device noun.
        resolver.resolve("brighter", ctx_chat_no_area)

        last_call = d_seen.calls[-1]
        assert last_call["prior_entity_ids"] == [
            "light.task_lamp_one"
        ]
        assert last_call["prior_service"] == "light.turn_on"
        assert last_call["assume_home_command"] is True

    def test_query_response_does_not_set_carryover(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # A Tier 2 query ("what lights are on?") that doesn't execute
        # anything shouldn't become carry-over context for the next
        # turn — we'd end up answering "brighter" against the
        # last-queried entity list.
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            # No entity_ids / service => resolver must NOT record turn
            speech="Nothing on.",
        ))
        session = SessionMemory()
        resolver, *_ = _make_resolver(
            disambiguator=disambig, session_memory=session,
            learned_context=learned,
        )
        resolver.resolve("what lights are on?", ctx_chat_no_area)
        assert session.last_turn(ctx_chat_no_area.session_id) is None


# ---------------------------------------------------------------------------
# Tests — HA state validator
# ---------------------------------------------------------------------------

class TestHAStateValidator:
    def test_turn_off_requires_something_on(self) -> None:
        cache = _FakeCache([
            _FakeEntity("light.office_lamp", "off", "office", "light"),
        ])
        v = HAStateValidator(cache)
        assert v.validates(service="light.turn_off", resolved_area_id="office") is False

    def test_turn_off_accepted_when_light_on(self) -> None:
        cache = _FakeCache([
            _FakeEntity("light.office_lamp", "on", "office", "light"),
        ])
        v = HAStateValidator(cache)
        assert v.validates(service="light.turn_off", resolved_area_id="office") is True

    def test_turn_off_scoped_by_area(self) -> None:
        # A lamp is on in the LIVING ROOM but not the office — so a
        # learned "turn off" guess targeting the office must be rejected.
        cache = _FakeCache([
            _FakeEntity("light.office_lamp", "off", "office", "light"),
            _FakeEntity("light.living_lamp", "on", "living_room", "light"),
        ])
        v = HAStateValidator(cache)
        assert v.validates(service="light.turn_off", resolved_area_id="office") is False

    def test_turn_on_always_plausible(self) -> None:
        cache = _FakeCache([])
        v = HAStateValidator(cache)
        assert v.validates(service="light.turn_on", resolved_area_id="any") is True

    def test_scene_turn_on_requires_scene_exists(self) -> None:
        cache = _FakeCache([
            _FakeEntity("scene.reading_office", "scening", "office", "scene"),
        ])
        v = HAStateValidator(cache)
        assert v.validates(
            service="scene.turn_on", resolved_area_id="office",
            entity_hint="scene.reading_office",
        ) is True
        assert v.validates(
            service="scene.turn_on", resolved_area_id="office",
            entity_hint="scene.gone_forever",
        ) is False

    def test_empty_service_is_accepted(self) -> None:
        v = HAStateValidator(_FakeCache([]))
        assert v.validates(service="", resolved_area_id="office") is True


# ---------------------------------------------------------------------------
# Tests — Learned-context guess
# ---------------------------------------------------------------------------

class TestLearnedContextGuess:
    def test_learned_row_seeds_prior_when_no_carryover(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # Teach the store: "brighter" in chat → office / light.turn_on
        learned.record_success(
            utterance="brighter",
            source_channel="chat",
            source_area_id=None,
            resolved_area_id="office",
            resolved_verb="light.turn_on",
        )
        # HA state has an office lamp currently ON — validation passes
        # (turn_on always passes, but we test the full pipeline).
        entities = [
            _FakeEntity("light.office_lamp", "on", "office", "light"),
        ]
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            service_data={"brightness_step_pct": 25},
            speech="Brightened.",
        ))
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        result = resolver.resolve("brighter", ctx_chat_no_area)
        assert result.handled is True
        # The learned guess set the prior service
        last_call = d_seen.calls[-1]
        assert last_call["prior_service"] == "light.turn_on"
        assert last_call["assume_home_command"] is True

    def test_learned_row_skipped_when_ha_state_rejects(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # Teach: "kill the lights" → office / turn_off.
        learned.record_success(
            utterance="kill the lights",
            source_channel="chat",
            source_area_id=None,
            resolved_area_id="office",
            resolved_verb="light.turn_off",
        )
        # HA state: office lights are all OFF. Turn-off guess must be
        # invalidated.
        entities = [
            _FakeEntity("light.office_lamp", "off", "office", "light"),
        ]
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=False, should_fall_through=True,
        ))
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        resolver.resolve("kill the lights", ctx_chat_no_area)
        last_call = d_seen.calls[-1]
        # Prior context NOT injected — the stale guess was rejected.
        assert last_call["prior_service"] is None
        assert last_call["assume_home_command"] is False

    def test_learned_lookup_suppressed_when_area_on_request(
        self, ctx_voice_living_room: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # A voice request with area context should NOT consult the
        # "no source area" learned rows.
        learned.record_success(
            utterance="brighter",
            source_channel="voice",
            source_area_id=None,
            resolved_area_id="office",
            resolved_verb="light.turn_on",
        )
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.liv_lamp"],
            speech="Brightened.",
        ))
        resolver, _, d_seen, _ = _make_resolver(
            disambiguator=disambig, learned_context=learned,
        )
        resolver.resolve("brighter", ctx_voice_living_room)
        last_call = d_seen.calls[-1]
        # No carry-over injected because the request had area_id set,
        # so the learned-context bypass was skipped.
        assert last_call["prior_service"] is None
        assert last_call["assume_home_command"] is False

    def test_successful_tier2_reinforces_learned(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # Fresh store — no learned rows.
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            speech="Done.",
        ))
        entities = [_FakeEntity("light.office_lamp", "off", "office", "light")]
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        resolver.resolve("turn on the office lights", ctx_chat_no_area)
        rows = learned.lookup(
            utterance="turn on the office lights",
            source_channel="chat",
            source_area_id=None,
        )
        assert len(rows) == 1
        assert rows[0].resolved_area_id == "office"
        assert rows[0].resolved_verb == "light.turn_on"
        assert rows[0].reinforcement == 1

    def test_reinforcement_increments_on_repeat(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on", entity_ids=["light.office_lamp"],
            speech="Done.",
        ))
        entities = [_FakeEntity("light.office_lamp", "off", "office", "light")]
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        for _ in range(3):
            resolver.resolve("turn on the office lights", ctx_chat_no_area)
        rows = learned.lookup(
            utterance="turn on the office lights",
            source_channel="chat",
            source_area_id=None,
        )
        assert rows[0].reinforcement == 3

    def test_tier2_fall_through_penalizes_consulted_row(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # Seed a row with reinforcement=1 so one failure deletes it.
        learned.record_success(
            utterance="brighter",
            source_channel="chat",
            source_area_id=None,
            resolved_area_id="office",
            resolved_verb="light.turn_on",
        )
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=False, should_fall_through=True,
        ))
        entities = [_FakeEntity("light.office_lamp", "on", "office", "light")]
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        result = resolver.resolve("brighter", ctx_chat_no_area)
        assert result.should_fall_through is True
        # Consulted row was penalized; reinforcement hit 0 → deleted.
        assert learned.count() == 0


# ---------------------------------------------------------------------------
# Tests — construction validation
# ---------------------------------------------------------------------------

class TestConstructionGuards:
    def test_rejects_zero_carryover_window(
        self, learned: LearnedContextStore,
    ) -> None:
        with pytest.raises(ValueError):
            _make_resolver(learned_context=learned, carryover_window_s=0)

    def test_resolved_area_inferred_from_entity_area(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        # When source_context has no area but Tier 2 resolves to
        # entities in a single HA area, that area is recorded and
        # learned.
        entities = [
            _FakeEntity("light.office_lamp", "off", "office", "light"),
            _FakeEntity("light.office_desk", "off", "office", "light"),
        ]
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on",
            entity_ids=["light.office_lamp", "light.office_desk"],
            speech="Office lamps on.",
        ))
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        result = resolver.resolve("turn on the office lights", ctx_chat_no_area)
        assert result.action is not None
        assert result.action.resolved_area_id == "office"
        # Learned row captured the area too
        rows = learned.lookup(
            utterance="turn on the office lights",
            source_channel="chat",
            source_area_id=None,
        )
        assert len(rows) == 1
        assert rows[0].resolved_area_id == "office"

    def test_resolved_area_none_when_entities_span_areas(
        self, ctx_chat_no_area: SourceContext, learned: LearnedContextStore,
    ) -> None:
        entities = [
            _FakeEntity("light.office_lamp", "off", "office", "light"),
            _FakeEntity("light.living_lamp", "off", "living_room", "light"),
        ]
        disambig = _FakeDisambiguator(_FakeDisambigResult(
            handled=True, decision="execute",
            service="light.turn_on",
            entity_ids=["light.office_lamp", "light.living_lamp"],
            speech="All lights on.",
        ))
        resolver, *_ = _make_resolver(
            disambiguator=disambig, learned_context=learned, entities=entities,
        )
        result = resolver.resolve("turn on all the lights", ctx_chat_no_area)
        assert result.action is not None
        assert result.action.resolved_area_id is None
        # No learned row because we can't pin an area
        assert learned.count() == 0
