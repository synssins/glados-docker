"""Per-turn system-message guards shared between the SSE and engine paths.

The SSE path in ``api_wrapper._stream_chat_sse_impl`` builds its own
messages array and injects these guards inline. The non-streaming
path submits raw text to the engine's priority queue, which builds
messages via ``ContextBuilder``; ``guard_prompt_for_turn`` registers
as a ``context_builder`` callback so the same guard lands for every
non-streaming / voice call too.

Without this, non-streaming turns went to the LLM with all MCP tools
visible and no chitchat restraint — ``"Tell me about the testing
tracks"`` hallucinated a ``testing_tracks`` tool and returned a
corporate-sounding refusal, while chitchat-style inputs ("hey you",
"thanks") triggered narrated fake device actions.
"""
from __future__ import annotations

from glados.intent.rules import looks_like_home_command


# Chitchat turns: no tool ran this turn. The guard blocks fabricated
# device actions and hallucinated sensor data, but explicitly permits
# reporting facts supplied in preceding system messages (weather
# cache, memory, canon RAG). Earlier wording read "do not invent
# sensor readings or system status" and the 14B read that as "stay
# silent" even when asked `"What's the weather?"` with a populated
# weather_cache block. No sample forbidden phrases listed — that
# seeds the model toward exactly those phrasings.
CHITCHAT_GUARD = (
    "No home-control tool ran this turn. Do not claim any "
    "device, thermostat, scene, or switch was changed. Do "
    "not fabricate sensor readings or room states that do "
    "not appear in earlier system messages. You MAY quote "
    "or paraphrase information that IS provided in earlier "
    "system messages (weather cache, memory facts, canon "
    "entries) — that is the intended use. Answer the user "
    "in one or two sentences. No sign-off line."
)


# Home-command turns: tools are available, but the 14B likes to
# call broad state-dump tools and narrate the entire inventory.
# Steer toward the targeted in-process tools and cap the answer.
HOME_COMMAND_GUARD = (
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


def guard_for_message(user_message: str) -> str:
    """Return the guard text appropriate for this user message.

    Home-command check uses the same precheck that SSE uses so the
    two paths can't disagree on classification.
    """
    if looks_like_home_command(user_message or ""):
        return HOME_COMMAND_GUARD
    return CHITCHAT_GUARD
