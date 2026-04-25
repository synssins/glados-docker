"""Tests for the unauthenticated landing page at /."""
import io
from unittest.mock import MagicMock

import pytest

from glados.webui.pages.landing import LANDING_HTML


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


# ── landing.py module ──────────────────────────────────────────────

def test_landing_html_is_string():
    assert isinstance(LANDING_HTML, str)
    assert len(LANDING_HTML) > 0


def test_landing_html_has_signin_link():
    assert 'href="/login"' in LANDING_HTML


def test_landing_html_has_tts_link():
    assert 'href="/tts"' in LANDING_HTML


def test_landing_html_has_glados_brand():
    assert "GLaDOS" in LANDING_HTML


# ── do_GET("/") behaviour ──────────────────────────────────────────

def test_root_landing_page_for_unauth(monkeypatch):
    """GET / while unauth → 200 landing page, NOT a 302."""
    from glados.webui import tts_ui
    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: False)

    h = _make_get_handler("/")
    tts_ui.Handler.do_GET(h)

    statuses = [e[1] for e in h._sent if e[0] == "status"]
    assert statuses == [200], f"Expected 200, got {statuses}"

    body = h.wfile.getvalue()
    assert b"GLaDOS" in body
    assert b"/login" in body
    assert b"/tts" in body


def test_root_no_redirect_for_unauth(monkeypatch):
    """GET / while unauth must NOT redirect to /login."""
    from glados.webui import tts_ui
    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: False)

    h = _make_get_handler("/")
    tts_ui.Handler.do_GET(h)

    locations = [e[2] for e in h._sent if e[0] == "header" and e[1] == "Location"]
    assert not locations, f"Unexpected redirect to {locations}"


def test_root_serves_spa_for_authed(monkeypatch):
    """GET / while authed → the SPA shell (dispatch, not landing).

    We verify this by checking the response is NOT the 200 landing page
    HTML (since _dispatch_get itself would write the SPA, but the MagicMock
    handler absorbs it; we just confirm the landing HTML was NOT written).
    """
    from glados.webui import tts_ui

    monkeypatch.setattr(tts_ui, "_is_authenticated", lambda h: True)
    monkeypatch.setattr(tts_ui, "require_perm", lambda h, p: True)

    h = _make_get_handler("/")
    # _dispatch_get on MagicMock won't write anything — we just verify
    # the landing HTML bytes were NOT written to the response.
    tts_ui.Handler.do_GET(h)

    body = h.wfile.getvalue()
    # If authed, we should NOT receive the standalone landing card HTML
    assert b'href="/login"' not in body or b"SPA" in body, \
        "Authed user should not receive the landing page"
