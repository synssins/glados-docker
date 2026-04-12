"""Piper-native TTS synthesizer for custom trained voices.

Uses piper's built-in espeak phonemizer and phoneme ID map,
which matches what piper_train produces. This is distinct from
tts_glados.py which uses a custom ONNX phonemizer.
"""

from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from piper import PiperVoice


class SpeechSynthesizer:
    """Wraps piper.PiperVoice to match the SpeechSynthesizerProtocol."""

    def __init__(self, model_path: Path) -> None:
        self._voice = PiperVoice.load(str(model_path))
        self.sample_rate: int = self._voice.config.sample_rate

    def generate_speech_audio(self, text: str, **kwargs) -> NDArray[np.float32]:
        """Synthesize text to audio using piper's native inference."""
        all_samples = []
        for chunk in self._voice.synthesize(text):
            all_samples.append(chunk.audio_int16_array)

        if not all_samples:
            return np.zeros((0, 1), dtype=np.float32)

        int16_audio = np.concatenate(all_samples)
        # Convert int16 to float32 [-1, 1] and reshape to (samples, 1)
        float_audio = int16_audio.astype(np.float32) / 32768.0
        return float_audio.reshape(-1, 1)
