"""Text-to-Speech (TTS) synthesis components.

Self-contained in-container TTS. Local ONNX inference via
`SpeechSynthesizer` (tts_glados.py) — no external Speaches dependency,
no espeak, no HuggingFace cache.

Model files live under `GLADOS_TTS_MODELS_DIR` (default
`/app/models/TTS`). Each voice is a `<name>.onnx` + `<name>.json`
pair. Discovery walks the dir at import-time for `list_available_voices`.

The legacy Speaches HTTP backend is retained behind an opt-in env flag
(`TTS_BACKEND=speaches`) for operators who still point at an external
Speaches. Default is `local`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class SpeechSynthesizerProtocol(Protocol):
    sample_rate: int

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]: ...


def _models_dir() -> Path:
    return Path(os.environ.get("GLADOS_TTS_MODELS_DIR", "/app/models/TTS"))


def _backend() -> str:
    return os.environ.get("TTS_BACKEND", "local").lower()


def list_available_voices() -> list[str]:
    """Enumerate voice stems from the local models dir.

    Returns ``["glados"]`` as a fallback if the dir is absent so config
    validation and WebUI dropdowns don't fail at startup.
    """
    base = _models_dir()
    voices: list[str] = []
    if base.exists():
        for p in base.glob("*.onnx"):
            if p.stem.endswith("phomenizer_en") or p.stem.startswith("phomenizer"):
                continue  # skip the phonemizer model
            voices.append(p.stem)
        sub = base / "voices"
        if sub.exists():
            for p in sub.glob("*.onnx"):
                voices.append(p.stem)
    return sorted(set(voices)) or ["glados"]


def get_speech_synthesizer(voice: str = "glados") -> SpeechSynthesizerProtocol:
    """Factory returning a local synth by default. ``TTS_BACKEND=speaches``
    flips to the legacy HTTP client for operators pointing at an external
    Speaches service."""
    if _backend() == "speaches":
        from .tts_speaches import SpeachesSynthesizer
        return SpeachesSynthesizer(voice=voice)

    from .tts_glados import SpeechSynthesizer
    base = _models_dir()
    # Allow voice="glados" to resolve to top-level glados.onnx, or a
    # named voice like "startrek-computer" from the voices/ subdir.
    top_level = base / f"{voice}.onnx"
    sub_level = base / "voices" / f"{voice}.onnx"
    if top_level.exists():
        model_path = top_level
    elif sub_level.exists():
        model_path = sub_level
    else:
        # fall back to the default glados.onnx — better than crashing at
        # import time if the operator's config references an unknown voice
        model_path = base / "glados.onnx"
    return SpeechSynthesizer(model_path=model_path)


__all__ = ["SpeechSynthesizerProtocol", "get_speech_synthesizer", "list_available_voices"]
