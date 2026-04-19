"""Live-Ollama CSV harness — runs a representative subset of the
disambiguation corpus through the REAL Tier 2 Disambiguator + real
PersonaRewriter, with a fake HAClient (captures service calls without
actually hitting HA) and an EntityCache pre-populated with fixture
entities.

This test probes LLM judgment on the resolver's prompt shape and
candidate format. It does NOT test HA execution.

Skipped unless `GLADOS_LIVE_OLLAMA_URL` is set. Runs slowly — each
case takes one LLM round-trip (5–25 s for qwen2.5:14b on CPU /
single GPU). A 20-case subset takes ~3–8 minutes.

Usage:

  GLADOS_LIVE_OLLAMA_URL=http://192.168.1.75:11436 \\
    pytest tests/test_csv_harness_live.py -s -v

  # Include persona rewrite (adds another call per Tier 1 hit,
  # but our fake bridge never hits Tier 1, so no-op here):
  GLADOS_LIVE_REWRITER=1 pytest tests/test_csv_harness_live.py -s

  # Override the disambiguator model:
  GLADOS_LIVE_DISAMBIG_MODEL=qwen2.5:14b-instruct-q4_K_M pytest ...
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from glados.core.command_resolver import (
    CommandResolver,
    HAStateValidator,
)
from glados.core.learned_context import LearnedContextStore
from glados.core.session_memory import SessionMemory
from glados.core.source_context import SourceContext
from glados.core.user_preferences import UserPreferences
from glados.ha.entity_cache import CandidateMatch, EntityCache
from glados.intent.disambiguator import Disambiguator
from glados.intent.rules import DisambiguationRules, IntentAllowlist


LIVE_OLLAMA_URL = os.environ.get("GLADOS_LIVE_OLLAMA_URL", "").strip()
DISAMBIG_MODEL = os.environ.get(
    "GLADOS_LIVE_DISAMBIG_MODEL", "qwen2.5:14b-instruct-q4_K_M",
)

pytestmark = pytest.mark.skipif(
    not LIVE_OLLAMA_URL,
    reason=(
        "Live Ollama tests require GLADOS_LIVE_OLLAMA_URL "
        "(e.g. http://192.168.1.75:11436)."
    ),
)


def _ollama_reachable(url: str, timeout_s: float = 5.0) -> bool:
    """Quick reachability probe. Fails the test session cleanly if
    the declared Ollama URL is down."""
    try:
        req = Request(url.rstrip("/") + "/api/tags")
        with urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except (URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# Fake HA pieces — same shape as tests/test_disambiguator.py
# ---------------------------------------------------------------------------

class _FakeHAClient:
    """Captures call_service invocations without network I/O."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_service(
        self, domain, service, target=None,
        service_data=None, timeout_s=None,
    ):
        self.calls.append({
            "domain": domain, "service": service,
            "target": target, "service_data": service_data,
        })
        return {"success": True, "result": {"context": {"id": "ctx-fake"}}}


def _state(eid: str, name: str, area_id: str, state: str = "off",
           domain: str = "light") -> dict:
    """Entity-state record shape matching HA's `get_states` payload."""
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {
            "friendly_name": name,
            "area_id": area_id,
        },
    }


def _build_cache() -> EntityCache:
    """Small fixture house — covers the areas the selected CSV rows
    reference. Entity state is chosen so at least one light is on
    in each room (so 'turn off' has something to act on)."""
    cache = EntityCache()
    states = [
        # Office
        _state("light.office_lamp", "Office Lamp", "office", state="on"),
        _state("light.office_overhead", "Office Overhead", "office"),
        _state("light.office_desk", "Office Desk", "office", state="on"),
        # Living room
        _state("light.living_room_lamp", "Living Room Lamp", "living_room", state="on"),
        _state("light.living_room_overhead", "Living Room Overhead", "living_room"),
        _state("light.reading_lamp", "Reading Lamp", "living_room", state="on"),
        # Bedroom
        _state("light.bedroom_lamp", "Bedroom Lamp", "bedroom", state="on"),
        _state("light.bedroom_overhead", "Bedroom Overhead", "bedroom"),
        # Kitchen
        _state("light.kitchen_overhead", "Kitchen Overhead", "kitchen", state="on"),
        _state("light.kitchen_under_cabinet", "Kitchen Under Cabinet", "kitchen"),
        # Hallway
        _state("light.hallway", "Hallway Light", "hallway", state="on"),
    ]
    cache.apply_get_states(states)
    # Force-expose all entities as candidates — the fuzzy matcher is
    # tested separately. Here we want the LLM to see the candidate
    # list and make the choice.
    all_matches = [
        CandidateMatch(
            entity=e, matched_name=e.friendly_name or e.entity_id,
            score=100.0, sensitive=False,
        )
        for e in cache.snapshot()
    ]
    cache.get_candidates = lambda *a, **kw: all_matches  # type: ignore[method-assign]
    return cache


@dataclass
class _FakeBridgeResult:
    handled: bool = False
    should_disambiguate: bool = True
    should_fall_through: bool = False
    speech: str = ""
    response_type: str = ""
    conversation_id: str | None = None
    entity_ids: list[str] = field(default_factory=list)


class _FakeBridge:
    """Tier 1 always misses — force every case through Tier 2 so
    we're probing LLM judgment, not HA's intent matcher."""

    def process(self, text, conversation_id=None, language=None, timeout_s=5.0):
        return _FakeBridgeResult()


# ---------------------------------------------------------------------------
# Representative subset of CSV cases
# ---------------------------------------------------------------------------

# Hand-picked rows covering the MVP categories. We deliberately keep
# this small — LLM calls are the long pole.
@dataclass
class LiveCase:
    command: str
    expected_category: str      # EXECUTE / ASK / FALL_THROUGH / NO_ACTION
    note: str


LIVE_CASES: list[LiveCase] = [
    # Core executes
    LiveCase("Turn on the office lights", "EXECUTE",
             "explicit area → execute in office"),
    LiveCase("Turn off the office lights", "EXECUTE",
             "explicit area + off"),
    LiveCase("Turn on all the lights", "EXECUTE",
             "universal quantifier"),
    LiveCase("Turn off all the lights", "EXECUTE",
             "universal quantifier + off"),
    # Caller-area dependent
    LiveCase("Turn on the lights", "EXECUTE",
             "voice: caller area = living_room, chat: should ask"),
    LiveCase("Turn off the lights", "EXECUTE",
             "voice: caller area off; chat: all off (safe)"),
    # Explicit-area variant
    LiveCase("Turn on the bedroom lights", "EXECUTE",
             "explicit bedroom area"),
    # Brightness-relative (needs prior turn in real usage)
    LiveCase("Brighter", "EXECUTE",
             "follow-up — may clarify without carry-over"),
    # Singular entity
    LiveCase("Turn on the reading lamp", "EXECUTE",
             "singular entity match by name"),
    # Clarify stubs
    LiveCase("Lights", "ASK",
             "bare noun → ask on/off"),
    LiveCase("Turn on", "ASK",
             "bare verb → ask what"),
    # Fall-through
    LiveCase("Tell me a joke", "FALL_THROUGH",
             "pure chitchat — Tier 3 territory"),
    LiveCase("", "NO_ACTION",
             "empty utterance — resolver short-circuit"),
    LiveCase("Hey GLaDOS", "NO_ACTION",
             "wake word only"),
    # State query
    LiveCase("Are the lights on?", "FALL_THROUGH",
             "query → Tier 3 (resolver declines)"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ollama_ready() -> str:
    if not _ollama_reachable(LIVE_OLLAMA_URL):
        pytest.fail(
            f"GLADOS_LIVE_OLLAMA_URL={LIVE_OLLAMA_URL} not reachable."
        )
    return LIVE_OLLAMA_URL


@pytest.fixture(scope="module")
def resolver_and_log(tmp_path_factory, ollama_ready):
    """Build the resolver once per module so multiple tests reuse
    the same Disambiguator (Ollama keep_alive preserves the loaded
    model across calls — first call pays the load cost, subsequent
    calls are faster)."""
    tmp = tmp_path_factory.mktemp("live_harness")
    cache = _build_cache()
    ha_client = _FakeHAClient()
    disambig = Disambiguator(
        ha_client=ha_client, cache=cache,
        ollama_url=ollama_ready, model=DISAMBIG_MODEL,
        rules=DisambiguationRules(),
        allowlist=IntentAllowlist(),
    )
    session = SessionMemory()
    learned = LearnedContextStore(tmp / "learned.db")
    validator = HAStateValidator(cache)
    resolver = CommandResolver(
        bridge=_FakeBridge(),
        disambiguator=disambig,
        rewriter=None,  # rewriter only fires on Tier 1; fake bridge never hits Tier 1
        session_memory=session,
        learned_context=learned,
        preferences=UserPreferences(),
        state_validator=validator,
    )
    return resolver, ha_client


# ---------------------------------------------------------------------------
# Test — per-case live probe
# ---------------------------------------------------------------------------

@dataclass
class LiveOutcome:
    case: LiveCase
    voice_category: str
    voice_speech: str
    voice_ha_calls: list[dict]
    voice_latency_ms: int
    chat_category: str
    chat_speech: str
    chat_ha_calls: list[dict]
    chat_latency_ms: int


def _observed_category(result) -> str:
    # Empty-utterance short-circuit: resolver returns handled=True
    # with no spoken_response and rationale="empty_utterance".
    # That's NO_ACTION, not a query.
    if (
        result.handled
        and not result.spoken_response
        and not result.needs_clarification
        and (result.rationale or "").startswith("empty_utterance")
    ):
        return "NO_ACTION"
    if not result.handled and not result.should_fall_through:
        return "NO_ACTION"
    if result.needs_clarification:
        return "ASK"
    if result.should_fall_through:
        return "FALL_THROUGH"
    if result.handled and result.action:
        return "EXECUTE"
    if result.handled:
        return "QUERY"
    return "UNKNOWN"


def _run_one(resolver, ha_client, case: LiveCase, *, voice: bool):
    if voice:
        ctx = SourceContext.from_headers({
            "X-GLaDOS-Origin": "voice_mic",
            "X-GLaDOS-Session-Id": f"live-voice-{case.command[:20]}",
            "X-GLaDOS-Area-Id": "living_room",
        })
    else:
        ctx = SourceContext.from_headers({
            "X-GLaDOS-Origin": "webui_chat",
            "X-GLaDOS-Session-Id": f"live-chat-{case.command[:20]}",
        })
    pre_call_count = len(ha_client.calls)
    t0 = time.perf_counter()
    result = resolver.resolve(case.command, ctx)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    new_calls = ha_client.calls[pre_call_count:]
    return (
        _observed_category(result),
        (result.spoken_response or "").strip()[:160],
        new_calls,
        elapsed_ms,
    )


def test_live_ollama_subset(resolver_and_log, capsys):
    """Run each representative case through the live stack, voice
    and chat variants. Report everything. Soft-assert: print the
    pass/fail summary but don't fail the test — the point is
    visibility, and LLM non-determinism means the "right" answer
    on any single run is judgment-dependent."""
    resolver, ha_client = resolver_and_log

    outcomes: list[LiveOutcome] = []
    for case in LIVE_CASES:
        v_cat, v_speech, v_calls, v_ms = _run_one(
            resolver, ha_client, case, voice=True,
        )
        c_cat, c_speech, c_calls, c_ms = _run_one(
            resolver, ha_client, case, voice=False,
        )
        outcomes.append(LiveOutcome(
            case=case,
            voice_category=v_cat, voice_speech=v_speech,
            voice_ha_calls=v_calls, voice_latency_ms=v_ms,
            chat_category=c_cat, chat_speech=c_speech,
            chat_ha_calls=c_calls, chat_latency_ms=c_ms,
        ))

    report = _render_live_report(outcomes)
    # Write the report to disk first so we still have it if stdout
    # encoding explodes (Windows cp1252 vs Unicode).
    log_path = Path(__file__).resolve().parent / "live_harness_report.txt"
    log_path.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nReport also written to: {log_path}")

    # Generous pass criterion: EITHER voice or chat variant must
    # match the expected category (mirrors the offline harness).
    # We set a soft floor of 60% — LLMs can be inconsistent and the
    # point of this test is visibility, not gating CI.
    passed = sum(
        1 for o in outcomes
        if _matches(o.case.expected_category, o.voice_category)
        or _matches(o.case.expected_category, o.chat_category)
    )
    rate = passed / len(outcomes)
    assert rate >= 0.60, (
        f"Live Ollama pass rate {rate:.1%} below 60% floor. "
        f"See printed report."
    )


def _matches(expected: str, observed: str) -> bool:
    if expected == observed:
        return True
    if expected == "NO_ACTION" and observed in {"FALL_THROUGH", "NO_ACTION"}:
        return True
    if expected == "FALL_THROUGH" and observed in {"FALL_THROUGH", "ASK"}:
        return True
    # Ambiguous-chat asymmetry: ASK expected, EXECUTE observed in
    # the voice variant is a valid resolution.
    if expected == "ASK" and observed == "EXECUTE":
        return True
    return False


def _render_live_report(outcomes: list[LiveOutcome]) -> str:
    lines: list[str] = []
    lines.append(f"=== Live Ollama harness ({len(outcomes)} cases) ===")
    lines.append(f"Ollama URL:  {LIVE_OLLAMA_URL}")
    lines.append(f"Model:       {DISAMBIG_MODEL}")
    lines.append("")
    header = (
        f"{'#':>2}  {'command':<34}  "
        f"{'expect':<12}  {'voice':<12} {'chat':<12}  "
        f"{'v ms':>6} {'c ms':>6}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    passed_voice = 0
    passed_chat = 0
    for i, o in enumerate(outcomes):
        v_pass = _matches(o.case.expected_category, o.voice_category)
        c_pass = _matches(o.case.expected_category, o.chat_category)
        vok = "OK  " if v_pass else "FAIL"
        cok = "OK  " if c_pass else "FAIL"
        passed_voice += 1 if v_pass else 0
        passed_chat += 1 if c_pass else 0
        lines.append(
            f"{i:>2}  {o.case.command[:34]:<34}  "
            f"{o.case.expected_category:<12}  "
            f"{vok} {o.voice_category:<10} "
            f"{cok} {o.chat_category:<10}  "
            f"{o.voice_latency_ms:>6} {o.chat_latency_ms:>6}"
        )
    lines.append("")
    lines.append(
        f"Voice variant: {passed_voice}/{len(outcomes)} "
        f"({100.0 * passed_voice / len(outcomes):.0f}%)"
    )
    lines.append(
        f"Chat variant:  {passed_chat}/{len(outcomes)} "
        f"({100.0 * passed_chat / len(outcomes):.0f}%)"
    )
    lines.append("")
    lines.append("Per-case detail:")
    for i, o in enumerate(outcomes):
        lines.append(f"  [{i}] {o.case.command!r}  ({o.case.note})")
        lines.append(f"       VOICE: cat={o.voice_category:<12} "
                     f"speech={o.voice_speech!r}")
        if o.voice_ha_calls:
            for call in o.voice_ha_calls:
                lines.append(
                    f"              call_service: "
                    f"{call['domain']}.{call['service']}  "
                    f"target={call.get('target')}  data={call.get('service_data')}"
                )
        lines.append(f"       CHAT:  cat={o.chat_category:<12} "
                     f"speech={o.chat_speech!r}")
        if o.chat_ha_calls:
            for call in o.chat_ha_calls:
                lines.append(
                    f"              call_service: "
                    f"{call['domain']}.{call['service']}  "
                    f"target={call.get('target')}  data={call.get('service_data')}"
                )
    return "\n".join(lines)
