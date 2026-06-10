"""LLM verdict for mode: llm rules. Acting is NEVER the failure default."""
from glados.events.config import EventRule
from glados.events.decision import Verdict, decide

RULE = EventRule.model_validate({
    "id": "hallway_dark_person",
    "trigger": {"entity_id": "binary_sensor.hallway_person", "to_state": "on"},
    "mode": "llm",
    "context_entities": ["sensor.hallway_lux", "sun.sun"],
    "decision_prompt": "Turn on the hallway light only if it is dark.",
    "action": {"kind": "ha_automation", "target": "automation.hallway_light_on"},
})


def _get_state(entity_id):
    return {"sensor.hallway_lux": "4", "sun.sun": "below_horizon"}.get(entity_id)


def test_act_verdict_parsed():
    llm = lambda cfg, system_prompt, user_prompt, **kw: (
        '{"act": true, "reason": "lux is 4, it is dark", "quip": "Let there be light."}'
    )
    v = decide(RULE, _get_state, llm=llm)
    assert v == Verdict(act=True, reason="lux is 4, it is dark", quip="Let there be light.")


def test_decline_verdict_parsed():
    llm = lambda cfg, system_prompt, user_prompt, **kw: '{"act": false, "reason": "bright"}'
    v = decide(RULE, _get_state, llm=llm)
    assert v.act is False and v.reason == "bright" and v.quip == ""


def test_think_block_stripped():
    llm = lambda cfg, system_prompt, user_prompt, **kw: (
        '<think>hmm</think>\n{"act": true, "reason": "dark", "quip": ""}'
    )
    assert decide(RULE, _get_state, llm=llm).act is True


def test_llm_none_means_no_act():
    v = decide(RULE, _get_state, llm=lambda *a, **kw: None)
    assert v.act is False and "decision error" in v.reason


def test_garbage_means_no_act():
    v = decide(RULE, _get_state, llm=lambda *a, **kw: "sure, turning it on!")
    assert v.act is False and "decision error" in v.reason


def test_llm_exception_means_no_act():
    def boom(*a, **kw):
        raise RuntimeError("connection refused")
    v = decide(RULE, _get_state, llm=boom)
    assert v.act is False and "decision error" in v.reason


def test_context_in_prompt_including_unavailable():
    captured = {}
    def llm(cfg, system_prompt, user_prompt, **kw):
        captured["user"] = user_prompt
        return '{"act": false, "reason": "x"}'
    decide(RULE, lambda eid: None, llm=llm)   # every context entity unreadable
    assert "sensor.hallway_lux: unavailable" in captured["user"]
    assert "Turn on the hallway light only if it is dark." in captured["user"]
    assert "binary_sensor.hallway_person" in captured["user"]
