"""Single source of truth for "should this listener be TLS-wrapped?"

When SSL cert + key files exist on disk, every externally-exposed
container port (OpenAI API on 8015, audio file server on 5051, WebUI
on 8052) wraps its socket with TLS using the same cert. When they
don't exist, every port stays plain HTTP. The decision is made once
here so adding a new listener is a one-line change at the bind site.

Cert paths come from ``glados.core.config_store.cfg.ssl.{cert,key}_path``
(env-driven via ``SSL_CERT`` / ``SSL_KEY``, defaults
``/app/certs/cert.pem`` and ``/app/certs/key.pem``). Decision is on file
*presence*, not on ``cfg.ssl.enabled`` — matches the WebUI's existing
bind logic in ``tts_ui.py``. The enabled flag is for runtime status
reporting only.
"""
from __future__ import annotations

import ssl
from pathlib import Path

from loguru import logger

# Loopback-only plain-HTTP port for in-container callers. Always plain
# regardless of TLS state on the public-facing ports — internal calls
# don't validate against the public domain cert anyway, and avoiding
# the mismatch is cheaper than wiring skip-verify into every caller.
INTERNAL_API_HOST = "127.0.0.1"
INTERNAL_API_PORT_DEFAULT = 18015


def _resolve_cert_paths() -> tuple[Path, Path]:
    """Return (cert, key) paths from config_store, falling back to the
    container's default mount points if the config isn't loadable yet.
    """
    try:
        from glados.core.config_store import cfg
        return Path(cfg.ssl.cert_path), Path(cfg.ssl.key_path)
    except Exception:
        return Path("/app/certs/cert.pem"), Path("/app/certs/key.pem")


def get_ssl_context() -> ssl.SSLContext | None:
    """Build an :class:`ssl.SSLContext` from the configured cert + key,
    or return ``None`` when files are missing or load fails.

    Callers wrap their server socket with the returned context if
    non-``None``, else fall through to plain HTTP. See
    :func:`maybe_wrap_socket` for the typical pattern.
    """
    cert_path, key_path = _resolve_cert_paths()
    if not (cert_path.exists() and key_path.exists()):
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx
    except (ssl.SSLError, OSError) as exc:
        logger.warning(
            "SSL context load failed at {!s}: {}; falling back to plain HTTP",
            cert_path, exc,
        )
        return None


def maybe_wrap_socket(server) -> str:
    """Wrap ``server.socket`` with TLS if a cert is available.

    Returns the protocol the server now speaks (``"https"`` or
    ``"http"``). Use the returned string for log lines / URL builders so
    operators can read which mode is active in the boot log.

    ``server`` is anything with a writable ``.socket`` attribute, e.g.
    :class:`http.server.HTTPServer` or
    :class:`http.server.ThreadingHTTPServer`.
    """
    ctx = get_ssl_context()
    if ctx is None:
        return "http"
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return "https"


def is_tls_active() -> bool:
    """True iff cert + key files are loadable (``get_ssl_context()`` would
    return non-None). Used by URL builders that need to choose
    ``http://`` vs ``https://`` without actually wrapping a socket.
    """
    return get_ssl_context() is not None


def internal_api_port() -> int:
    """Loopback-only plain-HTTP port for in-container API callers.

    Operator-tunable via ``GLADOS_INTERNAL_API_PORT``; default 18015.
    """
    import os
    try:
        return int(os.environ.get("GLADOS_INTERNAL_API_PORT", str(INTERNAL_API_PORT_DEFAULT)))
    except ValueError:
        return INTERNAL_API_PORT_DEFAULT


def internal_api_url() -> str:
    """Canonical internal URL for in-container API callers.

    Always plain HTTP, always 127.0.0.1, never bound to the LAN. Use
    this anywhere a in-container caller previously hardcoded
    ``http://localhost:8015``.
    """
    return f"http://{INTERNAL_API_HOST}:{internal_api_port()}"
