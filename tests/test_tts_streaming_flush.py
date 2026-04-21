"""Phase 8.11 — streaming-TTS pacing knobs + sentence-boundary flush.

The operator-perceived complaint: short replies ("Affirmative.",
"Off.", 13 chars) stall because pre-8.11 the flush predicate was
"current token is punctuation AND accumulated >= threshold." A
13-char sentence never meets a 30-char threshold, so the first
TTS call waited for a second sentence to come in. Under
``sentence_boundary_flush=True`` (the new default) the threshold
check is bypassed at punctuation — a complete sentence always
fires regardless of length.

Tests exercise the accumulator shape directly via a minimal
stand-in for the ``LLMProcessor`` flush predicate.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.core.config_store import AudioConfig, GladosConfigStore


# ── Config surface ─────────────────────────────────────────────────


def test_audio_config_defaults_phase_8_11() -> None:
    a = AudioConfig()
    assert a.first_tts_flush_chars == 30
    assert a.min_tts_flush_chars == 80
    assert a.sentence_boundary_flush is True


def test_audio_config_accepts_operator_overrides() -> None:
    a = AudioConfig(
        first_tts_flush_chars=20,
        min_tts_flush_chars=120,
        sentence_boundary_flush=False,
    )
    assert a.first_tts_flush_chars == 20
    assert a.min_tts_flush_chars == 120
    assert a.sentence_boundary_flush is False


def test_audio_config_yaml_round_trip(tmp_path: Path) -> None:
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    store.update_section(
        "audio",
        {
            "first_tts_flush_chars": 25,
            "min_tts_flush_chars": 100,
            "sentence_boundary_flush": False,
        },
    )
    reread = yaml.safe_load(
        (tmp_path / "audio.yaml").read_text(encoding="utf-8"),
    )
    assert reread["first_tts_flush_chars"] == 25
    assert reread["min_tts_flush_chars"] == 100
    assert reread["sentence_boundary_flush"] is False
    assert store.audio.min_tts_flush_chars == 100


# ── Sentence-boundary flush predicate (mirrors LLMProcessor logic) ─


def _flush_decision(
    accumulated_chars: int,
    first_flush_done: bool,
    first_threshold: int,
    threshold: int,
    sentence_boundary_flush: bool,
    is_sentence_end: bool,
) -> bool:
    """Pure-function mirror of the predicate at
    `glados/core/llm_processor.py:~990` so the test suite can hit
    every branch without spinning up the full pipeline."""
    if not is_sentence_end:
        return False
    t = first_threshold if not first_flush_done else threshold
    return sentence_boundary_flush or accumulated_chars >= t


def test_short_reply_flushes_immediately_under_boundary_flush() -> None:
    """The operator-reported "Affirmative." case: 13 chars, first
    flush, threshold 30 — pre-8.11 was STALL, post-8.11 is FLUSH."""
    assert _flush_decision(
        accumulated_chars=13,
        first_flush_done=False,
        first_threshold=30,
        threshold=80,
        sentence_boundary_flush=True,
        is_sentence_end=True,
    ) is True


def test_short_reply_stalls_when_boundary_flush_disabled() -> None:
    """Operator A/B: turning off sentence-boundary flush restores
    pre-8.11 behaviour — 13 chars < 30 threshold → STALL."""
    assert _flush_decision(
        accumulated_chars=13,
        first_flush_done=False,
        first_threshold=30,
        threshold=80,
        sentence_boundary_flush=False,
        is_sentence_end=True,
    ) is False


def test_mid_sentence_token_never_flushes() -> None:
    """Only sentence-end tokens trigger the predicate; a mid-sentence
    token passes through regardless of threshold state."""
    for boundary in (True, False):
        assert _flush_decision(
            accumulated_chars=999,
            first_flush_done=False,
            first_threshold=30,
            threshold=80,
            sentence_boundary_flush=boundary,
            is_sentence_end=False,
        ) is False


def test_subsequent_flush_uses_second_threshold() -> None:
    """First flush uses ``first_threshold`` (low); subsequent flushes
    use ``threshold`` (higher). Boundary-flush bypasses both."""
    # Threshold-only path: 50 chars < 80 subsequent threshold → STALL
    assert _flush_decision(
        accumulated_chars=50,
        first_flush_done=True,
        first_threshold=30,
        threshold=80,
        sentence_boundary_flush=False,
        is_sentence_end=True,
    ) is False
    # Boundary-flush path: fires regardless
    assert _flush_decision(
        accumulated_chars=50,
        first_flush_done=True,
        first_threshold=30,
        threshold=80,
        sentence_boundary_flush=True,
        is_sentence_end=True,
    ) is True


def test_exactly_at_threshold_fires() -> None:
    """``>=`` not ``>`` — a sentence exactly at the threshold should
    flush even without boundary-flush."""
    assert _flush_decision(
        accumulated_chars=30,
        first_flush_done=False,
        first_threshold=30,
        threshold=80,
        sentence_boundary_flush=False,
        is_sentence_end=True,
    ) is True


def test_long_reply_fires_via_threshold_alone() -> None:
    """Long sentence hits threshold on its own; boundary-flush off
    is fine, boundary-flush on is also fine — either way FLUSH."""
    for boundary in (True, False):
        assert _flush_decision(
            accumulated_chars=200,
            first_flush_done=False,
            first_threshold=30,
            threshold=80,
            sentence_boundary_flush=boundary,
            is_sentence_end=True,
        ) is True


# ── LLMProcessor constructor accepts the new flag ─────────────────


def test_llm_processor_sentence_boundary_flush_param() -> None:
    """Quick smoke: the new constructor arg is plumbed through and
    stored. Full integration is covered by existing LLMProcessor
    tests; we just verify the field is reachable."""
    from glados.core.llm_processor import LanguageModelProcessor
    import queue as _q, threading as _t

    lp = LanguageModelProcessor(
        llm_input_queue=_q.Queue(),
        tool_calls_queue=_q.Queue(),
        tts_input_queue=_q.Queue(),
        conversation_store=None,  # type: ignore[arg-type]
        completion_url="http://localhost/api/chat",  # type: ignore[arg-type]
        model_name="test",
        api_key=None,
        processing_active_event=_t.Event(),
        shutdown_event=_t.Event(),
        sentence_boundary_flush=False,
    )
    assert lp._sentence_boundary_flush is False

    lp2 = LanguageModelProcessor(
        llm_input_queue=_q.Queue(),
        tool_calls_queue=_q.Queue(),
        tts_input_queue=_q.Queue(),
        conversation_store=None,  # type: ignore[arg-type]
        completion_url="http://localhost/api/chat",  # type: ignore[arg-type]
        model_name="test",
        api_key=None,
        processing_active_event=_t.Event(),
        shutdown_event=_t.Event(),
        # default
    )
    assert lp2._sentence_boundary_flush is True
