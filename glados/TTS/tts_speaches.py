"""Speaches HTTP-backed speech synthesizer.

Replaces the bundled ONNX TTS engines (tts_glados / tts_piper / tts_kokoro)
with an HTTP client that hits `${SPEACHES_URL}/v1/audio/speech`. This is the
architecture plan's target state — the GLaDOS container does not run any
ML inference itself; all TTS is delegated to speaches.

The class satisfies `SpeechSynthesizerProtocol`:
    - `sample_rate: int`
    - `generate_speech_audio(text) -> NDArray[float32]`

Attitude params (length_scale, noise_scale, noise_w) are forwarded as an
`extra_body` payload so speaches-side Piper voices can honor them. Kokoro
ignores them silently, which is correct behavior.
"""

from __future__ import annotations

import io
import os
from typing import Any

import httpx
import numpy as np
import soundfile as sf
from loguru import logger
from numpy.typing import NDArray


_DEFAULT_TIMEOUT = float(os.environ.get("SPEACHES_TIMEOUT", "30"))
_DEFAULT_MODEL = os.environ.get("SPEACHES_TTS_MODEL", "hexgrad/Kokoro-82M")
_DEFAULT_SAMPLE_RATE = 24_000


class SpeachesSynthesizer:
    """HTTP client that satisfies SpeechSynthesizerProtocol.

    Parameters
    ----------
    voice : str
        Speaches-side voice identifier (e.g. "glados", "af_heart").
    base_url : str | None
        Overrides SPEACHES_URL env var. Defaults to
        http://host.docker.internal:8800.
    model : str | None
        Speaches model name. Defaults to SPEACHES_TTS_MODEL env var or
        hexgrad/Kokoro-82M.
    sample_rate : int
        Expected output sample rate. 24000 matches Kokoro; GLaDOS Piper
        voice will produce 22050 — soundfile reports the actual rate on
        decode and this value is updated on first successful call.
    """

    def __init__(
        self,
        voice: str = "glados",
        base_url: str | None = None,
        model: str | None = None,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.voice = voice
        self.base_url = (
            base_url
            or os.environ.get("SPEACHES_URL", "http://host.docker.internal:8800")
        ).rstrip("/")
        self.model = model or os.environ.get("SPEACHES_TTS_MODEL") or ""
        self.sample_rate = sample_rate
        self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def generate_speech_audio(
        self,
        text: str,
        length_scale: float | None = None,
        noise_scale: float | None = None,
        noise_w: float | None = None,
    ) -> NDArray[np.float32]:
        """Synthesize text to a float32 audio array via speaches.

        Attitude params are passed through as `extra_body` so Piper-family
        voices on the speaches side honor them. Kokoro and other non-Piper
        voices will ignore the extra fields.
        """
        if not text or not text.strip():
            return np.array([], dtype=np.float32)

        payload: dict[str, Any] = {
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
        }
        if self.model:
            payload["model"] = self.model
        # Attitude TTS params — top-level so both the GLaDOS Piper TTS
        # (litestar, validates fields) and speaches accept them.
        if length_scale is not None:
            payload["length_scale"] = length_scale
        if noise_scale is not None:
            payload["noise_scale"] = noise_scale
        if noise_w is not None:
            payload["noise_w"] = noise_w

        url = f"{self.base_url}/v1/audio/speech"
        try:
            resp = self._client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("speaches TTS request failed: {}", exc)
            return np.array([], dtype=np.float32)

        audio_bytes = resp.content
        if not audio_bytes:
            logger.warning("speaches returned empty audio for voice={}", self.voice)
            return np.array([], dtype=np.float32)

        try:
            data, rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to decode speaches audio: {}", exc)
            return np.array([], dtype=np.float32)

        if rate != self.sample_rate:
            self.sample_rate = int(rate)

        if data.ndim == 2:
            data = data[:, 0]
        return data.astype(np.float32, copy=False)

    def __del__(self) -> None:  # noqa: D401
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
