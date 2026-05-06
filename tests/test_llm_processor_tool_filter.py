"""Tests for llm_processor._filter_tools_for_message — the chitchat
tool-strip guard added 2026-04-21 after the non-streaming chat path
started returning corporate refusals ("my capabilities are limited
to the tools provided") for pure lore questions.

2026-05-05 extension: the same filter now applies to autonomy turns.
Routine autonomy ticks (status-only slot summaries) carry no HA-noun
keywords, so the filter strips the full ~95-tool MCP catalog and
autonomy keeps just speak/do_nothing/vision_look. Event-driven ticks
whose slot summaries mention a HA noun (door, light, person, …) keep
the catalog so autonomy can act in real time. Tests below document
both shapes."""

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

    # ── 2026-05-05 extension: filter applies to autonomy turns too ────

    def test_autonomy_routine_tick_strips_ha_tools(self) -> None:
        """Routine autonomy ticks fire every ~30s with status-only slot
        summaries (compaction state, emotion state, weather, etc.).
        These carry no HA nouns, so the filter strips the full ~95-tool
        MCP catalog. Autonomy keeps just speak/do_nothing/vision_look —
        enough to decide do_nothing fast and cheap.
        """
        tools = [
            _tool("speak"),
            _tool("do_nothing"),
            _tool("vision_look"),
            _tool("home_assistant.lock"),
            _tool("home_assistant.light.turn_on"),
            _tool("home_assistant.notify.send"),
            _tool("home_assistant.cover.open"),
        ]
        routine_tick = (
            "Autonomy update.\n"
            "Time: 2026-05-06T00:14:00\n"
            "Tasks:\n"
            "Message Compaction: monitoring - Context at 1700 tokens (threshold: 6000)\n"
            "Emotional State: active - Contemptuous Calm (intensity: 0.50)\n"
            "Decide whether to act."
        )
        out = LanguageModelProcessor._filter_tools_for_message(tools, routine_tick)
        names = {t["function"]["name"] for t in out}
        # MCP/HA dotted-name tools dropped on routine ticks
        assert "home_assistant.lock" not in names
        assert "home_assistant.light.turn_on" not in names
        assert "home_assistant.notify.send" not in names
        assert "home_assistant.cover.open" not in names
        # Built-in autonomy tools kept
        assert names == {"speak", "do_nothing", "vision_look"}

    def test_autonomy_event_tick_with_door_keyword_keeps_ha_tools(self) -> None:
        """When a slot summary mentions an HA noun ('door', 'light',
        'person', etc.), the keyword pre-filter trips
        looks_like_home_command and the catalog is preserved so
        autonomy can call home_assistant.* tools to react in real time.
        """
        tools = [
            _tool("speak"),
            _tool("home_assistant.lock"),
            _tool("home_assistant.cover.close"),
        ]
        event_tick = (
            "Autonomy update.\n"
            "Time: 2026-05-06T01:30:00\n"
            "Tasks:\n"
            "HA Sensor: alert - Door opened: front_door\n"
            "Decide whether to act."
        )
        out = LanguageModelProcessor._filter_tools_for_message(tools, event_tick)
        names = {t["function"]["name"] for t in out}
        # 'door' is in the keyword domain map → looks_like_home_command=True
        # → no stripping → all tools retained
        assert "home_assistant.lock" in names
        assert "home_assistant.cover.close" in names
        assert "speak" in names

    def test_autonomy_camera_slot_name_keeps_ha_tools(self) -> None:
        """Caveat captured by the test name: the filter is content-
        based, so a slot whose TITLE mentions an HA-mapped keyword
        ('camera', 'light', 'door', etc.) trips the domain map and
        the catalog is kept — even when the slot itself is in a
        passive/error/monitoring state.

        This is a known property of the gate. Effectiveness in
        practice depends on whether routine slot summaries mention
        HA nouns. Operator's current setup has 'Camera Watcher' as
        a persistent slot, so 'camera' is in every tick prompt —
        the filter would NOT strip tools on those ticks. Mitigation
        when needed: slot subagents can rename / suppress passive-
        state summaries OR the gate can be made stricter for
        autonomy mode (gate on slot.severity rather than keyword).
        """
        tools = [
            _tool("speak"),
            _tool("home_assistant.lock"),
            _tool("home_assistant.camera.snapshot"),
        ]
        tick = (
            "Autonomy update.\n"
            "Tasks:\n"
            "Camera Watcher: error - vision service unreachable\n"
            "Decide whether to act."
        )
        out = LanguageModelProcessor._filter_tools_for_message(tools, tick)
        names = {t["function"]["name"] for t in out}
        # 'camera' is in the keyword-domain map, so the filter keeps
        # the full toolset — including unrelated HA tools like
        # `home_assistant.lock`. Documented as a limitation, not a bug.
        assert "home_assistant.lock" in names
        assert "home_assistant.camera.snapshot" in names
        assert "speak" in names

    def test_autonomy_event_tick_with_light_keyword_keeps_ha_tools(self) -> None:
        """Slot summary explicitly mentioning 'light' — a more
        operator-friendly slot phrasing for the kitchen scenario."""
        tools = [
            _tool("speak"),
            _tool("home_assistant.light.turn_on"),
            _tool("home_assistant.light.turn_off"),
        ]
        event_tick = (
            "Autonomy update.\n"
            "Tasks:\n"
            "Presence Watcher: kitchen lights are on but kitchen is empty\n"
            "Decide whether to act."
        )
        out = LanguageModelProcessor._filter_tools_for_message(tools, event_tick)
        names = {t["function"]["name"] for t in out}
        assert "home_assistant.light.turn_on" in names
        assert "home_assistant.light.turn_off" in names

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
