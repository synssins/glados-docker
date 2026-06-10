"""Schema validation + load/save round-trip for configs/events.yaml."""
import pytest
import yaml

from glados.events.config import (
    EventsConfig,
    EventsConfigError,
    load_events_config,
    save_events_config,
)

VALID_RULE = {
    "id": "hallway_dark_person",
    "enabled": False,
    "trigger": {"entity_id": "binary_sensor.hallway_person", "to_state": "on"},
    "mode": "llm",
    "context_entities": ["sensor.hallway_lux", "sun.sun"],
    "decision_prompt": "Turn on the hallway light only if it is dark.",
    "action": {"kind": "ha_automation", "target": "automation.hallway_light_on"},
    "cooldown_s": 120,
    "min_clear_s": 30,
}


def _config(**overrides):
    rule = {**VALID_RULE, **overrides}
    return {"enabled": True, "rules": [rule]}


def test_valid_config_parses():
    cfg = EventsConfig.model_validate(_config())
    assert cfg.rules[0].id == "hallway_dark_person"
    assert cfg.rules[0].mode == "llm"
    assert cfg.rules[0].action.kind == "ha_automation"


def test_llm_mode_requires_decision_prompt():
    with pytest.raises(ValueError, match="decision_prompt"):
        EventsConfig.model_validate(_config(decision_prompt=None))


def test_announce_requires_speaker():
    with pytest.raises(ValueError, match="announce_speaker"):
        EventsConfig.model_validate(_config(announce=True))


def test_always_mode_announce_requires_text():
    with pytest.raises(ValueError, match="announce_text"):
        EventsConfig.model_validate(_config(
            mode="always", decision_prompt=None,
            announce=True, announce_speaker="media_player.kitchen",
        ))


def test_duplicate_ids_rejected():
    data = {"enabled": True, "rules": [VALID_RULE, VALID_RULE]}
    with pytest.raises(ValueError, match="unique"):
        EventsConfig.model_validate(data)


def test_unknown_keys_rejected():
    with pytest.raises(ValueError):
        EventsConfig.model_validate(_config(surprise_field=1))


def test_target_prefix_must_match_kind():
    with pytest.raises(ValueError, match="automation\\."):
        EventsConfig.model_validate(_config(
            action={"kind": "ha_automation", "target": "script.wrong_domain"},
        ))


def test_ha_service_target_needs_domain_dot_service():
    with pytest.raises(ValueError, match="domain.service"):
        EventsConfig.model_validate(_config(
            action={"kind": "ha_service", "target": "nodotservice"},
        ))


def test_load_save_round_trip(tmp_path):
    path = tmp_path / "events.yaml"
    cfg = EventsConfig.model_validate(_config())
    save_events_config(path, cfg)
    loaded = load_events_config(path)
    assert loaded == cfg
    # Atomic write leaves no temp file behind
    assert list(tmp_path.iterdir()) == [path]


def test_load_missing_file_returns_default(tmp_path):
    cfg = load_events_config(tmp_path / "absent.yaml")
    assert cfg.enabled is True
    assert cfg.rules == []


def test_load_malformed_yaml_raises(tmp_path):
    path = tmp_path / "events.yaml"
    path.write_text("rules: [not, {valid", encoding="utf-8")
    with pytest.raises(EventsConfigError):
        load_events_config(path)


def test_load_schema_violation_raises(tmp_path):
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump(_config(mode="nonsense")), encoding="utf-8")
    with pytest.raises(EventsConfigError):
        load_events_config(path)
