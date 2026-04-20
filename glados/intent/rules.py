"""Disambiguation rules: operator-editable naming convention,
state-based inference policy, and per-source × per-domain allowlist.

All loaded from `configs/disambiguation.yaml`. Sensible defaults so
the system works out of the box without a config file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from loguru import logger


# ---------------------------------------------------------------------------
# Naming convention — keyword → likely HA domains
# ---------------------------------------------------------------------------

# Maps keywords the user might say to the HA domains those keywords
# typically refer to. Used to narrow candidate entities before fuzzy
# matching. A query like "turn off the bedroom lights" produces
# domain hints ["light", "switch"] (operator convention: 'lights' may
# also map to wall switches that control overhead lights).
_DEFAULT_KEYWORD_DOMAINS: dict[str, list[str]] = {
    "light":     ["light", "switch"],
    "lights":    ["light", "switch"],
    "lamp":      ["light"],
    "lamps":     ["light"],
    "bulb":      ["light"],
    "switch":    ["switch"],
    "switches":  ["switch"],
    "outlet":    ["switch"],
    "plug":      ["switch"],
    "fan":       ["fan", "switch"],
    "scene":     ["scene"],
    "lock":      ["lock"],
    "unlock":    ["lock"],
    "garage":    ["cover"],
    "door":      ["lock", "cover", "binary_sensor"],
    "blinds":    ["cover"],
    "shades":    ["cover"],
    "shutter":   ["cover"],
    "thermostat": ["climate"],
    "temperature": ["climate", "sensor"],
    "ac":        ["climate"],
    "heat":      ["climate"],
    "music":     ["media_player"],
    "tv":        ["media_player"],
    "speaker":   ["media_player"],
    "camera":    ["camera"],
    "alarm":     ["alarm_control_panel"],
}


def domain_filter_for_utterance(utterance: str) -> list[str] | None:
    """Return a list of HA domains that the utterance plausibly refers
    to, or None for "no narrowing" (let fuzzy match across everything).

    Conservative: if any keyword matches, narrow; otherwise broad.
    """
    if not utterance:
        return None
    words = {w.strip(".,!?;:'\"").lower() for w in utterance.split()}
    domains: set[str] = set()
    for w in words:
        for hint in _DEFAULT_KEYWORD_DOMAINS.get(w, []):
            domains.add(hint)
    return sorted(domains) if domains else None


# ---------------------------------------------------------------------------
# Activity phrases — map to scene / script activation in Tier 2. Exact
# phrase match (whole-phrase boundary). Kept small on purpose; the LLM
# does the actual scene selection against the candidate list.
# ---------------------------------------------------------------------------

_ACTIVITY_PHRASES: frozenset[str] = frozenset({
    "movie", "movie time", "cinema", "cinema time",
    "bedtime", "time for bed", "going to bed", "going to sleep",
    "time to sleep", "time to read", "reading time", "reading",
    "goodnight", "good night",
    "good morning", "wake up", "wake-up", "morning routine",
    "dinner", "dinner time", "breakfast", "lunch",
    "focus", "focus mode", "work mode",
    "relax", "relax mode", "chill", "wind down",
    "party", "party mode",
})


def _has_activity_phrase(utterance: str) -> bool:
    if not utterance:
        return False
    # Normalise to lowercase, strip trailing punctuation from the ends,
    # then whole-phrase-match with leading / trailing space guards so
    # "wake" inside "I wake" doesn't falsely match "wake up".
    norm = " " + utterance.strip().lower().strip(".,!?;:'\"") + " "
    return any((" " + p + " ") in norm for p in _ACTIVITY_PHRASES)


# ---------------------------------------------------------------------------
# Phase 8.2 — Home-command verbs + ambient-state patterns
#
# Cluster B in the 435-test battery: 62 FAILs where the utterance carried
# real command intent but the Phase 6 precheck rejected it because no
# noun keyword matched. "Darken the bedroom", "bump it up", "it's too
# dark", "I want to read" all fell into chitchat and silently did
# nothing. The precheck now also catches:
#
#   • action verbs implied to drive a device ("darken", "dim", "bump", …)
#   • ambient-state phrases ("it's too dark", "I can't see", "time to read")
#
# Both sets are mergeable: operators add house-specific extras via the
# Personality → Command recognition card (saved into disambiguation.yaml
# and applied at runtime via `apply_precheck_overrides`).
# ---------------------------------------------------------------------------

# Shipped defaults — the 28-verb set from the remediation plan §6.8.2.
_HOME_COMMAND_VERBS: frozenset[str] = frozenset({
    "darken", "brighten", "dim", "lighten", "bump",
    "lower", "raise", "reduce", "increase", "soften",
    "tone", "crank", "kill", "douse", "extinguish",
    "illuminate", "light", "set", "put", "dial",
    "slide", "push", "pull", "close", "open", "shut", "drop",
})

# Shipped default ambient-state regexes. Conservative — they trigger
# only on clear environment-reference phrasing so "I want pizza" stays
# in chitchat but "I want to read" / "it's too dark" pass. Operators
# extend via the WebUI.
_AMBIENT_STATE_DEFAULT_PATTERNS: tuple[str, ...] = (
    # "it's too dark", "the living room is cold", etc.
    r"\b(?:it'?s|that'?s|the\s+\w+(?:\s+\w+){0,2}\s+is)\s+"
    r"(?:too\s+|way\s+too\s+|really\s+)?"
    r"(?:dark|bright|dim|hot|cold|cool|warm|stuffy|loud|quiet|noisy)\b",
    # "I can't see / hear / read"
    r"\bi\s+(?:can'?t|cannot)\s+(?:see|hear|read|sleep)\b",
    # "I need more light", "I want it brighter"
    r"\bi\s+(?:need|want|would\s+like)\s+"
    r"(?:more|less|some|the|it\s+)?\s*"
    r"(?:light|lights|sound|music|heat|warmth|quiet|silence|air|fan|"
    r"brighter|darker|louder|quieter|warmer|cooler|to\s+read|to\s+sleep)\b",
    # "time to read", "time for bed"
    r"\btime\s+(?:to|for)\s+"
    r"(?:read|reading|sleep|bed|wake|dinner|movie|watch|relax|focus)\b",
    # "... mode in the kitchen"
    r"\b(?:movie|reading|dinner|party|sleep|focus|relax|bedtime)\s+mode\s+in\b",
)


def _compile_patterns(raw: Iterable[str]) -> list[re.Pattern[str]]:
    """Compile a list of regex source strings to case-insensitive
    patterns. Invalid regexes are logged and skipped so one bad
    operator edit can't break the entire precheck."""
    out: list[re.Pattern[str]] = []
    for src in raw:
        if not src or not isinstance(src, str):
            continue
        try:
            out.append(re.compile(src, re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                "Invalid ambient pattern ignored: {!r} ({})", src, exc,
            )
    return out


_AMBIENT_STATE_PATTERNS: list[re.Pattern[str]] = _compile_patterns(
    _AMBIENT_STATE_DEFAULT_PATTERNS
)

# Runtime extras — populated by `apply_precheck_overrides(rules)` when
# the disambiguator loads (or hot-reloads) rules. Merged additively with
# the shipped defaults; shipped defaults are never removed at runtime.
_runtime_extra_verbs: frozenset[str] = frozenset()
_runtime_extra_ambient_patterns: list[re.Pattern[str]] = []


def _has_command_verb(utterance: str) -> bool:
    if not utterance:
        return False
    words = {w.strip(".,!?;:'\"").lower() for w in utterance.split()}
    if words & _HOME_COMMAND_VERBS:
        return True
    if _runtime_extra_verbs and (words & _runtime_extra_verbs):
        return True
    return False


def _matches_ambient_pattern(utterance: str) -> bool:
    if not utterance:
        return False
    for pat in _AMBIENT_STATE_PATTERNS:
        if pat.search(utterance):
            return True
    for pat in _runtime_extra_ambient_patterns:
        if pat.search(utterance):
            return True
    return False


def looks_like_home_command(utterance: str) -> bool:
    """Cheap precheck: does this utterance carry ANY signal that the
    operator intends a home-automation action?

    Phase 6 follow-up (2026-04-18): Tier 2 used to run its fuzzy
    candidate match unconditionally. That produced garbage responses
    for conversational utterances — "Say hello to my little friend,
    his name is Alan" fuzzy-matched "Alan" across friendly names /
    entity IDs and the LLM dutifully built an ambiguity response
    listing raw entity IDs. Now Tier 2 skips the LLM call entirely
    when there's no device-keyword and no activity phrase, and
    falls through to Tier 3 (the chitchat-capable full LLM) instead.

    Phase 8.2 expansion (2026-04-20): cluster-B FAILs showed the
    noun-only gate was too narrow. "Darken the bedroom", "bump it
    up", and ambient phrases like "it's too dark" carry intent but
    no device noun; they now pass the precheck via the verb and
    pattern checks added here. False-positives fall through to
    Tier 3 chitchat — cheap, no silent ignore.

    True when ANY of:
      • a keyword from the domain-filter map appears
      • a known activity phrase maps to a scene / script
      • a home-command verb appears (darken, dim, bump, …)
      • an ambient-state pattern matches (it's too dark, time to read, …)
    """
    if domain_filter_for_utterance(utterance) is not None:
        return True
    if _has_activity_phrase(utterance):
        return True
    if _has_command_verb(utterance):
        return True
    if _matches_ambient_pattern(utterance):
        return True
    return False


def explain_home_command_match(utterance: str) -> dict[str, Any]:
    """Same predicate as `looks_like_home_command`, but returns a
    structured explanation for the WebUI test-input preview box.
    Keys: `matches` (bool), `via` (list of reasons), `domains` (list
    of inferred HA domains when the noun-keyword path triggered)."""
    reasons: list[str] = []
    domains = domain_filter_for_utterance(utterance)
    if domains is not None:
        reasons.append("keyword")
    if _has_activity_phrase(utterance):
        reasons.append("activity_phrase")
    if _has_command_verb(utterance):
        reasons.append("command_verb")
    if _matches_ambient_pattern(utterance):
        reasons.append("ambient_pattern")
    return {
        "matches": bool(reasons),
        "via": reasons,
        "domains": domains,
    }


def apply_precheck_overrides(rules: "DisambiguationRules") -> None:
    """Update the module-level runtime extras from a loaded rules
    object. Additive with the shipped defaults — operator edits never
    remove a built-in. Called at startup and on hot-reload."""
    global _runtime_extra_verbs, _runtime_extra_ambient_patterns
    verbs = {
        v.strip().lower()
        for v in (rules.extra_command_verbs or [])
        if isinstance(v, str) and v.strip()
    }
    _runtime_extra_verbs = frozenset(verbs)
    _runtime_extra_ambient_patterns = _compile_patterns(
        rules.extra_ambient_patterns or []
    )


# ---------------------------------------------------------------------------
# Rules dataclass — loadable from YAML
# ---------------------------------------------------------------------------

@dataclass
class DisambiguationRules:
    """Operator-tunable rules for the disambiguator's LLM prompt.

    `naming_convention` is rendered into the system prompt so the LLM
    follows the operator's house terminology. `state_inference` toggles
    the state-based filter. `max_state_age_seconds` is the freshness
    budget for state-based decisions (Stage 3 plan: 5s default)."""

    naming_convention: dict[str, str] = field(default_factory=lambda: {
        "lamp / lamps":   "plug-in fixtures or smart bulbs",
        "light / lights": "overhead fixtures (plural = all in scope)",
        "switch / switches": "physical wall switches",
    })
    overhead_synonyms: list[str] = field(default_factory=lambda: [
        "overhead", "ceiling", "ceiling light",
    ])
    state_inference: bool = True
    max_state_age_seconds: float = 5.0
    candidate_limit: int = 12
    extra_guidance: str = ""  # Free-form text appended to system prompt
    # Phase 8.1 — candidate scoring controls surfaced on the WebUI's
    # Integrations → Home Assistant page. Empty `opposing_token_pairs`
    # means the scorer falls back to the shipped defaults (11 pairs);
    # an explicit empty list in the YAML disables the penalty entirely.
    # See glados.ha.entity_cache._DEFAULT_OPPOSING_TOKENS.
    opposing_token_pairs: list[list[str]] = field(default_factory=list)
    twin_dedup: bool = True
    # Phase 8.2 — precheck command verbs + ambient-state regexes,
    # edited via the WebUI's Personality → Command recognition card.
    # Merged additively with the shipped defaults. An empty list here
    # means "use only shipped defaults" (the defaults themselves are
    # NEVER removable at runtime — they're part of the container's
    # contract). Invalid regexes are logged and skipped.
    extra_command_verbs: list[str] = field(default_factory=list)
    extra_ambient_patterns: list[str] = field(default_factory=list)
    # Phase 8.3.5 — extra segment tokens used by the device-diversity
    # filter on top-K retrieval. Merged additively with the shipped
    # defaults (seg, segment, zone, channel, strip, group, head).
    # Edited via the Disambiguation rules WebUI card. Typical
    # addition for operators with LED strips that name segments
    # unconventionally (e.g. 'pixel' on some WLED configs).
    extra_segment_tokens: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Allowlist — per-source × per-domain matrix
# ---------------------------------------------------------------------------

# Default policy from Stage 3 plan section 6:
#  light/switch/fan/scene/script/media_player    — all sources allow
#  climate/input_*                               — all sources allow
#  cover (non-garage), vacuum                    — all sources allow except autonomy
#  cover (garage), camera                        — webui_chat only
#  lock, alarm_control_panel                     — webui_chat only (PIN later)

@dataclass(frozen=True)
class _DomainPolicy:
    allow: frozenset[str]


_DEFAULT_ALLOWLIST: dict[str, _DomainPolicy] = {
    "light":         _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "switch":        _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "fan":           _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "scene":         _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "script":        _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "media_player":  _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "climate":       _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "input_boolean": _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "input_number":  _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "input_select":  _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "input_text":    _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd","autonomy"})),
    "vacuum":        _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd"})),
    "cover":         _DomainPolicy(allow=frozenset({"webui_chat","api_chat","voice_mic","mqtt_cmd"})),
    # Sensitive — see _is_garage_cover for the device_class override.
    "lock":                 _DomainPolicy(allow=frozenset({"webui_chat"})),
    "alarm_control_panel":  _DomainPolicy(allow=frozenset({"webui_chat"})),
    "camera":               _DomainPolicy(allow=frozenset({"webui_chat"})),
}


class IntentAllowlist:
    """Decides whether a (source, domain, device_class) tuple is
    permitted to act. Reads from the static default matrix; future
    config support can override per-deployment."""

    def __init__(self, matrix: dict[str, _DomainPolicy] | None = None) -> None:
        self._matrix = matrix if matrix is not None else _DEFAULT_ALLOWLIST

    def is_allowed(
        self,
        source: str,
        domain: str,
        device_class: str | None = None,
    ) -> bool:
        # Garage covers escalate to the strict policy regardless of the
        # generic `cover` domain — same threat profile as a lock.
        if domain == "cover" and device_class == "garage":
            policy = self._matrix.get("lock")  # treat as lock-class
        else:
            policy = self._matrix.get(domain)
        if policy is None:
            # Unknown domain — default deny for safety. Operator can
            # extend the matrix to opt in.
            return False
        return source in policy.allow

    def explain_denial(self, source: str, domain: str,
                       device_class: str | None = None) -> str:
        if domain == "cover" and device_class == "garage":
            return (f"garage cover is treated as a sensitive domain and is "
                    f"only allowed from webui_chat (got {source!r})")
        return (f"domain {domain!r} is not allowed from source "
                f"{source!r} per the intent allowlist")


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def load_rules_from_yaml(path: str | Path) -> DisambiguationRules:
    """Load disambiguation rules from a YAML file. Missing keys fall
    back to defaults; missing file returns full defaults silently."""
    p = Path(path)
    if not p.exists():
        logger.debug("Disambiguation rules file not found, using defaults: {}", p)
        return DisambiguationRules()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load disambiguation rules ({}): {}", p, exc)
        return DisambiguationRules()
    rules = DisambiguationRules()
    nc = raw.get("naming_convention")
    if isinstance(nc, dict):
        rules.naming_convention = {str(k): str(v) for k, v in nc.items()}
    ov = raw.get("overhead_synonyms")
    if isinstance(ov, list):
        rules.overhead_synonyms = [str(s) for s in ov]
    if isinstance(raw.get("state_inference"), bool):
        rules.state_inference = bool(raw["state_inference"])
    if isinstance(raw.get("max_state_age_seconds"), (int, float)):
        rules.max_state_age_seconds = float(raw["max_state_age_seconds"])
    if isinstance(raw.get("candidate_limit"), int):
        rules.candidate_limit = int(raw["candidate_limit"])
    if isinstance(raw.get("extra_guidance"), str):
        rules.extra_guidance = raw["extra_guidance"]
    otp = raw.get("opposing_token_pairs")
    if isinstance(otp, list):
        cleaned: list[list[str]] = []
        for pair in otp:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                a = str(pair[0]).strip()
                b = str(pair[1]).strip()
                if a and b and a.lower() != b.lower():
                    cleaned.append([a, b])
        rules.opposing_token_pairs = cleaned
    if isinstance(raw.get("twin_dedup"), bool):
        rules.twin_dedup = bool(raw["twin_dedup"])
    ev = raw.get("extra_command_verbs")
    if isinstance(ev, list):
        rules.extra_command_verbs = [
            str(v).strip() for v in ev if str(v).strip()
        ]
    ep = raw.get("extra_ambient_patterns")
    if isinstance(ep, list):
        rules.extra_ambient_patterns = [
            str(s) for s in ep if isinstance(s, str) and s.strip()
        ]
    st = raw.get("extra_segment_tokens")
    if isinstance(st, list):
        rules.extra_segment_tokens = [
            str(t).strip().lower()
            for t in st
            if isinstance(t, str) and t.strip()
        ]
    return rules


# Phase 8.1: YAML round-trip helpers for the WebUI save path. The
# operator edits these rules on the Disambiguation rules card; we
# serialise the dataclass back to the same shape the loader accepts.

def rules_to_dict(rules: DisambiguationRules) -> dict[str, Any]:
    """Serialise a DisambiguationRules to a plain dict for YAML output."""
    return {
        "naming_convention": dict(rules.naming_convention),
        "overhead_synonyms": list(rules.overhead_synonyms),
        "state_inference": bool(rules.state_inference),
        "max_state_age_seconds": float(rules.max_state_age_seconds),
        "candidate_limit": int(rules.candidate_limit),
        "extra_guidance": str(rules.extra_guidance or ""),
        "opposing_token_pairs": [list(p) for p in rules.opposing_token_pairs],
        "twin_dedup": bool(rules.twin_dedup),
        "extra_command_verbs": list(rules.extra_command_verbs),
        "extra_ambient_patterns": list(rules.extra_ambient_patterns),
        "extra_segment_tokens": list(rules.extra_segment_tokens),
    }


def save_rules_to_yaml(path: str | Path, rules: DisambiguationRules) -> None:
    """Write disambiguation rules to disk. Writes to a sibling `.tmp`
    first, then renames — the disambiguator may be reading the file on
    another thread."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(rules_to_dict(rules), sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(p)
