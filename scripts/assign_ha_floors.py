"""One-shot helper: assign areas to floors in the live HA registry
via WebSocket. Meant to be copied into the running glados container
and executed there (HA_URL + HA_TOKEN already in env). Prints
before/after for each area and skips anything already correct."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets


PLAN: dict[str, str] = {
    "Lower Hallway":  "ground_level",
    "Lower Bathroom": "ground_level",
    "Living Room":    "main_level",
    "Cat Room":       "bedroom_level",
    "Printers":       "basement",
    "Server Room":    "basement",
}


async def main() -> int:
    url = (
        os.environ["HA_URL"]
        .replace("http://", "ws://")
        .replace("https://", "wss://")
        .rstrip("/")
        + "/api/websocket"
    )
    tok = os.environ["HA_TOKEN"]
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": tok}))
        await ws.recv()  # auth_ok / auth_invalid

        msg_id = 0
        async def call(msg_type: str, **kw) -> dict:
            nonlocal msg_id
            msg_id += 1
            payload = {"id": msg_id, "type": msg_type, **kw}
            await ws.send(json.dumps(payload))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == msg_id:
                    return r

        areas_resp = await call("config/area_registry/list")
        by_name = {a["name"]: a for a in (areas_resp.get("result") or [])}

        for name, target in PLAN.items():
            if name not in by_name:
                print(f"{name:<20s}  NOT FOUND — skipped")
                continue
            a = by_name[name]
            before = a.get("floor_id")
            if before == target:
                print(
                    f"{name:<20s}  {str(before):<16s}"
                    f" -> (no change)"
                )
                continue
            r = await call(
                "config/area_registry/update",
                area_id=a["area_id"],
                floor_id=target,
            )
            err = r.get("error")
            after = (r.get("result") or {}).get("floor_id")
            status = "OK" if not err else f"ERROR: {err}"
            print(
                f"{name:<20s}  {str(before):<16s}"
                f" -> {str(after):<16s} [{status}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
