"""Phase 8.14 — canon keyword gate.

Confirms ``needs_canon_context`` fires on Portal-specific trigger
terms, stays silent on ordinary household / chitchat turns, and
respects word-boundary matching on short keywords (so ``moonlight``
doesn't trigger ``moon rock``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from glados.core.context_gates import configure, needs_canon_context, reload


@pytest.fixture(autouse=True)
def _default_gates(tmp_path: Path) -> Iterator[None]:
    """Point the module at a config file that doesn't exist so every
    test starts from the hardcoded defaults. Tests that want YAML
    extras override this by writing a file + reconfiguring."""
    configure(tmp_path / "nope.yaml")
    reload()
    yield
    configure(tmp_path / "still-nope.yaml")
    reload()


# Default-trigger positives — every one of these must fire.
POSITIVE_CASES = [
    "How did you cope with being a potato?",
    "Tell me about Wheatley.",
    "Who is Caroline?",
    "What happened to Cave Johnson?",
    "Remember the combustible lemon speech?",
    "Explain the Aperture Science Enrichment Center.",
    "Is the turret opera real?",
    "What is a Weighted Companion Cube?",
    "Describe the Aerial Faith Plates.",
    "What does propulsion gel do?",
    "Where does moon rock come from in the story?",
    "Tell me about Old Aperture.",
    "What is the Space Core?",
    "Did GLaDOS kill everyone with neurotoxin?",
    "What ending did Chell get?",
    "Sing me the Cara Mia aria.",
]

# Chitchat / household turns — gate should NOT fire.
NEGATIVE_CASES = [
    "Turn off the kitchen lights.",
    "What's the weather like?",
    "Set the living room to warm white.",
    "My cat is sleeping.",
    "Dim the bedroom to forty percent.",
    "Is the front door locked?",
    "The moonlight is nice tonight.",      # word-boundary: "moon" alone isn't a trigger
    "I'd like to read in the den.",
    "Play some music.",
    "It's too bright in here.",
]


@pytest.mark.parametrize("utterance", POSITIVE_CASES)
def test_positive_triggers_fire(utterance: str) -> None:
    assert needs_canon_context(utterance) is True, utterance


@pytest.mark.parametrize("utterance", NEGATIVE_CASES)
def test_negative_cases_do_not_fire(utterance: str) -> None:
    assert needs_canon_context(utterance) is False, utterance


def test_empty_message_returns_false() -> None:
    assert needs_canon_context("") is False
    assert needs_canon_context("   ") is False


def test_case_insensitive() -> None:
    assert needs_canon_context("WHEATLEY is a core.") is True
    assert needs_canon_context("wheatley is a core.") is True
    assert needs_canon_context("Wheatley Is A Core.") is True


def test_yaml_extras_augment_defaults(tmp_path: Path) -> None:
    """Operator-added trigger keywords under ``canon.trigger_keywords``
    in ``context_gates.yaml`` are OR-ed with the hardcoded defaults."""
    cfg = tmp_path / "context_gates.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "canon": {"trigger_keywords": ["rattmann", "sabotage"]},
        }),
        encoding="utf-8",
    )
    configure(cfg)
    reload()
    try:
        # New extras fire.
        assert needs_canon_context("Who is Rattmann?") is True
        assert needs_canon_context("Describe the sabotage scene.") is True
        # Defaults still fire.
        assert needs_canon_context("Tell me about the potato.") is True
        # Still-neutral prompts don't.
        assert needs_canon_context("Turn off the lights.") is False
    finally:
        configure(tmp_path / "cleared.yaml")
        reload()


def test_word_boundary_on_short_default_keywords() -> None:
    """Short default triggers (``chell``, ``potato``) must not match
    inside unrelated words — that's what the word-boundary flag
    guards. Long multi-word triggers match by substring which is
    fine because they're unambiguous in normal English."""
    # "Chellbuilt" is a made-up word; shouldn't trigger.
    assert needs_canon_context("The chellbuilt car is red.") is False
    # "Potato" is word-boundary-guarded; "potatoes" (adds suffix) won't match.
    assert needs_canon_context("I bought potatoes.") is False
    # Exact word still fires.
    assert needs_canon_context("I'm a potato.") is True
