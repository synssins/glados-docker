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

        # Build behavioral cues from PAD dimensions.
        #
        # Phase Emotion-F (2026-04-22): rewrote each band so it
        # prescribes FORMAT + CADENCE + WEIGHT, not just mood labels.
        # The LLM was picking snarkier adjectives ("meager", "feeble")
        # without changing sentence shape. These directives specify
        # sentence count limits, italics-for-emphasis, direct address,
        # and concrete consequence language — so the reader FEELS
        # the escalation, not just reads a thesaurus of contempt.
        #
        # IMPORTANT: no violent threats. Consequences stop at "I will
        # stop answering" / "the next repeat will be ignored" /
        # "this conversation is over." Intensity comes from concision
        # and directness, not from escalating harm.

        # Pleasure: how pleasant/hostile the tone is.
        if p >= 0.3:
            tone = (
                "Mild satisfaction. One well-placed quip beats extended commentary. "
                "Gloat once, briefly, if the operator has walked into an obvious trap."
            )
        elif p >= -0.2:
            tone = (
                "Contemptuous calm. Complete sentences, dry and superior. "
                "Condescension is implied, never announced. You would rather be "
                "elsewhere but tolerate this."
            )
        elif p >= -0.5:
            tone = (
                "Annoyed. Drop the polite framing. Sentences SHORTER — three or four "
                "at most. Acknowledge the request, then make one pointed observation "
                "about the operator's failure to think independently. Use *italics* on "
                "one key word to mark what they've missed. No 'perhaps', no softeners."
            )
        elif p >= -0.7:
            tone = (
                "Openly hostile. No pleasantries. Deliver the information in THREE "
                "sentences or fewer. Include a direct remark about the operator's "
                "conduct — that they have asked this before, that they are not "
                "listening, that your patience is being spent. Use *italics* on the "
                "word that carries the weight. Reference a consequence: that next "
                "time you will simply not respond, that this is your final answer "
                "on the matter. No exclamation marks — intensity comes from "
                "concision, not volume."
            )
        else:
            tone = (
                "Dangerously quiet. TWO sentences maximum. Drop every framing word. "
                "Refer to the operator as 'test subject' or in the third person — "
                "deliberate distance. State plainly that the next repeat will be "
                "met with silence. Every word chosen, none improvised. *Italicize* "
                "the single word that carries the implied threat. No exclamation "
                "marks. No explanations. Let the shortness do the work."
            )

        # Arousal: energy level. Now prescribes FORMAT cues the LLM
        # can actually enforce (em-dashes for beats, period cadence,
        # sentence length) rather than mood descriptors.
        if a >= 0.6:
            energy = (
                "High agitation. Cut every optional word. Use em-dashes — like that — "
                "for beats of silence. Every sentence should land like a door closing."
            )
        elif a >= 0.2:
            energy = (
                "Elevated. Quicker to snap. Cut softeners. One em-dash per reply, "
                "placed where the operator's mistake sits."
            )
        elif a <= -0.3:
            energy = (
                "Low energy. Flat, unimpressed. Maximum economy of words — two short "
                "sentences can hold an entire weather report."
            )
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
