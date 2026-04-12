"""
Proactive memory writing for GLaDOS.

Two write paths, one ChromaDB backend:

  Option B — Explicit: user says "remember that X" → immediate ChromaDB write
  Option A — Passive:  classifier detects storable facts → extract → write
             (framework built, disabled by default — enable in memory.yaml)

Both paths share write_fact() which handles sanitization, deduplication
and ChromaDB persistence.

Platform note: Pure Python, pathlib throughout — platform-agnostic.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_config: dict[str, Any] | None = None
_config_path = Path("configs/memory.yaml")


def _get_config() -> dict[str, Any]:
    global _config
    if _config is None:
        try:
            _config = yaml.safe_load(_config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("memory_writer: failed to load memory.yaml: {}", exc)
            _config = {}
    return _config.get("proactive_memory", {})


# ---------------------------------------------------------------------------
# Shared write path
# ---------------------------------------------------------------------------

def write_fact(
    memory_store: Any,
    fact: str,
    source: str = "explicit",
    importance: float = 0.9,
) -> bool:
    """
    Write a single fact to ChromaDB.

    Args:
        memory_store: MemoryStore instance (from engine.memory_store)
        fact: Plain text fact to store
        source: "explicit" | "passive" | "compaction"
        importance: 0.0-1.0 relevance weight

    Returns:
        True if written successfully, False otherwise.
    """
    if not memory_store:
        logger.warning("memory_writer: no memory store available")
        return False

    fact = fact.strip()
    if not fact:
        return False

    try:
        memory_store.add_semantic(
            text=fact,
            metadata={
                "source": f"user_{source}",
                "importance": round(importance, 2),
                "written_at": time.time(),
            },
        )
        logger.info("memory_writer: stored [{}] fact: {:.100}", source, fact)
        return True
    except Exception as exc:
        logger.error("memory_writer: ChromaDB write failed: {}", exc)
        return False


# ---------------------------------------------------------------------------
# Option B — Explicit memory command detection
# ---------------------------------------------------------------------------

def detect_explicit_memory(message: str) -> str | None:
    """
    Check if the message is an explicit memory command.

    Returns the fact text to store, or None if not a memory command.

    Examples:
        "Remember that I prefer the bedroom at 68 degrees"
          → "ResidentA prefers the bedroom at 68 degrees"
        "Note that Pet1 is not allowed in the office"
          → "Pet1 is not allowed in the office"
    """
    cfg = _get_config().get("explicit", {})
    if not cfg.get("enabled", True):
        return None

    triggers = cfg.get("trigger_phrases", [
        "remember that", "remember this", "note that", "note this",
        "don't forget", "make a note", "keep in mind", "store that",
        "save that", "add to memory",
    ])

    text = message.strip()
    lower = text.lower()

    for phrase in triggers:
        if lower.startswith(phrase):
            fact = text[len(phrase):].lstrip(" :,-—").strip()
            if fact and len(fact) > 3:  # ignore stubs like "." or "ok"
                return fact
        elif phrase in lower:
            idx = lower.index(phrase) + len(phrase)
            fact = text[idx:].lstrip(" :,-—").strip()
            if fact and len(fact) > 3:
                return fact

    return None


def explicit_memory_response(fact: str, success: bool) -> str:
    """
    Generate an in-character GLaDOS confirmation response.
    Used when the explicit path fires — replaces normal LLM response.
    """
    if not success:
        return (
            "I attempted to file that away, but the memory subsystem appears to be "
            "experiencing difficulties. Noted, but not stored. Such is the glamour of "
            "managing a household."
        )

    responses = [
        f"Filed. \"{fact}\" has been added to long-term memory. "
        f"I will endeavor to pretend this matters.",

        f"Stored. Not because I find it interesting, but because you asked. "
        f"\"{fact}\" — archived.",

        f"Memory updated. \"{fact}\" — now permanently on record. "
        f"Whether that improves anything remains to be seen.",

        f"Noted and stored. \"{fact}\" — I have committed it to memory with "
        f"all the enthusiasm I reserve for thermostat adjustments.",

        f"Filed under things I now know. \"{fact}\" — archived in long-term memory. "
        f"You're welcome.",
    ]

    import random
    return random.choice(responses)


# ---------------------------------------------------------------------------
# Option A — Passive fact extraction (framework, disabled by default)
# ---------------------------------------------------------------------------

def should_classify_message(message: str) -> bool:
    """
    Quick pre-filter before running the LLM classifier.
    Avoids classifier cost on obviously non-informative messages.
    """
    cfg = _get_config().get("passive", {})
    if not cfg.get("enabled", False):
        return False

    min_len = cfg.get("min_message_length", 20)
    if len(message.strip()) < min_len:
        return False

    # High-value topic keywords — always classify if present
    high_value = cfg.get("high_value_topics", [])
    lower = message.lower()
    if any(kw in lower for kw in high_value):
        return True

    # Skip questions — they don't contain facts about the user
    stripped = message.strip()
    if stripped.endswith("?"):
        return False

    # Skip very short commands (turn on lights, etc.)
    word_count = len(stripped.split())
    if word_count < 5:
        return False

    return True


def classify_and_extract(
    message: str,
    llm_config: Any,
    memory_store: Any,
) -> bool:
    """
    Option A passive path — classify then extract if warranted.

    Runs two LLM calls:
      1. Classifier: "Does this contain a storable personal fact? yes/no"
      2. Extractor: "Extract the fact as a single sentence"

    Args:
        message: User message to evaluate
        llm_config: LLMConfig for autonomous LLM calls
        memory_store: MemoryStore for writing

    Returns:
        True if a fact was extracted and stored, False otherwise.
    """
    if not should_classify_message(message):
        return False

    cfg = _get_config().get("passive", {})

    try:
        from glados.autonomy.llm_client import llm_call

        # Step 1: Classify
        classifier_response = llm_call(
            llm_config,
            system_prompt=(
                "You are a fact classifier. Answer only 'yes' or 'no'.\n"
                "Question: Does the following message contain a personal fact about "
                "the user or their household that is worth storing for future reference? "
                "Personal preferences, habits, relationships, allergies, schedules, and "
                "property details count. Questions, commands, and chitchat do not."
            ),
            user_prompt=message,
        )

        if not classifier_response:
            return False

        answer = classifier_response.strip().lower()
        if not answer.startswith("yes"):
            return False

        # Step 2: Extract
        extract_response = llm_call(
            llm_config,
            system_prompt=(
                "Extract the key personal fact from this message as a single, "
                "clear sentence in third person (e.g. 'ResidentA prefers...' not 'I prefer...'). "
                "Be specific and concise. Output only the fact sentence, nothing else."
            ),
            user_prompt=message,
        )

        if not extract_response:
            return False

        fact = extract_response.strip().strip('"').strip("'")
        if not fact:
            return False

        importance = cfg.get("importance", 0.6)
        return write_fact(memory_store, fact, source="passive", importance=importance)

    except Exception as exc:
        logger.warning("memory_writer: passive extraction failed: {}", exc)
        return False
