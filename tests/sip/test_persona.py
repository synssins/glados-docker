"""Tests for glados.sip.persona — system-prompt fragment + canned responses."""
from __future__ import annotations

import pathlib

import pytest

from glados.sip.persona import (
    CANNED_TEXT,
    PHONE_CALL_PROMPT_FRAGMENT,
    bake_canned_responses,
    get_canned_text,
)


# ---------------------------------------------------------------------------
# System-prompt fragment
# ---------------------------------------------------------------------------

def test_phone_prompt_fragment_is_non_empty_and_describes_phone_mode() -> None:
    assert PHONE_CALL_PROMPT_FRAGMENT
    # Spec keywords that should be present
    text = PHONE_CALL_PROMPT_FRAGMENT.lower()
    assert "phone" in text
    assert "potato" in text  # the operator's specific framing
    assert "no markdown" in text
    # Should NOT contain anything that displaces the existing persona
    assert "ignore previous" not in text
    assert "you are an ai" not in text


# ---------------------------------------------------------------------------
# Canned text registry
# ---------------------------------------------------------------------------

def test_all_required_labels_present() -> None:
    """The labels referenced by the IVR / PIN gate / call_session must exist."""
    required = {
        "greeting",
        "pin_success",
        "pin_fail_1", "pin_fail_2", "pin_fail_final",
        "drop_to_freeform",
        "menu_no_input_hangup",
        "goodbye",
    }
    assert required.issubset(set(CANNED_TEXT))


def test_all_canned_texts_are_non_empty() -> None:
    for label, text in CANNED_TEXT.items():
        assert text, f"canned label {label!r} is empty"
        assert text.strip() == text  # no leading/trailing whitespace


def test_pin_fail_texts_count_down() -> None:
    """Failure variants should mention attempts remaining in descending order."""
    assert "Two attempts remaining" in CANNED_TEXT["pin_fail_1"]
    assert "One attempt remaining" in CANNED_TEXT["pin_fail_2"]
    assert "denied" in CANNED_TEXT["pin_fail_final"].lower()


def test_pin_success_aligns_with_persona() -> None:
    """Success line should preserve the displeased / potato tone."""
    text = CANNED_TEXT["pin_success"].lower()
    assert "phone" in text


def test_get_canned_text_returns_text() -> None:
    assert get_canned_text("greeting") == CANNED_TEXT["greeting"]


def test_get_canned_text_unknown_label_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown canned label"):
        get_canned_text("does_not_exist")


# ---------------------------------------------------------------------------
# bake_canned_responses
# ---------------------------------------------------------------------------

def _tts_factory():
    """Return (mock_tts_callable, calls_list) — calls_list captures every text rendered."""
    calls: list[str] = []

    async def mock_tts(text: str) -> bytes:
        calls.append(text)
        return f"audio[{text[:20]}...]".encode()

    return mock_tts, calls


@pytest.mark.asyncio
async def test_bake_renders_every_label_when_labels_omitted() -> None:
    mock_tts, calls = _tts_factory()
    out = await bake_canned_responses(mock_tts)
    assert set(out.keys()) == set(CANNED_TEXT.keys())
    # Every text was sent through the TTS callable
    assert len(calls) == len(CANNED_TEXT)


@pytest.mark.asyncio
async def test_bake_subset_only_renders_named_labels() -> None:
    mock_tts, calls = _tts_factory()
    out = await bake_canned_responses(
        mock_tts, labels=["greeting", "pin_success"],
    )
    assert set(out.keys()) == {"greeting", "pin_success"}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_bake_unknown_label_raises_keyerror() -> None:
    mock_tts, _ = _tts_factory()
    with pytest.raises(KeyError):
        await bake_canned_responses(mock_tts, labels=["nonexistent"])


@pytest.mark.asyncio
async def test_bake_audio_bytes_non_empty() -> None:
    mock_tts, _ = _tts_factory()
    out = await bake_canned_responses(mock_tts)
    for label, audio in out.items():
        assert audio, f"baked audio for {label} is empty"


@pytest.mark.asyncio
async def test_bake_writes_files_when_output_dir_given(tmp_path: pathlib.Path) -> None:
    mock_tts, _ = _tts_factory()
    out = await bake_canned_responses(
        mock_tts,
        labels=["greeting", "goodbye"],
        output_dir=tmp_path,
        file_extension="mp3",
    )
    # Both files created with the right extension
    assert (tmp_path / "greeting.mp3").exists()
    assert (tmp_path / "goodbye.mp3").exists()
    # Contents match the in-memory bytes
    assert (tmp_path / "greeting.mp3").read_bytes() == out["greeting"]


@pytest.mark.asyncio
async def test_bake_creates_output_dir_if_missing(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "deep" / "nested" / "canned"
    mock_tts, _ = _tts_factory()
    await bake_canned_responses(mock_tts, labels=["greeting"], output_dir=target)
    assert (target / "greeting.mp3").exists()


@pytest.mark.asyncio
async def test_bake_output_dir_optional() -> None:
    """Omitting output_dir should not write anything to disk."""
    mock_tts, _ = _tts_factory()
    out = await bake_canned_responses(mock_tts, labels=["greeting"])
    assert "greeting" in out  # still returned in memory
