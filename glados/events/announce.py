"""Optional spoken line after an event action fires.

Best-effort by design: announce failures are WARNINGs and never roll
back or block the action itself (spec §5).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from loguru import logger

_AUDIO = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
SERVE_DIR = _AUDIO / "glados_ha"


def _generate_tts(text: str) -> Path | None:
    """Synthesize `text` -> WAV in the serve dir. None on failure."""
    from glados.core.config_store import cfg as store_cfg
    url = f"{store_cfg.service_url('tts')}/v1/audio/speech"
    payload = {"input": text, "voice": "glados", "response_format": "wav"}
    req = Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        SERVE_DIR.mkdir(parents=True, exist_ok=True)
        with urlopen(req, timeout=30) as resp:
            out = SERVE_DIR / f"event_{uuid.uuid4().hex[:8]}.wav"
            out.write_bytes(resp.read())
            return out
    except Exception as exc:
        logger.warning("events announce: TTS failed: {}", exc)
        return None


def _serve_url(wav_path: Path) -> str:
    """TLS-aware public URL for a WAV already in the serve dir
    (mirrors screener._play_on_speaker, screener.py:858)."""
    from glados.core.config_store import cfg as store_cfg
    from glados.core.tls import is_tls_active
    proto = "https" if is_tls_active() else "http"
    return f"{proto}://{store_cfg.serve_host}:{store_cfg.serve_port}/{wav_path.name}"


def announce(text: str, speaker: str, call_service: Callable[..., dict]) -> bool:
    wav = _generate_tts(text)
    if wav is None:
        return False
    try:
        call_service(
            "media_player", "play_media",
            service_data={
                "entity_id": [speaker],
                "media_content_id": _serve_url(wav),
                "media_content_type": "music",
            },
        )
        return True
    except Exception as exc:
        logger.warning("events announce: play_media on {} failed: {}", speaker, exc)
        return False
