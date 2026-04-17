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
