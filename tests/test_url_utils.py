"""URL helpers backing the bare-base storage contract.

Operators paste a bare ``http(s)://host:port`` into the LLM & Services
WebUI URL field. The system stores the bare form everywhere and
appends protocol-internal paths (``/v1/chat/completions``,
``/v1/models``) only at dispatch time. ``strip_url_path`` and
``compose_endpoint`` are the two pure helpers every dispatch site
calls; they need to agree on the contract.

Acceptance criterion (from the URL-field UX fix): the api_wrapper's
outgoing POST URL must end in ``/v1/chat/completions`` even when the
configured ``glados.completion_url`` is bare. ``compose_endpoint``
backs that guarantee — exercised here against the same call shape
the dispatch sites use.
"""
from __future__ import annotations

import pytest

from glados.core.url_utils import compose_endpoint, strip_url_path


class TestStripUrlPath:
    @pytest.mark.parametrize("inp,expected", [
        ("http://host:11434", "http://host:11434"),
        ("http://host:11434/", "http://host:11434"),
        ("http://host:11434/v1/chat/completions", "http://host:11434"),
        ("http://host:11434/v1/chat/completions/", "http://host:11434"),
        ("http://host:11434/api/chat", "http://host:11434"),
        ("http://host:11434/api/tags", "http://host:11434"),
        ("http://host:11434/v1/models", "http://host:11434"),
        ("https://llm.example.com:443/anything/else", "https://llm.example.com:443"),
        ("http://192.168.1.75:11434", "http://192.168.1.75:11434"),
    ])
    def test_path_is_stripped(self, inp: str, expected: str) -> None:
        assert strip_url_path(inp) == expected

    @pytest.mark.parametrize("inp", ["", "   ", "\t\n", None])
    def test_empty_returns_empty(self, inp) -> None:
        assert strip_url_path(inp) == ""


class TestComposeEndpoint:
    def test_chat_completions_appended_to_bare(self) -> None:
        """The api_wrapper acceptance criterion: bare base + chat path
        composes to a complete OpenAI chat-completions URL."""
        assert (
            compose_endpoint("http://192.168.1.75:11434", "/v1/chat/completions")
            == "http://192.168.1.75:11434/v1/chat/completions"
        )

    def test_legacy_full_url_normalized_then_recomposed(self) -> None:
        """A legacy stored URL with ``/api/chat`` baked in still composes
        correctly — the path is stripped before the OpenAI suffix is
        appended."""
        assert (
            compose_endpoint("http://host:11434/api/chat", "/v1/chat/completions")
            == "http://host:11434/v1/chat/completions"
        )

    def test_models_path_for_discover(self) -> None:
        assert (
            compose_endpoint("http://host:11434", "/v1/models")
            == "http://host:11434/v1/models"
        )

    def test_path_without_leading_slash_is_normalized(self) -> None:
        assert (
            compose_endpoint("http://host:11434", "v1/chat/completions")
            == "http://host:11434/v1/chat/completions"
        )

    def test_empty_base_returns_empty(self) -> None:
        assert compose_endpoint("", "/v1/chat/completions") == ""
        assert compose_endpoint(None, "/v1/chat/completions") == ""


class TestApiWrapperOutgoingUrl:
    """Acceptance test for the URL-field UX fix: the api_wrapper's
    outgoing POST URL must end with ``/v1/chat/completions`` even when
    ``glados.completion_url`` is the bare ``scheme://host:port``."""

    def test_compose_at_bare_completion_url(self) -> None:
        # This mirrors the exact two-line dance in
        # ``api_wrapper._stream_chat_sse_impl`` and ``llm_processor`` — a
        # bare URL pulled from config, the path appended at the moment
        # of POST.
        completion_url_in_config = "http://host:port"
        outgoing = compose_endpoint(completion_url_in_config, "/v1/chat/completions")
        assert outgoing.endswith("/v1/chat/completions")
        assert outgoing == "http://host:port/v1/chat/completions"


class TestAutonomyLLMClientDispatchesAtChatPath:
    """Integration check: ``glados.autonomy.llm_client.llm_call`` reads the
    bare URL out of ``LLMConfig`` and posts to ``/v1/chat/completions``.
    This is the same call shape every subagent (observer, emotion, memory
    classifier, doorbell screener via the helper) uses. Patches
    ``requests.post`` and inspects the URL the helper actually fired."""

    def test_post_url_ends_in_chat_completions(self) -> None:
        from unittest.mock import patch

        from glados.autonomy.llm_client import LLMConfig, llm_call

        captured: dict[str, str] = {}

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "ok"}}]}

            def raise_for_status(self) -> None:
                return None

        def _capture(url, **kwargs):
            captured["url"] = url
            return _Resp()

        cfg = LLMConfig(url="http://host:11434", model="m", timeout=5.0)
        with patch("glados.autonomy.llm_client.requests.post", side_effect=_capture):
            llm_call(cfg, "sys", "user")
        assert captured["url"] == "http://host:11434/v1/chat/completions"

    def test_legacy_full_url_still_dispatches_to_chat_completions(self) -> None:
        """If a legacy ``LLMConfig.url`` still has ``/api/chat`` baked in,
        the dispatch site strips it and re-appends the OpenAI path so the
        outgoing request hits the right endpoint regardless."""
        from unittest.mock import patch

        from glados.autonomy.llm_client import LLMConfig, llm_call

        captured: dict[str, str] = {}

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "ok"}}]}

            def raise_for_status(self) -> None:
                return None

        def _capture(url, **kwargs):
            captured["url"] = url
            return _Resp()

        cfg = LLMConfig(url="http://host:11434/api/chat", model="m", timeout=5.0)
        with patch("glados.autonomy.llm_client.requests.post", side_effect=_capture):
            llm_call(cfg, "sys", "user")
        assert captured["url"] == "http://host:11434/v1/chat/completions"
