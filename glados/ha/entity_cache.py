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
    from rapidfuzz import fuzz, process
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit in bare dev envs
    fuzz = None
    process = None
    _RAPIDFUZZ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Per-domain fuzzy thresholds
# ---------------------------------------------------------------------------

# Safe domains: fuzzy match is fine at these scores.
_DOMAIN_CUTOFFS: dict[str, int] = {
    "light": 75,
    "switch": 75,
    "fan": 75,
    "scene": 75,
    "script": 75,
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

    def searchable_names(self) -> list[str]:
        """Names to use for fuzzy matching: friendly_name + aliases +
        entity_id (as a low-quality fallback)."""
        names = [self.friendly_name] if self.friendly_name else []
        names.extend(a for a in self.aliases if a)
        # Include entity_id with underscores replaced by spaces so a user
        # saying "kitchen ceiling" can still match `light.kitchen_ceiling`
        # even if friendly_name is empty.
        local = self.entity_id.split(".", 1)[-1].replace("_", " ")
        if local and local not in names:
            names.append(local)
        return names


@dataclass
class CandidateMatch:
    """One row of fuzzy match output."""
    entity: EntityState
    matched_name: str     # Which of the entity's names matched.
    score: float          # 0.0 – 100.0
    sensitive: bool       # True if this domain requires exact-match only.


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
            self._entities = rebuilt
            self._last_full_sync_at = ts
        return len(rebuilt)

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
    ) -> list[CandidateMatch]:
        """Fuzzy-match the user's query against entity names.

        Per-domain cutoffs are enforced; sensitive domains require an
        exact match (score >= 100) — no loose matches to locks /
        alarms / garage doors / cameras.

        Returns the top N matches sorted by descending score."""
        if not query or not query.strip():
            return []
        if not _RAPIDFUZZ_AVAILABLE:
            # Without rapidfuzz we can only do exact-name matching.
            # This path exists so bare dev envs can import the module.
            return self._exact_match_fallback(query, domain_filter, limit)

        q = query.strip().lower()
        scored: list[CandidateMatch] = []
        for entity in self.snapshot():
            if domain_filter and entity.domain not in domain_filter:
                continue
            names = entity.searchable_names()
            if not names:
                continue
            # Find the best-matching name on this entity.
            best = process.extractOne(q, names, scorer=fuzz.WRatio)
            if best is None:
                continue
            matched_name, score, _ = best
            cutoff = _cutoff_for(entity)
            sensitive = cutoff >= 100
            if score < cutoff:
                continue
            scored.append(CandidateMatch(
                entity=entity,
                matched_name=matched_name,
                score=float(score),
                sensitive=sensitive,
            ))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:limit]

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
                    ))
                    break
        return out[:limit]
