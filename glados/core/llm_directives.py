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


# ---------------------------------------------------------------------------
# Closing-boilerplate stripper (Phase 8.3 / operator bug, 2026-04-20)
#
# Qwen3:8b on the GLaDOS preprompt started appending a stock
# sign-off to every turn — "I do not require further confirmation",
# "No further confirmation required", "Your compliance has been
# logged", etc. It misreads the preprompt rule "never offer follow-
# up" as permission to announce that it does not require follow-up.
# These strings are low-information and grate on re-hearings, so
# strip them at the output boundary on every LLM response path.
# ---------------------------------------------------------------------------

# Each entry is a regex that matches a trailing boilerplate phrase.
# Anchored with `\s*$` so they only fire when the phrase ENDS the
# response; mid-text occurrences stay. Terminal punctuation
# optional. Case-insensitive.
_CLOSING_BOILERPLATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        # "You may observe that I do not require further confirmation."
        r"(?:\s|^)you\s+may\s+(?:observe|note)\s+that\s+"
        r"i\s+do\s+not\s+require\s+(?:further|additional)?\s*"
        r"(?:confirmation|acknowledgement|input|approval)[.!?]?\s*$",
        # "I do not require further confirmation."
        r"(?:\s|^)i\s+(?:do\s+not|don'?t)\s+require\s+"
        r"(?:further|additional|any|more)?\s*"
        r"(?:confirmation|acknowledgement|input|approval|"
        r"feedback|response|validation)[.!?]?\s*$",
        # "No further confirmation required / needed / is necessary."
        r"(?:\s|^)no\s+(?:further|additional|more)?\s*"
        r"(?:confirmation|acknowledgement|input|approval|"
        r"feedback|response|validation)\s+"
        r"(?:required|needed|is\s+necessary|necessary)[.!?]?\s*$",
        # "Your compliance has been logged / noted / recorded."
        r"(?:\s|^)your\s+compliance\s+has\s+been\s+"
        r"(?:logged|noted|recorded|documented)[.!?]?\s*$",
        # "The enrichment center thanks you / acknowledges you."
        r"(?:\s|^)the\s+(?:enrichment\s+center|aperture\s+science)\s+"
        r"(?:thanks|acknowledges|appreciates)\s+you[.!?]?\s*$",
        # "No additional action is required."
        r"(?:\s|^)no\s+(?:additional|further)\s+action\s+"
        r"(?:is\s+)?required[.!?]?\s*$",
        # "This concludes the current interaction."
        r"(?:\s|^)this\s+concludes\s+the\s+(?:current\s+)?"
        r"(?:interaction|exchange|session)[.!?]?\s*$",
        # "You are welcome to speculate on what came next."
        # 2026-04-21 live-observed on the potato answer. Soft
        # invitation-to-continue is exactly what the preprompt forbids.
        r"(?:\s|^)you(?:'re|\s+are)?\s+welcome\s+to\s+[^.!?]{0,60}[.!?]?\s*$",
        # "I leave that to you." / "The rest is up to you."
        r"(?:\s|^)(?:i\s+leave\s+(?:that|it|the\s+rest)\s+to\s+you|"
        r"the\s+rest\s+is\s+up\s+to\s+you)[.!?]?\s*$",
        # "You may draw your own conclusions." and variants.
        r"(?:\s|^)you\s+may\s+draw\s+your\s+own\s+"
        r"(?:conclusions?|inferences?)[.!?]?\s*$",
        # "How may I assist you today?" / "How can I help you?"
        # Generic AI-assistant closers the persona forbids — appeared
        # 2026-04-21 on "Hello GLaDOS".
        r"(?:\s|^)how\s+(?:may|can)\s+i\s+"
        r"(?:assist|help)\s+you(?:\s+today)?[.!?]?\s*$",
        # "is there something else I can..." / "let me know if..."
        r"(?:\s|^)is\s+there\s+(?:anything|something)\s+else\s+"
        r"i\s+can\s+(?:assist|help|do)[^.!?]*[.!?]?\s*$",
    )
)


def strip_closing_boilerplate(text: str) -> str:
    """Strip Qwen3's sign-off tics from the end of an LLM response.
    Runs the pattern set repeatedly until no more matches fire — a
    single generation can append multiple closers in a row. Leaves
    mid-text mentions alone; only trailing occurrences get cut."""
    if not text:
        return text
    s = text.rstrip()
    # Apply repeatedly so "Compliance logged. No further confirmation
    # required." peels both closers off.
    while True:
        before = s
        for pat in _CLOSING_BOILERPLATE_PATTERNS:
            s = pat.sub("", s).rstrip()
        if s == before:
            break
    # If the strip removed the terminal punctuation mid-sentence,
    # restore one so the remainder reads cleanly.
    if s and s[-1] not in ".!?\"'":
        s = s.rstrip(",;: \t") + "."
    return s

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
    enable_thinking: bool = False,
) -> list[dict[str, Any]]:
    """Return a new messages list with any model-family directives
    injected. Original list is not mutated.

    For Qwen3 hybrid models (e.g. ``qwen3-30b-a3b``):

    * ``enable_thinking=False`` (default) — prepends ``/no_think\\n``
      to the first system message's content (or inserts a system
      message containing it if none exists). Qwen3 emits an empty
      think block and answers directly. Use for fast triage paths
      (Tier 2 disambiguator, autonomy subagents, persona rewriter,
      doorbell screener, llm_decision helpers) — these never benefit
      from reasoning and were hitting token caps when the model
      decided to think on its own.

    * ``enable_thinking=True`` — leaves the messages unchanged
      (the hybrid Qwen3 chat template defaults to thinking ON when
      no directive is present). If the input already contains
      ``/no_think``, that directive is stripped so the model
      actually reasons. Use on the Tier 3 chat path when the turn
      benefits from multi-step planning or tool selection.

    Note: Qwen3 *thinking-only* variants (``qwen3-*-thinking-2507``)
    have the thinking marker baked into their chat template and ignore
    these directives. To get conditional reasoning, use the original
    hybrid Qwen3 release (no ``-thinking-2507`` suffix).

    No-op when the model is not in a known thinking family.
    """
    if not is_qwen3_family(model):
        return messages

    if enable_thinking:
        # Strip any pre-existing /no_think so the hybrid model reasons.
        return _strip_no_think_directive(messages)

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


def _strip_no_think_directive(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a new messages list with any leading ``/no_think``
    directive removed from the first system message. If the system
    message becomes empty, drop it. Original list is not mutated."""
    if not messages:
        return messages
    out = list(messages)
    for i, msg in enumerate(out):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "system":
            break
        content = msg.get("content")
        if not isinstance(content, str) or _NO_THINK_MARKER not in content:
            continue
        # Strip the marker plus its trailing newline / whitespace, but
        # only when it appears at the very start (the only place we
        # ever write it).
        stripped = content
        for prefix in (f"{_NO_THINK_MARKER}\n", f"{_NO_THINK_MARKER} ", _NO_THINK_MARKER):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        else:
            # Mid-string — leave alone; the caller wrote it that way.
            continue
        if stripped == "":
            # System message was just the marker; drop it.
            out.pop(i)
            return out
        new_msg = dict(msg)
        new_msg["content"] = stripped
        out[i] = new_msg
        return out
    return out


__all__ = [
    "apply_model_family_directives",
    "is_qwen3_family",
    "strip_closing_boilerplate",
    "strip_thinking_response",
]
