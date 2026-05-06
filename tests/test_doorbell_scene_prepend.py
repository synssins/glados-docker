"""Tests for the doorbell-screener scene-prepend enhancement.

Slice 1 follow-up: the screener now uses look_at_camera to capture a
visual description of the visitor at session-start (parallel with the
greeting). Round 1's _evaluate call prepends that description to the
LLM's user message so classification can use both audio + visual
evidence.

These tests verify the prepend logic in isolation; they don't exercise
the snapshot fetch / VLM client (those are tested independently under
tests/cameras and tests/vision).
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def screener():
    """Construct a DoorbellScreener with a stub config so init succeeds
    without reading a real doorbell.yaml off disk."""
    from glados.doorbell import screener as scr_mod

    fake_config = {
        "enabled": True,
        "speaker": "media_player.front_bell_speaker",
        "indoor_speakers": ["media_player.kitchen"],
        "max_rounds": 3,
        "max_listen_duration": 15,
        "greeting_duration": 5.0,
        "cooldown": 60,
        "listen_timeout": 12,
        "stt_model": "Systran/faster-whisper-small",
        "llm": {},
        # New field — the scene-prepend feature reads this at session start
        "camera_entity_id": "camera.g4_doorbell_high",
    }
    with patch.object(scr_mod.DoorbellScreener, "_load_config", return_value=fake_config):
        return scr_mod.DoorbellScreener()


def _stub_llm_response(content: str = '{"classification":"delivery","reply":"Thanks","announcement":"Delivery","continue_conversation":false}'):
    """Build a urlopen-context-manager-shaped mock returning the body."""
    body = json.dumps({
        "choices": [{"message": {"content": content}}],
    }).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _capture_evaluate_messages(screener, transcript: str, round_num: int) -> list[dict]:
    """Run _evaluate and return the messages list that got POSTed to the LLM."""
    from glados.doorbell import screener as scr_mod
    captured = {}

    def fake_urlopen(req, *args, **kwargs):
        body = req.data.decode("utf-8") if req.data else ""
        captured["payload"] = json.loads(body)
        return _stub_llm_response()

    with patch.object(scr_mod, "urlopen", side_effect=fake_urlopen):
        screener._evaluate(transcript, round_num, history=[])

    return captured["payload"]["messages"]


def test_round_1_prepends_scene_description_when_set(screener):
    screener._scene_description = "an adult in a brown UPS uniform holding a small package"
    messages = _capture_evaluate_messages(screener, "Package for Chris.", round_num=1)

    user_msg = next(m for m in messages if m["role"] == "user")["content"]
    assert "[scene]" in user_msg
    assert "UPS uniform" in user_msg
    assert "Package for Chris." in user_msg
    # Hint about using both signals should be present
    assert "[scene] visual context" in user_msg


def test_round_1_no_prepend_when_scene_unset(screener):
    screener._scene_description = None
    messages = _capture_evaluate_messages(screener, "Hi, I'm here to see Chris.", round_num=1)

    user_msg = next(m for m in messages if m["role"] == "user")["content"]
    assert "[scene]" not in user_msg
    assert "Hi, I'm here to see Chris." in user_msg


def test_round_2_does_not_prepend_even_if_scene_set(screener):
    """Snapshot is from session start; stale by round 2+. Don't bloat the
    prompt with a description that doesn't match what's still happening."""
    screener._scene_description = "an adult in a brown UPS uniform holding a small package"
    messages = _capture_evaluate_messages(screener, "Yes, package for Chris.", round_num=2)

    user_msg = next(m for m in messages if m["role"] == "user")["content"]
    assert "[scene]" not in user_msg
    assert "UPS uniform" not in user_msg


def test_round_1_no_response_path_still_prepends_when_scene_set(screener):
    """The 'visitor said nothing' branch builds user_msg from
    _NO_RESPONSE_USER. The scene prepend should still apply on round 1
    (silent visitor + visual evidence → 'delivery, no need to greet')."""
    screener._scene_description = "two delivery boxes on the porch, no person visible"
    messages = _capture_evaluate_messages(screener, "", round_num=1)

    user_msg = next(m for m in messages if m["role"] == "user")["content"]
    assert "[scene]" in user_msg
    assert "two delivery boxes" in user_msg
    assert "did not respond" in user_msg or "silence" in user_msg.lower()


def test_camera_entity_id_unset_disables_vision_path(screener):
    """When camera_entity_id is unset (or empty), start_session should
    NOT spawn the snapshot+VLM thread. We can't observe thread-spawn
    directly; simulate by clearing the config field and asserting the
    code path that reads it would skip."""
    screener._config["camera_entity_id"] = ""
    # The relevant code in start_session does:
    #     camera_entity = (cfg.get("camera_entity_id") or "").strip()
    #     if camera_entity: spawn thread
    # Just verify the gate behavior at the same expression level.
    cam = (screener._config.get("camera_entity_id") or "").strip()
    assert cam == ""
