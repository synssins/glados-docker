"""Dash normalization on TTS path.

Piper does not produce prosodic pauses for Unicode dashes. The
_normalize_dashes helper converts em/en-dashes and spaced hyphens to ", "
before text reaches the synthesizer. Both call sites use this helper:
  - glados/api/tts.py:generate_speech (engine path)
  - glados/webui/tts_ui.py:_apply_pronunciation_to_text (WebUI /api/generate)
"""
import pytest
from glados.webui.tts_ui import _normalize_dashes
from glados.api.tts import _normalize_dashes as _normalize_dashes_engine


@pytest.mark.parametrize("fn", [_normalize_dashes, _normalize_dashes_engine])
class TestNormalizeDashes:
    def test_em_dash_to_comma(self, fn):
        assert fn("She paused \u2014 and then continued.") == "She paused, and then continued."

    def test_en_dash_to_comma(self, fn):
        assert fn("Pages 12\u201318 are missing.") == "Pages 12, 18 are missing."

    def test_spaced_hyphen_to_comma(self, fn):
        assert fn("Hi - there.") == "Hi, there."

    def test_compound_hyphen_preserved(self, fn):
        assert fn("tea-cup") == "tea-cup"

    def test_no_double_commas(self, fn):
        assert fn("Wait \u2014 , \u2014 pause.") == "Wait, pause."

    def test_empty_passthrough(self, fn):
        assert fn("") == ""

    def test_none_passthrough(self, fn):
        assert fn(None) is None
