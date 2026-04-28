"""Pronunciation overrides apply on the TTS Generator synthesis path.

The /api/generate handler in tts_ui.py was bypassing the SpokenTextConverter
that the engine uses (glados/api/tts.py:generate_speech). Operator-configured
pronunciation corrections (e.g., AI -> Aye Eye) silently didn't apply on the
TTS Generator. This test ensures the converter is called.
"""
from unittest.mock import MagicMock, patch
from glados.webui.tts_ui import _apply_pronunciation_to_text


def test_apply_overrides_calls_converter():
    """The text-conversion helper should run input through SpokenTextConverter."""
    fake_converter = MagicMock()
    fake_converter.text_to_spoken.return_value = "Hello Aye Eye"
    with patch("glados.webui.tts_ui._get_pronunciation_converter", return_value=fake_converter):
        result = _apply_pronunciation_to_text("Hello AI")
    fake_converter.text_to_spoken.assert_called_once_with("Hello AI")
    assert result == "Hello Aye Eye"


def test_apply_overrides_returns_input_when_converter_unavailable():
    """If converter setup raises or returns None, pass through original text."""
    with patch("glados.webui.tts_ui._get_pronunciation_converter", return_value=None):
        assert _apply_pronunciation_to_text("Hello AI") == "Hello AI"


def test_apply_overrides_handles_converter_exception():
    """Defensive: swallow converter exceptions and return original text."""
    fake_converter = MagicMock()
    fake_converter.text_to_spoken.side_effect = RuntimeError("converter broke")
    with patch("glados.webui.tts_ui._get_pronunciation_converter", return_value=fake_converter):
        result = _apply_pronunciation_to_text("Hello AI")
    assert result == "Hello AI"
