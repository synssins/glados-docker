"""Text-to-Speech (TTS) synthesis components.

This module provides a protocol-based interface for text-to-speech synthesis
and a factory function to create synthesizer instances.

In the container, all synthesis is delegated to speaches over HTTP — the
container does not run ONNX inference. See `tts_speaches.SpeachesSynthesizer`.

Classes:
    SpeechSynthesizerProtocol: Protocol defining the TTS interface

Functions:
    get_speech_synthesizer: Factory returning a speaches-backed synthesizer
    list_available_voices: Query speaches for the current voice list
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx
import numpy as np
from loguru import logger
from numpy.typing import NDArray


class SpeechSynthesizerProtocol(Protocol):
    sample_rate: int

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]: ...


def _speaches_base_url() -> str:
    return os.environ.get("SPEACHES_URL", "http://host.docker.internal:8800").rstrip("/")


def list_available_voices() -> list[str]:
    """Query speaches for the set of registered voices.

    Returns a stable fallback list if speaches is unreachable, so config
    validation and WebUI dropdowns don't fail at startup.
    """
    url = f"{_speaches_base_url()}/v1/audio/speech/voices"
    try:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list speaches voices ({}) — returning fallback", exc)
        return ["glados"]

    if isinstance(data, list):
        voices = [v["name"] if isinstance(v, dict) and "name" in v else str(v) for v in data]
    elif isinstance(data, dict) and "voices" in data:
        voices = [v["name"] if isinstance(v, dict) and "name" in v else str(v) for v in data["voices"]]
    else:
        voices = ["glados"]
    return sorted(set(voices)) or ["glados"]


def get_speech_synthesizer(voice: str = "glados") -> SpeechSynthesizerProtocol:
    """Factory returning a speaches-backed synthesizer for the given voice.

    The voice name is a speaches-side identifier. Until the GLaDOS Kokoro
    voice is registered in speaches (Stage 4 work), operators can point at
    any stock speaches voice (e.g. `af_heart`) and GLaDOS's character will
    still come through via the LLM system prompt.
    """
    from .tts_speaches import SpeachesSynthesizer

    return SpeachesSynthesizer(voice=voice)


__all__ = ["SpeechSynthesizerProtocol", "get_speech_synthesizer", "list_available_voices"]
