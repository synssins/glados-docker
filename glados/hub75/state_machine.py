"""
Eye state machine for the HUB75 display.

Defines EyeState enum, EyeParams dataclass with per-state defaults,
priority rules, and transition logic.  Pure data — no I/O, no threading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class EyeState(Enum):
    """Possible eye display states, ordered by typical priority."""
    IDLE = auto()
    SPEAKING = auto()
    ANGRY = auto()
    THINKING = auto()
    CURIOUS = auto()
    ALERT = auto()
    SLEEPING = auto()
    GIF = auto()


@dataclass
class EyeParams:
    """Tunable eye rendering parameters.

    All spatial values are in pixels on the 64x64 panel.
    Color is RGB (0-255 per channel).
    """
    iris_color: tuple[int, int, int] = (220, 0, 0)
    iris_radius: float = 13.0
    top_lid: float = 0.0       # 0 = fully open, 1 = fully closed
    bottom_lid: float = 0.0    # 0 = fully open, 1 = fully closed
    brightness: float = 1.0    # 0.0 – 1.0
    pulse_speed: float = 1.5   # radians per second
    pulse_depth: float = 0.15  # fraction of brightness that pulses
    offset_x: float = 0.0      # pixels from panel center
    offset_y: float = 0.0      # pixels from panel center
    glow: float = 0.3          # halo intensity 0 – 1

    def copy(self) -> EyeParams:
        """Return a shallow copy."""
        return EyeParams(
            iris_color=self.iris_color,
            iris_radius=self.iris_radius,
            top_lid=self.top_lid,
            bottom_lid=self.bottom_lid,
            brightness=self.brightness,
            pulse_speed=self.pulse_speed,
            pulse_depth=self.pulse_depth,
            offset_x=self.offset_x,
            offset_y=self.offset_y,
            glow=self.glow,
        )


# ── Per-state defaults ────────────────────────────────────────

DEFAULT_PARAMS: dict[EyeState, EyeParams] = {
    EyeState.IDLE: EyeParams(
        iris_color=(180, 0, 0),
        brightness=0.55,
        pulse_speed=0.8,
        pulse_depth=0.12,
        glow=0.25,
    ),
    EyeState.SPEAKING: EyeParams(
        iris_color=(230, 0, 0),
        brightness=1.0,
        pulse_speed=3.5,
        pulse_depth=0.20,
        glow=0.4,
    ),
    EyeState.ANGRY: EyeParams(
        iris_color=(255, 0, 0),
        top_lid=0.38,
        bottom_lid=0.30,
        brightness=1.0,
        pulse_speed=4.0,
        pulse_depth=0.10,
        glow=0.5,
    ),
    EyeState.THINKING: EyeParams(
        iris_color=(200, 80, 0),
        brightness=0.50,
        pulse_speed=0.5,
        pulse_depth=0.08,
        glow=0.2,
    ),
    EyeState.CURIOUS: EyeParams(
        iris_color=(220, 10, 0),
        brightness=0.85,
        pulse_speed=1.2,
        pulse_depth=0.15,
        glow=0.35,
    ),
    EyeState.ALERT: EyeParams(
        iris_color=(255, 20, 20),
        brightness=1.0,
        pulse_speed=6.0,
        pulse_depth=0.25,
        glow=0.6,
    ),
    EyeState.SLEEPING: EyeParams(
        iris_color=(80, 0, 0),
        top_lid=0.55,
        bottom_lid=0.55,
        brightness=0.20,
        pulse_speed=0.3,
        pulse_depth=0.05,
        glow=0.1,
    ),
    EyeState.GIF: EyeParams(
        brightness=0.0,  # DDP suspended during GIF/preset playback
    ),
}


# ── Priority table ────────────────────────────────────────────

STATE_PRIORITY: dict[EyeState, int] = {
    EyeState.ALERT:    10,
    EyeState.SPEAKING: 9,
    EyeState.GIF:      8,
    EyeState.ANGRY:    5,
    EyeState.CURIOUS:  5,
    EyeState.THINKING: 5,
    EyeState.IDLE:     2,
    EyeState.SLEEPING: 1,
}


def can_transition(current: EyeState, requested: EyeState) -> bool:
    """Return True if the requested state can interrupt the current one."""
    return STATE_PRIORITY.get(requested, 0) >= STATE_PRIORITY.get(current, 0)


def get_default_params(
    state: EyeState,
    overrides: dict[str, dict[str, float]] | None = None,
) -> EyeParams:
    """Return the default EyeParams for a state, with optional config overrides.

    Args:
        state: The eye state to look up.
        overrides: ``hub75.yaml`` ``eye_state_overrides`` dict — keys are
                   lowercase state names, values are dicts of field overrides.
    """
    params = DEFAULT_PARAMS.get(state, EyeParams()).copy()
    if overrides:
        state_overrides = overrides.get(state.name.lower(), {})
        for field_name, value in state_overrides.items():
            if hasattr(params, field_name):
                setattr(params, field_name, value)
    return params
