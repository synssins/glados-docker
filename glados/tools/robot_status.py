"""
LLM tool: ``robot_status`` — query robot status and capabilities.

Returns a summary of all configured bots and their nodes, or
detailed status for a specific bot (servo positions, sensors, health).
"""

import json
import queue
from typing import Any

from loguru import logger

tool_definition = {
    "type": "function",
    "function": {
        "name": "robot_status",
        "description": (
            "Get the status of robot bots and their nodes. "
            "Without a bot_id, lists all available bots with their profiles and node health. "
            "With a bot_id, returns detailed status including servo positions and sensor readings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bot_id": {
                    "type": "string",
                    "description": "Optional: specific bot to query. Omit to list all bots.",
                },
            },
        },
    },
}


class RobotStatus:
    def __init__(
        self,
        llm_queue: queue.Queue[dict[str, Any]],
        tool_config: dict[str, Any] | None = None,
    ) -> None:
        self.llm_queue = llm_queue
        tool_config = tool_config or {}
        self._robot_manager = tool_config.get("robot_manager")

    def run(self, tool_call_id: str, call_args: dict[str, Any]) -> None:
        if self._robot_manager is None:
            self._send_result(tool_call_id, "error: robot subsystem is not enabled")
            return

        bot_id = str(call_args.get("bot_id", "")).strip() if call_args.get("bot_id") else ""
        mgr = self._robot_manager

        if not bot_id:
            # List all bots + nodes overview
            result = {
                "nodes": mgr.get_all_health(),
                "bots": mgr.get_bots_summary(),
            }
            self._send_result(tool_call_id, json.dumps(result))
            return

        # Detailed status for a specific bot
        from glados.robots.bot import Bot
        from glados.robots.profiles import get_profile

        bot_cfg = mgr._config.bots.get(bot_id)
        if bot_cfg is None:
            available = list(mgr._config.bots.keys())
            self._send_result(
                tool_call_id,
                f"error: unknown bot '{bot_id}'. Available bots: {available}",
            )
            return

        clients: dict[str, Any] = {}
        for role, node_id in bot_cfg.nodes.items():
            client = mgr.get_client(node_id)
            if client:
                clients[role] = client

        profile = get_profile(bot_cfg.profile)
        bot = Bot(bot_id, bot_cfg, clients, profile)

        status = bot.get_status()
        status["available_actions"] = bot.available_actions
        logger.debug("robot_status: bot={}, status={}", bot_id, status)
        self._send_result(tool_call_id, json.dumps(status))

    def _send_result(self, tool_call_id: str, content: str) -> None:
        self.llm_queue.put(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
                "type": "function_call_output",
            }
        )
