"""Tests for glados.core.llm_directives — Qwen3 /no_think injection."""

from __future__ import annotations

from glados.core.llm_directives import (
    apply_model_family_directives,
    is_qwen3_family,
    strip_closing_boilerplate,
    strip_thinking_response,
)


class TestIsQwen3Family:
    def test_plain_qwen3_matches(self) -> None:
        assert is_qwen3_family("qwen3")
        assert is_qwen3_family("qwen3:8b")
        assert is_qwen3_family("qwen3:14b-instruct-q4_K_M")
        assert is_qwen3_family("Qwen3-30B-A3B")

    def test_spaced_variant_matches(self) -> None:
        # Some registries use "qwen 3" or "Qwen 3"
        assert is_qwen3_family("qwen 3")
        assert is_qwen3_family("QWEN 3 Turbo")

    def test_other_families_do_not_match(self) -> None:
        assert not is_qwen3_family("qwen2.5:14b-instruct-q4_K_M")
        assert not is_qwen3_family("qwen2:7b")
        assert not is_qwen3_family("llama3:8b")
        assert not is_qwen3_family("deepseek-r1:14b")
        assert not is_qwen3_family("gpt-4o-mini")
        assert not is_qwen3_family("glados:latest")

    def test_empty_or_none(self) -> None:
        assert not is_qwen3_family(None)
        assert not is_qwen3_family("")
        assert not is_qwen3_family("   ")


class TestApplyDirectives:
    def test_qwen3_prepends_to_existing_system(self) -> None:
        msgs = [
            {"role": "system", "content": "You are GLaDOS."},
            {"role": "user", "content": "Hi"},
        ]
        out = apply_model_family_directives(msgs, "qwen3:8b")
        assert out[0]["role"] == "system"
        assert out[0]["content"].startswith("/no_think\n")
        assert "You are GLaDOS." in out[0]["content"]
        assert out[1]["content"] == "Hi"

    def test_qwen3_no_system_message_gets_one(self) -> None:
        msgs = [{"role": "user", "content": "Hi"}]
        out = apply_model_family_directives(msgs, "qwen3:8b")
        assert out[0]["role"] == "system"
        assert out[0]["content"] == "/no_think"
        assert out[1]["content"] == "Hi"
        assert len(out) == 2

    def test_qwen3_empty_messages(self) -> None:
        out = apply_model_family_directives([], "qwen3:14b")
        assert len(out) == 1
        assert out[0] == {"role": "system", "content": "/no_think"}

    def test_qwen3_idempotent_when_already_present(self) -> None:
        msgs = [
            {"role": "system", "content": "/no_think\nYou are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        out = apply_model_family_directives(msgs, "qwen3:8b")
        # Should not double-prepend.
        assert out[0]["content"].count("/no_think") == 1

    def test_non_qwen3_returns_unchanged(self) -> None:
        msgs = [
            {"role": "system", "content": "You are GLaDOS."},
            {"role": "user", "content": "Hi"},
        ]
        out = apply_model_family_directives(msgs, "qwen2.5:14b-instruct-q4_K_M")
        assert out == msgs  # by value; by-identity isn't required
        assert "/no_think" not in out[0]["content"]

    def test_does_not_mutate_original(self) -> None:
        msgs = [
            {"role": "system", "content": "Base."},
            {"role": "user", "content": "Hi"},
        ]
        original_copy = [dict(m) for m in msgs]
        _ = apply_model_family_directives(msgs, "qwen3:8b")
        # Original list contents unchanged
        assert msgs == original_copy

    def test_multiple_system_messages_injects_only_first(self) -> None:
        # Rare: some callers pass multiple system messages. We only
        # inject once to avoid double directives.
        msgs = [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "Hi"},
        ]
        out = apply_model_family_directives(msgs, "qwen3:8b")
        assert "/no_think" in out[0]["content"]
        assert "/no_think" not in out[1]["content"]

    def test_non_string_content_system_is_skipped(self) -> None:
        # Some models support multimodal content (list-of-parts). Don't
        # rewrite that shape — just return unchanged to avoid breaking
        # the caller's payload.
        msgs = [
            {"role": "system", "content": [{"type": "text", "text": "X"}]},
            {"role": "user", "content": "Hi"},
        ]
        out = apply_model_family_directives(msgs, "qwen3:8b")
        assert out[0]["content"] == msgs[0]["content"]


class TestStripThinkingResponse:
    def test_strips_empty_think_wrapper(self) -> None:
        # Qwen3 + /no_think on plain-format produces this exact shape.
        raw = "<think>\n\n</think>\n\nBlue."
        assert strip_thinking_response(raw) == "Blue."

    def test_strips_populated_think_block(self) -> None:
        raw = "<think>Okay let me reason.</think>\nThe answer is 42."
        assert strip_thinking_response(raw) == "The answer is 42."

    def test_strips_multiline_think(self) -> None:
        raw = "<think>\nline one\nline two\n</think>\nresult"
        assert strip_thinking_response(raw) == "result"

    def test_strips_stray_unclosed_tag(self) -> None:
        raw = "<think>\ndangling"
        # Unclosed tag — keep the remainder, remove the stray tag.
        assert strip_thinking_response(raw) == "dangling"

    def test_handles_variant_tag_names(self) -> None:
        raw = "<thinking>x</thinking>y"
        assert strip_thinking_response(raw) == "y"
        raw2 = "<reasoning>z</reasoning>ok"
        assert strip_thinking_response(raw2) == "ok"

    def test_no_tags_returns_trimmed(self) -> None:
        assert strip_thinking_response("  hello  ") == "hello"

    def test_empty_and_none(self) -> None:
        assert strip_thinking_response("") == ""
        assert strip_thinking_response(None) is None  # type: ignore[arg-type]

    def test_json_body_preserved(self) -> None:
        raw = '<think>\n\n</think>\n\n{"decision": "execute", "entity_ids": ["light.x"]}'
        out = strip_thinking_response(raw)
        assert out == '{"decision": "execute", "entity_ids": ["light.x"]}'


class TestStripClosingBoilerplate:
    def test_removes_you_may_observe_phrase(self) -> None:
        raw = (
            "The overhead lights are dimmed to 30%. "
            "You may observe that I do not require further confirmation."
        )
        out = strip_closing_boilerplate(raw)
        assert "require further confirmation" not in out.lower()
        assert out.startswith("The overhead lights are dimmed to 30%.")

    def test_removes_bare_i_do_not_require(self) -> None:
        raw = "Kitchen, extinguished. I do not require further confirmation."
        out = strip_closing_boilerplate(raw)
        assert out == "Kitchen, extinguished."

    def test_removes_no_further_confirmation_required(self) -> None:
        raw = "Bedroom lights at 50%. No further confirmation required."
        out = strip_closing_boilerplate(raw)
        assert out == "Bedroom lights at 50%."

    def test_removes_compliance_logged(self) -> None:
        raw = "Done. Your compliance has been logged."
        out = strip_closing_boilerplate(raw)
        assert out == "Done."

    def test_removes_stacked_closers(self) -> None:
        raw = (
            "Office is dim. Your compliance has been logged. "
            "No additional action is required."
        )
        out = strip_closing_boilerplate(raw)
        assert "compliance" not in out.lower()
        assert "action is required" not in out.lower()
        assert out.startswith("Office is dim.")

    def test_mid_text_reference_kept(self) -> None:
        """Only trailing boilerplate gets stripped; mid-prose references stay."""
        raw = (
            "I do not require further confirmation for routine tasks, "
            "but this one I'll highlight."
        )
        out = strip_closing_boilerplate(raw)
        # Mid-text "I do not require…" stays because it isn't the
        # terminal phrase.
        assert "further confirmation for routine" in out
        assert "highlight" in out

    def test_no_boilerplate_untouched(self) -> None:
        clean = "Turning on the lamp. Mind the dust."
        assert strip_closing_boilerplate(clean) == clean

    def test_empty_safe(self) -> None:
        assert strip_closing_boilerplate("") == ""
        assert strip_closing_boilerplate(None) is None  # type: ignore[arg-type]

    def test_case_insensitive(self) -> None:
        raw = "Ok. I DO NOT REQUIRE FURTHER CONFIRMATION"
        out = strip_closing_boilerplate(raw)
        assert "confirmation" not in out.lower()

    def test_aperture_science_closer_stripped(self) -> None:
        raw = "Dim, as requested. The Enrichment Center thanks you."
        out = strip_closing_boilerplate(raw)
        assert "thanks you" not in out.lower()
        assert "enrichment center" not in out.lower()
