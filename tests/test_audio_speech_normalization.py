"""Regression: /v1/audio/speech must normalize text before synthesis.

Pre-fix the OpenAI-compat ``/v1/audio/speech`` HTTP handler bypassed
``SpokenTextConverter`` and fed raw text to Piper. Operator-visible
symptom: "It will be 55 degrees today" was synthesized as "It will be
degrees today" — Piper silently dropped digit-only tokens. The fix
routes the handler through ``glados.api.tts.generate_speech`` which
runs ``text_to_spoken`` first.

This test pins the contract: ``generate_speech`` MUST pass normalized
text (digits → words, °F → "degrees", mph → "miles per hour", etc.)
to the synthesizer.
"""
from __future__ import annotations

import numpy as np
import pytest


def _patch_fake_synth(monkeypatch) -> dict:
    """Inject a fake synth into the api/tts cache so we can capture what
    text the synthesizer was called with — without spinning up Piper."""
    from glados.api import tts as _t

    captured: dict = {"text": None, "kwargs": None}

    class _FakeSynth:
        sample_rate = 22050

        def generate_speech_audio(self, text: str, **kwargs):
            captured["text"] = text
            captured["kwargs"] = kwargs
            return np.zeros(1024, dtype=np.float32)

    monkeypatch.setitem(_t._synthesizers, "glados", _FakeSynth())
    # Reset the cached converter so it picks up live config; downstream
    # tests that mutate cfg.tts_pronunciation won't pollute this run.
    _t.reset_converter()
    return captured


def test_generate_speech_expands_integer(monkeypatch) -> None:
    captured = _patch_fake_synth(monkeypatch)
    from glados.api.tts import generate_speech

    audio, sr = generate_speech("The temperature will be 55 degrees today.")
    assert sr == 22050
    assert audio.size > 0
    spoken = captured["text"].lower()
    assert "fifty-five" in spoken, f"digit '55' not normalized: {spoken!r}"
    assert "55" not in spoken, f"raw digits leaked through: {spoken!r}"


def test_generate_speech_expands_mph(monkeypatch) -> None:
    captured = _patch_fake_synth(monkeypatch)
    from glados.api.tts import generate_speech

    generate_speech("Wind is 13 mph.")
    spoken = captured["text"].lower()
    assert "thirteen miles per hour" in spoken, spoken


def test_generate_speech_expands_percent(monkeypatch) -> None:
    captured = _patch_fake_synth(monkeypatch)
    from glados.api.tts import generate_speech

    generate_speech("It is 81% humidity.")
    spoken = captured["text"].lower()
    assert "eighty-one percent" in spoken, spoken
    assert "%" not in spoken


def test_generate_speech_passes_through_kwargs(monkeypatch) -> None:
    """length_scale / noise_scale / noise_w must reach the synth."""
    captured = _patch_fake_synth(monkeypatch)
    from glados.api.tts import generate_speech

    generate_speech("test", length_scale=1.2, noise_scale=0.6, noise_w=0.7)
    assert captured["kwargs"] == {"length_scale": 1.2, "noise_scale": 0.6, "noise_w": 0.7}
