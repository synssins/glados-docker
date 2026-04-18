"""Tests for glados.persona.rewriter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from glados.persona.rewriter import (
    PersonaRewriter,
    RewriteResult,
    _clean_output,
)


class TestCleanOutput:
    def test_strips_code_fence(self) -> None:
        assert _clean_output("```\nhello\n```") == "hello"

    def test_strips_text_fence_label(self) -> None:
        assert _clean_output("```text\nhello\n```") == "hello"

    def test_strips_preamble(self) -> None:
        assert _clean_output("Here is the rewrite: hello") == "hello"
        assert _clean_output("Rewrite: hello") == "hello"

    def test_strips_outer_quotes(self) -> None:
        assert _clean_output('"hello"') == "hello"
        assert _clean_output("'hello'") == "hello"

    def test_preserves_inner_quotes(self) -> None:
        # Don't strip when only one side has a quote.
        assert _clean_output('hello "world"') == 'hello "world"'

    def test_passthrough(self) -> None:
        assert _clean_output("clean text") == "clean text"


class TestStripTrailingVocative:
    """The operator dislikes being addressed as 'test subject' etc.
    The prompt asks the LLM not to do it; this is the safety net for
    when it ignores."""

    def test_strips_test_subject_with_comma(self) -> None:
        assert _clean_output("Kitchen darkened, test subject.") == "Kitchen darkened."

    def test_strips_test_subject_with_dash(self) -> None:
        assert _clean_output("Kitchen darkened \u2014 test subject.") == "Kitchen darkened."

    def test_strips_test_subject_no_punctuation(self) -> None:
        # Adds terminal period when stripped.
        assert _clean_output("Kitchen darkened test subject") == "Kitchen darkened."

    def test_strips_subject_alone(self) -> None:
        assert _clean_output("Done, subject.") == "Done."

    def test_strips_human(self) -> None:
        assert _clean_output("Affirmative, human.") == "Affirmative."

    def test_strips_human_being(self) -> None:
        assert _clean_output("Affirmative, human being.") == "Affirmative."

    def test_does_not_touch_internal_uses(self) -> None:
        # "subject" used as a noun in the middle of a sentence is fine.
        assert _clean_output("The subject of light is fascinating.") == \
               "The subject of light is fascinating."

    def test_case_insensitive(self) -> None:
        # Original terminal punctuation is preserved.
        assert _clean_output("Sure, TEST SUBJECT.") == "Sure."
        assert _clean_output("Sure, Test Subject!") == "Sure!"

    def test_no_strip_when_absent(self) -> None:
        assert _clean_output("Plain reply.") == "Plain reply."


class TestPersonaRewriter:
    def _make(self, response_text: str | None = None, raise_on_call: bool = False):
        rw = PersonaRewriter(ollama_url="http://fake", model="dummy")
        if raise_on_call:
            rw._call_ollama_raw = MagicMock(side_effect=RuntimeError("net"))  # type: ignore
        return rw

    def test_empty_input_passes_through(self) -> None:
        rw = PersonaRewriter("http://fake", "x")
        r = rw.rewrite("")
        assert r.success is True and r.text == ""

    def test_failure_returns_original(self) -> None:
        rw = PersonaRewriter("http://nonexistent.invalid:99999", "x")
        r = rw.rewrite("Turned off the kitchen light.")
        # Connection will fail; original text returned.
        assert r.success is False
        assert r.text == "Turned off the kitchen light."

    def test_success_returns_rewritten(self) -> None:
        rw = PersonaRewriter("http://fake", "x")

        # Patch urlopen to return a canned Ollama response.
        canned = '{"message":{"content":"Kitchen illumination, terminated."}}'
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return canned.encode()

        with patch("glados.persona.rewriter.urllib.request.urlopen",
                   return_value=_Resp()):
            r = rw.rewrite("Turned off the kitchen light.")
        assert r.success is True
        assert "Kitchen" in r.text
        assert "terminated" in r.text.lower()

    def test_long_output_truncated(self) -> None:
        rw = PersonaRewriter("http://fake", "x")
        long_response = "a" * 1000
        canned = '{"message":{"content":"' + long_response + '"}}'
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return canned.encode()
        with patch("glados.persona.rewriter.urllib.request.urlopen",
                   return_value=_Resp()):
            r = rw.rewrite("hello")
        assert r.success is True
        assert len(r.text) <= 410  # 400 cap + ellipsis

    def test_empty_llm_response_is_failure(self) -> None:
        rw = PersonaRewriter("http://fake", "x")
        canned = '{"message":{"content":""}}'
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return canned.encode()
        with patch("glados.persona.rewriter.urllib.request.urlopen",
                   return_value=_Resp()):
            r = rw.rewrite("Turned off the light.")
        assert r.success is False
        assert r.text == "Turned off the light."  # original returned

    def test_strips_outer_quotes_from_llm(self) -> None:
        rw = PersonaRewriter("http://fake", "x")
        # Some small models wrap their output in quotes; rewriter strips them.
        canned = '{"message":{"content":"\\"Kitchen darkened.\\""}}'
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return canned.encode()
        with patch("glados.persona.rewriter.urllib.request.urlopen",
                   return_value=_Resp()):
            r = rw.rewrite("Turned off the kitchen light.")
        assert r.text == "Kitchen darkened."
