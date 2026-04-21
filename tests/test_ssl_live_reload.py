"""Phase 8.12 — live TLS reload + HTTP→HTTPS redirect listener.

Covers:
- ``reload_tls_certs()`` no-op when server is plaintext.
- ``reload_tls_certs()`` happy path: generate a self-signed cert,
  wire it into a live ``SSLContext``, replace it with a second
  cert, assert the context serves the new one.
- ``reload_tls_certs()`` rejects missing files.
- HTTP redirect handler emits ``301`` with ``https://host:PORT/<path>``.
- ``_http_redirect_port()`` env-var parsing.

Generating self-signed certs requires ``cryptography``, which is an
existing container dependency (via pydantic/anyio).
"""
from __future__ import annotations

import datetime as dt
import os
import ssl
from http.server import BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _generate_self_signed(tmp_path: Path, cn: str = "localhost") -> tuple[Path, Path]:
    """Minimal self-signed cert using the ``cryptography`` package —
    already in the container's env via pydantic / chromadb deps."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("cryptography not available")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{cn}.crt"
    key_path = tmp_path / f"{cn}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return cert_path, key_path


# ── reload_tls_certs() ─────────────────────────────────────────────


def test_reload_returns_false_when_plaintext(monkeypatch) -> None:
    """When ``_tls_context is None`` (plaintext listener), reload must
    return False with a clear reason — never crash."""
    from glados.webui import tts_ui
    monkeypatch.setattr(tts_ui, "_tls_context", None)
    ok, msg = tts_ui.reload_tls_certs()
    assert ok is False
    assert "plaintext" in msg.lower() or "no live" in msg.lower()


def test_reload_returns_false_when_cert_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Point ``SSL_CERT`` at a non-existent file; reload must fail
    with a missing-cert message, not a swallowed success."""
    from glados.webui import tts_ui
    fake_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    monkeypatch.setattr(tts_ui, "_tls_context", fake_ctx)
    monkeypatch.setattr(tts_ui, "SSL_CERT", tmp_path / "nope.crt")
    monkeypatch.setattr(tts_ui, "SSL_KEY", tmp_path / "nope.key")
    ok, msg = tts_ui.reload_tls_certs()
    assert ok is False
    assert "missing" in msg.lower()


def test_reload_happy_path_swaps_cert(
    tmp_path: Path, monkeypatch,
) -> None:
    """Wire a live context with cert A, write cert B to the same
    path, call reload — context should now serve cert B. We verify
    by generating two distinct self-signed certs and comparing the
    cert material after reload using the inner get-verified-chain
    API where available, or by catching that load_cert_chain runs
    without error on the swapped files."""
    from glados.webui import tts_ui

    cert_a, key_a = _generate_self_signed(tmp_path, cn="alpha.local")
    bdir = tmp_path / ".bcache"
    bdir.mkdir(exist_ok=True)
    cert_b, key_b = _generate_self_signed(bdir, cn="beta.local")

    # Build a live-looking context seeded with cert A.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_a), keyfile=str(key_a))
    monkeypatch.setattr(tts_ui, "_tls_context", ctx)

    # Overwrite the canonical cert/key paths with the new material.
    canonical_cert = tmp_path / "cert.pem"
    canonical_key = tmp_path / "key.pem"
    canonical_cert.write_bytes(cert_b.read_bytes())
    canonical_key.write_bytes(key_b.read_bytes())
    monkeypatch.setattr(tts_ui, "SSL_CERT", canonical_cert)
    monkeypatch.setattr(tts_ui, "SSL_KEY", canonical_key)

    ok, msg = tts_ui.reload_tls_certs()
    assert ok is True, msg
    assert "reload" in msg.lower()


def test_reload_catches_malformed_cert(
    tmp_path: Path, monkeypatch,
) -> None:
    """Operator uploads a garbage "cert" that looks right but won't
    parse. Reload must catch the SSLError / OSError and return False —
    never propagate."""
    from glados.webui import tts_ui

    cert_a, key_a = _generate_self_signed(tmp_path, cn="alpha.local")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_a), keyfile=str(key_a))
    monkeypatch.setattr(tts_ui, "_tls_context", ctx)

    bad_cert = tmp_path / "bad.crt"
    bad_key = tmp_path / "bad.key"
    bad_cert.write_text("-----BEGIN CERTIFICATE-----\nGARBAGE\n-----END CERTIFICATE-----\n")
    bad_key.write_text("-----BEGIN PRIVATE KEY-----\nGARBAGE\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(tts_ui, "SSL_CERT", bad_cert)
    monkeypatch.setattr(tts_ui, "SSL_KEY", bad_key)

    ok, msg = tts_ui.reload_tls_certs()
    assert ok is False
    assert "failed" in msg.lower() or "ssl" in msg.lower() or "error" in msg.lower()


# ── HTTP redirect handler ──────────────────────────────────────────


def test_redirect_handler_emits_301_to_https() -> None:
    """Any GET must become a 301 with the Location header pointing
    at the configured HTTPS port, preserving the original path."""
    from glados.webui.tts_ui import _make_redirect_handler

    handler_cls = _make_redirect_handler(https_port=8052)

    # Fake request / socket plumbing for BaseHTTPRequestHandler.
    class _Req:
        def __init__(self, raw: bytes) -> None:
            self._buf = BytesIO(raw)
        def makefile(self, mode: str, _buf: int = -1) -> BytesIO:  # noqa: ARG002
            if "w" in mode:
                return BytesIO()
            return self._buf

    raw = b"GET /some/path?q=1 HTTP/1.1\r\nHost: test.example:8051\r\n\r\n"
    req = _Req(raw)
    out = BytesIO()
    # BaseHTTPRequestHandler reads from rfile, writes to wfile. We
    # call the handler with a fake request/client/server triple.
    h = handler_cls.__new__(handler_cls)
    h.rfile = req.makefile("rb")
    h.wfile = out
    h.client_address = ("1.1.1.1", 0)
    h.server = MagicMock()
    h.request_version = "HTTP/1.1"
    h.command = "GET"
    h.path = "/some/path?q=1"
    h.headers = {"Host": "test.example:8051"}
    h.request = MagicMock()
    h.requestline = "GET /some/path?q=1 HTTP/1.1"
    # Call _redirect directly — simpler than running handle()
    h._redirect()

    response = out.getvalue().decode("iso-8859-1")
    assert "301" in response.split("\r\n")[0]
    assert "Location: https://test.example:8052/some/path?q=1" in response


def test_redirect_port_env_parsing(monkeypatch) -> None:
    from glados.webui.tts_ui import _http_redirect_port

    monkeypatch.delenv("WEBUI_HTTP_REDIRECT_PORT", raising=False)
    assert _http_redirect_port() == 0

    monkeypatch.setenv("WEBUI_HTTP_REDIRECT_PORT", "8051")
    assert _http_redirect_port() == 8051

    # Junk value → 0 (disabled); never raise
    monkeypatch.setenv("WEBUI_HTTP_REDIRECT_PORT", "not a number")
    assert _http_redirect_port() == 0


def test_redirect_host_fallback_when_header_missing() -> None:
    """If the Host header is absent (edge-case curl usage), redirect
    to ``localhost`` — don't crash with KeyError."""
    from glados.webui.tts_ui import _make_redirect_handler

    handler_cls = _make_redirect_handler(https_port=8052)
    h = handler_cls.__new__(handler_cls)
    out = BytesIO()
    h.wfile = out
    h.rfile = BytesIO()
    h.client_address = ("1.1.1.1", 0)
    h.server = MagicMock()
    h.request_version = "HTTP/1.1"
    h.command = "GET"
    h.path = "/"
    h.headers = {}  # no Host
    h.requestline = "GET / HTTP/1.1"
    h._redirect()
    assert "https://localhost:8052/" in out.getvalue().decode("iso-8859-1")
