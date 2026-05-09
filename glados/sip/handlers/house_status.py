"""IVR handler: 'house status'.

One-paragraph summary of HA state: lights on, climate, most recent
audit event. Pure formatter; no LLM.
"""
from __future__ import annotations

from glados.sip.handlers import HandlerContext


async def render(ctx: HandlerContext) -> str:
    states = ctx.ha_states or []
    audit = ctx.audit_recent or []

    lights_on = sum(
        1 for s in states
        if isinstance(s, dict)
        and str(s.get("entity_id", "")).startswith("light.")
        and s.get("state") == "on"
    )

    climate = next(
        (s for s in states
         if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("climate.")),
        None,
    )
    if climate is not None:
        attrs = climate.get("attributes") or {}
        temp = attrs.get("current_temperature")
        climate_phrase = (
            f"climate at {int(temp)}" if isinstance(temp, (int, float)) else "climate steady"
        )
    else:
        climate_phrase = "climate sensor not reporting"

    if audit:
        most_recent = audit[0]
        summary = most_recent.get("summary") or most_recent.get("description") or "an event"
        when = most_recent.get("time_ago", "recently")
        last_event = f"Last event was {summary}, {when}."
    else:
        last_event = "No recent events."

    return f"All quiet. {lights_on} lights on, {climate_phrase}. {last_event}"
