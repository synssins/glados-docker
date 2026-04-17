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

import yaml

# ---------------------------------------------------------------------------
# Configuration â€” all values from centralized config store
# ---------------------------------------------------------------------------
from glados.core.config_store import cfg as _cfg
from glados.observability import AuditEvent, Origin, audit

TTS_URL = _cfg.service_url("tts") + "/v1/audio/speech"
GLADOS_API_URL = _cfg.service_url("api_wrapper")
OLLAMA_CHAT_HOST = "localhost"
OLLAMA_CHAT_PORT = int(_cfg.service_url("ollama_interactive").rsplit(":", 1)[-1])
OLLAMA_CHAT_MODEL = "glados"       # Qwen 2.5 14B with GLaDOS personality
STT_URL = _cfg.service_url("stt")
VISION_URL = _cfg.service_url("vision")
OLLAMA_URL = _cfg.service_url("ollama_interactive") + "/api/generate"
OLLAMA_MODEL = "glados:latest"

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
    # Check expiry
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _create_session(remember: bool = False) -> str:
    """Create a signed session token."""
    ttl = _SESSION_LONG_S if remember else _SESSION_SHORT_S
    payload = json.dumps({
        "sub": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl,
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
    """Check if the request is authenticated (or auth is disabled)."""
    if not _AUTH_ENABLED or not _AUTH_PASSWORD_HASH:
        return True
    return _get_session_cookie(handler) is not None


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
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
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
        """Serve the login page HTML."""
        body = LOGIN_PAGE.encode()
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

        # Success â€” create session
        _clear_fails(client_ip)
        token = _create_session(remember=remember)
        max_age = _SESSION_LONG_S if remember else _SESSION_SHORT_S

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
        elif p == "/api/audio/stats":
            self._get_audio_stats()
        elif p.startswith("/api/logs"):
            self._get_logs()
        elif p == "/api/audit/recent" or p.startswith("/api/audit/recent?"):
            self._get_audit_recent()
        elif p == "/api/config":
            self._get_config()
        elif p.startswith("/api/config/"):
            section = p.split("/api/config/", 1)[1]
            if section == "raw":
                self._get_config_raw()
            else:
                self._get_config_section(section)
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

        # Main page and TTS/Chat routes are public
        if self.path in ("/", "/index.html") or _is_public_route(self.path):
            self._dispatch_get()
            return

        # Protected routes â€” require auth
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
            else:
                self._put_config_section(section)
        else:
            self._send_error(404, "Not found")

    def do_DELETE(self):
        if not self._require_auth():
            return
        if self.path.startswith("/api/files/"):
            self._delete_file()
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
        tts_req = urllib.request.Request(TTS_URL, data=tts_body,
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
            f"{GLADOS_API_URL}/v1/chat/completions",
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
            tts_req = urllib.request.Request(TTS_URL, data=tts_body,
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
                    TTS_URL, data=tts_body,
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
            f"{STT_URL}/v1/audio/transcriptions",
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
                f"{GLADOS_API_URL}/api/announcement-settings",
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
                f"{GLADOS_API_URL}/api/announcement-settings",
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
                f"{GLADOS_API_URL}/api/startup-speakers",
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
                f"{GLADOS_API_URL}/api/startup-speakers",
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
        import socket
        status = {}
        checks = {
            "glados_api": f"{GLADOS_API_URL}/health",
            "stt": f"{STT_URL}/health",
            "vision": f"{VISION_URL}/health",
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
        # TTS: socket connection test (server returns 404 on GET /)
        try:
            with socket.create_connection(("localhost", 5050), timeout=3):
                status["tts"] = True
        except Exception:
            status["tts"] = False
        # ChromaDB: heartbeat check
        try:
            req = urllib.request.Request("http://localhost:8000/api/v2/heartbeat")
            with urllib.request.urlopen(req, timeout=3) as resp:
                status["chromadb"] = resp.status < 400
        except Exception:
            status["chromadb"] = False
        self._send_json(200, status)

    # â”€â”€ Attitudes endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_attitudes(self):
        """Proxy attitude list from the API wrapper."""
        try:
            req = urllib.request.Request(f"{GLADOS_API_URL}/api/attitudes")
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
            req = urllib.request.Request(f"{TTS_URL.rsplit('/v1/', 1)[0]}/v1/voices")
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
        """Update a config section from JSON body."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_error(400, f"Invalid JSON: {e}")
            return
        try:
            _cfg.update_section(section, data)
            self._send_json(200, {"ok": True, "section": section})
        except KeyError as e:
            self._send_error(404, str(e))
        except Exception as e:
            self._send_error(400, f"Validation error: {e}")

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
            self._send_json(200, {"ok": True, "file": filename})
        except Exception as e:
            self._send_error(400, f"Error: {e}")

    def _reload_config(self):
        """Reload all config from disk."""
        try:
            _cfg.reload()
            self._send_json(200, {"ok": True, "message": "Config reloaded"})
        except Exception as e:
            self._send_error(500, f"Reload failed: {e}")

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
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())




# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HTML / CSS / JS â€” GLaDOS Control Panel (Responsive Sidebar Layout)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GLaDOS Control Panel</title>
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
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--orange);
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
}
.sidebar-brand span { color: var(--text-dim); font-weight: 400; font-size: 0.85rem; }
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
.section-title { font-size: 0.95rem; font-weight: 600; margin-bottom: 0.75rem; color: var(--text); }
.char-count { font-size: 0.8rem; color: var(--text-dim); text-align: right; margin-top: 0.25rem; }

/* â”€â”€ Chat Tab â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.chat-messages {
  height: 400px; overflow-y: auto;
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

/* â”€â”€ Config Styles â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.cfg-section-header {
  margin-bottom: 1.25rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--border);
}
.cfg-section-title {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 0.25rem;
}
.cfg-section-desc {
  font-size: 0.8rem;
  color: var(--text-dim);
}
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
.toast {
  position: fixed;
  bottom: 1.5rem; right: 1.5rem;
  padding: 0.75rem 1.25rem;
  border-radius: 6px;
  font-size: 0.85rem; font-weight: 500;
  z-index: 9999;
  opacity: 0;
  transform: translateY(10px);
  transition: opacity 0.3s, transform 0.3s;
  pointer-events: none;
}
.toast.visible { opacity: 1; transform: translateY(0); pointer-events: auto; }
.toast.success { background: var(--green); color: #fff; }
.toast.error { background: var(--red); color: #fff; }

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
  <div class="sidebar-brand">GLaDOS <span>Control</span></div>
  <div class="nav-items">
    <a class="nav-item active" onclick="switchTab('tts')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
      TTS Generator
    </a>
    <a class="nav-item" onclick="switchTab('chat')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      Chat
    </a>
    <a class="nav-item" onclick="switchTab('control')" data-requires-auth="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      System <span class="lock-icon" id="lockControl"></span>
    </a>
    <a class="nav-item" onclick="switchTab('config')" data-requires-auth="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
      Configuration <span class="lock-icon" id="lockConfig"></span>
    </a>
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
    <a class="nav-item active" onclick="switchTab('tts')">TTS</a>
    <a class="nav-item" onclick="switchTab('chat')">Chat</a>
    <a class="nav-item" onclick="switchTab('control')" data-requires-auth="true">System</a>
    <a class="nav-item" onclick="switchTab('config')" data-requires-auth="true">Config</a>
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
<div id="tab-tts" class="tab-content active">
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
<div id="tab-chat" class="tab-content">
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
<div id="tab-control" class="tab-content">
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
    <div class="section-title">Configuration Manager</div>
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;">
      <button class="cfg-tab-btn active" onclick="cfgSwitchSection('global',this)">Global</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('services',this)">Services</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('speakers',this)">Speakers</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('audio',this)">Audio</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('personality',this)">Personality</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('ssl',this)">SSL</button>
      <button class="cfg-tab-btn" onclick="cfgSwitchSection('raw',this)">Raw YAML</button>
    </div>

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

</main>

<!-- Toast -->
<div id="toast" class="toast"></div>

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

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (type || 'success') + ' visible';
  setTimeout(() => { t.className = 'toast'; }, 3000);
}

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
  // â”€â”€ Global: Network â”€â”€
  'network.serve_host':    { label: 'Server Host', desc: 'IP address for the file server', advanced: true },
  'network.serve_port':    { label: 'Server Port', desc: 'Port for the audio file server', advanced: true },
  // â”€â”€ Global: Paths â”€â”€
  'paths.glados_root':     { label: 'GLaDOS Root Path', desc: 'Root directory of the GLaDOS installation', advanced: true },
  // paths.nssm removed — NSSM is not used in the container deployment
  'paths.audio_base':      { label: 'Audio Base Path', desc: 'Root directory for all audio files', advanced: true },
  // â”€â”€ Global: SSL â”€â”€
  'ssl.domain':            { label: 'SSL Domain', desc: 'Domain name for SSL certificate', advanced: true },
  'ssl.certbot_dir':       { label: 'Certbot Directory', desc: 'Path to Let\'s Encrypt certificates', advanced: true },
  // â”€â”€ Global: Auth â”€â”€
  'auth.enabled':          { label: 'Authentication Enabled', desc: 'Require login to access System and Config' },
  'auth.password_hash':    { label: 'Password Hash', desc: 'Bcrypt hash (use set_password tool to change)', advanced: true, type: 'password' },
  'auth.session_secret':   { label: 'Session Secret', desc: 'Secret key for session tokens', advanced: true, type: 'password' },
  'auth.session_timeout_hours': { label: 'Session Timeout (hours)', desc: 'How long before a session expires' },
  // â”€â”€ Global: Mode Entities â”€â”€
  'mode_entities.maintenance_mode':    { label: 'Maintenance Mode Entity', desc: 'HA entity for maintenance mode', advanced: true },
  'mode_entities.maintenance_speaker': { label: 'Maintenance Speaker Entity', desc: 'HA entity for maintenance speaker selection', advanced: true },
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
  // â”€â”€ Global: Tuning â”€â”€
  'tuning.llm_connect_timeout_s':  { label: 'LLM Connect Timeout (s)', desc: 'Seconds to wait for LLM connection', advanced: true },
  'tuning.llm_read_timeout_s':     { label: 'LLM Read Timeout (s)', desc: 'Max seconds to wait for LLM response', advanced: true },
  'tuning.tts_flush_chars':        { label: 'TTS Flush Threshold', desc: 'Characters to buffer before sending to TTS', advanced: true },
  'tuning.engine_pause_time':      { label: 'Engine Pause Time (s)', desc: 'Pause between engine loop iterations', advanced: true },
  'tuning.mode_cache_ttl_s':       { label: 'Mode Cache TTL (s)', desc: 'Seconds to cache HA mode entity states', advanced: true },
  // â”€â”€ Audio â”€â”€
  'ha_output_dir':         { label: 'HA Output Directory', desc: 'Where HA playback files are stored', pathMask: '/app/audio_files/' },
  'archive_dir':           { label: 'Archive Directory', desc: 'Where archived audio files go', pathMask: '/app/audio_files/' },
  'archive_max_files':     { label: 'Max Archive Files', desc: 'Maximum files to keep in the archive' },
  'tts_ui_output_dir':     { label: 'TTS UI Output', desc: 'Output directory for WebUI TTS generation', pathMask: '/app/audio_files/' },
  'tts_ui_max_files':      { label: 'Max TTS UI Files', desc: 'Maximum generated files to keep' },
  'chat_audio_dir':        { label: 'Chat Audio Directory', desc: 'Where chat audio responses are stored', pathMask: '/app/audio_files/' },
  'chat_audio_max_files':  { label: 'Max Chat Audio Files', desc: 'Maximum chat audio files to keep' },
  'announcements_dir':     { label: 'Announcements Directory', desc: 'Pre-generated announcement WAV files', pathMask: '/app/audio_files/', advanced: true },
  'commands_dir':          { label: 'Commands Directory', desc: 'Pre-recorded command audio files', pathMask: '/app/audio_files/', advanced: true },
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
  global:      { title: 'Global Settings', desc: 'Home Assistant connection, network, paths, authentication, silent hours, and tuning' },
  services:    { title: 'Services', desc: 'Service endpoint URLs and health status for all GLaDOS services' },
  speakers:    { title: 'Speakers', desc: 'Home Assistant media player configuration' },
  audio:       { title: 'Audio', desc: 'Audio file paths, limits, and synthesis parameters' },
  personality: { title: 'Personality', desc: 'Attitudes, TTS defaults, HEXACO traits, and emotion model' },
  robots:      { title: 'Robots', desc: 'Robot node integration â€” ESP32 nodes, bots, and emergency stop' },
  raw:         { title: 'Raw YAML', desc: 'Edit configuration files directly as YAML' },
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

function cfgRenderSection(section) {
  const data = (section === 'ssl') ? (_cfgData.global || {}) : _cfgData[section];
  if (!data) {
    document.getElementById('cfg-form-area').innerHTML =
      '<div style="color:#ff6666;padding:20px;">Section not loaded. Click Reload.</div>';
    return;
  }
  const meta = SECTION_META[section] || {};
  let html = '<div class="cfg-section-header">'
    + '<div class="cfg-section-title">' + escHtml(meta.title || section) + '</div>'
    + '<div class="cfg-section-desc">' + escHtml(meta.desc || '') + '</div>'
    + '</div>';

  if (section === 'services') {
    html += cfgRenderServices(data);
  } else if (section === 'personality') {
    html += cfgRenderPersonality(data);
  } else if (section === 'ssl') {
    html += cfgRenderSsl(_cfgData.global && _cfgData.global.ssl ? _cfgData.global.ssl : {});
  } else {
    html += cfgBuildForm(data, section, '');
  }

  if (section !== 'ssl') {
    html += '<div class="cfg-save-row">'
      + '<button class="cfg-save-btn" onclick="cfgSaveSection(\'' + section + '\')">Save ' + escHtml(section) + '</button>'
      + '<span id="cfg-save-result" class="cfg-result"></span>'
      + '</div>';
  }
  document.getElementById('cfg-form-area').innerHTML = html;
}

function cfgBuildForm(obj, section, prefix) {
  let html = '';
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? prefix + '.' + key : key;
    const fieldId = 'cfg-' + section + '-' + path.replace(/\./g, '-');
    const meta = FIELD_META[path] || {};
    const label = meta.label || key;
    const desc = meta.desc || '';
    const isAdvanced = meta.advanced === true;
    const advAttr = isAdvanced ? ' data-advanced="true"' : '';

    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      // Check if entire group is advanced
      const groupAdvanced = Object.keys(value).every(k => {
        const childPath = path ? path + '.' + k : k;
        return (FIELD_META[childPath] || {}).advanced === true;
      });
      const gAdvAttr = groupAdvanced ? ' data-advanced="true"' : '';
      html += '<div class="cfg-group"' + gAdvAttr + '><div class="cfg-group-title">' + escHtml(key) + '</div>';
      html += cfgBuildForm(value, section, path);
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

function cfgRenderServices(data) {
  let html = '<div class="service-grid">';
  for (const [key, svc] of Object.entries(data)) {
    const name = SERVICE_NAMES[key] || key;
    const fieldId = 'cfg-services-' + key + '-url';
    const showVoice = (key === 'tts' && svc.voice !== undefined);
    html += '<div class="service-card">'
      + '<div class="service-card-header">'
      + '<span class="svc-health-dot" id="svc-dot-' + key + '"></span>'
      + '<span class="service-card-name">' + escHtml(name) + '</span>'
      + '</div>'
      + '<div class="cfg-field" style="margin-bottom:' + (showVoice ? '8px' : '0') + ';">'
      + '<label class="cfg-field-label">URL</label>'
      + '<input id="' + fieldId + '" data-path="' + key + '.url" data-type="string" value="' + escAttr(svc.url || '') + '">'
      + '</div>';
    if (showVoice) {
      html += '<div class="cfg-field" style="margin-bottom:0;">'
        + '<label class="cfg-field-label">Voice</label>'
        + '<input id="cfg-services-' + key + '-voice" data-path="' + key + '.voice" data-type="string" value="' + escAttr(svc.voice || '') + '">'
        + '</div>';
    }
    html += '</div>';
  }
  html += '</div>';
  // Ping services for health status
  setTimeout(() => cfgPingServices(data), 100);
  return html;
}

async function cfgPingServices(data) {
  for (const key of Object.keys(data)) {
    const dot = document.getElementById('svc-dot-' + key);
    if (!dot) continue;
    const url = (data[key].url || '').replace(/\/$/, '');
    if (!url) { dot.className = 'svc-health-dot err'; continue; }
    try {
      const r = await fetch(url + '/health', { signal: AbortSignal.timeout(3000) });
      dot.className = 'svc-health-dot ' + (r.ok ? 'ok' : 'err');
    } catch(e) {
      dot.className = 'svc-health-dot err';
    }
  }
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

  return html;
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

async function cfgSaveSection(section) {
  const data = cfgCollectForm(section);
  const resultEl = document.getElementById('cfg-save-result');
  resultEl.textContent = 'Saving...';
  resultEl.className = 'cfg-result';
  try {
    const r = await fetch('/api/config/' + section, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const resp = await r.json();
    if (r.ok) {
      resultEl.textContent = '';
      showToast('Saved! Restart services for changes to take effect.', 'success');
      _cfgData[section] = data;
    } else {
      resultEl.textContent = resp.error || ('Error (' + r.status + ')');
      resultEl.className = 'cfg-result err';
    }
  } catch(e) {
    resultEl.textContent = 'Error: ' + e.message;
    resultEl.className = 'cfg-result err';
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
      resultEl.textContent = '';
      showToast('Saved! Restart services for changes to take effect.', 'success');
      _cfgRaw[_cfgCurrentRawFile] = content;
      await cfgLoadAll();
    } else {
      const resp = await r.text();
      resultEl.textContent = resp;
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

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   Shared utilities
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

function switchTab(name) {
  // Auth gating: if not authenticated, block protected tabs
  if (!_isAuthenticated && (name === 'control' || name === 'config' || name === 'training')) {
    // Still switch to show the auth overlay
  }

  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');

  // Highlight correct nav items (sidebar + topbar)
  document.querySelectorAll('.nav-item').forEach(n => {
    if (n.getAttribute('onclick') === "switchTab('" + name + "')") {
      n.classList.add('active');
    }
  });

  try { localStorage.setItem('glados_active_tab', name); } catch(e) {}

  // Tab activation hooks
  if (name === 'control') { loadModes(); loadSpeakers(); loadHealth(); loadEyeDemo(); loadWeather(); loadGPU(); loadRobots(); loadVerbositySliders(); loadStartupSpeakers(); startGPUAutoRefresh(); startWeatherAutoRefresh(); startRobotAutoRefresh(); }
  if (name === 'config') { cfgLoadAll().then(() => cfgRenderSection(_cfgCurrentSection === 'raw' ? _cfgCurrentSection : _cfgCurrentSection)); loadAudioStats(); }
  if (name === 'training') { initTrainingTab(); }
  if (name === 'chat') {
    const ci = document.getElementById('chatInput');
    if (ci) ci.focus();
  }
  if (name === 'tts') {
    const ti = document.getElementById('textInput');
    if (ti) ti.focus();
  }
}

// Check auth on load, THEN restore saved tab
checkAuth().then(() => {
  try {
    const saved = localStorage.getItem('glados_active_tab');
    if (saved && document.getElementById('tab-' + saved)) {
      switchTab(saved);
    }
  } catch(e) {}
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
let chatStreamingAudio = null;

function renderChat() {
  const el = document.getElementById('chatMessages');
  if (chatHistory.length === 0) {
    el.innerHTML = '<div class="empty-msg">Send a message to start talking with GLaDOS.</div>';
    return;
  }
  let html = '';
  for (let i = 0; i < chatHistory.length; i++) {
    const msg = chatHistory[i];
    const isLast = (i === chatHistory.length - 1);
    if (msg.role === 'user') {
      html += '<div class="chat-msg user">' + escHtml(msg.content) + '</div>';
    } else {
      html += '<div class="chat-msg assistant">'
        + '<div class="msg-label">GLaDOS</div>'
        + escHtml(msg.content);
      if (isLast && chatStreaming) {
        html += '<span class="stream-cursor">|</span>';
      }
      if (msg.audio_url) {
        html += '<audio controls src="' + escAttr(msg.audio_url) + '"></audio>';
      }
      if (msg.timing) {
        const t = msg.timing;
        html += '<div class="chat-metrics">';
        if (t.prompt_tokens || t.completion_tokens) {
          html += '<span>' + (t.prompt_tokens||0) + '->' + (t.completion_tokens||0) + ' tok</span>';
        }
        if (t.tokens_per_second) {
          html += '<span>' + t.tokens_per_second + ' tok/s</span>';
        }
        if (t.time_to_first_token_ms != null) {
          html += '<span>TTFT ' + (t.time_to_first_token_ms/1000).toFixed(1) + 's</span>';
        }
        if (t.generation_time_ms) {
          html += '<span>LLM ' + (t.generation_time_ms/1000).toFixed(1) + 's</span>';
        }
        if (t.tts_time_ms) {
          html += '<span>TTS ' + (t.tts_time_ms/1000).toFixed(1) + 's</span>';
        }
        if (t.total_time_ms) {
          html += '<span>Total ' + (t.total_time_ms/1000).toFixed(1) + 's</span>';
        }
        if (t.emotion) {
          const pct = t.emotion_intensity != null ? ' ' + (t.emotion_intensity * 100).toFixed(0) + '%' : '';
          const p = t.pad_p != null ? (t.pad_p >= 0 ? '+' : '') + t.pad_p.toFixed(2) : '?';
          const a = t.pad_a != null ? (t.pad_a >= 0 ? '+' : '') + t.pad_a.toFixed(2) : '?';
          const d = t.pad_d != null ? (t.pad_d >= 0 ? '+' : '') + t.pad_d.toFixed(2) : '?';
          const lock = t.emotion_locked_h ? ' [locked ' + t.emotion_locked_h.toFixed(1) + 'h]' : '';
          const tip = 'Pleasure:' + p + ' Arousal:' + a + ' Dominance:' + d
            + (t.emotion_locked_h ? ' | Cooldown: ' + t.emotion_locked_h.toFixed(1) + 'h remaining' : '');
          html += '<span class="emotion-metric" title="' + tip + '">'
            + '\u26A1 ' + t.emotion + pct + lock + '</span>';
        }
        html += '</div>';
      }
      html += '</div>';
    }
  }
  if (chatWaiting) {
    html += '<div class="chat-msg assistant"><div class="msg-label">GLaDOS</div>'
      + '<span class="thinking"><span class="spinner"></span> Thinking...</span></div>';
  }
  el.innerHTML = html;
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
          if (chunk.audio_url) {
            try {
              chatStreamingAudio = new Audio(chunk.audio_url);
              chatStreamingAudio.play();
            } catch(e) {}
          }
          continue;
        }

        if (chunk.audio_replay_url !== undefined) {
          chatHistory[streamIdx].audio_url = chunk.audio_replay_url;
          renderChat();
          requestAnimationFrame(function() {
            var els = document.querySelectorAll('.chat-msg audio');
            var el = els[els.length - 1];
            if (el && chatStreamingAudio) {
              var bgAudio = chatStreamingAudio;
              chatStreamingAudio = null;
              el.addEventListener('canplay', function onReady() {
                el.removeEventListener('canplay', onReady);
                el.currentTime = bgAudio.currentTime;
                el.play().catch(function(){});
                bgAudio.pause();
              }, {once: true});
              el.load();
            } else if (el) {
              el.play().catch(function(){});
            }
          });
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
            try {
              chatStreamingAudio = new Audio(chunk.audio_url);
              chatStreamingAudio.play();
            } catch(e) {}
          }
        } else if (chunk.audio_replay_url !== undefined) {
          if (chatStreamingAudio) { chatStreamingAudio.pause(); chatStreamingAudio = null; }
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
