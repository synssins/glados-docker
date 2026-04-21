"""Phase 8.8 — positive anaphora follow-up detector.

``glados.intent.anaphora.is_anaphoric_followup`` decides whether a
short utterance is a continuation of the most-recent Tier 1/2 turn
(inherit target) or a new self-contained command (run retrieval).

The pre-8.8 heuristic lived in ``CommandResolver._looks_anaphoric``
and was "no distinctive qualifier words = anaphoric." It fired on
``"brighter"`` but silently missed every operator-reported failure
case: ``"turn it up more"`` (``more`` escaped the stopword list),
``"do that again"`` (``again`` escaped it), ``"keep going"`` (two
non-stopword tokens). These tests encode the operator-observed
positive cases and the negative cases from the Phase 8.3
regression ("bedroom strip segment 3" must NOT inherit prior
turn's desk lamp).
"""
from __future__ import annotations

import pytest

from glados.intent.anaphora import is_anaphoric_followup


POSITIVE_CASES = [
    # Bare intensity / direction adverbs.
    "brighter",
    "dimmer",
    "a bit brighter",
    "louder",
    "softer",
    "warmer",
    "cooler",
    "higher",
    "lower",
    "off",
    "on",
    "up",
    "down",
    # Pronoun deictic follow-ups.
    "turn it up",
    "turn it up more",
    "turn it off",
    "turn them off",
    "dim them",
    "dim those",
    "activate that",
    "do that again",
    "the same one",
    # Explicit repetition / continuation.
    "again",
    "more",
    "a bit more",
    "keep going",
    "continue",
    "do the same thing",
    # Additive extension onto a newly-named area.
    "and the kitchen too",
    "also the office",
    "the bedroom as well",
    # Filler-padded intensity.
    "please a bit brighter",
    "just a little dimmer",
]


NEGATIVE_CASES = [
    "",
    "   ",
    "turn on the kitchen lights",
    "set the desk lamp to fifty percent",
    "play some music",
    "what is the weather",
    "hello",
    "how are you",
    "tell me about Wheatley",
    "bedroom strip segment 3",
    "activate the evening scene",
    "lock the front door",
    "is the garage closed",
    "what time is it",
    "who turned on the lights",
]


@pytest.mark.parametrize("utterance", POSITIVE_CASES)
def test_positive_anaphora_fires(utterance: str) -> None:
    assert is_anaphoric_followup(utterance) is True, utterance


@pytest.mark.parametrize("utterance", NEGATIVE_CASES)
def test_negative_anaphora_skipped(utterance: str) -> None:
    assert is_anaphoric_followup(utterance) is False, utterance


class TestSpecificCases:
    """Operator-observed bugs that motivated the positive detector."""

    def test_turn_it_up_more_fires(self) -> None:
        """The P0 regression: ``more`` was not in the old stopword
        list, so ``_extract_qualifiers`` returned ``["more"]`` and
        ``_looks_anaphoric`` said False → fall through to chitchat →
        GLaDOS hallucinates a confirmation without dimming anything.
        Positive detector reads the pronoun ``it`` and fires."""
        assert is_anaphoric_followup("Turn it up more") is True

    def test_do_that_again_fires(self) -> None:
        """``again`` also escaped the old stopword list. Explicit
        repetition marker in the new detector catches it."""
        assert is_anaphoric_followup("Do that again") is True

    def test_segment_3_does_not_fire(self) -> None:
        """Phase 8.3 regression: ``bedroom strip segment 3`` is a
        new target, not a refinement. Inheriting the prior turn's
        entity would read ``segment 3`` as a brightness modifier
        on the wrong entity. The positive detector must reject it
        because no pronoun, no repetition marker, and there IS a
        content word (``segment``, ``bedroom``, ``strip``)."""
        assert is_anaphoric_followup("bedroom strip segment 3") is False

    def test_scene_name_does_not_fire(self) -> None:
        """Scene activations name their target. ``"evening"`` is a
        content word."""
        assert is_anaphoric_followup("activate the evening scene") is False

    def test_case_insensitive(self) -> None:
        assert is_anaphoric_followup("BRIGHTER") is True
        assert is_anaphoric_followup("Do That Again") is True

    def test_punctuation_tolerated(self) -> None:
        assert is_anaphoric_followup("Brighter!") is True
        assert is_anaphoric_followup("turn it up, more.") is True
        assert is_anaphoric_followup("again?") is True


class TestAdditiveContinuationSizeGuard:
    """``also`` / ``too`` / ``as well`` fire only on short utterances.
    A long sentence that happens to contain ``too`` is not a carry-
    over — it's a new statement."""

    def test_short_additive_fires(self) -> None:
        assert is_anaphoric_followup("also the office") is True
        assert is_anaphoric_followup("and the kitchen too") is True

    def test_long_additive_does_not_fire(self) -> None:
        # Seven tokens and no pronoun / repetition marker — should fall
        # back to negative.
        assert is_anaphoric_followup(
            "also please activate the kitchen evening scene",
        ) is False
