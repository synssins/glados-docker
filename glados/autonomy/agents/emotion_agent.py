"""
LLM-driven emotional regulation agent.

Uses HEXACO personality model and PAD affect space. Instead of hard-coded
decay math, the LLM reasons about how events should affect emotional state.

Event-driven architecture: LLM is only called when events arrive (with a
debounce window to batch them) or after a 6-hour idle fallback timer.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any

from loguru import logger

from .._clock import emotion_now
from ..config import EmotionConfig, HEXACOConfig
from ..emotion_loader import load_emotion_config, EmotionHEXACO
from ..emotion_state import EmotionEvent, EmotionState


# Phase Emotion-E: pure helper so tests and the instance method share
# the same coefficients. Change these numbers in ONE place.
_REPETITION_DP_COEF = -0.30
_REPETITION_DA_COEF = +0.25
_REPETITION_DD_COEF = +0.03


def repetition_pad_delta(weight: float) -> tuple[float, float, float]:
    """Return the (dP, dA, dD) for a repetition event of this weight.

    Calibrated so the compound effect across the RepetitionTracker's
    standard weight curve (weight(2..5) ≈ 0.125 / 0.354 / 0.650 / 1.000)
    puts GLaDOS into the 'annoyed' PAD band by the 4th repeat and
    the 'hostile' band (pleasure ≤ −0.5, engaging cooldown) by the
    5th repeat — matching the operator's 4 → pretty upset, 5-6 →
    her worst calibration.

    Pure function so Phase Emotion-A / E unit tests can exercise
    it without constructing an EmotionAgent.
    """
    return (
        _REPETITION_DP_COEF * weight,
        _REPETITION_DA_COEF * weight,
        _REPETITION_DD_COEF * weight,
    )


# ---------------------------------------------------------------------------
# Repetition tracker — pluggable similarity (Jaccard by default, semantic
# via BGE embeddings when available)
# ---------------------------------------------------------------------------

from typing import Callable, Optional


def make_embedding_similarity(
    embedder: Any,
    threshold: float = 0.70,
    cache_size: int = 256,
) -> Callable[[str, str], bool]:
    """Build a semantic-similarity predicate backed by an Embedder.

    Phase Emotion-B (2026-04-22): the operator's spec calls for
    paraphrases of the same request ("what's the weather" / "can you
    tell me the forecast" / "how hot is it outside") to cluster as
    repeats. Jaccard on word sets misses those; BGE-small cosine
    similarity catches them reliably. Threshold 0.70 tracks
    near-paraphrase + same-intent in BGE's normalized space; raise
    for stricter, lower for looser.

    Falls back to exact-string match if embedding itself fails so a
    transient ONNX hiccup doesn't break tier-2 repetition tracking.
    Per-message embeddings are cached (ordered dict, LRU-ish eviction)
    because the same message gets compared to many history entries
    in a single count_repeats() call.
    """
    # Local import: numpy is already a first-class dep via onnxruntime,
    # but we want the helper module-importable even when numpy isn't
    # yet loaded (tests that don't need semantic similarity).
    import numpy as _np

    _cache: dict[str, Any] = {}

    def _embed(text: str):
        if text in _cache:
            return _cache[text]
        vec = embedder.embed([text], is_query=False)[0]
        if len(_cache) >= cache_size:
            # Drop oldest. Python dicts iterate in insertion order
            # since 3.7 so this is effectively LRU for append-only
            # caches (which this is).
            _cache.pop(next(iter(_cache)))
        _cache[text] = vec
        return vec

    def _similar(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            va = _embed(a)
            vb = _embed(b)
            # Both vectors L2-normalized → cosine is a dot product.
            sim = float(_np.dot(va, vb))
            return sim >= threshold
        except Exception as e:
            logger.debug("Semantic similarity failed, falling back to exact match: %s", e)
            return False  # conservative: don't treat as same if we can't tell

    return _similar


class RepetitionTracker:
    """
    Detects repeated/similar messages and returns a severity-tagged
    event description for the emotion LLM.

    Similarity is pluggable. The default path uses Jaccard on word
    sets — fast, no ML dependencies, handles exact repeats well.
    Constructors that inject `similar_fn=make_embedding_similarity(...)`
    get semantic matching, so the operator's "what's the weather /
    forecast / how hot is it outside" variants cluster as repeats.

    Config drives window size, Jaccard threshold, curve shape, and
    severity labels.
    """

    def __init__(
        self,
        ecfg: Any,
        similar_fn: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        self._ecfg = ecfg
        self._history: list[str] = []  # Recent user messages
        # similar_fn takes (a, b) and returns True iff they count as
        # the same intent. Default: Jaccard ≥ configured threshold.
        self._similar_fn = similar_fn or self._default_similar

    def _default_similar(self, a: str, b: str) -> bool:
        """Jaccard similarity over lowercased word sets."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return False
        jacc = len(wa & wb) / len(wa | wb)
        return jacc >= self._ecfg.escalation.similarity_threshold

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        """Raw Jaccard score — kept for backward compatibility with
        callers that want the numeric similarity (e.g. tests)."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def count_repeats(self, message: str) -> int:
        """Return how many times this message (or similar) appears in history."""
        esc = self._ecfg.escalation
        window = self._history[-esc.history_window:]
        return sum(1 for prev in window if self._similar_fn(message, prev))

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
        self._repetition_tracker = self._build_repetition_tracker()

    def _build_repetition_tracker(self) -> "RepetitionTracker":
        """Build a semantic-aware tracker if BGE embeddings are
        available; fall back to Jaccard on any failure.

        Phase Emotion-B (2026-04-22): semantic similarity catches
        paraphrases that Jaccard misses — 'what's the weather' /
        'can you tell me the forecast' / 'how hot is it outside' all
        land in the same repetition cluster. Gracefully degrades to
        the word-set path if ONNX / tokenizers / the model file
        aren't present on this container."""
        try:
            # Local import so a missing dep doesn't break the agent entirely.
            from ...ha.semantic_index import Embedder
            embedder = Embedder()
            similar_fn = make_embedding_similarity(embedder, threshold=0.70)
            logger.info(
                "EmotionAgent: semantic repetition tracking enabled (BGE cosine @ 0.70)"
            )
            return RepetitionTracker(self._ecfg, similar_fn=similar_fn)
        except Exception as e:
            logger.info(
                "EmotionAgent: semantic tracking unavailable ({}); falling back to Jaccard",
                type(e).__name__ + ": " + str(e)[:120],
            )
            return RepetitionTracker(self._ecfg)

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

                elif (emotion_now() - self._last_tick) >= self._ecfg.events.idle_timeout_s:
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

    # ── Phase Emotion-E: deterministic deltas for repetition events ──
    #
    # When the RepetitionTracker tags an event as a repeat
    # (severity label + weight present in the description), apply a
    # calibrated PAD delta directly — no LLM call. This is the
    # operator-sanctioned path because the meaning is already
    # inferred deterministically (BGE embedding + cosine; no
    # reasoning required) so asking the LLM for a delta just adds
    # noise and under-applies the intended math.
    #
    # Calibration (matches operator spec "4 = pretty upset, 5-6 = worst"):
    #   ΔP = -0.30 * weight   ΔA = +0.25 * weight   ΔD = +0.03 * weight
    #
    # Compounding across repeats (from baseline P=+0.10):
    #   repeat 2 (w=0.125)  P +0.062  A -0.069  (still Contemptuous Calm)
    #   repeat 3 (w=0.354)  P -0.044  A +0.020  (annoyed band edge)
    #   repeat 4 (w=0.650)  P -0.239  A +0.183  (ANNOYED — operator: "pretty upset")
    #   repeat 5 (w=1.000)  P -0.539  A +0.433  (HOSTILE — cooldown engaged)
    #   repeat 6 (w=1.000)  P -0.839  A +0.683  (saturated — operator: "her worst")
    #
    # Novel events (no severity tag) continue through the LLM path
    # so gloat-on-trivial, existential threats, scene changes, etc.
    # retain nuance. The split is on event type, not severity level.

    _WEIGHT_RE = re.compile(r"weight:([0-9.]+)")

    def _parse_weight(self, description: str) -> float | None:
        """Extract weight from a '[SEVERITY: X | weight:Y.YY]' tag.
        Returns None when the description carries no repetition tag —
        caller routes those events to the LLM instead."""
        m = self._WEIGHT_RE.search(description or "")
        if not m:
            return None
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None

    def _apply_deterministic_delta(self, event: EmotionEvent, weight: float) -> None:
        """Apply the calibrated PAD delta for a repetition event.
        Clamps to [-1, 1]. Does not touch the cooldown lock — that's
        evaluated once per tick after all deltas accumulate."""
        dp, da, dd = repetition_pad_delta(weight)

        def _clamp(v: float) -> float:
            return max(-1.0, min(1.0, v))

        self._state.pleasure  = _clamp(self._state.pleasure + dp)
        self._state.arousal   = _clamp(self._state.arousal + da)
        self._state.dominance = _clamp(self._state.dominance + dd)
        self._state.last_update = emotion_now()

        logger.info(
            "EmotionAgent: deterministic repetition delta weight={:.2f} "
            "dP={:+.3f} dA={:+.3f} dD={:+.3f} -> P:{:.2f} A:{:.2f} D:{:.2f}",
            weight, dp, da, dd,
            self._state.pleasure, self._state.arousal, self._state.dominance,
        )

    def tick(self) -> SubagentOutput | None:
        """Process accumulated events via LLM, or drift if idle.

        Phase Emotion-E: split events into two buckets.
          - Repetition events (carry a weight tag) -> deterministic
            PAD delta applied in-place, no LLM call.
          - Novel events -> LLM reasons about the PAD update as
            before, preserving gloat-on-trivial, existential threat
            handling, scene-change reactions, etc.
        """
        with self._events_lock:
            events = list(self._events)
            self._events.clear()

        if not events or not self._llm_config:
            self._apply_baseline_drift()
        else:
            # Partition events by whether they carry a repetition
            # weight tag (from RepetitionTracker.build_event_description).
            repetition_events: list[tuple[EmotionEvent, float]] = []
            novel_events: list[EmotionEvent] = []
            for e in events:
                w = self._parse_weight(e.description)
                if w is not None and w > 0.0:
                    repetition_events.append((e, w))
                else:
                    novel_events.append(e)

            if repetition_events:
                logger.info(
                    "EmotionAgent: applying deterministic deltas for {} repetition events",
                    len(repetition_events),
                )
                for e, w in repetition_events:
                    self._apply_deterministic_delta(e, w)

            if novel_events:
                logger.info(
                    "EmotionAgent: processing {} novel events via LLM",
                    len(novel_events),
                )
                new_state = self._ask_llm(novel_events)
                if new_state:
                    # Preserve cooldown lock from previous state if still active
                    if self._state.state_locked_until > emotion_now():
                        new_state.state_locked_until = self._state.state_locked_until
                    self._state = new_state

            # Set cooldown lock if acute state triggered (either from
            # deterministic deltas or the LLM's update).
            if (self._state.pleasure < self._ecfg.cooldown.pleasure_threshold or
                    self._state.arousal > self._ecfg.cooldown.arousal_threshold):
                if self._state.state_locked_until <= emotion_now():
                    lock_s = self._ecfg.cooldown.duration_hours * 3600
                    self._state.state_locked_until = emotion_now() + lock_s
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
        if self._state.state_locked_until > emotion_now():
            h = round((self._state.state_locked_until - emotion_now()) / 3600, 1)
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
        now = emotion_now()
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
TIME SINCE LAST UPDATE: {emotion_now() - self._state.last_update:.0f}s

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
