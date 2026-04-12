"""Home Assistant audio I/O backend.

Routes GLaDOS speech output to Home Assistant media players via the HA REST API.
Audio input is not supported — use ``input_mode: "text"`` in the config.
"""

import json
import os
import queue
import threading
import time
import uuid
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from loguru import logger
from numpy.typing import NDArray

# urllib is stdlib — no new dependencies
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class _QuietHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves files from a fixed directory without logging every request."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug(f"FileServer: {format % args}")


class HomeAssistantAudioIO:
    """Audio I/O that sends speech to Home Assistant media players.

    Output:
        Encodes audio as WAV, serves it via a built-in HTTP file server,
        and tells HA to ``media_player/play_media`` from that URL.

    Input:
        Stubbed — returns an empty queue.  Use ``input_mode: "text"``.
    """

    SAMPLE_RATE: int = 24000  # Default sample rate (F5-TTS / Kokoro output rate)
    WAV_MAX_AGE_S: int = 60  # Clean up WAV files older than this

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        media_player_entities: list[str],
        serve_host: str,
        serve_port: int = 5051,
        serve_dir: str = str(Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files")) / "glados_ha"),
        vad_threshold: float | None = None,
    ) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token
        self._default_entities = list(media_player_entities)
        self.media_player_entities = list(media_player_entities)
        self.serve_host = serve_host
        self.serve_port = serve_port
        self.serve_dir = Path(serve_dir)
        self._is_playing = False
        self._sample_queue: queue.Queue[tuple[NDArray[np.float32], bool]] = queue.Queue()

        # ── Dynamic mode state (set at runtime by engine callbacks) ──
        self._maintenance_speaker: str | None = None
        self._silent_mode: bool = False

        # Ensure serve directory exists
        self.serve_dir.mkdir(parents=True, exist_ok=True)

        # Start the built-in file server
        self._start_file_server()

    # ------------------------------------------------------------------
    # Dynamic mode control (called by engine on HA entity changes)
    # ------------------------------------------------------------------

    @property
    def maintenance_speaker(self) -> str | None:
        return self._maintenance_speaker

    @maintenance_speaker.setter
    def maintenance_speaker(self, speaker: str | None) -> None:
        self._maintenance_speaker = speaker
        if speaker:
            self.media_player_entities = [speaker]
            logger.success(f"HA Audio: maintenance speaker override → {speaker}")
        else:
            self.media_player_entities = list(self._default_entities)
            logger.success(f"HA Audio: restored default speakers → {self._default_entities}")

    @property
    def silent_mode(self) -> bool:
        return self._silent_mode

    @silent_mode.setter
    def silent_mode(self, muted: bool) -> None:
        self._silent_mode = muted
        logger.success(f"HA Audio: silent mode → {muted}")

    # ------------------------------------------------------------------
    # Built-in HTTP file server
    # ------------------------------------------------------------------

    def _start_file_server(self) -> None:
        handler = partial(_QuietHandler, directory=str(self.serve_dir))
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.serve_port), handler)
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="ha-audio-fileserver",
            daemon=True,
        )
        self._server_thread.start()
        logger.info(
            f"HA audio file server listening on 0.0.0.0:{self.serve_port}, "
            f"serving {self.serve_dir}"
        )

    # ------------------------------------------------------------------
    # HA REST API helpers
    # ------------------------------------------------------------------

    def _ha_request(self, endpoint: str, payload: dict) -> None:
        """POST to a Home Assistant service endpoint with one retry."""
        url = f"{self.ha_url}/api/services/{endpoint}"
        data = json.dumps(payload).encode()
        headers = {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }
        req = Request(url, data=data, headers=headers, method="POST")

        for attempt in range(2):
            try:
                with urlopen(req, timeout=10) as resp:
                    logger.debug(f"HA {endpoint} → {resp.status}")
                    return
            except (HTTPError, URLError, OSError) as exc:
                if attempt == 0:
                    logger.warning(f"HA request failed ({exc}), retrying…")
                    time.sleep(0.5)
                else:
                    logger.error(f"HA request failed after retry: {exc}")

    # ------------------------------------------------------------------
    # WAV file management
    # ------------------------------------------------------------------

    def _cleanup_old_wavs(self) -> None:
        """Remove WAV files older than WAV_MAX_AGE_S from the serve directory."""
        cutoff = time.time() - self.WAV_MAX_AGE_S
        for f in self.serve_dir.glob("speech_*.wav"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    @staticmethod
    def _float32_to_wav_bytes(audio: NDArray[np.float32], sample_rate: int) -> bytes:
        """Encode float32 audio array to WAV bytes (16-bit PCM)."""
        # Clip and convert to int16
        pcm = np.clip(audio, -1.0, 1.0)
        pcm_int16 = (pcm * 32767).astype(np.int16)

        import io

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16.tobytes())
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Streaming / pre-encoding helpers (used by BufferedSpeechPlayer)
    # ------------------------------------------------------------------

    def prepare_wav(
        self,
        audio_data: NDArray[np.float32],
        sample_rate: int | None = None,
    ) -> str:
        """Encode audio to WAV, write to serve dir, return the serve URL.

        This does NOT call HA play_media — use :meth:`play_prepared_wav` for that.
        Separating encode from play lets the buffered player pre-encode the next
        chunk while the current one is still playing.

        Returns:
            The HTTP URL where the WAV file is served.
        """
        if sample_rate is None:
            sample_rate = self.SAMPLE_RATE

        self._cleanup_old_wavs()

        filename = f"speech_{uuid.uuid4().hex}.wav"
        wav_path = self.serve_dir / filename
        wav_bytes = self._float32_to_wav_bytes(audio_data, sample_rate)
        wav_path.write_bytes(wav_bytes)
        logger.debug(f"Prepared WAV: {len(wav_bytes)} bytes → {wav_path}")

        return f"http://{self.serve_host}:{self.serve_port}/{filename}"

    def play_prepared_wav(self, media_url: str) -> None:
        """Tell HA to play a pre-encoded WAV URL (from :meth:`prepare_wav`).

        Sets ``_is_playing`` and calls HA ``media_player/play_media``.
        """
        if self._silent_mode:
            logger.info(f"HA Audio: silent mode, not playing {media_url}")
            return

        self._is_playing = True
        self._ha_request(
            "media_player/play_media",
            {
                "entity_id": self.media_player_entities,
                "media_content_id": media_url,
                "media_content_type": "music",
            },
        )
        logger.info(f"HA play_media (prepared) → {media_url} on {self.media_player_entities}")

    # ------------------------------------------------------------------
    # AudioProtocol — output methods
    # ------------------------------------------------------------------

    def start_speaking(
        self,
        audio_data: NDArray[np.float32],
        sample_rate: int | None = None,
        text: str = "",
    ) -> None:
        if self._silent_mode:
            logger.info(f"HA Audio: silent mode active, suppressing: {text[:80]}")
            self._is_playing = False
            return

        if sample_rate is None:
            sample_rate = self.SAMPLE_RATE

        # Clean up old files first
        self._cleanup_old_wavs()

        # Encode to WAV and write to serve directory
        filename = f"speech_{uuid.uuid4().hex}.wav"
        wav_path = self.serve_dir / filename
        wav_bytes = self._float32_to_wav_bytes(audio_data, sample_rate)
        wav_path.write_bytes(wav_bytes)
        logger.debug(f"Wrote {len(wav_bytes)} bytes to {wav_path}")

        # Build the public URL that HA will fetch
        media_url = f"http://{self.serve_host}:{self.serve_port}/{filename}"

        self._is_playing = True

        # Tell HA to play
        self._ha_request(
            "media_player/play_media",
            {
                "entity_id": self.media_player_entities,
                "media_content_id": media_url,
                "media_content_type": "music",
            },
        )
        logger.info(f"HA play_media → {media_url} on {self.media_player_entities}")

    def measure_percentage_spoken(
        self, total_samples: int, sample_rate: int | None = None
    ) -> tuple[bool, int]:
        if sample_rate is None:
            sample_rate = self.SAMPLE_RATE

        expected_duration = total_samples / sample_rate
        start = time.monotonic()

        while self._is_playing:
            elapsed = time.monotonic() - start
            if elapsed >= expected_duration:
                self._is_playing = False
                return (False, 100)
            time.sleep(0.1)

        # Interrupted — calculate how far we got
        elapsed = time.monotonic() - start
        percentage = min(int(elapsed / expected_duration * 100), 100)
        return (True, percentage)

    def stop_speaking(self) -> None:
        if self._is_playing:
            self._is_playing = False
            # Tell HA to stop playback
            self._ha_request(
                "media_player/media_stop",
                {"entity_id": self.media_player_entities},
            )
            logger.info("HA media_stop sent")

    def check_if_speaking(self) -> bool:
        return self._is_playing

    # ------------------------------------------------------------------
    # AudioProtocol — input methods (stubbed)
    # ------------------------------------------------------------------

    def start_listening(self) -> None:
        logger.info(
            "HomeAssistantAudioIO: start_listening is a no-op. "
            "Use input_mode='text' or the HA voice pipeline for input."
        )

    def stop_listening(self) -> None:
        pass

    def get_sample_queue(self) -> queue.Queue[tuple[NDArray[np.float32], bool]]:
        return self._sample_queue
