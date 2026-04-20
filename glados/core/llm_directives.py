"""Model-family directive injection for outbound LLM requests.

Some model families (Qwen3, DeepSeek R1, …) enter a reasoning / thinking
mode by default, producing a `<think>…</think>` prelude that burns
output tokens and slows user-visible responses. Qwen3 accepts a
`/no_think` directive in the system or user prompt to suppress thinking
mode for a given turn.

This module centralises the detection + injection so every outbound
Ollama call (Tier 2 disambiguator, persona rewriter, Tier 3 agentic
loop, doorbell screener, autonomy subagents) can apply the same logic
without duplicating family-detection or prompt-rewrite code.

Non-Qwen3 models receive the messages unchanged — the `/no_think`
token is literal Qwen3 control syntax, not a universal convention.
"""

from __future__ import annotations

import re
from typing import Any

# Post-response stripper. `format: json` on Ollama already removes the
# think tags (the JSON schema constraint kicks in after the tags close),
# but plain-format callers — the persona rewriter and most autonomy
# subagents — receive the raw `<think>\n\n</think>\n\n<answer>` shape
# when /no_think is active. Downstream JSON parsers choke on the empty
# think wrapper, so strip it before handing off.
_THINKING_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_STRAY_THINK_TAG_RE = re.compile(
    r"</?(?:think|thinking|reasoning)\b[^>]*>",
    re.IGNORECASE,
)


def strip_thinking_response(text: str) -> str:
    """Remove `<think>…</think>` blocks and any stray unmatched tags
    from a non-streaming LLM response. Safe on arbitrary text —
    returns the input trimmed when no tags are present."""
    if not text:
        return text
    text = _THINKING_BLOCK_RE.sub("", text)
    text = _STRAY_THINK_TAG_RE.sub("", text)
    return text.strip()

# Families whose outputs benefit from /no_think. Matched case-insensitively
# against the model name. Kept as substring-search rather than regex so
# tags like "qwen3:8b-instruct-q4_k_m" and "qwen3-30b-a3b" both trigger.
_QWEN3_FAMILY_RE = re.compile(r"qwen\s*3", re.IGNORECASE)

# Marker we look for to avoid double-prepending the directive when a
# caller's system prompt already includes it (hand-written or
# already-processed messages).
_NO_THINK_MARKER = "/no_think"


def is_qwen3_family(model: str | None) -> bool:
    """True when the model name matches the Qwen3 family."""
    if not model:
        return False
    return bool(_QWEN3_FAMILY_RE.search(model))


def apply_model_family_directives(
    messages: list[dict[str, Any]],
    model: str | None,
) -> list[dict[str, Any]]:
    """Return a new messages list with any model-family directives
    injected. Original list is not mutated.

    For Qwen3: prepends `/no_think\\n` to the first system message's
    content. If no system message exists, inserts one at the start.
    The directive tells Qwen3 to emit an empty think block (so the
    model skips reasoning mode and answers directly). This is safe for
    strict-JSON prompts (Tier 2 disambiguator) AND tool-continuation
    prompts (Tier 3 agentic loop) — both were hitting token caps
    because the model was reasoning instead of answering.

    No-op when:
      - the model is not in a known thinking family
      - the first system message already contains `/no_think`
    """
    if not is_qwen3_family(model):
        return messages
    if not messages:
        return [{"role": "system", "content": _NO_THINK_MARKER}]

    out = list(messages)
    for i, msg in enumerate(out):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "system":
            # Stop at the first non-system message — Qwen3 templates put
            # system first, and we don't want to rewrite a user/assistant
            # turn further down.
            break
        content = msg.get("content")
        if isinstance(content, str) and _NO_THINK_MARKER in content:
            return out  # already present, nothing to do
        if isinstance(content, str):
            new_msg = dict(msg)
            new_msg["content"] = f"{_NO_THINK_MARKER}\n{content}"
            out[i] = new_msg
            return out
        # System message with non-string content (rare) — skip quietly.
        return out

    # No system message in the list → inject one at the front.
    return [{"role": "system", "content": _NO_THINK_MARKER}, *out]


__all__ = [
    "apply_model_family_directives",
    "is_qwen3_family",
    "strip_thinking_response",
]
