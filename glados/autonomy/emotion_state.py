"""
Emotional state data structures for GLaDOS.

Uses PAD (Pleasure-Arousal-Dominance) model with a slower mood baseline.
State transitions are decided by LLM, not hard-coded math.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional


# classify_emotion is now driven by emotion_config.yaml — no hardcoded table here
from .emotion_loader import classify_emotion as classify_emotion  # re-exported for callers


# ── Canonical PAD band names ────────────────────────────────────────────
#
# Bucket boundaries mirror the tone-directive bands in
# EmotionState.to_response_directive() so callers that key their own
# behaviour off pleasure (TTS param override, persona rewriter overlay)
# stay in lockstep with the LLM-side tone guidance. One source of truth.

_BAND_PLEASED    = "pleased"
_BAND_CALM       = "contemptuous"
_BAND_ANNOYED    = "annoyed"
_BAND_HOSTILE    = "hostile"
_BAND_MENACING   = "menacing"


def pad_band_name(pleasure: float) -> str:
    """Map pleasure to the canonical band name used across the pipeline."""
    if pleasure >= 0.3:
        return _BAND_PLEASED
    if pleasure >= -0.2:
        return _BAND_CALM
    if pleasure >= -0.5:
        return _BAND_ANNOYED
    if pleasure >= -0.7:
        return _BAND_HOSTILE
    return _BAND_MENACING


@dataclass
class EmotionState:
    """
    Current emotional state using PAD dimensions.

    State responds quickly to events. Mood drifts slowly toward state.
    All values range from -1 to +1.

    state_locked_until: Unix timestamp. When set, baseline drift is suppressed
    until this time passes. Set by EmotionAgent when acute emotion triggers
    (e.g. frustration from repeated questions). Implements 3-4 hour cooldown.
    """

    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    mood_pleasure: float = 0.0
    mood_arousal: float = 0.0
    mood_dominance: float = 0.0

    last_update: float = field(default_factory=time.time)
    state_locked_until: float = 0.0  # 0 = not locked

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmotionState:
        def _clamp(v: float) -> float:
            return max(-1.0, min(1.0, v))
        return cls(
            pleasure=_clamp(float(data.get("pleasure", 0.0))),
            arousal=_clamp(float(data.get("arousal", 0.0))),
            dominance=_clamp(float(data.get("dominance", 0.0))),
            mood_pleasure=_clamp(float(data.get("mood_pleasure", 0.0))),
            mood_arousal=_clamp(float(data.get("mood_arousal", 0.0))),
            mood_dominance=_clamp(float(data.get("mood_dominance", 0.0))),
            last_update=float(data.get("last_update", time.time())),
            state_locked_until=float(data.get("state_locked_until", 0.0)),
        )

    def to_prompt(self) -> str:
        """Human-readable summary for injection into main agent context."""
        p, a, d = self.pleasure, self.arousal, self.dominance
        name, intensity = classify_emotion(p, a, d)
        return (
            f"[emotion] {name} (intensity: {intensity:.2f}) "
            f"| P:{p:+.2f} A:{a:+.2f} D:{d:+.2f}"
        )

    def to_response_directive(self) -> str:
        """
        Behavioral directive injected immediately before the user message.

        Phase Emotion-G (2026-04-22): rewritten as terse hard-rule bullets
        instead of paragraph prose. The LLM treats prose as flavour to
        sample from — adjective choices — but it follows bullet rules.
        Focus is on AUDIBLE cues: sentence counts drive pacing, periods
        create full stops, em-dashes create beats Piper honours. Italics
        removed entirely — invisible at the TTS layer, so no value.

        Band bucketing matches pad_band_name(). Keyword markers
        ("contemptuous", "annoyed", "hostile", "barely contained",
        "dangerously quiet", "menacing") preserved for TestToneDirective.
        """
        p, a, d = self.pleasure, self.arousal, self.dominance
        name, intensity = classify_emotion(p, a, d)

        # Pleasure: primary mood bucket. Drives sentence count, word
        # choice register, and consequence language. One bucket per band;
        # each bucket is 3-5 terse rules, never a paragraph.
        if p >= 0.3:
            tone_rules = [
                "MOOD: mild satisfaction.",
                "LENGTH: one or two sentences.",
                "TONE: one well-placed quip. Gloat briefly if warranted.",
            ]
        elif p >= -0.2:
            tone_rules = [
                "MOOD: contemptuous calm. Dry. Superior.",
                "LENGTH: one to three complete sentences.",
                "TONE: condescension is implied, never announced.",
            ]
        elif p >= -0.5:
            tone_rules = [
                "MOOD: annoyed. Tone sharper.",
                "LENGTH: THREE sentences MAX.",
                "CADENCE: short declarative sentences. No softeners, no 'perhaps', no hedging.",
                "CONTENT: acknowledge the request, then ONE pointed jab at the operator's failure to think.",
                "PUNCTUATION: one em-dash permitted — placed where the mistake sits.",
            ]
        elif p >= -0.7:
            tone_rules = [
                "MOOD: openly hostile. Barely contained.",
                "LENGTH: THREE sentences or fewer.",
                "CADENCE: clipped. Each sentence lands like a door closing.",
                "CONTENT: deliver the information, then a direct remark about the operator's conduct — that they have asked this before, that they are not listening, that your patience is being spent.",
                "CONSEQUENCE: state plainly that the next repeat will be met with silence, or that this is your final answer on the matter.",
                "PUNCTUATION: em-dashes for beats — no decoration.",
                "BAN: no exclamation marks. Intensity is concision, not volume.",
            ]
        else:
            tone_rules = [
                "MOOD: dangerously quiet. Menacing.",
                "LENGTH: TWO sentences maximum.",
                "CADENCE: slow. Deliberate. Every period a full stop.",
                "ADDRESS: refer to the operator as 'test subject' or in the third person — deliberate distance.",
                "CONSEQUENCE: state plainly that the next repeat will be met with silence.",
                "CONTENT: no explanations. No framing. Let the shortness do the work.",
                "BAN: no exclamation marks. No advice. No closings.",
            ]

        # Arousal: pacing / energy level. Overlays additional rate cues.
        if a >= 0.6:
            energy_rules = ["RATE: high agitation. Cut every optional word."]
        elif a >= 0.2:
            energy_rules = ["RATE: elevated. Quicker to snap. Cut softeners."]
        elif a <= -0.3:
            energy_rules = ["RATE: low energy. Flat and unimpressed."]
        else:
            energy_rules = []

        # Dominance: confidence overlay.
        if d >= 0.5:
            control_rules = ["CONFIDENCE: completely in control. Condescension is effortless."]
        elif d <= -0.2:
            control_rules = ["CONFIDENCE: slightly uncertain — compensate with sharper posturing."]
        else:
            control_rules = []

        all_rules = tone_rules + energy_rules + control_rules
        body = "\n".join(f"- {r}" for r in all_rules)

        header = f"[respond as: {name} at {intensity:.0%} intensity]"
        footer = (
            "Embody the state; do not narrate it. "
            "HARD RULE: Never end with advice, suggestions, or closings directed at the human. "
            "Banned endings include but are not limited to: 'stay dry', 'your choice', "
            "'stay indoors', 'as you wish', 'take care', 'let me know', 'feel free', "
            "'I hope that helps', 'up to you', 'enjoy'. "
            "The last sentence must be a statement about the world, not an instruction to the human."
        )

        return f"{header}\n{body}\n{footer}"

    def to_display(self) -> dict:
        """Full display dict for test scripts and logging."""
        p, a, d = self.pleasure, self.arousal, self.dominance
        name, intensity = classify_emotion(p, a, d)
        return {
            "emotion": name,
            "intensity": intensity,
            "pleasure": round(p, 3),
            "arousal": round(a, 3),
            "dominance": round(d, 3),
            "mood_pleasure": round(self.mood_pleasure, 3),
            "mood_arousal": round(self.mood_arousal, 3),
            "mood_dominance": round(self.mood_dominance, 3),
        }


@dataclass(frozen=True)
class EmotionEvent:
    """
    An event that may affect emotional state.

    Uses natural language description - LLM interprets the semantics.
    """

    source: str  # "user", "vision", "system"
    description: str
    timestamp: float = field(default_factory=time.time)

    def to_prompt_line(self) -> str:
        """Format for inclusion in LLM prompt."""
        age = time.time() - self.timestamp
        if age < 60:
            age_str = f"{age:.0f}s ago"
        else:
            age_str = f"{age / 60:.1f}m ago"
        return f"- [{self.source}] {self.description} ({age_str})"


# ── PAD state provider registry ─────────────────────────────────────────
#
# Modules outside the autonomy package (TTS layer, persona rewriter)
# need to key their behaviour off the current PAD state without taking
# a direct dependency on the EmotionAgent singleton. The agent registers
# itself on construction via set_pad_state_provider(); consumers read
# through current_pad_state() / current_pad_band(). Both return None if
# no provider has registered yet, so callers can no-op gracefully on
# startup before the agent is wired.

_pad_state_provider: Optional[Callable[[], "EmotionState | None"]] = None


def set_pad_state_provider(fn: Callable[[], "EmotionState | None"]) -> None:
    """Register the callable that returns the current EmotionState."""
    global _pad_state_provider
    _pad_state_provider = fn


def current_pad_state() -> "EmotionState | None":
    fn = _pad_state_provider
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def current_pad_band() -> str | None:
    st = current_pad_state()
    if st is None:
        return None
    return pad_band_name(st.pleasure)
