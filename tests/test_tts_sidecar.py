"""Sidecar `.txt` files persist the generation prompt for each `.wav`.

The TTS Generator UI rebuild (2026-04-26 revert of Chunk 8) writes a
sibling `<wav>.txt` file containing the exact text passed to synth.
`_list_files` reads it (when present) and returns it as `prompt`.
`_delete_file` cleans up the sidecar alongside the wav.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from glados.webui import tts_ui
from glados.webui.tts_ui import Handler


def _make_handler(path: str, body: bytes = b"") -> Handler:
    h = Handler.__new__(Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h._status_code = None

    def _send_response(code: int, *_a, **_k) -> None:
        h._status_code = code

    h.send_response = _send_response  # type: ignore[method-assign]
    h.send_header = lambda *a, **k: None  # type: ignore[method-assign]
    h.end_headers = lambda: None  # type: ignore[method-assign]
    return h


def _resp(h: Handler) -> tuple[int, dict]:
    return h._status_code, json.loads(h.wfile.getvalue().decode("utf-8"))


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_ui, "OUTPUT_DIR", tmp_path)
    return tmp_path


def test_list_files_includes_prompt_from_sidecar(output_dir):
    wav = output_dir / "Hey-There.wav"
    wav.write_bytes(b"RIFF....fakewav")
    (output_dir / "Hey-There.wav.txt").write_text(
        "Hey there friend", encoding="utf-8"
    )
    h = _make_handler("/api/files")
    h._list_files()
    status, payload = _resp(h)
    assert status == 200
    rows = {f["name"]: f for f in payload["files"]}
    assert rows["Hey-There.wav"]["prompt"] == "Hey there friend"


def test_list_files_prompt_none_when_sidecar_missing(output_dir):
    (output_dir / "Legacy.wav").write_bytes(b"RIFF")
    h = _make_handler("/api/files")
    h._list_files()
    _, payload = _resp(h)
    rows = {f["name"]: f for f in payload["files"]}
    assert rows["Legacy.wav"]["prompt"] is None


def test_list_files_skips_txt_sidecars(output_dir):
    (output_dir / "x.wav").write_bytes(b"R")
    (output_dir / "x.wav.txt").write_text("hi", encoding="utf-8")
    h = _make_handler("/api/files")
    h._list_files()
    _, payload = _resp(h)
    names = [f["name"] for f in payload["files"]]
    assert names == ["x.wav"]


def test_delete_removes_sidecar(output_dir):
    wav = output_dir / "Doomed.wav"
    wav.write_bytes(b"R")
    sidecar = output_dir / "Doomed.wav.txt"
    sidecar.write_text("doomed", encoding="utf-8")
    h = _make_handler("/api/files/Doomed.wav")
    h._delete_file()
    status, _ = _resp(h)
    assert status == 200
    assert not wav.exists()
    assert not sidecar.exists()


def test_delete_tolerates_missing_sidecar(output_dir):
    wav = output_dir / "NoSidecar.wav"
    wav.write_bytes(b"R")
    h = _make_handler("/api/files/NoSidecar.wav")
    h._delete_file()
    status, _ = _resp(h)
    assert status == 200
    assert not wav.exists()


def test_generate_writes_sidecar(output_dir):
    body = json.dumps({"text": "Hello there", "format": "wav"}).encode()
    h = _make_handler("/api/generate", body)
    fake_audio = b"RIFFfakewav"

    class _FakeResp:
        def read(self): return fake_audio
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch(
        "glados.webui.tts_ui._apply_pronunciation_to_text",
        side_effect=lambda t: t,
    ), patch(
        "urllib.request.urlopen", return_value=_FakeResp()
    ), patch(
        "glados.webui.tts_ui._cleanup_old_files"
    ):
        h._generate()
    status, payload = _resp(h)
    assert status == 200
    fname = payload["filename"]
    assert (output_dir / fname).exists()
    assert (
        output_dir / f"{fname}.txt"
    ).read_text(encoding="utf-8") == "Hello there"
