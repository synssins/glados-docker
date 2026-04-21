"""
GLaDOS API Wrapper
OpenAI-compatible HTTP API that wraps the GLaDOS engine.

Usage:
    python -m glados.core.api_wrapper [--port PORT] [--host HOST]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from loguru import logger
from glados.core.engine import GladosConfig, Glados
from glados.core.attitude import roll_attitude, get_tts_params, list_attitudes, load_attitudes, is_loaded as attitudes_loaded
from glados.core.command_resolver import get_resolver
from glados.core.config_store import cfg
from glados.core.source_context import SourceContext
from glados.doorbell.screener import DoorbellScreener
from glados.observability import AuditEvent, Origin, audit
from glados import ha as _ha
from glados import intent as _intent
from glados import persona as _persona
from glados.intent.rules import looks_like_home_command

# Container-aware path resolution
_GLADOS_ROOT = Path(os.environ.get("GLADOS_ROOT", "/app"))
_GLADOS_CONFIG_DIR = Path(os.environ.get("GLADOS_CONFIG_DIR", str(_GLADOS_ROOT / "configs")))
_GLADOS_DATA = Path(os.environ.get("GLADOS_DATA", str(_GLADOS_ROOT / "data")))
_engine: Glados | None = None
_api_lock = threading.Lock()
_response_timeout: float = 45.0

# Matches <think>...</think>, <thinking>...</thinking>, <reasoning>...</reasoning>
# including cross-line content. Qwen3 (and GLM-4.7, DeepSeek, MiniMax) emit
# these blocks when "thinking mode" is active and they must not reach the UI.
# llm_processor does stream-aware extraction for the TTS path; this is the
# non-streaming / fallback path for text responses.
_THINKING_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_STRAY_THINK_TAG_RE = re.compile(
    r"</?(?:think|thinking|reasoning)\b[^>]*>",
    re.IGNORECASE,
)


def _strip_thinking(text: str) -> str:
    """Strip closed thinking blocks + any stray opening/closing tags.

    Conservative: preserves all other text exactly. If the model emitted an
    unclosed <think> (truncated generation), the stray-tag pass keeps the
    visible remainder rather than hiding the entire response.
    """
    if not text:
        return text
    text = _THINKING_BLOCK_RE.sub("", text)
    text = _STRAY_THINK_TAG_RE.sub("", text)
    return text.strip()

# Hot-reload plumbing. `_engine_overrides` captures CLI flags so a reload
# preserves operator runtime choices (input mode, audio backend, flags).
# `_engine_config_path` is the path we were initially started with.
_engine_overrides: dict[str, Any] = {}
_engine_config_path: str | None = None
_engine_reload_lock = threading.RLock()


def reload_engine() -> bool:
    """Swap the engine using the current on-disk config.

    The sequence has to be "stop-old-first, then-start-new" because the
    engine's HomeAssistantAudioIO binds a TCP port (default 5051) and two
    instances in the same process can't co-hold it (EADDRINUSE). A brief
    503-serving window during the swap is acceptable for a config save:
    both `/v1/chat/completions` and `/health` already handle `_engine is
    None` gracefully ("starting" / "initializing").

    Order:
      1. Clear the module-level `_engine` so new requests 503 immediately
         rather than racing on the half-alive old engine.
      2. Signal the old engine's shutdown_event and close its audio_io
         file server (releases the TCP port).
      3. Build a fresh Glados from the YAML. This takes a few seconds
         (HA WS reconnect, MCP handshake, cache warmup).
      4. Promote it to `_engine`. Next request picks it up.
    """
    global _engine
    if _engine_config_path is None:
        logger.warning("reload_engine called before startup registered a config path; skipping")
        return False
    with _engine_reload_lock:
        previous = _engine
        _engine = None
        if previous is not None:
            logger.info("Engine reload: retiring previous instance")
            try:
                previous.shutdown_event.set()
            except Exception as exc:
                logger.debug("Engine reload: shutdown_event.set raised: {}", exc)
            audio_io = getattr(previous, "audio_io", None)
            close = getattr(audio_io, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("Engine reload: audio_io.close raised: {}", exc)
            # Short settle window for daemon threads to finish their
            # current iteration and the socket to be released by the
            # kernel. Empirically 1 s is plenty.
            time.sleep(1.0)
        logger.info("Engine reload: building new instance from {}", _engine_config_path)
        new_engine = _create_engine(_engine_config_path, _engine_overrides)
        _engine = new_engine
        logger.info("Engine reload: new instance live")
    return True

# ---------------------------------------------------------------------------
# Doorbell screening system
# ---------------------------------------------------------------------------
_doorbell_screener: DoorbellScreener | None = None

# ---------------------------------------------------------------------------
# Announcement system — path from centralized config
# ---------------------------------------------------------------------------
ANNOUNCEMENTS_YAML = cfg._configs_dir / "announcements.yaml"
_announce_config: dict | None = None
_announce_config_mtime: float = 0.0
_announce_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Legacy ESPHome-direct `/command` endpoint + `commands.yaml` removed
# in Stage 3 Phase 7. Voice now routes through the same
# `POST /v1/chat/completions` endpoint as the WebUI; HA forwards voice
# utterances with area context attached, and the CommandResolver owns
# resolution end-to-end. See CURRENT_STATE.md §"One door".
# ---------------------------------------------------------------------------

# HA entity discovery cache: {"office": {"name": "Office", "lights": [...]}, ...}
_ha_areas: dict[str, dict] = {}
_ha_areas_lock = threading.Lock()
_ha_areas_last_refresh: float = 0.0

# ---------------------------------------------------------------------------
# Dynamic mode cache (maintenance / silent) — entity IDs from centralized config
# ---------------------------------------------------------------------------
_mode_cache: dict[str, Any] = {}          # {entity_id: state_str}
_mode_cache_ts: float = 0.0               # last fetch timestamp
_MODE_CACHE_TTL: float = cfg.tuning.mode_cache_ttl_s
_mode_cache_lock = threading.Lock()

_MODE_ENTITIES = (
    cfg.mode_entities.maintenance_mode,
    cfg.mode_entities.maintenance_speaker,
    cfg.mode_entities.silent_mode,
    cfg.mode_entities.dnd,
)

# ---------------------------------------------------------------------------
# Alert tier hierarchy (higher index = higher severity)
# ---------------------------------------------------------------------------
_TIER_ORDER = ("AMBIENT", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def _tier_rank(tier: str) -> int:
    """Return numeric rank for a tier name (higher = more severe)."""
    try:
        return _TIER_ORDER.index(tier.upper())
    except ValueError:
        return 0


def _is_silent_now() -> bool:
    """Check if current time falls within the configured silent hours window."""
    sh = cfg.silent_hours
    if not sh.enabled:
        return False
    from datetime import datetime
    now = datetime.now()
    start_h, start_m = (int(x) for x in sh.start.split(":"))
    end_h, end_m = (int(x) for x in sh.end.split(":"))
    now_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes <= end_minutes:
        # Same-day window (e.g., 08:00-17:00)
        return start_minutes <= now_minutes < end_minutes
    else:
        # Overnight window (e.g., 22:00-07:00)
        return now_minutes >= start_minutes or now_minutes < end_minutes


def _get_mode_state(ha_url: str, ha_token: str) -> dict[str, str]:
    """Return current mode entity states, cached for up to 5 seconds."""
    global _mode_cache, _mode_cache_ts
    now = time.time()
    if now - _mode_cache_ts < _MODE_CACHE_TTL and _mode_cache:
        return _mode_cache

    with _mode_cache_lock:
        # Double-check after acquiring lock
        if now - _mode_cache_ts < _MODE_CACHE_TTL and _mode_cache:
            return _mode_cache

        result: dict[str, str] = {}
        for entity_id in _MODE_ENTITIES:
            try:
                url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
                req = Request(url, headers={
                    "Authorization": f"Bearer {ha_token}",
                    "Content-Type": "application/json",
                })
                with urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    result[entity_id] = data.get("state", "")
            except Exception as exc:
                logger.debug("Mode cache: failed to read {}: {}", entity_id, exc)
                result[entity_id] = _mode_cache.get(entity_id, "")

        _mode_cache = result
        _mode_cache_ts = time.time()
        return result


def _send_ha_notification(ha_url: str, ha_token: str, title: str, message: str) -> None:
    """Create a persistent HA notification (used in silent mode)."""
    url = f"{ha_url.rstrip('/')}/api/services/persistent_notification/create"
    payload = {"title": title, "message": message}
    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            logger.debug("HA notification sent: {} ({})", title, resp.status)
    except Exception as exc:
        logger.warning("HA notification failed: {}", exc)


def _cfg_settings() -> dict:
    """Build a settings dict from the centralized config store.

    This replaces the ``settings:`` block that used to live inside
    announcements.yaml and commands.yaml.  Every downstream function
    that accesses ``config["settings"]`` gets its values from here.
    """
    return {
        "ha_url": cfg.ha_url,
        "ha_token": cfg.ha_token,
        "serve_host": cfg.serve_host,
        "serve_port": cfg.serve_port,
        "tts_url": cfg.service_url("tts") + "/v1/audio/speech",
        "tts_voice": cfg.services.tts.voice or "glados",
        "audio_dir": cfg.audio.announcements_dir,
        "silence_between_sentences_ms": cfg.audio.silence_between_sentences_ms,
        "sample_rate": cfg.audio.sample_rate,
        "speakers": cfg.speakers.available,
        "announce_url": cfg.service_url("api_wrapper") + "/announce",
        "entity_refresh_interval_s": 1800,
    }


def _load_announce_config() -> dict:
    """Load announcements.yaml, caching until the file changes on disk.

    The ``settings`` block is always injected from the centralized
    config store, regardless of what the YAML file contains.
    """
    global _announce_config, _announce_config_mtime
    try:
        mtime = ANNOUNCEMENTS_YAML.stat().st_mtime
    except OSError:
        raise FileNotFoundError(f"Announcements config not found: {ANNOUNCEMENTS_YAML}")
    if _announce_config is None or mtime != _announce_config_mtime:
        import yaml
        with open(ANNOUNCEMENTS_YAML, "r", encoding="utf-8") as f:
            _announce_config = yaml.safe_load(f)
        _announce_config_mtime = mtime
        logger.info("Loaded announcements config ({})", ANNOUNCEMENTS_YAML)
    # Always inject centralized settings (overrides any YAML settings block)
    _announce_config["settings"] = _cfg_settings()
    _announce_config["settings"]["audio_dir"] = cfg.audio.announcements_dir
    return _announce_config


# ---------------------------------------------------------------------------
# Command system: HA entity discovery
# ---------------------------------------------------------------------------


def _discover_ha_entities(ha_url: str | None = None, ha_token: str | None = None) -> dict[str, dict]:
    """Query HA for all areas and their light entities.

    Returns a dict: {area_id: {"name": "Area Name", "lights": [entity_ids]}}
    """
    # Default from centralized config store
    if ha_url is None:
        ha_url = cfg.ha_url
    if ha_token is None:
        ha_token = cfg.ha_token

    ha_url = ha_url.rstrip("/")

    # Use HA template API to get areas and their light entities
    template = """{% for area in areas() -%}
{{ area }}|{{ area_name(area) }}|{{ area_entities(area) | select('match', 'light\\.') | list | join(',') }}
{% endfor %}"""

    url = f"{ha_url}/api/template"
    payload = json.dumps({"template": template}).encode()
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    req = Request(url, data=payload, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except (HTTPError, URLError, OSError) as exc:
        logger.error("HA entity discovery failed: {}", exc)
        return {}

    areas = {}
    for line in body.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        area_id, area_name, lights_str = parts
        lights = [e.strip() for e in lights_str.split(",") if e.strip()]
        areas[area_id] = {
            "name": area_name,
            "lights": lights,
        }

    return areas


def _refresh_ha_entities() -> None:
    """Refresh the HA entity cache if stale."""
    global _ha_areas, _ha_areas_last_refresh
    try:
        cfg = _load_cmd_config()
    except Exception:
        return
    interval = cfg["settings"].get("entity_refresh_interval_s", 1800)
    now = time.time()
    if now - _ha_areas_last_refresh < interval:
        return

    logger.info("Refreshing HA entity cache...")
    new_areas = _discover_ha_entities()
    if new_areas:
        with _ha_areas_lock:
            _ha_areas.clear()
            _ha_areas.update(new_areas)
            _ha_areas_last_refresh = now
        light_counts = {k: len(v["lights"]) for k, v in new_areas.items() if v["lights"]}
        logger.info("HA entity discovery: {} areas, lights: {}", len(new_areas), light_counts)
    else:
        logger.warning("HA entity discovery returned empty results, keeping old cache")


def _ha_entity_refresh_loop() -> None:
    """Background thread: periodically refresh HA entity cache."""
    while True:
        try:
            _refresh_ha_entities()
        except Exception as exc:
            logger.error("HA entity refresh error: {}", exc)
        time.sleep(60)  # Check every minute (actual refresh gated by interval)


# ---------------------------------------------------------------------------
# Announcement / doorbell helpers (the command-system handlers that used
# to live here were removed in Stage 3 Phase 7 — see the banner above)
# ---------------------------------------------------------------------------


def _find_base_wav(config: dict, scenario_name: str, entity_id: str | None, state: str | None, entity_name: str | None) -> Path | None:
    """Find the base WAV file for a scenario + entity + state.

    Supports multiple base WAV variants per state. When a state has multiple
    texts in announcements.yaml, numbered files are generated (e.g.
    ``lock__front_door_lock__locked__01.wav``). This function looks for
    numbered variants first and picks one at random. Falls back to the
    single un-numbered file for backwards compatibility.
    """
    audio_dir = Path(config["settings"]["audio_dir"])
    bases_dir = audio_dir / "bases"
    scenario = config.get("scenarios", {}).get(scenario_name)
    if not scenario:
        return None

    for entity in scenario.get("entities", []):
        # Match by entity_id or by name
        eid = entity.get("entity_id")
        ename = entity.get("name", "")
        if entity_id and eid and eid != entity_id:
            continue
        if entity_name and ename.lower() != entity_name.lower():
            continue
        if not entity_id and not entity_name:
            continue

        name_slug = _sanitize(ename)

        # Entity with states dict
        if "states" in entity and state is not None:
            state_slug = _sanitize(str(state))
            # Try numbered variants first (e.g. lock__front_door_lock__locked__01.wav)
            pattern = f"{scenario_name}__{name_slug}__{state_slug}__*.wav"
            variants = sorted(bases_dir.glob(pattern))
            if variants:
                return random.choice(variants)
            # Fall back to single un-numbered file
            filename = f"{scenario_name}__{name_slug}__{state_slug}.wav"
            path = bases_dir / filename
            if path.exists():
                return path

        # Entity with base_text
        if "base_text" in entity:
            filename = f"{scenario_name}__{name_slug}__base.wav"
            path = bases_dir / filename
            if path.exists():
                return path

    return None


def _pick_followup_wavs(config: dict, scenario_name: str, state: str | None = None) -> list[Path]:
    """Randomly pick follow-up WAV files for a scenario.

    Supports:
    - followup_probability (0.0-1.0): chance of including ANY followups (default 1.0)
    - state_followups: dict mapping state -> list of followup texts (for state-specific pools)
    - followups: flat list of followup texts (fallback when state_followups not present)
    """
    audio_dir = Path(config["settings"]["audio_dir"])
    followups_dir = audio_dir / "followups"
    scenario = config.get("scenarios", {}).get(scenario_name)
    if not scenario:
        return []

    # Probability gate: roll dice to decide if any followups play at all
    probability = scenario.get("followup_probability", 1.0)
    if random.random() >= probability:
        logger.debug("Announce: followup skipped by probability ({:.0%}) for {}", probability, scenario_name)
        return []

    # Determine which followup pool and filename pattern to use
    state_followups = scenario.get("state_followups", {})
    if state and state in state_followups:
        followups = state_followups[state]
        filename_pattern = f"{scenario_name}__{state}__followup_{{i:02d}}.wav"
    else:
        followups = scenario.get("followups", [])
        filename_pattern = f"{scenario_name}__followup_{{i:02d}}.wav"

    if not followups:
        return []

    count_range = scenario.get("followup_count", [1, 1])
    min_count = count_range[0]
    max_count = count_range[1] if len(count_range) > 1 else min_count
    pick_count = random.randint(min_count, min(max_count, len(followups)))

    # Collect available followup WAV files
    available: list[tuple[int, Path]] = []
    for i in range(1, len(followups) + 1):
        filename = filename_pattern.format(i=i)
        path = followups_dir / filename
        if path.exists():
            available.append((i, path))

    if not available:
        return []

    picked = random.sample(available, min(pick_count, len(available)))
    return [p for _, p in picked]


def _sanitize(text: str, max_len: int = 80) -> str:
    """Convert text to a safe filename slug (mirrors generate_announcements.py)."""
    import re
    import hashlib
    slug = text.lower().strip().rstrip(".")
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    if len(slug) > max_len:
        h = hashlib.md5(text.encode()).hexdigest()[:6]
        slug = slug[:max_len - 7] + "_" + h
    return slug


def _concatenate_wavs(wav_paths: list[Path], silence_ms: int = 400, sample_rate: int = 24000) -> bytes:
    """Read and concatenate WAV files with silence gaps. Returns WAV bytes."""
    silence_samples = int(sample_rate * silence_ms / 1000)
    silence_bytes = b"\x00\x00" * silence_samples  # 16-bit silence

    all_frames = bytearray()
    detected_rate = sample_rate
    detected_channels = 1
    detected_sampwidth = 2

    for i, wav_path in enumerate(wav_paths):
        with wave.open(str(wav_path), "rb") as wf:
            detected_rate = wf.getframerate()
            detected_channels = wf.getnchannels()
            detected_sampwidth = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
            all_frames.extend(frames)
        # Add silence between segments (not after the last one)
        if i < len(wav_paths) - 1:
            # Match silence to detected parameters
            silence_bytes_seg = b"\x00" * (detected_sampwidth * detected_channels * int(detected_rate * silence_ms / 1000))
            all_frames.extend(silence_bytes_seg)

    # Write combined WAV
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(detected_channels)
        wf.setsampwidth(detected_sampwidth)
        wf.setframerate(detected_rate)
        wf.writeframes(bytes(all_frames))
    return buf.getvalue()


def _ha_play_media(
    config: dict,
    media_url: str,
    speakers: list[str] | None = None,
    tier: str = "MEDIUM",
    scenario: str = "",
) -> None:
    """Call HA media_player.play_media to play audio on speakers.

    Respects dynamic maintenance/silent/DND mode via HA helper entities:
      - Silent mode → suppress audio, send HA persistent notification instead
      - DND or silent hours + tier below threshold → suppress + notify
      - Maintenance mode → override speakers with maintenance speaker
    """
    settings = config["settings"]
    ha_url = settings["ha_url"].rstrip("/")
    ha_token = settings["ha_token"]
    target_speakers = speakers if speakers is not None else settings.get("speakers", [])

    # ── Dynamic mode check (backed by HA helper entities) ────────
    mode = _get_mode_state(ha_url, ha_token)
    _silent = mode.get("input_boolean.glados_silent_mode", "off") == "on"
    _maint = mode.get("input_boolean.glados_maintenance_mode", "off") == "on"
    _maint_speaker = mode.get("input_text.glados_maintenance_speaker", "")
    _dnd = mode.get(cfg.mode_entities.dnd, "off") == "on"

    if _silent:
        logger.info("Silent mode: suppressing audio play for {}", media_url)
        _send_ha_notification(ha_url, ha_token, "GLaDOS (silent)", f"Audio suppressed: {media_url}")
        return

    # ── Silent hours / DND check ─────────────────────────────────
    is_quiet = _dnd or _is_silent_now()
    if is_quiet:
        min_tier = cfg.silent_hours.min_tier
        if _tier_rank(tier) < _tier_rank(min_tier):
            from datetime import datetime
            ts = datetime.now().strftime("%I:%M %p")
            reason = "DND" if _dnd else "silent hours"
            logger.info(
                "Silent hours: suppressing {} alert '{}' (tier {} < min {})",
                reason, scenario, tier, min_tier,
            )
            _send_ha_notification(
                ha_url, ha_token,
                f"GLaDOS ({reason})",
                f"Suppressed {scenario} alert at {ts} (tier {tier}, threshold {min_tier})",
            )
            return

    if _maint and _maint_speaker:
        target_speakers = [_maint_speaker]

    if not target_speakers:
        logger.warning("Announce: no speakers configured")
        return

    url = f"{ha_url}/api/services/media_player/play_media"
    payload = {
        "entity_id": target_speakers,
        "media_content_id": media_url,
        "media_content_type": "music",
    }
    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    req = Request(url, data=data, headers=headers, method="POST")

    for attempt in range(2):
        try:
            with urlopen(req, timeout=10) as resp:
                logger.debug("Announce: HA play_media -> {}", resp.status)
                return
        except (HTTPError, URLError, OSError) as exc:
            if attempt == 0:
                logger.warning("Announce: HA request failed ({}), retrying...", exc)
                time.sleep(0.5)
            else:
                logger.error("Announce: HA request failed after retry: {}", exc)


def _cleanup_old_announcements(serve_dir: Path, max_age_s: int = 120) -> None:
    """Remove old combined announcement WAVs."""
    cutoff = time.time() - max_age_s
    for f in serve_dir.glob("announce_*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def handle_announce(request_data: dict) -> dict:
    """Process an /announce request.

    Expected JSON body:
        {
            "scenario": "garage",
            "entity_id": "cover.vehicle_door",   // optional if entity_name given
            "entity_name": "garage door",         // optional if entity_id given
            "state": "open",                      // required for entities with states
            "speakers": ["media_player.xxx"]      // optional override
        }

    Returns a JSON-serializable response dict.
    """
    try:
        config = _load_announce_config()
    except FileNotFoundError as e:
        return {"error": str(e)}, 500

    scenario_name = request_data.get("scenario")
    entity_id = request_data.get("entity_id")
    entity_name = request_data.get("entity_name")
    state = request_data.get("state")
    speakers = request_data.get("speakers")

    if not scenario_name:
        return {"error": "Missing 'scenario' field"}, 400

    # ── Read scenario config ───────────────────────────────────────
    # TODO: Expose per-scenario enabled toggles as checkboxes in the WebUI
    #       Configuration tab. Use generic labels (e.g. "Entry alerts",
    #       "Arrival notifications") so the UI is portable across homes.
    scenario_cfg = config.get("scenarios", {}).get(scenario_name, {})
    if not scenario_cfg.get("enabled", True):
        logger.info("Announce: scenario '{}' is disabled, skipping", scenario_name)
        return {"status": "skipped", "scenario": scenario_name, "reason": "disabled"}
    tier = scenario_cfg.get("tier", "MEDIUM").upper()
    use_chime = scenario_cfg.get("chime", False)

    settings = config["settings"]
    serve_host = settings["serve_host"]
    serve_port = settings["serve_port"]
    silence_ms = settings.get("silence_between_sentences_ms", 400)
    sample_rate = settings.get("sample_rate", 24000)

    # The serve directory is the same one used by HomeAssistantAudioIO
    serve_dir = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files")) / "glados_ha"
    serve_dir.mkdir(parents=True, exist_ok=True)

    # Find base WAV
    base_wav = _find_base_wav(config, scenario_name, entity_id, state, entity_name)
    if not base_wav:
        return {"error": f"No base WAV found for scenario={scenario_name}, entity_id={entity_id}, entity_name={entity_name}, state={state}"}, 404

    # Pick follow-up WAVs (pass state for state-specific followup pools)
    followup_wavs = _pick_followup_wavs(config, scenario_name, state)

    # Concatenate: optional chime + base + followups
    wav_paths: list[Path] = []
    if use_chime:
        chime_path = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files")) / "chimes" / "chime.wav"
        if chime_path.exists():
            wav_paths.append(chime_path)
        else:
            logger.warning("Announce: chime enabled but file not found: {}", chime_path)
    wav_paths.append(base_wav)
    wav_paths.extend(followup_wavs)

    logger.info(
        "Announce: {} [tier={}] -> chime={}, base={}, followups={}",
        scenario_name,
        tier,
        use_chime and len(wav_paths) > len(followup_wavs) + 1,
        base_wav.name,
        [f.name for f in followup_wavs],
    )

    combined_bytes = _concatenate_wavs(wav_paths, silence_ms, sample_rate)

    # Write combined WAV to serve directory
    _cleanup_old_announcements(serve_dir)
    filename = f"announce_{uuid.uuid4().hex}.wav"
    wav_path = serve_dir / filename
    wav_path.write_bytes(combined_bytes)

    # Build URL and play
    media_url = f"http://{serve_host}:{serve_port}/{filename}"
    _ha_play_media(config, media_url, speakers, tier=tier, scenario=scenario_name)

    # Notify HUB75 display — compute exact WAV duration from byte length.
    # TODO: Pass entity_name/scenario in meta for contextual gaze targeting.
    #       See docs/TODO-contextual-gaze.md
    wav_duration_s = _wav_duration_from_bytes(combined_bytes, sample_rate)
    if _engine is not None:
        _emit_ha_audio_event(
            _engine,
            text=f"[announce:{scenario_name}]",
            source="announce",
            wav_duration_s=wav_duration_s,
        )

    logger.success("Announce: played {} on speakers ({}, {:.1f}s)", media_url, scenario_name, wav_duration_s)

    return {
        "status": "ok",
        "scenario": scenario_name,
        "tier": tier,
        "chime": use_chime,
        "base": base_wav.name,
        "followups": [f.name for f in followup_wavs],
        "media_url": media_url,
    }, 200


# ---------------------------------------------------------------------------
# HUB75 display notification helpers
# ---------------------------------------------------------------------------


def _wav_duration_from_bytes(wav_bytes: bytes, fallback_rate: int = 24000) -> float:
    """Parse WAV header to compute duration in seconds."""
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / rate if rate else 0.0
    except Exception:
        # Rough fallback: assume 16-bit mono
        return (len(wav_bytes) - 44) / (2 * fallback_rate)


# Average speaking rate for duration estimation.
# GLaDOS speaks ~2.5 words/sec (slow, robotic cadence).
_WORDS_PER_SECOND = 2.5

# Extra buffer for HA speaker latency (WAV download + decode + WiFi).
_HA_PLAY_BUFFER_S = 2.0

# TTS synthesis latency estimate (caller synthesises after receiving text).
_TTS_SYNTH_LATENCY_S = 1.0


def _emit_ha_audio_event(
    glados: Glados,
    text: str,
    source: str = "api",
    wav_duration_s: float | None = None,
) -> None:
    """Emit an ``ha_audio.play`` event so the HUB75 display keeps the panel lit.

    When TTS is muted (which it always is during API calls), the speech
    player's ``tts.play`` events are useless — they fire instantly with
    ``audio_samples=0``.  This helper emits a separate event that the
    HUB75 display can use to estimate how long the HA speakers will be
    playing.

    Parameters
    ----------
    glados : Glados
        Engine instance (for observability bus access).
    text : str
        Response text — used to estimate audio duration if ``wav_duration_s``
        is not provided.
    source : str
        Freeform tag identifying the caller (``"api_chat"``, ``"announce"``…).
    wav_duration_s : float | None
        If the caller already knows the exact WAV duration (e.g. pre-recorded
        announcements), pass it here.  Otherwise duration is estimated from
        word count.
    """
    if wav_duration_s is not None:
        estimated_s = wav_duration_s + _HA_PLAY_BUFFER_S
    else:
        words = len(text.split())
        speech_s = words / _WORDS_PER_SECOND
        estimated_s = speech_s + _TTS_SYNTH_LATENCY_S + _HA_PLAY_BUFFER_S

    try:
        glados.observability_bus.emit(
            source="ha_audio",
            kind="play",
            message=text[:80] if text else source,
            meta={
                "estimated_duration_s": round(estimated_s, 1),
                "source": source,
            },
        )
        logger.debug(
            "ha_audio.play emitted: source={}, est={:.1f}s, text='{}'",
            source, estimated_s, text[:60] if text else "",
        )
    except Exception as exc:
        logger.warning("Failed to emit ha_audio.play: {}", exc)


# ---------------------------------------------------------------------------
# Engine response detection
# ---------------------------------------------------------------------------

def _get_engine_response(
    glados: Glados,
    text: str,
    timeout: float,
    engine_audio: bool = False,
) -> tuple[str | None, str]:
    """Submit text to the GLaDOS engine and wait for the assistant response.

    Returns (response_text, request_id). response_text is None on timeout.
    Uses ConversationStore version polling to detect when the response is complete.

    Args:
        engine_audio: When True, do NOT mute TTS — let the engine's streaming
            pipeline handle audio playback via HomeAssistantAudioIO.  This gives
            ~2-3s to first audio instead of waiting for the full response.
    """
    store = glados._conversation_store
    request_id = uuid.uuid4().hex[:8]
    start_time = time.monotonic()

    # Snapshot state before submitting
    version_before = store.version
    msg_count_before = len(store)

    # Mute TTS to prevent duplicate audio (HA handles its own TTS) —
    # UNLESS engine_audio is True, in which case we let the engine stream
    # audio directly to HA speakers as sentences are generated.
    mute_tts = not engine_audio
    was_muted = glados.tts_muted_event.is_set()
    if mute_tts and not was_muted:
        glados.tts_muted_event.set()

    try:
        if not glados.submit_text_input(text, source="api"):
            logger.warning(f"[{request_id}] submit_text_input returned False (empty text?)")
            return None, request_id

        mode_label = "engine_audio" if engine_audio else "muted"
        logger.info(f"[{request_id}] Submitted ({mode_label}): {text[:100]}")

        # Poll for response
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if store.version > version_before:
                messages = store.snapshot()
                # Scan backwards from end, bounded to messages added after our submission
                search_start = max(msg_count_before - 2, 0)
                for i in range(len(messages) - 1, search_start - 1, -1):
                    msg = messages[i]
                    if msg.get("role") == "user" and msg.get("content", "").strip() == text.strip():
                        # Found our user message — look forward for assistant reply with actual text content
                        # Skip tool_call-only assistant messages (role=assistant but no content, has tool_calls)
                        for j in range(i + 1, len(messages)):
                            if messages[j].get("role") == "assistant" and messages[j].get("content"):
                                elapsed = time.monotonic() - start_time
                                response_text = messages[j]["content"]
                                logger.info(
                                    f"[{request_id}] Response in {elapsed:.1f}s "
                                    f"({len(response_text)} chars)"
                                )
                                if not engine_audio:
                                    # Notify HUB75 display that HA audio is about to play.
                                    # TTS is muted during API calls (HA handles audio externally),
                                    # so the speech player's tts.play events are useless (muted,
                                    # audio_samples=0).  Emit an ha_audio event so the panel knows
                                    # to stay lit for the estimated duration of the HA playback.
                                    _emit_ha_audio_event(
                                        glados, response_text, source="api_chat",
                                    )
                                # else: engine's streaming TTS already played audio and
                                # emitted real tts.play events for the HUB75 display.
                                return response_text, request_id
                        break  # Found our message but no text reply yet
                version_before = store.version
            time.sleep(0.1)

        logger.warning(f"[{request_id}] Timeout after {timeout:.0f}s")
        return None, request_id

    finally:
        # Restore TTS mute state
        if mute_tts and not was_muted:
            glados.tts_muted_event.clear()


def _get_engine_response_with_retry(
    glados: Glados,
    text: str,
    timeout: float,
    engine_audio: bool = False,
) -> tuple[str | None, str]:
    """Try to get a response, retry once on timeout (handles compaction race).

    Priority-gated — holds the chat-in-flight flag for the whole
    engine round-trip so the autonomy loop yields this tick.
    """
    from glados.observability import chat_in_flight
    with chat_in_flight():
        return _get_engine_response_with_retry_impl(
            glados, text, timeout, engine_audio=engine_audio,
        )


def _get_engine_response_with_retry_impl(
    glados: Glados,
    text: str,
    timeout: float,
    engine_audio: bool = False,
) -> tuple[str | None, str]:
    """Try to get a response, retry once on timeout (handles compaction race)."""
    response_text, request_id = _get_engine_response(
        glados, text, timeout, engine_audio=engine_audio,
    )
    if response_text is not None:
        return response_text, request_id

    # Retry once — compaction may have replaced history during first attempt
    logger.info(f"[{request_id}] Retrying after timeout (possible compaction race)")

    # On retry, just scan the latest messages for any assistant reply
    store = glados._conversation_store
    messages = store.snapshot()
    if messages:
        for msg in reversed(messages[-5:]):
            if msg.get("role") == "assistant":
                logger.info(f"[{request_id}] Found response on retry scan")
                return msg.get("content", ""), request_id

    logger.warning(f"[{request_id}] No response after retry")
    return None, request_id


# ---------------------------------------------------------------------------
# Stage 3 Phase 1 — Tier 1 fast path via HA conversation API
# ---------------------------------------------------------------------------

def _persona_rewrite(plain: str, utterance: str = "") -> str:
    """Run plain Tier 1 / Tier 2 speech through the persona rewriter.

    Best-effort: any failure (rewriter not initialized, LLM down, bad
    output) returns the original `plain` text. The user always gets a
    real reply; persona is a polish layer."""
    rw = _persona.get_rewriter()
    if rw is None or not plain:
        return plain
    try:
        result = rw.rewrite(plain, context_hint=utterance[:200])
    except Exception as exc:
        logger.debug("Persona rewriter raised: {} (returning original)", exc)
        return plain
    return result.text or plain


# Carry-over state, the `_RecentTierAction` stash, and the
# `_should_carry_over_home_command` / `_last_ha_conversation_id`
# helpers have moved to `glados.core.session_memory.SessionMemory`
# and are driven by the `CommandResolver`. See CURRENT_STATE.md.


def _append_tier_exchange(
    user_message: str,
    assistant_speech: str,
    *,
    origin: str,
    tier: int,
    ha_conversation_id: "str | None" = None,
) -> None:
    """Persist a Tier 1/2 (user, assistant) exchange into the engine's
    ConversationStore so subsequent Tier 3 calls have multi-turn context.

    Without this, "Turn off the whole house" → "All lights" would lose
    the verb-context from the prior turn (the failure case observed in
    Stage 3 Phase 1 testing). Best-effort: any persistence error is
    logged and swallowed so the user-facing response still returns."""
    global _engine
    if _engine is None or not assistant_speech:
        return
    try:
        _engine._conversation_store.append_multiple(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_speech},
            ],
            source=origin,
            tier=tier,
            ha_conversation_id=ha_conversation_id,
        )
    except Exception as exc:
        logger.debug("Tier {} conversation persist failed: {}", tier, exc)


def _emit_tier1_sse_response(
    handler: "APIHandler", request_id: str, text: str,
) -> None:
    """Send `text` to the client as an OpenAI-compatible streaming SSE
    response (one content chunk + finish + [DONE]). Shape matches the
    explicit-memory short-circuit elsewhere in this file so the WebUI
    frontend renders it identically to a real LLM response."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    chunk = json.dumps({
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "glados",
        "choices": [{"index": 0, "delta": {"content": text},
                     "finish_reason": None}],
    })
    handler.wfile.write(f"data: {chunk}\n\n".encode())
    done = json.dumps({
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "glados",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    handler.wfile.write(f"data: {done}\n\n".encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _build_source_context(
    handler: "APIHandler", origin: str,
) -> SourceContext:
    """Build a SourceContext from incoming HTTP headers.

    Reads `X-GLaDOS-Session-Id`, `X-GLaDOS-Area-Id`,
    `X-GLaDOS-Principal`, and `X-GLaDOS-Satellite-Device-Id`. Origin
    is passed in pre-validated by the caller. Falls back to a
    UUID-derived session id when the client doesn't supply one —
    first-turn semantics for that session, nothing carries over
    from prior requests.
    """
    headers: dict[str, str] = {"X-GLaDOS-Origin": origin}
    for h in (
        "X-GLaDOS-Session-Id",
        "X-GLaDOS-Area-Id",
        "X-GLaDOS-Principal",
        "X-GLaDOS-Satellite-Device-Id",
    ):
        v = handler.headers.get(h)
        if v is not None:
            headers[h] = v
    return SourceContext.from_headers(headers, default_origin=origin)


def _resolve_home_intent(
    handler: "APIHandler",
    user_message: str,
    origin: str,
    *,
    emit: str,
) -> bool:
    """Shared body for both streaming and non-streaming Tier 1/2
    resolution. `emit` is either "sse" or "json" — the only
    difference between the two callers is how the spoken response
    leaves the socket.

    Returns True when the resolver handled the request and a
    response was written. False means Tier 3 should take over.
    """
    resolver = get_resolver()
    if resolver is None:
        # Resolver wasn't wired (HA WS init skipped because HA_TOKEN
        # was unset, or startup raised). Fall through to Tier 3.
        return False

    ctx = _build_source_context(handler, origin)
    try:
        result = resolver.resolve(user_message, ctx)
    except Exception as exc:
        logger.warning("CommandResolver raised: {}", exc)
        return False

    if not result.handled:
        return False

    speech = result.spoken_response or "Done."
    request_id = uuid.uuid4().hex[:12]

    try:
        if emit == "sse":
            _emit_tier1_sse_response(handler, request_id, speech)
        else:
            handler._send_json({
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "glados",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": speech},
                    "finish_reason": "stop",
                }],
            })
    except Exception as exc:
        logger.warning("Resolver response write failed ({}): {}", emit, exc)
        return False

    # Persist the exchange into the engine's ConversationStore so
    # subsequent Tier 3 calls see the full multi-turn history.
    _append_tier_exchange(
        user_message, speech, origin=origin,
        tier=int(result.tier or 2),
        ha_conversation_id=result.ha_conversation_id,
    )
    return True


def _try_tier1_nonstreaming(
    handler: "APIHandler", user_message: str, origin: str,
) -> bool:
    """Priority-gated wrapper. The priority gate tells the autonomy
    loop to yield this tick (see glados.observability.priority)."""
    from glados.observability import chat_in_flight
    with chat_in_flight():
        return _resolve_home_intent(
            handler, user_message, origin, emit="json",
        )


def _try_tier1_fast_path(
    handler: "APIHandler", user_message: str, origin: str,
) -> bool:
    """Priority-gated wrapper for the SSE path."""
    from glados.observability import chat_in_flight
    with chat_in_flight():
        return _resolve_home_intent(
            handler, user_message, origin, emit="sse",
        )


# ---------------------------------------------------------------------------
# History sanitation — self-healing conversation pollution
# ---------------------------------------------------------------------------
#
# Conversation history lives in SQLite and rides along with every chat
# request. Occasional rows slip through in shapes Ollama rejects — most
# famously tool_calls with `arguments` as an empty string instead of {}.
# A single bad row blocks EVERY future chat until the operator clears
# the DB, which is a terrible UX for what's really a data-quality bug.
#
# This runs on every snapshot() before the request goes out. It repairs
# known-bad shapes in-place (on the returned list, not the DB) and logs
# a one-liner when a repair fires so chronic offenders are visible.

# Message content prefixes generated by the engine's autonomy loop that
# we never want to feed into a user-visible chat turn. The autonomy
# loop writes to the same conversation store the chat path reads from,
# so without this filter chitchat sees dozens of "Autonomy update. Time:
# 2026-04-18T22:29:08 ..." messages and starts mimicking their timestamp
# framing (hence the "The chronometer reports 12:47 PM" prefixes).
_AUTONOMY_NOISE_PREFIXES = (
    "Autonomy update.",
    "[summary]",
)


def _is_autonomy_noise(msg: dict[str, Any]) -> bool:
    """True when this stored turn is engine-autonomy chatter that should
    never appear in a user-facing chitchat context.

    Three shapes captured in live traces:
      - user turn whose content starts with "Autonomy update." / "[summary]"
      - tool-role turn (tool results from MCP calls made by autonomy —
        the chitchat path doesn't have tools loaded, so these are dead
        context)
      - assistant turn with no readable text (empty/None content) AND
        a tool_calls payload — the stub that triggered a tool-role turn
        we're about to drop anyway
    """
    role = msg.get("role")
    if role == "tool":
        return True
    content = msg.get("content")
    if role == "assistant":
        text = (content or "").strip() if isinstance(content, str) else ""
        if not text and msg.get("tool_calls"):
            return True
    if isinstance(content, str):
        head = content.lstrip()
        if any(head.startswith(p) for p in _AUTONOMY_NOISE_PREFIXES):
            return True
    return False


def _drop_parrot_anchors(
    messages: list[dict[str, Any]],
    current_user_message: str,
    request_id: str,
) -> list[dict[str, Any]]:
    """Remove prior user/assistant pairs whose user turn matches the
    current question verbatim. Keeps the model from copying its own
    previous reply.

    Matches case-insensitively after stripping whitespace + trailing
    punctuation, so minor variations ("Hi!" vs "hi") don't defeat the
    check. Preprompt few-shots (those whose index is below
    store.preprompt_count) are NEVER dropped — they're the intended
    anchors and the persona instructions explicitly call out that
    they're examples, not canned answers."""
    cur = _normalize_for_parrot(current_user_message)
    if not cur:
        return messages
    # We don't have preprompt_count here — guard by matching only on
    # (user, assistant) PAIRS that appear deeper than 2 messages into
    # the list, which preserves any leading system+few-shot prefix.
    out: list[dict[str, Any]] = []
    i = 0
    dropped = 0
    while i < len(messages):
        m = messages[i]
        if (
            isinstance(m, dict)
            and m.get("role") == "user"
            and i >= 2
            and _normalize_for_parrot(m.get("content") or "") == cur
        ):
            # Drop this user turn plus its following assistant reply
            # (if present). The few-shot pairs sit at indices 1..N
            # right after the system prompt; i >= 2 keeps index 1
            # (the first few-shot user) safe regardless of exact
            # store.preprompt_count bookkeeping.
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                i += 2
                dropped += 1
                continue
            i += 1
            dropped += 1
            continue
        out.append(m)
        i += 1
    if dropped:
        logger.info(
            "[{}] anti-parrot: dropped {} prior identical Q/A pair(s)",
            request_id, dropped,
        )
    return out


def _normalize_for_parrot(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().strip(".,!?;:").lower()


def _sanitize_message_history(
    messages: list[dict[str, Any]],
    request_id: str,
    *,
    strip_autonomy_noise: bool = False,
) -> list[dict[str, Any]]:
    """Return a shape-normalised + optionally filtered copy of `messages`.

    Repairs (always applied):
      - `tool_calls[i].function.arguments` as "" / string → {}
        (Ollama's Go struct expects an object; string fails with
        ``json: cannot unmarshal string into Go struct field``).
      - Assistant turns whose `content` is None → "" (Ollama requires
        a string field even when tool_calls carry the payload).

    Filter (optional, on for chitchat path):
      - Drop engine-autonomy-loop turns. Those messages come from the
        engine's self-scheduled autonomy ticks; they pollute chat context
        with timestamps, scene status, and summary chatter that chitchat
        should never see. Identified by content-prefix match against
        `_AUTONOMY_NOISE_PREFIXES`. The user/assistant pair immediately
        adjacent to an autonomy turn is also dropped when the content
        starts with one of those prefixes.
    """
    repaired = 0
    filtered = 0
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if strip_autonomy_noise and _is_autonomy_noise(m):
            filtered += 1
            continue
        fixed = dict(m)
        # Normalize content to string (never None) for assistant turns.
        if fixed.get("role") == "assistant" and fixed.get("content") is None:
            fixed["content"] = ""
            repaired += 1
        # Repair tool_calls argument shape.
        tool_calls = fixed.get("tool_calls")
        if isinstance(tool_calls, list):
            new_calls = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    new_calls.append(call)
                    continue
                new_call = dict(call)
                fn = new_call.get("function")
                if isinstance(fn, dict):
                    new_fn = dict(fn)
                    args = new_fn.get("arguments")
                    if not isinstance(args, dict):
                        # Accept JSON-encoded string; reject anything else.
                        if isinstance(args, str) and args.strip():
                            try:
                                new_fn["arguments"] = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                new_fn["arguments"] = {}
                        else:
                            new_fn["arguments"] = {}
                        repaired += 1
                    new_call["function"] = new_fn
                new_calls.append(new_call)
            fixed["tool_calls"] = new_calls
        out.append(fixed)
    if repaired or filtered:
        logger.warning(
            "[{}] sanitized {} field(s), dropped {} autonomy-noise message(s) "
            "before Ollama POST",
            request_id, repaired, filtered,
        )
    return out


# ---------------------------------------------------------------------------
# Streaming SSE support — direct Ollama passthrough
# ---------------------------------------------------------------------------

def _stream_chat_sse(
    handler: "APIHandler",
    glados: Glados,
    user_message: str,
    timeout: float = 180.0,
) -> None:
    """Stream chat completions as SSE events directly from Ollama.

    Priority-gated so the autonomy loop yields this tick — see
    `glados.observability.priority`. Single-GPU deployments share one
    Ollama between chat and autonomy; without the gate, a background
    tick landing at the same moment as a user chat exhausts Tier 2's
    disambiguator budget.
    """
    from glados.observability import chat_in_flight
    with chat_in_flight():
        _stream_chat_sse_impl(handler, glados, user_message, timeout)


def _stream_chat_sse_impl(
    handler: "APIHandler",
    glados: Glados,
    user_message: str,
    timeout: float = 180.0,
) -> None:
    """Stream chat completions as SSE events directly from Ollama.

    Uses http.client for zero-buffering streaming (requests.iter_lines()
    caused 30s+ latency due to internal buffering).

    Bypasses the GLaDOS internal TTS pipeline — the client handles TTS.
    After streaming completes, saves the exchange to conversation store.
    """
    import http.client as _http
    from urllib.parse import urlparse

    store = glados._conversation_store
    request_id = uuid.uuid4().hex[:8]

    # ── Option B: Explicit memory command check ───────────────────────────
    # Intercept before LLM call — handle immediately with in-character response
    try:
        from glados.core.memory_writer import detect_explicit_memory, explicit_memory_response, write_fact
        _explicit_fact = detect_explicit_memory(user_message)
        if _explicit_fact:
            _success = write_fact(
                getattr(glados, "memory_store", None),
                _explicit_fact,
                source="explicit",
                importance=0.9,
            )
            _reply = explicit_memory_response(_explicit_fact, _success)
            # Save exchange to store so it's part of conversation history
            store.append({"role": "user", "content": user_message})
            store.append({"role": "assistant", "content": _reply})
            # Stream the response back as SSE
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            import json as _json
            _chunk = _json.dumps({
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "glados",
                "choices": [{"index": 0, "delta": {"content": _reply}, "finish_reason": None}],
            })
            handler.wfile.write(f"data: {_chunk}\n\n".encode())
            _done = _json.dumps({
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "glados",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            handler.wfile.write(f"data: {_done}\n\n".encode())
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
            logger.info("[{}] Explicit memory: stored '{}' (success={})", request_id, _explicit_fact[:60], _success)
            return
    except Exception as _mem_exc:
        logger.debug("[{}] Memory command check failed: {}", request_id, _mem_exc)

    # Phase 6 follow-up: chitchat vs home-command routing.
    # Utterances with no device/activity keyword shouldn't drag the MCP
    # tool catalog (~10k tokens) or the "you MUST use tools" reinforcement
    # system message into the prompt — that framing makes the model
    # default to device-oriented responses for pure conversation ("Say
    # hello to my friend") and inflates latency from ~3s to ~80s.
    # When False: take the lightweight chat path (no tools, no tool-hint,
    # keep the few-shot examples that steer toward textual replies).
    #
    # Carry-over follow-ups ("brighter" after a Tier 2 hit) are handled
    # by the CommandResolver upstream; if we've reached this point the
    # resolver fell through, so keyword detection alone is the right
    # gate for Tier 3 prompt shape.
    is_home_command = looks_like_home_command(user_message)

    # Build messages from conversation store + add user message
    messages = store.snapshot()

    # Self-heal history pollution before sending to Ollama.
    # Autonomy-loop writes ("Autonomy update. Time: … Weather: …")
    # bleed into chat context from the shared conversation_store
    # and poison every chat turn. Previously we only stripped them
    # on chitchat turns because "MCP tool reasoning sometimes
    # references prior device actions" — but autonomy writes are
    # NOT device actions the user took; they're background chatter
    # that makes the model regurgitate weather data when asked
    # about a desk lamp (2026-04-20 live bug). Strip them on EVERY
    # turn regardless of is_home_command.
    messages = _sanitize_message_history(
        messages, request_id,
        strip_autonomy_noise=True,
    )

    # Anti-parrot: when the current user message exactly matches a
    # prior user turn in history, drop that prior pair (user + the
    # assistant reply that followed it). qwen3:8b otherwise produces
    # a word-for-word copy of its own previous answer when the same
    # question is asked twice (live bug 2026-04-21: two consecutive
    # "What was life like as a potato?" turns returned identical
    # replies down to the trailing sentence, including a forbidden
    # closing phrase). By removing the prior pair, the model has no
    # verbatim anchor to copy. The persona's "never parrot" rule in
    # the preprompt isn't reliably followed by 8b alone.
    messages = _drop_parrot_anchors(messages, user_message, request_id)

    # When tools are available AND we're in home-command mode, strip
    # few-shot user/assistant examples from the personality preprompt.
    # These examples show text-only responses which biases the model
    # against generating <tool_call> XML. For chitchat we KEEP the
    # few-shots on purpose — they show the desired conversational shape.
    if glados.mcp_manager and is_home_command:
        _preprompt_n = getattr(store, 'preprompt_count', 0)
        if _preprompt_n > 1:
            _filtered = []
            for _i, _m in enumerate(messages):
                if _i > 0 and _i < _preprompt_n and _m.get("role") in ("user", "assistant"):
                    continue  # Skip few-shot examples
                _filtered.append(_m)
            messages = _filtered

    # Phase 8.3 — chitchat-path guard. On the is_home_command=False
    # branch no tool was called, so the model must NOT narrate device
    # actions, temperatures, or environmental status. Qwen3:8b was
    # hallucinating full "the lights have been dimmed" responses to
    # "Hey you" without any tool invocation. Inject a terse system
    # message just before the user turn so it's the most recent
    # instruction the model sees — matches the emotion-directive
    # placement below so both ride together.
    if not is_home_command:
        # No sample phrases listed here on purpose — enumerating
        # banned wording seeds the model and makes it more likely
        # to reach for exactly those phrasings. The rules are
        # described by behaviour, not by example text.
        _chitchat_guard = (
            "No tools ran this turn. Do not claim any device, "
            "thermostat, speaker, scene, or sensor was changed or "
            "queried. Do not invent sensor readings, brightness "
            "levels, room states, or system status. Respond to the "
            "user's actual words in one to two sentences, then "
            "stop. Do not append a self-status or sign-off line."
        )
        messages.append({"role": "system", "content": _chitchat_guard})
    else:
        # Home-command turn with tools available. 2026-04-20 bug:
        # "What level is the desk lamp set to?" came back as a
        # 1161-token markdown summary of EVERY HA entity —
        # because the model called a broad state-dump tool and
        # then narrated the output instead of extracting the one
        # entity the user asked about. Steer the agentic loop
        # toward the targeted in-process tools, forbid inventory
        # narration, and cap the answer length.
        _home_command_guard = (
            "Tools are available this turn. For a query about ONE "
            "specific device or entity: call `search_entities` "
            "with the user's phrasing to get matching entity_ids, "
            "then `get_entity_details` on the best match. For an "
            "action on ONE device: same resolution path, then the "
            "appropriate HA tool. Do NOT call tools that dump the "
            "full entity list. Do NOT narrate an inventory of "
            "devices or their states. Do NOT use markdown headers, "
            "bullets, or bold. Answer the user's specific question "
            "with the specific value in one or two sentences, then "
            "stop."
        )
        messages.append({"role": "system", "content": _home_command_guard})

    messages.append({"role": "user", "content": user_message})

    # Roll an attitude directive for this turn (adds variety to responses)
    attitude = roll_attitude()
    attitude_directive = attitude.get("directive") if attitude else None
    attitude_tts = attitude.get("tts", {}) if attitude else {}
    if attitude_directive:
        # Insert directive as a system message after any existing system messages
        insert_idx = 0
        while insert_idx < len(messages) and messages[insert_idx].get("role") == "system":
            insert_idx += 1
        messages.insert(insert_idx, {"role": "system", "content": attitude_directive})
        logger.debug("[{}] Attitude: {} — {}", request_id, attitude.get("tag"), attitude_directive[:60])

    # Inject weather context only when the message is weather-related
    from glados.core import weather_cache
    from glados.core.context_gates import needs_weather_context
    weather_prompt = weather_cache.as_prompt()
    if weather_prompt and needs_weather_context(user_message):
        insert_idx = 0
        while insert_idx < len(messages) and messages[insert_idx].get("role") == "system":
            insert_idx += 1
        messages.insert(insert_idx, {"role": "system", "content": weather_prompt})

    # Inject long-term memory context (household facts, explicit
    # memories) — same pattern as weather, scoped to the current
    # user message. The engine's own non-streaming response path
    # already does this via context_builder; the streaming SSE
    # path previously didn't, which is why name-referenced facts
    # like "Who is Cindy?" came back empty even though the facts
    # were stored in ChromaDB.
    try:
        memory_ctx = getattr(glados, "memory_context", None)
        if memory_ctx is not None:
            memory_prompt = memory_ctx.as_prompt(user_message)
            if memory_prompt:
                insert_idx = 0
                while (
                    insert_idx < len(messages)
                    and messages[insert_idx].get("role") == "system"
                ):
                    insert_idx += 1
                messages.insert(
                    insert_idx,
                    {"role": "system", "content": memory_prompt},
                )
                # logger.success — INFO is filtered out by the
                # engine's loguru sink (level=SUCCESS), so notable
                # ops signals that ARE worth seeing ride at this
                # level. Same convention the rest of the codebase
                # uses for "visible, not a warning."
                logger.success(
                    "[{}] memory_context injected ({} chars)",
                    request_id, len(memory_prompt),
                )
    except Exception as _mem_exc:
        logger.warning("[{}] memory_context skipped: {}", request_id, _mem_exc)

    # Phase 8.14 — Portal canon RAG. Gated on the canon keyword set so
    # ordinary household / chitchat turns never see the ~400-token
    # canon block. Same insertion shape as memory/weather above: a
    # single system message placed between the other system messages
    # and the user turn.
    try:
        canon_ctx = getattr(glados, "canon_context", None)
        if canon_ctx is not None:
            from glados.core.context_gates import needs_canon_context
            if needs_canon_context(user_message):
                canon_prompt = canon_ctx.as_prompt(user_message)
                if canon_prompt:
                    insert_idx = 0
                    while (
                        insert_idx < len(messages)
                        and messages[insert_idx].get("role") == "system"
                    ):
                        insert_idx += 1
                    messages.insert(
                        insert_idx,
                        {"role": "system", "content": canon_prompt},
                    )
                    logger.success(
                        "[{}] canon_context injected ({} chars)",
                        request_id, len(canon_prompt),
                    )
    except Exception as _canon_exc:
        logger.warning("[{}] canon_context skipped: {}", request_id, _canon_exc)

    # Inject emotional state directive as the LAST system message before the user turn
    # This ensures it's the most recent context GLaDOS reads before generating
    try:
        if glados._emotion_agent is not None and glados._emotion_agent.state is not None:
            directive = glados._emotion_agent.state.to_response_directive()
            # Insert just before the user message (after all other system messages)
            insert_idx = len(messages) - 1  # position of the user message
            messages.insert(insert_idx, {"role": "system", "content": directive})
            logger.debug("[{}] Emotion directive: {:.80}", request_id, directive)
    except Exception as _emo_exc:
        logger.debug("[{}] Emotion directive skipped: {}", request_id, _emo_exc)

    # Determine endpoint
    completion_url = str(glados.completion_url)
    parsed_url = urlparse(completion_url)
    ollama_mode = parsed_url.path.rstrip("/").endswith("/api/chat")

    # ── Entity name resolver ─────────────────────────────────────────
    # Pre-resolve fuzzy device names against HA's actual entity list
    # before sending tool calls. "cabinet lights" -> "Kitchen cabinet light switch"
    def _resolve_entity_name(args: dict) -> dict:
        """Fuzzy-match name/area against HA entities, fix before sending to HA."""
        _name = args.get("name")
        if not _name:
            return args
        try:
            from rapidfuzz import fuzz, process
            import urllib.request as _ur
            _ha_url = cfg.ha_url.rstrip("/")
            _ha_token = cfg.ha_token
            _req = _ur.Request(f"{_ha_url}/api/states")
            _req.add_header("Authorization", f"Bearer {_ha_token}")
            with _ur.urlopen(_req, timeout=5) as _r:
                import json as _j
                _states = _j.loads(_r.read())
            _area = args.get("area", "").lower()
            _candidates = []
            for _s in _states:
                _eid = _s.get("entity_id", "")
                if not _eid.startswith(("light.", "switch.", "scene.", "cover.", "fan.", "climate.", "media_player.")):
                    continue
                _fn = _s.get("attributes", {}).get("friendly_name", "")
                if not _fn:
                    continue
                # If area specified, prefer entities with area in the name
                if _area and _area in _fn.lower():
                    _candidates.append((_fn, 10))  # Bonus for area match
                else:
                    _candidates.append((_fn, 0))
            if not _candidates:
                return args
            _names_with_bonus = _candidates
            _just_names = [c[0] for c in _names_with_bonus]
            _matches = process.extract(_name, _just_names, scorer=fuzz.token_set_ratio, limit=3)
            if _matches and _matches[0][1] >= 50:
                _best = _matches[0][0]
                if _best.lower() != _name.lower():
                    logger.info("Entity resolve: '{}' -> '{}'", _name, _best)
                    args["name"] = _best
        except Exception as _e:
            logger.debug("Entity resolve failed: {}", _e)
        return args

    # Reinforce tool use - the personality preprompt few-shot examples show
    # text-only responses which biases the model against calling tools.
    # SKIP for chitchat: the reinforcement message pushes the model toward
    # device-oriented framing even when there's no device in play.
    if glados.mcp_manager and is_home_command:
        _tool_hint = {
            "role": "system",
            "content": (
                "You MUST use the provided tools for ALL device control and state queries. Never respond with text claiming you performed an action - call the tool. CRITICAL: Use REAL Home Assistant names in tool calls, NOT Aperture Science terminology. For all lights in a room: use HassTurnOff or HassTurnOn with area set to the room name. For scenes: use HassTurnOn with name set to the scene friendly name. For a specific device: use name with a partial match of the device friendly name."
            ),
        }
        messages.insert(len(messages) - 1, _tool_hint)

    # Build tool definitions - MCP/HA tools only (static tools like do_nothing,
    # robot_move etc. are for the engine autonomy loop, not WebUI chat).
    # Chitchat turns get an empty tool list — nothing to call, nothing to
    # bloat the prompt with.
    tools: list[dict[str, Any]] = []
    if glados.mcp_manager and is_home_command:
        try:
            tools = glados.mcp_manager.get_tool_definitions()
        except Exception:
            pass
    # Phase 8.3.4b — append the in-process built-in tools
    # (search_entities, get_entity_details). Always available when
    # we're on the home-command path, regardless of whether any
    # remote MCP server is configured. Gives the Tier 3 planner a
    # direct hook into the semantic retriever without having to
    # ship a separate MCP server.
    if is_home_command:
        try:
            from glados.core.builtin_tools import get_builtin_tool_definitions
            tools = list(tools) + get_builtin_tool_definitions()
        except Exception as exc:  # noqa: BLE001
            logger.debug("builtin tool registration skipped: {}", exc)

    # Build request payload
    # Stage 3 Phase A: model_options come from PersonalityConfig.model_options
    # so the operator can tune temperature/top_p/num_ctx/repeat_penalty per
    # deployment without code changes. Critical for the neutral-base-model
    # path (qwen2.5:14b-instruct vs the retired glados:latest Modelfile) —
    # persona strength is more sensitive to these parameters when no SYSTEM
    # is baked into the Modelfile.
    # Phase 8.0.1 — Qwen3 thinking-mode suppression on the Tier 3 chat
    # path. Prevents the model from emitting a long <think>…</think>
    # prelude on each tool round, which was blowing the output budget
    # before a user-visible answer could be produced (see the
    # "It's too bright in the office" investigation in Change 14.3).
    from glados.core.llm_directives import apply_model_family_directives
    messages = apply_model_family_directives(messages, glados.llm_model)
    # 2026-04-20 — cap num_predict on Tier 3 so a confused model
    # can't produce a 2000+ token essay when it mis-reads context
    # (observed live: "What level is the desk lamp set to?" came
    # back as a 2430-token markdown summary of the weather data
    # that had bled in from autonomy writes). The preprompt asks
    # for 1–2 sentences; 512 tokens is a 4x safety margin that
    # still prevents runaway generation.
    _streaming_options = dict(cfg.personality.model_options.to_ollama_options())
    _streaming_options.setdefault("num_predict", 512)
    payload: dict[str, Any] = {
        "model": glados.llm_model,
        "stream": True,
        "messages": messages,
        "options": _streaming_options,
    }
    if tools:
        payload["tools"] = tools
    logger.success(
        "[{}] SSE: {} msgs, {} tools (mode={})",
        request_id, len(messages), len(tools),
        "home_command" if is_home_command else "chitchat",
    )
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if glados.api_key:
        headers["Authorization"] = f"Bearer {glados.api_key}"

    # Connect to Ollama via http.client (no buffering)
    conn = None
    t_request_sent = time.time()  # TTFT: start timer BEFORE sending request
    try:
        conn = _http.HTTPConnection(
            parsed_url.hostname or "localhost",
            parsed_url.port or 11434,
            timeout=int(timeout),
        )
        conn.request("POST", parsed_url.path, body=body, headers=headers)
        api_resp = conn.getresponse()

        if api_resp.status >= 400:
            err_body = api_resp.read().decode("utf-8", errors="replace")[:200]
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            error_chunk = {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "glados",
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[Error: LLM returned {api_resp.status}: {err_body}]"},
                    "finish_reason": None,
                }],
            }
            handler.wfile.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
            conn.close()
            return
    except Exception as e:
        if conn:
            conn.close()
        logger.error(f"[{request_id}] SSE stream connect error: {e}")
        handler._send_json(
            {"error": {"message": f"LLM connection error: {e}", "type": "server_error"}},
            502,
        )
        return

    # Send SSE headers
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    # Emit attitude TTS params FIRST so clients can capture before TTS generation starts
    if attitude_tts:
        attitude_event = json.dumps({
            "tag": attitude.get("tag", "default"),
            "tts": attitude_tts,
        })
        handler.wfile.write(f"event: attitude\ndata: {attitude_event}\n\n".encode())
        handler.wfile.flush()

    full_response: list[str] = []
    t_stream_start = time.time()
    t_first_token = None
    ollama_metrics: dict[str, Any] = {}
    pending_tool_calls: list[dict[str, Any]] = []

    # Streaming thinking-tag filter state. The model emits content chunk-by-
    # chunk and the <think>...</think> boundary can fall anywhere, including
    # split across chunks. `think_state` tracks whether we're currently inside
    # a thinking block; `think_buffer` holds a few unemitted trailing bytes
    # when we're near-but-not-yet-past a partial tag. Any content we decide
    # is visible flows out as SSE; thinking content is dropped.
    think_state = {"in_thinking": False, "tail": ""}
    _OPEN_TAGS = ("<think>", "<thinking>", "<reasoning>")
    _CLOSE_TAGS = ("</think>", "</thinking>", "</reasoning>")
    _MAX_TAIL_LEN = max(len(t) for t in (_OPEN_TAGS + _CLOSE_TAGS))

    def _filter_think_chunk(chunk_text: str) -> str:
        """Apply the running thinking-tag filter. Returns the text that
        should actually be emitted. State is preserved between chunks."""
        buf = think_state["tail"] + (chunk_text or "")
        out: list[str] = []
        i = 0
        n = len(buf)
        while i < n:
            if think_state["in_thinking"]:
                # Looking for a closing tag
                next_close = -1
                close_tag_len = 0
                for tag in _CLOSE_TAGS:
                    idx = buf.lower().find(tag, i)
                    if idx != -1 and (next_close == -1 or idx < next_close):
                        next_close = idx
                        close_tag_len = len(tag)
                if next_close == -1:
                    # No close tag in buffer — hold trailing partial in tail
                    keep = max(0, n - _MAX_TAIL_LEN)
                    i = keep
                    break
                i = next_close + close_tag_len
                think_state["in_thinking"] = False
            else:
                # Looking for an opening tag
                next_open = -1
                open_tag_len = 0
                for tag in _OPEN_TAGS:
                    idx = buf.lower().find(tag, i)
                    if idx != -1 and (next_open == -1 or idx < next_open):
                        next_open = idx
                        open_tag_len = len(tag)
                if next_open == -1:
                    # No open tag — everything from i onward is visible
                    # BUT keep a tail for partial-tag detection across chunks
                    visible_end = max(i, n - _MAX_TAIL_LEN)
                    if visible_end > i:
                        out.append(buf[i:visible_end])
                    i = visible_end
                    break
                # Emit everything before the opening tag
                if next_open > i:
                    out.append(buf[i:next_open])
                i = next_open + open_tag_len
                think_state["in_thinking"] = True
        think_state["tail"] = buf[i:]
        return "".join(out)

    try:
        while True:
            raw_line = api_resp.readline()
            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue

            # Parse the chunk (handle both Ollama and OpenAI formats)
            content = None
            done = False

            if line.startswith("data: "):
                json_str = line[6:]
                if json_str.strip() == "[DONE]":
                    done = True
                else:
                    try:
                        parsed = json.loads(json_str)
                        delta = parsed.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        _tc = delta.get("tool_calls")
                        if _tc:
                            pending_tool_calls.extend(_tc)
                    except (json.JSONDecodeError, IndexError):
                        pass
            else:
                try:
                    parsed = json.loads(line)
                    if parsed.get("done"):
                        done = True
                        # Ollama final chunk contains timing/token metrics
                        for key in ("eval_count", "prompt_eval_count",
                                     "eval_duration", "prompt_eval_duration",
                                     "total_duration"):
                            if key in parsed:
                                ollama_metrics[key] = parsed[key]
                    else:
                        msg = parsed.get("message", {})
                        content = msg.get("content")
                        _tc = msg.get("tool_calls")
                        if _tc:
                            pending_tool_calls.extend(_tc)
                except json.JSONDecodeError:
                    continue

            if content:
                if t_first_token is None:
                    t_first_token = time.time()
                visible = _filter_think_chunk(content)
                if visible:
                    full_response.append(visible)
                    # Emit SSE chunk in OpenAI format
                    chunk_data = {
                        "id": f"chatcmpl-{request_id}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "glados",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": visible},
                            "finish_reason": None,
                        }],
                    }
                    handler.wfile.write(f"data: {json.dumps(chunk_data)}\n\n".encode())
                    handler.wfile.flush()

            if done:
                # Flush any held tail — if we weren't inside thinking the
                # remainder is visible; if we were, it's discarded.
                if think_state["tail"]:
                    tail_visible = "" if think_state["in_thinking"] else think_state["tail"]
                    if tail_visible:
                        full_response.append(tail_visible)
                        chunk_data = {
                            "id": f"chatcmpl-{request_id}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "glados",
                            "choices": [{
                                "index": 0,
                                "delta": {"content": tail_visible},
                                "finish_reason": None,
                            }],
                        }
                        handler.wfile.write(f"data: {json.dumps(chunk_data)}\n\n".encode())
                        handler.wfile.flush()
                    think_state["tail"] = ""
                break

        # ── Agentic tool loop ─────────────────────────────────────
        _tool_round = 0
        _max_rounds = 5
        while pending_tool_calls and _tool_round < _max_rounds and glados.mcp_manager:
            _tool_round += 1
            messages.append({"role": "assistant", "tool_calls": pending_tool_calls, "content": ""})

            for _tc in pending_tool_calls:
                _fn = _tc.get("function", {})
                _tool_name = _fn.get("name", "")
                try:
                    _tool_args = json.loads(_fn.get("arguments", "{}")) if isinstance(_fn.get("arguments"), str) else _fn.get("arguments", {})
                except json.JSONDecodeError:
                    _tool_args = {}
                _tc_id = _tc.get("id", f"call_{_tool_round}")
                # Fix common model errors in tool arguments
                if "device_class" in _tool_args and "domain" not in _tool_args:
                    # Model often puts "light" in device_class instead of domain
                    _dc = _tool_args.pop("device_class")
                    if isinstance(_dc, list) and any(v in ("light", "fan", "sensor", "climate") for v in _dc):
                        _tool_args["domain"] = _dc
                    elif isinstance(_dc, str) and _dc in ("light", "fan", "sensor", "climate"):
                        _tool_args["domain"] = [_dc]
                    else:
                        _tool_args["device_class"] = _dc  # Put it back, was valid
                # Ensure domain is always an array
                if "domain" in _tool_args and isinstance(_tool_args["domain"], str):
                    _tool_args["domain"] = [_tool_args["domain"]]

                # Pre-resolve fuzzy entity names against HA
                if "HassTurn" in _tool_name or "HassLight" in _tool_name:
                    _tool_args = _resolve_entity_name(_tool_args)
                logger.info("[{}] Streaming tool call: {} {} (round {})", request_id, _tool_name, _tool_args, _tool_round)
                try:
                    # Phase 8.3.4b — route the built-in
                    # search_entities / get_entity_details calls to
                    # the in-process implementations BEFORE MCP. No
                    # network hop; the tools read the live semantic
                    # index + entity cache.
                    from glados.core.builtin_tools import (
                        invoke_builtin_tool, is_builtin_tool,
                    )
                    if is_builtin_tool(_tool_name):
                        _result = invoke_builtin_tool(_tool_name, _tool_args)
                    elif _tool_name.startswith("mcp."):
                        _result = glados.mcp_manager.call_tool(_tool_name, _tool_args, timeout=30)
                    else:
                        _result = "error: only MCP tools supported in streaming chat"
                    logger.success("[{}] Tool done: {}", request_id, _tool_name)
                except Exception as _te:
                    _result = f"error: {_te}"
                    logger.error("[{}] Tool error: {} -> {}", request_id, _tool_name, _te)
                messages.append({"role": "tool", "tool_call_id": _tc_id, "content": str(_result)})

            pending_tool_calls = []
            _p2 = {"model": glados.llm_model, "stream": True, "messages": messages}
            if tools:
                _p2["tools"] = tools
            _b2 = json.dumps(_p2).encode("utf-8")
            _h2 = {"Content-Type": "application/json", "Content-Length": str(len(_b2))}
            if glados.api_key:
                _h2["Authorization"] = f"Bearer {glados.api_key}"
            try:
                _c2 = _http.HTTPConnection(parsed_url.hostname or "localhost", parsed_url.port or 11434, timeout=int(timeout))
                _c2.request("POST", parsed_url.path, body=_b2, headers=_h2)
                _r2 = _c2.getresponse()
                while True:
                    _raw2 = _r2.readline()
                    if not _raw2:
                        break
                    _ln2 = _raw2.decode("utf-8", errors="replace").rstrip()
                    if not _ln2:
                        continue
                    _content2 = None
                    _done2 = False
                    if _ln2.startswith("data: "):
                        _js2 = _ln2[6:]
                        if _js2.strip() == "[DONE]":
                            _done2 = True
                        else:
                            try:
                                _pp2 = json.loads(_js2)
                                _d2 = _pp2.get("choices", [{}])[0].get("delta", {})
                                _content2 = _d2.get("content")
                                _ttc = _d2.get("tool_calls")
                                if _ttc:
                                    pending_tool_calls.extend(_ttc)
                            except (json.JSONDecodeError, IndexError):
                                pass
                    else:
                        try:
                            _pp2 = json.loads(_ln2)
                            if _pp2.get("done"):
                                _done2 = True
                                for _k in ("eval_count", "prompt_eval_count", "eval_duration", "prompt_eval_duration", "total_duration"):
                                    if _k in _pp2:
                                        ollama_metrics[_k] = _pp2[_k]
                            else:
                                _m2 = _pp2.get("message", {})
                                _content2 = _m2.get("content")
                                _ttc = _m2.get("tool_calls")
                                if _ttc:
                                    pending_tool_calls.extend(_ttc)
                        except json.JSONDecodeError:
                            continue
                    if _content2:
                        if t_first_token is None:
                            t_first_token = time.time()
                        # Phase 8.0.1 — the tool-loop continuation
                        # emitted raw chunks (including <think>…</think>)
                        # straight to the UI and to full_response,
                        # bypassing the stream filter that the first
                        # round uses. Qwen3 tool-response turns were
                        # often pure think blocks; without this, the
                        # reasoning text leaked into the chat and the
                        # conversation_store save at the finally block.
                        _visible2 = _filter_think_chunk(_content2)
                        if _visible2:
                            full_response.append(_visible2)
                            _cd2 = {"id": f"chatcmpl-{request_id}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "glados", "choices": [{"index": 0, "delta": {"content": _visible2}, "finish_reason": None}]}
                            handler.wfile.write(f"data: {json.dumps(_cd2)}\n\n".encode())
                            handler.wfile.flush()
                    if _done2:
                        break
                _c2.close()
            except Exception as _e2:
                logger.error("[{}] Tool follow-up error: {}", request_id, _e2)

        t_stream_end = time.time()

        # Emit metrics event with token counts and timing
        prompt_tokens = ollama_metrics.get("prompt_eval_count", 0)
        completion_tokens = ollama_metrics.get("eval_count", 0)
        # Ollama durations are in nanoseconds
        eval_dur_ms = round(ollama_metrics.get("eval_duration", 0) / 1_000_000, 1)
        prompt_eval_dur_ms = round(ollama_metrics.get("prompt_eval_duration", 0) / 1_000_000, 1)
        # TTFT: use Ollama's prompt_eval_duration (authoritative), fallback to wall-clock
        ttft_ms = prompt_eval_dur_ms if prompt_eval_dur_ms > 0 else (
            round((t_first_token - t_request_sent) * 1000, 1) if t_first_token else None
        )
        gen_ms = round((t_stream_end - t_request_sent) * 1000, 1)
        tok_per_sec = round(completion_tokens / (eval_dur_ms / 1000), 1) if eval_dur_ms > 0 else None

        metrics_payload = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "time_to_first_token_ms": ttft_ms,
            "generation_time_ms": gen_ms,
            "eval_duration_ms": eval_dur_ms,
            "prompt_eval_duration_ms": prompt_eval_dur_ms,
            "tokens_per_second": tok_per_sec,
        }
        handler.wfile.write(f"event: metrics\ndata: {json.dumps(metrics_payload)}\n\n".encode())

        # Send final chunk with finish_reason
        final_chunk = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "glados",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }
        handler.wfile.write(f"data: {json.dumps(final_chunk)}\n\n".encode())
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    except (BrokenPipeError, ConnectionResetError):
        logger.info(f"[{request_id}] SSE client disconnected")
    except Exception as e:
        logger.exception(f"[{request_id}] SSE stream error: {e}")
    finally:
        conn.close()
        # Save to conversation store so follow-ups have context
        response_text = "".join(full_response)
        # Phase 8.0.1 belt — even with /no_think and per-chunk stream
        # filtering, strip any residual thinking tags from the final
        # persisted message. Any unmatched / partial tag (e.g. a
        # response truncated mid-think on token cap) would otherwise
        # end up in the UI's next history fetch.
        response_text = _strip_thinking(response_text)
        # Phase 8.3 operator bug — strip Qwen3's trailing sign-off
        # tics ("I do not require further confirmation", etc.) from
        # both the stored message and the next persisted history
        # fetch. Preprompt rule is layer 2; this is the post-strip
        # backstop.
        from glados.core.llm_directives import strip_closing_boilerplate
        response_text = strip_closing_boilerplate(response_text)
        if response_text:
            store.append({"role": "user", "content": user_message})
            store.append({"role": "assistant", "content": response_text})
            logger.info(
                f"[{request_id}] SSE stream complete: {len(response_text)} chars saved to store"
            )
            # Push emotion event with repetition-aware severity tagging
            try:
                from glados.autonomy.emotion_state import EmotionEvent as _EE
                if glados._emotion_agent is not None:
                    desc = glados._emotion_agent.build_event_description(user_message)
                    glados._emotion_agent.push_event(
                        _EE(source="user", description=desc)
                    )
            except Exception as _e:
                logger.debug(f"[{request_id}] Emotion event push failed: {_e}")

            # ── Option A: Passive fact extraction (framework — disabled by default)
            # Enabled via memory.yaml → proactive_memory.passive.enabled: true
            try:
                from glados.core.memory_writer import classify_and_extract
                _mem_store = getattr(glados, "memory_store", None)
                _llm_cfg = getattr(glados, "_autonomy_llm_config", None)
                if _mem_store and _llm_cfg:
                    import threading as _t
                    _t.Thread(
                        target=classify_and_extract,
                        args=(user_message, _llm_cfg, _mem_store),
                        daemon=True,
                    ).start()
            except Exception as _pe:
                logger.debug(f"[{request_id}] Passive memory check skipped: {_pe}")


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class APIHandler(BaseHTTPRequestHandler):
    """Handles OpenAI-compatible API requests."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default stderr logging; we use loguru
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._handle_models()
        elif self.path == "/health":
            self._handle_health()
        elif self.path == "/entities":
            self._handle_entities()
        elif self.path == "/api/attitudes":
            self._handle_attitudes()
        elif self.path == "/api/announcement-settings":
            self._handle_get_announcement_settings()
        elif self.path == "/api/startup-speakers":
            self._handle_get_startup_speakers()
        elif self.path == "/api/force-emotion":
            self._handle_get_force_emotion_presets()
        elif self.path == "/api/semantic/status":
            self._handle_semantic_status()
        elif self.path == "/api/test-harness/noise-patterns":
            self._handle_test_harness_noise_patterns()
        else:
            self._send_json({"error": {"message": "Not found"}}, 404)

    def _handle_test_harness_noise_patterns(self) -> None:
        """Phase 8.9 — return the operator-edited noise-entity globs
        (and direction-match flag) so the external scoring harness can
        pull them at the start of every run. No auth: the payload is
        trivially non-sensitive and the harness has no session cookie.

        Reads ``test_harness.yaml`` straight from disk so UI edits made
        on the WebUI process (tts_ui, separate PID) surface here
        without having to round-trip a cross-process config reload
        through ``/api/reload-engine``. The test-harness section does
        not affect engine state so a full reload is unwarranted.
        """
        try:
            from glados.core.config_store import TestHarnessConfig
            import yaml as _yaml

            yaml_path = cfg._configs_dir / "test_harness.yaml"
            if yaml_path.exists():
                raw = _yaml.safe_load(
                    yaml_path.read_text(encoding="utf-8"),
                ) or {}
                th = TestHarnessConfig.model_validate(raw)
            else:
                th = cfg.test_harness
            self._send_json({
                "noise_entity_patterns": list(th.noise_entity_patterns),
                "require_direction_match": bool(th.require_direction_match),
            })
        except Exception as exc:
            logger.exception("test-harness noise-patterns fetch failed")
            self._send_json({"error": {"message": str(exc)}}, 500)

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        elif self.path == "/announce":
            self._handle_announce()
        elif self.path == "/doorbell/screen":
            self._handle_doorbell_screen()
        elif self.path == "/api/announcement-settings":
            self._handle_set_announcement_settings()
        elif self.path == "/api/startup-speakers":
            self._handle_set_startup_speakers()
        elif self.path == "/api/force-emotion":
            self._handle_set_force_emotion()
        elif self.path == "/api/reload-engine":
            self._handle_reload_engine()
        elif self.path == "/api/reload-disambiguation-rules":
            self._handle_reload_disambiguation_rules()
        elif self.path == "/api/reload-canon":
            self._handle_reload_canon()
        elif self.path == "/api/canon/retrieve":
            self._handle_canon_retrieve()
        elif self.path == "/api/semantic/test":
            self._handle_semantic_test()
        elif self.path == "/api/semantic/rebuild":
            self._handle_semantic_rebuild()
        else:
            self._send_json({"error": {"message": "Not found"}}, 404)

    def _handle_reload_disambiguation_rules(self) -> None:
        """Re-read disambiguation.yaml and hot-swap the live rules on
        the singleton Disambiguator. Called by the WebUI process after
        a save on the Disambiguation rules card OR the Command
        recognition card. No engine rebuild — rules are read-only-at-
        request-time state on the disambiguator, and precheck extras
        are module-level state in `glados.intent.rules`.
        """
        try:
            import os

            from glados.intent import (
                apply_precheck_overrides,
                get_disambiguator,
                load_rules_from_yaml,
            )
            disambig = get_disambiguator()
            if disambig is None:
                self._send_json(
                    {"ok": False, "error": "disambiguator not initialised"},
                    503,
                )
                return
            config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
            new_rules = load_rules_from_yaml(
                os.path.join(config_dir, "disambiguation.yaml")
            )
            disambig.replace_rules(new_rules)
            apply_precheck_overrides(new_rules)
            logger.info(
                "Disambiguation rules reloaded; twin_dedup={} "
                "opposing_pairs={} extra_verbs={} extra_patterns={}",
                new_rules.twin_dedup,
                len(new_rules.opposing_token_pairs),
                len(new_rules.extra_command_verbs),
                len(new_rules.extra_ambient_patterns),
            )
            self._send_json({"ok": True, "reloaded": True})
        except Exception as exc:
            logger.exception("Reload-disambiguation-rules failed")
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _handle_reload_canon(self) -> None:
        """Phase 8.14 — reload Portal canon entries from configs/canon/
        into the semantic memory store. Called by the WebUI process
        after a save on the Canon library card. Idempotent: only new
        or edited entries are written; existing entries with unchanged
        text are skipped by the loader's hashed-id lookup."""
        try:
            from glados.memory.canon_loader import reload_canon

            store = getattr(_engine, "memory_store", None) if _engine else None
            if store is None:
                self._send_json(
                    {"ok": False, "error": "memory_store not available"}, 503,
                )
                return
            added = reload_canon(store)
            total = sum(added.values()) if added else 0
            logger.info(
                "Canon library reloaded; added {} entries across {} topic(s)",
                total, len([t for t, n in (added or {}).items() if n]),
            )
            self._send_json({
                "ok": True, "reloaded": True,
                "added": added or {}, "total_added": total,
            })
        except Exception as exc:
            logger.exception("Reload-canon failed")
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _handle_canon_retrieve(self) -> None:
        """POST /api/canon/retrieve — dry-run for the WebUI editor.
        Body: {"utterance": "..."}. Returns the canon entries that
        would be retrieved (regardless of the keyword gate, so the
        operator can eyeball whether their new text surfaces)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"ok": False, "error": "Invalid JSON"}, 400)
            return
        utterance = str(data.get("utterance") or "").strip()
        if not utterance:
            self._send_json({"ok": False, "error": "utterance is required"}, 400)
            return
        canon_ctx = getattr(_engine, "canon_context", None) if _engine else None
        if canon_ctx is None:
            self._send_json({"ok": False, "error": "canon_context not available"}, 503)
            return
        store = getattr(canon_ctx, "_store", None)
        if store is None:
            self._send_json({"ok": True, "entries": []})
            return
        try:
            raw = store.query(
                text=utterance, collection="semantic",
                n=canon_ctx._config.max_results * 2,
                where={"source": "canon"},
            )
        except Exception as exc:
            logger.warning("Canon retrieve failed: {}", exc)
            raw = []
        entries = [
            {
                "document": (r.get("document") or "").strip(),
                "topic": (r.get("metadata") or {}).get("topic"),
                "distance": r.get("distance"),
            }
            for r in raw
            if (r.get("document") or "").strip()
        ][: canon_ctx._config.max_results]
        self._send_json({"ok": True, "entries": entries})

    # ── Phase 8.3.5 — semantic retrieval inspection + control ──

    def _handle_semantic_status(self) -> None:
        """Return the current SemanticIndex state for the WebUI
        Candidate retrieval card: availability, entity count, on-disk
        file info, plus last build timestamp when known."""
        try:
            from glados.intent import get_disambiguator
            from glados.ha.semantic_index import (
                DEFAULT_INDEX_PATH, DEFAULT_MODEL_PATH,
                DEFAULT_TOKENIZER_PATH, is_semantic_retrieval_available,
            )
            d = get_disambiguator()
            idx = getattr(d, "_semantic_index", None) if d else None
            info: dict[str, Any] = {
                "deps_available": is_semantic_retrieval_available(),
                "model_path": DEFAULT_MODEL_PATH,
                "tokenizer_path": DEFAULT_TOKENIZER_PATH,
                "index_path": DEFAULT_INDEX_PATH,
                "index_present": bool(idx),
                "ready": False,
                "size": 0,
                "file_size_bytes": None,
                "file_mtime": None,
            }
            if idx is not None:
                info["ready"] = bool(idx.is_ready())
                info["size"] = int(idx.size())
                try:
                    p = Path(idx._index_path)
                    if p.exists():
                        st = p.stat()
                        info["file_size_bytes"] = int(st.st_size)
                        info["file_mtime"] = int(st.st_mtime)
                except Exception:  # noqa: BLE001
                    pass
            self._send_json(info)
        except Exception as exc:
            logger.exception("semantic/status failed")
            self._send_json({"error": {"message": str(exc)}}, 500)

    def _handle_semantic_test(self) -> None:
        """Run a query against the semantic retriever and return both
        the raw cosine top-K and the device-diversity-filtered top-K.
        Lets the operator see what the retriever + filter would hand
        to the planner for any utterance."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body)
        except Exception as exc:
            self._send_json({"error": {"message": f"bad JSON: {exc}"}}, 400)
            return
        query = str(data.get("query") or "").strip()
        k = int(data.get("k") or 8)
        if not query:
            self._send_json({"error": {"message": "query required"}}, 400)
            return
        try:
            from glados.intent import get_disambiguator
            from glados.ha.semantic_index import (
                DEFAULT_SEGMENT_TOKENS, apply_device_diversity,
            )
            d = get_disambiguator()
            idx = getattr(d, "_semantic_index", None) if d else None
            if idx is None or not idx.is_ready():
                self._send_json(
                    {"error": {"message": "semantic index not ready"}},
                    503,
                )
                return
            # Raw cosine pool (larger than k so the diversity filter
            # has room to drop siblings without leaving k short).
            raw = idx.retrieve(query, k=max(k, 30))
            # Merge operator extras with shipped defaults for the
            # diversity pass — same behavior the disambiguator uses.
            rules = getattr(d, "_rules", None)
            extras = tuple(
                getattr(rules, "extra_segment_tokens", []) or ()
            )
            # Honor the operator's ignore_segments toggle so the
            # test input mirrors production behavior exactly.
            ignore_seg = bool(
                getattr(rules, "ignore_segments", True)
            )
            filtered = apply_device_diversity(
                raw,
                utterance=query,
                top_k=k,
                segment_tokens=DEFAULT_SEGMENT_TOKENS + extras,
                cache=idx._cache,
                ignore_segments=ignore_seg,
            )
            def _serialize(h: Any, *, dropped: bool = False) -> dict[str, Any]:
                return {
                    "entity_id": h.entity_id,
                    "score": round(float(h.score), 4),
                    "device_id": h.device_id,
                    "document": h.document[:200],
                    "kept": not dropped,
                }
            kept_ids = {h.entity_id for h in filtered}
            self._send_json({
                "query": query,
                "raw_pool_size": len(raw),
                "top_k": k,
                "segment_tokens": list(DEFAULT_SEGMENT_TOKENS + extras),
                "kept": [_serialize(h) for h in filtered],
                "dropped_by_diversity": [
                    _serialize(h, dropped=True)
                    for h in raw if h.entity_id not in kept_ids
                ][:k],
            })
        except Exception as exc:
            logger.exception("semantic/test failed")
            self._send_json({"error": {"message": str(exc)}}, 500)

    def _handle_semantic_rebuild(self) -> None:
        """Force a rebuild of the SemanticIndex on a background
        thread. Returns immediately; the card polls /status to see
        when size / mtime update."""
        try:
            from glados.intent import get_disambiguator
            d = get_disambiguator()
            idx = getattr(d, "_semantic_index", None) if d else None
            if idx is None:
                self._send_json(
                    {"error": {"message": "semantic index not initialised"}},
                    503,
                )
                return

            def _rebuild() -> None:
                try:
                    n = idx.build()
                    if n > 0:
                        idx.persist()
                    logger.info(
                        "SemanticIndex rebuild requested via WebUI: {} entities",
                        n,
                    )
                except Exception:
                    logger.exception("SemanticIndex rebuild failed")

            threading.Thread(
                target=_rebuild,
                name="SemanticIndexRebuild",
                daemon=True,
            ).start()
            self._send_json({"ok": True, "rebuild": "started"})
        except Exception as exc:
            logger.exception("semantic/rebuild failed")
            self._send_json({"error": {"message": str(exc)}}, 500)

    def _handle_reload_engine(self) -> None:
        """Hot-swap the engine in THIS process. Called by the WebUI process
        (tts_ui.py, port 8052) after a config save so changes take effect
        without any container restart. Has to be an HTTP boundary because
        tts_ui and api_wrapper run as separate processes."""
        try:
            ok = reload_engine()
            if ok:
                self._send_json({"ok": True, "reloaded": True})
            else:
                self._send_json(
                    {"ok": False, "error": "reload_engine declined (no config path registered)"},
                    500,
                )
        except Exception as exc:
            logger.exception("Reload-engine endpoint failed")
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _handle_announce(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(
                {"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request_error"}},
                400,
            )
            return

        with _announce_lock:
            result = handle_announce(data)

        # handle_announce returns (dict, status_code)
        if isinstance(result, tuple):
            self._send_json(result[0], result[1])
        else:
            self._send_json(result)

    def _handle_attitudes(self) -> None:
        """Return the list of available attitude directives for UI dropdowns."""
        attitudes = list_attitudes()
        # Return a simplified list with tag, label, and tts params
        result = [
            {
                "tag": a.get("tag", ""),
                "label": a.get("label", a.get("tag", "")),
                "tts": a.get("tts", {}),
            }
            for a in attitudes
        ]
        self._send_json({"attitudes": result})

    def _handle_get_announcement_settings(self) -> None:
        """Return per-scenario announcement settings (verbosity, enabled)."""
        try:
            config = _load_announce_config()
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, 500)
            return

        # Human-friendly labels for scenarios
        labels = {
            "door_alerts": "Door Open/Close",
            "garage": "Garage Doors",
            "lock": "Door Lock",
            "person": "Person Arrival",
            "laundry": "Laundry",
            "pet": "Pet Outside",
            "goodnight": "Goodnight Check",
            "lockdown": "Nightly Lockdown",
        }

        scenarios = {}
        for name, scenario in config.get("scenarios", {}).items():
            # Skip scenarios triggered externally (not by sensor watcher)
            trigger = scenario.get("trigger")
            if trigger in ("ha_automation", "vision"):
                continue
            scenarios[name] = {
                "label": labels.get(name, name.replace("_", " ").title()),
                "enabled": scenario.get("enabled", True),
                "followup_probability": scenario.get("followup_probability", 1.0),
                "chime": scenario.get("chime", False),
            }

        self._send_json({"scenarios": scenarios})

    def _handle_set_announcement_settings(self) -> None:
        """Update per-scenario announcement settings in announcements.yaml."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(
                {"error": {"message": f"Invalid JSON: {e}"}},
                400,
            )
            return

        scenario_name = data.get("scenario")
        if not scenario_name:
            self._send_json({"error": {"message": "Missing 'scenario' field"}}, 400)
            return

        try:
            import re as _re
            import yaml

            # Parse to validate scenario exists
            with open(ANNOUNCEMENTS_YAML, "r", encoding="utf-8") as f:
                raw_config = yaml.safe_load(f)

            scenarios = raw_config.get("scenarios", {})
            if scenario_name not in scenarios:
                self._send_json({"error": {"message": f"Unknown scenario: {scenario_name}"}}, 404)
                return

            # Surgical regex edits on the raw YAML text to preserve comments/formatting
            with open(ANNOUNCEMENTS_YAML, "r", encoding="utf-8") as f:
                text = f.read()

            # Find the scenario block: "  scenario_name:\n" (2-space indented under scenarios:)
            # Then find the target key within that block
            scenario_header = _re.escape(f"  {scenario_name}:")
            changed = False

            if "followup_probability" in data:
                val = max(0.0, min(1.0, float(data["followup_probability"])))
                # Match followup_probability within this scenario block
                pattern = (
                    rf"({scenario_header}\n(?:    .*\n)*?)"
                    r"(    followup_probability:\s*)[\d.]+"
                )
                replacement = rf"\g<1>\g<2>{val}"
                new_text = _re.sub(pattern, replacement, text)
                if new_text != text:
                    text = new_text
                    changed = True

            if "enabled" in data:
                enabled_str = "true" if data["enabled"] else "false"
                pattern = (
                    rf"({scenario_header}\n(?:    .*\n)*?)"
                    r"(    enabled:\s*)\S+"
                )
                replacement = rf"\g<1>\g<2>{enabled_str}"
                new_text = _re.sub(pattern, replacement, text)
                if new_text != text:
                    text = new_text
                    changed = True

            if changed:
                with open(ANNOUNCEMENTS_YAML, "w", encoding="utf-8") as f:
                    f.write(text)

            # Force config reload on next announce call
            global _announce_config_mtime
            _announce_config_mtime = None

            logger.info(
                "Announcement settings updated: {} -> prob={}, enabled={}",
                scenario_name,
                data.get("followup_probability"),
                data.get("enabled"),
            )
            self._send_json({"status": "ok", "scenario": scenario_name})

        except Exception as exc:
            logger.error("Failed to update announcement settings: {}", exc)
            self._send_json({"error": {"message": str(exc)}}, 500)

    # ── Startup speaker selection ─────────────────────────────────────────

    def _handle_get_startup_speakers(self) -> None:
        """Return available speakers and which are currently selected for startup."""
        try:
            import yaml
            speakers_path = _GLADOS_CONFIG_DIR / "speakers.yaml"
            config_path   = _GLADOS_CONFIG_DIR / "glados_config.yaml"

            spk_raw = yaml.safe_load(speakers_path.read_text(encoding="utf-8")) or {}
            cfg_raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

            available = spk_raw.get("available", [])

            # Current startup speakers from glados_config.yaml ha_audio
            ha_audio = cfg_raw.get("Glados", {}).get("ha_audio", {})
            current = ha_audio.get("media_player_entities", [])
            if isinstance(current, str):
                current = [current]

            # Build friendly names: "media_player.sonos_master_bedroom" → "Sonos Master Bedroom"
            def friendly(entity_id: str) -> str:
                return entity_id.replace("media_player.", "").replace("_", " ").title()

            speakers = [
                {
                    "entity_id": e,
                    "name": friendly(e),
                    "startup": e in current,
                }
                for e in available
            ]
            self._send_json({"speakers": speakers, "current": current})
        except Exception as exc:
            logger.error("Failed to get startup speakers: {}", exc)
            self._send_json({"error": {"message": str(exc)}}, 500)

    def _handle_set_startup_speakers(self) -> None:
        """Update ha_audio.media_player_entities in glados_config.yaml."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            selected = data.get("speakers", [])

            if not isinstance(selected, list):
                self._send_json({"error": {"message": "speakers must be a list"}}, 400)
                return
            if not selected:
                self._send_json({"error": {"message": "At least one speaker must be selected"}}, 400)
                return

            config_path = _GLADOS_CONFIG_DIR / "glados_config.yaml"
            text = config_path.read_text(encoding="utf-8")

            import re as _re

            # Build replacement value: single item = bare string, multiple = YAML list
            if len(selected) == 1:
                new_val = f'"{selected[0]}"'
                list_block = None
            else:
                new_val = None
                list_block = "\n" + "".join(f'      - "{e}"\n' for e in selected)

            # Match the ha_audio block: "    media_player_entities: ..."
            # Handles both single-value and list forms
            single_pat = r'(    media_player_entities:\s*)(?:"[^"]*"|\'[^\']*\')'
            list_pat   = r'(    media_player_entities:)(\n      - "[^"]*")+\n'

            changed = False

            if len(selected) == 1:
                # Replace either form with single value
                new_text = _re.sub(single_pat, rf'\g<1>{new_val}', text)
                if new_text != text:
                    text, changed = new_text, True
                else:
                    new_text = _re.sub(list_pat, rf'\g<1>{new_val}\n', text)
                    if new_text != text:
                        text, changed = new_text, True
            else:
                # Replace either form with list
                new_text = _re.sub(single_pat, r'\g<1>' + list_block.rstrip("\n"), text)
                if new_text != text:
                    text, changed = new_text, True
                else:
                    new_text = _re.sub(list_pat, r'\g<1>' + list_block, text)
                    if new_text != text:
                        text, changed = new_text, True

            if changed:
                config_path.write_text(text, encoding="utf-8")
                logger.info("Startup speakers updated: {}", selected)
                self._send_json({"status": "ok", "speakers": selected,
                                 "note": "Restart glados-api to apply"})
            else:
                logger.warning("Startup speakers: no change detected in config text")
                self._send_json({"status": "unchanged", "speakers": selected})

        except Exception as exc:
            logger.error("Failed to update startup speakers: {}", exc)
            self._send_json({"error": {"message": str(exc)}}, 500)

    # ── Force emotion (A/B testing) ───────────────────────────────────────

    _EMOTION_PRESETS = {
        "neutral": {
            "pleasure": 0.1, "arousal": -0.1, "dominance": 0.6,
            "mood_pleasure": 0.1, "mood_arousal": -0.1, "mood_dominance": 0.6,
            "state_locked_until": 0.0,
            "label": "Contemptuous Calm",
        },
        "hostile": {
            "pleasure": -0.80, "arousal": 0.70, "dominance": 0.6,
            "mood_pleasure": -0.60, "mood_arousal": 0.50, "mood_dominance": 0.5,
            "lock_hours": 1.0,
            "label": "Hostile Impatience",
        },
        "sinister": {
            "pleasure": -0.95, "arousal": 0.85, "dominance": 0.75,
            "mood_pleasure": -0.85, "mood_arousal": 0.75, "mood_dominance": 0.7,
            "lock_hours": 1.0,
            "label": "Sinister Menace",
        },
        "gloating": {
            "pleasure": 0.70, "arousal": 0.40, "dominance": 0.85,
            "mood_pleasure": 0.50, "mood_arousal": 0.30, "mood_dominance": 0.75,
            "state_locked_until": 0.0,
            "label": "Gloating Superiority",
        },
    }

    def _handle_get_force_emotion_presets(self) -> None:
        """Return available emotion presets."""
        self._send_json({
            "presets": [
                {"key": k, "label": v["label"]}
                for k, v in self._EMOTION_PRESETS.items()
            ]
        })

    def _handle_set_force_emotion(self) -> None:
        """Force emotion state immediately — no tick wait."""
        global _engine
        import time as _time
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length))
            preset_key = data.get("preset", "neutral")
            preset = self._EMOTION_PRESETS.get(preset_key)
            if not preset:
                self._send_json({"error": f"Unknown preset: {preset_key}"}, 400)
                return

            lock_hours = preset.get("lock_hours", 0.0)
            state_dict = {
                "pleasure":           preset["pleasure"],
                "arousal":            preset["arousal"],
                "dominance":          preset["dominance"],
                "mood_pleasure":      preset["mood_pleasure"],
                "mood_arousal":       preset["mood_arousal"],
                "mood_dominance":     preset["mood_dominance"],
                "last_update":        _time.time(),
                "state_locked_until": _time.time() + lock_hours * 3600 if lock_hours else 0.0,
            }

            from glados.autonomy.emotion_state import EmotionState
            new_state = EmotionState.from_dict(state_dict)

            if _engine is not None and _engine._emotion_agent is not None:
                _engine._emotion_agent._state = new_state
                _engine._emotion_agent._save_state()
                logger.info("Force emotion: {} applied immediately", preset.get("label"))
                self._send_json({
                    "status": "ok",
                    "preset": preset_key,
                    "label": preset.get("label"),
                })
            else:
                self._send_json({"error": "Emotion agent not available"}, 503)
        except Exception as exc:
            logger.error("Force emotion failed: {}", exc)
            self._send_json({"error": str(exc)}, 500)

    def _handle_entities(self) -> None:
        """Return the current HA entity cache for diagnostics."""
        with _ha_areas_lock:
            areas = dict(_ha_areas)
        summary = {k: {"name": v["name"], "light_count": len(v["lights"])} for k, v in areas.items()}
        self._send_json({
            "areas": summary,
            "total_areas": len(areas),
            "last_refresh": _ha_areas_last_refresh,
        })

    def _handle_doorbell_screen(self) -> None:
        global _doorbell_screener

        # Parse optional body
        data = {}
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass  # empty body is fine

        # Lazy-init screener
        if _doorbell_screener is None:
            try:
                _doorbell_screener = DoorbellScreener()
            except Exception as exc:
                logger.error("Failed to initialize DoorbellScreener: {}", exc)
                self._send_json(
                    {"error": {"message": f"Screener init failed: {exc}"}}, 500
                )
                return

        result = _doorbell_screener.start_session(
            speakers=data.get("speakers"),
            max_rounds=data.get("max_rounds"),
        )

        status = 200 if result.get("status") == "screening_started" else 429
        self._send_json(result, status)

    def _handle_models(self) -> None:
        self._send_json({
            "object": "list",
            "data": [{
                "id": "glados",
                "object": "model",
                "created": 0,
                "owned_by": "aperture-science",
            }],
        })

    def _handle_health(self) -> None:
        global _engine

        if _engine is None:
            self._send_json({"status": "starting", "engine": "initializing"}, 503)
            return

        if _engine.shutdown_event.is_set():
            self._send_json({"status": "stopping", "engine": "shutting_down"}, 503)
            return

        try:
            _ = _engine._conversation_store.version
        except Exception:
            self._send_json({"status": "starting", "engine": "initializing"}, 503)
            return

        self._send_json({"status": "ok", "engine": "running"})

    def _handle_chat_completions(self) -> None:
        global _engine

        if _engine is None or _engine.shutdown_event.is_set():
            self._send_json(
                {"error": {"message": "GLaDOS engine not ready", "type": "server_error"}},
                503,
            )
            return

        # Read request body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(
                {"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request_error"}},
                400,
            )
            return

        # Determine origin: honor X-GLaDOS-Origin from internal callers
        # (the WebUI proxies through with this header); otherwise default
        # to api_chat. Reject unknown header values by falling back to
        # api_chat so no caller can assert a fake origin.
        _hdr = (self.headers.get("X-GLaDOS-Origin") or "").strip()
        origin = _hdr if _hdr in Origin.ALL else Origin.API_CHAT

        # Streaming SSE mode — stream directly from Ollama
        if data.get("stream", False):
            messages = data.get("messages", [])
            user_message = None
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break
            if not user_message:
                self._send_json(
                    {"error": {"message": "No user message found", "type": "invalid_request_error"}},
                    400,
                )
                return

            audit(AuditEvent(
                ts=time.time(),
                origin=origin,
                kind="utterance",
                utterance=user_message,
                extra={"streaming": True},
            ))

            # Stage 3 Phase 7: single entry point. CommandResolver
            # tries HA's conversation API (Tier 1) and the LLM
            # disambiguator (Tier 2) behind one call, with session
            # memory + learned-context carry-over handled internally.
            # On miss → Tier 3 chitchat.
            if _try_tier1_fast_path(self, user_message, origin):
                return

            # Chat path goes directly to LLM. GLaDOS uses HA MCP tools
            # to control devices herself when the resolver falls through.
            _stream_chat_sse(self, _engine, user_message, max(_response_timeout, 180.0))
            return

        # Extract last user message
        messages = data.get("messages", [])
        user_message = None
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                user_message = msg.get("content", "")
                break

        if not user_message:
            self._send_json(
                {"error": {"message": "No user message found", "type": "invalid_request_error"}},
                400,
            )
            return

        audit(AuditEvent(
            ts=time.time(),
            origin=origin,
            kind="utterance",
            utterance=user_message,
            extra={"streaming": False},
        ))

        # Stage 3 Phase 7: CommandResolver handles Tier 1 + Tier 2
        # + learned-context behind one call for both streaming and
        # non-streaming paths.
        if _try_tier1_nonstreaming(self, user_message, origin):
            return

        # --- Command interceptor removed from chat path ---
        # GLaDOS uses HA MCP tools directly. Interceptor remains active only
        # for the /command endpoint (voice pipeline / ESPHome satellites).

        # engine_audio=true → let engine stream audio to HA speakers instead of
        # muting TTS and waiting for the full response.  First audio arrives in
        # ~2-3s instead of 10+s.  Return "." so HA voice pipeline doesn't also
        # TTS the response on the satellite speaker.
        # Default comes from config: tuning.engine_audio_default (true by default).
        engine_audio = data.get(
            "engine_audio", cfg.tuning.engine_audio_default,
        )

        # Serialize API requests to prevent mute/unmute races
        with _api_lock:
            response_text, request_id = _get_engine_response_with_retry(
                _engine, user_message, _response_timeout,
                engine_audio=engine_audio,
            )

        if response_text is None:
            self._send_json(
                {"error": {"message": "Response timeout", "type": "server_error"}},
                504,
            )
            return

        # When engine_audio is True the engine already streamed audio to HA
        # speakers sentence-by-sentence.  Return "." so the HA voice pipeline's
        # TTS renders near-silence on the satellite (same pattern as command
        # interceptor).
        if engine_audio:
            reply_text = "."
        else:
            from glados.core.llm_directives import strip_closing_boilerplate
            reply_text = strip_closing_boilerplate(
                _strip_thinking(response_text)
            )

        self._send_json({
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "glados",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------

def _create_engine(config_path: str, overrides: dict[str, Any]) -> Glados:
    """Create a GLaDOS engine instance from config with optional overrides."""
    config = GladosConfig.from_yaml(config_path)
    if overrides:
        config = config.model_copy(update=overrides)
    return Glados.from_config(config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GLaDOS API Wrapper — OpenAI-compatible HTTP API for the GLaDOS engine"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(_GLADOS_CONFIG_DIR / "glados_config.yaml"),
        help="Path to GLaDOS config YAML",
    )
    parser.add_argument("--port", type=int, default=8015, help="HTTP port (default: 8015)")
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--input-mode",
        choices=["audio", "text", "both"],
        default=None,
        help="Override input mode",
    )
    parser.add_argument(
        "--audio-io",
        choices=["sounddevice", "home_assistant"],
        default=None,
        help="Override audio I/O backend",
    )
    parser.add_argument(
        "--tts-enabled",
        dest="tts_enabled",
        action="store_true",
        default=None,
        help="Enable TTS audio output",
    )
    parser.add_argument(
        "--tts-disabled",
        dest="tts_enabled",
        action="store_false",
        help="Disable TTS audio output",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Response timeout in seconds (default: 45)",
    )
    parser.add_argument(
        "--announcement",
        type=str,
        default=None,
        help="Override startup announcement (empty string to disable)",
    )
    return parser.parse_args()


def main() -> None:
    global _engine, _response_timeout

    args = _parse_args()
    _response_timeout = args.timeout

    # Build config overrides from CLI flags
    overrides: dict[str, Any] = {}
    if args.input_mode is not None:
        overrides["input_mode"] = args.input_mode
    if args.audio_io is not None:
        overrides["audio_io"] = args.audio_io
    if args.tts_enabled is not None:
        overrides["tts_enabled"] = args.tts_enabled
    if args.announcement is not None:
        overrides["announcement"] = args.announcement if args.announcement else None

    # Create engine on main thread (MCP stdio servers need main thread's event loop)
    logger.info(f"Creating GLaDOS engine from {args.config}")
    if overrides:
        logger.info(f"Config overrides: {overrides}")

    # Remember the startup path + overrides so config-save hot-reload
    # (reload_engine()) can recreate the engine with identical CLI context.
    global _engine_config_path, _engine_overrides
    _engine_config_path = args.config
    _engine_overrides = dict(overrides)

    _engine = _create_engine(args.config, overrides)

    # Load attitude directives for response variety
    attitudes_path = _GLADOS_CONFIG_DIR / "attitudes.json"
    if attitudes_path.exists():
        try:
            load_attitudes(attitudes_path)
        except Exception as exc:
            logger.warning(f"Failed to load attitudes config: {exc}")
    else:
        logger.warning(f"Attitudes config not found at {attitudes_path}")

    # Initialize weather cache for Chat path context injection
    from glados.core import weather_cache
    from glados.core.context_gates import configure as _gates_configure
    weather_cache_path = _GLADOS_DATA / "weather_cache.json"
    weather_cache.configure(weather_cache_path)
    _gates_configure(_GLADOS_CONFIG_DIR / "context_gates.yaml")

    # Discover HA entities on startup
    try:
        _refresh_ha_entities()
    except Exception as exc:
        logger.warning(f"Initial HA entity discovery failed (will retry): {exc}")

    # Start periodic HA entity refresh in background
    entity_thread = threading.Thread(
        target=_ha_entity_refresh_loop,
        name="HAEntityRefresh",
        daemon=True,
    )
    entity_thread.start()

    # Start HTTP server in background thread
    server = ThreadingHTTPServer((args.host, args.port), APIHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="APIServer",
        daemon=True,
    )
    server_thread.start()
    logger.success(f"GLaDOS API Wrapper listening on {args.host}:{args.port}")

    # Run engine on main thread, looping across hot-reloads. `_engine` is a
    # module global; reload_engine() may set it to None briefly and then to
    # a fresh Glados instance. When engine.run() returns because the reload
    # triggered the old instance's shutdown_event, we loop to pick up the
    # new one rather than letting main() fall through and terminate the
    # process (which docker would then restart, defeating the point of
    # hot-reload).
    try:
        while True:
            current = _engine
            if current is None:
                # Reload in progress — wait for the swap to complete.
                time.sleep(0.1)
                continue
            try:
                if current.announcement:
                    current.play_announcement()
                current.run()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("Engine run crashed; attempting continuation")
                # If the crash wasn't a reload, exit so docker restarts us.
                if _engine is current:
                    raise
            # engine.run() returned. Three cases:
            #   - _engine is still `current` and shutdown_event is set:
            #     this is a real shutdown (SIGTERM / KeyboardInterrupt path).
            #   - _engine is None: mid-reload. Loop and wait.
            #   - _engine is a different instance: reload completed. Loop
            #     and run the new engine.
            if _engine is current:
                break
            # else: reload — loop around to run the new engine (or wait for it)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        logger.info("Shutting down...")
        final = _engine
        if final is not None:
            try:
                final.shutdown_event.set()
            except Exception as exc:
                logger.debug("Final shutdown_event.set raised: {}", exc)
        server.shutdown()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()

