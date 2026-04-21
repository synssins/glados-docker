"""Tier 1 — Home Assistant conversation bridge.

Wraps the WebSocket `conversation/process` call with classification
logic so the fast-path intercept in api_wrapper knows whether the
utterance was handled, or needs to fall through to Tier 2 / Tier 3.

HA's response payload format (via `conversation/process` over WS):

    {
      "response": {
        "response_type": "action_done" | "query_answer" | "error",
        "speech": {
          "plain": {"speech": "…"},
          "ssml": {"speech": "…"}
        },
        "data": {
          "code": "no_intent_match" | "no_valid_targets" | ...,
          "targets": [...],
          "success": [...],
          "failed": [...]
        }
      },
      "conversation_id": "…",
      "continue_conversation": bool
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from .ws_client import HAClient


# Error codes that mean "HA couldn't handle this — try Tier 2/3".
# Unknown error codes are also treated as fall-through; better safe than
# sorry.
_FALL_THROUGH_CODES: frozenset[str] = frozenset({
    "no_intent_match",
    "no_valid_targets",
})


# Speech responses HA emits when its intent parser matched a template
# but the underlying entity attributes are missing/null. Real example:
# HA matched "Tell me about my equipment" to a Person intent with empty
# first/last name, returning literal "None None" as a query_answer.
# Treating these as "handled" produces garbage user-facing text.
# Comparison is case-insensitive after stripping punctuation/whitespace.
_GARBAGE_SPEECH_TOKENS: frozenset[str] = frozenset({
    "",
    "none",
    "none none",
    "null",
    "null null",
    "undefined",
    "n/a",
})


def _is_garbage_speech(speech: str) -> bool:
    """Heuristic: HA emitted a templated answer but the variables it
    interpolated are null. Treating this as a Tier 1 win would surface
    "None None" in the chat UI."""
    if not speech:
        return True
    cleaned = "".join(ch for ch in speech.lower() if ch.isalnum() or ch.isspace())
    cleaned = " ".join(cleaned.split())
    return cleaned in _GARBAGE_SPEECH_TOKENS


@dataclass
class ConversationResult:
    """The outcome of a Tier 1 attempt.

    - `handled` is True when HA executed an action or answered a query
      successfully and the caller should return HA's response (after
      persona rewrite) as the final reply.
    - `should_disambiguate` is True when HA matched an intent but
      couldn't resolve the target — the LLM disambiguator (Tier 2)
      should try again with the entity cache.
    - `should_fall_through` is True when Tier 1 doesn't apply at all
      and Tier 3 (full LLM with tools) should handle it."""

    handled: bool
    should_disambiguate: bool
    should_fall_through: bool
    speech: str                         # HA's plain-language response, if any
    response_type: str                  # action_done | query_answer | error | ""
    error_code: str | None              # data.code, if error
    conversation_id: str | None
    raw: dict[str, Any]                 # Full response for audit / debugging


def _extract_speech(response: dict[str, Any]) -> str:
    """Pull plain-text speech from an HA response, preferring plain over SSML."""
    speech = (response.get("speech") or {})
    plain = (speech.get("plain") or {}).get("speech") or ""
    if plain:
        return str(plain)
    ssml = (speech.get("ssml") or {}).get("speech") or ""
    return str(ssml)


# Weather-related tokens. If the user's utterance contains any of
# these, a weather-sourced HA query_answer is credible. If not, HA
# is almost certainly falling back to weather.openweathermap as a
# last resort (observed 2026-04-21: "Hey, what was life like as a
# potato?" returned "56 °F and sunny").
_WEATHER_TOKENS: frozenset[str] = frozenset({
    "weather", "temperature", "temp", "forecast", "rain", "raining",
    "rainy", "snow", "snowing", "snowy", "sunny", "cloudy",
    "cloud", "clouds", "wind", "windy", "humid", "humidity",
    "hot", "cold", "warm", "cool", "freezing", "degrees",
    "fahrenheit", "celsius", "precipitation", "storm", "stormy",
    "overcast", "outside", "outdoors", "outdoor",
})


def _looks_like_weather_question(text: str) -> bool:
    if not text:
        return False
    words = {w.strip(".,!?;:'\"").lower() for w in text.split()}
    return bool(words & _WEATHER_TOKENS)


def _response_source_is_weather_only(response: dict[str, Any]) -> bool:
    """True iff every entry in response.data.success looks like a
    weather entity. Used to detect HA's weather-fallback pattern."""
    data = response.get("data") or {}
    success = data.get("success") or []
    if not isinstance(success, list) or not success:
        return False
    for s in success:
        if not isinstance(s, dict):
            return False
        sid = str(s.get("id") or "")
        if not sid.startswith("weather."):
            return False
    return True


def classify(raw: dict[str, Any], utterance: str = "") -> ConversationResult:
    """Classify HA's WS response frame into a Tier 1 decision.

    Handles both the unwrapped `response` shape and the full WS
    `{id, type: result, success, result: {response, conversation_id}}`
    wrapper. Callers can pass whichever they have."""

    # Unwrap WS result frame if present.
    if "result" in raw and isinstance(raw.get("result"), dict):
        payload = raw["result"]
    else:
        payload = raw

    response = (payload.get("response") or {}) if isinstance(payload, dict) else {}
    response_type = str(response.get("response_type") or "")
    speech = _extract_speech(response)
    conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
    error_code: str | None = None

    if response_type == "action_done":
        # HA ran the action. Even if some targets failed, we treat
        # this as handled and let the persona rewriter describe the
        # partial success from the speech text. Exception: if HA's
        # speech is the "None None"-style garbage that means HA
        # interpolated null entity attributes, fall through.
        if _is_garbage_speech(speech):
            return ConversationResult(
                handled=False, should_disambiguate=False,
                should_fall_through=True, speech=speech,
                response_type=response_type,
                error_code="garbage_speech",
                conversation_id=conversation_id, raw=raw,
            )
        return ConversationResult(
            handled=True,
            should_disambiguate=False,
            should_fall_through=False,
            speech=speech,
            response_type=response_type,
            error_code=None,
            conversation_id=conversation_id,
            raw=raw,
        )

    if response_type == "query_answer":
        # State query — HA has the answer. Pass through the persona
        # rewriter and return to the user. Same garbage-speech guard
        # as action_done; HA's query intent often interpolates null
        # values when the underlying entity has no attribute.
        if _is_garbage_speech(speech):
            return ConversationResult(
                handled=False, should_disambiguate=False,
                should_fall_through=True, speech=speech,
                response_type=response_type,
                error_code="garbage_speech",
                conversation_id=conversation_id, raw=raw,
            )
        # HA's weather-fallback: when it can't parse the utterance, HA
        # sometimes returns a weather-sourced query_answer with
        # targets=[] even for pure chitchat ("Hey, what was life like
        # as a potato?" → "56 °F and sunny", seen 2026-04-21). Detect
        # this pattern and fall through so Tier 3 can handle the real
        # question.
        if (
            _response_source_is_weather_only(response)
            and not _looks_like_weather_question(utterance)
        ):
            return ConversationResult(
                handled=False, should_disambiguate=False,
                should_fall_through=True, speech=speech,
                response_type=response_type,
                error_code="weather_fallback_misclassify",
                conversation_id=conversation_id, raw=raw,
            )
        return ConversationResult(
            handled=True,
            should_disambiguate=False,
            should_fall_through=False,
            speech=speech,
            response_type=response_type,
            error_code=None,
            conversation_id=conversation_id,
            raw=raw,
        )

    if response_type == "error":
        error_code = str((response.get("data") or {}).get("code") or "") or None
        if error_code in _FALL_THROUGH_CODES:
            # Intent matched but target ambiguous OR no intent matched —
            # disambiguator (Tier 2) gets a chance with the entity cache.
            return ConversationResult(
                handled=False,
                should_disambiguate=True,
                should_fall_through=False,
                speech=speech,
                response_type=response_type,
                error_code=error_code,
                conversation_id=conversation_id,
                raw=raw,
            )
        # Any other error: treat as failed handling; full LLM takes over.
        return ConversationResult(
            handled=False,
            should_disambiguate=False,
            should_fall_through=True,
            speech=speech,
            response_type=response_type,
            error_code=error_code,
            conversation_id=conversation_id,
            raw=raw,
        )

    # Unknown response_type — fall through to Tier 3.
    return ConversationResult(
        handled=False,
        should_disambiguate=False,
        should_fall_through=True,
        speech=speech,
        response_type=response_type,
        error_code=None,
        conversation_id=conversation_id,
        raw=raw,
    )


class ConversationBridge:
    """Thin wrapper around HAClient.conversation_process + classify."""

    def __init__(self, ha_client: HAClient, default_language: str = "en") -> None:
        self._ha = ha_client
        self._lang = default_language

    def process(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str | None = None,
        timeout_s: float | None = None,
    ) -> ConversationResult:
        """Run an utterance through HA's intent pipeline and classify
        the result. Never raises — exceptions become fall-through
        results so the caller's Tier 3 path always has a chance."""
        if not self._ha.is_connected():
            logger.debug("Conversation bridge: HA not connected, falling through")
            return _fall_through("ha_not_connected")
        try:
            raw = self._ha.conversation_process(
                text=text,
                language=language or self._lang,
                conversation_id=conversation_id,
                timeout_s=timeout_s,
            )
        except TimeoutError:
            return _fall_through("timeout")
        except Exception as exc:
            logger.warning("Conversation bridge: HA call failed: {}", exc)
            return _fall_through(f"exception:{type(exc).__name__}")
        if not raw.get("success", True):
            return _fall_through("not_success")
        return classify(raw, utterance=text)


def _fall_through(reason: str) -> ConversationResult:
    return ConversationResult(
        handled=False,
        should_disambiguate=False,
        should_fall_through=True,
        speech="",
        response_type="",
        error_code=reason,
        conversation_id=None,
        raw={},
    )
