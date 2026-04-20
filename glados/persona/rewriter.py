"""Persona rewriter for Tier 1 responses.

Tier 1 (HA conversation API) returns plain-English text like
"Turned off the kitchen light." That's correct but bland — the user
expects GLaDOS's voice. This module transforms HA's plain confirmation
into a GLaDOS-flavored reply via a short Ollama call.

Design notes:
- Uses the same fast autonomy Ollama as the disambiguator. Short prompt,
  short output → typically 2-5s on the 14B model, 1-2s on a 3B model.
- Pure best-effort. If the LLM is slow, errors, or returns garbage,
  the caller falls back to HA's original speech. The user always gets
  a real response; the persona is a polish layer, not a hard dependency.
- No tools. No state. The LLM only restyles the input string.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from loguru import logger


# Tunables. Both env-overridable so the operator can dial them per
# deployment without a code change.
_REWRITER_TIMEOUT_S = float(os.environ.get("REWRITER_TIMEOUT_S", "8"))
_REWRITER_MAX_INPUT_CHARS = 500
_REWRITER_MAX_OUTPUT_CHARS = 400


@dataclass
class RewriteResult:
    """Outcome of one rewrite attempt."""
    success: bool
    text: str               # Rewritten text on success, or original on failure
    latency_ms: int
    error: str = ""


class PersonaRewriter:
    """Stateless rewriter. Safe to call concurrently.

    `rewrite(plain_text)` returns the GLaDOS-voiced restyling, or the
    original `plain_text` if the LLM call fails. Never raises.
    """

    def __init__(self, ollama_url: str, model: str) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model

    def rewrite(self, plain_text: str, context_hint: str = "") -> RewriteResult:
        """Restyle `plain_text` in GLaDOS's voice.

        `context_hint` is optional extra context for the LLM (e.g. the
        original user utterance) so it can match tone to the situation.
        Empty hint is fine.
        """
        t0 = time.perf_counter()
        if not plain_text or not plain_text.strip():
            return RewriteResult(success=True, text=plain_text, latency_ms=0)

        # Hard cap input size to bound LLM latency / token cost.
        input_text = plain_text[:_REWRITER_MAX_INPUT_CHARS]

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(input_text, context_hint)},
        ]
        # Phase 8.0.1 — kill Qwen3 think-mode on the rewrite call. A
        # ~30-char plain response from HA should produce a one-liner,
        # not a think-block prelude that eats num_predict=200.
        from glados.core.llm_directives import apply_model_family_directives
        messages = apply_model_family_directives(messages, self._model)
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.6,    # some creativity, not chaos
                "top_p": 0.9,
                "num_ctx": 1024,
                "num_predict": 200,    # cap output token count
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            self._ollama_url + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_REWRITER_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.debug("PersonaRewriter call failed: {} (returning original)", exc)
            return RewriteResult(
                success=False, text=plain_text,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                error=str(exc),
            )

        out = (data.get("message") or {}).get("content", "") or ""
        # Phase 8.0.1 — Qwen3 with /no_think still emits empty
        # <think>…</think> tags around the actual response on plain-
        # format calls (confirmed against Ollama). Strip before the
        # existing _clean_output vocative-strip pass.
        from glados.core.llm_directives import (
            strip_closing_boilerplate, strip_thinking_response,
        )
        out = strip_thinking_response(out)
        # Phase 8.3 operator bug — drop "I do not require further
        # confirmation" and related sign-off tics from the rewriter
        # output. Defence in depth with the preprompt-side rule.
        out = strip_closing_boilerplate(out)
        out = _clean_output(out)
        if not out:
            return RewriteResult(
                success=False, text=plain_text,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                error="empty_output",
            )
        if len(out) > _REWRITER_MAX_OUTPUT_CHARS:
            out = out[:_REWRITER_MAX_OUTPUT_CHARS].rstrip() + "..."
        return RewriteResult(
            success=True, text=out,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
ROLE: You are a tone editor. Rewrite a plain confirmation message
in the same voice as the rest of the system (set by the operator's
preprompt elsewhere). Preserve every fact in the input — entity
names, numbers, states, room names — but restyle the phrasing.

Hard rules:
- Output ONE OR TWO sentences. No more.
- Output prose only — no JSON, no quotes around the response,
  no preamble like "Here is the rewrite:".
- Preserve every concrete fact (entity names, room names, numbers,
  on/off states, times) from the input. Do not change them.
- Do not add new device actions or instructions.
- Do not invent context that wasn't in the input.
- Do not address the user with a noun-of-address (labels, titles).
  Speak ABOUT the action.
- Do NOT copy any phrase from this instruction verbatim — always
  compose fresh text for THIS input. No example phrases are shown
  on purpose; pick your own wording each time.
"""


def _build_user_prompt(plain_text: str, context_hint: str) -> str:
    parts = [f"Rewrite this in GLaDOS's voice:\n\n{plain_text}"]
    if context_hint:
        parts.append(f"\n(For context, the user said: {context_hint!r})")
    return "\n".join(parts)


def _clean_output(raw: str) -> str:
    """Strip code fences, leading "Here is..." chatter, outer quotes
    that small models sometimes wrap their output in, and trailing
    vocative labels like "test subject" that the operator dislikes."""
    s = raw.strip()
    # Strip code fences.
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("text\n") or s.lower().startswith("plaintext\n"):
            s = s.split("\n", 1)[1] if "\n" in s else s
    # Drop common preambles.
    for prefix in (
        "here is the rewrite:", "here's the rewrite:",
        "rewrite:", "glados:", "response:",
    ):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].lstrip()
    # Strip surrounding quotes.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    s = strip_trailing_vocative(s.strip())
    return s.strip()


# Vocative labels the user has banned. Matched as a trailing form-of-
# address: ", test subject." / "—test subject." / " test subject!".
# Case-insensitive. Punctuation around them gets cleaned up.
_BANNED_VOCATIVES: tuple[str, ...] = (
    "test subject",
    "subject",
    "test subjects",
    "subjects",
    "human",
    "humans",
    "human being",
    "human beings",
    "meatbag",
    "meatbags",
)


def strip_trailing_vocative(text: str) -> str:
    """Remove a trailing form-of-address like ", test subject." even if
    the LLM ignored the prompt instruction. Operates per-sentence so
    only the END of a sentence is trimmed; references in the middle
    of prose (rare, but possible) are left alone."""
    if not text:
        return text
    import re
    # Match: optional separator (comma / dash / nothing), the vocative,
    # then optional terminal punctuation, anchored to end-of-string OR
    # end-of-sentence. Sorted longest-first so "test subject" wins
    # over "subject".
    vocs = sorted(_BANNED_VOCATIVES, key=len, reverse=True)
    pattern = (
        r"(\s*[,\-\u2014\u2013]?\s*)\b("
        + "|".join(re.escape(v) for v in vocs)
        + r")\b(\s*[.!?]?)(\s*)$"
    )
    new = re.sub(pattern, r"\3", text, flags=re.IGNORECASE)
    # If we trimmed and the result lacks terminal punctuation, add one.
    if new != text and new and new[-1] not in ".!?":
        new = new.rstrip(",;: \t") + "."
    return new


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_REWRITER: PersonaRewriter | None = None
_LOCK = threading.Lock()


def init_rewriter(rewriter: PersonaRewriter) -> None:
    global _REWRITER
    with _LOCK:
        _REWRITER = rewriter


def get_rewriter() -> PersonaRewriter | None:
    return _REWRITER
