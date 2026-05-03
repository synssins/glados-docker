"""
LLM-driven message summarization for conversation compaction.

Uses the LLM to summarize messages and extract facts, following
the LLM-first principle (complex reasoning in prompts, not code).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from .llm_client import LLMConfig, llm_call
from .token_estimator import TokenEstimator, get_default_estimator

if TYPE_CHECKING:
    pass


# Patterns identifying transient/queryable-on-demand state that should never
# be canonized as long-term memory. The compaction LLM (small triage model)
# can produce summaries describing tool errors or current library/database
# state; if those land in ChromaDB they get injected into future chat
# context as canon and the model trusts them over re-querying. Belt-and-
# suspenders to the prompt-side EXCLUDE list — applied at the storage
# boundary in extract_facts() and summarize_messages().
#
# Real-world failure case the filter catches (operator-flagged 2026-05-03):
# `radarr_get_movies` failed transiently with "database is locked"; the
# assistant replied "your library has no movies"; compaction summarized
# that into ChromaDB as a fact; future chats then saw "no movies in your
# library" injected and the model picked the wrong tool / confirmed the
# phantom emptiness.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    r"database is locked",
    r"(?:database|library|file|server|service) is locked",
    r"currently unavailable",
    r"cannot access (?:your |the )?(?:library|database|files?|movies?|server)",
    r"failed to (?:retrieve|access|connect|reach)",
    r"(?:radarr|sonarr|tautulli|plex).{0,40}(?:locked|unavailable|error|temporarily)",
    r"(?:is|are) not (?:currently )?(?:in|available in) your library",
    r"no movies (?:are )?(?:currently )?(?:in|available in) your library",
    r"system does not (?:currently )?have any (?:movies|shows|titles)",
    r"temporary issue with .{0,30}(?:database|service|server|api)",
    r"(?:movie|tv|media) database is temporarily",
    r"^Is .{1,80} in your.*library\?$",  # user questions captured as facts
)
_TRANSIENT_RE = re.compile(
    "|".join(f"(?:{p})" for p in _TRANSIENT_PATTERNS),
    re.IGNORECASE,
)


def _is_transient_state(text: str | None) -> bool:
    """True if `text` describes transient/queryable state that should not be
    written to long-term memory (tool error, current data-source contents,
    user question accidentally captured as a fact)."""
    if not text:
        return False
    return bool(_TRANSIENT_RE.search(text))


def estimate_tokens(
    messages: list[dict[str, Any]],
    estimator: TokenEstimator | None = None,
) -> int:
    """
    Estimate token count for messages.

    Args:
        messages: List of message dicts to estimate tokens for.
        estimator: Optional token estimator. Uses default if not provided.

    Returns:
        Estimated token count.
    """
    if estimator is None:
        estimator = get_default_estimator()
    return estimator.estimate(messages)


def summarize_messages(
    messages: list[dict[str, Any]],
) -> str | None:
    """
    Use LLM to summarize a list of conversation messages.

    Routes to the ``llm_triage`` service slot — pure summarization is a
    perfect fit for the small fast triage model (no persona involvement).

    Returns a concise summary preserving key information.
    """
    if not messages:
        return None

    # Format messages for the prompt
    formatted = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            formatted.append(f"{role}: {content}")
        elif isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            text = " ".join(text_parts).strip()
            if text:
                formatted.append(f"{role}: {text}")

    if not formatted:
        return None

    conversation = "\n".join(formatted)

    system_prompt = """You are a summarization assistant. Summarize the conversation below.

Rules:
- Be concise but preserve important context
- Mention key topics discussed
- Note any decisions made or tasks assigned
- Mention any facts learned about the user
- Keep to 2-4 sentences maximum

DO NOT include in the summary:
- Tool errors or transient API failures (e.g. "database is locked", "service is unavailable")
- Current contents of queryable services: media library (Radarr/Sonarr/Plex movies/shows),
  sensor states, calendar events, current weather, current time
These are looked up on demand, not memorized."""

    user_prompt = f"Summarize this conversation:\n\n{conversation}"

    llm_config = LLMConfig.for_slot("llm_triage")
    response = llm_call(llm_config, system_prompt, user_prompt)
    if not response:
        return response
    if _is_transient_state(response):
        logger.debug(
            "summarize_messages: dropping transient-state summary ({} chars)",
            len(response),
        )
        return None
    logger.debug("Summarized {} messages into summary", len(messages))
    return response


def extract_facts(
    messages: list[dict[str, Any]],
) -> list[str]:
    """
    Use LLM to extract factual information from messages.

    Routes to the ``llm_triage`` service slot — fact extraction is a
    classification task that belongs on the small fast triage model.

    Returns a list of discrete facts worth remembering.
    """
    if not messages:
        return []

    # Noise patterns to exclude from fact extraction input
    _NOISE_PREFIXES = (
        "[emotion]", "[memory]", "[summary]", "[behavior",
        "autonomy update", "camera ", "scene ", "seconds since",
        "message compaction", "behavior observer", "emotional state",
        "ha sensor", "weather ->", "slot update",
    )

    # Format messages for the prompt — skip internal system/autonomy content
    formatted = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        lower = content.lower()
        if any(lower.startswith(p) or p in lower[:80] for p in _NOISE_PREFIXES):
            continue
        formatted.append(f"{role}: {content}")

    if not formatted:
        return []

    conversation = "\n".join(formatted)

    system_prompt = """You extract important personal facts from conversations for long-term memory storage.

Output one fact per line in third person (e.g. "Alex prefers..." not "I prefer...").
Only extract facts that are worth remembering across sessions — months from now.

INCLUDE:
- Personal details (name, age, relationships, health, injuries, preferences)
- Household details (people, pets, locations, regular routines)
- Professional context (job, skills, tools used)
- Significant events (deaths, milestones, decisions)

EXCLUDE — do not extract these, they are not worth storing:
- Weather conditions (temperature, rain, wind — ephemeral)
- System state (camera status, emotional state, compaction status)
- Autonomy updates or internal monitoring messages
- Anything starting with [emotion], [summary], or [memory]
- The AI's own capabilities or how the system works
- Vague or abstract statements that lack specific factual content
- Conversational filler or pleasantries
- Tool errors or transient API failures (e.g. "database is locked", "service is unavailable",
  "cannot access X right now") — these describe momentary state, not permanent facts
- Current state of queryable services: media library contents (movies/shows in
  Radarr/Sonarr/Plex), sensor states, calendar events, current weather, current time.
  These are looked up on demand, never memorized.

If no facts worth storing are present, output exactly: NONE
Do not output "No important facts" or any other explanation — just NONE."""

    user_prompt = f"Extract facts from this conversation:\n\n{conversation}"

    llm_config = LLMConfig.for_slot("llm_triage")
    response = llm_call(llm_config, system_prompt, user_prompt)
    if not response:
        return []

    # Parse response into list of facts
    facts = []
    for line in response.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Skip if LLM returned the NONE sentinel or a variation
        if line.upper() in ("NONE", "NONE.", "NO FACTS", "NO IMPORTANT FACTS"):
            return []
        # Remove bullet points if present
        if line.startswith("- "):
            line = line[2:]
        elif line.startswith("* "):
            line = line[2:]
        if line and len(line) > 10:  # ignore stubs
            facts.append(line)

    # Storage-boundary safety net — drop facts describing transient state
    # (tool errors, current library contents). These become poison pills if
    # injected into future chat context: model trusts the stale fact over
    # re-querying the source. See _is_transient_state docstring above.
    pre_filter_count = len(facts)
    facts = [f for f in facts if not _is_transient_state(f)]
    if len(facts) < pre_filter_count:
        logger.debug(
            "extract_facts: filtered {} transient-state fact(s) ({} -> {})",
            pre_filter_count - len(facts), pre_filter_count, len(facts),
        )

    logger.debug("Extracted {} facts from {} messages", len(facts), len(messages))
    return facts
