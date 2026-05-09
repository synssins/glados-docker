"""Tests for glados.sip.recording.

WAV format is used throughout — it requires only the standard library
``wave`` module, so tests run on any platform without ffmpeg. The MP3
path is a thin wrapper over pydub.export and is exercised via live
deploy (Task 14).
"""
from __future__ import annotations

import json
import pathlib
import time
import wave

import pytest

from glados.sip.recording import CallRecording, TranscriptLine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _silence_pcm(duration_ms: int = 100, rate_hz: int = 8000) -> bytes:
    """Generate raw 8 kHz mono int16 silence."""
    n_samples = rate_hz * duration_ms // 1000
    return b"\x00\x00" * n_samples


def _new_recording(
    tmp_path: pathlib.Path,
    *,
    call_id: str = "test_call_001",
    format: str = "wav",
    retention_count: int = 5,
    metadata_seed: dict | None = None,
) -> CallRecording:
    return CallRecording(
        store_path=tmp_path,
        call_id=call_id,
        format=format,
        retention_count=retention_count,
        metadata_seed=metadata_seed,
    )


# ---------------------------------------------------------------------------
# Basic close → all 3 files written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_writes_audio_metadata_transcript(tmp_path: pathlib.Path) -> None:
    rec = _new_recording(tmp_path)
    rec.append_audio(_silence_pcm(200))
    rec.append_transcript("GLaDOS", "Halt. Identify.")
    rec.append_transcript("Caller", "8316")
    await rec.close()

    assert rec.audio_path.exists()
    assert rec.metadata_path.exists()
    assert rec.transcript_path.exists()


@pytest.mark.asyncio
async def test_wav_audio_roundtrips(tmp_path: pathlib.Path) -> None:
    """Audio bytes round-trip through the WAV file."""
    rec = _new_recording(tmp_path)
    payload = _silence_pcm(500)
    rec.append_audio(payload)
    await rec.close()

    with wave.open(str(rec.audio_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000
        frames = wf.readframes(wf.getnframes())
        assert frames == payload


@pytest.mark.asyncio
async def test_metadata_includes_seed_and_lifecycle_fields(tmp_path: pathlib.Path) -> None:
    rec = _new_recording(
        tmp_path,
        metadata_seed={"direction": "inbound", "remote_caller_id": "Operator Mobile"},
    )
    rec.append_audio(_silence_pcm(50))
    rec.update_metadata(pin_outcome="accepted", pin_attempts=1)
    await rec.close()

    data = json.loads(rec.metadata_path.read_text())
    assert data["call_id"] == "test_call_001"
    assert data["direction"] == "inbound"
    assert data["remote_caller_id"] == "Operator Mobile"
    assert data["pin_outcome"] == "accepted"
    assert data["pin_attempts"] == 1
    assert data["started_at"] is not None
    assert data["ended_at"] is not None
    assert isinstance(data["duration_s"], float)
    assert data["audio_path"].endswith(".wav")
    assert data["transcript_path"].endswith(".txt")


@pytest.mark.asyncio
async def test_transcript_format_and_order(tmp_path: pathlib.Path) -> None:
    rec = _new_recording(tmp_path)
    rec.append_transcript("GLaDOS", "Halt. Identify.")
    time.sleep(0.01)  # ensure distinct timestamps
    rec.append_transcript("Caller", "8316")
    time.sleep(0.01)
    rec.append_transcript("GLaDOS", "Acknowledged.")
    await rec.close()

    lines = rec.transcript_path.read_text().strip().splitlines()
    assert len(lines) == 3
    # Each line: [HH:MM:SS] Speaker: text
    assert lines[0].endswith("GLaDOS: Halt. Identify.")
    assert lines[1].endswith("Caller: 8316")
    assert lines[2].endswith("GLaDOS: Acknowledged.")
    # Timestamp prefix matches HH:MM:SS pattern
    for line in lines:
        assert line[0] == "[" and line[9] == "]"


@pytest.mark.asyncio
async def test_empty_call_still_produces_all_files(tmp_path: pathlib.Path) -> None:
    """A call with no audio and no transcript should still close cleanly."""
    rec = _new_recording(tmp_path)
    await rec.close()
    assert rec.audio_path.exists()
    assert rec.metadata_path.exists()
    assert rec.transcript_path.exists()
    # WAV is empty, transcript is empty
    assert rec.transcript_path.read_text() == ""


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: pathlib.Path) -> None:
    rec = _new_recording(tmp_path)
    rec.append_transcript("GLaDOS", "first")
    await rec.close()

    # Second close: should not modify, not crash
    rec.append_transcript("GLaDOS", "second-attempt")  # ignored after close
    await rec.close()

    text = rec.transcript_path.read_text()
    assert "first" in text
    assert "second-attempt" not in text


# ---------------------------------------------------------------------------
# FIFO retention prune
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_keeps_only_n_newest(tmp_path: pathlib.Path) -> None:
    """Close 7 recordings; retention=5 should leave only 5 trios."""
    for i in range(7):
        rec = _new_recording(tmp_path, call_id=f"call_{i:03d}", retention_count=5)
        rec.append_audio(_silence_pcm(20))
        rec.append_transcript("GLaDOS", f"call number {i}")
        await rec.close()
        # Sleep a tick so mtimes are distinct (Windows resolution can be coarse)
        time.sleep(0.02)

    wavs = sorted(tmp_path.glob("*.wav"))
    jsons = sorted(tmp_path.glob("*.json"))
    txts = sorted(tmp_path.glob("*.txt"))
    assert len(wavs) == 5
    assert len(jsons) == 5
    assert len(txts) == 5

    # Oldest two (call_000, call_001) should be gone
    surviving = {w.stem for w in wavs}
    assert "call_000" not in surviving
    assert "call_001" not in surviving
    assert "call_006" in surviving  # newest definitely kept


@pytest.mark.asyncio
async def test_retention_zero_disables_pruning(tmp_path: pathlib.Path) -> None:
    """retention_count=0 keeps everything."""
    for i in range(7):
        rec = _new_recording(tmp_path, call_id=f"call_{i:03d}", retention_count=0)
        await rec.close()
        time.sleep(0.01)

    assert len(list(tmp_path.glob("*.wav"))) == 7


@pytest.mark.asyncio
async def test_retention_handles_partial_trios(tmp_path: pathlib.Path) -> None:
    """If a stale call is missing a sibling file, prune doesn't crash."""
    # Create 5 calls (retention=5 → no prune yet). Then delete call_000's
    # .json. Close a 6th call → prune fires on call_000; its .json is
    # already missing; missing_ok=True should absorb without raising.
    for i in range(5):
        rec = _new_recording(tmp_path, call_id=f"call_{i:03d}", retention_count=5)
        await rec.close()
        time.sleep(0.02)
    (tmp_path / "call_000.json").unlink()  # break the oldest trio

    rec = _new_recording(tmp_path, call_id="call_005", retention_count=5)
    await rec.close()

    # After prune: 5 wavs total, oldest (call_000) is fully gone
    assert len(list(tmp_path.glob("*.wav"))) == 5
    assert not (tmp_path / "call_000.wav").exists()
    assert not (tmp_path / "call_000.txt").exists()


# ---------------------------------------------------------------------------
# Append-after-close ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_after_close_ignored(tmp_path: pathlib.Path) -> None:
    rec = _new_recording(tmp_path)
    rec.append_audio(_silence_pcm(50))
    await rec.close()
    rec.append_audio(_silence_pcm(50))  # ignored
    rec.append_transcript("GLaDOS", "after-close")  # ignored
    rec.update_metadata(extra="ignored")  # ignored

    # Re-read on-disk files: shouldn't have grown
    with wave.open(str(rec.audio_path), "rb") as wf:
        assert wf.getnframes() == 8000 * 50 // 1000  # 50 ms only

    assert "after-close" not in rec.transcript_path.read_text()

    data = json.loads(rec.metadata_path.read_text())
    assert "extra" not in data


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsupported_format_raises() -> None:
    with pytest.raises(ValueError, match="unsupported format"):
        CallRecording(
            store_path=pathlib.Path("/tmp"),
            call_id="x",
            format="ogg",
        )


def test_transcript_line_text_format() -> None:
    line = TranscriptLine(timestamp=0, speaker="GLaDOS", text="hello")
    out = line.to_text_line()
    assert "GLaDOS: hello" in out
    assert out.startswith("[") and out[9] == "]"
