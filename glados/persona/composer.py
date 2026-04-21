"""Phase 8.7 — response composer.

The composer sits between the planner's execute decision and the
user-facing speech. Three output modes are supported:

  - `silent` — emit empty speech (TTS produces nothing; chat returns
    the empty string, which the API layer already maps to no reply).
  - `chime` — hand back a sentinel token the audio pipeline knows to
    replace with a short sound file rather than synthesising speech.
  - `quip`  — pick a pre-written Portal-voice line from the on-disk
    quip library. No device names leak into the line.
  - `LLM`   — keep the LLM-emitted speech verbatim (pre-8.7 behaviour,
    still the default so deploys don't change spoken output until
    the operator opts in).

Inputs are a ComposeRequest dataclass. Output is a ComposedSpeech
(text + which mode was actually used; the caller honours that mode
when routing to TTS vs a chime file).

This module is intentionally LLM-free — the grammar-constrained
Qwen3 fallback lives in a separate follow-up commit (§8.7d). That
keeps the quip path unit-testable without Ollama up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .quip_selector import (
    QuipLibrary,
    QuipRequest,
    VALID_CATEGORIES,
    mood_from_affect,
)


ResponseMode = Literal["silent", "chime", "quip", "LLM"]
VALID_MODES: frozenset[str] = frozenset({"silent", "chime", "quip", "LLM"})


_BRIGHTEN_TOKENS: frozenset[str] = frozenset({
    "brighten", "brighter", "up", "raise", "increase", "more",
    "higher", "light",
})
_DIM_TOKENS: frozenset[str] = frozenset({
    "dim", "dimmer", "down", "lower", "reduce", "decrease", "less",
    "darken", "darker", "soften",
})


def classify_intent(service: str, utterance: str = "") -> str:
    """Map a service-call + utterance to a quip-library intent
    directory. Returns one of turn_on / turn_off / brightness_up /
    brightness_down / scene_activate / generic.

    Brightness direction only resolves when the utterance itself
    contains an unambiguous direction keyword — otherwise the caller
    gets turn_on/turn_off and the quip library's matching file
    provides the line."""
    svc = (service or "").lower()
    # Split on domain.service and the bare form.
    suffix = svc.split(".", 1)[-1]
    if suffix.startswith("turn_off") or suffix == "turn_off":
        return "turn_off"
    if "scene" in svc:
        return "scene_activate"
    if suffix.startswith("turn_on") or suffix == "turn_on":
        # Brightness direction heuristic — only fires when the
        # utterance has a clearly-worded direction.
        words = {
            w.strip(".,!?;:'\"").lower() for w in (utterance or "").split()
        }
        if words & _DIM_TOKENS:
            return "brightness_down"
        if words & _BRIGHTEN_TOKENS - {"light", "up"}:
            # 'light' is ambiguous (noun vs verb); require a stronger
            # hint before claiming brightness_up.
            return "brightness_up"
        return "turn_on"
    return "generic"


@dataclass(frozen=True)
class ComposeRequest:
    """Everything the composer needs to choose output for one turn.

    `llm_speech` is the optimistic line the planner produced via the
    LLM. The composer either uses it (LLM mode), replaces it with a
    quip (quip mode), emits a chime sentinel (chime mode), or emits
    nothing (silent mode)."""
    event_category: str           # "command_ack" | "query_answer" | ...
    intent: str                   # "turn_on", "turn_off", "brightness_up", ...
    llm_speech: str = ""
    outcome: str = "success"
    entity_count: int = 1
    affect: dict[str, float] | None = None
    time_of_day: str = ""
    mode: ResponseMode = "LLM"


@dataclass(frozen=True)
class ComposedSpeech:
    """Composer output. `mode` reflects the mode that was effectively
    used, which may differ from the request's mode when the requested
    path has no content to deliver and the composer falls back to
    LLM. Callers route to TTS/chime based on this field."""
    text: str
    mode: ResponseMode


# Chime mode returns this sentinel. The TTS / audio pipeline is
# responsible for intercepting it and playing the operator's chime
# file instead of running synthesis.
CHIME_SENTINEL = "\x00chime\x00"


def compose(req: ComposeRequest, library: QuipLibrary | None) -> ComposedSpeech:
    """Decide the spoken output for this turn.

    Fallback behaviour when the requested mode has no content:
      - quip mode with an empty library → falls back to LLM speech
      - silent / chime modes always honour the request
    """
    if req.mode not in VALID_MODES:
        return ComposedSpeech(text=req.llm_speech or "", mode="LLM")

    if req.mode == "silent":
        return ComposedSpeech(text="", mode="silent")

    if req.mode == "chime":
        return ComposedSpeech(text=CHIME_SENTINEL, mode="chime")

    if req.mode == "quip":
        if req.event_category not in VALID_CATEGORIES or library is None:
            # Graceful degrade — no quip category for this event, or no
            # library loaded yet; keep the LLM's speech.
            return ComposedSpeech(text=req.llm_speech or "", mode="LLM")
        quip_req = QuipRequest(
            event_category=req.event_category,
            intent=req.intent,
            outcome=req.outcome,
            mood=mood_from_affect(req.affect),
            entity_count=req.entity_count,
            time_of_day=req.time_of_day,
        )
        picked = library.pick(quip_req)
        if picked:
            return ComposedSpeech(text=picked, mode="quip")
        return ComposedSpeech(text=req.llm_speech or "", mode="LLM")

    # LLM mode — pass the optimistic speech through unchanged.
    return ComposedSpeech(text=req.llm_speech or "", mode="LLM")


__all__ = [
    "CHIME_SENTINEL",
    "ComposeRequest",
    "ComposedSpeech",
    "ResponseMode",
    "VALID_MODES",
    "compose",
]
