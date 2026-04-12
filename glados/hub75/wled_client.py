"""
WLED JSON API client for the HUB75 display.

Thin HTTP wrapper using ``urllib`` (stdlib only — no aiohttp dependency).
All methods are fire-and-forget: exceptions are logged, never raised.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

from loguru import logger

_TIMEOUT = 10  # seconds — ESP32 on USB power can take 4-5s to respond


class WledClient:
    """Lightweight client for the WLED JSON API."""

    def __init__(self, ip: str) -> None:
        self._ip = ip
        self._base = f"http://{ip}"

    # ── Public API ────────────────────────────────────────────

    def ping(self) -> tuple[bool, float]:
        """Check if WLED is reachable.

        Returns:
            ``(ok, latency_ms)`` tuple.
        """
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(f"{self._base}/json/info")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if resp.status == 200:
                    latency = (time.monotonic() - t0) * 1000
                    return True, round(latency, 1)
        except Exception as exc:
            logger.warning("WLED ping failed at {}: {}", self._ip, exc)
        return False, 0.0

    def set_brightness(self, bri: int) -> None:
        """Set global brightness (0–255)."""
        self._post_state({"bri": min(255, max(0, bri))})

    def set_preset(self, preset_id: int) -> None:
        """Activate a WLED preset by ID."""
        self._post_state({"ps": preset_id})

    def set_live_override(self, realtime_priority: bool) -> None:
        """Control whether DDP (realtime) or WLED effects take priority.

        Args:
            realtime_priority: ``True`` → DDP takes priority (lor=0).
                               ``False`` → WLED effects take priority (lor=1).
        """
        # lor=0: realtime (DDP) overrides effects
        # lor=1: effects override realtime
        self._post_state({"lor": 0 if realtime_priority else 1})

    def turn_on(self, brightness: int | None = None) -> None:
        """Turn the display on, optionally setting brightness."""
        payload: dict = {"on": True}
        if brightness is not None:
            payload["bri"] = min(255, max(0, brightness))
        self._post_state(payload)

    def turn_off(self) -> None:
        """Turn the display off."""
        self._post_state({"on": False})

    # ── Internal ──────────────────────────────────────────────

    def _post_state(self, payload: dict) -> None:
        """POST to /json/state with the given payload.

        Fire-and-forget: logs warning on any error, never raises.
        """
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self._base}/json/state",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT):
                pass
        except Exception as exc:
            logger.warning("WLED POST /json/state failed: {} — payload: {}", exc, payload)
