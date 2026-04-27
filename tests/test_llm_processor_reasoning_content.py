"""OpenAI extended-streaming reasoning channel — GLM-4.7-Flash,
DeepSeek-R1, OpenAI o-series, and any future reasoning-mode model
emits chain-of-thought via ``delta.reasoning_content`` (separate
SSE channel) and the final answer via ``delta.content``.

The streaming parser previously only consumed ``delta.content`` and
silently dropped any chunk that carried ``reasoning_content`` instead.
That is OpenAI-extended-protocol non-compliance: the channel exists
and must be acknowledged. We log it at DEBUG and don't emit it to
TTS or the conversation store — same end behaviour as ``<think>``
tag stripping today, but for the separate-channel shape.

These are unit tests against ``_process_chunk`` directly. Full
streaming-loop integration is exercised by the existing
``test_tts_streaming_flush`` and ``test_autonomy_crosstalk`` paths.
"""

from __future__ import annotations

import queue as _q
import threading as _t

from glados.core.llm_processor import LanguageModelProcessor


def _make_lp() -> LanguageModelProcessor:
    return LanguageModelProcessor(
        llm_input_queue=_q.Queue(),
        tool_calls_queue=_q.Queue(),
        tts_input_queue=_q.Queue(),
        conversation_store=None,  # type: ignore[arg-type]
        completion_url="http://localhost/v1/chat/completions",  # type: ignore[arg-type]
        model_name="test",
        api_key=None,
        processing_active_event=_t.Event(),
        shutdown_event=_t.Event(),
    )


class TestProcessChunkReasoningContent:
    """``_process_chunk`` must distinguish reasoning channel from content.

    Returns ``None`` for reasoning chunks (so they don't enter the TTS /
    sentence pipeline) but logs them at DEBUG so the channel is observed.
    """

    def test_openai_delta_content_returns_string(self) -> None:
        lp = _make_lp()
        chunk = {
            "choices": [{"delta": {"content": "Hello"}, "index": 0}],
        }
        assert lp._process_chunk(chunk) == "Hello"

    def test_openai_delta_reasoning_content_returns_none(self) -> None:
        """Reasoning chunks must NOT bleed into the speakable content
        stream. Returning ``None`` keeps the existing TTS pipeline
        behaviour for non-content chunks (today: chunk is skipped)."""
        lp = _make_lp()
        chunk = {
            "choices": [
                {
                    "delta": {"reasoning_content": "Let me think about this..."},
                    "index": 0,
                }
            ],
        }
        assert lp._process_chunk(chunk) is None

    def test_openai_delta_reasoning_content_logs_at_debug(self, caplog) -> None:
        """The channel must be acknowledged in logs even though we
        drop the payload. This is the audit trail that proves the
        middleware saw the reasoning channel rather than silently
        ignoring 99% of the stream."""
        import logging

        # loguru -> logging interceptor (loguru's logger doesn't go through
        # caplog by default). Wire a minimal propagating handler.
        from loguru import logger as _loguru_logger

        records: list[str] = []

        def _sink(message: object) -> None:
            records.append(str(message))

        sink_id = _loguru_logger.add(_sink, level="DEBUG")
        try:
            lp = _make_lp()
            chunk = {
                "choices": [
                    {
                        "delta": {"reasoning_content": "Token-by-token CoT"},
                        "index": 0,
                    }
                ],
            }
            lp._process_chunk(chunk)
        finally:
            _loguru_logger.remove(sink_id)

        assert any("reasoning" in r.lower() for r in records), (
            "reasoning_content channel must be DEBUG-logged so its presence "
            f"is observable. Got log records: {records!r}"
        )

    def test_openai_delta_with_both_channels_prefers_content(self) -> None:
        """If a single chunk somehow carries both channels (rare but the
        OpenAI shape allows it), the content channel wins — that's what
        downstream TTS expects."""
        lp = _make_lp()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "content": "answer",
                        "reasoning_content": "ignored",
                    },
                    "index": 0,
                }
            ],
        }
        assert lp._process_chunk(chunk) == "answer"

    def test_openai_empty_delta_still_returns_none(self) -> None:
        lp = _make_lp()
        chunk = {"choices": [{"delta": {}, "index": 0}]}
        assert lp._process_chunk(chunk) is None

    def test_openai_tool_calls_still_returned(self) -> None:
        """Existing tool_calls path must still work — adding the
        reasoning branch must not regress the tool path."""
        lp = _make_lp()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "speak"}}
                        ]
                    },
                    "index": 0,
                }
            ],
        }
        result = lp._process_chunk(chunk)
        assert isinstance(result, list)
        assert result[0]["function"]["name"] == "speak"

    def test_ollama_message_reasoning_content_returns_none(self) -> None:
        """Some Ollama-compatible servers (mainline 0.14+ reasoning
        support) emit ``message.reasoning_content`` on the non-OpenAI
        shape. Same drop-and-log behaviour."""
        lp = _make_lp()
        chunk = {"message": {"reasoning_content": "thoughts"}}
        assert lp._process_chunk(chunk) is None

    def test_ollama_message_content_still_returns_string(self) -> None:
        lp = _make_lp()
        chunk = {"message": {"content": "from ollama"}}
        assert lp._process_chunk(chunk) == "from ollama"

    def test_error_chunk_with_string_message_does_not_crash(self) -> None:
        """LM Studio returns
        ``{"error": {"message": "..."}, "message": "Context size has been exceeded."}``
        on errors — note ``message`` is a STRING here, not a dict. The
        Ollama-format branch was crashing with
        ``'str' object has no attribute 'get'`` on this shape, taking
        the entire stream down silently. Must return None instead."""
        lp = _make_lp()
        chunk = {
            "error": {"message": "Context size has been exceeded."},
            "message": "Context size has been exceeded.",
        }
        assert lp._process_chunk(chunk) is None

    def test_openai_choices_with_non_dict_delta_does_not_crash(self) -> None:
        """Defensive: if upstream returns malformed choices, don't crash."""
        lp = _make_lp()
        chunk = {"choices": [{"delta": "not-a-dict"}]}
        assert lp._process_chunk(chunk) is None
