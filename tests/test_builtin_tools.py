"""Tests for glados.core.builtin_tools — Phase 8.3.4b."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from glados.core.builtin_tools import (
    TOOL_GET_ENTITY_DETAILS,
    TOOL_SEARCH_ENTITIES,
    get_builtin_tool_definitions,
    invoke_builtin_tool,
    is_builtin_tool,
)


class TestToolRegistry:
    def test_is_builtin_tool_matches_exact_names(self) -> None:
        assert is_builtin_tool(TOOL_SEARCH_ENTITIES)
        assert is_builtin_tool(TOOL_GET_ENTITY_DETAILS)

    def test_is_builtin_tool_rejects_unknown(self) -> None:
        assert not is_builtin_tool("mcp.HassTurnOn")
        assert not is_builtin_tool("")
        assert not is_builtin_tool("SEARCH_ENTITIES")  # case-sensitive

    def test_tool_definitions_openai_shape(self) -> None:
        defs = get_builtin_tool_definitions()
        assert len(defs) == 2
        names = [d["function"]["name"] for d in defs]
        assert TOOL_SEARCH_ENTITIES in names
        assert TOOL_GET_ENTITY_DETAILS in names
        for d in defs:
            assert d["type"] == "function"
            assert "description" in d["function"]
            params = d["function"]["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_search_entities_params_schema(self) -> None:
        defs = get_builtin_tool_definitions()
        search = next(
            d for d in defs
            if d["function"]["name"] == TOOL_SEARCH_ENTITIES
        )
        props = search["function"]["parameters"]["properties"]
        assert "query" in props
        assert "top_k" in props
        assert "domain_filter" in props
        assert search["function"]["parameters"]["required"] == ["query"]

    def test_get_entity_details_params_schema(self) -> None:
        defs = get_builtin_tool_definitions()
        get = next(
            d for d in defs
            if d["function"]["name"] == TOOL_GET_ENTITY_DETAILS
        )
        props = get["function"]["parameters"]["properties"]
        assert "entity_id" in props
        assert get["function"]["parameters"]["required"] == ["entity_id"]


class TestInvokeSearchEntities:
    def test_empty_query_errors(self) -> None:
        result = invoke_builtin_tool(TOOL_SEARCH_ENTITIES, {"query": ""})
        payload = json.loads(result)
        assert "error" in payload

    def test_missing_query_errors(self) -> None:
        result = invoke_builtin_tool(TOOL_SEARCH_ENTITIES, {})
        payload = json.loads(result)
        assert "error" in payload

    def test_invalid_domain_filter_shape_errors(self) -> None:
        result = invoke_builtin_tool(
            TOOL_SEARCH_ENTITIES,
            {"query": "desk", "domain_filter": "light"},  # should be list
        )
        payload = json.loads(result)
        assert "error" in payload
        assert "domain_filter" in payload["error"]

    def test_falls_back_to_fuzzy_when_no_semantic_ready(self) -> None:
        """With no singleton disambiguator/index AND a usable fuzzy
        cache, search should fall through to the fuzzy path and
        return results. Exercises the graceful-degradation contract."""
        from glados.ha.entity_cache import EntityCache
        cache = EntityCache()
        cache.apply_get_states([
            {
                "entity_id": "light.task_lamp_one",
                "state": "on",
                "attributes": {"friendly_name": "Office Desk Monitor Lamp"},
            },
            {
                "entity_id": "light.kitchen_ceiling",
                "state": "off",
                "attributes": {"friendly_name": "Kitchen Ceiling"},
            },
        ])
        with patch("glados.ha.get_cache", return_value=cache), \
             patch("glados.intent.get_disambiguator", return_value=None):
            out = invoke_builtin_tool(
                TOOL_SEARCH_ENTITIES, {"query": "desk lamp", "top_k": 3},
            )
        payload = json.loads(out)
        assert payload.get("fallback") == "fuzzy"
        ids = [r["entity_id"] for r in payload["results"]]
        assert "light.task_lamp_one" in ids


class TestInvokeGetEntityDetails:
    def test_empty_entity_id_errors(self) -> None:
        result = invoke_builtin_tool(TOOL_GET_ENTITY_DETAILS, {})
        payload = json.loads(result)
        assert "error" in payload

    def test_missing_cache_errors(self) -> None:
        with patch("glados.ha.get_cache", return_value=None):
            out = invoke_builtin_tool(
                TOOL_GET_ENTITY_DETAILS, {"entity_id": "light.x"},
            )
        payload = json.loads(out)
        assert "error" in payload
        assert "not initialised" in payload["error"]

    def test_returns_state_and_attributes_for_real_entity(self) -> None:
        from glados.ha.entity_cache import EntityCache
        cache = EntityCache()
        cache.apply_get_states([
            {
                "entity_id": "light.task_lamp_one",
                "state": "on",
                "attributes": {
                    "friendly_name": "Office Desk Monitor Lamp",
                    "brightness": 200,
                    "color_mode": "brightness",
                    "supported_color_modes": ["brightness"],
                    # Noise that should be scrubbed:
                    "icon": "mdi:lightbulb",
                    "entity_picture": "/fake.png",
                    "supported_features": 41,
                },
            },
        ])
        with patch("glados.ha.get_cache", return_value=cache):
            out = invoke_builtin_tool(
                TOOL_GET_ENTITY_DETAILS,
                {"entity_id": "light.task_lamp_one"},
            )
        payload = json.loads(out)
        assert payload["entity_id"] == "light.task_lamp_one"
        assert payload["state"] == "on"
        assert payload["friendly_name"] == "Office Desk Monitor Lamp"
        # Decision-relevant attributes kept
        assert payload["attributes"]["brightness"] == 200
        assert payload["attributes"]["color_mode"] == "brightness"
        # Noise scrubbed
        assert "icon" not in payload["attributes"]
        assert "entity_picture" not in payload["attributes"]
        assert "supported_features" not in payload["attributes"]

    def test_returns_error_for_unknown_entity(self) -> None:
        from glados.ha.entity_cache import EntityCache
        cache = EntityCache()
        with patch("glados.ha.get_cache", return_value=cache):
            out = invoke_builtin_tool(
                TOOL_GET_ENTITY_DETAILS, {"entity_id": "light.ghost"},
            )
        payload = json.loads(out)
        assert "not found" in payload["error"]


class TestUnknownToolName:
    def test_unknown_tool_returns_error_payload(self) -> None:
        out = invoke_builtin_tool("mystery_tool", {})
        payload = json.loads(out)
        assert "unknown built-in tool" in payload["error"]
