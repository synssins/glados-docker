"""Core audio data structures for GLaDOS voice assistant.

This module defines message classes used for audio processing and communication
between different components of the voice assistant pipeline.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass
class AudioMessage:
    """Audio message container for TTS output.

    Args:
        audio: Generated audio samples as float32 array
        text: Associated text that was synthesized
        is_eos: Flag indicating end of speech stream
        lane: Which `LLMProcessor` lane generated this audio — one of
            ``"priority"`` (interactive / user-initiated) or
            ``"autonomy"`` (background autonomy subagent). Propagated
            by ``BufferedSpeechPlayer`` into the ``_source`` field of
            the appended conversation-store row so the non-streaming
            API response scanner can skip autonomy-produced assistant
            messages (otherwise an autonomy-generated reply that
            interleaves between a user request and its reply gets
            returned to the API caller as if it were the reply).
    """

    audio: NDArray[np.float32]
    text: str
    is_eos: bool = False
    lane: str = "priority"


@dataclass
class AudioInputMessage:
    """Audio input message container for ASR processing.

    Args:
        audio_sample: Raw audio input samples as float32 array
        vad_confidence: Voice activity detection confidence flag
    """

    audio_sample: NDArray[np.float32]
    vad_confidence: bool = False
