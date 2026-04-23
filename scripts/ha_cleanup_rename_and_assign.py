"""One-shot: rename Theater area -> Basement, then assign high-
confidence orphan entities to their obvious areas. Idempotent —
skips anything already in its target state. Prints before/after
for every mutation so the operator can eyeball diff."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets


RENAME_AREA: list[tuple[str, str]] = [
    # (old_name, new_name)
    ("Theater", "Basement"),
]

ENTITY_ASSIGNMENTS: dict[str, str] = {
    # Living Room
    "light.floor_lamp_one":                 "Living Room",
    "light.living_room_floor_lamp_switch":        "Living Room",
    "light.living_room_overhead_lights_switch":   "Living Room",
    "switch.living_room_overhead_lights_switch":  "Living Room",
    # Master Bedroom
    "light.master_bedroom_color_bar_one":         "Master Bedroom",
    "light.master_bedroom_color_light_bar_cindy": "Master Bedroom",
    "light.master_bedroom_color_light_bar_two":   "Master Bedroom",
    # Office
    "light.wiz_office_ceiling_1_north":           "Office",
    "light.wiz_office_ceiling_2_east":            "Office",
    "light.wiz_office_ceiling_3_west":            "Office",
    # Printers
    "switch.3d_printer_shelf_lights":             "Printers",
    # Wood Shop (workbench appliance)
    "switch.btt_cb1_workbench_lights_2":          "Wood Shop",
    # Basement (after rename)
    "light.basement_main_lights":                 "Basement",
    "switch.basement_perimeter_lights_2":         "Basement",
}


# Prefix-based bulk assignment: every entity whose object_id starts
# with one of these prefixes + "_" (or equals it exactly) and has no
# area yet, gets the mapped area. Applied AFTER the explicit list so
# explicit-list targets always win. Matched against the object_id
# (the part after the domain), so "outdoor" matches switch.outdoor
# and switch.outdoor_motion_zone but NOT switch.back_yard_motion.
PREFIX_ASSIGNMENTS: list[tuple[str, str]] = [
    ("outdoor",       "Back Yard"),
    ("wood_shop",      "Wood Shop"),
    ("front_driveway", "Driveway"),
    ("front_bell", "Porch"),
    ("front_bell",    "Porch"),
    ("mini_mill",      "Wood Shop"),
]


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
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": tok}))
        await ws.recv()

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

        # --- Phase 1: rename areas --------------------------------
        areas = (await call("config/area_registry/list")).get("result") or []
        by_name = {a["name"]: a for a in areas}
        for old, new in RENAME_AREA:
            if old not in by_name:
                print(f"RENAME {old!r}: not present — skipping")
                continue
            if new in by_name and by_name[new]["area_id"] != by_name[old]["area_id"]:
                print(f"RENAME {old!r}: target {new!r} already exists — skipping (manual merge required)")
                continue
            a = by_name[old]
            r = await call(
                "config/area_registry/update",
                area_id=a["area_id"], name=new,
            )
            ok = not r.get("error")
            print(f"RENAME  {old:<20s} -> {new:<20s}  [{'OK' if ok else r.get('error')}]")

        # Refresh area list
        areas = (await call("config/area_registry/list")).get("result") or []
        by_name = {a["name"]: a for a in areas}

        # --- Phase 2: entity assignments --------------------------
        # Sanity-check: every target must exist in the registry.
        missing = [n for n in set(ENTITY_ASSIGNMENTS.values()) if n not in by_name]
        if missing:
            print(f"\nERROR: target areas not in registry: {missing}")
            return 1

        ents = (await call("config/entity_registry/list")).get("result") or []
        by_eid = {e["entity_id"]: e for e in ents}

        changed = 0
        skipped = 0
        missing_ent = 0
        for eid, target_name in ENTITY_ASSIGNMENTS.items():
            e = by_eid.get(eid)
            if e is None:
                print(f"ASSIGN  {eid:<50s}  MISSING from registry")
                missing_ent += 1
                continue
            target_aid = by_name[target_name]["area_id"]
            before = e.get("area_id")
            if before == target_aid:
                print(f"ASSIGN  {eid:<50s}  already in {target_name}  (skip)")
                skipped += 1
                continue
            r = await call(
                "config/entity_registry/update",
                entity_id=eid, area_id=target_aid,
            )
            ok = not r.get("error")
            if ok:
                print(f"ASSIGN  {eid:<50s}  {str(before):<15s} -> {target_name}")
                changed += 1
            else:
                print(f"ASSIGN  {eid:<50s}  ERROR: {r.get('error')}")

        # --- Phase 3: prefix-based bulk assignments ---------------
        # Refresh: earlier assignments may have changed area_ids.
        ents = (await call("config/entity_registry/list")).get("result") or []
        prefix_changed = 0
        prefix_skipped = 0
        print("\n=== Prefix bulk assignments ===")
        for prefix, target_name in PREFIX_ASSIGNMENTS:
            target_aid = by_name[target_name]["area_id"]
            matched = 0
            for e in ents:
                eid = e["entity_id"]
                obj = eid.split(".", 1)[-1]
                if not (obj == prefix or obj.startswith(prefix + "_")):
                    continue
                matched += 1
                if e.get("area_id"):
                    prefix_skipped += 1
                    continue
                r = await call(
                    "config/entity_registry/update",
                    entity_id=eid, area_id=target_aid,
                )
                if not r.get("error"):
                    prefix_changed += 1
            print(f"  prefix {prefix!r:<20s} -> {target_name:<15s}  matched={matched}")

        print(f"\nSummary: renamed={len(RENAME_AREA)}  explicit={changed}  "
              f"already-correct={skipped}  prefix_assigned={prefix_changed}  "
              f"prefix_already_had_area={prefix_skipped}  missing={missing_ent}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
