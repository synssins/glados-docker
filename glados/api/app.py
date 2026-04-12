from dataclasses import dataclass
import io
import os
import re
import threading
import time
from pathlib import Path
from typing import Literal

from litestar import Litestar, post
from litestar.response import Stream

from litestar import get

from .log import structlog_plugin
from .tts import generate_speech, write_audio
from glados.TTS import list_available_voices
ResponseFormat = Literal["mp3", "wav", "ogg"]

# Archive settings — from centralized config, env vars override
try:
    from glados.core.config_store import cfg as _cfg
    _default_archive_dir = _cfg.audio.archive_dir
    _default_archive_max = str(_cfg.audio.archive_max_files)
except Exception:
    _default_archive_dir = str(Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files")) / "glados_archive")
    _default_archive_max = "50"

ARCHIVE_DIR = Path(os.environ.get("GLADOS_AUDIO_ARCHIVE", _default_archive_dir))
ARCHIVE_MAX_FILES = int(os.environ.get("GLADOS_ARCHIVE_MAX_FILES", _default_archive_max))
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _archive_filename(text: str) -> str:
    """Generate a short filename from input text."""
    words = re.sub(r"[^a-zA-Z0-9\s]", "", text).split()[:5]
    stem = "-".join(w.lower() for w in words) if words else "speech"
    stem = stem[:60]
    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"{ts}_{stem}"


def _archive_cleanup() -> None:
    """Keep only ARCHIVE_MAX_FILES most recent files."""
    try:
        files = sorted(ARCHIVE_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[ARCHIVE_MAX_FILES:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _archive_audio(audio, sample_rate: int, text: str) -> None:
    """Save an MP3 copy of already-generated audio to the archive directory."""
    try:
        stem = _archive_filename(text)
        path = ARCHIVE_DIR / f"{stem}.mp3"
        with open(path, "wb") as f:
            write_audio(f, audio, sample_rate, format="mp3")
        _archive_cleanup()
    except Exception:
        pass  # Archiving is best-effort; never block the response


@dataclass
class RequestData:
    input: str
    model: str = "glados"
    voice: str = "glados"
    response_format: ResponseFormat = "mp3"
    speed: float = 1.0
    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w: float | None = None


CONTENT_TYPES: dict[ResponseFormat, str] = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}


@post("/v1/audio/speech")
async def create_speech(data: RequestData) -> Stream:
    """
    Generate speech audio from input text.

    Parameters:
        data: The request data containing input text and speech parameters

    Returns:
        Stream: Stream of bytes data containing the generated speech
    """
    audio, sample_rate = generate_speech(
        data.input,
        voice=data.voice,
        length_scale=data.length_scale,
        noise_scale=data.noise_scale,
        noise_w=data.noise_w,
    )

    buffer = io.BytesIO()
    write_audio(buffer, audio, sample_rate, format=data.response_format)
    buffer.seek(0)

    # Archive a copy as MP3 in background (reuses already-generated audio)
    threading.Thread(target=_archive_audio, args=(audio, sample_rate, data.input), daemon=True).start()

    return Stream(
        buffer,
        headers={
            "content-type": CONTENT_TYPES[data.response_format],
            "content-disposition": f'attachment; filename="speech.{data.response_format}"',
        },
    )


@get("/v1/voices")
async def get_voices() -> dict:
    """List all available TTS voices."""
    return {"voices": list_available_voices()}


app = Litestar([create_speech, get_voices], plugins=[structlog_plugin])
