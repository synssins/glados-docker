"""Tests for GLADOS_AUTH_BYPASS mode."""
import importlib
import os
import pytest


@pytest.fixture
def bypass_on(monkeypatch):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", "1")
    import glados.auth.bypass as b
    importlib.reload(b)
    yield b
    monkeypatch.delenv("GLADOS_AUTH_BYPASS", raising=False)
    importlib.reload(b)


@pytest.fixture
def bypass_off(monkeypatch):
    monkeypatch.delenv("GLADOS_AUTH_BYPASS", raising=False)
    import glados.auth.bypass as b
    importlib.reload(b)
    yield b


def test_bypass_active_when_env_set(bypass_on):
    assert bypass_on.active() is True


def test_bypass_inactive_by_default(bypass_off):
    assert bypass_off.active() is False


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_bypass_inactive_for_falsy_values(monkeypatch, val):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", val)
    import glados.auth.bypass as b
    importlib.reload(b)
    assert b.active() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes", "On"])
def test_bypass_active_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("GLADOS_AUTH_BYPASS", val)
    import glados.auth.bypass as b
    importlib.reload(b)
    assert b.active() is True


def test_banner_html_when_active(bypass_on):
    html = bypass_on.banner_html()
    assert "AUTHENTICATION BYPASS" in html.upper()
    assert "GLADOS_AUTH_BYPASS" in html
    assert "background" in html.lower()  # styled


def test_banner_html_empty_when_inactive(bypass_off):
    assert bypass_off.banner_html() == ""


def test_audit_tag_when_active(bypass_on):
    tag = bypass_on.audit_tag(remote_addr="10.0.0.5")
    assert tag["auth_bypass"] is True
    assert tag["operator_id"] == "bypass:10.0.0.5"


def test_audit_tag_empty_when_inactive(bypass_off):
    assert bypass_off.audit_tag(remote_addr="10.0.0.5") == {}


def test_audit_tag_unknown_addr_when_active(bypass_on):
    tag = bypass_on.audit_tag()
    assert tag["operator_id"] == "bypass:unknown"


# ── Banner injection ─────────────────────────────────────────────

def test_inject_bypass_banner_no_op_when_inactive(bypass_off):
    from glados.webui.tts_ui import _inject_bypass_banner
    body = b"<html><body><h1>Hi</h1></body></html>"
    assert _inject_bypass_banner(body) == body


def test_inject_bypass_banner_after_body_when_active(bypass_on):
    # Need to reload tts_ui too so its bypass import sees the active state
    from glados.webui import tts_ui
    body = b"<html><body><h1>Hi</h1></body></html>"
    out = tts_ui._inject_bypass_banner(body)
    assert b"AUTHENTICATION BYPASS" in out.upper()
    # Banner is after <body>
    body_idx = out.find(b"<body>")
    banner_idx = out.upper().find(b"AUTHENTICATION BYPASS")
    assert body_idx < banner_idx


def test_inject_bypass_banner_prepends_when_no_body_tag(bypass_on):
    from glados.webui import tts_ui
    body = b"<h1>No body tag</h1>"
    out = tts_ui._inject_bypass_banner(body)
    assert out.startswith(b"\n<div id=\"glados-auth-bypass-banner\"") or b"AUTHENTICATION BYPASS" in out[:200].upper()


# ── /api/auth/status with bypass ─────────────────────────────────

def test_auth_status_reports_bypass(bypass_on):
    from glados.webui import tts_ui
    from unittest.mock import MagicMock
    h = MagicMock()
    h.headers = {"Cookie": ""}
    h._json_response = None
    h._send_json = lambda c, p: setattr(h, "_json_response", (c, p))
    tts_ui.Handler._get_auth_status(h)
    code, body = h._json_response
    assert code == 200
    assert body["bypass"] is True
    assert body["user"]["role"] == "admin"


def test_auth_status_normal_when_inactive(bypass_off):
    from glados.webui import tts_ui
    from unittest.mock import MagicMock
    h = MagicMock()
    h.headers = {"Cookie": ""}
    h._json_response = None
    h._send_json = lambda c, p: setattr(h, "_json_response", (c, p))
    h._resolved_user = None
    tts_ui.Handler._get_auth_status(h)
    code, body = h._json_response
    assert body["bypass"] is False
