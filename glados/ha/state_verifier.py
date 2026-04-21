"""Post-execute state verification — Phase 8.4.

The WS `call_service` dispatch returns an acknowledgement from HA the
moment HA accepts the request, NOT when the action has actually
landed on the target entity. Under normal conditions the ack is
followed quickly by one or more `state_changed` events that reflect
the change. Under abnormal conditions — target unavailable, Z-Wave
node dead, integration unhealthy — HA reports `action_done` and the
entity never moves.

Phase 8.4 puts a verification layer between the call and the user-
facing speech. The disambiguator invokes:

    watch = verifier.begin_watch([ExpectedTransition(...)], timeout_s=3.0)
    ha_client.call_service(...)
    result = watch.wait()
    if not result.verified:
        # Replace optimistic "Done." speech with honest message
        # and flag the audit row.

Scopes explicitly kept out of this phase:
  - Scene verification (`scene.turn_on` doesn't produce a state change
    on the scene entity itself; the cascade happens on child entities
    the scene touches). Handled as `skipped` for now.
  - Retries / auto-repair. 8.4 reports truth; 8.7 composer will decide
    how to speak about it.

Callers must be careful to begin the watch BEFORE call_service, not
after — the ack-then-state_changed sequence can fire in <1 ms and a
late subscription will miss it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExpectedTransition:
    """One entity's expected post-call state.

    `expected_state` is the `state` field HA reports after the action
    ("on", "off", "cleaning", etc.). None means "any state change
    counts" — useful for `toggle` where the destination is unknown.

    `expected_attrs` is a dict of attribute-name → desired value.
    Numeric attributes with an entry in `attr_tolerance` are compared
    with absolute tolerance (for brightness_pct where HA rounds). All
    other attributes are compared with `==`.

    `skip_verification` marks a call that shouldn't be verified at all
    (e.g. scene.turn_on). Watch will mark it `skipped` immediately.
    """
    entity_id: str
    expected_state: str | None = None
    expected_attrs: dict[str, Any] = field(default_factory=dict)
    attr_tolerance: dict[str, float] = field(default_factory=dict)
    skip_verification: bool = False


@dataclass
class EntityVerification:
    entity_id: str
    verified: bool
    skipped: bool = False
    observed_state: str | None = None
    observed_attrs: dict[str, Any] = field(default_factory=dict)
    mismatch_reason: str | None = None


@dataclass
class VerificationResult:
    """Outcome of a watch. `verified` is True iff EVERY non-skipped
    expected transition was satisfied within the timeout window."""
    verified: bool
    timed_out: bool
    elapsed_s: float
    per_entity: dict[str, EntityVerification]

    @property
    def failed_entity_ids(self) -> list[str]:
        return [
            eid for eid, r in self.per_entity.items()
            if not r.verified and not r.skipped
        ]

    @property
    def any_skipped(self) -> bool:
        return any(r.skipped for r in self.per_entity.values())


# ---------------------------------------------------------------------------
# Verifier + Watch
# ---------------------------------------------------------------------------

class StateVerifier:
    """Factory for Watch objects. Holds references to the HA client
    (for callback registration) and the entity cache (used for the
    pre-call state snapshot so "no change" can distinguish from
    "cache didn't have a prior value")."""

    def __init__(self, ha_client: Any, cache: Any) -> None:
        self._ha = ha_client
        self._cache = cache

    def begin_watch(
        self,
        expected: list[ExpectedTransition],
        *,
        timeout_s: float = 3.0,
    ) -> "Watch":
        """Start watching for the given transitions. Returns a Watch
        the caller blocks on via `wait()` AFTER dispatching the
        call_service. The watch registers its state_changed callback
        IMMEDIATELY, before any call is made, so the ack→event
        sequence can't be missed."""
        return Watch(self._ha, self._cache, expected, timeout_s=timeout_s)


class Watch:
    """One-shot watch for a set of expected transitions. Thread-safe:
    the state_changed callback runs on the client's asyncio thread;
    `wait()` is called from the thread that dispatched call_service."""

    def __init__(
        self,
        ha_client: Any,
        cache: Any,
        expected: list[ExpectedTransition],
        *,
        timeout_s: float = 3.0,
    ) -> None:
        self._ha = ha_client
        self._cache = cache
        self._expected = list(expected)
        self._timeout_s = float(timeout_s)
        self._start = time.monotonic()
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._observed: dict[str, dict[str, Any]] = {}
        self._closed = False
        # Only non-skipped transitions need satisfaction. An all-
        # skipped watch is already complete; wait() returns
        # immediately.
        self._needed = {
            t.entity_id for t in self._expected if not t.skip_verification
        }
        if not self._needed:
            self._event.set()
        else:
            self._ha.on_state_changed(self._on_state_changed)

    # ── Callback ────────────────────────────────────────────

    def _on_state_changed(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        eid = data.get("entity_id")
        if eid not in self._needed:
            return
        new_state = data.get("new_state")
        if not isinstance(new_state, dict):
            return
        with self._lock:
            self._observed[eid] = new_state
            # Check if every expected transition is now satisfied.
            if self._all_satisfied_locked():
                self._event.set()

    def _all_satisfied_locked(self) -> bool:
        for t in self._expected:
            if t.skip_verification:
                continue
            observed = self._observed.get(t.entity_id)
            if observed is None:
                return False
            if not _matches(t, observed):
                return False
        return True

    # ── Public blocking API ─────────────────────────────────

    def wait(self) -> VerificationResult:
        """Block up to `timeout_s` for every expected transition to
        be observed. Returns a VerificationResult either way. Safe to
        call multiple times; the underlying event is one-shot."""
        remaining = max(0.0, self._timeout_s - (time.monotonic() - self._start))
        triggered = self._event.wait(remaining) if remaining > 0 else False
        elapsed = time.monotonic() - self._start
        # Close the callback subscription before building the result —
        # any events arriving after wait() returns are not our concern.
        self._close()
        return self._build_result(triggered=triggered, elapsed=elapsed)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ha.off_state_changed(self._on_state_changed)
        except Exception as exc:  # noqa: BLE001
            logger.debug("StateVerifier: off_state_changed raised: {}", exc)

    def _build_result(
        self, *, triggered: bool, elapsed: float,
    ) -> VerificationResult:
        per_entity: dict[str, EntityVerification] = {}
        with self._lock:
            observed_copy = dict(self._observed)
        for t in self._expected:
            if t.skip_verification:
                per_entity[t.entity_id] = EntityVerification(
                    entity_id=t.entity_id,
                    verified=False,
                    skipped=True,
                )
                continue
            observed = observed_copy.get(t.entity_id)
            if observed is None:
                per_entity[t.entity_id] = EntityVerification(
                    entity_id=t.entity_id,
                    verified=False,
                    mismatch_reason="no state_changed observed",
                )
                continue
            ok = _matches(t, observed)
            attrs = observed.get("attributes") or {}
            reason = None if ok else _describe_mismatch(t, observed)
            per_entity[t.entity_id] = EntityVerification(
                entity_id=t.entity_id,
                verified=ok,
                observed_state=observed.get("state"),
                observed_attrs=dict(attrs),
                mismatch_reason=reason,
            )
        all_verified = all(
            r.verified or r.skipped for r in per_entity.values()
        )
        return VerificationResult(
            verified=all_verified,
            timed_out=not triggered and bool(self._needed),
            elapsed_s=elapsed,
            per_entity=per_entity,
        )


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _matches(
    expected: ExpectedTransition,
    observed: dict[str, Any],
) -> bool:
    """True when `observed` satisfies the expected transition."""
    if expected.expected_state is not None:
        if observed.get("state") != expected.expected_state:
            return False
    if expected.expected_attrs:
        attrs = observed.get("attributes") or {}
        for key, want in expected.expected_attrs.items():
            got = attrs.get(key)
            if got is None:
                return False
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                tol = expected.attr_tolerance.get(key, 0.0)
                if abs(got - want) > tol:
                    return False
            else:
                if got != want:
                    return False
    return True


def _describe_mismatch(
    expected: ExpectedTransition,
    observed: dict[str, Any],
) -> str:
    """Short human-readable diff for audit / debug."""
    parts: list[str] = []
    if expected.expected_state is not None:
        got = observed.get("state")
        if got != expected.expected_state:
            parts.append(f"state={got!r} (want {expected.expected_state!r})")
    if expected.expected_attrs:
        attrs = observed.get("attributes") or {}
        for key, want in expected.expected_attrs.items():
            got = attrs.get(key)
            if got != want:
                tol = expected.attr_tolerance.get(key, 0.0)
                if tol and isinstance(got, (int, float)) and isinstance(want, (int, float)):
                    parts.append(f"{key}={got} (want {want} ± {tol})")
                else:
                    parts.append(f"{key}={got!r} (want {want!r})")
    return "; ".join(parts) or "no transition details"


# ---------------------------------------------------------------------------
# Inference helpers — service → expected state mapping
# ---------------------------------------------------------------------------

# Services that don't produce a meaningful state change on the
# targeted entity itself. Scenes cascade to child entities; scripts
# don't map to a single observable state; refreshes don't change
# anything. These are marked skip_verification so they don't show
# as failures.
_SKIP_SERVICES: frozenset[str] = frozenset({
    "scene.turn_on", "scene.apply",
    "script.turn_on", "script.toggle",
    "homeassistant.update_entity", "homeassistant.reload_config_entry",
    "automation.trigger", "automation.reload",
    "notify.notify",
})

# Service → destination state. `toggle` is None (any change OK).
_SERVICE_STATE_MAP: dict[str, str | None] = {
    "turn_on": "on",
    "turn_off": "off",
    "toggle": None,
    "open_cover": "open",
    "close_cover": "closed",
    "lock": "locked",
    "unlock": "unlocked",
    "start": None,
    "stop": None,
}


def expected_from_service_call(
    domain: str,
    service: str,
    entity_ids: list[str],
    service_data: dict[str, Any] | None = None,
) -> list[ExpectedTransition]:
    """Derive a sensible ExpectedTransition per entity from a pending
    call_service dispatch. Works for simple on/off/toggle cases with
    optional attribute checks (brightness_pct, color_temp_kelvin).
    Unknown services get skip_verification — we don't want to fail
    on shapes we haven't modelled."""
    fq = f"{domain}.{service}"
    if fq in _SKIP_SERVICES:
        return [
            ExpectedTransition(entity_id=eid, skip_verification=True)
            for eid in entity_ids
        ]
    base_state = _SERVICE_STATE_MAP.get(service)
    # Numeric attr tolerances — HA rounds brightness_pct and
    # color_temp_kelvin often lands on the nearest Mired / Kelvin bucket
    # the bulb supports. Keep tolerance generous so "within a click" is
    # still "verified."
    attr_expected: dict[str, Any] = {}
    tolerance: dict[str, float] = {}
    sd = service_data or {}
    # `brightness_pct` is service-call-only — HA reports the resulting
    # state under the 0-255 `brightness` attribute. Translate so the
    # verifier can find the attribute in state_changed payloads.
    # 5% on 100-scale ≈ 12.75 on 255-scale; use 15 for a little headroom
    # around bulbs that bucket brightness to discrete levels.
    if "brightness_pct" in sd:
        try:
            attr_expected["brightness"] = round(float(sd["brightness_pct"]) * 255 / 100)
            tolerance["brightness"] = 15.0
        except (TypeError, ValueError):
            pass
    for numeric_key, tol in (
        ("brightness", 15.0),
        ("color_temp_kelvin", 200.0),
        ("color_temp", 20.0),
        ("volume_level", 0.05),
        ("temperature", 1.0),
        ("percentage", 5.0),
    ):
        if numeric_key in sd and numeric_key not in attr_expected:
            attr_expected[numeric_key] = sd[numeric_key]
            tolerance[numeric_key] = tol
    # Non-numeric attribute expectations — colour names, presets,
    # HVAC modes — compared exactly.
    for exact_key in ("color_name", "preset_mode", "hvac_mode"):
        if exact_key in sd:
            attr_expected[exact_key] = sd[exact_key]
    # Unknown service (not in _SERVICE_STATE_MAP) AND no attr
    # expectations → skip. For known services with
    # base_state=None (toggle, start, stop), "any state change"
    # is still a valid verification signal and not skipped.
    if service not in _SERVICE_STATE_MAP and not attr_expected:
        return [
            ExpectedTransition(entity_id=eid, skip_verification=True)
            for eid in entity_ids
        ]
    return [
        ExpectedTransition(
            entity_id=eid,
            expected_state=base_state,
            expected_attrs=attr_expected,
            attr_tolerance=tolerance,
        )
        for eid in entity_ids
    ]


__all__ = [
    "EntityVerification",
    "ExpectedTransition",
    "StateVerifier",
    "VerificationResult",
    "Watch",
    "expected_from_service_call",
]
