"""Tests for ``glados.core.context_gates.needs_time_context``.

Mirrors the weather and canon gate test idioms — defaults-only path
(no YAML present) since that's the production state operators hit on
fresh installs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from glados.core.context_gates import (
    _TIME_DEFAULT_TRIGGERS,
    configure,
    needs_time_context,
)


@pytest.fixture(autouse=True)
def _reset_config(tmp_path: Path) -> None:
    missing = tmp_path / "context_gates_missing.yaml"
    configure(missing)
    yield
    configure(missing)


# ── Direct triggers fire ────────────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "what time is it",
    "What time is it?",                 # capitalisation
    "What's the time?",
    "do you know what time it is",
    "tell me the current time",
    "what hour is it",
    "what o'clock is it",
    "look at the clock",
    "what's the current date",
    "what's today's date",
    "what day is it",
    "what date is it today",
    "what year is it",
])
def test_time_triggers_fire(phrase: str) -> None:
    assert needs_time_context(phrase) is True, phrase


# ── Incidental "time" / "day" / "date" mentions must NOT fire ──────


@pytest.mark.parametrize("phrase", [
    "I'm having a great time",
    "all the time",
    "lifetime",
    "by the time you read this",
    "any time",
    "good day",
    "all day",
    "have a nice day",
    "make my day",
    "out of date",
    "the date format is wrong",
    "clockwork orange",                 # word-boundary on "clock"
    "deadlock detected",                # ditto
    "turn on the kitchen light",
    "play some music",
    "what's the weather like",
    "",
])
def test_incidental_mentions_do_not_fire(phrase: str) -> None:
    assert needs_time_context(phrase) is False, phrase


# ── Default trigger list contents ──────────────────────────────────


def test_defaults_include_core_phrasings() -> None:
    """Lock the most operator-common phrasings against accidental
    deletion. Same regression-pin pattern as the weather gate."""
    texts = {trig.text for trig in _TIME_DEFAULT_TRIGGERS}
    assert "what time" in texts
    assert "time is it" in texts
    assert "what day" in texts


def test_clock_is_word_boundary_only() -> None:
    """``clock`` must require word-boundary match — substring would
    fire on ``clockwork`` / ``deadlock`` / ``Sherlock`` etc."""
    clock = next(t for t in _TIME_DEFAULT_TRIGGERS if t.text == "clock")
    assert clock.needs_word_boundary is True
