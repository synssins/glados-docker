"""IVR handler: 'recent doorbell events'.

Last 3 doorbell events with timestamps + screener verdicts. Pure
formatter; no LLM.
"""
from __future__ import annotations

from glados.sip.handlers import HandlerContext


async def render(ctx: HandlerContext) -> str:
    events = ctx.doorbell_events or []
    if not events:
        return "No recent doorbell events."

    recent = events[:3]
    parts: list[str] = []
    for ev in recent:
        when = ev.get("time_ago") or ev.get("timestamp", "recently")
        verdict = ev.get("verdict") or ev.get("description") or "ring"
        parts.append(f"{when}: {verdict}")

    if len(parts) == 1:
        return f"One doorbell event: {parts[0]}."
    body = "; ".join(parts)
    return f"Last {len(parts)} doorbell events. {body}."
