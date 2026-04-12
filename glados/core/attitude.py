"""
Attitude directive system for GLaDOS response variety.

Loads a pool of attitude directives from the centralized personality
config (``configs/personality.yaml``), each paired with TTS synthesis
parameters. On every LLM turn, one is randomly selected and injected
as a system message.

Thread-safe: uses threading.local() so each thread (engine LLM
processor, HTTP handler, etc.) has independent attitude state.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# ── Module state ──────────────────────────────────────────────────────

_attitudes: list[dict[str, Any]] = []
_weights: list[float] = []
_default_tts: dict[str, float] = {"length_scale": 1.0, "noise_scale": 0.667, "noise_w": 0.8}
_loaded = False
_lock = threading.Lock()

# Per-thread current attitude
_thread_local = threading.local()


# ── Public API ────────────────────────────────────────────────────────

def load_attitudes(path: str | Path) -> None:
    """Load attitude definitions from a JSON or YAML file.

    Args:
        path: Path to attitudes config file (JSON or YAML).

    Raises:
        FileNotFoundError: If the config file doesn't exist.
    """
    global _attitudes, _weights, _default_tts, _loaded

    path = Path(path)
    with open(path, encoding="utf-8") as f:
        if path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(f) or {}
        else:
            data = json.load(f)

    attitudes = data.get("attitudes", [])
    if not attitudes:
        logger.warning("Attitude config at {} has no attitudes defined", path)
        return

    # Normalise: YAML uses nested dicts for tts, JSON uses the same format
    normalised = []
    for a in attitudes:
        entry = dict(a)
        # Ensure tts is a plain dict (Pydantic models may have been serialised)
        if "tts" in entry and hasattr(entry["tts"], "model_dump"):
            entry["tts"] = entry["tts"].model_dump()
        normalised.append(entry)

    with _lock:
        _attitudes = normalised
        _weights = [a.get("weight", 1.0) for a in normalised]
        _default_tts = data.get("default_tts", _default_tts)
        # default_tts may also be a Pydantic model
        if hasattr(_default_tts, "model_dump"):
            _default_tts = _default_tts.model_dump()
        _loaded = True

    logger.info("Loaded {} attitude directives from {}", len(normalised), path)


def roll_attitude() -> dict[str, Any]:
    """Randomly select an attitude (weighted) and store it as the current
    thread's active attitude.

    Returns:
        The selected attitude dict (tag, directive, tts, etc.).
        Returns an empty dict if no attitudes are loaded.
    """
    with _lock:
        if not _attitudes:
            return {}
        attitude = random.choices(_attitudes, weights=_weights, k=1)[0]

    _thread_local.current = attitude
    logger.debug("Attitude rolled: {}", attitude.get("tag", "unknown"))
    return attitude


def get_current_attitude() -> dict[str, Any] | None:
    """Get the current thread's active attitude, if any."""
    return getattr(_thread_local, "current", None)


def set_attitude(tag: str) -> dict[str, Any] | None:
    """Force a specific attitude by tag name.

    Args:
        tag: The attitude tag to set (e.g. "quiet_menace").

    Returns:
        The attitude dict, or None if tag not found.
    """
    with _lock:
        for attitude in _attitudes:
            if attitude.get("tag") == tag:
                _thread_local.current = attitude
                logger.debug("Attitude set: {}", tag)
                return attitude
    logger.warning("Attitude tag '{}' not found", tag)
    return None


def get_attitude_directive() -> str | None:
    """Get the current thread's attitude directive string.

    Returns:
        The directive text, or None if no attitude is active.
    """
    attitude = get_current_attitude()
    if attitude:
        return attitude.get("directive")
    return None


def get_tts_params() -> dict[str, float]:
    """Get TTS synthesis parameters for the current attitude.

    Returns:
        Dict with length_scale, noise_scale, noise_w.
        Falls back to default_tts if no attitude is active.
    """
    attitude = get_current_attitude()
    if attitude:
        tts = attitude.get("tts")
        if tts:
            return dict(tts)  # Return a copy
    with _lock:
        return dict(_default_tts)


def list_attitudes() -> list[dict[str, Any]]:
    """Return all loaded attitudes (for API/dropdown).

    Returns:
        List of attitude dicts with tag, label, directive, tts params.
    """
    with _lock:
        return list(_attitudes)


def get_default_tts() -> dict[str, float]:
    """Return the default TTS parameters."""
    with _lock:
        return dict(_default_tts)


def is_loaded() -> bool:
    """Check if attitudes have been loaded."""
    return _loaded
