"""Tests for the four built-in IVR handlers (deterministic formatters)."""
from __future__ import annotations

import pytest

from glados.sip.handlers import HandlerContext, get_handler
from glados.sip.handlers import door_locks, doorbell_recent, house_status, security_state


# ---------------------------------------------------------------------------
# get_handler registry
# ---------------------------------------------------------------------------

def test_get_handler_returns_renderable_for_known_names() -> None:
    for name in ("house_status", "security_state", "door_locks", "doorbell_recent"):
        fn = get_handler(name)
        assert callable(fn)


def test_get_handler_unknown_raises_keyerror_with_helpful_message() -> None:
    with pytest.raises(KeyError, match="unknown IVR handler"):
        get_handler("does_not_exist")


# ---------------------------------------------------------------------------
# house_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_house_status_counts_lights_and_reports_climate() -> None:
    ctx = HandlerContext(
        ha_states=[
            {"entity_id": "light.kitchen", "state": "on"},
            {"entity_id": "light.bedroom", "state": "off"},
            {"entity_id": "light.living_room", "state": "on"},
            {"entity_id": "climate.main", "state": "heat",
             "attributes": {"current_temperature": 71}},
        ],
        audit_recent=[
            {"summary": "back door opened", "time_ago": "11 minutes ago"},
        ],
    )
    out = await house_status.render(ctx)
    assert "2 lights on" in out
    assert "climate at 71" in out
    assert "back door opened" in out
    assert "11 minutes ago" in out


@pytest.mark.asyncio
async def test_house_status_handles_no_climate() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "light.kitchen", "state": "on"},
    ])
    out = await house_status.render(ctx)
    assert "1 lights on" in out  # not pluralised; acceptable for terse phone audio
    assert "climate sensor not reporting" in out


@pytest.mark.asyncio
async def test_house_status_handles_no_audit() -> None:
    ctx = HandlerContext(ha_states=[])
    out = await house_status.render(ctx)
    assert "No recent events" in out


@pytest.mark.asyncio
async def test_house_status_with_empty_context() -> None:
    out = await house_status.render(HandlerContext())
    assert "0 lights on" in out


# ---------------------------------------------------------------------------
# security_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_state_alarm_armed() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "alarm_control_panel.house", "state": "armed_away"},
        {"entity_id": "lock.front_door", "state": "locked"},
        {"entity_id": "lock.back_door", "state": "locked"},
    ])
    out = await security_state.render(ctx)
    assert "armed away" in out
    assert "All 2 doors locked" in out


@pytest.mark.asyncio
async def test_security_state_partial_locks() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "alarm_control_panel.house", "state": "disarmed"},
        {"entity_id": "lock.front_door", "state": "locked"},
        {"entity_id": "lock.back_door", "state": "unlocked"},
        {"entity_id": "lock.garage", "state": "unlocked"},
    ])
    out = await security_state.render(ctx)
    assert "disarmed" in out
    assert "1 of 3 doors locked, 2 unlocked" in out


@pytest.mark.asyncio
async def test_security_state_motion_active() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "alarm_control_panel.house", "state": "disarmed"},
        {"entity_id": "binary_sensor.motion_kitchen", "state": "on",
         "attributes": {"device_class": "motion"}},
        {"entity_id": "binary_sensor.motion_office", "state": "off",
         "attributes": {"device_class": "motion"}},
    ])
    out = await security_state.render(ctx)
    assert "1 motion sensor active" in out


@pytest.mark.asyncio
async def test_security_state_no_motion() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "alarm_control_panel.house", "state": "disarmed"},
        {"entity_id": "binary_sensor.motion_office", "state": "off",
         "attributes": {"device_class": "motion"}},
    ])
    out = await security_state.render(ctx)
    assert "No motion" in out


# ---------------------------------------------------------------------------
# door_locks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_door_locks_lists_all_in_alphabetical_order() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "lock.front_door", "state": "locked",
         "attributes": {"friendly_name": "Front Door"}},
        {"entity_id": "lock.back_door", "state": "unlocked",
         "attributes": {"friendly_name": "Back Door"}},
        {"entity_id": "lock.garage", "state": "locked",
         "attributes": {"friendly_name": "Garage"}},
    ])
    out = await door_locks.render(ctx)
    # Alphabetical: Back Door, Front Door, Garage
    back_idx = out.index("Back Door")
    front_idx = out.index("Front Door")
    garage_idx = out.index("Garage")
    assert back_idx < front_idx < garage_idx
    assert "Back Door is unlocked" in out
    assert "Front Door is locked" in out
    assert "Garage is locked" in out


@pytest.mark.asyncio
async def test_door_locks_no_locks() -> None:
    out = await door_locks.render(HandlerContext(ha_states=[]))
    assert out == "No locks configured."


@pytest.mark.asyncio
async def test_door_locks_single_lock() -> None:
    ctx = HandlerContext(ha_states=[
        {"entity_id": "lock.front_door", "state": "locked",
         "attributes": {"friendly_name": "Front Door"}},
    ])
    out = await door_locks.render(ctx)
    assert out == "Front Door is locked."


# ---------------------------------------------------------------------------
# doorbell_recent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doorbell_recent_lists_last_three() -> None:
    ctx = HandlerContext(doorbell_events=[
        {"time_ago": "5 minutes ago", "verdict": "delivery person"},
        {"time_ago": "1 hour ago", "verdict": "neighbor"},
        {"time_ago": "this morning", "verdict": "package drop-off"},
        {"time_ago": "yesterday", "verdict": "kids at door"},  # should be cut
    ])
    out = await doorbell_recent.render(ctx)
    assert "5 minutes ago" in out
    assert "neighbor" in out
    assert "package drop-off" in out
    assert "kids at door" not in out  # 4th event cut off


@pytest.mark.asyncio
async def test_doorbell_recent_no_events() -> None:
    out = await doorbell_recent.render(HandlerContext(doorbell_events=[]))
    assert out == "No recent doorbell events."


@pytest.mark.asyncio
async def test_doorbell_recent_single_event() -> None:
    ctx = HandlerContext(doorbell_events=[
        {"time_ago": "5 minutes ago", "verdict": "UPS"},
    ])
    out = await doorbell_recent.render(ctx)
    assert "One doorbell event" in out
    assert "UPS" in out
