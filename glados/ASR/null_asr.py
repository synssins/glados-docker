"""Null ASR transcriber — stub for container mode where STT is external (speaches)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from numpy.typing import NDArray


class NullTranscriber:
    """No-op transcriber. All speech recognition is handled by the external speaches service."""

    def __init__(self, model_path: str = "", **kwargs: Any) -> None:
        pass

    def transcribe(self, audio_source: NDArray[Any]) -> str:
        return ""

    def transcribe_file(self, audio_path: Path) -> str:
        return ""
