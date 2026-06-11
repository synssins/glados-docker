"""Tests for doorbell screener round-2 fixes.

Fix 1: Abort session when door contact sensor reports open.
Fix 2: Configurable speaker-play timeout.
Fix 3: One announcement per visitor (suppress no_response duplicates).
Fix 4: Short visual identifier + carrier ID in scene prompt / eval prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_screener(extra_config=None):
    """Build a DoorbellScreener with a stub config."""
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
    if extra_config:
        fake_config.update(extra_config)

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


EVAL_JSON = (
    '{"classification":"unknown","reply":"Who is there?","announcement":"Someone is at the door.",'
    '"continue_conversation":false}'
)

NO_RESPONSE_JSON = (
    '{"classification":"no_response","reply":"","announcement":"Someone rang but did not respond.",'
    '"continue_conversation":false}'
)


# ===========================================================================
# FIX 1 — Abort session when door opens
# ===========================================================================


class TestDoorOpened:
    """Unit tests for _door_opened() helper."""

    def test_returns_false_when_key_unset(self):
        """Feature off (no key) → _door_opened is always False."""
        s = _make_screener()  # no door_contact_sensor key
        assert s._door_opened() is False

    def test_returns_false_when_key_empty(self):
        """Empty string → feature off."""
        s = _make_screener({"door_contact_sensor": ""})
        assert s._door_opened() is False

    def test_returns_true_when_sensor_on(self):
        """Sensor state 'on' → door is open."""
        s = _make_screener({"door_contact_sensor": "binary_sensor.front_door"})
        with patch.object(s, "_get_ha_state", return_value="on"):
            assert s._door_opened() is True

    def test_returns_false_when_sensor_off(self):
        """Sensor state 'off' → door is closed."""
        s = _make_screener({"door_contact_sensor": "binary_sensor.front_door"})
        with patch.object(s, "_get_ha_state", return_value="off"):
            assert s._door_opened() is False

    def test_returns_false_on_sensor_read_failure(self):
        """None (read failure) must NOT count as open."""
        s = _make_screener({"door_contact_sensor": "binary_sensor.front_door"})
        with patch.object(s, "_get_ha_state", return_value=None):
            assert s._door_opened() is False


class TestDoorAbortDuringListen:
    """Door opens during listen → no transcribe/evaluate/announce calls."""

    def _run_with_door_open_at_listen(self, open_during: str = "wait_for_speech"):
        """Run a session where the door opens during the listen phase."""
        from glados.doorbell import screener as scr_mod

        s = _make_screener({"door_contact_sensor": "binary_sensor.front_door"})
        calls = {"transcribe": 0, "evaluate": 0, "announce": 0}

        def fake_wait_for_speech(timeout):
            # Simulate door opening mid-listen by patching _door_opened
            raise scr_mod.SessionAborted()

        with (
            patch.object(scr_mod.time, "sleep"),
            patch.object(s, "_start_capture", return_value=MagicMock()),
            patch.object(s, "_play_on_speaker"),
            patch.object(s, "_wait_for_speech", side_effect=fake_wait_for_speech),
            patch.object(s, "_wait_for_silence"),
            patch.object(s, "_stop_capture"),
            patch.object(s, "_ensure_valid_wav", return_value=Path("capture.wav")),
            patch.object(s, "_transcribe", side_effect=lambda *a, **kw: calls.__setitem__("transcribe", calls["transcribe"] + 1) or ""),
            patch.object(s, "_evaluate", side_effect=lambda *a, **kw: calls.__setitem__("evaluate", calls["evaluate"] + 1) or {}),
            patch.object(s, "_announce_inside", side_effect=lambda *a, **kw: calls.__setitem__("announce", calls["announce"] + 1)),
            patch.object(s, "_generate_tts", return_value=None),
        ):
            s._run_session("db_test", ["media_player.kitchen"], max_rounds=3)

        return calls

    def test_door_opens_during_speech_wait_skips_all_actions(self):
        calls = self._run_with_door_open_at_listen()
        assert calls["transcribe"] == 0
        assert calls["evaluate"] == 0
        assert calls["announce"] == 0


class TestDoorAbortBeforeAnnounce:
    """Door opens after evaluation but before announce → no announce."""

    def test_door_open_after_eval_suppresses_announce(self):
        from glados.doorbell import screener as scr_mod

        s = _make_screener({"door_contact_sensor": "binary_sensor.front_door"})
        announce_calls = []

        # Door is closed until after evaluation, then opens
        door_state = {"open": False}

        def fake_door_opened():
            return door_state["open"]

        def fake_evaluate(*a, **kw):
            door_state["open"] = True  # door opens after LLM returns
            return {
                "classification": "delivery",
                "reply": "",
                "announcement": "A delivery has arrived.",
                "continue_conversation": False,
            }

        with (
            patch.object(scr_mod.time, "sleep"),
            patch.object(s, "_start_capture", return_value=MagicMock()),
            patch.object(s, "_play_on_speaker"),
            patch.object(s, "_wait_for_speech", return_value=False),
            patch.object(s, "_wait_for_silence"),
            patch.object(s, "_stop_capture"),
            patch.object(s, "_ensure_valid_wav", return_value=Path("capture.wav")),
            patch.object(s, "_transcribe", return_value="delivery"),
            patch.object(s, "_evaluate", side_effect=fake_evaluate),
            patch.object(s, "_announce_inside", side_effect=lambda text, spks: announce_calls.append(text)),
            patch.object(s, "_generate_tts", return_value=None),
            patch.object(s, "_door_opened", side_effect=fake_door_opened),
        ):
            s._run_session("db_test", ["media_player.kitchen"], max_rounds=3)

        assert len(announce_calls) == 0


class TestDoorAbortFeatureOff:
    """Feature off (key unset) → behavior unchanged."""

    def test_no_door_key_runs_normal_session(self):
        """Without door_contact_sensor, session completes normally."""
        from glados.doorbell import screener as scr_mod

        s = _make_screener()  # no door_contact_sensor
        announce_calls = []

        with (
            patch.object(scr_mod.time, "sleep"),
            patch.object(s, "_start_capture", return_value=MagicMock()),
            patch.object(s, "_play_on_speaker"),
            patch.object(s, "_wait_for_speech", return_value=False),
            patch.object(s, "_wait_for_silence"),
            patch.object(s, "_stop_capture"),
            patch.object(s, "_ensure_valid_wav", return_value=Path("capture.wav")),
            patch.object(s, "_transcribe", return_value="hello"),
            patch.object(s, "_evaluate", return_value={
                "classification": "guest",
                "reply": "One moment.",
                "announcement": "A visitor is at the door.",
                "continue_conversation": False,
            }),
            patch.object(s, "_announce_inside", side_effect=lambda text, spks: announce_calls.append(text)),
            patch.object(s, "_generate_tts", return_value=None),
        ):
            s._run_session("db_test", ["media_player.kitchen"], max_rounds=1)

        assert len(announce_calls) == 1


# ===========================================================================
# FIX 2 — Configurable speaker-play timeout
# ===========================================================================


class TestPlayTimeout:
    """_play_on_speaker uses configured play_timeout."""

    def _capture_urlopen_timeout(self, screener):
        """Run _play_on_speaker and capture the timeout kwarg passed to urlopen."""
        from glados.doorbell import screener as scr_mod
        from glados.core.config_store import cfg as store_cfg

        captured = {}
        fake_resp = MagicMock()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.status = 200

        def fake_urlopen(req, timeout=None, **kwargs):
            captured["timeout"] = timeout
            return fake_resp

        wav_path = MagicMock(spec=Path)
        wav_path.name = "test.wav"
        wav_path.parent = SERVE_DIR_SENTINEL  # not SERVE_DIR, so it hits the copy branch

        # Patch both the SERVE_DIR and the path copy so it doesn't need real files
        from glados.doorbell import screener as scr_mod2
        with (
            patch.object(scr_mod2, "urlopen", side_effect=fake_urlopen),
            patch.object(store_cfg, "ha_url", "http://ha.local:8123", create=True),
            patch.object(store_cfg, "ha_token", "test-token", create=True),
            patch.object(store_cfg, "serve_host", "localhost", create=True),
            patch.object(store_cfg, "serve_port", "8015", create=True),
        ):
            # Use a wav_path whose parent IS SERVE_DIR so no file copy needed
            real_wav = MagicMock(spec=Path)
            real_wav.name = "test.wav"
            from glados.doorbell.screener import SERVE_DIR
            real_wav.parent = SERVE_DIR

            screener._play_on_speaker(real_wav, "media_player.test")

        return captured.get("timeout")

    def test_play_timeout_uses_configured_value(self):
        """play_timeout: 17 in config → urlopen called with timeout=17."""
        from glados.core import tls as tls_mod
        from glados.core.config_store import cfg as store_cfg

        s = _make_screener({"play_timeout": 17})

        captured = {}
        fake_resp = MagicMock()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.status = 200

        def fake_urlopen(req, timeout=None, **kwargs):
            captured["timeout"] = timeout
            return fake_resp

        from glados.doorbell.screener import SERVE_DIR
        from glados.doorbell import screener as scr_mod

        wav = MagicMock(spec=Path)
        wav.name = "test.wav"
        wav.parent = SERVE_DIR

        with (
            patch.object(scr_mod, "urlopen", side_effect=fake_urlopen),
            patch.object(store_cfg, "ha_url", "http://ha.local:8123", create=True),
            patch.object(store_cfg, "ha_token", "test-token", create=True),
            patch.object(store_cfg, "serve_host", "localhost", create=True),
            patch.object(store_cfg, "serve_port", "8015", create=True),
            patch.object(tls_mod, "is_tls_active", return_value=False),
        ):
            s._play_on_speaker(wav, "media_player.test")

        assert captured.get("timeout") == 17

    def test_play_timeout_defaults_to_30(self):
        """No play_timeout in config → urlopen called with timeout=30."""
        from glados.core import tls as tls_mod
        from glados.core.config_store import cfg as store_cfg

        s = _make_screener()  # no play_timeout key

        captured = {}
        fake_resp = MagicMock()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.status = 200

        def fake_urlopen(req, timeout=None, **kwargs):
            captured["timeout"] = timeout
            return fake_resp

        from glados.doorbell.screener import SERVE_DIR
        from glados.doorbell import screener as scr_mod

        wav = MagicMock(spec=Path)
        wav.name = "test.wav"
        wav.parent = SERVE_DIR

        with (
            patch.object(scr_mod, "urlopen", side_effect=fake_urlopen),
            patch.object(store_cfg, "ha_url", "http://ha.local:8123", create=True),
            patch.object(store_cfg, "ha_token", "test-token", create=True),
            patch.object(store_cfg, "serve_host", "localhost", create=True),
            patch.object(store_cfg, "serve_port", "8015", create=True),
            patch.object(tls_mod, "is_tls_active", return_value=False),
        ):
            s._play_on_speaker(wav, "media_player.test")

        assert captured.get("timeout") == 30


# Sentinel so mypy/linters don't complain about use before import
SERVE_DIR_SENTINEL = object()


# ===========================================================================
# FIX 3 — One announcement per visitor
# ===========================================================================


class TestOneAnnouncementPerVisitor:
    """Round 2+ no_response classification → announce suppressed."""

    def test_second_round_no_response_suppressed(self):
        """Round 1 classifies 'unknown' (announces, continue=True);
        round 2 classifies 'no_response' → no second announce call."""
        from glados.doorbell import screener as scr_mod

        s = _make_screener()
        announce_calls = []

        round_counter = {"n": 0}

        def fake_evaluate(transcript, round_num, history):
            round_counter["n"] = round_num
            if round_num == 1:
                return {
                    "classification": "unknown",
                    "reply": "Who is there?",
                    "announcement": "Someone is at the door.",
                    "continue_conversation": True,
                }
            return {
                "classification": "no_response",
                "reply": "",
                "announcement": "The visitor did not respond.",
                "continue_conversation": False,
            }

        with (
            patch.object(scr_mod.time, "sleep"),
            patch.object(s, "_start_capture", return_value=MagicMock()),
            patch.object(s, "_play_on_speaker"),
            patch.object(s, "_wait_for_speech", return_value=False),
            patch.object(s, "_wait_for_silence"),
            patch.object(s, "_stop_capture"),
            patch.object(s, "_ensure_valid_wav", return_value=Path("capture.wav")),
            patch.object(s, "_transcribe", return_value=""),
            patch.object(s, "_evaluate", side_effect=fake_evaluate),
            patch.object(s, "_announce_inside", side_effect=announce_calls.append),
            patch.object(s, "_generate_tts", return_value=None),
        ):
            s._run_session("db_test", ["media_player.kitchen"], max_rounds=2)

        # Ran both rounds
        assert round_counter["n"] == 2
        # But only one announce (round 1)
        assert len(announce_calls) == 1

    def test_round2_non_no_response_still_announces(self):
        """Round 2 classification != 'no_response' → still announces."""
        from glados.doorbell import screener as scr_mod

        s = _make_screener()
        announce_calls = []

        def fake_evaluate(transcript, round_num, history):
            if round_num == 1:
                return {
                    "classification": "unknown",
                    "reply": "Who is there?",
                    "announcement": "Someone is at the door.",
                    "continue_conversation": True,
                }
            return {
                "classification": "delivery",
                "reply": "Thank you.",
                "announcement": "A delivery has arrived.",
                "continue_conversation": False,
            }

        with (
            patch.object(scr_mod.time, "sleep"),
            patch.object(s, "_start_capture", return_value=MagicMock()),
            patch.object(s, "_play_on_speaker"),
            patch.object(s, "_wait_for_speech", return_value=False),
            patch.object(s, "_wait_for_silence"),
            patch.object(s, "_stop_capture"),
            patch.object(s, "_ensure_valid_wav", return_value=Path("capture.wav")),
            patch.object(s, "_transcribe", return_value="delivery"),
            patch.object(s, "_evaluate", side_effect=fake_evaluate),
            patch.object(s, "_announce_inside", side_effect=announce_calls.append),
            patch.object(s, "_generate_tts", return_value=None),
        ):
            s._run_session("db_test", ["media_player.kitchen"], max_rounds=2)

        assert len(announce_calls) == 2


# ===========================================================================
# FIX 4 — Short visual identifier + carrier ID
# ===========================================================================


class TestScenePromptCarrierInstruction:
    """_capture_scene_async prompt includes carrier-identification instruction."""

    def test_scene_prompt_includes_carrier_instruction(self):
        """The VLM prompt must include carrier-ID instruction text."""
        from glados.doorbell import screener as scr_mod

        s = _make_screener({"camera_entity_id": "camera.doorbell_high"})

        captured_prompt = {}

        def fake_describe_images(images, prompt):
            captured_prompt["prompt"] = prompt
            return "a person at the door"

        fake_img = b"\xff\xd8\xff"  # minimal JPEG sentinel

        with (
            patch("glados.doorbell.screener.fetch_snapshot", return_value=fake_img),
            patch("glados.doorbell.screener.describe_images", side_effect=fake_describe_images),
            patch("glados.doorbell.screener.CameraSnapshotError", Exception),
            patch("glados.doorbell.screener.VisionClientError", Exception),
            patch("glados.core.config_store.cfg") as mock_cfg,
        ):
            mock_cfg.ha_url = "http://ha.local:8123"
            mock_cfg.ha_token = "test-token"
            s._active_session = "db_test"
            s._capture_scene_async("db_test", "camera.doorbell_high")

        prompt = captured_prompt.get("prompt", "")
        # Must mention carrier identification
        assert any(carrier in prompt for carrier in ["UPS", "FedEx", "USPS", "Amazon"]), (
            f"Carrier brands not in scene prompt: {prompt!r}"
        )
        # Must instruct one short sentence
        assert "one" in prompt.lower() or "single" in prompt.lower() or "short" in prompt.lower(), (
            f"No brevity instruction in scene prompt: {prompt!r}"
        )


class TestEvalSystemPromptVisualIdentifier:
    """_EVAL_SYSTEM_PROMPT contains the visual-identifier instruction."""

    def test_eval_prompt_mentions_visual_identifier(self):
        from glados.doorbell.screener import _EVAL_SYSTEM_PROMPT

        lower = _EVAL_SYSTEM_PROMPT.lower()
        # Must instruct including a visual identifier in the announcement
        assert "visual" in lower or "identifier" in lower or "appearance" in lower, (
            "No visual-identifier instruction in _EVAL_SYSTEM_PROMPT"
        )
        # Must reference the announcement field
        assert "announcement" in lower
