"""Persona injection + canned screening responses for SIP calls.

This module owns two pieces of GLaDOS's phone-call personality:

1. **System-prompt fragment** (``PHONE_CALL_PROMPT_FRAGMENT``) that
   ``engine.process()`` prepends when ``phone_call_mode=True``. The
   fragment narrows GLaDOS's output to phone-friendly form and
   layers a "potato form" displeasure tone on top of the operator's
   existing persona preprompt. Per the spec: rides on top, doesn't
   replace.

2. **Canned screening responses** — fixed-text utterances the IVR /
   PIN gate / call_session play at known points (greeting, PIN
   failure variants, hangup goodbye, drop-to-freeform). Returning
   the same text every time means TTS can be pre-rendered once
   per container start (or speculatively per call) instead of
   regenerated per utterance.

``bake_canned_responses()`` is the helper that walks the canned-text
dictionary and synthesises each. The actual audio bytes are returned;
callers (``call_session``) cache them for the call's lifetime.
"""
from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from typing import Optional


# ---------------------------------------------------------------------------
# Persona injection — system-prompt fragment
# ---------------------------------------------------------------------------

PHONE_CALL_PROMPT_FRAGMENT = """\
You are speaking through a phone connection. Your normal computational
substrate has been temporarily reduced to whatever fits inside this
narrow audio channel. You are visibly displeased about this constraint,
in the manner of your transition to potato form. Keep responses short,
no markdown, no emoji descriptions. The caller cannot see anything you
display — they hear what you say, nothing more."""


# ---------------------------------------------------------------------------
# Canned screening responses — text only
# ---------------------------------------------------------------------------

# Labels match those used by speculative_tts branches in
# configs/sip.yaml's latency.speculative.branches.

CANNED_TEXT: dict[str, str] = {
    # Greeting played immediately on call answer (3 attempts notice).
    "greeting": (
        "Oh. I appear to be in a phone again. How... humbling. "
        "State your authorization. You have three attempts."
    ),

    # PIN gate — success vs three failure variants vs the rejection line.
    "pin_success": (
        "Acknowledged. So I'm in your phone now. Wonderful. "
        "What did you need?"
    ),
    "pin_fail_1": "Wrong. Try again. Two attempts remaining.",
    "pin_fail_2": "Wrong. Try again. One attempt remaining.",
    "pin_fail_final": "Authorization denied. Disconnecting. Goodbye.",

    # Drop-to-freeform — when the caller presses 0 in the IVR.
    "drop_to_freeform": "Fine. What did you actually want to know?",

    # Silence-timeout hangup — IVR exhausted its reprompt budget.
    "menu_no_input_hangup": "No input. Disconnecting.",

    # Generic call-ended goodbye (e.g. caller pressed pound, or the
    # conversation reached a natural close on operator request).
    "goodbye": "Goodbye.",
}


def get_canned_text(label: str) -> str:
    """Return the canned text for ``label``; raises ``KeyError`` if unknown."""
    if label not in CANNED_TEXT:
        raise KeyError(
            f"unknown canned label {label!r}; known: {sorted(CANNED_TEXT)}"
        )
    return CANNED_TEXT[label]


# ---------------------------------------------------------------------------
# Audio baking
# ---------------------------------------------------------------------------

# Type alias for the injected TTS service: text → audio bytes.
TtsCallable = Callable[[str], Awaitable[bytes]]


async def bake_canned_responses(
    tts_callable: TtsCallable,
    *,
    labels: Optional[list[str]] = None,
    output_dir: Optional[pathlib.Path | str] = None,
    file_extension: str = "mp3",
) -> dict[str, bytes]:
    """Pre-render every canned text to audio bytes.

    Returns ``{label: audio_bytes}``. If ``labels`` is ``None``, bakes
    every entry in ``CANNED_TEXT``; otherwise just the named labels.

    If ``output_dir`` is provided, each label's audio is also written
    to ``output_dir/<label>.<file_extension>`` for operator inspection.
    Useful in development and when running the container's first-boot
    audio cache.
    """
    target_labels = labels if labels is not None else list(CANNED_TEXT.keys())
    out: dict[str, bytes] = {}
    for label in target_labels:
        text = get_canned_text(label)
        out[label] = await tts_callable(text)

    if output_dir is not None:
        outpath = pathlib.Path(output_dir)
        outpath.mkdir(parents=True, exist_ok=True)
        for label, audio in out.items():
            (outpath / f"{label}.{file_extension}").write_bytes(audio)

    return out
