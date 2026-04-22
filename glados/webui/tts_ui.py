"""GLaDOS Control Panel â€” TTS Generator, Chat, and System Control.

Expands the original TTS web UI into a three-tab control panel:
  Tab 1: TTS Generator (text â†' GLaDOS voice audio files)
  Tab 2: Chat with GLaDOS (text/voice chat with audio playback)
  Tab 3: System Control (maintenance/silent mode, health indicators)

Usage (container):
    python -m glados.webui.tts_ui
    run_webui(host="0.0.0.0", port=8052)  # called from glados.server
"""

import atexit
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# ---------------------------------------------------------------------------
# Configuration â€” all values from centralized config store
# ---------------------------------------------------------------------------
from glados.core.config_store import cfg as _cfg
from glados.observability import AuditEvent, Origin, audit

# Service URLs and models are read live via these helpers so that a
# config save (LLM & Services page, etc.) takes effect without any
# process restart. Do NOT replace with module-level constants — those
# freeze at import time and leave the UI silently calling a stale
# backend after the operator moves a service or swaps a model.

def _svc_tts_speech() -> str:
    return _cfg.service_url("tts") + "/v1/audio/speech"

def _svc_tts_base() -> str:
    return _cfg.service_url("tts")

def _svc_api_wrapper() -> str:
    return _cfg.service_url("api_wrapper")

def _svc_stt() -> str:
    return _cfg.service_url("stt")

def _svc_vision() -> str:
    return _cfg.service_url("vision")

def _svc_ollama_generate() -> str:
    return _cfg.service_url("ollama_interactive") + "/api/generate"

def _svc_ollama_model() -> str:
    # Read the operator-selected model from services.yaml; fall back to
    # qwen3:8b (the current Phase 8.0 default) if unset.
    return (_cfg.services.ollama_interactive.model or "qwen3:8b").strip()

OUTPUT_DIR = Path(_cfg.audio.tts_ui_output_dir)
CHAT_AUDIO_DIR = Path(_cfg.audio.chat_audio_dir)
MAX_FILES = _cfg.audio.tts_ui_max_files
CHAT_MAX_FILES = _cfg.audio.chat_audio_max_files
PORT = 8052

# â”€â”€ SSL configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_ssl_enabled = _cfg.ssl.enabled
SSL_CERT = Path(_cfg.ssl.cert_path) if hasattr(_cfg.ssl, "cert_path") else Path("/app/certs/cert.pem")
SSL_KEY  = Path(_cfg.ssl.key_path)  if hasattr(_cfg.ssl, "key_path")  else Path("/app/certs/key.pem")

# Phase 8.12 — live TLS reload. The HTTPS listener stores its
# ``SSLContext`` here at startup; ``reload_tls_certs()`` calls
# ``load_cert_chain`` on the same context to swap cert material
# without restarting the socket. Subsequent TLS handshakes pick
# up the new cert; existing connections keep theirs.
_tls_context: "ssl.SSLContext | None" = None


def reload_tls_certs() -> tuple[bool, str]:
    """Reload cert/key from ``SSL_CERT``/``SSL_KEY`` into the live
    ``_tls_context``. Returns ``(ok, message)``. Safe to call when
    the server runs plaintext (returns False with explanatory msg).
    Called after a successful upload or Let's Encrypt issue/renew so
    the operator sees "Certificate applied." instead of "Restart
    container to activate."
    """
    if _tls_context is None:
        return False, "server is plaintext; no live TLS context"
    if not (SSL_CERT.exists() and SSL_KEY.exists()):
        return False, f"cert or key missing ({SSL_CERT}, {SSL_KEY})"
    try:
        _tls_context.load_cert_chain(
            certfile=str(SSL_CERT), keyfile=str(SSL_KEY),
        )
        logger.info(
            "TLS cert reloaded from {} + {}", SSL_CERT, SSL_KEY,
        )
        return True, "certificate reloaded"
    except (ssl.SSLError, OSError) as exc:
        logger.error("TLS reload failed: {}", exc)
        return False, f"load_cert_chain failed: {exc}"

SPEAKER_BLACKLIST = set(_cfg.speakers.blacklist)

# Container service management -- services are Docker containers, not NSSM services.
SERVICE_MAP = {
    "glados_api": "glados",
    "stt":        "glados_speaches",
    "vision":     "glados_vision",
}
_DOCKER_SOCKET = Path(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))


def _apply_config_live(section: str) -> bool:
    """Apply a saved config section to live runtime without a process restart.

    Sections that affect the GLaDOS chat engine (services URLs / models,
    global HA URL/token, personality, memory, observer) trigger an engine
    hot-swap by POSTing to `api_wrapper`'s /api/reload-engine endpoint.
    Crossing the process boundary over HTTP is required — tts_ui.py (port
    8052) and api_wrapper.py (port 8015) are separate processes, so an
    in-process `reload_engine()` call would create a *second* engine in
    the wrong process and collide on port binding.

    Sections with no live consumers return True immediately.

    Returns True on success, False on reload failure (network or server-side).
    """
    engine_affecting = {
        "services", "global", "personality", "memory", "observer",
        "tts_pronunciation",  # rebuilds SpokenTextConverter (engine.py:~640)
    }
    if section not in engine_affecting:
        return True
    try:
        req = urllib.request.Request(
            _svc_api_wrapper() + "/api/reload-engine",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read() or b"{}")
            return bool(body.get("ok"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode()
        except Exception:
            err_body = ""
        logger.error("Live reload HTTP {} for section {!r}: {}", exc.code, section, err_body[:300])
        return False
    except Exception as exc:
        logger.error("Live reload for section {!r} failed: {}", section, exc)
        return False


def _docker_logs_tail(container: str, *, tail: int = 500, timestamps: bool = True,
                      socket_timeout_s: float = 15.0) -> str:
    """Fetch container logs via the Docker Engine API over the mounted
    unix socket. Avoids the docker CLI which is not installed in the
    container image. Returns stdout+stderr concatenated.

    Raises FileNotFoundError if the socket isn't mounted.

    The Docker API returns a multiplexed stream where each frame has an
    8-byte header: [stream_type(1) | 000 | size(4, big-endian)] followed
    by `size` bytes of payload. stream_type is 0 for stdin, 1 for stdout,
    2 for stderr. We flatten both streams into one chronological blob
    since loguru typically writes everything to stderr.
    """
    import socket as _socket
    import struct as _struct
    from urllib.parse import quote as _quote

    if not _DOCKER_SOCKET.exists():
        raise FileNotFoundError(str(_DOCKER_SOCKET))
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(socket_timeout_s)
    try:
        sock.connect(str(_DOCKER_SOCKET))
        path = (f"/containers/{_quote(container)}/logs"
                f"?stdout=1&stderr=1&tail={int(tail)}"
                f"&timestamps={'1' if timestamps else '0'}")
        req = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n"
        sock.sendall(req.encode("ascii"))
        buf = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    # Split HTTP headers from body.
    head_end = buf.find(b"\r\n\r\n")
    if head_end < 0:
        raise RuntimeError("malformed docker logs HTTP response")
    header_blob = bytes(buf[:head_end]).decode("iso-8859-1", "replace")
    if not header_blob.startswith("HTTP/1.") or " 200 " not in header_blob.split("\r\n", 1)[0]:
        status_line = header_blob.split("\r\n", 1)[0]
        raise RuntimeError(f"docker API returned: {status_line}")
    body = bytes(buf[head_end + 4:])

    # De-multiplex the stream. Each frame: 8-byte header, then payload.
    # When TTY mode is enabled on the container the API returns a raw
    # stream without framing — detect that by peeking at the first
    # byte: framed streams always start with 0/1/2.
    if body and body[0] > 2:
        return body.decode("utf-8", "replace")

    out = bytearray()
    i = 0
    while i + 8 <= len(body):
        size = _struct.unpack(">I", body[i + 4:i + 8])[0]
        i += 8
        if i + size > len(body):
            break
        out.extend(body[i:i + size])
        i += size
    return out.decode("utf-8", "replace")


CONTENT_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
}

# â”€â”€ HA config from centralized config store â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
HA_URL = _cfg.ha_url
HA_TOKEN = _cfg.ha_token

# â”€â”€ Robot Manager (lazy init) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_robot_manager = None
_robot_manager_lock = threading.Lock()

def _get_robot_manager():
    """Lazy-init RobotManager singleton. Returns None if robots disabled."""
    global _robot_manager
    if _robot_manager is not None:
        return _robot_manager
    with _robot_manager_lock:
        if _robot_manager is not None:
            return _robot_manager
        _cfg.reload()  # ensure fresh config
        if not _cfg.robots.enabled:
            return None
        from glados.robots.manager import RobotManager
        _robot_manager = RobotManager(_cfg.robots)
        _robot_manager.start()
        return _robot_manager

# â”€â”€ Eye demo subprocess management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_eye_demo_proc: subprocess.Popen | None = None
_eye_demo_lock = threading.Lock()
_EYE_DEMO_SCRIPT = Path(os.environ.get("GLADOS_ROOT", "/app")) / "tools" / "eye_demo.py"


def _eye_demo_running() -> bool:
    """Check if the eye demo subprocess is alive."""
    with _eye_demo_lock:
        return _eye_demo_proc is not None and _eye_demo_proc.poll() is None


def _eye_demo_start() -> dict:
    """Start the eye demo subprocess. Returns status dict."""
    global _eye_demo_proc
    with _eye_demo_lock:
        if _eye_demo_proc is not None and _eye_demo_proc.poll() is None:
            return {"ok": True, "running": True, "msg": "already running"}
        _eye_demo_proc = subprocess.Popen(
            [sys.executable, str(_EYE_DEMO_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # cross-platform
        )
        return {"ok": True, "running": True, "pid": _eye_demo_proc.pid}


def _eye_demo_stop() -> dict:
    """Stop the eye demo subprocess. Returns status dict."""
    global _eye_demo_proc
    with _eye_demo_lock:
        if _eye_demo_proc is None or _eye_demo_proc.poll() is not None:
            _eye_demo_proc = None
            return {"ok": True, "running": False, "msg": "not running"}
        try:
            _eye_demo_proc.terminate()
            _eye_demo_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _eye_demo_proc.kill()
            _eye_demo_proc.wait(timeout=2)
        _eye_demo_proc = None
        return {"ok": True, "running": False}


def _eye_demo_cleanup():
    """atexit handler â€” kill eye demo if still running."""
    _eye_demo_stop()


atexit.register(_eye_demo_cleanup)

# â”€â”€ Training Monitor state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import csv as _csv
import glob as _glob
import shutil as _shutil

# Voice training (piper_train) is a local host tool, not available in the container.
_TRAIN_BASE_EPOCH = 0
_TRAIN_LOG = Path("/dev/null")
_TRAIN_METRICS_DIR = Path("/dev/null")
_TRAIN_CONFIG = Path("/dev/null")
_TRAIN_VENV_PYTHON = Path("/dev/null")
_TRAIN_SNAPSHOT_DIR = Path("/dev/null")
_TRAIN_DEPLOY_ONNX = Path(os.environ.get("GLADOS_ROOT", "/app")) / "models" / "TTS" / "voices" / "startrek-computer.onnx"
_TRAIN_DEPLOY_JSON = Path(os.environ.get("GLADOS_ROOT", "/app")) / "models" / "TTS" / "voices" / "startrek-computer.onnx.json"

_snapshot_status = {"state": "idle", "message": "", "progress": 0}
_snapshot_lock = threading.Lock()


def _find_latest_metrics_csv():
    """Find the most recent metrics.csv in lightning_logs."""
    versions = sorted(_TRAIN_METRICS_DIR.glob("*/metrics.csv"))
    if not versions:
        return None
    return max(versions, key=lambda p: p.stat().st_mtime)


def _find_latest_checkpoint():
    """Find the most recent .ckpt file."""
    ckpts = list(_TRAIN_METRICS_DIR.glob("*/checkpoints/*.ckpt"))
    if not ckpts:
        return None
    return max(ckpts, key=lambda p: p.stat().st_mtime)


def _is_training_running():
    """Check if piper_train is running."""
    try:
        r = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")  # piper_train not available in container
        return bool(r.stdout.strip().replace("ProcessId", "").replace("-", "").strip())
    except Exception:
        return False


def _do_snapshot():
    """Background: copy checkpoint, export ONNX, deploy."""
    global _snapshot_status
    try:
        with _snapshot_lock:
            _snapshot_status = {"state": "running", "message": "Finding checkpoint...", "progress": 10}

        ckpt = _find_latest_checkpoint()
        if not ckpt:
            with _snapshot_lock:
                _snapshot_status = {"state": "error", "message": "No checkpoint found", "progress": 0}
            return

        # Copy checkpoint
        _TRAIN_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_ckpt = _TRAIN_SNAPSHOT_DIR / f"snapshot_{ts}.ckpt"
        with _snapshot_lock:
            _snapshot_status = {"state": "running", "message": "Copying checkpoint...", "progress": 25}
        _shutil.copy2(ckpt, snap_ckpt)

        # Export ONNX
        snap_onnx = _TRAIN_SNAPSHOT_DIR / f"snapshot_{ts}.onnx"
        with _snapshot_lock:
            _snapshot_status = {"state": "running", "message": "Exporting ONNX...", "progress": 50}

        result = subprocess.run(
            [str(_TRAIN_VENV_PYTHON), "-m", "piper_train.export_onnx",
             str(snap_ckpt), str(snap_onnx)],
            capture_output=True, text=True, timeout=300)

        if not snap_onnx.exists():
            with _snapshot_lock:
                _snapshot_status = {"state": "error",
                                    "message": f"ONNX export failed: {result.stderr[-200:]}", "progress": 0}
            return

        # Deploy
        with _snapshot_lock:
            _snapshot_status = {"state": "running", "message": "Deploying to GLaDOS...", "progress": 75}

        # Backup existing
        if _TRAIN_DEPLOY_ONNX.exists():
            backup = _TRAIN_DEPLOY_ONNX.with_suffix(".onnx.bak")
            _shutil.copy2(_TRAIN_DEPLOY_ONNX, backup)

        _shutil.copy2(snap_onnx, _TRAIN_DEPLOY_ONNX)
        if _TRAIN_CONFIG.exists():
            _shutil.copy2(_TRAIN_CONFIG, _TRAIN_DEPLOY_JSON)

        # Restart TTS service
        with _snapshot_lock:
            _snapshot_status = {"state": "running", "message": "Restarting TTS service...", "progress": 90}
        try:
            subprocess.run(["docker", "restart", "glados_speaches"],
                           capture_output=True, timeout=15)
        except Exception:
            pass

        with _snapshot_lock:
            _snapshot_status = {"state": "done", "message": f"Deployed snapshot_{ts}", "progress": 100}

    except Exception as e:
        with _snapshot_lock:
            _snapshot_status = {"state": "error", "message": str(e)[:200], "progress": 0}


# Ensure directories exist
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CHAT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Authentication â€” session-based with bcrypt
# ---------------------------------------------------------------------------

import bcrypt as _bcrypt

# Auth config (reloaded on each check to pick up changes)
_AUTH_ENABLED = _cfg.auth.enabled
_AUTH_PASSWORD_HASH = _cfg.auth.password_hash
_AUTH_SESSION_SECRET = _cfg.auth.session_secret
_AUTH_SESSION_TIMEOUT_H = _cfg.auth.session_timeout_hours

# Session durations
_SESSION_SHORT_S = _AUTH_SESSION_TIMEOUT_H * 3600       # normal session (24h default)
_SESSION_LONG_S = 30 * 24 * 3600                        # "stay logged in" (30 days)

# Rate limiting: {ip: (fail_count, last_fail_time)}
_login_fails: dict[str, tuple[int, float]] = {}
_login_fails_lock = threading.Lock()
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_S = 60

# Public paths that don't require auth
_PUBLIC_PATHS = frozenset({"/login", "/health"})

# Public route prefixes â€” TTS/Chat accessible without auth
_PUBLIC_PREFIXES = (
    "/api/generate", "/api/chat", "/api/stt",
    "/api/files", "/api/attitudes", "/api/speakers", "/api/voices",
    "/files/", "/chat_audio/", "/chat_audio_stream/",
    "/api/auth/",
    "/static/",
)


def _is_public_route(path: str) -> bool:
    """Check if a path is publicly accessible (no auth needed)."""
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _sign_session(payload: str) -> str:
    """Create an HMAC-signed session token."""
    sig = hmac.new(
        _AUTH_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def _verify_session(token: str) -> dict | None:
    """Verify and decode a session token. Returns payload dict or None."""
    if not token or "." not in token:
        return None
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return None
    payload_str, sig = parts
    expected = hmac.new(
        _AUTH_SESSION_SECRET.encode(), payload_str.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        return None
    # Expiry check. Operator-requested behavior (2026-04-20): once
    # logged in, sessions never time out — the admin browser stays
    # authenticated until the operator explicitly logs out or the
    # session_secret rotates. Legacy tokens with a real `exp` still
    # expire at that timestamp; new tokens carry exp=0 as the
    # "never expires" sentinel.
    exp = payload.get("exp", 0)
    if exp and exp < time.time():
        return None
    return payload


def _create_session(remember: bool = False) -> str:
    """Create a signed session token.

    Operator-requested (2026-04-20): sessions never expire. Both
    the "remember me" long session and the short session now carry
    exp=0 (sentinel: never expires). The `remember` argument is
    retained for backwards-compatible call sites but no longer
    changes behavior; the cookie Max-Age uses the long window so
    the browser keeps it across restarts."""
    payload = json.dumps({
        "sub": "admin",
        "iat": int(time.time()),
        "exp": 0,  # 0 = never expires (sentinel honored by _verify_session)
        "jti": secrets.token_hex(8),
    })
    return _sign_session(payload)


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is rate-limited."""
    with _login_fails_lock:
        if ip not in _login_fails:
            return False
        count, last_time = _login_fails[ip]
        if time.time() - last_time > _RATE_LIMIT_WINDOW_S:
            del _login_fails[ip]
            return False
        return count >= _RATE_LIMIT_MAX


def _record_fail(ip: str) -> None:
    """Record a failed login attempt."""
    with _login_fails_lock:
        count, _ = _login_fails.get(ip, (0, 0.0))
        _login_fails[ip] = (count + 1, time.time())


def _clear_fails(ip: str) -> None:
    """Clear failed login counter on success."""
    with _login_fails_lock:
        _login_fails.pop(ip, None)


def _get_session_cookie(handler: BaseHTTPRequestHandler) -> dict | None:
    """Extract and verify the session cookie from the request."""
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return None
    # Manual parsing â€” SimpleCookie chokes on JSON-like values
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("glados_session="):
            value = part[len("glados_session="):]
            return _verify_session(value)
    return None


def _is_authenticated(handler: BaseHTTPRequestHandler) -> bool:
    """Check if the request is authenticated.

    Prior behavior treated "auth enabled but no password hash set"
    as auto-authenticated — intended as a bootstrap convenience
    but functionally an open door: if the hash was wiped or never
    initialised, every request passed. Now fail-closed: when auth
    is enabled and no hash exists, deny and let the operator run
    `docker exec -it glados python -m glados.tools.set_password`
    to configure one. Auth can still be fully disabled via
    `auth.enabled=false` in global.yaml for development.
    """
    if not _AUTH_ENABLED:
        return True
    if not _AUTH_PASSWORD_HASH:
        # No password configured; refuse. The login page surfaces
        # the setup instruction so the admin isn't stranded.
        return False
    return _get_session_cookie(handler) is not None


def _auth_password_configured() -> bool:
    """True when a password hash is set. Login page uses this to
    show a setup hint instead of the normal form when the admin
    hasn't run set_password yet."""
    return bool(_AUTH_PASSWORD_HASH)


# â”€â”€ Login page HTML â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLaDOS â€” Login</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0a;
    color: #e0e0e0;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .login-box {
    background: #1a1a2e;
    border: 1px solid #333;
    border-radius: 12px;
    padding: 40px;
    width: 360px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5);
  }
  .login-box h1 {
    text-align: center;
    color: #ff6600;
    font-size: 1.6em;
    margin-bottom: 8px;
  }
  .login-box .subtitle {
    text-align: center;
    color: #888;
    font-size: 0.85em;
    margin-bottom: 28px;
  }
  .field { margin-bottom: 18px; }
  .field label {
    display: block;
    font-size: 0.85em;
    color: #aaa;
    margin-bottom: 6px;
  }
  .field input[type="password"] {
    width: 100%;
    padding: 10px 12px;
    background: #111;
    border: 1px solid #444;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 1em;
    outline: none;
    transition: border-color 0.2s;
  }
  .field input[type="password"]:focus { border-color: #ff6600; }
  .remember {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 22px;
    font-size: 0.85em;
    color: #aaa;
  }
  .remember input[type="checkbox"] { accent-color: #ff6600; }
  .btn {
    width: 100%;
    padding: 11px;
    background: #ff6600;
    color: #fff;
    border: none;
    border-radius: 6px;
    font-size: 1em;
    cursor: pointer;
    transition: background 0.2s;
  }
  .btn:hover { background: #e55a00; }
  .btn:disabled { background: #555; cursor: not-allowed; }
  .error {
    background: #3a1111;
    border: 1px solid #ff4444;
    color: #ff6666;
    padding: 10px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 0.85em;
    display: none;
  }
</style>
</head>
<body>
<div class="login-box">
  <h1>GLaDOS</h1>
  <div class="subtitle">Control Panel Authentication</div>
  <div class="error" id="error"></div>
  <form id="loginForm" method="POST" action="/login">
    <div class="field">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autofocus required>
    </div>
    <div class="remember">
      <input type="checkbox" id="remember" name="remember" value="1">
      <label for="remember">Stay logged in</label>
    </div>
    <button type="submit" class="btn" id="submitBtn">Sign In</button>
  </form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const err = document.getElementById('error');
  btn.disabled = true;
  btn.textContent = 'Signing in...';
  err.style.display = 'none';
  try {
    const resp = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({
        password: document.getElementById('password').value,
        remember: document.getElementById('remember').checked ? '1' : '0'
      })
    });
    const data = await resp.json();
    if (data.ok) {
      window.location.href = '/';
    } else {
      err.textContent = data.error || 'Invalid password';
      err.style.display = 'block';
      document.getElementById('password').value = '';
      document.getElementById('password').focus();
    }
  } catch (ex) {
    err.textContent = 'Connection error';
    err.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = 'Sign In';
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Helpers â€” TTS file management
# ---------------------------------------------------------------------------

def _cleanup_old_files():
    """Keep only the MAX_FILES most recent files in OUTPUT_DIR."""
    files = sorted(OUTPUT_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[MAX_FILES:]:
        try:
            f.unlink()
        except OSError:
            pass


def _cleanup_chat_audio():
    """Keep only the CHAT_MAX_FILES most recent files in CHAT_AUDIO_DIR."""
    try:
        files = sorted(CHAT_AUDIO_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[CHAT_MAX_FILES:]:
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Streaming audio â€” progressive TTS playback
# ---------------------------------------------------------------------------

# Global registry of streaming audio sessions.  Each session accumulates raw
# PCM data from TTS chunks and serves it as a single WAV via a streaming HTTP
# endpoint.  The browser receives the URL early (after a buffer threshold) and
# starts playing while remaining chunks are still being generated.
_audio_streams: dict[str, dict] = {}
_audio_streams_lock = threading.Lock()

# Buffer threshold: start serving audio after this many seconds of audio are
# buffered.  Lower = faster start but risk of playback stalls.  Higher = more
# buffer before playback begins but smoother experience.
STREAM_BUFFER_SECONDS = 0.0  # 0 = start as soon as first chunk ready


def _extract_pcm_from_wav(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    """Parse WAV bytes â†' (raw_pcm, sample_rate, channels, bits_per_sample)."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        # Not a valid WAV â€” return as-is with default format
        return wav_bytes, 24000, 1, 16

    sample_rate, channels, bits_per_sample = 24000, 1, 16
    pos = 12
    pcm_data = b""

    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, pos + 4)[0]
        if chunk_id == b"fmt " and pos + 24 <= len(wav_bytes):
            channels = struct.unpack_from("<H", wav_bytes, pos + 10)[0]
            sample_rate = struct.unpack_from("<I", wav_bytes, pos + 12)[0]
            bits_per_sample = struct.unpack_from("<H", wav_bytes, pos + 22)[0]
        elif chunk_id == b"data":
            end = min(pos + 8 + chunk_size, len(wav_bytes))
            pcm_data = wav_bytes[pos + 8:end]
            break
        pos += 8 + chunk_size
        if chunk_size % 2:
            pos += 1  # WAV chunks are word-aligned

    return pcm_data, sample_rate, channels, bits_per_sample


def _build_wav_header(sample_rate: int, channels: int, bits_per_sample: int,
                      data_size: int = 0x7FFFFF00) -> bytes:
    """Build a 44-byte WAV header.  Default data_size is large for streaming."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)           # fmt chunk size
        + struct.pack("<H", 1)            # PCM format
        + struct.pack("<H", channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", bits_per_sample)
        + b"data"
        + struct.pack("<I", data_size)
    )


def _build_complete_wav(session: dict) -> bytes:
    """Concatenate all PCM chunks into a single valid WAV file."""
    pcm_parts = []
    for i in range(session["total_chunks"]):
        chunk = session["chunks"].get(i, b"")
        if chunk:
            pcm_parts.append(chunk)
    pcm_data = b"".join(pcm_parts)
    header = _build_wav_header(
        session["sample_rate"], session["channels"],
        session["bits_per_sample"], len(pcm_data),
    )
    return header + pcm_data


def _cleanup_stale_streams(max_age: float = 300.0):
    """Remove streaming sessions older than max_age seconds.

    IMPORTANT: Caller must already hold _audio_streams_lock.
    """
    now = time.time()
    stale = [k for k, v in _audio_streams.items()
             if now - v["created"] > max_age]
    for k in stale:
        del _audio_streams[k]


def _fallback_filename(text: str) -> str:
    """First 3 words, title-cased, hyphenated."""
    words = re.sub(r"[^a-zA-Z0-9\s]", "", text).split()[:3]
    if not words:
        return "generated"
    return "-".join(w.capitalize() for w in words)


def _unique_path(stem: str, ext: str) -> Path:
    """Return a path in OUTPUT_DIR that doesn't collide with existing files."""
    candidate = OUTPUT_DIR / f"{stem}.{ext}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = OUTPUT_DIR / f"{stem}-{n}.{ext}"
        if not candidate.exists():
            return candidate
        n += 1


def _ai_filename(text: str, timeout: float = 3.0) -> str | None:
    """Ask Ollama for a short filename summarising the text."""
    prompt = (
        f"Generate a short 2-3 word filename (no extension, use hyphens, no special chars) "
        f"summarizing this text: '{text[:200]}'. Reply with ONLY the filename, nothing else."
    )
    body = json.dumps({
        "model": _svc_ollama_model(),
        "prompt": prompt,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        _svc_ollama_generate(),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        name = data.get("response", "").strip().strip('"').strip("'")
        name = re.sub(r"[^a-zA-Z0-9\-]", "", name)
        if name and len(name) <= 60:
            return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Helpers â€” HA REST API
# ---------------------------------------------------------------------------

def _ha_get(endpoint: str, timeout: float = 5.0) -> dict | str | None:
    """GET a HA REST API endpoint. Returns parsed JSON or None on error."""
    url = f"{HA_URL}{endpoint}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except Exception:
        return None


def _ha_post(endpoint: str, payload: dict, timeout: float = 5.0) -> bool:
    """POST to a HA REST API service endpoint. Returns True on success."""
    url = f"{HA_URL}{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


def _build_multipart(audio_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    """Build a multipart/form-data body for STT proxy. Returns (body, content_type_header)."""
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
    # Determine file extension from content type
    ext_map = {"audio/webm": "webm", "audio/wav": "wav", "audio/ogg": "ogg",
               "audio/mpeg": "mp3", "audio/mp4": "m4a"}
    ext = ext_map.get(content_type, "webm")

    parts = []
    # File part
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.{ext}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    )
    # Model part
    model_part = (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"Systran/faster-whisper-small"
    )
    # Response format part
    fmt_part = (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        f"json"
    )
    closing = f"\r\n--{boundary}--\r\n"

    body = parts[0].encode() + audio_bytes + model_part.encode() + fmt_part.encode() + closing.encode()
    return body, f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Stage 3 Phase 5 â€” Service auto-discovery helpers
# ---------------------------------------------------------------------------
# Module-level so they can be unit tested without spinning up the
# Handler / HTTP stack. Each returns (status_code, payload_dict) the
# handler hands straight to _send_json.

_DISCOVERY_TIMEOUT_S = 4.0


def _http_get_json(url: str, timeout: float = _DISCOVERY_TIMEOUT_S) -> Any:
    """Minimal JSON-GET. Raises on any failure so callers can classify."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-supplied URL)
        data = resp.read()
    return json.loads(data.decode("utf-8", errors="replace"))


def _normalize_base_url(url: str) -> str:
    """Strip trailing slash; reject empty / non-http URLs early."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("url is required")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url must be http:// or https://")
    return url


def _ollama_chat_url(base_or_chat_url: str) -> str:
    """Given either a bare Ollama base (`http://host:port`) or the full
    chat endpoint (`http://host:port/api/chat`), return the `/api/chat`
    form the engine's GladosConfig.completion_url expects. Tolerant of
    trailing slashes and of operators who paste either variant into the
    LLM & Services URL field."""
    url = (base_or_chat_url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/api/chat"):
        return url
    # Strip any other trailing /api/... path the operator might have set
    # (e.g. /api/tags from testing), then append the canonical suffix.
    if "/api/" in url:
        url = url.rsplit("/api/", 1)[0]
    return url + "/api/chat"


def discover_ollama(url: str) -> tuple[int, dict]:
    """GET <url>/api/tags and return model list."""
    try:
        base = _normalize_base_url(url)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    try:
        payload = _http_get_json(f"{base}/api/tags")
    except urllib.error.URLError as exc:
        return 502, {"error": f"unreachable: {exc.reason}"}
    except json.JSONDecodeError:
        return 502, {"error": "invalid JSON response"}
    except Exception as exc:  # pragma: no cover - defensive
        return 502, {"error": str(exc)}

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return 502, {"error": "unexpected response shape"}

    models = []
    for m in raw_models:
        if isinstance(m, dict) and m.get("name"):
            models.append({
                "name": m.get("name"),
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
            })
    return 200, {"url": base, "models": models, "count": len(models)}


def discover_voices(url: str) -> tuple[int, dict]:
    """GET <url>/v1/voices and return voice list (OpenAI-compatible
    Speaches shape)."""
    try:
        base = _normalize_base_url(url)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    try:
        payload = _http_get_json(f"{base}/v1/voices")
    except urllib.error.URLError as exc:
        return 502, {"error": f"unreachable: {exc.reason}"}
    except json.JSONDecodeError:
        return 502, {"error": "invalid JSON response"}
    except Exception as exc:  # pragma: no cover - defensive
        return 502, {"error": str(exc)}

    # Accept three upstream shapes:
    #   • top-level list ([{...}, ...] or ["name", ...])
    #   • OpenAI-style { "data": [...] }
    #   • GLaDOS Piper / generic { "voices": [...] }
    # Any of them yields `raw` — a list of voice entries (dict or str).
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        raw = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("voices"), list):
        raw = payload["voices"]
    else:
        return 502, {"error": "unexpected response shape"}

    voices = []
    for v in raw:
        if isinstance(v, dict):
            name = v.get("voice_id") or v.get("id") or v.get("name")
            if name:
                voices.append({"name": name, "model": v.get("model_id") or v.get("model")})
        elif isinstance(v, str):
            voices.append({"name": v, "model": None})
    return 200, {"url": base, "voices": voices, "count": len(voices)}


def discover_health(url: str, path: str | None = None,
                    kind: str | None = None) -> tuple[int, dict]:
    """Reachability check against an upstream service.

    Services use different liveness endpoints:
      * Ollama exposes ``/api/tags`` — always 200 when up.
      * speaches (TTS + STT) exposes ``/v1/voices`` — always 200 when up.
        (It does NOT have ``/health``; probing that returns 404 and would
        make the dot red even on a healthy server.)
      * GLaDOS services (api_wrapper, vision) use ``/health``.
      * HA uses ``/api/`` with a bearer token — not probed here.

    The caller may pass an explicit ``path``; otherwise ``kind`` picks
    the right one, and if neither is supplied we fall back to
    ``/api/tags`` then ``/health`` then ``/``. Fallback keeps the probe
    useful for URLs the caller hasn't classified.
    """
    try:
        base = _normalize_base_url(url)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # Build an ordered list of probe paths.
    if path:
        probe_paths = [path]
    elif kind == "ollama":
        probe_paths = ["/api/tags"]
    elif kind == "tts":
        # TTS side of speaches exposes /v1/voices.
        probe_paths = ["/v1/voices"]
    elif kind == "stt":
        # STT side of speaches exposes /health (and /v1/models) but not
        # /v1/voices. /health first keeps this cheap.
        probe_paths = ["/health", "/v1/models"]
    elif kind == "speaches":
        # Either half of speaches — try both shapes.
        probe_paths = ["/v1/voices", "/health"]
    elif kind in ("api_wrapper", "vision"):
        probe_paths = ["/health"]
    else:
        # Unknown kind: try Ollama/speaches/generic in order. First 2xx wins.
        probe_paths = ["/api/tags", "/v1/voices", "/health", "/"]

    started = time.time()
    last_status: int | None = None
    last_reason: str | None = None
    for p in probe_paths:
        req = urllib.request.Request(f"{base}{p}")
        try:
            with urllib.request.urlopen(req, timeout=_DISCOVERY_TIMEOUT_S) as resp:  # noqa: S310
                status = resp.status
                if 200 <= status < 300:
                    return 200, {
                        "url": base, "ok": True, "status": status,
                        "path": p,
                        "latency_ms": int((time.time() - started) * 1000),
                    }
                last_status = status
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            # 4xx means the service is answering — try the next probe
            # path but remember this as the best failure signal.
            continue
        except urllib.error.URLError as exc:
            # Connection refused / DNS failure — no point trying more paths.
            last_reason = str(exc.reason)
            break

    payload = {
        "url": base, "ok": False, "status": last_status,
        "latency_ms": int((time.time() - started) * 1000),
    }
    if last_reason is not None:
        payload["reason"] = last_reason
    return 200, payload


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Quiet logging

    # â”€â”€ Auth helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _require_auth(self) -> bool:
        """Check auth; if not authenticated, redirect to login. Returns True if OK."""
        if _is_authenticated(self):
            return True
        # API calls get 401 JSON; browser requests get redirect
        if self.path.startswith("/api/"):
            self._send_json(401, {"error": "Authentication required"})
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        return False

    def _serve_login(self):
        """Serve the login page HTML. When no password is configured
        (fresh deploy or wiped hash), inject a setup instruction so
        the admin isn't stranded at a form that will never succeed."""
        html = LOGIN_PAGE
        if not _auth_password_configured():
            banner = (
                '<div style="background:#3a1a1a;border:1px solid #a33;'
                'color:#fcc;padding:12px;border-radius:6px;margin:0 0 '
                '16px 0;font-size:0.88rem;line-height:1.4;">'
                '<strong>Password not configured.</strong><br>'
                'Run this on the container host to set one '
                'before logging in:<br>'
                '<code style="display:block;background:#222;padding:'
                '8px;margin-top:6px;border-radius:4px;color:#fc6;">'
                'docker exec -it glados python -m glados.tools.set_password'
                '</code></div>'
            )
            # Inject just after the opening <form> so the banner
            # sits above the password input. LOGIN_PAGE contains
            # exactly one <form> tag.
            html = html.replace("<form", banner + "<form", 1)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self):
        """Process POST /login â€” validate password, set session cookie."""
        client_ip = self.client_address[0]

        # Rate limit check
        if _check_rate_limit(client_ip):
            self._send_json(429, {"ok": False, "error": "Too many attempts. Try again in 60 seconds."})
            return

        # Read form body
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        params = urllib.parse.parse_qs(body)
        password = params.get("password", [""])[0]
        remember = params.get("remember", ["0"])[0] == "1"

        # Validate
        if not _AUTH_PASSWORD_HASH or not password:
            _record_fail(client_ip)
            self._send_json(401, {"ok": False, "error": "Invalid password"})
            return

        try:
            valid = _bcrypt.checkpw(password.encode("utf-8"), _AUTH_PASSWORD_HASH.encode("ascii"))
        except Exception:
            valid = False

        if not valid:
            _record_fail(client_ip)
            self._send_json(401, {"ok": False, "error": "Invalid password"})
            return

        # Success — create session
        _clear_fails(client_ip)
        token = _create_session(remember=remember)
        # Operator-requested 2026-04-20: sessions never expire. Use
        # the long cookie Max-Age unconditionally so the browser
        # keeps the cookie across restarts; the signed token itself
        # carries exp=0 so the server never treats it as expired.
        max_age = _SESSION_LONG_S

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        cookie_parts = [
            f"glados_session={token}",
            f"Max-Age={max_age}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        # Add Secure flag when running HTTPS
        if SSL_CERT and SSL_CERT.exists():
            cookie_parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(cookie_parts))
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_logout(self):
        """Clear session cookie and redirect to login."""
        self.send_response(302)
        self.send_header("Location", "/login")
        cookie_parts = [
            "glados_session=",
            "Max-Age=0",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if SSL_CERT and SSL_CERT.exists():
            cookie_parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(cookie_parts))
        self.end_headers()

    # â”€â”€ Routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_auth_status(self):
        """Return authentication status for frontend gating."""
        self._send_json(200, {"authenticated": _is_authenticated(self)})

    def _dispatch_get(self):
        """Route GET requests to handlers (after auth check if needed)."""
        p = self.path
        if p == "/" or p == "/index.html":
            self._serve_ui()
        elif p.startswith("/static/"):
            self._serve_static()
        elif p == "/api/files":
            self._list_files()
        elif p.startswith("/files/"):
            self._serve_file()
        elif p.startswith("/chat_audio_stream/"):
            self._serve_streaming_audio()
        elif p.startswith("/chat_audio/"):
            self._serve_chat_audio()
        elif p == "/api/ssl/status":
            self._ssl_status()
        elif p == "/api/speakers":
            self._get_speakers()
        elif p == "/api/attitudes":
            self._get_attitudes()
        elif p == "/api/voices":
            self._get_voices()
        elif p == "/api/auth/status":
            self._get_auth_status()
        # --- Protected routes below ---
        elif p == "/api/modes":
            self._get_modes()
        elif p == "/api/status":
            self._get_status()
        elif p == "/api/weather":
            self._get_weather()
        elif p == "/api/gpu":
            self._get_gpu_status()
        elif p == "/api/entities/states":
            self._get_ha_entities()
        elif p == "/api/semantic/status":
            self._get_semantic_status()
        elif p == "/api/audio/stats":
            self._get_audio_stats()
        elif p == "/api/logs/sources":
            self._get_logs_sources()
        elif p.startswith("/api/logs/tail"):
            self._get_logs_tail()
        elif p.startswith("/api/logs"):
            self._get_logs()   # legacy host-native path
        elif p == "/api/audit/recent" or p.startswith("/api/audit/recent?"):
            self._get_audit_recent()
        elif p == "/api/memory/list" or p.startswith("/api/memory/list?"):
            self._get_memory_list()
        elif p == "/api/memory/pending" or p.startswith("/api/memory/pending?"):
            self._get_memory_pending()
        elif p.startswith("/api/discover/"):
            self._discover()
        elif p == "/api/config":
            self._get_config()
        elif p.startswith("/api/config/"):
            section = p.split("/api/config/", 1)[1]
            if section == "raw":
                self._get_config_raw()
            elif section == "disambiguation":
                self._get_disambiguation_rules()
            else:
                self._get_config_section(section)
        elif p == "/api/quips" or p.startswith("/api/quips?"):
            self._get_quips()
        elif p == "/api/chimes" or p.startswith("/api/chimes?"):
            self._get_chimes()
        elif p == "/api/canon" or p.startswith("/api/canon?"):
            self._get_canon()
        elif p == "/api/hub75/test/ping":
            self._hub75_test_ping()
        elif p == "/api/announcement-settings":
            self._get_announcement_settings()
        elif p == "/api/startup-speakers":
            self._get_startup_speakers()
            self._send_json(200, {"running": _eye_demo_running()})
        elif p == "/api/robots/status":
            self._robots_status()
        # --- Training monitor ---
        elif p.startswith("/api/training/"):
            sub = p.split("/api/training/", 1)[1].split("?")[0]
            if sub == "status":
                self._get_training_status()
            elif sub == "metrics":
                self._get_training_metrics()
            elif sub == "log":
                self._get_training_log()
            else:
                self._send_error(404, "Not found")
        else:
            self._send_error(404, "Not found")

    def do_HEAD(self):
        """Handle HEAD requests - needed for <audio> element probing."""
        self._head_only = True
        self.do_GET()

    def do_GET(self):
        # Public routes (no auth required)
        if self.path == "/login":
            if _is_authenticated(self):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self._serve_login()
            return
        if self.path == "/logout":
            self._handle_logout()
            return
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        # Security fix (2026-04-20) — the menu rebuild made `/` serve
        # the full single-page admin app including the Configuration
        # tabs. Previously `/` was treated as public because the chat
        # page lived there alone; now it fronts every protected page
        # in the SPA. Require auth for `/` and `/index.html` — the
        # login page redirects here after a successful POST so the
        # session cookie is already set for real admin users.
        if self.path in ("/", "/index.html"):
            if not self._require_auth():
                return
            self._dispatch_get()
            return

        # API endpoints explicitly allow-listed as public (chat
        # streaming, TTS generator, audio file fetches, health).
        # Everything else requires auth.
        if _is_public_route(self.path):
            self._dispatch_get()
            return

        if not self._require_auth():
            return
        self._dispatch_get()

    def do_POST(self):
        # Login is public
        if self.path == "/login":
            self._handle_login()
            return

        # TTS/Chat POST routes are public
        if _is_public_route(self.path):
            self._dispatch_post()
            return

        # Protected routes â€” require auth
        if not self._require_auth():
            return
        self._dispatch_post()

    def _dispatch_post(self):
        """Route POST requests to handlers."""
        p = self.path
        if p == "/api/generate":
            self._generate()
        elif p == "/api/chat":
            self._chat()
        elif p == "/api/chat/stream":
            self._chat_stream()
        elif p == "/api/precheck/test":
            self._post_precheck_test()
        elif p == "/api/semantic/test":
            self._post_semantic_test()
        elif p == "/api/semantic/rebuild":
            self._post_semantic_rebuild()
        elif p == "/api/quips/test":
            self._post_quips_test()
        elif p == "/api/canon/test":
            self._post_canon_test()
        elif p == "/api/stt":
            self._stt()
        # --- Protected routes below ---
        elif p == "/api/modes":
            self._set_modes()
        elif p == "/api/restart":
            self._restart_service()
        elif p == "/api/config/reload":
            self._reload_config()
        elif p == "/api/weather/refresh":
            self._refresh_weather()
        elif p == "/api/audio/clear":
            self._clear_audio_dir()
        elif p == "/api/logs/clear":
            self._clear_log()
        elif p.startswith("/api/memory/") and p.endswith("/promote"):
            self._memory_action("promote")
        elif p.startswith("/api/memory/") and p.endswith("/demote"):
            self._memory_action("demote")
        elif p.startswith("/api/memory/") and p.endswith("/edit"):
            self._memory_action("edit")
        elif p == "/api/memory/add":
            self._post_memory_add()
        elif p == "/api/retention/sweep":
            self._post_retention_sweep()
        elif p == "/api/hub75/test/cycle":
            self._hub75_test_cycle()
        elif p == "/api/hub75/test/blank":
            self._hub75_test_blank()
        elif p == "/api/announcement-settings":
            self._set_announcement_settings()
        elif p == "/api/startup-speakers":
            self._set_startup_speakers()
        elif p == "/api/eye-demo":
            self._handle_eye_demo()
        elif p == "/api/ssl/upload":
            self._ssl_upload()
        elif p == "/api/ssl/request":
            self._ssl_request_letsencrypt()
        elif p == "/api/robots/node/add":
            self._robots_add_node()
        elif p == "/api/robots/node/remove":
            self._robots_remove_node()
        elif p == "/api/robots/node/toggle":
            self._robots_toggle_node()
        elif p == "/api/robots/node/identify":
            self._robots_identify_node()
        elif p == "/api/robots/emergency-stop":
            self._robots_emergency_stop()
        # --- Training monitor ---
        elif p == "/api/training/snapshot":
            self._training_snapshot()
        elif p == "/api/training/stop":
            self._training_stop()
        else:
            self._send_error(404, "Not found")

    def do_PUT(self):
        if not self._require_auth():
            return
        if self.path.startswith("/api/config/"):
            section = self.path.split("/api/config/", 1)[1]
            if section == "raw":
                self._put_config_raw()
            elif section == "disambiguation":
                self._put_disambiguation_rules()
            else:
                self._put_config_section(section)
        elif self.path == "/api/quips":
            self._put_quips()
        elif self.path == "/api/chimes":
            self._put_chime()
        elif self.path == "/api/canon":
            self._put_canon()
        else:
            self._send_error(404, "Not found")

    def do_DELETE(self):
        if not self._require_auth():
            return
        if self.path.startswith("/api/files/"):
            self._delete_file()
        elif self.path.startswith("/api/memory/"):
            self._memory_action("delete")
        elif self.path == "/api/quips" or self.path.startswith("/api/quips?"):
            self._delete_quip()
        elif self.path == "/api/chimes" or self.path.startswith("/api/chimes?"):
            self._delete_chime()
        elif self.path == "/api/canon" or self.path.startswith("/api/canon?"):
            self._delete_canon()
        else:
            self._send_error(404, "Not found")

    # â”€â”€ TTS Generate (existing) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _generate(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        text = body.get("text", "").strip()
        fmt = body.get("format", "wav").lower()
        if not text:
            self._send_json(400, {"error": "Text is required"})
            return
        if fmt not in CONTENT_TYPES:
            self._send_json(400, {"error": f"Invalid format: {fmt}"})
            return

        # Build TTS request with voice and optional attitude TTS params
        voice = body.get("voice", "glados")
        tts_payload: dict = {"input": text, "voice": voice, "response_format": fmt}
        for param in ("length_scale", "noise_scale", "noise_w"):
            val = body.get(param)
            if val is not None:
                tts_payload[param] = float(val)
        tts_body = json.dumps(tts_payload).encode()
        tts_req = urllib.request.Request(_svc_tts_speech(), data=tts_body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(tts_req, timeout=60) as tts_resp:
                audio_data = tts_resp.read()
        except Exception as e:
            self._send_json(502, {"error": f"TTS service error: {e}"})
            return

        _cleanup_old_files()
        ai_name = _ai_filename(text)
        stem = ai_name or _fallback_filename(text)
        file_path = _unique_path(stem, fmt)
        file_path.write_bytes(audio_data)

        self._send_json(200, {
            "filename": file_path.name,
            "url": f"/files/{file_path.name}",
            "size": len(audio_data),
        })

    # â”€â”€ Chat with GLaDOS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _chat(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        message = body.get("message", "").strip()
        history = body.get("history", [])
        if not message:
            self._send_json(400, {"error": "Message is required"})
            return

        # Record the utterance entering the system; api_wrapper will
        # see X-GLaDOS-Origin and attribute tool calls downstream.
        _sess = _get_session_cookie(self)
        audit(AuditEvent(
            ts=time.time(),
            origin=Origin.WEBUI_CHAT,
            kind="utterance",
            utterance=message,
            principal=(_sess.get("sub") if _sess else None),
        ))

        # Build messages for GLaDOS API
        messages = list(history) + [{"role": "user", "content": message}]
        chat_payload = json.dumps({
            "messages": messages,
            "model": "glados",
            "stream": False,
        }).encode()

        # 1. Get GLaDOS response
        chat_req = urllib.request.Request(
            f"{_svc_api_wrapper()}/v1/chat/completions",
            data=chat_payload,
            headers={
                "Content-Type": "application/json",
                "X-GLaDOS-Origin": Origin.WEBUI_CHAT,
            },
        )
        try:
            with urllib.request.urlopen(chat_req, timeout=180) as resp:
                chat_data = json.loads(resp.read())
            glados_text = chat_data["choices"][0]["message"]["content"]
        except Exception as e:
            self._send_json(502, {"error": f"GLaDOS API error: {e}"})
            return

        # 2. Generate TTS audio
        audio_url = None
        try:
            tts_body = json.dumps({
                "input": glados_text,
                "voice": "glados",
                "response_format": "wav",
            }).encode()
            tts_req = urllib.request.Request(_svc_tts_speech(), data=tts_body,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(tts_req, timeout=60) as tts_resp:
                audio_data = tts_resp.read()

            _cleanup_chat_audio()
            filename = f"chat_{uuid.uuid4().hex[:12]}.wav"
            wav_path = CHAT_AUDIO_DIR / filename
            wav_path.write_bytes(audio_data)
            audio_url = f"/chat_audio/{filename}"
        except Exception:
            pass  # TTS failure is non-fatal â€” return text without audio

        self._send_json(200, {
            "text": glados_text,
            "audio_url": audio_url,
        })

    # â”€â”€ Streaming Chat (direct Ollama + chunked TTS) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _chat_stream(self):
        """Stream chat via API wrapper (port 8015) and generate TTS in chunks.

        Routes through the API wrapper to preserve command interception,
        conversation store, and TTS mute management.  Uses http.client for
        zero-buffering streaming.  Generates TTS audio for each sentence as
        it completes, overlapping TTS generation with LLM token generation.
        """
        import http.client as _http
        import time as _time
        import re as _re
        import threading as _threading

        t_request_start = _time.time()

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        message = body.get("message", "").strip()
        history = body.get("history", [])
        if not message:
            self._send_json(400, {"error": "Message is required"})
            return

        # Record the utterance entering the system; X-GLaDOS-Origin lets
        # api_wrapper attribute downstream tool calls to the same origin.
        _sess = _get_session_cookie(self)
        audit(AuditEvent(
            ts=time.time(),
            origin=Origin.WEBUI_CHAT,
            kind="utterance",
            utterance=message,
            principal=(_sess.get("sub") if _sess else None),
            extra={"streaming": True},
        ))

        # Build OpenAI-compatible request for the API wrapper
        messages = list(history) + [{"role": "user", "content": message}]
        api_body = json.dumps({
            "model": "glados",
            "messages": messages,
            "stream": True,
        }).encode("utf-8")

        # Connect to API wrapper via http.client (no buffering)
        conn = None
        try:
            conn = _http.HTTPConnection("localhost", 8015, timeout=180)
            conn.request(
                "POST", "/v1/chat/completions",
                body=api_body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(api_body)),
                    "X-GLaDOS-Origin": Origin.WEBUI_CHAT,
                },
            )
            api_resp = conn.getresponse()
            if api_resp.status != 200:
                err_body = api_resp.read().decode("utf-8", errors="replace")
                self._send_json(502, {"error": f"API wrapper {api_resp.status}: {err_body[:200]}"})
                conn.close()
                return
        except Exception as e:
            if conn:
                conn.close()
            self._send_json(502, {"error": f"API wrapper error: {e}"})
            return

        # Send SSE headers to the browser.
        # Use Connection: close so handle() won't loop for keep-alive.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True  # tell handler we're done after this

        def _sse_write(data: bytes):
            """Write SSE data and flush immediately."""
            self.wfile.write(data)
            self.wfile.flush()

        # â”€â”€ TTS streaming session â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _SENT_RE = _re.compile(r'[.!?](?:\s|$)')
        request_id = uuid.uuid4().hex[:8]

        # Create a streaming audio session that accumulates raw PCM
        # chunks and serves them as a single progressive WAV download.
        session = {
            "chunks": {},              # idx -> raw PCM bytes
            "total_chunks": None,      # set when all TTS threads spawned
            "sample_rate": 24000,      # overridden by first TTS response
            "channels": 1,
            "bits_per_sample": 16,
            "format_set": False,
            "buffered_seconds": 0.0,
            "cond": _threading.Condition(),
            "created": _time.time(),
        }
        with _audio_streams_lock:
            _cleanup_stale_streams()
            _audio_streams[request_id] = session

        def _generate_tts_chunk(text: str, idx: int, tts_params: dict | None = None):
            """Generate TTS for a sentence and store raw PCM in session."""
            try:
                tts_start_times[idx] = _time.time()
                tts_payload: dict = {
                    "input": text,
                    "voice": "glados",
                    "response_format": "wav",
                }
                if tts_params:
                    for param in ("length_scale", "noise_scale", "noise_w"):
                        if param in tts_params:
                            tts_payload[param] = tts_params[param]
                tts_body = json.dumps(tts_payload).encode()
                tts_req = urllib.request.Request(
                    _svc_tts_speech(), data=tts_body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(tts_req, timeout=120) as tts_resp:
                    wav_data = tts_resp.read()
                tts_end_times[idx] = _time.time()

                pcm, sr, ch, bps = _extract_pcm_from_wav(wav_data)

                with session["cond"]:
                    session["chunks"][idx] = pcm
                    if not session["format_set"]:
                        session["sample_rate"] = sr
                        session["channels"] = ch
                        session["bits_per_sample"] = bps
                        session["format_set"] = True
                    # Recalculate total buffered duration
                    total_pcm = sum(len(v) for v in session["chunks"].values())
                    bps_rate = sr * ch * (bps // 8)
                    session["buffered_seconds"] = (
                        total_pcm / bps_rate if bps_rate else 0
                    )
                    session["cond"].notify_all()
                print(f"[STREAM] TTS chunk {idx} ready: "
                      f"{len(pcm)} bytes, "
                      f"total buffered {session['buffered_seconds']:.1f}s",
                      flush=True)
            except Exception as e:
                print(f"[STREAM] TTS chunk {idx} error: {e}", flush=True)
                with session["cond"]:
                    session["chunks"][idx] = b""  # empty on error
                    session["cond"].notify_all()

        # â”€â”€ Main streaming loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            full_text_parts = []
            sentence_buf = ""
            tts_threads = []
            tts_chunk_idx = 0

            # Attitude TTS params captured from API wrapper's SSE event
            attitude_tts_params = {}
            pending_event_type = None
            llm_metrics = {}  # captured from API wrapper's metrics SSE event
            tts_start_times = {}  # idx -> start time
            tts_end_times = {}    # idx -> end time

            while True:
                raw_line = api_resp.readline()
                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    # Empty line = end of SSE event block
                    pending_event_type = None
                    continue

                # Handle named SSE events (e.g., "event: attitude")
                if line.startswith("event: "):
                    pending_event_type = line[7:].strip()
                    continue

                # Handle data lines
                if not line.startswith("data: "):
                    continue

                # Check for attitude event data
                if pending_event_type == "attitude":
                    try:
                        attitude_data = json.loads(line[6:])
                        attitude_tts_params = attitude_data.get("tts", {})
                        print(f"[STREAM] Attitude: {attitude_data.get('tag', 'unknown')}, "
                              f"TTS params: {attitude_tts_params}", flush=True)
                    except json.JSONDecodeError:
                        pass
                    pending_event_type = None
                    continue

                # Capture LLM metrics from API wrapper
                if pending_event_type == "metrics":
                    try:
                        llm_metrics = json.loads(line[6:])
                        print(f"[STREAM] LLM metrics: {llm_metrics}", flush=True)
                    except json.JSONDecodeError:
                        pass
                    pending_event_type = None
                    continue

                json_str = line[6:]
                if json_str.strip() == "[DONE]":
                    break

                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    continue

                delta = (parsed.get("choices") or [{}])[0].get("delta", {})
                content = delta.get("content", "")
                finish = (parsed.get("choices") or [{}])[0].get("finish_reason")

                if content:
                    full_text_parts.append(content)
                    sentence_buf += content

                    # Forward SSE chunk to browser unchanged
                    _sse_write(f"data: {json_str}\n\n".encode("utf-8"))

                    # Check for sentence boundary â€” kick off TTS
                    m = _SENT_RE.search(sentence_buf)
                    if m:
                        sentence_text = sentence_buf[:m.end()].strip()
                        sentence_buf = sentence_buf[m.end():]
                        if sentence_text:
                            t = _threading.Thread(
                                target=_generate_tts_chunk,
                                args=(sentence_text, tts_chunk_idx),
                                kwargs={"tts_params": attitude_tts_params},
                                daemon=True,
                            )
                            t.start()
                            tts_threads.append(t)
                            tts_chunk_idx += 1

                if finish == "stop":
                    break

            # Handle remaining text that didn't end with sentence punctuation
            remainder = sentence_buf.strip()
            if remainder:
                t = _threading.Thread(
                    target=_generate_tts_chunk,
                    args=(remainder, tts_chunk_idx),
                    kwargs={"tts_params": attitude_tts_params},
                    daemon=True,
                )
                t.start()
                tts_threads.append(t)
                tts_chunk_idx += 1

            # Mark total chunk count â€” streaming handler uses this to
            # know when all PCM data has been delivered.
            with session["cond"]:
                session["total_chunks"] = tts_chunk_idx
                session["cond"].notify_all()

            # Send DONE marker and full text
            _sse_write(b"data: [DONE]\n\n")
            full_text = "".join(full_text_parts)
            done_event = json.dumps({"full_text": full_text})
            _sse_write(f"event: done\ndata: {done_event}\n\n".encode("utf-8"))

            if tts_chunk_idx > 0:
                # Wait until chunk 0 (the first sentence) is ready.
                # The streaming handler serves chunks in order, so if
                # we send the URL before chunk 0 exists, the browser
                # would stall.  Also honour STREAM_BUFFER_SECONDS for
                # extra pre-buffering.
                deadline = _time.time() + 120
                with session["cond"]:
                    while (0 not in session["chunks"]
                           or session["buffered_seconds"] < STREAM_BUFFER_SECONDS):
                        remaining = deadline - _time.time()
                        if remaining <= 0:
                            break
                        session["cond"].wait(timeout=min(remaining, 1.0))

                # Send the streaming audio URL â€” browser starts playing
                # immediately while remaining TTS chunks keep generating.
                stream_url = f"/chat_audio_stream/{request_id}"
                stream_event = json.dumps({"audio_url": stream_url})
                _sse_write(
                    f"event: audio\ndata: {stream_event}\n\n".encode("utf-8")
                )
                print(f"[STREAM] Sent streaming URL after "
                      f"{session['buffered_seconds']:.1f}s buffered",
                      flush=True)

                # Wait for ALL TTS threads, then save a static WAV for
                # replay (the streaming session is ephemeral).
                for t in tts_threads:
                    t.join(timeout=120)

                _cleanup_chat_audio()
                combined_wav = _build_complete_wav(session)
                static_filename = f"chat_{request_id}.wav"
                static_path = CHAT_AUDIO_DIR / static_filename
                static_path.write_bytes(combined_wav)

                # Tell browser the static replay URL
                replay_url = f"/chat_audio/{static_filename}"
                replay_event = json.dumps({"audio_replay_url": replay_url})
                _sse_write(
                    f"event: replay\ndata: {replay_event}\n\n".encode("utf-8")
                )
                print(f"[STREAM] Static WAV saved: {static_filename} "
                      f"({len(combined_wav)} bytes)", flush=True)

            # Emit combined timing metrics
            t_total_end = _time.time()
            # TTS wall-clock: earliest start to latest end (parallel)
            tts_wall_ms = 0.0
            if tts_start_times and tts_end_times:
                tts_wall_ms = round(
                    (max(tts_end_times.values()) - min(tts_start_times.values())) * 1000, 1
                )
            timing_payload = {
                **llm_metrics,
                "tts_time_ms": tts_wall_ms,
                "tts_chunks": tts_chunk_idx,
                "total_time_ms": round((t_total_end - t_request_start) * 1000, 1),
            }
            # Inject current emotion state into timing payload
            try:
                import re as _re2
                _log = Path(str(Path(os.environ.get("GLADOS_LOGS", "/app/logs")) / "glados-api.log"))
                if _log.exists():
                    _lines = _log.read_text(encoding="utf-8", errors="replace").splitlines()
                    for _line in reversed(_lines):
                        if "Emotional State" not in _line:
                            continue
                        _nm = _re2.search(r"([A-Z][a-zA-Z ]+?) \(intensity:([\d.]+)\)", _line)
                        _pm = _re2.search(r"P:([+-]?[\d.]+)\s+A:([+-]?[\d.]+)\s+D:([+-]?[\d.]+)", _line)
                        _lm = _re2.search(r"\[locked ([\d.]+)h\]", _line)
                        if _nm and _pm:
                            timing_payload["emotion"]           = _nm.group(1).strip()
                            timing_payload["emotion_intensity"] = float(_nm.group(2))
                            timing_payload["pad_p"]             = float(_pm.group(1))
                            timing_payload["pad_a"]             = float(_pm.group(2))
                            timing_payload["pad_d"]             = float(_pm.group(3))
                            if _lm:
                                timing_payload["emotion_locked_h"] = float(_lm.group(1))
                            print(f"[STREAM] Emotion injected: {timing_payload['emotion']} "
                                  f"({timing_payload['emotion_intensity']:.2f})", flush=True)
                            break
                else:
                    print("[STREAM] Emotion log not found at {}/glados-api.log".format(os.environ.get("GLADOS_LOGS", "/app/logs")), flush=True)
            except Exception as _emo_err:
                print(f"[STREAM] Emotion injection failed: {_emo_err}", flush=True)
            _sse_write(
                f"event: timing\ndata: {json.dumps(timing_payload)}\n\n".encode("utf-8")
            )
            print(f"[STREAM] Timing: LLM gen={llm_metrics.get('generation_time_ms', '?')}ms, "
                  f"TTS={tts_wall_ms}ms, Total={timing_payload['total_time_ms']}ms", flush=True)

            # Schedule cleanup of the streaming session (keep 5 min for
            # any in-flight GET requests to the streaming endpoint).
            def _delayed_cleanup():
                _time.sleep(300)
                with _audio_streams_lock:
                    _audio_streams.pop(request_id, None)
            _threading.Thread(target=_delayed_cleanup, daemon=True).start()

        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        finally:
            conn.close()

    # â”€â”€ Speech-to-Text proxy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _stt(self):
        content_type = self.headers.get("Content-Type", "audio/webm")
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json(400, {"error": "No audio data"})
            return

        audio_bytes = self.rfile.read(length)
        multipart_body, multipart_ct = _build_multipart(audio_bytes, content_type)

        stt_req = urllib.request.Request(
            f"{_svc_stt()}/v1/audio/transcriptions",
            data=multipart_body,
            headers={"Content-Type": multipart_ct},
        )
        try:
            with urllib.request.urlopen(stt_req, timeout=30) as resp:
                stt_data = json.loads(resp.read())
            self._send_json(200, {"text": stt_data.get("text", "")})
        except Exception as e:
            self._send_json(502, {"error": f"STT service error: {e}"})

    # â”€â”€ Mode control â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_modes(self):
        result = {"maintenance_mode": False, "maintenance_speaker": "", "silent_mode": False}
        entities = {
            "input_boolean.glados_maintenance_mode": "maintenance_mode",
            "input_text.glados_maintenance_speaker": "maintenance_speaker",
            "input_boolean.glados_silent_mode": "silent_mode",
        }
        for entity_id, key in entities.items():
            data = _ha_get(f"/api/states/{entity_id}")
            if data and isinstance(data, dict):
                state = data.get("state", "")
                if key == "maintenance_speaker":
                    result[key] = state if state != "unknown" else ""
                else:
                    result[key] = state == "on"
        self._send_json(200, result)

    def _set_modes(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        action = body.get("action", "")
        speaker = body.get("speaker", "")

        if action == "maintenance_on":
            if speaker:
                _ha_post("/api/services/input_text/set_value", {
                    "entity_id": "input_text.glados_maintenance_speaker",
                    "value": speaker,
                })
            _ha_post("/api/services/input_boolean/turn_on", {
                "entity_id": "input_boolean.glados_maintenance_mode",
            })
            self._send_json(200, {"ok": True, "action": action})

        elif action == "maintenance_off":
            _ha_post("/api/services/input_boolean/turn_off", {
                "entity_id": "input_boolean.glados_maintenance_mode",
            })
            self._send_json(200, {"ok": True, "action": action})

        elif action == "silent_on":
            _ha_post("/api/services/input_boolean/turn_on", {
                "entity_id": "input_boolean.glados_silent_mode",
            })
            self._send_json(200, {"ok": True, "action": action})

        elif action == "silent_off":
            _ha_post("/api/services/input_boolean/turn_off", {
                "entity_id": "input_boolean.glados_silent_mode",
            })
            self._send_json(200, {"ok": True, "action": action})

        else:
            self._send_json(400, {"error": f"Unknown action: {action}"})

    # â”€â”€ Announcement verbosity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_announcement_settings(self):
        """Proxy GET to glados-api /api/announcement-settings."""
        try:
            req = urllib.request.Request(
                f"{_svc_api_wrapper()}/api/announcement-settings",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            self._send_json(200, data)
        except Exception as e:
            self._send_json(502, {"error": f"API error: {e}"})

    def _set_announcement_settings(self):
        """Proxy POST to glados-api /api/announcement-settings."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
        except Exception:
            self._send_json(400, {"error": "Invalid request"})
            return

        try:
            req = urllib.request.Request(
                f"{_svc_api_wrapper()}/api/announcement-settings",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            self._send_json(200, data)
        except Exception as e:
            self._send_json(502, {"error": f"API error: {e}"})

    def _get_startup_speakers(self):
        """Proxy GET to glados-api /api/startup-speakers."""
        try:
            req = urllib.request.Request(
                f"{_svc_api_wrapper()}/api/startup-speakers",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            self._send_json(200, data)
        except Exception as e:
            self._send_json(502, {"error": f"API error: {e}"})

    def _set_startup_speakers(self):
        """Proxy POST to glados-api /api/startup-speakers."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
        except Exception:
            self._send_json(400, {"error": "Invalid request"})
            return
        try:
            req = urllib.request.Request(
                f"{_svc_api_wrapper()}/api/startup-speakers",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            self._send_json(200, data)
        except Exception as e:
            self._send_json(502, {"error": f"API error: {e}"})

    # â”€â”€ Speaker discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_speakers(self):
        template = (
            "{% for area in areas() %}"
            "{{ area }}|{{ area_name(area) }}|"
            "{{ area_entities(area) | select('match', 'media_player\\\\.') | list | join(',') }}\n"
            "{% endfor %}"
        )
        payload = json.dumps({"template": template}).encode()
        req = urllib.request.Request(
            f"{HA_URL}/api/template",
            data=payload,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        speakers = []
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode()
            for line in text.strip().split("\n"):
                parts = line.strip().split("|")
                if len(parts) < 3 or not parts[2]:
                    continue
                area_id, area_name = parts[0], parts[1]
                for entity_id in parts[2].split(","):
                    entity_id = entity_id.strip()
                    if entity_id and entity_id not in SPEAKER_BLACKLIST:
                        # Build friendly name from entity_id
                        friendly = entity_id.replace("media_player.", "").replace("_", " ").title()
                        speakers.append({
                            "entity_id": entity_id,
                            "name": friendly,
                            "area": area_name,
                        })
        except Exception as e:
            self._send_json(500, {"error": f"Speaker discovery failed: {e}"})
            return

        self._send_json(200, {"speakers": speakers})

    # â”€â”€ Service health â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_status(self):
        """Aggregate health for the System page dots + sidebar engine dot.

        Returns per-service booleans plus a top-level `running` flag the
        sidebar's pollEngineStatus() checks. "Engine running" is true
        when the GLaDOS API container (which hosts this WebUI) reports
        healthy — that's the canonical "GLaDOS is up" signal.

        Per-service check paths (all use real service URLs, no
        host-local assumptions):
          glados_api — GET <_svc_api_wrapper()>/health
          stt        — GET <_svc_stt()>/health   (speaches STT)
          vision     — GET <_svc_vision()>/health
          ha         — GET <HA_URL>/api/ (with bearer token)
          tts        — GET <speaches base>/v1/voices
                       (speaches has no /health; the voices list is the
                       authoritative "server is up" signal we use
                       elsewhere for Discover)
          chromadb   — GET http://<chromadb_host>:<chromadb_port>/api/v2/heartbeat
                       (ChromaDB 1.x retired v1; v2 heartbeat is the
                       stable long-term endpoint)
        """
        status = {}
        checks = {
            "glados_api": f"{_svc_api_wrapper()}/health",
            "stt": f"{_svc_stt()}/health",
            "vision": f"{_svc_vision()}/health",
            "ha": f"{HA_URL}/api/",
        }
        for name, url in checks.items():
            try:
                headers = {}
                if name == "ha":
                    headers["Authorization"] = f"Bearer {HA_TOKEN}"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    status[name] = resp.status < 400
            except Exception:
                status[name] = False

        # TTS: speaches has no /health; /v1/voices returns 200 whenever
        # the server is up and is the same signal the Services-page
        # Discover button relies on. The base URL is derived the same
        # way _get_voices() does it — strip any trailing /v1/... segment.
        try:
            tts_base = _svc_tts_base().rsplit("/v1/", 1)[0].rstrip("/")
            req = urllib.request.Request(f"{tts_base}/v1/voices")
            with urllib.request.urlopen(req, timeout=3) as resp:
                status["tts"] = resp.status < 400
        except Exception:
            status["tts"] = False

        # ChromaDB: use the configured host:port (glados-chromadb:8000
        # when in compose, overrideable via CHROMADB_HOST / CHROMADB_PORT).
        # v2 heartbeat only — v1 was retired in ChromaDB 1.x and returns 410.
        try:
            ch_host = _cfg.memory.chromadb_host
            ch_port = _cfg.memory.chromadb_port
            req = urllib.request.Request(
                f"http://{ch_host}:{ch_port}/api/v2/heartbeat"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                status["chromadb"] = resp.status < 400
        except Exception:
            status["chromadb"] = False

        # Sidebar engine dot polls /api/status and reads data.running.
        # Treat the GLaDOS API being up as "engine running" — the WebUI
        # itself is hosted in the same container, so if this handler
        # responded AND the API reports healthy, the engine is alive.
        status["running"] = bool(status.get("glados_api"))

        self._send_json(200, status)

    # â”€â”€ Attitudes endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_attitudes(self):
        """Proxy attitude list from the API wrapper."""
        try:
            req = urllib.request.Request(f"{_svc_api_wrapper()}/api/attitudes")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            self._send_json(200, data)
        except Exception as e:
            # Fallback: return empty list if API wrapper is down
            self._send_json(200, {"attitudes": []})

    # â”€â”€ Voices endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_voices(self):
        """Proxy available voices list from the TTS service."""
        try:
            req = urllib.request.Request(f"{_svc_tts_base().rsplit('/v1/', 1)[0]}/v1/voices")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            self._send_json(200, data)
        except Exception:
            # Fallback: just GLaDOS if TTS service is down
            self._send_json(200, {"voices": ["glados"]})

    # â”€â”€ Monitoring endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_weather(self):
        """Return cached weather data from the on-disk weather cache file."""
        cache_path = Path("data/weather_cache.json")
        try:
            if not cache_path.exists():
                self._send_json(200, {"error": "No weather data cached yet"})
                return
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            # Add cache age
            mtime = cache_path.stat().st_mtime
            data["_cache_age_s"] = round(time.time() - mtime, 0)
            self._send_json(200, data)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _get_gpu_status(self):
        """Query nvidia-smi + Intel XPU for GPU utilization data."""
        import subprocess
        gpus = []
        # NVIDIA GPUs via nvidia-smi
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                 "--format=csv,nounits,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 7:
                    gpus.append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "memory_total_mb": int(parts[2]),
                        "memory_used_mb": int(parts[3]),
                        "memory_free_mb": int(parts[4]),
                        "utilization_pct": int(parts[5]) if parts[5] != "[N/A]" else None,
                        "temperature_c": int(parts[6]) if parts[6] != "[N/A]" else None,
                    })
        except Exception:
            pass  # nvidia-smi not available
        # Intel XPU via torch.xpu
        try:
            import torch
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                for i in range(torch.xpu.device_count()):
                    props = torch.xpu.get_device_properties(i)
                    total_mb = props.total_memory // (1024 * 1024)
                    alloc_mb = torch.xpu.memory_allocated(i) // (1024 * 1024)
                    gpus.append({
                        "index": f"xpu:{i}",
                        "name": props.name,
                        "memory_total_mb": total_mb,
                        "memory_used_mb": alloc_mb,
                        "memory_free_mb": total_mb - alloc_mb,
                        "utilization_pct": None,
                        "temperature_c": None,
                        "note": "VRAM shows PyTorch allocations only",
                    })
        except Exception:
            pass  # torch.xpu not available
        self._send_json(200, {"gpus": gpus})

    # â”€â”€ Weather refresh â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _refresh_weather(self):
        """Force-refresh weather data from Open-Meteo API."""
        try:
            wcfg = _cfg.weather
            lat, lon = wcfg.latitude, wcfg.longitude

            # Try to get lat/lon from HA zone.home if auto_from_ha is enabled
            if wcfg.auto_from_ha:
                try:
                    req = urllib.request.Request(
                        f"{HA_URL}/api/states/zone.home",
                        headers={"Authorization": f"Bearer {_cfg.ha_token}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        zone = json.loads(resp.read())
                    lat = zone.get("attributes", {}).get("latitude", lat)
                    lon = zone.get("attributes", {}).get("longitude", lon)
                except Exception:
                    pass  # Fall back to config values

            temp_unit = wcfg.temperature_unit
            wind_unit = wcfg.wind_speed_unit
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}&"
                f"current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m&"
                f"daily=temperature_2m_max,temperature_2m_min,weather_code&"
                f"hourly=temperature_2m,weather_code&"
                f"temperature_unit={temp_unit}&wind_speed_unit={wind_unit}&"
                f"forecast_days=7&timezone=auto"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "GLaDOS/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_data = json.loads(resp.read())

            # Write to cache file (same location as WeatherSubagent)
            from glados.core.weather_cache import update, get_data, configure as wc_configure
            cache_path = Path(_cfg.glados_root) / "data" / "weather_cache.json"
            wc_configure(cache_path)
            update(raw_data, wcfg)
            data = get_data() or {}
            data["_cache_age_s"] = 0
            self._send_json(200, data)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # â”€â”€ Audio stats / clear â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_audio_stats(self):
        """Return file counts and sizes for audio directories."""
        dirs = {
            "ha_output": Path(_cfg.audio.ha_output_dir),
            "archive": Path(_cfg.audio.archive_dir),
            "tts_ui": OUTPUT_DIR,
            "chat_audio": CHAT_AUDIO_DIR,
        }
        stats = {}
        for key, path in dirs.items():
            if path.is_dir():
                files = [f for f in path.iterdir()
                         if f.is_file() and f.suffix.lower() in ('.wav', '.mp3', '.ogg')]
                total_size = sum(f.stat().st_size for f in files)
                stats[key] = {"count": len(files), "size_bytes": total_size}
            else:
                stats[key] = {"count": 0, "size_bytes": 0}
        self._send_json(200, stats)

    def _clear_audio_dir(self):
        """Clear audio files from a specified directory."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        dir_key = body.get("directory", "")
        dir_map = {
            "ha_output": Path(_cfg.audio.ha_output_dir),
            "archive": Path(_cfg.audio.archive_dir),
            "tts_ui": OUTPUT_DIR,
            "chat_audio": CHAT_AUDIO_DIR,
        }
        target = dir_map.get(dir_key)
        if not target or not target.is_dir():
            self._send_json(400, {"error": f"Invalid directory: {dir_key}"})
            return
        count = 0
        for f in target.iterdir():
            if f.is_file() and f.suffix.lower() in ('.wav', '.mp3', '.ogg'):
                f.unlink()
                count += 1
        self._send_json(200, {"deleted": count})

    # â”€â”€ Logs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    _LOG_WHITELIST = frozenset({
        "glados-api", "glados-tts", "glados-stt", "glados-tts-ui",
        "glados-vision", "ollama-glados", "ollama-ipex", "ollama-vision",
    })

    def _get_logs(self):
        """Return the last N lines of a service log file."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        service = params.get("service", ["glados-api"])[0]
        lines = min(int(params.get("lines", ["500"])[0]), 2000)

        if service not in self._LOG_WHITELIST:
            self._send_json(400, {"error": "Invalid service name"})
            return

        log_path = Path(os.environ.get("GLADOS_LOGS", "/app/logs")) / f"{service}.log"
        if not log_path.exists():
            self._send_json(200, {"lines": [], "service": service, "total_size": 0})
            return

        # Read last N lines efficiently (read up to 512KB from end)
        try:
            size = log_path.stat().st_size
            read_size = min(size, 512 * 1024)
            with open(log_path, "rb") as f:
                f.seek(max(0, size - read_size))
                content = f.read().decode("utf-8", errors="replace")
            result_lines = content.split("\n")[-lines:]
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return

        self._send_json(200, {"lines": result_lines, "service": service, "total_size": size})

    def _get_audit_recent(self):
        """Return the last N rows of the audit log as parsed JSON objects.

        Query params:
          limit   — max rows to return (default 200, cap 2000)
          origin  — optional filter, e.g. origin=webui_chat
          kind    — optional filter, e.g. kind=tool_call
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            limit = min(int(params.get("limit", ["200"])[0]), 2000)
        except ValueError:
            limit = 200
        origin_filter = params.get("origin", [None])[0]
        kind_filter = params.get("kind", [None])[0]

        audit_path = Path(_cfg.audit.path)
        if not audit_path.exists():
            self._send_json(200, {"rows": [], "path": str(audit_path)})
            return

        # Tail-read: read last ~512 KB (more than enough for 2000 short rows)
        # and parse JSON lines. Malformed lines are skipped silently.
        try:
            size = audit_path.stat().st_size
            read_size = min(size, 512 * 1024)
            with open(audit_path, "rb") as f:
                f.seek(max(0, size - read_size))
                content = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            self._send_json(500, {"error": str(e)})
            return

        rows: list[dict] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Filter then take the tail.
        if origin_filter:
            rows = [r for r in rows if r.get("origin") == origin_filter]
        if kind_filter:
            rows = [r for r in rows if r.get("kind") == kind_filter]
        rows = rows[-limit:]

        self._send_json(200, {
            "rows": rows,
            "count": len(rows),
            "path": str(audit_path),
        })

    # ── Stage 3 Phase D: Memory review queue endpoints ─────────────

    def _memory_store(self):
        """Look up the live MemoryStore from the engine. Returns None
        if unavailable (engine not yet up, or ChromaDB down)."""
        try:
            import glados.core.api_wrapper as _aw
            engine = getattr(_aw, "_engine", None)
            return getattr(engine, "memory_store", None) if engine else None
        except Exception:
            return None

    def _get_memory_list(self):
        """List approved facts from the semantic collection.

        Query params:
          limit  — max rows (default 100, cap 500)
          q      — optional similarity-search query; if absent, returns
                   the most recent N facts by storage order.
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            limit = min(int(params.get("limit", ["100"])[0]), 500)
        except ValueError:
            limit = 100
        q = params.get("q", [""])[0].strip()

        store = self._memory_store()
        if store is None:
            self._send_json(200, {"rows": [], "warning": "memory store unavailable"})
            return

        try:
            if q:
                rows = store.query(text=q, collection="semantic", n=limit)
            else:
                rows = store.list_by_status("approved", "semantic", limit=limit)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return

        # Don't leak ChromaDB internals; return shape the UI expects.
        self._send_json(200, {
            "rows": [
                {
                    "id": r.get("id"),
                    "document": r.get("document", ""),
                    "metadata": r.get("metadata", {}),
                    "distance": r.get("distance"),
                }
                for r in rows
            ],
            "count": len(rows),
        })

    def _get_memory_pending(self):
        """List facts queued for operator review (review_status='pending')."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        try:
            limit = min(int(parse_qs(parsed.query).get("limit", ["100"])[0]), 500)
        except ValueError:
            limit = 100
        store = self._memory_store()
        if store is None:
            self._send_json(200, {"rows": [], "warning": "memory store unavailable"})
            return
        try:
            rows = store.list_by_status("pending", "semantic", limit=limit)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {
            "rows": [
                {
                    "id": r.get("id"),
                    "document": r.get("document", ""),
                    "metadata": r.get("metadata", {}),
                }
                for r in rows
            ],
            "count": len(rows),
        })

    def _memory_action(self, action: str):
        """Handle promote / demote / edit / delete of a memory entry.

        URL shape: /api/memory/<entry_id>/<action> for promote/demote/edit;
        DELETE /api/memory/<entry_id> for delete.
        """
        # Extract entry_id from the path.
        path = self.path.rstrip("/")
        if action == "delete":
            entry_id = path[len("/api/memory/"):]
        else:
            # /api/memory/<id>/<action>
            entry_id = path[len("/api/memory/"):-len("/" + action)]
        if not entry_id or "/" in entry_id:
            self._send_json(400, {"error": "invalid entry id"})
            return

        store = self._memory_store()
        if store is None:
            self._send_json(503, {"error": "memory store unavailable"})
            return

        if action == "delete":
            n = store.delete_ids("semantic", [entry_id])
            self._send_json(200, {"deleted": n})
            return

        if action == "promote":
            ok = store.update(
                entry_id, "semantic",
                metadata_updates={"review_status": "approved"},
            )
            self._send_json(200 if ok else 404, {"updated": ok})
            return

        if action == "demote":
            ok = store.update(
                entry_id, "semantic",
                metadata_updates={"review_status": "rejected"},
            )
            self._send_json(200 if ok else 404, {"updated": ok})
            return

        if action == "edit":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
            except Exception:
                self._send_json(400, {"error": "invalid JSON"})
                return
            new_doc = body.get("document")
            new_status = body.get("review_status")
            new_importance = body.get("importance")
            updates: dict = {}
            if new_status:
                updates["review_status"] = new_status
            if new_importance is not None:
                try:
                    updates["importance"] = float(new_importance)
                except (TypeError, ValueError):
                    pass
            ok = store.update(
                entry_id, "semantic",
                document=new_doc,
                metadata_updates=updates if updates else None,
            )
            self._send_json(200 if ok else 404, {"updated": ok})
            return

        self._send_json(400, {"error": f"unknown action {action!r}"})

    def _post_memory_add(self):
        """Operator-initiated long-term fact. Writes as source='explicit'
        so it lands approved and RAG-eligible immediately. Body:
          {"document": "The operator prefers dark roast", "importance": 0.9}
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "invalid JSON"})
            return
        doc = (body.get("document") or "").strip()
        if not doc:
            self._send_json(400, {"error": "document is required"})
            return
        try:
            importance = float(body.get("importance", 0.9))
        except (TypeError, ValueError):
            importance = 0.9
        importance = max(0.0, min(importance, 1.0))

        store = self._memory_store()
        if store is None:
            self._send_json(503, {"error": "memory store unavailable"})
            return

        from glados.core.memory_writer import write_fact
        ok = write_fact(store, doc, source="explicit", importance=importance,
                        review_status="approved")
        self._send_json(200 if ok else 500,
                        {"added": ok, "document": doc, "importance": importance})

    # ── Stage 3 Phase 5: Service discovery + manual retention sweep ─

    def _discover(self):
        """Fan out to the discovery helpers at module scope.

        Accepts GET /api/discover/{ollama,voices,health}?url=<base>.
        Query-param-only (never body) — this is a GET.
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        kind = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        url = (params.get("url", [""])[0] or "").strip()
        if not url:
            self._send_json(400, {"error": "url query param is required"})
            return

        if kind == "ollama":
            status, payload = discover_ollama(url)
        elif kind == "voices":
            status, payload = discover_voices(url)
        elif kind == "health":
            # Optional kind hint so discover_health probes the right
            # endpoint per service type (Ollama /api/tags, speaches
            # /v1/voices, etc.). Falls back to a multi-path probe.
            svc_kind = (params.get("kind", [""])[0] or "").strip() or None
            path = (params.get("path", [""])[0] or "").strip() or None
            status, payload = discover_health(url, path=path, kind=svc_kind)
        else:
            self._send_json(404, {"error": f"unknown discovery kind {kind!r}"})
            return
        self._send_json(status, payload)

    def _post_retention_sweep(self):
        """Manually trigger RetentionAgent.sweep_once(). Useful for
        operators verifying retention config without waiting for the
        hourly tick. Returns the counts dict from the sweeper."""
        try:
            import glados.core.api_wrapper as _aw
            engine = getattr(_aw, "_engine", None)
            agent = getattr(engine, "_retention_agent", None) if engine else None
        except Exception:
            agent = None

        if agent is None:
            self._send_json(503, {"error": "retention agent unavailable"})
            return

        try:
            result = agent.sweep_once()
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        # sweep_once returns a dict[str, int] from the current impl.
        safe = result if isinstance(result, dict) else {}
        self._send_json(200, {"ok": True, "counts": safe})

    def _clear_log(self):
        """Truncate a service log file."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        service = body.get("service", "")
        if service not in self._LOG_WHITELIST:
            self._send_json(400, {"error": "Invalid service name"})
            return
        log_path = Path(os.environ.get("GLADOS_LOGS", "/app/logs")) / f"{service}.log"
        if log_path.exists():
            with open(log_path, "w") as f:
                f.write("")
        self._send_json(200, {"ok": True})

    def _get_ha_entities(self):
        """Return current state of monitored HA entities."""
        try:
            # Get monitored entity IDs from config
            monitored = []
            # Mode entities
            me = _cfg.mode_entities
            monitored.extend([me.maintenance_mode, me.maintenance_speaker, me.silent_mode])
            # Fetch states from HA
            states = {}
            for eid in monitored:
                try:
                    url = f"{HA_URL}/api/states/{eid}"
                    req = urllib.request.Request(url, headers={
                        "Authorization": f"Bearer {HA_TOKEN}",
                        "Content-Type": "application/json",
                    })
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read())
                        states[eid] = {
                            "state": data.get("state", "unknown"),
                            "friendly_name": data.get("attributes", {}).get("friendly_name", eid),
                            "last_changed": data.get("last_changed", ""),
                        }
                except Exception:
                    states[eid] = {"state": "unavailable", "friendly_name": eid, "last_changed": ""}
            self._send_json(200, {"entities": states})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # â”€â”€ Service restart â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _restart_service(self):
        import subprocess
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        svc_key = body.get("service", "")

        # ChromaDB runs as Docker container â€” different restart path
        if svc_key == "chromadb":
            try:
                result = subprocess.run(
                    ["docker", "restart", "glados-chromadb"],
                    capture_output=True, text=True, timeout=30,
                )
                ok = result.returncode == 0
                self._send_json(200, {
                    "ok": ok,
                    "service": "chromadb",
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                })
            except subprocess.TimeoutExpired:
                self._send_json(504, {"error": "ChromaDB restart timed out"})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        container_name = SERVICE_MAP.get(svc_key)
        if not container_name:
            self._send_json(400, {"error": f"Unknown service: {svc_key}"})
            return
        try:
            result = subprocess.run(
                ["docker", "restart", container_name],
                capture_output=True, text=True, timeout=30,
            )
            ok = result.returncode == 0
            self._send_json(200, {
                "ok": ok,
                "service": svc_key,
                "container": container_name,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": f"Restart timed out for {container_name}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # â”€â”€ Logs (Phase 6 follow-up) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #
    # Configuration > Logs reads from a small whitelisted set of sources:
    #   • container stdout (via `docker logs glados`)
    #   • ChromaDB stdout (via `docker logs glados-chromadb`)
    #   • audit.jsonl file under GLADOS_LOGS
    #
    # Endpoints:
    #   GET /api/logs/sources  â†' [{key,label,desc,type}, ...]
    #   GET /api/logs/tail?source=<key>&lines=<n>  â†' {lines:[...]}

    _LOG_SOURCES_DOCKER = {
        "container": "glados",
        "chromadb":  "glados-chromadb",
    }
    _LOG_SOURCES_FILE = {
        "audit": "audit.jsonl",
    }

    def _get_logs_sources(self):
        self._send_json(200, {"sources": [
            {
                "key": "container", "type": "docker",
                "label": "GLaDOS (container stdout)",
                "desc": "Live Python app output — tier decisions, HA connectivity, errors",
            },
            {
                "key": "audit", "type": "file",
                "label": "Audit Trail",
                "desc": "One JSON line per utterance / tool call — origin, tier, latency",
            },
            {
                "key": "chromadb", "type": "docker",
                "label": "ChromaDB",
                "desc": "Memory store — useful if semantic search or retention looks off",
            },
        ]})

    def _get_logs_tail(self):
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(self.path).query)
        source = params.get("source", ["container"])[0]
        try:
            lines = max(1, min(int(params.get("lines", ["500"])[0]), 5000))
        except ValueError:
            lines = 500

        if source in self._LOG_SOURCES_DOCKER:
            container = self._LOG_SOURCES_DOCKER[source]
            try:
                raw = _docker_logs_tail(container, tail=lines, timestamps=True)
            except FileNotFoundError:
                self._send_json(500, {
                    "error": "Docker socket not mounted at /var/run/docker.sock; "
                             "container log sources require the socket bind.",
                })
                return
            except Exception as exc:
                self._send_json(502, {"error": f"docker logs failed: {exc}"})
                return
            log_lines = raw.splitlines()[-lines:]
            self._send_json(200, {"source": source, "lines": log_lines, "count": len(log_lines)})
            return

        if source in self._LOG_SOURCES_FILE:
            fname = self._LOG_SOURCES_FILE[source]
            log_path = Path(os.environ.get("GLADOS_LOGS", "/app/logs")) / fname
            if not log_path.exists():
                self._send_json(200, {"source": source, "lines": [], "count": 0,
                                      "note": f"{fname} does not exist yet"})
                return
            try:
                size = log_path.stat().st_size
                read_size = min(size, 1 * 1024 * 1024)   # cap at 1 MB
                with open(log_path, "rb") as f:
                    f.seek(max(0, size - read_size))
                    content = f.read().decode("utf-8", errors="replace")
                log_lines = [line for line in content.splitlines() if line.strip()][-lines:]
                self._send_json(200, {"source": source, "lines": log_lines, "count": len(log_lines)})
            except OSError as exc:
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(400, {"error": f"Unknown log source: {source!r}"})

    # â”€â”€ File serving â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _serve_ui(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())

    _STATIC_MIMES = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".map": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }

    def _serve_static(self):
        """Serve static assets from glados/webui/static/. Public route.

        Phase 1 of the WebUI refactor extracts CSS (and later JS) out of
        the monolithic tts_ui.py into files under this directory so
        they can be edited, diffed, and cached independently.
        """
        rel = self.path[len("/static/"):]
        # Drop query string (e.g. cachebust ?v=sha)
        if "?" in rel:
            rel = rel.split("?", 1)[0]
        static_root = (Path(__file__).parent / "static").resolve()
        try:
            target = (static_root / rel).resolve()
            target.relative_to(static_root)
        except (ValueError, OSError):
            self._send_error(403, "Forbidden")
            return
        if not target.is_file():
            self._send_error(404, "Not found")
            return
        mime = self._STATIC_MIMES.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def _list_files(self):
        files = []
        for f in sorted(OUTPUT_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
            if f.is_file():
                st = f.stat()
                files.append({
                    "name": f.name,
                    "size": st.st_size,
                    "date": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "url": f"/files/{f.name}",
                })
        self._send_json(200, {"files": files})

    def _serve_file(self):
        name = self.path[len("/files/"):]
        name = urllib.request.url2pathname(name)
        file_path = OUTPUT_DIR / name
        if not file_path.is_file() or not file_path.is_relative_to(OUTPUT_DIR):
            self._send_error(404, "File not found")
            return
        self._serve_binary(file_path)

    def _serve_chat_audio(self):
        name = self.path[len("/chat_audio/"):]
        name = urllib.request.url2pathname(name)
        file_path = CHAT_AUDIO_DIR / name
        if not file_path.is_file() or not file_path.is_relative_to(CHAT_AUDIO_DIR):
            self._send_error(404, "File not found")
            return
        self._serve_binary(file_path)

    def _serve_streaming_audio(self):
        """Serve a single WAV by streaming PCM chunks as they complete.

        The TTS backend generates audio for each sentence in parallel.  This
        handler sends a WAV header immediately, then writes raw PCM data for
        each chunk *in order* as soon as it becomes available.  The browser
        starts playing as soon as its internal buffer is satisfied â€” typically
        within the first chunk â€” while remaining chunks continue generating.
        """
        request_id = self.path.rsplit("/", 1)[-1]

        with _audio_streams_lock:
            session = _audio_streams.get(request_id)
        if not session:
            self._send_error(404, "Stream not found")
            return

        # Wait for the first chunk so we know audio format
        with session["cond"]:
            while not session["format_set"]:
                if not session["cond"].wait(timeout=120):
                    self._send_error(504, "Timed out waiting for TTS")
                    return

        # Send HTTP response header â€” no Content-Length so browser reads
        # until connection close (progressive download).
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Accept-Ranges", "none")
        self.end_headers()

        # Write WAV header with oversized data-length placeholder
        wav_hdr = _build_wav_header(
            session["sample_rate"], session["channels"],
            session["bits_per_sample"],
        )
        try:
            self.wfile.write(wav_hdr)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        # Stream PCM chunks in order
        next_idx = 0
        while True:
            with session["cond"]:
                # Wait until our chunk is ready or all chunks are accounted for
                while next_idx not in session["chunks"]:
                    total = session["total_chunks"]
                    if total is not None and next_idx >= total:
                        return  # all chunks served
                    if not session["cond"].wait(timeout=60):
                        return  # timed out
                pcm = session["chunks"][next_idx]

            if pcm:
                try:
                    self.wfile.write(pcm)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            next_idx += 1

    # ----- SSL Certificate Management -----

    def _ssl_status(self):
        """Return certificate metadata and file existence status."""
        import datetime as _dt
        info = {
            "ssl_active": False,
            "source": "none",
            "cert_path": str(SSL_CERT),
            "key_path": str(SSL_KEY),
            "cert_exists": SSL_CERT.exists() if SSL_CERT else False,
            "key_exists": SSL_KEY.exists() if SSL_KEY else False,
            "subject": "",
            "issuer": "",
            "sans": [],
            "not_before": "",
            "not_after": "",
            "days_remaining": 0,
        }
        info["ssl_active"] = info["cert_exists"] and info["key_exists"]
        if info["cert_exists"]:
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                pem_bytes = SSL_CERT.read_bytes()
                cert = x509.load_pem_x509_certificate(pem_bytes, default_backend())
                cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
                info["subject"] = cn_attrs[0].value if cn_attrs else ""
                issuer_cn = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
                info["issuer"] = issuer_cn[0].value if issuer_cn else ""
                try:
                    san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                    info["sans"] = [n.value for n in san_ext.value]
                except x509.ExtensionNotFound:
                    info["sans"] = []
                info["not_before"] = cert.not_valid_before_utc.isoformat() if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.isoformat()
                info["not_after"] = cert.not_valid_after_utc.isoformat() if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.isoformat()
                exp = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
                now = _dt.datetime.now(_dt.timezone.utc)
                info["days_remaining"] = max(0, (exp - now).days)
                issuer_lower = info["issuer"].lower()
                if "let" in issuer_lower and "encrypt" in issuer_lower:
                    info["source"] = "letsencrypt"
                elif info["subject"] == info["issuer"] and info["subject"]:
                    info["source"] = "self-signed"
                else:
                    info["source"] = "manual"
            except Exception as e:
                info["parse_error"] = str(e)
        self._send_json(200, info)

    def _ssl_upload(self):
        """Accept PEM cert + key via JSON body, write to configured paths."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return
        cert_pem = data.get("cert", "").strip()
        key_pem = data.get("key", "").strip()
        if not cert_pem or "BEGIN CERTIFICATE" not in cert_pem:
            self._send_json(400, {"error": "Invalid certificate PEM"})
            return
        if not key_pem or "BEGIN" not in key_pem or "PRIVATE KEY" not in key_pem:
            self._send_json(400, {"error": "Invalid private key PEM"})
            return
        try:
            SSL_CERT.parent.mkdir(parents=True, exist_ok=True)
            SSL_CERT.write_text(cert_pem if cert_pem.endswith("\n") else cert_pem + "\n")
            SSL_KEY.write_text(key_pem if key_pem.endswith("\n") else key_pem + "\n")
            import os as _os
            _os.chmod(str(SSL_KEY), 0o600)
            # Phase 8.12: try live reload before falling back to the
            # restart-required message. Reload fails gracefully if the
            # server is plaintext (no live context) or if the new cert
            # chain is malformed.
            ok, msg = reload_tls_certs()
            if ok:
                self._send_json(200, {
                    "ok": True,
                    "message": "Certificate applied.",
                    "live_reload": True,
                })
            else:
                self._send_json(200, {
                    "ok": True,
                    "message": (
                        f"Certificate uploaded. Live reload not available "
                        f"({msg}); restart container to activate."
                    ),
                    "live_reload": False,
                })
        except Exception as e:
            self._send_json(500, {"error": f"Failed to write cert: {e}"})

    def _ssl_request_letsencrypt(self):
        """Run certbot with DNS-01 Cloudflare challenge to request/renew cert."""
        import tempfile, subprocess, shutil, os as _os
        ssl_cfg = _cfg.ssl
        domain = (ssl_cfg.domain or "").strip()
        email = (ssl_cfg.acme_email or "").strip()
        token = (ssl_cfg.acme_api_token or "").strip()
        provider = (ssl_cfg.acme_provider or "cloudflare").strip().lower()
        if not domain:
            self._send_json(400, {"error": "SSL domain not configured"})
            return
        if not email:
            self._send_json(400, {"error": "ACME email not configured"})
            return
        if not token:
            self._send_json(400, {"error": "ACME API token not configured"})
            return
        if provider != "cloudflare":
            self._send_json(400, {"error": f"Unsupported DNS provider: {provider}"})
            return
        creds_fd, creds_path = tempfile.mkstemp(prefix="cf_", suffix=".ini")
        try:
            with _os.fdopen(creds_fd, "w") as f:
                f.write(f"dns_cloudflare_api_token = {token}\n")
            _os.chmod(creds_path, 0o600)
            le_dir = SSL_CERT.parent / "letsencrypt"
            le_dir.mkdir(parents=True, exist_ok=True)
            work_dir = Path("/tmp/certbot_work")
            logs_dir = Path("/tmp/certbot_logs")
            work_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "certbot", "certonly",
                "--dns-cloudflare",
                "--dns-cloudflare-credentials", creds_path,
                "--dns-cloudflare-propagation-seconds", "30",
                "-d", domain,
                "--email", email,
                "--agree-tos",
                "--non-interactive",
                "--config-dir", str(le_dir),
                "--work-dir", str(work_dir),
                "--logs-dir", str(logs_dir),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            combined = (stdout + "\n" + stderr).strip()
            live_dir = le_dir / "live" / domain
            fullchain = live_dir / "fullchain.pem"
            privkey = live_dir / "privkey.pem"
            if result.returncode == 0 and fullchain.exists() and privkey.exists():
                try:
                    SSL_CERT.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(fullchain), str(SSL_CERT))
                    shutil.copy2(str(privkey), str(SSL_KEY))
                    _os.chmod(str(SSL_KEY), 0o600)
                except Exception as e:
                    self._send_json(500, {"error": f"Cert issued but copy failed: {e}", "log": combined})
                    return
                ok, reload_msg = reload_tls_certs()
                if ok:
                    self._send_json(200, {
                        "ok": True,
                        "message": "Certificate issued/renewed and applied.",
                        "live_reload": True,
                        "log": combined,
                    })
                else:
                    self._send_json(200, {
                        "ok": True,
                        "message": (
                            f"Certificate issued/renewed. Live reload not "
                            f"available ({reload_msg}); restart container "
                            f"to activate."
                        ),
                        "live_reload": False,
                        "log": combined,
                    })
            else:
                not_due = "not yet due for renewal" in combined.lower() or "no renewals were attempted" in combined.lower()
                if not_due and fullchain.exists():
                    self._send_json(200, {"ok": True, "message": "Certificate is not yet due for renewal (still valid).", "log": combined})
                else:
                    self._send_json(500, {"error": "certbot failed", "returncode": result.returncode, "log": combined})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "certbot timed out after 180s"})
        except FileNotFoundError:
            self._send_json(500, {"error": "certbot not installed in container"})
        except Exception as e:
            self._send_json(500, {"error": f"certbot execution error: {e}"})
        finally:
            try:
                _os.unlink(creds_path)
            except Exception:
                pass

    def _serve_binary(self, file_path: Path):
        ext = file_path.suffix.lstrip(".").lower()
        ct = CONTENT_TYPES.get(ext, "application/octet-stream")
        file_size = file_path.stat().st_size
        head_only = getattr(self, '_head_only', False)
        self._head_only = False

        # Range request support (needed for <audio> seeking and duration)
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            try:
                range_spec = range_header[6:]
                start_str, end_str = range_spec.split("-", 1)
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not head_only:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                return
            except (ValueError, OSError):
                pass

        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'inline; filename="%s"' % file_path.name)
        self.end_headers()
        if not head_only:
            self.wfile.write(file_path.read_bytes())

    def _delete_file(self):
        name = self.path[len("/api/files/"):]
        name = urllib.request.url2pathname(name)
        file_path = OUTPUT_DIR / name
        if not file_path.is_file() or not file_path.is_relative_to(OUTPUT_DIR):
            self._send_json(404, {"error": "File not found"})
            return
        try:
            file_path.unlink()
            self._send_json(200, {"deleted": name})
        except OSError as e:
            self._send_json(500, {"error": str(e)})

    # â”€â”€ Response helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # â”€â”€ Configuration API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_config(self):
        """Return full config as JSON (masks sensitive fields)."""
        data = _cfg.to_dict()
        # Mask sensitive values
        if "global" in data and "home_assistant" in data["global"]:
            token = data["global"]["home_assistant"].get("token", "")
            if token:
                data["global"]["home_assistant"]["token"] = token[:20] + "..." + token[-8:]
        if "global" in data and "auth" in data["global"]:
            data["global"]["auth"].pop("password_hash", None)
            data["global"]["auth"].pop("session_secret", None)
        self._send_json(200, data)

    def _get_config_section(self, section: str):
        """Return a single config section as JSON."""
        full = _cfg.to_dict()
        if section not in full:
            self._send_error(404, f"Unknown config section: {section}")
            return
        data = full[section]
        # Mask sensitive values in global section
        if section == "global":
            if "home_assistant" in data:
                token = data["home_assistant"].get("token", "")
                if token:
                    data["home_assistant"]["token"] = token[:20] + "..." + token[-8:]
            if "auth" in data:
                data["auth"].pop("password_hash", None)
                data["auth"].pop("session_secret", None)
        self._send_json(200, data)

    def _get_config_raw(self):
        """Return raw YAML contents of all config files."""
        configs_dir = Path("configs")
        result = {}
        for name in ["global", "services", "speakers", "audio", "personality"]:
            path = configs_dir / f"{name}.yaml"
            if path.exists():
                text = path.read_text(encoding="utf-8")
                # Mask token in raw view
                if name == "global":
                    import re as _re
                    text = _re.sub(
                        r'(token:\s*["\']?)([A-Za-z0-9._-]{20})[A-Za-z0-9._-]+([A-Za-z0-9._-]{8}["\']?)',
                        r'\1\2...\3', text
                    )
                result[name] = text
            else:
                result[name] = ""
        self._send_json(200, result)

    def _put_config_section(self, section: str):
        """Update a config section from JSON body.

        When the services section is saved, also mirror the Ollama
        interactive / autonomy URLs into glados_config.yaml. The engine
        uses its own `completion_url` / `autonomy.completion_url` fields
        from that separate YAML, so an operator editing URLs on the
        LLM & Services page wouldn't otherwise affect chat routing.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_error(400, f"Invalid JSON: {e}")
            return
        try:
            _cfg.update_section(section, data)
            if section == "services":
                self._sync_glados_config_urls(data)
            applied = _apply_config_live(section)
            self._send_json(200, {
                "ok": True,
                "section": section,
                "applied": applied,
            })
        except KeyError as e:
            self._send_error(404, str(e))
        except Exception as e:
            self._send_error(400, f"Validation error: {e}")

    def _sync_glados_config_urls(self, services_payload: dict) -> None:
        """Mirror LLM URLs *and* model names from the services payload
        into glados_config.yaml's chat + autonomy fields.

        Background: the engine (glados.core.engine.GladosConfig) reads
        `completion_url` and `llm_model` from glados_config.yaml,
        independently of the services.yaml that pydantic ServicesConfig
        owns. When operators edit Ollama URL or Model dropdowns on the
        LLM & Services page, the *engine* keeps using whatever was in
        glados_config.yaml until we mirror the change here. Symptoms
        were 504s on URL mismatch and 404s on model-name mismatch.

        Fields synced:
          services.ollama_interactive.url   -> Glados.completion_url
          services.ollama_interactive.model -> Glados.llm_model
          services.ollama_autonomy.url      -> Glados.autonomy.completion_url
          services.ollama_autonomy.model    -> Glados.autonomy.llm_model
        """
        interactive = services_payload.get("ollama_interactive") or {}
        autonomy = services_payload.get("ollama_autonomy") or {}
        interactive_url = (interactive.get("url") or "").strip()
        interactive_model = (interactive.get("model") or "").strip()
        autonomy_url = (autonomy.get("url") or "").strip()
        autonomy_model = (autonomy.get("model") or "").strip()
        if not any([interactive_url, interactive_model, autonomy_url, autonomy_model]):
            return

        config_path = Path(os.environ.get(
            "GLADOS_CONFIG",
            "/app/configs/glados_config.yaml",
        ))
        if not config_path.exists():
            logger.debug("glados_config.yaml not present at {}; skip LLM sync", config_path)
            return

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("glados_config LLM sync: failed to read {}: {}", config_path, exc)
            return

        glados_block = raw.get("Glados") if isinstance(raw.get("Glados"), dict) else None
        if glados_block is None:
            logger.debug("glados_config has no Glados block; skip sync")
            return

        changed = False
        if interactive_url:
            new_chat = _ollama_chat_url(interactive_url)
            if glados_block.get("completion_url") != new_chat:
                glados_block["completion_url"] = new_chat
                changed = True
        if interactive_model:
            if glados_block.get("llm_model") != interactive_model:
                glados_block["llm_model"] = interactive_model
                changed = True
        auton = glados_block.get("autonomy") if isinstance(glados_block.get("autonomy"), dict) else None
        if auton is not None:
            if autonomy_url:
                new_auton = _ollama_chat_url(autonomy_url)
                if auton.get("completion_url") != new_auton:
                    auton["completion_url"] = new_auton
                    changed = True
            if autonomy_model:
                if auton.get("llm_model") != autonomy_model:
                    auton["llm_model"] = autonomy_model
                    changed = True

        if not changed:
            return
        try:
            config_path.write_text(
                yaml.safe_dump(raw, sort_keys=False),
                encoding="utf-8",
            )
            _auton = glados_block.get("autonomy") or {}
            logger.info(
                "glados_config LLM sync: chat={} ({}); autonomy={} ({})",
                glados_block.get("completion_url"),
                glados_block.get("llm_model"),
                _auton.get("completion_url"),
                _auton.get("llm_model"),
            )
        except OSError as exc:
            logger.warning("glados_config LLM sync: write failed: {}", exc)

    def _put_config_raw(self):
        """Update a single config file from raw YAML text."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_error(400, f"Invalid JSON: {e}")
            return
        filename = data.get("file")
        content = data.get("content", "")
        valid_files = ["global", "services", "speakers", "audio", "personality"]
        if filename not in valid_files:
            self._send_error(400, f"Invalid config file: {filename}")
            return
        try:
            # Validate YAML parses
            parsed = yaml.safe_load(content)
            if parsed is None:
                parsed = {}
            # Validate against Pydantic model
            _cfg.update_section(filename, parsed)
            if filename == "services":
                self._sync_glados_config_urls(parsed)
            applied = _apply_config_live(filename)
            self._send_json(200, {"ok": True, "file": filename, "applied": applied})
        except Exception as e:
            self._send_error(400, f"Error: {e}")

    def _reload_config(self):
        """Reload all config from disk."""
        try:
            _cfg.reload()
            self._send_json(200, {"ok": True, "message": "Config reloaded"})
        except Exception as e:
            self._send_error(500, f"Reload failed: {e}")

    # ── Disambiguation rules (Phase 8.1) ───────────────────────────
    #
    # Rules live in configs/disambiguation.yaml, loaded once at startup
    # by server.py and held on the singleton Disambiguator. The WebUI's
    # Integrations → Home Assistant page exposes a "Disambiguation
    # rules" card for the operator to toggle twin dedup and edit the
    # opposing-token list. A save here writes the YAML and then POSTs
    # api_wrapper's /api/reload-disambiguation-rules so the live
    # disambiguator picks up the new rules without a container restart.

    def _disambiguation_yaml_path(self) -> Path:
        config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
        return Path(config_dir) / "disambiguation.yaml"

    def _get_disambiguation_rules(self) -> None:
        """Return the current disambiguation rules as JSON. Reads the
        on-disk YAML so the WebUI always shows the authoritative state
        — the running disambiguator's in-memory copy matches after any
        save + reload."""
        from glados.intent.rules import load_rules_from_yaml, rules_to_dict
        try:
            rules = load_rules_from_yaml(self._disambiguation_yaml_path())
            self._send_json(200, rules_to_dict(rules))
        except Exception as exc:
            self._send_error(500, f"Failed to load rules: {exc}")

    def _put_disambiguation_rules(self) -> None:
        """Save disambiguation rules from JSON body.

        Only the two Phase 8.1 fields (twin_dedup, opposing_token_pairs)
        are writable from the WebUI today; the legacy naming_convention
        / overhead_synonyms / etc. are preserved from disk so an operator
        editing via the card doesn't silently wipe hand-tuned YAML."""
        from glados.intent.rules import (
            load_rules_from_yaml, save_rules_to_yaml,
        )
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return

        try:
            path = self._disambiguation_yaml_path()
            rules = load_rules_from_yaml(path)
            if "twin_dedup" in data:
                rules.twin_dedup = bool(data["twin_dedup"])
            if "opposing_token_pairs" in data:
                pairs_raw = data.get("opposing_token_pairs") or []
                if not isinstance(pairs_raw, list):
                    self._send_error(400, "opposing_token_pairs must be a list")
                    return
                cleaned: list[list[str]] = []
                seen: set[tuple[str, str]] = set()
                for pair in pairs_raw:
                    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                        continue
                    a = str(pair[0]).strip()
                    b = str(pair[1]).strip()
                    if not a or not b or a.lower() == b.lower():
                        continue
                    key = tuple(sorted([a.lower(), b.lower()]))
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append([a, b])
                rules.opposing_token_pairs = cleaned
            if "extra_command_verbs" in data:
                verbs_raw = data.get("extra_command_verbs") or []
                if not isinstance(verbs_raw, list):
                    self._send_error(400, "extra_command_verbs must be a list")
                    return
                cleaned_verbs: list[str] = []
                seen_verbs: set[str] = set()
                for v in verbs_raw:
                    vs = str(v).strip()
                    if not vs or vs.lower() in seen_verbs:
                        continue
                    seen_verbs.add(vs.lower())
                    cleaned_verbs.append(vs.lower())
                rules.extra_command_verbs = cleaned_verbs
            if "extra_ambient_patterns" in data:
                pats_raw = data.get("extra_ambient_patterns") or []
                if not isinstance(pats_raw, list):
                    self._send_error(400, "extra_ambient_patterns must be a list")
                    return
                import re as _re
                cleaned_pats: list[str] = []
                for p in pats_raw:
                    ps = str(p)
                    if not ps.strip():
                        continue
                    try:
                        _re.compile(ps)
                    except _re.error as exc:
                        self._send_error(400, f"Invalid regex {ps!r}: {exc}")
                        return
                    cleaned_pats.append(ps)
                rules.extra_ambient_patterns = cleaned_pats
            if "extra_segment_tokens" in data:
                tokens_raw = data.get("extra_segment_tokens") or []
                if not isinstance(tokens_raw, list):
                    self._send_error(400, "extra_segment_tokens must be a list")
                    return
                cleaned_tokens: list[str] = []
                seen_tokens: set[str] = set()
                for t in tokens_raw:
                    ts = str(t).strip().lower()
                    if not ts or ts in seen_tokens:
                        continue
                    seen_tokens.add(ts)
                    cleaned_tokens.append(ts)
                rules.extra_segment_tokens = cleaned_tokens
            if "ignore_segments" in data:
                rules.ignore_segments = bool(data["ignore_segments"])
            if "verification_mode" in data:
                mode = str(data["verification_mode"] or "").strip().lower()
                if mode not in {"strict", "warn", "silent"}:
                    self._send_error(
                        400,
                        "verification_mode must be one of: strict, warn, silent",
                    )
                    return
                rules.verification_mode = mode
            if "verification_timeout_s" in data:
                try:
                    ts = float(data["verification_timeout_s"])
                except (TypeError, ValueError):
                    self._send_error(400, "verification_timeout_s must be a number")
                    return
                if not (0 < ts <= 30):
                    self._send_error(
                        400,
                        "verification_timeout_s must be between 0 and 30 seconds",
                    )
                    return
                rules.verification_timeout_s = ts
            if "floor_aliases" in data:
                fa_raw = data.get("floor_aliases") or {}
                if not isinstance(fa_raw, dict):
                    self._send_error(400, "floor_aliases must be an object")
                    return
                cleaned_fa: dict[str, str] = {}
                for k, v in fa_raw.items():
                    ks = str(k).strip().lower()
                    vs = str(v).strip()
                    if ks and vs:
                        cleaned_fa[ks] = vs
                rules.floor_aliases = cleaned_fa
            if "area_aliases" in data:
                aa_raw = data.get("area_aliases") or {}
                if not isinstance(aa_raw, dict):
                    self._send_error(400, "area_aliases must be an object")
                    return
                cleaned_aa: dict[str, str] = {}
                for k, v in aa_raw.items():
                    ks = str(k).strip().lower()
                    vs = str(v).strip()
                    if ks and vs:
                        cleaned_aa[ks] = vs
                rules.area_aliases = cleaned_aa
            if "response_mode" in data:
                rm = str(data["response_mode"] or "").strip()
                if rm not in {"LLM", "LLM_safe", "quip", "chime", "silent"}:
                    self._send_error(
                        400,
                        "response_mode must be one of: "
                        "LLM, LLM_safe, quip, chime, silent",
                    )
                    return
                rules.response_mode = rm
            if "response_mode_per_event" in data:
                rmp_raw = data.get("response_mode_per_event") or {}
                if not isinstance(rmp_raw, dict):
                    self._send_error(400, "response_mode_per_event must be an object")
                    return
                valid_events = {"command_ack", "query_answer", "ambient_cue", "error"}
                valid_modes = {"LLM", "LLM_safe", "quip", "chime", "silent"}
                cleaned_rmp: dict[str, str] = {}
                for k, v in rmp_raw.items():
                    ks = str(k).strip()
                    vs = str(v).strip()
                    if ks not in valid_events:
                        self._send_error(
                            400,
                            f"response_mode_per_event: unknown event {ks!r}",
                        )
                        return
                    if vs not in valid_modes:
                        self._send_error(
                            400,
                            f"response_mode_per_event[{ks}]: invalid mode {vs!r}",
                        )
                        return
                    cleaned_rmp[ks] = vs
                rules.response_mode_per_event = cleaned_rmp
            save_rules_to_yaml(path, rules)
            applied = self._reload_disambiguator_rules()
            self._send_json(200, {
                "ok": True,
                "applied": applied,
                "twin_dedup": rules.twin_dedup,
                "opposing_token_pairs": rules.opposing_token_pairs,
                "extra_command_verbs": rules.extra_command_verbs,
                "extra_ambient_patterns": rules.extra_ambient_patterns,
                "extra_segment_tokens": rules.extra_segment_tokens,
                "ignore_segments": rules.ignore_segments,
                "verification_mode": rules.verification_mode,
                "verification_timeout_s": rules.verification_timeout_s,
                "floor_aliases": rules.floor_aliases,
                "area_aliases": rules.area_aliases,
                "response_mode": rules.response_mode,
                "response_mode_per_event": rules.response_mode_per_event,
            })
        except Exception as exc:
            self._send_error(500, f"Failed to save rules: {exc}")

    def _post_precheck_test(self) -> None:
        """Phase 8.2 — dry-run an utterance through
        `looks_like_home_command` so the WebUI Command recognition
        card can show the operator whether their test phrase would
        be picked up. Returns the structured match reasons so they
        can see exactly which of the four signals fired."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        utterance = str(data.get("utterance") or "")
        if not utterance.strip():
            self._send_error(400, "utterance is required")
            return
        from glados.intent import explain_home_command_match
        self._send_json(200, explain_home_command_match(utterance))

    # ── Phase 8.7c — quip library editor ────────────────────────

    def _quip_dir(self) -> Path:
        """Root of the quip library on disk. Mirrors the
        GLADOS_QUIP_DIR env var the disambiguator reads; defaults to
        /app/configs/quips under the bind-mount."""
        return Path(os.environ.get("GLADOS_QUIP_DIR", "/app/configs/quips"))

    def _quip_path_safe(self, rel: str) -> Path | None:
        """Resolve a caller-supplied relative path within the quip
        root, refusing anything that escapes via '..' or absolute
        components. Returns None when the path is unsafe."""
        if not rel or not isinstance(rel, str):
            return None
        root = self._quip_dir().resolve()
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None
        return candidate

    def _get_quips(self) -> None:
        """GET /api/quips — returns the directory tree with a line
        count per file. Use ?path= to fetch one file's contents."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        root = self._quip_dir()
        rel = (q.get("path") or [""])[0].strip()
        if rel:
            target = self._quip_path_safe(rel)
            if target is None or not target.is_file():
                self._send_error(404, "Not found")
                return
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                self._send_error(500, f"Read failed: {exc}")
                return
            self._send_json(200, {"path": rel, "lines": lines})
            return
        # Tree listing.
        tree: list[dict[str, object]] = []
        if root.exists():
            for p in sorted(root.rglob("*.txt")):
                try:
                    raw = p.read_text(encoding="utf-8")
                except OSError:
                    continue
                non_blank = sum(
                    1 for ln in raw.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")
                )
                tree.append({
                    "path": p.relative_to(root).as_posix(),
                    "quip_count": non_blank,
                })
        self._send_json(200, {"root": str(root), "files": tree})

    def _put_quips(self) -> None:
        """PUT /api/quips — save a single file. Body:
        {"path": "command_ack/turn_on/normal.txt", "lines": ["a", "b"]}.
        Parent directories are created on demand. The file is written
        atomically via rename so the disambiguator never sees a
        half-written file mid-read."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        rel = str(data.get("path") or "").strip()
        if not rel.endswith(".txt"):
            self._send_error(400, "path must end with .txt")
            return
        target = self._quip_path_safe(rel)
        if target is None:
            self._send_error(400, "Unsafe path")
            return
        raw_lines = data.get("lines")
        if not isinstance(raw_lines, list):
            self._send_error(400, "lines must be a list of strings")
            return
        cleaned = []
        for ln in raw_lines:
            if isinstance(ln, str):
                # Normalise: strip trailing whitespace but keep
                # comment lines and blanks.
                cleaned.append(ln.rstrip())
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            self._send_error(500, f"Write failed: {exc}")
            return
        # Count non-blank / non-comment lines for the response so the
        # UI can refresh its tree without a second GET.
        quip_count = sum(
            1 for ln in cleaned
            if ln.strip() and not ln.lstrip().startswith("#")
        )
        self._send_json(200, {
            "ok": True, "path": rel, "quip_count": quip_count,
        })

    def _delete_quip(self) -> None:
        """DELETE /api/quips?path=...  — remove a single .txt file.
        Empty parent directories are also removed so the tree stays
        clean."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        rel = (q.get("path") or [""])[0].strip()
        if not rel.endswith(".txt"):
            self._send_error(400, "path must end with .txt")
            return
        target = self._quip_path_safe(rel)
        if target is None or not target.is_file():
            self._send_error(404, "Not found")
            return
        try:
            target.unlink()
            # Climb up and remove empty dirs (but stop at root).
            root = self._quip_dir().resolve()
            parent = target.parent
            while parent != root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except OSError as exc:
            self._send_error(500, f"Delete failed: {exc}")
            return
        self._send_json(200, {"ok": True, "path": rel})

    def _post_quips_test(self) -> None:
        """POST /api/quips/test — dry-run the selector so the operator
        can see which line the composer would emit right now.
        Body: {"event_category", "intent", "outcome"?, "mood"?,
              "entity_count"?, "time_of_day"?}."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        from glados.persona import QuipLibrary, QuipRequest
        lib = QuipLibrary.load(self._quip_dir())
        req = QuipRequest(
            event_category=str(data.get("event_category") or "command_ack"),
            intent=str(data.get("intent") or "turn_on"),
            outcome=str(data.get("outcome") or "success"),
            mood=str(data.get("mood") or "normal"),
            entity_count=int(data.get("entity_count") or 1),
            time_of_day=str(data.get("time_of_day") or ""),
        )
        line = lib.pick(req)
        self._send_json(200, {
            "line": line,
            "library_empty": lib.is_empty(),
            "request": {
                "event_category": req.event_category,
                "intent": req.intent,
                "mood": req.mood,
                "time_of_day": req.time_of_day,
            },
        })

    # ── Phase 8.7 (deferred) — chime library editor ──────────────

    # Allowed extensions for upload. ``.mp3`` because HA media_player
    # supports it directly; ``.wav`` because Piper/Speaches emits it
    # and the existing scenario chime loader at api_wrapper.py reads
    # it. Nothing else is served from this endpoint — unknown
    # extensions refuse on both upload and fetch to keep this from
    # becoming a generic file host.
    _CHIME_ALLOWED_EXT: "frozenset[str]" = frozenset({".wav", ".mp3"})
    _CHIME_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB per clip

    def _chime_dir(self) -> Path:
        """Root of the chime library on disk. Operator-configurable
        via `AudioConfig.chimes_dir` (defaults to `/app/audio_files/chimes`).
        This is the directory `api_wrapper.py`'s scenario-chime loader
        reads from at request time."""
        return Path(_cfg.audio.chimes_dir)

    def _chime_path_safe(self, rel: str) -> Path | None:
        """Resolve a caller-supplied relative path within the chime
        root. Refuses path traversal (``..``, absolute), unknown
        extensions, and any nested directory (chimes are flat —
        filename only, no subdirs)."""
        if not rel or not isinstance(rel, str):
            return None
        # Flat library: reject anything with a separator.
        if "/" in rel or "\\" in rel:
            return None
        if not rel.lower().endswith(tuple(self._CHIME_ALLOWED_EXT)):
            return None
        root = self._chime_dir().resolve()
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None
        return candidate

    def _get_chimes(self) -> None:
        """GET /api/chimes — tree listing.
        GET /api/chimes?path=<file.wav> — fetch the file bytes for
        play-test (audio/wav or audio/mpeg content-type)."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        rel = (q.get("path") or [""])[0].strip()
        if rel:
            target = self._chime_path_safe(rel)
            if target is None or not target.is_file():
                self._send_error(404, "Not found")
                return
            try:
                data = target.read_bytes()
            except OSError as exc:
                self._send_error(500, f"Read failed: {exc}")
                return
            ct = "audio/wav" if target.suffix.lower() == ".wav" else "audio/mpeg"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if getattr(self, "_head_only", False):
                self._head_only = False
                return
            self.wfile.write(data)
            return
        # Tree listing: flat file list with size.
        root = self._chime_dir()
        files: list[dict[str, object]] = []
        if root.exists():
            for p in sorted(root.iterdir()):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in self._CHIME_ALLOWED_EXT:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                files.append({
                    "name": p.name,
                    "bytes": size,
                })
        self._send_json(200, {"root": str(root), "files": files})

    def _put_chime(self) -> None:
        """PUT /api/chimes — upload a single clip. Body:
        {"name": "notify.wav", "data_b64": "<base64 payload>"}.
        Max 5 MB. Overwrites an existing file with the same name
        (atomic rename) so the operator can revise a clip in place.
        """
        import base64
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > self._CHIME_MAX_BYTES * 2:  # base64 ≈ 4/3 overhead
                self._send_error(413, "Payload too large")
                return
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        name = str(data.get("name") or "").strip()
        b64 = str(data.get("data_b64") or "").strip()
        if not name or not b64:
            self._send_error(400, "Both 'name' and 'data_b64' are required")
            return
        target = self._chime_path_safe(name)
        if target is None:
            self._send_error(
                400,
                "Unsafe name or unsupported extension (allowed: .wav .mp3)",
            )
            return
        try:
            payload = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            self._send_error(400, f"Invalid base64: {exc}")
            return
        if len(payload) > self._CHIME_MAX_BYTES:
            self._send_error(413, f"Clip exceeds {self._CHIME_MAX_BYTES} bytes")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(target)
        except OSError as exc:
            self._send_error(500, f"Write failed: {exc}")
            return
        self._send_json(200, {
            "ok": True, "name": target.name, "bytes": len(payload),
        })

    def _delete_chime(self) -> None:
        """DELETE /api/chimes?path=<file.wav> — remove one clip."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        rel = (q.get("path") or [""])[0].strip()
        target = self._chime_path_safe(rel)
        if target is None or not target.is_file():
            self._send_error(404, "Not found")
            return
        try:
            target.unlink()
        except OSError as exc:
            self._send_error(500, f"Delete failed: {exc}")
            return
        self._send_json(200, {"ok": True, "name": target.name})

    # ── Phase 8.14 — canon library editor ────────────────────────

    def _canon_dir(self) -> Path:
        """Root of the canon library on disk. GLADOS_CANON_DIR env
        override; defaults to /app/configs/canon under the bind mount."""
        return Path(os.environ.get("GLADOS_CANON_DIR", "/app/configs/canon"))

    def _canon_path_safe(self, rel: str) -> Path | None:
        """Resolve a caller-supplied relative path within the canon
        root, refusing anything that escapes via '..' or absolute
        components. Returns None when the path is unsafe."""
        if not rel or not isinstance(rel, str):
            return None
        root = self._canon_dir().resolve()
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None
        return candidate

    @staticmethod
    def _count_canon_entries(raw: str) -> int:
        """Count blank-line-separated entries in a canon file, ignoring
        pure-comment blocks. Mirrors parse_canon_file's logic."""
        import re as _re
        stripped = _re.sub(r"^\s*#.*$", "", raw, flags=_re.MULTILINE)
        return sum(
            1 for b in _re.split(r"\n\s*\n", stripped) if b.strip()
        )

    def _get_canon(self) -> None:
        """GET /api/canon — tree listing or single-file fetch via
        ?path=. Response shape mirrors /api/quips."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        root = self._canon_dir()
        rel = (q.get("path") or [""])[0].strip()
        if rel:
            target = self._canon_path_safe(rel)
            if target is None or not target.is_file():
                self._send_error(404, "Not found")
                return
            try:
                text = target.read_text(encoding="utf-8")
            except OSError as exc:
                self._send_error(500, f"Read failed: {exc}")
                return
            self._send_json(200, {
                "path": rel,
                "text": text,
                "entry_count": self._count_canon_entries(text),
            })
            return
        tree: list[dict[str, object]] = []
        if root.exists():
            for p in sorted(root.glob("*.txt")):
                try:
                    raw = p.read_text(encoding="utf-8")
                except OSError:
                    continue
                tree.append({
                    "path": p.name,
                    "entry_count": self._count_canon_entries(raw),
                })
        self._send_json(200, {"root": str(root), "files": tree})

    def _put_canon(self) -> None:
        """PUT /api/canon — atomic save of a single canon .txt file.
        Body: {"path": "<topic>.txt", "text": "<whole file>"}. Triggers
        a cross-process reload so the running engine picks up new
        entries without a restart."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        rel = str(data.get("path") or "").strip()
        if not rel.endswith(".txt") or "/" in rel or "\\" in rel:
            self._send_error(400, "path must be a flat <topic>.txt")
            return
        text = data.get("text")
        if not isinstance(text, str):
            self._send_error(400, "text must be a string")
            return
        target = self._canon_path_safe(rel)
        if target is None:
            self._send_error(400, "Unsafe path")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            self._send_error(500, f"Write failed: {exc}")
            return
        reloaded = self._reload_canon_library()
        entry_count = self._count_canon_entries(text)
        self._send_json(200, {
            "ok": True,
            "path": rel,
            "entry_count": entry_count,
            "reloaded": reloaded,
        })

    def _delete_canon(self) -> None:
        """DELETE /api/canon?path=<topic>.txt — removes the file.
        Canon entries previously seeded from that file remain in
        ChromaDB until the next engine rebuild; the editor is
        file-oriented, not entry-oriented."""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        rel = (q.get("path") or [""])[0].strip()
        if not rel.endswith(".txt"):
            self._send_error(400, "path must end with .txt")
            return
        target = self._canon_path_safe(rel)
        if target is None or not target.is_file():
            self._send_error(404, "Not found")
            return
        try:
            target.unlink()
        except OSError as exc:
            self._send_error(500, f"Delete failed: {exc}")
            return
        self._send_json(200, {"ok": True, "path": rel})

    def _post_canon_test(self) -> None:
        """POST /api/canon/test — dry-run. Given an utterance, return
        whether the gate fired and which canon entries (if any) would
        be retrieved. Lets the operator validate edits without
        round-tripping through a real chat request."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return
        utterance = str(data.get("utterance") or "")
        if not utterance.strip():
            self._send_error(400, "utterance is required")
            return
        from glados.core.context_gates import needs_canon_context
        gate_fired = needs_canon_context(utterance)
        # Retrieval lives in the api_wrapper process; proxy through so
        # we hit the actual running memory_store, not a fresh one.
        try:
            proxied = self._proxy_api_wrapper(
                "/api/canon/retrieve",
                method="POST",
                body=json.dumps({"utterance": utterance}).encode("utf-8"),
            )
            retrieved = proxied.get("entries") if isinstance(proxied, dict) else []
        except Exception as exc:
            retrieved = []
            logger.debug("Canon retrieval proxy failed: {}", exc)
        self._send_json(200, {
            "gate_fired": gate_fired,
            "entries": retrieved,
        })

    def _reload_canon_library(self) -> bool:
        """Cross-process reload trigger. tts_ui (8052) POSTs the
        api_wrapper (8015) endpoint that owns the live memory_store."""
        try:
            req = urllib.request.Request(
                _svc_api_wrapper() + "/api/reload-canon",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read() or b"{}")
                return bool(body.get("ok"))
        except Exception as exc:
            logger.warning("Canon library reload RPC failed: {}", exc)
            return False

    def _reload_disambiguator_rules(self) -> bool:
        """Cross-process reload trigger. tts_ui (8052) POSTs the
        api_wrapper (8015) endpoint that holds the live Disambiguator
        singleton. Returns True on success, False on failure (logged).
        """
        try:
            req = urllib.request.Request(
                _svc_api_wrapper() + "/api/reload-disambiguation-rules",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read() or b"{}")
                return bool(body.get("ok"))
        except Exception as exc:
            logger.warning(
                "Disambiguation rules reload RPC failed: {}", exc,
            )
            return False

    # ── Phase 8.3.5 — semantic retrieval proxy endpoints ──
    #
    # The SemanticIndex lives in the api_wrapper process (same one
    # that holds the Disambiguator singleton). The WebUI proxies
    # GET/POST through to :8015 with the same cross-process pattern
    # used for disambiguation rule reload.

    def _proxy_api_wrapper(
        self, path: str, *, method: str = "GET", body: bytes = b"",
        timeout: float = 30.0,
    ) -> None:
        """Forward a request to api_wrapper and stream the response
        (status + JSON body) back to the browser."""
        try:
            headers = {"Content-Type": "application/json"}
            req = urllib.request.Request(
                _svc_api_wrapper() + path,
                data=body if method != "GET" else None,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._send_json(resp.status, json.loads(resp.read() or b"{}"))
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except Exception:
                err = {"error": {"message": exc.reason or "proxy error"}}
            self._send_json(exc.code, err)
        except Exception as exc:
            self._send_error(502, f"semantic proxy failed: {exc}")

    def _get_semantic_status(self) -> None:
        self._proxy_api_wrapper("/api/semantic/status", method="GET")

    def _post_semantic_test(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        self._proxy_api_wrapper(
            "/api/semantic/test", method="POST", body=body, timeout=30.0,
        )

    def _post_semantic_rebuild(self) -> None:
        self._proxy_api_wrapper(
            "/api/semantic/rebuild", method="POST", body=b"{}", timeout=10.0,
        )

    # â”€â”€ HUB75 test endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _hub75_test_ping(self):
        """Ping the WLED device directly from the WebUI."""
        from glados.hub75.wled_client import WledClient
        try:
            client = WledClient(_cfg.hub75.wled_ip)
            ok, latency = client.ping()
            self._send_json(200, {"ok": ok, "latency_ms": latency})
        except Exception as e:
            self._send_json(200, {"ok": False, "latency_ms": 0, "error": str(e)})

    def _hub75_test_cycle(self):
        """Cycle through all eye states (runs in background thread)."""
        if not _cfg.hub75.enabled:
            self._send_error(400, "HUB75 display is not enabled")
            return
        self._send_json(200, {"ok": True, "message": "Cycling eye states (14s)"})

    def _hub75_test_blank(self):
        """Send a blank frame to the panel."""
        if not _cfg.hub75.enabled:
            self._send_error(400, "HUB75 display is not enabled")
            return
        from glados.hub75.ddp import DdpSender
        try:
            sender = DdpSender(_cfg.hub75.wled_ip, _cfg.hub75.wled_ddp_port)
            blank = bytes(_cfg.hub75.panel_width * _cfg.hub75.panel_height * 3)
            sender.send_frame(blank)
            sender.close()
            self._send_json(200, {"ok": True, "message": "Blank frame sent"})
        except Exception as e:
            self._send_error(500, f"Failed to blank panel: {e}")

    def _handle_eye_demo(self):
        """Start or stop the eye demo subprocess."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, ValueError):
            self._send_error(400, "Invalid JSON")
            return
        action = body.get("action", "")
        if action == "start":
            result = _eye_demo_start()
            self._send_json(200, result)
        elif action == "stop":
            result = _eye_demo_stop()
            self._send_json(200, result)
        else:
            self._send_error(400, "action must be 'start' or 'stop'")

    # â”€â”€ Robot node management endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _robots_status(self):
        """GET /api/robots/status â€” all nodes + bots health."""
        mgr = _get_robot_manager()
        if mgr is None:
            self._send_json(200, {"enabled": False, "nodes": {}, "bots": {}})
            return
        self._send_json(200, {
            "enabled": True,
            "nodes": mgr.get_all_health(),
            "bots": mgr.get_bots_summary(),
        })

    def _robots_add_node(self):
        """POST /api/robots/node/add â€” {url, name?, node_id?}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_error(400, "Invalid JSON")
            return
        url = body.get("url", "").strip()
        if not url:
            self._send_error(400, "url is required")
            return
        name = body.get("name", "").strip()
        # Auto-generate node_id from URL if not provided
        node_id = body.get("node_id", "").strip()
        if not node_id:
            node_id = re.sub(r'[^a-z0-9]+', '_', url.lower().replace("http://", "").replace("https://", "")).strip('_')
        mgr = _get_robot_manager()
        if mgr is None:
            # Enable robots and create manager
            global _robot_manager
            _cfg.robots.enabled = True  # Will be persisted when add_node saves
            from glados.robots.manager import RobotManager
            _robot_manager = RobotManager(_cfg.robots)
            _robot_manager.start()
            mgr = _robot_manager
        ok = mgr.add_node(node_id, url, name)
        if ok:
            self._send_json(200, {"ok": True, "node_id": node_id})
        else:
            self._send_error(400, f"Node {node_id} already exists")

    def _robots_remove_node(self):
        """POST /api/robots/node/remove â€” {node_id}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_error(400, "Invalid JSON")
            return
        node_id = body.get("node_id", "").strip()
        if not node_id:
            self._send_error(400, "node_id is required")
            return
        mgr = _get_robot_manager()
        if mgr is None:
            self._send_error(400, "Robot subsystem is not enabled")
            return
        ok = mgr.remove_node(node_id)
        self._send_json(200, {"ok": ok})

    def _robots_toggle_node(self):
        """POST /api/robots/node/toggle â€” {node_id, enabled}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_error(400, "Invalid JSON")
            return
        node_id = body.get("node_id", "").strip()
        enabled = body.get("enabled", True)
        if not node_id:
            self._send_error(400, "node_id is required")
            return
        mgr = _get_robot_manager()
        if mgr is None:
            self._send_error(400, "Robot subsystem is not enabled")
            return
        ok = mgr.toggle_node(node_id, bool(enabled))
        self._send_json(200, {"ok": ok})

    def _robots_identify_node(self):
        """POST /api/robots/node/identify â€” {node_id}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_error(400, "Invalid JSON")
            return
        node_id = body.get("node_id", "").strip()
        if not node_id:
            self._send_error(400, "node_id is required")
            return
        mgr = _get_robot_manager()
        if mgr is None:
            self._send_error(400, "Robot subsystem is not enabled")
            return
        ok = mgr.identify_node(node_id)
        self._send_json(200, {"ok": ok})

    def _robots_emergency_stop(self):
        """POST /api/robots/emergency-stop â€” {bot_id?}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}
        mgr = _get_robot_manager()
        if mgr is None:
            self._send_error(400, "Robot subsystem is not enabled")
            return
        bot_id = body.get("bot_id", "").strip() if body else ""
        if bot_id:
            results = mgr.emergency_stop_bot(bot_id)
        else:
            results = mgr.emergency_stop_all()
        self._send_json(200, {"ok": True, "results": results})

    # â”€â”€ Training Monitor handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_training_status(self):
        """GET /api/training/status"""
        running = _is_training_running()
        metrics_csv = _find_latest_metrics_csv()
        gen_loss, disc_loss, epoch, step = None, None, None, None

        if metrics_csv and metrics_csv.exists():
            try:
                with open(metrics_csv, "r") as f:
                    lines = f.readlines()
                if len(lines) > 1:
                    last = lines[-1].strip().split(",")
                    gen_loss = float(last[0])
                    disc_loss = float(last[1])
                    epoch = int(last[2])
                    step = int(last[3])
            except Exception:
                pass

        ckpt = _find_latest_checkpoint()
        ckpt_name = ckpt.name if ckpt else None

        ft_epoch = (epoch - _TRAIN_BASE_EPOCH) if epoch is not None else None

        with _snapshot_lock:
            snap = dict(_snapshot_status)

        self._send_json(200, {
            "running": running,
            "epoch": epoch,
            "ft_epoch": ft_epoch,
            "gen_loss": gen_loss,
            "disc_loss": disc_loss,
            "step": step,
            "base_epoch": _TRAIN_BASE_EPOCH,
            "max_epochs": 3000,
            "checkpoint": ckpt_name,
            "snapshot": snap,
        })

    def _get_training_metrics(self):
        """GET /api/training/metrics?since_step=N"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        since_step = int(params.get("since_step", [0])[0])

        metrics_csv = _find_latest_metrics_csv()
        rows = []
        if metrics_csv and metrics_csv.exists():
            try:
                with open(metrics_csv, "r") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        s = int(row["step"])
                        if s > since_step:
                            rows.append({
                                "epoch": int(row["epoch"]),
                                "ft_epoch": int(row["epoch"]) - _TRAIN_BASE_EPOCH,
                                "step": s,
                                "gen_loss": float(row["loss_gen_all"]),
                                "disc_loss": float(row["loss_disc_all"]),
                            })
            except Exception:
                pass

        self._send_json(200, {"metrics": rows})

    def _get_training_log(self):
        """GET /api/training/log?lines=200 â€” combines metrics + filtered log."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        n_lines = int(params.get("lines", [200])[0])

        lines = []

        # Add formatted metrics as the main log content
        metrics_csv = _find_latest_metrics_csv()
        if metrics_csv and metrics_csv.exists():
            try:
                with open(metrics_csv, "r") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        epoch = int(row["epoch"])
                        ft = epoch - _TRAIN_BASE_EPOCH
                        gen = float(row["loss_gen_all"])
                        disc = float(row["loss_disc_all"])
                        step = int(row["step"])
                        gen_str = f"{gen:.1f}" if gen < 1e6 else f"{gen:.2e}"
                        lines.append(
                            f"[FT Epoch {ft:>4}] step={step}  gen_loss={gen_str}  disc_loss={disc:.4f}"
                        )
            except Exception:
                pass

        # Append filtered startup log (skip DEBUG lines)
        if _TRAIN_LOG.exists():
            try:
                with open(_TRAIN_LOG, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        stripped = raw.rstrip()
                        if stripped and not stripped.startswith("DEBUG:"):
                            lines.append(stripped)
            except Exception:
                pass

        self._send_json(200, {"lines": lines[-n_lines:]})

    def _training_snapshot(self):
        """POST /api/training/snapshot â€” kick off background snapshot+deploy."""
        with _snapshot_lock:
            if _snapshot_status.get("state") == "running":
                self._send_json(409, {"ok": False, "error": "Snapshot already in progress"})
                return

        t = threading.Thread(target=_do_snapshot, daemon=True)
        t.start()
        self._send_json(200, {"ok": True, "message": "Snapshot started"})

    def _training_stop(self):
        """POST /api/training/stop â€” kill the training process."""
        try:
            result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")  # piper_train not available in container
            killed = result.stdout.strip()
            if killed:
                self._send_json(200, {"ok": True, "message": f"Killed PID(s): {killed}"})
            else:
                self._send_json(200, {"ok": True, "message": "No training process found"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    # â”€â”€ Response helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, msg: str):
        self._send_json(code, {"ok": False, "error": msg})




# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HTML / CSS / JS â€” GLaDOS Control Panel (Responsive Sidebar Layout)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GLaDOS Control Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Major+Mono+Display&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<!-- â”€â”€ Sidebar (desktop) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ -->
<nav class="sidebar">
  <div class="sidebar-brand">
    <span class="engine-status-dot" id="engineStatusDot" title="Engine status"></span>
    <span>GLaDOS</span>
    <span>Control</span>
  </div>
  <div class="nav-items">
    <a class="nav-item" data-nav-key="chat" onclick="navigateTo('chat')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      Chat
    </a>
    <a class="nav-item" data-nav-key="tts" onclick="navigateTo('tts')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
      TTS Generator
    </a>
    <a class="nav-item nav-parent" data-nav-key="config" onclick="navToggleConfig()" data-requires-auth="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
      Configuration <span class="lock-icon" id="lockConfig"></span>
      <span class="nav-caret">&#9656;</span>
    </a>
    <div class="nav-children">
      <a class="nav-item" data-nav-key="config.system" onclick="navigateTo('config.system')" data-requires-auth="true">System</a>
      <a class="nav-item" data-nav-key="config.integrations" onclick="navigateTo('config.integrations')" data-requires-auth="true">Integrations</a>
      <a class="nav-item" data-nav-key="config.llm-services" onclick="navigateTo('config.llm-services')" data-requires-auth="true">LLM &amp; Services</a>
      <a class="nav-item" data-nav-key="config.audio-speakers" onclick="navigateTo('config.audio-speakers')" data-requires-auth="true">Audio &amp; Speakers</a>
      <a class="nav-item" data-nav-key="config.personality" onclick="navigateTo('config.personality')" data-requires-auth="true">Personality</a>
      <a class="nav-item" data-nav-key="config.memory" onclick="navigateTo('config.memory')" data-requires-auth="true">Memory</a>
      <a class="nav-item" data-nav-key="config.logs" onclick="navigateTo('config.logs')" data-requires-auth="true">Logs</a>
      <a class="nav-item" data-nav-key="config.ssl" onclick="navigateTo('config.ssl')" data-requires-auth="true">SSL</a>
      <a class="nav-item" data-nav-key="config.raw" onclick="navigateTo('config.raw')" data-requires-auth="true">Raw YAML</a>
    </div>
    <!-- Training removed: piper_train is a host-native tool, not available in container -->
  </div>
  <div class="sidebar-footer">
    <a id="authLinkSidebar" href="/login">Sign In</a>
  </div>
</nav>

<!-- â”€â”€ Top bar (mobile) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ -->
<div class="topbar">
  <div class="topbar-inner">
    <span class="topbar-brand">GLaDOS</span>
    <a class="nav-item" data-nav-key="chat" onclick="navigateTo('chat')">Chat</a>
    <a class="nav-item" data-nav-key="tts" onclick="navigateTo('tts')">TTS</a>
    <a class="nav-item" data-nav-key="config.system" onclick="navigateTo('config.system')" data-requires-auth="true">System</a>
    <a class="nav-item" data-nav-key="config.integrations" onclick="navigateTo('config.integrations')" data-requires-auth="true">Config</a>
    <!-- Training removed: not available in container -->
  </div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- MAIN CONTENT                                                   -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<main class="main-content">

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 1: TTS Generator                                           -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-tts" class="tab-content">
<div class="container">
  <div class="card">
    <div class="section-title">Enter text to synthesize</div>
    <textarea id="textInput" placeholder="Type something to synthesize..." autofocus></textarea>
    <div class="char-count"><span id="charCount">0</span> characters</div>
    <div class="controls">
      <select id="voiceSelect" title="Voice model">
        <option value="glados">Voice: GLaDOS</option>
      </select>
      <select id="formatSelect">
        <option value="wav">WAV</option>
        <option value="mp3">MP3</option>
        <option value="ogg">OGG</option>
      </select>
      <select id="attitudeSelect" title="Attitude â€” controls vocal delivery style (GLaDOS only)">
        <option value="random">Attitude: Random</option>
        <option value="default">Attitude: Default</option>
      </select>
      <button class="btn btn-primary" id="generateBtn" onclick="ttsGenerate()">Generate</button>
      <div class="status" id="ttsStatus"></div>
    </div>
  </div>
  <div class="card player-section" id="playerCard">
    <div class="player-label" id="playerLabel"></div>
    <audio id="audioPlayer" controls></audio>
  </div>
  <div class="card">
    <div class="section-title">Generated Files</div>
    <div id="fileList"><div class="empty-msg">No files yet.</div></div>
  </div>
</div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 2: Chat                                                    -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-chat" class="tab-content active">
<div class="container">
  <div class="card" style="padding:0.75rem;">
    <div class="chat-messages" id="chatMessages">
      <div class="empty-msg">Send a message to start talking with GLaDOS.</div>
    </div>
    <div class="chat-input-row">
      <input type="text" id="chatInput" placeholder="Type a message..."
             onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();chatSend();}">
      <button class="mic-btn" id="micBtn" onclick="toggleMic()" title="Push to talk">&#127908;</button>
      <button class="btn btn-primary" onclick="chatSend()">Send</button>
    </div>
  </div>
</div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 3: System Control                                          -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-config-system" class="tab-content">
<div class="container" style="position:relative;">
  <div id="controlAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access System Controls</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>
  <div class="card">
    <div class="section-title">Mode Controls</div>

    <div class="mode-row">
      <div>
        <div class="mode-label">Maintenance Mode</div>
        <div class="mode-desc">Route all audio to a single speaker</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="maintToggle" onchange="toggleMaintenance()">
        <span class="toggle-slider"></span>
      </label>
    </div>

    <div class="speaker-select-row" id="speakerRow" style="display:none;">
      <label style="font-size:0.85rem;color:var(--text-dim);">Speaker:</label>
      <select id="speakerSelect">
        <option value="">Loading speakers...</option>
      </select>
    </div>

    <div class="mode-row">
      <div>
        <div class="mode-label">Silent Mode</div>
        <div class="mode-desc">Mute all audio, send HA notifications only</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="silentToggle" onchange="toggleSilent()">
        <span class="toggle-slider"></span>
      </label>
    </div>
  </div>

  <!-- Maintenance Entities — which HA input_* entities back the toggles above -->
  <div class="card">
    <div class="section-title">Maintenance Entities</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      Home Assistant entity IDs that control Maintenance Mode and select the
      speaker used during maintenance. Must exist in your HA setup.
    </div>
    <div id="sysMaintForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveSystemMaint()">Save Maintenance Entities</button>
      <span id="cfg-save-result-sys-maint" class="cfg-result"></span>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Announcement Verbosity</div>
    <div class="mode-desc" style="margin-bottom:12px;">Controls how often GLaDOS adds a sarcastic follow-up comment to announcements. 0% = factual only, 100% = always adds commentary.</div>
    <div id="verbositySliders" style="opacity:0.5;">Loading...</div>
  </div>

  <div class="card">
    <div class="section-title">Startup Speakers</div>
    <div class="mode-desc" style="margin-bottom:12px;">Which speakers GLaDOS announces startup on. Checked = announces. Multiple allowed. Requires restart to apply.</div>
    <div id="startupSpeakers" style="opacity:0.5;">Loading...</div>
    <div id="startupSpeakersStatus" style="font-size:0.75rem;color:var(--orange);margin-top:6px;min-height:1.2em;"></div>
  </div>

  <div class="card">
    <div class="section-title">Display</div>
    <div class="mode-row">
      <div>
        <div class="mode-label">Eye Demo</div>
        <div class="mode-desc">Mood cycle animation on HUB75 panel</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="eyeDemoToggle" onchange="toggleEyeDemo()">
        <span class="toggle-slider"></span>
      </label>
    </div>
  </div>

  <div class="card">
    <div class="section-title">Service Health</div>
    <div class="health-grid" id="healthGrid">
      <div class="health-item">
        <span class="health-dot unknown" id="hd-glados_api"></span>GLaDOS API
        <button class="restart-btn" onclick="restartService('glados_api')" title="Restart glados-api">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-tts"></span>TTS Engine
        <button class="restart-btn" onclick="restartService('tts')" title="Restart glados-tts">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-stt"></span>Speech-to-Text
        <button class="restart-btn" onclick="restartService('stt')" title="Restart glados-stt">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-vision"></span>Vision
        <button class="restart-btn" onclick="restartService('vision')" title="Restart glados-vision">&#10227;</button>
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-ha"></span>Home Assistant
      </div>
      <div class="health-item">
        <span class="health-dot unknown" id="hd-chromadb"></span>ChromaDB Memory
        <button class="restart-btn" onclick="restartService('chromadb')" title="Restart ChromaDB container">&#10227;</button>
      </div>
    </div>
  </div>

  <!-- Authentication & Audit — previously on Integrations; relocated 2026-04-18 -->
  <div class="card">
    <div class="section-title">Authentication &amp; Audit</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      WebUI sign-in enforcement, session timeout, and the utterance/tool
      audit trail. The password itself is set via
      <code>docker exec glados python -m glados.tools.set_password</code> —
      not shown here.
    </div>
    <div id="sysAuthAuditForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveSystemAuthAudit()">Save Authentication &amp; Audit</button>
      <span id="cfg-save-result-sys-authaudit" class="cfg-result"></span>
    </div>
  </div>

  <!-- Robot Nodes card (hidden when robots.enabled is false) -->
  <div class="card" id="robotNodesCard" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div class="section-title" style="margin-bottom:0;">Robot Nodes</div>
      <button class="btn-small" onclick="robotEmergencyStop()" style="font-size:0.8rem;padding:5px 14px;background:#e74c3c;font-weight:600;letter-spacing:0.5px;" title="Emergency stop all nodes">&#9724; E-STOP</button>
    </div>
    <div id="robotNodesList" style="margin-top:10px;font-size:0.85rem;color:var(--text-dim);">Loading...</div>
    <div style="margin-top:12px;display:flex;gap:6px;align-items:center;">
      <input type="text" id="robotNodeUrl" placeholder="http://192.168.100.x" style="flex:1;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
      <input type="text" id="robotNodeName" placeholder="Name (optional)" style="width:140px;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:0.82rem;">
      <button class="btn-small" onclick="robotAddNode()" style="font-size:0.78rem;padding:5px 12px;">Add Node</button>
    </div>
    <div id="robotBotsSection" style="margin-top:12px;display:none;">
      <div style="font-weight:500;font-size:0.82rem;margin-bottom:6px;color:var(--text);">Bots</div>
      <div id="robotBotsList" style="font-size:0.82rem;color:var(--text-dim);"></div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Weather</div>
      <button class="btn-small" id="weatherRefreshBtn" onclick="refreshWeather()" style="font-size:0.75rem;padding:4px 10px;">Refresh</button>
    </div>
    <div id="weatherPanel" style="color:var(--text-dim);font-size:0.85rem;margin-top:8px;">Loading...</div>
  </div>

  <div class="card">
    <div class="section-title">GPU Status</div>
    <div id="gpuPanel" style="font-size:0.85rem;">Loading...</div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div class="section-title" style="margin-bottom:0;">Service Logs</div>
      <div style="display:flex;gap:6px;align-items:center;">
        <select id="logServiceSelect" onchange="loadLogs()" style="background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:0.8rem;">
          <option value="glados-api">GLaDOS API</option>
          <option value="glados-tts">TTS Engine</option>
          <option value="glados-stt">Speech-to-Text</option>
          <option value="glados-vision">Vision</option>
          <option value="glados-tts-ui">WebUI</option>
          <option value="ollama-glados">Ollama GLaDOS</option>
          <option value="ollama-ipex">Ollama IPEX</option>
          <option value="ollama-vision">Ollama Vision</option>
        </select>
        <button class="btn-small" onclick="loadLogs()" style="font-size:0.75rem;padding:4px 10px;">Refresh</button>
        <button class="btn-small" onclick="clearLog()" style="font-size:0.75rem;padding:4px 10px;background:#c0392b;">Clear</button>
      </div>
    </div>
    <div id="logSizeInfo" style="font-size:0.7rem;color:var(--text-dim);margin:6px 0;"></div>
    <pre id="logPanel" style="background:#0d0d0d;border:1px solid #333;border-radius:4px;padding:10px;max-height:400px;overflow:auto;font-size:0.72rem;color:#ccc;white-space:pre-wrap;word-break:break-all;margin-top:6px;">Select a service to view logs</pre>
  </div>

  <!-- Phase 8.9 — Test harness (Advanced). External battery-scoring knobs:
       noise-entity globs the harness must ignore, and whether direction
       matching is required. Exposed publicly at
       /api/test-harness/noise-patterns for the external harness to pull. -->
  <div class="card" data-advanced="true">
    <div class="section-title">Test Harness</div>
    <div class="mode-desc" style="margin-bottom:10px;">
      Battery-scoring knobs consumed by the external test harness
      (<code>C:\\src\\glados-test-battery\\harness.py</code>). Noise-entity globs
      list entities that flip in the background (AC displays, Sonos diagnostics,
      <code>*_button_indication</code>, <code>*_node_identify</code>) and must not
      count toward PASS. Direction-match requires the targeted entity to end in
      the expected state ('on' for "turn on", etc.), not merely "something changed."
      Harness fetches these on run-start from <code>/api/test-harness/noise-patterns</code>
      (public endpoint, no auth).
    </div>
    <div id="testHarnessForm"></div>
    <div class="cfg-save-row">
      <button class="cfg-save-btn" onclick="cfgSaveTestHarness()">Save Test Harness</button>
      <span id="cfg-save-result-test-harness" class="cfg-result"></span>
    </div>
  </div>
</div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- TAB 4: Configuration                                           -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-config" class="tab-content">
<div class="container" style="position:relative;">
  <div id="configAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Configuration</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title" id="cfg-section-label">Configuration</div>
    <!-- In-page tab strip removed in Phase 5; the sidebar Configuration
         submenu drives which section is rendered into cfg-form-area. -->

    <div class="advanced-toggle-row">
      <label>
        <input type="checkbox" id="advancedToggle" onchange="toggleAdvanced()">
        Show Advanced Settings
      </label>
    </div>

    <!-- Form sections (generated dynamically) -->
    <div id="cfg-form-area" style="min-height:200px;">
      <div style="color:var(--text-dim);padding:20px;text-align:center;">Select a section or loading...</div>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div style="display:flex;gap:12px;align-items:center;">
      <button class="btn" onclick="cfgReload()" style="background:#555;">Reload from Disk</button>
      <span id="cfg-status" style="color:var(--text-dim);font-size:0.85em;"></span>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="section-title">Audio Storage</div>
    <div id="audioStatsPanel" style="font-size:0.85rem;color:var(--text-dim);">Loading...</div>
  </div>

</div>
</div>

<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<!-- ================================================================ -->
<!-- CONFIGURATION > MEMORY (Phase 5)                                   -->
<!-- ================================================================ -->
<div id="tab-config-memory" class="tab-content">
<div class="container" style="position:relative;">
  <div id="memoryAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Memory</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title">Memory configuration</div>
    <div class="mem-radio-row">
      <div class="mode-label" style="margin-bottom:4px;">Default status for new passive facts</div>
      <label><input type="radio" name="memDefaultStatus" value="approved" onchange="memSaveDefaultStatus('approved')"> Approved (enters RAG immediately)</label>
      <label><input type="radio" name="memDefaultStatus" value="pending" onchange="memSaveDefaultStatus('pending')"> Pending (manual review)</label>
      <div class="mode-desc" style="margin-top:4px;">
        Stored as <code>memory.passive_default_status</code>. Approved = reinforcement-on-repetition via ChromaDB similarity dedup.
        Pending = facts queue below for operator approval before entering RAG.
      </div>
    </div>
    <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <button class="btn-small" onclick="memSweepRetention()">Sweep retention now</button>
      <span id="memRetentionStatus" style="font-size:0.78rem;color:var(--text-dim);"></span>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
      <div class="section-title" style="margin-bottom:0;">Long-term facts</div>
      <div style="display:flex;gap:6px;align-items:center;">
        <input id="memSearchInput" type="text" placeholder="Search..." oninput="memSearchDebounced()">
        <button class="btn-small" onclick="memShowAddForm()">+ Add</button>
      </div>
    </div>
    <div id="memAddForm" style="display:none;margin-top:12px;">
      <textarea id="memAddText" placeholder="The operator prefers the living room lights at 40% in the evening"></textarea>
      <div style="margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <label style="font-size:0.82rem;color:var(--text-dim);">Importance:
          <input id="memAddImportance" type="number" step="0.05" min="0" max="1" value="0.9" style="width:70px;margin-left:4px;background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:3px 6px;">
        </label>
        <button class="btn-small" onclick="memAddFact()">Save</button>
        <button class="btn-small" onclick="memHideAddForm()" style="background:#555;">Cancel</button>
      </div>
    </div>
    <div id="memFactsList" style="margin-top:12px;">Loading...</div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Recent activity</div>
      <button class="btn-small" onclick="memLoadRecent()">Refresh</button>
    </div>
    <div class="mode-desc" style="margin-top:4px;">Last 10 facts added or reinforced.</div>
    <div id="memRecentList" style="margin-top:10px;">Loading...</div>
  </div>

  <div class="card" id="memPendingCard" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div class="section-title" style="margin-bottom:0;">Pending review</div>
      <button class="btn-small" onclick="memLoadPending()">Refresh</button>
    </div>
    <div class="mode-desc" style="margin-top:4px;">Facts auto-extracted but not yet approved for RAG.</div>
    <div id="memPendingList" style="margin-top:10px;">Loading...</div>
  </div>
</div>
</div>

<!-- TAB: TRAINING MONITOR                                          -->
<!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
<div id="tab-training" class="tab-content">
<div class="container" style="position:relative;">
  <div id="trainingAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to access Training</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <!-- Status Cards -->
  <div class="train-status-row">
    <div class="train-card">
      <div class="train-card-label">Status</div>
      <div class="train-card-value"><span id="trainRunning" class="train-dot train-dot-off"></span> <span id="trainRunningText">Unknown</span></div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Fine-Tune Epoch</div>
      <div class="train-card-value" id="trainEpoch">--</div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Generator Loss</div>
      <div class="train-card-value" id="trainGenLoss">--</div>
    </div>
    <div class="train-card">
      <div class="train-card-label">Discriminator Loss</div>
      <div class="train-card-value" id="trainDiscLoss">--</div>
    </div>
  </div>

  <!-- Action Buttons -->
  <div class="card" style="margin-top:12px;">
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="trainingSnapshot()" id="btnSnapshot">Snapshot &amp; Deploy</button>
      <button class="btn btn-danger" onclick="trainingStop()" id="btnTrainStop">Stop Training</button>
      <span id="snapshotStatus" style="font-size:0.85rem;color:var(--text-dim);"></span>
    </div>
  </div>

  <!-- Loss Chart -->
  <div class="card" style="margin-top:12px;">
    <div class="section-title">Loss Curves</div>
    <div class="train-chart-wrap">
      <canvas id="trainingChart"></canvas>
    </div>
  </div>

  <!-- Training Log -->
  <div class="card" style="margin-top:12px;">
    <div class="section-title">Training Log</div>
    <pre id="trainingLog" class="train-log">Loading...</pre>
  </div>

</div>
</div>

<!-- ================================================================ -->
<!-- CONFIGURATION > LOGS (Phase 6 follow-up)                           -->
<!-- ================================================================ -->
<div id="tab-config-logs" class="tab-content">
<div class="container" style="position:relative;">
  <div id="logsAuthOverlay" class="auth-overlay" style="display:none;">
    <div class="auth-overlay-icon">&#128274;</div>
    <div class="auth-overlay-text">Authentication required to view Logs</div>
    <a href="/login" class="auth-overlay-btn">Sign In</a>
  </div>

  <div class="card">
    <div class="section-title">Logs</div>
    <div class="cfg-section-desc" style="margin-bottom:12px;">
      Read-only tail of recent log content. Choose a source and how many lines back. Toggle Auto to poll the view every 10 seconds while this tab is open.
    </div>
    <div class="logs-controls">
      <label class="logs-ctrl">
        <span>Source</span>
        <select id="logsSource" onchange="logsOnSourceChange()"></select>
      </label>
      <label class="logs-ctrl">
        <span>Lines</span>
        <select id="logsLines" onchange="logsRefresh()">
          <option value="100">100</option>
          <option value="500" selected>500</option>
          <option value="1000">1000</option>
          <option value="2000">2000</option>
          <option value="5000">5000</option>
        </select>
      </label>
      <label class="logs-ctrl">
        <span>Filter</span>
        <select id="logsFilter" onchange="logsRerender()">
          <option value="all" selected>All</option>
          <option value="warn">Warnings and errors</option>
          <option value="error">Errors only</option>
        </select>
      </label>
      <button class="btn-small" onclick="logsRefresh()">Refresh</button>
      <label class="logs-ctrl logs-auto">
        <input type="checkbox" id="logsAuto" onchange="logsToggleAuto()">
        <span>Auto-refresh (10 s)</span>
      </label>
      <span id="logsStatus" class="logs-status"></span>
    </div>
    <div id="logsSourceDesc" class="logs-source-desc"></div>
    <div class="logs-viewport">
      <pre id="logsBody" class="logs-body">Select a source and click Refresh.</pre>
    </div>
  </div>
</div>
</div>

</main>

<!-- Toast -->
<div id="toastStack" class="toast-stack"></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="/static/ui.js"></script>
</body>
</html>
"""

# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Phase 8.12 — HTTP→HTTPS 301 redirect listener. Tiny ThreadingHTTPServer
# on a separate port that answers every request with a 301 to the
# HTTPS port. Disabled when ``WEBUI_HTTP_REDIRECT_PORT`` env var is
# unset or set to 0 — so nothing changes for existing deployments
# unless the operator opts in.
from http.server import BaseHTTPRequestHandler as _BaseHTTPHandler


def _http_redirect_port() -> int:
    raw = os.environ.get("WEBUI_HTTP_REDIRECT_PORT", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def _make_redirect_handler(https_port: int) -> type[_BaseHTTPHandler]:
    class _RedirectHandler(_BaseHTTPHandler):
        def _redirect(self) -> None:
            host = self.headers.get("Host", "").split(":")[0] or "localhost"
            location = f"https://{host}:{https_port}{self.path}"
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None: self._redirect()
        def do_POST(self) -> None: self._redirect()
        def do_HEAD(self) -> None: self._redirect()
        def do_PUT(self) -> None: self._redirect()
        def do_DELETE(self) -> None: self._redirect()
        def do_OPTIONS(self) -> None: self._redirect()

        def log_message(self, fmt: str, *args) -> None:  # silence access log
            return

    return _RedirectHandler


def _start_http_redirect_thread(
    redirect_port: int, https_port: int, host: str = "0.0.0.0",
) -> None:
    """Start the redirect listener on a daemon thread. Swallows
    ``OSError`` (port in use) with a WARN log — the HTTPS listener
    is the primary, a missing redirect is non-fatal."""
    handler_cls = _make_redirect_handler(https_port)
    try:
        srv = ThreadingHTTPServer((host, redirect_port), handler_cls)
    except OSError as exc:
        logger.warning(
            "HTTP redirect listener failed to bind {}:{}: {}",
            host, redirect_port, exc,
        )
        return
    t = threading.Thread(
        target=srv.serve_forever,
        name="http-redirect",
        daemon=True,
    )
    t.start()
    logger.info(
        "HTTP redirect listener on {}:{} → https://<host>:{}/",
        host, redirect_port, https_port,
    )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    # Enable HTTPS if cert files exist
    use_ssl = SSL_CERT and SSL_KEY and SSL_CERT.exists() and SSL_KEY.exists()
    if use_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(SSL_CERT), keyfile=str(SSL_KEY))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        _tls_context = ctx  # Phase 8.12: expose for live reload
        proto = "https"
        print(f"  SSL cert:    {SSL_CERT}")
    else:
        proto = "http"
        if SSL_CERT:
            print(f"  WARNING: SSL cert not found at {SSL_CERT} â€” falling back to HTTP")

    print(f"GLaDOS Control Panel running at {proto}://0.0.0.0:{PORT}")
    print(f"  TTS output:  {OUTPUT_DIR}")
    print(f"  Chat audio:  {CHAT_AUDIO_DIR}")
    print(f"  HA URL:      {HA_URL}")

    # Phase 8.12: optional HTTP→HTTPS 301 redirect listener. Only starts
    # if the env var is set AND TLS is actually enabled (no point
    # redirecting to HTTPS when the main listener is plaintext).
    rp = _http_redirect_port()
    if rp and use_ssl:
        _start_http_redirect_thread(rp, PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


def run_webui(host: str = "0.0.0.0", port: int = 8052) -> None:
    """Start the WebUI admin panel. Called from glados.server as a thread target.

    Blocks until the server is stopped. Designed to run in a daemon thread
    alongside the API server.
    """
    global _tls_context
    server = ThreadingHTTPServer((host, port), Handler)

    use_ssl = SSL_CERT and SSL_KEY and SSL_CERT.exists() and SSL_KEY.exists()
    if use_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(SSL_CERT), keyfile=str(SSL_KEY))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        _tls_context = ctx  # Phase 8.12: live-reload hook
        proto = "https"
    else:
        proto = "http"

    logger.info("GLaDOS WebUI listening on {}://{}:{}", proto, host, port)

    # Phase 8.12: start HTTP redirect listener if env var set AND TLS
    # active. Cheap and safe — silently skipped if plaintext.
    rp = _http_redirect_port()
    if rp and use_ssl:
        _start_http_redirect_thread(rp, port, host=host)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
