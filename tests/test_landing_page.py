"""Tests for / route — SPA shell served to both auth and unauth users.

The landing page module was removed (Fix #3). / now always returns the
SPA shell; client-side updateAuthUI() handles role-based sidebar visibility.
"""
import io
from unittest.mock import MagicMock

import pytest


def _make_get_handler(path, cookie=""):
    h = MagicMock()
    h.path = path
    h.command = "GET"
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Cookie": cookie}
    h._sent = []
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: h._sent.append(("end_headers",))
    h.wfile = io.BytesIO()
    return h


# ── do_GET("/") behaviour ──────────────────────────────────────────

def test_root_renders_spa_shell_for_unauth(monkeypatch):
    """GET / while unauth → SPA shell (dispatch), NOT the old landing card."""
    from glados.webui import tts_ui
    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: False)

    h = _make_get_handler("/")
    tts_ui.Handler.do_GET(h)

    # _dispatch_get routes to _serve_ui which writes the SPA.
    # The old landing page had an inline <style> card — confirm it's gone.
    body = h.wfile.getvalue()
    assert b'class="card"' not in body
    # No redirect to /login
    locations = [e[2] for e in h._sent if e[0] == "header" and e[1] == "Location"]
    assert not locations, f"Unexpected redirect: {locations}"


def test_root_no_redirect_for_unauth(monkeypatch):
    """GET / while unauth must NOT redirect to /login."""
    from glados.webui import tts_ui
    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: False)

    h = _make_get_handler("/")
    tts_ui.Handler.do_GET(h)

    locations = [e[2] for e in h._sent if e[0] == "header" and e[1] == "Location"]
    assert not locations, f"Unexpected redirect to {locations}"


def test_root_serves_spa_for_authed(monkeypatch):
    """GET / while authed → SPA shell (same path as unauth now)."""
    from glados.webui import tts_ui

    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: True)
    monkeypatch.setattr(tts_ui, "require_perm", lambda h, p: True)

    h = _make_get_handler("/")
    tts_ui.Handler.do_GET(h)

    # Landing card HTML must NOT appear regardless of auth state.
    body = h.wfile.getvalue()
    assert b'class="card"' not in body
