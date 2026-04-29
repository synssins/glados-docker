"""Tests for glados.core.tls — the single source of truth for TLS wrapping.

Validates:
- get_ssl_context() returns None when cert files are absent.
- get_ssl_context() returns a configured SSLContext when files exist.
- maybe_wrap_socket() returns the right protocol string AND actually
  wraps the underlying socket when a cert is present.
- internal_api_url() / internal_api_port() honor the env-driven knob.
"""
from __future__ import annotations

import socket
import ssl
from pathlib import Path

import pytest


@pytest.fixture
def cert_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert+key pair in tmp_path. Returns (cert, key)."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.local")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return cert_path, key_path


def _patch_paths(monkeypatch, cert: Path, key: Path) -> None:
    """Point glados.core.tls at the given cert/key paths."""
    from glados.core import tls as _tls
    monkeypatch.setattr(
        _tls, "_resolve_cert_paths", lambda: (cert, key)
    )


def test_get_ssl_context_returns_none_when_files_absent(monkeypatch, tmp_path) -> None:
    _patch_paths(monkeypatch, tmp_path / "missing.pem", tmp_path / "missing.key")
    from glados.core import tls as _tls
    assert _tls.get_ssl_context() is None
    assert _tls.is_tls_active() is False


def test_get_ssl_context_returns_context_when_files_present(monkeypatch, cert_pair) -> None:
    _patch_paths(monkeypatch, *cert_pair)
    from glados.core import tls as _tls
    ctx = _tls.get_ssl_context()
    assert ctx is not None
    assert isinstance(ctx, ssl.SSLContext)
    assert _tls.is_tls_active() is True


def test_maybe_wrap_socket_no_cert_returns_http(monkeypatch, tmp_path) -> None:
    _patch_paths(monkeypatch, tmp_path / "missing.pem", tmp_path / "missing.key")
    from glados.core.tls import maybe_wrap_socket

    class _FakeServer:
        def __init__(self) -> None:
            self.socket = socket.socket()
    fake = _FakeServer()
    raw_sock = fake.socket
    proto = maybe_wrap_socket(fake)
    assert proto == "http"
    assert fake.socket is raw_sock  # untouched
    fake.socket.close()


def test_maybe_wrap_socket_with_cert_returns_https_and_wraps(monkeypatch, cert_pair) -> None:
    _patch_paths(monkeypatch, *cert_pair)
    from glados.core.tls import maybe_wrap_socket

    class _FakeServer:
        def __init__(self) -> None:
            self.socket = socket.socket()
    fake = _FakeServer()
    raw_sock = fake.socket
    proto = maybe_wrap_socket(fake)
    assert proto == "https"
    # ssl.SSLContext.wrap_socket returns an SSLSocket distinct from the raw.
    assert isinstance(fake.socket, ssl.SSLSocket)
    assert fake.socket is not raw_sock


def test_internal_api_port_default(monkeypatch) -> None:
    monkeypatch.delenv("GLADOS_INTERNAL_API_PORT", raising=False)
    from glados.core import tls as _tls
    assert _tls.internal_api_port() == 18015


def test_internal_api_port_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GLADOS_INTERNAL_API_PORT", "19999")
    from glados.core import tls as _tls
    assert _tls.internal_api_port() == 19999


def test_internal_api_port_invalid_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("GLADOS_INTERNAL_API_PORT", "not-a-number")
    from glados.core import tls as _tls
    assert _tls.internal_api_port() == 18015


def test_internal_api_url_uses_loopback(monkeypatch) -> None:
    monkeypatch.delenv("GLADOS_INTERNAL_API_PORT", raising=False)
    from glados.core.tls import internal_api_url
    assert internal_api_url() == "http://127.0.0.1:18015"
