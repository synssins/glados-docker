"""
Anaphora / follow-up detection for carry-over turns (Phase 8.8).

A follow-up utterance refers back to the most recent Tier 1/2 action
without naming the target device itself. Examples that must match:

    "brighter"                   bare intensity adverb
    "a bit more"                 continuation marker
    "turn it up more"            pronoun + action + intensity
    "turn them off"              pronoun + action
    "do that again"              explicit repetition
    "do the same thing"          repetition phrase
    "keep going"                 continuation
    "again"                      bare repetition
    "and the kitchen too"        additive extension
    "also the office"            additive extension

Examples that must NOT match (they name a new target or aren't
home-control at all):

    "turn on the kitchen lights"    names a new target
    "what's the weather"            unrelated domain
    "hello"                         greeting
    "bedroom strip segment 3"       names a new target
    "play some music"               new action on a different target

The pre-Phase-8.8 implementation was "no distinctive qualifier words
= anaphoric" — which worked for ``"brighter"`` but missed
``"turn it up more"`` because ``more`` wasn't in the stopword list.
This module replaces that heuristic with an explicit positive
detector keyed on marker categories below.
"""

from __future__ import annotations

import re


# Third-person / demonstrative deictics that reach back to a prior
# turn's target ("turn *it* off", "dim *those*", "hold *that* again").
# First-person ("me", "we") is excluded — those address the assistant,
# not the prior device.
_PRONOUN_DEICTICS: frozenset[str] = frozenset({
    "it", "its", "that", "those", "these", "this",
    "them", "they", "their",
    "one", "ones",
})


# Explicit repetition / continuation markers. Presence of any of
# these short words in an utterance is strong evidence the speaker is
# extending a prior turn rather than starting a new one.
_REPETITION_MARKERS: frozenset[str] = frozenset({
    "again", "more", "same", "additionally",
    "keep", "continue", "resume",
})


# Additive continuations — "also the kitchen", "and the office too".
# Paired with a short utterance (<= 6 tokens), these typically carry
# over the prior action onto a newly-named area.
_ADDITIVE_MARKERS: frozenset[str] = frozenset({
    "also", "too", "as-well",
})


# Bare-adverb intensity / direction words. Said alone or with only
# filler they unambiguously reference the prior action's subject:
# "brighter" after "turn on the lamp" means "that lamp, but brighter."
_INTENSITY_ADVERBS: frozenset[str] = frozenset({
    "brighter", "dimmer", "lighter", "darker",
    "louder", "softer", "quieter",
    "warmer", "cooler", "hotter", "colder",
    "higher", "lower",
    "faster", "slower",
    "up", "down",
    "on", "off",
    "more", "less",
})


# Filler/stopwords that don't disqualify an otherwise-anaphoric
# utterance. Kept intentionally narrow — only the words that would
# show up in a genuine follow-up sentence.
_FILLER: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but",
    "please", "just", "now", "bit", "little",
    "to", "by", "of", "in", "at",
    "make", "set", "put", "turn", "switch",
    "do", "go", "give", "keep", "let",
    "i", "me", "my", "we", "us",
    "be", "is", "are", "was",
    "if", "then", "so",
    "very", "really", "quite", "even",
})


_WORD_RE = re.compile(r"[a-z']+")


# WH-question / copula-question lead-ins. Utterances starting with any
# of these are state questions, not action follow-ups — ``"what time
# is it"`` tokenises to include the pronoun ``it`` but asking the time
# is never a refinement of a prior light-turn-off. The
# ``CommandResolver`` short-circuits these via ``_is_state_query``
# upstream, but the detector stays correct in isolation by rejecting
# them here too.
_QUESTION_LEADS: frozenset[str] = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "why",
    "how",
})


def _tokenize(utterance: str) -> list[str]:
    return _WORD_RE.findall((utterance or "").lower())


def is_anaphoric_followup(utterance: str) -> bool:
    """Return True when the utterance looks like a follow-up to a
    prior Tier 1/2 action — i.e. the caller should try inheriting
    that prior turn's entity/action target.

    The detector is intentionally biased toward specificity: it would
    rather let a genuine follow-up fall through than false-fire on a
    new command. The cost of a false positive (the disambiguator
    re-uses the wrong target) is higher than the cost of a false
    negative (the engine gives a chitchat reply once, the user
    rephrases).

    Rules (any single rule fires):

    1. Pronoun deictic is present (it, them, that, those, these, …).
    2. Explicit repetition marker (again, more, same, keep, continue).
    3. Bare intensity/direction adverb with no content word apart
       from fillers — e.g. "brighter", "a bit louder", "off".
    4. Short additive continuation — utterance starts with "also" or
       ends with "too" / "as well" AND is <= 6 tokens long.

    Empty / whitespace-only utterances return False.
    """
    tokens = _tokenize(utterance)
    if not tokens:
        return False

    # WH-question guard. ``"what time is it"`` otherwise fires rule 1
    # on ``it``. State queries short-circuit in the resolver upstream,
    # but rejecting them here keeps the module correct stand-alone.
    if tokens[0] in _QUESTION_LEADS:
        return False

    token_set = set(tokens)

    # Rule 1: pronoun deictic anywhere in the utterance.
    if token_set & _PRONOUN_DEICTICS:
        return True

    # Rule 2: explicit repetition marker anywhere.
    if token_set & _REPETITION_MARKERS:
        return True

    # Rule 3: bare intensity utterance. Any intensity adverb is
    # present AND every token is either an intensity adverb, a
    # filler, or a pronoun deictic — no content word of its own.
    if token_set & _INTENSITY_ADVERBS:
        content = token_set - _INTENSITY_ADVERBS - _FILLER - _PRONOUN_DEICTICS
        if not content:
            return True

    # Rule 4: short additive continuation.
    if len(tokens) <= 6 and (
        tokens[0] in _ADDITIVE_MARKERS
        or tokens[-1] in _ADDITIVE_MARKERS
        or (len(tokens) >= 2 and " ".join(tokens[-2:]) == "as well")
    ):
        return True

    return False
