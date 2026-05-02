"""Server-side endpoints backing the Configuration → Logging WebUI page.

All endpoints assume the caller has already passed admin authentication
(checked in tts_ui.py before dispatch). Audit events fire from
``set_group_state`` / ``replace_config`` / ``reset_to_defaults`` paths
so every operator change is observable from the audit log.
"""

from __future__ import annotations

import json
import time
from typing import Any

import yaml
from loguru import logger
from pydantic import ValidationError

from glados.observability import (
    LogGroupId,
    LogGroupRegistry,
    LogGroupsConfig,
    LogLevel,
    audit,
    get_registry,
)
from glados.observability.audit import AuditEvent, Origin
from glados.observability.log_groups import (
    LOCKED_ON_GROUP_IDS,
    BUILTIN_GROUPS,
    group_logger,
)


_log = group_logger(LogGroupId.WEBUI.CONFIG_SAVE)


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def list_groups_payload() -> dict[str, Any]:
    """Single GET that backs the Logging page render."""
    reg = get_registry()
    activity = reg.all_recent_activity()
    groups = []
    for g in reg.list_groups():
        groups.append(
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "category": g.category or "Other",
                "enabled": g.enabled,
                "level": g.level.value,
                "locked": g.id in LOCKED_ON_GROUP_IDS,
                "recent_5min": int(activity.get(g.id, 0)),
            }
        )
    return {
        "default_level": reg.default_level,
        "global_override_level": reg.global_override_level,
        "groups": groups,
        "available_levels": [lvl.value for lvl in LogLevel],
    }


def raw_yaml_payload() -> dict[str, Any]:
    reg = get_registry()
    return {"yaml": reg.export_yaml()}


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


def update_group(
    *,
    user: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Toggle a single group's enabled / level state."""
    gid = body.get("id")
    if not isinstance(gid, str) or not gid:
        return 400, {"ok": False, "error": "missing 'id'"}
    enabled = body.get("enabled")
    level = body.get("level")
    if enabled is not None and not isinstance(enabled, bool):
        return 400, {"ok": False, "error": "'enabled' must be a bool"}
    if level is not None and not isinstance(level, str):
        return 400, {"ok": False, "error": "'level' must be a string"}
    if level is not None:
        try:
            LogLevel(level.upper())
        except ValueError:
            return 400, {
                "ok": False,
                "error": f"invalid level {level!r}; must be one of {[l.value for l in LogLevel]}",
            }
    reg = get_registry()
    before = reg.get(gid)
    if before is None:
        return 404, {"ok": False, "error": f"unknown group {gid!r}"}
    try:
        updated = reg.set_group_state(gid, enabled=enabled, level=level)
    except KeyError:
        return 404, {"ok": False, "error": f"unknown group {gid!r}"}
    except PermissionError as exc:
        return 403, {"ok": False, "error": str(exc)}
    _audit_change(
        user=user,
        action="log_groups.update",
        detail={
            "id": gid,
            "before": {"enabled": before.enabled, "level": before.level.value},
            "after": {"enabled": updated.enabled, "level": updated.level.value},
        },
    )
    _log.info(
        "log group {} updated by {}: enabled={} level={}",
        gid, user, updated.enabled, updated.level.value,
    )
    return 200, {
        "ok": True,
        "group": {
            "id": updated.id,
            "enabled": updated.enabled,
            "level": updated.level.value,
        },
    }


def bulk_update(
    *,
    user: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Bulk operations: enable_all / disable_all / category_enable / category_disable."""
    op = body.get("op")
    if op not in (
        "enable_all",
        "disable_all",
        "category_enable",
        "category_disable",
        "set_default_level",
    ):
        return 400, {"ok": False, "error": f"unsupported op {op!r}"}

    reg = get_registry()
    affected: list[str] = []

    if op == "set_default_level":
        level = body.get("level")
        if not isinstance(level, str):
            return 400, {"ok": False, "error": "'level' required"}
        try:
            reg.set_default_level(level)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        _audit_change(
            user=user,
            action="log_groups.set_default_level",
            detail={"level": level.upper()},
        )
        return 200, {"ok": True, "default_level": reg.default_level}

    desired_enabled = op in ("enable_all", "category_enable")
    if op in ("category_enable", "category_disable"):
        category = body.get("category")
        if not isinstance(category, str) or not category:
            return 400, {"ok": False, "error": "'category' required"}
    else:
        category = None

    for grp in reg.list_groups():
        if grp.id in LOCKED_ON_GROUP_IDS and not desired_enabled:
            continue
        if category is not None and (grp.category or "Other") != category:
            continue
        if grp.enabled != desired_enabled:
            try:
                reg.set_group_state(grp.id, enabled=desired_enabled)
                affected.append(grp.id)
            except PermissionError:
                # locked-on protection — already filtered above, defensive.
                continue
    _audit_change(
        user=user,
        action="log_groups.bulk",
        detail={"op": op, "category": category, "affected_count": len(affected)},
    )
    _log.info(
        "log group bulk op {} by {}: {} groups changed (category={})",
        op, user, len(affected), category,
    )
    return 200, {"ok": True, "op": op, "affected": affected}


def reset_to_defaults(*, user: str) -> tuple[int, dict[str, Any]]:
    reg = get_registry()
    reg.reset_to_defaults()
    _audit_change(user=user, action="log_groups.reset", detail={})
    _log.info("log groups reset to defaults by {}", user)
    return 200, {"ok": True}


def save_raw_yaml(
    *,
    user: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    text = body.get("yaml")
    if not isinstance(text, str):
        return 400, {"ok": False, "error": "'yaml' must be a string"}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return 400, {"ok": False, "error": f"YAML parse error: {exc}"}
    try:
        new_cfg = LogGroupsConfig.model_validate(parsed)
    except ValidationError as exc:
        return 400, {"ok": False, "error": f"schema validation failed: {exc}"}
    reg = get_registry()
    try:
        reg.replace_config(new_cfg)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    _audit_change(
        user=user,
        action="log_groups.raw_yaml_save",
        detail={"bytes": len(text)},
    )
    _log.info("log groups raw YAML saved by {} ({} bytes)", user, len(text))
    return 200, {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit_change(*, user: str, action: str, detail: dict[str, Any]) -> None:
    """Best-effort audit emission. Audit subsystem failures must never
    break the operator-facing save path — if the audit logger is not
    initialised, we still allow the change to land."""
    try:
        evt = AuditEvent(
            ts=time.time(),
            origin=Origin.WEBUI_CHAT,
            kind="config_change",
            principal=user,
            extra={"action": action, **detail},
        )
        audit(evt)
    except Exception as exc:
        logger.warning("log_groups audit emit failed: {}", exc)


__all__ = [
    "bulk_update",
    "list_groups_payload",
    "raw_yaml_payload",
    "reset_to_defaults",
    "save_raw_yaml",
    "update_group",
]
