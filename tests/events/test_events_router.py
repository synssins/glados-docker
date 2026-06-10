"""Router gate chain + dispatch. All collaborators injected; no I/O."""
import yaml

from glados.events.config import EventsConfig
from glados.events.decision import Verdict
from glados.events.router import EventRouter

RULE = {
    "id": "hallway",
    "enabled": True,
    "trigger": {"entity_id": "binary_sensor.hall_person", "to_state": "on"},
    "mode": "always",
    "action": {"kind": "ha_automation", "target": "automation.hall_light"},
    "cooldown_s": 60,
    "min_clear_s": 0,
}


class FakeClock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t


def _event(entity="binary_sensor.hall_person", old="off", new="on"):
    return {
        "entity_id": entity,
        "old_state": {"state": old} if old is not None else None,
        "new_state": {"state": new} if new is not None else None,
    }


def make_router(tmp_path, rule_overrides=None, *, master=True, verdict=None,
                quiet=False):
    rule = {**RULE, **(rule_overrides or {})}
    path = tmp_path / "events.yaml"
    path.write_text(
        yaml.safe_dump({"enabled": master, "rules": [rule]}), encoding="utf-8"
    )
    fired, announced = [], []
    clock = FakeClock()
    router = EventRouter(
        config_path=path,
        call_service=lambda *a, **kw: {},
        get_state=lambda eid: "4",
        decision_fn=lambda r, gs: verdict or Verdict(act=True, reason="ok", quip="zap"),
        action_fn=lambda spec, cs, **kw: fired.append(spec.target),
        announce_fn=lambda text, speaker, cs: announced.append((text, speaker)),
        quiet_check=lambda: quiet,
        clock=clock,
    )
    router.load()
    return router, fired, announced, clock


def test_match_and_fire_always_mode(tmp_path):
    router, fired, _, _ = make_router(tmp_path)
    router.handle_state_changed(_event())
    assert fired == ["automation.hall_light"]
    assert router.status()["rules"][0]["last_result"] == "fired"


def test_no_match_wrong_entity(tmp_path):
    router, fired, _, _ = make_router(tmp_path)
    router.handle_state_changed(_event(entity="binary_sensor.other"))
    assert fired == []


def test_no_match_wrong_to_state(tmp_path):
    router, fired, _, _ = make_router(tmp_path)
    router.handle_state_changed(_event(new="off"))
    assert fired == []


def test_from_state_narrowing(tmp_path):
    router, fired, _, _ = make_router(
        tmp_path, {"trigger": {"entity_id": "binary_sensor.hall_person",
                               "to_state": "on", "from_state": "off"}})
    router.handle_state_changed(_event(old="unavailable"))
    assert fired == []
    router.handle_state_changed(_event(old="off"))
    assert fired == ["automation.hall_light"]


def test_master_switch_off(tmp_path):
    router, fired, _, _ = make_router(tmp_path, master=False)
    router.handle_state_changed(_event())
    assert fired == []


def test_disabled_rule(tmp_path):
    router, fired, _, _ = make_router(tmp_path, {"enabled": False})
    router.handle_state_changed(_event())
    assert fired == []


def test_quiet_mode_blocks(tmp_path):
    router, fired, _, _ = make_router(tmp_path, quiet=True)
    router.handle_state_changed(_event())
    assert fired == []


def test_cooldown_blocks_second_fire(tmp_path):
    router, fired, _, clock = make_router(tmp_path)
    router.handle_state_changed(_event())
    clock.t += 30                      # < cooldown_s 60
    router.handle_state_changed(_event())
    assert fired == ["automation.hall_light"]
    clock.t += 31                      # past cooldown
    router.handle_state_changed(_event())
    assert len(fired) == 2


def test_decline_consumes_cooldown(tmp_path):
    router, fired, _, clock = make_router(
        tmp_path,
        {"mode": "llm", "decision_prompt": "dark?"},
        verdict=Verdict(act=False, reason="bright"),
    )
    router.handle_state_changed(_event())
    assert fired == []
    assert router.status()["rules"][0]["last_result"] == "declined"
    clock.t += 30
    router.handle_state_changed(_event())          # still cooling down
    assert router.status()["rules"][0]["fire_count"] == 0


def test_min_clear_blocks_flapping(tmp_path):
    router, fired, _, clock = make_router(
        tmp_path, {"min_clear_s": 30, "cooldown_s": 0})
    router.handle_state_changed(_event())          # first fire: no prior clear info -> allowed
    assert len(fired) == 1
    router.handle_state_changed(_event(old="on", new="off"))   # entity clears at t
    clock.t += 10
    router.handle_state_changed(_event())          # cleared only 10s < 30 -> blocked
    assert len(fired) == 1
    router.handle_state_changed(_event(old="on", new="off"))
    clock.t += 31
    router.handle_state_changed(_event())
    assert len(fired) == 2


def test_llm_act_fires_and_announces_quip(tmp_path):
    router, fired, announced, _ = make_router(
        tmp_path,
        {"mode": "llm", "decision_prompt": "dark?",
         "announce": True, "announce_speaker": "media_player.kitchen"},
    )
    router.handle_state_changed(_event())
    assert fired == ["automation.hall_light"]
    assert announced == [("zap", "media_player.kitchen")]


def test_always_mode_announces_static_text(tmp_path):
    router, fired, announced, _ = make_router(
        tmp_path,
        {"announce": True, "announce_speaker": "media_player.kitchen",
         "announce_text": "Hall light on."},
    )
    router.handle_state_changed(_event())
    assert announced == [("Hall light on.", "media_player.kitchen")]


def test_action_failure_sets_error_status(tmp_path):
    from glados.events.actions.ha_action import HAActionError
    def failing_action(spec, cs, **kw):
        raise HAActionError("nope")
    router, _, _, _ = make_router(tmp_path)
    router._action_fn = failing_action
    router.handle_state_changed(_event())
    st = router.status()["rules"][0]
    assert st["last_result"] == "error"
    assert "nope" in st["last_reason"]


def test_dry_run_decides_without_acting(tmp_path):
    router, fired, announced, _ = make_router(
        tmp_path, {"mode": "llm", "decision_prompt": "dark?"})
    result = router.run_rule("hallway", dry_run=True)
    assert result["verdict"]["act"] is True
    assert fired == [] and announced == []


def test_manual_fire_bypasses_gates(tmp_path):
    router, fired, _, _ = make_router(tmp_path, {"enabled": False})
    result = router.run_rule("hallway", dry_run=False)
    assert result["result"] == "fired"
    assert fired == ["automation.hall_light"]
