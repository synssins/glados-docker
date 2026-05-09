"""Audio bridge for SIP — PCM I/O + resample + self-listen mute.

Sits between the SIP stack (8 kHz PCM, what baresip publishes after
codec decode for PCMU/PCMA) and the existing STT/TTS pipeline (16 kHz
PCM, mono, signed 16-bit).

**Transport-agnostic by design.** The bridge takes generic asyncio
stream reader/writer objects for the SIP-side audio. Whether those
streams come from a Unix socket served by a custom baresip module, a
named pipe, TCP loopback, or any other transport is the caller's
concern (likely ``call_session.py`` in Task 11).

This decoupling matters because:

1. baresip's stock ``aufile`` audio module only handles WAV files,
   not continuous PCM streaming. Wiring the bridge to whatever
   transport actually works is a Task 11 problem.
2. Unit tests can use ``asyncio.StreamReader/Writer`` mocks —
   no platform-specific I/O setup needed for testing.
3. If we ever swap the SIP stack itself, the bridge stays stable.

What this module does:

- **Inbound (caller → STT):** consume 8 kHz PCM from the rx stream,
  upsample to 16 kHz, deliver via callback. Drop frames while
  ``tts_active=True`` (self-listen mute).
- **Outbound (TTS → caller):** accept 16 kHz PCM via ``write_outbound``,
  downsample to 8 kHz, write to the tx stream.
- **Frame discipline:** 20 ms frames at the SIP-side rate
  (160 samples = 320 bytes int16 at 8 kHz).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional

import numpy as np
from loguru import logger
from scipy.signal import resample_poly


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PCM_DTYPE = np.int16
BYTES_PER_SAMPLE = 2

# Standard SIP-side rate for PCMU/PCMA (8 kHz μ-law/A-law decoded to PCM).
SIP_RATE_HZ = 8000

# Existing STT/TTS pipeline rate.
INTERNAL_RATE_HZ = 16000

# Frame duration. RTP convention is 20 ms per packet for 8 kHz codecs.
FRAME_MS = 20


def _frame_samples(rate_hz: int) -> int:
    """Samples in one 20-ms frame at the given rate."""
    return rate_hz * FRAME_MS // 1000


# ---------------------------------------------------------------------------
# Resampling helpers — pure functions, no I/O
# ---------------------------------------------------------------------------

def upsample_8k_to_16k(pcm_bytes: bytes) -> bytes:
    """Convert 8 kHz int16 PCM bytes to 16 kHz int16 PCM bytes.

    Sample count doubles. ``scipy.signal.resample_poly`` applies an
    anti-aliasing low-pass filter automatically.
    """
    if not pcm_bytes:
        return b""
    samples = np.frombuffer(pcm_bytes, dtype=PCM_DTYPE)
    upsampled = resample_poly(samples, up=2, down=1).astype(PCM_DTYPE, copy=False)
    return upsampled.tobytes()


def downsample_16k_to_8k(pcm_bytes: bytes) -> bytes:
    """Convert 16 kHz int16 PCM bytes to 8 kHz int16 PCM bytes.

    Sample count halves. Anti-aliasing low-pass is applied before
    decimation.
    """
    if not pcm_bytes:
        return b""
    samples = np.frombuffer(pcm_bytes, dtype=PCM_DTYPE)
    downsampled = resample_poly(samples, up=1, down=2).astype(PCM_DTYPE, copy=False)
    return downsampled.tobytes()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

# Type alias for the inbound callback. Receives 16 kHz int16 PCM bytes.
InboundCallback = Callable[[bytes], Awaitable[None]]


class AudioBridge:
    """Two-direction PCM bridge with resampling + self-listen mute.

    Construct with the SIP-side stream pair plus an inbound-PCM callback,
    then ``start()``. The bridge spawns one reader task (rx → STT) and
    one writer task (TTS → tx). ``stop()`` cancels both and closes the
    writer.

    Usage:
        bridge = AudioBridge(rx_reader, tx_writer, on_pcm_in=stt.consume)
        await bridge.start()
        ...
        await bridge.write_outbound(pcm_16k_chunk)  # TTS produces this
        bridge.set_tts_active(True)  # mute STT during playback
        ...
        await bridge.stop()
    """

    # Outbound queue depth. ~64 frames × 20 ms = 1.28 s of buffered TTS
    # audio. Enough to absorb LLM-side jitter without drifting too far
    # ahead of the caller.
    _OUTBOUND_QUEUE_MAX = 64

    def __init__(
        self,
        rx_reader: asyncio.StreamReader,
        tx_writer: asyncio.StreamWriter,
        *,
        on_pcm_in: InboundCallback,
        sip_rate_hz: int = SIP_RATE_HZ,
        internal_rate_hz: int = INTERNAL_RATE_HZ,
    ) -> None:
        if sip_rate_hz != SIP_RATE_HZ:
            raise NotImplementedError(
                f"Only {SIP_RATE_HZ} Hz SIP-side rate supported in v1 (got {sip_rate_hz}). "
                "G.722 / 16 kHz wideband would skip the upsample step entirely."
            )
        if internal_rate_hz != INTERNAL_RATE_HZ:
            raise NotImplementedError(
                f"Only {INTERNAL_RATE_HZ} Hz internal rate supported in v1 (got {internal_rate_hz})."
            )

        self._rx_reader = rx_reader
        self._tx_writer = tx_writer
        self._on_pcm_in = on_pcm_in

        self._sip_frame_bytes = _frame_samples(sip_rate_hz) * BYTES_PER_SAMPLE
        self._internal_frame_bytes = _frame_samples(internal_rate_hz) * BYTES_PER_SAMPLE

        self._tts_active = False
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._OUTBOUND_QUEUE_MAX)

        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._stopped = False

        # Stats — useful for diagnosing stalls.
        self.frames_in_total = 0
        self.frames_in_dropped_self_listen = 0
        self.frames_out_total = 0

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the reader + writer tasks. Idempotent: second call is a no-op."""
        if self._reader_task is not None:
            return
        self._stopped = False
        self._reader_task = asyncio.create_task(self._reader_loop(), name="sip-audio-rx")
        self._writer_task = asyncio.create_task(self._writer_loop(), name="sip-audio-tx")
        logger.bind(group="sip").debug(
            f"audio_bridge started (sip-rate {SIP_RATE_HZ} Hz, "
            f"internal-rate {INTERNAL_RATE_HZ} Hz, frame {FRAME_MS} ms)"
        )

    async def stop(self) -> None:
        """Cancel reader/writer; close the tx writer."""
        self._stopped = True
        for task in (self._reader_task, self._writer_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._writer_task = None
        try:
            self._tx_writer.close()
            await self._tx_writer.wait_closed()
        except Exception:
            pass
        logger.bind(group="sip").debug(
            f"audio_bridge stopped (in {self.frames_in_total}, "
            f"dropped {self.frames_in_dropped_self_listen}, out {self.frames_out_total})"
        )

    # ------------------------------------------------------------------
    # Public PCM I/O
    # ------------------------------------------------------------------

    async def write_outbound(self, pcm_16k: bytes) -> None:
        """Queue a 16 kHz PCM chunk for transmission.

        The chunk can be any length; the writer loop will fragment it
        into 20-ms 8 kHz frames after downsampling. Backpressure: if
        the queue is full (~1.28 s of audio buffered), this awaits.
        """
        if not pcm_16k:
            return
        await self._outbound.put(pcm_16k)

    def set_tts_active(self, active: bool) -> None:
        """Toggle the self-listen mute.

        While ``active=True``, inbound frames from the rx stream are
        dropped before resampling — they don't reach the STT consumer.
        ``call_session`` should set this True around TTS playback
        windows and clear it ~100 ms after the final TTS packet.
        """
        self._tts_active = active

    @property
    def tts_active(self) -> bool:
        return self._tts_active

    # ------------------------------------------------------------------
    # Internal — reader loop (rx → STT)
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read 20-ms 8 kHz frames from rx; resample to 16 kHz; deliver."""
        log = logger.bind(group="sip")
        try:
            while not self._stopped:
                frame_8k = await self._read_exact(self._sip_frame_bytes)
                if frame_8k is None:
                    break  # EOF / closed
                self.frames_in_total += 1
                if self._tts_active:
                    self.frames_in_dropped_self_listen += 1
                    continue
                frame_16k = upsample_8k_to_16k(frame_8k)
                try:
                    await self._on_pcm_in(frame_16k)
                except Exception as e:
                    log.exception(f"audio_bridge: STT consumer raised: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"audio_bridge reader loop crashed: {e}")

    async def _read_exact(self, n: int) -> Optional[bytes]:
        """Read exactly ``n`` bytes from rx_reader. Returns None on EOF."""
        try:
            chunk = await self._rx_reader.readexactly(n)
            return chunk
        except asyncio.IncompleteReadError:
            return None
        except (ConnectionResetError, BrokenPipeError):
            return None

    # ------------------------------------------------------------------
    # Internal — writer loop (TTS → tx)
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Drain outbound queue; downsample 16k → 8k; frame; write to tx."""
        log = logger.bind(group="sip")
        # Carry-over buffer: 16 kHz chunks may not align to 20-ms 16k frames
        # (320 samples = 640 bytes). Buffer the remainder for the next chunk.
        remainder_16k = bytearray()
        try:
            while not self._stopped:
                try:
                    chunk = await asyncio.wait_for(self._outbound.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # check _stopped flag
                remainder_16k.extend(chunk)
                # Emit as many full frames as we have.
                while len(remainder_16k) >= self._internal_frame_bytes:
                    frame_16k = bytes(remainder_16k[: self._internal_frame_bytes])
                    del remainder_16k[: self._internal_frame_bytes]
                    frame_8k = downsample_16k_to_8k(frame_16k)
                    try:
                        self._tx_writer.write(frame_8k)
                        await self._tx_writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        log.warning("audio_bridge writer: tx stream closed")
                        return
                    self.frames_out_total += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"audio_bridge writer loop crashed: {e}")
