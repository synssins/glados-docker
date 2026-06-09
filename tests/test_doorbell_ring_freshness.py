"""Regression tests for the doorbell-ring freshness guard.

Root cause (2026-06-03): UniFi Protect `event.*_doorbell` entities carry the
last-press timestamp as their state. On an integration reconnect the entity
transitions `unavailable` -> its RESTORED (stale) timestamp, which HA delivers
as a `state_changed`. The old doorbell fast path treated ANY non-empty,
non-`unavailable` new_state as a ring, so the restore fired a full screening
session with no visitor present -> a phantom "someone is at the door"
announcement roughly once per morning (when the integration reconnects).

The guard discriminates a live ring (timestamp ~= now) from a restore (stale
timestamp) on freshness, not on old_state (which the restore makes unreliable).

These tests never trigger a real screening session — `_trigger_doorbell_screening`
is replaced with a recorder, so nothing touches a speaker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from glados.autonomy.agents.ha_sensor_watcher import (
    EntityCategory,
    HomeAssistantSensorSubagent,
)

DOORBELL_EID = "event.test_doorbell_doorbell"


def _make_subagent() -> HomeAssistantSensorSubagent:
    """Build a minimal sensor watcher without running __init__ (which loads
    config/announcements). We set only the attributes _process_ws_message
    touches on the doorbell path, and record screening triggers instead of
    firing them."""
    inst = object.__new__(HomeAssistantSensorSubagent)
    inst._MODE_ENTITIES = set()
    inst._entity_categories = {DOORBELL_EID: EntityCategory.ALERT}
    inst._vision_entities = {DOORBELL_EID: "front_door"}
    inst._detection_types = {DOORBELL_EID: "doorbell_ring"}
    inst._triggered: list[str] = []
    inst._trigger_doorbell_screening = lambda eid: inst._triggered.append(eid)  # type: ignore[assignment]
    return inst


def _state_changed_msg(old_state: str, new_state: str) -> dict:
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": DOORBELL_EID,
                "old_state": {"state": old_state},
                "new_state": {"state": new_state, "attributes": {}},
            },
        },
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ── helper: _is_fresh_doorbell_ring ──────────────────────────────────────

def test_fresh_timestamp_is_a_ring():
    sub = _make_subagent()
    assert sub._is_fresh_doorbell_ring(_iso(datetime.now(timezone.utc))) is True


def test_stale_restored_timestamp_is_not_a_ring():
    sub = _make_subagent()
    stale = _iso(datetime.now(timezone.utc) - timedelta(days=3))
    assert sub._is_fresh_doorbell_ring(stale) is False


def test_unavailable_unknown_empty_are_not_rings():
    sub = _make_subagent()
    assert sub._is_fresh_doorbell_ring("unavailable") is False
    assert sub._is_fresh_doorbell_ring("unknown") is False
    assert sub._is_fresh_doorbell_ring("") is False


def test_unparseable_state_is_not_a_ring():
    sub = _make_subagent()
    assert sub._is_fresh_doorbell_ring("WELCOME") is False


# ── integration: _process_ws_message doorbell fast path ──────────────────

def test_restore_transition_does_not_trigger_screening():
    """unavailable -> stale timestamp (the morning reconnect) must NOT screen."""
    sub = _make_subagent()
    stale = _iso(datetime.now(timezone.utc) - timedelta(days=3))
    sub._process_ws_message(_state_changed_msg("unavailable", stale))
    assert sub._triggered == []


def test_real_ring_triggers_screening():
    """A fresh-timestamp press must still screen normally."""
    sub = _make_subagent()
    fresh = _iso(datetime.now(timezone.utc))
    sub._process_ws_message(_state_changed_msg("2026-05-27T22:21:25.635+00:00", fresh))
    assert sub._triggered == [DOORBELL_EID]
