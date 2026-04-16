"""
Home Assistant Sensor Watcher subagent for GLaDOS autonomy system.

Connects to HA's WebSocket API for real-time state_changed events.
A background async thread handles the persistent WebSocket connection while
the synchronous tick() method drains accumulated events and reports
significant ones to the autonomy system.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from ..subagent import Subagent, SubagentConfig, SubagentOutput


class EntityCategory(Enum):
    """Importance categories for monitored entities."""

    ALERT = 0.9
    NOTABLE = 0.6
    ROUTINE = 0.3


# Human-readable descriptions for common HA state transitions
STATE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "cover": {
        "open": "opened",
        "opening": "opening",
        "closed": "closed",
        "closing": "closing",
    },
    "lock": {
        "locked": "locked",
        "unlocked": "unlocked",
    },
    "binary_sensor": {
        "on": "detected",
        "off": "cleared",
    },
    "person": {
        "home": "arrived home",
        "not_home": "left home",
    },
}


@dataclass
class HAStateEvent:
    """A parsed state-change event from Home Assistant."""

    entity_id: str
    old_state: str
    new_state: str
    friendly_name: str
    category: EntityCategory
    timestamp: float


class HomeAssistantSensorSubagent(Subagent):
    """
    Subagent that monitors Home Assistant entity state changes in real-time.

    Uses the HA WebSocket API to subscribe to state_changed events. A daemon
    thread runs the async WebSocket listener and enqueues events into a
    thread-safe queue. The synchronous tick() method drains the queue and
    reports significant changes.
    """

    # GLaDOS-style announcement templates by domain
    ANNOUNCEMENT_TEMPLATES: dict[str, dict[str, str]] = {
        "cover": {
            "open": "the {name} has been opened.",
            "closed": "the {name} has been closed.",
            "opening": "the {name} is opening.",
            "closing": "the {name} is closing.",
        },
        "lock": {
            "locked": "the {name} has been locked.",
            "unlocked": "the {name} has been unlocked.",
        },
        "binary_sensor": {
            "on": "motion detected on {name}.",
            "off": "motion cleared on {name}.",
        },
        "person": {
            "home": "{name} has arrived home.",
            "not_home": "{name} has left the facility.",
        },
    }

    def __init__(
        self,
        config: SubagentConfig,
        ha_ws_url: str = "ws://192.168.1.104:8123/api/websocket",
        ha_token: str = "",
        entity_categories: dict[str, str] | None = None,
        debounce_seconds: float = 5.0,
        min_importance: float = 0.0,
        tts_queue: queue.Queue[str] | None = None,
        vision_api_url: str = "http://localhost:8016",
        vision_entities: dict[str, str] | None = None,
        announce_url: str = "http://localhost:8015/announce",
        announcements_yaml: str = str(Path(
            os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
        ) / "announcements.yaml"),
        smart_detection: Any | None = None,
        mode_change_callback: Any | None = None,
        pet_outdoor_monitor: dict | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._ha_ws_url = ha_ws_url
        self._ha_token = ha_token
        self._ha_url = ha_ws_url.replace("ws://", "http://").replace("wss://", "https://").split("/api/")[0]
        self._debounce_seconds = debounce_seconds
        self._min_importance = min_importance
        self._tts_queue = tts_queue  # Fallback TTS injection for entities not in announcements
        self._vision_api_url = vision_api_url.rstrip("/")
        self._announce_url = announce_url
        self._announcements_yaml = announcements_yaml
        # Map motion entity_id -> vision camera_id for triggering snapshot analysis
        self._vision_entities: dict[str, str] = vision_entities or {}
        # Smart detection: detection_type per entity, populated by auto-discovery
        self._detection_types: dict[str, str] = {}
        # Smart detection config (SmartDetectionConfig or None)
        self._smart_detection = smart_detection

        # Pre-generated maintenance WAVs — paths from centralized config
        try:
            from glados.core.config_store import cfg as _cfg
            self._maintenance_audio_dir = Path(_cfg.audio.announcements_dir) / "maintenance"
            self._serve_dir = Path(_cfg.audio.ha_output_dir)
            self._serve_host = _cfg.serve_host
            self._serve_port = _cfg.serve_port
            _me = _cfg.mode_entities
            self._eid_maintenance = _me.maintenance_mode
            self._eid_maintenance_speaker = _me.maintenance_speaker
            self._eid_silent = _me.silent_mode
        except Exception:
            _audio = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
            self._maintenance_audio_dir = _audio / "glados_announcements" / "maintenance"
            self._serve_dir = _audio / "glados_ha"
            self._serve_host = "192.168.1.75"
            self._serve_port = 5051
            self._eid_maintenance = "input_boolean.glados_maintenance_mode"
            self._eid_maintenance_speaker = "input_text.glados_maintenance_speaker"
            self._eid_silent = "input_boolean.glados_silent_mode"
        self._MODE_ENTITIES = {
            self._eid_maintenance,
            self._eid_maintenance_speaker,
            self._eid_silent,
        }

        # ── Dynamic mode management ──────────────────────────────────
        self._mode_change_callback = mode_change_callback
        self._maintenance_mode: bool = False
        self._maintenance_speaker: str = ""
        self._silent_mode: bool = False
        self._initial_mode_fetch: bool = False  # Suppress audio during initial state load
        self._maintenance_entered_at: float = 0.0
        self._maintenance_last_reminder: float = 0.0
        self._maintenance_reminder_interval_s: float = 3600.0  # 1 hour
        self._maintenance_auto_expiry_s: float = 8 * 3600.0  # 8 hours

        # Load announcement config: maps entity_id -> scenario name
        self._announce_entities: dict[str, str] = {}
        # Goodnight trigger: entity_id -> {security_check, delay_s}
        self._goodnight_trigger: dict[str, dict] = {}
        # Lockdown config: loaded from announcements.yaml lockdown scenario
        self._lockdown_config: dict | None = None
        self._lockdown_last_fired: float = 0.0
        self._lockdown_cooldown_s: float = 300.0  # 5 min cooldown
        self._load_announcement_entities()

        # ── Pet outdoor cold weather monitor ──────────────────────────
        self._pet_monitor_config: dict | None = pet_outdoor_monitor
        self._pet_outdoor_since: float | None = None   # timestamp when animal first detected
        self._pet_alert_fired: bool = False             # prevent re-alerting
        self._pet_alert_cooldown_until: float = 0.0     # cooldown after alert
        # Derive the animal detection entity from pet monitor camera config
        self._pet_animal_entity: str | None = None
        if self._pet_monitor_config and self._pet_monitor_config.get("enabled"):
            logger.info("HA Sensor: pet outdoor monitor enabled for '{}'",
                        self._pet_monitor_config.get("pet_name", "pet"))

        # Build entity -> category mapping from config
        self._entity_categories: dict[str, EntityCategory] = {}
        if entity_categories:
            for entity_id, cat_name in entity_categories.items():
                try:
                    self._entity_categories[entity_id] = EntityCategory[cat_name.upper()]
                except KeyError:
                    logger.warning(
                        "HA Sensor: unknown category '{}' for {}, defaulting to ROUTINE",
                        cat_name, entity_id,
                    )
                    self._entity_categories[entity_id] = EntityCategory.ROUTINE

        # Thread-safe event queue (bridge between async WS and sync tick)
        self._event_queue: queue.Queue[HAStateEvent] = queue.Queue(maxsize=200)

        # Debounce: last event time per entity_id
        self._last_event_time: dict[str, float] = {}

        # Person arrival cache: person entity_id -> timestamp when they last arrived home.
        # Populated from WebSocket state_changed events (before the whitelist filter)
        # so we never miss a person arrival, even due to race conditions with the REST API.
        self._person_arrival_cache: dict[str, float] = {}

        # Vision in-flight guard: camera_ids currently being analyzed.
        # Prevents multiple simultaneous requests to the vision service for
        # the same camera (e.g., person_detected + motion_detected fire
        # within milliseconds and would otherwise both trigger analysis).
        self._vision_in_flight: set[str] = set()
        self._vision_in_flight_lock = threading.Lock()

        # WebSocket thread control
        self._ws_thread: threading.Thread | None = None
        self._ws_stop_event = asyncio.Event()
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._consecutive_errors = 0

        # Pending output: holds significant events until autonomy loop consumes them.
        # The 5s tick interval is faster than the 30s autonomy cycle, so we must
        # persist events across multiple ticks to ensure the autonomy dispatch sees them.
        self._pending_output: SubagentOutput | None = None
        self._pending_since: float = 0.0
        self._pending_max_age_s: float = 120.0  # hold for up to 120s (must exceed autonomy cooldown + tick interval)

    def on_start(self) -> None:
        """Spawn the WebSocket listener daemon thread and run smart detection discovery."""
        # Auto-discover detection entities for configured cameras
        if self._smart_detection and self._smart_detection.enabled:
            self._discover_detection_entities()

        # Fetch initial mode state from HA (maintenance/silent)
        self._fetch_initial_mode_state()

        self._ws_thread = threading.Thread(
            target=self._ws_thread_entry,
            name="HA-Sensor-WS",
            daemon=True,
        )
        self._ws_thread.start()
        logger.success("HA Sensor: WebSocket listener thread started")

    def on_stop(self) -> None:
        """Signal the WebSocket thread to stop and wait for it."""
        if self._ws_loop and not self._ws_loop.is_closed():
            self._ws_loop.call_soon_threadsafe(self._ws_stop_event.set)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5.0)
            if self._ws_thread.is_alive():
                logger.warning("HA Sensor: WebSocket thread did not stop within timeout")
        logger.info("HA Sensor: stopped")

    def tick(self) -> SubagentOutput | None:
        """Drain the event queue and report significant state changes."""
        # ── Pet outdoor cold weather check ─────────────────────────────
        self._check_pet_outdoor()

        # ── Maintenance mode timer checks ────────────────────────────
        if self._maintenance_mode and self._maintenance_entered_at > 0:
            now = time.time()
            elapsed = now - self._maintenance_entered_at

            # Auto-expiry
            if elapsed >= self._maintenance_auto_expiry_s:
                logger.success(
                    "HA Sensor: maintenance mode auto-expired after {:.1f}h",
                    elapsed / 3600,
                )
                self._auto_disable_maintenance()

            # Hourly reminder
            elif now - self._maintenance_last_reminder >= self._maintenance_reminder_interval_s:
                self._maintenance_last_reminder = now
                hours = int(elapsed / 3600)
                minutes = int((elapsed % 3600) / 60)
                self._play_maintenance_reminder(hours, minutes)

        events: list[HAStateEvent] = []
        while not self._event_queue.empty():
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break

        # If we have new events, process them
        if events:
            # Deduplicate: keep only the latest event per entity_id
            latest_by_entity: dict[str, HAStateEvent] = {}
            for event in events:
                latest_by_entity[event.entity_id] = event

            deduped = list(latest_by_entity.values())

            # Filter by minimum importance
            significant = [
                e for e in deduped
                if e.category.value >= self._min_importance
            ]

            if significant:
                # Sort by importance (highest first)
                significant.sort(key=lambda e: e.category.value, reverse=True)

                # Build summary from the most important event
                top = significant[0]
                summary = self._describe_event(top)
                if len(significant) > 1:
                    summary += f" (+{len(significant) - 1} more)"

                # Build detailed report
                report = self._generate_report(significant)

                # Notify for ALERT and NOTABLE events
                max_importance = max(e.category.value for e in significant)
                notify = max_importance >= EntityCategory.NOTABLE.value

                output = SubagentOutput(
                    status="done",
                    summary=summary,
                    report=report,
                    notify_user=notify,
                    importance=max_importance,
                    confidence=0.9,
                    next_run=self._config.loop_interval_s,
                )

                # Persist significant events so the autonomy loop (30s cycle)
                # has time to read them before they're overwritten
                self._pending_output = output
                self._pending_since = time.time()
                return output

        # No new events — return the pending output if it hasn't expired,
        # so the autonomy loop's next tick still sees the event
        if self._pending_output is not None:
            age = time.time() - self._pending_since
            if age < self._pending_max_age_s:
                # Re-emit but don't re-notify (autonomy loop deduplicates)
                return self._pending_output
            # Expired — clear it
            self._pending_output = None

        status = "connected" if self._connected else "disconnected"
        return SubagentOutput(
            status=status,
            summary=f"HA Sensor: {status}, no new events",
            notify_user=False,
            importance=0.0,
            next_run=self._config.loop_interval_s,
        )

    # ── WebSocket thread ──────────────────────────────────────────────

    def _ws_thread_entry(self) -> None:
        """Entry point for the WebSocket daemon thread."""
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        try:
            self._ws_loop.run_until_complete(self._ws_main())
        except Exception as exc:
            logger.error("HA Sensor: WebSocket thread crashed: {}", exc)
        finally:
            self._ws_loop.close()

    async def _ws_main(self) -> None:
        """Main async WebSocket handler with auto-reconnect."""
        try:
            import websockets
            from websockets.asyncio.client import connect
        except ImportError:
            logger.error("HA Sensor: 'websockets' package not installed")
            return

        backoff = 1.0
        max_backoff = 60.0

        while not self._ws_stop_event.is_set():
            try:
                logger.info("HA Sensor: connecting to {}", self._ha_ws_url)
                async with connect(self._ha_ws_url) as ws:
                    # Step 1: Receive auth_required
                    auth_msg = json.loads(await ws.recv())
                    if auth_msg.get("type") != "auth_required":
                        logger.error("HA Sensor: unexpected first message: {}", auth_msg)
                        continue

                    # Step 2: Send auth token
                    await ws.send(json.dumps({
                        "type": "auth",
                        "access_token": self._ha_token,
                    }))

                    # Step 3: Check auth result
                    auth_result = json.loads(await ws.recv())
                    if auth_result.get("type") != "auth_ok":
                        logger.error(
                            "HA Sensor: authentication failed: {}",
                            auth_result.get("message", "unknown error"),
                        )
                        await asyncio.sleep(30)
                        continue

                    logger.success("HA Sensor: authenticated to Home Assistant")
                    self._connected = True
                    self._consecutive_errors = 0
                    backoff = 1.0

                    # Step 4: Subscribe to state_changed events
                    await ws.send(json.dumps({
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }))

                    sub_result = json.loads(await ws.recv())
                    if not sub_result.get("success"):
                        logger.error("HA Sensor: subscription failed: {}", sub_result)
                        continue

                    logger.success("HA Sensor: subscribed to state_changed events")

                    # Step 5: Listen for events
                    async for raw in ws:
                        if self._ws_stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            self._process_ws_message(msg)
                        except json.JSONDecodeError:
                            logger.warning("HA Sensor: invalid JSON from WS")
                        except Exception as exc:
                            logger.warning("HA Sensor: error processing message: {}", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected = False
                self._consecutive_errors += 1
                logger.warning(
                    "HA Sensor: WebSocket connection lost ({}), reconnecting in {:.0f}s",
                    exc, backoff,
                )
                # Wait with stop-event awareness
                try:
                    await asyncio.wait_for(
                        self._wait_for_stop(),
                        timeout=backoff,
                    )
                    break  # Stop event was set
                except asyncio.TimeoutError:
                    pass  # Normal timeout, reconnect
                backoff = min(backoff * 2, max_backoff)

        self._connected = False
        logger.info("HA Sensor: WebSocket listener shutting down")

    async def _wait_for_stop(self) -> None:
        """Await until the stop event is set."""
        while not self._ws_stop_event.is_set():
            await asyncio.sleep(0.5)

    def _process_ws_message(self, msg: dict) -> None:
        """Parse a WebSocket message and enqueue if it's a relevant state change."""
        if msg.get("type") != "event":
            return

        event_data = msg.get("event", {})
        if event_data.get("event_type") != "state_changed":
            return

        data = event_data.get("data", {})
        entity_id = data.get("entity_id", "")

        # Track person arrivals BEFORE whitelist filter — we need this for
        # smart detection suppression even if the person entity isn't monitored.
        if entity_id.startswith("person."):
            new_person_state = (data.get("new_state") or {}).get("state", "")
            old_person_state = (data.get("old_state") or {}).get("state", "")
            if new_person_state == "home" and old_person_state != "home":
                self._person_arrival_cache[entity_id] = time.time()
                logger.info(
                    "HA Sensor: person arrival cached: {} (for suppression)",
                    entity_id,
                )
                # Check if this arrival completes all-persons-home for lockdown
                self._maybe_trigger_lockdown()

        # ── Mode entity handling (always processed, never filtered) ──
        if entity_id in self._MODE_ENTITIES:
            new_mode_state = (data.get("new_state") or {}).get("state", "")
            self._handle_mode_change(entity_id, new_mode_state)
            return

        # Only process whitelisted entities
        if entity_id not in self._entity_categories:
            return

        old_state_obj = data.get("old_state") or {}
        new_state_obj = data.get("new_state") or {}
        old_state = old_state_obj.get("state", "unknown")
        new_state = new_state_obj.get("state", "unknown")

        # Skip if state didn't actually change
        if old_state == new_state:
            return

        # Skip unavailable/unknown transitions
        if new_state in ("unavailable", "unknown") or old_state in ("unavailable", "unknown"):
            return

        # Debounce: skip if we reported this entity too recently
        now = time.time()
        last_time = self._last_event_time.get(entity_id, 0.0)
        if now - last_time < self._debounce_seconds:
            return

        self._last_event_time[entity_id] = now

        # Extract friendly name
        friendly_name = new_state_obj.get("attributes", {}).get(
            "friendly_name", entity_id
        )

        # ── Pet outdoor monitor: intercept animal detections for the monitored camera ──
        # The pet monitor handles animal detections separately (duration + temp check)
        # instead of sending them through the vision snapshot pipeline.
        if entity_id == self._pet_animal_entity:
            if new_state == "on":
                if self._pet_outdoor_since is None:
                    self._pet_outdoor_since = time.time()
                    logger.info(
                        "Pet monitor: animal detected on '{}', starting timer",
                        self._pet_monitor_config.get("camera", "?"),
                    )
            elif new_state == "off":
                if self._pet_outdoor_since is not None:
                    elapsed = (time.time() - self._pet_outdoor_since) / 60.0
                    logger.info(
                        "Pet monitor: animal detection cleared after {:.1f}min",
                        elapsed,
                    )
                self._pet_outdoor_since = None
                self._pet_alert_fired = False
            return  # Do NOT pass to vision pipeline

        # Vision-linked entities (motion sensors and smart detection) are handled
        # entirely by the vision pipeline. Skip the normal event queue and
        # direct TTS for BOTH "on" (triggers snapshot) and "off" (ignored).
        if entity_id in self._vision_entities:
            # Doorbell ring events use event.* entities which change timestamps,
            # not on/off. Trigger the doorbell screening system instead of vision.
            if self._detection_types.get(entity_id) == "doorbell_ring":
                self._trigger_doorbell_screening(entity_id)
                return

            if new_state == "on":
                # Suppression check: skip if a known person recently arrived home
                if self._check_suppression(entity_id):
                    det_type = self._detection_types.get(entity_id, "unknown")
                    logger.info(
                        "HA Sensor: suppressed {} (type={}, person recently arrived)",
                        entity_id, det_type,
                    )
                    return

                camera_id = self._vision_entities[entity_id]
                detection_type = self._detection_types.get(entity_id)
                logger.success(
                    "HA Sensor: detection on {} -> vision pipeline (camera '{}', type='{}')",
                    entity_id, camera_id, detection_type or "motion",
                )
                self._trigger_vision_analysis(camera_id, entity_id, detection_type)
            else:
                logger.debug("HA Sensor: ignoring {} state '{}' (vision entity)", entity_id, new_state)
            return

        event = HAStateEvent(
            entity_id=entity_id,
            old_state=old_state,
            new_state=new_state,
            friendly_name=friendly_name,
            category=self._entity_categories[entity_id],
            timestamp=now,
        )

        # Pre-generated announcement for entities in announcements.yaml.
        # Falls back to direct TTS injection for entities not in the config.
        # When a pre-generated announcement handles the event, we skip the
        # event queue to prevent the autonomy LLM from generating a duplicate
        # spoken response. The pre-generated WAVs already provide variety.
        if entity_id in self._goodnight_trigger:
            self._trigger_goodnight_check(entity_id)
        elif entity_id in self._announce_entities:
            scenario = self._announce_entities[entity_id]
            # Skip transitional states (e.g. "locking", "unlocking", "opening", "closing")
            # — only announce final states that have pre-generated audio.
            if new_state in ("locking", "unlocking", "opening", "closing"):
                logger.debug("HA Sensor: skipping transitional state '{}' for {}", new_state, entity_id)
            else:
                self._trigger_announcement(scenario, entity_id, new_state)
                return  # Pre-generated announcement handles it — skip event queue
        elif self._tts_queue is not None:
            announcement = self._build_announcement(event)
            if announcement:
                try:
                    self._tts_queue.put_nowait(announcement)
                    logger.success("HA Sensor: direct TTS announcement: {}", announcement)
                except queue.Full:
                    logger.warning("HA Sensor: TTS queue full, dropping announcement")

        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            logger.warning("HA Sensor: event queue full, dropping event for {}", entity_id)

    # ── Smart detection auto-discovery ──────────────────────────────

    def _discover_detection_entities(self) -> None:
        """Query HA to auto-discover detection binary_sensors for configured cameras.

        For each camera entity (e.g., camera.g4_doorbell_high), derives the device
        prefix (g4_doorbell) and finds matching binary_sensor.*_detected entities.
        Populates _vision_entities, _detection_types, and _entity_categories.

        Also discovers event.* entities (e.g., doorbell ring) for configured cameras.
        """
        sd = self._smart_detection
        skip_types = set(getattr(sd, "skip_detection_types", []))
        discovered = 0

        try:
            resp = httpx.get(
                f"{self._ha_url}/api/states",
                headers={"Authorization": f"Bearer {self._ha_token}"},
                timeout=15.0,
            )
            resp.raise_for_status()
            all_entity_ids = [s["entity_id"] for s in resp.json()]
        except Exception as e:
            logger.error("Smart detection: failed to query HA API for auto-discovery: {}", e)
            return

        for camera_name, cam_cfg in sd.cameras.items():
            # Derive device prefix: camera.g4_doorbell_high -> g4_doorbell
            cam_entity = cam_cfg.entity
            prefix = cam_entity.replace("camera.", "").replace("_high", "")

            for eid in all_entity_ids:
                # Match binary_sensor.*_detected entities by device prefix
                if eid.startswith("binary_sensor.") and eid.endswith("_detected"):
                    sensor_name = eid.replace("binary_sensor.", "")
                    # Check if this sensor belongs to this camera's device
                    # Handles both exact prefix (g4_doorbell_person_detected)
                    # and alternate prefixes (front_doorbell_speaking_detected)
                    if prefix not in sensor_name:
                        continue

                    # Extract detection type: g4_doorbell_person_detected -> person
                    # Remove the prefix and "_detected" suffix
                    remainder = sensor_name
                    if remainder.startswith(prefix + "_"):
                        remainder = remainder[len(prefix) + 1:]
                    remainder = remainder.replace("_detected", "")
                    det_type = remainder

                    # Skip audio-only detection types
                    if det_type in skip_types:
                        logger.debug("Smart detection: skipping audio-only {} ({})", eid, det_type)
                        continue

                    # Register in vision pipeline
                    self._vision_entities[eid] = camera_name
                    self._detection_types[eid] = det_type
                    category_name = sd.category_overrides.get(det_type, sd.default_category)
                    try:
                        self._entity_categories[eid] = EntityCategory[category_name.upper()]
                    except KeyError:
                        self._entity_categories[eid] = EntityCategory.ALERT
                    discovered += 1
                    logger.success(
                        "Smart detection: {} -> camera '{}', type='{}', category={}",
                        eid, camera_name, det_type, category_name,
                    )

                # Match event.*_doorbell entities (doorbell ring button press)
                elif eid.startswith("event.") and eid.endswith("_doorbell") and prefix in eid:
                    if eid not in self._vision_entities:
                        self._vision_entities[eid] = camera_name
                        self._detection_types[eid] = "doorbell_ring"
                        self._entity_categories[eid] = EntityCategory.ALERT
                        discovered += 1
                        logger.success(
                            "Smart detection: {} -> camera '{}', type='doorbell_ring'",
                            eid, camera_name,
                        )

        logger.success("Smart detection: auto-discovered {} detection entities across {} cameras",
                       discovered, len(sd.cameras))

        # Resolve pet outdoor monitor animal entity from discovered entities
        if self._pet_monitor_config and self._pet_monitor_config.get("enabled"):
            pet_camera = self._pet_monitor_config.get("camera", "")
            for eid, cam_name in self._vision_entities.items():
                if cam_name == pet_camera and self._detection_types.get(eid) == "animal":
                    self._pet_animal_entity = eid
                    logger.success(
                        "Pet outdoor monitor: linked to entity {} (camera '{}')",
                        eid, cam_name,
                    )
                    break
            if not self._pet_animal_entity:
                logger.warning(
                    "Pet outdoor monitor: no animal detection entity found for camera '{}'. "
                    "The entity may not be registered in HA yet.",
                    pet_camera,
                )

    def _check_suppression(self, entity_id: str) -> bool:
        """Check if a detection event should be suppressed due to person arrival.

        Returns True if the event should be suppressed (a known person
        recently arrived home within the configured time window).
        """
        if not self._smart_detection or not self._smart_detection.suppression:
            return False

        det_type = self._detection_types.get(entity_id)
        if not det_type:
            return False

        supp = self._smart_detection.suppression.get(det_type)
        if not supp:
            return False

        check_persons = supp.get("check_persons", [])
        window_minutes = supp.get("window_minutes", 10)
        return self._person_recently_arrived(check_persons, window_minutes)

    def _person_recently_arrived(self, person_entities: list[str], window_minutes: int) -> bool:
        """Check if any listed person arrived home within the suppression window.

        Uses the local WebSocket event cache first (populated in real-time from
        state_changed events), which avoids the race condition where the doorbell
        detection fires before HA's REST API reflects the person arrival.

        Falls back to querying the HA REST API if no cached arrival is found.
        """
        from datetime import datetime, timezone

        now = time.time()
        window_seconds = window_minutes * 60

        # 1) Check local cache first — populated from WebSocket events in real-time.
        #    This catches arrivals that happen simultaneously with detections.
        for person_id in person_entities:
            cached_arrival = self._person_arrival_cache.get(person_id)
            if cached_arrival and (now - cached_arrival) < window_seconds:
                elapsed = now - cached_arrival
                logger.info(
                    "HA Sensor: suppression hit (cache): {} arrived {:.0f}s ago (within {}min window)",
                    person_id, elapsed, window_minutes,
                )
                return True

        # 2) Fallback: query HA REST API (handles arrivals that occurred before
        #    this service started, when we had no WebSocket connection yet).
        cutoff = datetime.now(timezone.utc).timestamp() - window_seconds

        for person_id in person_entities:
            try:
                resp = httpx.get(
                    f"{self._ha_url}/api/states/{person_id}",
                    headers={"Authorization": f"Bearer {self._ha_token}"},
                    timeout=5.0,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("state") == "home":
                    last_changed = datetime.fromisoformat(data["last_changed"]).timestamp()
                    if last_changed > cutoff:
                        elapsed = datetime.now(timezone.utc).timestamp() - last_changed
                        logger.info(
                            "HA Sensor: suppression hit (REST): {} arrived {:.0f}s ago (within {}min window)",
                            person_id, elapsed, window_minutes,
                        )
                        # Backfill the cache so future checks are instant
                        self._person_arrival_cache[person_id] = last_changed
                        return True
            except Exception as e:
                logger.warning("HA Sensor: suppression check failed for {}: {}", person_id, e)

        return False

    # ── Announcement system ─────────────────────────────────────────

    def _load_announcement_entities(self) -> None:
        """Load announcements.yaml and build entity_id -> scenario mapping."""
        try:
            import yaml
            with open(self._announcements_yaml, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except Exception as exc:
            logger.warning("HA Sensor: could not load announcements.yaml: {}", exc)
            return

        for scenario_name, scenario in config.get("scenarios", {}).items():
            trigger = scenario.get("trigger")

            # Lockdown scenario: triggered by person arrivals, not a specific entity
            if trigger == "sensor_watcher" and scenario.get("person_entities"):
                person_eids = scenario["person_entities"]
                entity_names = [e.get("name") for e in scenario.get("entities", [])]
                self._lockdown_config = {
                    "scenario": scenario_name,
                    "person_entities": person_eids,
                    "lock_entity": scenario.get("lock_entity"),
                    "lock_pre_delay_s": scenario.get("lock_pre_delay_s", 2.5),
                    "lockdown_delay_s": scenario.get("lockdown_delay_s", 1200),
                    "entity_names": entity_names,
                }
                logger.success(
                    "HA Sensor: lockdown config loaded — persons={}, lock={}",
                    person_eids, scenario.get("lock_entity"),
                )
                continue

            # Goodnight-style triggers: sensor_watcher monitors a trigger entity
            # and checks security state before announcing
            if trigger == "sensor_watcher" and "trigger_entity" in scenario:
                trigger_eid = scenario["trigger_entity"]
                security_check = {}
                for item in scenario.get("security_check", []):
                    for eid, expected in item.items():
                        security_check[eid] = expected
                self._goodnight_trigger[trigger_eid] = {
                    "scenario": scenario_name,
                    "security_check": security_check,
                    "delay_s": scenario.get("trigger_delay_s", 3),
                }
                self._announce_entities[trigger_eid] = scenario_name
                logger.success(
                    "HA Sensor: goodnight trigger on {} (checks {} entities)",
                    trigger_eid, len(security_check),
                )
                continue

            # Skip scenarios triggered by HA automations or vision (not sensor watcher)
            if trigger in ("ha_automation", "vision"):
                continue
            for entity in scenario.get("entities", []):
                eid = entity.get("entity_id")
                if eid:
                    self._announce_entities[eid] = scenario_name

        if self._announce_entities:
            logger.success(
                "HA Sensor: loaded {} announcement entities: {}",
                len(self._announce_entities),
                list(self._announce_entities.keys()),
            )

    def _trigger_announcement(self, scenario: str, entity_id: str, state: str) -> None:
        """Call the /announce endpoint in a background thread."""
        def _do_announce() -> None:
            try:
                response = httpx.post(
                    self._announce_url,
                    json={
                        "scenario": scenario,
                        "entity_id": entity_id,
                        "state": state,
                    },
                    timeout=15.0,
                )
                response.raise_for_status()
                result = response.json()
                logger.success(
                    "HA Sensor: announcement played: scenario={}, base={}, followups={}",
                    scenario,
                    result.get("base", "?"),
                    result.get("followups", []),
                )
            except Exception as exc:
                logger.warning(
                    "HA Sensor: announcement failed for {} ({}): {}",
                    entity_id, scenario, exc,
                )
                # Fallback to direct TTS if announcement fails
                if self._tts_queue is not None:
                    fallback = f"{entity_id.split('.')[-1].replace('_', ' ')} changed to {state}."
                    try:
                        self._tts_queue.put_nowait(fallback)
                        logger.info("HA Sensor: fallback TTS: {}", fallback)
                    except queue.Full:
                        pass

        threading.Thread(target=_do_announce, name="HA-Announce", daemon=True).start()

    def _trigger_goodnight_check(self, trigger_entity_id: str) -> None:
        """Check all doors/locks and announce secure or insecure."""
        config = self._goodnight_trigger[trigger_entity_id]
        scenario = config["scenario"]
        security_check = config["security_check"]
        delay_s = config["delay_s"]

        def _do_check() -> None:
            try:
                # Brief delay so lights-off automation finishes first
                if delay_s > 0:
                    time.sleep(delay_s)

                # Query HA REST API for each entity's current state
                ha_url = self._ha_ws_url.replace("ws://", "http://").replace("/api/websocket", "")
                headers = {"Authorization": f"Bearer {self._ha_token}"}
                all_secure = True

                for eid, expected_state in security_check.items():
                    try:
                        resp = httpx.get(
                            f"{ha_url}/api/states/{eid}",
                            headers=headers,
                            timeout=5.0,
                        )
                        resp.raise_for_status()
                        actual = resp.json().get("state", "unknown")
                        if actual != expected_state:
                            all_secure = False
                            logger.warning(
                                "HA Sensor: goodnight check — {} is '{}' (expected '{}')",
                                eid, actual, expected_state,
                            )
                    except Exception as exc:
                        logger.warning("HA Sensor: goodnight check — failed to query {}: {}", eid, exc)
                        all_secure = False

                entity_name = "secure" if all_secure else "insecure"
                logger.success("HA Sensor: goodnight security check -> {}", entity_name)

                response = httpx.post(
                    self._announce_url,
                    json={"scenario": scenario, "entity_name": entity_name},
                    timeout=15.0,
                )
                response.raise_for_status()
                result = response.json()
                logger.success(
                    "HA Sensor: goodnight announced: {}, base={}, followups={}",
                    entity_name, result.get("base", "?"), result.get("followups", []),
                )
            except Exception as exc:
                logger.warning("HA Sensor: goodnight check failed: {}", exc)

        threading.Thread(target=_do_check, name="HA-Goodnight", daemon=True).start()

    # ── Lockdown: all-persons-home detection + lock + announcement ──

    def _maybe_trigger_lockdown(self) -> None:
        """When a person arrives home, start a delayed lockdown check.

        Waits lockdown_delay_s (default 20 min) then re-verifies everyone
        is still home before firing lock + ominous announcement.
        """
        if not self._lockdown_config:
            return

        # Cooldown: don't re-fire within 5 minutes
        now = time.time()
        if now - self._lockdown_last_fired < self._lockdown_cooldown_s:
            return

        # Quick pre-check: are all persons home RIGHT NOW?
        # If not, no point starting a timer.
        if not self._all_persons_home():
            return

        # Mark cooldown immediately to prevent duplicate timers
        self._lockdown_last_fired = now
        delay = self._lockdown_config.get("lockdown_delay_s", 1200)  # 20 min default
        logger.info(
            "HA Sensor: all persons home — lockdown timer started ({:.0f}m)",
            delay / 60,
        )
        threading.Thread(
            target=self._delayed_lockdown, args=(delay,),
            name="HA-Lockdown-Timer", daemon=True,
        ).start()

    def _all_persons_home(self) -> bool:
        """Query HA to verify all tracked persons are currently home."""
        person_eids = self._lockdown_config["person_entities"]
        ha_url = self._ha_ws_url.replace("ws://", "http://").replace("/api/websocket", "")
        headers = {"Authorization": f"Bearer {self._ha_token}"}

        for eid in person_eids:
            try:
                resp = httpx.get(
                    f"{ha_url}/api/states/{eid}",
                    headers=headers,
                    timeout=5.0,
                )
                resp.raise_for_status()
                state = resp.json().get("state", "unknown")
                if state != "home":
                    logger.debug("HA Sensor: lockdown check — {} is '{}'", eid, state)
                    return False
            except Exception as exc:
                logger.warning("HA Sensor: lockdown check failed for {}: {}", eid, exc)
                return False
        return True

    def _delayed_lockdown(self, delay_s: float) -> None:
        """Sleep, then re-verify all persons still home before firing lockdown."""
        time.sleep(delay_s)

        # Re-verify everyone is still home after the delay
        if not self._all_persons_home():
            logger.info("HA Sensor: lockdown cancelled — not all persons still home after delay")
            # Reset cooldown so next arrival can start a new timer
            self._lockdown_last_fired = 0.0
            return

        logger.success("HA Sensor: lockdown timer expired, all still home — initiating lockdown")
        self._do_lockdown()

    def _do_lockdown(self) -> None:
        """Fire lock command. The resulting lock state change will trigger
        the normal lock announcement via ``_process_ws_message`` → ``_trigger_announcement``.

        Previously this method also played an ominous lockdown-scenario announcement,
        causing a duplicate (lockdown WAV + autonomy LLM speech). Now the lock
        scenario's pre-generated WAV (with 5% followup chance) handles it.
        """
        config = self._lockdown_config
        lock_entity = config.get("lock_entity")

        ha_url = self._ha_ws_url.replace("ws://", "http://").replace("/api/websocket", "")
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }

        # Fire the lock command — the state change will be picked up by
        # the WebSocket listener, which triggers the lock announcement.
        if lock_entity:
            try:
                resp = httpx.post(
                    f"{ha_url}/api/services/lock/lock",
                    headers=headers,
                    json={"entity_id": lock_entity},
                    timeout=10.0,
                )
                resp.raise_for_status()
                logger.success("HA Sensor: lockdown — lock command sent to {}", lock_entity)
            except Exception as exc:
                logger.warning("HA Sensor: lockdown — lock command failed: {}", exc)

    # ── Mode management (maintenance / silent) ─────────────────────

    def _fetch_initial_mode_state(self) -> None:
        """Query HA REST API for initial state of mode entities on startup.

        Sets _initial_mode_fetch flag to suppress audio playback during
        initial state load — the startup announcement handles its own
        maintenance mode routing via _resolve_startup_speaker().
        """
        self._initial_mode_fetch = True
        headers = {"Authorization": f"Bearer {self._ha_token}"}
        for entity_id in self._MODE_ENTITIES:
            try:
                resp = httpx.get(
                    f"{self._ha_url}/api/states/{entity_id}",
                    headers=headers,
                    timeout=5.0,
                )
                resp.raise_for_status()
                state = resp.json().get("state", "")
                self._handle_mode_change(entity_id, state)
            except Exception as exc:
                logger.warning(
                    "HA Sensor: failed to fetch initial state for {}: {}",
                    entity_id, exc,
                )
        self._initial_mode_fetch = False

    def _handle_mode_change(self, entity_id: str, new_state: str) -> None:
        """Handle changes to maintenance/silent mode HA entities."""
        changed = False

        if entity_id == self._eid_maintenance:
            enabled = new_state == "on"
            if enabled != self._maintenance_mode:
                self._maintenance_mode = enabled
                changed = True
                if enabled:
                    self._maintenance_entered_at = time.time()
                    self._maintenance_last_reminder = time.time()
                    if not self._initial_mode_fetch:
                        # Play enter WAV (speaker may not be set yet if entities arrive out of order)
                        # Skipped on initial fetch — startup announcement handles its own routing
                        threading.Thread(
                            target=lambda: time.sleep(1) or self._play_maintenance_wav("maintenance_enter.wav"),
                            daemon=True,
                        ).start()
                    else:
                        logger.info("HA Sensor: maintenance mode active at startup (audio suppressed)")
                else:
                    # Play exit WAV before clearing speaker
                    if not self._initial_mode_fetch:
                        self._play_maintenance_wav("maintenance_exit.wav")
                    self._maintenance_entered_at = 0.0
                logger.success("HA Sensor: maintenance mode → {}", enabled)

        elif entity_id == self._eid_maintenance_speaker:
            if new_state != self._maintenance_speaker:
                self._maintenance_speaker = new_state
                changed = True
                logger.success("HA Sensor: maintenance speaker → {}", new_state)

        elif entity_id == self._eid_silent:
            enabled = new_state == "on"
            if enabled != self._silent_mode:
                self._silent_mode = enabled
                changed = True
                logger.success("HA Sensor: silent mode → {}", enabled)

        if changed and self._mode_change_callback:
            try:
                self._mode_change_callback(
                    maintenance_mode=self._maintenance_mode,
                    maintenance_speaker=self._maintenance_speaker,
                    silent_mode=self._silent_mode,
                )
            except Exception as exc:
                logger.error("HA Sensor: mode change callback failed: {}", exc)

    def _play_maintenance_wav(self, wav_name: str) -> None:
        """Play a pre-recorded maintenance WAV on the maintenance speaker via HA."""
        wav_path = self._maintenance_audio_dir / wav_name
        if not wav_path.exists():
            logger.warning("HA Sensor: maintenance WAV not found: {}", wav_path)
            return

        speaker = self._maintenance_speaker
        if not speaker:
            logger.warning("HA Sensor: no maintenance speaker set, cannot play {}", wav_name)
            return

        try:
            import shutil
            self._serve_dir.mkdir(parents=True, exist_ok=True)
            dest = self._serve_dir / wav_name
            shutil.copy2(wav_path, dest)
            media_url = f"http://{self._serve_host}:{self._serve_port}/{wav_name}"

            httpx.post(
                f"{self._ha_url}/api/services/media_player/play_media",
                headers={"Authorization": f"Bearer {self._ha_token}",
                         "Content-Type": "application/json"},
                json={
                    "entity_id": [speaker],
                    "media_content_id": media_url,
                    "media_content_type": "music",
                },
                timeout=10.0,
            )
            logger.success("HA Sensor: played {} on {}", wav_name, speaker)
        except Exception as exc:
            logger.error("HA Sensor: failed to play {}: {}", wav_name, exc)

    def _auto_disable_maintenance(self) -> None:
        """Turn off maintenance mode via HA after auto-expiry."""
        # Play expiry announcement BEFORE disabling (so speaker is still set)
        self._play_maintenance_wav("maintenance_expired.wav")
        try:
            httpx.post(
                f"{self._ha_url}/api/services/input_boolean/turn_off",
                headers={"Authorization": f"Bearer {self._ha_token}",
                         "Content-Type": "application/json"},
                json={"entity_id": self._eid_maintenance},
                timeout=5.0,
            )
            logger.success("HA Sensor: maintenance mode auto-disabled via HA")
        except Exception as exc:
            logger.error("HA Sensor: failed to auto-disable maintenance: {}", exc)

    def _play_maintenance_reminder(self, hours: int, minutes: int) -> None:
        """Play a maintenance mode reminder via TTS queue."""
        if self._tts_queue:
            if hours == 0:
                time_str = f"{minutes} minutes"
            elif minutes == 0:
                time_str = f"{hours} {'hour' if hours == 1 else 'hours'}"
            else:
                time_str = f"{hours} {'hour' if hours == 1 else 'hours'} and {minutes} minutes"
            try:
                self._tts_queue.put_nowait(
                    f"Maintenance mode has been active for {time_str}. "
                    "All audio is still routing to the maintenance speaker. "
                    "Say exit maintenance mode when you are finished."
                )
                logger.success("HA Sensor: maintenance reminder played ({})", time_str)
            except queue.Full:
                pass

    # ── Pet outdoor cold weather monitor ─────────────────────────────

    def _check_pet_outdoor(self) -> None:
        """Check if the pet has been outside long enough in cold weather to alert."""
        if not self._pet_monitor_config or not self._pet_monitor_config.get("enabled"):
            return
        if self._pet_outdoor_since is None or self._pet_alert_fired:
            return
        if time.time() < self._pet_alert_cooldown_until:
            return

        elapsed_min = (time.time() - self._pet_outdoor_since) / 60.0
        threshold_min = self._pet_monitor_config.get("duration_threshold_minutes", 10)
        if elapsed_min < threshold_min:
            return

        # Duration threshold exceeded — check temperature
        from glados.core.weather_cache import get_data
        weather = get_data()
        if not weather:
            logger.warning("Pet monitor: no weather data available, skipping check")
            return

        temp_f = weather.get("current", {}).get("temperature")
        threshold_f = self._pet_monitor_config.get("temperature_threshold_f", 45)
        if temp_f is None:
            logger.warning("Pet monitor: temperature data missing from weather cache")
            return

        pet_name = self._pet_monitor_config.get("pet_name", "pet")
        if temp_f >= threshold_f:
            logger.debug(
                "Pet monitor: {} outside {:.0f}min but temp {:.0f}°F >= {:.0f}°F, no alert",
                pet_name, elapsed_min, temp_f, threshold_f,
            )
            return

        # ALERT: pet outside too long in cold weather
        logger.warning(
            "Pet monitor: {} outside {:.0f}min, temp {:.0f}°F < {:.0f}°F — alerting!",
            pet_name, elapsed_min, temp_f, threshold_f,
        )
        self._pet_alert_fired = True
        cooldown_min = self._pet_monitor_config.get("cooldown_minutes", 30)
        self._pet_alert_cooldown_until = time.time() + (cooldown_min * 60)

        # Pick a random line from vision announcements and speak it
        speaker = self._pet_monitor_config.get("speaker", "media_player.office_c97a_3")
        scenario = self._pet_monitor_config.get("announcement_scenario", "pet_cold_weather")
        text = self._pick_vision_announcement_line(scenario)
        if text:
            self._play_tts_on_speaker(text, speaker)
        elif self._tts_queue is not None:
            # Fallback: use TTS queue (plays on default engine speaker)
            fallback = f"{pet_name} has been outside for {int(elapsed_min)} minutes and it is {int(temp_f)} degrees."
            try:
                self._tts_queue.put_nowait(fallback)
                logger.info("Pet monitor: fallback TTS: {}", fallback)
            except queue.Full:
                logger.warning("Pet monitor: TTS queue full, could not announce")

    def _pick_vision_announcement_line(self, scenario: str) -> str | None:
        """Pick a random announcement line from vision_announcements.yaml."""
        import random
        try:
            vision_yaml = Path(
                os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
            ) / "vision_announcements.yaml"
            if not vision_yaml.exists():
                logger.warning("Pet monitor: vision_announcements.yaml not found")
                return None
            import yaml  # type: ignore[import-untyped]
            with open(vision_yaml, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            scenarios = data.get("scenarios", {})
            cfg = scenarios.get(scenario)
            if not cfg:
                logger.warning("Pet monitor: scenario '{}' not found in vision_announcements.yaml", scenario)
                return None
            lines = cfg.get("lines", [])
            if not lines:
                return None
            return random.choice(lines)
        except Exception as exc:
            logger.warning("Pet monitor: failed to load vision announcement: {}", exc)
            return None

    def _play_tts_on_speaker(self, text: str, speaker: str) -> None:
        """Generate TTS audio and play it on a specific HA speaker.

        Uses the GLaDOS TTS API (port 5050) to generate WAV, saves to the
        serve directory, and plays via HA media_player.play_media.
        """
        import uuid as _uuid

        def _do_play() -> None:
            try:
                from glados.core.config_store import cfg as _cfg
                tts_url = _cfg.service_url("tts")
            except Exception:
                tts_url = "http://localhost:5050"

            try:
                # Generate WAV via TTS API
                response = httpx.post(
                    f"{tts_url}/v1/audio/speech",
                    json={"input": text, "voice": "glados", "response_format": "wav"},
                    timeout=30.0,
                )
                response.raise_for_status()
                wav_data = response.content

                # Save to serve directory
                wav_name = f"pet_alert_{_uuid.uuid4().hex[:8]}.wav"
                self._serve_dir.mkdir(parents=True, exist_ok=True)
                dest = self._serve_dir / wav_name
                dest.write_bytes(wav_data)

                # Play on HA speaker
                media_url = f"http://{self._serve_host}:{self._serve_port}/{wav_name}"
                httpx.post(
                    f"{self._ha_url}/api/services/media_player/play_media",
                    headers={
                        "Authorization": f"Bearer {self._ha_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "entity_id": [speaker],
                        "media_content_id": media_url,
                        "media_content_type": "music",
                    },
                    timeout=10.0,
                )
                logger.success("Pet monitor: played alert on {} — '{}'", speaker, text[:60])
            except Exception as exc:
                logger.error("Pet monitor: TTS/play failed: {}", exc)

        threading.Thread(target=_do_play, name="Pet-Alert-TTS", daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────

    def _trigger_doorbell_screening(self, entity_id: str) -> None:
        """Trigger the doorbell screening system via the API wrapper endpoint.

        Fires HTTP POST to localhost:8015/doorbell/screen in a background thread.
        The screening endpoint returns immediately and runs the full screening
        session (greeting, listen, transcribe, evaluate, reply, announce) in its
        own background thread.
        """
        from glados.core.config_store import cfg as _cfg

        def _do_screen() -> None:
            try:
                logger.success(
                    "HA Sensor: doorbell ring detected on {}, triggering screening",
                    entity_id,
                )
                api_url = _cfg.service_url("api_wrapper")
                response = httpx.post(
                    f"{api_url}/doorbell/screen",
                    json={},
                    timeout=10.0,
                )
                result = response.json()
                logger.success(
                    "HA Sensor: doorbell screening response: {}", result,
                )
            except Exception as exc:
                logger.warning("HA Sensor: doorbell screening trigger failed: {}", exc)

        threading.Thread(target=_do_screen, name="HA-Doorbell-Screen", daemon=True).start()

    def _trigger_vision_analysis(
        self, camera_id: str, entity_id: str, detection_type: str | None = None,
    ) -> None:
        """Trigger the vision service to capture and analyze a camera snapshot.

        Calls the vision service's /snapshot-test endpoint which captures a frame,
        runs it through the vision LLM, and returns a verdict with severity and
        optional GLaDOS personality text.

        Uses an in-flight guard (``_vision_in_flight``) to prevent multiple
        simultaneous requests for the same camera. When a person walks up to
        the front door, HA typically fires several detection entities at once
        (person_detected, motion_detected, etc.). Without the guard, each
        would spawn its own vision analysis, resulting in double/triple
        announcements.

        Args:
            camera_id: Vision service camera identifier (e.g., "front_door").
            entity_id: HA entity that triggered this (for logging).
            detection_type: What was detected (e.g., "person", "package").
                If provided, the vision service uses SECONDARY mode with a
                verification prompt tailored to the detection type.
        """
        # ── In-flight guard: only one analysis per camera at a time ──
        with self._vision_in_flight_lock:
            if camera_id in self._vision_in_flight:
                logger.info(
                    "HA Sensor: vision already in-flight for '{}', skipping duplicate (trigger: {}, type: {})",
                    camera_id, entity_id, detection_type or "motion",
                )
                return
            self._vision_in_flight.add(camera_id)

        def _do_analysis() -> None:
            try:
                logger.success(
                    "HA Sensor: triggering vision analysis for camera '{}' (trigger: {}, type: {})",
                    camera_id, entity_id, detection_type or "motion",
                )
                payload: dict[str, Any] = {"camera_id": camera_id}
                if detection_type:
                    payload["detection_type"] = detection_type
                    payload["mode"] = "secondary"
                response = httpx.post(
                    f"{self._vision_api_url}/snapshot-test",
                    json=payload,
                    timeout=60.0,
                )
                response.raise_for_status()
                result = response.json()
                verdict = result.get("verdict", {})
                severity = verdict.get("severity", "unknown")
                description = verdict.get("description", "No description")
                glados_text = result.get("glados_text")
                logger.success(
                    "HA Sensor: vision result for '{}': severity={}, description={}",
                    camera_id, severity, description,
                )

                # The vision service handles the full announcement pipeline:
                # GLaDOS text -> TTS -> play on camera-configured speaker.
                # Do NOT inject into TTS queue (would cause duplicate on different speaker).
                if glados_text:
                    logger.success("HA Sensor: vision announced on camera speaker: {}", glados_text[:80])
                else:
                    logger.success("HA Sensor: vision service returned no announcement (severity: {})", severity)

            except Exception as exc:
                logger.warning("HA Sensor: vision analysis failed for '{}': {}", camera_id, exc)
            finally:
                # Always release the in-flight guard so the camera can be
                # analyzed again for the next real event.
                with self._vision_in_flight_lock:
                    self._vision_in_flight.discard(camera_id)

        threading.Thread(target=_do_analysis, name="HA-Vision-Trigger", daemon=True).start()

    def _build_announcement(self, event: HAStateEvent) -> str | None:
        """Build a TTS announcement string for direct speech injection."""
        domain = event.entity_id.split(".")[0]
        templates = self.ANNOUNCEMENT_TEMPLATES.get(domain, {})
        template = templates.get(event.new_state)
        if template:
            return template.format(name=event.friendly_name)
        return f"{event.friendly_name} changed to {event.new_state}."

    def _describe_event(self, event: HAStateEvent) -> str:
        """Build a human-readable description of a state change."""
        domain = event.entity_id.split(".")[0]
        state_map = STATE_DESCRIPTIONS.get(domain, {})
        action = state_map.get(event.new_state, f"changed to {event.new_state}")
        return f"{event.friendly_name} {action}"

    def _generate_report(self, events: list[HAStateEvent]) -> str:
        """Generate a detailed report of state changes."""
        lines = ["## Home Assistant State Changes", ""]

        for event in events[:20]:
            category_label = event.category.name.lower()
            icon = {
                "alert": "[!]",
                "notable": "[i]",
                "routine": "[.]",
            }.get(category_label, "[?]")

            description = self._describe_event(event)
            lines.append(f"### {icon} {description}")
            lines.append(f"- **Entity:** {event.entity_id}")
            lines.append(f"- **Change:** {event.old_state} -> {event.new_state}")
            lines.append(f"- **Category:** {category_label}")
            lines.append("")

        return "\n".join(lines)
