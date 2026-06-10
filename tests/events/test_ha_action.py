"""EventActionSpec -> HAClient.call_service mapping, retry, loud failure."""
import pytest

from glados.events.config import EventActionSpec
from glados.events.actions.ha_action import HAActionError, execute_ha_action


class Recorder:
    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    def __call__(self, domain, service, service_data=None, target=None, timeout_s=None):
        self.calls.append((domain, service, service_data, target))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("HA says no")
        return {"success": True}


def test_automation_maps_to_automation_trigger():
    rec = Recorder()
    spec = EventActionSpec(kind="ha_automation", target="automation.hallway_light_on")
    execute_ha_action(spec, rec, sleep=lambda s: None)
    assert rec.calls == [
        ("automation", "trigger", {}, {"entity_id": "automation.hallway_light_on"}),
    ]


def test_script_and_scene_map_to_turn_on():
    for kind, target, domain in [
        ("ha_script", "script.evening", "script"),
        ("ha_scene", "scene.movie", "scene"),
    ]:
        rec = Recorder()
        execute_ha_action(EventActionSpec(kind=kind, target=target), rec, sleep=lambda s: None)
        assert rec.calls == [(domain, "turn_on", {}, {"entity_id": target})]


def test_ha_service_maps_domain_service_entity_data():
    rec = Recorder()
    spec = EventActionSpec(
        kind="ha_service", target="light.turn_on",
        entity_id="light.hallway", data={"brightness_pct": 40},
    )
    execute_ha_action(spec, rec, sleep=lambda s: None)
    assert rec.calls == [
        ("light", "turn_on", {"brightness_pct": 40}, {"entity_id": "light.hallway"}),
    ]


def test_one_retry_then_success():
    rec = Recorder(fail_times=1)
    execute_ha_action(
        EventActionSpec(kind="ha_scene", target="scene.movie"), rec, sleep=lambda s: None
    )
    assert len(rec.calls) == 2


def test_two_failures_raise_ha_action_error():
    rec = Recorder(fail_times=2)
    with pytest.raises(HAActionError):
        execute_ha_action(
            EventActionSpec(kind="ha_scene", target="scene.movie"), rec, sleep=lambda s: None
        )
    assert len(rec.calls) == 2
