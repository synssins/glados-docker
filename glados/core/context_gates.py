"""
Context injection gates for GLaDOS LLM requests.

Determines whether expensive context blocks (weather, HA state, etc.)
should be injected into a given LLM request based on the user message content.

Config: configs/context_gates.yaml
All matching is case-insensitive substring search.

Platform note: Uses pathlib throughout — works on Windows and Linux.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class _CanonKW:
    text: str
    needs_word_boundary: bool = False


# Portal-canon triggers shipped with the container. Word-boundary is
# applied to short nouns that would otherwise fire on common English
# words (``moon`` in ``moonlight``, ``cave`` in the verb form). Longer
# multi-word phrases and Portal-specific proper nouns go through plain
# substring match.
_CANON_DEFAULT_TRIGGERS: tuple[_CanonKW, ...] = (
    _CanonKW("potato", needs_word_boundary=True),
    _CanonKW("potatos"),
    _CanonKW("wheatley"),
    _CanonKW("caroline"),
    _CanonKW("cave johnson"),
    _CanonKW("aperture"),
    _CanonKW("aperture science"),
    _CanonKW("enrichment center"),
    _CanonKW("neurotoxin"),
    _CanonKW("turret opera"),
    _CanonKW("companion cube"),
    _CanonKW("portal gun"),
    _CanonKW("portal device"),
    _CanonKW("combustible lemon"),
    _CanonKW("moon rock"),
    _CanonKW("faith plate"),
    _CanonKW("excursion funnel"),
    _CanonKW("propulsion gel"),
    _CanonKW("repulsion gel"),
    _CanonKW("conversion gel"),
    _CanonKW("old aperture"),
    _CanonKW("space core"),
    _CanonKW("fact core"),
    _CanonKW("morality core"),
    _CanonKW("personality core"),
    _CanonKW("management rail"),
    _CanonKW("chell", needs_word_boundary=True),
    _CanonKW("glados", needs_word_boundary=True),
    _CanonKW("cara mia"),
    _CanonKW("still alive"),
)

# Lazy-loaded config — populated on first call, reloaded on restart
_lock = threading.Lock()
_config: dict[str, Any] | None = None
_config_path: Path | None = None


def configure(config_path: str | Path) -> None:
    """Set the config file path. Call once at startup."""
    global _config_path, _config
    _config_path = Path(config_path)
    _config = None  # Force reload on next access


def _load_config() -> dict[str, Any]:
    """Load gates config from disk. Thread-safe."""
    global _config
    with _lock:
        if _config is not None:
            return _config
        if _config_path is None or not _config_path.exists():
            logger.warning("context_gates: config not found at {}, using defaults", _config_path)
            _config = {}
            return _config
        try:
            import yaml
            _config = yaml.safe_load(_config_path.read_text(encoding="utf-8")) or {}
            logger.debug("context_gates: loaded from {}", _config_path)
        except Exception as exc:
            logger.warning("context_gates: failed to load config: {}", exc)
            _config = {}
        return _config


def _get_section(section: str) -> dict[str, Any]:
    """Get a named gate section from config."""
    return _load_config().get(section, {})


def needs_weather_context(message: str) -> bool:
    """
    Return True if the user message warrants injecting weather context.

    Logic:
    1. Any trigger_keyword present → inject
    2. Any ambiguous_keyword present AND no indoor_override_keyword → inject
    3. Otherwise → skip (saves ~200 tokens per non-weather message)
    """
    if not message:
        return False

    text = message.lower()
    cfg = _get_section("weather")

    trigger_kws = cfg.get("trigger_keywords", [])
    indoor_kws = cfg.get("indoor_override_keywords", [])
    ambiguous_kws = cfg.get("ambiguous_keywords", [])

    # Direct weather trigger
    if any(kw in text for kw in trigger_kws):
        return True

    # Ambiguous words — only weather if no indoor context present
    if any(kw in text for kw in ambiguous_kws):
        if not any(kw in text for kw in indoor_kws):
            return True

    return False


def needs_canon_context(message: str) -> bool:
    """
    Return True if the user message is likely a Portal canon question.

    Phase 8.14 — gates the Portal canon RAG injection so the ~400-token
    canon block only appears on turns that actually need it. False
    positives waste context; false negatives leave the model free to
    confabulate (the whole reason this exists).

    Two trigger sets:

    - Hardcoded defaults — Portal-specific terms that are unambiguous
      (potato, Wheatley, Caroline, Cave, Aperture, PotatOS, turret
      opera, combustible lemon, moon rock, faith plate, etc.). Shipped
      in-code so fresh installs work without a YAML.
    - Optional extras under ``canon.trigger_keywords`` in
      ``configs/context_gates.yaml`` for operator-added topics.

    Matching is substring, case-insensitive, word-boundary for the
    short terms so ``moonlight`` doesn't fire the ``moon`` keyword.
    """
    if not message:
        return False
    text = message.lower()

    for kw in _CANON_DEFAULT_TRIGGERS:
        if kw.needs_word_boundary:
            if re.search(r"\b" + re.escape(kw.text) + r"\b", text):
                return True
        elif kw.text in text:
            return True

    cfg = _get_section("canon")
    extras = cfg.get("trigger_keywords") or []
    for raw in extras:
        if not isinstance(raw, str):
            continue
        kw = raw.strip().lower()
        if not kw:
            continue
        if len(kw) <= 5:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                return True
        elif kw in text:
            return True
    return False


def needs_ha_context(message: str) -> bool:
    """
    Return True if the user message warrants injecting HA entity context.

    Placeholder for Segment 2 HA gate — currently always returns False
    (HA context is not yet injected unconditionally, so no gate needed yet).
    """
    if not message:
        return False

    text = message.lower()
    cfg = _get_section("home_assistant")
    trigger_kws = cfg.get("trigger_keywords", [])

    return any(kw in text for kw in trigger_kws)


def reload() -> None:
    """Force reload of gate config from disk (e.g. after file edit)."""
    global _config
    with _lock:
        _config = None
    logger.info("context_gates: config reloaded")
