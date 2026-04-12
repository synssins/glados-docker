"""Audio input/output components.

In the container, only the Home Assistant audio backend is used.
The sounddevice backend requires physical audio hardware and is excluded.
"""

import queue
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .vad import VAD


class AudioProtocol(Protocol):
    def __init__(self, vad_threshold: float | None = None) -> None: ...
    def start_listening(self) -> None: ...
    def stop_listening(self) -> None: ...
    def start_speaking(
        self, audio_data: NDArray[np.float32], sample_rate: int | None = None, text: str = ""
    ) -> None: ...
    def measure_percentage_spoken(self, total_samples: int, sample_rate: int | None = None) -> tuple[bool, int]: ...
    def check_if_speaking(self) -> bool: ...
    def stop_speaking(self) -> None: ...
    def get_sample_queue(self) -> queue.Queue[tuple[NDArray[np.float32], bool]]: ...


def get_audio_system(
    backend_type: str = "home_assistant",
    vad_threshold: float | None = None,
    ha_config: dict | None = None,
) -> AudioProtocol:
    """Factory function for audio I/O backends.

    In the container the only supported backend is 'home_assistant'.
    The 'sounddevice' backend is excluded — it requires physical audio hardware.
    """
    if backend_type == "home_assistant":
        from .homeassistant_io import HomeAssistantAudioIO
        if ha_config is None:
            raise ValueError("ha_config is required for the home_assistant backend")
        return HomeAssistantAudioIO(vad_threshold=vad_threshold, **ha_config)
    elif backend_type == "sounddevice":
        raise ValueError(
            "sounddevice backend is not available in the GLaDOS container. "
            "Use backend_type='home_assistant' instead."
        )
    else:
        raise ValueError(f"Unsupported audio backend: {backend_type!r}")


__all__ = ["VAD", "AudioProtocol", "get_audio_system"]

