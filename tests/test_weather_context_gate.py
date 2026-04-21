"""Bug B regression test — weather gate default triggers.

``"What's the weather like?"`` on ``stream:false`` returned an empty
reply because `needs_weather_context` had no default triggers and the
``configs/context_gates.yaml`` file doesn't exist in fresh installs.
Weather context was never injected into the LLM prompt, so the model
produced nothing useful with the guard telling it not to fabricate.

Fix: ship hardcoded default trigger / ambiguous / indoor-override
lists in-code, merge with optional YAML extras. Same pattern the
canon gate already uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from glados.core.context_gates import (
    _WEATHER_DEFAULT_AMBIGUOUS,
    _WEATHER_DEFAULT_INDOOR_OVERRIDE,
    _WEATHER_DEFAULT_TRIGGERS,
    configure,
    needs_weather_context,
)


@pytest.fixture(autouse=True)
def _reset_config(tmp_path: Path) -> None:
    """Every test starts with a missing YAML so we're testing the
    defaults-only path — matching the production state operators hit
    on fresh installs."""
    missing = tmp_path / "context_gates_missing.yaml"
    configure(missing)
    yield
    configure(missing)


# ── Direct triggers (positive) ─────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "What's the weather like?",
    "what is the weather",
    "tell me the weather",
    "how's the weather",
    "is it raining outside?",
    "is it snowing",
    "what's the forecast for today",
    "is it humid out",
    "is it sunny",
    "is it cloudy",
    "looks overcast",
    "it's windy",
    "a storm is coming",
    "just a drizzle",
    "what's the temperature outside?",
])
def test_direct_triggers_fire(phrase: str) -> None:
    assert needs_weather_context(phrase) is True, phrase


# ── Ambiguous triggers + indoor-override interaction ──────────────


@pytest.mark.parametrize("phrase", [
    "how hot is it",                 # ambiguous "hot", no indoor word
    "is it cold out",                # "cold" + "out"
    "is it warm outside",            # "warm" + "outside"
    "it's freezing",
])
def test_ambiguous_without_indoor_fires(phrase: str) -> None:
    assert needs_weather_context(phrase) is True, phrase


@pytest.mark.parametrize("phrase", [
    "it's cold in here, turn up the heater",   # "cold" + "heater"
    "it's hot in the living room",              # "hot" + "living room"
    "the kitchen is warm",                      # "warm" + "kitchen"
    "turn on the fan, it's hot",                # "hot" + "fan"
    "adjust the thermostat, too cold",          # "cold" + "thermostat"
])
def test_ambiguous_with_indoor_does_not_fire(phrase: str) -> None:
    assert needs_weather_context(phrase) is False, phrase


# ── Non-weather must not fire ─────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "hello",
    "turn on the kitchen light",
    "what time is it",
    "play some music",
    "tell me about the testing tracks",
    "",
])
def test_unrelated_does_not_fire(phrase: str) -> None:
    assert needs_weather_context(phrase) is False, phrase


# ── Sanity on the default lists themselves ────────────────────────


def test_defaults_contain_operator_reported_phrase_components() -> None:
    """Lock: a stripped copy edit that accidentally drops "weather"
    or "forecast" from the default trigger list would silently
    reopen the bug. Pin the critical members."""
    assert "weather" in _WEATHER_DEFAULT_TRIGGERS
    assert "forecast" in _WEATHER_DEFAULT_TRIGGERS
    assert "temperature" in _WEATHER_DEFAULT_TRIGGERS


def test_defaults_treat_hot_cold_as_ambiguous() -> None:
    """``"hot"`` / ``"cold"`` must NOT be in the direct-trigger list —
    otherwise ``"it's cold in here, turn up the heater"`` would
    injection-spam weather context onto an HVAC command."""
    assert "hot" not in _WEATHER_DEFAULT_TRIGGERS
    assert "cold" not in _WEATHER_DEFAULT_TRIGGERS
    assert "hot" in _WEATHER_DEFAULT_AMBIGUOUS
    assert "cold" in _WEATHER_DEFAULT_AMBIGUOUS


def test_indoor_override_covers_common_rooms() -> None:
    for room in ("living room", "kitchen", "bedroom", "thermostat", "heater"):
        assert room in _WEATHER_DEFAULT_INDOOR_OVERRIDE, room
