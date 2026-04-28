"""Autonomy LLM client must accept the OpenAI extended response shape.

Reasoning models (GLM-4.7-Flash, DeepSeek-R1, OpenAI o-series) return
the chain of thought in ``message.reasoning_content`` and the final
answer in ``message.content``. When the budget cap or the schema-
constrained JSON path makes ``content`` empty, the substantive output
sits in ``reasoning_content``. The autonomy client today only reads
``content``; an empty value triggers the ``unexpected response format``
warning storm and returns ``None``, retrying continuously.

Fix: when ``content`` is empty / missing but ``reasoning_content`` is
populated, fall back to it. This is OpenAI-extended-protocol compliance
that benefits any reasoning-mode model on any compatible backend.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from glados.autonomy.llm_client import LLMConfig, llm_call


def _config() -> LLMConfig:
    return LLMConfig(
        url="http://aibox.local/v1/chat/completions",
        model="glm-4.7-flash",
        api_key=None,
        timeout=5.0,
    )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestLLMCallReasoningContent:
    """OpenAI extended response shape parity for the autonomy lane."""

    def test_openai_content_present_returned_as_before(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": "real answer", "reasoning_content": "thoughts"}}
            ]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") == "real answer"

    def test_openai_empty_content_falls_back_to_reasoning(self) -> None:
        """The motivating bug: GLM emits content="" plus reasoning_content
        with the substantive output. Autonomy should not warn / retry —
        it should return the reasoning text."""
        payload = {
            "choices": [
                {"message": {"content": "", "reasoning_content": "the actual reply"}}
            ]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") == "the actual reply"

    def test_openai_null_content_falls_back_to_reasoning(self) -> None:
        """Some servers emit ``content: null`` rather than empty string
        when the channel is unused. Same fallback."""
        payload = {
            "choices": [
                {"message": {"content": None, "reasoning_content": "answer"}}
            ]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") == "answer"

    def test_openai_missing_content_key_falls_back_to_reasoning(self) -> None:
        """Strict OpenAI-extended servers may omit ``content`` entirely
        when the answer was returned via reasoning."""
        payload = {
            "choices": [{"message": {"reasoning_content": "answer"}}]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") == "answer"

    def test_openai_neither_channel_returns_none(self) -> None:
        """If the model returned no usable text in either channel, the
        function returns ``None`` (caller decides whether to retry)."""
        payload = {
            "choices": [{"message": {"content": "", "reasoning_content": ""}}]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") is None

    def test_ollama_empty_content_falls_back_to_reasoning(self) -> None:
        """Ollama 0.14+ also exposes ``message.reasoning_content`` on the
        non-OpenAI shape for reasoning models."""
        payload = {"message": {"content": "", "reasoning_content": "ollama answer"}}
        with patch("requests.post", return_value=_FakeResponse(payload)):
            assert llm_call(_config(), "sys", "user") == "ollama answer"

    def test_thinking_tags_stripped_from_reasoning_fallback(self) -> None:
        """``strip_thinking_response`` is the existing post-processor —
        it removes the ``<think>...</think>`` wrapper that some servers
        leave in the reasoning channel. Fallback path must run through
        the same stripper for shape parity with the content path."""
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "<think>cot</think>final answer",
                    }
                }
            ]
        }
        with patch("requests.post", return_value=_FakeResponse(payload)):
            result = llm_call(_config(), "sys", "user")
        assert result is not None
        assert "<think>" not in result
        assert "final answer" in result
