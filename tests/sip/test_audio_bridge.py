"""Tests for glados.sip.audio_bridge.

Uses asyncio stream mocks (no FIFOs, no platform-specific I/O), so the
suite runs on any platform that has scipy + numpy.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from glados.sip.audio_bridge import (
    AudioBridge,
    BYTES_PER_SAMPLE,
    INTERNAL_RATE_HZ,
    SIP_RATE_HZ,
    downsample_16k_to_8k,
    upsample_8k_to_16k,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sine_pcm(rate_hz: int, duration_ms: int, freq_hz: int = 440) -> bytes:
    """Generate a sine-wave int16 PCM buffer."""
    n = rate_hz * duration_ms // 1000
    t = np.arange(n) / rate_hz
    samples = (np.sin(2 * np.pi * freq_hz * t) * 16000).astype(np.int16)
    return samples.tobytes()


def _silence_pcm(rate_hz: int, duration_ms: int) -> bytes:
    n = rate_hz * duration_ms // 1000
    return (np.zeros(n, dtype=np.int16)).tobytes()


def _frame_8k_bytes(n_frames: int = 1) -> int:
    # 20 ms × 8000 Hz = 160 samples = 320 bytes int16
    return 320 * n_frames


def _frame_16k_bytes(n_frames: int = 1) -> int:
    # 20 ms × 16000 Hz = 320 samples = 640 bytes int16
    return 640 * n_frames


class _CollectingWriter:
    """Stand-in for asyncio.StreamWriter used by the tx side."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _make_reader(payload: bytes) -> asyncio.StreamReader:
    """Build an asyncio.StreamReader pre-fed with payload + EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# Resampler unit tests
# ---------------------------------------------------------------------------

def test_upsample_8k_to_16k_doubles_sample_count() -> None:
    pcm = _sine_pcm(rate_hz=SIP_RATE_HZ, duration_ms=20)
    out = upsample_8k_to_16k(pcm)
    # 160 samples → 320 samples; bytes double
    assert len(out) == 2 * len(pcm)


def test_downsample_16k_to_8k_halves_sample_count() -> None:
    pcm = _sine_pcm(rate_hz=INTERNAL_RATE_HZ, duration_ms=20)
    out = downsample_16k_to_8k(pcm)
    assert len(out) == len(pcm) // 2


def test_upsample_empty_returns_empty() -> None:
    assert upsample_8k_to_16k(b"") == b""


def test_downsample_empty_returns_empty() -> None:
    assert downsample_16k_to_8k(b"") == b""


def test_resample_roundtrip_preserves_sine_shape() -> None:
    """Up + down should approximately preserve the signal."""
    original = _sine_pcm(rate_hz=SIP_RATE_HZ, duration_ms=200, freq_hz=300)
    up = upsample_8k_to_16k(original)
    down = downsample_16k_to_8k(up)
    # Same length back
    assert len(down) == len(original)
    # Energy preserved within a tolerance (resampling adds small distortion)
    orig_arr = np.frombuffer(original, dtype=np.int16).astype(np.float64)
    rt_arr = np.frombuffer(down, dtype=np.int16).astype(np.float64)
    orig_rms = np.sqrt(np.mean(orig_arr**2))
    rt_rms = np.sqrt(np.mean(rt_arr**2))
    # Allow 10 % RMS drift
    assert abs(orig_rms - rt_rms) / orig_rms < 0.1


# ---------------------------------------------------------------------------
# Bridge integration tests (mocked streams)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_inbound_delivers_resampled_frames_to_callback() -> None:
    """Push 5 frames of 8k PCM to rx; STT consumer should see 5 frames of 16k PCM."""
    five_frames_8k = _silence_pcm(SIP_RATE_HZ, duration_ms=100)  # 5 × 20 ms
    reader = _make_reader(five_frames_8k)
    writer = _CollectingWriter()

    received: list[bytes] = []

    async def on_pcm(frame: bytes) -> None:
        received.append(frame)

    bridge = AudioBridge(reader, writer, on_pcm_in=on_pcm)
    await bridge.start()
    # Reader will drain rx; wait for it to finish consuming.
    await asyncio.wait_for(bridge._reader_task, timeout=2.0)
    await bridge.stop()

    assert len(received) == 5
    assert all(len(f) == _frame_16k_bytes() for f in received)
    assert bridge.frames_in_total == 5
    assert bridge.frames_in_dropped_self_listen == 0


@pytest.mark.asyncio
async def test_bridge_self_listen_mute_drops_inbound_frames() -> None:
    """While tts_active=True, rx frames should be dropped (not forwarded to STT)."""
    five_frames_8k = _silence_pcm(SIP_RATE_HZ, duration_ms=100)
    reader = _make_reader(five_frames_8k)
    writer = _CollectingWriter()

    received: list[bytes] = []

    async def on_pcm(frame: bytes) -> None:
        received.append(frame)

    bridge = AudioBridge(reader, writer, on_pcm_in=on_pcm)
    bridge.set_tts_active(True)
    await bridge.start()
    await asyncio.wait_for(bridge._reader_task, timeout=2.0)
    await bridge.stop()

    assert received == []
    assert bridge.frames_in_total == 5
    assert bridge.frames_in_dropped_self_listen == 5


@pytest.mark.asyncio
async def test_bridge_self_listen_mute_resumes_after_clearing_flag() -> None:
    """Frames received before TTS clears stay dropped; frames after pass through."""
    # 5 frames before mute clear, 5 after — but we control timing via control flow.
    pre_mute = _silence_pcm(SIP_RATE_HZ, duration_ms=40)   # 2 frames
    post_mute = _silence_pcm(SIP_RATE_HZ, duration_ms=60)  # 3 frames

    reader = asyncio.StreamReader()
    writer = _CollectingWriter()

    received: list[bytes] = []

    async def on_pcm(frame: bytes) -> None:
        received.append(frame)

    bridge = AudioBridge(reader, writer, on_pcm_in=on_pcm)
    bridge.set_tts_active(True)
    await bridge.start()

    # Feed pre-mute frames while muted
    reader.feed_data(pre_mute)
    await asyncio.sleep(0.05)

    # Clear mute; feed post-mute frames
    bridge.set_tts_active(False)
    reader.feed_data(post_mute)
    reader.feed_eof()

    await asyncio.wait_for(bridge._reader_task, timeout=2.0)
    await bridge.stop()

    assert len(received) == 3  # only post-mute survived
    assert bridge.frames_in_total == 5
    assert bridge.frames_in_dropped_self_listen == 2


@pytest.mark.asyncio
async def test_bridge_outbound_writes_downsampled_frames_to_tx() -> None:
    """Queue 16k chunks; tx stream should receive 8k frames."""
    reader = asyncio.StreamReader()
    reader.feed_eof()  # No inbound; reader exits immediately
    writer = _CollectingWriter()

    async def noop(_: bytes) -> None:
        pass

    bridge = AudioBridge(reader, writer, on_pcm_in=noop)
    await bridge.start()

    # Queue 100 ms of 16 kHz audio (5 × 640-byte frames)
    chunk = _silence_pcm(INTERNAL_RATE_HZ, duration_ms=100)
    await bridge.write_outbound(chunk)

    # Give the writer loop time to process
    await asyncio.sleep(0.2)
    await bridge.stop()

    # Should have written 5 × 320-byte 8k frames
    assert len(writer.buf) == _frame_8k_bytes(5)
    assert bridge.frames_out_total == 5


@pytest.mark.asyncio
async def test_bridge_outbound_handles_partial_frame_chunks() -> None:
    """A 16k chunk that's not aligned to 20-ms frames should buffer the remainder."""
    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = _CollectingWriter()

    async def noop(_: bytes) -> None:
        pass

    bridge = AudioBridge(reader, writer, on_pcm_in=noop)
    await bridge.start()

    # 30 ms = 1.5 frames worth at 16k. 16k frame is 640 bytes; 30 ms = 960 bytes.
    chunk1 = _silence_pcm(INTERNAL_RATE_HZ, duration_ms=30)
    await bridge.write_outbound(chunk1)
    await asyncio.sleep(0.1)
    # After chunk1: 1 full frame emitted (640 bytes consumed → 320 bytes 8k out),
    # 320 bytes 16k buffered.
    assert bridge.frames_out_total == 1

    # Send another 30 ms — total buffer = 320 + 960 = 1280 bytes = exactly 2 frames
    chunk2 = _silence_pcm(INTERNAL_RATE_HZ, duration_ms=30)
    await bridge.write_outbound(chunk2)
    await asyncio.sleep(0.1)
    # +2 frames out
    assert bridge.frames_out_total == 3

    await bridge.stop()
    assert len(writer.buf) == _frame_8k_bytes(3)


@pytest.mark.asyncio
async def test_bridge_stop_is_idempotent() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = _CollectingWriter()

    async def noop(_: bytes) -> None:
        pass

    bridge = AudioBridge(reader, writer, on_pcm_in=noop)
    await bridge.start()
    await bridge.stop()
    # Second stop should be a no-op
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_stop_without_start_is_safe() -> None:
    reader = asyncio.StreamReader()
    writer = _CollectingWriter()

    async def noop(_: bytes) -> None:
        pass

    bridge = AudioBridge(reader, writer, on_pcm_in=noop)
    await bridge.stop()  # never started; should not crash


@pytest.mark.asyncio
async def test_bridge_unsupported_sip_rate_raises() -> None:
    reader = asyncio.StreamReader()
    writer = _CollectingWriter()

    async def noop(_: bytes) -> None:
        pass

    with pytest.raises(NotImplementedError, match="SIP-side rate"):
        AudioBridge(reader, writer, on_pcm_in=noop, sip_rate_hz=16000)


@pytest.mark.asyncio
async def test_bridge_consumer_exception_does_not_crash_reader_loop() -> None:
    """If the STT consumer raises, the reader loop logs and keeps going."""
    five_frames_8k = _silence_pcm(SIP_RATE_HZ, duration_ms=100)
    reader = _make_reader(five_frames_8k)
    writer = _CollectingWriter()

    call_count = 0

    async def flaky(frame: bytes) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("intentional STT failure on frame 2")

    bridge = AudioBridge(reader, writer, on_pcm_in=flaky)
    await bridge.start()
    await asyncio.wait_for(bridge._reader_task, timeout=2.0)
    await bridge.stop()

    # All 5 frames should have been attempted despite the one failure
    assert call_count == 5


@pytest.mark.asyncio
async def test_bridge_inbound_partial_frame_at_eof_is_dropped() -> None:
    """A trailing partial frame (not a full 320 bytes) should not be delivered."""
    # 1.5 frames worth of 8k PCM
    one_and_half = _silence_pcm(SIP_RATE_HZ, duration_ms=30)  # 480 bytes
    reader = _make_reader(one_and_half)
    writer = _CollectingWriter()

    received: list[bytes] = []

    async def on_pcm(frame: bytes) -> None:
        received.append(frame)

    bridge = AudioBridge(reader, writer, on_pcm_in=on_pcm)
    await bridge.start()
    await asyncio.wait_for(bridge._reader_task, timeout=2.0)
    await bridge.stop()

    # Only 1 complete frame delivered; partial is dropped
    assert len(received) == 1
    assert bridge.frames_in_total == 1
