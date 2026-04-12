"""
Camera Watcher subagent for GLaDOS autonomy system.

Polls the GLaDOS Vision Validation Service for recent detection events
and notifies GLaDOS when significant camera activity occurs.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..subagent import Subagent, SubagentConfig, SubagentOutput


# Map severity strings to importance levels
SEVERITY_IMPORTANCE = {
    "alert": 0.9,
    "notable": 0.6,
    "routine": 0.2,
    "false_alarm": 0.0,
}


class CameraWatcherSubagent(Subagent):
    """
    Subagent that monitors camera detection events.

    Polls the vision validation service's /recent endpoint for new events
    and writes summaries to the slot store for GLaDOS autonomy tick prompts.
    """

    def __init__(
        self,
        config: SubagentConfig,
        vision_api_url: str = "http://localhost:8016",
        **kwargs,
    ) -> None:
        super().__init__(config, **kwargs)
        self._vision_api_url = vision_api_url.rstrip("/")
        self._last_event_ts: str | None = None
        self._consecutive_errors: int = 0

    def tick(self) -> SubagentOutput | None:
        """Poll vision service for recent events and report notable ones."""
        events = self._fetch_recent_events()
        if events is None:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                return SubagentOutput(
                    status="error",
                    summary="Camera vision service unreachable",
                    notify_user=False,
                    importance=0.1,
                )
            return None

        self._consecutive_errors = 0

        # Filter to only new events (after our watermark)
        new_events = self._filter_new_events(events)

        if not new_events:
            return SubagentOutput(
                status="done",
                summary="No new camera events",
                notify_user=False,
                importance=0.0,
                next_run=self._config.loop_interval_s,
            )

        # Update watermark to latest event
        self._last_event_ts = new_events[0].get("timestamp")

        # Find the most important new event
        most_important = max(
            new_events,
            key=lambda e: SEVERITY_IMPORTANCE.get(e.get("severity", "false_alarm"), 0.0),
        )

        severity = most_important.get("severity", "routine")
        importance = SEVERITY_IMPORTANCE.get(severity, 0.2)
        camera_id = most_important.get("camera_id", "unknown")
        description = most_important.get("description", "Unknown activity")
        objects = most_important.get("objects", [])

        # Build summary for autonomy tick prompt
        summary = f"Camera '{camera_id}': {description}"
        if len(new_events) > 1:
            summary += f" (+{len(new_events) - 1} more events)"

        # Build detailed report for get_report tool
        report = self._generate_report(new_events)

        # Notify GLaDOS for notable+ severity
        notify = severity in ("alert", "notable")

        return SubagentOutput(
            status="done",
            summary=summary,
            report=report,
            notify_user=notify,
            importance=importance,
            confidence=most_important.get("confidence", 0.5),
            next_run=self._config.loop_interval_s,
        )

    def _fetch_recent_events(self) -> list[dict] | None:
        """Fetch recent events from the vision service."""
        try:
            response = httpx.get(
                f"{self._vision_api_url}/recent",
                timeout=8.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("CameraWatcher: failed to fetch events: %s", exc)
            return None

    def _filter_new_events(self, events: list[dict]) -> list[dict]:
        """Filter events to only those after the last seen timestamp."""
        if self._last_event_ts is None:
            # First run — return events but don't spam GLaDOS with old ones
            # Just take the latest event if any
            return events[:1] if events else []

        new = []
        for event in events:
            ts = event.get("timestamp", "")
            if ts > self._last_event_ts:
                new.append(event)
            else:
                break  # Events are ordered newest first
        return new

    def _generate_report(self, events: list[dict]) -> str:
        """Generate a detailed markdown report of camera events."""
        lines = ["## Camera Activity Report", ""]

        for event in events[:10]:  # Cap at 10 most recent
            severity = event.get("severity", "unknown")
            camera = event.get("camera_id", "unknown")
            desc = event.get("description", "No description")
            objects = event.get("objects", [])
            confidence = event.get("confidence", 0)
            ts = event.get("timestamp", "")
            announced = event.get("announced", False)
            glados_text = event.get("glados_text")

            severity_icon = {
                "alert": "[!]",
                "notable": "[i]",
                "routine": "[.]",
                "false_alarm": "[ok]",
            }.get(severity, "[?]")

            lines.append(f"### {severity_icon} {camera} -- {severity.upper()}")
            lines.append(f"- **Time:** {ts}")
            lines.append(f"- **Description:** {desc}")
            if objects:
                lines.append(f"- **Objects:** {', '.join(objects)}")
            lines.append(f"- **Confidence:** {confidence:.0%}")
            lines.append(f"- **Announced:** {'Yes' if announced else 'No'}")
            if glados_text:
                lines.append(f"- **GLaDOS said:** {glados_text}")
            lines.append("")

        return "\n".join(lines)
