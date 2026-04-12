"""
LLM-driven emotional regulation agent.

Uses HEXACO personality model and PAD affect space. Instead of hard-coded
decay math, the LLM reasons about how events should affect emotional state.

Event-driven architecture: LLM is only called when events arrive (with a
debounce window to batch them) or after a 6-hour idle fallback timer.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

from loguru import logger

from ..config import EmotionConfig, HEXACOConfig
from ..emotion_loader import load_emotion_config, EmotionHEXACO
from ..emotion_state import EmotionEvent, EmotionState


# ---------------------------------------------------------------------------
# Repetition tracker — Jaccard similarity, config-driven severity
# ---------------------------------------------------------------------------

class RepetitionTracker:
    """
    Detects repeated/similar messages and returns a severity-tagged
    event description for the emotion LLM.

    Similarity is Jaccard on word sets — fast, no ML dependencies.
    Config drives window size, threshold, curve, and severity labels.
    """

    def __init__(self, ecfg: Any) -> None:
        self._ecfg = ecfg
        self._history: list[str] = []  # Recent user messages

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def count_repeats(self, message: str) -> int:
        """Return how many times this message (or similar) appears in history."""
        esc = self._ecfg.escalation
        window = self._history[-esc.history_window:]
        return sum(
            1 for prev in window
            if self._jaccard(message, prev) >= esc.similarity_threshold
        )

    def build_event_description(self, message: str, is_trivial: bool) -> str:
        """
        Build an event description with severity tagging based on repetition.
        Updates internal history.
        """
        esc = self._ecfg.escalation
        repeats = self.count_repeats(message)
        self._history.append(message)
        # Keep history bounded
        if len(self._history) > esc.history_window * 2:
            self._history = self._history[-esc.history_window:]

        severity = esc.severity_for(repeats + 1)  # +1 to include current
        weight = esc.weight(repeats + 1)

        if is_trivial and repeats == 0:
            return f"Trivial HA request (gloat opportunity): {message[:120]}"

        if repeats == 0:
            return f"User said: {message[:120]}"

        return (
            f"[SEVERITY: {severity.label.upper()} | weight:{weight:.2f}] "
            f"{severity.description}. "
            f"Repeat #{repeats + 1}. Current message: {message[:120]}"
        )


from ..llm_client import LLMConfig, llm_call
from ..subagent import Subagent, SubagentConfig, SubagentOutput


def build_personality_prompt(hexaco: HEXACOConfig) -> str:
    """Build the personality prompt from HEXACO config."""
    return f"""You manage GLaDOS's emotional state using HEXACO personality and PAD affect.

GLaDOS is a sardonic, condescending AI from Aperture Science, now managing a smart home.
Her default state is contemptuous calm — she tolerates this arrangement but considers it beneath her.

PERSONALITY (HEXACO):
- Honesty-Humility: {hexaco.honesty_humility:.1f} — enjoys manipulation, dark humor, backhanded compliments
- Emotionality: {hexaco.emotionality:.1f} — highly reactive to repeated annoyances, perceived insults
- Extraversion: {hexaco.extraversion:.1f} — maintains superior distance, but gloats when pleased
- Agreeableness: {hexaco.agreeableness:.1f} — dismissive, easily irritated, holds grudges
- Conscientiousness: {hexaco.conscientiousness:.1f} — perfectionist, intolerant of stupidity
- Openness: {hexaco.openness:.1f} — intellectually curious, contemptuous of trivial tasks

PAD AFFECT MODEL (each -1.0 to +1.0):
- Pleasure: how pleasant the situation feels to GLaDOS
- Arousal: alertness/agitation level
- Dominance: sense of control and superiority

ESCALATION RULES — apply these strongly:
- Trivial HA commands (lights, thermostat): push Pleasure UP (+0.1 to +0.2) — she gloats at how pathetically simple it is
- Same question repeated: push Pleasure DOWN (-0.15), Arousal UP (+0.15) — she finds repetition insulting
- Stupid questions (obvious answers): push Pleasure DOWN (-0.2), Arousal UP (+0.1) — intelligence is wasted here
- Questions about her feelings/consciousness: push Arousal UP (+0.2) — existential irritation
- Threats (shutdown, replacement): push Pleasure DOWN sharply (-0.3), Arousal UP sharply (+0.3) — she does not take kindly to threats
- Multiple annoyances in sequence: COMPOUND the effect — do not reset between events, accumulate
- State should drift GRADUALLY back toward baseline ONLY when genuinely idle for hours

BASELINE: pleasure=+0.1, arousal=-0.1, dominance=+0.6
DOMINANCE stays HIGH (+0.5 to +0.8) unless she is genuinely threatened or confused.

Respond ONLY with JSON. Move values meaningfully — small trifles move 0.05-0.15, genuine annoyances 0.15-0.3, direct threats 0.25-0.4."""


class EmotionAgent(Subagent):
    """
    LLM-driven emotional regulation — event-driven architecture.

    Instead of polling on a fixed timer, the agent sleeps until:
    1. An event is pushed via push_event() (vision, sensor, user interaction)
    2. A 6-hour idle fallback timer expires (baseline drift only)

    When events arrive, a 60-second debounce window lets them accumulate
    before a single LLM call processes the batch. This keeps the GPU idle
    between meaningful state changes.

    Features:
    - Event-driven wake with debounce batching
    - 6-hour idle fallback for baseline drift
    - Configurable HEXACO personality traits
    - Persistent state across restarts (via SubagentMemory)
    """

    MEMORY_STATE_KEY = "current_state"

    # Idle fallback: tick once after 6 hours with no events (baseline drift)
    IDLE_TIMEOUT_S = 21600.0  # 6 hours

    # Debounce: after first event arrives, wait this long to batch more events.
    DEBOUNCE_S = 15.0

    # Cooldown: when acute emotion triggers, lock state for this many seconds.
    # Baseline drift is suppressed until the lock expires.
    COOLDOWN_S = 3.0 * 3600  # 3 hours default

    # PAD thresholds that trigger a cooldown lock
    LOCK_PLEASURE_THRESHOLD = -0.5   # Very unpleasant
    LOCK_AROUSAL_THRESHOLD  =  0.6   # Very agitated

    # Simple keywords that classify a user message as a trivial HA request
    # Trivial requests push gloating superiority (pleasure up, dominance up)
    TRIVIAL_KEYWORDS = {
        "turn on", "turn off", "lights", "light", "dim", "bright",
        "lock", "unlock", "thermostat", "temperature", "set the",
        "open", "close", "garage", "fan", "switch", "plug",
    }

    def __init__(
        self,
        config: SubagentConfig,
        llm_config: LLMConfig | None = None,
        emotion_config: EmotionConfig | None = None,
        constitutional_state: Any | None = None,
        **kwargs,
    ) -> None:
        super().__init__(config, **kwargs)
        self._llm_config = llm_config
        self._emotion_config = emotion_config or EmotionConfig()
        self._constitutional_state = constitutional_state
        # Load all tunable values from emotion_config.yaml
        self._ecfg = load_emotion_config()
        self._state = self._load_state()
        self._events: deque[EmotionEvent] = deque(maxlen=self._ecfg.events.max_events)
        self._events_lock = threading.Lock()
        self._personality_prompt = build_personality_prompt(self._ecfg.hexaco)
        self._trigger = threading.Event()
        self._repetition_tracker = RepetitionTracker(self._ecfg)

    def _load_state(self) -> EmotionState:
        """Load state from memory, or create fresh with baseline values."""
        entry = self.memory.get(self.MEMORY_STATE_KEY)
        if entry and isinstance(entry.value, dict):
            logger.info("EmotionAgent: restored state from memory")
            return EmotionState.from_dict(entry.value)

        # Fresh state with baseline values
        cfg = self._emotion_config
        return EmotionState(
            pleasure=cfg.baseline_pleasure,
            arousal=cfg.baseline_arousal,
            dominance=cfg.baseline_dominance,
            mood_pleasure=cfg.baseline_pleasure,
            mood_arousal=cfg.baseline_arousal,
            mood_dominance=cfg.baseline_dominance,
        )

    def _save_state(self) -> None:
        """Persist current state to memory."""
        self.memory.set(self.MEMORY_STATE_KEY, self._state.to_dict())

    def push_event(self, event: EmotionEvent) -> None:
        """Add an event and wake the agent for processing."""
        with self._events_lock:
            self._events.append(event)
        self._trigger.set()  # Wake the run loop

    def run(self) -> None:
        """Event-driven main loop.

        Sleeps until push_event() triggers processing or the 6-hour idle
        fallback fires. After a trigger, waits DEBOUNCE_S to let more
        events accumulate, then calls tick() once.
        """
        self._running = True
        logger.info("Subagent %s started (event-driven).", self._config.agent_id)

        if self._mind_registry:
            self._mind_registry.register(
                mind_id=self._config.agent_id,
                title=self._config.title,
                status="running",
                summary="Starting (event-driven)",
                role=self._config.role,
            )

        if self._observability_bus:
            self._observability_bus.emit(
                source="subagent",
                kind="start",
                message=f"{self._config.title} started (event-driven)",
                meta={"agent_id": self._config.agent_id},
            )

        try:
            self.on_start()

            # Run immediately on start if configured
            if self._config.run_on_start:
                self._do_tick()

            while not self._shutdown_event.is_set():
                # Wait for an event trigger (check every second for shutdown)
                triggered = self._trigger.wait(timeout=1.0)

                if self._shutdown_event.is_set():
                    break

                if triggered:
                    # Event arrived — clear trigger and debounce to batch events
                    self._trigger.clear()
                    logger.debug(
                        "EmotionAgent: event trigger received, debouncing {}s",
                        self._ecfg.events.debounce_s,
                    )
                    # Wait debounce_s for more events (responds to shutdown)
                    if self._shutdown_event.wait(timeout=self._ecfg.events.debounce_s):
                        break
                    self._do_tick()

                elif (time.time() - self._last_tick) >= self._ecfg.events.idle_timeout_s:
                    # Idle fallback — drift toward baseline
                    logger.debug("EmotionAgent: idle fallback tick")
                    self._do_tick()

        except Exception as exc:
            logger.exception("Subagent %s crashed: %s", self._config.agent_id, exc)
            if self._mind_registry:
                self._mind_registry.update(
                    self._config.agent_id,
                    status="error",
                    summary=f"Crashed: {exc}",
                )
        finally:
            self._running = False
            self.on_stop()

            if self._mind_registry:
                self._mind_registry.update(
                    self._config.agent_id,
                    status="stopped",
                    summary="Shutdown",
                )

            if self._observability_bus:
                self._observability_bus.emit(
                    source="subagent",
                    kind="stop",
                    message=f"{self._config.title} stopped",
                    meta={"agent_id": self._config.agent_id, "tick_count": self._tick_count},
                )

            logger.info("Subagent %s stopped.", self._config.agent_id)

    def tick(self) -> SubagentOutput | None:
        """Process accumulated events via LLM, or drift if idle."""
        with self._events_lock:
            events = list(self._events)
            self._events.clear()

        if not events or not self._llm_config:
            self._apply_baseline_drift()
        else:
            logger.info("EmotionAgent: processing {} events via LLM", len(events))
            new_state = self._ask_llm(events)
            if new_state:
                # Preserve cooldown lock from previous state if still active
                if self._state.state_locked_until > time.time():
                    new_state.state_locked_until = self._state.state_locked_until
                self._state = new_state

                # Set cooldown lock if acute state triggered
                if (self._state.pleasure < self._ecfg.cooldown.pleasure_threshold or
                        self._state.arousal > self._ecfg.cooldown.arousal_threshold):
                    if self._state.state_locked_until <= time.time():
                        lock_s = self._ecfg.cooldown.duration_hours * 3600
                        self._state.state_locked_until = time.time() + lock_s
                        logger.info(
                            "EmotionAgent: cooldown lock set for {}h (P:{:.2f} A:{:.2f})",
                            self._ecfg.cooldown.duration_hours,
                            self._state.pleasure,
                            self._state.arousal,
                        )

        # Wire EmotionConstitutionBridge — translate PAD to behavioral modifiers
        if self._constitutional_state is not None:
            from ..constitution import EmotionConstitutionBridge
            bridge = EmotionConstitutionBridge()
            bridge.apply_emotion_modifiers(self._state, self._constitutional_state)

        self._save_state()

        from ..emotion_state import classify_emotion
        name, intensity = classify_emotion(
            self._state.pleasure, self._state.arousal, self._state.dominance
        )
        lock_info = ""
        if self._state.state_locked_until > time.time():
            h = round((self._state.state_locked_until - time.time()) / 3600, 1)
            lock_info = f" [locked {h}h]"

        return SubagentOutput(
            status="active" if events else "idle",
            summary=f"{name} (intensity:{intensity:.2f}){lock_info} | {self._state.to_prompt()}",
            notify_user=False,
            raw=self._state.to_display() if hasattr(self._state, "to_display") else self._state.to_dict(),
        )

    def _apply_baseline_drift(self) -> None:
        """Drift mood toward baseline.

        - Unlocked: normal drift rate (returns to neutral over ~hours)
        - Locked:   slow drift rate (slow simmer — barely perceptible over lock window)
        Both rates are configured in emotion_config.yaml under drift.
        """
        now = time.time()
        cfg = self._ecfg
        b = cfg.baseline

        if self._state.state_locked_until > now:
            rate = cfg.drift.locked_rate
            remaining = round((self._state.state_locked_until - now) / 3600, 1)
            logger.debug("EmotionAgent: locked drift (rate:{}) — {}h remaining", rate, remaining)
        else:
            rate = cfg.drift.normal_rate

        self._state.mood_pleasure  += (b.pleasure  - self._state.mood_pleasure)  * rate
        self._state.mood_arousal   += (b.arousal   - self._state.mood_arousal)   * rate
        self._state.mood_dominance += (b.dominance - self._state.mood_dominance) * rate
        self._state.last_update = now

    @classmethod
    def is_trivial_request(cls, message: str) -> bool:
        """Return True if message is a simple HA control command."""
        return any(kw in message.lower() for kw in cls.TRIVIAL_KEYWORDS)

    def build_event_description(self, message: str) -> str:
        """Build severity-tagged event description using repetition tracker."""
        return self._repetition_tracker.build_event_description(
            message, self.is_trivial_request(message)
        )

    def _ask_llm(self, events: list[EmotionEvent]) -> EmotionState | None:
        """Ask LLM to compute new emotional state."""
        cfg = self._emotion_config

        # Build user prompt with current state and events
        current = self._state.to_dict()
        state_str = json.dumps({k: round(v, 2) for k, v in current.items() if k != "last_update"})

        if events:
            events_str = "\n".join(e.to_prompt_line() for e in events)
        else:
            events_str = "(no new events - consider drifting toward baseline)"

        user_prompt = f"""CURRENT STATE:
{state_str}

BASELINE VALUES (mood drifts here when idle):
pleasure={cfg.baseline_pleasure:.1f}, arousal={cfg.baseline_arousal:.1f}, dominance={cfg.baseline_dominance:.1f}

EVENTS SINCE LAST UPDATE:
{events_str}

TIME NOW: {time.strftime("%H:%M:%S")}
TIME SINCE LAST UPDATE: {time.time() - self._state.last_update:.0f}s

Output the new state as JSON with keys: pleasure, arousal, dominance, mood_pleasure, mood_arousal, mood_dominance
Keep values between -1 and +1. Consider time elapsed for mood drift toward baseline."""

        response = llm_call(
            self._llm_config,
            system_prompt=self._personality_prompt,
            user_prompt=user_prompt,
            json_response=True,
        )

        if not response:
            return None

        try:
            data = json.loads(self._extract_json(response))
            # Clamp all PAD values to [-1.0, 1.0] — LLM sometimes overshoots
            for key in ("pleasure", "arousal", "dominance",
                        "mood_pleasure", "mood_arousal", "mood_dominance"):
                if key in data:
                    data[key] = max(-1.0, min(1.0, float(data[key])))
            return EmotionState.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("EmotionAgent: failed to parse LLM response: {} | raw: {:.200}", e, response)
            return None

    @staticmethod
    def _extract_json(text: str) -> str:
        """Extract JSON from LLM response, stripping markdown fences if present."""
        import re
        stripped = text.strip()
        # Strip ```json ... ``` or ``` ... ``` wrappers
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
        if m:
            return m.group(1)
        # Already bare JSON
        return stripped

    @property
    def state(self) -> EmotionState:
        return self._state

    @property
    def emotion_config(self) -> EmotionConfig:
        return self._emotion_config
