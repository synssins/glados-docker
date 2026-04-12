"""
LLM tool: ``robot_move`` — execute a movement action on a robot bot.

Translates natural-language movement intents from the LLM into concrete
NodeClient calls via the Bot/Profile system.
"""

import json
import queue
from typing import Any

from loguru import logger

tool_definition = {
    "type": "function",
    "function": {
        "name": "robot_move",
        "description": (
            "Execute a movement action on a robot. "
            "Use this to move joints, drive a mobile base, turn, steer, or perform synchronized multi-joint moves. "
            "Call robot_status first if you need to know available bots and their capabilities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bot_id": {
                    "type": "string",
                    "description": "The bot to control (e.g. 'lab_arm', 'scout'). Use robot_status to list available bots.",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "The action to perform. Available actions depend on the bot's profile: "
                        "arm: move_joint, sync_move, get_joint_positions, set_torque | "
                        "tracked: drive, turn, stop | "
                        "4wheel: drive, steer, stop | "
                        "arm_on_base: all arm + base actions"
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action-specific parameters. Examples: "
                        "move_joint: {joint_id: 1, position: 512, speed: 200} | "
                        "sync_move: {moves: [{joint_id: 1, position: 512}, {joint_id: 2, position: 300}]} | "
                        "drive: {speed: 500, duration_ms: 2000} | "
                        "turn: {direction: 'left', speed: 300} | "
                        "set_torque: {joint_id: 1, enabled: true}"
                    ),
                },
            },
            "required": ["bot_id", "action"],
        },
    },
}


class RobotMove:
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

        bot_id = str(call_args.get("bot_id", "")).strip()
        action = str(call_args.get("action", "")).strip()
        params = call_args.get("params", {})

        if not bot_id or not action:
            self._send_result(tool_call_id, "error: bot_id and action are required")
            return

        # Get or build the Bot from the manager
        from glados.robots.bot import Bot
        from glados.robots.profiles import get_profile

        mgr = self._robot_manager
        bot_cfg = mgr._config.bots.get(bot_id)
        if bot_cfg is None:
            available = list(mgr._config.bots.keys())
            self._send_result(
                tool_call_id,
                f"error: unknown bot '{bot_id}'. Available bots: {available}",
            )
            return

        if not bot_cfg.enabled:
            self._send_result(tool_call_id, f"error: bot '{bot_id}' is disabled")
            return

        # Build clients dict for this bot
        clients: dict[str, Any] = {}
        for role, node_id in bot_cfg.nodes.items():
            client = mgr.get_client(node_id)
            if client is None:
                self._send_result(
                    tool_call_id,
                    f"error: node '{node_id}' (role '{role}') not found for bot '{bot_id}'",
                )
                return
            clients[role] = client

        profile = get_profile(bot_cfg.profile)
        bot = Bot(bot_id, bot_cfg, clients, profile)

        result = bot.execute_action(action, params)
        logger.info("robot_move: bot={}, action={}, result={}", bot_id, action, result)
        self._send_result(tool_call_id, json.dumps(result))

    def _send_result(self, tool_call_id: str, content: str) -> None:
        self.llm_queue.put(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
                "type": "function_call_output",
            }
        )
