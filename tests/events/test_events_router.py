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
    # get_state returns "4" (not "on"), so load() seeds _clear_since at t=1000.
    # The first fire is now blocked until min_clear_s elapses from load time.
    router, fired, _, clock = make_router(
        tmp_path, {"min_clear_s": 30, "cooldown_s": 0})
    # Advance past min_clear so the seeded clear is old enough to allow the first fire.
    clock.t += 31
    router.handle_state_changed(_event())          # seeded clear is 31s old -> allowed
    assert len(fired) == 1
    router.handle_state_changed(_event(old="on", new="off"))   # entity clears at t
    clock.t += 10
    router.handle_state_changed(_event())          # cleared only 10s < 30 -> blocked
    assert len(fired) == 1
    router.handle_state_changed(_event(old="on", new="off"))
    clock.t += 31
    router.handle_state_changed(_event())
    assert len(fired) == 2


def test_min_clear_cold_start_blocks_when_entity_in_trigger_state(tmp_path):
    # get_state returns "on" (the trigger state) at load time -> no seeding.
    # First event must be blocked (min_clear not yet satisfied).
    # After a clear transition + min_clear_s elapses -> fires.
    rule = {**RULE, "min_clear_s": 30, "cooldown_s": 0}
    path = tmp_path / "events.yaml"
    path.write_text(
        yaml.safe_dump({"enabled": True, "rules": [rule]}), encoding="utf-8"
    )
    fired = []
    clock = FakeClock()
    router = EventRouter(
        config_path=path,
        call_service=lambda *a, **kw: {},
        get_state=lambda eid: "on",   # entity IS in trigger state at load
        decision_fn=lambda r, gs: Verdict(act=True, reason="ok", quip="zap"),
        action_fn=lambda spec, cs, **kw: fired.append(spec.target),
        announce_fn=lambda text, speaker, cs: None,
        quiet_check=lambda: False,
        clock=clock,
    )
    router.load()
    # Immediate trigger event: no _clear_since seeded -> blocked
    router.handle_state_changed(_event())
    assert len(fired) == 0
    # Entity clears, then min_clear_s elapses
    router.handle_state_changed(_event(old="on", new="off"))
    clock.t += 31
    router.handle_state_changed(_event())
    assert len(fired) == 1


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


def test_concurrent_events_do_not_double_fire(tmp_path):
    """FIX 1: Two threads racing through the gate on the same rule at the
    same clock time.  The atomic check-and-stamp under _lock ensures the
    second thread sees the stamp the first thread wrote and is blocked by
    the cooldown, even though both arrived before any LLM work started.

    Choreography:
      - cooldown_s=60 so any non-zero elapsed time blocks the second fire.
      - Both threads hit _gate_and_run at FakeClock t=1000 (same instant).
      - decision_fn blocks until we explicitly release it, so the LLM work
        is entirely outside the lock (as required) and t2 sees the stamp.
    """
    import threading

    # t1 will block here until we let it proceed past the gate into action.
    t1_in_decision = threading.Event()
    proceed = threading.Event()

    def slow_decision(rule, gs):
        t1_in_decision.set()
        proceed.wait(timeout=2)
        return Verdict(act=True, reason="ok", quip="zap")

    rule = {**RULE, "mode": "llm", "decision_prompt": "go?", "cooldown_s": 60}
    path = tmp_path / "events.yaml"
    path.write_text(
        yaml.safe_dump({"enabled": True, "rules": [rule]}), encoding="utf-8"
    )
    fired = []
    clock = FakeClock()   # frozen at t=1000
    router = EventRouter(
        config_path=path,
        call_service=lambda *a, **kw: {},
        get_state=lambda eid: "4",
        decision_fn=slow_decision,
        action_fn=lambda spec, cs, **kw: fired.append(spec.target),
        announce_fn=lambda text, speaker, cs: None,
        quiet_check=lambda: False,
        clock=clock,
    )
    router.load()

    def fire_event():
        router.handle_state_changed(_event())

    t1 = threading.Thread(target=fire_event)
    t1.start()
    t1_in_decision.wait(timeout=2)   # t1 past the gate stamp, blocked in LLM call

    # t2 races in at the same clock time; cooldown stamp was written by t1
    # so t2 sees now (1000) - last (1000) = 0 < 60 -> blocked.
    t2 = threading.Thread(target=fire_event)
    t2.start()
    t2.join(timeout=2)               # t2 should return quickly (blocked by gate)

    proceed.set()                    # release t1's LLM call
    t1.join(timeout=5)

    assert len(fired) == 1, f"expected 1 fire, got {len(fired)}: {fired}"
