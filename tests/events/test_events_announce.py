from pathlib import Path
from unittest.mock import patch

from glados.events import announce as ann


def test_announce_plays_generated_wav(tmp_path):
    calls = []
    def call_service(domain, service, service_data=None, target=None, timeout_s=None):
        calls.append((domain, service, service_data))
        return {}
    wav = tmp_path / "evt_x.wav"
    wav.write_bytes(b"RIFF")
    with patch.object(ann, "_generate_tts", return_value=wav) as gen, \
         patch.object(ann, "_serve_url", return_value="http://serve.example/evt_x.wav"):
        ok = ann.announce("Let there be light.", "media_player.kitchen", call_service)
    assert ok is True
    gen.assert_called_once()
    assert calls == [(
        "media_player", "play_media",
        {"entity_id": ["media_player.kitchen"],
         "media_content_id": "http://serve.example/evt_x.wav",
         "media_content_type": "music"},
    )]


def test_tts_failure_returns_false_never_raises():
    with patch.object(ann, "_generate_tts", return_value=None):
        ok = ann.announce("x", "media_player.kitchen", lambda *a, **kw: {})
    assert ok is False


def test_play_failure_returns_false_never_raises(tmp_path):
    wav = tmp_path / "evt_x.wav"
    wav.write_bytes(b"RIFF")
    def boom(*a, **kw):
        raise RuntimeError("speaker offline")
    with patch.object(ann, "_generate_tts", return_value=wav), \
         patch.object(ann, "_serve_url", return_value="http://s/e.wav"):
        assert ann.announce("x", "media_player.kitchen", boom) is False
