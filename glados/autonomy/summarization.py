"""
LLM-driven message summarization for conversation compaction.

Uses the LLM to summarize messages and extract facts, following
the LLM-first principle (complex reasoning in prompts, not code).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from .llm_client import LLMConfig, llm_call
from .token_estimator import TokenEstimator, get_default_estimator

if TYPE_CHECKING:
    pass


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
    llm_config: LLMConfig,
) -> str | None:
    """
    Use LLM to summarize a list of conversation messages.

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
- Keep to 2-4 sentences maximum"""

    user_prompt = f"Summarize this conversation:\n\n{conversation}"

    response = llm_call(llm_config, system_prompt, user_prompt)
    if response:
        logger.debug("Summarized {} messages into summary", len(messages))
    return response


def extract_facts(
    messages: list[dict[str, Any]],
    llm_config: LLMConfig,
) -> list[str]:
    """
    Use LLM to extract factual information from messages.

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

Output one fact per line in third person (e.g. "Chris prefers..." not "I prefer...").
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

If no facts worth storing are present, output exactly: NONE
Do not output "No important facts" or any other explanation — just NONE."""

    user_prompt = f"Extract facts from this conversation:\n\n{conversation}"

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

    logger.debug("Extracted {} facts from {} messages", len(facts), len(messages))
    return facts
