"""Regression tests for the doorbell listening pipeline (2026-06-09).

Live failure, observed end-to-end on a real ring: greeting plays, the
visitor speaks, but GLaDOS replies ~35 s later with the invalid-JSON
fallback ("I'm sorry, I wasn't able to process that"). Three stacked
causes:

1. `_evaluate` posts to an OpenAI `/v1/chat/completions` endpoint but
   builds an Ollama-style payload (`options.num_predict`) and parses the
   Ollama response shape (`result["message"]["content"]`). llama.cpp
   returns `choices[0].message.content`, so the parsed content is always
   "" -> JSONDecodeError -> hardcoded fallback reply. (Same bug class as
   the rewriter's 86c6bec.)
2. `_run_session` only transcribes the captured audio when the HA
   speaking sensor flipped on. When the sensor is missing/unreliable the
   visitor's recorded speech is silently discarded untranscribed. The
   capture must ALWAYS be transcribed — Whisper is the real speech
   detector; the sensor is only an early-stop optimization.
3. `_get_ha_state` swallowed every error as "unknown", so a nonexistent
   speaking-sensor entity (the live config gap) failed silently on every
   session. Unreadable sensors must be loud.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger


@pytest.fixture
def screener():
    """DoorbellScreener with a stub config (no doorbell.yaml on disk).

    greeting_duration=0 so _run_session doesn't really sleep.
    """
    from glados.doorbell import screener as scr_mod

    fake_config = {
        "enabled": True,
        "speaker": "media_player.front_bell_speaker",
        "indoor_speakers": ["media_player.kitchen"],
        "max_rounds": 3,
        "max_listen_duration": 15,
        "greeting_duration": 0.0,
        "cooldown": 60,
        "listen_timeout": 12,
        "silence_gap": 2.0,
        "stt_model": "Systran/faster-whisper-small",
        "llm": {},
        "speaking_sensor": "binary_sensor.front_bell_speaking",
    }
    with patch.object(scr_mod.DoorbellScreener, "_load_config", return_value=fake_config):
        return scr_mod.DoorbellScreener()


def _openai_response(content: str):
    """urlopen-context-manager mock returning an OpenAI chat-completions body."""
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


GOOD_JSON = (
    '{"classification":"delivery","reply":"Leave it at the door.",'
    '"announcement":"A delivery has arrived.","continue_conversation":false}'
)


# ── Fix 1: OpenAI wire format ────────────────────────────────────────────


def test_evaluate_parses_openai_response_shape(screener):
    """An OpenAI-shaped completion must round-trip into the evaluation dict
    (the live bug: Ollama-shape parsing read content='' and fell back)."""
    from glados.doorbell import screener as scr_mod

    with patch.object(scr_mod, "urlopen", return_value=_openai_response(GOOD_JSON)):
        result = screener._evaluate("UPS delivery", round_num=1, history=[])

    assert result["classification"] == "delivery"
    assert result["reply"] == "Leave it at the door."
    assert result["continue_conversation"] is False


def test_evaluate_sends_openai_params_not_ollama_options(screener):
    """Payload must use top-level OpenAI params (max_tokens, temperature);
    Ollama's `options` block is ignored by OpenAI-compatible servers."""
    from glados.doorbell import screener as scr_mod

    captured = {}

    def fake_urlopen(req, *args, **kwargs):
        captured["payload"] = json.loads(req.data.decode())
        return _openai_response(GOOD_JSON)

    with patch.object(scr_mod, "urlopen", side_effect=fake_urlopen):
        screener._evaluate("UPS delivery", round_num=1, history=[])

    payload = captured["payload"]
    assert "options" not in payload
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == pytest.approx(0.6)


def test_evaluate_strips_think_block_from_openai_content(screener):
    """Reasoning-family preludes still get stripped before JSON parsing."""
    from glados.doorbell import screener as scr_mod

    content = f"<think>hmm a package</think>\n{GOOD_JSON}"
    with patch.object(scr_mod, "urlopen", return_value=_openai_response(content)):
        result = screener._evaluate("UPS delivery", round_num=1, history=[])

    assert result["classification"] == "delivery"


# ── Fix 2: always transcribe the capture ─────────────────────────────────


def _run_one_round(screener, transcribe_returns: str):
    """Drive one _run_session round with all I/O stubbed; return recorders."""
    calls = {"transcribe": [], "evaluate": []}

    def fake_transcribe(wav_path, skip_seconds=0):
        calls["transcribe"].append(skip_seconds)
        return transcribe_returns

    def fake_evaluate(transcript, round_num, history):
        calls["evaluate"].append(transcript)
        return {
            "classification": "unknown",
            "reply": "",
            "announcement": "Someone is at the door.",
            "continue_conversation": False,
        }

    from glados.doorbell import screener as scr_mod

    with (
        patch.object(scr_mod.time, "sleep"),
        patch.object(screener, "_start_capture", return_value=MagicMock()),
        patch.object(screener, "_play_on_speaker"),
        patch.object(screener, "_wait_for_speech", return_value=False),
        patch.object(screener, "_wait_for_silence"),
        patch.object(screener, "_stop_capture"),
        patch.object(screener, "_ensure_valid_wav", return_value=Path("capture.wav")),
        patch.object(screener, "_transcribe", side_effect=fake_transcribe),
        patch.object(screener, "_evaluate", side_effect=fake_evaluate),
        patch.object(screener, "_announce_inside"),
        patch.object(screener, "_generate_tts", return_value=None),
    ):
        screener._run_session("db_test", [], max_rounds=1)

    return calls


def test_capture_is_transcribed_even_without_sensor_speech(screener):
    """The recorded audio must reach STT even when the HA speaking sensor
    never fired — Whisper is the speech detector of record."""
    calls = _run_one_round(screener, transcribe_returns="Hi, UPS delivery for Chris")
    assert len(calls["transcribe"]) == 1
    assert calls["evaluate"] == ["Hi, UPS delivery for Chris"]


def test_round1_transcription_trims_greeting(screener):
    """Round 1 still trims the greeting bleed-through from the capture."""
    screener._config["greeting_duration"] = 4.5
    calls = _run_one_round(screener, transcribe_returns="hello")
    assert calls["transcribe"] == [4.5]


def test_empty_transcript_still_routes_to_no_response(screener):
    """Whisper returning '' (true silence) keeps the no_response path."""
    calls = _run_one_round(screener, transcribe_returns="")
    assert calls["evaluate"] == [""]


# ── Fix 3: unreadable speaking sensor is loud ────────────────────────────


def test_wait_for_speech_logs_error_when_sensor_unreadable(screener):
    """A speaking sensor that can't be read for the whole window must emit
    an ERROR naming the entity — not silently report 'no speech'."""
    from urllib.error import URLError

    from glados.doorbell import screener as scr_mod

    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m), level="ERROR")
    try:
        with patch.object(scr_mod, "urlopen", side_effect=URLError("not found")):
            detected = screener._wait_for_speech(timeout=0.1)
    finally:
        logger.remove(sink_id)

    assert detected is False
    joined = "\n".join(str(r) for r in records)
    assert "binary_sensor.front_bell_speaking" in joined
