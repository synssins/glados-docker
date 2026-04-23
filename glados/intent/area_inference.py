"""Phase 8.5 — utterance → (area_id, floor_id) inference.

Cluster C of the battery — 16 FAILs like "turn off the downstairs
lights" or "is anything on outside?" — fails today because nothing
maps spoken location keywords to Home Assistant's registry ids. This
module supplies that mapping.

Inference is shape-first (keyword tokens, not regex gymnastics), and
operator-overridable: the built-in keyword table can be extended via
`DisambiguationRules.floor_aliases` and `area_aliases`, each a
{keyword: registry-name} dict. When both a built-in and an override
match, the override wins — operators always get the last word.

No AI, no model call. This runs inline on every utterance; cost
budget is microseconds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


# Shipped floor keywords. Each key is the spoken phrase; each value is
# a tuple of case-insensitive substrings that are tried against the
# floor-registry `name` field. First registry floor whose name contains
# ANY hint term wins.
#
# Why not lump "main floor" and "ground floor" together: split-level
# houses (including the reference deployment) have BOTH — a "Ground
# Level" you walk in on AND a "Main Level" half a flight up. Lumping
# them under a single hint set makes "main floor" resolve to the
# wrong floor.  Houses that only have one of the two still work
# because only one registry entry exists to match.
_FLOOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    # Basement family
    "basement":      ("basement", "cellar"),
    "cellar":        ("basement", "cellar"),
    # Ground / lower / first-floor (US convention) family
    "ground level":  ("ground", "first"),
    "ground floor":  ("ground", "first"),
    "lower level":   ("lower", "ground"),
    "lower floor":   ("lower", "ground"),
    "downstairs":    ("downstairs", "lower", "ground", "first"),
    "first floor":   ("first", "ground"),  # US. UK-style rigs use alias.
    # Main-level family — the common-area floor when it's distinct
    # from the ground floor (split-level houses).
    "main level":    ("main",),
    "main floor":    ("main",),
    # Upper / bedroom / second floor family
    "upstairs":      ("upstairs", "upper", "bedroom", "top", "second"),
    "upper level":   ("upper", "bedroom", "top"),
    "upper floor":   ("upper", "bedroom", "top"),
    "bedroom level": ("bedroom",),
    "bedroom floor": ("bedroom",),
    "top floor":     ("top", "upper", "bedroom"),
    "second floor":  ("second", "upper", "bedroom"),
    # Attic
    "attic":         ("attic", "loft"),
    "loft":          ("loft", "attic"),
}


# Area keywords. Outdoor-ish keywords match any area whose name
# contains one of the listed substrings. This is intentionally loose:
# operators name "Backyard", "Front Yard", "Patio", "Deck" in wildly
# different ways, so we accept any area whose name carries an
# outdoor-ish token.
_AREA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "outside":   ("outside", "outdoor", "yard", "patio", "deck", "porch", "garden"),
    "outdoors":  ("outside", "outdoor", "yard", "patio", "deck", "porch", "garden"),
    "outdoor":   ("outside", "outdoor", "yard", "patio", "deck", "porch", "garden"),
    "yard":      ("yard", "outdoor", "outside"),
    "rear yard": ("yard", "back yard", "rear yard"),
    "front yard": ("front yard", "frontyard", "front"),
}


@dataclass(frozen=True)
class AreaFloorHint:
    """Result of inferring area / floor from an utterance.

    `area_id` and `floor_id` are None when no keyword matched or the
    keyword didn't resolve to any entry in the live registry. `matched_keyword`
    is the literal substring that fired — included for observability
    (audit log + WebUI diagnostics). Both ids can be set when, e.g.,
    an utterance names an area that pins a specific floor; in
    practice we only fill the one we actually matched on and let the
    retriever decide which filter is stricter."""
    area_id: str | None = None
    floor_id: str | None = None
    matched_keyword: str | None = None
    source: str = ""  # "floor_keyword" | "area_keyword" | "floor_alias" | "area_alias"


# Precompiled boundary matcher. Floor keywords like "main floor" span
# word boundaries; we need \b on both ends or "main floor lamp" would
# match "floor" unexpectedly. Sorted longest-first so "first floor"
# beats "first" when both are present.
_WORD_BOUNDARY = re.compile(r"\b\w+\b")


def _find_keyword(utterance: str, candidates: Mapping[str, object]) -> str | None:
    """Scan `utterance` for the longest matching keyword from `candidates`.
    Longest-match avoids "first" eating "first floor" when both exist."""
    if not utterance:
        return None
    low = utterance.lower()
    # Longest-first ordering.
    keys = sorted(candidates.keys(), key=len, reverse=True)
    for kw in keys:
        # Word-boundary match — "outside" must not fire on "insider".
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return kw
    return None


def _resolve_floor(
    hint_terms: tuple[str, ...],
    floor_names: Mapping[str, str],
) -> str | None:
    """Return the floor_id whose registry name contains any of the hint
    terms. First match wins — the registry is tiny (typically 1-4
    floors), so the iteration cost is irrelevant."""
    for floor_id, name in floor_names.items():
        nlow = (name or "").lower()
        for t in hint_terms:
            if t in nlow:
                return floor_id
    return None


def _resolve_area(
    hint_terms: tuple[str, ...],
    area_names: Mapping[str, str],
) -> str | None:
    for area_id, name in area_names.items():
        nlow = (name or "").lower()
        for t in hint_terms:
            if t in nlow:
                return area_id
    return None


def infer_area_floor(
    utterance: str,
    *,
    area_names: Mapping[str, str] | None = None,
    floor_names: Mapping[str, str] | None = None,
    floor_aliases: Mapping[str, str] | None = None,
    area_aliases: Mapping[str, str] | None = None,
) -> AreaFloorHint:
    """Resolve an utterance to a concrete (area_id, floor_id) hint.

    Precedence:
      1. Operator aliases (floor_aliases first, then area_aliases)
      2. Shipped floor keywords
      3. Shipped area keywords

    Floor wins over area when both match, because floor is a stricter
    filter and "downstairs kitchen" is better served by "the kitchen
    on the downstairs floor" than "any area called 'kitchen'"."""
    area_names = area_names or {}
    floor_names = floor_names or {}
    floor_aliases = floor_aliases or {}
    area_aliases = area_aliases or {}

    # --- 1. Operator floor alias ----------------------------------------
    kw = _find_keyword(utterance, floor_aliases)
    if kw:
        target = str(floor_aliases[kw] or "").strip().lower()
        if target:
            for fid, name in floor_names.items():
                if (name or "").strip().lower() == target:
                    return AreaFloorHint(
                        floor_id=fid, matched_keyword=kw,
                        source="floor_alias",
                    )
    # --- 2. Operator area alias -----------------------------------------
    kw = _find_keyword(utterance, area_aliases)
    if kw:
        target = str(area_aliases[kw] or "").strip().lower()
        if target:
            for aid, name in area_names.items():
                if (name or "").strip().lower() == target:
                    return AreaFloorHint(
                        area_id=aid, matched_keyword=kw,
                        source="area_alias",
                    )
    # --- 3. Shipped floor keywords --------------------------------------
    kw = _find_keyword(utterance, _FLOOR_KEYWORDS)
    if kw:
        fid = _resolve_floor(_FLOOR_KEYWORDS[kw], floor_names)
        if fid:
            return AreaFloorHint(
                floor_id=fid, matched_keyword=kw,
                source="floor_keyword",
            )
    # --- 4. Shipped area keywords ---------------------------------------
    kw = _find_keyword(utterance, _AREA_KEYWORDS)
    if kw:
        aid = _resolve_area(_AREA_KEYWORDS[kw], area_names)
        if aid:
            return AreaFloorHint(
                area_id=aid, matched_keyword=kw,
                source="area_keyword",
            )
    return AreaFloorHint()


__all__ = ["AreaFloorHint", "infer_area_floor"]
