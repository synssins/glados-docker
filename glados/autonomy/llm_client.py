"""
Simple LLM client for subagent use.

Provides a minimal interface for making LLM calls without the full
complexity of the main LLM processor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar

import requests
from loguru import logger


MAX_AUTONOMY_USER_PROMPT_CHARS = 8000
_TRUNCATION_SENTINEL = "[…truncated…]\n\n"


def _truncate_user_prompt(
    prompt: str, budget: int = MAX_AUTONOMY_USER_PROMPT_CHARS,
) -> tuple[str, bool]:
    """If ``prompt`` exceeds ``budget`` characters, drop the oldest
    content and prepend a sentinel so the model sees something
    explanatory. Returns ``(text, truncated)``."""
    if len(prompt) <= budget:
        return prompt, False
    return _TRUNCATION_SENTINEL + prompt[-budget:], True


@dataclass
class LLMConfig:
    """Configuration for LLM API calls.

    `model` is intentionally required (no default). Callers resolve it
    from cfg.service_model("llm_autonomy") or equivalent. Hard-coded
    defaults silently route to unintended backends; empty-required fails
    loud at construction time instead.
    """

    url: str
    model: str
    api_key: str | None = None
    timeout: float = 30.0

    _ALLOWED_SLOTS: ClassVar[tuple[str, ...]] = (
        "llm_interactive",
        "llm_autonomy",
        "llm_triage",
        "llm_vision",
    )

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @classmethod
    def for_slot(cls, slot: str, *, timeout: float = 30.0) -> "LLMConfig":
        """Resolve an ``LLMConfig`` from one of the four well-known
        service slots in ``cfg.services``. Slot must be one of
        ``llm_interactive``, ``llm_autonomy``, ``llm_triage``,
        ``llm_vision``."""
        if slot not in cls._ALLOWED_SLOTS:
            raise ValueError(
                f"Unknown service slot {slot!r}; "
                f"expected one of {cls._ALLOWED_SLOTS}"
            )
        # Local import: cfg loads on first access and pulls in much of
        # the engine. Keeping it inside the method avoids a circular
        # import at llm_client module load time, matching the pattern
        # already used in apply_model_family_directives below.
        from glados.core.config_store import cfg
        endpoint = getattr(cfg.services, slot)
        return cls(url=endpoint.url, model=endpoint.model, timeout=timeout)


def llm_call(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    json_response: bool = False,
    json_schema: dict | None = None,
    max_tokens: int | None = None,
) -> str | None:
    """
    Make a simple LLM call.

    Returns the assistant's response text, or None on error.

    ``max_tokens``: optional defensive cap on output length. Passed
    through as the OpenAI-spec ``max_tokens`` field. Useful when the
    backend doesn't honor schema constraints (e.g. OpenArc silently
    drops ``response_format``) and a small classifier model can ramble
    well past any useful answer. Leave ``None`` for the backend default.
    """
    # Budget enforcement — autonomy callers occasionally pass a
    # decade of conversation history. Truncate to the most-recent
    # MAX_AUTONOMY_USER_PROMPT_CHARS chars so LM Studio's ctx
    # window is never the bottleneck.
    original_len = len(user_prompt)
    user_prompt, truncated = _truncate_user_prompt(user_prompt)
    if truncated:
        logger.warning(
            "LLM call: user_prompt truncated from {} to {} chars",
            original_len, len(user_prompt),
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Phase 8.0.1 — suppress Qwen3 thinking mode for every subagent
    # call that flows through this helper (observer, emotion agent,
    # memory classifier, doorbell screener when it routes through
    # here, etc.). Wall-clock win is especially large on the JSON
    # response_format path where the think prefix invalidates the
    # schema-constrained output and forces a retry.
    from glados.core.llm_directives import apply_model_family_directives
    messages = apply_model_family_directives(messages, config.model)

    data: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": False,
    }
    if max_tokens is not None:
        # Defensive cap. Some backends (e.g. OpenArc) do not honor
        # schema-constrained decoding and the model can run on for
        # hundreds of tokens of <think> content even with /no_think
        # in the system prompt. The cap bounds the request budget;
        # the existing tolerant JSON parser handles partial output.
        data["max_tokens"] = max_tokens
    # NOTE: do not pass ``temperature: 0.0``. OpenArc / OpenVINO GenAI
    # defaults ``do_sample=true`` from the model's generation_config.json;
    # combined with ``temperature=0`` the sampler divides by zero and
    # crashes the worker, which OpenArc then auto-unloads with no
    # auto-reload. Use the model default temperature (omit the field)
    # or pass a small positive value (e.g. 0.1) if determinism is
    # required.

    if json_schema is not None:
        # Schema-constrained decoding: LM Studio's llama.cpp runtime
        # converts the schema to a grammar and rejects any token
        # sequence that doesn't conform. Use this when the caller has
        # a well-defined output shape and wants the runtime to enforce
        # it (e.g. triage with an ``enum`` of valid plugin names so
        # the model literally cannot hallucinate a name).
        #
        # ``json_schema`` argument shape mirrors OpenAI's
        # response_format spec — caller passes the inner object:
        #
        #   {
        #     "name": "<short id>",
        #     "strict": True,
        #     "schema": {<JSON Schema>},
        #   }
        data["response_format"] = {
            "type": "json_schema",
            "json_schema": json_schema,
        }
    elif json_response:
        # Soft JSON hint without a schema. LM Studio's llama.cpp
        # runtime (build 2.13.0+) rejects the legacy
        # ``{"type": "json_object"}`` form with a 400 in ~3 ms:
        # ``'response_format.type' must be 'json_schema' or 'text'``.
        # ``text`` is the closest legacy-compatible behaviour: each
        # caller already wraps ``json.loads`` in try/except and
        # tolerates malformed responses, and the system prompts
        # already say "reply with JSON {…}". The system prompt +
        # tolerant parser pair does the real work; the legacy
        # ``json_object`` flag was only a soft assist.
        #
        # TODO(json_schema): evolve emotion agent / observer /
        # doorbell screener call sites to pass an explicit
        # ``json_schema`` so they get hard constraints instead of
        # this soft hint. Triage already migrated.
        data["response_format"] = {"type": "text"}

    # ``config.url`` is the bare ``scheme://host:port`` operators paste into
    # the LLM & Services WebUI URL field; the OpenAI chat-completions path
    # is appended only at dispatch time so the user never has to type it.
    from glados.core.url_utils import compose_endpoint
    endpoint = compose_endpoint(config.url, "/v1/chat/completions")
    try:
        response = requests.post(
            endpoint,
            headers=config.headers,
            json=data,
            timeout=config.timeout,
        )
        response.raise_for_status()
        result = response.json()

        # Handle OpenAI-style response
        # Phase 8.0.1 — strip Qwen3's empty <think>…</think> wrapper
        # that plain-format calls emit when /no_think is active.
        # Downstream JSON parsers (emotion agent, memory classifier)
        # were failing on the raw content.
        # 2026-04-27 — fall back to ``reasoning_content`` when ``content``
        # is empty / null / missing. Reasoning models (GLM-4.x,
        # DeepSeek-R1, OpenAI o-series) emit the substantive output
        # there when the budget cap or schema-constrained JSON path
        # leaves the content channel empty.
        from glados.core.llm_directives import strip_thinking_response
        if "choices" in result and result["choices"]:
            msg = result["choices"][0].get("message", {})
            content = msg.get("content") or msg.get("reasoning_content")
            if not content:
                return None
            return strip_thinking_response(content)

        # Handle Ollama-style response
        if "message" in result:
            msg = result["message"]
            content = msg.get("content") or msg.get("reasoning_content")
            if not content:
                return None
            return strip_thinking_response(content)

        logger.warning("LLM call: unexpected response format")
        return None

    except requests.Timeout:
        logger.warning("LLM call timed out")
        return None
    except requests.RequestException as e:
        logger.warning("LLM call failed: {}", e)
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM call: failed to parse response: {}", e)
        return None
