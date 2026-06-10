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
