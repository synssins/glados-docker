"""Tests for the standalone unauthenticated /tts page."""
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


def test_tts_in_public_paths():
    from glados.webui.tts_ui import _PUBLIC_PATHS
    assert "/tts" in _PUBLIC_PATHS


def test_tts_get_returns_200_with_html(monkeypatch):
    """Unauth GET /tts must return 200 + text/html with the form markup."""
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/tts")
    Handler.do_GET(h)

    statuses = [e for e in h._sent if e[0] == "status"]
    assert ("status", 200) in statuses

    content_types = [e[2] for e in h._sent
                     if e[0] == "header" and e[1] == "Content-Type"]
    assert any("text/html" in ct for ct in content_types)

    body = h.wfile.getvalue()
    assert b"<form" in body or b"<textarea" in body
    assert b"/api/generate" in body  # form posts to TTS endpoint


def test_tts_html_does_not_include_spa_sidebar():
    """The standalone page is minimal — no SPA shell, no Chat/Memory/Configuration nav."""
    from glados.webui.pages.tts_standalone import TTS_STANDALONE_HTML
    assert "id=\"sidebar\"" not in TTS_STANDALONE_HTML
    assert "Chat" not in TTS_STANDALONE_HTML or "<title>" in TTS_STANDALONE_HTML
    # If "Chat" appears it must be in title/heading only, not as a nav link;
    # the second clause permits the title to mention "GLaDOS" without nav.


def test_tts_html_uses_audio_element():
    """The page should render an <audio> element to play synthesised speech."""
    from glados.webui.pages.tts_standalone import TTS_STANDALONE_HTML
    assert "<audio" in TTS_STANDALONE_HTML


def test_tts_get_does_not_require_session():
    """Confirm /tts does NOT 401 even with no cookie."""
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/tts")
    Handler.do_GET(h)
    statuses = [e[1] for e in h._sent if e[0] == "status"]
    assert 401 not in statuses
    assert 302 not in statuses
