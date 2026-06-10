# Event→Action Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a configured HA event fires (person in dark hallway), GLaDOS decides (`always` or fast-LLM verdict) and triggers a whitelisted HA automation/script/scene/service — with a full WebUI rule editor, per-rule announce (default silent), and loud auditing of every fire/decline/failure.

**Architecture:** A new `glados/events/` package: pydantic-validated `configs/events.yaml` (single source of truth), an `EventRouter` subscribed to the EXISTING `HAClient` singleton's `on_state_changed()` fan-out (no new WebSocket; `ha_sensor_watcher` untouched), a triage-lane LLM decision module (fail-safe = don't act), and an `ha_action` executor on `HAClient.call_service()`. Admin-only REST + Integrations → Events WebUI page edit the YAML and expose dry-run/fire.

**Tech Stack:** Python 3.12, pydantic v2, the existing `HAClient` (`glados/ha/ws_client.py`), `llm_call`/`LLMConfig` (`glados/autonomy/llm_client.py`), audit pipeline (`glados/observability/audit.py`), `tts_ui.py` route conventions with `require_perm`, design-system v3 WebUI conventions.

**Spec:** `docs/superpowers/specs/2026-06-09-event-action-engine-design.md`

**Branch:** `feat/event-action-engine`. TDD per repo standard: every task writes the failing test first and runs it before implementing. Full suite must stay green (`python -m pytest -q` → currently 1913 passed / 5 skipped).

**Key existing signatures (verified 2026-06-10 — do not re-derive):**
- `glados.ha.get_client() -> HAClient | None`, `get_cache() -> EntityCache | None` (`glados/ha/__init__.py:52,60`), stood up by `server.py:_init_ha_client` (server.py:156).
- `HAClient.on_state_changed(cb: Callable[[dict], None])` (`ws_client.py:382`) — `cb(data)` where `data = {"entity_id": str, "old_state": {"state": ...}|None, "new_state": {"state": ...}|None}`.
- `HAClient.call_service(domain, service, service_data=None, target=None, timeout_s=None) -> dict` (`ws_client.py:164`), thread-safe.
- `LLMConfig.for_slot("llm_triage")` (`llm_client.py:63`); `llm_call(config, system_prompt, user_prompt, json_response=False, json_schema=None, max_tokens=None) -> str | None` (`llm_client.py:82`) — OpenAI wire format internally.
- `EntityCache.snapshot() -> list[EntityState]` (`entity_cache.py:510`); `EntityState` has `entity_id`, `friendly_name`, `domain`, `state` (`entity_cache.py:316`).
- `Origin` = plain class of string constants + `ALL` frozenset (`audit.py:40`); emit via `audit(AuditEvent(...))` (`audit.py:226`).
- WebUI routes: `if/elif` chains in `tts_ui.py` `do_GET`/`do_POST`; admin gate `if not require_perm(self, "admin"): return` (pattern at `tts_ui.py:1891`). Pages: `glados/webui/pages/<name>.py` + nav `<a>` in `pages/_shell.py:31-53` (`data-nav-key="config.X"` inside the admin-gated Configuration group).
- Speaker-play pattern to mirror: `screener._play_on_speaker` (`glados/doorbell/screener.py:858`) — TLS-aware serve URL from `store_cfg.serve_host/serve_port`, WAV copied into the serve dir.

---

## File Structure

| File | Responsibility |
|---|---|
| `glados/events/__init__.py` | Package marker + `get_router()` singleton accessor. |
| `glados/events/config.py` | Schema + load/save (atomic) for `configs/events.yaml`. |
| `glados/events/decision.py` | `mode: llm` verdict (context → triage LLM → fail-safe parse). |
| `glados/events/actions/__init__.py` | Package marker. |
| `glados/events/actions/ha_action.py` | EventActionSpec → `call_service`, one retry, loud failure. |
| `glados/events/announce.py` | Optional spoken line: TTS → serve dir → `media_player.play_media`. |
| `glados/events/router.py` | Match, gate chain, dispatch, runtime status, reload. |
| `glados/observability/audit.py` (modify) | `Origin.EVENT_RULE`. |
| `glados/core/engine.py` (modify) | Construct + start router; quiet-mode flag. |
| `glados/webui/tts_ui.py` (modify) | Admin REST: CRUD/targets/dry_run/fire/master. |
| `glados/webui/pages/events.py` + `_shell.py` (modify) + `static/ui.js`/`style.css` (modify) | Integrations → Events page. |
| `configs/events.example.yaml` | Committed example, placeholder entity ids only. |
| `tests/events/…` | One test file per module (see tasks). |

---

### Task 1: `Origin.EVENT_RULE` audit origin

**Files:**
- Modify: `glados/observability/audit.py` (Origin class at line ~40)
- Test: `tests/events/test_audit_origin.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/events/__init__.py  (empty file, required — tests/ is a package)
```

```python
# tests/events/test_audit_origin.py
"""Origin.EVENT_RULE exists and is a member of Origin.ALL."""
from glados.observability.audit import Origin


def test_event_rule_origin_exists():
    assert Origin.EVENT_RULE == "event_rule"


def test_event_rule_in_all():
    assert "event_rule" in Origin.ALL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_audit_origin.py -q`
Expected: FAIL — `AttributeError: type object 'Origin' has no attribute 'EVENT_RULE'`

- [ ] **Step 3: Implement**

In `glados/observability/audit.py`, inside the `Origin` class (after `MQTT_CMD`), add:

```python
    EVENT_RULE = "event_rule"      # event→action engine rule firing
```

and add `"event_rule"` to the `ALL` frozenset literal in the same class.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_audit_origin.py -q` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/observability/audit.py tests/events/
git commit -m "feat(events): add Origin.EVENT_RULE audit origin"
```

---

### Task 2: `events.yaml` schema + atomic load/save

**Files:**
- Create: `glados/events/__init__.py`, `glados/events/config.py`
- Test: `tests/events/test_events_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_events_config.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/events/test_events_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'glados.events'`

- [ ] **Step 3: Implement**

```python
# glados/events/__init__.py
"""Event→Action engine — HA executes, GLaDOS decides.

Spec: docs/superpowers/specs/2026-06-09-event-action-engine-design.md
"""
from __future__ import annotations

_router = None


def set_router(router) -> None:
    global _router
    _router = router


def get_router():
    """The process-wide EventRouter, or None before engine init."""
    return _router
```

```python
# glados/events/config.py
"""Schema + load/save for configs/events.yaml (single source of truth)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventsConfigError(Exception):
    """events.yaml is unreadable or fails schema validation."""


class EventTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    to_state: str
    from_state: str | None = None


_KIND_PREFIX = {
    "ha_automation": "automation.",
    "ha_script": "script.",
    "ha_scene": "scene.",
}


class EventActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ha_automation", "ha_script", "ha_scene", "ha_service"]
    target: str
    entity_id: str | None = None          # ha_service only
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_target(self) -> "EventActionSpec":
        prefix = _KIND_PREFIX.get(self.kind)
        if prefix and not self.target.startswith(prefix):
            raise ValueError(
                f"action target for kind={self.kind} must start with {prefix!r}"
            )
        if self.kind == "ha_service" and "." not in self.target:
            raise ValueError("ha_service target must be 'domain.service'")
        return self


class EventRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    enabled: bool = False
    trigger: EventTrigger
    mode: Literal["always", "llm"] = "always"
    context_entities: list[str] = Field(default_factory=list)
    decision_prompt: str | None = None
    action: EventActionSpec
    cooldown_s: float = 60.0
    min_clear_s: float = 0.0
    announce: bool = False
    announce_speaker: str | None = None
    announce_text: str | None = None

    @model_validator(mode="after")
    def _check_conditionals(self) -> "EventRule":
        if self.mode == "llm" and not (self.decision_prompt or "").strip():
            raise ValueError(f"rule {self.id!r}: mode=llm requires decision_prompt")
        if self.announce and not self.announce_speaker:
            raise ValueError(f"rule {self.id!r}: announce=true requires announce_speaker")
        if self.announce and self.mode == "always" and not (self.announce_text or "").strip():
            raise ValueError(
                f"rule {self.id!r}: announce=true with mode=always requires announce_text"
            )
        return self


class EventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    rules: list[EventRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "EventsConfig":
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self


def default_events_path() -> Path:
    return Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "events.yaml"


def load_events_config(path: Path) -> EventsConfig:
    """Missing file → defaults (engine on, no rules). Malformed → raise."""
    if not path.exists():
        return EventsConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return EventsConfig.model_validate(raw)
    except Exception as exc:
        raise EventsConfigError(f"{path}: {exc}") from exc


def save_events_config(path: Path, config: EventsConfig) -> None:
    """Atomic write: temp file + os.replace, mirroring config-store style."""
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_config.py -q` → 12 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/events/ tests/events/test_events_config.py
git commit -m "feat(events): events.yaml schema + atomic load/save"
```

---

### Task 3: Decision module (`mode: llm`, fail-safe = no act)

**Files:**
- Create: `glados/events/decision.py`
- Test: `tests/events/test_events_decision.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_events_decision.py
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
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/events/test_events_decision.py -q` → FAIL (`No module named 'glados.events.decision'`).

- [ ] **Step 3: Implement**

```python
# glados/events/decision.py
"""mode: llm verdict — triage-lane LLM decides act / don't-act.

Fail-safe direction is fixed: any timeout, error, or unparseable
response is a NO-ACT. Acting is never the failure default.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call
from glados.core.llm_directives import strip_thinking_response
from glados.events.config import EventRule

_SYSTEM_PROMPT = """\
You are the decision module of a smart-home event engine. An event has
fired and one pre-approved action is available. Decide whether to take
it, using ONLY the provided context.

Reply with ONLY a single JSON object, no prose, no markdown:
{"act": true|false, "reason": "one short sentence", "quip": "optional dry one-liner in GLaDOS's voice, empty string if none"}"""


@dataclass
class Verdict:
    act: bool
    reason: str
    quip: str = ""


def _no_act(why: str) -> Verdict:
    logger.warning("events decision error → no-act: {}", why)
    return Verdict(act=False, reason=f"decision error: {why}")


def decide(
    rule: EventRule,
    get_state: Callable[[str], str | None],
    llm: Callable[..., str | None] = llm_call,
) -> Verdict:
    lines = [
        f"Question: {rule.decision_prompt}",
        f"Trigger: {rule.trigger.entity_id} changed to "
        f"'{rule.trigger.to_state}'.",
        f"Local time: {time.strftime('%A %H:%M')}",
        "Context:",
    ]
    for eid in rule.context_entities:
        state = get_state(eid)
        lines.append(f"  {eid}: {state if state is not None else 'unavailable'}")
    lines.append(
        f"Available action: {rule.action.target}. Should it run right now?"
    )
    user_prompt = "\n".join(lines)

    try:
        raw = llm(
            LLMConfig.for_slot("llm_triage"),
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=128,
        )
    except Exception as exc:
        return _no_act(f"LLM call raised: {exc}")
    if not raw:
        return _no_act("LLM returned empty response")

    txt = strip_thinking_response(raw)
    start = txt.find("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        return _no_act(f"no JSON object in response: {txt[:80]!r}")
    try:
        obj = json.loads(txt[start:end + 1])
    except json.JSONDecodeError as exc:
        return _no_act(f"invalid JSON: {exc}")
    if not isinstance(obj, dict) or not isinstance(obj.get("act"), bool):
        return _no_act(f"missing/invalid 'act' field: {obj!r}")
    return Verdict(
        act=obj["act"],
        reason=str(obj.get("reason") or "").strip() or "(no reason given)",
        quip=str(obj.get("quip") or "").strip(),
    )
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_decision.py -q` → 7 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/events/decision.py tests/events/test_events_decision.py
git commit -m "feat(events): triage-lane LLM decision module, fail-safe no-act"
```

---

### Task 4: `ha_action` executor

**Files:**
- Create: `glados/events/actions/__init__.py` (empty), `glados/events/actions/ha_action.py`
- Test: `tests/events/test_ha_action.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_ha_action.py
"""EventActionSpec → HAClient.call_service mapping, retry, loud failure."""
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
```

- [ ] **Step 2: Run to verify failure** — FAIL (`No module named 'glados.events.actions'`).

- [ ] **Step 3: Implement**

```python
# glados/events/actions/ha_action.py
"""Execute a whitelisted HA action via HAClient.call_service.

One retry with a short backoff, then a loud, typed failure — never a
silent one (house rule).
"""
from __future__ import annotations

import time
from typing import Callable

from loguru import logger

from glados.events.config import EventActionSpec


class HAActionError(Exception):
    """The HA service call failed after retry."""


def _map_call(spec: EventActionSpec) -> tuple[str, str, dict, dict]:
    if spec.kind == "ha_automation":
        return "automation", "trigger", {}, {"entity_id": spec.target}
    if spec.kind == "ha_script":
        return "script", "turn_on", {}, {"entity_id": spec.target}
    if spec.kind == "ha_scene":
        return "scene", "turn_on", {}, {"entity_id": spec.target}
    domain, service = spec.target.split(".", 1)
    target = {"entity_id": spec.entity_id} if spec.entity_id else {}
    return domain, service, dict(spec.data), target


def execute_ha_action(
    spec: EventActionSpec,
    call_service: Callable[..., dict],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    domain, service, service_data, target = _map_call(spec)
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            call_service(domain, service, service_data=service_data, target=target)
            if attempt == 2:
                logger.warning("events ha_action {} succeeded on retry", spec.target)
            return
        except Exception as exc:
            last_exc = exc
            logger.error(
                "events ha_action {}/{} target={} attempt {} failed: {}",
                domain, service, spec.target, attempt, exc,
            )
            if attempt == 1:
                sleep(1.0)
    raise HAActionError(f"{domain}.{service} ({spec.target}) failed twice: {last_exc}")
```

Note: tests assert positional `(domain, service, service_data, target)` via the Recorder's signature; the production call passes them as keywords, matching `HAClient.call_service(domain, service, service_data=None, target=None, timeout_s=None)` (`ws_client.py:164`).

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_ha_action.py -q` → 5 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/events/actions/ tests/events/test_ha_action.py
git commit -m "feat(events): ha_action executor with retry + loud failure"
```

---

### Task 5: Announce module

**Files:**
- Create: `glados/events/announce.py`
- Test: `tests/events/test_events_announce.py`

Mirrors `screener._generate_tts` + `_play_on_speaker` (`glados/doorbell/screener.py:858`): synthesize WAV via the internal TTS endpoint, drop it in the serve dir, play through `media_player.play_media` — but via the injected `call_service` (WS) instead of raw REST. Failures are WARNINGs; the action result stands (spec §5/§7).

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_events_announce.py
from pathlib import Path
from unittest.mock import patch

from glados.events import announce as ann


def test_announce_plays_generated_wav(tmp_path):
    calls = []
    def call_service(domain, service, service_data=None, target=None, timeout_s=None):
        calls.append((domain, service, service_data))
        return {}
    wav = tmp_path / "evt_x.wav"
    wav.write_bytes(b"RIFF")
    with patch.object(ann, "_generate_tts", return_value=wav) as gen, \
         patch.object(ann, "_serve_url", return_value="http://serve.example/evt_x.wav"):
        ok = ann.announce("Let there be light.", "media_player.kitchen", call_service)
    assert ok is True
    gen.assert_called_once()
    assert calls == [(
        "media_player", "play_media",
        {"entity_id": ["media_player.kitchen"],
         "media_content_id": "http://serve.example/evt_x.wav",
         "media_content_type": "music"},
    )]


def test_tts_failure_returns_false_never_raises():
    with patch.object(ann, "_generate_tts", return_value=None):
        ok = ann.announce("x", "media_player.kitchen", lambda *a, **kw: {})
    assert ok is False


def test_play_failure_returns_false_never_raises(tmp_path):
    wav = tmp_path / "evt_x.wav"
    wav.write_bytes(b"RIFF")
    def boom(*a, **kw):
        raise RuntimeError("speaker offline")
    with patch.object(ann, "_generate_tts", return_value=wav), \
         patch.object(ann, "_serve_url", return_value="http://s/e.wav"):
        assert ann.announce("x", "media_player.kitchen", boom) is False
```

- [ ] **Step 2: Run to verify failure** — FAIL (`No module named 'glados.events.announce'`).

- [ ] **Step 3: Implement**

```python
# glados/events/announce.py
"""Optional spoken line after an event action fires.

Best-effort by design: announce failures are WARNINGs and never roll
back or block the action itself (spec §5).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from loguru import logger

_AUDIO = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
SERVE_DIR = _AUDIO / "glados_ha"


def _generate_tts(text: str) -> Path | None:
    """Synthesize `text` → WAV in the serve dir. None on failure."""
    from glados.core.config_store import cfg as store_cfg
    url = f"{store_cfg.service_url('tts')}/v1/audio/speech"
    payload = {"input": text, "voice": "glados", "response_format": "wav"}
    req = Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        SERVE_DIR.mkdir(parents=True, exist_ok=True)
        with urlopen(req, timeout=30) as resp:
            out = SERVE_DIR / f"event_{uuid.uuid4().hex[:8]}.wav"
            out.write_bytes(resp.read())
            return out
    except Exception as exc:
        logger.warning("events announce: TTS failed: {}", exc)
        return None


def _serve_url(wav_path: Path) -> str:
    """TLS-aware public URL for a WAV already in the serve dir
    (mirrors screener._play_on_speaker, screener.py:858)."""
    from glados.core.config_store import cfg as store_cfg
    from glados.core.tls import is_tls_active
    proto = "https" if is_tls_active() else "http"
    return f"{proto}://{store_cfg.serve_host}:{store_cfg.serve_port}/{wav_path.name}"


def announce(text: str, speaker: str, call_service: Callable[..., dict]) -> bool:
    wav = _generate_tts(text)
    if wav is None:
        return False
    try:
        call_service(
            "media_player", "play_media",
            service_data={
                "entity_id": [speaker],
                "media_content_id": _serve_url(wav),
                "media_content_type": "music",
            },
        )
        return True
    except Exception as exc:
        logger.warning("events announce: play_media on {} failed: {}", speaker, exc)
        return False
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_announce.py -q` → 3 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/events/announce.py tests/events/test_events_announce.py
git commit -m "feat(events): best-effort announce via TTS + media_player"
```

---

### Task 6: EventRouter — match, gates, dispatch, status

**Files:**
- Create: `glados/events/router.py`
- Test: `tests/events/test_events_router.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_events_router.py
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
    router.handle_state_changed(_event())          # first fire: no prior clear info → allowed
    assert len(fired) == 1
    router.handle_state_changed(_event(old="on", new="off"))   # entity clears at t
    clock.t += 10
    router.handle_state_changed(_event())          # cleared only 10s < 30 → blocked
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
```

- [ ] **Step 2: Run to verify failure** — FAIL (`No module named 'glados.events.router'`).

- [ ] **Step 3: Implement**

```python
# glados/events/router.py
"""EventRouter — match HA state changes against operator rules and act.

Gate order (spec §4): master enabled → quiet/maintenance → rule.enabled
→ cooldown (consumed by declines too) → min_clear. Every fire, decline,
and failure is audited and kept in per-rule runtime status.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from loguru import logger

from glados.events.actions.ha_action import HAActionError, execute_ha_action
from glados.events.announce import announce as default_announce
from glados.events.config import (
    EventRule,
    EventsConfig,
    EventsConfigError,
    load_events_config,
)
from glados.events.decision import Verdict, decide as default_decide
from glados.observability.audit import AuditEvent, Origin, audit


class EventRouter:
    def __init__(
        self,
        config_path: Path,
        call_service: Callable[..., dict],
        get_state: Callable[[str], str | None],
        decision_fn: Callable[..., Verdict] = default_decide,
        action_fn: Callable[..., None] = execute_ha_action,
        announce_fn: Callable[..., bool] = default_announce,
        quiet_check: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_path = config_path
        self._call_service = call_service
        self._get_state = get_state
        self._decision_fn = decision_fn
        self._action_fn = action_fn
        self._announce_fn = announce_fn
        self._quiet_check = quiet_check
        self._clock = clock
        self._lock = threading.Lock()
        self._config = EventsConfig(enabled=False, rules=[])
        self._load_error: str | None = None
        # Per-rule runtime state.
        self._last_attempt: dict[str, float] = {}   # fire OR decline ts
        self._clear_since: dict[str, float] = {}    # entity left trigger state
        self._status: dict[str, dict] = {}
        self._fire_count: dict[str, int] = {}

    # ── config lifecycle ─────────────────────────────────────────────

    def load(self) -> bool:
        """(Re)read events.yaml. On schema error: engine off, loud, True
        container health (spec §7)."""
        try:
            with self._lock:
                self._config = load_events_config(self._config_path)
                self._load_error = None
            logger.success(
                "events: loaded {} rule(s), engine {}",
                len(self._config.rules),
                "enabled" if self._config.enabled else "DISABLED",
            )
            return True
        except EventsConfigError as exc:
            with self._lock:
                self._config = EventsConfig(enabled=False, rules=[])
                self._load_error = str(exc)
            logger.error("events: config error — engine disabled: {}", exc)
            return False

    # ── event handling ───────────────────────────────────────────────

    def handle_state_changed(self, data: dict) -> None:
        entity_id = data.get("entity_id") or ""
        new = (data.get("new_state") or {}).get("state")
        old = (data.get("old_state") or {}).get("state")
        with self._lock:
            cfg = self._config
        for rule in cfg.rules:
            if rule.trigger.entity_id != entity_id:
                continue
            # Track when the entity LEFT the trigger state (for min_clear).
            if old == rule.trigger.to_state and new != rule.trigger.to_state:
                self._clear_since[rule.id] = self._clock()
                continue
            if new != rule.trigger.to_state:
                continue
            if rule.trigger.from_state is not None and old != rule.trigger.from_state:
                continue
            self._gate_and_run(rule, cfg)

    def _gate_and_run(self, rule: EventRule, cfg: EventsConfig) -> None:
        now = self._clock()
        if not cfg.enabled:
            return
        if self._quiet_check():
            logger.success("events: {} suppressed (quiet/maintenance mode)", rule.id)
            return
        if not rule.enabled:
            return
        last = self._last_attempt.get(rule.id, 0.0)
        if last and now - last < rule.cooldown_s:
            return
        if rule.min_clear_s > 0:
            cleared = self._clear_since.get(rule.id)
            if cleared is not None and now - cleared < rule.min_clear_s:
                return
        self._last_attempt[rule.id] = now
        self._execute(rule, manual=False)

    # ── execution (shared by live events and the Fire button) ───────

    def _execute(self, rule: EventRule, *, manual: bool) -> dict:
        verdict = Verdict(act=True, reason="mode: always")
        if rule.mode == "llm":
            verdict = self._decision_fn(rule, self._get_state)
        if not verdict.act:
            self._record(rule, "declined", verdict.reason)
            return {"result": "declined", "reason": verdict.reason}
        try:
            self._action_fn(rule.action, self._call_service)
        except HAActionError as exc:
            self._record(rule, "error", str(exc))
            return {"result": "error", "reason": str(exc)}
        self._fire_count[rule.id] = self._fire_count.get(rule.id, 0) + 1
        self._record(rule, "fired", verdict.reason, manual=manual)
        if rule.announce and rule.announce_speaker:
            text = verdict.quip if rule.mode == "llm" else (rule.announce_text or "")
            if text:
                self._announce_fn(text, rule.announce_speaker, self._call_service)
        return {"result": "fired", "reason": verdict.reason}

    def _record(self, rule: EventRule, result: str, reason: str, *, manual: bool = False) -> None:
        self._status[rule.id] = {
            "last_result": result,
            "last_reason": reason,
            "last_ts": self._clock(),
        }
        level = logger.error if result == "error" else logger.success
        level("events: rule {} → {} ({}){}", rule.id, result, reason,
              " [manual]" if manual else "")
        try:
            audit(AuditEvent(
                ts=self._clock(),
                origin=Origin.EVENT_RULE,
                kind=f"event_{result}",
                detail=f"{rule.id}: {reason}",
            ))
        except Exception:           # audit must never break dispatch
            logger.warning("events: audit emit failed for rule {}", rule.id)

    # ── WebUI surface ────────────────────────────────────────────────

    def run_rule(self, rule_id: str, *, dry_run: bool) -> dict:
        """Dry-run (decision only) or manual Fire (bypasses gates —
        operator-initiated, so the unattended-action rule is satisfied)."""
        with self._lock:
            rule = next((r for r in self._config.rules if r.id == rule_id), None)
        if rule is None:
            return {"error": f"no rule {rule_id!r}"}
        if dry_run:
            verdict = (
                self._decision_fn(rule, self._get_state)
                if rule.mode == "llm"
                else Verdict(act=True, reason="mode: always")
            )
            return {"verdict": {"act": verdict.act, "reason": verdict.reason,
                                "quip": verdict.quip}}
        return self._execute(rule, manual=True)

    def status(self) -> dict:
        with self._lock:
            cfg = self._config
            err = self._load_error
        return {
            "enabled": cfg.enabled,
            "load_error": err,
            "rules": [
                {
                    **r.model_dump(),
                    **self._status.get(r.id, {"last_result": "never",
                                              "last_reason": "", "last_ts": 0.0}),
                    "fire_count": self._fire_count.get(r.id, 0),
                }
                for r in cfg.rules
            ],
        }
```

If `AuditEvent`'s constructor fields differ from `(ts, origin, kind, detail)`, match the real dataclass in `audit.py` (check its definition while implementing; keep `origin=Origin.EVENT_RULE` and put the rule id + reason in whatever free-text field exists). The audit emit is wrapped so a mismatch can never break dispatch, but the test suite must exercise the real signature — add an assertion in the router test if the field names differ.

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_router.py -q` → 15 passed. Then the full suite: `python -m pytest -q` → no regressions.

- [ ] **Step 5: Commit**

```bash
git add glados/events/router.py tests/events/test_events_router.py
git commit -m "feat(events): EventRouter — gates, dispatch, status, dry-run/fire"
```

---

### Task 7: Engine wiring + example config

**Files:**
- Modify: `glados/core/engine.py` (next to the ha_sensor wiring around line ~1131; mode-change handler around line ~1228)
- Create: `configs/events.example.yaml`
- Test: `tests/events/test_events_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_events_wiring.py
"""Engine stands up the router iff the HA client singleton exists."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from glados.events import get_router, set_router
from glados.events.router import EventRouter


def teardown_function():
    set_router(None)


def test_init_event_router_subscribes_to_ha_client(tmp_path):
    from glados.core import engine as engine_mod
    client = MagicMock()
    client.call_service.return_value = {}
    cache = MagicMock()
    cache.get.return_value = None
    (tmp_path / "events.yaml").write_text("enabled: true\nrules: []\n", encoding="utf-8")
    with patch.object(engine_mod, "_ha_get_client", return_value=client), \
         patch.object(engine_mod, "_ha_get_cache", return_value=cache), \
         patch.object(engine_mod, "_events_config_path", return_value=tmp_path / "events.yaml"):
        engine_mod.init_event_router(quiet_check=lambda: False)
    router = get_router()
    assert isinstance(router, EventRouter)
    client.on_state_changed.assert_called_once_with(router.handle_state_changed)


def test_init_event_router_no_ha_client_stays_off(tmp_path):
    from glados.core import engine as engine_mod
    with patch.object(engine_mod, "_ha_get_client", return_value=None):
        engine_mod.init_event_router(quiet_check=lambda: False)
    assert get_router() is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (`AttributeError: ... no attribute 'init_event_router'`).

- [ ] **Step 3: Implement**

In `glados/core/engine.py`, add module-level helpers (so tests can patch them) and the init function:

```python
# Near the other imports:
from glados.ha import get_client as _ha_get_client, get_cache as _ha_get_cache


def _events_config_path():
    from glados.events.config import default_events_path
    return default_events_path()


def init_event_router(quiet_check) -> None:
    """Stand up the event→action engine on the existing HAClient.

    No HA client (HA_TOKEN unset) → engine stays off, loudly. A config
    error disables the engine but never the container (spec §7).
    """
    from glados.events import set_router
    from glados.events.router import EventRouter

    client = _ha_get_client()
    if client is None:
        logger.error(
            "events: HA client not initialized — event→action engine OFF "
            "(set HA_TOKEN / check HA connectivity)"
        )
        return
    cache = _ha_get_cache()

    def get_state(entity_id: str) -> str | None:
        ent = cache.get(entity_id) if cache else None
        return ent.state if ent else None

    router = EventRouter(
        config_path=_events_config_path(),
        call_service=client.call_service,
        get_state=get_state,
        quiet_check=quiet_check,
    )
    router.load()
    client.on_state_changed(router.handle_state_changed)
    set_router(router)
    logger.success("events: router subscribed to HA state stream")
```

Call it from engine init, right after the ha_sensor subagent registration block (engine.py ~line 1149):

```python
        # Event→action engine (spec 2026-06-09). Quiet check follows the
        # silent-mode flag maintained by the mode-change handler below.
        init_event_router(quiet_check=lambda: getattr(self, "_events_quiet", False))
```

In the mode-change handler (the method whose signature includes `maintenance_mode: bool, maintenance_speaker: str, silent_mode: bool` at engine.py ~1228), add one line at the top of the method body:

```python
        self._events_quiet = silent_mode
```

Create `configs/events.example.yaml`:

```yaml
# Event→Action engine rules — copy to events.yaml on the config volume.
# Spec: docs/superpowers/specs/2026-06-09-event-action-engine-design.md
# HA executes (automations/scripts/scenes); GLaDOS decides whether/when.
# Placeholder entity ids — replace with your own. Rules ship DISABLED.
enabled: true
rules:
  - id: hallway_dark_person
    enabled: false
    trigger:
      entity_id: binary_sensor.hallway_person_detected
      to_state: "on"
    mode: llm
    context_entities:
      - sensor.hallway_lux
      - sun.sun
    decision_prompt: >
      Turn on the hallway light only if the hallway is dark.
    action:
      kind: ha_automation
      target: automation.hallway_light_on
    cooldown_s: 120
    min_clear_s: 30
    announce: false
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_wiring.py -q` → 2 passed; full suite green.

- [ ] **Step 5: Commit**

```bash
git add glados/core/engine.py configs/events.example.yaml tests/events/test_events_wiring.py
git commit -m "feat(events): engine wiring on existing HAClient + example config"
```

---

### Task 8: Admin REST API

**Files:**
- Modify: `glados/webui/tts_ui.py` (route chains in `do_GET`/`do_POST`/`do_PUT`/`do_DELETE`; follow the `require_perm(self, "admin")` pattern at tts_ui.py:1891)
- Test: `tests/events/test_events_api.py`

Routes (all admin; 401 unauthenticated / 403 non-admin via `require_perm`):

| Method+Path | Behavior |
|---|---|
| `GET /api/integrations/events` | `router.status()` (includes `load_error`); 503 JSON `{error}` if router is None. |
| `POST /api/integrations/events` | Create rule from JSON body. **`enabled` forced to `false` on create** (spec §1). Validate via `EventRule.model_validate` → 400 with the pydantic message on failure. Save + `router.load()`. |
| `PUT /api/integrations/events/<id>` | Replace rule (may set `enabled: true`). 404 unknown id, 400 invalid. Save + reload. |
| `DELETE /api/integrations/events/<id>` | Remove rule. 404 unknown id. Save + reload. |
| `POST /api/integrations/events/master` | Body `{"enabled": bool}` → flip master switch. Save + reload. |
| `GET /api/integrations/events/targets` | From `glados.ha.get_cache().snapshot()`: `{"automations": [...], "scripts": [...], "scenes": [...], "media_players": [...], "entities": [...]}` — each item `{"entity_id", "friendly_name"}`, grouped by `EntityState.domain` (`entities` = binary_sensor + sensor + sun + person, for trigger/context pickers). 503 if cache is None. |
| `POST /api/integrations/events/<id>/dry_run` | `router.run_rule(id, dry_run=True)`. |
| `POST /api/integrations/events/<id>/fire` | `router.run_rule(id, dry_run=False)` — real fire, bypasses gates (operator-initiated). |

Implementation notes:
- CRUD mutates via `load_events_config(path)` → modify → `save_events_config(path, cfg)` → `router.load()` so the file stays the single source of truth and the live router follows it.
- Path comes from `glados.events.config.default_events_path()`.
- The handlers live in helper functions (`_events_api_get`, `_events_api_post`, …) called from the route chains, mirroring how `_get_ha_entities()` (tts_ui.py:3987) is dispatched.

- [ ] **Step 1: Write the failing tests**

```python
# tests/events/test_events_api.py
"""REST CRUD round-trip onto a temp events.yaml + RBAC gating.

Follows the repo convention (tests/test_route_gating.py): handlers are
exercised at the Python level with a mocked request object — no HTTP
server is started.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from glados.events import set_router
from glados.events.config import EventsConfig, load_events_config
from glados.events.router import EventRouter
from glados.webui import tts_ui

RULE_BODY = {
    "id": "hallway",
    "enabled": True,                  # must be forced false on create
    "trigger": {"entity_id": "binary_sensor.hall_person", "to_state": "on"},
    "mode": "always",
    "action": {"kind": "ha_automation", "target": "automation.hall_light"},
}


@pytest.fixture
def events_env(tmp_path):
    path = tmp_path / "events.yaml"
    path.write_text("enabled: true\nrules: []\n", encoding="utf-8")
    router = EventRouter(
        config_path=path,
        call_service=lambda *a, **kw: {},
        get_state=lambda eid: None,
    )
    router.load()
    set_router(router)
    with patch.object(tts_ui, "_events_path", return_value=path):
        yield path, router
    set_router(None)


def _handler(body: dict | None = None):
    h = MagicMock()
    h._resolved_user = MagicMock(role="admin")
    if body is not None:
        h.rfile.read.return_value = json.dumps(body).encode()
        h.headers = {"Content-Length": str(len(h.rfile.read.return_value))}
    return h


def test_create_forces_disabled(events_env):
    path, router = events_env
    with patch.object(tts_ui, "require_perm", return_value=True):
        status, payload = tts_ui._events_api_create(_handler(), RULE_BODY)
    assert status == 200
    saved = load_events_config(path)
    assert saved.rules[0].id == "hallway"
    assert saved.rules[0].enabled is False          # forced on create


def test_create_invalid_returns_400(events_env):
    bad = {**RULE_BODY, "mode": "llm"}              # llm without decision_prompt
    with patch.object(tts_ui, "require_perm", return_value=True):
        status, payload = tts_ui._events_api_create(_handler(), bad)
    assert status == 400
    assert "decision_prompt" in payload["error"]


def test_update_can_enable(events_env):
    path, router = events_env
    with patch.object(tts_ui, "require_perm", return_value=True):
        tts_ui._events_api_create(_handler(), RULE_BODY)
        status, _ = tts_ui._events_api_update(
            _handler(), "hallway", {**RULE_BODY, "enabled": True})
    assert status == 200
    assert load_events_config(path).rules[0].enabled is True


def test_update_unknown_404(events_env):
    with patch.object(tts_ui, "require_perm", return_value=True):
        status, _ = tts_ui._events_api_update(_handler(), "ghost", RULE_BODY)
    assert status == 404


def test_delete_round_trip(events_env):
    path, _ = events_env
    with patch.object(tts_ui, "require_perm", return_value=True):
        tts_ui._events_api_create(_handler(), RULE_BODY)
        status, _ = tts_ui._events_api_delete(_handler(), "hallway")
    assert status == 200
    assert load_events_config(path).rules == []


def test_master_toggle(events_env):
    path, _ = events_env
    with patch.object(tts_ui, "require_perm", return_value=True):
        status, _ = tts_ui._events_api_master(_handler(), {"enabled": False})
    assert status == 200
    assert load_events_config(path).enabled is False


def test_targets_grouped_by_domain(events_env):
    ent = lambda eid, dom: MagicMock(entity_id=eid, friendly_name=eid, domain=dom)
    cache = MagicMock()
    cache.snapshot.return_value = [
        ent("automation.hall_light", "automation"),
        ent("script.evening", "script"),
        ent("scene.movie", "scene"),
        ent("media_player.kitchen", "media_player"),
        ent("binary_sensor.hall_person", "binary_sensor"),
        ent("light.hallway", "light"),
    ]
    with patch.object(tts_ui, "require_perm", return_value=True), \
         patch.object(tts_ui, "_events_get_cache", return_value=cache):
        status, payload = tts_ui._events_api_targets(_handler())
    assert status == 200
    assert payload["automations"][0]["entity_id"] == "automation.hall_light"
    assert payload["media_players"][0]["entity_id"] == "media_player.kitchen"
    assert {"entity_id": "binary_sensor.hall_person", "friendly_name": "binary_sensor.hall_person"} in payload["entities"]


def test_rbac_denied_short_circuits(events_env):
    with patch.object(tts_ui, "require_perm", return_value=False) as rp:
        result = tts_ui._events_api_create(_handler(), RULE_BODY)
    assert result is None                # handler already wrote 401/403
    rp.assert_called_once()
```

- [ ] **Step 2: Run to verify failure** — FAIL (`AttributeError: module ... has no attribute '_events_api_create'`).

- [ ] **Step 3: Implement**

In `tts_ui.py`, add module-level helpers (signatures the tests pin):

```python
def _events_path():
    from glados.events.config import default_events_path
    return default_events_path()


def _events_get_cache():
    from glados.ha import get_cache
    return get_cache()


def _events_reload():
    from glados.events import get_router
    router = get_router()
    if router:
        router.load()


def _events_api_create(handler, body: dict):
    if not require_perm(handler, "admin"):
        return None
    from glados.events.config import EventRule, load_events_config, save_events_config
    try:
        body = dict(body)
        body["enabled"] = False                     # rules are born disabled
        rule = EventRule.model_validate(body)
    except Exception as exc:
        return 400, {"error": str(exc)}
    path = _events_path()
    cfg = load_events_config(path)
    if any(r.id == rule.id for r in cfg.rules):
        return 400, {"error": f"rule id {rule.id!r} already exists"}
    cfg.rules.append(rule)
    save_events_config(path, cfg)
    _events_reload()
    return 200, {"ok": True, "id": rule.id}
```

`_events_api_update` / `_events_api_delete` / `_events_api_master` / `_events_api_targets` / `_events_api_status` / `_events_api_run(handler, rule_id, dry_run)` follow the same shape: `require_perm` first (return None when denied), mutate via load/save, `_events_reload()`, return `(status, payload)` tuples. `_events_api_targets` groups `cache.snapshot()` by `EntityState.domain`: `automation→automations`, `script→scripts`, `scene→scenes`, `media_player→media_players`, and `binary_sensor|sensor|sun|person → entities`, each item `{"entity_id": e.entity_id, "friendly_name": e.friendly_name}`. `_events_api_run` calls `glados.events.get_router().run_rule(rule_id, dry_run=dry_run)` (503 when router is None).

Wire the route chains: in `do_GET` add `/api/integrations/events` and `/api/integrations/events/targets`; in `do_POST` add `/api/integrations/events`, `/api/integrations/events/master`, and the regex-matched `/api/integrations/events/<id>/dry_run|fire`; add the `/api/integrations/events/<id>` cases to `do_PUT` and `do_DELETE` (create `do_PUT`/`do_DELETE` dispatch entries following the existing pattern if those verbs lack a chain for this prefix). Each route parses the JSON body, calls its helper, and writes the `(status, payload)` JSON response with the handler's existing JSON-write helper.

- [ ] **Step 4: Run tests** — `python -m pytest tests/events/test_events_api.py -q` → 9 passed; full suite green.

- [ ] **Step 5: Commit**

```bash
git add glados/webui/tts_ui.py tests/events/test_events_api.py
git commit -m "feat(events): admin REST — rule CRUD, targets, dry-run, fire, master"
```

---

### Task 9: WebUI — Integrations → Events page

**Files:**
- Create: `glados/webui/pages/events.py`
- Modify: `glados/webui/pages/_shell.py` (nav entry), `glados/webui/static/ui.js`, `glados/webui/static/style.css`

No JS test harness exists in this repo — the API surface is covered by Task 8's tests; this task ships the page and verifies manually (matching how the Plugins page landed in Change 32/33).

- [ ] **Step 1: Page shell**

`glados/webui/pages/events.py` — follow the established convention (`.page-shell > .container > .page-header > h2.page-title`, then cards; see any recent page module for the exact wrapper):

```python
"""Integrations → Events — event→action rule editor (admin-only)."""

HTML = """
<div class="page-shell" id="page-events">
  <div class="container">
    <div class="page-header">
      <h2 class="page-title">Events</h2>
      <p class="page-subtitle">When something happens, GLaDOS decides — and Home Assistant does.</p>
    </div>
    <div class="card" id="ev-master-card">
      <div class="section-title">Engine</div>
      <label class="switch"><input type="checkbox" id="ev-master-toggle"><span>Event engine enabled</span></label>
      <div id="ev-load-error" class="text-danger" hidden></div>
    </div>
    <div class="card">
      <div class="section-title">Rules</div>
      <div id="ev-rule-list"></div>
      <button class="btn" id="ev-add-rule">Add rule</button>
    </div>
    <div class="card" id="ev-editor-card" hidden>
      <div class="section-title" id="ev-editor-title">New rule</div>
      <div id="ev-editor-form"></div>
    </div>
  </div>
</div>
"""
```

- [ ] **Step 2: Nav entry**

In `glados/webui/pages/_shell.py`, inside the admin-gated Configuration `.nav-children` group (next to `data-nav-key="config.integrations"`, shell.py:46), add:

```html
<a class="nav-item" data-nav-key="config.events" href="#events">Events</a>
```

and register the page HTML in whatever page-map structure `_shell.py`/the page loader uses for the other `pages/*.py` modules (mirror the Integrations page registration exactly).

- [ ] **Step 3: ui.js**

Add an `evInit()` section following the conventions of the Plugins page functions. Required behaviors (complete list — implement all):

- `evLoad()` — `GET /api/integrations/events` → render master toggle, `load_error` banner, and the rule list: per rule a row with enable toggle (PUT with flipped `enabled`), id, trigger summary (`entity → state`), action target, mode badge, status line (`last_result` + `last_reason` + relative `last_ts` + `fire_count`), and buttons Edit / Delete / Dry-run / Fire.
- `evRenderEditor(rule|null)` — form fields: id (text, locked when editing), trigger entity (`<select>` from targets `entities` + free-text fallback), to_state, from_state, mode select (`always`/`llm`), decision_prompt textarea (shown for `llm`), context entities multi-select (from `entities`), action kind select, target select (options switch between `automations`/`scripts`/`scenes` lists by kind; free-form domain.service + entity_id + JSON data fields for `ha_service`), cooldown_s, min_clear_s, announce checkbox, announce_speaker select (from `media_players`), announce_text (shown when announce && mode=always). Save → POST (create) or PUT (edit) → `evLoad()`; render the 400 error message inline on validation failure.
- `evDryRun(id)` — POST dry_run → show the verdict (`act` / `reason` / `quip`) in the rule's status line.
- `evFire(id)` — `confirm("Really fire this rule's action now?")` then POST fire → `evLoad()`.
- Master toggle → POST `/api/integrations/events/master`.
- Targets fetched once per page-open from `GET /api/integrations/events/targets`.

- [ ] **Step 4: style.css** — only if a needed utility is missing; the v3 utility-class layer should cover badges, rows, and the danger banner. Add at most a `#ev-rule-list .ev-row` flex rule.

- [ ] **Step 5: Verify**

Run: `python -m pytest -q` (no regressions — page modules are import-checked by existing webui tests). Manual checklist (executed at deploy, Task 10): page renders, pickers populate from live HA, create→appears disabled, dry-run shows verdict, fire flips the target, non-admin gets 403.

- [ ] **Step 6: Commit**

```bash
git add glados/webui/pages/events.py glados/webui/pages/_shell.py glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "feat(events): Integrations → Events WebUI — rule editor, dry-run, fire"
```

---

### Task 10: Docs + deploy + live verification

**Files:**
- Modify: `docs/CHANGES.md` (next Change number), `docs/roadmap.md` (mark event-action acting-side shipped; announce-side cascade stays queued), `README.md` (one short paragraph under Architecture)

- [ ] **Step 1: Write docs** — CHANGES entry covering: new `glados/events/` package, Origin.EVENT_RULE, REST surface, WebUI page, `configs/events.example.yaml`, the spec link, and the explicit non-goals (announce cascade, MQTT, watcher consolidation).

- [ ] **Step 2: Full suite + push + PR**

```bash
python -m pytest -q          # expect ~1965+ passed / 5 skipped, 0 fail
git push -u origin feat/event-action-engine
# PR via gh (PAT in SESSION_STATE.md §Credentials), wait for CI, merge.
```

- [ ] **Step 3: Deploy** — GHCR build on main → `scripts/deploy_ghcr.py` (env vars per SESSION_STATE.md). Copy `events.example.yaml` → `events.yaml` on the config volume only if the operator wants the starter file; otherwise the engine boots with defaults (on, zero rules).

- [ ] **Step 4: Live verification (operator-assisted)**
  1. Boot log shows `events: router subscribed to HA state stream`.
  2. Operator creates the hallway rule in the WebUI with their real entity ids (real ids never enter the repo).
  3. Dry-run in current light conditions → sane verdict + reason.
  4. Enable the rule; walk into the hallway in the dark → light turns on; WebUI shows `fired` + reason; docker log line `events: rule … → fired`.
  5. Repeat in daylight → `declined` with a lux-based reason.

- [ ] **Step 5: Commit docs + update SESSION_STATE.md handoff**

```bash
git add docs/CHANGES.md docs/roadmap.md README.md
git commit -m "docs(events): Change NN — event→action engine acting side"
```

---

## Self-Review (completed 2026-06-10)

- **Spec coverage:** §1 substrate→T6/T7, ha_action→T4, WebUI editor→T8/T9, visibility→T1/T6; §3 decision slot→T3; §4 schema+gates→T2/T6; §5 announce→T5; §6 REST/RBAC/dry-run-vs-fire→T8/T9; §7 error rows→T2 (malformed), T3 (LLM), T4 (HA call), T5 (announce), T6 (status), T7 (no client / config error); §8 testing list→Tasks 1-8 test files. Maintenance-mode gate→T6 quiet_check + T7 silent-mode flag. Gaps: none.
- **Placeholder scan:** Task 8 Step 3 describes sibling helpers by pattern with one complete exemplar (`_events_api_create`) and pins every signature via the tests in Step 1; Task 9 lists complete behavior inventories for JS (no repo JS harness exists). No TBD/TODO markers.
- **Type consistency:** `EventActionSpec.kind` literals match between T2/T4/T6; `Verdict(act, reason, quip)` consistent T3/T6; `run_rule(rule_id, dry_run=...)` consistent T6/T8; `call_service(domain, service, service_data=, target=, timeout_s=)` consistent T4/T5/T6 with `ws_client.py:164`; status dict keys (`last_result/last_reason/last_ts/fire_count`) consistent T6/T8/T9.
- **Known judgment points for the implementer:** exact `AuditEvent` field names (T6 note); exact JSON-write helper name in `tts_ui.py` route wiring (T8); page-registration mechanics in `_shell.py` (T9 — mirror the Integrations page).
