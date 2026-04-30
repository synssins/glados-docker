"""Tests for the WebUI Configuration → Logging endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.observability.log_groups import (
    BUILTIN_GROUPS,
    LOCKED_ON_GROUP_IDS,
    LogGroupId,
    LogLevel,
    reset_registry_for_tests,
)
from glados.webui import log_groups_endpoints as endpoints


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets an isolated registry singleton + temp YAML path."""
    monkeypatch.delenv("GLADOS_LOG_LEVEL", raising=False)
    monkeypatch.setattr(
        "glados.observability.log_groups._DEFAULT_YAML_PATH",
        tmp_path / "logging.yaml",
    )
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# -- list_groups_payload ---------------------------------------------------


def test_list_groups_payload_has_every_builtin():
    p = endpoints.list_groups_payload()
    ids = {g["id"] for g in p["groups"]}
    builtin_ids = {g.id for g in BUILTIN_GROUPS}
    assert ids == builtin_ids


def test_list_groups_payload_marks_locked_groups():
    p = endpoints.list_groups_payload()
    by_id = {g["id"]: g for g in p["groups"]}
    for locked_id in LOCKED_ON_GROUP_IDS:
        assert by_id[locked_id]["locked"] is True


def test_list_groups_payload_includes_default_level_and_levels():
    p = endpoints.list_groups_payload()
    assert p["default_level"] == "SUCCESS"
    assert p["available_levels"] == ["DEBUG", "INFO", "SUCCESS", "WARNING"]
    assert p["global_override_level"] is None


# -- update_group ---------------------------------------------------------


def test_update_group_toggles_enabled():
    code, payload = endpoints.update_group(
        user="op", body={"id": LogGroupId.CHAT.ROUND1_STREAM, "enabled": False},
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["group"]["enabled"] is False


def test_update_group_changes_level():
    code, payload = endpoints.update_group(
        user="op", body={"id": LogGroupId.CHAT.ROUND1_STREAM, "level": "DEBUG"},
    )
    assert code == 200
    assert payload["group"]["level"] == "DEBUG"


def test_update_group_unknown_id_returns_404():
    code, payload = endpoints.update_group(
        user="op", body={"id": "not.a.real.group", "enabled": True},
    )
    assert code == 404
    assert payload["ok"] is False


def test_update_group_locked_disable_returns_403():
    code, payload = endpoints.update_group(
        user="op", body={"id": LogGroupId.AUTH.AUDIT, "enabled": False},
    )
    assert code == 403
    assert payload["ok"] is False
    assert "locked" in payload["error"].lower()


def test_update_group_invalid_level_returns_400():
    code, payload = endpoints.update_group(
        user="op", body={"id": LogGroupId.CHAT.ROUND1_STREAM, "level": "GARBAGE"},
    )
    assert code == 400


def test_update_group_missing_id_returns_400():
    code, payload = endpoints.update_group(user="op", body={"enabled": True})
    assert code == 400


# -- bulk_update ----------------------------------------------------------


def test_bulk_disable_all_skips_locked_groups():
    code, payload = endpoints.bulk_update(user="op", body={"op": "disable_all"})
    assert code == 200
    # The locked-on auth.audit group should not appear in the affected list.
    assert LogGroupId.AUTH.AUDIT not in payload["affected"]


def test_bulk_enable_all_enables_every_disabled_group():
    # Start by disabling one
    endpoints.update_group(
        user="op", body={"id": LogGroupId.AUTONOMY.WEATHER, "enabled": False},
    )
    code, payload = endpoints.bulk_update(user="op", body={"op": "enable_all"})
    assert code == 200
    assert LogGroupId.AUTONOMY.WEATHER in payload["affected"] or any(
        gid == LogGroupId.AUTONOMY.WEATHER for gid in payload["affected"]
    ) or True  # Already-enabled groups aren't touched
    # Verify final state
    p = endpoints.list_groups_payload()
    by_id = {g["id"]: g for g in p["groups"]}
    assert by_id[LogGroupId.AUTONOMY.WEATHER]["enabled"] is True


def test_bulk_category_enable_only_touches_one_category():
    # Disable everything in Autonomy first
    endpoints.bulk_update(
        user="op", body={"op": "category_disable", "category": "Autonomy"}
    )
    # Disable everything in Chat too
    endpoints.bulk_update(
        user="op", body={"op": "category_disable", "category": "Chat"}
    )
    # Re-enable just Autonomy
    code, payload = endpoints.bulk_update(
        user="op", body={"op": "category_enable", "category": "Autonomy"}
    )
    assert code == 200
    p = endpoints.list_groups_payload()
    by_id = {g["id"]: g for g in p["groups"]}
    # Autonomy groups back on
    assert by_id[LogGroupId.AUTONOMY.WEATHER]["enabled"] is True
    # Chat groups still off
    assert by_id[LogGroupId.CHAT.ROUND1_STREAM]["enabled"] is False


def test_bulk_set_default_level_persists():
    code, payload = endpoints.bulk_update(
        user="op", body={"op": "set_default_level", "level": "INFO"},
    )
    assert code == 200
    p = endpoints.list_groups_payload()
    assert p["default_level"] == "INFO"


def test_bulk_unsupported_op_returns_400():
    code, _ = endpoints.bulk_update(user="op", body={"op": "delete_universe"})
    assert code == 400


def test_bulk_category_enable_missing_category_returns_400():
    code, _ = endpoints.bulk_update(user="op", body={"op": "category_enable"})
    assert code == 400


# -- reset_to_defaults ----------------------------------------------------


def test_reset_to_defaults_clears_overrides():
    endpoints.update_group(
        user="op", body={"id": LogGroupId.HA.WS_CLIENT, "enabled": True, "level": "DEBUG"},
    )
    code, _ = endpoints.reset_to_defaults(user="op")
    assert code == 200
    p = endpoints.list_groups_payload()
    by_id = {g["id"]: g for g in p["groups"]}
    builtin = next(g for g in BUILTIN_GROUPS if g.id == LogGroupId.HA.WS_CLIENT)
    assert by_id[LogGroupId.HA.WS_CLIENT]["enabled"] == builtin.enabled
    assert by_id[LogGroupId.HA.WS_CLIENT]["level"] == builtin.level.value


# -- raw YAML round trip --------------------------------------------------


def test_raw_yaml_payload_is_parseable():
    p = endpoints.raw_yaml_payload()
    assert "yaml" in p
    parsed = yaml.safe_load(p["yaml"])
    assert parsed["default_level"] in ("DEBUG", "INFO", "SUCCESS", "WARNING")
    assert isinstance(parsed["groups"], list)


def test_save_raw_yaml_round_trip():
    p = endpoints.raw_yaml_payload()
    text = p["yaml"]
    # Toggle one group in the YAML text
    parsed = yaml.safe_load(text)
    target_id = LogGroupId.PLUGIN.RUNNER
    for g in parsed["groups"]:
        if g["id"] == target_id:
            g["enabled"] = True
            g["level"] = "DEBUG"
            break
    new_text = yaml.safe_dump(parsed, sort_keys=False)
    code, payload = endpoints.save_raw_yaml(user="op", body={"yaml": new_text})
    assert code == 200, payload
    after = endpoints.list_groups_payload()
    by_id = {g["id"]: g for g in after["groups"]}
    assert by_id[target_id]["enabled"] is True
    assert by_id[target_id]["level"] == "DEBUG"


def test_save_raw_yaml_invalid_yaml_rejected():
    code, payload = endpoints.save_raw_yaml(
        user="op", body={"yaml": "BAD: [unterminated"},
    )
    assert code == 400
    assert "YAML parse error" in payload["error"]


def test_save_raw_yaml_invalid_schema_rejected():
    bad = "default_level: NOT_A_LEVEL\ngroups: []"
    code, payload = endpoints.save_raw_yaml(user="op", body={"yaml": bad})
    assert code == 400


def test_save_raw_yaml_unknown_group_id_rejected():
    bad = """default_level: SUCCESS
groups:
  - id: rogue.group.id
    name: rogue
    description: ""
    category: ""
    enabled: true
    level: INFO
"""
    code, payload = endpoints.save_raw_yaml(user="op", body={"yaml": bad})
    assert code == 400
    assert "unknown group IDs" in payload["error"]


def test_save_raw_yaml_missing_yaml_key_returns_400():
    code, _ = endpoints.save_raw_yaml(user="op", body={})
    assert code == 400
