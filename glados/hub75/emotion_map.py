"""
Attitude tag and PAD emotion resolver for the HUB75 display.

Maps GLaDOS's 18 attitude directives to EyeState, and modulates
EyeParams based on PAD (Pleasure-Arousal-Dominance) values.

Pure data + math — no I/O.
"""

from __future__ import annotations

from .state_machine import EyeParams, EyeState

# ── Attitude tag → EyeState mapping ──────────────────────────

ATTITUDE_TO_EYE_STATE: dict[str, EyeState] = {
    "cold_professional":       EyeState.THINKING,
    "quiet_menace":            EyeState.ANGRY,
    "withering_contempt":      EyeState.ANGRY,
    "theatrical_exasperation": EyeState.ANGRY,
    "sinister_helpfulness":    EyeState.CURIOUS,
    "passive_aggressive":      EyeState.THINKING,
    "bored_superiority":       EyeState.SLEEPING,
    "grudging_competence":     EyeState.IDLE,
    "dark_amusement":          EyeState.CURIOUS,
    "faux_sympathy":           EyeState.CURIOUS,
    "ominous_cheerfulness":    EyeState.ALERT,
    "disappointed_parent":     EyeState.ANGRY,
    "thinly_veiled_hostility": EyeState.ANGRY,
    "scientific_detachment":   EyeState.THINKING,
    "martyrdom":               EyeState.SLEEPING,
    "outright_hostility":      EyeState.ANGRY,
    "weary_resignation":       EyeState.SLEEPING,
    "condescending_patience":  EyeState.THINKING,
}


def resolve_attitude(tag: str | None) -> EyeState:
    """Map an attitude tag to an EyeState.

    Returns ``EyeState.IDLE`` for unknown or ``None`` tags.
    """
    if tag is None:
        return EyeState.IDLE
    return ATTITUDE_TO_EYE_STATE.get(tag, EyeState.IDLE)


# ── PAD modulation ────────────────────────────────────────────


def apply_pad_modulation(
    params: EyeParams,
    pleasure: float,
    arousal: float,
    dominance: float,
) -> EyeParams:
    """Apply PAD emotional modulation on top of base EyeParams.

    Returns a **new** EyeParams — never mutates the input.

    Modulation rules:
      - Arousal (A, -1..+1): scales ``pulse_speed`` by ``(1 + A * 0.4)``
      - Pleasure (P, -1..+1): warms iris colour — R += 10*P, G += 5*P
      - Dominance (D, -1..+1): scales ``brightness`` by ``(1 + D * 0.2)``

    All values are clamped to valid ranges after modulation.
    """
    # Arousal → pulse speed
    new_pulse_speed = params.pulse_speed * (1.0 + arousal * 0.4)
    new_pulse_speed = max(0.1, new_pulse_speed)

    # Pleasure → colour warmth
    r = int(params.iris_color[0] + 10.0 * pleasure)
    g = int(params.iris_color[1] + 5.0 * pleasure)
    b = params.iris_color[2]
    r = min(255, max(0, r))
    g = min(255, max(0, g))

    # Dominance → brightness
    new_brightness = params.brightness * (1.0 + dominance * 0.2)
    new_brightness = min(1.0, max(0.0, new_brightness))

    return EyeParams(
        iris_color=(r, g, b),
        iris_radius=params.iris_radius,
        top_lid=params.top_lid,
        bottom_lid=params.bottom_lid,
        brightness=new_brightness,
        pulse_speed=new_pulse_speed,
        pulse_depth=params.pulse_depth,
        offset_x=params.offset_x,
        offset_y=params.offset_y,
        glow=params.glow,
    )
