"""``llm_call`` JSON-output modes.

Three response-shape configurations the caller can pick from:

  - default               -> no ``response_format`` key in the body
  - json_response=True    -> ``{"type": "text"}`` (soft hint; LM Studio
                              rejects the legacy ``json_object`` form
                              with a 400, so callers that just want a
                              JSON-ish reply use the prompt + tolerant
                              parser pair)
  - json_schema=<dict>    -> ``{"type": "json_schema", "json_schema":
                              <dict>}`` (grammar-constrained; LM Studio
                              converts to a llama.cpp grammar so the
                              model literally cannot emit invalid
                              tokens)

Tests assert the request body that ``llm_call`` posts upstream — the
runtime's reply shape isn't relevant here, just what we send.
"""

from __future__ import annotations

from unittest.mock import patch

from glados.autonomy.llm_client import LLMConfig, llm_call


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _config() -> LLMConfig:
    return LLMConfig(url="http://example:11434", model="m", timeout=5.0)


def _capture_post(seen: dict):
    def _capture(url, **kwargs):
        seen["body"] = kwargs.get("json")
        return _Resp({"choices": [{"message": {"content": "ok"}}]})
    return _capture


def test_default_no_response_format() -> None:
    seen: dict = {}
    with patch("requests.post", side_effect=_capture_post(seen)):
        llm_call(_config(), "sys", "user")
    assert "response_format" not in seen["body"]


def test_json_response_uses_text_type() -> None:
    seen: dict = {}
    with patch("requests.post", side_effect=_capture_post(seen)):
        llm_call(_config(), "sys", "user", json_response=True)
    assert seen["body"]["response_format"] == {"type": "text"}


def test_json_schema_passes_through() -> None:
    schema = {
        "name": "test_schema",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "string", "enum": ["a", "b"]}},
            "required": ["x"],
            "additionalProperties": False,
        },
    }
    seen: dict = {}
    with patch("requests.post", side_effect=_capture_post(seen)):
        llm_call(_config(), "sys", "user", json_schema=schema)
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"] == schema


def test_json_schema_wins_over_json_response_flag() -> None:
    """Both flags set: schema wins. The flag is the legacy soft hint;
    schema is the hard constraint and should not be downgraded."""
    schema = {"name": "s", "strict": True, "schema": {"type": "object"}}
    seen: dict = {}
    with patch("requests.post", side_effect=_capture_post(seen)):
        llm_call(_config(), "sys", "user", json_response=True, json_schema=schema)
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"] == schema


def test_json_schema_none_falls_through_to_json_response_flag() -> None:
    """json_schema=None (the default) does NOT trigger the schema
    branch — should behave identically to omitting the parameter."""
    seen: dict = {}
    with patch("requests.post", side_effect=_capture_post(seen)):
        llm_call(_config(), "sys", "user", json_response=True, json_schema=None)
    assert seen["body"]["response_format"] == {"type": "text"}
