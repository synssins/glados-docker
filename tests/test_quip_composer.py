"""Tests for glados.persona.quip_selector + composer — Phase 8.7."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from glados.persona import (
    CHIME_SENTINEL,
    ComposeRequest,
    QuipLibrary,
    QuipRequest,
    classify_intent,
    compose,
    format_entity_count,
    mood_from_affect,
)


# ---------------------------------------------------------------------------
# QuipLibrary loading
# ---------------------------------------------------------------------------

class TestQuipLibraryLoad:
    def test_missing_root_returns_empty_library(self, tmp_path: Path) -> None:
        lib = QuipLibrary.load(tmp_path / "does_not_exist")
        assert lib.is_empty()

    def test_empty_root_returns_empty_library(self, tmp_path: Path) -> None:
        # Directory exists but contains no .txt files.
        (tmp_path / "something_else").mkdir()
        lib = QuipLibrary.load(tmp_path)
        assert lib.is_empty()

    def test_loads_txt_files_and_skips_comments_and_blanks(
        self, tmp_path: Path,
    ) -> None:
        d = tmp_path / "command_ack" / "turn_on"
        d.mkdir(parents=True)
        (d / "normal.txt").write_text(
            "# header comment\n"
            "Line one.\n"
            "\n"                    # blank — ignored
            "   # indented comment\n"
            "Line two.\n",
            encoding="utf-8",
        )
        lib = QuipLibrary.load(tmp_path)
        assert not lib.is_empty()
        picked = lib.pick(QuipRequest(event_category="command_ack", intent="turn_on"))
        assert picked in {"Line one.", "Line two."}


# ---------------------------------------------------------------------------
# Selector fallback chain
# ---------------------------------------------------------------------------

@pytest.fixture
def _stocked_library(tmp_path: Path) -> QuipLibrary:
    # Build a deterministic corpus:
    #   command_ack/turn_on/normal.txt        → "NORMAL"
    #   command_ack/turn_on/cranky.txt        → "CRANKY"
    #   command_ack/turn_on/evening.txt       → "EVENING"
    #   global/acknowledgement.txt            → "GLOBAL"
    paths = {
        "command_ack/turn_on/normal.txt":    "NORMAL",
        "command_ack/turn_on/cranky.txt":    "CRANKY",
        "command_ack/turn_on/evening.txt":   "EVENING",
        "global/acknowledgement.txt":        "GLOBAL",
    }
    for rel, content in paths.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content + "\n", encoding="utf-8")
    return QuipLibrary.load(tmp_path)


class TestSelectorFallback:
    def test_most_specific_mood_wins(self, _stocked_library: QuipLibrary) -> None:
        out = _stocked_library.pick(QuipRequest(
            event_category="command_ack", intent="turn_on", mood="cranky",
        ))
        assert out == "CRANKY"

    def test_falls_back_to_normal_when_mood_file_missing(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        out = _stocked_library.pick(QuipRequest(
            event_category="command_ack", intent="turn_on", mood="amused",
        ))
        # amused.txt not stocked — falls to normal.txt
        assert out == "NORMAL"

    def test_time_of_day_file_when_present(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        out = _stocked_library.pick(QuipRequest(
            event_category="command_ack", intent="turn_on",
            mood="unknown", time_of_day="evening",
        ))
        assert out == "EVENING"

    def test_unknown_intent_falls_through_to_global(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        out = _stocked_library.pick(QuipRequest(
            event_category="command_ack", intent="explode_sun",
        ))
        assert out == "GLOBAL"

    def test_invalid_category_returns_empty_string(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        out = _stocked_library.pick(QuipRequest(
            event_category="not_a_category", intent="turn_on",
        ))
        assert out == ""

    def test_seeded_rng_gives_deterministic_pick(
        self, tmp_path: Path,
    ) -> None:
        d = tmp_path / "command_ack" / "turn_on"
        d.mkdir(parents=True)
        (d / "normal.txt").write_text(
            "A\nB\nC\n", encoding="utf-8",
        )
        lib = QuipLibrary.load(tmp_path)
        rng = random.Random(42)
        out = lib.pick(
            QuipRequest(event_category="command_ack", intent="turn_on"),
            rng=rng,
        )
        assert out in {"A", "B", "C"}


# ---------------------------------------------------------------------------
# Mood mapping
# ---------------------------------------------------------------------------

class TestMoodFromAffect:
    def test_none_affect_is_normal(self) -> None:
        assert mood_from_affect(None) == "normal"

    def test_empty_dict_is_normal(self) -> None:
        assert mood_from_affect({}) == "normal"

    def test_anger_above_threshold_is_cranky(self) -> None:
        assert mood_from_affect({"anger": 0.8, "joy": 0.2}) == "cranky"

    def test_joy_above_threshold_is_amused(self) -> None:
        assert mood_from_affect({"joy": 0.8}) == "amused"

    def test_anger_ties_break_toward_cranky(self) -> None:
        """Plan spec: anger > 0.6 wins first. With both elevated,
        cranky is selected."""
        out = mood_from_affect({"anger": 0.9, "joy": 0.9})
        assert out == "cranky"

    def test_non_numeric_fields_degrade_to_normal(self) -> None:
        assert mood_from_affect({"anger": "hot"}) == "normal"


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class TestCompose:
    def test_llm_mode_passes_speech_through(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Illumination restored.", mode="LLM",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "LLM"
        assert out.text == "Illumination restored."

    def test_silent_mode_emits_empty_string(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="x", mode="silent",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "silent"
        assert out.text == ""

    def test_chime_mode_returns_sentinel(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="x", mode="chime",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "chime"
        assert out.text == CHIME_SENTINEL

    def test_quip_mode_returns_a_library_line(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Illumination restored.", mode="quip",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "quip"
        assert out.text in {"NORMAL", "CRANKY", "EVENING", "GLOBAL"}

    def test_quip_mode_with_empty_library_falls_back_to_llm(
        self, tmp_path: Path,
    ) -> None:
        empty = QuipLibrary.load(tmp_path)
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Illumination restored.", mode="quip",
        )
        out = compose(req, empty)
        assert out.mode == "LLM"
        assert out.text == "Illumination restored."

    def test_invalid_mode_falls_back_to_llm(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="x", mode="garbage",  # type: ignore[arg-type]
        )
        out = compose(req, _stocked_library)
        assert out.mode == "LLM"
        assert out.text == "x"


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class TestClassifyIntent:
    def test_turn_off(self) -> None:
        assert classify_intent("light.turn_off", "off") == "turn_off"

    def test_turn_on_bare(self) -> None:
        assert classify_intent("light.turn_on", "turn on the lamp") == "turn_on"

    def test_turn_on_with_dim_keyword_maps_to_brightness_down(self) -> None:
        assert classify_intent(
            "light.turn_on", "dim the lamp",
        ) == "brightness_down"

    def test_turn_on_with_brighten_keyword(self) -> None:
        assert classify_intent(
            "light.turn_on", "brighten the hallway",
        ) == "brightness_up"

    def test_scene_turn_on(self) -> None:
        assert classify_intent(
            "scene.turn_on", "evening scene",
        ) == "scene_activate"

    def test_unknown_service_is_generic(self) -> None:
        assert classify_intent("cover.close", "close the garage") == "generic"


# ---------------------------------------------------------------------------
# format_entity_count
# ---------------------------------------------------------------------------

class TestFormatEntityCount:
    def test_zero(self) -> None:
        assert format_entity_count(0) == ""

    def test_one(self) -> None:
        assert format_entity_count(1) == "one"

    def test_two(self) -> None:
        assert format_entity_count(2) == "both"

    def test_three(self) -> None:
        assert format_entity_count(3) == "all three"

    def test_seven(self) -> None:
        assert format_entity_count(7) == "all 7"

    def test_many(self) -> None:
        assert format_entity_count(42) == "the entire set"


# ---------------------------------------------------------------------------
# Phase 8.7d — LLM-safe composer
# ---------------------------------------------------------------------------

class TestLLMSafeComposer:
    """The dedicated LLM composer never sees device names. This
    suite confirms:
      1. compose() with mode=LLM_safe calls the LLM and returns its
         text when configured.
      2. Falls back to LLM passthrough when URL/model unset or the
         call fails.
      3. The tidy step strips quote wrappers, <think> blocks, and
         multi-line commentary."""

    def test_llm_safe_mode_uses_composer_when_configured(
        self, monkeypatch, _stocked_library: QuipLibrary,
    ) -> None:
        captured: list[tuple] = []

        def _fake_compose(req, *, ollama_url, model, timeout_s=5.0):
            captured.append((req, ollama_url, model))
            return "Compliance. Minor adjustment recorded."

        monkeypatch.setattr(
            "glados.persona.composer.compose_speech", _fake_compose,
            raising=False,
        )
        # Above monkeypatch is insufficient — composer.py imports
        # compose_speech lazily inside compose(). Re-patch the
        # source module's attribute so the lazy import sees the fake.
        import glados.persona.llm_composer as _llmc
        monkeypatch.setattr(_llmc, "compose_speech", _fake_compose)

        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Passthrough speech.",
            mode="LLM_safe",
            llm_safe_url="http://localhost:11434",
            llm_safe_model="qwen3:8b",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "LLM_safe"
        assert "Compliance" in out.text
        assert captured, "fake composer should have been called"

    def test_llm_safe_without_url_falls_back_to_passthrough(
        self, _stocked_library: QuipLibrary,
    ) -> None:
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Fallback speech.",
            mode="LLM_safe",
            llm_safe_url="", llm_safe_model="",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "LLM"
        assert out.text == "Fallback speech."

    def test_llm_safe_empty_response_falls_back(
        self, monkeypatch, _stocked_library: QuipLibrary,
    ) -> None:
        import glados.persona.llm_composer as _llmc
        monkeypatch.setattr(
            _llmc, "compose_speech",
            lambda req, **_: "",  # simulate network/parse failure
        )
        req = ComposeRequest(
            event_category="command_ack", intent="turn_on",
            llm_speech="Original.",
            mode="LLM_safe",
            llm_safe_url="http://fake", llm_safe_model="qwen3:8b",
        )
        out = compose(req, _stocked_library)
        assert out.mode == "LLM"
        assert out.text == "Original."


class TestLLMComposerTidy:
    def test_strips_think_block(self) -> None:
        from glados.persona.llm_composer import _tidy
        raw = "<think>reasoning</think>The chamber is now lit."
        assert _tidy(raw) == "The chamber is now lit."

    def test_strips_orphan_open_think_tag(self) -> None:
        """Regression: Qwen3:8b sometimes opens <think> but runs out
        of token budget before closing it, so we see just the raw
        tag and reasoning. Everything from <think> onward is
        reasoning and should be discarded."""
        from glados.persona.llm_composer import _tidy
        raw = "Compliance. <think>the user wants me to"
        assert _tidy(raw) == "Compliance."

    def test_bare_open_think_returns_empty(self) -> None:
        """When the reply IS only '<think>' with no prefix, there's
        no user-visible speech to salvage — return empty so the
        caller falls back to passthrough."""
        from glados.persona.llm_composer import _tidy
        assert _tidy("<think>") == ""
        assert _tidy("<think>reasoning but no reply") == ""

    def test_strips_wrapping_quotes(self) -> None:
        from glados.persona.llm_composer import _tidy
        assert _tidy('"Silence. Compliance."') == "Silence. Compliance."

    def test_takes_only_first_line(self) -> None:
        from glados.persona.llm_composer import _tidy
        raw = "Off.\nAdditional commentary.\nThird line."
        assert _tidy(raw) == "Off."

    def test_rejects_json_like_output(self) -> None:
        from glados.persona.llm_composer import _tidy
        assert _tidy('{"speech": "Off."}') == ""

    def test_rejects_code_fence(self) -> None:
        from glados.persona.llm_composer import _tidy
        assert _tidy("```\nOff.\n```") == ""

    def test_empty_input_returns_empty(self) -> None:
        from glados.persona.llm_composer import _tidy
        assert _tidy("") == ""
        assert _tidy("   ") == ""
