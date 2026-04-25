"""Tests for the /tts route after Fix #3.

/tts now 302-redirects to / so the SPA shell renders with TTS Generator
as the default active panel. The standalone page module is kept but no
longer served directly.
"""
import io
from unittest.mock import MagicMock

import pytest


def _make_get_handler(path):
    h = MagicMock()
    h.path = path
    h.command = "GET"
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Cookie": ""}
    h._sent = []
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: h._sent.append(("end_headers",))
    h.wfile = io.BytesIO()
    return h


def test_tts_get_returns_302_to_root():
    """GET /tts must 302 to / (SPA shell, TTS panel is default for unauth)."""
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/tts")
    Handler.do_GET(h)

    statuses = [e[1] for e in h._sent if e[0] == "status"]
    assert 302 in statuses, f"Expected 302, got {statuses}"

    locations = [e[2] for e in h._sent if e[0] == "header" and e[1] == "Location"]
    assert locations == ["/"], f"Expected redirect to /, got {locations}"


def test_tts_get_does_not_return_200():
    """GET /tts must NOT return 200 with the old standalone HTML."""
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/tts")
    Handler.do_GET(h)

    statuses = [e[1] for e in h._sent if e[0] == "status"]
    assert 200 not in statuses


def test_tts_standalone_module_audio_element():
    """The standalone module (kept for reference) still has an <audio> element."""
    from glados.webui.pages.tts_standalone import TTS_STANDALONE_HTML
    assert "<audio" in TTS_STANDALONE_HTML


def test_tts_standalone_module_uses_json_url():
    """Fix #1: standalone page JS must use resp.json() + data.url, not resp.blob()."""
    from glados.webui.pages.tts_standalone import TTS_STANDALONE_HTML
    assert "resp.json()" in TTS_STANDALONE_HTML
    assert "data.url" in TTS_STANDALONE_HTML
    assert "resp.blob()" not in TTS_STANDALONE_HTML
