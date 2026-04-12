"""Buffered speech player for streaming TTS.

Replaces SpeechPlayer when streaming_tts is enabled.  Uses a two-thread
architecture to pre-buffer audio so that the next chunk is always ready
before the current one finishes playing, eliminating gaps and stuttering.

Architecture
------------
audio_output_queue (from TTS synthesizer)
        │
        ▼
  ┌─────────────┐
  │ Accumulator  │  pulls AudioMessages, pre-encodes WAVs,
  │   thread     │  pushes PreparedChunks to _play_buffer
  └─────┬───────┘
        │
   _play_buffer (collections.deque of PreparedChunk)
        │
        ▼
  ┌─────────────┐
  │   Player     │  waits for buffer ≥ threshold, then plays
  │   thread     │  chunks sequentially with zero gap
  └─────────────┘
"""

from __future__ import annotations

import collections
import io
import queue
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from loguru import logger
from numpy.typing import NDArray

from ..audio_io import AudioProtocol
from ..observability import ObservabilityBus, trim_message
from .audio_data import AudioMessage
from .conversation_store import ConversationStore


@dataclass
class PreparedChunk:
    """A pre-encoded audio chunk ready for immediate playback."""

    audio_data: NDArray[np.float32]  # raw audio for duration calculation
    text: str
    is_eos: bool = False


class BufferedSpeechPlayer:
    """Buffered speech player that pre-buffers audio to prevent stuttering.

    Parameters
    ----------
    buffer_seconds : float
        Minimum seconds of audio to accumulate before starting playback.
        Default 3.0.  Set higher for slower TTS / unreliable networks.
    """

    def __init__(
        self,
        audio_io: AudioProtocol,
        audio_output_queue: queue.Queue[AudioMessage],
        conversation_store: ConversationStore,
        tts_sample_rate: int,
        shutdown_event: threading.Event,
        currently_speaking_event: threading.Event,
        processing_active_event: threading.Event,
        pause_time: float,
        buffer_seconds: float = 3.0,
        tts_muted_event: threading.Event | None = None,
        interaction_state: "InteractionState | None" = None,
        observability_bus: ObservabilityBus | None = None,
    ) -> None:
        self.audio_io = audio_io
        self.audio_output_queue = audio_output_queue
        self._conversation_store = conversation_store
        self.tts_sample_rate = tts_sample_rate
        self.shutdown_event = shutdown_event
        self.currently_speaking_event = currently_speaking_event
        self.processing_active_event = processing_active_event
        self.pause_time = pause_time
        self.buffer_seconds = buffer_seconds
        self._tts_muted_event = tts_muted_event
        self._interaction_state = interaction_state
        self._observability_bus = observability_bus

        # Buffer between accumulator and player threads
        self._play_buffer: collections.deque[PreparedChunk] = collections.deque()
        self._buffer_lock = threading.Lock()
        self._buffer_ready = threading.Event()  # signalled when buffer has data
        self._interrupt_flag = threading.Event()  # signalled to cancel current stream

        # Text accumulator for conversation store (same as SpeechPlayer)
        self._text_accumulator: list[str] = []
        self._text_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public entry point (matches SpeechPlayer.run signature)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry point — starts accumulator and player threads, blocks until shutdown."""
        acc_thread = threading.Thread(
            target=self._accumulator_loop, name="BufferedPlayer-Acc", daemon=True
        )
        play_thread = threading.Thread(
            target=self._player_loop, name="BufferedPlayer-Play", daemon=True
        )
        acc_thread.start()
        play_thread.start()

        logger.info(
            "BufferedSpeechPlayer started (buffer={:.1f}s)", self.buffer_seconds
        )

        # Block until shutdown
        acc_thread.join()
        play_thread.join()
        logger.info("BufferedSpeechPlayer finished.")

    # ------------------------------------------------------------------
    # Accumulator thread
    # ------------------------------------------------------------------

    def _accumulator_loop(self) -> None:
        """Pull AudioMessages from the TTS queue and buffer them for playback."""
        logger.debug("Accumulator thread started.")
        while not self.shutdown_event.is_set():
            try:
                audio_msg = self.audio_output_queue.get(timeout=self.pause_time)
            except queue.Empty:
                continue

            tts_muted = bool(self._tts_muted_event and self._tts_muted_event.is_set())

            # --- EOS token ---
            if audio_msg.is_eos:
                chunk = PreparedChunk(
                    audio_data=np.array([], dtype=np.float32),
                    text="",
                    is_eos=True,
                )
                with self._buffer_lock:
                    self._play_buffer.append(chunk)
                self._buffer_ready.set()
                continue

            # --- Muted: log but don't play ---
            if tts_muted:
                if audio_msg.text:
                    logger.info(f"Assistant (muted): {audio_msg.text}")
                    if self._interaction_state:
                        self._interaction_state.mark_assistant()
                    if self._observability_bus:
                        self._observability_bus.emit(
                            source="tts",
                            kind="play",
                            message=trim_message(audio_msg.text),
                            meta={"audio_samples": 0, "muted": True},
                        )
                        self._observability_bus.emit(
                            source="tts",
                            kind="finish",
                            message=trim_message(audio_msg.text),
                            meta={"muted": True},
                        )
                    with self._text_lock:
                        self._text_accumulator.append(audio_msg.text)
                self.currently_speaking_event.clear()
                continue

            # --- Normal audio chunk: buffer it ---
            audio_len = len(audio_msg.audio) if audio_msg.audio is not None else 0
            if audio_len and audio_msg.text:
                chunk = PreparedChunk(
                    audio_data=audio_msg.audio,
                    text=audio_msg.text,
                )
                with self._buffer_lock:
                    self._play_buffer.append(chunk)
                self._buffer_ready.set()
                logger.debug(
                    "Buffered chunk: '{}' ({:.1f}s, buf={})",
                    audio_msg.text[:40],
                    audio_len / self.tts_sample_rate,
                    len(self._play_buffer),
                )
            else:
                logger.warning(
                    f"Accumulator: empty audio/text: {audio_len}, {audio_msg}"
                )

        logger.debug("Accumulator thread finished.")

    # ------------------------------------------------------------------
    # Player thread
    # ------------------------------------------------------------------

    def _player_loop(self) -> None:
        """Play buffered chunks, waiting for pre-fill threshold before starting."""
        logger.debug("Player thread started.")
        while not self.shutdown_event.is_set():
            # Wait for something in the buffer
            self._buffer_ready.wait(timeout=self.pause_time)
            if self.shutdown_event.is_set():
                break

            # Check if we have enough buffered audio to start
            if not self._should_start_playback():
                continue

            # Play all buffered chunks until buffer is empty or interrupted
            self._play_buffered_stream()

        logger.debug("Player thread finished.")

    def _buffered_duration(self) -> float:
        """Total seconds of audio currently in the buffer."""
        with self._buffer_lock:
            total_samples = sum(
                len(c.audio_data)
                for c in self._play_buffer
                if not c.is_eos
            )
        return total_samples / self.tts_sample_rate if self.tts_sample_rate else 0.0

    def _should_start_playback(self) -> bool:
        """Check if we have enough audio buffered or if EOS arrived."""
        with self._buffer_lock:
            if not self._play_buffer:
                self._buffer_ready.clear()
                return False
            # Always start if EOS is in the buffer (flush remaining)
            if any(c.is_eos for c in self._play_buffer):
                return True
        # Check duration threshold
        return self._buffered_duration() >= self.buffer_seconds

    def _play_buffered_stream(self) -> None:
        """Play chunks from buffer sequentially until empty, EOS, or interrupted."""
        self._interrupt_flag.clear()

        while not self.shutdown_event.is_set() and not self._interrupt_flag.is_set():
            # Get next chunk
            chunk: PreparedChunk | None = None
            with self._buffer_lock:
                if self._play_buffer:
                    chunk = self._play_buffer.popleft()
                else:
                    self._buffer_ready.clear()

            if chunk is None:
                # Buffer empty — wait briefly for more chunks, then stop
                # This gives the TTS synthesizer a moment to generate the next chunk
                time.sleep(0.3)
                with self._buffer_lock:
                    if self._play_buffer:
                        continue  # More chunks arrived
                break  # Buffer truly empty, stream done

            # --- EOS ---
            if chunk.is_eos:
                logger.debug("BufferedPlayer: EOS received, flushing.")
                with self._text_lock:
                    if self._text_accumulator:
                        self._conversation_store.append(
                            {"role": "assistant", "content": " ".join(self._text_accumulator)}
                        )
                    self._text_accumulator = []
                self.currently_speaking_event.clear()
                break

            # --- Play this chunk ---
            audio_len = len(chunk.audio_data)
            self.currently_speaking_event.set()
            if self._interaction_state:
                self._interaction_state.mark_assistant()
            if self._observability_bus:
                self._observability_bus.emit(
                    source="tts",
                    kind="play",
                    message=trim_message(chunk.text),
                    meta={"audio_samples": audio_len, "streaming": True},
                )

            self.audio_io.start_speaking(chunk.audio_data, self.tts_sample_rate)
            logger.success(f"TTS (streaming): {chunk.text}")

            # Wait for playback to finish or interruption
            interrupted, percentage_played = self.audio_io.measure_percentage_spoken(
                audio_len, self.tts_sample_rate
            )

            if interrupted:
                clipped = self._clip_text(chunk.text, percentage_played)
                logger.success(f"TTS interrupted at {percentage_played}%: {clipped}")
                if self._observability_bus:
                    self._observability_bus.emit(
                        source="tts",
                        kind="interrupt",
                        message=trim_message(clipped),
                        level="warning",
                        meta={"percentage": round(float(percentage_played), 2)},
                    )

                with self._text_lock:
                    self._text_accumulator.append(clipped)
                    self._conversation_store.append_multiple([
                        {"role": "assistant", "content": " ".join(self._text_accumulator)},
                        {
                            "role": "user",
                            "content": (
                                "[SYSTEM: User interrupted mid-response! Full intended output: "
                                f"'{chunk.text}']"
                            ),
                        },
                    ])
                    self._text_accumulator = []

                # Clear everything on interrupt
                self._clear_all()
                break
            else:
                # Playback completed normally
                logger.success(f"BufferedPlayer: chunk done: '{chunk.text[:40]}'")
                with self._text_lock:
                    self._text_accumulator.append(chunk.text)
                if self._observability_bus:
                    self._observability_bus.emit(
                        source="tts",
                        kind="finish",
                        message=trim_message(chunk.text),
                    )

            self.currently_speaking_event.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_all(self) -> None:
        """Clear both the play buffer and the upstream audio queue."""
        self._interrupt_flag.set()
        with self._buffer_lock:
            self._play_buffer.clear()
        self.currently_speaking_event.clear()
        # Drain upstream queue
        try:
            while True:
                self.audio_output_queue.get_nowait()
        except queue.Empty:
            pass

    def _clear_audio_queue(self) -> None:
        """Compatibility: clears audio queue (called externally on interrupt)."""
        self._clear_all()

    @staticmethod
    def _clip_text(text: str, percentage_played: float) -> str:
        """Clip text proportionally to percentage played."""
        tokens = text.split()
        percentage_played = max(0.0, min(100.0, float(percentage_played)))
        words = round((percentage_played / 100) * len(tokens))
        return " ".join(tokens[:words])

    @staticmethod
    def clip_interrupted_sentence(generated_text: str, percentage_played: float) -> str:
        """Compatibility alias for _clip_text."""
        return BufferedSpeechPlayer._clip_text(generated_text, percentage_played)
