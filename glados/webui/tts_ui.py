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
            self._send_json(200, {"ok": True, "message": "Certificate uploaded. Restart container to activate HTTPS."})
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
                self._send_json(200, {"ok": True, "message": "Certificate issued/renewed successfully. Restart container to activate.", "log": combined})
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
<style>
/* â”€â”€ Variables â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
:root {
  --bg-dark: #1a1a1e;
  --bg-card: #242429;
  --bg-input: #2e2e35;
  --bg-sidebar: #16161a;
  --border: #3a3a42;
  --text: #e0e0e0;
  --text-dim: #888;
  --text-muted: #666;
  --orange: #f4a623;
  --orange-hover: #f5b84d;
  --orange-dim: #c4841a;
  --red: #e05555;
  --red-hover: #e87777;
  --green: #4caf50;
  --blue: #4a9eff;
  /* Phase 5: distinctive display face for headings + branding.
     Body text stays system-ui for readability. Swapping this var is
     all it takes to try a different display font. */
  --font-display: 'Major Mono Display', 'Consolas', monospace;
  --sidebar-w: 220px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: var(--bg-dark);
  color: var(--text);
  display: flex;
}

/* â”€â”€ Sidebar (desktop) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.sidebar {
  width: var(--sidebar-w);
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0; left: 0; bottom: 0;
  z-index: 100;
  overflow-y: auto;
}
.sidebar-brand {
  padding: 1.25rem 1rem;
  font-family: var(--font-display);
  font-size: 1.15rem;
  font-weight: 400;
  color: var(--orange);
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.sidebar-brand span { color: var(--text-dim); font-weight: 400; font-size: 0.75rem; letter-spacing: 0.02em; }
/* Engine status dot in the brand header. Polled by pollEngineStatus(). */
.engine-status-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--text-dim);
  flex-shrink: 0;
  transition: background 0.2s;
}
.engine-status-dot.running { background: var(--green); }
.engine-status-dot.starting { background: var(--orange); }
.engine-status-dot.stopping { background: var(--red); }
.nav-items { flex: 1; padding: 0.5rem 0; }
.nav-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.7rem 1rem;
  color: var(--text-dim);
  text-decoration: none;
  font-size: 0.88rem;
  font-weight: 500;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: all 0.15s;
  user-select: none;
}
.nav-item:hover { color: var(--text); background: rgba(255,255,255,0.03); }
.nav-item.active {
  color: var(--orange);
  background: rgba(244,166,35,0.08);
  border-left-color: var(--orange);
}
.nav-item svg { width: 18px; height: 18px; flex-shrink: 0; opacity: 0.7; }
.nav-item.active svg { opacity: 1; }
.nav-item .lock-icon { margin-left: auto; font-size: 0.7rem; opacity: 0.5; }
/* ── Phase 5: hierarchical nav (Configuration as parent) ── */
.nav-parent { /* same layout rules as .nav-item; combined via class list */ }
.nav-parent .nav-caret {
  margin-left: auto;
  font-size: 0.7rem;
  transition: transform 0.2s ease;
  display: inline-block;
}
.nav-parent.open .nav-caret { transform: rotate(90deg); }
.nav-children {
  max-height: 0;
  overflow: hidden;
  transition: max-height 0.2s ease;
  background: rgba(0,0,0,0.15);
}
.nav-parent.open + .nav-children { max-height: 800px; }
.nav-children .nav-item {
  padding-left: 2.75rem;
  font-size: 0.82rem;
  gap: 0.5rem;
}
.nav-children .nav-item svg { width: 14px; height: 14px; }
.sidebar-footer {
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--border);
}
.sidebar-footer a {
  color: var(--text-dim);
  text-decoration: none;
  font-size: 0.8rem;
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.4rem 0;
  transition: color 0.15s;
}
.sidebar-footer a:hover { color: var(--orange); }

/* â”€â”€ Top bar (mobile) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.topbar {
  display: none;
  background: var(--bg-sidebar);
  border-bottom: 1px solid var(--border);
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  position: fixed;
  top: 0;
  z-index: 100;
}
.topbar-inner {
  display: flex; align-items: center;
  padding: 0.5rem; gap: 0.25rem;
  min-width: max-content;
}
.topbar-brand {
  font-size: 1rem; font-weight: 700; color: var(--orange);
  padding: 0 0.75rem; white-space: nowrap;
}
.topbar .nav-item {
  padding: 0.5rem 0.75rem;
  border-left: none;
  border-bottom: 2px solid transparent;
  white-space: nowrap;
  font-size: 0.8rem;
}
.topbar .nav-item.active {
  border-bottom-color: var(--orange);
  border-left-color: transparent;
}

/* â”€â”€ Main content â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.main-content {
  margin-left: var(--sidebar-w);
  flex: 1;
  min-height: 100vh;
  padding: 1.5rem;
  max-width: 900px;
}

/* â”€â”€ Tabs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.tab-content { display: none; }
.tab-content.active { display: block; }
.container {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

/* â”€â”€ Cards â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem;
  position: relative;
}

/* â”€â”€ Inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
textarea, input[type="text"], input[type="number"], input[type="password"] {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 0.95rem;
  padding: 0.75rem;
  font-family: inherit;
}
textarea { min-height: 120px; resize: vertical; }
textarea:focus, input:focus { outline: none; border-color: var(--orange); }
select {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 0.9rem;
  padding: 0.5rem 0.75rem;
  cursor: pointer;
}
select:focus { outline: none; border-color: var(--orange); }

/* â”€â”€ Buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.btn {
  padding: 0.55rem 1.5rem;
  border: none;
  border-radius: 6px;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-primary { background: var(--orange); color: #111; }
.btn-primary:hover:not(:disabled) { background: var(--orange-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-danger {
  background: transparent; color: var(--red);
  border: 1px solid var(--red);
  padding: 0.3rem 0.6rem; font-size: 0.8rem;
}
.btn-danger:hover { background: var(--red); color: #fff; }
.btn-small {
  background: transparent; color: var(--orange);
  border: 1px solid var(--orange-dim);
  padding: 0.3rem 0.6rem; font-size: 0.8rem;
}
.btn-small:hover { background: var(--orange-dim); color: #fff; }
.controls {
  display: flex; gap: 0.75rem; margin-top: 0.75rem;
  align-items: center; flex-wrap: wrap;
}

/* â”€â”€ Status / Spinner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.status {
  font-size: 0.85rem; color: var(--text-dim);
  margin-left: 0.5rem;
  display: flex; align-items: center; gap: 0.4rem;
}
.spinner {
  display: inline-block; width: 16px; height: 16px;
  border: 2px solid var(--border); border-top-color: var(--orange);
  border-radius: 50%; animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* â”€â”€ TTS Player / Files â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.player-section { display: none; }
.player-section.visible { display: block; }
.player-section audio { width: 100%; margin-top: 0.5rem; border-radius: 6px; }
.player-label { font-size: 0.85rem; color: var(--orange); font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th {
  text-align: left; color: var(--text-dim); font-weight: 600;
  padding: 0.5rem 0.4rem; border-bottom: 1px solid var(--border);
  font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
}
td { padding: 0.5rem 0.4rem; border-bottom: 1px solid var(--bg-input); vertical-align: middle; }
tr:hover td { background: rgba(244,166,35,0.04); }
.file-name { font-weight: 500; word-break: break-all; }
.file-size, .file-date { color: var(--text-dim); white-space: nowrap; }
.file-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
a.dl-link {
  color: var(--orange); text-decoration: none; font-size: 0.8rem;
  border: 1px solid var(--orange-dim);
  padding: 0.3rem 0.6rem; border-radius: 6px; transition: background 0.15s;
}
a.dl-link:hover { background: var(--orange-dim); color: #fff; }
.empty-msg { text-align: center; color: var(--text-dim); padding: 2rem; font-style: italic; }
.section-title {
  font-family: var(--font-display);
  font-size: 0.92rem;
  font-weight: 400;
  letter-spacing: 0.04em;
  margin-bottom: 0.75rem;
  color: var(--text);
}
.char-count { font-size: 0.8rem; color: var(--text-dim); text-align: right; margin-top: 0.25rem; }

/* â”€â”€ Chat Tab â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.chat-messages {
  /* Dynamic height: fill available vertical space above the input row.
     260px budget covers .main-content padding (1.5rem top/bottom),
     the chat card padding, the input row (~52px), the message's own
     margin-bottom, and a small safety gap. Clamped to a readable
     minimum for short viewports. */
  height: calc(100vh - 260px);
  min-height: 320px;
  overflow-y: auto;
  display: flex; flex-direction: column; gap: 0.75rem;
  padding: 0.5rem; margin-bottom: 0.75rem;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
.chat-msg {
  max-width: 85%;
  padding: 0.6rem 0.9rem;
  border-radius: 12px;
  font-size: 0.9rem;
  line-height: 1.45;
  word-wrap: break-word;
}
.chat-msg.user {
  align-self: flex-end;
  background: var(--orange-dim);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.chat-msg.assistant {
  align-self: flex-start;
  background: var(--bg-input);
  color: var(--text);
  border-bottom-left-radius: 4px;
}
.chat-msg.assistant .msg-label {
  font-size: 0.75rem; color: var(--orange); font-weight: 600;
  margin-bottom: 0.25rem;
}
.chat-msg audio {
  width: 100%; margin-top: 0.4rem; height: 32px;
  border-radius: 4px;
}
.chat-metrics {
  display: flex; flex-wrap: wrap; gap: 0.6rem;
  margin-top: 0.4rem; padding-top: 0.3rem;
  border-top: 1px solid rgba(255,255,255,0.06);
  font-size: 0.68rem; color: var(--text-dim); opacity: 0.7;
}
.chat-metrics span::before {
  content: '\25CF'; margin-right: 0.25rem; font-size: 0.5rem;
  vertical-align: middle;
}
.chat-metrics .emotion-metric {
  color: var(--orange); opacity: 0.9;
  cursor: help;
}
.chat-metrics .emotion-metric::before {
  content: ''; /* suppress bullet â€” emotion has its own icon */
}
.chat-msg .thinking {
  color: var(--text-dim); font-style: italic;
}
.stream-cursor {
  display: inline-block;
  color: var(--orange);
  animation: blink-cursor 0.7s step-end infinite;
  font-weight: bold;
  margin-left: 2px;
}
@keyframes blink-cursor {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
.chat-input-row {
  display: flex; gap: 0.5rem; align-items: flex-end;
}
.chat-input-row input[type="text"] {
  flex: 1; padding: 0.6rem 0.75rem;
}
.mic-btn {
  width: 40px; height: 40px;
  border-radius: 50%;
  border: 2px solid var(--border);
  background: var(--bg-input);
  color: var(--text-dim);
  font-size: 1.1rem;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
  flex-shrink: 0;
}
.mic-btn:hover { border-color: var(--orange); color: var(--orange); }
.mic-btn.recording {
  border-color: var(--red);
  background: rgba(224,85,85,0.15);
  color: var(--red);
  animation: pulse 1s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(224,85,85,0.3); }
  50% { box-shadow: 0 0 0 8px rgba(224,85,85,0); }
}

/* â”€â”€ Control Panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.mode-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--bg-input);
}
.mode-row:last-child { border-bottom: none; }
.mode-label { font-weight: 500; }
.mode-desc { font-size: 0.8rem; color: var(--text-dim); margin-top: 0.15rem; }
.toggle {
  position: relative; width: 52px; height: 28px;
  cursor: pointer; flex-shrink: 0;
}
.toggle input { display: none; }
.toggle-slider {
  position: absolute; inset: 0;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 14px; transition: 0.3s;
}
.toggle-slider::before {
  content: ''; position: absolute;
  width: 22px; height: 22px;
  left: 2px; bottom: 2px;
  background: var(--text-dim);
  border-radius: 50%; transition: 0.3s;
}
.toggle input:checked + .toggle-slider {
  background: var(--orange-dim); border-color: var(--orange);
}
.toggle input:checked + .toggle-slider::before {
  transform: translateX(24px);
  background: var(--orange);
}
.speaker-select-row {
  display: flex; gap: 0.5rem; align-items: center;
  padding: 0.5rem 0; margin-left: 1rem;
}
.speaker-select-row select { flex: 1; }
.health-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
}
.health-item {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.4rem 0;
  font-size: 0.85rem;
}
.health-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.health-dot.ok { background: var(--green); }
.health-dot.err { background: var(--red); }
.health-dot.unknown { background: var(--text-dim); }
.restart-btn {
  margin-left: auto;
  background: transparent;
  border: 1px solid var(--text-dim);
  color: var(--text-dim);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 0.8rem;
  cursor: pointer;
  transition: all 0.2s;
}
.restart-btn:hover {
  border-color: var(--orange);
  color: var(--orange);
}
.restart-btn.restarting {
  border-color: var(--orange);
  color: var(--orange);
  animation: spin 1s linear infinite;
}

/* â”€â”€ Weather / GPU â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.weather-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.weather-item { background: #1a1a2e; padding: 10px 14px; border-radius: 6px; }
.weather-label { font-size: 0.75rem; color: var(--text-dim); margin-bottom: 2px; }
.weather-value { font-size: 1.1rem; color: var(--text); }
.weather-value.highlight { color: var(--orange); }
.gpu-card {
  background: #1a1a2e;
  padding: 12px 16px;
  border-radius: 6px;
  margin-bottom: 8px;
}
.gpu-name { font-weight: 600; color: var(--orange); margin-bottom: 6px; font-size: 0.85rem; }
.gpu-bar-bg {
  background: #333;
  border-radius: 3px;
  height: 8px;
  margin-top: 4px;
  overflow: hidden;
}
.gpu-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.5s;
}
.gpu-bar-fill.mem { background: #4caf50; }
.gpu-bar-fill.hot { background: #ff9800; }
.gpu-bar-fill.crit { background: #f44336; }
.gpu-stat { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-dim); margin-top: 4px; }

/* -- Memory page (Phase 5) -- */
.mem-fact, .mem-recent, .mem-pending {
  padding: 10px 2px;
  border-bottom: 1px solid var(--border);
}
.mem-fact:last-child, .mem-recent:last-child, .mem-pending:last-child { border-bottom: none; }
.mem-fact-text { font-size: 0.92rem; color: var(--text); }
.mem-fact-meta { font-size: 0.72rem; color: var(--text-dim); margin-top: 3px; letter-spacing: 0.01em; }
.mem-fact-actions { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
#memAddForm textarea {
  width: 100%;
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 8px 10px;
  font-size: 0.88rem;
  resize: vertical;
  min-height: 60px;
}
#memSearchInput {
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 0.82rem;
  min-width: 160px;
}
.mem-radio-row label { margin-right: 16px; font-size: 0.85rem; }
.mem-recent .mem-bump { color: var(--orange); font-weight: 500; }

/* â”€â”€ Config Styles â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.cfg-section-header {
  margin-bottom: 1.25rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--border);
}
.cfg-section-title {
  font-family: var(--font-display);
  font-size: 1.05rem;
  font-weight: 400;
  letter-spacing: 0.04em;
  color: var(--text);
  margin-bottom: 0.25rem;
}
.cfg-section-desc {
  font-size: 0.8rem;
  color: var(--text-dim);
}
.cfg-subsection-title {
  /* Merged Phase 6 pages (e.g. Audio & Speakers) stack two backing
     sections under one header; this subsection title separates them. */
  font-family: var(--font-display);
  font-size: 0.95rem;
  font-weight: 400;
  letter-spacing: 0.04em;
  color: var(--orange);
  margin: 0.25rem 0 0.75rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--border);
}
.cfg-placeholder-card {
  /* Read-only "coming soon" cards on Integrations for MQTT / Media Stack
     while those features are still on the roadmap. Kept visually quieter
     than real config cards so operators don't mistake them for active
     integrations. */
  background: var(--bg-input);
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 12px 16px;
  margin-top: 12px;
  opacity: 0.8;
}
.cfg-placeholder-title {
  font-size: 0.95rem;
  color: var(--text);
  margin-bottom: 4px;
}
.cfg-placeholder-desc {
  font-size: 0.8rem;
  color: var(--text-dim);
  line-height: 1.4;
}
.cfg-placeholder-tag {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 6px;
  font-size: 0.7rem;
  background: rgba(255,255,255,0.06);
  border: 1px solid var(--border);
  border-radius: 3px;
  color: var(--text-dim);
  letter-spacing: 0.03em;
  text-transform: uppercase;
}

/* â”€â”€ Logs page â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.logs-controls {
  display: flex;
  gap: 14px;
  align-items: flex-end;
  flex-wrap: wrap;
  margin-bottom: 10px;
  padding: 10px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.85rem;
}
.logs-ctrl {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.logs-ctrl > span {
  font-size: 0.7rem;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.logs-ctrl select {
  padding: 4px 8px;
  background: #0d0d0d;
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 0.82rem;
  min-width: 160px;
}
.logs-ctrl.logs-auto {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  padding-bottom: 3px;
}
.logs-ctrl.logs-auto > span {
  text-transform: none;
  font-size: 0.82rem;
  color: var(--text);
  letter-spacing: 0;
}
.logs-status {
  font-size: 0.75rem;
  color: var(--text-dim);
  margin-left: auto;
  font-family: var(--font-mono);
  padding-bottom: 3px;
}
.logs-source-desc {
  font-size: 0.78rem;
  color: var(--text-dim);
  margin-bottom: 10px;
  padding-left: 4px;
}
.logs-viewport {
  background: #0a0a0a;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: auto;
  max-height: 620px;
}
.logs-body {
  margin: 0;
  padding: 10px 14px;
  font-family: var(--font-mono);
  font-size: 0.74rem;
  line-height: 1.45;
  color: #ccc;
  white-space: pre-wrap;
  word-break: break-all;
  tab-size: 4;
}
.logs-body .log-error   { color: #ff6d6d; }
.logs-body .log-warn    { color: #ffc15c; }
.logs-body .log-info    { color: #8ec2ff; }
.logs-body .log-success { color: #7ed17e; }
.logs-body .log-dim     { color: #666; }
.cfg-tab-btn {
  background: #222;
  border: 1px solid #444;
  color: var(--text-dim);
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.8rem;
  transition: all 0.2s;
}
.cfg-tab-btn:hover { border-color: var(--orange); color: var(--text); }
.cfg-tab-btn.active { background: var(--orange); color: #fff; border-color: var(--orange); }
.cfg-field { margin-bottom: 14px; }
.cfg-field-label {
  display: block;
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--text);
  margin-bottom: 2px;
}
.cfg-field-desc {
  font-size: 0.73rem;
  color: var(--text-muted);
  margin-bottom: 4px;
}
.cfg-field-hint {
  font-size: 0.7rem;
  color: var(--text-muted);
  font-style: italic;
  margin-top: 2px;
}
.cfg-field input, .cfg-field select {
  width: 100%;
  padding: 7px 10px;
  background: #111;
  border: 1px solid #444;
  border-radius: 4px;
  color: var(--text);
  font-size: 0.85rem;
  font-family: 'Consolas', monospace;
}
.cfg-field input:focus { border-color: var(--orange); outline: none; }
.cfg-group {
  border: 1px solid #333;
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 14px;
}
.cfg-group-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--orange);
  margin-bottom: 10px;
}
.cfg-save-row {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #333;
}
.cfg-save-btn {
  background: var(--orange);
  color: #fff;
  border: none;
  padding: 8px 20px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.85rem;
}
.cfg-save-btn:hover { background: #e55a00; }
.cfg-save-btn:disabled { background: #555; cursor: not-allowed; }
.cfg-result { font-size: 0.8rem; }
.cfg-result.ok { color: #4caf50; }
.cfg-result.err { color: #ff4444; }
.cfg-textarea {
  width: 100%;
  min-height: 400px;
  background: #0d0d0d;
  border: 1px solid #444;
  border-radius: 4px;
  color: #e0e0e0;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.8rem;
  padding: 10px;
  line-height: 1.5;
  resize: vertical;
  tab-size: 2;
}
.cfg-textarea:focus { border-color: var(--orange); outline: none; }
.cfg-file-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}
.cfg-file-tab {
  background: #222;
  border: 1px solid #333;
  color: var(--text-dim);
  padding: 4px 10px;
  border-radius: 3px;
  cursor: pointer;
  font-size: 0.75rem;
}
.cfg-file-tab:hover { color: var(--text); }
.cfg-file-tab.active { background: #333; color: var(--orange); border-color: var(--orange); }

/* â”€â”€ Advanced toggle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.advanced-toggle-row {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 16px;
  padding: 8px 12px;
  background: var(--bg-input);
  border-radius: 6px;
  font-size: 0.82rem;
  color: var(--text-dim);
}
.advanced-toggle-row label { cursor: pointer; display: flex; align-items: center; gap: 6px; }
[data-advanced="true"] { display: none; }
body.show-advanced [data-advanced="true"] { display: block; }
body.show-advanced .cfg-group[data-advanced="true"] { display: block; }
body.show-advanced .cfg-field[data-advanced="true"] { display: block; }
body.show-advanced .service-card[data-advanced="true"] { display: block; }

/* â”€â”€ Service cards â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.service-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.service-card {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.service-card-header {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 8px;
}
.service-card-name {
  font-weight: 600;
  font-size: 0.88rem;
}
.svc-health-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--text-dim);
  flex-shrink: 0;
}
.svc-health-dot.ok { background: var(--green); }
.svc-health-dot.err { background: var(--red); }
/* Phase 5 service auto-discovery */
.svc-url-row { display: flex; gap: 6px; align-items: center; }
.svc-url-row input { flex: 1; }
.svc-discover-btn {
  background: #2e2e35;
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 5px 10px;
  font-size: 0.78rem;
  cursor: pointer;
  white-space: nowrap;
}
.svc-discover-btn:hover { background: #3a3a42; color: var(--orange); }
.svc-discover-status {
  font-size: 0.72rem;
  color: var(--text-dim);
  min-width: 70px;
}
.svc-discover-status.ok { color: var(--green); }
.svc-discover-status.err { color: var(--red); }
.svc-dropdown {
  width: 100%;
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 0.84rem;
}

/* â”€â”€ Attitudes table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.att-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.att-table th {
  text-align: left; color: var(--text-dim); font-weight: 600;
  padding: 6px 8px; border-bottom: 1px solid var(--border);
  font-size: 0.75rem; text-transform: uppercase;
}
.att-table td { padding: 6px 8px; border-bottom: 1px solid var(--bg-input); }
.att-table .tag-cell {
  font-family: 'Consolas', monospace; font-size: 0.78rem; color: var(--orange);
}
.att-table .tts-cell {
  font-family: 'Consolas', monospace; font-size: 0.75rem; color: var(--text-dim);
}

/* â”€â”€ Preprompt pairs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.preprompt-pair {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
}
.preprompt-role {
  font-size: 0.75rem; font-weight: 600;
  color: var(--orange); text-transform: uppercase;
  margin-bottom: 4px;
}
.preprompt-text {
  width: 100%; min-height: 60px;
  background: #0d0d0d; border: 1px solid #444;
  border-radius: 4px; color: var(--text);
  font-size: 0.82rem; padding: 8px;
  resize: vertical; font-family: inherit;
}

/* â”€â”€ Auth overlay â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.auth-overlay {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(26,26,30,0.95);
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 1rem;
  border-radius: 8px;
  min-height: 300px;
}
.auth-overlay-icon { font-size: 2.5rem; opacity: 0.5; }
.auth-overlay-text { color: var(--text-dim); font-size: 0.95rem; }
.auth-overlay-btn {
  background: var(--orange); color: #111;
  border: none; padding: 0.5rem 1.5rem;
  border-radius: 6px; font-weight: 600;
  cursor: pointer; font-size: 0.9rem;
  text-decoration: none;
}
.auth-overlay-btn:hover { background: var(--orange-hover); }

/* â”€â”€ Toast â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.toast-stack {
  position: fixed;
  bottom: 1.5rem; right: 1.5rem;
  display: flex; flex-direction: column-reverse; gap: 0.5rem;
  z-index: 9999;
  pointer-events: none;
  max-width: min(420px, calc(100vw - 3rem));
}
.toast {
  padding: 0.7rem 1.1rem;
  border-radius: 6px;
  font-size: 0.84rem; font-weight: 500;
  box-shadow: 0 4px 14px rgba(0,0,0,0.35);
  opacity: 0;
  transform: translateY(6px);
  transition: opacity 0.25s ease, transform 0.25s ease;
  pointer-events: auto;
}
.toast.visible { opacity: 1; transform: translateY(0); }
.toast.success { background: var(--green); color: #fff; }
.toast.error   { background: var(--red);   color: #fff; }
.toast.info    { background: #2e4262;      color: #e7ecff; }

/* â”€â”€ Responsive â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
@media (max-width: 768px) {
  .sidebar { display: none; }
  .topbar { display: block; }
  .main-content {
    margin-left: 0;
    margin-top: 54px;
    padding: 1rem;
    max-width: 100%;
  }
  .service-grid { grid-template-columns: 1fr; }
  .health-grid { grid-template-columns: 1fr; }
  .weather-grid { grid-template-columns: 1fr; }
  .train-status-row { grid-template-columns: 1fr 1fr; }
}

/* â”€â”€ Training Monitor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.train-status-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 0;
}
.train-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  text-align: center;
}
.train-card-label {
  font-size: 0.75rem;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}
.train-card-value {
  font-size: 1.3rem;
  font-weight: 600;
  color: var(--text);
}
.train-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}
.train-dot-on { background: #22c55e; box-shadow: 0 0 6px #22c55e80; }
.train-dot-off { background: #666; }
.train-chart-wrap {
  position: relative;
  height: 300px;
  width: 100%;
}
.train-log {
  background: #0a0a0a;
  color: #ccc;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.78rem;
  line-height: 1.4;
  padding: 12px;
  border-radius: 6px;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.btn-danger {
  background: #dc2626 !important;
  border-color: #dc2626 !important;
}
.btn-danger:hover {
  background: #b91c1c !important;
}
@keyframes pulse-snap {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.snap-running { animation: pulse-snap 1.5s infinite; color: var(--accent) !important; }
</style>
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
<script>
/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Authentication & Auth Gating
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _isAuthenticated = false;

async function checkAuth() {
  try {
    const r = await fetch('/api/auth/status');
    const data = await r.json();
    _isAuthenticated = data.authenticated === true;
  } catch(e) {
    _isAuthenticated = false;
  }
  updateAuthUI();
}

function updateAuthUI() {
  // Update sidebar/topbar auth link
  const sidebarLink = document.getElementById('authLinkSidebar');
  if (sidebarLink) {
    sidebarLink.href = _isAuthenticated ? '/logout' : '/login';
    sidebarLink.textContent = _isAuthenticated ? 'Logout' : 'Sign In';
  }

  // Update lock icons
  const locks = document.querySelectorAll('.lock-icon');
  locks.forEach(l => {
    l.textContent = _isAuthenticated ? '' : '\u{1F512}';
  });

  // Show/hide auth overlays
  const controlOverlay = document.getElementById('controlAuthOverlay');
  const configOverlay = document.getElementById('configAuthOverlay');
  if (controlOverlay) controlOverlay.style.display = _isAuthenticated ? 'none' : 'flex';
  if (configOverlay) configOverlay.style.display = _isAuthenticated ? 'none' : 'flex';
}

// Stackable toast system (Phase 5). Multiple toasts can be on screen
// simultaneously; each auto-dismisses after 4 s. Fade-in/out via CSS.
function showToast(msg, type) {
  const stack = document.getElementById('toastStack');
  if (!stack) return;  // safety: older fragments that haven't mounted yet
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'success');
  el.textContent = msg;
  stack.appendChild(el);
  // Force reflow so the transition runs from opacity:0.
  requestAnimationFrame(() => { el.classList.add('visible'); });
  setTimeout(() => {
    el.classList.remove('visible');
    // Remove after transition finishes so stack doesn't grow forever.
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  }, 4000);
}

// Phase 5: engine status in sidebar header. Polls /api/status every 30 s
// when the page is visible; skips polling when tab is hidden so we don't
// wake the container unnecessarily.
let _engineStatusTimer = null;
async function pollEngineStatus() {
  const dot = document.getElementById('engineStatusDot');
  if (!dot) return;
  try {
    const r = await fetch('/api/status', { signal: AbortSignal.timeout(5000) });
    if (r.ok) {
      const data = await r.json();
      // /api/status returns {"running": bool, ...}; map to dot state.
      if (data && data.running) {
        dot.className = 'engine-status-dot running';
        dot.title = 'Engine running';
      } else {
        dot.className = 'engine-status-dot stopping';
        dot.title = 'Engine not running';
      }
    } else if (r.status === 401 || r.status === 403) {
      // Unauthenticated — the endpoint is protected; keep the dot neutral.
      dot.className = 'engine-status-dot';
      dot.title = 'Sign in to see engine status';
    } else {
      dot.className = 'engine-status-dot stopping';
      dot.title = 'HTTP ' + r.status;
    }
  } catch(e) {
    dot.className = 'engine-status-dot stopping';
    dot.title = 'Unreachable';
  }
}
function startEngineStatusPoll() {
  if (_engineStatusTimer) clearInterval(_engineStatusTimer);
  pollEngineStatus();
  _engineStatusTimer = setInterval(() => {
    if (document.hidden) return;
    pollEngineStatus();
  }, 30000);
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollEngineStatus();
});
// Kicked off after first checkAuth() resolves (see Shared utilities block).

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Advanced Mode Toggle
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

function toggleAdvanced() {
  const on = document.getElementById('advancedToggle').checked;
  document.body.classList.toggle('show-advanced', on);
  try { localStorage.setItem('glados_advanced', on ? '1' : '0'); } catch(e) {}
}

(function restoreAdvanced() {
  try {
    if (localStorage.getItem('glados_advanced') === '1') {
      document.getElementById('advancedToggle').checked = true;
      document.body.classList.add('show-advanced');
    }
  } catch(e) {}
})();

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Field Metadata Registry
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const FIELD_META = {
  // â”€â”€ Global: Home Assistant â”€â”€
  'home_assistant.url':    { label: 'Home Assistant URL', desc: 'Base URL of your Home Assistant instance' },
  'home_assistant.ws_url': { label: 'WebSocket URL', desc: 'WebSocket endpoint for real-time HA events', advanced: true },
  'home_assistant.token':  { label: 'API Token', desc: 'Long-lived access token for HA', type: 'password' },
  // â”€â”€ Global: Network â”€â”€ (Phase 6: hidden — env-driven, YAML edit is inert)
  'network.serve_host':    { label: 'Server Host', desc: 'env-driven; edit via SERVE_HOST', hidden: true },
  'network.serve_port':    { label: 'Server Port', desc: 'env-driven; edit via SERVE_PORT', hidden: true },
  // â”€â”€ Global: Paths â”€â”€ (Phase 6: all hidden — env-driven, no WebUI-writable effect)
  'paths.glados_root':     { label: 'GLaDOS Root Path', desc: 'env-driven; edit via GLADOS_ROOT', hidden: true },
  'paths.audio_base':      { label: 'Audio Base Path', desc: 'env-driven; edit via GLADOS_AUDIO', hidden: true },
  'paths.logs':            { label: 'Logs Path', desc: 'env-driven; edit via GLADOS_LOGS', hidden: true },
  'paths.data':            { label: 'Data Path', desc: 'env-driven; edit via GLADOS_DATA', hidden: true },
  'paths.assets':          { label: 'Assets Path', desc: 'env-driven; edit via GLADOS_ASSETS', hidden: true },
  // â”€â”€ Global: SSL â”€â”€
  // SSL fields are edited on the dedicated Configuration > SSL page
  // (cfgRenderSsl). They were previously duplicated here via FIELD_META
  // auto-rendering which produced two conflicting forms for the same
  // settings. Phase 5 removed the duplicates; the SSL page is the
  // single source of truth for ssl.*.
  // â”€â”€ Global: Auth â”€â”€ (Phase 6: all advanced — operators rarely touch this after initial setup)
  'auth.enabled':          { label: 'Authentication Enabled', desc: 'Require login to access System and Config' },
  'auth.password_hash':    { label: 'Password Hash', desc: 'Bcrypt hash (use set_password tool to change)', advanced: true, type: 'password' },
  'auth.session_secret':   { label: 'Session Secret', desc: 'Secret key for session tokens', advanced: true, type: 'password' },
  'auth.session_timeout_hours': { label: 'Session Timeout (hours)', desc: 'How long before a session expires' },
  // â”€â”€ Global: Mode Entities â”€â”€
  'mode_entities.maintenance_mode':    { label: 'Maintenance Mode Entity', desc: 'HA entity for maintenance mode' },
  'mode_entities.maintenance_speaker': { label: 'Maintenance Speaker Entity', desc: 'HA entity for maintenance speaker selection' },
  'mode_entities.silent_mode':         { label: 'Silent Mode Entity', desc: 'HA entity for silent mode', advanced: true },
  'mode_entities.dnd':                 { label: 'Do Not Disturb Entity', desc: 'HA entity for manual DND toggle', advanced: true },
  // â”€â”€ Global: Silent Hours â”€â”€
  'silent_hours.enabled':  { label: 'Silent Hours Enabled', desc: 'Drop low-priority alerts during the quiet window' },
  'silent_hours.start':    { label: 'Start Time', desc: 'When silent hours begin', options: [
    '00:00','01:00','02:00','03:00','04:00','05:00','06:00','07:00','08:00','09:00','10:00','11:00',
    '12:00','13:00','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00','22:00','23:00'] },
  'silent_hours.end':      { label: 'End Time', desc: 'When silent hours end', options: [
    '00:00','01:00','02:00','03:00','04:00','05:00','06:00','07:00','08:00','09:00','10:00','11:00',
    '12:00','13:00','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00','22:00','23:00'] },
  'silent_hours.min_tier': { label: 'Minimum Tier to Play', desc: 'Alerts below this tier are suppressed during silent hours', options: ['AMBIENT','LOW','MEDIUM','HIGH','CRITICAL'] },
  // â”€â”€ Global: Audit â”€â”€ (Phase 6: hidden — deprecated / no WebUI-writable effect)
  'audit.enabled':                 { label: 'Audit Log Enabled', desc: 'Write utterance/tool audit trail to JSONL' },
  'audit.path':                    { label: 'Audit Log Path', desc: 'env-driven via GLADOS_LOGS', hidden: true },
  'audit.retention_days':          { label: 'Audit Retention (days)', desc: 'rotation not implemented', hidden: true },
  // â”€â”€ Global: Weather â”€â”€ (Phase 6: unit fields hidden — UI preference, not backend)
  'weather.latitude':              { label: 'Weather Latitude', desc: 'Used when auto_from_ha is false' },
  'weather.longitude':             { label: 'Weather Longitude', desc: 'Used when auto_from_ha is false' },
  'weather.auto_from_ha':          { label: 'Auto-read Weather from HA', desc: 'Read lat/long from your HA configuration' },
  'weather.temperature_unit':      { label: 'Temperature Unit', desc: 'display preference', hidden: true },
  'weather.wind_speed_unit':       { label: 'Wind Speed Unit', desc: 'display preference', hidden: true },
  // â”€â”€ Global: Tuning â”€â”€
  'tuning.llm_connect_timeout_s':  { label: 'LLM Connect Timeout (s)', desc: 'Seconds to wait for LLM connection', advanced: true },
  'tuning.llm_read_timeout_s':     { label: 'LLM Read Timeout (s)', desc: 'Max seconds to wait for LLM response', advanced: true },
  'tuning.tts_flush_chars':        { label: 'TTS Flush Threshold', desc: 'Characters to buffer before sending to TTS', advanced: true },
  'tuning.engine_pause_time':      { label: 'Engine Pause Time (s)', desc: 'Pause between engine loop iterations', advanced: true },
  'tuning.mode_cache_ttl_s':       { label: 'Mode Cache TTL (s)', desc: 'Seconds to cache HA mode entity states', advanced: true },
  'tuning.engine_audio_default':   { label: 'Engine Audio Default', desc: 'no code consumers', hidden: true },
  // â”€â”€ Audio â”€â”€
  // Phase 6: audio directory paths are hidden — editing them via the UI
  // can't create the destination folder, so changes either silently do
  // nothing (path exists) or break playback (path doesn't exist).
  // Advanced file-count caps stay behind the Advanced toggle.
  'ha_output_dir':         { label: 'HA Output Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'archive_dir':           { label: 'Archive Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'archive_max_files':     { label: 'Max Archive Files', desc: 'Maximum files to keep in the archive', advanced: true },
  'tts_ui_output_dir':     { label: 'TTS UI Output', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'tts_ui_max_files':      { label: 'Max TTS UI Files', desc: 'Maximum generated files to keep', advanced: true },
  'chat_audio_dir':        { label: 'Chat Audio Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'chat_audio_max_files':  { label: 'Max Chat Audio Files', desc: 'Maximum chat audio files to keep', advanced: true },
  'announcements_dir':     { label: 'Announcements Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'commands_dir':          { label: 'Commands Directory', desc: 'env-driven via GLADOS_AUDIO', hidden: true },
  'silence_between_sentences_ms': { label: 'Silence Between Sentences (ms)', desc: 'Milliseconds of silence inserted between sentences' },
  'sample_rate':           { label: 'Sample Rate (Hz)', desc: 'Audio sample rate for WAV output', advanced: true },
  // â”€â”€ Speakers â”€â”€
  'default':               { label: 'Default Speaker', desc: 'Default HA media player for audio output' },
  'available':             { label: 'Available Speakers', desc: 'Comma-separated list of available speaker entity IDs' },
  'blacklist':             { label: 'Blocked Speakers', desc: 'Speakers to exclude from selection' },
  // â”€â”€ Personality: default_tts â”€â”€
  'default_tts.length_scale': { label: 'Default Length Scale', desc: 'Speech duration (higher = slower)', advanced: true },
  'default_tts.noise_scale':  { label: 'Default Noise Scale', desc: 'Phoneme variation', advanced: true },
  'default_tts.noise_w':      { label: 'Default Noise W', desc: 'Duration variation', advanced: true },
  // â”€â”€ Robots â”€â”€
  'enabled':                       { label: 'Robots Enabled', desc: 'Master enable for the robot subsystem' },
  'health_poll_interval_s':        { label: 'Health Poll Interval (s)', desc: 'How often to poll node health endpoints' },
  'request_timeout_s':             { label: 'Request Timeout (s)', desc: 'HTTP timeout for robot node API calls' },
  'emergency_stop_timeout_s':      { label: 'E-Stop Timeout (s)', desc: 'Shorter timeout for emergency stop (must be fast)' },
  'auth_token':                    { label: 'Global Auth Token', desc: 'Bearer token sent with all API requests (except e-stop)', type: 'password' },
};

const SECTION_META = {
  // Phase 6 page names (operators see these titles in the sidebar).
  integrations:     { title: 'Integrations', desc: 'Home Assistant, MQTT, and media-stack integrations (MQTT + *arr/Plex arrive in later phases)' },
  'llm-services':   { title: 'LLM & Services', desc: 'Ollama, TTS (speaches), STT, vision — endpoint URLs, health, and model options' },
  'audio-speakers': { title: 'Audio & Speakers', desc: 'HA media players and speech synthesis parameters' },
  personality:      { title: 'Personality', desc: 'Attitudes, TTS defaults, HEXACO traits, and emotion model' },
  memory:           { title: 'Memory', desc: 'ChromaDB retention, passive-fact defaults, and the review queue' },
  ssl:              { title: 'SSL', desc: 'HTTPS certificates — Let\'s Encrypt (DNS-01) or manual upload' },
  raw:              { title: 'Raw YAML', desc: 'Edit configuration files directly as YAML' },
  // Legacy section metas kept as defensive fallback — navigateTo() migrates
  // legacy nav keys to their Phase 6 equivalents, but direct cfgRenderSection
  // calls from elsewhere (error paths, older browser tabs, etc.) still find
  // a title instead of rendering the bare key.
  global:           { title: 'Integrations', desc: 'Home Assistant connection and related integration settings' },
  services:         { title: 'LLM & Services', desc: 'Service endpoint URLs and health' },
  speakers:         { title: 'Audio & Speakers', desc: 'Home Assistant media player configuration' },
  audio:            { title: 'Audio & Speakers', desc: 'Audio file paths, limits, and synthesis parameters' },
  robots:           { title: 'Robots', desc: 'Robot node integration — ESP32 nodes, bots, and emergency stop' },
};

const SERVICE_NAMES = {
  tts: 'TTS Engine',
  stt: 'Speech-to-Text',
  api_wrapper: 'API Wrapper',
  vision: 'Vision Service',
  ollama_interactive: 'Ollama Interactive',
  ollama_autonomy: 'Ollama Autonomy',
  ollama_vision: 'Ollama Vision',
};

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   TAB 4: Configuration Manager
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _cfgData = {};
let _cfgRaw = {};
let _cfgCurrentSection = 'global';
let _cfgCurrentRawFile = 'global';

async function cfgLoadAll() {
  try {
    const r = await fetch('/api/config');
    if (r.status === 401) return;
    _cfgData = await r.json();
  } catch(e) { console.error('Config load failed:', e); }
}

async function cfgLoadRaw() {
  try {
    const r = await fetch('/api/config/raw');
    if (r.ok) _cfgRaw = await r.json();
  } catch(e) { console.error('Raw config load failed:', e); }
}

function cfgSwitchSection(name, btn) {
  document.querySelectorAll('.cfg-tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  _cfgCurrentSection = name;
  if (name === 'raw') {
    cfgLoadRaw().then(() => cfgRenderRaw());
  } else {
    cfgRenderSection(name);
  }
}

// Phase 6: virtual pages map onto existing backing sections for data
// access + save semantics. Field IDs use the backing name so
// cfgCollectForm / cfgSaveSection keep working unchanged.
const _CFG_BACKING = {
  'integrations':   'global',
  'llm-services':   'services',
  // 'audio-speakers' has no single backing — rendered by a custom
  // path that calls cfgBuildForm twice (speakers + audio) with
  // per-subsection save buttons.
};

function cfgRenderSection(section) {
  if (section === 'audio-speakers') {
    _cfgRenderAudioSpeakers();
    return;
  }
  const backing = _CFG_BACKING[section] || section;

  const data = (section === 'ssl') ? (_cfgData.global || {}) : _cfgData[backing];
  if (!data) {
    document.getElementById('cfg-form-area').innerHTML =
      '<div style="color:#ff6666;padding:20px;">Section not loaded. Click Reload.</div>';
    return;
  }
  const meta = SECTION_META[section] || SECTION_META[backing] || {};
  let html = '<div class="cfg-section-header">'
    + '<div class="cfg-section-title">' + escHtml(meta.title || section) + '</div>'
    + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
    + '</div>';

  if (backing === 'services') {
    html += cfgRenderServices(data);
  } else if (backing === 'personality') {
    html += cfgRenderPersonality(data);
  } else if (section === 'ssl') {
    html += cfgRenderSsl(_cfgData.global && _cfgData.global.ssl ? _cfgData.global.ssl : {});
  } else {
    // Skip keys that belong to a dedicated page (ssl → SSL tab; auth /
    // audit / mode_entities → System tab) or are env-only (paths, network
    // are driven by GLADOS_ROOT / SERVE_HOST etc., so the YAML-backed form
    // is inert inside the container).
    const skipKeys = (backing === 'global')
        ? ['ssl', 'paths', 'network', 'auth', 'audit', 'mode_entities']
        : null;
    html += cfgBuildForm(data, backing, '', skipKeys);
  }

  if (section !== 'ssl') {
    const label = meta.title || backing;
    html += '<div class="cfg-save-row">'
      + '<button class="cfg-save-btn" onclick="cfgSaveSection(\'' + backing + '\')">Save ' + escHtml(label) + '</button>'
      + '<span id="cfg-save-result" class="cfg-result"></span>'
      + '</div>';
  }

  // Page-specific extras appended AFTER the main form + save button.
  if (section === 'integrations') {
    html += _cfgRenderIntegrationsExtras();
  } else if (section === 'llm-services') {
    html += _cfgRenderLLMServicesExtras();
  }

  document.getElementById('cfg-form-area').innerHTML = html;
}

// Phase 6: placeholder cards for Stage 3 Phase 2 (MQTT peer bus) and
// the post-Stage-3 media stack (*arr + Plex). Render as read-only cards
// so operators know the page exists for future growth without requiring
// real configuration yet.
function _cfgRenderIntegrationsExtras() {
  let html = '';
  // Phase 8.1 — Disambiguation rules card. Loaded lazily so the card
  // paints a placeholder immediately; the GET resolves and
  // _disambPopulate() replaces the body once rules arrive.
  html += ''
    + '<div class="card" id="cfg-disambiguation-card" style="margin-top:14px;">'
    +   '<div class="cfg-subsection-title">Disambiguation rules</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Controls how Tier&nbsp;2 picks entities when the utterance is ambiguous. '
    +     'Rules apply against Home&nbsp;Assistant&rsquo;s live entity cache; no entity data is stored here.'
    +   '</div>'
    +   '<div id="cfg-disamb-body">Loading rules&hellip;</div>'
    + '</div>';
  // Phase 8.3.5 — Candidate retrieval card. Status, rebuild, and
  // a live test input for the semantic retriever + device-
  // diversity filter. Lives directly under Disambiguation rules
  // since the two systems compose: the rules define which tokens
  // count as segments, the retriever applies them.
  html += ''
    + '<div class="card" id="cfg-candretrieval-card" style="margin-top:14px;">'
    +   '<div class="cfg-subsection-title">Candidate retrieval</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Phase&nbsp;8.3 semantic retriever (BGE-small-en-v1.5 ONNX) with a '
    +     'device-diversity filter on top-K. Use the test input to see which '
    +     'entities would be handed to the planner for any phrasing.'
    +   '</div>'
    +   '<div id="cfg-candretrieval-body">Loading retriever status&hellip;</div>'
    + '</div>';
  html += ''
    + '<div class="cfg-placeholder-card">'
    +   '<div class="cfg-placeholder-title">MQTT <span class="cfg-placeholder-tag">Coming soon</span></div>'
    +   '<div class="cfg-placeholder-desc">'
    +     'Peer-bus integration with Node-RED / Sonorium arrives in '
    +     '<strong>Stage 3 Phase 2</strong>. See <code>docs/Stage 3.md</code> for the plan.'
    +   '</div>'
    + '</div>'
    + '<div class="cfg-placeholder-card">'
    +   '<div class="cfg-placeholder-title">Media Stack <span class="cfg-placeholder-tag">Coming soon</span></div>'
    +   '<div class="cfg-placeholder-desc">'
    +     'Voice control for Radarr / Sonarr / Lidarr / Plex is targeted post-Stage-3; '
    +     'track it in <code>docs/roadmap.md</code>.'
    +   '</div>'
    + '</div>';
  // Defer fetch until the DOM exists. setTimeout(0) punts to the next
  // tick — by then cfg-form-area has been painted.
  setTimeout(_cfgLoadDisambiguation, 0);
  setTimeout(_cfgLoadCandRetrieval, 0);
  return html;
}

// Phase 8.1 — Disambiguation rules card population and save.

async function _cfgLoadDisambiguation() {
  const body = document.getElementById('cfg-disamb-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load rules (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _disambPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading rules: ' + escHtml(e.message) + '</div>';
  }
}

function _disambPopulate(data) {
  const body = document.getElementById('cfg-disamb-body');
  if (!body) return;
  const dedup = (data.twin_dedup === false) ? false : true;
  const ignoreSeg = (data.ignore_segments === false) ? false : true;
  const pairs = Array.isArray(data.opposing_token_pairs) ? data.opposing_token_pairs : [];
  const verifyMode = (typeof data.verification_mode === 'string') ? data.verification_mode : 'strict';
  const verifyTimeout = (typeof data.verification_timeout_s === 'number') ? data.verification_timeout_s : 3.0;
  // Phase 8.5 — {spoken keyword: registry name} alias maps.
  const floorAliases = (data.floor_aliases && typeof data.floor_aliases === 'object')
    ? data.floor_aliases : {};
  const areaAliases = (data.area_aliases && typeof data.area_aliases === 'object')
    ? data.area_aliases : {};
  let html = '';
  html += '<div class="cfg-field" style="display:flex;align-items:center;gap:10px;">'
    +   '<input type="checkbox" id="cfg-disamb-twin-dedup"' + (dedup ? ' checked' : '') + ' style="width:auto;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-twin-dedup" style="margin:0;">'
    +     'Collapse light/switch twins by device_id'
    +   '</label>'
    + '</div>'
    + '<div class="cfg-field-desc" style="margin:-6px 0 14px 28px;">'
    +   'When both <code>light.foo</code> and <code>switch.foo</code> represent the same physical relay, '
    +   'keep the light side (the only domain that honours <code>brightness_pct</code>). The switch still wins '
    +   'automatically when the light has no dim capability (Inovelli fan/light edge case).'
    + '</div>';
  // Phase 8.3 follow-up — operator-requested: drop segment
  // entities entirely from candidate lists. Most deployments
  // never address segments directly; the planner never sees them.
  html += '<div class="cfg-field" style="display:flex;align-items:center;gap:10px;">'
    +   '<input type="checkbox" id="cfg-disamb-ignore-segments"' + (ignoreSeg ? ' checked' : '') + ' style="width:auto;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-ignore-segments" style="margin:0;">'
    +     'Ignore segment entities (master lamp / scene only)'
    +   '</label>'
    + '</div>'
    + '<div class="cfg-field-desc" style="margin:-6px 0 14px 28px;">'
    +   'Drops any entity whose name or id matches the segment-token pattern before candidate resolution runs. '
    +   'Operators control the whole lamp or a preset scene; per-segment control is rare. Disable if your house '
    +   'genuinely needs per-segment control (theatrical lighting, etc.).'
    + '</div>';
  html += '<div class="cfg-field-label" style="margin-top:6px;">Opposing-token pairs</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'If an utterance contains one side of a pair and a candidate&rsquo;s entity name contains the other, '
    +   'the candidate loses 50 rank points. Leave empty to use the shipped defaults '
    +   '(<code>upstairs/downstairs</code>, <code>lower/upper</code>, <code>front/back</code>, '
    +   '<code>inside/outside</code>, <code>indoor/outdoor</code>, <code>master/guest</code>, '
    +   '<code>left/right</code>, <code>top/bottom</code>, <code>primary/secondary</code>, '
    +   '<code>north/south</code>, <code>east/west</code>).'
    + '</div>';
  html += '<div id="cfg-disamb-pairs" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px;"></div>';
  html += '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_disambAddPair()">+ Add pair</button>';
  // Phase 8.3.5 — operator-editable extra segment tokens used by
  // the device-diversity filter on top-K retrieval. Merges with
  // the shipped defaults (seg, segment, zone, channel, strip,
  // group, head); entries here add to (never replace) that list.
  const tokens = Array.isArray(data.extra_segment_tokens) ? data.extra_segment_tokens : [];
  html += '<div class="cfg-field-label" style="margin-top:14px;">Extra segment tokens</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Added to the shipped defaults (<code>seg, segment, zone, channel, strip, group, head</code>) '
    +   'when detecting multi-segment devices like Gledopto LED strips. Add house-specific tokens (e.g. '
    +   '<code>pixel</code>) if your strip entities use a different naming convention.'
    + '</div>'
    + '<div id="cfg-disamb-tokens" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_disambAddToken()">+ Add token</button>';
  // Phase 8.4 — post-execute state verification.
  html += '<div class="cfg-field-label" style="margin-top:18px;">Post-execute state verification</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:8px;">'
    +   'After every <code>call_service</code>, the disambiguator waits for Home&nbsp;Assistant to report a '
    +   'matching <code>state_changed</code> event. '
    +   '<strong>Strict</strong> replaces the optimistic speech with an honest failure line when no matching '
    +   'transition lands within the timeout &mdash; so GLaDOS never confidently announces a change that '
    +   'silently failed. '
    +   '<strong>Warn</strong> still audits the outcome but keeps the optimistic line. '
    +   '<strong>Silent</strong> skips verification entirely (pre-Phase-8.4 behaviour).'
    + '</div>'
    + '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-verify-mode" style="margin:0;min-width:140px;">Mode</label>'
    +   '<select id="cfg-disamb-verify-mode" style="flex:1;min-width:160px;">'
    +     '<option value="strict"' + (verifyMode === 'strict' ? ' selected' : '') + '>Strict (replace speech on failure)</option>'
    +     '<option value="warn"' + (verifyMode === 'warn' ? ' selected' : '') + '>Warn (audit only)</option>'
    +     '<option value="silent"' + (verifyMode === 'silent' ? ' selected' : '') + '>Silent (no verification)</option>'
    +   '</select>'
    + '</div>'
    + '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap;">'
    +   '<label class="cfg-field-label" for="cfg-disamb-verify-timeout" style="margin:0;min-width:140px;">Timeout (seconds)</label>'
    +   '<input type="number" id="cfg-disamb-verify-timeout" min="0.1" max="30" step="0.1" value="' + escAttr(verifyTimeout.toFixed(1)) + '" style="flex:1;min-width:120px;">'
    + '</div>';
  // Phase 8.5 — area / floor alias editor.
  html += '<div class="cfg-field-label" style="margin-top:18px;">Area &amp; floor aliases</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:8px;">'
    +   'Map house-specific keywords the shipped defaults don&rsquo;t know to the exact <em>registry name</em> '
    +   'of one of your HA areas or floors. Examples: <code>living floor &rarr; Main Level</code>, '
    +   '<code>mom&rsquo;s room &rarr; Master Bedroom</code>. Keywords match case-insensitively against the utterance; '
    +   'registry names must match exactly (including punctuation and spacing) so the inference can resolve them.'
    + '</div>';
  html += '<div class="cfg-field-label" style="margin-top:6px;">Floor aliases</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Utterance keyword &rarr; floor-registry name (e.g. <code>main level &rarr; Main Level</code>).'
    + '</div>'
    + '<div id="cfg-disamb-floor-aliases" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_disambAddFloorAlias()">+ Add floor alias</button>';
  html += '<div class="cfg-field-label" style="margin-top:14px;">Area aliases</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Utterance keyword &rarr; area-registry name (e.g. <code>mom&rsquo;s room &rarr; Master Bedroom</code>).'
    + '</div>'
    + '<div id="cfg-disamb-area-aliases" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_disambAddAreaAlias()">+ Add area alias</button>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveDisambiguation()">Save Disambiguation rules</button>'
    + '<span id="cfg-save-result-disamb" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
  const rows = document.getElementById('cfg-disamb-pairs');
  pairs.forEach(p => _disambRenderPairRow(rows, p[0] || '', p[1] || ''));
  const tokensHost = document.getElementById('cfg-disamb-tokens');
  tokens.forEach(t => _disambRenderTokenRow(tokensHost, t));
  const floorHost = document.getElementById('cfg-disamb-floor-aliases');
  Object.keys(floorAliases).forEach(k =>
    _disambRenderAliasRow(floorHost, 'floor', k, floorAliases[k] || ''));
  const areaHost = document.getElementById('cfg-disamb-area-aliases');
  Object.keys(areaAliases).forEach(k =>
    _disambRenderAliasRow(areaHost, 'area', k, areaAliases[k] || ''));
}

function _disambRenderAliasRow(host, kind, keyword, target) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-alias-row cfg-disamb-alias-' + kind;
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-alias-keyword" value="' + escAttr(keyword) + '" placeholder="e.g. living floor" style="flex:1;">'
    + '<span style="opacity:0.6;">&rarr;</span>'
    + '<input type="text" class="cfg-disamb-alias-target" value="' + escAttr(target) + '" placeholder="e.g. Main Level" style="flex:1;">'
    + '<button type="button" title="Remove alias" style="background:#a33;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddFloorAlias() {
  const host = document.getElementById('cfg-disamb-floor-aliases');
  if (host) _disambRenderAliasRow(host, 'floor', '', '');
}

function _disambAddAreaAlias() {
  const host = document.getElementById('cfg-disamb-area-aliases');
  if (host) _disambRenderAliasRow(host, 'area', '', '');
}

function _disambRenderTokenRow(host, t) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-token-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-token" value="' + escAttr(t) + '" placeholder="e.g. pixel" style="flex:1;">'
    + '<button type="button" title="Remove token" style="background:#a33;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddToken() {
  const host = document.getElementById('cfg-disamb-tokens');
  if (host) _disambRenderTokenRow(host, '');
}

function _disambRenderPairRow(host, a, b) {
  const row = document.createElement('div');
  row.className = 'cfg-disamb-pair-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-disamb-pair-a" value="' + escAttr(a) + '" placeholder="e.g. upstairs" style="flex:1;">'
    + '<span style="opacity:0.6;">&harr;</span>'
    + '<input type="text" class="cfg-disamb-pair-b" value="' + escAttr(b) + '" placeholder="e.g. downstairs" style="flex:1;">'
    + '<button type="button" title="Remove pair" style="background:#a33;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _disambAddPair() {
  const host = document.getElementById('cfg-disamb-pairs');
  if (host) _disambRenderPairRow(host, '', '');
}

// ── Phase 8.3.5 — Candidate retrieval card ──────────────────

async function _cfgLoadCandRetrieval() {
  const body = document.getElementById('cfg-candretrieval-body');
  if (!body) return;
  let status = null;
  try {
    const r = await fetch('/api/semantic/status');
    if (r.ok) status = await r.json();
    else body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load status (' + r.status + ').</div>';
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading status: ' + escHtml(e.message) + '</div>';
    return;
  }
  if (!status) return;
  _candRetrievalPopulate(status);
}

function _candRetrievalPopulate(status) {
  const body = document.getElementById('cfg-candretrieval-body');
  if (!body) return;
  const ready = !!status.ready;
  const mtime = status.file_mtime ? new Date(status.file_mtime * 1000).toLocaleString() : 'never';
  const sizeMb = status.file_size_bytes ? (status.file_size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '—';
  let html = '';
  // Status row
  html += '<div class="cfg-field-desc" style="margin-bottom:10px;line-height:1.6;">'
    + '<strong>Status:</strong> '
    + (ready ? '<span style="color:#6c6;">ready</span>' : '<span style="color:#d99;">not ready</span>')
    + ' &middot; <strong>Entities indexed:</strong> ' + (status.size || 0)
    + ' &middot; <strong>Last persist:</strong> ' + escHtml(mtime)
    + ' &middot; <strong>File size:</strong> ' + sizeMb
    + '</div>';
  if (!status.deps_available) {
    html += '<div class="cfg-field-desc" style="color:#d99;margin-bottom:8px;">'
      + 'Embedding dependencies or model files are missing. Tier&nbsp;2 stays on the fuzzy matcher.'
      + '</div>';
  }
  // Rebuild button
  html += '<div style="display:flex;gap:8px;align-items:center;margin-bottom:16px;">'
    + '<button class="cfg-save-btn" onclick="_candRetrievalRebuild()">Rebuild index</button>'
    + '<span class="cfg-field-desc" style="margin:0;">'
    + '(Background. Poll the status line above to confirm size updates.)'
    + '</span>'
    + '</div>';
  // Test input
  html += '<div class="cfg-field-label">Test a query</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    + 'See which entities the retriever + device-diversity filter would hand to the planner. '
    + 'Useful for confirming Gledopto-style multi-segment devices don&rsquo;t swamp top-K.'
    + '</div>'
    + '<div style="display:flex;gap:6px;align-items:center;">'
    + '<input type="text" id="cfg-candretrieval-q" placeholder="e.g. desk lamp" style="flex:1;">'
    + '<input type="number" id="cfg-candretrieval-k" value="8" min="1" max="20" style="width:60px;">'
    + '<button type="button" class="cfg-save-btn" onclick="_candRetrievalTest()">Test</button>'
    + '</div>'
    + '<div id="cfg-candretrieval-result" style="margin-top:10px;"></div>';
  body.innerHTML = html;
}

async function _candRetrievalRebuild() {
  showToast('Rebuild started...', 'info');
  try {
    const r = await fetch('/api/semantic/rebuild', {method: 'POST'});
    if (!r.ok) { showToast('Rebuild failed', 'err'); return; }
    // Give the background a couple seconds, then refresh the status line
    setTimeout(_cfgLoadCandRetrieval, 3000);
  } catch (e) {
    showToast('Rebuild error: ' + e.message, 'err');
  }
}

async function _candRetrievalTest() {
  const qEl = document.getElementById('cfg-candretrieval-q');
  const kEl = document.getElementById('cfg-candretrieval-k');
  const res = document.getElementById('cfg-candretrieval-result');
  if (!qEl || !res) return;
  const query = (qEl.value || '').trim();
  const k = parseInt(kEl.value || '8', 10) || 8;
  if (!query) { res.innerHTML = '<div class="cfg-field-desc">Enter a query.</div>'; return; }
  res.innerHTML = '<div class="cfg-field-desc">Running&hellip;</div>';
  try {
    const r = await fetch('/api/semantic/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, k}),
    });
    const resp = await r.json();
    if (!r.ok) {
      res.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">'
        + escHtml((resp.error && resp.error.message) || ('HTTP ' + r.status))
        + '</div>';
      return;
    }
    _candRetrievalRenderTable(res, resp);
  } catch (e) {
    res.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _candRetrievalRenderTable(host, resp) {
  const kept = Array.isArray(resp.kept) ? resp.kept : [];
  const dropped = Array.isArray(resp.dropped_by_diversity) ? resp.dropped_by_diversity : [];
  const tokens = Array.isArray(resp.segment_tokens) ? resp.segment_tokens : [];
  let html = '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    + '<strong>Raw pool:</strong> ' + resp.raw_pool_size
    + ' &middot; <strong>Segment tokens in effect:</strong> <code>' + tokens.map(escHtml).join(', ') + '</code>'
    + '</div>';
  const renderRows = (list, color) => {
    if (!list.length) return '<tr><td colspan="3" style="padding:6px;color:#888;">(none)</td></tr>';
    return list.map(h =>
      '<tr>'
      + '<td style="padding:4px 8px;font-family:monospace;color:' + color + ';">' + escHtml(h.entity_id) + '</td>'
      + '<td style="padding:4px 8px;text-align:right;">' + h.score.toFixed(3) + '</td>'
      + '<td style="padding:4px 8px;font-family:monospace;font-size:0.85em;color:#aaa;">' + escHtml(h.document || '') + '</td>'
      + '</tr>'
    ).join('');
  };
  html += '<div style="margin-top:8px;"><strong>Kept (top ' + resp.top_k + '):</strong></div>'
    + '<table style="width:100%;border-collapse:collapse;font-size:0.88rem;">'
    + '<thead><tr style="text-align:left;color:#888;"><th style="padding:4px 8px;">entity_id</th><th style="padding:4px 8px;text-align:right;">score</th><th style="padding:4px 8px;">document</th></tr></thead>'
    + '<tbody>' + renderRows(kept, '#6c6') + '</tbody>'
    + '</table>';
  if (dropped.length) {
    html += '<div style="margin-top:10px;"><strong>Dropped by diversity filter:</strong></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.88rem;">'
      + '<tbody>' + renderRows(dropped, '#d99') + '</tbody>'
      + '</table>';
  }
  host.innerHTML = html;
}

async function cfgSaveDisambiguation() {
  const resultEl = document.getElementById('cfg-save-result-disamb');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const twinEl = document.getElementById('cfg-disamb-twin-dedup');
  const twin = twinEl ? !!twinEl.checked : true;
  const ignoreSegEl = document.getElementById('cfg-disamb-ignore-segments');
  const ignoreSegments = ignoreSegEl ? !!ignoreSegEl.checked : true;
  const pairs = [];
  document.querySelectorAll('#cfg-disamb-pairs .cfg-disamb-pair-row').forEach(row => {
    const a = (row.querySelector('.cfg-disamb-pair-a') || {}).value || '';
    const b = (row.querySelector('.cfg-disamb-pair-b') || {}).value || '';
    if (a.trim() && b.trim()) pairs.push([a.trim(), b.trim()]);
  });
  const tokens = [];
  document.querySelectorAll('#cfg-disamb-tokens .cfg-disamb-token-row .cfg-disamb-token').forEach(el => {
    const t = (el.value || '').trim();
    if (t) tokens.push(t);
  });
  const verifyModeEl = document.getElementById('cfg-disamb-verify-mode');
  const verifyMode = verifyModeEl ? String(verifyModeEl.value || 'strict') : 'strict';
  const verifyTimeoutEl = document.getElementById('cfg-disamb-verify-timeout');
  let verifyTimeout = verifyTimeoutEl ? parseFloat(verifyTimeoutEl.value) : 3.0;
  if (!isFinite(verifyTimeout) || verifyTimeout <= 0) verifyTimeout = 3.0;
  // Phase 8.5 — collect alias rows.
  const floorAliases = {};
  document.querySelectorAll('#cfg-disamb-floor-aliases .cfg-disamb-alias-row').forEach(row => {
    const k = ((row.querySelector('.cfg-disamb-alias-keyword') || {}).value || '').trim();
    const v = ((row.querySelector('.cfg-disamb-alias-target') || {}).value || '').trim();
    if (k && v) floorAliases[k] = v;
  });
  const areaAliases = {};
  document.querySelectorAll('#cfg-disamb-area-aliases .cfg-disamb-alias-row').forEach(row => {
    const k = ((row.querySelector('.cfg-disamb-alias-keyword') || {}).value || '').trim();
    const v = ((row.querySelector('.cfg-disamb-alias-target') || {}).value || '').trim();
    if (k && v) areaAliases[k] = v;
  });
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        twin_dedup: twin,
        ignore_segments: ignoreSegments,
        opposing_token_pairs: pairs,
        extra_segment_tokens: tokens,
        verification_mode: verifyMode,
        verification_timeout_s: verifyTimeout,
        floor_aliases: floorAliases,
        area_aliases: areaAliases,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

// Phase 6: cross-section cards under LLM & Services so operators can tune
// persona strength (personality.model_options) and request timeouts
// (global.tuning.llm_*_timeout_s) without hunting through the schema.
// Each card saves its own backing section independently.
function _cfgRenderLLMServicesExtras() {
  const mo = (_cfgData.personality || {}).model_options || {};
  const t = ((_cfgData.global || {}).tuning) || {};
  let html = '';

  // Model Options card (personality.model_options)
  html += '<div class="card" style="margin-top:14px;">';
  html += '<div class="cfg-subsection-title">Model Options</div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Temperature</label>'
    + '<div class="cfg-field-desc">0.0 is deterministic, 1.0+ is creative</div>'
    + '<input id="cfg-personality-model_options-temperature" data-path="model_options.temperature" data-type="number" type="number" step="any" value="' + escAttr(String(mo.temperature ?? 0.7)) + '"></div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Top P</label>'
    + '<div class="cfg-field-desc">Nucleus sampling threshold (0.0 - 1.0)</div>'
    + '<input id="cfg-personality-model_options-top_p" data-path="model_options.top_p" data-type="number" type="number" step="any" value="' + escAttr(String(mo.top_p ?? 0.9)) + '"></div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Context Window (num_ctx)</label>'
    + '<div class="cfg-field-desc">Tokens of context the model sees per turn</div>'
    + '<input id="cfg-personality-model_options-num_ctx" data-path="model_options.num_ctx" data-type="number" type="number" value="' + escAttr(String(mo.num_ctx ?? 16384)) + '"></div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Repeat Penalty</label>'
    + '<div class="cfg-field-desc">Higher values reduce parroting (typical 1.0 - 1.3)</div>'
    + '<input id="cfg-personality-model_options-repeat_penalty" data-path="model_options.repeat_penalty" data-type="number" type="number" step="any" value="' + escAttr(String(mo.repeat_penalty ?? 1.1)) + '"></div>';
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveModelOptions()">Save Model Options</button>'
    + '<span id="cfg-save-result-model-options" class="cfg-result"></span></div>';
  html += '</div>';

  // LLM Timeouts card (global.tuning.llm_*)
  html += '<div class="card" style="margin-top:14px;" data-advanced="true">';
  html += '<div class="cfg-subsection-title">LLM Timeouts <span class="cfg-placeholder-tag">advanced</span></div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Connect Timeout (s)</label>'
    + '<div class="cfg-field-desc">Seconds to wait for LLM connection</div>'
    + '<input id="cfg-llm-connect-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_connect_timeout_s ?? 10)) + '"></div>';
  html += '<div class="cfg-field"><label class="cfg-field-label">Read Timeout (s)</label>'
    + '<div class="cfg-field-desc">Max seconds to wait for LLM response</div>'
    + '<input id="cfg-llm-read-timeout" data-type="number" type="number" value="' + escAttr(String(t.llm_read_timeout_s ?? 180)) + '"></div>';
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveLLMTimeouts()">Save Timeouts</button>'
    + '<span id="cfg-save-result-timeouts" class="cfg-result"></span></div>';
  html += '</div>';

  return html;
}

// Model Options: wrap the existing personality section save with a
// targeted update — read current personality data, overlay the four
// model_options fields, PUT the full personality payload back.
async function cfgSaveModelOptions() {
  const resultEl = document.getElementById('cfg-save-result-model-options');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const current = _cfgData.personality || {};
  const next = Object.assign({}, current, {
    model_options: {
      temperature: Number(document.getElementById('cfg-personality-model_options-temperature').value),
      top_p: Number(document.getElementById('cfg-personality-model_options-top_p').value),
      num_ctx: parseInt(document.getElementById('cfg-personality-model_options-num_ctx').value, 10),
      repeat_penalty: Number(document.getElementById('cfg-personality-model_options-repeat_penalty').value),
    }
  });
  try {
    const r = await fetch('/api/config/personality', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData.personality = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

// LLM Timeouts: targeted update of the global.tuning.llm_*_timeout_s
// fields, preserving all other tuning + global settings.
async function cfgSaveLLMTimeouts() {
  const resultEl = document.getElementById('cfg-save-result-timeouts');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const current = _cfgData.global || {};
  const tuning = Object.assign({}, current.tuning || {}, {
    llm_connect_timeout_s: parseInt(document.getElementById('cfg-llm-connect-timeout').value, 10),
    llm_read_timeout_s: parseInt(document.getElementById('cfg-llm-read-timeout').value, 10),
  });
  const next = Object.assign({}, current, { tuning });
  try {
    const r = await fetch('/api/config/global', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData.global = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

// Phase 6 follow-up: System tab absorbs auth, audit, and the two
// maintenance_* mode entities that used to live under Integrations.
// These forms render into the System tab and save back to the 'global'
// backing. They use section name 'sysaux' for field IDs so they don't
// collide with an Integrations page that may still have the full
// global form rendered in another tab's DOM.

function loadSystemConfigCards() {
  // _cfgData may not be loaded yet on first visit — fetch if empty.
  const have = _cfgData && _cfgData.global;
  const run = () => {
    _cfgRenderSystemMaintForm();
    _cfgRenderSystemAuthAuditForm();
    _cfgRenderTestHarnessForm();
  };
  if (have) { run(); }
  else if (typeof cfgLoadAll === 'function') { cfgLoadAll().then(run); }
}

// Phase 8.9 — Test harness card. Simple two-field editor: patterns
// (textarea, one glob per line) + direction-match toggle. Saves to
// /api/config/test_harness.
function _cfgRenderTestHarnessForm() {
  const host = document.getElementById('testHarnessForm');
  if (!host) return;
  const th = _cfgData.test_harness || {};
  const patterns = Array.isArray(th.noise_entity_patterns)
    ? th.noise_entity_patterns.join('\n') : '';
  const require = th.require_direction_match !== false;
  host.innerHTML =
    '<div class="cfg-field">'
    + '<label class="cfg-label" for="th-patterns">Noise entity patterns'
    + ' <span style="color:var(--text-dim);font-weight:normal;">'
    + '(fnmatch globs, one per line — e.g. <code>switch.midea_ac_*_display</code>)'
    + '</span></label>'
    + '<textarea id="th-patterns" rows="8" style="width:100%;background:var(--bg-input);'
    + 'color:var(--text);border:1px solid var(--border);border-radius:4px;padding:8px;'
    + 'font-family:monospace;font-size:0.82rem;">'
    + escHtml(patterns) + '</textarea>'
    + '</div>'
    + '<div class="cfg-field" style="margin-top:10px;">'
    + '<label style="display:flex;align-items:center;gap:8px;">'
    + '<input type="checkbox" id="th-direction"' + (require ? ' checked' : '') + '>'
    + '<span>Require direction match (recommended)</span>'
    + '</label>'
    + '<div class="mode-desc" style="margin-top:4px;">'
    + 'When on, harness requires the targeted entity to end in the expected state. '
    + 'When off, any state change counts — use only for A/B comparison against the pre-8.9 scorer.'
    + '</div>'
    + '</div>';
}

async function cfgSaveTestHarness() {
  const resultEl = document.getElementById('cfg-save-result-test-harness');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const raw = document.getElementById('th-patterns');
  const dir = document.getElementById('th-direction');
  if (!raw || !dir) return;
  const patterns = raw.value.split('\n').map(s => s.trim()).filter(Boolean);
  const body = {
    noise_entity_patterns: patterns,
    require_direction_match: !!dir.checked,
  };
  try {
    const r = await fetch('/api/config/test_harness', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const txt = await r.text();
    let resp = {};
    try { resp = JSON.parse(txt); } catch (_) { resp = { error: txt }; }
    if (r.ok) {
      _cfgData.test_harness = body;
      if (resultEl) resultEl.textContent = '';
      showToast('Test-harness config saved.', 'success');
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

function _cfgRenderSystemMaintForm() {
  const me = (_cfgData.global || {}).mode_entities || {};
  // Only the maintenance pair — silent_mode / dnd belong on Audio & Speakers.
  const subset = {
    mode_entities: {
      maintenance_mode:    me.maintenance_mode    || '',
      maintenance_speaker: me.maintenance_speaker || '',
    },
  };
  const host = document.getElementById('sysMaintForm');
  if (!host) return;
  host.innerHTML = cfgBuildForm(subset, 'sysaux', '', null);
}

function _cfgRenderSystemAuthAuditForm() {
  const g = _cfgData.global || {};
  const subset = {
    auth:  g.auth  || {},
    audit: g.audit || {},
  };
  const host = document.getElementById('sysAuthAuditForm');
  if (!host) return;
  host.innerHTML = cfgBuildForm(subset, 'sysaux', '', null);
}

// Generic save helper for the System-tab subset forms. Collects every
// `[id^="cfg-sysaux-"]` input inside `scopeEl`, rebuilds nested paths
// from `data-path`, deep-merges the result into a copy of _cfgData.global,
// and PUTs /api/config/global. Scoping to the form element prevents
// stray inputs from other cards bleeding into the payload.
async function _cfgSaveSystemSubset(scopeEl, resultElId) {
  const resultEl = document.getElementById(resultElId);
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }

  const delta = {};
  scopeEl.querySelectorAll('[id^="cfg-sysaux-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else if (type === 'array') val = el.value.split(',').map(s => s.trim()).filter(Boolean);
    else val = el.value;

    const parts = path.split('.');
    let cur = delta;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in cur)) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = val;
  });

  // Deep-merge delta into a snapshot of the current global config so we
  // don't clobber sibling fields (silent_hours, tuning, home_assistant, etc.).
  const next = JSON.parse(JSON.stringify(_cfgData.global || {}));
  const _merge = (dst, src) => {
    for (const k of Object.keys(src)) {
      if (src[k] && typeof src[k] === 'object' && !Array.isArray(src[k])) {
        if (!dst[k] || typeof dst[k] !== 'object') dst[k] = {};
        _merge(dst[k], src[k]);
      } else {
        dst[k] = src[k];
      }
    }
  };
  _merge(next, delta);

  try {
    const r = await fetch('/api/config/global', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(next),
    });
    // The legacy /api/config/<section> handler uses _send_error for
    // non-ok paths, which emits plain text rather than JSON. Parse
    // defensively so our handler doesn't swallow the real error behind
    // "Unexpected token 'V'".
    const bodyText = await r.text();
    let resp = {};
    try { resp = JSON.parse(bodyText); } catch (_) { resp = { error: bodyText }; }
    if (r.ok) {
      _cfgData.global = next;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

async function cfgSaveSystemMaint() {
  const form = document.getElementById('sysMaintForm');
  if (form) await _cfgSaveSystemSubset(form, 'cfg-save-result-sys-maint');
}

async function cfgSaveSystemAuthAudit() {
  const form = document.getElementById('sysAuthAuditForm');
  if (form) await _cfgSaveSystemSubset(form, 'cfg-save-result-sys-authaudit');
}

// Phase 6 merged page: renders Speakers + Audio side-by-side with
// per-subsection Save buttons (each targets its own backing section).
function _cfgRenderAudioSpeakers() {
  const speakers = _cfgData.speakers;
  const audio = _cfgData.audio;
  if (!speakers || !audio) {
    document.getElementById('cfg-form-area').innerHTML =
      '<div style="color:#ff6666;padding:20px;">Audio &amp; Speakers sections not loaded. Click Reload.</div>';
    return;
  }
  const meta = SECTION_META['audio-speakers'] || {};
  let html = '<div class="cfg-section-header">'
    + '<div class="cfg-section-title">' + escHtml(meta.title || 'Audio & Speakers') + '</div>'
    + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
    + '</div>';

  html += '<div class="cfg-subsection-title">Speakers</div>';
  html += cfgBuildForm(speakers, 'speakers', '');
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveSection(\'speakers\', \'cfg-save-result-speakers\')">Save Speakers</button>'
    + '<span id="cfg-save-result-speakers" class="cfg-result"></span>'
    + '</div>';

  html += '<div class="cfg-subsection-title" style="margin-top:18px;">Audio</div>';
  html += cfgBuildForm(audio, 'audio', '');
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveSection(\'audio\', \'cfg-save-result-audio\')">Save Audio</button>'
    + '<span id="cfg-save-result-audio" class="cfg-result"></span>'
    + '</div>';

  // Phase 8.7a — Response behavior card. Picks whether speech comes
  // from the LLM (default), a pre-written Portal-voice quip from
  // configs/quips/, a sound chime, or nothing at all. Configurable
  // globally OR per event category.
  html += ''
    + '<div class="card" id="cfg-response-behavior-card" style="margin-top:18px;">'
    +   '<div class="cfg-subsection-title">Response behavior</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Choose how GLaDOS acknowledges commands. '
    +     '<strong>LLM</strong> (default) has the language model write each reply &mdash; expressive but can drift. '
    +     '<strong>Quip</strong> picks a pre-written line from <code>configs/quips/</code> &mdash; never leaks device names, no drift. '
    +     '<strong>Chime</strong> plays a sound file. '
    +     '<strong>Silent</strong> makes no audible reply at all.'
    +   '</div>'
    +   '<div id="cfg-response-behavior-body">Loading&hellip;</div>'
    + '</div>';

  // Phase 8.10 — Pronunciation overrides. Two maps the operator edits
  // as plain text: one-per-line key=value rows. Piper mispronounces
  // common short abbreviations ("AI" as one-letter "Aye"); this pass
  // expands them BEFORE the all-caps splitter that caused the slur.
  html += ''
    + '<div class="card" id="cfg-pronunciation-card" style="margin-top:18px;">'
    +   '<div class="cfg-subsection-title">TTS Pronunciation overrides</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Piper pronounces short abbreviations poorly by default &mdash; '
    +     '<code>AI</code> becomes one slurred letter, <code>HA</code> reads '
    +     'mechanically. These overrides expand each key before the text-to-speech '
    +     'converter processes it. <strong>Word expansions</strong> match whole '
    +     'words case-insensitively. <strong>Symbol expansions</strong> replace '
    +     'literal characters. One <code>key = value</code> pair per line.'
    +   '</div>'
    +   '<div id="cfg-pronunciation-body">Loading&hellip;</div>'
    + '</div>';

  document.getElementById('cfg-form-area').innerHTML = html;
  setTimeout(_cfgLoadResponseBehavior, 0);
  setTimeout(_cfgLoadPronunciation, 0);
}

async function _cfgLoadPronunciation() {
  const body = document.getElementById('cfg-pronunciation-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/tts_pronunciation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _pronunciationPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _pronunciationPopulate(data) {
  const body = document.getElementById('cfg-pronunciation-body');
  if (!body) return;
  const sym = data.symbol_expansions || {};
  const words = data.word_expansions || {};
  const symText = Object.entries(sym).map(a => a[0] + ' = ' + a[1]).join('\n');
  const wordText = Object.entries(words).map(a => a[0] + ' = ' + a[1]).join('\n');
  const ta = 'background:var(--bg-input);color:var(--text);border:1px solid var(--border);'
    + 'border-radius:4px;padding:8px;width:100%;font-family:monospace;font-size:0.82rem;';
  let html = '';
  html += '<div class="cfg-field">'
    + '<label class="cfg-label">Word expansions <span style="color:var(--text-dim);font-weight:normal;">(whole-word, case-insensitive)</span></label>'
    + '<textarea id="cfg-pr-words" rows="6" style="' + ta + '">' + escHtml(wordText) + '</textarea>'
    + '</div>';
  html += '<div class="cfg-field" style="margin-top:10px;">'
    + '<label class="cfg-label">Symbol expansions <span style="color:var(--text-dim);font-weight:normal;">(literal replace, e.g. <code>%</code>, <code>&amp;</code>)</span></label>'
    + '<textarea id="cfg-pr-symbols" rows="3" style="' + ta + '">' + escHtml(symText) + '</textarea>'
    + '</div>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSavePronunciation()">Save Pronunciation</button>'
    + '<span id="cfg-save-result-pronunciation" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
}

function _parsePronunciationRows(text) {
  const out = {};
  String(text || '').split(/\r?\n/).forEach(line => {
    const t = line.trim();
    if (!t) return;
    const eq = t.indexOf('=');
    if (eq < 1) return;
    const k = t.substring(0, eq).trim();
    const v = t.substring(eq + 1).trim();
    if (k) out[k] = v;
  });
  return out;
}

async function cfgSavePronunciation() {
  const resultEl = document.getElementById('cfg-save-result-pronunciation');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const words = _parsePronunciationRows(document.getElementById('cfg-pr-words').value);
  const symbols = _parsePronunciationRows(document.getElementById('cfg-pr-symbols').value);
  try {
    const r = await fetch('/api/config/tts_pronunciation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        word_expansions: words,
        symbol_expansions: symbols,
      }),
    });
    const txt = await r.text();
    let resp = {};
    try { resp = JSON.parse(txt); } catch (_) { resp = { error: txt }; }
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Pronunciation overrides saved. Restart TTS to fully apply.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

async function _cfgLoadResponseBehavior() {
  const body = document.getElementById('cfg-response-behavior-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _responseBehaviorPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _responseBehaviorPopulate(data) {
  const body = document.getElementById('cfg-response-behavior-body');
  if (!body) return;
  const globalMode = (typeof data.response_mode === 'string') ? data.response_mode : 'LLM';
  const perEvent = (data.response_mode_per_event && typeof data.response_mode_per_event === 'object')
    ? data.response_mode_per_event : {};
  const MODES = [
    { value: 'LLM',      label: 'LLM (planner speech, pass-through)' },
    { value: 'LLM_safe', label: 'LLM (safe, no device names)' },
    { value: 'quip',     label: 'Quip (pre-written library)' },
    { value: 'chime',    label: 'Chime (sound file)' },
    { value: 'silent',   label: 'Silent (no reply)' },
  ];
  const EVENT_ROWS = [
    { key: 'command_ack',  label: 'Command acknowledgement',  desc: 'Replies after a light / switch / scene command fires.' },
    { key: 'query_answer', label: 'Query answer',             desc: 'Replies to "is the kitchen on?" and similar.' },
    { key: 'ambient_cue',  label: 'Ambient cue',              desc: 'Replies to "it\'s too dark", "time to read".' },
    { key: 'error',        label: 'Error / failure',          desc: 'Replies when a transition did not land.' },
  ];
  function modeSelect(id, value) {
    let h = '<select id="' + id + '">';
    h += '<option value="">&mdash; inherit global &mdash;</option>';
    MODES.forEach(m => {
      const sel = (m.value === value) ? ' selected' : '';
      h += '<option value="' + m.value + '"' + sel + '>' + escHtml(m.label) + '</option>';
    });
    h += '</select>';
    return h;
  }
  let html = '';
  html += '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">'
    +   '<label class="cfg-field-label" for="cfg-rb-global" style="margin:0;min-width:140px;">Global mode</label>'
    +   '<select id="cfg-rb-global" style="flex:1;min-width:200px;">';
  MODES.forEach(m => {
    const sel = (m.value === globalMode) ? ' selected' : '';
    html += '<option value="' + m.value + '"' + sel + '>' + escHtml(m.label) + '</option>';
  });
  html += '</select></div>';
  html += '<div class="cfg-field-label" style="margin-top:12px;">Per-event override</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Leave rows at <em>inherit global</em> unless you want a specific category to behave differently (for example, '
    +   'silent command acknowledgements but LLM replies for queries).'
    + '</div>';
  EVENT_ROWS.forEach(row => {
    const current = perEvent[row.key] || '';
    html += '<div class="cfg-field" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px;">'
      +   '<label class="cfg-field-label" style="margin:0;min-width:180px;flex-shrink:0;" for="cfg-rb-ev-' + row.key + '">'
      +     escHtml(row.label)
      +   '</label>'
      +   modeSelect('cfg-rb-ev-' + row.key, current)
      + '</div>'
      + '<div class="cfg-field-desc" style="margin:-4px 0 4px 190px;">' + escHtml(row.desc) + '</div>';
  });
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveResponseBehavior()">Save Response behavior</button>'
    + '<span id="cfg-save-result-rb" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
}

async function cfgSaveResponseBehavior() {
  const resultEl = document.getElementById('cfg-save-result-rb');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const globalEl = document.getElementById('cfg-rb-global');
  const globalMode = globalEl ? String(globalEl.value || 'LLM') : 'LLM';
  const perEvent = {};
  ['command_ack', 'query_answer', 'ambient_cue', 'error'].forEach(k => {
    const el = document.getElementById('cfg-rb-ev-' + k);
    const v = el ? String(el.value || '') : '';
    if (v) perEvent[k] = v;
  });
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        response_mode: globalMode,
        response_mode_per_event: perEvent,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Response behavior saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

function cfgBuildForm(obj, section, prefix, skipKeys) {
  let html = '';
  for (const [key, value] of Object.entries(obj)) {
    // Top-level skip list: callers pass keys that belong to a dedicated
    // page (e.g. `ssl` has its own tab) or are env-only. Only checked at
    // the top level so a nested field named `ssl` inside another group
    // is not accidentally hidden.
    if (skipKeys && !prefix && skipKeys.indexOf(key) !== -1) continue;
    const path = prefix ? prefix + '.' + key : key;
    const fieldId = 'cfg-' + section + '-' + path.replace(/\./g, '-');
    const meta = FIELD_META[path] || {};
    // Phase 6 user-friendly pass: hidden-flagged fields stay in the schema
    // (Raw YAML / env still drive them) but disappear from the friendly
    // form so non-technical users aren't asked to touch deprecated paths,
    // env-only fields, or settings that have no UI-writable effect.
    if (meta.hidden) continue;
    const label = meta.label || key;
    const desc = meta.desc || '';
    const isAdvanced = meta.advanced === true;
    const advAttr = isAdvanced ? ' data-advanced="true"' : '';

    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      // Skip the group entirely if every child is hidden — otherwise we'd
      // render an empty <div class="cfg-group"> with just a heading, which
      // is what Commit 1 explicitly set out to avoid elsewhere.
      const childKeys = Object.keys(value);
      const visibleChildKeys = childKeys.filter(k => {
        const childPath = path ? path + '.' + k : k;
        return !((FIELD_META[childPath] || {}).hidden === true);
      });
      if (visibleChildKeys.length === 0) continue;
      // Check if every VISIBLE child is advanced — if so, the whole group
      // can collapse behind the Advanced toggle. Hidden children don't
      // factor in; they're never rendered either way.
      const groupAdvanced = visibleChildKeys.every(k => {
        const childPath = path ? path + '.' + k : k;
        return (FIELD_META[childPath] || {}).advanced === true;
      });
      const gAdvAttr = groupAdvanced ? ' data-advanced="true"' : '';
      html += '<div class="cfg-group"' + gAdvAttr + '><div class="cfg-group-title">' + escHtml(key) + '</div>';
      html += cfgBuildForm(value, section, path, skipKeys);
      html += '</div>';
    } else if (Array.isArray(value)) {
      // Skip arrays of objects (handled by custom renderers)
      if (value.length > 0 && typeof value[0] === 'object') continue;
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<input id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="array"'
        + ' value="' + escAttr(value.join(', ')) + '" placeholder="comma-separated values">'
        + '</div>';
    } else if (typeof value === 'boolean') {
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<select id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="bool">'
        + '<option value="true"' + (value ? ' selected' : '') + '>true</option>'
        + '<option value="false"' + (!value ? ' selected' : '') + '>false</option>'
        + '</select></div>';
    } else if (meta.options && Array.isArray(meta.options)) {
      // Dropdown select from predefined options
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<select id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="' + typeof value + '">';
      for (const opt of meta.options) {
        const sel = (String(value) === String(opt)) ? ' selected' : '';
        html += '<option value="' + escAttr(opt) + '"' + sel + '>' + escHtml(opt) + '</option>';
      }
      html += '</select></div>';
    } else {
      const inputType = (meta.type === 'password') ? 'password' : (typeof value === 'number' ? 'number' : 'text');
      const step = typeof value === 'number' && !Number.isInteger(value) ? ' step="any"' : '';
      let displayVal = value ?? '';
      let hintHtml = '';
      // Path masking
      if (meta.pathMask && typeof displayVal === 'string' && displayVal.startsWith(meta.pathMask)) {
        const fullPath = displayVal;
        displayVal = displayVal.slice(meta.pathMask.length);
        hintHtml = '<div class="cfg-field-hint">Full path: ' + escHtml(fullPath) + '</div>';
      }
      html += '<div class="cfg-field"' + advAttr + '>'
        + '<label class="cfg-field-label">' + escHtml(label) + '</label>';
      if (desc) html += '<div class="cfg-field-desc">' + escHtml(desc) + '</div>';
      html += '<input id="' + fieldId + '" data-path="' + escAttr(path) + '" data-type="' + typeof value + '"'
        + (meta.pathMask ? ' data-path-mask="' + escAttr(meta.pathMask) + '"' : '')
        + ' type="' + inputType + '"' + step + ' value="' + escAttr(String(displayVal)) + '">'
        + hintHtml + '</div>';
    }
  }
  return html;
}

/* â”€â”€ Services custom renderer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

// Decide which discovery endpoint a given service should use.
// Ollama URLs go through /api/tags; TTS URLs through /v1/voices.
function _svcDiscoverKind(key) {
  if (key === 'tts') return 'voices';
  if (key.indexOf('ollama') === 0) return 'ollama';
  return null;
}

// Services that are deprecated / have no operator-facing effect — kept
// in the schema for backward compatibility but hidden from the friendly
// Services grid. Raw YAML still shows them.
const SERVICES_HIDDEN = new Set(['gladys_api']);

function cfgRenderServices(data) {
  let html = '<div class="service-grid">';
  for (const [key, svc] of Object.entries(data)) {
    if (SERVICES_HIDDEN.has(key)) continue;
    const name = SERVICE_NAMES[key] || key;
    const urlId = 'cfg-services-' + key + '-url';
    const discoverKind = _svcDiscoverKind(key);
    const hasVoice = (key === 'tts' && svc.voice !== undefined);
    const hasModel = (svc.model !== undefined) || (discoverKind === 'ollama');
    html += '<div class="service-card">'
      + '<div class="service-card-header">'
      + '<span class="svc-health-dot" id="svc-dot-' + key + '"></span>'
      + '<span class="service-card-name">' + escHtml(name) + '</span>'
      + '</div>'
      + '<div class="cfg-field" style="margin-bottom:6px;">'
      + '<label class="cfg-field-label">URL</label>'
      + '<div class="svc-url-row">'
      +   '<input id="' + urlId + '" data-path="' + key + '.url" data-type="string" value="' + escAttr(svc.url || '') + '"'
      +     (discoverKind ? ' onblur="svcUrlBlur(\'' + escAttr(key) + '\')"' : '')
      +   '>';
    if (discoverKind) {
      html += '<button type="button" class="svc-discover-btn" title="Discover from upstream" onclick="svcDiscover(\'' + escAttr(key) + '\')">&#x21bb; Discover</button>';
    }
    html +=   '<span class="svc-discover-status" id="svc-status-' + key + '"></span>'
      + '</div>'
      + '</div>';
    if (hasVoice) {
      html += '<div class="cfg-field" style="margin-bottom:6px;">'
        + '<label class="cfg-field-label">Voice</label>'
        + '<select id="cfg-services-' + key + '-voice" data-path="' + key + '.voice" data-type="string" class="svc-dropdown">'
        +   '<option value="' + escAttr(svc.voice || '') + '" selected>' + escHtml(svc.voice || '(none)') + '</option>'
        + '</select>'
        + '</div>';
    }
    if (hasModel) {
      html += '<div class="cfg-field" style="margin-bottom:0;">'
        + '<label class="cfg-field-label">Model</label>'
        + '<select id="cfg-services-' + key + '-model" data-path="' + key + '.model" data-type="string" class="svc-dropdown">'
        +   '<option value="' + escAttr(svc.model || '') + '" selected>' + escHtml(svc.model || '(click Discover to list)') + '</option>'
        + '</select>'
        + '</div>';
    }
    html += '</div>';
  }
  html += '</div>';
  // Ping + seed dropdowns from current URLs.
  setTimeout(() => cfgPingServices(data), 100);
  return html;
}

// Map a service grid key to the probe kind discover_health uses.
// Ollama endpoints use /api/tags, TTS uses /v1/voices, STT uses
// /health, GLaDOS-own services use /health. Without this hint,
// every Ollama / TTS dot is false-red because /health returns 404.
function _svcHealthKind(key) {
  if (key.indexOf('ollama') === 0) return 'ollama';
  if (key === 'tts') return 'tts';
  if (key === 'stt') return 'stt';
  if (key === 'api_wrapper' || key === 'vision') return key;
  return null;
}

async function cfgPingServices(data) {
  for (const key of Object.keys(data)) {
    const dot = document.getElementById('svc-dot-' + key);
    if (!dot) continue;
    const url = (data[key].url || '').replace(/\/$/, '');
    if (!url) { dot.className = 'svc-health-dot err'; continue; }
    try {
      const hint = _svcHealthKind(key);
      const qs = 'url=' + encodeURIComponent(url)
               + (hint ? '&kind=' + encodeURIComponent(hint) : '');
      const r = await fetch('/api/discover/health?' + qs,
                             { signal: AbortSignal.timeout(3500) });
      const d = await r.json();
      dot.className = 'svc-health-dot ' + (d.ok ? 'ok' : 'err');
      if (d.latency_ms != null) dot.title = d.latency_ms + ' ms';
    } catch(e) {
      dot.className = 'svc-health-dot err';
    }
  }
}

// ── Service auto-discovery (Phase 5) ────────────────────────────
let _svcBlurTimers = {};

function svcUrlBlur(key) {
  // Debounce blur-triggered discovery so rapid tab-throughs don't fire
  // N simultaneous upstream calls. First one wins; subsequent blurs
  // inside 300ms are dropped.
  if (_svcBlurTimers[key]) clearTimeout(_svcBlurTimers[key]);
  _svcBlurTimers[key] = setTimeout(() => { svcDiscover(key); }, 300);
}

async function svcDiscover(key) {
  const kind = _svcDiscoverKind(key);
  if (!kind) return;
  const urlInput = document.getElementById('cfg-services-' + key + '-url');
  const status = document.getElementById('svc-status-' + key);
  if (!urlInput) return;
  const url = (urlInput.value || '').trim().replace(/\/$/, '');
  if (!url) {
    if (status) status.textContent = '';
    return;
  }
  if (status) { status.className = 'svc-discover-status'; status.textContent = 'discovering…'; }
  try {
    const r = await fetch('/api/discover/' + kind + '?url=' + encodeURIComponent(url));
    const data = await r.json();
    if (!r.ok) {
      if (status) { status.className = 'svc-discover-status err'; status.textContent = data.error || 'failed'; }
      return;
    }
    if (kind === 'ollama') {
      _svcPopulateDropdown('cfg-services-' + key + '-model', (data.models || []).map(m => m.name));
      if (status) { status.className = 'svc-discover-status ok'; status.textContent = data.count + ' models'; }
    } else if (kind === 'voices') {
      _svcPopulateDropdown('cfg-services-' + key + '-voice', (data.voices || []).map(v => v.name));
      if (status) { status.className = 'svc-discover-status ok'; status.textContent = data.count + ' voices'; }
    }
    // Refresh the dot too — the URL may have changed.
    const dot = document.getElementById('svc-dot-' + key);
    if (dot) {
      try {
        const hint = _svcHealthKind(key);
        const qs = 'url=' + encodeURIComponent(url)
                 + (hint ? '&kind=' + encodeURIComponent(hint) : '');
        const hr = await fetch('/api/discover/health?' + qs);
        const hd = await hr.json();
        dot.className = 'svc-health-dot ' + (hd.ok ? 'ok' : 'err');
        if (hd.latency_ms != null) dot.title = hd.latency_ms + ' ms';
      } catch(e) {}
    }
  } catch(e) {
    if (status) { status.className = 'svc-discover-status err'; status.textContent = 'error'; }
  }
}

function _svcPopulateDropdown(id, options) {
  const el = document.getElementById(id);
  if (!el) return;
  const current = el.value;
  const values = new Set(options || []);
  if (current) values.add(current);  // keep current selection even if upstream hasn't loaded it
  const sorted = Array.from(values).sort();
  let html = '';
  for (const v of sorted) {
    const sel = (v === current) ? ' selected' : '';
    html += '<option value="' + escAttr(v) + '"' + sel + '>' + escHtml(v) + '</option>';
  }
  if (html === '') {
    html = '<option value="">(no options returned)</option>';
  }
  el.innerHTML = html;
}

/* â”€â”€ Personality custom renderer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

function cfgRenderPersonality(data) {
  let html = '';

  // Default TTS params (advanced)
  if (data.default_tts) {
    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">Default TTS Parameters</div>';
    html += cfgBuildForm(data.default_tts, 'personality', 'default_tts');
    html += '</div>';
  }

  // Attitudes table (read-only display)
  if (data.attitudes && data.attitudes.length > 0) {
    html += '<div class="cfg-group"><div class="cfg-group-title">Attitudes (' + data.attitudes.length + ')</div>';
    html += '<table class="att-table"><tr><th>Tag</th><th>Label</th><th>Weight</th><th>TTS Params</th></tr>';
    for (const a of data.attitudes) {
      const tts = a.tts || {};
      const ttsStr = 'L:' + (tts.length_scale ?? '-') + ' N:' + (tts.noise_scale ?? '-') + ' W:' + (tts.noise_w ?? '-');
      html += '<tr>'
        + '<td class="tag-cell">' + escHtml(a.tag || '') + '</td>'
        + '<td>' + escHtml(a.label || '') + '</td>'
        + '<td>' + (a.weight ?? 1.0) + '</td>'
        + '<td class="tts-cell">' + escHtml(ttsStr) + '</td>'
        + '</tr>';
    }
    html += '</table>';
    html += '<div style="font-size:0.73rem;color:var(--text-muted);margin-top:6px;">Edit attitudes via Raw YAML tab</div>';
    html += '</div>';
  }

  // Preprompt entries
  if (data.preprompt && data.preprompt.length > 0) {
    html += '<div class="cfg-group"><div class="cfg-group-title">Preprompt Messages</div>';
    for (let i = 0; i < data.preprompt.length; i++) {
      const entry = data.preprompt[i];
      for (const role of ['system', 'user', 'assistant']) {
        if (entry[role] != null) {
          html += '<div class="preprompt-pair">'
            + '<div class="preprompt-role">' + role + '</div>'
            + '<textarea class="preprompt-text" data-preprompt="' + i + '-' + role + '">' + escHtml(entry[role]) + '</textarea>'
            + '</div>';
        }
      }
    }
    html += '</div>';
  }

  // HEXACO (advanced)
  if (data.hexaco) {
    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">HEXACO Personality Traits</div>';
    for (const [k, v] of Object.entries(data.hexaco)) {
      const fieldId = 'cfg-personality-hexaco-' + k;
      html += '<div class="cfg-field">'
        + '<label class="cfg-field-label">' + escHtml(k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())) + '</label>'
        + '<input id="' + fieldId + '" data-path="hexaco.' + k + '" data-type="number" type="number" step="any" value="' + v + '">'
        + '</div>';
    }
    html += '</div>';
  }

  // Emotion (advanced)
  if (data.emotion) {
    html += '<div class="cfg-group" data-advanced="true"><div class="cfg-group-title">Emotion Model</div>';
    for (const [k, v] of Object.entries(data.emotion)) {
      const fieldId = 'cfg-personality-emotion-' + k;
      if (typeof v === 'boolean') {
        html += '<div class="cfg-field">'
          + '<label class="cfg-field-label">' + escHtml(k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())) + '</label>'
          + '<select id="' + fieldId + '" data-path="emotion.' + k + '" data-type="bool">'
          + '<option value="true"' + (v ? ' selected' : '') + '>true</option>'
          + '<option value="false"' + (!v ? ' selected' : '') + '>false</option>'
          + '</select></div>';
      } else {
        html += '<div class="cfg-field">'
          + '<label class="cfg-field-label">' + escHtml(k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())) + '</label>'
          + '<input id="' + fieldId + '" data-path="emotion.' + k + '" data-type="number" type="number" step="any" value="' + v + '">'
          + '</div>';
      }
    }
    html += '</div>';
  }

  // Phase 8.2 — Command recognition card. Separate card with its own
  // fetch / save cycle; writes to disambiguation.yaml, same file as the
  // Disambiguation rules card under Integrations → Home Assistant.
  html += ''
    + '<div class="card" id="cfg-cmdrec-card" style="margin-top:14px;">'
    +   '<div class="cfg-subsection-title">Command recognition</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Tunes the Tier&nbsp;1/2 precheck gate. When an utterance matches any of these signals, '
    +     'GLaDOS attempts a home-control intent before falling through to chitchat. Shipped defaults '
    +     '(command verbs like <code>darken</code>, <code>dim</code>, <code>bump</code> and ambient '
    +     'phrases like <em>&ldquo;it&rsquo;s too dark&rdquo;</em>) are always active; the fields below add extras.'
    +   '</div>'
    +   '<div id="cfg-cmdrec-body">Loading command recognition rules&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadCommandRecognition, 0);

  // Phase 8.7c — Quip library editor. Tree on the left, textarea on
  // the right for the currently-selected file. Save button writes to
  // disk through PUT /api/quips. Live-test card at the bottom shows
  // which line the selector would pick right now.
  html += ''
    + '<div class="card" id="cfg-quip-card" style="margin-top:14px;">'
    +   '<div class="cfg-subsection-title">Quip library</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'When <strong>Response behavior</strong> is set to <em>quip</em> (see Audio &amp; Speakers), GLaDOS '
    +     'replies using a line from the on-disk library under <code>configs/quips/</code>. Each file holds '
    +     'one quip per line; <code>#</code> lines are comments. Edit directly below or via the raw files.'
    +   '</div>'
    +   '<div id="cfg-quip-body">Loading quip library&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadQuips, 0);

  // Phase 8.14 — Portal canon library editor. Retrieval-augmented
  // facts the model pulls when a trigger keyword fires. Tree on the
  // left by topic, textarea on the right for the selected file,
  // dry-run panel at the bottom to preview retrieval.
  html += ''
    + '<div class="card" id="cfg-canon-card" style="margin-top:14px;">'
    +   '<div class="cfg-subsection-title">Canon library</div>'
    +   '<div class="cfg-field-desc" style="margin-bottom:10px;">'
    +     'Curated Portal 1/2 facts under <code>configs/canon/</code>. GLaDOS retrieves relevant '
    +     'entries per-turn when a trigger keyword fires (potato, Wheatley, Caroline, Cave, Aperture, '
    +     'turret opera, combustible lemon, moon rock, etc.). Entries are blank-line-separated; '
    +     '<code>#</code> lines are comments. Saves reload into the running engine immediately.'
    +   '</div>'
    +   '<div id="cfg-canon-body">Loading canon library&hellip;</div>'
    + '</div>';
  setTimeout(_cfgLoadCanon, 0);

  return html;
}

// Phase 8.7c — Quip library editor.

let _quipSelectedPath = null;

async function _cfgLoadQuips() {
  const body = document.getElementById('cfg-quip-body');
  if (!body) return;
  try {
    const r = await fetch('/api/quips');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _quipRenderTree(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _quipRenderTree(data) {
  const body = document.getElementById('cfg-quip-body');
  if (!body) return;
  const files = data.files || [];
  // Group by category/intent for the tree.
  const tree = {};
  files.forEach(f => {
    const parts = f.path.split('/');
    if (parts.length >= 3) {
      const cat = parts[0], intent = parts[1], leaf = parts.slice(2).join('/');
      tree[cat] = tree[cat] || {};
      tree[cat][intent] = tree[cat][intent] || [];
      tree[cat][intent].push({ leaf, path: f.path, count: f.quip_count });
    } else if (parts.length === 2) {
      // category/file.txt flat layout (outcome_modifier, global)
      const cat = parts[0];
      tree[cat] = tree[cat] || {};
      tree[cat]['_'] = tree[cat]['_'] || [];
      tree[cat]['_'].push({ leaf: parts[1], path: f.path, count: f.quip_count });
    }
  });
  let html = '<div style="display:flex;gap:14px;flex-wrap:wrap;">';
  // Left: tree
  html += '<div id="cfg-quip-tree" style="flex:1;min-width:260px;max-height:420px;overflow-y:auto;border:1px solid #333;border-radius:4px;padding:8px;background:#1a1a1a;">';
  Object.keys(tree).sort().forEach(cat => {
    html += '<div style="font-weight:bold;margin-top:6px;color:#ffa94d;">' + escHtml(cat) + '</div>';
    Object.keys(tree[cat]).sort().forEach(intent => {
      if (intent !== '_') {
        html += '<div style="margin-left:10px;margin-top:4px;color:#9cdcfe;font-size:0.9em;">' + escHtml(intent) + '</div>';
      }
      tree[cat][intent].forEach(f => {
        const indent = (intent === '_') ? 10 : 22;
        html += '<div style="margin-left:' + indent + 'px;cursor:pointer;padding:2px 4px;border-radius:2px;" '
          + 'onclick="_quipLoad(\'' + escAttr(f.path) + '\')" '
          + 'onmouseover="this.style.background=\'#2a2a2a\'" '
          + 'onmouseout="this.style.background=\'transparent\'">'
          + '&rarr; ' + escHtml(f.leaf) + ' <span style="color:#888;font-size:0.85em;">(' + f.count + ')</span>'
          + '</div>';
      });
    });
  });
  if (!Object.keys(tree).length) {
    html += '<div class="cfg-field-desc">Library is empty. Create a file with <em>New file</em> below.</div>';
  }
  html += '</div>';
  // Right: editor pane
  html += '<div style="flex:2;min-width:320px;display:flex;flex-direction:column;gap:8px;">';
  html += '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-quip-path" placeholder="command_ack/turn_on/normal.txt" style="flex:1;min-width:220px;">'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_quipLoadFromPath()">Open</button>'
    + '<button type="button" class="cfg-save-btn" style="background:#a33;" onclick="_quipDelete()">Delete</button>'
    + '</div>';
  html += '<textarea id="cfg-quip-editor" style="width:100%;min-height:280px;font-family:monospace;background:#1a1a1a;color:#ddd;border:1px solid #333;padding:8px;"></textarea>';
  html += '<div class="cfg-save-row"><button class="cfg-save-btn" onclick="_quipSave()">Save file</button>'
    + '<span id="cfg-quip-save-result" class="cfg-result"></span></div>';
  html += '</div>';  // editor pane
  html += '</div>';  // flex wrapper
  // Dry-run card
  html += '<div class="cfg-field-label" style="margin-top:14px;">Live test</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">Pick a category + intent + mood and see which line the composer would emit right now.</div>'
    + '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">'
    + '<select id="cfg-quip-test-cat">'
    +   '<option value="command_ack">command_ack</option>'
    +   '<option value="query_answer">query_answer</option>'
    +   '<option value="ambient_cue">ambient_cue</option>'
    +   '<option value="error">error</option>'
    + '</select>'
    + '<input type="text" id="cfg-quip-test-intent" placeholder="turn_on / turn_off / ..." value="turn_on" style="min-width:140px;">'
    + '<select id="cfg-quip-test-mood">'
    +   '<option value="normal">normal</option>'
    +   '<option value="cranky">cranky</option>'
    +   '<option value="amused">amused</option>'
    + '</select>'
    + '<button class="cfg-save-btn" style="background:#333;" onclick="_quipDryRun()">Pick a line</button>'
    + '</div>'
    + '<div id="cfg-quip-test-result" style="margin-top:8px;font-family:monospace;color:#9cdcfe;"></div>';
  body.innerHTML = html;
}

async function _quipLoad(path) {
  const ed = document.getElementById('cfg-quip-editor');
  const pathEl = document.getElementById('cfg-quip-path');
  try {
    const r = await fetch('/api/quips?path=' + encodeURIComponent(path));
    if (!r.ok) { showToast('Load failed (' + r.status + ')', 'warn'); return; }
    const data = await r.json();
    if (ed) ed.value = (data.lines || []).join('\n');
    if (pathEl) pathEl.value = data.path || path;
    _quipSelectedPath = data.path || path;
  } catch (e) {
    showToast('Load error: ' + e.message, 'warn');
  }
}

function _quipLoadFromPath() {
  const pathEl = document.getElementById('cfg-quip-path');
  if (pathEl && pathEl.value.trim()) _quipLoad(pathEl.value.trim());
}

async function _quipSave() {
  const pathEl = document.getElementById('cfg-quip-path');
  const ed = document.getElementById('cfg-quip-editor');
  const result = document.getElementById('cfg-quip-save-result');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) { showToast('Enter a path first', 'warn'); return; }
  const lines = (ed ? ed.value : '').split('\n');
  if (result) { result.textContent = 'Saving...'; result.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/quips', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path, lines }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (result) result.textContent = '';
      showToast('Saved: ' + path + ' (' + (resp.quip_count || 0) + ' quips)', 'success');
      _cfgLoadQuips();  // refresh tree counts
    } else if (result) {
      result.textContent = resp.error || ('Error (' + r.status + ')');
      result.className = 'cfg-result err';
    }
  } catch (e) {
    if (result) { result.textContent = 'Error: ' + e.message; result.className = 'cfg-result err'; }
  }
}

async function _quipDelete() {
  const pathEl = document.getElementById('cfg-quip-path');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) return;
  if (!confirm('Delete ' + path + '?')) return;
  try {
    const r = await fetch('/api/quips?path=' + encodeURIComponent(path), {method: 'DELETE'});
    if (r.ok) {
      showToast('Deleted: ' + path, 'success');
      const ed = document.getElementById('cfg-quip-editor');
      if (ed) ed.value = '';
      _cfgLoadQuips();
    } else {
      showToast('Delete failed (' + r.status + ')', 'warn');
    }
  } catch (e) {
    showToast('Delete error: ' + e.message, 'warn');
  }
}

async function _quipDryRun() {
  const catEl = document.getElementById('cfg-quip-test-cat');
  const intentEl = document.getElementById('cfg-quip-test-intent');
  const moodEl = document.getElementById('cfg-quip-test-mood');
  const resEl = document.getElementById('cfg-quip-test-result');
  const payload = {
    event_category: catEl ? catEl.value : 'command_ack',
    intent: intentEl ? intentEl.value : 'turn_on',
    mood: moodEl ? moodEl.value : 'normal',
  };
  try {
    const r = await fetch('/api/quips/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (resEl) {
      if (data.line) {
        resEl.textContent = '→ ' + data.line;
        resEl.style.color = '#9cdcfe';
      } else if (data.library_empty) {
        resEl.textContent = 'Library is empty — composer would fall back to LLM speech.';
        resEl.style.color = '#f66';
      } else {
        resEl.textContent = 'No line matched — composer would fall back to LLM speech for this request.';
        resEl.style.color = '#fa5';
      }
    }
  } catch (e) {
    if (resEl) { resEl.textContent = 'Error: ' + e.message; resEl.style.color = '#f66'; }
  }
}

// Phase 8.14 — Canon library editor. Tree of topic files on the left,
// textarea on the right for the whole-file content, dry-run panel at
// the bottom that shows gate firing + retrieved entries.

let _canonSelectedPath = null;

async function _cfgLoadCanon() {
  const body = document.getElementById('cfg-canon-body');
  if (!body) return;
  try {
    const r = await fetch('/api/canon');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _canonRenderTree(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error: ' + escHtml(e.message) + '</div>';
  }
}

function _canonRenderTree(data) {
  const body = document.getElementById('cfg-canon-body');
  if (!body) return;
  const files = data.files || [];
  let html = '<div style="display:flex;gap:14px;flex-wrap:wrap;">';
  html += '<div id="cfg-canon-tree" style="flex:1;min-width:220px;max-height:420px;overflow-y:auto;border:1px solid #333;border-radius:4px;padding:8px;background:#1a1a1a;">';
  if (!files.length) {
    html += '<div class="cfg-field-desc">No canon files yet. Enter a <em>&lt;topic&gt;.txt</em> path and click Save to create one.</div>';
  } else {
    files.forEach(f => {
      html += '<div style="cursor:pointer;padding:3px 4px;border-radius:2px;" '
        + 'onclick="_canonLoad(\'' + escAttr(f.path) + '\')" '
        + 'onmouseover="this.style.background=\'#2a2a2a\'" '
        + 'onmouseout="this.style.background=\'transparent\'">'
        + '&rarr; ' + escHtml(f.path) + ' <span style="color:#888;font-size:0.85em;">(' + (f.entry_count || 0) + ' entries)</span>'
        + '</div>';
    });
  }
  html += '</div>';
  html += '<div style="flex:2;min-width:320px;display:flex;flex-direction:column;gap:8px;">';
  html += '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-canon-path" placeholder="<topic>.txt" style="flex:1;min-width:220px;">'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_canonLoadFromPath()">Open</button>'
    + '<button type="button" class="cfg-save-btn" style="background:#a33;" onclick="_canonDelete()">Delete</button>'
    + '</div>';
  html += '<textarea id="cfg-canon-editor" style="width:100%;min-height:300px;font-family:monospace;background:#1a1a1a;color:#ddd;border:1px solid #333;padding:8px;" placeholder="# Optional comment line.\n\nFirst canon entry. One to three sentences.\n\nSecond canon entry. Blank line separates."></textarea>';
  html += '<div class="cfg-save-row"><button class="cfg-save-btn" onclick="_canonSave()">Save file</button>'
    + '<span id="cfg-canon-save-result" class="cfg-result"></span></div>';
  html += '</div>';
  html += '</div>';
  html += '<div class="cfg-field-label" style="margin-top:14px;">Retrieval test</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">Enter an utterance; the panel shows whether the canon gate fires and which entries would be injected into the LLM context.</div>'
    + '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
    + '<input type="text" id="cfg-canon-test-utt" placeholder="How did you cope with being a potato?" style="flex:1;min-width:280px;">'
    + '<button class="cfg-save-btn" style="background:#333;" onclick="_canonDryRun()">Retrieve</button>'
    + '</div>'
    + '<div id="cfg-canon-test-result" style="margin-top:8px;font-family:monospace;font-size:0.9em;color:#ddd;"></div>';
  body.innerHTML = html;
}

async function _canonLoad(path) {
  const ed = document.getElementById('cfg-canon-editor');
  const pathEl = document.getElementById('cfg-canon-path');
  try {
    const r = await fetch('/api/canon?path=' + encodeURIComponent(path));
    if (!r.ok) { showToast('Load failed (' + r.status + ')', 'warn'); return; }
    const data = await r.json();
    if (ed) ed.value = data.text || '';
    if (pathEl) pathEl.value = data.path || path;
    _canonSelectedPath = data.path || path;
  } catch (e) {
    showToast('Load error: ' + e.message, 'warn');
  }
}

function _canonLoadFromPath() {
  const pathEl = document.getElementById('cfg-canon-path');
  if (pathEl && pathEl.value.trim()) _canonLoad(pathEl.value.trim());
}

async function _canonSave() {
  const pathEl = document.getElementById('cfg-canon-path');
  const ed = document.getElementById('cfg-canon-editor');
  const result = document.getElementById('cfg-canon-save-result');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) { showToast('Enter a path first', 'warn'); return; }
  const text = ed ? ed.value : '';
  if (result) { result.textContent = 'Saving...'; result.className = 'cfg-result'; }
  try {
    const r = await fetch('/api/canon', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path, text }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (result) result.textContent = '';
      const reloadedNote = resp.reloaded ? ' (live)' : ' (reload failed — restart to apply)';
      showToast('Saved: ' + path + ' (' + (resp.entry_count || 0) + ' entries)' + reloadedNote, 'success');
      _cfgLoadCanon();
    } else if (result) {
      result.textContent = resp.error || ('Error (' + r.status + ')');
      result.className = 'cfg-result err';
    }
  } catch (e) {
    if (result) { result.textContent = 'Error: ' + e.message; result.className = 'cfg-result err'; }
  }
}

async function _canonDelete() {
  const pathEl = document.getElementById('cfg-canon-path');
  const path = pathEl ? pathEl.value.trim() : '';
  if (!path) return;
  if (!confirm('Delete ' + path + '?')) return;
  try {
    const r = await fetch('/api/canon?path=' + encodeURIComponent(path), {method: 'DELETE'});
    if (r.ok) {
      showToast('Deleted: ' + path, 'success');
      const ed = document.getElementById('cfg-canon-editor');
      if (ed) ed.value = '';
      _cfgLoadCanon();
    } else {
      showToast('Delete failed (' + r.status + ')', 'warn');
    }
  } catch (e) {
    showToast('Delete error: ' + e.message, 'warn');
  }
}

async function _canonDryRun() {
  const uttEl = document.getElementById('cfg-canon-test-utt');
  const resEl = document.getElementById('cfg-canon-test-result');
  const utterance = uttEl ? uttEl.value.trim() : '';
  if (!utterance) { showToast('Enter an utterance first', 'warn'); return; }
  try {
    const r = await fetch('/api/canon/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ utterance }),
    });
    const data = await r.json();
    if (!resEl) return;
    let html = '<div style="margin-bottom:6px;">Gate: '
      + (data.gate_fired ? '<span style="color:#6f6;">FIRED</span>' : '<span style="color:#f96;">skipped</span>')
      + '</div>';
    const entries = data.entries || [];
    if (!entries.length) {
      html += '<div style="color:#f96;">No entries retrieved.</div>';
    } else {
      html += '<div>' + entries.length + ' entr' + (entries.length === 1 ? 'y' : 'ies') + ':</div>';
      entries.forEach(e => {
        const dist = (e.distance != null) ? (' <span style="color:#888;">[' + e.distance.toFixed(3) + ']</span>') : '';
        const topic = e.topic ? ' <span style="color:#ffa94d;">[' + escHtml(e.topic) + ']</span>' : '';
        html += '<div style="margin:4px 0;padding:4px 6px;border-left:2px solid #555;">'
          + topic + dist + '<br>' + escHtml(e.document || '')
          + '</div>';
      });
    }
    resEl.innerHTML = html;
  } catch (e) {
    if (resEl) { resEl.innerHTML = '<span style="color:#f66;">Error: ' + escHtml(e.message) + '</span>'; }
  }
}

// Phase 8.2 — Command recognition card fetch/render/save.

async function _cfgLoadCommandRecognition() {
  const body = document.getElementById('cfg-cmdrec-body');
  if (!body) return;
  try {
    const r = await fetch('/api/config/disambiguation');
    if (!r.ok) {
      body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Failed to load rules (' + r.status + ').</div>';
      return;
    }
    const data = await r.json();
    _cmdrecPopulate(data);
  } catch (e) {
    body.innerHTML = '<div class="cfg-field-desc" style="color:#d66;">Error loading rules: ' + escHtml(e.message) + '</div>';
  }
}

function _cmdrecPopulate(data) {
  const body = document.getElementById('cfg-cmdrec-body');
  if (!body) return;
  const verbs = Array.isArray(data.extra_command_verbs) ? data.extra_command_verbs : [];
  const patterns = Array.isArray(data.extra_ambient_patterns) ? data.extra_ambient_patterns : [];
  let html = '';
  html += '<div class="cfg-field-label" style="margin-top:4px;">Extra command verbs</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Single words (not phrases). Merged with the shipped defaults (darken, brighten, dim, lighten, '
    +   'bump, lower, raise, reduce, increase, soften, tone, crank, kill, douse, extinguish, illuminate, '
    +   'light, set, put, dial, slide, push, pull, close, open, shut, drop).'
    + '</div>'
    + '<div id="cfg-cmdrec-verbs" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_cmdrecAddVerb()">+ Add verb</button>';
  html += '<div class="cfg-field-label" style="margin-top:14px;">Extra ambient-state patterns (regex)</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Case-insensitive Python regex. Invalid patterns are rejected on save.'
    + '</div>'
    + '<div id="cfg-cmdrec-patterns" style="display:flex;flex-direction:column;gap:6px;margin-bottom:6px;"></div>'
    + '<button type="button" class="cfg-save-btn" style="background:#333;" onclick="_cmdrecAddPattern()">+ Add pattern</button>';
  html += '<div class="cfg-field-label" style="margin-top:18px;">Test input</div>'
    + '<div class="cfg-field-desc" style="margin-bottom:6px;">'
    +   'Type a phrase and see whether the current precheck (defaults + any edits above once saved) would recognise it.'
    + '</div>'
    + '<div style="display:flex;gap:6px;align-items:center;">'
    +   '<input type="text" id="cfg-cmdrec-test-input" placeholder="e.g. bump the living room up a bit" style="flex:1;">'
    +   '<button type="button" class="cfg-save-btn" onclick="_cmdrecTest()">Test</button>'
    + '</div>'
    + '<div id="cfg-cmdrec-test-result" class="cfg-field-desc" style="margin-top:8px;"></div>';
  html += '<div class="cfg-save-row" style="margin-top:14px;">'
    + '<button class="cfg-save-btn" onclick="cfgSaveCommandRecognition()">Save Command recognition</button>'
    + '<span id="cfg-save-result-cmdrec" class="cfg-result"></span>'
    + '</div>';
  body.innerHTML = html;
  const verbsHost = document.getElementById('cfg-cmdrec-verbs');
  verbs.forEach(v => _cmdrecRenderVerbRow(verbsHost, v));
  const patsHost = document.getElementById('cfg-cmdrec-patterns');
  patterns.forEach(p => _cmdrecRenderPatternRow(patsHost, p));
}

function _cmdrecRenderVerbRow(host, v) {
  const row = document.createElement('div');
  row.className = 'cfg-cmdrec-verb-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-cmdrec-verb" value="' + escAttr(v) + '" placeholder="e.g. nudge" style="flex:1;">'
    + '<button type="button" title="Remove verb" style="background:#a33;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _cmdrecRenderPatternRow(host, p) {
  const row = document.createElement('div');
  row.className = 'cfg-cmdrec-pattern-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = ''
    + '<input type="text" class="cfg-cmdrec-pattern" value="' + escAttr(p) + '" placeholder="e.g. \\\\bthe cats? (?:need|want)\\\\b" style="flex:1;font-family:monospace;">'
    + '<button type="button" title="Remove pattern" style="background:#a33;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">&times;</button>';
  const del = row.querySelector('button');
  if (del) del.addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _cmdrecAddVerb() {
  const host = document.getElementById('cfg-cmdrec-verbs');
  if (host) _cmdrecRenderVerbRow(host, '');
}

function _cmdrecAddPattern() {
  const host = document.getElementById('cfg-cmdrec-patterns');
  if (host) _cmdrecRenderPatternRow(host, '');
}

async function _cmdrecTest() {
  const input = document.getElementById('cfg-cmdrec-test-input');
  const resultEl = document.getElementById('cfg-cmdrec-test-result');
  if (!input || !resultEl) return;
  const utt = (input.value || '').trim();
  if (!utt) {
    resultEl.innerHTML = '<span style="color:#999;">Enter an utterance above.</span>';
    return;
  }
  resultEl.textContent = 'Testing...';
  try {
    const r = await fetch('/api/precheck/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({utterance: utt}),
    });
    const resp = await r.json();
    if (!r.ok) {
      resultEl.innerHTML = '<span style="color:#d66;">' + escHtml(resp.error || ('HTTP ' + r.status)) + '</span>';
      return;
    }
    const matches = !!resp.matches;
    const reasons = Array.isArray(resp.via) ? resp.via : [];
    const domains = Array.isArray(resp.domains) ? resp.domains : null;
    let out = '<div style="color:' + (matches ? '#6c6' : '#d66') + ';font-weight:bold;">'
      + (matches ? 'Recognised as a home command.' : 'Not recognised &mdash; falls to chitchat.')
      + '</div>';
    if (matches) {
      out += '<div>Matched via: <code>' + reasons.map(escHtml).join('</code>, <code>') + '</code></div>';
    }
    if (domains && domains.length) {
      out += '<div>Domain hints: <code>' + domains.map(escHtml).join('</code>, <code>') + '</code></div>';
    }
    resultEl.innerHTML = out;
  } catch (e) {
    resultEl.innerHTML = '<span style="color:#d66;">Error: ' + escHtml(e.message) + '</span>';
  }
}

async function cfgSaveCommandRecognition() {
  const resultEl = document.getElementById('cfg-save-result-cmdrec');
  if (resultEl) { resultEl.textContent = 'Saving...'; resultEl.className = 'cfg-result'; }
  const verbs = [];
  document.querySelectorAll('#cfg-cmdrec-verbs .cfg-cmdrec-verb-row .cfg-cmdrec-verb').forEach(el => {
    const v = (el.value || '').trim();
    if (v) verbs.push(v);
  });
  const patterns = [];
  document.querySelectorAll('#cfg-cmdrec-patterns .cfg-cmdrec-pattern-row .cfg-cmdrec-pattern').forEach(el => {
    const p = (el.value || '').trim();
    if (p) patterns.push(p);
  });
  try {
    const r = await fetch('/api/config/disambiguation', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        extra_command_verbs: verbs,
        extra_ambient_patterns: patterns,
      }),
    });
    const resp = await r.json();
    if (r.ok) {
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Error: ' + e.message; resultEl.className = 'cfg-result err'; }
  }
}

/* â”€â”€ Config form data collection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

function cfgCollectForm(section) {
  if (section === 'personality') return cfgCollectPersonality();
  if (section === 'services') return cfgCollectServices();

  const result = {};
  document.querySelectorAll('[id^="cfg-' + section + '-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else if (type === 'array') val = el.value.split(',').map(s => s.trim()).filter(Boolean);
    else {
      val = el.value;
      // Re-apply path mask
      if (el.dataset.pathMask && val && !val.startsWith(el.dataset.pathMask)) {
        val = el.dataset.pathMask + val;
      }
    }
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = val;
  });
  return result;
}

function cfgCollectServices() {
  const result = {};
  document.querySelectorAll('[id^="cfg-services-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = el.value;
  });
  return result;
}

function cfgCollectPersonality() {
  const result = {};
  // Collect standard fields
  document.querySelectorAll('[id^="cfg-personality-"]').forEach(el => {
    const path = el.dataset.path;
    if (!path) return;
    const type = el.dataset.type;
    let val;
    if (type === 'bool') val = el.value === 'true';
    else if (type === 'number') val = parseFloat(el.value);
    else val = el.value;
    const parts = path.split('.');
    let obj = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in obj)) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = val;
  });
  // Collect preprompt textareas
  const prepromptEls = document.querySelectorAll('[data-preprompt]');
  if (prepromptEls.length > 0) {
    const entries = {};
    prepromptEls.forEach(el => {
      const [idx, role] = el.dataset.preprompt.split('-');
      if (!entries[idx]) entries[idx] = {};
      entries[idx][role] = el.value;
    });
    result.preprompt = Object.values(entries);
  }
  // Preserve attitudes as-is (read-only in form view)
  if (_cfgData.personality && _cfgData.personality.attitudes) {
    result.attitudes = _cfgData.personality.attitudes;
  }
  return result;
}

async function cfgSaveSsl() {
  const result = document.getElementById('cfg-save-result');
  const data = _cfgData.global || {};
  if (!data.ssl) data.ssl = {};
  const getVal = (id, def) => {
    const el = document.getElementById(id);
    if (!el) return def;
    if (el.type === 'checkbox') return el.checked;
    return el.value;
  };
  data.ssl.enabled = getVal('ssl-enabled', false);
  data.ssl.domain = getVal('ssl-domain', '');
  data.ssl.acme_email = getVal('ssl-acme-email', '');
  data.ssl.acme_provider = getVal('ssl-acme-provider', 'cloudflare');
  data.ssl.acme_api_token = getVal('ssl-acme-token', '');
  data.ssl.use_letsencrypt = getVal('ssl-use-le', false);
  data.ssl.cert_path = getVal('ssl-cert-path', '/app/certs/cert.pem');
  data.ssl.key_path = getVal('ssl-key-path', '/app/certs/key.pem');
  result.textContent = 'Saving...';
  fetch('/api/config/global', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  }).then(r => r.json()).then(d => {
    if (d.ok) { result.textContent = 'Saved. Restart container for changes to take effect.'; result.style.color = '#6f6'; }
    else { result.textContent = 'Error: ' + (d.error || 'unknown'); result.style.color = '#f66'; }
  }).catch(e => { result.textContent = 'Error: ' + e; result.style.color = '#f66'; });
}

async function sslRefreshStatus() {
  try {
    const r = await fetch('/api/ssl/status');
    const d = await r.json();
    const statusEl = document.getElementById('ssl-status-display');
    if (!statusEl) return;
    const active = d.ssl_active ? '<span style="color:#6f6">Active (HTTPS)</span>' : '<span style="color:#f66">Inactive (HTTP)</span>';
    let html = '<div class="cfg-ssl-status">';
    html += '<div><strong>Status:</strong> ' + active + '</div>';
    if (d.cert_exists) {
      html += '<div><strong>Source:</strong> ' + escHtml(d.source) + '</div>';
      html += '<div><strong>Subject:</strong> ' + escHtml(d.subject || '-') + '</div>';
      html += '<div><strong>Issuer:</strong> ' + escHtml(d.issuer || '-') + '</div>';
      if (d.sans && d.sans.length) {
        html += '<div><strong>SANs:</strong> ' + d.sans.map(escHtml).join(', ') + '</div>';
      }
      html += '<div><strong>Issued:</strong> ' + escHtml((d.not_before || '').slice(0,10)) + '</div>';
      html += '<div><strong>Expires:</strong> ' + escHtml((d.not_after || '').slice(0,10)) + ' (' + d.days_remaining + ' days)</div>';
    } else {
      html += '<div>No certificate installed</div>';
    }
    html += '</div>';
    statusEl.innerHTML = html;
  } catch(e) {
    console.error('SSL status fetch failed:', e);
  }
}

async function sslRequestLetsEncrypt() {
  const resultEl = document.getElementById('ssl-request-result');
  resultEl.textContent = 'Requesting certificate from Lets Encrypt (30-60s)...';
  resultEl.style.color = '#ccc';
  try {
    const r = await fetch('/api/ssl/request', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      resultEl.innerHTML = '<div style="color:#6f6">' + escHtml(d.message) + '</div>' + (d.log ? '<pre style="font-size:11px;max-height:200px;overflow:auto;margin-top:8px;">' + escHtml(d.log) + '</pre>' : '');
      sslRefreshStatus();
    } else {
      resultEl.innerHTML = '<div style="color:#f66">Error: ' + escHtml(d.error || 'unknown') + '</div>' + (d.log ? '<pre style="font-size:11px;max-height:200px;overflow:auto;margin-top:8px;">' + escHtml(d.log) + '</pre>' : '');
    }
  } catch(e) {
    resultEl.innerHTML = '<div style="color:#f66">Request failed: ' + escHtml(String(e)) + '</div>';
  }
}

async function sslUploadFiles() {
  const resultEl = document.getElementById('ssl-upload-result');
  const certInput = document.getElementById('ssl-upload-cert');
  const keyInput = document.getElementById('ssl-upload-key');
  if (!certInput.files[0] || !keyInput.files[0]) {
    resultEl.innerHTML = '<div style="color:#f66">Select both cert and key files</div>';
    return;
  }
  try {
    const certText = await certInput.files[0].text();
    const keyText = await keyInput.files[0].text();
    resultEl.textContent = 'Uploading...';
    const r = await fetch('/api/ssl/upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cert: certText, key: keyText}),
    });
    const d = await r.json();
    if (d.ok || r.ok) {
      resultEl.innerHTML = '<div style="color:#6f6">' + escHtml(d.message || 'Uploaded') + '</div>';
      sslRefreshStatus();
    } else {
      resultEl.innerHTML = '<div style="color:#f66">Error: ' + escHtml(d.error || 'upload failed') + '</div>';
    }
  } catch(e) {
    resultEl.innerHTML = '<div style="color:#f66">Upload failed: ' + escHtml(String(e)) + '</div>';
  }
}

function cfgRenderSsl(ssl) {
  ssl = ssl || {};
  let html = '<div style="max-width:700px;">';
  html += '<div id="ssl-status-display" class="cfg-ssl-status-box" style="background:#1a1a1a;padding:12px;border-radius:4px;margin-bottom:16px;">Loading status...</div>';
  html += '<div style="margin-bottom:12px;"><button onclick="sslRefreshStatus()" class="cfg-btn">Refresh Status</button></div>';

  html += '<h3 style="margin-top:20px;">Lets Encrypt (Cloudflare DNS)</h3>';
  html += '<div class="cfg-row"><label>Enable HTTPS:</label><input type="checkbox" id="ssl-enabled"' + (ssl.enabled ? ' checked' : '') + '></div>';
  html += '<div class="cfg-row"><label>Use Lets Encrypt:</label><input type="checkbox" id="ssl-use-le"' + (ssl.use_letsencrypt ? ' checked' : '') + '></div>';
  html += '<div class="cfg-row"><label>Domain:</label><input type="text" id="ssl-domain" value="' + escAttr(ssl.domain || '') + '" placeholder="glados.example.com"></div>';
  html += '<div class="cfg-row"><label>ACME Email:</label><input type="text" id="ssl-acme-email" value="' + escAttr(ssl.acme_email || '') + '" placeholder="admin@example.com"></div>';
  html += '<div class="cfg-row"><label>DNS Provider:</label><select id="ssl-acme-provider"><option value="cloudflare"' + ((ssl.acme_provider === 'cloudflare' || !ssl.acme_provider) ? ' selected' : '') + '>Cloudflare</option></select></div>';
  html += '<div class="cfg-row"><label>API Token:</label><input type="password" id="ssl-acme-token" value="' + escAttr(ssl.acme_api_token || '') + '" placeholder="Cloudflare API token"></div>';
  html += '<div style="margin:12px 0;"><button onclick="sslRequestLetsEncrypt()" class="cfg-btn">Request / Renew Certificate</button></div>';
  html += '<div id="ssl-request-result" style="margin-bottom:20px;"></div>';

  html += '<h3 style="margin-top:20px;">Manual Upload</h3>';
  html += '<div class="cfg-row"><label>Certificate PEM:</label><input type="file" id="ssl-upload-cert" accept=".pem,.crt,.cert"></div>';
  html += '<div class="cfg-row"><label>Private Key PEM:</label><input type="file" id="ssl-upload-key" accept=".pem,.key"></div>';
  html += '<div style="margin:12px 0;"><button onclick="sslUploadFiles()" class="cfg-btn">Upload Certificate</button></div>';
  html += '<div id="ssl-upload-result" style="margin-bottom:20px;"></div>';

  html += '<h3 style="margin-top:20px;">File Paths (advanced)</h3>';
  html += '<div class="cfg-row"><label>Certificate Path:</label><input type="text" id="ssl-cert-path" value="' + escAttr(ssl.cert_path || '/app/certs/cert.pem') + '"></div>';
  html += '<div class="cfg-row"><label>Key Path:</label><input type="text" id="ssl-key-path" value="' + escAttr(ssl.key_path || '/app/certs/key.pem') + '"></div>';

  html += '<div style="margin-top:24px;padding:12px;background:#2a2010;border-left:3px solid #fa0;">';
  html += 'Container restart is required after certificate changes take effect.';
  html += '</div>';

  html += '<div class="cfg-save-row" style="margin-top:20px;">';
  html += '<button class="cfg-save-btn" onclick="cfgSaveSsl()">Save SSL Settings</button>';
  html += '<span id="cfg-save-result" class="cfg-result"></span>';
  html += '</div>';

  html += '</div>';
  setTimeout(function(){ try { sslRefreshStatus(); } catch(e){} }, 100);
  return html;
}

async function cfgSaveSection(section, resultElId) {
  // resultElId is optional — defaults to the page-wide #cfg-save-result.
  // Merged pages (Audio & Speakers) pass a per-subsection ID so each
  // save button updates its own status span.
  const data = cfgCollectForm(section);
  const resultEl = document.getElementById(resultElId || 'cfg-save-result');
  if (resultEl) {
    resultEl.textContent = 'Saving...';
    resultEl.className = 'cfg-result';
  }
  try {
    const r = await fetch('/api/config/' + section, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const resp = await r.json();
    if (r.ok) {
      _cfgData[section] = data;
      if (resultEl) resultEl.textContent = '';
      if (resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
    } else if (resultEl) {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    if (resultEl) {
      resultEl.textContent = 'Error: ' + e.message;
      resultEl.className = 'cfg-result err';
    }
  }
}

function cfgRenderRaw() {
  const meta = SECTION_META.raw || {};
  let html = '<div class="cfg-section-header">'
    + '<div class="cfg-section-title">' + escHtml(meta.title || 'Raw YAML') + '</div>'
    + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
    + '</div>';

  const files = ['global', 'services', 'speakers', 'audio', 'personality'];
  html += '<div class="cfg-file-tabs">';
  files.forEach(f => {
    const cls = f === _cfgCurrentRawFile ? 'cfg-file-tab active' : 'cfg-file-tab';
    html += '<button class="' + cls + '" onclick="cfgSwitchRawFile(\'' + f + '\')">' + f + '.yaml</button>';
  });
  html += '</div>';
  const content = _cfgRaw[_cfgCurrentRawFile] || '';
  html += '<textarea class="cfg-textarea" id="cfg-raw-editor">' + content.replace(/</g,'&lt;') + '</textarea>';
  html += '<div class="cfg-save-row">'
    + '<button class="cfg-save-btn" onclick="cfgSaveRaw()">Save ' + _cfgCurrentRawFile + '.yaml</button>'
    + '<span id="cfg-save-result" class="cfg-result"></span>'
    + '</div>';
  document.getElementById('cfg-form-area').innerHTML = html;
}

function cfgSwitchRawFile(name) {
  _cfgCurrentRawFile = name;
  cfgRenderRaw();
}

async function cfgSaveRaw() {
  const content = document.getElementById('cfg-raw-editor').value;
  const resultEl = document.getElementById('cfg-save-result');
  resultEl.textContent = 'Saving...';
  resultEl.className = 'cfg-result';
  try {
    const r = await fetch('/api/config/raw', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: _cfgCurrentRawFile, content})
    });
    if (r.ok) {
      let resp = {};
      try { resp = await r.json(); } catch (_) { /* old server */ }
      resultEl.textContent = '';
      if (resp && resp.applied === false) {
        showToast('Saved, but live apply failed. Check container logs.', 'warn');
      } else {
        showToast('Changes saved.', 'success');
      }
      _cfgRaw[_cfgCurrentRawFile] = content;
      await cfgLoadAll();
    } else {
      let msg = 'Error (' + r.status + ')';
      try {
        const body = await r.json();
        if (body && body.error) msg = body.error;
      } catch (_) {}
      resultEl.textContent = msg;
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    resultEl.textContent = 'Error: ' + e.message;
    resultEl.className = 'cfg-result err';
  }
}

async function cfgReload() {
  const status = document.getElementById('cfg-status');
  status.textContent = 'Reloading...';
  try {
    const r = await fetch('/api/config/reload', {method: 'POST'});
    if (r.ok) {
      await cfgLoadAll();
      cfgRenderSection(_cfgCurrentSection === 'raw' ? 'global' : _cfgCurrentSection);
      status.textContent = '';
      showToast('Reloaded from disk.', 'success');
    } else {
      status.textContent = 'Reload failed';
    }
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  }
}

// Load config data on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
  cfgLoadAll();
});

/* ===============================================================
   Memory page (Phase 5) — long-term facts, recent activity,
   passive default-status toggle, manual retention sweep.
   =============================================================== */

let _memCachedConfig = null;

function memoryLoadAll() {
  memLoadConfig();
  memLoadFacts();
  memLoadRecent();
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   CONFIGURATION > LOGS (Phase 6 follow-up)
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _logsRawLines = [];          // latest raw lines from /api/logs/tail
let _logsSources = [];
let _logsAutoTimer = null;
const LOGS_AUTO_INTERVAL_MS = 10000;

// Tab entry: on first visit populate the source list; refresh every time.
async function logsOnTabActivate() {
  if (_logsSources.length === 0) {
    try {
      const r = await fetch('/api/logs/sources');
      if (r.status === 401) { logsSetStatus('auth required'); return; }
      const j = await r.json();
      _logsSources = j.sources || [];
      const sel = document.getElementById('logsSource');
      sel.innerHTML = '';
      for (const s of _logsSources) {
        const opt = document.createElement('option');
        opt.value = s.key; opt.textContent = s.label; sel.appendChild(opt);
      }
    } catch (e) {
      logsSetStatus('failed to list sources: ' + e.message);
      return;
    }
  }
  logsUpdateSourceDesc();
  logsRefresh();
}

function logsOnSourceChange() {
  logsUpdateSourceDesc();
  logsRefresh();
}

function logsUpdateSourceDesc() {
  const key = document.getElementById('logsSource').value;
  const s = _logsSources.find(x => x.key === key);
  document.getElementById('logsSourceDesc').textContent = s ? s.desc : '';
}

function logsSetStatus(msg) {
  document.getElementById('logsStatus').textContent = msg || '';
}

async function logsRefresh() {
  const source = document.getElementById('logsSource').value;
  const lines  = document.getElementById('logsLines').value;
  if (!source) return;
  logsSetStatus('loading...');
  try {
    const r = await fetch('/api/logs/tail?source=' + encodeURIComponent(source) + '&lines=' + lines);
    if (r.status === 401) { logsSetStatus('auth required'); return; }
    const j = await r.json();
    if (!r.ok) {
      _logsRawLines = [];
      document.getElementById('logsBody').textContent = 'error: ' + (j.error || r.status);
      logsSetStatus('error');
      return;
    }
    _logsRawLines = j.lines || [];
    logsRerender();
    const when = new Date().toLocaleTimeString();
    logsSetStatus(`${_logsRawLines.length} line${_logsRawLines.length===1?'':'s'} · refreshed ${when}`);
    // Pin to bottom after each refresh so new content is visible.
    const vp = document.querySelector('.logs-viewport');
    if (vp) vp.scrollTop = vp.scrollHeight;
  } catch (e) {
    logsSetStatus('fetch failed: ' + e.message);
  }
}

// Classify a line's severity. Works for loguru default format, Python's
// stdlib logging, and audit JSONL (checks for "level":"ERROR" patterns).
function _logsSeverity(line) {
  const s = line || '';
  if (/\|\s*ERROR\s*\||\bERROR\b|\"level\":\s*\"ERROR\"|Traceback|Exception:|Error:/i.test(s)) return 'error';
  if (/\|\s*WARN(ING)?\s*\||\bWARN(ING)?\b|\"level\":\s*\"WARNING\"/i.test(s)) return 'warn';
  if (/\|\s*SUCCESS\s*\||\"level\":\s*\"SUCCESS\"/i.test(s)) return 'success';
  if (/\|\s*INFO\s*\||\"level\":\s*\"INFO\"/i.test(s)) return 'info';
  if (/\|\s*DEBUG\s*\||\"level\":\s*\"DEBUG\"/i.test(s)) return 'dim';
  return null;
}

function logsRerender() {
  const filter = document.getElementById('logsFilter').value;
  const body = document.getElementById('logsBody');
  if (_logsRawLines.length === 0) {
    body.textContent = '(no log content yet)';
    return;
  }
  const frag = document.createDocumentFragment();
  let shown = 0;
  for (const raw of _logsRawLines) {
    const sev = _logsSeverity(raw);
    if (filter === 'error' && sev !== 'error') continue;
    if (filter === 'warn' && sev !== 'error' && sev !== 'warn') continue;
    const span = document.createElement('span');
    if (sev) span.className = 'log-' + sev;
    span.textContent = raw + '\n';
    frag.appendChild(span);
    shown++;
  }
  body.innerHTML = '';
  if (shown === 0) {
    body.textContent = '(filter matches no lines)';
  } else {
    body.appendChild(frag);
  }
}

function logsToggleAuto() {
  const on = document.getElementById('logsAuto').checked;
  if (on && !_logsAutoTimer) {
    _logsAutoTimer = setInterval(logsRefresh, LOGS_AUTO_INTERVAL_MS);
    logsRefresh();
  } else if (!on && _logsAutoTimer) {
    clearInterval(_logsAutoTimer);
    _logsAutoTimer = null;
  }
}

async function memLoadConfig() {
  try {
    const r = await fetch('/api/config/memory');
    if (!r.ok) return;
    const cfg = await r.json();
    _memCachedConfig = cfg;
    const defaultStatus = cfg.passive_default_status || 'approved';
    document.querySelectorAll('input[name="memDefaultStatus"]').forEach(rb => {
      rb.checked = (rb.value === defaultStatus);
    });
    const pendingCard = document.getElementById('memPendingCard');
    if (pendingCard) {
      pendingCard.style.display = (defaultStatus === 'pending') ? '' : 'none';
    }
    if (defaultStatus === 'pending') memLoadPending();
  } catch(e) { /* ignore */ }
}

async function memSaveDefaultStatus(val) {
  if (!_memCachedConfig) {
    // Fetch latest before PUT to preserve other fields.
    try {
      const r = await fetch('/api/config/memory');
      if (r.ok) _memCachedConfig = await r.json();
    } catch(e) {}
  }
  const body = Object.assign({}, _memCachedConfig || {}, {passive_default_status: val});
  try {
    const r = await fetch('/api/config/memory', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      showToast('Save failed: ' + t, 'error');
      return;
    }
    showToast('Default status saved: ' + val, 'success');
    memLoadConfig();
  } catch(e) {
    showToast('Save failed: ' + e.message, 'error');
  }
}

function memShowAddForm() { document.getElementById('memAddForm').style.display = ''; }
function memHideAddForm() {
  document.getElementById('memAddForm').style.display = 'none';
  document.getElementById('memAddText').value = '';
}

async function memAddFact() {
  const text = document.getElementById('memAddText').value.trim();
  if (!text) { showToast('Text required', 'error'); return; }
  const importance = parseFloat(document.getElementById('memAddImportance').value);
  try {
    const r = await fetch('/api/memory/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: text, importance: importance}),
    });
    const data = await r.json();
    if (!data.added) {
      showToast('Add failed: ' + (data.error || 'unknown'), 'error');
      return;
    }
    memHideAddForm();
    memLoadFacts();
    memLoadRecent();
    showToast('Fact added', 'success');
  } catch(e) {
    showToast('Add failed: ' + e.message, 'error');
  }
}

let _memSearchTimer;
function memSearchDebounced() {
  clearTimeout(_memSearchTimer);
  _memSearchTimer = setTimeout(memLoadFacts, 300);
}

async function memLoadFacts() {
  const q = document.getElementById('memSearchInput');
  const qv = q ? q.value.trim() : '';
  const url = qv
    ? '/api/memory/list?q=' + encodeURIComponent(qv) + '&limit=50'
    : '/api/memory/list?limit=50';
  try {
    const r = await fetch(url);
    const data = await r.json();
    _memRenderFacts(data.rows || []);
  } catch(e) {
    document.getElementById('memFactsList').textContent = 'Load failed: ' + e.message;
  }
}

function _memRenderFacts(rows) {
  const el = document.getElementById('memFactsList');
  if (rows.length === 0) {
    el.innerHTML = '<div style="color:var(--text-dim);padding:8px;">No facts yet. Click + Add to record one.</div>';
    return;
  }
  let html = '';
  rows.forEach(r => { html += _memFactCard(r); });
  el.innerHTML = html;
}

function _memFactCard(r) {
  const m = r.metadata || {};
  const sourceRaw = m.source || '';
  const source = sourceRaw.replace(/^user_/, '') || 'unknown';
  const importance = (m.importance != null) ? Number(m.importance).toFixed(2) : '?';
  const mentions = m.mention_count || 1;
  const age = _memFmtAge(m.written_at);
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  return '<div class="mem-fact" data-id="' + id + '">'
    + '<div class="mem-fact-text">' + doc + '</div>'
    + '<div class="mem-fact-meta">source=' + escHtml(source)
      + '  importance=' + importance
      + '  mentions=' + mentions
      + '  age=' + age + '</div>'
    + '<div class="mem-fact-actions">'
    +   '<button class="btn-small" onclick="memEdit(\'' + id + '\')">Edit</button>'
    +   ' <button class="btn-small" style="background:#c0392b;" onclick="memDelete(\'' + id + '\')">Delete</button>'
    + '</div></div>';
}

function _memFmtAge(ts) {
  if (!ts) return '?';
  const d = (Date.now() / 1000) - Number(ts);
  if (d < 60)    return Math.max(0, Math.floor(d)) + 's';
  if (d < 3600)  return Math.floor(d / 60) + 'm';
  if (d < 86400) return Math.floor(d / 3600) + 'h';
  return Math.floor(d / 86400) + 'd';
}

async function memEdit(id) {
  const row = document.querySelector('.mem-fact[data-id="' + id + '"], .mem-recent[data-id="' + id + '"], .mem-pending[data-id="' + id + '"]');
  const currentText = row ? (row.querySelector('.mem-fact-text, strong') || {}).textContent || '' : '';
  const newText = prompt('Edit fact:', currentText);
  if (newText == null || newText.trim() === '' || newText.trim() === currentText.trim()) return;
  try {
    await fetch('/api/memory/' + encodeURIComponent(id) + '/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: newText.trim()}),
    });
    memLoadFacts(); memLoadRecent(); memLoadPending();
    showToast('Edited', 'success');
  } catch(e) {
    showToast('Edit failed: ' + e.message, 'error');
  }
}

async function memDelete(id) {
  if (!confirm('Delete this fact? This cannot be undone.')) return;
  try {
    await fetch('/api/memory/' + encodeURIComponent(id), {method: 'DELETE'});
    memLoadFacts(); memLoadRecent(); memLoadPending();
    showToast('Deleted', 'success');
  } catch(e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function memPromote(id) {
  try {
    await fetch('/api/memory/' + encodeURIComponent(id) + '/promote', {method: 'POST'});
    memLoadPending(); memLoadFacts(); memLoadRecent();
    showToast('Approved', 'success');
  } catch(e) {
    showToast('Approve failed: ' + e.message, 'error');
  }
}

async function memReject(id) {
  if (!confirm('Reject this fact? It will not enter RAG.')) return;
  try {
    await fetch('/api/memory/' + encodeURIComponent(id) + '/demote', {method: 'POST'});
    memLoadPending();
    showToast('Rejected', 'success');
  } catch(e) {
    showToast('Reject failed: ' + e.message, 'error');
  }
}

// Recent activity: reuses /api/memory/list; sorts by max(written_at,
// last_mentioned_at) and shows the top 10. Reinforcement-bump rows
// can offer "Update wording from latest mention" when last_mention_text
// differs from the canonical document.
async function memLoadRecent() {
  try {
    const r = await fetch('/api/memory/list?limit=200');
    const data = await r.json();
    const rows = (data.rows || []).slice();
    rows.sort((a, b) => {
      const am = a.metadata || {}, bm = b.metadata || {};
      const at = Math.max(Number(am.last_mentioned_at || 0), Number(am.written_at || 0));
      const bt = Math.max(Number(bm.last_mentioned_at || 0), Number(bm.written_at || 0));
      return bt - at;
    });
    _memRenderRecent(rows.slice(0, 10));
  } catch(e) {
    document.getElementById('memRecentList').textContent = 'Load failed: ' + e.message;
  }
}

function _memRenderRecent(rows) {
  const el = document.getElementById('memRecentList');
  if (rows.length === 0) {
    el.innerHTML = '<div style="color:var(--text-dim);padding:8px;">No recent activity.</div>';
    return;
  }
  let html = '';
  rows.forEach(r => { html += _memRecentItem(r); });
  el.innerHTML = html;
}

function _memRecentItem(r) {
  const m = r.metadata || {};
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  const mentions = m.mention_count || 1;
  const isReinforcement = mentions > 1;
  const age = _memFmtAge(Math.max(Number(m.last_mentioned_at || 0), Number(m.written_at || 0)));
  const lastText = m.last_mention_text || '';
  const canUpdate = isReinforcement && lastText && lastText !== (r.document || '');
  const importance = Number(m.importance || 0).toFixed(2);
  const origImportance = Number(m.original_importance || 0).toFixed(2);
  let label = isReinforcement
    ? '<span class="mem-bump">reinforced</span> importance ' + origImportance + ' &rarr; ' + importance + ', mentions=' + mentions
    : 'new fact';
  let html = '<div class="mem-recent" data-id="' + id + '">'
    + '<div><strong>' + doc + '</strong></div>'
    + '<div class="mem-fact-meta">' + label + '  &bull;  ' + age + ' ago</div>';
  if (canUpdate) {
    html += '<div class="mem-fact-meta">Latest mention: &ldquo;' + escHtml(lastText) + '&rdquo;</div>';
    html += '<div class="mem-fact-actions">'
      + '<button class="btn-small" onclick="memUpdateWording(\'' + id + '\')">Update wording from latest mention</button>'
      + ' <button class="btn-small" onclick="memEdit(\'' + id + '\')">Edit</button>'
      + ' <button class="btn-small" style="background:#c0392b;" onclick="memDelete(\'' + id + '\')">Delete</button>'
      + '</div>';
  } else {
    html += '<div class="mem-fact-actions">'
      + '<button class="btn-small" onclick="memEdit(\'' + id + '\')">Edit</button>'
      + ' <button class="btn-small" style="background:#c0392b;" onclick="memDelete(\'' + id + '\')">Delete</button>'
      + '</div>';
  }
  html += '</div>';
  return html;
}

async function memUpdateWording(id) {
  // Refetch to get the latest last_mention_text for the target id,
  // then POST the edit. Keeps the button idempotent and avoids stale
  // cached text.
  try {
    const r = await fetch('/api/memory/list?limit=200');
    const data = await r.json();
    const match = (data.rows || []).find(x => x.id === id);
    if (!match) return;
    const lt = (match.metadata || {}).last_mention_text;
    if (!lt) { showToast('No alternate wording available', 'error'); return; }
    await fetch('/api/memory/' + encodeURIComponent(id) + '/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document: lt}),
    });
    memLoadFacts(); memLoadRecent();
    showToast('Wording updated', 'success');
  } catch(e) {
    showToast('Update failed: ' + e.message, 'error');
  }
}

async function memLoadPending() {
  try {
    const r = await fetch('/api/memory/pending?limit=50');
    const data = await r.json();
    const el = document.getElementById('memPendingList');
    const rows = data.rows || [];
    if (rows.length === 0) {
      el.innerHTML = '<div style="color:var(--text-dim);padding:8px;">Nothing pending.</div>';
      return;
    }
    let html = '';
    rows.forEach(r => { html += _memPendingCard(r); });
    el.innerHTML = html;
  } catch(e) {
    document.getElementById('memPendingList').textContent = 'Load failed: ' + e.message;
  }
}

function _memPendingCard(r) {
  const m = r.metadata || {};
  const doc = escHtml(r.document || '');
  const id = escAttr(r.id || '');
  const importance = Number(m.importance || 0).toFixed(2);
  const age = _memFmtAge(m.written_at);
  return '<div class="mem-pending" data-id="' + id + '">'
    + '<div><strong>' + doc + '</strong></div>'
    + '<div class="mem-fact-meta">source=passive  importance=' + importance + '  age=' + age + '</div>'
    + '<div class="mem-fact-actions">'
    +   '<button class="btn-small" onclick="memPromote(\'' + id + '\')">Approve</button>'
    +   ' <button class="btn-small" onclick="memEdit(\'' + id + '\')">Edit</button>'
    +   ' <button class="btn-small" style="background:#c0392b;" onclick="memReject(\'' + id + '\')">Reject</button>'
    + '</div></div>';
}

async function memSweepRetention() {
  const s = document.getElementById('memRetentionStatus');
  s.textContent = 'Sweeping...';
  try {
    const r = await fetch('/api/retention/sweep', {method: 'POST'});
    const data = await r.json();
    if (data.ok) {
      const parts = Object.entries(data.counts || {}).map(([k, v]) => k + '=' + v).join(', ');
      s.textContent = 'Done: ' + (parts || 'no changes');
      showToast('Retention swept', 'success');
    } else {
      s.textContent = 'Error: ' + (data.error || 'unknown');
      showToast('Sweep failed', 'error');
    }
  } catch(e) {
    s.textContent = 'Error: ' + e.message;
    showToast('Sweep failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Shared utilities
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

// ── Phase 5 navigation ──────────────────────────────────────────
// Dotted keys drive the whole UI now: "chat", "tts", "config.system",
// "config.global", ..., "config.memory", "config.raw". The mapping
// from key → DOM panel lives in _panelIdFor(); Configuration children
// all render into the single #tab-config host (except System and
// Memory which have their own HTML).

function _panelIdFor(key) {
  if (key === 'config.system') return 'tab-config-system';
  if (key === 'config.memory') return 'tab-config-memory';
  if (key === 'config.logs')   return 'tab-config-logs';
  if (key && key.indexOf('config.') === 0) return 'tab-config';
  return 'tab-' + key;
}

// Legacy keys translated on read so operators don't see a blank page
// after upgrade. Two generations of legacy keys are supported now:
//   • pre-Phase-5: bare 'tts'/'chat'/'control'/'config'
//   • pre-Phase-6: 'config.global' / '.services' / '.speakers' / '.audio'
// If cfgLoadAll hasn't populated _cfgData yet by the time these old
// pages were stored, the new virtual equivalents still render once
// the data arrives.
function _migrateLegacyKey(k) {
  if (k === 'control') return 'config.system';
  if (k === 'config')  return 'config.integrations';
  if (k === 'config.global')    return 'config.integrations';
  if (k === 'config.services')  return 'config.llm-services';
  if (k === 'config.speakers')  return 'config.audio-speakers';
  if (k === 'config.audio')     return 'config.audio-speakers';
  return k;
}

function navToggleConfig() {
  // Clicking the Configuration parent toggles the submenu when it's
  // expanded but we're NOT on a config.* page. When on a child page,
  // the submenu is already pinned open (auto-expand) — toggling it
  // would hide the current page's sibling links, so do nothing.
  const parent = document.querySelector('.nav-parent[data-nav-key="config"]');
  if (!parent) return;
  const onChild = (_activeNavKey || '').indexOf('config.') === 0;
  if (onChild) return;
  parent.classList.toggle('open');
}

let _activeNavKey = 'chat';

function navigateTo(key) {
  key = _migrateLegacyKey(key);
  // Leaving Logs? Tear down the 10 s polling timer so we don't keep
  // hitting /api/logs/tail when the operator's on another page.
  if (_activeNavKey === 'config.logs' && key !== 'config.logs') {
    const el = document.getElementById('logsAuto');
    if (el && el.checked) { el.checked = false; logsToggleAuto(); }
  }
  _activeNavKey = key;

  const panelId = _panelIdFor(key);
  const panel = document.getElementById(panelId);
  if (!panel) return;

  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  panel.classList.add('active');

  // Highlight matching nav items in both sidebar and topbar.
  document.querySelectorAll('.nav-item[data-nav-key="' + key + '"]').forEach(n => {
    n.classList.add('active');
  });

  // Auto-expand Configuration parent iff the active key is a child.
  const parent = document.querySelector('.nav-parent[data-nav-key="config"]');
  if (parent) {
    if (key.indexOf('config.') === 0) parent.classList.add('open');
    else parent.classList.remove('open');
  }

  try { localStorage.setItem('glados_active_tab', key); } catch(e) {}

  // Tab activation hooks
  if (key === 'config.system') {
    loadModes(); loadSpeakers(); loadHealth(); loadEyeDemo();
    loadWeather(); loadGPU(); loadRobots();
    loadVerbositySliders(); loadStartupSpeakers();
    startGPUAutoRefresh(); startWeatherAutoRefresh(); startRobotAutoRefresh();
    if (typeof loadSystemConfigCards === 'function') loadSystemConfigCards();
  } else if (key === 'config.memory') {
    // Memory page UI arrives in Phase 5 Commit 3; placeholder for now.
    if (typeof memoryLoadAll === 'function') memoryLoadAll();
  } else if (key === 'config.logs') {
    if (typeof logsOnTabActivate === 'function') logsOnTabActivate();
  } else if (key.indexOf('config.') === 0) {
    const section = key.substring('config.'.length);
    _cfgCurrentSection = section;
    cfgLoadAll().then(() => {
      if (section === 'raw') cfgLoadRaw().then(() => cfgRenderRaw());
      else cfgRenderSection(section);
    });
    loadAudioStats();
  } else if (key === 'training') {
    initTrainingTab();
  } else if (key === 'chat') {
    const ci = document.getElementById('chatInput');
    if (ci) ci.focus();
  } else if (key === 'tts') {
    const ti = document.getElementById('textInput');
    if (ti) ti.focus();
  }
}

// Backward-compat shim for any older inline onclick that still calls
// switchTab(). Routes through the new key mapping.
function switchTab(name) { navigateTo(_migrateLegacyKey(name)); }

// Check auth on load, THEN restore saved tab (default: Chat).
checkAuth().then(() => {
  let restored = false;
  try {
    const raw = localStorage.getItem('glados_active_tab');
    if (raw) {
      const key = _migrateLegacyKey(raw);
      if (document.getElementById(_panelIdFor(key))) {
        navigateTo(key);
        restored = true;
      }
    }
  } catch(e) {}
  if (!restored) navigateTo('chat');
  // Phase 5: sidebar engine status dot.
  startEngineStatusPoll();
});

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function escAttr(s) {
  return String(s).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}
function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 1: TTS Generator
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const textInput  = document.getElementById('textInput');
const charCount  = document.getElementById('charCount');
const voiceSel   = document.getElementById('voiceSelect');
const formatSel  = document.getElementById('formatSelect');
const attitudeSel= document.getElementById('attitudeSelect');
const genBtn     = document.getElementById('generateBtn');
const ttsStatus  = document.getElementById('ttsStatus');
const playerCard = document.getElementById('playerCard');
const playerLabel= document.getElementById('playerLabel');
const audioPlayer= document.getElementById('audioPlayer');
const fileListEl = document.getElementById('fileList');

let _attitudes = [];

async function loadVoices() {
  try {
    const resp = await fetch('/api/voices');
    const data = await resp.json();
    const voices = data.voices || ['glados'];
    voiceSel.innerHTML = '';
    for (const v of voices) {
      const opt = document.createElement('option');
      opt.value = v;
      const label = v.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
      opt.textContent = 'Voice: ' + label;
      voiceSel.appendChild(opt);
    }
  } catch (e) { console.warn('Failed to load voices:', e); }
}
loadVoices();

voiceSel.addEventListener('change', () => {
  const isGlados = voiceSel.value === 'glados';
  attitudeSel.disabled = !isGlados;
  if (!isGlados) attitudeSel.value = 'default';
});

async function loadAttitudes() {
  try {
    const resp = await fetch('/api/attitudes');
    const data = await resp.json();
    _attitudes = data.attitudes || [];
    for (const a of _attitudes) {
      const opt = document.createElement('option');
      opt.value = a.tag;
      opt.textContent = 'Attitude: ' + (a.label || a.tag);
      attitudeSel.appendChild(opt);
    }
  } catch (e) { console.warn('Failed to load attitudes:', e); }
}
loadAttitudes();

function getSelectedTtsParams() {
  const val = attitudeSel.value;
  if (val === 'default') return {};
  if (val === 'random') {
    if (_attitudes.length === 0) return {};
    const pick = _attitudes[Math.floor(Math.random() * _attitudes.length)];
    return pick.tts || {};
  }
  const found = _attitudes.find(a => a.tag === val);
  return found ? (found.tts || {}) : {};
}

textInput.addEventListener('input', () => { charCount.textContent = textInput.value.length; });
textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ttsGenerate(); }
});

async function ttsGenerate() {
  const text = textInput.value.trim();
  if (!text) return;
  genBtn.disabled = true;
  ttsStatus.innerHTML = '<span class="spinner"></span> Generating...';
  try {
    const ttsParams = getSelectedTtsParams();
    const payload = { text, voice: voiceSel.value, format: formatSel.value, ...ttsParams };
    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Generation failed');
    audioPlayer.src = data.url;
    playerLabel.textContent = data.filename;
    playerCard.classList.add('visible');
    audioPlayer.play().catch(() => {});
    const attLabel = attitudeSel.options[attitudeSel.selectedIndex].textContent;
    ttsStatus.innerHTML = '<span style="color:var(--green)">Done! (' + escHtml(attLabel) + ')</span>';
    setTimeout(() => { ttsStatus.innerHTML = ''; }, 3000);
  } catch (e) {
    ttsStatus.innerHTML = '<span style="color:var(--red)">' + escHtml(e.message) + '</span>';
  } finally {
    genBtn.disabled = false;
    refreshFiles();
  }
}

async function refreshFiles() {
  try {
    const resp = await fetch('/api/files');
    const data = await resp.json();
    if (!data.files || data.files.length === 0) {
      fileListEl.innerHTML = '<div class="empty-msg">No files yet.</div>';
      return;
    }
    let html = '<table><tr><th>Name</th><th>Size</th><th>Date</th><th>Actions</th></tr>';
    for (const f of data.files) {
      html += '<tr>'
        + '<td class="file-name">' + escHtml(f.name) + '</td>'
        + '<td class="file-size">' + fmtSize(f.size) + '</td>'
        + '<td class="file-date">' + fmtDate(f.date) + '</td>'
        + '<td class="file-actions">'
          + '<button class="btn-small" onclick="playFile(\'' + escAttr(f.url) + '\',\'' + escAttr(f.name) + '\')">Play</button>'
          + '<a class="dl-link" href="' + escAttr(f.url) + '" download="' + escAttr(f.name) + '">Download</a>'
          + '<button class="btn btn-danger" onclick="deleteFile(\'' + escAttr(f.name) + '\')">Delete</button>'
        + '</td></tr>';
    }
    html += '</table>';
    fileListEl.innerHTML = html;
  } catch (e) { console.error('Failed to refresh files:', e); }
}

function playFile(url, name) {
  audioPlayer.src = url;
  playerLabel.textContent = name;
  playerCard.classList.add('visible');
  audioPlayer.play().catch(() => {});
}

async function deleteFile(name) {
  try { await fetch('/api/files/' + encodeURIComponent(name), { method: 'DELETE' }); } catch (e) {}
  refreshFiles();
}

refreshFiles();

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 2: Chat
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let chatHistory = [];
let chatWaiting = false;
let mediaRecorder = null;
let micStream = null;
let chatStreaming = false;
// Chat audio is now handled entirely by the visible <audio controls>
// element in the message DOM. No separate background player.

function renderChat() {
  // Incremental renderer. The previous version rewrote innerHTML on
  // every content chunk during streaming, which destroyed the <audio>
  // element and its playback state each time — the visible controls
  // kept appearing to reset to 0:00 while TTS audio played from a
  // detached (doomed) element. Here we reconcile the DOM against
  // chatHistory: existing <audio> elements are preserved across
  // re-renders so the operator's play/pause/volume/seek interactions
  // stick to a single persistent element.
  const el = document.getElementById('chatMessages');
  if (chatHistory.length === 0) {
    el.innerHTML = '<div class="empty-msg">Send a message to start talking with GLaDOS.</div>';
    return;
  }
  // Clear empty-state div if it exists
  const empty = el.querySelector('.empty-msg');
  if (empty) empty.remove();

  // Reconcile message-div count
  const want = chatHistory.length + (chatWaiting ? 1 : 0);
  while (el.children.length > want) {
    el.removeChild(el.lastChild);
  }
  while (el.children.length < want) {
    const div = document.createElement('div');
    div.className = 'chat-msg';
    el.appendChild(div);
  }

  for (let i = 0; i < chatHistory.length; i++) {
    const msg = chatHistory[i];
    const isLast = (i === chatHistory.length - 1);
    const msgEl = el.children[i];
    msgEl.className = 'chat-msg ' + msg.role;

    if (msg.role === 'user') {
      if (msgEl.textContent !== msg.content) {
        msgEl.textContent = msg.content;
      }
      continue;
    }

    // --- assistant message ---
    // Clear any leftover "Thinking..." placeholder span that was
    // rendered earlier when chatWaiting was true. Without this, the
    // spinner keeps spinning next to the real content because my
    // incremental reconciler reuses the same <div> slot.
    const stale = msgEl.querySelector('.thinking');
    if (stale) stale.remove();

    let labelEl = msgEl.querySelector('.msg-label');
    if (!labelEl) {
      labelEl = document.createElement('div');
      labelEl.className = 'msg-label';
      labelEl.textContent = 'GLaDOS';
      msgEl.appendChild(labelEl);
    }

    let textEl = msgEl.querySelector('.content-text');
    if (!textEl) {
      textEl = document.createElement('span');
      textEl.className = 'content-text';
      // Insert after label
      msgEl.appendChild(textEl);
    }
    if (textEl.textContent !== (msg.content || '')) {
      textEl.textContent = msg.content || '';
    }

    let cursor = msgEl.querySelector('.stream-cursor');
    if (isLast && chatStreaming) {
      if (!cursor) {
        cursor = document.createElement('span');
        cursor.className = 'stream-cursor';
        cursor.textContent = '|';
        msgEl.appendChild(cursor);
      }
    } else if (cursor) {
      cursor.remove();
    }

    // Audio: create ONCE, swap src in place if the URL changes (e.g.
    // streaming -> static replay). Never destroy the element — that's
    // what caused the regression.
    let audioEl = msgEl.querySelector('audio');
    if (msg.audio_url) {
      if (!audioEl) {
        audioEl = document.createElement('audio');
        audioEl.controls = true;
        audioEl.preload = 'auto';
        audioEl.src = msg.audio_url;
        msgEl.appendChild(audioEl);
        audioEl.play().catch(function() {});
      } else {
        const currentSrc = audioEl.getAttribute('src') || '';
        if (currentSrc !== msg.audio_url) {
          // URL swap (streaming URL -> static replay). Preserve
          // playback position and resume-if-was-playing.
          const pos = audioEl.currentTime || 0;
          const wasPlaying = !audioEl.paused && !audioEl.ended;
          audioEl.src = msg.audio_url;
          audioEl.addEventListener('loadedmetadata', function _once() {
            audioEl.removeEventListener('loadedmetadata', _once);
            try { audioEl.currentTime = pos; } catch (_) {}
            if (wasPlaying) audioEl.play().catch(function() {});
          }, {once: true});
          audioEl.load();
        }
      }
    } else if (audioEl) {
      audioEl.remove();
    }

    // Metrics: rebuild contents of the metrics div only when timing changes
    const wantMetrics = !!msg.timing;
    let metricsEl = msgEl.querySelector('.chat-metrics');
    if (wantMetrics) {
      if (!metricsEl) {
        metricsEl = document.createElement('div');
        metricsEl.className = 'chat-metrics';
        msgEl.appendChild(metricsEl);
      }
      const t = msg.timing;
      // Only rebuild if content changed — cheap to rebuild though, so
      // keep the logic straightforward.
      const parts = [];
      if (t.prompt_tokens || t.completion_tokens) {
        parts.push('<span>' + (t.prompt_tokens||0) + '->' + (t.completion_tokens||0) + ' tok</span>');
      }
      if (t.tokens_per_second) {
        parts.push('<span>' + t.tokens_per_second + ' tok/s</span>');
      }
      if (t.time_to_first_token_ms != null) {
        parts.push('<span>TTFT ' + (t.time_to_first_token_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.generation_time_ms) {
        parts.push('<span>LLM ' + (t.generation_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.tts_time_ms) {
        parts.push('<span>TTS ' + (t.tts_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.total_time_ms) {
        parts.push('<span>Total ' + (t.total_time_ms/1000).toFixed(1) + 's</span>');
      }
      if (t.emotion) {
        const pct = t.emotion_intensity != null ? ' ' + (t.emotion_intensity * 100).toFixed(0) + '%' : '';
        const p = t.pad_p != null ? (t.pad_p >= 0 ? '+' : '') + t.pad_p.toFixed(2) : '?';
        const a = t.pad_a != null ? (t.pad_a >= 0 ? '+' : '') + t.pad_a.toFixed(2) : '?';
        const d = t.pad_d != null ? (t.pad_d >= 0 ? '+' : '') + t.pad_d.toFixed(2) : '?';
        const lock = t.emotion_locked_h ? ' [locked ' + t.emotion_locked_h.toFixed(1) + 'h]' : '';
        const tip = 'Pleasure:' + p + ' Arousal:' + a + ' Dominance:' + d
          + (t.emotion_locked_h ? ' | Cooldown: ' + t.emotion_locked_h.toFixed(1) + 'h remaining' : '');
        parts.push('<span class="emotion-metric" title="' + escAttr(tip) + '">'
          + '\u26A1 ' + escHtml(t.emotion) + pct + escHtml(lock) + '</span>');
      }
      const newHtml = parts.join('');
      if (metricsEl.innerHTML !== newHtml) {
        metricsEl.innerHTML = newHtml;
      }
    } else if (metricsEl) {
      metricsEl.remove();
    }
  }

  // "Thinking..." placeholder slot
  if (chatWaiting) {
    const thinkEl = el.children[chatHistory.length];
    if (thinkEl) {
      thinkEl.className = 'chat-msg assistant';
      thinkEl.innerHTML = '<div class="msg-label">GLaDOS</div>'
        + '<span class="thinking"><span class="spinner"></span> Thinking...</span>';
    }
  }

  el.scrollTop = el.scrollHeight;
}

async function chatSend() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || chatWaiting) return;

  chatHistory.push({role: 'user', content: text});
  input.value = '';
  chatWaiting = true;
  renderChat();

  const apiHistory = chatHistory.filter(m => m.role === 'user' || m.role === 'assistant')
    .map(m => ({role: m.role, content: m.content}));
  const history = apiHistory.slice(0, -1);

  try {
    await chatSendStreaming(text, history);
  } catch (e) {
    try {
      await chatSendBatch(text, history);
    } catch (e2) {
      chatHistory.push({role: 'assistant', content: 'Error: ' + e2.message});
      chatWaiting = false;
      renderChat();
    }
  }
}

function playAudioQueue(urls, onAllDone) {
  if (!urls || urls.length === 0) { if (onAllDone) onAllDone(); return; }
  let idx = 0;
  function playNext() {
    if (idx >= urls.length) { if (onAllDone) onAllDone(); return; }
    const audio = new Audio(urls[idx]);
    idx++;
    audio.onended = playNext;
    audio.onerror = playNext;
    audio.play().catch(() => playNext());
  }
  playNext();
}

async function chatSendStreaming(text, history) {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message: text, history }),
  });
  if (!resp.ok) throw new Error('Stream endpoint unavailable (' + resp.status + ')');
  if (!resp.body) throw new Error('ReadableStream not supported');

  const streamIdx = chatHistory.length;
  chatHistory.push({role: 'assistant', content: '', audio_url: null, timing: null});
  chatWaiting = false;
  chatStreaming = true;

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let pendingEventType = null;

  while (true) {
    const {done, value} = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, {stream: true});
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) { pendingEventType = null; continue; }

      // Capture named SSE event types
      if (trimmed.startsWith('event: ')) {
        pendingEventType = trimmed.slice(7).trim();
        continue;
      }

      if (!trimmed.startsWith('data: ')) continue;
      if (trimmed === 'data: [DONE]') continue;

      try {
        const chunk = JSON.parse(trimmed.slice(6));

        // Handle named events
        if (pendingEventType === 'timing') {
          chatHistory[streamIdx].timing = chunk;
          renderChat();
          pendingEventType = null;
          continue;
        }
        pendingEventType = null;

        if (chunk.full_text !== undefined) {
          chatStreaming = false;
          renderChat();
          continue;
        }

        if (chunk.audio_url !== undefined) {
          // Streaming audio URL. renderChat() is the single source of
          // DOM truth — on first mount it creates the <audio controls>
          // element, wires .play(), and never destroys it on later
          // content-chunk re-renders. No invisible background player,
          // no handoff, no restart-at-0:00 regression.
          if (chunk.audio_url) {
            chatHistory[streamIdx].audio_url = chunk.audio_url;
            renderChat();
          }
          continue;
        }

        if (chunk.audio_replay_url !== undefined) {
          // Static finalized WAV. renderChat() detects the src change
          // and swaps it in place on the existing element while
          // preserving currentTime + resuming if it was playing.
          chatHistory[streamIdx].audio_url = chunk.audio_replay_url;
          renderChat();
          continue;
        }

        if (chunk.audio_urls !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_urls[0] || null;
          renderChat();
          playAudioQueue(chunk.audio_urls);
          continue;
        }

        const content = chunk.choices?.[0]?.delta?.content;
        if (content) {
          chatHistory[streamIdx].content += content;
          renderChat();
        }
      } catch (e) { /* skip malformed chunks */ }
    }
  }

  // Process remaining buffer
  if (buffer.trim()) {
    for (const line of buffer.split('\n')) {
      const t = line.trim();
      if (!t.startsWith('data: ') || t === 'data: [DONE]') continue;
      try {
        const chunk = JSON.parse(t.slice(6));
        if (chunk.full_text !== undefined) {
          chatStreaming = false;
        } else if (chunk.audio_url !== undefined) {
          if (chunk.audio_url) {
            chatHistory[streamIdx].audio_url = chunk.audio_url;
          }
        } else if (chunk.audio_replay_url !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_replay_url;
        } else if (chunk.audio_urls !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_urls[0] || null;
          playAudioQueue(chunk.audio_urls);
        } else {
          const content = chunk.choices?.[0]?.delta?.content;
          if (content) chatHistory[streamIdx].content += content;
        }
      } catch(e) {}
    }
  }

  chatStreaming = false;
  renderChat();
}

async function chatSendBatch(text, history) {
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message: text, history }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || 'Chat failed');

  chatHistory.push({
    role: 'assistant',
    content: data.text,
    audio_url: data.audio_url || null,
  });

  chatWaiting = false;
  renderChat();

  if (data.audio_url) {
    try { new Audio(data.audio_url).play(); } catch(e) {}
  }
}

/* â”€â”€ Microphone (push-to-talk) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */

async function toggleMic() {
  const btn = document.getElementById('micBtn');
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    btn.classList.remove('recording');
    return;
  }

  try {
    if (!micStream) {
      micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    }
    const chunks = [];
    mediaRecorder = new MediaRecorder(micStream, {mimeType: 'audio/webm'});
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      btn.classList.remove('recording');
      const blob = new Blob(chunks, {type: 'audio/webm'});
      if (blob.size < 100) return;

      const input = document.getElementById('chatInput');
      input.value = 'Transcribing...';
      input.disabled = true;

      try {
        const resp = await fetch('/api/stt', {
          method: 'POST',
          headers: {'Content-Type': 'audio/webm'},
          body: blob,
        });
        const data = await resp.json();
        if (data.text && data.text.trim()) {
          input.value = data.text.trim();
          input.disabled = false;
          chatSend();
        } else {
          input.value = '';
          input.disabled = false;
        }
      } catch (e) {
        input.value = '';
        input.disabled = false;
        console.error('STT failed:', e);
      }
    };
    mediaRecorder.start();
    btn.classList.add('recording');
  } catch (e) {
    console.error('Mic access denied:', e);
    btn.style.display = 'none';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Tab 3: System Control
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _modeRefreshInterval = null;
let _healthRefreshInterval = null;

async function loadModes() {
  try {
    const resp = await fetch('/api/modes');
    const data = await resp.json();
    document.getElementById('maintToggle').checked = data.maintenance_mode;
    document.getElementById('silentToggle').checked = data.silent_mode;
    const speakerRow = document.getElementById('speakerRow');
    speakerRow.style.display = data.maintenance_mode ? 'flex' : 'none';
    if (data.maintenance_speaker) {
      const sel = document.getElementById('speakerSelect');
      for (const opt of sel.options) {
        if (opt.value === data.maintenance_speaker) { opt.selected = true; break; }
      }
    }
  } catch (e) { console.error('Failed to load modes:', e); }

  if (!_modeRefreshInterval) {
    _modeRefreshInterval = setInterval(loadModes, 10000);
  }
}

async function loadVerbositySliders() {
  const container = document.getElementById('verbositySliders');
  if (!container) return;
  try {
    const resp = await fetch('/api/announcement-settings');
    const data = await resp.json();
    const scenarios = data.scenarios || {};
    let html = '';
    for (const [key, cfg] of Object.entries(scenarios)) {
      const pct = Math.round((cfg.followup_probability || 0) * 100);
      html += '<div class="mode-row" style="flex-wrap:wrap;gap:4px;">'
        + '<div style="flex:1;min-width:120px;">'
        + '<div class="mode-label">' + cfg.label + '</div>'
        + '</div>'
        + '<div style="display:flex;align-items:center;gap:8px;min-width:200px;">'
        + '<input type="range" min="0" max="100" value="' + pct + '" '
        + 'style="flex:1;accent-color:var(--accent);" '
        + 'oninput="this.nextElementSibling.textContent=this.value+\'%\'" '
        + 'onchange="setVerbosity(\'' + key + '\',this.value)">'
        + '<span style="font-size:0.85rem;min-width:36px;text-align:right;">' + pct + '%</span>'
        + '</div></div>';
    }
    container.innerHTML = html || '<div style="color:var(--text-dim);">No announcement scenarios found.</div>';
    container.style.opacity = '1';
  } catch (e) {
    container.innerHTML = '<div style="color:var(--error);">Failed to load announcement settings.</div>';
    container.style.opacity = '1';
    console.error('Failed to load verbosity:', e);
  }
}

async function setVerbosity(scenario, pctValue) {
  try {
    await fetch('/api/announcement-settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scenario, followup_probability: parseInt(pctValue) / 100}),
    });
  } catch (e) { console.error('Failed to set verbosity:', e); }
}

async function loadStartupSpeakers() {
  const container = document.getElementById('startupSpeakers');
  if (!container) return;
  try {
    const resp = await fetch('/api/startup-speakers');
    const data = await resp.json();
    const speakers = data.speakers || [];
    if (!speakers.length) {
      container.innerHTML = '<div style="color:var(--text-dim);">No speakers found in speakers.yaml.</div>';
      container.style.opacity = '1';
      return;
    }
    let html = '';
    for (const sp of speakers) {
      html += '<div class="mode-row">'
        + '<div style="flex:1;">'
        + '<div class="mode-label">' + escHtml(sp.name) + '</div>'
        + '<div class="mode-desc" style="font-size:0.72rem;">' + escHtml(sp.entity_id) + '</div>'
        + '</div>'
        + '<label class="toggle">'
        + '<input type="checkbox" ' + (sp.startup ? 'checked' : '') + ' '
        + 'onchange="saveStartupSpeakers()" data-speaker="' + escAttr(sp.entity_id) + '">'
        + '<span class="toggle-slider"></span>'
        + '</label>'
        + '</div>';
    }
    container.innerHTML = html;
    container.style.opacity = '1';
  } catch (e) {
    container.innerHTML = '<div style="color:var(--error);">Failed to load speakers.</div>';
    container.style.opacity = '1';
    console.error('Failed to load startup speakers:', e);
  }
}

async function saveStartupSpeakers() {
  const status = document.getElementById('startupSpeakersStatus');
  const checkboxes = document.querySelectorAll('[data-speaker]');
  const selected = [];
  checkboxes.forEach(cb => { if (cb.checked) selected.push(cb.dataset.speaker); });
  if (!selected.length) {
    if (status) status.textContent = 'At least one speaker must be selected.';
    // Re-check the last unchecked box
    checkboxes.forEach(cb => { cb.checked = true; });
    return;
  }
  try {
    const resp = await fetch('/api/startup-speakers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({speakers: selected}),
    });
    const data = await resp.json();
    if (status) {
      status.textContent = data.status === 'ok'
        ? 'Saved. Restart glados-api to apply.'
        : (data.note || 'Saved.');
      setTimeout(() => { if (status) status.textContent = ''; }, 4000);
    }
  } catch (e) {
    if (status) status.textContent = 'Failed to save.';
    console.error('Failed to save startup speakers:', e);
  }
}

async function loadSpeakers() {
  const sel = document.getElementById('speakerSelect');
  try {
    const resp = await fetch('/api/speakers');
    const data = await resp.json();
    sel.innerHTML = '<option value="">-- Select speaker --</option>';
    for (const sp of (data.speakers || [])) {
      const opt = document.createElement('option');
      opt.value = sp.entity_id;
      opt.textContent = sp.name + ' (' + sp.area + ')';
      sel.appendChild(opt);
    }
  } catch (e) {
    sel.innerHTML = '<option value="">Error loading speakers</option>';
  }
}

async function toggleMaintenance() {
  const on = document.getElementById('maintToggle').checked;
  const speakerRow = document.getElementById('speakerRow');

  if (on) {
    speakerRow.style.display = 'flex';
    const speaker = document.getElementById('speakerSelect').value;
    if (!speaker) {
      document.getElementById('maintToggle').checked = false;
      document.getElementById('speakerSelect').focus();
      return;
    }
    await fetch('/api/modes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'maintenance_on', speaker}),
    });
  } else {
    speakerRow.style.display = 'none';
    await fetch('/api/modes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'maintenance_off'}),
    });
  }
}

async function toggleSilent() {
  const on = document.getElementById('silentToggle').checked;
  await fetch('/api/modes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: on ? 'silent_on' : 'silent_off'}),
  });
}

async function loadEyeDemo() {
  try {
    const resp = await fetch('/api/eye-demo');
    const data = await resp.json();
    document.getElementById('eyeDemoToggle').checked = data.running;
  } catch (e) { console.error('Failed to load eye demo state:', e); }
}

async function toggleEyeDemo() {
  const toggle = document.getElementById('eyeDemoToggle');
  const action = toggle.checked ? 'start' : 'stop';
  try {
    const resp = await fetch('/api/eye-demo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
    const data = await resp.json();
    if (!data.ok) {
      toggle.checked = !toggle.checked;
      console.error('Eye demo toggle failed:', data);
    }
  } catch (e) {
    toggle.checked = !toggle.checked;
    console.error('Eye demo toggle error:', e);
  }
}

async function loadHealth() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    for (const [key, ok] of Object.entries(data)) {
      const dot = document.getElementById('hd-' + key);
      if (dot) {
        dot.className = 'health-dot ' + (ok ? 'ok' : 'err');
      }
    }
  } catch (e) {
    document.querySelectorAll('.health-dot').forEach(d => d.className = 'health-dot unknown');
  }

  if (!_healthRefreshInterval) {
    _healthRefreshInterval = setInterval(loadHealth, 30000);
  }
}

async function restartService(key) {
  const btn = document.querySelector('.health-item #hd-' + key)?.parentElement?.querySelector('.restart-btn');
  if (btn) {
    btn.classList.add('restarting');
    btn.disabled = true;
  }
  const dot = document.getElementById('hd-' + key);
  if (dot) dot.className = 'health-dot unknown';
  try {
    const resp = await fetch('/api/restart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service: key}),
    });
    const data = await resp.json();
    if (data.ok) {
      setTimeout(loadHealth, 3000);
    } else {
      if (dot) dot.className = 'health-dot err';
      alert('Restart failed: ' + (data.stderr || data.error || 'unknown error'));
    }
  } catch (e) {
    if (dot) dot.className = 'health-dot err';
    alert('Restart request failed: ' + e.message);
  } finally {
    if (btn) {
      btn.classList.remove('restarting');
      btn.disabled = false;
    }
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Weather & GPU monitoring
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadWeather() {
  const panel = document.getElementById('weatherPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/weather');
    const data = await resp.json();
    if (data.error) {
      panel.innerHTML = '<div style="color:var(--text-dim)">' + escHtml(data.error) + '</div>';
      return;
    }
    const c = data.current || {};
    const t = data.today || {};
    const units = data.units || {};
    let html = '<div class="weather-grid">';
    html += '<div class="weather-item"><div class="weather-label">Temperature</div>'
      + '<div class="weather-value highlight">' + (c.temperature ?? '?') + (units.temperature || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Condition</div>'
      + '<div class="weather-value">' + escHtml(c.condition || '?') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Wind</div>'
      + '<div class="weather-value">' + (c.wind_speed ?? '?') + ' ' + (units.wind_speed || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Humidity</div>'
      + '<div class="weather-value">' + (c.humidity ?? '?') + '%</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Today High / Low</div>'
      + '<div class="weather-value">' + (t.high ?? '?') + (units.temperature || '') + ' / ' + (t.low ?? '?') + (units.temperature || '') + '</div></div>';
    html += '<div class="weather-item"><div class="weather-label">Today</div>'
      + '<div class="weather-value">' + escHtml(t.condition || '?') + '</div></div>';
    html += '</div>';
    if (data._cache_age_s != null) {
      const mins = Math.round(data._cache_age_s / 60);
      html += '<div style="font-size:0.7rem;color:#666;margin-top:6px;">Cache age: ' + mins + 'm</div>';
    }
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load weather</div>';
  }
}

async function loadGPU() {
  const panel = document.getElementById('gpuPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/gpu');
    const data = await resp.json();
    if (data.error) {
      panel.innerHTML = '<div style="color:var(--text-dim)">' + escHtml(data.error) + '</div>';
      return;
    }
    const gpus = data.gpus || [];
    if (!gpus.length) {
      panel.innerHTML = '<div style="color:var(--text-dim)">No GPUs detected</div>';
      return;
    }
    let html = '';
    for (const g of gpus) {
      const memPct = Math.round(g.memory_used_mb / g.memory_total_mb * 100);
      const barClass = memPct > 90 ? 'crit' : memPct > 70 ? 'hot' : 'mem';
      html += '<div class="gpu-card">'
        + '<div class="gpu-name">GPU ' + g.index + ': ' + escHtml(g.name) + '</div>'
        + '<div class="gpu-stat">'
        + '<span>VRAM: ' + g.memory_used_mb + ' / ' + g.memory_total_mb + ' MB (' + memPct + '%)</span>'
        + '<span>' + (g.temperature_c != null ? g.temperature_c + '\u00B0C' : '') + '</span>'
        + '</div>'
        + '<div class="gpu-bar-bg"><div class="gpu-bar-fill ' + barClass + '" style="width:' + memPct + '%"></div></div>';
      if (g.note) {
        html += '<div style="font-size:0.7rem;color:#888;margin-top:2px;">' + escHtml(g.note) + '</div>';
      }
      html += '</div>';
    }
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load GPU data</div>';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   GPU auto-refresh (15s), Weather auto-refresh (5min)
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _gpuRefreshInterval = null;
let _weatherRefreshInterval = null;

function startGPUAutoRefresh() {
  if (!_gpuRefreshInterval) {
    _gpuRefreshInterval = setInterval(loadGPU, 15000);
  }
}

function startWeatherAutoRefresh() {
  if (!_weatherRefreshInterval) {
    _weatherRefreshInterval = setInterval(loadWeather, 300000);
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Weather manual refresh
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function refreshWeather() {
  const btn = document.getElementById('weatherRefreshBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Refreshing...';
  try {
    const resp = await fetch('/api/weather/refresh', {method: 'POST'});
    if (resp.ok) {
      await loadWeather();
      showToast('Weather refreshed', 'success');
    } else {
      const data = await resp.json().catch(() => ({}));
      showToast('Refresh failed: ' + (data.error || resp.status), 'error');
    }
  } catch(e) {
    showToast('Refresh failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Service Logs viewer
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadLogs() {
  const service = document.getElementById('logServiceSelect').value;
  const panel = document.getElementById('logPanel');
  const info = document.getElementById('logSizeInfo');
  if (!panel) return;
  panel.textContent = 'Loading...';
  try {
    const resp = await fetch('/api/logs?service=' + encodeURIComponent(service) + '&lines=500');
    const data = await resp.json();
    panel.textContent = (data.lines || []).join('\n') || '(empty)';
    panel.scrollTop = panel.scrollHeight;
    if (info && data.total_size != null) {
      info.textContent = 'Log size: ' + fmtSize(data.total_size);
    }
  } catch(e) {
    panel.textContent = 'Failed to load logs: ' + e.message;
  }
}

async function clearLog() {
  const service = document.getElementById('logServiceSelect').value;
  if (!confirm('Clear ' + service + ' log file?')) return;
  try {
    const resp = await fetch('/api/logs/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service: service})
    });
    if (resp.ok) {
      showToast('Log cleared', 'success');
      loadLogs();
    } else {
      showToast('Clear failed', 'error');
    }
  } catch(e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Audio Storage stats and cleanup
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

async function loadAudioStats() {
  const panel = document.getElementById('audioStatsPanel');
  if (!panel) return;
  try {
    const resp = await fetch('/api/audio/stats');
    const data = await resp.json();
    const labels = {
      ha_output: 'HA Playback',
      archive: 'Archive',
      tts_ui: 'TTS Generator',
      chat_audio: 'Chat Audio',
    };
    let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">';
    for (const [key, stats] of Object.entries(data)) {
      html += '<div style="background:var(--bg-input);padding:10px;border-radius:6px;">'
        + '<div style="font-weight:500;margin-bottom:4px;">' + escHtml(labels[key] || key) + '</div>'
        + '<div style="font-size:0.78rem;color:var(--text-dim);">' + stats.count + ' files (' + fmtSize(stats.size_bytes) + ')</div>'
        + '<button class="btn-small" style="margin-top:6px;font-size:0.72rem;padding:3px 8px;" onclick="clearAudioDir(\'' + key + '\')">Clear</button>'
        + '</div>';
    }
    html += '</div>';
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ff6666">Failed to load audio stats</div>';
  }
}

async function clearAudioDir(key) {
  if (!confirm('Clear all audio files in this directory?')) return;
  try {
    const resp = await fetch('/api/audio/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({directory: key})
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast('Cleared ' + data.deleted + ' files', 'success');
      loadAudioStats();
    } else {
      showToast('Clear failed: ' + (data.error || 'unknown'), 'error');
    }
  } catch(e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Robot Nodes management
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

let _robotRefreshInterval = null;

async function loadRobots() {
  const card = document.getElementById('robotNodesCard');
  if (!card) return;
  try {
    const resp = await fetch('/api/robots/status');
    const data = await resp.json();
    if (!data.enabled) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    const list = document.getElementById('robotNodesList');
    const nodes = data.nodes || {};
    const nodeIds = Object.keys(nodes);
    if (nodeIds.length === 0) {
      list.innerHTML = '<div style="color:var(--text-dim);">No nodes configured. Add one below.</div>';
    } else {
      let html = '<div class="health-grid">';
      for (const [nid, n] of Object.entries(nodes)) {
        const dotClass = !n.enabled ? 'unknown' : (n.reachable ? 'ok' : 'err');
        const label = n.name || nid;
        const uptimeStr = n.reachable && n.uptime_s > 0 ? ' (' + fmtUptime(n.uptime_s) + ')' : '';
        html += '<div class="health-item" style="justify-content:space-between;">'
          + '<span><span class="health-dot ' + dotClass + '"></span>' + escHtml(label) + uptimeStr + '</span>'
          + '<span style="display:flex;gap:4px;align-items:center;">'
          + '<button class="restart-btn" onclick="robotIdentify(\'' + escHtml(nid) + '\')" title="Identify (flash LED)">&#128161;</button>'
          + '<label class="toggle" style="transform:scale(0.75);margin:0;"><input type="checkbox" ' + (n.enabled ? 'checked' : '') + ' onchange="robotToggle(\'' + escHtml(nid) + '\', this.checked)"><span class="toggle-slider"></span></label>'
          + '<button class="restart-btn" onclick="robotRemove(\'' + escHtml(nid) + '\')" title="Remove node" style="color:#e74c3c;">&#10005;</button>'
          + '</span></div>';
      }
      html += '</div>';
      list.innerHTML = html;
    }

    // Bots section
    const bots = data.bots || {};
    const botIds = Object.keys(bots);
    const botsSection = document.getElementById('robotBotsSection');
    if (botIds.length > 0) {
      botsSection.style.display = '';
      let bhtml = '';
      for (const [bid, b] of Object.entries(bots)) {
        const bLabel = b.name || bid;
        bhtml += '<div style="background:var(--bg-input);padding:8px 10px;border-radius:4px;margin-bottom:4px;">'
          + '<strong>' + escHtml(bLabel) + '</strong> <span style="color:var(--text-dim);">(' + escHtml(b.profile) + ')</span>';
        for (const [role, rn] of Object.entries(b.nodes || {})) {
          const rdot = rn.reachable ? '&#9679;' : '&#9675;';
          bhtml += ' <span style="margin-left:8px;">' + rdot + ' ' + escHtml(role) + ': ' + escHtml(rn.node_id) + '</span>';
        }
        bhtml += '</div>';
      }
      document.getElementById('robotBotsList').innerHTML = bhtml;
    } else {
      botsSection.style.display = 'none';
    }
  } catch(e) {
    console.error('Failed to load robots:', e);
  }
}

function fmtUptime(s) {
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

function startRobotAutoRefresh() {
  if (!_robotRefreshInterval) {
    _robotRefreshInterval = setInterval(loadRobots, 15000);
  }
}

async function robotAddNode() {
  const url = document.getElementById('robotNodeUrl').value.trim();
  if (!url) { showToast('Enter a node URL', 'error'); return; }
  const name = document.getElementById('robotNodeName').value.trim();
  try {
    const resp = await fetch('/api/robots/node/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, name})
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('robotNodeUrl').value = '';
      document.getElementById('robotNodeName').value = '';
      showToast('Node added: ' + data.node_id, 'success');
      loadRobots();
    } else {
      showToast(data.error || 'Failed to add node', 'error');
    }
  } catch(e) {
    showToast('Add node failed: ' + e.message, 'error');
  }
}

async function robotRemove(nodeId) {
  if (!confirm('Remove robot node "' + nodeId + '"?')) return;
  try {
    const resp = await fetch('/api/robots/node/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId})
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Node removed', 'success');
      loadRobots();
    } else {
      showToast('Remove failed', 'error');
    }
  } catch(e) {
    showToast('Remove failed: ' + e.message, 'error');
  }
}

async function robotToggle(nodeId, enabled) {
  try {
    await fetch('/api/robots/node/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId, enabled})
    });
    setTimeout(loadRobots, 1000);
  } catch(e) {
    showToast('Toggle failed: ' + e.message, 'error');
  }
}

async function robotIdentify(nodeId) {
  try {
    const resp = await fetch('/api/robots/node/identify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId})
    });
    const data = await resp.json();
    if (data.ok) showToast('LED identify sent', 'success');
    else showToast('Identify failed â€” node unreachable?', 'error');
  } catch(e) {
    showToast('Identify failed: ' + e.message, 'error');
  }
}

async function robotEmergencyStop() {
  try {
    const resp = await fetch('/api/robots/emergency-stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const data = await resp.json();
    if (data.ok) showToast('EMERGENCY STOP sent to all nodes', 'success');
    else showToast('Emergency stop failed', 'error');
    loadRobots();
  } catch(e) {
    showToast('Emergency stop failed: ' + e.message, 'error');
  }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Training Monitor
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
let _trainChart = null;
let _trainLastStep = 0;
let _trainRefreshInterval = null;
let _trainLogInterval = null;

function initTrainingTab() {
  // Auth overlay
  const overlay = document.getElementById('trainingAuthOverlay');
  if (overlay) overlay.style.display = _isAuthenticated ? 'none' : 'flex';
  const lock = document.getElementById('lockTraining');
  if (lock) lock.textContent = _isAuthenticated ? '' : '\u{1F512}';

  if (!_isAuthenticated) return;

  loadTrainingStatus();
  loadTrainingMetrics(true);
  loadTrainingLog();

  // Start polling
  if (_trainRefreshInterval) clearInterval(_trainRefreshInterval);
  _trainRefreshInterval = setInterval(() => {
    if (document.getElementById('tab-training').classList.contains('active')) {
      loadTrainingStatus();
      loadTrainingMetrics(false);
    }
  }, 5000);

  if (_trainLogInterval) clearInterval(_trainLogInterval);
  _trainLogInterval = setInterval(() => {
    if (document.getElementById('tab-training').classList.contains('active')) {
      loadTrainingLog();
    }
  }, 10000);
}

async function loadTrainingStatus() {
  try {
    const r = await fetch('/api/training/status');
    const d = await r.json();

    const dot = document.getElementById('trainRunning');
    const txt = document.getElementById('trainRunningText');
    if (d.running) {
      dot.className = 'train-dot train-dot-on';
      txt.textContent = 'Training';
    } else {
      dot.className = 'train-dot train-dot-off';
      txt.textContent = 'Stopped';
    }

    document.getElementById('trainEpoch').textContent = d.ft_epoch != null ? d.ft_epoch + ' / ' + (d.max_epochs - d.base_epoch) : '--';
    document.getElementById('trainGenLoss').textContent = d.gen_loss != null ? (d.gen_loss > 1e6 ? d.gen_loss.toExponential(1) : d.gen_loss.toFixed(1)) : '--';
    document.getElementById('trainDiscLoss').textContent = d.disc_loss != null ? d.disc_loss.toFixed(3) : '--';

    // Snapshot status
    const ss = document.getElementById('snapshotStatus');
    if (d.snapshot) {
      ss.textContent = d.snapshot.message || '';
      if (d.snapshot.state === 'running') {
        ss.className = 'snap-running';
        document.getElementById('btnSnapshot').disabled = true;
      } else {
        ss.className = '';
        document.getElementById('btnSnapshot').disabled = false;
      }
    }
  } catch(e) {}
}

async function loadTrainingMetrics(fullLoad) {
  try {
    const since = fullLoad ? 0 : _trainLastStep;
    const r = await fetch('/api/training/metrics?since_step=' + since);
    const d = await r.json();
    if (!d.metrics || d.metrics.length === 0) return;

    if (fullLoad || !_trainChart) {
      _trainLastStep = 0;
      initTrainingChart(d.metrics);
    } else {
      appendTrainingChart(d.metrics);
    }

    _trainLastStep = d.metrics[d.metrics.length - 1].step;
  } catch(e) {}
}

function initTrainingChart(data) {
  const ctx = document.getElementById('trainingChart');
  if (_trainChart) _trainChart.destroy();

  const labels = data.map(m => m.ft_epoch);
  const genData = data.map(m => m.gen_loss);
  const discData = data.map(m => m.disc_loss);

  _trainChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Generator Loss',
          data: genData,
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245,158,11,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          yAxisID: 'yGen',
          tension: 0.2,
        },
        {
          label: 'Discriminator Loss',
          data: discData,
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59,130,246,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          yAxisID: 'yDisc',
          tension: 0.2,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#999' } },
        tooltip: { backgroundColor: '#1e1e1e', titleColor: '#fff', bodyColor: '#ccc' }
      },
      scales: {
        x: {
          title: { display: true, text: 'Fine-Tune Epoch', color: '#666' },
          ticks: { color: '#666', maxTicksLimit: 20 },
          grid: { color: '#222' }
        },
        yGen: {
          type: 'logarithmic',
          position: 'left',
          title: { display: true, text: 'Gen Loss (log)', color: '#f59e0b' },
          ticks: { color: '#f59e0b' },
          grid: { color: '#222' }
        },
        yDisc: {
          type: 'linear',
          position: 'right',
          title: { display: true, text: 'Disc Loss', color: '#3b82f6' },
          ticks: { color: '#3b82f6' },
          grid: { drawOnChartArea: false }
        }
      }
    }
  });
}

function appendTrainingChart(data) {
  if (!_trainChart || !data.length) return;
  for (const m of data) {
    _trainChart.data.labels.push(m.ft_epoch);
    _trainChart.data.datasets[0].data.push(m.gen_loss);
    _trainChart.data.datasets[1].data.push(m.disc_loss);
  }
  _trainChart.update('none');
}

async function loadTrainingLog() {
  try {
    const r = await fetch('/api/training/log?lines=100');
    const d = await r.json();
    const el = document.getElementById('trainingLog');
    if (d.lines && d.lines.length > 0) {
      el.textContent = d.lines.join('\n');
      el.scrollTop = el.scrollHeight;
    } else {
      el.textContent = 'No training log available.';
    }
  } catch(e) {}
}

async function trainingSnapshot() {
  if (!confirm('Snapshot the current checkpoint and deploy to GLaDOS TTS?\n\nThis will export the model to ONNX and restart the TTS service.')) return;
  try {
    const r = await fetch('/api/training/snapshot', {method:'POST'});
    const d = await r.json();
    if (d.ok) showToast('Snapshot started...', 'success');
    else showToast(d.error || 'Snapshot failed', 'error');
  } catch(e) {
    showToast('Snapshot request failed', 'error');
  }
}

async function trainingStop() {
  if (!confirm('Stop the training process?\n\nThis will kill the running piper_train process. You will need to restart training manually.')) return;
  try {
    const r = await fetch('/api/training/stop', {method:'POST'});
    const d = await r.json();
    if (d.ok) showToast(d.message, 'success');
    else showToast(d.error || 'Stop failed', 'error');
  } catch(e) {
    showToast('Stop request failed', 'error');
  }
}

</script>
</body>
</html>
"""

# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    # Enable HTTPS if cert files exist
    use_ssl = SSL_CERT and SSL_KEY and SSL_CERT.exists() and SSL_KEY.exists()
    if use_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(SSL_CERT), keyfile=str(SSL_KEY))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
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
    server = ThreadingHTTPServer((host, port), Handler)

    use_ssl = SSL_CERT and SSL_KEY and SSL_CERT.exists() and SSL_KEY.exists()
    if use_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(SSL_CERT), keyfile=str(SSL_KEY))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        proto = "https"
    else:
        proto = "http"

    from loguru import logger
    logger.info("GLaDOS WebUI listening on {}://{}:{}", proto, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
