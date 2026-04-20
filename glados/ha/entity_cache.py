"""In-memory mirror of Home Assistant entity state.

Populated by the HA WebSocket client. Single writer (WS loop thread),
many readers (tool executor, disambiguator, WebUI).

Design notes:
- `EntityState` stores only what GLaDOS actually needs for name
  resolution and state-based disambiguation: friendly_name, domain,
  device_class, state, a timestamp, and the raw attributes dict for
  anything else callers might need.
- `get_candidates()` is the one place that fuzzy name matching lives.
  Per-domain cutoffs are encoded here so sensitive domains
  (`lock`, `alarm_control_panel`, `cover`+garage, `camera`) can reject
  loose matches before they reach the rest of the pipeline.
- `age()` returns how stale a given entity's state is. The
  disambiguator uses this to skip state-based inference when the
  cache is older than the configured freshness budget.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:  # rapidfuzz is a production dep, but some test envs may not have it.
    from rapidfuzz import fuzz, process, utils as _rf_utils
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit in bare dev envs
    fuzz = None
    process = None
    _rf_utils = None
    _RAPIDFUZZ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Per-domain fuzzy thresholds
# ---------------------------------------------------------------------------

# Safe domains: fuzzy match is fine at these scores.
_DOMAIN_CUTOFFS: dict[str, int] = {
    "light": 75,
    "switch": 75,
    "fan": 75,
    # Scenes/scripts are loose semantic categories — users say "evening
    # scene" or "movie mode" without matching the operator's exact
    # friendly_name like "Living Room Scene: Evening". Lower cutoff so
    # these match.
    "scene": 60,
    "script": 60,
    "media_player": 75,
    "climate": 80,
    "input_boolean": 80,
    "input_number": 80,
    "input_select": 80,
    "input_text": 80,
    "vacuum": 80,
    "cover": 80,          # Non-garage covers still use loose match.
    "sensor": 75,         # Read-only; used for state queries.
    "binary_sensor": 75,
}


# Command-verb stopwords stripped from user queries before fuzzy
# matching. They consume tokens that don't help identify the entity
# and dilute WRatio scores. Order doesn't matter; matching is on words.
#
# Direction / quantity modifiers ("up", "down", "half", "more",
# "dimmer") belong to the action payload (service_data) rather than
# the entity name, so stripping them from the fuzzy query prevents
# them from dragging the score below cutoff. Without this, "turn the
# desk lamp down by half" produced 0 candidates because "down by
# half" polluted the WRatio score against "Office Desk Monitor Lamp".
# Multi-word words like "downstairs" are unaffected — matching is
# whole-word.
_QUERY_STOPWORDS: frozenset[str] = frozenset({
    # Action verbs
    "activate", "deactivate", "trigger", "run", "start", "stop",
    "turn", "switch", "set", "make", "please",
    "adjust", "change", "increase", "decrease", "raise", "reduce",
    # Politeness / filler
    "can", "you", "could", "would",
    # Determiners / prepositions
    "the", "a", "an", "my", "some",
    "on", "off",            # status verbs — consumed by domain inference instead
    "to", "for", "in", "of", "by", "at",
    # Direction / quantity modifiers — belong to service_data, not the
    # entity name. Listed whole-word so "downstairs", "upstairs",
    # "highlight", "lowpass" etc. are unaffected.
    "up", "down", "higher", "lower",
    "brighter", "dimmer", "warmer", "cooler",
    "louder", "quieter", "faster", "slower",
    "more", "less", "bit", "little", "much",
    "half", "halfway", "fully", "maximum", "minimum", "max", "min",
})


def _preprocess_query(query: str) -> str:
    """Strip command-verb noise so fuzzy match focuses on the entity
    name. 'activate the evening scene' -> 'evening scene'. Always lower-
    cased. Leaves the query unchanged if it shrinks to nothing
    (defensive: don't make a meaningful query empty)."""
    if not query:
        return query
    words = [w.strip(".,!?;:'\"") for w in query.split()]
    kept = [w for w in words if w and w.lower() not in _QUERY_STOPWORDS]
    if not kept:
        return query.strip().lower()
    return " ".join(kept).lower()

# Soft ranking bonuses applied on top of the raw WRatio score.
#
# _COVERAGE_BONUS is the max extra points a candidate can gain from
# containing all user-query tokens whole-word in its name/aliases.
# Scaled by coverage ratio so a 2/3 candidate gets 2/3 of the bonus.
#
# _AREA_BONUS is added when the candidate's area_id matches the
# source's area (e.g., a voice satellite in the living room). Flat
# because area-match is binary.
#
# Values are tuned so full-coverage + area-match (15 + 10 = 25) beats
# a loose WRatio hit (~85) against a full-coverage no-area hit (~88),
# but does NOT clobber a genuinely higher-scoring match. The cutoff
# admission check still uses the raw score, so this only rearranges
# candidates that already earned a seat at the table.
_COVERAGE_BONUS: float = 15.0
_AREA_BONUS: float = 10.0

# Phase 8.1: opposing-token penalty. When the utterance contains one
# side of a pair and the candidate's name contains the other, apply a
# flat negative to the rank score. -50 is enough to drop a candidate
# below a full-coverage alternative without nuking it entirely — a
# synonym override in the prompt can still outrank the penalty if the
# operator's rules call for it.
_OPPOSING_TOKEN_PENALTY: float = 50.0

# Default opposing-token pairs. Shipped as defaults so the system works
# out of the box; operators add house-specific pairs via the
# Disambiguation rules WebUI card. Pairs are symmetric — order within
# a pair doesn't matter; matching is whole-word case-insensitive.
_DEFAULT_OPPOSING_TOKENS: tuple[tuple[str, str], ...] = (
    ("upstairs", "downstairs"),
    ("lower", "upper"),
    ("front", "back"),
    ("inside", "outside"),
    ("indoor", "outdoor"),
    ("master", "guest"),
    ("left", "right"),
    ("top", "bottom"),
    ("primary", "secondary"),
    ("north", "south"),
    ("east", "west"),
)


def _name_words(names: list[str]) -> set[str]:
    """Whole-word token set for the entity's names + aliases."""
    words: set[str] = set()
    for name in names:
        for w in name.lower().split():
            words.add(w.strip(".,!?;:'\"-_()/"))
    return words


def _coverage_ratio(query_tokens: list[str], name_words: set[str]) -> float:
    """Fraction of query tokens that appear whole-word in the name set.
    Returns 0.0 when the query has no tokens so callers can treat the
    bonus as additive without a division-by-zero guard."""
    if not query_tokens:
        return 0.0
    hits = sum(1 for t in query_tokens if t in name_words)
    return hits / len(query_tokens)


def _utterance_words(query: str) -> set[str]:
    """Whole-word token set of the raw utterance, used for the opposing-
    token check. Stopword-stripping isn't applied here — "upstairs" isn't
    a stopword, and the direction tokens we care about are all real
    content words the user typed."""
    if not query:
        return set()
    words: set[str] = set()
    for w in query.lower().split():
        words.add(w.strip(".,!?;:'\"-_()/"))
    words.discard("")
    return words


def _normalise_opposing_pairs(
    pairs: list[tuple[str, str]] | list[list[str]] | None,
) -> tuple[tuple[str, str], ...]:
    """Return a tuple of lowercase (a, b) pairs. None means "use the
    shipped defaults"; an explicit empty list disables the penalty."""
    if pairs is None:
        return _DEFAULT_OPPOSING_TOKENS
    out: list[tuple[str, str]] = []
    for p in pairs:
        if not p or len(p) < 2:
            continue
        a = str(p[0]).strip().lower()
        b = str(p[1]).strip().lower()
        if a and b and a != b:
            out.append((a, b))
    return tuple(out)


def _opposing_token_hit(
    utterance_words: set[str],
    name_words: set[str],
    pairs: tuple[tuple[str, str], ...],
) -> bool:
    """True when the utterance contains one side of a pair and the
    candidate name contains the other. Direction-symmetric — either
    order qualifies."""
    if not pairs or not utterance_words or not name_words:
        return False
    for a, b in pairs:
        if a in utterance_words and b in name_words:
            return True
        if b in utterance_words and a in name_words:
            return True
    return False


def _light_has_dim_capability(entity: EntityState) -> bool:
    """True when the entity is a light that actually supports dimming /
    colour — i.e. `supported_color_modes` lists something richer than
    just `onoff`. Inovelli fan/light controllers expose a decorative LED
    indicator as a `light.*` with `supported_color_modes=['onoff']`; we
    want the `switch.*` side to win the twin-dedup in that case because
    the switch is the real control."""
    if entity.domain != "light":
        return False
    modes = entity.attributes.get("supported_color_modes") or []
    if not isinstance(modes, (list, tuple, set)):
        return False
    for m in modes:
        if str(m).lower() != "onoff":
            return True
    return False


def _dedup_light_switch_twins(
    candidates: list[CandidateMatch],
) -> list[CandidateMatch]:
    """Collapse `light.*` / `switch.*` twins that share a device_id.

    Rule: keep the light unless its `supported_color_modes` lacks any
    real dim capability (i.e. the light side is a decorative LED
    indicator), in which case keep the switch. Candidates without a
    device_id are never merged — they come from the same entity_id at
    most once by construction, and we have no way to prove they're
    twins without a registry id to join on."""
    # Group candidates by device_id; only device_id-keyed groups where
    # both light.* and switch.* appear are candidates for merging.
    by_device: dict[str, list[CandidateMatch]] = {}
    for c in candidates:
        did = c.device_id
        if not did:
            continue
        by_device.setdefault(did, []).append(c)

    drop_entity_ids: set[str] = set()
    for group in by_device.values():
        if len(group) < 2:
            continue
        lights = [c for c in group if c.entity.domain == "light"]
        switches = [c for c in group if c.entity.domain == "switch"]
        if not lights or not switches:
            continue
        # Pick the "keeper" side. Prefer light when it has real dim
        # capability; otherwise the switch is the canonical control.
        any_dim_light = any(_light_has_dim_capability(c.entity) for c in lights)
        losers = switches if any_dim_light else lights
        for c in losers:
            drop_entity_ids.add(c.entity.entity_id)

    if not drop_entity_ids:
        return candidates
    return [c for c in candidates if c.entity.entity_id not in drop_entity_ids]


# Sensitive domains: fuzzy match produces wrong-device outcomes with
# real-world consequences. Require exact friendly_name or alias match
# (score >= 100). No fuzzy fallback.
_SENSITIVE_DOMAINS: frozenset[str] = frozenset({
    "lock",
    "alarm_control_panel",
    "camera",
})

# Garage covers specifically — identified by device_class='garage' —
# are treated as sensitive even though the `cover` domain is otherwise
# permissive.
_SENSITIVE_DEVICE_CLASSES: frozenset[str] = frozenset({"garage"})

# Default cutoff when a domain isn't in the table above.
_DEFAULT_CUTOFF: int = 75


def _cutoff_for(entity: EntityState) -> int:
    """Return the minimum fuzzy score that is allowed to match this entity."""
    if entity.domain in _SENSITIVE_DOMAINS:
        return 100
    if entity.device_class in _SENSITIVE_DEVICE_CLASSES:
        return 100
    return _DOMAIN_CUTOFFS.get(entity.domain, _DEFAULT_CUTOFF)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EntityState:
    entity_id: str
    friendly_name: str
    domain: str
    state: str
    state_as_of: float                        # Unix epoch seconds.
    device_class: str | None = None
    area_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    # Phase 8.1: populated from HA's entity_registry (separate WS call
    # from get_states; see EntityCache.apply_entity_registry). Used to
    # collapse light/switch twins that share a single physical device.
    # None until the registry sync lands — new entities added via
    # state_changed default to None and backfill on the next resync.
    device_id: str | None = None

    def searchable_names(self) -> list[str]:
        """Names to use for fuzzy matching.

        Prefer friendly_name + aliases. Only fall back to the entity_id-
        derived label when both are empty. Otherwise the entity_id form
        produces false high scores for common short words: e.g.
        `scene.scene_go_away` would match the query 'activate the evening
        scene' on the token "scene" alone (~85), beating real
        friendly_name candidates that score in the 40s but actually
        relate to evening."""
        names = [self.friendly_name] if self.friendly_name else []
        names.extend(a for a in self.aliases if a)
        if names:
            return names
        # Last-resort fallback for unlabeled entities.
        local = self.entity_id.split(".", 1)[-1].replace("_", " ")
        return [local] if local else []


@dataclass
class CandidateMatch:
    """One row of fuzzy match output."""
    entity: EntityState
    matched_name: str     # Which of the entity's names matched.
    score: float          # Raw WRatio score, 0.0 – 100.0
    sensitive: bool       # True if this domain requires exact-match only.
    # Qualifier coverage: fraction of query tokens (after stopword
    # strip) that appear whole-word in the entity's friendly_name or
    # aliases. 1.0 = every user qualifier is present; 0.0 = none.
    # Exposed to the disambiguator prompt so the LLM can prefer tight
    # matches when there's no synonym / scope override.
    coverage: float = 0.0
    # True iff the candidate's area_id matches the source_area hint
    # passed to get_candidates (e.g., a voice satellite in the living
    # room). None when no area hint was provided for this query so the
    # prompt can omit the signal entirely.
    area_match: bool | None = None
    # Phase 8.1: HA device_id for the entity. Populated via the
    # entity_registry sync. Primarily used internally by the twin-
    # dedup pass but exposed on CandidateMatch for observability.
    device_id: str | None = None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _extract_entity_state(state_obj: dict[str, Any], as_of: float) -> EntityState | None:
    """Build an EntityState from a HA state dict (as returned by
    `get_states` or emitted in a `state_changed` event's `new_state`).

    Returns None if the object doesn't look like a state (missing id)."""
    entity_id = state_obj.get("entity_id")
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    attrs = state_obj.get("attributes") or {}
    return EntityState(
        entity_id=entity_id,
        friendly_name=str(attrs.get("friendly_name") or ""),
        domain=domain,
        state=str(state_obj.get("state") or ""),
        state_as_of=as_of,
        device_class=(attrs.get("device_class") or None),
        area_id=(attrs.get("area_id") or None),
        aliases=list(attrs.get("aliases") or []),
        attributes=attrs,
    )


class EntityCache:
    """Thread-safe in-memory cache of HA entity state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, EntityState] = {}
        # Timestamp of last full resync (get_states). Used for global
        # staleness fallback when an entry doesn't exist.
        self._last_full_sync_at: float = 0.0

    # ── Writers ──────────────────────────────────────────────

    def apply_get_states(self, states: list[dict[str, Any]]) -> int:
        """Replace the cache with a full `get_states` snapshot. Returns
        the number of entities loaded."""
        ts = _now()
        rebuilt: dict[str, EntityState] = {}
        for s in states:
            e = _extract_entity_state(s, as_of=ts)
            if e is not None:
                rebuilt[e.entity_id] = e
        with self._lock:
            # Preserve device_id across the rebuild — state_changed
            # events carry no device_id, and the entity_registry apply
            # that will backfill them runs *after* this call in the
            # startup sequence. Without this carry-over, a state_changed
            # landing between the two applies would null out a good id.
            for eid, prior in self._entities.items():
                new_ent = rebuilt.get(eid)
                if new_ent is not None and prior.device_id and not new_ent.device_id:
                    new_ent.device_id = prior.device_id
            self._entities = rebuilt
            self._last_full_sync_at = ts
        return len(rebuilt)

    def apply_entity_registry(self, entries: list[dict[str, Any]]) -> int:
        """Annotate existing entities with their device_id from HA's
        `config/entity_registry/list` response.

        Each entry shape: `{entity_id, device_id, platform, ...}`. Only
        device_id is consumed. Returns the count of entities actually
        updated. Safe to call repeatedly; missing entities are skipped
        silently (state_changed may not have reached us yet)."""
        updated = 0
        with self._lock:
            for entry in entries:
                eid = entry.get("entity_id")
                if not eid:
                    continue
                ent = self._entities.get(eid)
                if ent is None:
                    continue
                did = entry.get("device_id")
                did = str(did) if did else None
                if ent.device_id != did:
                    ent.device_id = did
                    updated += 1
        return updated

    def apply_state_changed(self, event_data: dict[str, Any]) -> None:
        """Apply a single `state_changed` event. Tolerates missing fields."""
        new_state = event_data.get("new_state")
        entity_id = event_data.get("entity_id")
        if new_state is None:
            # Entity removed.
            if entity_id:
                with self._lock:
                    self._entities.pop(entity_id, None)
            return
        e = _extract_entity_state(new_state, as_of=_now())
        if e is None:
            return
        with self._lock:
            prior = self._entities.get(e.entity_id)
            if prior is not None and prior.device_id and not e.device_id:
                # state_changed carries no device_id — preserve it from
                # the last registry sync so dedup keeps working across
                # state refreshes.
                e.device_id = prior.device_id
            self._entities[e.entity_id] = e

    # ── Readers ──────────────────────────────────────────────

    def get(self, entity_id: str) -> EntityState | None:
        with self._lock:
            return self._entities.get(entity_id)

    def age(self, entity_id: str) -> float:
        """Seconds since the entity's state was last updated. Returns
        `float('inf')` if the entity is unknown."""
        with self._lock:
            e = self._entities.get(entity_id)
        if e is None:
            return float("inf")
        return max(0.0, _now() - e.state_as_of)

    def last_full_sync_age(self) -> float:
        with self._lock:
            ts = self._last_full_sync_at
        if ts <= 0:
            return float("inf")
        return max(0.0, _now() - ts)

    def snapshot(self) -> list[EntityState]:
        """Shallow copy of all entities. Safe to iterate without locking."""
        with self._lock:
            return list(self._entities.values())

    def size(self) -> int:
        with self._lock:
            return len(self._entities)

    # ── Fuzzy name resolution ────────────────────────────────

    def get_candidates(
        self,
        query: str,
        domain_filter: list[str] | None = None,
        limit: int = 10,
        source_area: str | None = None,
        opposing_token_pairs: list[tuple[str, str]] | list[list[str]] | None = None,
        twin_dedup: bool = True,
    ) -> list[CandidateMatch]:
        """Fuzzy-match the user's query against entity names.

        Per-domain cutoffs are enforced on the raw WRatio score;
        sensitive domains require an exact match (score >= 100) — no
        loose matches to locks / alarms / garage doors / cameras.

        Ranking blends three signals so the disambiguator LLM sees
        the best-matching candidates first WITHOUT eliminating
        partial matches (which would preempt synonym / scope-
        broadening rules in the prompt):

          - Raw WRatio on friendly_name / aliases (admission gate).
          - Qualifier coverage: how many of the user's query tokens
            appear whole-word in the entity name. "desk lamp" vs
            "Office Desk Monitor Lamp" = 2/2 coverage = full bonus.
          - Area match: when `source_area` is provided (e.g., a voice
            satellite in the living room), entities in that area
            outrank identically-named ones elsewhere.

        Returns the top N matches sorted by blended rank."""
        if not query or not query.strip():
            return []
        if not _RAPIDFUZZ_AVAILABLE:
            # Without rapidfuzz we can only do exact-name matching.
            # This path exists so bare dev envs can import the module.
            return self._exact_match_fallback(query, domain_filter, limit)

        # Strip command verbs so we match on the entity-identifying part:
        # "activate the evening scene" -> "evening scene".
        q = _preprocess_query(query)
        query_tokens = [t for t in q.split() if t]
        # Phase 8.1: resolve the opposing-token set used for scoring.
        # Normalise to whole-word lowercase pairs; None means "use shipped
        # defaults"; an empty list means "disable the penalty".
        opposing = _normalise_opposing_pairs(opposing_token_pairs)
        # Detect which side(s) of any pair the user's utterance mentions;
        # the penalty then fires against candidates carrying the opposite.
        utterance_words = _utterance_words(query)
        scored: list[tuple[float, CandidateMatch]] = []
        for entity in self.snapshot():
            if domain_filter and entity.domain not in domain_filter:
                continue
            names = entity.searchable_names()
            if not names:
                continue
            # Find the best-matching name on this entity. `default_process`
            # lowercases + strips punctuation on both sides so case
            # mismatches and "Scene:" prefixes don't tank legitimate
            # scores.
            best = process.extractOne(
                q, names, scorer=fuzz.WRatio,
                processor=_rf_utils.default_process,
            )
            if best is None:
                continue
            matched_name, score, _ = best
            cutoff = _cutoff_for(entity)
            sensitive = cutoff >= 100
            if score < cutoff:
                continue
            # Compute soft-ranking signals on top of the admission score.
            name_words = _name_words(names)
            coverage = _coverage_ratio(query_tokens, name_words)
            area_match: bool | None = None
            area_bonus = 0.0
            if source_area is not None:
                area_match = (entity.area_id == source_area)
                if area_match:
                    area_bonus = _AREA_BONUS
            # Phase 8.1: opposing-token penalty. "upstairs lights"
            # shouldn't pick a downstairs fixture just because it's a
            # good fuzzy hit on the rest of the tokens.
            opposing_penalty = (
                _OPPOSING_TOKEN_PENALTY
                if _opposing_token_hit(utterance_words, name_words, opposing)
                else 0.0
            )
            rank_score = (
                float(score)
                + _COVERAGE_BONUS * coverage
                + area_bonus
                - opposing_penalty
            )
            scored.append((rank_score, CandidateMatch(
                entity=entity,
                matched_name=matched_name,
                score=float(score),
                sensitive=sensitive,
                coverage=coverage,
                area_match=area_match,
                device_id=entity.device_id,
            )))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        ranked = [cand for _rank, cand in scored]
        # Phase 8.1: twin dedup. Zooz / Inovelli dimmers expose both
        # `light.foo` and `switch.foo` for one physical relay. Keep the
        # light (the only side that honors brightness_pct), unless the
        # light reports no real dim capability — in which case the
        # switch is the canonical control (Inovelli fan/light pattern).
        if twin_dedup:
            ranked = _dedup_light_switch_twins(ranked)
        return ranked[:limit]

    def _exact_match_fallback(
        self,
        query: str,
        domain_filter: list[str] | None,
        limit: int,
    ) -> list[CandidateMatch]:
        q = query.strip().lower()
        out: list[CandidateMatch] = []
        for entity in self.snapshot():
            if domain_filter and entity.domain not in domain_filter:
                continue
            for name in entity.searchable_names():
                if name.lower() == q:
                    out.append(CandidateMatch(
                        entity=entity,
                        matched_name=name,
                        score=100.0,
                        sensitive=_cutoff_for(entity) >= 100,
                        device_id=entity.device_id,
                    ))
                    break
        return out[:limit]
