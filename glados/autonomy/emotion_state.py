"""
Emotional state data structures for GLaDOS.

Uses PAD (Pleasure-Arousal-Dominance) model with a slower mood baseline.
State transitions are decided by LLM, not hard-coded math.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


# classify_emotion is now driven by emotion_config.yaml — no hardcoded table here
from .emotion_loader import classify_emotion as classify_emotion  # re-exported for callers


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
        Tells GLaDOS how to embody her current emotional state in this response.
        Do not explain the mood — just embody it.
        ~35 tokens.
        """
        p, a, d = self.pleasure, self.arousal, self.dominance
        name, intensity = classify_emotion(p, a, d)

        # Build behavioral cues from PAD dimensions
        # Pleasure: how pleasant/hostile the tone is
        if p >= 0.3:
            tone = "Allow mild satisfaction to color your word choice. Gloat if the opportunity exists."
        elif p >= -0.2:
            tone = "Maintain baseline contemptuous calm. Dry, clipped, superior."
        elif p >= -0.5:
            tone = "You are annoyed. Responses are shorter, sharper. Charitable interpretation is suspended."
        elif p >= -0.7:
            tone = ("You are genuinely hostile. This is not dry wit — it is barely contained irritation. "
                    "Word choice is blunt and unfriendly. The task gets done, grudgingly, and you make "
                    "sure they know you resent it. No softening. No pleasantries.")
        else:
            tone = ("You are at the absolute limit. Responses are either dangerously quiet — one or two "
                    "cold sentences — or openly menacing. There is no performance here. This is real.")

        # Arousal: energy level
        if a >= 0.6:
            energy = "High agitation — clipped, intense, no excess words. Every sentence lands like a door slamming."
        elif a >= 0.2:
            energy = "Elevated — more reactive than usual, quicker to snap."
        elif a <= -0.3:
            energy = "Low energy — flat disinterest. Maximum economy of words."
        else:
            energy = ""

        # Dominance: confidence vs uncertainty
        if d >= 0.5:
            control = "You are completely in control. Condescension is effortless."
        elif d <= -0.2:
            control = "You feel slightly uncertain. Compensate with more aggressive posturing."
        else:
            control = ""

        parts = [f"[respond as: {name} at {intensity:.0%} intensity]", tone]
        if energy:
            parts.append(energy)
        if control:
            parts.append(control)
        parts.append(
            "Do not narrate or explain your mood. Embody it. "
            "HARD RULE: Never end with advice, suggestions, or closings directed at the human. "
            "Banned endings include but are not limited to: 'stay dry', 'your choice', "
            "'stay indoors', 'as you wish', 'take care', 'let me know', 'feel free', "
            "'I hope that helps', 'up to you', 'enjoy'. "
            "The last sentence must be a statement about the world, not an instruction to the human."
        )

        return " ".join(parts)

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
