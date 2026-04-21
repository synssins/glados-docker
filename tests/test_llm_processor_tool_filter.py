"""Tests for llm_processor._filter_tools_for_message — the chitchat
tool-strip guard added 2026-04-21 after the non-streaming chat path
started returning corporate refusals ("my capabilities are limited
to the tools provided") for pure lore questions."""

from __future__ import annotations

from glados.core.llm_processor import LanguageModelProcessor


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


class TestFilterToolsForMessage:
    """Covers the chitchat strip + the older slow-clap filter."""

    def test_chitchat_strips_ha_dotted_tools(self) -> None:
        tools = [
            _tool("speak"),
            _tool("home_assistant.turn_on"),
            _tool("home_assistant.get_state"),
            _tool("vision_look"),
        ]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "What was life like as a potato?",
        )
        names = {t["function"]["name"] for t in out}
        # Built-in engine tools kept; HA/MCP dotted tools dropped.
        assert "speak" in names
        assert "vision_look" in names
        assert "home_assistant.turn_on" not in names
        assert "home_assistant.get_state" not in names

    def test_chitchat_strips_unprefixed_ha_tool_names(self) -> None:
        """MCP servers sometimes flatten names (no dot). Explicit
        denylist catches the common ones on chitchat turns."""
        tools = [
            _tool("search_entities"),
            _tool("get_entity_details"),
            _tool("turn_on"),
            _tool("scene_turn_on"),
            _tool("speak"),
        ]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "Tell me about Wheatley.",
        )
        names = {t["function"]["name"] for t in out}
        assert names == {"speak"}

    def test_home_command_keeps_ha_tools(self) -> None:
        tools = [
            _tool("speak"),
            _tool("home_assistant.turn_on"),
            _tool("search_entities"),
        ]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "turn off the kitchen lights",
        )
        names = {t["function"]["name"] for t in out}
        # Home command → keep everything.
        assert "home_assistant.turn_on" in names
        assert "search_entities" in names
        assert "speak" in names

    def test_slow_clap_gate_still_works(self) -> None:
        tools = [_tool("speak"), _tool("slow clap")]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "turn off the lights",  # no 'clap'
        )
        names = {t["function"]["name"] for t in out}
        assert "slow clap" not in names
        assert "speak" in names

    def test_slow_clap_kept_when_utterance_mentions_clap(self) -> None:
        tools = [_tool("speak"), _tool("slow clap")]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "give me a slow clap for the effort",
        )
        names = {t["function"]["name"] for t in out}
        assert "slow clap" in names

    def test_weather_question_chitchat_strips_ha_tools(self) -> None:
        """'What's the weather like?' is chitchat to the precheck
        (no device keyword, no command verb). Strip the MCP bundle
        so the model leans on the injected weather context instead
        of hunting for a parametric weather_forecast tool."""
        tools = [
            _tool("home_assistant.weather_forecast"),
            _tool("home_assistant.get_state"),
            _tool("speak"),
        ]
        out = LanguageModelProcessor._filter_tools_for_message(
            tools, "What's the weather like?",
        )
        names = {t["function"]["name"] for t in out}
        assert "home_assistant.weather_forecast" not in names
        assert "speak" in names
