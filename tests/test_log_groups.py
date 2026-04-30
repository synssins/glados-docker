"""Tests for the per-group log filter (glados.observability.log_groups)."""
from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
import yaml
from loguru import logger

from glados.observability.log_groups import (
    BUILTIN_GROUPS,
    LOCKED_ON_GROUP_IDS,
    LogGroup,
    LogGroupId,
    LogGroupRegistry,
    LogGroupsConfig,
    LogLevel,
    group_logger,
    install_loguru_sink,
    reset_registry_for_tests,
)


# -- Fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Each test gets a fresh module-level registry singleton + clean env."""
    monkeypatch.delenv("GLADOS_LOG_LEVEL", raising=False)
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()
    try:
        logger.remove()
    except ValueError:
        pass


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    return tmp_path / "logging.yaml"


# -- Schema validation -----------------------------------------------------


def test_log_group_id_must_have_dot():
    with pytest.raises(Exception):
        LogGroup(id="malformed", name="x", description="", category="", enabled=True, level=LogLevel.INFO)


def test_log_group_id_lowercase_only():
    with pytest.raises(Exception):
        LogGroup(id="Chat.Foo", name="x", description="", category="", enabled=True, level=LogLevel.INFO)


def test_log_group_valid_id_passes():
    g = LogGroup(id="chat.round1", name="x", description="", category="Chat", enabled=True, level=LogLevel.INFO)
    assert g.id == "chat.round1"


# -- Builtin invariants ---------------------------------------------------


def test_every_constant_id_has_a_builtin():
    constant_ids = set(LogGroupId.all_ids())
    builtin_ids = {g.id for g in BUILTIN_GROUPS}
    missing = constant_ids - builtin_ids
    assert not missing, f"constants without builtins: {missing}"


def test_every_builtin_has_a_constant():
    constant_ids = set(LogGroupId.all_ids())
    builtin_ids = {g.id for g in BUILTIN_GROUPS}
    extras = builtin_ids - constant_ids
    assert not extras, f"builtins without LogGroupId constants: {extras}"


def test_locked_groups_exist_as_builtins():
    builtin_ids = {g.id for g in BUILTIN_GROUPS}
    for locked in LOCKED_ON_GROUP_IDS:
        assert locked in builtin_ids, f"locked group {locked!r} has no builtin"


# -- Default registry ------------------------------------------------------


def test_defaults_loads_all_builtins():
    reg = LogGroupRegistry.defaults()
    ids = {g.id for g in reg.list_groups()}
    assert ids == {g.id for g in BUILTIN_GROUPS}


def test_default_level_is_success():
    reg = LogGroupRegistry.defaults()
    assert reg.default_level == "SUCCESS"


# -- Filter decisions ------------------------------------------------------


def _level_no(name: str) -> int:
    return logger.level(name).no


def test_decide_passes_unbound_at_default_level():
    reg = LogGroupRegistry.defaults()
    assert reg.decide(None, _level_no("SUCCESS")) is True
    assert reg.decide(None, _level_no("INFO")) is False  # below default SUCCESS floor


def test_decide_unknown_group_falls_back_to_default_level():
    reg = LogGroupRegistry.defaults()
    assert reg.decide("unknown.thing", _level_no("SUCCESS")) is True
    assert reg.decide("unknown.thing", _level_no("DEBUG")) is False


def test_decide_disabled_group_drops_records():
    reg = LogGroupRegistry.defaults()
    reg.set_group_state(LogGroupId.PLUGIN.RUNNER, enabled=False)
    assert reg.decide(LogGroupId.PLUGIN.RUNNER, _level_no("INFO")) is False
    assert reg.decide(LogGroupId.PLUGIN.RUNNER, _level_no("DEBUG")) is False


def test_decide_enabled_group_passes_at_or_above_level():
    reg = LogGroupRegistry.defaults()
    reg.set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=True, level=LogLevel.DEBUG)
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("DEBUG")) is True
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("INFO")) is True
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("ERROR")) is True


def test_decide_error_critical_bypass_filter():
    reg = LogGroupRegistry.defaults()
    # Even if the group is disabled and level is WARNING, ERROR/CRITICAL pass.
    reg.set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=False, level=LogLevel.WARNING)
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("ERROR")) is True
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("CRITICAL")) is True


def test_decide_locked_group_cannot_be_disabled():
    reg = LogGroupRegistry.defaults()
    with pytest.raises(PermissionError):
        reg.set_group_state(LogGroupId.AUTH.AUDIT, enabled=False)


def test_locked_group_passes_records_even_if_internal_state_corrupt(tmp_path: Path):
    # Manually construct a registry where audit is disabled internally — the
    # decide() path should still pass records because of the locked-on policy.
    cfg = LogGroupsConfig(
        default_level=LogLevel.SUCCESS,
        groups=[
            LogGroup(
                id=LogGroupId.AUTH.AUDIT, name="audit", description="",
                category="Auth", enabled=False, level=LogLevel.SUCCESS,
            ),
        ] + [g for g in BUILTIN_GROUPS if g.id != LogGroupId.AUTH.AUDIT],
    )
    reg = LogGroupRegistry(cfg, persistence_path=tmp_path / "x.yaml")
    assert reg.decide(LogGroupId.AUTH.AUDIT, _level_no("SUCCESS")) is True


# -- Global env override ---------------------------------------------------


def test_global_env_override_lowers_floor(monkeypatch):
    monkeypatch.setenv("GLADOS_LOG_LEVEL", "WARNING")
    reg = LogGroupRegistry.defaults()
    # GLADOS_LOG_LEVEL=WARNING means: everything below WARNING is dropped
    # regardless of per-group settings.
    reg.set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=True, level=LogLevel.DEBUG)
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("DEBUG")) is False
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("INFO")) is False
    # WARNING and above pass.
    assert reg.decide(LogGroupId.CHAT.ROUND1_STREAM, _level_no("WARNING")) is True


def test_invalid_global_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv("GLADOS_LOG_LEVEL", "GIBBERISH")
    reg = LogGroupRegistry.defaults()
    assert reg.global_override_level is None


# -- Persistence -----------------------------------------------------------


def test_set_group_state_persists_to_yaml(tmp_yaml: Path):
    reg = LogGroupRegistry.defaults(persistence_path=tmp_yaml)
    reg.set_group_state(LogGroupId.AUTONOMY.WEATHER, enabled=True, level=LogLevel.DEBUG)
    assert tmp_yaml.exists()
    raw = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8"))
    weather = next(g for g in raw["groups"] if g["id"] == LogGroupId.AUTONOMY.WEATHER)
    assert weather["enabled"] is True
    assert weather["level"] == "DEBUG"


def test_from_yaml_round_trip(tmp_yaml: Path):
    reg = LogGroupRegistry.defaults(persistence_path=tmp_yaml)
    reg.set_group_state(LogGroupId.HA.WS_CLIENT, enabled=True, level=LogLevel.DEBUG)
    reg.set_default_level(LogLevel.INFO)
    reload = LogGroupRegistry.from_yaml(tmp_yaml)
    grp = reload.get(LogGroupId.HA.WS_CLIENT)
    assert grp is not None
    assert grp.enabled is True
    assert grp.level == LogLevel.DEBUG
    assert reload.default_level == "INFO"


def test_from_yaml_missing_file_falls_back_to_defaults(tmp_yaml: Path):
    assert not tmp_yaml.exists()
    reg = LogGroupRegistry.from_yaml(tmp_yaml, warn_on_missing=False)
    ids = {g.id for g in reg.list_groups()}
    assert ids == {g.id for g in BUILTIN_GROUPS}


def test_from_yaml_corrupt_file_falls_back_to_defaults(tmp_yaml: Path):
    tmp_yaml.write_text("this: is: not: valid: yaml: ::::", encoding="utf-8")
    reg = LogGroupRegistry.from_yaml(tmp_yaml)
    ids = {g.id for g in reg.list_groups()}
    assert ids == {g.id for g in BUILTIN_GROUPS}


def test_from_yaml_corrupt_file_preserves_backup(tmp_yaml: Path):
    tmp_yaml.write_text("BAD: [unterminated", encoding="utf-8")
    LogGroupRegistry.from_yaml(tmp_yaml)
    backups = list(tmp_yaml.parent.glob(f"{tmp_yaml.name}.broken-*"))
    assert backups, "expected a .broken-<ts> backup to be written"


def test_merge_drops_orphan_ids(tmp_yaml: Path):
    """A YAML referring to an unknown group ID gets that ID dropped on load."""
    payload = {
        "default_level": "SUCCESS",
        "groups": [
            {
                "id": "chat.round1_stream",
                "name": "Chat — Round 1 LLM Stream",
                "description": "",
                "category": "Chat",
                "enabled": False,
                "level": "DEBUG",
            },
            {
                "id": "totally.fake.group",
                "name": "Fake",
                "description": "",
                "category": "",
                "enabled": True,
                "level": "INFO",
            },
        ],
    }
    tmp_yaml.write_text(yaml.safe_dump(payload), encoding="utf-8")
    reg = LogGroupRegistry.from_yaml(tmp_yaml)
    assert reg.get("totally.fake.group") is None


def test_merge_overlays_new_builtin_ids(tmp_yaml: Path):
    """An older YAML missing a recently-added builtin still gets that group at load."""
    payload = {
        "default_level": "SUCCESS",
        "groups": [
            {
                "id": "chat.round1_stream",
                "name": "Chat — Round 1 LLM Stream",
                "description": "",
                "category": "Chat",
                "enabled": False,
                "level": "DEBUG",
            },
        ],
    }
    tmp_yaml.write_text(yaml.safe_dump(payload), encoding="utf-8")
    reg = LogGroupRegistry.from_yaml(tmp_yaml)
    # Operator's intent on round1_stream preserved
    assert reg.get("chat.round1_stream").enabled is False
    # New builtins are still present
    assert reg.get(LogGroupId.AUTONOMY.WEATHER) is not None


# -- Replace + reset --------------------------------------------------------


def test_replace_config_rejects_unknown_ids(tmp_yaml: Path):
    reg = LogGroupRegistry.defaults(persistence_path=tmp_yaml)
    new = LogGroupsConfig(
        default_level=LogLevel.INFO,
        groups=[
            LogGroup(
                id="rogue.group", name="Rogue", description="",
                category="X", enabled=True, level=LogLevel.INFO,
            ),
        ],
    )
    with pytest.raises(ValueError, match="unknown group IDs"):
        reg.replace_config(new)


def test_reset_to_defaults_clears_overrides(tmp_yaml: Path):
    reg = LogGroupRegistry.defaults(persistence_path=tmp_yaml)
    reg.set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=False, level=LogLevel.DEBUG)
    reg.reset_to_defaults()
    grp = reg.get(LogGroupId.CHAT.ROUND1_STREAM)
    builtin = next(g for g in BUILTIN_GROUPS if g.id == LogGroupId.CHAT.ROUND1_STREAM)
    assert grp.enabled == builtin.enabled
    assert grp.level == builtin.level


# -- Activity counter -------------------------------------------------------


def test_activity_counter_records_hits():
    reg = LogGroupRegistry.defaults()
    for _ in range(5):
        reg.record_hit(LogGroupId.CHAT.ROUND1_STREAM)
    assert reg.recent_activity(LogGroupId.CHAT.ROUND1_STREAM) == 5


def test_activity_counter_does_not_record_unbound():
    reg = LogGroupRegistry.defaults()
    reg.record_hit(None)
    snapshot = reg.all_recent_activity()
    assert all(v >= 0 for v in snapshot.values())


def test_activity_counter_window_evicts_old_hits():
    reg = LogGroupRegistry.defaults()
    reg._activity.window_seconds = 0.05  # 50 ms window for the test
    reg.record_hit(LogGroupId.CHAT.ROUND1_STREAM)
    assert reg.recent_activity(LogGroupId.CHAT.ROUND1_STREAM) == 1
    time.sleep(0.1)
    assert reg.recent_activity(LogGroupId.CHAT.ROUND1_STREAM) == 0


# -- Loguru sink integration ------------------------------------------------


def test_install_sink_filters_disabled_group(tmp_yaml: Path, monkeypatch):
    """End-to-end: bound logger calls get filtered per registry state."""
    # Build a registry with chat.round1 disabled, install the sink against it.
    monkeypatch.setattr(
        "glados.observability.log_groups._DEFAULT_YAML_PATH", tmp_yaml,
    )
    reset_registry_for_tests()

    buf = io.StringIO()
    install_loguru_sink(buf)

    # Runtime change: disable round1
    from glados.observability.log_groups import get_registry
    get_registry().set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=False)
    get_registry().set_group_state(LogGroupId.CHAT.ROUND2_STREAM, enabled=True, level=LogLevel.INFO)

    log_r1 = group_logger(LogGroupId.CHAT.ROUND1_STREAM)
    log_r2 = group_logger(LogGroupId.CHAT.ROUND2_STREAM)
    log_r1.info("dropped because disabled")
    log_r2.info("kept because enabled")
    out = buf.getvalue()
    assert "dropped because disabled" not in out
    assert "kept because enabled" in out


def test_install_sink_passes_errors_unconditionally(tmp_yaml: Path, monkeypatch):
    """ERROR records bypass the filter even on disabled groups."""
    monkeypatch.setattr(
        "glados.observability.log_groups._DEFAULT_YAML_PATH", tmp_yaml,
    )
    reset_registry_for_tests()

    buf = io.StringIO()
    install_loguru_sink(buf)

    from glados.observability.log_groups import get_registry
    get_registry().set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=False)
    log_r1 = group_logger(LogGroupId.CHAT.ROUND1_STREAM)
    log_r1.error("error always passes")
    assert "error always passes" in buf.getvalue()


def test_install_sink_records_activity_only_for_passed_records(tmp_yaml: Path, monkeypatch):
    monkeypatch.setattr(
        "glados.observability.log_groups._DEFAULT_YAML_PATH", tmp_yaml,
    )
    reset_registry_for_tests()

    buf = io.StringIO()
    install_loguru_sink(buf)

    from glados.observability.log_groups import get_registry
    reg = get_registry()
    reg.set_group_state(LogGroupId.CHAT.ROUND1_STREAM, enabled=True, level=LogLevel.INFO)
    reg.set_group_state(LogGroupId.CHAT.ROUND2_STREAM, enabled=False)

    group_logger(LogGroupId.CHAT.ROUND1_STREAM).info("counted")
    group_logger(LogGroupId.CHAT.ROUND2_STREAM).info("not counted")

    assert reg.recent_activity(LogGroupId.CHAT.ROUND1_STREAM) == 1
    assert reg.recent_activity(LogGroupId.CHAT.ROUND2_STREAM) == 0


# -- by_category UI helper --------------------------------------------------


def test_by_category_groups_correctly():
    reg = LogGroupRegistry.defaults()
    by_cat = reg.by_category()
    assert "Chat" in by_cat
    chat_ids = {g.id for g in by_cat["Chat"]}
    assert LogGroupId.CHAT.ROUND1_STREAM in chat_ids
    assert LogGroupId.AUTONOMY.WEATHER not in chat_ids
