"""
Hub75Display — main controller for the HUB75 LED panel.

Subscribes to the ObservabilityBus, drives the render loop in a daemon
thread, and manages state transitions, gaze animation, and WLED
communication.  All rendering and image processing happens here on
the AIBox; only raw DDP pixel streams are sent to the panel.

The panel is **dormant by default** — it only lights up when GLaDOS is
actively engaged (speaking, processing an LLM request, showing an
emotion).  After speech ends, the eye fades out and the panel goes
dark to save power and reduce wear on USB-powered setups.

Integration: instantiated in ``engine.py`` (conditional on config),
receives the shared ObservabilityBus via constructor injection.
"""

from __future__ import annotations

import math
import queue
import random
import threading
import time
from typing import Any

from loguru import logger

from ..core.config_store import Hub75DisplayConfig, Hub75GazeConfig
from ..observability.bus import ObservabilityBus
from ..observability.events import ObservabilityEvent
from .ddp import DdpSender
from .emotion_map import apply_pad_modulation, resolve_attitude
from .gif_player import GifPlayer
from .renderer import lerp_params, render_eye_frame
from .state_machine import (
    DEFAULT_PARAMS,
    EyeParams,
    EyeState,
    STATE_PRIORITY,
    can_transition,
    get_default_params,
)
from .wled_client import WledClient

# States that keep the panel active (eye lit).
# Everything else = panel dormant (off).
_ACTIVE_STATES = frozenset({
    EyeState.SPEAKING,
    EyeState.ANGRY,
    EyeState.THINKING,
    EyeState.CURIOUS,
    EyeState.ALERT,
    EyeState.GIF,
})

# ── Eye-close animation timing ──────────────────────────────
# Instead of a gradual brightness fade, the eye blinks rapidly a couple
# of times and then closes its lids before going dormant.
#
# Timeline (seconds after close animation starts):
#   0.00 - 0.12  blink 1: close lids
#   0.12 - 0.22  blink 1: open lids
#   0.22 - 0.34  blink 2: close lids
#   0.34 - 0.44  blink 2: open lids
#   0.44 - 0.85  final close: lids shut smoothly
#   0.85 - 1.00  hold closed, then go dormant
_CLOSE_BLINK_1_SHUT = 0.12
_CLOSE_BLINK_1_OPEN = 0.22
_CLOSE_BLINK_2_SHUT = 0.34
_CLOSE_BLINK_2_OPEN = 0.44
_CLOSE_FINAL_START = 0.44
_CLOSE_FINAL_SHUT = 0.85
_CLOSE_HOLD_END = 1.00

# Debounce for tts.finish — wait this long after the last non-muted TTS chunk
# finishes before starting fade-out.  Only used for direct audio_io playback
# (non-API path).  For HA-based audio (API path), ha_audio.play provides the
# estimated duration directly.
_SPEECH_DEBOUNCE_S = 1.5

# Extra time for HA speaker latency (only used in non-muted tts.play path).
_HA_SPEAKER_BUFFER_S = 3.5

# Assumed TTS sample rate for duration estimation from sample counts.
_ASSUMED_TTS_SAMPLE_RATE = 22050


class Hub75Display:
    """HUB75 LED panel display controller.

    Lifecycle:
        1. ``__init__()`` — stores refs, initialises state. No connections.
        2. ``start()``    — opens sockets, pings WLED, starts daemon threads.
        3. ``stop()``     — blanks panel, closes sockets, stops threads.

    The panel starts dormant and activates when a bus event
    indicates GLaDOS is engaged (tts.play, llm.request, etc.).
    """

    def __init__(
        self,
        observability_bus: ObservabilityBus,
        config: Hub75DisplayConfig,
    ) -> None:
        self._bus = observability_bus
        self._cfg = config

        # ── State ─────────────────────────────────────────────
        self._lock = threading.Lock()
        self._running = False

        self._current_state = EyeState.IDLE
        self._current_params = get_default_params(EyeState.IDLE, config.eye_state_overrides)
        self._target_params = self._current_params.copy()
        self._prev_params = self._current_params.copy()
        self._transition_start = time.monotonic()

        # Restore stack: save state before SPEAKING so we can return to it
        self._pre_speech_state: EyeState = EyeState.IDLE
        self._pending_emotion_state: EyeState | None = None

        # PAD values from EmotionAgent (updated via bus or direct)
        self._pad_p: float = 0.0
        self._pad_a: float = 0.0
        self._pad_d: float = 0.0

        # ── Panel active/dormant tracking ────────────────────
        # Panel starts dormant — no frames sent until GLaDOS is engaged.
        self._panel_active = False
        self._fade_out_start: float = 0.0
        self._fading_out = False

        # ── Speech debounce ─────────────────────────────────
        # Streaming TTS emits one tts.play/tts.finish pair per audio
        # chunk.  We debounce tts.finish so that the panel stays lit
        # across chunk boundaries instead of fading out between them.
        self._speech_finish_pending = False
        self._speech_finish_deadline: float = 0.0
        # Earliest time the panel should start fading, estimated from
        # audio sample count + HA speaker latency buffer.  Ensures the
        # eye stays lit until the HA speaker actually finishes playback.
        self._min_wake_until: float = 0.0

        # GIF/preset job queue
        self._gif_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=8)

        # ── Gaze animation state ──────────────────────────────
        self._gaze_cfg: Hub75GazeConfig = config.gaze
        self._gaze_target_x: float = 0.0
        self._gaze_target_y: float = 0.0
        self._gaze_current_x: float = 0.0
        self._gaze_current_y: float = 0.0
        self._gaze_next_change: float = 0.0  # monotonic time for next target pick

        # Blink state
        self._blink_next: float = 0.0
        self._blink_start: float = 0.0
        self._blinking: bool = False

        # ── Components (created in start()) ───────────────────
        self._sender: DdpSender | None = None
        self._wled: WledClient | None = None
        self._gif_player: GifPlayer | None = None
        self._wled_available = False

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Initialise connections and start daemon threads."""
        if self._running:
            return

        ip = self._cfg.wled_ip
        if ip in ("0.0.0.0", ""):
            logger.warning("HUB75: WLED IP is unset — display will not connect")

        # Initialise components
        delay_s = getattr(self._cfg, 'ddp_inter_packet_delay_ms', 1.5) / 1000.0
        self._sender = DdpSender(ip, self._cfg.wled_ddp_port, inter_packet_delay=delay_s)
        self._wled = WledClient(ip)
        self._gif_player = GifPlayer(
            self._sender, self._wled,
            self._cfg.panel_width, self._cfg.panel_height,
        )

        # Ping WLED — gentle startup to avoid overloading USB-powered devices
        ok, latency = self._wled.ping()
        if ok:
            logger.info("HUB75: WLED reachable at {} ({:.0f}ms)", ip, latency)
            self._wled.set_brightness(self._cfg.global_brightness)
            time.sleep(0.5)
            self._wled.set_live_override(realtime_priority=True)
            time.sleep(0.3)
            # Start with panel OFF — it will wake when GLaDOS speaks
            self._wled.turn_off()
            self._wled_available = True
            self._panel_active = False
            logger.info("HUB75: WLED initialised (brightness={}, panel dormant)",
                        self._cfg.global_brightness)
        else:
            logger.warning(
                "HUB75: WLED unreachable at {} — display disabled until reconnect", ip
            )
            self._wled_available = False

        # Initialise gaze timing
        now = time.monotonic()
        self._gaze_next_change = now + random.uniform(
            self._gaze_cfg.fixation_min, self._gaze_cfg.fixation_max
        )
        self._blink_next = now + random.uniform(
            self._gaze_cfg.blink_interval_min, self._gaze_cfg.blink_interval_max
        )

        self._running = True

        # Start daemon threads
        threading.Thread(target=self._run_loop, daemon=True, name="hub75-render").start()
        threading.Thread(target=self._bus_drain_loop, daemon=True, name="hub75-bus").start()
        threading.Thread(target=self._wled_health_loop, daemon=True, name="hub75-health").start()

        logger.info("HUB75: display started ({}x{} @ {}fps, dormant until engaged)",
                     self._cfg.panel_width, self._cfg.panel_height, self._cfg.fps)

    def stop(self) -> None:
        """Blank the panel and shut down cleanly."""
        self._running = False
        time.sleep(0.1)  # Let threads finish current iteration

        if self._sender:
            blank = bytes(self._cfg.panel_width * self._cfg.panel_height * 3)
            self._sender.send_frame(blank)
            self._sender.close()

        if self._wled and self._wled_available:
            self._wled.turn_off()

        logger.info("HUB75: display stopped")

    # ── Panel Wake / Sleep ───────────────────────────────────

    def _wake_panel(self) -> None:
        """Turn the panel on and start streaming frames.

        Also cancels any in-progress fade-out — this is important for
        streaming TTS where a new chunk's ``ha_audio.play`` arrives while
        a previous close animation is still running.

        We do NOT call WLED ``turn_on()`` here.  WLED is in live-override
        mode (``lor=0``) from startup, so DDP frames render immediately
        regardless of the on/off state.  Calling ``turn_on()`` would
        flash WLED's default orange display before DDP takes over.
        """
        if self._panel_active and not self._fading_out:
            return
        was_fading = self._fading_out
        self._panel_active = True
        self._fading_out = False
        self._speech_finish_pending = False
        if was_fading:
            logger.debug("HUB75: panel re-woke (cancelled close animation)")
        else:
            logger.debug("HUB75: panel woke — streaming active")

    def _begin_fade_out(self) -> None:
        """Start the eye-close animation before going dormant."""
        if self._fading_out or not self._panel_active:
            return
        self._fading_out = True
        self._fade_out_start = time.monotonic()
        logger.debug("HUB75: beginning eye-close animation")

    @staticmethod
    def _close_lid_amount(t: float) -> float:
        """Compute lid closure amount for the eye-close animation.

        Returns 0.0 (open) to 1.0 (shut) for the current phase of the
        blink-blink-close sequence.  Returns -1.0 when the animation is
        complete and the caller should go dormant.
        """
        if t < 0:
            return 0.0

        # ── Blink 1 ──
        if t < _CLOSE_BLINK_1_SHUT:
            # Closing: 0 → 1
            return t / _CLOSE_BLINK_1_SHUT
        if t < _CLOSE_BLINK_1_OPEN:
            # Opening: 1 → 0
            return 1.0 - (t - _CLOSE_BLINK_1_SHUT) / (_CLOSE_BLINK_1_OPEN - _CLOSE_BLINK_1_SHUT)

        # ── Blink 2 ──
        if t < _CLOSE_BLINK_2_SHUT:
            # Closing: 0 → 1
            return (t - _CLOSE_BLINK_1_OPEN) / (_CLOSE_BLINK_2_SHUT - _CLOSE_BLINK_1_OPEN)
        if t < _CLOSE_BLINK_2_OPEN:
            # Opening: 1 → 0
            return 1.0 - (t - _CLOSE_BLINK_2_SHUT) / (_CLOSE_BLINK_2_OPEN - _CLOSE_BLINK_2_SHUT)

        # ── Final close ──
        if t < _CLOSE_FINAL_SHUT:
            # Lids smoothly shut: 0 → 1
            return (t - _CLOSE_FINAL_START) / (_CLOSE_FINAL_SHUT - _CLOSE_FINAL_START)

        # ── Hold closed ──
        if t < _CLOSE_HOLD_END:
            return 1.0

        # ── Done ──
        return -1.0

    def _go_dormant(self) -> None:
        """Turn the panel off — no more frames sent."""
        if not self._panel_active:
            return
        # Send a final blank frame
        if self._sender and self._wled_available:
            blank = bytes(self._cfg.panel_width * self._cfg.panel_height * 3)
            self._sender.send_frame(blank)
        if self._wled and self._wled_available:
            self._wled.turn_off()
        self._panel_active = False
        self._fading_out = False
        self._min_wake_until = 0.0
        logger.debug("HUB75: panel dormant")

    # ── Render Loop (daemon thread) ───────────────────────────

    def _run_loop(self) -> None:
        """Main render loop — deadline-based to prevent frame pileup."""
        frame_interval = 1.0 / max(1, self._cfg.fps)
        next_frame = time.monotonic()

        while self._running:
            now = time.monotonic()

            # If we've fallen behind by 2+ frames, skip ahead instead
            # of trying to catch up (which floods the ESP32).
            if now - next_frame > frame_interval * 2:
                next_frame = now

            if now >= next_frame:
                try:
                    self._tick_render(now)
                except Exception as exc:
                    logger.warning("HUB75: render error: {}", exc)
                next_frame += frame_interval
            else:
                time.sleep(max(0.001, next_frame - now))

    def _tick_render(self, now: float) -> None:
        """Single render tick: update gaze, compute params, send frame."""
        if not self._sender or not self._wled_available:
            return

        # ── Check debounced speech-finish ────────────────────
        # If tts.finish fired and the debounce window has elapsed
        # without a new tts.play, NOW we actually fade out.
        if self._speech_finish_pending and now >= self._speech_finish_deadline:
            self._speech_finish_pending = False
            logger.success("HUB75: speech done — starting fade-out")
            with self._lock:
                restore = self._pre_speech_state
                pending = self._pending_emotion_state
                self._pending_emotion_state = None
            if pending is not None:
                self._force_state(pending)
            else:
                self._force_state(restore)
            self._begin_fade_out()

        # ── Dormant: don't send any frames ───────────────────
        if not self._panel_active:
            return

        # ── GIF state: DDP rendering handled by GifPlayer ────
        with self._lock:
            if self._current_state == EyeState.GIF:
                return

        # Process any pending GIF jobs
        self._process_gif_queue()

        # ── State transition interpolation ────────────────────
        with self._lock:
            transition_t = (now - self._transition_start) / max(
                0.01, self._cfg.transition_duration
            )
            transition_t = min(1.0, transition_t)
            params = lerp_params(self._prev_params, self._target_params, transition_t)

        # ── Eye-close animation: rapid blinks → lids shut → dormant
        if self._fading_out:
            t = now - self._fade_out_start
            lid = self._close_lid_amount(t)
            if lid < 0:
                # Animation complete — go dormant
                self._go_dormant()
                return
            params.top_lid = max(params.top_lid, lid)
            params.bottom_lid = max(params.bottom_lid, lid * 0.6)

        # ── Gaze animation ────────────────────────────────────
        if self._gaze_cfg.enabled:
            self._tick_gaze(params, now)

        # ── Blink animation ───────────────────────────────────
        if self._gaze_cfg.enabled:
            self._tick_blink(params, now)

        # ── PAD modulation ────────────────────────────────────
        params = apply_pad_modulation(params, self._pad_p, self._pad_a, self._pad_d)

        # ── Render and send ───────────────────────────────────
        wall_time = time.time()  # For pulse animation
        frame = render_eye_frame(
            params, wall_time,
            self._cfg.panel_width, self._cfg.panel_height,
        )
        self._sender.send_frame(frame)

    # ── Gaze Animation ────────────────────────────────────────

    def _tick_gaze(self, params: EyeParams, now: float) -> None:
        """Update gaze position — smooth saccades with fixation pauses.

        TODO: Contextual gaze — override random targets with named locations
              when GLaDOS mentions them (front door, bedroom, etc.).
              See docs/TODO-contextual-gaze.md for full plan.
        """
        cfg = self._gaze_cfg

        # Reduce gaze range during certain states
        range_scale = 1.0
        with self._lock:
            state = self._current_state
        if state == EyeState.SLEEPING:
            range_scale = 0.1
        elif state == EyeState.SPEAKING:
            range_scale = 0.3
        elif state == EyeState.ALERT:
            range_scale = 0.5

        # Pick new gaze target when fixation expires
        if now >= self._gaze_next_change:
            self._gaze_target_x = random.uniform(-cfg.range_x, cfg.range_x) * range_scale
            self._gaze_target_y = random.uniform(-cfg.range_y, cfg.range_y) * range_scale
            self._gaze_next_change = now + random.uniform(cfg.fixation_min, cfg.fixation_max)

        # Saccade interpolation — move toward target
        dx = self._gaze_target_x - self._gaze_current_x
        dy = self._gaze_target_y - self._gaze_current_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist > 0.1:
            step = min(dist, cfg.saccade_speed / max(1, self._cfg.fps))
            self._gaze_current_x += dx / dist * step
            self._gaze_current_y += dy / dist * step

        params.offset_x = self._gaze_current_x
        params.offset_y = self._gaze_current_y

    def _tick_blink(self, params: EyeParams, now: float) -> None:
        """Animate periodic blinks — quick top-lid close then open."""
        cfg = self._gaze_cfg

        if not self._blinking:
            if now >= self._blink_next:
                self._blinking = True
                self._blink_start = now
        else:
            elapsed = now - self._blink_start
            half_dur = cfg.blink_duration / 2.0

            if elapsed < half_dur:
                blink_amount = elapsed / half_dur
            elif elapsed < cfg.blink_duration:
                blink_amount = 1.0 - (elapsed - half_dur) / half_dur
            else:
                self._blinking = False
                self._blink_next = now + random.uniform(
                    cfg.blink_interval_min, cfg.blink_interval_max
                )
                blink_amount = 0.0

            if self._blinking:
                blink_lid = blink_amount * 0.9
                params.top_lid = max(params.top_lid, blink_lid)
                params.bottom_lid = max(params.bottom_lid, blink_lid * 0.3)

    # ── Bus Drain Loop (daemon thread) ────────────────────────

    def _bus_drain_loop(self) -> None:
        """Drain ObservabilityBus events and dispatch state changes."""
        while self._running:
            try:
                events = self._bus.drain(max_items=50)
                for event in events:
                    try:
                        self._handle_event(event)
                    except Exception as exc:
                        logger.warning("HUB75: error handling event {}.{}: {}",
                                       event.source, event.kind, exc)
            except Exception as exc:
                logger.warning("HUB75: bus drain error: {}", exc)

            time.sleep(0.01)

    def _handle_event(self, event: ObservabilityEvent) -> None:
        """Dispatch a single bus event to the appropriate handler."""
        src, kind = event.source, event.kind

        # ── ha_audio.play — PRIMARY signal for HA-based audio ─────
        # Emitted by the api_wrapper when it knows HA speakers are
        # about to play audio (chat responses, announcements, commands).
        # This is the ONLY reliable signal — the engine's own tts.play
        # events are always muted (audio_samples=0) because the
        # api_wrapper intentionally mutes TTS to prevent duplicate audio.
        if src == "ha_audio" and kind == "play":
            estimated_s = float(event.meta.get("estimated_duration_s", 8.0))
            was_dormant = not self._panel_active
            self._speech_finish_pending = False
            self._wake_panel()
            with self._lock:
                if self._current_state != EyeState.SPEAKING:
                    self._pre_speech_state = self._current_state
            self._force_state(EyeState.SPEAKING)
            # Snap to SPEAKING immediately when waking from dormant
            # — skip the 0.4s interpolation from IDLE so the eye opens
            # instantly instead of showing a half-closed rectangle.
            if was_dormant:
                with self._lock:
                    self._prev_params = self._target_params.copy()
                    self._transition_start = time.monotonic() - 10.0

            # Keep panel lit for the estimated duration.
            now_mono = time.monotonic()
            self._min_wake_until = max(
                self._min_wake_until,
                now_mono + estimated_s,
            )
            # Also set up the debounced fade-out deadline so the panel
            # fades out after estimated playback completes.
            self._speech_finish_pending = True
            self._speech_finish_deadline = self._min_wake_until
            # TODO: Parse event.meta for entity_name/scenario, resolve to
            #       gaze target, call _push_gaze_target(). See docs/TODO-contextual-gaze.md
            logger.success(
                "HUB75: ha_audio.play — panel ON for {:.1f}s (source={}, msg='{}')",
                estimated_s, event.meta.get("source", "?"), event.message[:60],
            )

        # ── tts.play — NON-muted path (direct audio_io playback) ─
        # Only fires with useful data when TTS is NOT muted (e.g.
        # autonomy speaking directly through audio_io, not via API).
        # Muted events (audio_samples=0) are ignored.
        elif src == "tts" and kind == "play":
            if event.meta.get("muted"):
                return  # Muted — useless, skip entirely
            audio_samples = event.meta.get("audio_samples", 0)
            if not audio_samples:
                return  # No audio data — skip

            was_dormant = not self._panel_active
            self._speech_finish_pending = False
            self._wake_panel()
            with self._lock:
                if self._current_state != EyeState.SPEAKING:
                    self._pre_speech_state = self._current_state
            self._force_state(EyeState.SPEAKING)
            if was_dormant:
                with self._lock:
                    self._prev_params = self._target_params.copy()
                    self._transition_start = time.monotonic() - 10.0

            audio_dur = audio_samples / _ASSUMED_TTS_SAMPLE_RATE
            self._min_wake_until = max(
                self._min_wake_until,
                time.monotonic() + audio_dur + _HA_SPEAKER_BUFFER_S,
            )
            logger.success(
                "HUB75: tts.play (non-muted) samples={} dur={:.1f}s",
                audio_samples, audio_dur,
            )

        elif src == "tts" and kind in ("finish", "interrupt"):
            if event.meta.get("muted"):
                return  # Muted — skip
            # Non-muted TTS chunk ended — set debounce deadline.
            self._speech_finish_pending = True
            now_mono = time.monotonic()
            earliest = max(
                now_mono + _SPEECH_DEBOUNCE_S,
                self._min_wake_until,
            )
            self._speech_finish_deadline = earliest

        elif src == "llm" and kind == "request":
            # LLM request — apply attitude/emotion but do NOT wake the panel.
            # Only ha_audio.play / non-muted tts.play should wake the panel
            # (prevents autonomy ticks from keeping the display lit).
            tag = event.meta.get("attitude_tag")
            if tag:
                eye_state = resolve_attitude(tag)
                with self._lock:
                    if self._current_state == EyeState.SPEAKING:
                        self._pending_emotion_state = eye_state
                    else:
                        pass
                self.set_emotion(tag)

        elif src == "emotion" and kind == "update":
            # PAD values updated by EmotionAgent
            self._pad_p = float(event.meta.get("pleasure", 0.0))
            self._pad_a = float(event.meta.get("arousal", 0.0))
            self._pad_d = float(event.meta.get("dominance", 0.0))

    # ── WLED Health Loop (daemon thread) ──────────────────────

    def _wled_health_loop(self) -> None:
        """Periodically ping WLED to detect reconnection.

        Health pings are SKIPPED while the panel is actively streaming
        to avoid HTTP calls competing with DDP on the ESP32.
        """
        while self._running:
            time.sleep(30)
            if not self._running:
                break
            # Skip health checks while panel is active — HTTP requests
            # to the ESP32 cause DDP frame distortion on USB power.
            if self._panel_active:
                continue
            try:
                if self._wled:
                    ok, latency = self._wled.ping()
                    if ok and not self._wled_available:
                        logger.info("HUB75: WLED reconnected at {} ({:.0f}ms)",
                                    self._cfg.wled_ip, latency)
                        self._wled.set_brightness(self._cfg.global_brightness)
                        time.sleep(0.5)
                        self._wled.set_live_override(realtime_priority=True)
                        time.sleep(0.3)
                        self._wled.turn_off()  # Stay dormant until engaged
                        self._wled_available = True
                    elif not ok and self._wled_available:
                        logger.warning("HUB75: WLED lost connection at {}",
                                       self._cfg.wled_ip)
                        self._wled_available = False
            except Exception as exc:
                logger.warning("HUB75: health check error: {}", exc)

    # ── GIF Queue ─────────────────────────────────────────────

    def _process_gif_queue(self) -> None:
        """Process one pending GIF/preset job if available."""
        try:
            job_type, payload = self._gif_queue.get_nowait()
        except queue.Empty:
            return

        if not self._gif_player:
            return

        self._wake_panel()
        self._force_state(EyeState.GIF)

        try:
            if job_type == "preset":
                preset_id, duration = payload
                self._gif_player.play_preset(preset_id, duration)
            elif job_type == "gif":
                path, loops = payload
                self._gif_player.play_gif_file(path, loops)
        except Exception as exc:
            logger.warning("HUB75: GIF job ({}) error: {}", job_type, exc)
        finally:
            self._force_state(EyeState.IDLE)
            self._begin_fade_out()

    # ── Public State API ──────────────────────────────────────

    def set_state(self, state: EyeState) -> None:
        """Request a state change (respects priority rules)."""
        with self._lock:
            if not can_transition(self._current_state, state):
                return
            self._apply_state(state)

    def _force_state(self, state: EyeState) -> None:
        """Force a state change regardless of priority."""
        with self._lock:
            self._apply_state(state)

    def _apply_state(self, state: EyeState) -> None:
        """Apply a state change (must be called while holding self._lock)."""
        if state == self._current_state:
            return
        self._prev_params = self._target_params.copy()
        self._target_params = get_default_params(state, self._cfg.eye_state_overrides)
        self._current_state = state
        self._transition_start = time.monotonic()
        logger.debug("HUB75: state → {}", state.name)

    def set_emotion(self, attitude_tag: str) -> None:
        """Set eye state based on an attitude tag."""
        eye_state = resolve_attitude(attitude_tag)
        with self._lock:
            if self._current_state == EyeState.SPEAKING:
                self._pending_emotion_state = eye_state
                return
        self.set_state(eye_state)

    def update_pad(self, pleasure: float, arousal: float, dominance: float) -> None:
        """Store latest PAD values for modulation by the render loop."""
        self._pad_p = pleasure
        self._pad_a = arousal
        self._pad_d = dominance

    def play_preset(self, preset_id: int, duration_s: float = 5.0) -> None:
        """Queue a WLED preset job."""
        try:
            self._gif_queue.put_nowait(("preset", (preset_id, duration_s)))
        except queue.Full:
            logger.warning("HUB75: GIF queue full, dropping preset {}", preset_id)

    def play_gif(self, category: str, name: str) -> None:
        """Queue a GIF file playback job."""
        if not self._gif_player:
            return
        try:
            path = GifPlayer.get_asset_path(category, name, self._cfg.assets_dir)
            self._gif_queue.put_nowait(("gif", (str(path), 1)))
        except FileNotFoundError as exc:
            logger.warning("HUB75: {}", exc)
        except queue.Full:
            logger.warning("HUB75: GIF queue full, dropping {}/{}", category, name)

    # ── Test helpers (for WebUI) ──────────────────────────────

    def test_ping(self) -> dict[str, Any]:
        """Ping WLED and return result dict."""
        if not self._wled:
            return {"ok": False, "latency_ms": 0, "error": "not initialised"}
        ok, latency = self._wled.ping()
        return {"ok": ok, "latency_ms": latency}

    def test_cycle_states(self, duration: float = 2.0) -> None:
        """Cycle through all eye states for testing."""
        self._wake_panel()
        states = [
            EyeState.IDLE, EyeState.SPEAKING, EyeState.THINKING,
            EyeState.CURIOUS, EyeState.ANGRY, EyeState.ALERT,
            EyeState.SLEEPING,
        ]
        for state in states:
            self._force_state(state)
            time.sleep(duration)
        self._force_state(EyeState.IDLE)
        self._begin_fade_out()

    def test_blank(self) -> None:
        """Send an all-black frame."""
        if self._sender and self._wled_available:
            blank = bytes(self._cfg.panel_width * self._cfg.panel_height * 3)
            self._sender.send_frame(blank)
