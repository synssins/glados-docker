"""Unit tests for the chat-shape gate that decides whether the
Tier 1 + Tier 2 home-command resolver runs on a given chat utterance.

The gate is a thin wrapper over ``looks_like_home_command`` exposed
as ``_should_run_command_resolver``. Its job: when an utterance
carries no home-command signal, skip the resolver entirely and let
the chat path fall straight through to Tier 3. This elides the
~5 s wall-clock cost of Tier 1's HA conversation round-trip and
Tier 2's disambiguator LLM call — both of which were going to miss
anyway for chat-shaped utterances.

Strictly subtractive: True (any signal at all) → behavior unchanged
from before the gate; False → skip resolver, fall through to chat.
"""

from glados.core.api_wrapper import _should_run_command_resolver


# ── chat-flavored utterances → gate returns False (skip resolver) ─────


def test_tell_me_about_request_skips_resolver():
    assert _should_run_command_resolver(
        "Briefly tell me three things you know about cats."
    ) is False


def test_explanation_request_skips_resolver():
    assert _should_run_command_resolver(
        "Explain how solar panels work."
    ) is False


def test_what_do_you_think_skips_resolver():
    assert _should_run_command_resolver(
        "What do you think about the meaning of life?"
    ) is False


def test_pure_chitchat_skips_resolver():
    assert _should_run_command_resolver(
        "Hi there, how are you doing today?"
    ) is False


# ── home-command utterances → gate returns True (run resolver) ────────


def test_explicit_light_command_runs_resolver():
    assert _should_run_command_resolver(
        "Turn off the kitchen lights."
    ) is True


def test_dim_verb_alone_runs_resolver():
    # Phase 8.2 expansion — verb alone should pass even without a noun.
    assert _should_run_command_resolver("Dim the bedroom.") is True


def test_ambient_state_phrase_runs_resolver():
    # "It's too dark" — ambient-state pattern signals likely home command.
    assert _should_run_command_resolver(
        "It's too dark in here."
    ) is True


def test_state_query_runs_resolver_for_internal_short_circuit():
    # State queries DO pass the outer gate ("lights" is a domain
    # keyword); the resolver's internal `_is_state_query` short-circuit
    # then handles them. That sequencing must be preserved — if we
    # gated state queries OUT here, the resolver wouldn't get a
    # chance to do its own short-circuit, and audit would lose the
    # state-query rationale entry.
    assert _should_run_command_resolver(
        "What lights are on?"
    ) is True


# ── degenerate inputs ─────────────────────────────────────────────────


def test_empty_string_does_not_run_resolver():
    assert _should_run_command_resolver("") is False


def test_whitespace_only_does_not_run_resolver():
    assert _should_run_command_resolver("   \t\n  ") is False
