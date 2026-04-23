"""
Background data fetcher for the HUB75 info panel.

Runs in a daemon thread, periodically reads:
  - Weather: from the on-disk JSON cache (no HTTP needed — the weather
    subagent already updates it)
  - Home status: from the HA REST API (configurable interval)

Updates an :class:`InfoPanelData` instance that the render loop reads.

This module is scaffolding for a future info-panel display mode.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from loguru import logger

from .info_renderer import InfoPanelData


class InfoFetcher:
    """Background fetcher for info panel data.

    Args:
        data: The shared :class:`InfoPanelData` to populate.
        weather_cache_path: Path to the weather cache JSON file.
        ha_url: Home Assistant base URL (e.g. ``http://homeassistant.local:8123``).
        ha_token: Long-lived access token for HA REST API.
        home_entities: List of ``(entity_id, ok_state)`` tuples to poll.
        weather_interval: Seconds between weather cache reads.
        home_interval: Seconds between HA entity polls.
    """

    def __init__(
        self,
        data: InfoPanelData,
        weather_cache_path: str = str(
            Path(os.environ.get("GLADOS_DATA", "/app/data")) / "weather_cache.json"
        ),
        ha_url: str = "",
        ha_token: str = "",
        home_entities: list[tuple[str, str]] | None = None,
        weather_interval: float = 60.0,
        home_interval: float = 45.0,
    ) -> None:
        self._data = data
        self._weather_path = Path(weather_cache_path)
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        # Operator supplies their own entity list via home_entities. The
        # shipped default is empty so no site-specific IDs live in code.
        self._home_entities = home_entities or []
        self._weather_interval = weather_interval
        self._home_interval = home_interval
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background fetch loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hub75-info-fetch",
        )
        self._thread.start()
        logger.info("HUB75: info fetcher started")

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._running = False

    # ── Main loop ──────────────────────────────────────────

    def _run(self) -> None:
        """Alternates between weather reads and HA polls."""
        # Immediate first fetch on startup
        self._fetch_weather()
        self._fetch_home()

        last_weather = time.monotonic()
        last_home = time.monotonic()

        while self._running:
            now = time.monotonic()

            if now - last_weather >= self._weather_interval:
                self._fetch_weather()
                last_weather = now

            if now - last_home >= self._home_interval:
                self._fetch_home()
                last_home = now

            time.sleep(5.0)  # Check intervals every 5 s

    # ── Fetch helpers ──────────────────────────────────────

    def _fetch_weather(self) -> None:
        """Read the weather cache JSON from disk (zero network overhead)."""
        try:
            if self._weather_path.exists():
                text = self._weather_path.read_text(encoding="utf-8")
                data = json.loads(text)
                self._data.update_weather(data)
                logger.debug("HUB75: weather cache refreshed")
        except Exception as exc:
            logger.debug("HUB75: weather cache read failed: {}", exc)

    def _fetch_home(self) -> None:
        """Fetch entity states from the HA REST API."""
        if not self._ha_token:
            return

        states: dict[str, str] = {}
        for entity_id, _ok_state in self._home_entities:
            try:
                url = f"{self._ha_url}/api/states/{entity_id}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._ha_token}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode())
                    states[entity_id] = payload.get("state", "unknown")
            except Exception as exc:
                logger.debug("HUB75: HA fetch {} failed: {}", entity_id, exc)
                states[entity_id] = "unknown"

        self._data.update_home(states)
        logger.debug("HUB75: home status refreshed ({} entities)", len(states))
