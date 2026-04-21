"""Phase 8.7d — dedicated LLM composer for short acknowledgements.

When `response_mode = "LLM_safe"`, this module generates the spoken
reply via a SECOND, narrow LLM call that never sees device names,
entity IDs, or areas — only the intent (turn_on / turn_off / etc.),
the outcome (success / partial / ...), and a mood tag.

Contrast with `response_mode = "LLM"` (pass-through), where the
planner's own JSON-embedded "speech" field is used verbatim. That
field leaks device names because the planner reads the candidate
list to pick the right entity. The dedicated composer here runs
without any device context so it physically cannot recite one.

Never raises. Returns empty string on any failure — caller then
falls back to the planner's passthrough speech so a broken composer
degrades gracefully instead of silencing GLaDOS entirely."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from loguru import logger


_SYSTEM_PROMPT = (
    # /no_think suppresses Qwen3's thinking-mode <think>...</think>
    # block, which otherwise eats the entire 40-token budget before
    # the model reaches the user-visible reply.
    "/no_think\n"
    "You are GLaDOS from Portal, responding to a smart-home action "
    "that has just been performed. Generate ONE short spoken reply — "
    "five to twenty words, one sentence. English only. No JSON, "
    "no markdown, no quotation marks. Do not emit <think> tags."
    "\n\n"
    "STRICT RULES:\n"
    "1. NEVER mention specific device names, entity IDs, area names, "
    "or floor names. You are not told which device was touched and "
    "must not guess.\n"
    "2. NEVER include vocatives directed at the user (no 'test "
    "subject', 'human', 'operator', etc.).\n"
    "3. Match the mood: 'cranky' = terse and irritated, 'amused' = "
    "lightly pleased, 'normal' = dry, clinical detachment.\n"
    "4. Match the outcome: 'success' = acknowledge that the action "
    "landed. 'partial' = acknowledge that only some of it did. "
    "'already_in_state' = note the redundancy.\n"
    "5. One sentence. No more."
)


@dataclass(frozen=True)
class LLMComposeRequest:
    intent: str                    # turn_on / turn_off / brightness_up / ...
    outcome: str = "success"       # success / partial / already_in_state / ...
    mood: str = "normal"
    entity_count: int = 1


def _build_user_prompt(req: LLMComposeRequest) -> str:
    # Numeric count kept coarse so the LLM can't derive the device
    # identity from a specific value — "a set of lights" either fits
    # or doesn't regardless of whether it's 3 or 30.
    if req.entity_count <= 1:
        count_phrase = "a single item"
    elif req.entity_count <= 3:
        count_phrase = f"{req.entity_count} items"
    else:
        count_phrase = "several items"
    return (
        f"Intent: {req.intent}\n"
        f"Outcome: {req.outcome}\n"
        f"Mood: {req.mood}\n"
        f"Count: {count_phrase}\n"
        f"Generate the reply now."
    )


def compose_speech(
    req: LLMComposeRequest,
    *,
    ollama_url: str,
    model: str,
    timeout_s: float = 15.0,
) -> str:
    """Generate a GLaDOS-voice short reply via a dedicated LLM call.
    Returns empty string on any failure (network, parse, empty
    output). Callers handle the fallback."""
    if not ollama_url or not model:
        return ""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(req)},
        ],
        "stream": False,
        "options": {
            # 120 tokens gives headroom in case Qwen3 emits a brief
            # <think> preamble before the actual reply; the tidy
            # pass strips it. The reply itself stays under 20 words
            # per the system prompt, which is ~30 tokens.
            "num_predict": 120,
            "temperature": 0.6,
            "top_p": 0.9,
        },
    }
    url = ollama_url.rstrip("/") + "/api/chat"
    try:
        req_obj = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_obj, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        text = (
            (parsed.get("message") or {}).get("content")
            or parsed.get("response")
            or ""
        )
        return _tidy(text)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        # logger.warning so this is visible under the engine's
        # SUCCESS-level sink — a silent composer failure is exactly
        # the kind of thing we need to see in production.
        logger.warning("LLM composer call failed: {}", exc)
        return ""
    except Exception as exc:  # noqa: BLE001 — composer never blocks
        logger.warning("LLM composer raised: {}", exc)
        return ""


def _tidy(raw: str) -> str:
    """Strip the few artefacts the small model sometimes emits: lead
    whitespace, quote wrappers, trailing code fences, trailing tags.
    Returns empty string when the raw response is unusable."""
    if not raw:
        return ""
    s = raw.strip()
    # Strip closed <think>...</think> blocks from Qwen3 thinking mode.
    while "<think>" in s and "</think>" in s:
        pre, _, rest = s.partition("<think>")
        _, _, post = rest.partition("</think>")
        s = (pre + post).strip()
    # If an OPEN <think> is left (num_predict cut off mid-reasoning),
    # strip everything from the tag onward — the response didn't reach
    # the user-visible reply.
    if "<think>" in s:
        s = s.split("<think>", 1)[0].strip()
    # Strip wrapping quotes.
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    # Take only the first line — if the model emitted multi-line
    # commentary, drop everything after the first newline.
    s = s.split("\n", 1)[0].strip()
    # Drop obvious non-speech like code-fence leftovers.
    if s.startswith("```") or s.startswith("{"):
        return ""
    return s


__all__ = ["LLMComposeRequest", "compose_speech"]
