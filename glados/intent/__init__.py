"""Stage 3 Phase 1 — Tier 2 disambiguation.

When HA's conversation API misses (no_intent_match, no_valid_targets,
or "garbage_speech" rejection), the disambiguator pulls candidate
entities from the local cache and asks a constrained LLM to either
pick the right one(s), ask a clarifying question, or refuse.

This is the layer that handles the operator's own naming convention
("lights" = overhead = all of them; "lamp" = plug-in; "overhead" is a
specific override) and state-based inference ("turn off the lights"
when only one is on).
"""

from .disambiguator import (
    Disambiguator,
    DisambiguationResult,
    get_disambiguator,
    init_disambiguator,
)
from .rules import (
    DisambiguationRules,
    IntentAllowlist,
    domain_filter_for_utterance,
    load_rules_from_yaml,
)

__all__ = [
    "Disambiguator",
    "DisambiguationResult",
    "DisambiguationRules",
    "IntentAllowlist",
    "domain_filter_for_utterance",
    "get_disambiguator",
    "init_disambiguator",
    "load_rules_from_yaml",
]
