"""User-prompt size budget enforced inside llm_call. Stops LM Studio
'Context size exceeded' error chunks from the autonomy lane."""

from __future__ import annotations

from unittest.mock import patch

from glados.autonomy.llm_client import (
    LLMConfig,
    MAX_AUTONOMY_USER_PROMPT_CHARS,
    _truncate_user_prompt,
    llm_call,
)


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestTruncateUserPrompt:
    def test_short_prompt_unchanged(self) -> None:
        out, truncated = _truncate_user_prompt("hi", 100)
        assert out == "hi"
        assert truncated is False

    def test_exact_budget_unchanged(self) -> None:
        text = "x" * 100
        out, truncated = _truncate_user_prompt(text, 100)
        assert out == text
        assert truncated is False

    def test_long_prompt_truncates_oldest(self) -> None:
        text = "OLD" + ("x" * 200) + "RECENT"
        out, truncated = _truncate_user_prompt(text, 50)
        assert truncated is True
        assert "[…truncated…]" in out
        # Most-recent suffix is preserved
        assert out.endswith("RECENT")
        # Oldest prefix is gone
        assert "OLD" not in out

    def test_truncation_keeps_total_within_budget_plus_sentinel(self) -> None:
        text = "x" * 1000
        out, truncated = _truncate_user_prompt(text, 100)
        # Budget is 100 characters of original content;
        # sentinel adds a bounded prefix.
        assert truncated is True
        assert len(out) <= 100 + len("[…truncated…]\n\n")


class TestLLMCallBudget:
    def _config(self) -> LLMConfig:
        return LLMConfig(
            url="http://example/v1/chat/completions",
            model="m",
            timeout=5.0,
        )

    def test_oversized_user_prompt_logs_warning(self) -> None:
        from loguru import logger as _loguru_logger
        records: list[str] = []
        sink_id = _loguru_logger.add(lambda m: records.append(str(m)), level="DEBUG")
        try:
            payload = {"choices": [{"message": {"content": "ok"}}]}
            with patch("requests.post", return_value=_Resp(payload)):
                llm_call(
                    self._config(),
                    "sys",
                    "x" * (MAX_AUTONOMY_USER_PROMPT_CHARS + 5000),
                )
        finally:
            _loguru_logger.remove(sink_id)
        assert any("truncated" in r.lower() for r in records), records

    def test_truncated_prompt_actually_sent(self) -> None:
        """The POST body's user message must be the truncated form."""
        seen = {}

        def _capture(url, **kwargs):
            seen["body"] = kwargs.get("json")
            return _Resp({"choices": [{"message": {"content": "ok"}}]})

        with patch("requests.post", side_effect=_capture):
            llm_call(
                self._config(),
                "sys",
                "OLDEST" + ("x" * 20000) + "NEWEST",
            )
        user_msg = seen["body"]["messages"][-1]
        assert user_msg["role"] == "user"
        assert "OLDEST" not in user_msg["content"]
        assert user_msg["content"].endswith("NEWEST")
