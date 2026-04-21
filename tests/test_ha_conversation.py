"""Tests for glados.ha.conversation classification."""

from __future__ import annotations

from glados.ha.conversation import ConversationResult, classify


def _wrap(response: dict, conversation_id: str = "c1") -> dict:
    """Build the full WS result frame shape."""
    return {
        "id": 2,
        "type": "result",
        "success": True,
        "result": {
            "response": response,
            "conversation_id": conversation_id,
            "continue_conversation": False,
        },
    }


def _speech(plain: str) -> dict:
    return {"plain": {"speech": plain}, "ssml": {"speech": ""}}


class TestClassify:
    def test_action_done_is_handled(self) -> None:
        r = classify(_wrap({
            "response_type": "action_done",
            "speech": _speech("Turned off the kitchen lights."),
            "data": {"success": [{"id": "light.kitchen"}]},
        }))
        assert r.handled is True
        assert r.should_disambiguate is False
        assert r.should_fall_through is False
        assert "kitchen" in r.speech.lower()
        assert r.response_type == "action_done"

    def test_query_answer_is_handled(self) -> None:
        r = classify(_wrap({
            "response_type": "query_answer",
            "speech": _speech("The kitchen light is on."),
            "data": {},
        }))
        assert r.handled is True
        assert "kitchen" in r.speech.lower()

    def test_weather_fallback_on_chitchat_falls_through(self) -> None:
        """Regression for 2026-04-21: HA's conversation/process
        returned a weather-sourced query_answer for the pure chitchat
        "Hey, what was life like as a potato?" — targets=[] but
        success=[weather.openweathermap], speech="56 °F and sunny".
        The bridge must detect this pattern and fall through so
        Tier 3 can answer the actual question."""
        r = classify(
            _wrap({
                "response_type": "query_answer",
                "speech": _speech("56 °F and sunny"),
                "data": {
                    "targets": [],
                    "success": [
                        {"id": "weather.openweathermap", "type": "entity"},
                    ],
                    "failed": [],
                },
            }),
            utterance="Hey, what was life like as a potato?",
        )
        assert r.handled is False
        assert r.should_fall_through is True
        assert r.error_code == "weather_fallback_misclassify"

    def test_weather_question_still_handled(self) -> None:
        """If the user ACTUALLY asked about the weather, the
        weather-sourced answer is legitimate and should pass through."""
        r = classify(
            _wrap({
                "response_type": "query_answer",
                "speech": _speech("56 °F and sunny"),
                "data": {
                    "targets": [],
                    "success": [
                        {"id": "weather.openweathermap", "type": "entity"},
                    ],
                    "failed": [],
                },
            }),
            utterance="What's the weather like outside?",
        )
        assert r.handled is True
        assert "sunny" in r.speech.lower()

    def test_empty_nop_action_done_falls_through(self) -> None:
        """Regression for 2026-04-21: HA returned action_done with
        all three data lists empty and speech="9:55 AM" (a time-
        slot template fill) for "Tell me about the testing tracks".
        Trust that no targets/success/failed means HA did nothing."""
        r = classify(_wrap({
            "response_type": "action_done",
            "speech": _speech("9:55 AM"),
            "speech_slots": {"time": "09:55:43"},
            "data": {"targets": [], "success": [], "failed": []},
        }))
        assert r.handled is False
        assert r.should_fall_through is True
        assert r.error_code == "empty_nop_misclassify"

    def test_action_done_with_targets_still_handled(self) -> None:
        """Regular action_done with populated success list remains
        the happy path — we only guard against the all-empty case."""
        r = classify(_wrap({
            "response_type": "action_done",
            "speech": _speech("Turned off the kitchen light."),
            "data": {
                "targets": [{"type": "entity", "id": "light.kitchen"}],
                "success": [{"id": "light.kitchen"}],
                "failed": [],
            },
        }))
        assert r.handled is True

    def test_non_weather_source_query_answer_still_handled(self) -> None:
        """A query_answer sourced from a non-weather entity (e.g.
        sensor.living_room_temperature) is legitimate regardless of
        the utterance — we only guard against the weather fallback."""
        r = classify(
            _wrap({
                "response_type": "query_answer",
                "speech": _speech("72 degrees"),
                "data": {
                    "targets": [],
                    "success": [
                        {"id": "sensor.living_room_temperature", "type": "entity"},
                    ],
                },
            }),
            utterance="Hey, what was life like as a potato?",
        )
        # Not weather-sourced → trust HA's answer.
        assert r.handled is True

    def test_no_intent_match_triggers_disambiguation(self) -> None:
        r = classify(_wrap({
            "response_type": "error",
            "speech": _speech("Sorry, I couldn't understand that."),
            "data": {"code": "no_intent_match"},
        }))
        assert r.handled is False
        assert r.should_disambiguate is True
        assert r.should_fall_through is False
        assert r.error_code == "no_intent_match"

    def test_no_valid_targets_triggers_disambiguation(self) -> None:
        r = classify(_wrap({
            "response_type": "error",
            "speech": _speech("No device named bedroom lights."),
            "data": {"code": "no_valid_targets"},
        }))
        assert r.handled is False
        assert r.should_disambiguate is True

    def test_failed_to_handle_falls_through(self) -> None:
        r = classify(_wrap({
            "response_type": "error",
            "speech": _speech("An error occurred."),
            "data": {"code": "failed_to_handle"},
        }))
        assert r.handled is False
        assert r.should_fall_through is True
        assert r.should_disambiguate is False

    def test_unknown_response_type_falls_through(self) -> None:
        r = classify(_wrap({
            "response_type": "mystery",
            "speech": _speech(""),
        }))
        assert r.handled is False
        assert r.should_fall_through is True

    def test_conversation_id_is_preserved(self) -> None:
        r = classify(_wrap({"response_type": "action_done",
                            "speech": _speech("ok")}, conversation_id="abc"))
        assert r.conversation_id == "abc"

    def test_ssml_fallback_when_plain_empty(self) -> None:
        r = classify(_wrap({
            "response_type": "action_done",
            "speech": {"plain": {"speech": ""},
                       "ssml": {"speech": "<speak>SSML only</speak>"}},
        }))
        assert "SSML" in r.speech

    def test_garbage_speech_action_done_falls_through(self) -> None:
        """Real failure observed in prod: HA returned response_type=
        query_answer with speech 'None None' for 'Tell me about my
        equipment'. Treating that as handled would surface 'None None'
        as the chat reply. Must fall through to LLM."""
        for bad in ["None None", "None", "", "  ", "null null", "NONE"]:
            r = classify(_wrap({
                "response_type": "query_answer",
                "speech": _speech(bad),
            }))
            assert r.handled is False, f"speech={bad!r} should not be handled"
            assert r.should_fall_through is True
            assert r.error_code == "garbage_speech"

    def test_garbage_speech_action_done_also_filtered(self) -> None:
        r = classify(_wrap({
            "response_type": "action_done",
            "speech": _speech("None None"),
        }))
        assert r.handled is False
        assert r.error_code == "garbage_speech"

    def test_real_short_speech_is_NOT_garbage(self) -> None:
        """Make sure short legitimate replies like '2:22 PM' aren't
        misclassified as garbage."""
        for good in ["2:22 PM", "ok", "Turned off the light",
                     "On", "5 lights", "23 degrees"]:
            r = classify(_wrap({
                "response_type": "query_answer",
                "speech": _speech(good),
            }))
            assert r.handled is True, f"speech={good!r} should be handled"

    def test_unwrapped_response_also_works(self) -> None:
        """Classify should work on either the full WS frame or just the
        inner `result` payload, for caller convenience."""
        inner = {
            "response": {"response_type": "action_done",
                         "speech": _speech("ok")},
            "conversation_id": "c1",
        }
        r = classify(inner)
        assert r.handled is True
