import io
import threading

import numpy as np
from numpy.typing import NDArray
import soundfile as sf

from glados.TTS import get_speech_synthesizer, list_available_voices, SpeechSynthesizerProtocol
from glados.utils import spoken_text_converter

# ── Voice-aware cache: each voice loaded once, reused forever ─────
_lock = threading.Lock()
_synthesizers: dict[str, SpeechSynthesizerProtocol] = {}
_converter: spoken_text_converter.SpokenTextConverter | None = None


def _get_synthesizer(voice: str = "glados") -> SpeechSynthesizerProtocol:
    if voice not in _synthesizers:
        with _lock:
            if voice not in _synthesizers:
                _synthesizers[voice] = get_speech_synthesizer(voice)
    return _synthesizers[voice]


def _get_converter() -> spoken_text_converter.SpokenTextConverter:
    global _converter
    if _converter is None:
        with _lock:
            if _converter is None:
                # Phase 8.10: pull pronunciation overrides so the
                # shared-cache converter matches the engine-side one.
                # Cached-forever is fine because operator saves trigger
                # a hot-reload which re-imports this module.
                try:
                    from glados.core.config_store import cfg as _pr_cfg
                    _pr = _pr_cfg.tts_pronunciation
                    _converter = spoken_text_converter.SpokenTextConverter(
                        symbol_expansions=dict(_pr.symbol_expansions),
                        word_expansions=dict(_pr.word_expansions),
                    )
                except Exception:
                    # Config not yet loadable (very early import path):
                    # fall back to an empty-overrides converter so the
                    # module stays importable.
                    _converter = spoken_text_converter.SpokenTextConverter()
    return _converter


def reset_converter() -> None:
    """Drop the cached converter so the next ``_get_converter`` call
    pulls the current ``cfg.tts_pronunciation``. Used by the engine-
    reload path when the operator saves new pronunciation overrides."""
    global _converter
    with _lock:
        _converter = None


def generate_speech(
    text: str,
    voice: str = "glados",
    length_scale: float | None = None,
    noise_scale: float | None = None,
    noise_w: float | None = None,
) -> tuple[NDArray[np.float32], int]:
    """Generate speech audio from text using the specified voice.

    Args:
        text: Text to convert to speech.
        voice: Voice name ("glados", or a custom Piper voice).
        length_scale: Controls pacing/duration (lower = faster).
        noise_scale: Controls expressiveness/randomness.
        noise_w: Controls pitch variance.

    Returns:
        (audio_samples, sample_rate) — numpy array + sample rate.
    """
    synth = _get_synthesizer(voice)
    conv = _get_converter()
    converted_text = conv.text_to_spoken(text)
    tts_kwargs: dict[str, float] = {}
    if length_scale is not None:
        tts_kwargs["length_scale"] = length_scale
    if noise_scale is not None:
        tts_kwargs["noise_scale"] = noise_scale
    if noise_w is not None:
        tts_kwargs["noise_w"] = noise_w
    audio = synth.generate_speech_audio(converted_text, **tts_kwargs)
    return audio, synth.sample_rate


def write_audio(f: str | io.BytesIO, audio: NDArray[np.float32], sample_rate: int, *, format: str) -> None:
    """Encode already-generated audio samples to a file."""
    sf.write(f, audio, sample_rate, format=format.upper())


def write_glados_audio_file(f: str | io.BytesIO, text: str, *, format: str) -> None:
    """Generate GLaDOS-style speech audio from text and write to a file.

    Parameters:
        f: File path or BytesIO object to write the audio to
        text: Text to convert to speech
        format: Audio format (e.g., "mp3", "wav", "ogg")
    """
    audio, sample_rate = generate_speech(text)
    sf.write(f, audio, sample_rate, format=format.upper())
