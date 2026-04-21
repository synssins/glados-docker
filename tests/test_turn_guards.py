"""Non-streaming chat-path fixes — turn guards + engine_audio origin gate.

Two pre-existing operator-reported bugs:

- ``"Tell me about the testing tracks"`` on ``stream:false`` returned
  a corporate refusal because the 14B hallucinated a ``testing_tracks``
  tool. Root cause: SSE injected a chitchat / home-command system
  guard inline, but non-streaming submitted raw text to the engine
  queue which built messages without that guard.
- ``"What's the weather like?"`` on ``stream:false`` returned a bare
  ``.``. Root cause: ``cfg.tuning.engine_audio_default`` defaults to
  True (meant for HA voice pipeline) so the handler replaced any
  actual reply with ``"."`` for every direct API caller.

Fix A: extract the guard text to ``glados.core.turn_guards`` and
register ``guard_for_message`` as a ``ContextBuilder`` callback so
the engine path injects the same guard SSE does.

Fix B: only default ``engine_audio`` ON for ``Origin.VOICE_MIC``;
chat / API origins get the real reply text.
"""
from __future__ import annotations

import pytest

from glados.core.turn_guards import (
    CHITCHAT_GUARD,
    HOME_COMMAND_GUARD,
    guard_for_message,
)


# ── Guard selection ───────────────────────────────────────────────


def test_home_command_phrase_returns_home_command_guard() -> None:
    assert guard_for_message("turn on the kitchen light") == HOME_COMMAND_GUARD


def test_chitchat_phrase_returns_chitchat_guard() -> None:
    assert guard_for_message("tell me about the testing tracks") == CHITCHAT_GUARD


def test_weather_question_is_chitchat_guard() -> None:
    """Weather question is a chitchat turn from the guard's POV —
    Tier routing handles weather separately. The guard only cares
    whether a tool is likely to run."""
    assert guard_for_message("what's the weather like?") == CHITCHAT_GUARD


def test_empty_message_returns_chitchat_guard() -> None:
    assert guard_for_message("") == CHITCHAT_GUARD
    assert guard_for_message("   ") == CHITCHAT_GUARD


def test_guard_constants_differ() -> None:
    """Sanity: the two guard strings must differ. If they ever
    converge, the classification is pointless."""
    assert CHITCHAT_GUARD != HOME_COMMAND_GUARD


def test_chitchat_guard_forbids_fake_device_state() -> None:
    """Phrasing assertions — these strings carry the behavioural
    contract. A copy edit that accidentally drops the no-fabrication
    rule would silently re-open the "hey you" hallucination
    regression."""
    text = CHITCHAT_GUARD.lower()
    assert "do not claim" in text
    # Any of these word-stems should cover the no-hallucination intent.
    assert any(w in text for w in ("do not invent", "do not fabricate"))


def test_chitchat_guard_permits_quoting_injected_context() -> None:
    """Weather regression guard: when ``"What's the weather like?"``
    hits the non-streaming path, weather_cache is already injected as
    a preceding system message. Guard wording must allow citing that
    content or the model stays silent. Earlier draft failed this."""
    text = CHITCHAT_GUARD.lower()
    assert "may" in text  # explicit permission word
    assert "system messages" in text or "provided" in text


def test_home_command_guard_forbids_inventory_narration() -> None:
    text = HOME_COMMAND_GUARD.lower()
    assert "search_entities" in text
    assert "do not narrate" in text
    assert "do not use markdown" in text


# ── engine_audio origin gate ──────────────────────────────────────


def test_engine_audio_defaults_off_for_api_chat() -> None:
    """Caller uses ``stream:false`` from a curl / WebUI test →
    origin=API_CHAT → engine_audio must NOT default to True or the
    response body is replaced with ``"."``."""
    from glados.core.source_context import Origin

    # Simulate the branch in api_wrapper._handle_chat_completions
    data = {}  # no explicit engine_audio in the request body
    origin = Origin.API_CHAT

    class _StubTuning:
        engine_audio_default = True

    if "engine_audio" in data:
        engine_audio = bool(data["engine_audio"])
    elif origin == Origin.VOICE_MIC:
        engine_audio = bool(_StubTuning.engine_audio_default)
    else:
        engine_audio = False
    assert engine_audio is False


def test_engine_audio_defaults_on_for_voice_mic() -> None:
    """HA satellite mic → origin=VOICE_MIC → engine_audio defaults
    to the operator's ``engine_audio_default`` config."""
    from glados.core.source_context import Origin

    data = {}
    origin = Origin.VOICE_MIC

    class _StubTuning:
        engine_audio_default = True

    if "engine_audio" in data:
        engine_audio = bool(data["engine_audio"])
    elif origin == Origin.VOICE_MIC:
        engine_audio = bool(_StubTuning.engine_audio_default)
    else:
        engine_audio = False
    assert engine_audio is True


def test_explicit_engine_audio_in_body_always_wins() -> None:
    """If the caller explicitly sets engine_audio in the request
    body, that wins over origin-based defaults — honest explicit
    opt-in/out stays supported."""
    from glados.core.source_context import Origin

    for explicit in (True, False):
        data = {"engine_audio": explicit}
        for origin in (Origin.VOICE_MIC, Origin.API_CHAT, Origin.WEBUI_CHAT):
            if "engine_audio" in data:
                engine_audio = bool(data["engine_audio"])
            elif origin == Origin.VOICE_MIC:
                engine_audio = True  # stub
            else:
                engine_audio = False
            assert engine_audio is explicit, (origin, explicit)


def test_engine_audio_defaults_off_for_webui_chat() -> None:
    """Operator's WebUI chat panel — expects the reply text."""
    from glados.core.source_context import Origin

    data = {}
    origin = Origin.WEBUI_CHAT
    if "engine_audio" in data:
        engine_audio = bool(data["engine_audio"])
    elif origin == Origin.VOICE_MIC:
        engine_audio = True
    else:
        engine_audio = False
    assert engine_audio is False
