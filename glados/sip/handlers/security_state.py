"""IVR handler: 'security state'.

Alarm armed/disarmed, doors locked count, motion sensors firing.
Pure formatter; no LLM.
"""
from __future__ import annotations

from glados.sip.handlers import HandlerContext


def _entity_states(states, prefix: str) -> list[dict]:
    return [
        s for s in (states or [])
        if isinstance(s, dict) and str(s.get("entity_id", "")).startswith(prefix)
    ]


async def render(ctx: HandlerContext) -> str:
    states = ctx.ha_states or []

    # Alarm panels
    alarms = _entity_states(states, "alarm_control_panel.")
    if alarms:
        alarm = alarms[0]
        alarm_state = str(alarm.get("state", "unknown")).replace("_", " ")
        alarm_phrase = f"Alarm {alarm_state}."
    else:
        alarm_phrase = "No alarm panel configured."

    # Locks
    locks = _entity_states(states, "lock.")
    if locks:
        locked = sum(1 for s in locks if s.get("state") == "locked")
        unlocked = len(locks) - locked
        if unlocked == 0:
            lock_phrase = f"All {len(locks)} doors locked."
        else:
            lock_phrase = f"{locked} of {len(locks)} doors locked, {unlocked} unlocked."
    else:
        lock_phrase = "No locks configured."

    # Motion sensors firing
    motion = [
        s for s in _entity_states(states, "binary_sensor.")
        if s.get("attributes", {}).get("device_class") == "motion"
    ]
    motion_active = sum(1 for s in motion if s.get("state") == "on")
    if motion_active > 0:
        motion_phrase = f"{motion_active} motion sensor{'s' if motion_active != 1 else ''} active."
    else:
        motion_phrase = "No motion."

    return f"{alarm_phrase} {lock_phrase} {motion_phrase}"
