"""
LLM tool: ``robot_emergency_stop`` — immediately stop all robot motion.

Safety-critical: sends emergency stop to all nodes (or a specific bot's
nodes).  Uses short timeouts and no authentication to ensure it always
works.
"""

import json
import queue
from typing import Any

from loguru import logger

tool_definition = {
    "type": "function",
    "function": {
        "name": "robot_emergency_stop",
        "description": (
            "EMERGENCY STOP — immediately halt all robot motion. "
            "Stops all servos and motors on all nodes (or a specific bot). "
            "Use this whenever safety is a concern or when asked to stop robot movement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bot_id": {
                    "type": "string",
                    "description": "Optional: stop only this bot's nodes. Omit to stop ALL nodes.",
                },
            },
        },
    },
}


class RobotEmergencyStop:
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

        if bot_id:
            results = mgr.emergency_stop_bot(bot_id)
            logger.warning("EMERGENCY STOP (tool): bot={}, results={}", bot_id, results)
        else:
            results = mgr.emergency_stop_all()
            logger.warning("EMERGENCY STOP (tool): ALL nodes, results={}", results)

        # Build human-readable summary
        total = len(results)
        ok_count = sum(1 for v in results.values() if v)
        msg = f"Emergency stop sent to {total} node(s): {ok_count} succeeded"
        if ok_count < total:
            failed = [nid for nid, ok in results.items() if not ok]
            msg += f", {total - ok_count} failed ({', '.join(failed)})"

        self._send_result(tool_call_id, json.dumps({"ok": ok_count > 0, "message": msg, "results": results}))

    def _send_result(self, tool_call_id: str, content: str) -> None:
        self.llm_queue.put(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
                "type": "function_call_output",
            }
        )
