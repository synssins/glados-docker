"""Keyword-based plugin intent matcher.

Phase 2c gate #1: zero-latency word-boundary match between the user's
chat message and each enabled plugin's ``intent_keywords`` list. If
any keyword hits, that plugin's tools get advertised to the LLM on
the chitchat path. Multi-plugin matches are unioned -- the LLM
disambiguates downstream.

Stemming is intentionally minimal: suffix-strip only (``s``, ``es``,
``ies`` -> singular; ``ing`` -> bare). Operators declare keywords in
their canonical singular form ("movie", "torrent") and the matcher
absorbs the most common English plural / progressive variants. We
do NOT pull in a real stemmer (NLTK / Snowball) -- the dependency
weight isn't worth it for a chat-time gate that has to run on every
turn.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loader import Plugin


# Word-boundary tokenizer. Apostrophes are split on so "don't" becomes
# ["don", "t"] -- that's fine, the false-negative on contractions is
# preferable to maintaining a contraction lexicon.
_WORD_RE = re.compile(r"[a-z0-9]+")


def _stems(word: str) -> set[str]:
    """Return every plausible canonical form for matching.

    Suffix-strip only -- no morphological analysis. We emit MULTIPLE
    candidates per word because suffixes are genuinely ambiguous:
    "movies" could be "movie"+s or "mov"+ies->y. Returning both stems
    and letting the keyword side also stem solves the ambiguity
    without a real lexicon."""
    out: set[str] = {word}
    if len(word) > 4 and word.endswith("ies"):
        out.add(word[:-3] + "y")  # stories -> story
    if len(word) > 3 and word.endswith("ing"):
        # "bring" stays "bring"; "running" -> "runn" which won't match
        # "run" but we accept the false-negative here. The triage LLM
        # is the safety net for stemmer misses.
        out.add(word[:-3])
    if len(word) > 3 and word.endswith("es"):
        out.add(word[:-2])  # boxes -> box
    if len(word) > 2 and word.endswith("s"):
        out.add(word[:-1])  # movies -> movie
    return out


def _tokens(message: str) -> set[str]:
    """Lowercase + word-split the message and emit every stem candidate.
    Returns a set so repeat words don't multiply work downstream."""
    out: set[str] = set()
    for raw in _WORD_RE.findall(message.lower()):
        out |= _stems(raw)
    return out


def match_plugins(message: str, plugins: Iterable["Plugin"]) -> list["Plugin"]:
    """Return the subset of ``plugins`` whose ``intent_keywords`` match
    a stemmed token in ``message``.

    Each keyword is itself stemmed before comparison so an operator
    can declare "movies" and still match a user query for "movie"
    (and vice versa). Plugins with empty ``intent_keywords`` are
    never matched here -- the triage LLM gate handles them."""
    if not message:
        return []
    msg_tokens = _tokens(message)
    out: list["Plugin"] = []
    for plugin in plugins:
        keywords = getattr(plugin.manifest_v2, "intent_keywords", None) or []
        if not keywords:
            continue
        for kw in keywords:
            # Stem both sides: operator may declare singular OR plural;
            # message may contain the other form. Set intersection
            # collapses every variant pair to a single check.
            if _stems(kw.lower()) & msg_tokens:
                out.append(plugin)
                break
    return out
