"""Per-call recording — MP3 + JSON metadata + .txt transcript.

Each ongoing call gets a ``CallRecording`` instance. Audio is appended
in raw 8 kHz mono int16 PCM as the call progresses; transcripts are
appended utterance-by-utterance with timestamps. On ``close()`` the
recording finalises:

- ``<call_id>.{mp3|wav}`` — mixed audio (whatever PCM was appended)
- ``<call_id>.json`` — call metadata (direction, caller-id, duration,
  pin outcome, paths, etc.)
- ``<call_id>.txt`` — transcript with ``[HH:MM:SS] Speaker: text`` lines

Then FIFO retention runs: glob ``store_path/*.{mp3|wav}`` ordered by
mtime, trim trios (audio + json + txt) past ``retention_count``.

Format is configurable per ``configs/sip.yaml`` ``recordings.format``.
WAV uses the standard-library ``wave`` module — no native deps. MP3
uses ``pydub``, which shells out to ``ffmpeg`` (already in the image
via the existing apt block).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
import wave
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class TranscriptLine:
    """One utterance in the transcript."""
    timestamp: float          # epoch seconds; converted to HH:MM:SS for the .txt
    speaker: str              # 'GLaDOS' | 'Caller' | freeform
    text: str

    def to_text_line(self) -> str:
        ts = dt.datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        return f"[{ts}] {self.speaker}: {self.text}"


class CallRecording:
    """Owns the on-disk artifacts for a single call.

    Usage:
        rec = CallRecording(
            store_path=Path("media/sip-recordings"),
            call_id="2026-05-09T14-22-31_inbound_op",
            metadata_seed={"direction": "inbound", "remote_caller_id": "Op"},
        )
        rec.append_audio(pcm_8k_chunk)
        rec.append_transcript("GLaDOS", "Acknowledged.")
        rec.update_metadata(pin_outcome="accepted", pin_attempts=1)
        await rec.close()
    """

    def __init__(
        self,
        *,
        store_path: pathlib.Path | str,
        call_id: str,
        metadata_seed: dict[str, Any] | None = None,
        retention_count: int = 5,
        sample_rate_hz: int = 8000,
        sample_width: int = 2,            # int16
        channels: int = 1,
        format: str = "wav",              # "wav" | "mp3"
    ) -> None:
        if format not in ("wav", "mp3"):
            raise ValueError(f"unsupported format {format!r}; use 'wav' or 'mp3'")

        self._store_path = pathlib.Path(store_path)
        self._call_id = call_id
        self._retention_count = retention_count
        self._sample_rate_hz = sample_rate_hz
        self._sample_width = sample_width
        self._channels = channels
        self._format = format

        self._store_path.mkdir(parents=True, exist_ok=True)

        self._audio_path = self._store_path / f"{call_id}.{format}"
        self._json_path = self._store_path / f"{call_id}.json"
        self._txt_path = self._store_path / f"{call_id}.txt"

        self._audio_buf = bytearray()
        self._transcript: list[TranscriptLine] = []

        seed = dict(metadata_seed or {})
        self._metadata: dict[str, Any] = {
            "call_id": call_id,
            "started_at": _utc_iso(),
            "ended_at": None,
            "duration_s": None,
            "audio_path": self._audio_path.name,
            "transcript_path": self._txt_path.name,
            **seed,
        }
        self._started_monotonic = time.monotonic()
        self._closed = False

    # ------------------------------------------------------------------
    # Streaming appenders
    # ------------------------------------------------------------------

    def append_audio(self, pcm: bytes) -> None:
        """Append raw int16 PCM samples at the configured sample rate."""
        if self._closed or not pcm:
            return
        self._audio_buf.extend(pcm)

    def append_transcript(self, speaker: str, text: str) -> None:
        """Append one utterance to the transcript."""
        if self._closed or not text:
            return
        self._transcript.append(TranscriptLine(
            timestamp=time.time(),
            speaker=speaker,
            text=text.strip(),
        ))

    def update_metadata(self, **kwargs: Any) -> None:
        """Set or overwrite metadata fields. Persisted on close."""
        if self._closed:
            return
        self._metadata.update(kwargs)

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def audio_path(self) -> pathlib.Path:
        return self._audio_path

    @property
    def transcript_path(self) -> pathlib.Path:
        return self._txt_path

    @property
    def metadata_path(self) -> pathlib.Path:
        return self._json_path

    # ------------------------------------------------------------------
    # Close — write all 3 files + prune
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Finalise audio, write metadata + transcript, prune retention."""
        if self._closed:
            return
        self._closed = True

        self._metadata["ended_at"] = _utc_iso()
        self._metadata["duration_s"] = round(time.monotonic() - self._started_monotonic, 2)

        try:
            self._write_audio()
        except Exception as e:
            logger.bind(group="sip").exception(f"recording: audio write failed: {e}")

        try:
            self._write_metadata()
        except Exception as e:
            logger.bind(group="sip").exception(f"recording: metadata write failed: {e}")

        try:
            self._write_transcript()
        except Exception as e:
            logger.bind(group="sip").exception(f"recording: transcript write failed: {e}")

        try:
            self._prune_retention()
        except Exception as e:
            logger.bind(group="sip").exception(f"recording: retention prune failed: {e}")

    # ------------------------------------------------------------------
    # Internal — file writes
    # ------------------------------------------------------------------

    def _write_audio(self) -> None:
        if self._format == "wav":
            self._write_wav()
        else:
            self._write_mp3()

    def _write_wav(self) -> None:
        with wave.open(str(self._audio_path), "wb") as wf:
            wf.setnchannels(self._channels)
            wf.setsampwidth(self._sample_width)
            wf.setframerate(self._sample_rate_hz)
            wf.writeframes(bytes(self._audio_buf))

    def _write_mp3(self) -> None:
        # pydub shells out to ffmpeg. Don't import at top-level — keep
        # the test path import-free when format=wav.
        from pydub import AudioSegment  # type: ignore[import-not-found]

        seg = AudioSegment(
            data=bytes(self._audio_buf),
            sample_width=self._sample_width,
            frame_rate=self._sample_rate_hz,
            channels=self._channels,
        )
        seg.export(str(self._audio_path), format="mp3")

    def _write_metadata(self) -> None:
        with self._json_path.open("w", encoding="utf-8") as f:
            json.dump(self._metadata, f, indent=2)
            f.write("\n")

    def _write_transcript(self) -> None:
        with self._txt_path.open("w", encoding="utf-8") as f:
            for line in self._transcript:
                f.write(line.to_text_line() + "\n")

    def _prune_retention(self) -> None:
        """Keep only the ``retention_count`` newest call recordings.

        Deletes the audio + json + txt trio for any call past the cap.
        Pruning is done by audio-file mtime — the most-recently-closed
        call is the newest.
        """
        if self._retention_count <= 0:
            return
        ext = self._format
        candidates = sorted(
            self._store_path.glob(f"*.{ext}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in candidates[self._retention_count:]:
            stem = stale.stem
            for sibling in (
                self._store_path / f"{stem}.{ext}",
                self._store_path / f"{stem}.json",
                self._store_path / f"{stem}.txt",
            ):
                try:
                    sibling.unlink(missing_ok=True)
                except OSError:
                    pass


def _utc_iso() -> str:
    """ISO 8601 timestamp in UTC with millisecond precision."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
