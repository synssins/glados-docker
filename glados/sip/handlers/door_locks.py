"""IVR handler: 'door locks'.

Per-door lock state, ordered alphabetically by friendly name. Pure
formatter; no LLM.
"""
from __future__ import annotations

from glados.sip.handlers import HandlerContext


async def render(ctx: HandlerContext) -> str:
    states = ctx.ha_states or []
    locks = [
        s for s in states
        if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("lock.")
    ]
    if not locks:
        return "No locks configured."

    # Order by friendly name (or entity_id as fallback)
    def _sort_key(s: dict) -> str:
        return str(s.get("attributes", {}).get("friendly_name") or s.get("entity_id", ""))

    locks.sort(key=_sort_key)

    parts: list[str] = []
    for lock in locks:
        name = lock.get("attributes", {}).get("friendly_name") or lock.get("entity_id")
        # Strip the "lock." prefix if friendly_name is missing
        name = str(name).replace("lock.", "").replace("_", " ")
        state = lock.get("state", "unknown")
        if state == "locked":
            parts.append(f"{name} is locked")
        elif state == "unlocked":
            parts.append(f"{name} is unlocked")
        else:
            parts.append(f"{name} is {state}")

    if len(parts) == 1:
        return parts[0] + "."
    return ", ".join(parts[:-1]) + ", and " + parts[-1] + "."
