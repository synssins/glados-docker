"""Text-to-Speech (TTS) synthesis components.

This module provides a protocol-based interface for text-to-speech synthesis
and a factory function to create synthesizer instances for different voices.

Classes:
    SpeechSynthesizerProtocol: Protocol defining the TTS interface

Functions:
    get_speech_synthesizer: Factory function to create TTS instances
    list_available_voices: List all available voice names
"""

from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from ..utils.resources import resource_path

_VOICES_DIR = Path(resource_path("models/TTS/voices"))


class SpeechSynthesizerProtocol(Protocol):
    sample_rate: int

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]: ...


def _get_custom_voice_names() -> list[str]:
    """Discover custom Piper ONNX voices in models/TTS/voices/."""
    if not _VOICES_DIR.is_dir():
        return []
    return sorted(
        p.stem
        for p in _VOICES_DIR.glob("*.onnx")
        if p.with_suffix(".json").exists()
    )


def list_available_voices() -> list[str]:
    """Return all available voice names (GLaDOS + custom Piper voices)."""
    return ["glados"] + _get_custom_voice_names()


def get_speech_synthesizer(
    voice: str = "glados",
) -> SpeechSynthesizerProtocol:
    """
    Factory function to get an instance of an audio synthesizer.

    Parameters:
        voice: Voice name. "glados" for GLaDOS, or any custom Piper voice
               name matching a file in models/TTS/voices/<name>.onnx.

    Returns:
        SpeechSynthesizerProtocol: An instance of the requested speech synthesizer.

    Raises:
        ValueError: If the specified voice is not available.
    """
    if voice.lower() == "glados":
        from ..TTS import tts_glados

        return tts_glados.SpeechSynthesizer()

    # Check custom Piper voices (use piper-native inference, not GLaDOS phonemizer)
    model_path = _VOICES_DIR / f"{voice}.onnx"
    if model_path.exists() and model_path.with_suffix(".json").exists():
        from ..TTS import tts_piper

        return tts_piper.SpeechSynthesizer(model_path=model_path)

    # Fall through to Kokoro
    from ..TTS import tts_kokoro

    available_voices = tts_kokoro.get_voices()
    if voice not in available_voices:
        all_voices = list_available_voices()
        raise ValueError(f"Voice '{voice}' not available. Available: {all_voices}")

    return tts_kokoro.SpeechSynthesizer(voice=voice)


__all__ = ["SpeechSynthesizerProtocol", "get_speech_synthesizer", "list_available_voices"]
